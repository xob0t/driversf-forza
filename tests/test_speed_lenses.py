"""The fast versions of lenses.py's hot loops give exactly what the original code gave.
Each reference below is the original code, kept here only to compare against; the
inputs are random lens-like point sets (a few seconds in all).

uv run python tests/test_speed_lenses.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import numpy as np

from dsfforza.convert import lenses


def _closing_reference(pos, values, radius):
    """The original closing: every ray's distance to every other ray, a chunk at a time."""
    out = values
    for reduce, fill in ((np.max, -np.inf), (np.min, np.inf)):
        res = np.empty_like(out)
        for c0 in range(0, len(pos), lenses.RAY_CHUNK):
            near = np.linalg.norm(pos[c0 : c0 + lenses.RAY_CHUNK, None] - pos[None], axis=2) < radius
            res[c0 : c0 + lenses.RAY_CHUNK] = reduce(np.where(near, out[None], fill), axis=1)
        out = res
    return out


def _lens_points(rng, n):
    """Points on a curved lens patch (metres), a few millimetres apart, in a random order."""
    u, v = rng.uniform(-0.15, 0.15, n), rng.uniform(-0.05, 0.05, n)
    centre = rng.uniform(-1.0, 1.0, 3)
    pos = np.stack([u, v, 0.2 * u * u + 0.1 * v * v], 1) + centre
    return pos[rng.permutation(n)]


def test_closing_matches_the_distance_matrix():
    rng = np.random.default_rng(7)
    for case in range(30):
        n = int(rng.choice([0, 1, 2, 5, 255, 256, 257, 900])) if case else 3000
        pos = _lens_points(rng, n)
        if case % 5 == 1 and n:
            pos = np.round(pos / 0.004) * 0.004  # on a grid: ties at the radius
        if case % 7 == 3 and n > 2:
            pos[rng.integers(0, n, 2)] = np.nan  # a non-finite ray: near nothing
            pos[rng.integers(0, n)] = (np.inf, 0.0, 0.0)
        if case % 9 == 4:
            pos = np.concatenate([pos, pos[: n // 3]])  # rays on the same points
        m = len(pos)
        values = np.where(rng.random(m) < 0.8, rng.uniform(0.0, 0.4, m), 0.0)
        radius = float(rng.choice([lenses.GLOW_BULB, 0.004, 0.05]))
        with np.errstate(invalid="ignore"):
            got, want = lenses._closing(pos, values, radius), _closing_reference(pos, values, radius)
        assert got.dtype == want.dtype and got.shape == want.shape
        assert np.array_equal(got, want, equal_nan=True), (case, n, radius)
        assert np.array_equal(np.signbit(got), np.signbit(want))


def _intersect_reference(origins, dirs, tris):
    """The original Moller-Trumbore over every ray and triangle."""
    v0, e1, e2 = tris
    dx, dy, dz = (dirs[:, c : c + 1] for c in range(3))
    ax, ay, az = (e1[None, :, c] for c in range(3))
    bx, by, bz = (e2[None, :, c] for c in range(3))
    px, py, pz = dy * bz - dz * by, dz * bx - dx * bz, dx * by - dy * bx  # d x e2
    det = ax * px + ay * py + az * pz
    ok = np.abs(det) > 1e-12
    inv = 1.0 / np.where(ok, det, 1.0)
    sx, sy, sz = (origins[:, c : c + 1] - v0[None, :, c] for c in range(3))
    u = (sx * px + sy * py + sz * pz) * inv
    qx, qy, qz = sy * az - sz * ay, sz * ax - sx * az, sx * ay - sy * ax  # s x e1
    v = (dx * qx + dy * qy + dz * qz) * inv
    t = (bx * qx + by * qy + bz * qz) * inv
    hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0) & (t <= lenses.RAY_DEPTH)
    return np.where(hit, t, np.inf), u, v


def test_intersect_matches_at_every_hit():
    # Rays from a lens into a box of small triangles around them, some on the rays' own
    # starting plane, some degenerate or edge-on; hits on shared edges and corners too.
    rng = np.random.default_rng(3)
    hits = 0
    for case in range(60):
        n, m = int(rng.integers(0, 300)), int(rng.integers(0, 400))
        origins = _lens_points(rng, n) if n else np.zeros((0, 3))
        normal = rng.normal(size=(n, 3))
        dirs = -normal / np.linalg.norm(normal, axis=1, keepdims=True) if n else np.zeros((0, 3))
        if case % 4 == 0 and n:
            dirs = np.tile([0.0, 0.0, 1.0], (n, 1))  # along an axis
        centre = origins.mean(0) if n else np.zeros(3)
        a = centre + rng.uniform(-0.3, 0.3, (m, 3))
        b, c = a + rng.normal(0, 0.05, (m, 3)), a + rng.normal(0, 0.05, (m, 3))
        if case % 3 == 1 and m:
            a, b, c = (np.round(x / 0.01) * 0.01 for x in (a, b, c))  # a lattice: hits on edges
            b[: m // 5] = a[: m // 5]  # degenerate
        if case % 5 == 2 and m and n:
            b[: m // 4, 2] = a[: m // 4, 2] = c[: m // 4, 2] = origins[0, 2]  # edge-on to some rays
        tris = (a, b - a, c - a)
        with np.errstate(invalid="ignore", divide="ignore"):
            t, u, v = lenses._intersect(origins, dirs, tris)
            rt, ru, rv = _intersect_reference(origins, dirs, tris)
        assert np.array_equal(t, rt), case
        hit = np.isfinite(rt)
        assert np.array_equal(u[hit], ru[hit]) and np.array_equal(v[hit], rv[hit]), case
        hits += int(hit.sum())
    assert hits > 1000, hits


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
