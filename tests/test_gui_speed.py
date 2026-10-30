"""gui_assets' numpy forms give what their per-pixel loops gave (kept here as references):
the garage picture's PNG preview and the nameplates' coverage at more sizes than
test_speed.py's.

    uv run python tests/test_gui_speed.py      # or every test: uv run python -m tests
"""

from __future__ import annotations

import numpy as np

from dsfforza.game import gui_assets as g


def _preview_reference(picture) -> bytearray:
    """The original per-pixel checkerboard blend."""
    flat = bytearray(picture.px)
    for i in range(picture.w * picture.h):
        a = flat[i * 4 + 3] / 255
        bg = 90 if ((i % picture.w) // 16 + (i // picture.w) // 16) % 2 else 140
        for c in range(3):
            flat[i * 4 + c] = int(flat[i * 4 + c] * a + bg * (1 - a))
        flat[i * 4 + 3] = 255
    return flat


def test_preview_matches_the_pixel_loop():
    rng = np.random.default_rng(2)
    for w, h in ((64, 40), (37, 19)):
        rgba = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
        rgba[..., 3][rng.random((h, w)) < 0.3] = 255
        rgba[..., 3][rng.random((h, w)) < 0.3] = 0
        picture = g.Image.from_array(rgba)
        assert g.preview_rgba(picture) == _preview_reference(picture)


def test_plate_coverage_tiles_match_row_by_row():
    """Tiles of other sizes (one row, one column) count what the default tiles count."""
    for text, height in (("MURCIELAGO LP640", 76), ("#77 X", 30), ("Q", 40)):
        key = text.upper().translate(g.TEXT_FOLD)
        expected = g._text_plate(key, height, 0.22).px
        saved = g.PLATE_TILE
        try:
            for tile in ((1, 24), (3, 1), (16, 64)):
                g.PLATE_TILE = tile
                assert g._text_plate(key, height, 0.22).px == expected, (text, tile)
        finally:
            g.PLATE_TILE = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
