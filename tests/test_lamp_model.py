"""tools/re/lamp_model.py against the values read back in game (research/lights-re.md)."""

import importlib.util
import unittest

import numpy as np

from dsfforza import REPO

_spec = importlib.util.spec_from_file_location("lamp_model", REPO / "tools" / "re" / "lamp_model.py")
lm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lm)


class Levels(unittest.TestCase):
    def test_free_roam(self):
        # Running lights only: what every car has on all the time.
        drl = lm.DRL
        self.assertEqual(lm.level(0x01, drl), 0)
        self.assertEqual(lm.level(0x03, drl), 2)
        self.assertEqual(lm.level(0x18, drl), 0.2)
        self.assertEqual(lm.level(0x1D, drl), 1)
        self.assertEqual(lm.level(0x12, drl), 3)

    def test_braking_and_headlights(self):
        self.assertEqual(lm.level(0x18, lm.DRL | lm.BRAKE), 1)
        self.assertEqual(lm.level(0x02, lm.DRL | lm.BRAKE), 1)
        self.assertEqual(lm.level(0x06, lm.BRAKE), 0.5)
        self.assertEqual(lm.level(0x1D, lm.DRL | lm.HEAD), 10)
        self.assertEqual(lm.level(0x01, lm.HEAD), 2)

    def test_uploaded_colours(self):
        # lampprobe, run vm1-20261030-142442: car 130 braking by day, car 241's 0x03 tails,
        # car 130's 0x1D lamps by day.
        np.testing.assert_allclose(lm.emission(0x19, 6, 1, 5), (2.939, 0, 0), atol=1e-3)
        np.testing.assert_allclose(lm.emission(0x03, 6, 1, 5), (5.878, 0, 0), atol=1e-3)
        np.testing.assert_allclose(lm.emission(0x1D, 8, 0.1, 5), (5, 4.788, 3.802), atol=1e-3)
        np.testing.assert_allclose(lm.emission(0x18, 6, lm.DRL, 1, mask=True), (0.1176, 0, 0), atol=1e-3)


class Post(unittest.TestCase):
    def test_exposure(self):
        lc = [0, 2, -1, 0.3]
        self.assertEqual(lm.exposure(0.003, lc, 0.5), 2.0)  # dark: the offset
        self.assertEqual(lm.exposure(0.5, lc, 1.5), 1.5)  # bright: middle grey
        self.assertAlmostEqual(lm.exposure(0.1722, lc, 1.5), 1.686, places=3)

    def test_tonemap_white_point(self):
        # Reinhard extended: luminance W maps to 1.
        w = 5.0
        c = lm.tonemap(np.array([[w, w, w]]), 1.0, w)
        np.testing.assert_allclose(c, [[1, 1, 1]], atol=1e-6)


if __name__ == "__main__":
    unittest.main()
