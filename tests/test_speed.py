"""The fast (numpy, indexed) versions of the converter's hot loops give exactly
what the original pure-Python loops gave. Each reference below is the original
code, kept here only to compare against; the inputs are random scenes (a few
seconds in all).

uv run python tests/test_speed.py     # or every test: uv run python -m tests speed
"""

from __future__ import annotations

import math
import random

from dsfforza.convert import cockpit, damage
from dsfforza.convert.damage import plan as damage_plan
from dsfforza.formats import carbin

# --- damage.Grid.nearest ------------------------------------------------------------


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


def test_grid_nearest_matches_the_ring_search():
    rng = random.Random(7)
    for _ in range(25):
        n = rng.choice([1, 2, 5, 17, 80, 300])
        cell = rng.choice([0.06, 0.08, 0.1])
        centres = [tuple(rng.uniform(-1, 1) for _ in range(3)) for _ in range(rng.randint(1, 4))]
        pts = []
        for _ in range(n):
            c, s = rng.choice(centres), rng.choice([0.01, 0.1, 0.4])
            pts.append(tuple(c[a] + rng.gauss(0, s) for a in range(3)))
        if rng.random() < 0.3:  # on cell boundaries, with equal distances
            pts = [tuple(round(x / cell) * cell for x in p) for p in pts]
        grid = damage.Grid(pts, list(range(n)), cell=cell)
        for _ in range(12):
            q = rng.choice(pts) if rng.random() < 0.2 else tuple(rng.uniform(-1.2, 1.2) for _ in range(3))
            k = rng.choice([1, 6, 16])
            assert grid.nearest(q, k) == _nearest_reference(grid, q, k)
    assert damage.Grid([], []).nearest((0.0, 0.0, 0.0)) == []


def test_closest_on_triangles_matches_closest_on_triangle():
    import numpy as np

    rng = random.Random(11)
    tris, points = [], []
    for _ in range(400):
        a = tuple(rng.uniform(-1, 1) for _ in range(3))
        kind = rng.random()
        if kind < 0.1:  # degenerate: a point, or a line
            b = a if rng.random() < 0.5 else tuple(a[i] + rng.uniform(-0.2, 0.2) for i in range(3))
            c = b
        else:
            b = tuple(a[i] + rng.uniform(-0.3, 0.3) for i in range(3))
            c = tuple(a[i] + rng.uniform(-0.3, 0.3) for i in range(3))
        tris.append((a, b, c))
        points.append(rng.choice([a, b, c]) if rng.random() < 0.1 else tuple(rng.uniform(-1.2, 1.2) for _ in range(3)))
    corners = np.array(tris, dtype=np.float64)
    for p in points[:60]:
        d, bary = damage.closest_on_triangles(p, corners)
        for n, t in enumerate(tris):
            want_d, want_bary = damage.closest_on_triangle(p, *t)
            assert d[n] == want_d and tuple(bary[n].tolist()) == want_bary


def _vote_reference(cells, cell, f, q, normal, reach=None):
    """The original DamagePlan._vote: every cell of each ring, one closest_on_triangle per triangle."""
    from collections import Counter

    key = tuple(int(math.floor(q[a] / cell)) for a in range(3))
    scored = []
    for r in range(8):
        seen = set()
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    if max(abs(dx), abs(dy), abs(dz)) != r:
                        continue
                    seen.update(cells.get((key[0] + dx, key[1] + dy, key[2] + dz), ()))
        for n in seen:
            t = f.tris[n]
            d, _ = damage.closest_on_triangle(q, *t[0])
            scored.append(
                (math.sqrt(d) + (damage.PANEL_FACING * (1 - damage._dot(normal, t[2])) if normal else 0.0), t[1])
            )
        if len(scored) >= 3 and sorted(scored)[2][0] <= r * cell:
            break
    if reach is not None:
        scored = [sc for sc in scored if sc[0] <= reach]
    if not scored:
        return None
    votes = Counter()
    for score, pair in sorted(scored)[:3]:
        votes[pair] += 1.0 / (score + 0.005)
    return votes.most_common(1)[0][0]


