"""forza_materials.triangles_alpha_cut gives what triangle_alpha_cut gives triangle by triangle,
on random UV triangles of every kind over a random alpha atlas.

uv run python tests/test_speed_alpha_cut.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import numpy as np

from dsfforza.convert import forza_materials as fm


def test_triangles_alpha_cut_matches_one_by_one():
    rng = np.random.default_rng(12)
    cuts = 0
    for case in range(12):
        width, height = int(rng.choice([4, 64, 512])), int(rng.choice([4, 32, 256]))
        rgba = rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
        rgba[..., 3] = np.where(rng.random((height, width)) < 0.7, 0, rgba[..., 3])  # mostly cut
        if case % 3 == 0:
            rgba[: height // 2, :, 3] = 255  # an opaque half
        atlas = (width, height, bytearray(rgba.tobytes()))
        n = int(rng.choice([0, 1, 300, 2000]))
        a = rng.uniform(-0.2, 1.2, (n, 1, 2))
        size = rng.choice([0.0005, 0.01, 0.2], (n, 1, 1))
        uvs = a + rng.normal(0, 1, (n, 3, 2)) * size
        if n:
            uvs[: n // 10, 2] = uvs[: n // 10, 0]  # degenerate
            uvs[n // 10 : n // 5] = np.round(uvs[n // 10 : n // 5] * (width, height)) / (width, height)
        tris = [[tuple(map(float, p)) for p in t] for t in uvs]
        want = [fm.triangle_alpha_cut(atlas, t) for t in tris]
        assert fm.triangles_alpha_cut(atlas, tris) == want, case
        cuts += sum(want)
    assert cuts > 100


def test_triangles_alpha_cut_non_finite_raises():
    atlas = (8, 8, bytearray(8 * 8 * 4))
    try:
        fm.triangles_alpha_cut(
            atlas, [[(0.1, 0.1), (0.5, 0.1), (0.1, 0.5)], [(float("nan"), 0.1), (0.5, 0.1), (0.1, 0.5)]]
        )
    except ValueError:
        return
    raise AssertionError("no error")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
