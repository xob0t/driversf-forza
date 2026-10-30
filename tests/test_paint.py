"""Paint palettes for Forza colours (research/paint-re.md, "From a Forza colour to the
palette"): one albedo at every angle that shows the swatch on the median sunlit panel, capped
at the knee, under Forza's clear coat."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from dsfforza.build import port_car
from dsfforza.build.forza_roster import Source
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


class Survey(unittest.TestCase):
    def test_entry_measures(self):
        row = paint.survey_entry(GREY, paint.shade(GREY))
        self.assertLess(row["dE"], 3)
        self.assertGreater(row["sun_gain"], 5)  # a grey keeps its shading
        self.assertEqual(row["group"], "grey")
        self.assertEqual(paint.colour_group(F50_RED.rgb), "red")
        self.assertEqual(paint.colour_group(BLUE.rgb), "blue")


# A per-colour file as the FH1 TVR Sagaris has them (ColorShaderSettings1.xml, trimmed).
SAGARIS = """<ShaderSettings><!-- colour 1 -->
  <body>
    <PaintColor r="0.0" g="0.298" b="0.31" a="1.0"/>
    <PaintScale value="0.65"/>
    <SpecularAmount2 value="7.0"/>
    <SpecularAmount3 value="6.0"/>
    <TwoToneColor r="0.28" g="0.15" b="0.0" a="1.0"/>
    <TwoTonePower value="28.000002"/>
    <TwoToneBias value="0.01"/>
    <TwoToneScale value="0.5"/>
  </body>
  <misc><PaintScale value="2.0"/></misc>
</ShaderSettings>"""
SATIN = "<ShaderSettings><body><FresnelScalar value='0'/><SpecularAmount2 value=\"5.0\"/></body></ShaderSettings>"


class ColourSettings(unittest.TestCase):
    def test_body_colours_read_as_linear(self):
        # Forza converts float4 colours from sRGB to linear before the upload (#379).
        body = paint.parse_body(SAGARIS)
        self.assertAlmostEqual(body["TwoToneColor"][0], paint.srgb_to_linear(0.28), places=6)
        self.assertEqual(body["PaintScale"], 0.65)  # the body's, not misc's
        self.assertEqual(paint.parse_body("<ShaderSettings><misc/></ShaderSettings>"), {})

    def test_satin_has_no_reflection(self):
        q = paint.shade(paint.ForzaColour(5, (0xAD, 0xB5, 0xB8), True, "Gray", {"FresnelScalar": 0.0}))
        self.assertEqual(q["g_hCustomReflectionAmount"], 0.0)

    def test_two_tone_faces_the_camera(self):
        # Forza's two-tone weight is TwoToneScale x sat(N.E + bias)^power: the second colour
        # facing the camera. Colour 2 (DSF's facing colour) takes the mix, colour 3 the swatch.
        colour = paint.ForzaColour(1, (0x00, 0x4C, 0x4F), True, "Blue", paint.parse_body(SAGARIS))
        q = paint.shade(colour)
        self.assertEqual(q["g_vec3TintColour3"][:3], paint.swatch_albedo(colour.rgb))
        self.assertGreater(q["g_vec3TintColour2"][0], q["g_vec3TintColour3"][0])  # browner facing
        self.assertLess(q["g_fresnelpower"], 0.5)  # Forza's weight drops off just past facing
        self.assertEqual(q["g_fresnelbias"], 0.0)
        # the lobes and PaintScale of the file: 0.3 x 13 / 13 x 0.5 / 0.65
        self.assertAlmostEqual(q["g_hSecondSpecularIntensity"], 0.3 * 0.5 / 0.65, places=3)

    def test_two_tone_power_follows_forzas_curve(self):
        self.assertAlmostEqual(paint.two_tone_power(2.0, 0.0), 0.58, delta=0.05)
        self.assertLess(paint.two_tone_power(28.0, 0.01), paint.two_tone_power(2.0, 0.0))

    def test_colours_take_their_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "ColorShaderSettings3.xml").write_text(SATIN)
            self.assertEqual(paint.colour_settings(Path(tmp), 3), {"FresnelScalar": 0.0, "SpecularAmount2": 5.0})
            self.assertEqual(paint.colour_settings(Path(tmp), 4), {})
        self.assertEqual(paint.colour_settings(None, 1), {})


class _Info:
    def __init__(self, filename):
        self.filename = filename

    def is_dir(self):
        return False


class _Zip:
    def __init__(self, files):
        self.files = files

    def entries(self):
        return [_Info(n) for n in self.files]

    def read(self, info):
        return self.files[info.filename]


class _Image:
    def close(self):
        pass


class Extraction(unittest.TestCase):
    def test_colour_settings_extracted_with_their_own_stamp(self):
        files = {
            "TVR_Sagaris_05/ColorShaderSettings1.xml": SAGARIS.encode(),
            "TVR_Sagaris_05/ShaderSettings.xml": b"<ShaderSettings/>",
            "TVR_Sagaris_05/sub/ColorShaderSettings2.xml": b"x",
        }
        source = Source("FH1", "FH1 disc", "media/cars/TVR_Sagaris_05.zip", 0, 3, "car", "x.iso")
        opened = []

        def fake_open(src):
            opened.append(src)
            return _Image(), _Zip(files), "tvr_sagaris_05/"

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(port_car, "open_car_zip", fake_open):
            got = port_car.extract_colour_settings(source, "TVR_Sagaris_05", Path(tmp), log=lambda *_: None)
            self.assertEqual(got, ["ColorShaderSettings1.xml"])
            folder = Path(tmp, "fh1", "TVR_Sagaris_05")
            self.assertEqual(paint.colour_settings(folder, 1)["TwoToneScale"], 0.5)
            stamp = json.loads((folder / port_car.COLOUR_SETTINGS_STAMP).read_text())
            self.assertEqual(stamp["files"], ["ColorShaderSettings1.xml"])
            # cached: the stamp answers, the zip isn't opened again
            port_car.extract_colour_settings(source, "TVR_Sagaris_05", Path(tmp), log=lambda *_: None)
            self.assertEqual(len(opened), 1)


if __name__ == "__main__":
    unittest.main()
