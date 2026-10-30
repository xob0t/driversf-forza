"""trim_art.local_contrast (a separable window) gives what the 5 x 5 loop gave.

uv run python tests/test_speed_contrast.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import numpy as np

from dsfforza.convert import trim_art


def _local_contrast_reference(image):
    """The original: the window's max and min over all its offsets."""
    lum = np.rint(image[..., :3].astype(np.float64) @ (0.299, 0.587, 0.114)).astype(np.int64)
    opaque = image[..., 3] >= trim_art.ALPHA_OPAQUE
    r = trim_art.DETAIL_WINDOW // 2
    hi_src = np.where(opaque, lum, -1)
    lo_src = np.where(opaque, lum, 256)
    hi, lo = hi_src.copy(), lo_src.copy()
    padded_hi = np.pad(hi_src, r, mode="edge")
    padded_lo = np.pad(lo_src, r, mode="edge")
    h, w = lum.shape
    for dy in range(trim_art.DETAIL_WINDOW):
        for dx in range(trim_art.DETAIL_WINDOW):
            hi = np.maximum(hi, padded_hi[dy : dy + h, dx : dx + w])
            lo = np.minimum(lo, padded_lo[dy : dy + h, dx : dx + w])
    hi = np.where(hi < 0, lum, hi)
    lo = np.where(lo > 255, lum, lo)
    return np.stack((hi, lo), axis=-1)


def test_local_contrast_matches_the_window_loop():
    rng = np.random.default_rng(13)
    for h, w in ((1, 1), (1, 7), (3, 2), (17, 33), (128, 64)):
        image = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
        image[..., 3] = np.where(rng.random((h, w)) < 0.4, 0, 255)  # transparent texels too
        got, want = trim_art.local_contrast(image), _local_contrast_reference(image)
        assert got.dtype == want.dtype and np.array_equal(got, want), (h, w)
        image[..., 3] = 0  # all transparent
        assert np.array_equal(trim_art.local_contrast(image), _local_contrast_reference(image))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
