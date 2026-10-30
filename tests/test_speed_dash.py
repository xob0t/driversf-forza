"""digital_dash's faster blur and static detail (slices instead of np.take copies; one
channel instead of three equal ones) and dash_scene.Canvas.image (only the rows drawn
on box-filtered) give exactly what the code before them gave. The references below
are that code, kept here only to compare against, on random images.

uv run python tests/test_speed_dash.py     # or: uv run python -m tests speed_dash
"""

from __future__ import annotations

import math

import numpy as np

from dsfforza.convert import dash_scene as ds
from dsfforza.convert import digital_dash as dd


def _blur_reference(a, weight, sigma: float):
    """The original: each shifted copy taken with np.take."""
    if sigma <= 0:
        return a
    r = int(math.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2).astype(np.float32)
    num, den = a * weight[..., None], weight[..., None].astype(np.float32)
    for axis in (0, 1):
        pad = [(0, 0)] * 3
        pad[axis] = (r, r)
        pn, pd = np.pad(num, pad), np.pad(den, pad)
        n = a.shape[axis]
        num = sum(k[t] * np.take(pn, range(t, t + n), axis=axis) for t in range(2 * r + 1))
        den = sum(k[t] * np.take(pd, range(t, t + n), axis=axis) for t in range(2 * r + 1))
    return num / np.maximum(den, 1e-6)


def _static_detail_reference(base, ev_step, shown, c: int):
    """The original: the luminance blurred as three equal channels."""
    lum = base[..., :3] @ np.array([0.299, 0.587, 0.114], np.float32)
    lit = (ev_step[0] >= 0).astype(np.float32)
    near = _blur_reference((lum * lit)[..., None].repeat(3, -1), lit, 2.5 * c)[..., 0]
    seen = _blur_reference(lit[..., None].repeat(3, -1), np.ones_like(lit), 2.5 * c)[..., 0] > 0.01
    return shown & (ev_step[0] < 0) & seen & (np.abs(lum - near) >= dd.STATIC_DETAIL)


def _scene(rng, h: int, w: int):
    base = rng.random((h, w, 4), dtype=np.float32)
    base[rng.random((h, w)) < 0.3] = 0.0
    ev_step = np.full((dd.CHANGES, h, w), -1, np.int32)
    lit = rng.random((h, w)) < 0.4
    ev_step[0][lit] = rng.integers(0, 40, lit.sum())
    shown = rng.random((h, w)) < 0.9
    return base, ev_step, shown


def test_blur_matches_np_take():
    rng = np.random.default_rng(1)
    for h, w, sigma in ((30, 50, 1.5), (64, 17, 4.0), (5, 90, 10.0), (40, 40, 0.0)):
        a = rng.random((h, w, 3), dtype=np.float32) - 0.25
        weight = (rng.random((h, w)) < 0.7).astype(np.float32)
        got, want = dd._blur(a, weight, sigma), _blur_reference(a, weight, sigma)
        assert got.dtype == want.dtype and got.tobytes() == want.tobytes()


def test_static_detail_matches_three_channels():
    rng = np.random.default_rng(2)
    for h, w, c in ((40, 120, 1), (60, 90, 4), (33, 71, 6)):
        base, ev_step, shown = _scene(rng, h, w)
        assert (dd.static_detail(base, ev_step, shown, c) == _static_detail_reference(base, ev_step, shown, c)).all()


def _image_reference(canvas: ds.Canvas, alpha: bool):
    """The original: the whole canvas box-filtered."""
    from PIL import Image

    c = canvas
    img = c.rgb.reshape(c.H // c.ss, c.ss, c.W // c.ss, c.ss, 3).mean((1, 3))
    if not alpha:
        return Image.fromarray((np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8))
    a = c.alpha.reshape(c.H // c.ss, c.ss, c.W // c.ss, c.ss).mean((1, 3))
    rgb = np.where(a[..., None] > 1e-6, img / np.maximum(a[..., None], 1e-6), 0.0)
    out = np.concatenate([np.clip(rgb, 0, 1), np.clip(a, 0, 1)[..., None]], -1)
    return Image.fromarray((out * 255 + 0.5).astype(np.uint8), "RGBA")


def test_canvas_image_matches_the_whole_canvas():
    rng = np.random.default_rng(4)
    for case in range(60):
        ss = (1, 2, 4)[case % 3]
        canvas = ds.Canvas(int(rng.integers(40, 700)), ss)
        for _ in range(int(rng.integers(0, 5))):  # boxes of every size, one pixel up to the canvas
            h, w = int(rng.integers(1, canvas.H + 1)), int(rng.integers(1, canvas.W + 1))
            if rng.random() < 0.4:
                h, w = int(rng.integers(1, 4)), int(rng.integers(1, 4))
            y0, x0 = int(rng.integers(0, canvas.H - h + 1)), int(rng.integers(0, canvas.W - w + 1))
            a = rng.random((h, w), dtype=np.float32) * (rng.random((h, w)) < 0.8)
            canvas.blend(y0, x0, rng.random((h, w, 3), dtype=np.float32) * 1.2, a.astype(np.float32))
        for alpha in (False, True):
            got, want = canvas.image(alpha), _image_reference(canvas, alpha)
            assert got.mode == want.mode and got.size == want.size and got.tobytes() == want.tobytes()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
