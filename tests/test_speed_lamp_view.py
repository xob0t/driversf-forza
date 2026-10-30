"""dsf_materials._zbuffer (Texturing._lamp_view's z-buffer, all triangles at once) gives what
drawing the triangles one at a time gave, ties and all.

uv run python tests/test_speed_lamp_view.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import numpy as np

from dsfforza.convert import dsf_materials as dm


def _zbuffer_reference(pos, uvs, x0, y0, width, height):
    """The original loop of Texturing._lamp_view."""
    P, U, W, H = pos, uvs, width, height
    depth = np.full((H, W), -np.inf)
    uv = np.zeros((H, W, 2))
    for n in range(len(P)):
        X = (P[n, :, 0] - x0) / dm.VISIBLE_STEP
        Y = (P[n, :, 1] - y0) / dm.VISIBLE_STEP
        Z = P[n, :, 2]
        area = (X[1] - X[0]) * (Y[2] - Y[0]) - (Y[1] - Y[0]) * (X[2] - X[0])
        if abs(area) < 1e-9:
            continue
        gx, gy = np.meshgrid(
            np.arange(int(X.min()), int(np.ceil(X.max())) + 1) + 0.5,
            np.arange(int(Y.min()), int(np.ceil(Y.max())) + 1) + 0.5,
        )
        w0 = ((X[1] - gx) * (Y[2] - gy) - (Y[1] - gy) * (X[2] - gx)) / area
        w1 = ((X[2] - gx) * (Y[0] - gy) - (Y[2] - gy) * (X[0] - gx)) / area
        w2 = 1 - w0 - w1
        z = w0 * Z[0] + w1 * Z[1] + w2 * Z[2]
        ix, iy = gx.astype(int), gy.astype(int)
        ok = (w0 >= 0) & (w1 >= 0) & (w2 >= 0) & (ix < W) & (iy < H)
        ok &= z > depth[iy.clip(0, H - 1), ix.clip(0, W - 1)]
        ix, iy = ix[ok], iy[ok]
        depth[iy, ix] = z[ok]
        uv[iy, ix, 0] = (w0 * U[n, 0, 0] + w1 * U[n, 1, 0] + w2 * U[n, 2, 0])[ok]
        uv[iy, ix, 1] = (w0 * U[n, 0, 1] + w1 * U[n, 1, 1] + w2 * U[n, 2, 1])[ok]
    return depth, uv


def test_zbuffer_matches_one_triangle_at_a_time():
    rng = np.random.default_rng(21)
    for case in range(25):
        n = int(rng.choice([0, 1, 5, 200, 1500]))
        centre = rng.uniform(-0.8, 0.8, (n, 1, 3))
        P = centre + rng.normal(0, 1, (n, 3, 3)) * rng.choice([0.002, 0.02, 0.2], (n, 1, 1))
        if n > 4:
            P[: n // 4] = P[n // 4 : 2 * (n // 4)]  # the same triangle twice: a tie in z everywhere
            P[2 * (n // 4) : 3 * (n // 4), :, 2] = 0.1  # coplanar at one depth: ties across triangles
            P[-2, :, 2] = np.nan  # no depth: never drawn
            P[-1, 2] = P[-1, 0]  # degenerate
        U = rng.uniform(0, 1, (n, 3, 2))
        if case == 4:
            dm.ZBUFFER_CHUNK, chunk = 30, dm.ZBUFFER_CHUNK
        x0 = P[..., 0].min() if n else 0.0
        y0 = P[..., 1].min() if n else 0.0
        W = int((P[..., 0].max() - x0) / dm.VISIBLE_STEP) + 2 if n else 1
        H = int((P[..., 1].max() - y0) / dm.VISIBLE_STEP) + 2 if n else 1
        with np.errstate(invalid="ignore"):
            got = dm._zbuffer(P, U, x0, y0, W, H)
            want = _zbuffer_reference(P, U, x0, y0, W, H)
        if case == 4:
            dm.ZBUFFER_CHUNK = chunk
        for g, w in zip(got, want, strict=True):
            assert np.array_equal(g, w) and np.array_equal(np.signbit(g), np.signbit(w)), case


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
