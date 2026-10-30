"""The fast versions of the cockpit cull, the atlas steps of the cockpit and the
engine-sound steps give exactly what the code before them gave. Each reference
below is that code (the numpy rasteriser, near-plane clip and triangle
bookkeeping of cockpit.py, the IMA ADPCM encoder and decoder and the WAV writer
of wwise_bank.py, the cut search of engine_sound.seamless), kept here only to
compare against, on random inputs (a few seconds in all); the region cut and the
atlas render are compared with the loops dsf_materials keeps. tests/test_speed.py
compares the same functions with the original pure-Python loops on smaller scenes.

uv run python tests/test_speed_cockpit.py     # or: uv run python -m tests speed_cockpit
"""

from __future__ import annotations

import math
import random
import struct
import tempfile
from pathlib import Path

import numpy as np

from dsfforza.convert import cockpit, engine_sound
from dsfforza.convert import dsf_materials as dm
from dsfforza.convert import forza_materials as fm
from dsfforza.convert.cockpit import visibility
from dsfforza.formats import carbin
from dsfforza.formats import wwise_bank as wb

# --- cockpit: the references ---------------------------------------------------------


def _screen_parts_reference(rel, axis, sign, half, skip=None):
    u_axis, v_axis = [a for a in range(3) if a != axis]
    z = rel[:, :, axis] * sign
    live = z.max(axis=1) >= cockpit.NEAR_PLANE
    if skip is not None:
        live &= ~skip
    crossing = live & (z.min(axis=1) < cockpit.NEAR_PLANE)
    whole = np.flatnonzero(live & ~crossing)
    uvz = [np.stack((rel[whole, :, u_axis], rel[whole, :, v_axis], z[whole]), axis=-1)]
    owner = [whole]
    rank = [np.zeros(len(whole), dtype=np.int64)]
    clipped, clip_owner, clip_rank = [], [], []
    for n in np.flatnonzero(crossing).tolist():
        face = [(p[u_axis], p[v_axis], sign * p[axis]) for p in rel[n].tolist()]
        poly = cockpit._clip_near(face)
        for i in range(1, len(poly) - 1):
            clipped.append((poly[0], poly[i], poly[i + 1]))
            clip_owner.append(n)
            clip_rank.append(i)
    if clipped:
        uvz.append(np.array(clipped, dtype=np.float64))
        owner.append(np.array(clip_owner, dtype=np.int64))
        rank.append(np.array(clip_rank, dtype=np.int64))
    uvz, owner, rank = np.concatenate(uvz), np.concatenate(owner), np.concatenate(rank)
    order = np.lexsort((rank, owner))
    uvz, owner = uvz[order], owner[order]
    u, v, zz = uvz[..., 0], uvz[..., 1], uvz[..., 2]
    return np.stack((half + half * u / zz, half - half * v / zz, 1 / zz), axis=-1), owner


def _raster_reference(parts, owner, resolution, depth, ids=None, chunk=1 << 20):
    px, py, pw = parts[:, 0, 0], parts[:, 0, 1], parts[:, 0, 2]
    qx, qy, qw = parts[:, 1, 0], parts[:, 1, 1], parts[:, 1, 2]
    rx, ry, rw = parts[:, 2, 0], parts[:, 2, 1], parts[:, 2, 2]
    x0 = np.maximum(np.trunc(np.minimum(np.minimum(px, qx), rx)), 0).astype(np.int64)
    x1 = np.minimum(np.trunc(np.maximum(np.maximum(px, qx), rx)) + 1, resolution).astype(np.int64)
    y0 = np.maximum(np.trunc(np.minimum(np.minimum(py, qy), ry)), 0).astype(np.int64)
    y1 = np.minimum(np.trunc(np.maximum(np.maximum(py, qy), ry)) + 1, resolution).astype(np.int64)
    bbox = (x0 < x1) & (y0 < y1)
    area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
    drawn = np.flatnonzero(bbox & ~(np.abs(area) < 1e-12))
    covered = np.zeros(len(parts), dtype=bool)
    width = x1 - x0
    counts = np.where(bbox, width * (y1 - y0), 0)
    start = 0
    while start < len(drawn):
        total = np.cumsum(counts[drawn[start:]])
        stop = start + max(1, int(np.searchsorted(total, chunk, side="right")))
        idx = drawn[start:stop]
        start = stop
        c = counts[idx]
        rep = np.repeat(idx, c)
        local = np.arange(len(rep), dtype=np.int64) - np.repeat(np.cumsum(c) - c, c)
        x = x0[rep] + local % width[rep]
        y = y0[rep] + local // width[rep]
        xc, yc = x + 0.5, y + 0.5
        a = area[rep]
        w0 = ((qx[rep] - xc) * (ry[rep] - yc) - (qy[rep] - yc) * (rx[rep] - xc)) / a
        w1 = ((rx[rep] - xc) * (py[rep] - yc) - (ry[rep] - yc) * (px[rep] - xc)) / a
        w2 = 1 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        covered[rep[inside]] = True
        w = w0 * pw[rep] + w1 * qw[rep] + w2 * rw[rep]
        keep = inside & (w > 0)
        k, w, n = (y * resolution + x)[keep], w[keep], rep[keep]
        if not len(k):
            continue
        order = np.lexsort((n, -w, k))
        k, w, n = k[order], w[order], n[order]
        first = np.ones(len(k), dtype=bool)
        first[1:] = k[1:] != k[:-1]
        k, w, n = k[first], w[first], n[first]
        win = w > depth[k]
        depth[k[win]] = w[win]
        if ids is not None:
            ids[k[win]] = owner[n[win]]
    return covered, bbox


