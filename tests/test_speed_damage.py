"""The batched (numpy) versions of the damage step's per-vertex and per-triangle
loops give exactly what the original loops gave: Grid.nearest_many,
Uv1Field.lookup_many, DamagePlan._disp_prefetch and _uv1_prefetch,
zone_maps and zone_votes. Each reference below is the original code, kept here
only to compare against; the inputs are random scenes with the cases the
batched paths must get right (copies of points, points on cell boundaries,
equal distances and scores).

uv run python tests/test_speed_damage.py     # or: uv run python -m tests speed_damage
"""

from __future__ import annotations

import math
import random
import struct
from collections import Counter
from types import SimpleNamespace

from dsfforza.convert import damage

# --- Grid.nearest ---------------------------------------------------------------------


def _nearest_reference(grid: damage.Grid, p, k: int):
    """The original ring-by-ring search over every cell of each ring."""
    if not grid.points:
        return []
    cx, cy, cz = grid._key(p)
    found = []
    r = 0
    while True:
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    if max(abs(dx), abs(dy), abs(dz)) != r:
                        continue
                    for n in grid.cells.get((cx + dx, cy + dy, cz + dz), ()):
                        found.append((math.dist(p, grid.points[n]), n))
        if len(found) >= min(k, len(grid.points)) and sorted(found)[min(k, len(found)) - 1][0] <= r * grid.cell:
            break
        if len(found) == len(grid.points):
            break
        r += 1
    found.sort()
    return found[:k]


