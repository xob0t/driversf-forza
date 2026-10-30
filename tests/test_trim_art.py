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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
