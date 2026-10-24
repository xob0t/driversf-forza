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


# --- cockpit.visible_triangles and GlassSight -----------------------------------------


def _visible_reference(sections, eyes, resolution=256, near_keep=0.25, tiny_slack=0.03):
    """The original per-pixel loops of cockpit.visible_triangles."""
    fm = cockpit.fm
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
    fm = cockpit.fm
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