def _point_scene(rng: random.Random, cell: float):
    """Points in dense clusters and a sparse spread, with copies of positions (the
    host's split vertices), some on cell corners; and query points on them, on
    cell corners, near and far."""
    centres = [tuple(rng.uniform(-1, 1) for _ in range(3)) for _ in range(rng.randint(1, 4))]
    pts = []
    for _ in range(rng.choice([1, 2, 5, 17, 80, 400, 1500])):
        c, s = rng.choice(centres), rng.choice([0.01, 0.05, 0.2, 0.6])
        pts.append(tuple(c[a] + rng.gauss(0, s) for a in range(3)))
    if rng.random() < 0.3:
        pts = [tuple(round(x / cell) * cell for x in p) for p in pts]
    if rng.random() < 0.6:
        pts += [rng.choice(pts) for _ in range(len(pts) // 2 + 1)]
        rng.shuffle(pts)
    qs = []
    for _ in range(rng.choice([1, 40, 150])):
        kind = rng.random()
        if kind < 0.2:
            qs.append(rng.choice(pts))
        elif kind < 0.3:
            qs.append(tuple(rng.randint(-15, 15) * cell for _ in range(3)))
        elif kind < 0.8:
            p = rng.choice(pts)
            qs.append(tuple(x + rng.gauss(0, 0.05) for x in p))
        else:
            qs.append(tuple(rng.uniform(-2.5, 2.5) for _ in range(3)))
    return pts, qs


def test_nearest_many_matches_nearest():
    """Grid.nearest_many against nearest and the original ring search, under
    several settings: the default, small chunks, a first look at ring 0, 1 or 4,
    mostly the cube of offsets or always every occupied cell, no one-by-one."""
    rng = random.Random(41)
    saved = damage.NEAREST_RINGS, damage.NEAREST_CHUNK, damage.NEAREST_CUBE, damage.NEAREST_FEW
    settings = [saved, (2, 50, 4, 32), (0, 400_000, 64, 0), (4, 1000, 0, 0), (1, 300, 4, 1)]
    try:
        for scene in range(40):
            cell = rng.choice([0.06, 0.08])
            pts, qs = _point_scene(rng, cell)
            grid = damage.Grid(pts, list(range(len(pts))), cell=cell)
            for k in (1, 6, 16):
                want = [grid.nearest(q, k) for q in qs]
                if scene % 5 == 0 and k == 6:  # nearest itself against the ring search (a few: it is slow)
                    assert want[:8] == [_nearest_reference(grid, q, k) for q in qs[:8]]
                for setting in settings:
                    damage.NEAREST_RINGS, damage.NEAREST_CHUNK, damage.NEAREST_CUBE, damage.NEAREST_FEW = setting
                    fresh = damage.Grid(pts, list(range(len(pts))), cell=cell)
                    assert fresh.nearest_many(qs, k) == want, (scene, k, setting)
    finally:
        damage.NEAREST_RINGS, damage.NEAREST_CHUNK, damage.NEAREST_CUBE, damage.NEAREST_FEW = saved
    assert damage.Grid([], []).nearest_many([(0.0, 0.0, 0.0)]) == [[]]
    assert damage.Grid([(0.0, 0.0, 0.0)], [0]).nearest_many([]) == []


def test_nearest_many_on_exact_ring_distances():
    """Points exactly t * cell away along an axis (the stop test's <= decides),
    and a k-th and (k + 1)-th at the same distance from different positions."""
    saved = damage.NEAREST_FEW
    try:
        damage.NEAREST_FEW = 0
        cell = 0.08
        pts = [(t * cell, 0.0, 0.0) for t in range(-6, 7)] + [(0.0, t * cell, 0.0) for t in range(-6, 7)]
        pts += [(0.0, 0.0, t * cell) for t in range(-6, 7)] + [(0.0, 0.0, 0.0)] * 3
        grid = damage.Grid(pts, list(range(len(pts))), cell=cell)
        qs = [(0.0, 0.0, 0.0), (cell / 2, 0.0, 0.0), (1e-17, 0.0, 0.0), (0.0, -cell, 0.0), (3 * cell, 3 * cell, 0.0)]
        for k in (1, 3, 4, 6, 7, 16):
            assert (
                grid.nearest_many(qs, k)
                == [grid.nearest(q, k) for q in qs]
                == [_nearest_reference(grid, q, k) for q in qs]
            )
    finally:
        damage.NEAREST_FEW = saved


# --- Uv1Field.lookup --------------------------------------------------------------------


def _lookup_reference(field: damage.Uv1Field, q, pair, material, local, any_panel=False, normal=None, chart=None):
    """The original Uv1Field.lookup (Grid.nearest is tested against the ring search above)."""
    keys = (
        ([("chart", chart)] if chart is not None else [])
        + ([] if any_panel else [("pm", pair, material), ("pc", pair, local)])
        + [("c", local), ("all",), ("degenerate",), ("tiled",)]
    )
    for key in keys:
        grid = field.groups.get(key)
        if grid is None:
            continue
        best = None
        for _, n in grid.nearest(q, 16):
            t = field.tris[grid.values[n]]
            d, bary = damage.closest_on_triangle(q, *t[0])
            score = math.sqrt(d) + (damage.FACING_WEIGHT * (1 - damage._dot(normal, t[5])) if normal else 0.0)
            if best is None or score < best[0]:
                best = (score, bary, t[1], field.chart[grid.values[n]])
        if best is not None:
            bary, uvs = best[1], best[2]
            return (
                (sum(bary[k] * uvs[k][0] for k in range(3)), sum(bary[k] * uvs[k][1] for k in range(3))),
                best[3],
            )
    return None


PAIRS = [(0, 0), (1, 2), (3, 4), (5, 6)]
MATERIALS = [b"m0", b"m1", b"m2"]


def _uv1_scene(rng: random.Random) -> damage.Uv1Field:
    """Host UV1 triangles: clusters and a sparse spread over panels and materials,
    charts joined through shared corners, exact copies (equal scores, decided by
    the nearest order), coplanar neighbours, degenerate and tiled UVs."""
    tris = []
    for _ in range(rng.choice([3, 60, 400, 1200])):
        c = rng.choice([(0.5, 0.2, -0.3), (-0.6, 0.1, 0.4)]) if rng.random() < 0.6 else (0, 0, 0)
        spread = 0.15 if c != (0, 0, 0) else 0.9
        a = tuple(c[i] + rng.uniform(-spread, spread) for i in range(3))
        size = 0.3 if rng.random() < 0.05 else 0.04
        corners = [a] + [tuple(a[i] + rng.uniform(-size, size) for i in range(3)) for _ in range(2)]
        if tris and rng.random() < 0.3:  # shares a corner with an earlier triangle (one chart)
            corners[0] = rng.choice(tris)[0][rng.randrange(3)]
        corners = tuple(corners)
        normal = damage._norm(damage._cross(damage._sub(corners[1], corners[0]), damage._sub(corners[2], corners[0])))
        if normal is None:
            continue
        kind = rng.random()
        if kind < 0.05:  # degenerate UV
            u0 = (rng.random(), rng.random())
            uvs = (u0, u0, (u0[0] + 0.001, u0[1]))
        elif kind < 0.1:  # tiled
            uvs = tuple((rng.uniform(-4, 5), rng.uniform(-4, 5)) for _ in range(3))
        else:
            uvs = tuple((rng.random(), rng.random()) for _ in range(3))
        pair, material = rng.choice(PAIRS), rng.choice(MATERIALS)
        tris.append((corners, uvs, pair, material, material == b"m0", normal))
        if rng.random() < 0.1:  # a copy on another panel
            tris.append((corners, uvs, rng.choice(PAIRS), material, material == b"m0", normal))
        if rng.random() < 0.05:  # a coplanar neighbour across an edge
            far = tuple(corners[1][i] + corners[2][i] - corners[0][i] for i in range(3))
            tris.append(((corners[2], corners[1], far), uvs, pair, material, material == b"m0", normal))
    return damage.Uv1Field(tris, damage.Box((-1, -1, -1), (1, 1, 1)))


def _uv1_items(rng: random.Random, field: damage.Uv1Field, count: int) -> list:
    items = []
    charts = sorted(set(field.chart))
    for _ in range(count):
        kind = rng.random()
        if kind < 0.3 and field.tris:
            pts = rng.choice(field.tris)[0]
            q = rng.choice(pts) if rng.random() < 0.5 else tuple(sum(p[a] for p in pts) / 3 for a in range(3))
        elif kind < 0.4:
            q = tuple(rng.randint(-15, 15) * 0.06 for _ in range(3))
        else:
            q = tuple(rng.uniform(-1.3, 1.3) for _ in range(3))
        normal = damage._norm(tuple(rng.uniform(-1, 1) for _ in range(3))) if rng.random() < 0.8 else None
        chart = rng.choice(charts + [-1]) if charts and rng.random() < 0.2 else None
        pair = rng.choice(PAIRS + [(9, 9)])
        material = rng.choice(MATERIALS + [b"none"])
        items.append((q, pair, material, rng.random() < 0.5, rng.random() < 0.2, normal, chart))
    return items


def test_lookup_many_matches_lookup():
    rng = random.Random(43)
    saved = damage.NEAREST_FEW
    try:
        for scene in range(8):
            field = _uv1_scene(rng)
            items = _uv1_items(rng, field, 200)
            want = [field.lookup(*item) for item in items]
            assert want == [_lookup_reference(field, *item) for item in items]
            for few in (32, 0):
                damage.NEAREST_FEW = few
                fresh = damage.Uv1Field(field.tris, field.box)
                assert fresh.lookup_many(items) == want, (scene, few)
    finally:
        damage.NEAREST_FEW = saved
    assert damage.Uv1Field([], damage.Box((-1, -1, -1), (1, 1, 1))).lookup_many(items[:3]) == [None] * 3


def test_lookup_many_equal_scores_go_by_nearest_order():
    """Two copies of a triangle with different UVs: the same score, and the one
    nearest's order has first wins (lookup keeps a best only on a lower score)."""
    corners = ((0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (0.0, 0.1, 0.0))
    up = (0.0, 0.0, 1.0)
    tris = [
        (corners, ((0.1, 0.1), (0.2, 0.1), (0.1, 0.2)), (1, 2), b"m1", False, up),
        (corners, ((0.6, 0.6), (0.7, 0.6), (0.6, 0.7)), (1, 2), b"m1", False, up),
    ]
    field = damage.Uv1Field(tris, damage.Box((-1, -1, -1), (1, 1, 1)))
    items = [((0.02, 0.03, z), (1, 2), b"m1", False, False, up, None) for z in (0.0, 0.05, -0.2)] * 12
    want = [_lookup_reference(field, *item) for item in items]
    assert all(w[0][0] < 0.5 for w in want)  # the first copy's UVs
    assert field.lookup_many(items) == want


# --- DamagePlan._disp, _uv1 -------------------------------------------------------------


def _disp_reference(plan, f: damage.HostField, pair, p) -> tuple:
    """The original DamagePlan._disp."""
    scale = f.box.scale_to(plan.forza_box)
    src, flip = plan._source(f, pair)
    grid = f.by_pair[src] if src is not None else f.all_grid
    q = plan._forza_norm(p)
    hits = grid.nearest((-q[0], q[1], q[2]) if flip else q, plan.opts.k)
    wsum, d = 0.0, [0.0, 0.0, 0.0]
    for dist, n in hits:
        w = 1.0 / (dist + 0.02) ** 2
        disp = f.disp[grid.values[n]]
        if flip:
            disp = (-disp[0], disp[1], disp[2])
        for a in range(3):
            d[a] += w * disp[a]
        wsum += w
    return tuple(d[a] / wsum * scale[a] for a in range(3)) if wsum else (0.0, 0.0, 0.0)


def _plan(f2d=None):
    plan = object.__new__(damage.DamagePlan)
    plan.opts = damage.DamageOptions()
    plan.forza_box = damage.Box((-1.1, -0.7, -2.3), (1.0, 0.8, 2.2))
    plan.f2d = f2d
    # Doors 31 (-x) and 32 (+x): the +x door has a record but no host vertices, so
    # it borrows the -x door's, mirrored.
    plan.panels = {
        (0, 0): damage.Panel((0, 0), [], 0, 38),
        (1, 2): damage.Panel((1, 2), [], 1, 31),
        (3, 4): damage.Panel((3, 4), [], 2, 32),
        (5, 6): damage.Panel((5, 6), [], 3, 4),
    }
    return plan


def _host_field(rng: random.Random) -> damage.HostField:
    points, disp, pairs = [], [], []
    for _ in range(rng.choice([30, 400, 2000])):
        c = rng.choice([(0.5, 0.2, -0.3), (-0.6, 0.1, 0.4), (0.0, 0.0, 0.0)])
        p = tuple(c[a] + rng.gauss(0, 0.3) for a in range(3))
        for _ in range(rng.choice([1, 1, 2, 3])):  # split vertices: copies of a position
            points.append(p)
            disp.append(tuple(rng.gauss(0, 0.05) for _ in range(3)))
            pairs.append(rng.choice([(0, 0), (1, 2), (5, 6)]))
    f = damage.HostField(points, disp, pairs, damage.Box.of(points))
    groups = {}
    for n, pair in enumerate(pairs):
        groups.setdefault(pair, []).append(n)
    for pair, ns in groups.items():
        f.by_pair[pair] = damage.Grid([f.normalise(points[n]) for n in ns], ns)
    f.all_grid = damage.Grid([f.normalise(p) for p in points], list(range(len(points))))
    return f


def test_disp_prefetch_matches_disp():
    rng = random.Random(47)
    for _ in range(4):
        f = _host_field(rng)
        plan = _plan()
        keys = []
        for _ in range(300):
            p = rng.choice(f.points) if rng.random() < 0.3 else tuple(rng.uniform(-1.5, 1.5) for _ in range(3))
            keys.append((rng.choice([(0, 0), (1, 2), (3, 4), (5, 6), (7, 8)]), p))
        keys += keys[:20]  # asked twice
        plan._disp_prefetch(f, keys)
        cache = dict(plan._disp_cache)
        assert len(cache) == len({(id(f), pair, p) for pair, p in keys})
        fresh = _plan()
        for pair, p in keys:
            want = _disp_reference(fresh, f, pair, p)
            assert cache[(id(f), pair, p)] == want == fresh._disp(f, pair, p)


def test_uv1_prefetch_matches_uv1():
    rng = random.Random(53)
    field = _uv1_scene(rng)
    f = damage.HostField([], [], [], damage.Box((-1, -1, -1), (1, 1, 1)), uv1=field, local={b"m0"})
    f2d = SimpleNamespace(SHARED={"a": b"m1"}, SHARED_EXTERIOR={}, PAINT=b"m1")
    items = [
        (q, pair, material, any_panel, normal, chart)
        for q, pair, material, _, any_panel, normal, chart in _uv1_items(rng, field, 300)
    ]
    plan = _plan(f2d)
    plan._forza_norm = lambda p: tuple(x * 0.9 for x in p)
    plan._uv1_prefetch(f, items + items[:10])
    cache = dict(plan._uv1_cache)
    fresh = _plan(f2d)
    fresh._forza_norm = plan._forza_norm
    for p, pair, material, any_panel, normal, chart in items:
        want = _lookup_reference(
            field, fresh._forza_norm(p), pair, material, fresh._is_local(f, material), any_panel, normal, chart
        )
        got = cache[(id(f), pair, material, p, any_panel, normal, chart)]
        assert got == want == fresh._uv1(f, p, pair, material, any_panel, normal, chart)


# --- zone_maps, zone_vote --------------------------------------------------------------


def _zone_maps_reference(body, place, lods, zones=damage.EXPOSED_ZONES) -> dict:
    """The original zone_maps: every sample of every triangle, one at a time."""
    maps = {}
    for kind, is_panel, axis, sign, which, _ in zones:
        across = [a for a in range(3) if a != axis]
        pick = max if which == "inner" else min
        secs = [s for s in body if is_panel(s.name.lower())]
        for lod in lods:
            cells: dict = {}
            for s in secs:
                for sub in s.subsections:
                    if sub.lod != lod:
                        continue
                    pool = s.vertices_for(sub)
                    for t in sub.triangles():
                        for p in damage._tri_samples([place.point(pool[i].position) for i in t], damage.BAY_CELL / 2):
                            key = (
                                math.floor(p[across[0]] / damage.BAY_CELL),
                                math.floor(p[across[1]] / damage.BAY_CELL),
                            )
                            d = sign * p[axis]
                            old = cells.get(key)
                            cells[key] = d if old is None else pick(old, d)
            if cells:
                maps[(lod, kind)] = cells
    return maps


def _zone_vote_reference(maps, lod, pts, zones=damage.EXPOSED_ZONES):
    """The original zone_vote and zone_of."""

    def zone_of(c):
        for kind, _, axis, sign, which, reach in zones:
            cells = maps.get((lod, kind))
            if not cells:
                continue
            across = [a for a in range(3) if a != axis]
            d0 = cells.get((math.floor(c[across[0]] / damage.BAY_CELL), math.floor(c[across[1]] / damage.BAY_CELL)))
            if d0 is None:
                continue
            d = sign * c[axis] - d0
            if d > (damage.BAY_CLEARANCE if which == "inner" else damage.BUMPER_CLEARANCE) and (
                reach is None or d < reach
            ):
                return kind
        return None

    c = tuple(sum(p[k] for p in pts) / 3 for k in range(3))
    samples = [c] + list(damage._tri_samples(pts, damage.ZONE_STEP))
    votes = Counter(zone_of(q) for q in samples)
    votes.pop(None, None)
    if not votes:
        return None
    kind, n = votes.most_common(1)[0]
    return kind if n >= damage.ZONE_SHARE * len(samples) else None


class _Sub:
    def __init__(self, lod, tris):
        self.lod, self.tris = lod, tris

    def triangles(self):
        return list(self.tris)


class _Section:
    def __init__(self, name, subs, pool):
        self.name, self.subsections, self.pool = name, subs, pool

    def vertices_for(self, sub):
        return self.pool


def _panel_body(rng: random.Random):
    """Bonnet, boot lid and bumpers over a car-sized box (placed space: +y up,
    +z front), in two LODs, with triangles of every sample count up to the cap
    of 40, degenerate ones, copies (equal extremes), points on cell boundaries
    and at a zero coordinate of either sign."""
    body = []
    for name, centre in (
        ("hood", (0.0, 0.8, 1.2)),
        ("trunk_lid", (0.0, 0.85, -1.6)),
        ("bumperF", (0.0, 0.4, 2.1)),
        ("bumperR", (0.0, 0.4, -2.2)),
        ("door_lf", (0.8, 0.6, 0.0)),
    ):
        pool, subs = [], []
        for lod in (1, 2):
            tris = []
            for _ in range(rng.choice([5, 40, 150])):
                size = rng.choice([0.01, 0.05, 0.3, 2.5])
                a = tuple(centre[i] + rng.uniform(-0.6, 0.6) for i in range(3))
                corners = [a] + [tuple(a[i] + rng.uniform(-size, size) for i in range(3)) for _ in range(2)]
                kind = rng.random()
                if kind < 0.1:
                    corners = [tuple(round(x / damage.BAY_CELL) * damage.BAY_CELL for x in p) for p in corners]
                elif kind < 0.15:
                    corners = [corners[0]] * 3
                elif kind < 0.2:
                    corners = [(rng.choice([0.0, -0.0]), p[1], rng.choice([0.0, -0.0, p[2]])) for p in corners]
                base = len(pool)
                pool += [SimpleNamespace(position=p) for p in corners]
                tris.append((base, base + 1, base + 2))
                if rng.random() < 0.1:
                    tris.append((base, base + 1, base + 2))
            subs.append(_Sub(lod, tris))
        body.append(_Section(name, subs, pool))
    return body


def _same(a: dict, b: dict) -> bool:
    """Equal maps, with each value's float bits (the sign of zero too)."""
    return a.keys() == b.keys() and all(
        a[k].keys() == b[k].keys() and all(struct.pack("<d", a[k][c]) == struct.pack("<d", b[k][c]) for c in a[k])
        for k in a
    )


def test_zone_maps_match_the_sample_loop():
    rng = random.Random(59)
    place = SimpleNamespace(point=lambda p: p)
    for _ in range(4):
        body = _panel_body(rng)
        for engine in (None, "rear"):
            zones = damage.exposed_zones(engine)
            want = _zone_maps_reference(body, place, [1, 2], zones)
            assert want and _same(damage.zone_maps(body, place, [1, 2], zones), want)


def test_zone_votes_match_zone_vote():
    rng = random.Random(61)
    place = SimpleNamespace(point=lambda p: p)
    for _ in range(3):
        body = _panel_body(rng)
        zones = damage.exposed_zones(rng.choice([None, "mid"]))
        maps = _zone_maps_reference(body, place, [1, 2], zones)
        tris = []
        for _ in range(300):  # trims around and under the panels
            c = rng.choice([(0.0, 0.6, 1.2), (0.0, 0.6, -1.6), (0.0, 0.3, 1.9), (0.0, 0.3, -2.0), (0.0, 0.0, 0.0)])
            size = rng.choice([0.02, 0.1, 0.5, 1.5])
            a = tuple(c[i] + rng.uniform(-0.7, 0.7) for i in range(3))
            pts = [a] + [tuple(a[i] + rng.uniform(-size, size) for i in range(3)) for _ in range(2)]
            if rng.random() < 0.1:
                pts = [tuple(round(x / damage.BAY_CELL) * damage.BAY_CELL for x in p) for p in pts]
            tris.append(pts)
        for lod in (1, 2, 3):
            want = [_zone_vote_reference(maps, lod, pts, zones) for pts in tris]
            assert want == [damage.zone_vote(maps, lod, pts, zones) for pts in tris]
            assert damage.zone_votes(maps, lod, tris, zones) == want
            assert sum(w is not None for w in want) > (10 if lod < 3 else -1)
    assert damage.zone_votes({}, 1, []) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