def test_panel_vote_matches_the_ring_search():
    from collections import defaultdict

    rng = random.Random(23)
    tris = []
    for _ in range(900):  # dense patches and a sparse spread, four panels
        c = rng.choice([(0.5, 0.2, -0.3), (-0.6, 0.1, 0.4)]) if rng.random() < 0.7 else (0, 0, 0)
        spread = 0.08 if c != (0, 0, 0) else 0.9
        a = tuple(c[i] + rng.uniform(-spread, spread) for i in range(3))
        corners = (
            a,
            tuple(a[i] + rng.uniform(-0.05, 0.05) for i in range(3)),
            tuple(a[i] + rng.uniform(-0.05, 0.05) for i in range(3)),
        )
        normal = damage._norm(damage._cross(damage._sub(corners[1], corners[0]), damage._sub(corners[2], corners[0])))
        if normal is not None:
            tris.append((corners, rng.choice([(0, 0), (1, 2), (3, 4), (5, 6)]), normal, rng.random() < 0.5))
    f = damage.HostField([], [], [], damage.Box((-1, -1, -1), (1, 1, 1)), tris=tris)
    plan = object.__new__(damage.DamagePlan)
    plan._grids = {}
    plan._forza_norm = lambda p: p
    allowed = {(0, 0), (1, 2), (3, 4), (5, 6)}
    index = plan._tri_index(f, allowed)
    cells = defaultdict(list)
    for n, (pts, _, _, _) in enumerate(tris):
        lo = [int(math.floor(min(p[a] for p in pts) / 0.08)) for a in range(3)]
        hi = [int(math.floor(max(p[a] for p in pts) / 0.08)) for a in range(3)]
        for i in range(lo[0], hi[0] + 1):
            for j in range(lo[1], hi[1] + 1):
                for k in range(lo[2], hi[2] + 1):
                    cells[(i, j, k)].append(n)
    for _ in range(150):
        q = tuple(rng.uniform(-1, 1) for _ in range(3))
        normal = damage._norm(tuple(rng.uniform(-1, 1) for _ in range(3))) if rng.random() < 0.8 else None
        reach = rng.choice([None, 0.12])
        assert plan._vote(index, f, q, normal, reach) == _vote_reference(cells, 0.08, f, q, normal, reach)


def _vote_scene(rng: random.Random):
    """Host triangles (f.tris rows) with the cases the batched vote must get right:
    dense patches and a sparse spread (stops at every ring, and none by ring 7),
    large triangles over many cells (scored once per ring they touch), exact
    duplicates on different panels and coplanar copies (equal scores, decided by
    the panel), and corners on cell boundaries."""
    panels = [(0, 0), (1, 2), (3, 4), (5, 6), (1, 0)]
    tris = []
    for _ in range(rng.choice([40, 300, 900])):
        c = rng.choice([(0.5, 0.2, -0.3), (-0.6, 0.1, 0.4)]) if rng.random() < 0.6 else (0, 0, 0)
        spread = 0.08 if c != (0, 0, 0) else 0.9
        a = tuple(c[i] + rng.uniform(-spread, spread) for i in range(3))
        size = 0.4 if rng.random() < 0.08 else 0.05
        corners = [a] + [tuple(a[i] + rng.uniform(-size, size) for i in range(3)) for _ in range(2)]
        if rng.random() < 0.15:  # on cell boundaries
            corners = [tuple(round(x / 0.08) * 0.08 for x in p) for p in corners]
        corners = tuple(corners)
        normal = damage._norm(damage._cross(damage._sub(corners[1], corners[0]), damage._sub(corners[2], corners[0])))
        if normal is None:
            continue
        tris.append((corners, rng.choice(panels), normal, rng.random() < 0.5))
        if rng.random() < 0.1:  # the same triangle on another panel
            tris.append((corners, rng.choice(panels), normal, rng.random() < 0.5))
        if rng.random() < 0.05:  # a shifted copy in its plane: equal distances off the patch
            shift = damage._cross(normal, (0.0, 0.0, 1.0))
            moved = tuple(tuple(p[i] + 0.01 * shift[i] for i in range(3)) for p in corners)
            tris.append((moved, rng.choice(panels), normal, rng.random() < 0.5))
    return damage.HostField([], [], [], damage.Box((-1, -1, -1), (1, 1, 1)), tris=tris)


