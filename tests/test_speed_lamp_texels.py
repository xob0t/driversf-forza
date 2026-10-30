"""lenses._texels_many gives exactly what lenses._texels gives triangle by triangle, on random
UV triangles of every kind (inside, across and outside the atlas, slivers, degenerate).

uv run python tests/test_speed_lamp_texels.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import numpy as np

from dsfforza.convert import lenses


def _random_triangles(rng, n, width, height):
    kind = rng.integers(0, 8, n)
    a = rng.uniform(-0.3, 1.3, (n, 2)) * (width, height)
    size = np.where(kind[:, None] < 2, 0.3, np.where(kind[:, None] < 5, 3.0, 30.0))
    pts = a[:, None] + rng.normal(0, 1, (n, 3, 2)) * size[:, :, None]
    pts[kind == 5, 2] = pts[kind == 5, 0]  # degenerate
    pts[kind == 6] = np.round(pts[kind == 6])  # corners on texel corners: centres on the edges
    pts[kind == 7, :, 1] = pts[kind == 7, :1, 1] + 1e-11 * rng.normal(size=(int((kind == 7).sum()), 3))
    return pts


def test_texels_many_matches_texels():
    rng = np.random.default_rng(4)
    for case in range(10):
        width, height = int(rng.choice([4, 64, 512])), int(rng.choice([4, 64, 256]))
        pts = _random_triangles(rng, int(rng.choice([0, 1, 2, 500, 3000])), width, height)
        if case == 2:
            lenses.TEXELS_CHUNK, chunk = 40, lenses.TEXELS_CHUNK  # many chunks, triangles bigger than one
        # big boxes one by one (the default), every box one by one, none
        lenses.TEXELS_ONE_BY_ONE, one_by_one = (256, 0, 10**9)[case % 3], lenses.TEXELS_ONE_BY_ONE
        # and with any number of small boxes batched (the default needs TEXELS_BATCH_MIN)
        lenses.TEXELS_BATCH_MIN, batch_min = (8, 0, 1)[case % 3], lenses.TEXELS_BATCH_MIN
        ix, iy, bary, at = lenses._texels_many(pts)
        lenses.TEXELS_ONE_BY_ONE, lenses.TEXELS_BATCH_MIN = one_by_one, batch_min
        if case == 2:
            lenses.TEXELS_CHUNK = chunk
        assert len(at) == len(pts) + 1 and at[0] == 0 and at[-1] == len(ix) == len(iy) == len(bary)
        for k, tri in enumerate(pts):
            want = lenses._texels(tri.copy())
            got = (ix[at[k] : at[k + 1]], iy[at[k] : at[k + 1]], bary[at[k] : at[k + 1]])
            for g, w in zip(got, want, strict=True):
                assert g.dtype == w.dtype and np.array_equal(g, w), (case, k, tri)


def test_texels_many_non_finite_raises_as_texels():
    pts = np.array([[[0.0, 0.0], [4.0, 0.0], [0.0, 4.0]], [[np.nan, 0.0], [1.0, 1.0], [0.0, 2.0]]])
    for fn in (lenses._texels_many, lambda p: [lenses._texels(t) for t in p]):
        try:
            fn(pts)
        except ValueError:
            continue
        raise AssertionError("no error")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
