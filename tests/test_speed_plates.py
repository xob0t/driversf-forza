"""plates.depth_map (all triangles' box cells at once) and lod_depths' triangle
filter give exactly what the loops before them gave. The reference below is the
original per-triangle z-buffer, kept here only to compare against, on random
scenes with ties, slivers, triangles off the map and chunk boundaries.

uv run python tests/test_speed_plates.py     # or: uv run python -m tests speed_plates
"""

from __future__ import annotations

import random

import numpy as np

from dsfforza.convert import plates as pm


def _depth_map_reference(tris, end: str, s_range, y_range) -> pm.DepthMap:
    """The original loop: one triangle at a time, a strict `<` z-buffer."""
    s0, s1 = s_range
    y0, y1 = y_range
    width, height = max(1, int(round((s1 - s0) / pm.PX))), max(1, int(round((y1 - y0) / pm.PX)))
    depth = np.full((height, width), np.inf)
    cls = np.zeros((height, width), np.int8)
    sec = np.full((height, width), -1, np.int32)
    feat = np.zeros((height, width), np.int8)
    names: list = []
    index: dict = {}
    for pts, c, name, f in tris:
        v = [pm.view_coords(end, p) for p in pts]
        q = [(a[0] - s0) / pm.PX for a in v]
        r = [(y1 - a[1]) / pm.PX for a in v]
        area = (q[1] - q[0]) * (r[2] - r[0]) - (r[1] - r[0]) * (q[2] - q[0])
        if abs(area) < 1e-9:
            continue
        cx0, cx1 = max(int(min(q)), 0), min(int(max(q)) + 1, width)
        cy0, cy1 = max(int(min(r)), 0), min(int(max(r)) + 1, height)
        if cx0 >= cx1 or cy0 >= cy1:
            continue
        xs, ys = np.meshgrid(np.arange(cx0, cx1) + 0.5, np.arange(cy0, cy1) + 0.5)
        w0 = ((q[1] - xs) * (r[2] - ys) - (r[1] - ys) * (q[2] - xs)) / area
        w1 = ((q[2] - xs) * (r[0] - ys) - (r[2] - ys) * (q[0] - xs)) / area
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not inside.any():
            continue
        d = w0 * v[0][2] + w1 * v[1][2] + w2 * v[2][2]
        win = depth[cy0:cy1, cx0:cx1]
        better = inside & (d < win)
        if not better.any():
            continue
        win[better] = d[better]
        cls[cy0:cy1, cx0:cx1][better] = c
        feat[cy0:cy1, cx0:cx1][better] = f
        if name not in index:
            index[name] = len(names)
            names.append(name)
        sec[cy0:cy1, cx0:cx1][better] = index[name]
    return pm.DepthMap(end, s0, y1, depth, cls, sec, names, feat)


def _same(a: pm.DepthMap, b: pm.DepthMap) -> None:
    assert (a.end, a.s0, a.y1, a.names) == (b.end, b.s0, b.y1, b.names)
    for f in ("depth", "cls", "sec", "feat"):
        x, y = getattr(a, f), getattr(b, f)
        assert x.dtype == y.dtype and x.shape == y.shape and x.tobytes() == y.tobytes(), f


def _scene(rng: random.Random, n: int) -> list:
    tris = []
    for _ in range(n):
        c = [rng.uniform(-1.0, 1.0), rng.uniform(-0.1, 1.8), rng.uniform(-2.5, 2.5)]
        size = rng.choice([0.002, 0.01, 0.03, 0.2, 1.5])
        pts = [tuple(c[a] + rng.uniform(-size, size) for a in range(3)) for _ in range(3)]
        if rng.random() < 0.05:  # a sliver
            pts[2] = pts[1]
        if rng.random() < 0.15 and tris:  # the same face again (equal depths)
            pts = list(rng.choice(tris)[0])
        if rng.random() < 0.1:  # on cell centres and edges
            pts = [tuple(round(x / pm.PX * 2) * pm.PX / 2 for x in p) for p in pts]
        tris.append((pts, rng.randint(0, 5), f"s{rng.randint(0, 80)}", rng.randint(0, 4)))
    return tris


def test_depth_map_matches_the_loop():
    rng = random.Random(11)
    chunk = pm.DEPTH_CHUNK
    try:
        for _ in range(40):
            pm.DEPTH_CHUNK = rng.choice([1, 7, 300, chunk])
            tris = _scene(rng, rng.choice([0, 1, 5, 60, 400]))
            end = rng.choice(["rear", "front"])
            s = rng.uniform(0.05, 0.9)
            y0 = rng.uniform(-0.2, 0.6)
            ranges = rng.choice([((-s, s), (y0, y0 + 1.5)), ((0.1, 0.1 + s / 3), (y0, y0 + 0.12))])
            _same(_depth_map_reference(tris, end, *ranges), pm.depth_map(tris, end, *ranges))
    finally:
        pm.DEPTH_CHUNK = chunk


def test_lod_depths_filter_matches_the_loop():
    rng = random.Random(5)
    for _ in range(30):
        tris = _scene(rng, 200)
        corners = np.array([t[0] for t in tris], float).reshape(-1, 3, 3)
        end = rng.choice(["rear", "front"])
        s, y, w, h = rng.uniform(-0.5, 0.5), rng.uniform(0, 1.2), rng.uniform(0.1, 0.5), rng.uniform(0.05, 0.3)
        want = [t for t in tris if any(abs(pm.view_coords(end, p)[0] - s) < w and abs(p[1] - y) < h for p in t[0])]
        across = -corners[:, :, 0] if end == "rear" else corners[:, :, 0]
        near = ((np.abs(across - s) < w) & (np.abs(corners[:, :, 1] - y) < h)).any(1)
        assert [tris[i] for i in np.flatnonzero(near).tolist()] == want


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