def _visible_reference(sections, eyes, resolution=256, near_keep=0.25, tiny_slack=0.03):
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
    points = np.array([t[2] for t in tris], dtype=np.float64).reshape(-1, 3, 3)
    skip = np.zeros(len(tris), dtype=bool)
    skip[sorted(glass)] = True
    for eye in eyes:
        rel = points - np.array(eye, dtype=np.float64)
        x, y, z = rel[..., 0], rel[..., 1], rel[..., 2]
        seen.update(np.flatnonzero((x * x + y * y + z * z).min(axis=1) < near_keep * near_keep).tolist())
        for axis, sign in cockpit.CUBE_FACES:
            ids = np.full(resolution * resolution, -1, dtype=np.int64)
            depth = np.zeros(resolution * resolution)
            parts, owner = _screen_parts_reference(rel, axis, sign, half, skip)
            covered, bbox = _raster_reference(parts, owner, resolution, depth, ids)
            seen.update(ids[ids >= 0].tolist())
            cx = np.trunc((parts[:, 0, 0] + parts[:, 1, 0] + parts[:, 2, 0]) / 3)
            cy = np.trunc((parts[:, 0, 1] + parts[:, 1, 1] + parts[:, 2, 1]) / 3)
            best = bbox & (cx >= 0) & (cx < resolution) & (cy >= 0) & (cy < resolution)
            hit = set(owner[covered].tolist())
            tiny = {}
            for n, k, w in zip(
                owner[best].tolist(),
                (cy[best] * resolution + cx[best]).astype(np.int64).tolist(),
                parts[best, :, 2].max(axis=1).tolist(),
                strict=True,
            ):
                tiny[n] = (k, w)
            seen.update(n for n, (k, w) in tiny.items() if n not in hit and w >= depth[k] * (1 - tiny_slack))
    verts = {v for n in seen - glass for v in tris[n][3]}
    kept = {}
    for n, (key, k, _, vids) in enumerate(tris):
        if n in seen or any(v in verts for v in vids):
            kept.setdefault(key, set()).add(k)
    return kept


def _sight_reference(sections, eye, resolution=128, triangles=None):
    """GlassSight's depth faces, as GlassSight built them."""
    half = resolution / 2
    tris = [[tuple(p[a] - eye[a] for a in range(3)) for p in t] for t in (triangles or [])]
    for s in sections:
        pool = s.lod0_vertices
        for sub in s.subsections:
            if sub.lod != 0 or fm.classify_cockpit(sub.material)[0] == fm.GLASS:
                continue
            for t in sub.triangles():
                tris.append([tuple(pool[i].position[a] - eye[a] for a in range(3)) for i in t])
    rel = np.array(tris, dtype=np.float64).reshape(-1, 3, 3)
    faces = {}
    for axis, sign in cockpit.CUBE_FACES:
        depth = np.zeros(resolution * resolution)
        parts, owner = _screen_parts_reference(rel, axis, sign, half)
        _raster_reference(parts, owner, resolution, depth)
        faces[(axis, sign)] = depth.tolist()
    return faces


# --- cockpit: random scenes ------------------------------------------------------------


def _vertex(p) -> carbin.Vertex:
    return carbin.Vertex(tuple(p), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0, 1.0))


