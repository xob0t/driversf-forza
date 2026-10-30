"""trim_art.seen_from_outside (views sharing a screen x axis one after the other, its pixel
columns reused) gives what the original gave, on random scenes.

uv run python tests/test_speed_seen.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import numpy as np

from dsfforza.convert import trim_art


def _seen_reference(occluders, candidates):
    """The original: the views in _view_directions' order, each projected afresh.

    For each candidate triangle (array (n, 3, 3), Forza space), whether a point
    of it (centroid, or a corner pulled a quarter of the way in) is in front of or
    within trim_art.VIEW_SLACK of what an orthographic depth buffer of `occluders` (array
    (m, 3, 3), the candidates among them) shows, from any of the views of
    _view_directions."""
    import numpy as np

    if len(candidates) == 0:
        return np.zeros(0, bool)
    surface = np.concatenate(
        [trim_art._surface_points(occluders, trim_art.VIEW_PIXEL * 0.7), candidates.reshape(-1, 3)]
    )
    centroid = candidates.mean(1, keepdims=True)
    probes = np.concatenate([centroid, candidates + (centroid - candidates) * 0.25], axis=1)  # (n, 4, 3)
    seen = np.zeros(len(candidates), bool)
    for d in trim_art._view_directions():
        d = np.array(d)
        up = np.array([0.0, 1.0, 0.0]) if abs(d[1]) < 0.99 else np.array([0.0, 0.0, 1.0])
        right = np.cross(up, d)
        right /= np.linalg.norm(right)
        up = np.cross(d, right)
        sx, sy, depth = surface @ right, surface @ up, surface @ d
        np.negative(depth, out=depth)  # smaller depth: nearer the camera
        x0, y0 = sx.min(), sy.min()
        w = int((sx.max() - x0) / trim_art.VIEW_PIXEL) + 1
        h = int((sy.max() - y0) / trim_art.VIEW_PIXEL) + 1
        sx -= x0  # the pixel of each point, ((sx - x0) / trim_art.VIEW_PIXEL) as int, in place
        sx /= trim_art.VIEW_PIXEL
        sy -= y0
        sy /= trim_art.VIEW_PIXEL
        flat = sy.astype(int)
        flat *= w
        flat += sx.astype(int)
        buffer = np.full(w * h, np.inf)
        np.minimum.at(buffer, flat, depth)
        buffer = buffer.reshape(h, w)
        # Pixels the points missed take the nearest of their neighbours (no see-through gaps).
        holes = np.isinf(buffer)
        if holes.any():
            padded = np.pad(buffer, 1, constant_values=np.inf)
            near = padded[:h, :w].copy()
            for dy, dx in ((dy, dx) for dy in range(3) for dx in range(3) if dy or dx):
                np.minimum(near, padded[dy : dy + h, dx : dx + w], out=near)
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


def _scene(rng, n):
    """A closed box shell of 2 x 0.7 x 1 m in 12 * n * n triangles, and 3 * n small triangles
    scattered inside and outside it (the inside ones hidden from every view)."""
    shell = []
    for axis in range(3):
        for side in (-1.0, 1.0):
            u, v = [a for a in range(3) if a != axis]
            for i in range(n):
                for j in range(n):
                    q = np.zeros((4, 3))
                    q[:, axis] = side
                    q[:, u] = -1 + 2 * np.array([i, i + 1, i + 1, i]) / n
                    q[:, v] = -1 + 2 * np.array([j, j, j + 1, j + 1]) / n
                    shell += [q[[0, 1, 2]], q[[0, 2, 3]]]
    loose = rng.uniform(-1.4, 1.4, (3 * n, 1, 3)) + rng.normal(0, 0.03, (3 * n, 3, 3))
    return np.concatenate([np.array(shell), loose]) * (2.0, 0.7, 1.0), len(shell)


def test_seen_from_outside_matches_the_original():
    rng = np.random.default_rng(23)
    hidden = seen = 0
    for n in (1, 3, 6, 10, 16):
        tris, shell = _scene(rng, n)
        for candidates in (tris[shell:], tris[rng.random(len(tris)) < 0.3]):
            got = trim_art.seen_from_outside(tris, candidates)
            want = _seen_reference(tris, candidates)
            assert np.array_equal(got, want), n
            seen += int(want.sum())
            hidden += int((~want).sum())
    assert seen > 50 and hidden > 20, (seen, hidden)
    assert trim_art.seen_from_outside(tris, np.zeros((0, 3, 3))).shape == (0,)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