def test_batched_panel_votes_match_the_ring_search():
    """DamagePlan._votes (all of a section's points at once, by ring counts) against
    the original per-point ring search, over several VOTE_RINGS schedules: the
    default, single rounds, and (1,) where most points fall back to _vote."""
    from collections import defaultdict

    rng = random.Random(31)
    saved = damage_plan.VOTE_RINGS, damage_plan.VOTE_CHUNK
    try:
        for scene in range(6):
            f = _vote_scene(rng)
            plan = object.__new__(damage.DamagePlan)
            plan._grids = {}
            plan._forza_norm = lambda p: p
            allowed = {(0, 0), (1, 2), (3, 4), (5, 6), (1, 0)}
            if scene % 3 == 1:
                allowed = {(1, 2), (3, 4)}
            for paint in (False, True):
                index = plan._tri_index(f, allowed, paint=paint)
                if index is None:
                    continue
                cells = defaultdict(list)
                for n, (pts, pair, _, is_paint) in enumerate(f.tris):
                    if pair not in allowed or (paint and not is_paint):
                        continue
                    lo = [int(math.floor(min(p[a] for p in pts) / 0.08)) for a in range(3)]
                    hi = [int(math.floor(max(p[a] for p in pts) / 0.08)) for a in range(3)]
                    for i in range(lo[0], hi[0] + 1):
                        for j in range(lo[1], hi[1] + 1):
                            for k in range(lo[2], hi[2] + 1):
                                cells[(i, j, k)].append(n)
                qs, normals = [], []
                for _ in range(80):
                    kind = rng.random()
                    if kind < 0.3:  # on a host triangle: a corner, or its centroid
                        pts = rng.choice(f.tris)[0]
                        q = (
                            rng.choice(pts)
                            if rng.random() < 0.5
                            else tuple(sum(p[a] for p in pts) / 3 for a in range(3))
                        )
                    elif kind < 0.4:  # on a cell boundary
                        q = tuple(rng.randint(-12, 12) * 0.08 for _ in range(3))
                    else:
                        q = tuple(rng.uniform(-1.3, 1.3) for _ in range(3))
                    qs.append(q)
                    normals.append(
                        damage._norm(tuple(rng.uniform(-1, 1) for _ in range(3))) if rng.random() < 0.8 else None
                    )
                for reach in (None, 0.12):
                    want = [_vote_reference(cells, 0.08, f, q, n, reach) for q, n in zip(qs, normals, strict=True)]
                    for rings, chunk in (((1, 2, 3, 5, 7), 400_000), ((7,), 50), ((2, 4), 1000), ((1,), 400_000)):
                        damage_plan.VOTE_RINGS, damage_plan.VOTE_CHUNK = rings, chunk
                        assert plan._votes(index, f, qs, normals, reach) == want, (scene, paint, reach, rings)
    finally:
        damage_plan.VOTE_RINGS, damage_plan.VOTE_CHUNK = saved


def test_batched_panel_vote_stops_on_an_exact_ring_score():
    """The third best exactly at r * cell stops the ring loop at r (<=). Two small
    triangles of one panel at distance exactly k * 0.08 under the point, and a
    large one of another panel nearer that spans rings k..k+2: stopped at ring k
    the small ones win; one ring later the large one counts twice and wins."""
    flat = (0.0, 0.0, 1.0)
    small = ((0.0, 0.0, 0.0), (0.03, 0.0, 0.0), (0.0, 0.03, 0.0))
    large = ((0.0, 0.0, 0.01), (-0.3, 0.0, 0.01), (0.0, -0.3, 0.01))
    tris = [(small, (1, 2), flat, True), (small, (1, 2), flat, True), (large, (5, 6), flat, True)]
    f = damage.HostField([], [], [], damage.Box((-1, -1, -1), (1, 1, 1)), tris=tris)
    plan = object.__new__(damage.DamagePlan)
    plan._grids = {}
    plan._forza_norm = lambda p: p
    index = plan._tri_index(f, {(1, 2), (5, 6)})
    cells = index[1]
    qs = [(0.0, 0.0, k * 0.08) for k in (1, 2, 3)]
    want = [_vote_reference(cells, 0.08, f, q, None) for q in qs]
    assert want == [(1, 2)] * 3
    assert plan._votes(index, f, qs, [None] * 3) == want


def test_nearest_pairs_match_nearest_pair():
    """The batched _nearest_pairs (paint triangles try the paint vote first) gives
    what the per-point vote path gives."""
    rng = random.Random(37)
    f = _vote_scene(rng)
    plan = object.__new__(damage.DamagePlan)
    plan._grids = {}
    plan._forza_norm = lambda p: p
    allowed = {(0, 0), (1, 2), (3, 4), (5, 6), (1, 0)}
    qs = [tuple(rng.uniform(-1, 1) for _ in range(3)) for _ in range(150)]
    normals = [damage._norm(tuple(rng.uniform(-1, 1) for _ in range(3))) for _ in qs]
    paints = [rng.random() < 0.5 for _ in qs]
    want = []
    for q, n, paint in zip(qs, normals, paints, strict=True):
        pair = plan._vote(plan._tri_index(f, allowed, paint=True), f, q, n, damage.PAINT_REACH) if paint else None
        want.append(pair if pair is not None else plan._vote(plan._tri_index(f, allowed), f, q, n))
    assert plan._nearest_pairs(qs, allowed, f, normals, paints) == want
    assert [plan._nearest_pair(q, allowed, f, n, p) for q, n, p in zip(qs, normals, paints, strict=True)] == want


# --- cockpit.visible_triangles and GlassSight -----------------------------------------