def _cabin(rng: random.Random, triangles: int, spread: float = 1.2) -> list[carbin.Section]:
    """A random cabin around the origin: triangles from sub-pixel to larger than
    a cube face, some through the near planes or with a vertex exactly on one,
    shared vertices, duplicates and coplanar copies (equal depths), glass, a
    degenerate triangle, an empty subsection and a LOD-1 one."""
    sections = []
    for name, material in (("dash", "plastic"), ("glassf", "cockpit_glass"), ("seat", "leather"), ("roof", "cloth")):
        verts, indices = [], []
        for _ in range(triangles):
            c = [rng.uniform(-spread, spread) for _ in range(3)]
            size = rng.choice([0.002, 0.01, 0.05, 0.2, 0.6, 2.5])
            base = len(verts)
            pts = [[c[a] + rng.uniform(-size, size) for a in range(3)] for _ in range(3)]
            if rng.random() < 0.05:  # a vertex on a near plane of the first eye
                pts[0][rng.randrange(3)] = rng.choice([1, -1]) * cockpit.NEAR_PLANE
            verts += [_vertex(p) for p in pts]
            indices += [base, base + 1, base + 2]
            if rng.random() < 0.2:  # a neighbour sharing an edge
                verts.append(_vertex([c[a] + rng.uniform(-size, size) for a in range(3)]))
                indices += [base + 1, base + 2, base + 3]
            if rng.random() < 0.05:  # the same triangle twice
                indices += [base, base + 1, base + 2]
            if rng.random() < 0.05:  # the same plane, another winding
                indices += [base + 2, base + 1, base]
        verts += [_vertex((0.3, 0.1, -0.5))] * 3  # degenerate
        indices += [len(verts) - 3, len(verts) - 2, len(verts) - 1]
        subs = [
            carbin.SubSection(material, 0, 4, (0.0, 1.0, 0.0, 1.0) * 2, indices),
            carbin.SubSection(material, 0, 4, (0.0, 1.0, 0.0, 1.0) * 2, []),
            carbin.SubSection(material, 1, 4, (0.0, 1.0, 0.0, 1.0) * 2, indices[:6]),
        ]
        sections.append(carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod0_vertices=verts, subsections=subs))
    return sections


def _scene_parts(rng: random.Random, sections, eye, half):
    tris, skip = [], []
    for s in sections:
        for sub in s.subsections:
            if sub.lod == 0:
                for t in sub.triangles():
                    tris.append([s.lod0_vertices[i].position for i in t])
                    skip.append(fm.classify_cockpit(sub.material)[0] == fm.GLASS)
    rel = np.array(tris, dtype=np.float64).reshape(-1, 3, 3) - np.array(eye)
    return rel, np.array(skip)


def test_screen_parts_match_the_clip_loop():
    rng = random.Random(11)
    for triangles in (40, 300):
        sections = _cabin(rng, triangles, spread=0.4)
        rel, skip = _scene_parts(rng, sections, (0.0, 0.0, 0.0), 32.0)
        for axis, sign in cockpit.CUBE_FACES:
            for mask in (None, skip):
                parts, owner = cockpit._screen_parts(rel, axis, sign, 32.0, mask)
                want_parts, want_owner = _screen_parts_reference(rel, axis, sign, 32.0, mask)
                assert np.array_equal(owner, want_owner)
                assert parts.tobytes() == want_parts.tobytes()


def test_raster_matches_the_chunked_raster():
    rng = random.Random(7)
    tile, chunk = visibility.RASTER_TILE, 1 << 16
    try:
        for resolution, triangles in ((24, 60), (48, 200), (72, 100)):
            sections = _cabin(rng, triangles)
            rel, skip = _scene_parts(rng, sections, (0.02, -0.01, 0.03), resolution / 2)
            for axis, sign in cockpit.CUBE_FACES:
                parts, owner = cockpit._screen_parts(rel, axis, sign, resolution / 2, skip)
                if rng.random() < 0.3:  # equal depths: every part at the same 1 / z
                    parts[:, :, 2] = 0.5
                for visibility.RASTER_TILE, chunk in ((1, 1 << 16), (2, 300), (8, 1 << 16), (16, 1000), (64, 1 << 20)):
                    depth = np.zeros(resolution * resolution)
                    ids = np.full(resolution * resolution, -1, dtype=np.int64)
                    depth[::7] = 0.3  # a depth to beat
                    want_depth, want_ids = depth.copy(), ids.copy()
                    got = cockpit._raster(parts, owner, resolution, depth, ids, chunk=chunk)
                    want = _raster_reference(parts, owner, resolution, want_depth, want_ids)
                    assert all(np.array_equal(a, b) for a, b in zip(got, want, strict=True))
                    assert depth.tobytes() == want_depth.tobytes() and np.array_equal(ids, want_ids)
    finally:
        visibility.RASTER_TILE = tile


