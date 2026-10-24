"""Regression tests for the appearance converters, on synthetic data (no game
or Forza files).

    uv run python tests/test_appearance.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


from dsfforza.convert import dsf_materials as dm  # noqa: E402
from dsfforza.convert import forza_materials as fm  # noqa: E402
from dsfforza.convert import glass  # noqa: E402


class _Vertex:
    def __init__(self, position, uv0):
        self.position, self.uv0, self.normal = position, uv0, (0.0, 0.0, 1.0)


class _Sub:
    def __init__(self, material, tris, uv_transform=(0.0, 1.0, 0.0, 1.0), lod=1):
        self.material, self.lod, self.uv_transform, self._tris = material, lod, uv_transform, tris

    def triangles(self):
        return self._tris


class _Section:
    def __init__(self, name, vertices, subsections):
        self.name, self.vertices, self.subsections = name, vertices, subsections

    def vertices_for(self, sub):
        return self.vertices


class RedLampSwatch(unittest.TestCase):
    """A red lamp swatch keeps the cell plan() reserved for it once the car's
    own red lamp curve exists (it used to be converted with the shared curve
    in plan() and with the car's curve in triangle_uvs: KeyError on the FH1
    BMW M5 and Lamborghini Gallardo)."""

    def setUp(self):
        size = 64
        lights = bytearray()
        for _ in range(size):
            for x in range(size):
                lights += bytes((200, 20, 20, 4 * x))  # red lamp texels, glow mask rising along u
        self.atlases = {fm.LIGHTS: (size, size, lights), fm.NODAMAGE: (16, 16, bytearray(16 * 16 * 4))}

    def test_swatch_cell_found(self):
        atlases = self.atlases

        class FakeAtlases:
            def __init__(self, car_dir, lod0=False):
                pass

            def get(self, family):
                return atlases[family]

        corners = [_Vertex((0.0, 0.0, 2.0), (0.0, 0.0)), _Vertex((1.0, 0.0, 2.0), (1.0, 0.0)),
                   _Vertex((1.0, 1.0, 2.0), (1.0, 1.0)), _Vertex((0.0, 1.0, 2.0), (0.0, 1.0))]
        lamp = _Sub("tail_light", [(0, 1, 2), (0, 2, 3)])
        swatch = _Sub("tail_light", [(0, 1, 2)], uv_transform=(0.5, 0.0, 0.5, 0.0))  # one texel
        section = _Section("taillightL", corners, [lamp, swatch])
        with mock.patch.object(fm, "AtlasCache", FakeAtlases):
            t = dm.Texturing(Path("."))
            t.plan([section], {1})
            self.assertIsNotNone(t._red_lamp_curve(), "the test needs the car's own red curve")
            self.assertEqual(t.material(section, swatch), dm.LIGHTS_MAT)
            uvs, key = t.triangle_uvs(section, swatch, 0, [corners[i].uv0 for i in (0, 1, 2)])
            self.assertIn(key, t.solids)
            self.assertEqual(uvs, [t.solid_uv(key)] * 3)
            # The cell holds the colour the car's curve gives that texel.
            texel = dm.matched_lamp_texel(200, 20, 20, 128, t._red_lamp_curve(), t._red_lens_scale())
            self.assertEqual(key, ("swatch",) + texel)
            self.assertEqual(t.solid_colours[key], texel)


class MatchedLampAlpha(unittest.TestCase):
    def test_curve_ends(self):
        self.assertEqual(dm.matched_lamp_alpha(200, 20, 20, 0), 0)
        self.assertEqual(dm.matched_lamp_alpha(255, 20, 20, 255), 255)
        self.assertEqual(dm.matched_lamp_alpha(255, 255, 255, 255), 255)


class TinyPane(unittest.TestCase):
    """Interior glass whose triangles all miss the 1 cm occupancy cells (a
    sliver) used to raise ValueError (max() of an empty grid)."""

    def _sliver(self):
        return [(0.5, 1.0, 1.0), (0.5, 1.002, 1.0), (0.5, 1.0, 1.003)]  # 2-3 mm, facing +x

    def test_pane(self):
        pane = glass._Pane([(self._sliver(), [(1.0, 0.0, 0.0)] * 3, (0.5,) * 3, b"\x01\x02\x00\x01")],
                           (0.0, 0.0, -1.0), (0.0, -1.0, 0.0), (1.0, 0.0, 0.0))
        self.assertEqual(pane.area, 0.0)
        self.assertEqual(pane.rim, [])
        self.assertFalse(pane.inside((0.0, 0.0)))

    def test_window(self):
        window = glass.InteriorGlass(seed="test")
        window.add("glassLF", self._sliver(), [(1.0, 0.0, 0.0)] * 3, [0.5] * 3, b"\x01\x02\x00\x01")
        tris = window.triangles()
        self.assertEqual({region for _, region in tris}, {"surface"})


if __name__ == "__main__":
    unittest.main()