def _visible_reference(sections, eyes, resolution=256, near_keep=0.25, tiny_slack=0.03):
    """The original per-pixel loops of cockpit.visible_triangles."""
    from dsfforza.convert import forza_materials as fm

    tris, glass = [], set()
    for si, s in enumerate(sections):
        for ui, sub in enumerate(s.subsections):
            if sub.lod != 0:
                continue
            is_glass = fm.classify_cockpit(sub.material)[0] == fm.GLASS
            pool = s.lod0_vertices
            for k, t in enumerate(sub.triangles()):
                if is_glass:
                    glass.add(len(tris))
                tris.append(((si, ui), k, tuple(pool[i].position for i in t), tuple((si, i) for i in t)))
    seen = set(glass)
    half = resolution / 2
    for eye in eyes:
        rel = [tuple(tuple(p[a] - eye[a] for a in range(3)) for p in t[2]) for t in tris]
        for n, r in enumerate(rel):
            if min(x * x + y * y + z * z for x, y, z in r) < near_keep * near_keep:
                seen.add(n)
        for axis, sign in cockpit.CUBE_FACES:
            ids = [-1] * (resolution * resolution)
            depth = [0.0] * (resolution * resolution)
            tiny = []
            for n, r in enumerate(rel):
                if n in glass:
                    continue
                covered, best = False, None
                for pts in cockpit._face_parts(r, axis, sign, half):
                    (px, py, pw), (qx, qy, qw), (rx, ry, rw) = pts
                    x0, x1 = max(int(min(px, qx, rx)), 0), min(int(max(px, qx, rx)) + 1, resolution)
                    y0, y1 = max(int(min(py, qy, ry)), 0), min(int(max(py, qy, ry)) + 1, resolution)
                    if x0 >= x1 or y0 >= y1:
                        continue
                    area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
                    cx, cy = int((px + qx + rx) / 3), int((py + qy + ry) / 3)
                    if 0 <= cx < resolution and 0 <= cy < resolution:
                        best = (cy * resolution + cx, max(pw, qw, rw))
                    if abs(area) < 1e-12:
                        continue
                    for y in range(y0, y1):
                        yc = y + 0.5
                        for x in range(x0, x1):
                            xc = x + 0.5
                            w0 = ((qx - xc) * (ry - yc) - (qy - yc) * (rx - xc)) / area
                            w1 = ((rx - xc) * (py - yc) - (ry - yc) * (px - xc)) / area
                            w2 = 1 - w0 - w1
                            if w0 < 0 or w1 < 0 or w2 < 0:
                                continue
                            covered = True
                            w = w0 * pw + w1 * qw + w2 * rw
                            k = y * resolution + x
                            if w > depth[k]:
                                depth[k] = w
                                ids[k] = n
                if not covered and best is not None:
                    tiny.append((n, best[0], best[1]))
            seen.update(i for i in ids if i >= 0)
            seen.update(n for n, k, w in tiny if w >= depth[k] * (1 - tiny_slack))
    verts = {v for n in seen - glass for v in tris[n][3]}
    kept = {}
    for n, (key, k, _, vids) in enumerate(tris):
        if n in seen or any(v in verts for v in vids):
            kept.setdefault(key, set()).add(k)
    return kept


def _sight_reference(sections, eye, resolution=128):
    """The original depth loops of cockpit.GlassSight: {face: depth list}."""
    from dsfforza.convert import forza_materials as fm

    half = resolution / 2
    tris = []
    for s in sections:
        pool = s.lod0_vertices
        for sub in s.subsections:
            if sub.lod != 0 or fm.classify_cockpit(sub.material)[0] == fm.GLASS:
                continue
            for t in sub.triangles():
                tris.append([tuple(pool[i].position[a] - eye[a] for a in range(3)) for i in t])
    faces = {}
    for axis, sign in cockpit.CUBE_FACES:
        depth = [0.0] * (resolution * resolution)
        for r in tris:
            for pts in cockpit._face_parts(r, axis, sign, half):
                (px, py, pw), (qx, qy, qw), (rx, ry, rw) = pts
                x0, x1 = max(int(min(px, qx, rx)), 0), min(int(max(px, qx, rx)) + 1, resolution)
                y0, y1 = max(int(min(py, qy, ry)), 0), min(int(max(py, qy, ry)) + 1, resolution)
                area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
                if x0 >= x1 or y0 >= y1 or abs(area) < 1e-12:
                    continue
                for y in range(y0, y1):
                    yc = y + 0.5
                    for x in range(x0, x1):
                        xc = x + 0.5
                        w0 = ((qx - xc) * (ry - yc) - (qy - yc) * (rx - xc)) / area
                        w1 = ((rx - xc) * (py - yc) - (ry - yc) * (px - xc)) / area
                        w2 = 1 - w0 - w1
                        if w0 < 0 or w1 < 0 or w2 < 0:
                            continue
                        w = w0 * pw + w1 * qw + w2 * rw
                        k = y * resolution + x
                        depth[k] = max(depth[k], w)
        faces[(axis, sign)] = depth
    return faces


