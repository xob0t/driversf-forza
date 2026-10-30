"""Paint palettes: the colour an entry shows (the fresnel mix taken literally) and the
recoloured entries for Forza colours (issue #289: the F50 GT's red showed dark maroon; issue #345:
saturated colours clipped to 1.0 and drew flat)."""

from __future__ import annotations

import unittest

from dsfforza.convert import paint


def _entry(c2, c3, power, bias=0.01, metallic=False):
    return {
        "g_vec4TintColour1_Alpha": (0.0, 1.0 if metallic else 0.0, 0.0, 1.0),
        "g_vec3TintColour2": tuple(c2) + (1.0,),
        "g_vec3TintColour3": tuple(c3) + (1.0,),
        "g_fresnelpower": power,
        "g_fresnelbias": bias,
        "g_hCustomFresnelPower": 1.0,
        "g_hCustomReflectionAmount": 0.1,
        "g_hSecondSpecularIntensity": 1.0,
        "g_hSecondSpecularPower": 100.0,
    }


# Vanilla entries (solid): the MP4-12C's red, low power and a high bias, so it shows colour
# 3 almost everywhere; a red with power 15 that shows colour 2 with lighter rims; a black.
LOW_POWER = {"sid": 225, "params": _entry((1.0, 0.0, 0.0), (0.007, 0.006, 0.006), 0.05, bias=0.75)}
HIGH_POWER = {"sid": 126, "params": _entry((0.111, 0.003, 0.003), (0.494, 0.117, 0.117), 15.0)}
BLACK = {"sid": 201, "params": _entry((0.004, 0.004, 0.004), (0.06, 0.06, 0.07), 10.0)}
# The brightest vanilla kind (a flat grey with bright rims), metallic so it is never picked for
# the solid colours here: it sets the brightness cap (paint.brightness_cap).
CAP = {"sid": 136, "params": _entry((0.384, 0.384, 0.384), (0.789, 0.789, 0.789), 15.0, metallic=True)}
F50_RED = paint.ForzaColour(1, (0xC0, 0x00, 0x00), False, "Red")  # FER_F50GT_96


def _red(params, ndv):
    return paint._mix(params, ndv)[0]


class Apparent(unittest.TestCase):
    def test_low_power_entry_shows_colour_3(self):
        q = LOW_POWER["params"]
        shown = paint.apparent(q)
        self.assertLess(shown[0], 0.03)  # colour 2 is 1.0, but 98% of colour 3 shows
        self.assertGreater(paint.shown_weight(q), 0.97)

    def test_high_power_entry_shows_colour_2(self):
        q = HIGH_POWER["params"]
        self.assertAlmostEqual(paint.apparent(q)[0], 0.111, delta=0.02)
        self.assertLess(paint.shown_weight(q), 0.05)

    def test_flat_entry_shows_its_colour(self):
        q = _entry((0.3, 0.3, 0.3), (0.3, 0.3, 0.3), 10.0)
        for c in paint.apparent(q):
            self.assertAlmostEqual(c, 0.3, places=6)

    def test_apparent_is_the_area_weighted_mix(self):
        q = _entry((0.6, 0.2, 0.1), (0.02, 0.03, 0.05), 0.5)
        xs = [(k + 0.5) / 32 for k in range(32)]
        want = [sum(x * paint._mix(q, x)[i] for x in xs) / sum(xs) for i in range(3)]
        for got, w in zip(paint.apparent(q), want):
            self.assertAlmostEqual(got, w, places=6)
        self.assertAlmostEqual(paint.brightness(q)[-1], paint._lum(paint.apparent(q)), places=6)


