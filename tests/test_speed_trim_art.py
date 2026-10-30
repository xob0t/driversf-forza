"""trim_art's batched texel lookups give exactly what the per-triangle ones give, on random
triangles of every kind (inside, across the edges, outside, slivers, degenerate, non-finite).

uv run python tests/test_speed_trim_art.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import math

import numpy as np

from dsfforza.convert import forza_materials as fm
from dsfforza.convert import trim_art
from dsfforza.formats import carbin


def _random_triangles(rng, n, width, height):
    kind = rng.integers(0, 8, n)
    a = rng.uniform(-0.2, 1.2, (n, 2)) * (width, height)
    size = np.where(kind[:, None] < 2, 0.3, np.where(kind[:, None] < 5, 4.0, 40.0))
    pts = a[:, None] + rng.normal(0, 1, (n, 3, 2)) * size[:, :, None]
    pts[kind == 5, 2] = pts[kind == 5, 0]  # degenerate: two corners the same
    pts[kind == 6] = np.round(pts[kind == 6])  # corners on texel corners: centres on the edges
    pts[kind == 7, :, 1] = pts[kind == 7, :1, 1] + 1e-13 * rng.normal(size=(int((kind == 7).sum()), 3))  # slivers
    pts[rng.random(n) < 0.02, rng.integers(0, 3), 0] = np.nan
    pts[rng.random(n) < 0.01, 1, 1] = np.inf
    pts[rng.random(n) < 0.01] *= 1e17  # far outside
    return pts


def test_triangles_cells_match_triangle_cells():
    rng = np.random.default_rng(11)
    for case in range(12):
        width, height = int(rng.choice([1, 7, 64, 512])), int(rng.choice([1, 9, 64, 256]))
        pts = _random_triangles(rng, int(rng.choice([0, 1, 3, 400, 3000])), width, height)
        if case == 3:
            trim_art.CELLS_CHUNK, chunk = 50, trim_art.CELLS_CHUNK  # many chunks, triangles bigger than one
        rows, cols, offsets = trim_art.triangles_cells((height, width), pts)
        if case == 3:
            trim_art.CELLS_CHUNK = chunk
        assert len(offsets) == len(pts) + 1 and offsets[0] == 0
        for k, tri in enumerate(pts):
            want_y, want_x = trim_art.triangle_cells((height, width), [tuple(map(float, p)) for p in tri])
            got = slice(offsets[k], offsets[k + 1])
            assert np.array_equal(rows[got], want_y) and np.array_equal(cols[got], want_x), (case, k, tri)
        assert offsets[-1] == len(rows) == len(cols)


def test_triangles_texels_match_triangle_texels():
    rng = np.random.default_rng(5)
    image = rng.integers(0, 256, (32, 48, 6))
    pts = _random_triangles(rng, 500, 48, 32)
    every, offsets = trim_art.triangles_texels(image, pts)
    for k, tri in enumerate(pts):
        want = trim_art.triangle_texels(image, [tuple(map(float, p)) for p in tri])
        got = every[offsets[k] : offsets[k + 1]]
        assert got.dtype == want.dtype and np.array_equal(got, want)


def test_texel_points_as_per_vertex():
    rng = np.random.default_rng(2)
    pool = [
        carbin.Vertex((0.0, 0.0, 0.0), tuple(rng.uniform(-2, 2, 2)), (0.0, 0.0), (0.0, 1.0, 0.0)) for _ in range(50)
    ]
    tris = [tuple(int(i) for i in rng.integers(0, 50, 3)) for _ in range(80)]
    m = fm.SubMaterial("misc", fm.TRIM, fm.NODAMAGE, "textured", (0.25, 0.7, -0.1, 1.3), (0, 0, 1, 1))
    pts = trim_art.texel_points(m, pool, tris, 512, 256)
    for k, tri in enumerate(tris):
        want = [((u := m.uv(pool[i].uv0))[0] * 512, u[1] * 256) for i in tri]
        assert [tuple(p) for p in pts[k].tolist()] == want
    assert trim_art.texel_points(m, pool, [], 512, 256).shape == (0, 3, 2)
    assert not math.isnan(pts.sum())


def test_lod_triangles_per_pool():
    rng = np.random.default_rng(9)
    pool = [carbin.Vertex(tuple(rng.normal(size=3)), (0.0, 0.0), (0.0, 0.0), (0.0, 1.0, 0.0)) for _ in range(30)]
    lod0 = [carbin.Vertex(tuple(rng.normal(size=3)), (0.0, 0.0), (0.0, 0.0), (0.0, 1.0, 0.0)) for _ in range(30)]
    subs = [
        carbin.SubSection(name, lod, 4, (0, 1, 0, 1, 0, 1, 0, 1), [int(i) for i in rng.integers(0, 30, 3 * n)])
        for name, lod, n in (("misc", 1, 5), ("glass", 1, 4), ("frame", 1, 3), ("misc", 0, 2), ("misc", 3, 1))
    ]
    sections = [carbin.Section("body", (0, 0, 0), (0, 0, 0), (0, 0, 0), pool, lod0, subs)]
    got = trim_art.lod_triangles(sections, 1)
    want = np.concatenate(
        [np.array([[pool[i].position for i in t] for t in sub.triangles()]) for sub in (subs[0], subs[2])]
    )
    assert np.array_equal(got, want)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
