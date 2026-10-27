"""Things that glow or are too bright on converted cars: paint brighter than any stock
paint, livery decal slots drawn from the car atlases."""

from __future__ import annotations

import unittest
from pathlib import Path

from dsfforza.convert import forza_materials as fm
from dsfforza.convert import paint


def _entry(c2, c3, power, bias=0.01, metallic=True):
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
        q = paint.shade(paint.ForzaColour(1, (0xFF, 0xBF, 0x19), True, "Yellow"), STOCK)
        self.assertEqual(q["_template"], 301)
        self.assertLess(q["_dimmed"], 0.8)
        cap = paint.brightness_cap(STOCK)
        for got, limit in zip(paint.brightness(q), cap):
            self.assertLessEqual(got, limit + 1e-6)
        # hue kept: both colours scaled alike
        c2 = q["g_vec3TintColour2"]
        self.assertAlmostEqual(c2[1] / c2[0], paint.srgb_to_linear(0xBF / 255), places=3)

    def test_ordinary_colour_untouched(self):
        q = paint.shade(paint.ForzaColour(1, (0xF5, 0xF5, 0xF5), True, "White"), STOCK)
        self.assertEqual(q["_dimmed"], 1.0)
        self.assertAlmostEqual(q["g_vec3TintColour2"][0], paint.srgb_to_linear(0xF5 / 255), places=4)


class LiveryDecals(unittest.TestCase):
    def test_livery_badge_skipped(self):
        self.assertEqual(fm.classify("liveryBadge")[0], fm.SKIP)
        # cockpit carbins: a bonnet section (outside) and a cabin section
        self.assertEqual(fm.classify_cockpit("liveryBadge", "hooda")[0], fm.SKIP)
        self.assertEqual(fm.classify_cockpit("liveryBadge", "interior")[0], fm.SKIP)
        # other badges still sample their atlases
        self.assertEqual(fm.classify("badge"), (fm.TEXTURED, fm.NODAMAGE))
        self.assertEqual(fm.classify_cockpit("badge", "interior"), (fm.TEXTURED, fm.INTERIOR))


