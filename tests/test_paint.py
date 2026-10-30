"""Paint palettes for Forza colours (research/paint-re.md, "From a Forza colour to the
palette"): one albedo at every angle that shows the swatch on the median sunlit panel, capped
at the knee, under Forza's clear coat."""

from __future__ import annotations

import unittest

import numpy as np

from dsfforza.convert import paint

F50_RED = paint.ForzaColour(1, (0xC0, 0x00, 0x00), False, "Red")  # FER_F50GT_96, the F40
BLUE = paint.ForzaColour(1, (0x03, 0x6B, 0xE3), True, "Blue")  # NIS_370Z_10
GREY = paint.ForzaColour(1, (0x99, 0x99, 0x99), False, "Gray")
WHITE = paint.ForzaColour(1, (0xFC, 0xFC, 0xFC), True, "White")
BLACK = paint.ForzaColour(1, (0x06, 0x06, 0x06), False, "Black")


class Albedo(unittest.TestCase):
    def test_flat_entry_of_the_swatch_albedo(self):
        q = paint.shade(F50_RED)
        self.assertEqual(q["g_vec3TintColour2"], q["g_vec3TintColour3"])
        self.assertEqual(q["g_vec3TintColour2"][:3], paint.swatch_albedo(F50_RED.rgb))
        self.assertEqual(paint.apparent(q), q["g_vec3TintColour2"][:3])

    def test_swatches_show_as_themselves(self):
        # A grey under the knee comes back as its swatch through the game's chain; a red's
        # red channel does, its green and blue lifted by the grade (paint-re.md: no red on
        # screen is purer than (233, 91, 97)).
        np.testing.assert_allclose(paint.shows(paint.shade(GREY)), GREY.rgb, atol=6)
        red = paint.shows(paint.shade(F50_RED))
        self.assertAlmostEqual(red[0], 0xC0, delta=6)
        self.assertLess(max(red[1:]), 100)
        a = paint.swatch_albedo(F50_RED.rgb)
        self.assertAlmostEqual(a[0], 0.146, delta=0.01)
        self.assertLess(max(a[1:]), 0.005)

    def test_capped_at_the_knee(self):
        # Colours the screen can't show stop where more albedo would only flatten the body:
        # 0.3 for a saturated colour, 0.5 for a grey; the hue is kept.
        for colour in (BLUE, WHITE):
            a = np.array(paint.swatch_albedo(colour.rgb))
            self.assertLessEqual(a.max(), paint.knee(a) + 2e-3)
        blue = paint.swatch_albedo(BLUE.rgb)
        self.assertGreater(blue[2], 5 * blue[1])
        self.assertLess(blue[0], 0.01)

    def test_black_is_black(self):
        self.assertLess(max(paint.swatch_albedo(BLACK.rgb)), 0.003)

    def test_knee(self):
        self.assertAlmostEqual(paint.knee(np.array([0.2, 0.2, 0.2])), 0.5)
        self.assertAlmostEqual(paint.knee(np.array([0.2, 0.0, 0.0])), 0.3)


class ClearCoat(unittest.TestCase):
    def test_reflection_is_forzas_clear_coat(self):
        # FresnelScalar 0.9 (Normal.xml) -> 1.0; a satin colour (FresnelScalar 0) none.
        self.assertEqual(paint.shade(F50_RED)["g_hCustomReflectionAmount"], 1.0)
        satin = paint.clear_coat(paint.FORZA_BODY | {"FresnelScalar": 0.0})
        self.assertEqual(satin["g_hCustomReflectionAmount"], 0.0)

    def test_sheen_from_the_metallic_lobes(self):
        solid, metallic = paint.shade(F50_RED), paint.shade(BLUE)
        self.assertEqual((solid["g_hSecondSpecularIntensity"], solid["g_hSecondSpecularPower"]), (0.1, 5.0))
        self.assertAlmostEqual(metallic["g_hSecondSpecularIntensity"], 0.3)
        self.assertAlmostEqual(metallic["g_hSecondSpecularPower"], 66 / 13, places=2)
        # The 370Z's silver file (SpecularAmount2 1, PaintScale 0.7): 0.3 x 1 / 13 x 0.5 / 0.7.
        silver = paint.clear_coat(paint.FORZA_BODY | {"SpecularAmount2": 1.0, "SpecularPower2": 6.0, "PaintScale": 0.7})
        self.assertAlmostEqual(silver["g_hSecondSpecularIntensity"], 0.3 / 13 * 0.5 / 0.7, places=3)

    def test_metallic_flag_kept(self):
        self.assertTrue(paint.is_metallic(paint.shade(BLUE)))
        self.assertFalse(paint.is_metallic(paint.shade(F50_RED)))

    def test_every_palette_parameter_set(self):
        q = paint.shade(F50_RED)
        for name in (
            "g_vec4TintColour1_Alpha",
            "g_vec3TintColour2",
            "g_vec3TintColour3",
            "g_fresnelpower",
            "g_fresnelbias",
            "g_hCustomFresnelPower",
            "g_hCustomReflectionAmount",
            "g_hSecondSpecularIntensity",
            "g_hSecondSpecularPower",
        ):
            self.assertIn(name, q)


class Apparent(unittest.TestCase):
    def test_low_power_shows_colour_3(self):
        # The fresnel term taken literally: power 0.05 with bias 0.75 shows colour 3.
        q = {
            "g_vec3TintColour2": (1.0, 0.0, 0.0),
            "g_vec3TintColour3": (0.007, 0.006, 0.006),
            "g_fresnelpower": 0.05,
            "g_fresnelbias": 0.75,
        }
        self.assertLess(paint.apparent(q)[0], 0.05)

    def test_stainless_preset_untouched(self):
        q = paint.shade(paint.ForzaColour(1, (0x99, 0x99, 0x99), False, "Gray", "stainless"))
        self.assertEqual(q, paint.FINISH_PRESETS["stainless"])


class Survey(unittest.TestCase):
    def test_entry_measures(self):
        row = paint.survey_entry(GREY, paint.shade(GREY))
        self.assertLess(row["dE"], 3)
        self.assertGreater(row["sun_gain"], 5)  # a grey keeps its shading
        self.assertEqual(row["group"], "grey")
        self.assertEqual(paint.colour_group(F50_RED.rgb), "red")
        self.assertEqual(paint.colour_group(BLUE.rgb), "blue")


if __name__ == "__main__":
    unittest.main()