def _cabin(rng: random.Random, triangles: int) -> list[carbin.Section]:
    """A random cabin around the origin: small and large triangles, some crossing
    the eye's near planes, shared vertices, a glass subsection, duplicates (equal
    depths) and one degenerate triangle."""
    sections = []
    for name, material in (("dash", "plastic"), ("glassf", "cockpit_glass"), ("seat", "leather")):
        verts, indices = [], []
        for _ in range(triangles):
            c = [rng.uniform(-1.2, 1.2) for _ in range(3)]
            size = rng.choice([0.004, 0.03, 0.2, 0.9])
            base = len(verts)
            for _ in range(3):
                p = tuple(c[a] + rng.uniform(-size, size) for a in range(3))
                verts.append(carbin.Vertex(p, (0.0, 0.0), (0.0, 0.0), (0.0, 0.0, 1.0)))
            indices += [base, base + 1, base + 2]
            if rng.random() < 0.2:  # a neighbour sharing an edge
                verts.append(
                    carbin.Vertex(tuple(c[a] + rng.uniform(-size, size) for a in range(3)), (0, 0), (0, 0), (0, 0, 1))
                )
                indices += [base + 1, base + 2, base + 3]
            if rng.random() < 0.05:  # the same triangle twice
                indices += [base, base + 1, base + 2]
        verts += [carbin.Vertex((0.3, 0.1, -0.5), (0, 0), (0, 0), (0, 0, 1))] * 3  # degenerate
        indices += [len(verts) - 3, len(verts) - 2, len(verts) - 1]
        sub = carbin.SubSection(material, 0, 4, (0.0, 1.0, 0.0, 1.0) * 2, indices)
        sections.append(carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod0_vertices=verts, subsections=[sub]))
    return sections


def test_visible_triangles_match_the_pixel_loops():
    rng = random.Random(3)
    for triangles, resolution in ((60, 32), (150, 48)):
        sections = _cabin(rng, triangles)
        eyes = [(0.0, 0.0, 0.0), (0.12, 0.0, 0.0), (0.0, 0.08, -0.05)]
        assert cockpit.visible_triangles(sections, eyes, resolution) == _visible_reference(sections, eyes, resolution)


def test_glass_sight_matches_the_pixel_loops():
    rng = random.Random(5)
    sections = _cabin(rng, 120)
    eye = (0.05, -0.02, 0.01)
    place = type("Place", (), {"dy": 0.0, "dz": 0.0})()
    sight = cockpit.GlassSight(sections, eye, place, resolution=40)
    assert sight.faces == _sight_reference(sections, eye, resolution=40)


# --- formats.xds: untile and decode_rgba -------------------------------------------------


def _untile_reference(data: bytes):
    """The original block-by-block untile."""
    from dsfforza.formats import xds

    fmt, width, height = xds.parse_header(data)
    fourcc, block_size = xds.FORMATS[fmt]
    payload = data[xds.HEADER_SIZE :]
    blocks_w, blocks_h = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    aligned_w, aligned_h = (blocks_w + 31) & ~31, (blocks_h + 31) & ~31
    out = bytearray(blocks_w * blocks_h * block_size)
    for n in range(aligned_w * aligned_h):
        src = n * block_size
        if src + block_size > len(payload):
            break
        x, y = xds.tiled_x(n, blocks_w, block_size), xds.tiled_y(n, blocks_w, block_size)
        if x >= blocks_w or y >= blocks_h:
            continue
        block = bytearray(payload[src : src + block_size])
        block[0::2], block[1::2] = block[1::2], block[0::2]
        dst = (y * blocks_w + x) * block_size
        out[dst : dst + block_size] = block
    return bytes(out), width, height, fourcc


