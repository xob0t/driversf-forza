"""Checks for src/dsfforza/convert/digital_dash.py and formats/anark.py that need no game files.

uv run python tests/test_digital_dash.py     # or every test: uv run python -m tests digital_dash
"""

from __future__ import annotations

import numpy as np

from dsfforza.convert import digital_dash as dd
from dsfforza.formats import anark


def test_name_hash():
    # Anark's class and property hashes (h = (h + c) * 65599 from 0; keys keep 24 bits).
    assert anark.h32("Scene") == 0x068C6274 and anark.h32("Camera") == 0xF36612BB
    assert anark.h24("position.x") == 0x7C230D


def test_digi_data_is_what_the_engine_writes():
    # FUN_00891670: mph digits units first, (digit + 1) x 23/256, 0 for a leading blank.
    d = dd.digi_data(107, 3500, 3, 7000)
    assert abs(d[(2, 0)] - 8 * 23 / 256) < 1e-9 and abs(d[(2, 1)] - 1 * 23 / 256) < 1e-9
    assert abs(d[(2, 2)] - 2 * 23 / 256) < 1e-9 and d[(2, 3)] == 0.0
    assert abs(d[(1, 0)] - 0.25) < 1e-9 and abs(d[(6, 0)] - 3 / 7) < 1e-9
    assert dd.digi_data(5, 0, 0, 7000)[(2, 1)] == 0.0  # 5 mph: the tens cell is blank


def test_runs_to_rects_cover_the_mask():
    rng = np.random.default_rng(1)
    mask = rng.random((40, 60)) > 0.6
    mask[5:20, 10:30] = True
    cover = np.zeros_like(mask)
    for x0, x1, y0, y1 in dd._runs_to_rects(mask, slack=0):
        assert not cover[y0:y1, x0:x1].any()  # exact runs don't overlap
        cover[y0:y1, x0:x1] = True
    assert (cover == mask).all()
    # with slack the rectangles may grow a little, but still hold every pixel
    cover[:] = False
    for x0, x1, y0, y1 in dd._runs_to_rects(mask):
        cover[y0:y1, x0:x1] = True
    assert (cover | ~mask).all()


def _patch():
    # A 0.2 x 0.1 m display in z = -0.7, UV0 0..1 across it, showing render-target
    # pixels 100..900 x 200..600 (transform scale 800/1280, 400/720).
    pts = [(0.0, 0.0, -0.7), (0.2, 0.0, -0.7), (0.2, 0.1, -0.7), (0.0, 0.1, -0.7)]
    uv = [(0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]
    tris = [(0, 1, 2), (0, 2, 3)]
    t = (100 / 1280, 800 / 1280, 200 / 720, 400 / 720)
    return dd.DisplayPatch(
        "interior", 0, t, [[pts[i] for i in tr] for tr in tris], [[uv[i] for i in tr] for tr in tris], (0, 0, 1)
    )


def test_display_patch_maps_the_scene_onto_the_display():
    p = _patch()
    assert p.window() == (100.0, 900.0, 200.0, 600.0)
    centre = p.point(500, 400)
    assert all(abs(a - b) < 1e-9 for a, b in zip(centre, (0.1, 0.05, -0.7), strict=True))
    top_left = p.point(100, 200, lift=0.001)  # render-target y down, display v up
    assert all(abs(a - b) < 1e-9 for a, b in zip(top_left, (0.0, 0.1, -0.699), strict=True))
    outside = p.point(1000, 400)  # extrapolated from the nearest triangle
    assert abs(outside[0] - 0.225) < 1e-9


def test_preview_draws_the_cells_the_engine_picks():
    # One digit quad over cell 0 of a strip: the engine's offset shows cell digit + 1.
    W, H = 64, 512
    plan = dd.DigitalPlan(W, H, bytearray(W * H * 4))
    pitch = dd.DIGIT_PITCH * H
    for k in range(11):
        plan.fill(0, round(k * pitch) + 2, 8, round((k + 1) * pitch) - 2, (k * 20, 0, 0, 255))
    q = dd.Quad([(0, 0, 0)] * 4, [(0, 40 / H), (8 / W, 40 / H), (8 / W, 4 / H), (0, 4 / H)], dd.SPEED_DIGITS[0])
    q.rt = [(0, 10), (10, 10), (10, 0), (0, 0)]
    plan.quads.append(q)
    for mph in (3, 7):
        img = np.asarray(dd.preview(plan, mph, 0, 1, 7000, window=(0, 10, 0, 10), label=False))
        assert tuple(img[6, 6]) == ((mph + 1) * 20, 0, 0)


def test_texture_files():
    rgba = bytearray(range(256)) * 64  # 64 x 64 texels
    dds = dd.rgba_dds(64, 64, rgba)
    assert dds[:4] == b"DDS " and len(dds) == 128 + 64 * 64 * 4 and dds[128 : 128 + 4] == bytes([2, 1, 0, 3])
    dxt = dd.dxt5_dds(64, 64, rgba)
    assert dxt[84:88] == b"DXT5" and len(dxt) == 128 + 64 * 64


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