def test_visible_triangles_match_the_numpy_raster():
    rng = random.Random(3)
    for triangles, resolution in ((80, 32), (200, 48), (100, 64)):
        sections = _cabin(rng, triangles)
        eyes = [(0.0, 0.0, 0.0), (0.12, 0.0, 0.0), (0.0, 0.08, -0.05), (0.01, 0.0, 0.0)]
        assert cockpit.visible_triangles(sections, eyes, resolution) == _visible_reference(sections, eyes, resolution)
    assert cockpit.visible_triangles([], [(0.0, 0.0, 0.0)], 16) == _visible_reference([], [(0.0, 0.0, 0.0)], 16)


def test_glass_sight_matches_the_numpy_raster():
    rng = random.Random(5)
    sections = _cabin(rng, 150)
    eye = (0.05, -0.02, 0.01)
    place = type("Place", (), {"dy": 0.0, "dz": 0.0})()
    extra = [[(rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1)) for _ in range(3)] for _ in range(30)]
    for triangles in (None, extra):
        sight = cockpit.GlassSight(sections, eye, place, resolution=48, triangles=triangles)
        want = _sight_reference(sections, eye, resolution=48, triangles=triangles)
        assert sight.faces == want


def _gauge_fraction_reference(sight: cockpit.GaugeSight, eye, points) -> float:
    """The original GaugeSight.fraction: each point's box test over every triangle."""
    points = np.array(points, dtype=np.float64).reshape(-1, 3)
    if not len(points):
        return 1.0
    eye = np.array(eye, dtype=np.float64)
    p0, e1, e2 = sight.tris[:, 0], sight.e1, sight.e2
    seen = 0
    for p in points:
        d = p - eye
        length = float(np.sqrt(d @ d))
        d = d / length
        lo, hi = np.minimum(eye, p) - 1e-4, np.maximum(eye, p) + 1e-4
        box = ((sight.hi >= lo) & (sight.lo <= hi)).all(axis=1)
        h = np.cross(d, e2[box])
        a = (e1[box] * h).sum(axis=1)
        ok = np.abs(a) > 1e-12
        f = np.where(ok, 1 / np.where(ok, a, 1), 0)
        sv = eye - p0[box]
        u = f * (sv * h).sum(axis=1)
        q = np.cross(sv, e1[box])
        v = f * (q @ d)
        t = f * (e2[box] * q).sum(axis=1)
        clear = length - cockpit.GAUGE_SIGHT_CLEARANCE
        hit = ok & (u >= 0) & (u <= 1) & (v >= 0) & (u + v <= 1) & (t > 1e-4) & (t < clear)
        seen += not hit.any()
    return seen / len(points)


def test_gauge_sight_matches_every_triangle():
    rng = random.Random(9)
    for triangles in (30, 200, 600):
        sections = _cabin(rng, triangles, spread=0.6)
        points = [tuple(rng.uniform(-0.5, 0.5) for _ in range(3)) for _ in range(rng.choice([1, 12, 40]))]
        sight = cockpit.GaugeSight(sections, points)
        for _ in range(6):
            eye = tuple(rng.uniform(-0.6, 0.6) for _ in range(3))
            if rng.random() < 0.3:
                eye = points[0]  # a point at the eye
            with np.errstate(invalid="ignore"):
                got, want = sight(eye), _gauge_fraction_reference(sight, eye, points)
            assert got == want
        assert sight.fraction((0.0, 0.0, 0.0), []) == 1.0


# --- dsf_materials: region cut, atlas render, decoded atlases ----------------------------


def _uv_triangles(rng: random.Random, count: int) -> list:
    """UV triangles in islands: separated by seams, touching (no seam), an L of
    islands (no seam, but a split that saves area), large and sliver triangles,
    UVs past 0..1 and on texel edges."""
    tris = []
    islands = [(rng.uniform(-0.1, 0.9), rng.uniform(-0.1, 0.9), rng.uniform(0.01, 0.3)) for _ in range(6)]
    islands += [(0.1, 0.1, 0.05), (0.1, 0.16, 0.05), (0.16, 0.1, 0.05)]  # an L
    for _ in range(count):
        u, v, size = rng.choice(islands)
        if rng.random() < 0.1:
            tris.append([(round(u * 64) / 64, round(v * 64) / 64)] * 2 + [(u + size, v)])
            continue
        tris.append([(u + rng.uniform(0, size), v + rng.uniform(0, size)) for _ in range(3)])
    return tris


