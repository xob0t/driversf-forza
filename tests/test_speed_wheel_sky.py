"""forza2dsf.wheel_sky (the voxels filled per axis in place, the rays stepped
WHEEL_AO_BLOCK at a time) gives exactly what the code before it gave. The
reference below is that code, kept here only to compare against, on random
wheels (a few seconds in all).

uv run python tests/test_speed_wheel_sky.py     # or: uv run python -m tests speed_wheel_sky
"""

from __future__ import annotations

import itertools
import math

import numpy as np

from dsfforza.convert import forza2dsf as f2d


def _wheel_sky_reference(points, normals, triangles) -> list[float]:
    """The original: the voxels filled one (triangle count, sample) array at a
    time, the rays stepped one voxel at a time."""
    pts = np.asarray(points, float).reshape(-1, 3)
    if not len(pts):
        return []
    nrm = np.asarray(normals, float).reshape(-1, 3)
    nrm = nrm / np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
    tri = np.asarray(triangles, float).reshape(-1, 3, 3)
    h = f2d.WHEEL_AO_VOXEL
    corners = np.concatenate([tri.reshape(-1, 3), pts])
    lo = corners.min(0) - 2 * h
    dims = np.ceil((corners.max(0) + 2 * h - lo) / h).astype(int) + 1
    grid = np.zeros(dims, bool)
    if len(tri):  # each triangle sampled at half-voxel spacing
        a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
        longest = np.max(
            [np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1)], 0
        )
        steps = np.maximum(1, np.ceil(longest / (0.5 * h)).astype(int))
        for n in np.unique(steps):
            sel = steps == n
            i, j = np.mgrid[0 : n + 1, 0 : n + 1]
            keep = i + j <= n
            fu, fv = (i[keep] / n)[None, :, None], (j[keep] / n)[None, :, None]
            s = a[sel][:, None] + fu * (b[sel] - a[sel])[:, None] + fv * (c[sel] - a[sel])[:, None]
            q = np.floor((s.reshape(-1, 3) - lo) / h).astype(int)
            grid[q[:, 0], q[:, 1], q[:, 2]] = True
    hemi = f2d._hemisphere(f2d.WHEEL_AO_RAYS)
    helper = np.where(np.abs(nrm[:, :1]) < 0.9, [[1.0, 0.0, 0.0]], [[0.0, 1.0, 0.0]])
    t = np.cross(nrm, helper)
    t /= np.linalg.norm(t, axis=1, keepdims=True)
    bt = np.cross(nrm, t)
    dirs = (
        hemi[None, :, :1] * t[:, None] + hemi[None, :, 1:2] * bt[:, None] + hemi[None, :, 2:] * nrm[:, None]
    ).reshape(-1, 3)
    pos = np.repeat(pts + nrm * h, f2d.WHEEL_AO_RAYS, 0)
    light = np.zeros(len(dirs))  # per ray: 1 out of the wheel's face, f2d.WHEEL_SKY_SIDE out of its side
    alive = dirs[:, 0] >= 0  # inboard: the car
    travelled, length = 0.0, float(np.linalg.norm(dims * h))
    while travelled < length and alive.any():
        idx = np.nonzero(alive)[0]
        q = np.floor((pos[idx] - lo) / h).astype(int)
        inside = np.all((q >= 0) & (q < dims), 1)
        gone = idx[~inside]
        light[gone] = np.where(q[~inside, 0] >= dims[0], 1.0, f2d.WHEEL_SKY_SIDE)
        alive[gone] = False
        idx, q = idx[inside], q[inside]
        if travelled >= f2d.WHEEL_AO_SKIP * h:
            alive[idx[grid[q[:, 0], q[:, 1], q[:, 2]]]] = False  # hit the wheel
        pos[idx] += dirs[idx] * h
        travelled += h
    vis = light.reshape(-1, f2d.WHEEL_AO_RAYS).mean(1)
    return [round(float(f2d.WHEEL_SKY_OPEN * v), 4) for v in vis]


def _wheel(rng: np.random.Generator, spokes: int, segments: int):
    """A tyre (cylinder) and a dish of spokes, as triangles, and points on them."""
    radius, width = rng.uniform(0.25, 0.4), rng.uniform(0.15, 0.3)
    tris = []
    ring = [(math.cos(a), math.sin(a)) for a in np.linspace(0, 2 * math.pi, segments + 1)]
    for (c0, s0), (c1, s1) in itertools.pairwise(ring):
        a, b = (-width / 2, radius * c0, radius * s0), (width / 2, radius * c0, radius * s0)
        c, d = (-width / 2, radius * c1, radius * s1), (width / 2, radius * c1, radius * s1)
        tris += [(a, b, c), (b, d, c)]
    for k in range(spokes):
        t = 2 * math.pi * k / spokes
        hub = (width / 4, 0.0, 0.0)
        tris.append(
            (
                hub,
                (0.0, radius * math.cos(t), radius * math.sin(t)),
                (0.0, radius * math.cos(t + 0.2), radius * math.sin(t + 0.2)),
            )
        )
    tris = np.array(tris) + rng.normal(0, 0.002, (len(tris), 3, 3))
    pick = rng.integers(0, len(tris), 300)
    w = rng.dirichlet((1, 1, 1), 300)
    points = np.einsum("nk,nka->na", w, tris[pick])
    normals = rng.normal(size=(300, 3))
    if rng.random() < 0.3:
        points = np.round(points / f2d.WHEEL_AO_VOXEL) * f2d.WHEEL_AO_VOXEL  # on voxel boundaries
    return points.tolist(), normals.tolist(), tris.tolist()


def test_wheel_sky_matches_the_voxel_loop():
    rng = np.random.default_rng(3)
    block = f2d.WHEEL_AO_BLOCK
    try:
        for case in range(8):
            f2d.WHEEL_AO_BLOCK = (1, 5, 16, block)[case % 4]
            points, normals, tris = _wheel(rng, int(rng.integers(0, 9)), int(rng.integers(6, 40)))
            assert f2d.wheel_sky(points, normals, tris) == _wheel_sky_reference(points, normals, tris)
        assert f2d.wheel_sky([], [], []) == _wheel_sky_reference([], [], []) == []
        lone = ([(0.0, 0.0, 0.0)], [(1.0, 0.0, 0.0)], [])
        assert f2d.wheel_sky(*lone) == _wheel_sky_reference(*lone)
    finally:
        f2d.WHEEL_AO_BLOCK = block


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
