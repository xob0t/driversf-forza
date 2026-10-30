"""tools/re/paint_model.py against the values read back in game (research/paint-re.md)."""

import importlib.util
import unittest

import numpy as np

from dsfforza import REPO

_spec = importlib.util.spec_from_file_location("paint_model", REPO / "tools" / "re" / "paint_model.py")
pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm)

# Vanilla 126's palette entry 0 as GPCH 26 received it (paintprobe, run vm1-20261030-152849).
RED = {
    "g_vec3TintColour2": (0.111, 0.003, 0.003),
    "g_vec3TintColour3": (0.494, 0.117, 0.117),
    "g_fresnelpower": 15.0,
    "g_fresnelbias": 0.01,
    "g_hCustomFresnelPower": 1.0,
    "g_hCustomReflectionAmount": 0.5,
    "g_hSecondSpecularIntensity": 0.1,
    "g_hSecondSpecularPower": 5.0,
}


class Albedo(unittest.TestCase):
    def test_texel_leaves_the_palette_colour(self):
        # The atlas paint block is (255, 198, 255), sRGB-read: red and blue 1, so A = paint.
        np.testing.assert_allclose(pm.TEXEL, (1.0, 0.5647, 1.0), atol=1e-3)
        np.testing.assert_allclose(
            pm.albedo(RED, 1.0), (0.111 + 0.01 * 0.383, 0.003 + 0.01 * 0.114, 0.003 + 0.01 * 0.114), atol=1e-6
        )

    def test_fresnel_as_stored(self):
        # The power is used as stored: power 15 keeps colour 2 until grazing angles.
        self.assertLess(pm.fresnel(RED, 0.5), 0.011)
        self.assertGreater(pm.fresnel(RED, 0.02), 0.7)


class Shade(unittest.TestCase):
    def test_reflection_floor_on_facing_panels(self):
        n = np.array([0.0, 1.0, 0.0])
        e = np.array([0.0, 1.0, 0.0])
        _, parts = pm.shade(RED, n, e, env=np.ones(3), parts=True)
        # 0.565 x 0.025 x 0.5 x (0.638 + 0.112 + 0.25) x 3 = 0.021 of the environment
        np.testing.assert_allclose(parts["refl"], 0.0212, atol=5e-4)

    def test_sun_and_ambient_on_a_roof(self):
        # A roof facing up in ClearDay: sun N.L 0.76 x (3, 2.92, 2.55), ambient (0.454 +
        # 0.147 x 0.76) and so on: the light on it over its albedo.
        n = np.array([0.0, 1.0, 0.0])
        e = np.array([0.0, 1.0, 0.0])
        _, parts = pm.shade(RED, n, e, env=np.zeros(3), parts=True)
        light = (parts["sun"] + parts["ambient"]) / parts["albedo"]
        np.testing.assert_allclose(
            light,
            (0.76 * 3 + 0.454 + 0.147 * 0.76, 0.76 * 2.923 + 0.517 + 0.17 * 0.76, 0.76 * 2.554 + 0.6 + 0.2 * 0.76),
            rtol=0.01,
        )


class Target(unittest.TestCase):
    def test_knee(self):
        self.assertAlmostEqual(pm.knee(np.array([0.2, 0.2, 0.2])), 0.5)
        self.assertAlmostEqual(pm.knee(np.array([0.2, 0.0, 0.0])), 0.3)

    def test_round_trip(self):
        # A mid grey comes back as itself through the chain it was solved with.
        swatch = np.array([153, 153, 153]) / 255
        a = pm.albedo_for(swatch)
        shown = pm.screen(pm.LIGHT_REF * a, la=pm.LA_REF)
        np.testing.assert_allclose(shown, swatch, atol=3 / 255)


if __name__ == "__main__":
    unittest.main()