def test_find_regions_match_the_box_loop():
    rng = random.Random(53)
    for count, size in ((1, (64, 64)), (40, (64, 32)), (400, (256, 256)), (3000, (1024, 512))):
        tris = _uv_triangles(rng, count)
        assert dm.find_regions(tris, size) == dm._find_regions_loop(tris, size)
    # Bars with no seam between them: _best_split cuts an L and a staircase, not a cross.
    shapes = [
        ([(0, 0, 1, 0.2), (0, 0.2, 0.2, 1)], 2),
        ([(0, 0, 0.3, 0.3), (0.25, 0.25, 0.55, 0.55), (0.5, 0.5, 0.8, 0.8), (0.75, 0.75, 1, 1)], 4),
        ([(0.4, 0, 0.6, 1), (0, 0.4, 1, 0.6)], 1),
    ]
    for bars, regions in shapes:
        tris = []
        for u0, v0, u1, v1 in bars:
            for _ in range(60):
                tris.append([(rng.uniform(u0, u1), rng.uniform(v0, v1)) for _ in range(3)])
            tris.append([(u0, v0), (u1, v0), (u1, v1)])
        for size in ((256, 256), (512, 128)):
            got = dm.find_regions(tris, size)
            assert got == dm._find_regions_loop(tris, size) and len(got[0]) == regions
    # Equal totals and equal sort keys: a symmetric L of grid-aligned, repeated
    # triangles (the loop keeps the largest k, x before y, ids in input order).
    cells = [(i, 0) for i in range(8)] + [(0, j) for j in range(1, 8)]
    tris = [[(i / 8, j / 8), ((i + 1) / 8, j / 8), (i / 8, (j + 1) / 8)] for i, j in cells for _ in range(3)]
    for order in (tris, tris[::-1], rng.sample(tris, len(tris))):
        got = dm.find_regions(order, (128, 128))
        assert got == dm._find_regions_loop(order, (128, 128)) and len(got[0]) == 2
    # Two such Ls apart: each is a seam group first (in _groups' order), then split.
    tris = [[(u / 2.5 + du, v / 2.5) for u, v in t] for du in (0.0, 0.55) for t in tris]
    for order in (tris, rng.sample(tris, len(tris))):
        got = dm.find_regions(order, (256, 256))
        assert got == dm._find_regions_loop(order, (256, 256)) and len(got[0]) == 4
    tris = _uv_triangles(rng, 50)
    tris[7] = [(float("nan"), 0.5), (0.2, 0.2), (0.3, 0.3)]  # NaN: the loop (max(0.0, nan) is 0.0)
    assert dm.find_regions(tris, (128, 128)) == dm._find_regions_loop(tris, (128, 128))
    tris = [[(0.1, 0.1), (0.2, 0.2), (float("inf"), -float("inf"))]] + _uv_triangles(rng, 30)
    assert dm.find_regions(tris, (128, 128)) == dm._find_regions_loop(tris, (128, 128))


def test_region_cut_steps_match_the_loops():
    # _groups_np and _best_split_np against _groups and _best_split, ids in any
    # order, on integer boxes with many equal starts, ends and centres.
    rng = random.Random(61)
    for count in (1, 2, 5, 40, 300):
        boxes = []
        for _ in range(count):
            x0, y0 = rng.randrange(0, 120, 4), rng.randrange(0, 120, 4)
            boxes.append((x0, y0, x0 + rng.choice((1, 2, 4, 8, 30, 90)), y0 + rng.choice((1, 3, 4, 60))))
        array = np.array(boxes, dtype=np.int64)
        ids = rng.sample(range(count), count)
        for axis in (0, 1):
            want = dm._groups(ids, boxes, axis)
            assert [g.tolist() for g in dm._groups_np(np.array(ids), array, axis)] == want
        for min_area in (1, 64 * 64):
            want = dm._best_split(ids, boxes, min_area=min_area)
            got = dm._best_split_np(np.array(ids), array, min_area=min_area)
            assert (got is None and want is None) or [h.tolist() for h in got] == want