class RedLensScale(unittest.TestCase):
    """A car whose visible red lamp surface is brighter than stock's visible median is
    darkened to it; darker ones are left alone (never brightened)."""

    def test_bright_flat_lens_darkened(self):
        from dsfforza.convert import dsf_materials as dm

        k = dm.red_lens_scale([177] * 1000)  # the TT RS's tail lens
        self.assertAlmostEqual(dm._shared_lens(177) * k, dm.VISIBLE_RED_STOCK_MEDIAN, places=3)
        r, _g, _b, _ = dm.matched_lamp_texel(177, 0, 0, 200, None, k)
        self.assertEqual(r, dm.VISIBLE_RED_STOCK_MEDIAN)

    def test_dark_lens_untouched(self):
        from dsfforza.convert import dsf_materials as dm

        self.assertEqual(dm.red_lens_scale([100] * 1000), 1.0)
        self.assertIsNone(dm.red_lens_scale([177] * 10))  # too few samples
        self.assertEqual(dm.matched_lamp_texel(100, 0, 0, 200, None, 1.0), dm.matched_lamp_texel(100, 0, 0, 200))

    def test_visible_red_sees_the_outer_lens(self):
        """Two stacked quads behind the car: only the outer one (larger Forza z) counts."""
        from unittest import mock

        from dsfforza.convert import dsf_materials as dm

        size = 8
        img = bytearray()
        for _ in range(size):
            for x in range(size):
                img += bytes((200, 0, 0, 255) if x < size // 2 else (90, 0, 0, 255))
        atlases = {fm.LIGHTS: (size, size, img), fm.NODAMAGE: (4, 4, bytearray(64))}

        class FakeAtlases:
            def __init__(self, car_dir, lod0=False):
                pass

            def get(self, family):
                return atlases[family]

        class V:
            def __init__(self, p, uv):
                self.position, self.uv0, self.normal = p, uv, (0.0, 0.0, 1.0)

        class Sub:
            def __init__(self, material, tris, lod=1):
                self.material, self.lod, self.uv_transform, self._tris = material, lod, (0.0, 1.0, 0.0, 1.0), tris

            def triangles(self):
                return self._tris

        class Section:
            def __init__(self, name, vertices, subs):
                self.name, self.vertices, self.subsections = name, vertices, subs

            def vertices_for(self, sub):
                return self.vertices

        def quad(z, u0, u1):
            return [
                V((0.0, 0.0, z), (u0, 0.1)),
                V((0.3, 0.0, z), (u1, 0.1)),
                V((0.3, 0.2, z), (u1, 0.9)),
                V((0.0, 0.2, z), (u0, 0.9)),
            ]

        outer = Section("taillightL", quad(2.0, 0.55, 0.95), [Sub("tail_light", [(0, 1, 2), (0, 2, 3)])])
        inner = Section("taillightL2", quad(1.9, 0.05, 0.45), [Sub("reflector", [(0, 1, 2), (0, 2, 3)])])
        with mock.patch.object(fm, "AtlasCache", FakeAtlases):
            t = dm.Texturing(Path())
            t.plan([inner, outer], {1})
            seen = t.visible_red()
        self.assertGreater(len(seen), 1000)
        self.assertEqual(set(seen), {90})


class WhiteLensScale(unittest.TestCase):
    """Head lamps whose visible white surface is brighter than stock's visible median are
    darkened to it (issue #115: the Lotus Carlton's lit head lamps were white blobs by day);
    darker ones and other hues are left alone."""

    def test_bright_lens_darkened(self):
        from dsfforza.convert import dsf_materials as dm

        k = dm.white_lens_scale([208] * 1000)  # the Carlton's baked head lamp lens
        self.assertAlmostEqual(208 * k, dm.VISIBLE_WHITE_STOCK_MEDIAN, places=3)
        r, g, b, a = dm.matched_lamp_texel(208, 208, 210, 150, None, None, k)
        self.assertEqual((r, g, b), (137, 137, 138))
        self.assertEqual(a, dm.matched_lamp_alpha(208, 208, 210, 150))  # the glow is the original texel's

    def test_dark_lens_and_other_hues_untouched(self):
        from dsfforza.convert import dsf_materials as dm

        self.assertEqual(dm.white_lens_scale([100] * 1000), 1.0)
        self.assertIsNone(dm.white_lens_scale([208] * 10))  # too few samples
        self.assertEqual(
            dm.matched_lamp_texel(200, 200, 200, 99, None, None, 1.0), dm.matched_lamp_texel(200, 200, 200, 99)
        )
        amber = dm.matched_lamp_texel(230, 150, 30, 200)
        self.assertEqual(dm.matched_lamp_texel(230, 150, 30, 200, None, None, 0.5), amber)

    def test_visible_white_sees_the_front_lens(self):
        """Two stacked quads in front of the car (Forza -z) and one at the rear: only the
        outer front one counts."""
        from unittest import mock

        from dsfforza.convert import dsf_materials as dm

        size = 8
        img = bytearray()
        for y in range(size):
            for x in range(size):
                img += bytes(
                    (200, 200, 200, 255)
                    if x < size // 2
                    else ((90, 90, 90, 255) if y < size // 2 else (60, 60, 60, 255))
                )
        atlases = {fm.LIGHTS: (size, size, img), fm.NODAMAGE: (4, 4, bytearray(64))}

        class FakeAtlases:
            def __init__(self, car_dir, lod0=False):
                pass

            def get(self, family):
                return atlases[family]

        class V:
            def __init__(self, p, uv):
                self.position, self.uv0, self.normal = p, uv, (0.0, 0.0, -1.0)

        class Sub:
            def __init__(self, material, tris, lod=1):
                self.material, self.lod, self.uv_transform, self._tris = material, lod, (0.0, 1.0, 0.0, 1.0), tris

            def triangles(self):
                return self._tris

        class Section:
            def __init__(self, name, vertices, subs):
                self.name, self.vertices, self.subsections = name, vertices, subs

            def vertices_for(self, sub):
                return self.vertices

        def quad(z, u0, u1, v0=0.1, v1=0.4):
            return [
                V((0.0, 0.0, z), (u0, v0)),
                V((0.3, 0.0, z), (u1, v0)),
                V((0.3, 0.2, z), (u1, v1)),
                V((0.0, 0.2, z), (u0, v1)),
            ]

        tris = [(0, 1, 2), (0, 2, 3)]
        outer = Section("headlightL", quad(-2.0, 0.55, 0.95), [Sub("head_light", tris)])
        inner = Section("headlightL2", quad(-1.9, 0.05, 0.45), [Sub("reflector", tris)])
        rear = Section("reverse", quad(2.0, 0.55, 0.95, 0.6, 0.9), [Sub("reverse_light", tris)])
        with mock.patch.object(fm, "AtlasCache", FakeAtlases):
            t = dm.Texturing(Path())
            t.plan([inner, outer, rear], {1})
            seen = t.visible_white()
        self.assertGreater(len(seen), 1000)
        self.assertEqual(set(seen), {90})


if __name__ == "__main__":
    unittest.main()