def _decode_reference(blocks: bytes, width: int, height: int, fourcc: bytes) -> bytearray:
    """The original per-block, per-texel DXT decoder."""
    import struct

    def rgb565(c):
        return ((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31

    block_size = 8 if fourcc == b"DXT1" else 16
    blocks_w, blocks_h = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    out = bytearray(width * height * 4)
    for by in range(blocks_h):
        for bx in range(blocks_w):
            b = blocks[(by * blocks_w + bx) * block_size : (by * blocks_w + bx + 1) * block_size]
            alpha = [255] * 16
            if block_size == 16:
                a0, a1 = b[0], b[1]
                table = [a0, a1] + (
                    [((6 - i) * a0 + (i + 1) * a1) // 7 for i in range(6)]
                    if a0 > a1
                    else [((4 - i) * a0 + (i + 1) * a1) // 5 for i in range(4)] + [0, 255]
                )
                bits = int.from_bytes(b[2:8], "little")
                alpha = [table[(bits >> (3 * i)) & 7] for i in range(16)]
                b = b[8:]
            c0, c1, idx = struct.unpack_from("<HHI", b)
            p0, p1 = rgb565(c0), rgb565(c1)
            if c0 > c1 or block_size == 16:
                colours = [
                    p0,
                    p1,
                    tuple((2 * x + y) // 3 for x, y in zip(p0, p1)),
                    tuple((x + 2 * y) // 3 for x, y in zip(p0, p1)),
                ]
            else:
                colours = [p0, p1, tuple((x + y) // 2 for x, y in zip(p0, p1)), (0, 0, 0)]
            for i in range(16):
                x, y = bx * 4 + i % 4, by * 4 + i // 4
                if x < width and y < height:
                    o = (y * width + x) * 4
                    out[o : o + 3] = bytes(colours[(idx >> (2 * i)) & 3])
                    out[o + 3] = alpha[i]
    return out


def _xds(fmt: int, width: int, height: int, payload: bytes) -> bytes:
    import struct

    header = bytearray(52)
    struct.pack_into(">II", header, 0x20, fmt, (width - 1) | (height - 1) << 13)
    return bytes(header) + payload


def test_xds_untile_and_decode_match_the_block_loops():
    from dsfforza.formats import xds

    rng = random.Random(13)
    for fmt, width, height, short in (
        (18, 64, 32, 0),
        (20, 128, 64, 0),
        (20, 20, 12, 0),
        (19, 8, 200, 0),
        (18, 96, 96, 300),
    ):
        block = xds.FORMATS[fmt][1]
        blocks = ((max(1, (width + 3) // 4) + 31) & ~31) * ((max(1, (height + 3) // 4) + 31) & ~31)
        payload = bytes(rng.getrandbits(8) for _ in range(blocks * block - short * block))
        data = _xds(fmt, width, height, payload)
        assert xds.untile(data) == _untile_reference(data)
        linear, w, h, fourcc = xds.untile(data)
        assert xds.decode_rgba(linear, w, h, fourcc) == _decode_reference(linear, w, h, fourcc)


# --- dsf_materials: DXT1/DXT5 encoding and the mip filter --------------------------------


def _colour_block_reference(px):
    import struct

    def q565(r, g, b):
        return ((r * 31 + 127) // 255) << 11 | ((g * 63 + 127) // 255) << 5 | ((b * 31 + 127) // 255)

    def x565(c):
        r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
        return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)

    lo = [min(p[c] for p in px) for c in range(3)]
    hi = [max(p[c] for p in px) for c in range(3)]
    axis = max(range(3), key=lambda c: hi[c] - lo[c])
    mean = [sum(p[c] for p in px) / 16 for c in range(3)]
    e0, e1 = list(hi), list(lo)
    for c in range(3):
        if c != axis and sum((p[axis] - mean[axis]) * (p[c] - mean[c]) for p in px) < 0:
            e0[c], e1[c] = lo[c], hi[c]
    inset = [(e0[c] - e1[c]) / 16 for c in range(3)]
    e0 = [max(0, min(255, round(e0[c] - inset[c]))) for c in range(3)]
    e1 = [max(0, min(255, round(e1[c] + inset[c]))) for c in range(3)]
    c0, c1 = q565(*e0), q565(*e1)
    if c0 < c1:
        c0, c1 = c1, c0
    if c0 == c1:
        return struct.pack("<HHI", c0, c1, 0)
    p0, p1 = x565(c0), x565(c1)
    pal = [p0, p1, tuple((2 * a + b) // 3 for a, b in zip(p0, p1)), tuple((a + 2 * b) // 3 for a, b in zip(p0, p1))]
    bits = 0
    for i, p in enumerate(px):
        best = min(range(4), key=lambda k: sum((p[c] - pal[k][c]) ** 2 for c in range(3)))
        bits |= best << (2 * i)
    return struct.pack("<HHI", c0, c1, bits)


def _alpha_block_reference(alpha):
    a0, a1 = max(alpha), min(alpha)
    if a0 == a1:
        return bytes((a0, a1)) + bytes(6)
    pal = [a0, a1] + [((7 - k) * a0 + k * a1) // 7 for k in range(1, 7)]
    bits = 0
    for i, a in enumerate(alpha):
        bits |= min(range(8), key=lambda k: abs(a - pal[k])) << (3 * i)
    return bytes((a0, a1)) + bits.to_bytes(6, "little")


def _encode_reference(rgba, width, height, dxt1=False):
    out = bytearray()
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            px, alpha = [], []
            for y in range(4):
                o = ((by + y) * width + bx) * 4
                row = rgba[o : o + 16]
                for x in range(4):
                    px.append((row[x * 4], row[x * 4 + 1], row[x * 4 + 2]))
                    alpha.append(row[x * 4 + 3])
            out += (b"" if dxt1 else _alpha_block_reference(alpha)) + _colour_block_reference(px)
    return bytes(out)


def _downsample_reference(rgba, width, height):
    w2, h2 = max(1, width // 2), max(1, height // 2)
    out = bytearray(w2 * h2 * 4)
    for y in range(h2):
        r0, r1 = (2 * y) * width * 4, (2 * y + 1) * width * 4
        for x in range(w2):
            a, b = r0 + 8 * x, r1 + 8 * x
            o = (y * w2 + x) * 4
            for c in range(4):
                out[o + c] = (rgba[a + c] + rgba[a + 4 + c] + rgba[b + c] + rgba[b + 4 + c] + 2) // 4
    return out, w2, h2


def test_dxt_encoders_match_the_block_loops():
    from dsfforza.convert import dsf_materials as dm

    rng = random.Random(17)
    for width, height in ((16, 8), (32, 32), (8, 4)):
        texels = []
        for _ in range(width * height):
            kind = rng.random()
            if kind < 0.3:  # flat patches: equal endpoints
                texels.append((90, 90, 90, 255))
            elif kind < 0.6:  # a gradient (correlated channels, rounding ties)
                v = rng.randrange(256)
                texels.append((v, 255 - v, v // 2, rng.choice((0, 128, 255))))
            else:
                texels.append(tuple(rng.randrange(256) for _ in range(4)))
        rgba = bytes(c for t in texels for c in t)
        assert dm.encode_dxt5(rgba, width, height) == _encode_reference(rgba, width, height)
        assert dm.encode_dxt1(rgba, width, height) == _encode_reference(rgba, width, height, dxt1=True)
        assert dm.downsample(rgba, width, height) == _downsample_reference(rgba, width, height)
    odd = bytes(rng.randrange(256) for _ in range(7 * 5 * 4))
    assert dm.downsample(odd, 7, 5) == _downsample_reference(odd, 7, 5)


def test_atlas_render_matches_the_texel_loop():
    from pathlib import Path

    from dsfforza.convert import dsf_materials as dm
    from dsfforza.convert import forza_materials as fm

    rng = random.Random(19)

    class Atlases:
        def __init__(self):
            self.maps = {
                fm.NODAMAGE: (64, 32, bytearray(rng.randrange(256) for _ in range(64 * 32 * 4))),
                fm.LIGHTS: (32, 32, bytearray(rng.choice((0, 40, 200, 255)) for _ in range(32 * 32 * 4))),
            }

        def get(self, family):
            return self.maps[family]

    t = dm.Texturing(Path(), size=(64, 64), cutout_colour=(34, 34, 36, 0), lift={fm.NODAMAGE: 30})
    t.atlases = Atlases()
    t.regions = {
        fm.NODAMAGE: [
            dm.Region(fm.NODAMAGE, (0, 0, 40, 20), (0, 0), (28, 18)),
            dm.Region(fm.NODAMAGE, (30, 10, 64, 32), (24, 10), (40, 30)),
        ],
        fm.LIGHTS: [dm.Region(fm.LIGHTS, (2, 3, 30, 31), (4, 40), (22, 20))],
    }
    t.scale = {fm.NODAMAGE: 0.6, fm.LIGHTS: 0.75}
    t.alpha_only = {(fm.NODAMAGE, 1)}
    t.solids = {("paint",): (56, 56), ("swatch", 1, 2, 3, 4): (48, 56)}
    t.solid_colours = {("paint",): t.plain, ("swatch", 1, 2, 3, 4): (1, 2, 3, 4)}
    assert t.render() == t._render_texels()


# --- formats.wwise_bank: IMA ADPCM and WAV previews ------------------------------------


def _decode_ima_reference(data: bytes, channels: int = 1):
    import struct

    from dsfforza.formats import wwise_bank as wb

    out = [[] for _ in range(channels)]
    span = wb.BLOCK * channels
    for base in range(0, len(data) - span + 1, span):
        for ch in range(channels):
            b = base + ch * wb.BLOCK
            pred, idx = struct.unpack_from("<hB", data, b)
            idx = min(idx, 88)
            samples = [pred]
            for i in range(1, wb.BLOCK_SAMPLES):
                byte = data[b + 4 + (i - 1) // 2]
                nib = (byte >> 4) if (i - 1) & 1 else byte & 0xF
                pred, idx = wb._expand(nib, pred, idx)
                samples.append(pred)
            out[ch].extend(samples)
    return out


def _encode_block_reference(x, idx):
    from dsfforza.formats import wwise_bank as wb

    pred = x[0]
    nibs = []
    err = 0.0
    for i in range(1, wb.BLOCK_SAMPLES):
        step = wb.STEPS[idx]
        d = x[i] - pred
        sign = 8 if d < 0 else 0
        mag = min(7, (abs(d) * 4) // step) if step else 0
        nib = sign | mag
        pred, idx = wb._expand(nib, pred, idx)
        err += (x[i] - pred) ** 2
        nibs.append(nib)
    nibs.append(0)
    body = bytes(nibs[k] | (nibs[k + 1] << 4) for k in range(0, wb.BLOCK_SAMPLES, 2))
    return body, idx, err


def test_ima_adpcm_matches_the_sample_loops():
    import struct
    import tempfile
    import wave
    from pathlib import Path

    from dsfforza.formats import wwise_bank as wb

    rng = random.Random(29)
    signal = [int(20000 * math.sin(i / 7.0) + rng.randint(-3000, 3000)) for i in range(64 * 6)]
    signal += [32767, -32768] * 40 + [0] * 50  # clipping and silence
    for idx in (0, 5, 40, 88):
        for b in range(0, len(signal) - 64, 64):
            block = signal[b : b + 64]
            assert wb._encode_block(block, idx) == _encode_block_reference(block, idx)
    data = wb.encode_ima(signal)
    assert wb.decode_ima(data) == _decode_ima_reference(data)
    stereo = bytes(rng.getrandbits(8) for _ in range(wb.BLOCK * 2 * 5))
    assert wb.decode_ima(stereo, 2) == _decode_ima_reference(stereo, 2)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "x.wav"
        channels = [[1.7, -2.9, 40000, -40000.5, 12], [3, 4, -5, 6, 7, 8]]
        wb.write_wav(path, channels, 22050)
        with wave.open(str(path)) as w:
            frames = w.readframes(w.getnframes())
        want = b"".join(struct.pack("<h", max(-32768, min(32767, int(c[i])))) for i in range(5) for c in channels)
        assert frames == want


# --- gui_assets: nameplate coverage -------------------------------------------------------


def _text_plate_reference(text: str, height: int, italic: float):
    """The original per-pixel, per-sample nameplate rasteriser."""
    import itertools

    from dsfforza.game import gui_assets as g

    cell = height / 9.0
    stroke = cell * 0.95
    outline = cell * 0.55
    advance = 5.4 * cell
    widths = [advance if ch != " " else advance * 0.6 for ch in text]
    width = int(sum(widths) + 2 * (stroke + outline) + italic * 6 * cell + 2)
    img = g.Image(width, height)
    origin_y = 1.5 * cell
    segs = []
    x = stroke + outline + 1
    for ch, w in zip(text, widths):
        for line in g.STROKES.get(ch, []):
            pts = [(x + gx * cell + italic * (6 - gy) * cell, origin_y + gy * cell) for gx, gy in line]
            segs += list(itertools.pairwise(pts))
        x += w
    ss = 3
    reach = stroke + outline + 1
    for py in range(height):
        near = [(a, b) for a, b in segs if min(a[1], b[1]) - reach <= py + 1 and max(a[1], b[1]) + reach >= py]
        for px in range(width):
            local = [(a, b) for a, b in near if min(a[0], b[0]) - reach <= px + 1 and max(a[0], b[0]) + reach >= px]
            if not local:
                continue
            core = edge = 0
            for sy in range(ss):
                for sx in range(ss):
                    fx, fy = px + (sx + 0.5) / ss, py + (sy + 0.5) / ss
                    d = min(g._segment_distance(fx, fy, a, b) for a, b in local)
                    if d <= stroke:
                        core += 1
                    elif d <= stroke + outline:
                        edge += 1
            if not core and not edge:
                continue
            t = (py - origin_y) / (6 * cell)
            shade = 235 - 150 * math.exp(-(((t - 0.55) / 0.18) ** 2)) - 40 * max(0.0, t)
            c, e = core / (ss * ss), edge / (ss * ss)
            alpha = min(1.0, c + e)
            lum = max(0.0, (shade * c + 20 * e) / max(alpha, 1e-6))
            o = (py * width + px) * 4
            img.px[o : o + 4] = bytes((int(lum), int(lum), int(min(255, lum * 1.03)), int(alpha * 255)))
    return img


def test_text_plate_matches_the_sample_loops():
    from dsfforza.game import gui_assets as g

    for text, height in (("S5S RAPTOR", 76), ("M3", 40), ("205T16 & 9-8.7/", 50)):
        new, old = g._text_plate(text, height, 0.22), _text_plate_reference(text, height, 0.22)
        assert (new.w, new.h) == (old.w, old.h) and new.px == old.px


def _untile32_reference(data: bytes):
    from dsfforza.formats import xds

    _, width, height = xds.parse_header(data)
    src = data[xds.HEADER_SIZE :]
    aligned_w, aligned_h = (width + 31) & ~31, (height + 31) & ~31
    out = bytearray(width * height * 4)
    for n in range(aligned_w * aligned_h):
        o = n * 4
        if o + 4 > len(src):
            break
        x, y = xds.tiled_x(n, width, 4), xds.tiled_y(n, width, 4)
        if x >= width or y >= height:
            continue
        a, r, g, b = src[o : o + 4]
        d = (y * width + x) * 4
        out[d : d + 4] = bytes((r, g, b, a))
    return width, height, out


def test_untile32_matches_the_texel_loop():
    from dsfforza.game import gui_assets as g

    rng = random.Random(31)
    for width, height, short in ((64, 32, 0), (40, 24, 0), (96, 64, 500)):
        texels = ((width + 31) & ~31) * ((height + 31) & ~31) - short
        data = _xds(6, width, height, bytes(rng.getrandbits(8) for _ in range(4 * texels)))
        assert g.untile32(data) == _untile32_reference(data)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