def test_atlas_render_matches_the_texel_loop_on_many_regions():
    rng = random.Random(59)

    class Atlases:
        def __init__(self):
            self.maps = {
                fm.NODAMAGE: (96, 80, bytearray(rng.randrange(256) for _ in range(96 * 80 * 4))),
                fm.INTERIOR: (64, 64, bytearray(rng.choice((0, 15, 128, 201, 255)) for _ in range(64 * 64 * 4))),
            }

        def get(self, family):
            return self.maps[family]

    t = dm.Texturing(
        Path(),
        size=(128, 128),
        families=(fm.NODAMAGE, fm.INTERIOR),
        cutout_colour=(34, 34, 36, 0),
        lift={fm.NODAMAGE: 25},
        tone={fm.INTERIOR: (1.4, 1.2)},
    )
    t.atlases = Atlases()
    t.regions = {
        fm.NODAMAGE: [
            dm.Region(fm.NODAMAGE, (0, 0, 30, 20), (0, 0), (20, 14)),
            dm.Region(fm.NODAMAGE, (50, 40, 96, 80), (24, 0), (40, 34)),
            dm.Region(fm.NODAMAGE, (10, 60, 12, 61), (70, 0), (9, 9)),
            dm.Region(fm.NODAMAGE, (90, 0, 96, 6), (80, 0), (0, 5)),  # empty
        ],
        fm.INTERIOR: [
            dm.Region(fm.INTERIOR, (5, 5, 60, 40), (0, 40), (70, 45)),
            dm.Region(fm.INTERIOR, (40, 50, 64, 64), (72, 40), (30, 18)),
        ],
    }
    t.scale = {fm.NODAMAGE: 0.7, fm.INTERIOR: 1.3}
    t.alpha_only = {(fm.INTERIOR, 1)}
    t.solids = {("paint",): (120, 120)}
    t.solid_colours = {("paint",): t.plain}
    assert t.render() == t._render_texels()


def test_decoded_atlases_are_shared_and_copied():
    from dsfforza.formats import xds

    calls = []
    untile, decode = xds.untile, xds.decode_rgba
    try:
        xds.untile = lambda data: (data, 2, 1, b"DXT5")
        xds.decode_rgba = lambda blocks, w, h, fourcc: calls.append(blocks) or bytearray(blocks)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / fm.FAMILY_FILES[fm.NODAMAGE][0]
            path.write_bytes(bytes(range(8)))
            a, b = fm._decoded(path), fm._decoded(path)
            assert a == b == (2, 1, bytes(range(8))) and len(calls) == 1
            path.write_bytes(bytes(range(12)))  # changed (size): decoded again
            assert fm._decoded(path)[2] == bytes(range(12)) and len(calls) == 2
            first, second = fm.AtlasCache(Path(folder)), fm.AtlasCache(Path(folder))
            assert fm.fill_transparent(first.get(fm.NODAMAGE), (99, 99, 99), below=255) == 3
            assert second.get(fm.NODAMAGE)[2] == bytearray(range(12))  # its own copy
            assert isinstance(second.get(fm.NODAMAGE)[2], bytearray) and len(calls) == 2
    finally:
        xds.untile, xds.decode_rgba = untile, decode


# --- wwise_bank: the references ---------------------------------------------------------


