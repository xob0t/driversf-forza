"""trim_art.triangle_cells gives what its np.mgrid form gave (kept here as the reference).

uv run python tests/test_trim_art.py      # or every test: uv run python -m tests
"""

from __future__ import annotations

import math
import random

import numpy as np

from dsfforza.convert import trim_art


def _triangle_cells_reference(shape, pts):
    """The original, with np.mgrid."""
    height, width = shape[:2]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if not all(math.isfinite(c) for c in xs + ys):
        return np.zeros(0, dtype=int), np.zeros(0, dtype=int)
    x0, x1 = max(0, int(math.floor(min(xs)))), min(width, int(math.ceil(max(xs))) + 1)
    y0, y1 = max(0, int(math.floor(min(ys)))), min(height, int(math.ceil(max(ys))) + 1)
    area = (xs[1] - xs[0]) * (ys[2] - ys[0]) - (ys[1] - ys[0]) * (xs[2] - xs[0])
    if x0 < x1 and y0 < y1 and abs(area) > 1e-12:
        gy, gx = np.mgrid[y0:y1, x0:x1] + 0.5
        w0 = ((xs[1] - gx) * (ys[2] - gy) - (ys[1] - gy) * (xs[2] - gx)) / area
        w1 = ((xs[2] - gx) * (ys[0] - gy) - (ys[2] - gy) * (xs[0] - gx)) / area
        inside = (w0 >= 0) & (w1 >= 0) & (w0 + w1 <= 1)
        if inside.any():
            iy, ix = np.nonzero(inside)
            return iy + y0, ix + x0
    points = list(zip(xs, ys)) + [(sum(xs) / 3, sum(ys) / 3)]
    return (
        np.array([min(height - 1, max(0, int(y))) for _, y in points], dtype=int),
        np.array([min(width - 1, max(0, int(x))) for x, _ in points], dtype=int),
    )


def test_triangle_cells_match_the_mgrid_form():
    rng = random.Random(3)
    cases = [
        [(1.5, 1.5), (1.5, 1.5), (1.5, 1.5)],  # no area: corners and centroid
        [(float("nan"), 0.0), (0.0, 0.0), (1.0, 1.0)],  # non-finite: nothing
    ]
    for _ in range(3000):
        cx, cy, size = rng.uniform(-5, 260), rng.uniform(-5, 130), rng.choice((0.3, 1, 3, 10, 40))
        cases.append([(cx + rng.uniform(-size, size), cy + rng.uniform(-size, size)) for _ in range(3)])
    for pts in cases:
        (ys, xs), (ry, rx) = trim_art.triangle_cells((128, 256), pts), _triangle_cells_reference((128, 256), pts)
        assert ys.tolist() == ry.tolist() and xs.tolist() == rx.tolist() and ys.dtype == ry.dtype


def _surface_points_reference(tris, spacing: float):
    """The original, out of place."""
    if len(tris) == 0:
        return np.zeros((0, 3))
    lengths = np.linalg.norm(tris - np.roll(tris, -1, axis=1), axis=2)
    k = lengths.argmax(1)
    rows = np.arange(len(tris))
    a, b, c = tris[rows, k], tris[rows, (k + 1) % 3], tris[rows, (k + 2) % 3]
    base = lengths[rows, k]
    height = np.linalg.norm(np.cross(b - a, c - a), axis=1) / np.maximum(base, 1e-12)
    k1 = np.clip(np.ceil(base / spacing), 1, 400).astype(np.int64)
    k2 = np.clip(np.ceil(height / spacing), 1, 400).astype(np.int64)
    counts = (k1 + 1) * (k2 + 1)
    tri = np.repeat(rows, counts)
    local = np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts)
    s = (local % (k1 + 1)[tri]) / k1[tri]
    t = (local // (k1 + 1)[tri]) / k2[tri]
    s, t = s[:, None], t[:, None]
    return (1 - t) * (a[tri] + s * (b[tri] - a[tri])) + t * c[tri]


def _seen_from_outside_reference(occluders, candidates):
    """The original depth buffers (minimum.at on fresh arrays, the hole fill by np.min)."""
    if len(candidates) == 0:
        return np.zeros(0, bool)
    surface = np.concatenate(
        [_surface_points_reference(occluders, trim_art.VIEW_PIXEL * 0.7), candidates.reshape(-1, 3)]
    )
    centroid = candidates.mean(1, keepdims=True)
    probes = np.concatenate([centroid, candidates + (centroid - candidates) * 0.25], axis=1)
    seen = np.zeros(len(candidates), bool)
    for d in trim_art._view_directions():
        d = np.array(d)
        up = np.array([0.0, 1.0, 0.0]) if abs(d[1]) < 0.99 else np.array([0.0, 0.0, 1.0])
        right = np.cross(up, d)
        right /= np.linalg.norm(right)
        up = np.cross(d, right)
        sx, sy, depth = surface @ right, surface @ up, -(surface @ d)
        x0, y0 = sx.min(), sy.min()
        w = int((sx.max() - x0) / trim_art.VIEW_PIXEL) + 1
        h = int((sy.max() - y0) / trim_art.VIEW_PIXEL) + 1
        ix = ((sx - x0) / trim_art.VIEW_PIXEL).astype(int)
        iy = ((sy - y0) / trim_art.VIEW_PIXEL).astype(int)
        buffer = np.full(w * h, np.inf)
        np.minimum.at(buffer, iy * w + ix, depth)
        buffer = buffer.reshape(h, w)
        holes = np.isinf(buffer)
        if holes.any():
            padded = np.pad(buffer, 1, constant_values=np.inf)
            near = np.min([padded[dy : dy + h, dx : dx + w] for dy in range(3) for dx in range(3)], axis=0)
            buffer[holes] = near[holes]
        todo = ~seen
        p = probes[todo].reshape(-1, 3)
        px = np.clip(((p @ right - x0) / trim_art.VIEW_PIXEL).astype(int), 0, w - 1)
        py = np.clip(((p @ up - y0) / trim_art.VIEW_PIXEL).astype(int), 0, h - 1)
        visible = -(p @ d) <= buffer[py, px] + trim_art.VIEW_SLACK
        seen[np.flatnonzero(todo)] = visible.reshape(-1, probes.shape[1]).any(1)
        if seen.all():
            break
    return seen


def _scene(rng, n: int):
    """A box-shaped shell of n random triangles, with some inside it."""
    centres = rng.uniform(-1, 1, (n, 1, 3)) * np.array([0.9, 0.5, 2.0])
    outer = rng.random(n) < 0.7
    centres[outer] /= np.abs(centres[outer] / np.array([0.9, 0.5, 2.0])).max(axis=2, keepdims=True)
    return centres + rng.normal(0, 0.08, (n, 3, 3))


def test_seen_from_outside_matches_the_original_buffers():
    rng = np.random.default_rng(4)
    for n in (1, 60, 400):
        occluders = _scene(rng, n)
        assert (
            trim_art._surface_points(occluders, 0.014).tobytes()
            == _surface_points_reference(occluders, 0.014).tobytes()
        )
        candidates = occluders[rng.random(n) < 0.5]
        assert (
            trim_art.seen_from_outside(occluders, candidates) == _seen_from_outside_reference(occluders, candidates)
        ).all()
    assert len(trim_art.seen_from_outside(_scene(rng, 5), np.zeros((0, 3, 3)))) == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