class Shade(unittest.TestCase):
    def _shows_target(self, q, colour):
        target = paint.target_colour(colour)
        for got, want in zip(paint.apparent(q), target):
            self.assertAlmostEqual(got, want, places=4)

    def test_f50_red_on_the_low_power_template_is_bright(self):
        # Before: colour 2 = the swatch (0.527, 0, 0), colour 3 = 0.016, showing #300000.
        q = paint.shade(F50_RED, [LOW_POWER, CAP])
        self.assertEqual(q["_template"], 225)
        self._shows_target(q, F50_RED)
        for ndv in (0.9, 0.7, 0.5):
            self.assertGreater(_red(q, ndv), 0.12)  # sRGB above 0x60, not maroon
        # its shading kept
        self.assertEqual((q["g_fresnelpower"], q["g_fresnelbias"]), (0.05, 0.75))
        # the ratio would need colour 2 at 33 x colour 3: colour 2 at full red instead
        self.assertAlmostEqual(q["g_vec3TintColour2"][0], 1.0, places=6)
        self.assertEqual(q["g_vec3TintColour2"][1:3], (0.0, 0.0))

    def test_f50_red_on_the_high_power_template(self):
        q = paint.shade(F50_RED, [HIGH_POWER, CAP])
        self.assertEqual(q["g_fresnelpower"], 15.0)
        # the red shows as the target; the rim's off-channels add a trace of grey
        shown = paint.apparent(q)
        self.assertAlmostEqual(shown[0], paint.target_colour(F50_RED)[0], places=6)
        self.assertLess(max(shown[1:]), 0.005)
        self.assertAlmostEqual(paint._lum(shown) / paint._lum(paint.target_colour(F50_RED)), 1.0, delta=0.05)

    def test_saturated_red_keeps_the_rim_off_channels_and_headroom(self):
        # Issue #345: the F40's #c00000 on vanilla 126 got colour 3 (1, 0, 0), the hue at full
        # scale, and drew flat in game. The rim keeps 126's off-channels and stays under 1.
        t3 = HIGH_POWER["params"]["g_vec3TintColour3"][:3]
        for rgb in ((0xC0, 0, 0), (0xFF, 0, 1), (0xE1, 0x0D, 0x12)):
            colour = paint.ForzaColour(1, rgb, False, "Red")
            q = paint.shade(colour, [HIGH_POWER, CAP])
            c2, c3 = q["g_vec3TintColour2"][:3], q["g_vec3TintColour3"][:3]
            self.assertLessEqual(max(c2 + c3), paint.HEADROOM + 1e-9, rgb)
            self.assertGreater(min(c3) / max(c3), 0.5 * min(t3) / max(t3), rgb)
            self.assertGreater(max(c3), 2 * max(c2), rgb)  # still a brighter rim
            shown, want = paint.apparent(q), paint.target_colour(colour)
            self.assertAlmostEqual(shown[0], want[0], places=6, msg=rgb)
            self.assertAlmostEqual(paint._lum(shown) / paint._lum(want), 1.0, delta=0.05, msg=rgb)

    def test_template_colour_comes_back_unchanged(self):
        # A Forza colour that is the template's own shown colour gives the template's slots.
        for entry in (HIGH_POWER, LOW_POWER):
            q = entry["params"]
            new2, new3 = paint.recolour(q, paint.apparent(q))
            for got, want in zip(new2 + new3, q["g_vec3TintColour2"][:3] + q["g_vec3TintColour3"][:3]):
                self.assertAlmostEqual(got, want, delta=2e-3)

    def test_slot_at_full_scale_only_where_its_template_has_one(self):
        # Vanilla's 1.0 channels are in slots that barely show (the MP4-12C's colour 2): kept
        # there, never reached elsewhere.
        self.assertEqual(paint.headroom((1.0, 0.0, 0.0)), 1.0)
        self.assertEqual(paint.headroom((0.494, 0.117, 0.117)), paint.HEADROOM)
        bright = paint.ForzaColour(1, (0xFF, 0x40, 0x00), False, "Orange")
        q = paint.shade(bright, [HIGH_POWER, CAP])
        self.assertLess(max(q["g_vec3TintColour2"][:3] + q["g_vec3TintColour3"][:3]), 0.99)

    def test_white_on_a_flat_template_is_scaled(self):
        flat = {"sid": 130, "params": _entry((0.3, 0.3, 0.3), (0.3, 0.3, 0.3), 10.0)}
        white = paint.ForzaColour(1, (0xFA, 0xFA, 0xFA), False, "White")
        q = paint.shade(white, [flat, CAP])
        want = paint.target_colour(white)
        for slot in ("g_vec3TintColour2", "g_vec3TintColour3"):
            for got, w in zip(q[slot][:3], want):
                self.assertAlmostEqual(got, w, places=6)

    def test_template_chosen_by_the_colour_it_shows(self):
        # By colour 2 the MP4-12C's red (1.0) was the bright one; it shows a dark red.
        self.assertEqual(paint.shade(F50_RED, [LOW_POWER, HIGH_POWER, CAP])["_template"], 126)
        dark = paint.ForzaColour(1, (0x50, 0x00, 0x00), False, "Red")
        self.assertEqual(paint.shade(dark, [LOW_POWER, HIGH_POWER, CAP])["_template"], 225)

    def test_near_black_shows_the_vanilla_black_in_its_own_hue(self):
        # The Firebird's #0f140d on a black with a green rim: as bright as that black, in the
        # Forza colour's hue (green / red 1.45 in linear), not the rim's (2.0).
        green_rim = {"sid": 200, "params": _entry((0.006, 0.007, 0.006), (0.02, 0.04, 0.02), 5.3)}
        q = paint.shade(paint.ForzaColour(1, (0x0F, 0x14, 0x0D), False, "Black"), [green_rim, HIGH_POWER, CAP])
        self.assertEqual(q["_template"], 200)
        shown = paint.apparent(q)
        self.assertAlmostEqual(paint._lum(shown), paint._lum(paint.apparent(green_rim["params"])), places=5)
        rim = q["g_vec3TintColour3"][:3]
        forza = paint.ForzaColour(1, (0x0F, 0x14, 0x0D), False, "").linear
        self.assertAlmostEqual(rim[1] / rim[0], forza[1] / forza[0], places=3)
        black = paint.shade(paint.ForzaColour(1, (5, 5, 5), False, "Black"), [BLACK, HIGH_POWER, CAP])
        self.assertEqual(black["_template"], 201)

    def test_vanilla_colour_comes_back_as_itself(self):
        # A vanilla entry's own shown colour as the Forza colour shows the same colour.
        for entry in (LOW_POWER, HIGH_POWER):
            shown = paint.apparent(entry["params"])
            rgb = tuple(round(paint.linear_to_srgb(c / paint.SHOWN_GAIN) * 255) for c in shown)
            q = paint.shade(paint.ForzaColour(1, rgb, False, ""), [entry, CAP])
            self.assertAlmostEqual(paint._lum(paint.apparent(q)) / paint._lum(shown), 1.0, delta=0.03)

    def test_stainless_preset_untouched(self):
        q = paint.shade(paint.ForzaColour(1, (0x99, 0x99, 0x99), False, "Gray", "stainless"), [HIGH_POWER, CAP])
        self.assertEqual(q, paint.FINISH_PRESETS["stainless"])


if __name__ == "__main__":
    unittest.main()