def _encode_ima_reference(samples):
    n = -(-len(samples) // wb.BLOCK_SAMPLES) * wb.BLOCK_SAMPLES
    s = [max(-32768, min(32767, int(v))) for v in samples] + [0] * (n - len(samples))
    out = bytearray()
    idx = None
    for b in range(0, n, wb.BLOCK_SAMPLES):
        x = s[b : b + wb.BLOCK_SAMPLES]
        best = None
        near = range(89) if idx is None else sorted({max(0, min(88, idx + d)) for d in (-8, -4, -2, 0, 2, 4, 8)})
        for cand in near:
            body, end, err = wb._encode_block(x, cand)
            if best is None or err < best[2]:
                best = (body, end, err, cand)
        body, idx, _, start = best
        out += struct.pack("<hBB", x[0], start, 0) + body
    return bytes(out)


def _decode_ima_reference(data: bytes, channels: int = 1):
    out = [[] for _ in range(channels)]
    span = wb.BLOCK * channels
    for base in range(0, len(data) - span + 1, span):
        for ch in range(channels):
            b = base + ch * wb.BLOCK
            pred, idx = struct.unpack_from("<hB", data, b)
            idx = min(idx, 88)
            samples = [pred]
            for byte in data[b + 4 : b + 4 + wb.BLOCK_SAMPLES // 2]:
                for nib in (byte & 0xF, byte >> 4):
                    m = nib & 7
                    pred = pred - wb._DIFF[idx][m] if nib & 8 else pred + wb._DIFF[idx][m]
                    pred = -32768 if pred < -32768 else (32767 if pred > 32767 else pred)
                    idx = wb._NEXT[idx][m]
                    samples.append(pred)
            out[ch].extend(samples[: wb.BLOCK_SAMPLES])
    return out


def _wav_frames_reference(channels) -> bytes:
    n = min(len(c) for c in channels)
    frames = bytearray(2 * n * len(channels))
    for k, c in enumerate(channels):
        clipped = [max(-32768, min(32767, int(v))) for v in c[:n]]
        frames[2 * k :: 2 * len(channels)] = bytes(v & 0xFF for v in clipped)
        frames[2 * k + 1 :: 2 * len(channels)] = bytes((v >> 8) & 0xFF for v in clipped)
    return bytes(frames)


def _sounds(rng: random.Random) -> list:
    """Engine-like loops (harmonics, noise, level changes), clipping, silence,
    steps, white noise; lengths that aren't whole blocks, one block and none."""
    out = []
    for k in range(5):
        f, n = rng.uniform(0.01, 0.2), rng.randint(500, 4000)
        level = [rng.uniform(0.05, 1.0) for _ in range(8)]
        out.append(
            [
                int(
                    level[i * 8 // n] * 26000 * (math.sin(i * f) + 0.3 * math.sin(i * f * 3.1 + k))
                    + rng.randint(-2000, 2000)
                )
                for i in range(n)
            ]
        )
    out.append([32767, -32768] * 100 + [0] * 70 + [40000, -50000] * 10)
    out.append([rng.choice((-30000, 0, 30000)) for _ in range(64)] * 9)
    out.append([rng.randint(-32768, 32767) for _ in range(1000)])
    out.append([0] * 64)
    out.append([5, -3, 7])
    out.append([])
    return out


def test_ima_encoder_matches_the_block_loop():
    rng = random.Random(29)
    sounds = _sounds(rng)
    want = [_encode_ima_reference(s) for s in sounds]
    rounds = wb.ENCODE_ROUNDS
    try:
        for wb.ENCODE_ROUNDS in (rounds, 0, 1, 2):  # 0 and 1: guesses left unsettled, the loop decides
            assert wb.encode_ima_many(sounds) == want
        wb.ENCODE_ROUNDS = rounds
        assert [wb.encode_ima(s) for s in sounds] == want
        assert wb.encode_ima_many([np.array(s, dtype=np.int64) for s in sounds]) == want
        assert wb.encode_ima_many([np.clip(s, -32768, 32767).astype(np.int16) for s in sounds[:4]]) == want[:4]
        floats = [[v + 0.7 for v in s] for s in sounds[:3]]  # int() truncates toward zero
        assert wb.encode_ima_many(floats) == [_encode_ima_reference(s) for s in floats]
        assert wb.encode_ima_many([]) == []
    finally:
        wb.ENCODE_ROUNDS = rounds


def test_ima_decoder_matches_the_sample_loop():
    rng = random.Random(31)
    for sound in _sounds(rng)[:6]:
        data = wb.encode_ima(sound)
        assert wb.decode_ima(data) == _decode_ima_reference(data)
    for channels in (1, 2, 3):
        data = bytes(rng.getrandbits(8) for _ in range(wb.BLOCK * channels * 7 + 5))  # step indices past 88 too
        assert wb.decode_ima(data, channels) == _decode_ima_reference(data, channels)
        assert [a.tolist() for a in wb.decode_ima_arrays(data, channels)] == _decode_ima_reference(data, channels)
    assert wb.decode_ima(b"\x00" * 10) == [[]]


def test_wav_writer_matches_the_sample_loop():
    import wave

    rng = random.Random(37)
    cases = [
        [[1.7, -2.9, 40000, -40000.5, 12], [3, 4, -5, 6, 7, 8]],
        [np.array([rng.randint(-40000, 40000) for _ in range(300)])],
        [[rng.randint(-32768, 32767) for _ in range(301)], np.array([rng.uniform(-40000, 40000) for _ in range(299)])],
        [np.array([0, 1, 2], dtype=np.uint8), [True, False, True]],
    ]
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "x.wav"
        for channels in cases:
            wb.write_wav(path, channels, 22050)
            with wave.open(str(path)) as w:
                assert w.getnchannels() == len(channels)
                assert w.readframes(w.getnframes()) == _wav_frames_reference(channels)


# --- engine_sound ------------------------------------------------------------------------


def _seamless_reference(x, rate, max_seconds, fade=0.06):
    n = len(x)
    if n <= max_seconds * rate:
        return x
    xf = int(fade * rate)
    target = int(max_seconds * rate)
    tail_ref = x[n - xf :]
    best, best_len = -2.0, target
    for length in range(max(xf * 2, target - rate // 10), target + 1):
        seg = x[length - xf : length]
        c = float(np.dot(seg, tail_ref) / (np.linalg.norm(seg) * np.linalg.norm(tail_ref) + 1e-12))
        if c > best:
            best, best_len = c, length
    y = x[:best_len].copy()
    w = np.sin(np.linspace(0, math.pi / 2, xf)) ** 2
    y[best_len - xf :] = y[best_len - xf :] * (1 - w) + tail_ref * w
    return y


def test_seamless_matches_the_cut_loop():
    rng = np.random.default_rng(41)
    rate = 8000
    t = np.arange(int(2.5 * rate)) / rate
    signals = [
        np.sin(2 * np.pi * 37.0 * t) + 0.3 * np.sin(2 * np.pi * 111.0 * t),
        np.sin(2 * np.pi * 40.0 * t),  # periodic: equal correlations a period apart
        rng.normal(size=len(t)),
        np.zeros(len(t)),
        np.r_[rng.normal(size=len(t) // 2), np.zeros(len(t) - len(t) // 2)],
        np.where(np.sin(2 * np.pi * 25.0 * t) > 0, 0.5, -0.5),
    ]
    for x in signals:
        for seconds in (1.1, 0.6):
            assert engine_sound.seamless(x, rate, seconds).tobytes() == _seamless_reference(x, rate, seconds).tobytes()
    short = rng.normal(size=1000)
    assert engine_sound.seamless(short, rate, 1.1) is short


def test_seamless_keeps_the_first_of_equal_cuts():
    # A signal whose correlation is the same at every cut: the loop keeps the first.
    rate = 4000
    x = np.ones(int(1.5 * rate))
    assert engine_sound.seamless(x, rate, 1.1).tobytes() == _seamless_reference(x, rate, 1.1).tobytes()
    lengths = range(400, 600)
    assert engine_sound._cut_candidates(x, 240, lengths, x[-240:]) == list(lengths)


def test_warp_many_matches_warp():
    rng = random.Random(43)
    xs = np.r_[np.linspace(-100, 13000, 997), [0.0, 800.0, 6500.0, 12000.0]]
    curves = [
        [(0.0, 0.0), (800.0, 10.0), (800.0, 25.0), (6500.0, 255.0), (12000.0, 255.0)],
        [(0, -1200), (1000, 0), (7000, 2400)],  # integers
        [(500.0, 3.0)],
        [(0.0, 1.0), (5000.0, 0.5), (3000.0, 2.0), (9000.0, 0.0)],  # not increasing
        [(rng.uniform(0, 12000), rng.uniform(-2400, 2400)) for _ in range(9)],
    ]
    for points in curves:
        want = np.array([engine_sound.warp(points, x) for x in xs], dtype=np.float64)
        assert engine_sound.warp_many(points, xs).tobytes() == want.tobytes()
        triples = [(x, y, 4) for x, y in points]
        want = np.array([engine_sound.curve_at(triples, x) for x in xs], dtype=np.float64) / 255
        assert (engine_sound.curve_at_many(triples, xs) / 255).tobytes() == want.tobytes()


def test_sweep_resampling_matches_the_modulo_form():
    rng = np.random.default_rng(47)
    for length in (64, 1000, 4097):
        src = rng.normal(size=length)
        cents = np.r_[np.full(500, -2400.0), np.linspace(-2400, 2400, 3000), np.full(300, 1800.0)]
        gain = rng.uniform(0, 1, size=len(cents))
        pos = np.cumsum(2 ** (cents / 1200)) % len(src)
        i0 = pos.astype(int)
        frac = pos - i0
        want = gain * (src[i0] * (1 - frac) + src[(i0 + 1) % len(src)] * frac)
        assert (gain * engine_sound._play(src, cents)).tobytes() == want.tobytes()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
