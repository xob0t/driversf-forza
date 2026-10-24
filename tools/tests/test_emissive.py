"""Things that glow or are too bright on converted cars: paint brighter than any stock
paint, livery decal slots drawn from the car atlases."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import forza_materials as fm  # noqa: E402
import paint  # noqa: E402


def _entry(c2, c3, power, bias=0.01, metallic=True):
    return {"g_vec4TintColour1_Alpha": (0.0, 1.0 if metallic else 0.0, 0.0, 1.0),
            "g_vec3TintColour2": tuple(c2) + (1.0,), "g_vec3TintColour3": tuple(c3) + (1.0,),
            "g_fresnelpower": power, "g_fresnelbias": bias, "g_hCustomFresnelPower": 1.0,
            "g_hCustomReflectionAmount": 0.1, "g_hSecondSpecularIntensity": 1.0, "g_hSecondSpecularPower": 100.0}


# A small stock set: the security van's yellow (the S5S Raptor's template), a flat grey
# with bright rims (the brightest stock kind) and a white with a dark edge tint.
STOCK = [
    {"sid": 301, "params": _entry((0.567, 0.302, 0.032), (0.267, 0.125, 0.045), 1.0)},
    {"sid": 136, "params": _entry((0.384, 0.384, 0.384), (0.789, 0.789, 0.789), 15.0)},
    {"sid": 176, "params": _entry((0.899, 0.974, 1.0), (0.02, 0.023, 0.024), 0.1)},
]


class PaintBrightness(unittest.TestCase):
    def test_too_bright_colour_is_dimmed_to_stock(self):
        # FH1 S5S Raptor: #ffbf19 metallic, linear (1.0, 0.52, 0.01) on the van's yellow.
        q = paint.shade(paint.ForzaColour(1, (0xff, 0xbf, 0x19), True, "Yellow"), STOCK)
        self.assertEqual(q["_template"], 301)
        self.assertLess(q["_dimmed"], 0.8)
        cap = paint.brightness_cap(STOCK)
        for got, limit in zip(paint.brightness(q), cap):
            self.assertLessEqual(got, limit + 1e-6)
        # hue kept: both colours scaled alike
        c2 = q["g_vec3TintColour2"]
        self.assertAlmostEqual(c2[1] / c2[0], paint.srgb_to_linear(0xbf / 255), places=3)

    def test_ordinary_colour_untouched(self):
        q = paint.shade(paint.ForzaColour(1, (0xf5, 0xf5, 0xf5), True, "White"), STOCK)
        self.assertEqual(q["_dimmed"], 1.0)
        self.assertAlmostEqual(q["g_vec3TintColour2"][0], paint.srgb_to_linear(0xf5 / 255), places=4)


class LiveryDecals(unittest.TestCase):
    def test_livery_badge_skipped(self):
        self.assertEqual(fm.classify("liveryBadge")[0], fm.SKIP)
        # cockpit carbins: a bonnet section (outside) and a cabin section
        self.assertEqual(fm.classify_cockpit("liveryBadge", "hooda")[0], fm.SKIP)
        self.assertEqual(fm.classify_cockpit("liveryBadge", "interior")[0], fm.SKIP)
        # other badges still sample their atlases
        self.assertEqual(fm.classify("badge"), (fm.TEXTURED, fm.NODAMAGE))
        self.assertEqual(fm.classify_cockpit("badge", "interior"), (fm.TEXTURED, fm.INTERIOR))


if __name__ == "__main__":
    unittest.main()
