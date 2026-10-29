"""Regression tests for the exterior textures: rim textures from the car's wheel.xds, the
alpha test of decal triangles and their cut-out geometry, and the wheel's baked sky term,
on synthetic data (no game or Forza files).

    uv run python tests/test_exterior_textures.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import copy
import dataclasses
import struct
import unittest
from pathlib import Path
from unittest import mock

from dsfforza.build import port_car
from dsfforza.build.forza_roster import Source
from dsfforza.convert import cutout, forza2dsf, trim_art
from dsfforza.convert import dsf_materials as dm
from dsfforza.convert import forza_materials as fm
from dsfforza.formats import carbin


def _atlas(size, opaque):
    """RGBA atlas, alpha 255 where opaque(x, y), else 0."""
    data = bytearray()
    for y in range(size):
        for x in range(size):
            data += bytes((40, 40, 40, 255 if opaque(x, y) else 0))
    return size, size, data


class AlphaCut(unittest.TestCase):
    """The alpha test looks at the texels inside the UV triangle. The bounding box
    kept long decal triangles whose box reached opaque artwork next to them: the
    F-150 Raptor's hidden stripe_badge mud splash (alpha 0 under every triangle in
    its nodamage atlas) came out as black patches on the bed and rear doors."""

    def setUp(self):
        self.atlas = _atlas(64, lambda x, y: x < 16 and y < 16)  # opaque artwork in the top-left corner

    def test_triangle_beside_opaque_art_is_cut(self):
        tri = [(0.0, 0.9), (0.9, 0.9), (0.9, 0.0)]  # its box covers the artwork, the triangle doesn't
        self.assertGreaterEqual(fm.triangle_max_alpha(self.atlas, tri), 128)
        self.assertTrue(fm.triangle_alpha_cut(self.atlas, tri))

    def test_triangle_on_opaque_art_stays(self):
        self.assertFalse(fm.triangle_alpha_cut(self.atlas, [(0.02, 0.02), (0.2, 0.02), (0.02, 0.2)]))

    def test_partly_opaque_triangle_stays(self):
        # a letter quad's triangle a third over the artwork
        self.assertFalse(fm.triangle_alpha_cut(self.atlas, [(0.0, 0.0), (0.6, 0.0), (0.0, 0.6)]))

    def test_tiny_triangles_use_their_corners(self):
        tiny = 0.001
        self.assertFalse(fm.triangle_alpha_cut(self.atlas, [(0.1, 0.1), (0.1 + tiny, 0.1), (0.1, 0.1 + tiny)]))
        self.assertTrue(fm.triangle_alpha_cut(self.atlas, [(0.6, 0.6), (0.6 + tiny, 0.6), (0.6, 0.6 + tiny)]))


class ClassifyWheel(unittest.TestCase):
    def test_roles(self):
        for name in ("rim", "chrome_rim", "inner_rim", "outer_rim", "wheel_black", "wheel_emblem", "rim_bump_metal2"):
            self.assertEqual(fm.classify_wheel(name), (fm.TEXTURED, fm.WHEEL), name)
        for name in ("tire", "tire_V2", "tyre"):
            self.assertEqual(fm.classify_wheel(name)[0], fm.TYRE, name)
        for name in ("blur_rim", "blur_lip", "chrome_blur_lip", "chrome_blur_rim"):
            self.assertEqual(fm.classify_wheel(name)[0], fm.SKIP, name)


class ChromeTrim(unittest.TestCase):
    """Forza's chrome techniques go to DSF's shared chrome materials (vanilla: GLOBAL_LOD_Chrome
    in full entries, GLOBAL_Chassis_Chrome in clean exteriors), not the flat dark trim: the
    Mustang Boss 429's bumpers and window frames came out black (out/testdrive11)."""

    def setUp(self):
        quad = [((0.0, 0.0, 0.0), (0.2, 0.2)), ((1.0, 0.0, 0.0), (0.4, 0.2)), ((1.0, 0.2, 0.0), (0.4, 0.3))]
        verts = [carbin.Vertex(p, uv, (0.7, 0.1), (0.0, 0.0, 1.0), bytes((90, 0, 0, 0))) for p, uv in quad]
        transform = (0.0, 1.0, 0.0, 1.0)
        self.subs = {
            name: carbin.SubSection(name, 1, 4, transform, [0, 1, 2])
            for name in ("chrome", "chrome_2", "black", "matte_colors", "body")
        }
        self.section = carbin.Section("bumperFa", (0, 0, 0), (0, 0, 0), (1, 1, 1), verts, [], list(self.subs.values()))

    def test_roles(self):
        for name in ("chrome", "chrome_2", "chrome_3", "chrome_bump", "chrome_4"):
            self.assertEqual(fm.classify(name)[0], fm.CHROME, name)
        self.assertEqual(fm.classify("lights_chrome"), (fm.CHROME, fm.LIGHTS))
        for name in ("chrome_rim", "chrome_blur_lip", "badge_chrome", "black", "matte_colors"):
            self.assertNotEqual(fm.classify(name)[0], fm.CHROME, name)
        # The interior entry draws chrome from the atlas like its other trims.
        self.assertEqual(fm.classify_cockpit("chrome", "a_pillar"), (fm.TEXTURED, fm.NODAMAGE))
        self.assertEqual(fm.classify_cockpit("chrome", "interior"), (fm.TEXTURED, fm.INTERIOR))
        self.assertEqual(fm.classify_cockpit("chrome")[0], fm.TEXTURED)

    def _materials(self, shared):
        builder = forza2dsf.Builder(b"\x01\x02\x03\x04")
        place = forza2dsf.Placement(0.0, 0.0)
        added = forza2dsf.add_geometry(builder, [self.section], 1, place, wheel=False, shared=shared)
        out = {}
        for sub in added:
            buf = builder.buffers[sub.stride]
            v = builder.indices[sub.first_index] + sub.base_vertex
            out[sub.material] = struct.unpack_from("<2e", buf, v * sub.stride + 8)
        return out

    def test_exterior_and_full_entry_materials(self):
        for shared, chrome, trim in (
            (forza2dsf.SHARED_EXTERIOR, forza2dsf.CHROME_LOD1, forza2dsf.TRIM_LOD1),
            (forza2dsf.SHARED, forza2dsf.CHROME, forza2dsf.TRIM),
        ):
            mats = self._materials(shared)
            self.assertIn(chrome, mats)
            self.assertIn(trim, mats)  # black and matte_colors stay flat trim
            self.assertAlmostEqual(mats[chrome][0], forza2dsf.CHROME_UV[0], places=3)
            self.assertAlmostEqual(mats[chrome][1], forza2dsf.CHROME_UV[1], places=3)
            self.assertEqual(forza2dsf.MATERIAL_LAYOUT[chrome], (32, 5))

    def test_textured_binding_keeps_chrome_shared(self):
        t = dm.Texturing(Path())
        sub = self.subs["chrome"]
        t.resolved[(id(self.section), id(sub))] = fm.resolve(self.section, sub)
        self.assertEqual(t.material(self.section, sub), fm.CHROME)
        binding = dm.Binding(t, dm.Installed({dm.CHASSIS: b"C" * 8, dm.LIGHTS_MAT: b"L" * 8}, b"T" * 8, {}))
        self.assertEqual(binding.material(self.section, sub, forza2dsf.SHARED_EXTERIOR), forza2dsf.CHROME_LOD1)


class _Vertex:
    def __init__(self, position, uv0):
        self.position, self.uv0, self.uv1, self.normal = position, uv0, (0.0, 0.0), (1.0, 0.0, 0.0)
        self.extra = bytes((90, 0, 0, 0))


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


class WheelAtlas(unittest.TestCase):
    """Rims sample the car's wheel.xds through the atlas on the Chassis material
    (they were all one silver swatch of DSF's tyre texture); the tyre stays on the
    shared tyre material, blur discs are left out."""

    def setUp(self):
        wheel = bytearray()
        for _y in range(32):
            for _x in range(32):
                wheel += bytes((200, 60, 10, 0))  # orange rim artwork (alpha: gloss mask, not a cut-out)
        atlases = {
            fm.WHEEL: (32, 32, wheel),
            fm.NODAMAGE: (16, 16, bytearray(16 * 16 * 4)),
            fm.LIGHTS: (16, 16, bytearray(16 * 16 * 4)),
        }

        class FakeAtlases:
            def __init__(self, car_dir, lod0=False):
                pass

            def get(self, family):
                return atlases[family]

        self.patches = [
            mock.patch.object(fm, "AtlasCache", FakeAtlases),
            mock.patch.object(dm.Texturing, "wheel_texture", lambda self: True),
        ]
        for p in self.patches:
            p.start()
        corners = [
            _Vertex((-0.1, 0.0, 0.0), (0.1, 0.1)),
            _Vertex((-0.1, 0.2, 0.0), (0.9, 0.1)),
            _Vertex((-0.1, 0.2, 0.2), (0.9, 0.9)),
            _Vertex((-0.1, 0.0, 0.2), (0.1, 0.9)),
        ]
        self.rim = _Sub("rim", [(0, 1, 2), (0, 2, 3)])
        self.tire = _Sub("tire", [(0, 1, 2)], uv_transform=(0.0, 31.0, 0.0, 1.0))
        self.blur = _Sub("blur_rim", [(0, 1, 2)])
        self.wheel = _Section("wheel", corners, [self.rim, self.tire, self.blur])

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_rims_on_the_atlas(self):
        t = dm.Texturing(Path())
        t.plan([], {1}, wheels=([self.wheel], {1}))
        self.assertIn(fm.WHEEL, t.families)
        # Forza's rim reflection: a gloss copy of the Lights material (tests/test_gloss.py)
        self.assertTrue(t.material(self.wheel, self.rim).startswith(dm.GLOSS_PREFIX))
        self.assertEqual(t.material(self.wheel, self.tire), fm.TYRE)
        self.assertEqual(t.material(self.wheel, self.blur), fm.SKIP)
        uvs, key = t.triangle_uvs(self.wheel, self.rim, 0, [self.wheel.vertices[i].uv0 for i in (0, 1, 2)])
        self.assertEqual(key, (fm.WHEEL, 0))
        rgba = t.render()
        x, y = (int(c * n) for c, n in zip(uvs[2], t.size))
        # times the rim technique's default PaintScale, 0.75 (fm.WHEEL_PAINT_SCALE)
        self.assertEqual(tuple(rgba[(y * t.size[0] + x) * 4 : (y * t.size[0] + x) * 4 + 3]), (150, 45, 8))

    def test_paint_scale_per_technique(self):
        """Forza's rim shaders scale the texel by PaintScale: rim 0.75, inner_rim 1;
        texels no triangle covers take the scale covering the most texels (the rim's)."""
        rim = _Sub("rim", [(0, 1, 2), (0, 2, 3)], uv_transform=(0.0, 0.5, 0.0, 0.5))  # u, v 0.05..0.45
        inner = _Sub("inner_rim", [(0, 1, 2)], uv_transform=(0.5, 0.5, 0.5, 0.5))  # u, v 0.55..0.95
        wheel = _Section("wheel", self.wheel.vertices, [rim, inner, self.tire])
        t = dm.Texturing(Path(__file__).parent)
        t.plan([], {1}, wheels=([wheel], {1}))
        self.assertEqual(t.wheel_paint, {"rim": 0.75, "inner_rim": 1.0})
        _, _, rgba = t.atlases.get(fm.WHEEL)

        def texel(x, y):
            return tuple(rgba[(y * 32 + x) * 4 : (y * 32 + x) * 4 + 3])

        self.assertEqual(texel(8, 8), (150, 45, 8))  # rim
        self.assertEqual(texel(28, 20), (200, 60, 10))  # inner_rim (its lower right triangle)
        self.assertEqual(texel(31, 0), (150, 45, 8))  # no triangle: the rim's scale

    def test_paint_scale_from_the_car(self):
        """A car's ShaderSettings.xml overrides the default (FOR_FocusRS500_10's near-black
        rim texels at 7.5); techniques without the parameter (wheel_emblem) stay at 1."""
        import tempfile

        xml = '<ShaderSettings>\n  <rim>\n    <PaintScale value="0.500000" />\n'
        xml += '    <SpecularColor r="1" g="1" b="1" a="0"/>\n  </rim>\n</ShaderSettings>\n'
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ShaderSettings.xml").write_text(xml)
            settings = fm.shader_settings(Path(tmp))
            self.assertEqual(settings, {"rim": {"PaintScale": 0.5, "SpecularColor": 1.0}})
            self.assertEqual(fm.wheel_paint_scale("rim", settings), 0.5)
            self.assertEqual(fm.wheel_paint_scale("inner_rim", settings), 1.0)
            self.assertEqual(fm.wheel_paint_scale("wheel_emblem", {"wheel_emblem": {"PaintScale": 3.0}}), 1.0)
            t = dm.Texturing(Path(tmp))
            t.plan([], {1}, wheels=([self.wheel], {1}))
        _, _, rgba = t.atlases.get(fm.WHEEL)
        self.assertEqual(tuple(rgba[:3]), (100, 30, 5))

    def test_wheel_material(self):
        t = dm.Texturing(Path())
        t.plan([], {1}, wheels=([self.wheel], {1}))
        chassis, lights, gloss = b"C" * 8, b"L" * 8, b"G" * 8
        kind = t.material(self.wheel, self.rim)
        materials = {dm.CHASSIS: chassis, dm.LIGHTS_MAT: lights, kind: gloss}
        binding = dm.Binding(t, dm.Installed(materials, b"T" * 8, {}))
        shared = forza2dsf.SHARED_EXTERIOR
        self.assertEqual(forza2dsf.wheel_material(binding, self.wheel, self.rim, shared), gloss)
        self.assertEqual(forza2dsf.wheel_material(binding, self.wheel, self.tire, shared), forza2dsf.TYRE_LOD1)
        self.assertIsNone(forza2dsf.wheel_material(binding, self.wheel, self.blur, shared))
        # no textures: everything but the blur on the shared tyre (swatches)
        self.assertEqual(forza2dsf.wheel_material(None, self.wheel, self.rim, shared), forza2dsf.TYRE_LOD1)
        self.assertIsNone(forza2dsf.wheel_material(None, self.wheel, self.blur, shared))


def _uv_area(tris) -> float:
    return sum(abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2 for a, b, c in tris)


class MaskTriangles(unittest.TestCase):
    """Outlines of a mask (texel centres) triangulated: holes stay open, the area is the mask's."""

    def test_square_with_hole(self):
        mask = [[0] * 12 for _ in range(12)]
        for y in range(1, 11):
            for x in range(1, 11):
                mask[y][x] = 1
        for y in range(4, 8):
            for x in range(4, 8):
                mask[y][x] = 0
        tris, fallback = cutout.mask_triangles(mask)
        self.assertFalse(fallback)
        self.assertAlmostEqual(_uv_area(tris), 100 - 16, delta=6)  # corners rounded by half a texel
        hole_centre = (6.0, 6.0)
        for t in tris:
            self.assertFalse(
                cutout._point_in_tri(hole_centre, *t) or cutout._point_in_tri(hole_centre, t[0], t[2], t[1])
            )

    def test_letters(self):
        # an "O" and an "L" side by side, with a diagonal stroke through the O
        mask = [[0] * 40 for _ in range(24)]
        for y in range(24):
            for x in range(40):
                r = ((x - 10) ** 2 + (y - 12) ** 2) ** 0.5
                if 5 <= r <= 9 and abs(x - y - 0) > 1:
                    mask[y][x] = 1
                if (24 <= x <= 27 and 3 <= y <= 20) or (24 <= x <= 36 and 17 <= y <= 20):
                    mask[y][x] = 1
        tris, fallback = cutout.mask_triangles(mask)
        self.assertFalse(fallback)
        self.assertAlmostEqual(_uv_area(tris), sum(map(sum, mask)), delta=0.08 * sum(map(sum, mask)))


class CutAlphaTested(unittest.TestCase):
    """A badge quad whose RGB is one flat colour and whose alpha holds the lettering
    (the 205 T16's "205 turbo 16") drew as a solid bar: DSF's Chassis material has no
    alpha test. Its triangles are replaced by pieces over the passing texels."""

    SIZE = 64

    def setUp(self):
        size = self.SIZE

        def opaque(x, y):  # two bars ("letters") inside the badge area x 8..56, y 8..24
            return 8 <= y < 24 and (12 <= x < 20 or 30 <= x < 50)

        self.atlas = _atlas(size, opaque)

        test = self

        class FakeAtlases:
            def get(self, family):
                return test.atlas

        self.atlases = FakeAtlases()
        corners = [
            ((0.0, 0.0, 0.0), (8, 8)),
            ((1.0, 0.0, 0.0), (56, 8)),
            ((1.0, 0.25, 0.0), (56, 24)),
            ((0.0, 0.25, 0.0), (8, 24)),
        ]
        verts = [
            carbin.Vertex(p, (u / size, v / size), (0.5, 0.5), (0.0, 0.0, 1.0), bytes((100, 1, 2, 3)))
            for p, (u, v) in corners
        ]
        transform = (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0)
        self.badge = carbin.SubSection("badge", 1, 4, transform, [0, 1, 2, 0, 2, 3])
        self.paint = carbin.SubSection("body", 1, 4, transform, [0, 1, 2])
        self.section = carbin.Section("trunk", (0, 0, 0), (0, 0, 0), (1, 1, 1), verts, [], [self.badge, self.paint])

    def test_quad_cut_to_letters(self):
        (out,), report = cutout.cut_alpha_tested([self.section], self.atlases)
        badge = out.subsections[0]
        self.assertIs(out.subsections[1], self.paint)  # other techniques untouched
        self.assertEqual(report["trunk/badge"]["cut"], 2)
        pool = out.vertices_for(badge)
        tris = badge.triangles()
        uv = [[(pool[i].uv0[0] * self.SIZE, pool[i].uv0[1] * self.SIZE) for i in t] for t in tris]
        self.assertAlmostEqual(_uv_area(uv), 16 * 8 + 16 * 20, delta=25)  # the letters, not the 48 x 16 quad (768)
        _, _, rgba = self.atlas
        for t in uv:
            cx, cy = (sum(p[0] for p in t) / 3, sum(p[1] for p in t) / 3)
            self.assertEqual(rgba[(int(cy) * self.SIZE + int(cx)) * 4 + 3], 255)
        for t in tris:  # same winding as the source: normals keep facing +z
            a, b, c = (pool[i].position for i in t)
            self.assertGreater((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]), 0)
            for i in t:  # positions follow the UVs (x = (u - 8) / 48)
                self.assertAlmostEqual(pool[i].position[0], (pool[i].uv0[0] * self.SIZE - 8) / 48, places=4)
                self.assertEqual(pool[i].extra[0], 100)

    def test_opaque_badge_untouched(self):
        self.atlas = _atlas(self.SIZE, lambda x, y: True)
        (out,), report = cutout.cut_alpha_tested([self.section], self.atlases)
        self.assertIs(out, self.section)
        self.assertEqual(report, {})


class TrimArt(unittest.TestCase):
    """Trim triangles on light opaque artwork that can be seen from outside go to the atlas:
    the Alfa GTA's quadrifoglio badges are `misc` (trim) triangles on white and green texels,
    and the shared trim drew them as black triangles (out/final-gui2)."""

    SIZE = 64

    def setUp(self):
        size = self.SIZE
        data = bytearray()
        for y in range(size):
            for x in range(size):
                if y >= 32:
                    data += bytes((250, 250, 250, 0))  # paint-through filler: white, alpha 0
                elif x < 32:
                    data += bytes((240, 240, 240, 255))  # white badge artwork
                else:
                    data += bytes((20, 20, 20, 255))  # dark trim swatch
        atlas = (size, size, data)

        class FakeAtlases:
            def get(self, family):
                return atlas

        self.atlases = FakeAtlases()
        self.verts = []
        self.transform = (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0)

    def quad(self, corners, uv_box, normal=(0.0, 1.0, 0.0)):
        """Two triangles over four corners, UVs over uv_box (u0, v0, u1, v1) in texels."""
        u0, v0, u1, v1 = (c / self.SIZE for c in uv_box)
        base = len(self.verts)
        for p, uv in zip(corners, ((u0, v0), (u1, v0), (u1, v1), (u0, v1))):
            self.verts.append(carbin.Vertex(p, uv, (0.5, 0.5), normal, bytes((100, 0, 0, 0))))
        return [base, base + 1, base + 2, base, base + 2, base + 3]

    def box(self, half):
        """A closed cube of paint around the origin (12 triangles)."""
        h = half
        c = [(x, y, z) for x in (-h, h) for y in (-h, h) for z in (-h, h)]
        faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
        out = []
        for f in faces:
            out += self.quad([c[i] for i in f], (0, 0, 1, 1))
        return out

    def top(self, y, x0, x1, uv_box):
        return self.quad([(x0, y, -0.1), (x1, y, -0.1), (x1, y, 0.1), (x0, y, 0.1)], uv_box)

    def test_roles(self):
        self.assertEqual(fm.classify(trim_art.ART_TECHNIQUE[fm.NODAMAGE]), (fm.TEXTURED, fm.NODAMAGE))
        self.assertEqual(fm.classify(trim_art.ART_TECHNIQUE[fm.LIGHTS]), (fm.TEXTURED, fm.LIGHTS))
        for name in ("black", "black_glass", "undercarriage", "numberplate", "numplate", "mirrorLeft"):
            self.assertTrue(trim_art.keeps_trim(name), name)
        for name in ("misc", "frame", "matte_colors", "plastic2"):
            self.assertFalse(trim_art.keeps_trim(name), name)

    def test_is_artwork(self):
        import numpy as np

        self.assertTrue(trim_art.is_artwork(np.array([[240, 240, 240, 255]] * 4)))
        self.assertTrue(trim_art.is_artwork(np.array([[20, 150, 40, 255]] * 4)))  # the green cloverleaf
        self.assertFalse(trim_art.is_artwork(np.array([[20, 20, 20, 255]] * 4)))
        self.assertFalse(trim_art.is_artwork(np.array([[250, 250, 250, 0]] * 4)))  # filler
        # bare metal: a flat neutral grey (the Veyron's scoops: one texel of 177; the
        # Crossfire's strakes); painted artwork: colour, detail or white
        self.assertTrue(trim_art.is_metal("misc", np.array([[177, 176, 177, 255]] * 4)))
        self.assertTrue(trim_art.is_metal("misc", np.array([[212, 206, 198, 255], [205, 200, 191, 255]])))
        self.assertTrue(trim_art.is_metal("smooth_metal", np.array([[20, 150, 40, 255]] * 4)))
        self.assertFalse(trim_art.is_metal("misc", np.array([[160, 213, 175, 255]] * 4)))  # cloverleaf green
        self.assertFalse(trim_art.is_metal("misc", np.array([[250, 250, 250, 255]] * 4)))  # white
        self.assertFalse(
            trim_art.is_metal("misc", np.array([[200, 200, 200, 255], [40, 40, 40, 255]] * 2))
        )  # lettering
        image = np.zeros((8, 8, 4), np.uint8)
        none = trim_art.triangle_texels(image, [(float("inf"), 1.0), (2.0, 2.0), (1.0, 3.0)])
        self.assertEqual(len(none), 0)  # non-finite UVs (a GMC_YukonXL_01 trim)
        self.assertFalse(trim_art.is_artwork(none))

    def test_visible_artwork_moves(self):
        paint = carbin.SubSection("body", 1, 4, self.transform, self.box(0.5))
        white = self.top(0.52, -0.3, -0.1, (4, 4, 28, 28))  # on the roof: a white badge
        dark = self.top(0.52, 0.1, 0.3, (36, 4, 60, 28))  # on the roof: dark trim
        filler = self.top(0.52, 0.32, 0.45, (4, 36, 28, 60))  # white but alpha 0
        hidden = self.top(0.0, -0.3, 0.3, (4, 4, 28, 28))  # white, inside the box
        # dark: flat near black, and misc has a specular in Forza: a gloss cell (tests/test_gloss.py)
        misc = carbin.SubSection("misc", 1, 4, self.transform, white + dark + filler + hidden)
        black = carbin.SubSection("black", 1, 4, self.transform, list(white))
        lod3 = carbin.SubSection("misc", 3, 4, self.transform, list(white))
        handle = self.top(0.52, -0.45, -0.35, (4, 4, 28, 28))  # on the roof: a metal part
        metal = carbin.SubSection("smooth_metal", 1, 4, self.transform, handle)
        section = carbin.Section(
            "body", (0, 0, 0), (0, 0, 0), (1, 1, 1), self.verts, [], [paint, misc, black, lod3, metal]
        )
        (out,), report = trim_art.split_trim_art([section], self.atlases, lods={1})
        self.assertEqual(
            report,
            {
                "body/misc": {
                    "triangles": 4,
                    "metal": 0,
                    "detail": 0,
                    "grey": 0,
                    "gloss": 2,
                    "area": unittest.mock.ANY,
                },
                "body/smooth_metal": {
                    "triangles": 2,
                    "metal": 2,
                    "detail": 0,
                    "grey": 0,
                    "gloss": 0,
                    "area": unittest.mock.ANY,
                },
            },
        )
        self.assertAlmostEqual(report["body/misc"]["area"], 2 * 0.2 * 0.2, places=6)
        names = [s.material for s in out.subsections]
        art = fm.with_source(trim_art.ART_TECHNIQUE[fm.NODAMAGE], "misc")
        gloss = fm.with_source(trim_art.GLOSS_TECHNIQUE, "misc")
        self.assertEqual(names, ["body", "misc", art, gloss, "black", "misc", trim_art.METAL_TECHNIQUE])
        self.assertEqual(fm.classify(trim_art.METAL_TECHNIQUE)[0], fm.CHROME)  # DSF's shared chrome
        self.assertIs(out.subsections[0], paint)
        self.assertIs(out.subsections[4], black)  # black* stays trim
        self.assertIs(out.subsections[5], lod3)  # not a requested LOD
        self.assertEqual(out.subsections[1].indices, filler + hidden)
        self.assertEqual(out.subsections[2].indices, white)
        self.assertEqual(out.subsections[2].uv_transform, self.transform)
        self.assertEqual(out.subsections[3].indices, dark)

    def test_nothing_to_move(self):
        dark = self.top(0.0, 0.1, 0.3, (36, 4, 60, 28))
        section = carbin.Section(
            "body",
            (0, 0, 0),
            (0, 0, 0),
            (1, 1, 1),
            self.verts,
            [],
            # frame: no specular or reflection in Forza, so flat black stays on the shared trim
            [carbin.SubSection("frame", 1, 4, self.transform, dark)],
        )
        (out,), report = trim_art.split_trim_art([section], self.atlases, settings={})
        self.assertIs(out, section)
        self.assertEqual(report, {})

    def _plan(self, art_box):
        """A 48 x 48 atlas for an emblem over texels (36, 4)-(60, 28) and trim artwork over art_box."""
        emblem = carbin.SubSection("emblem", 1, 4, self.transform, self.top(0.5, 0.0, 0.2, (36, 4, 60, 28)))
        art = carbin.SubSection(
            trim_art.ART_TECHNIQUE[fm.NODAMAGE], 1, 4, self.transform, self.top(0.5, -0.3, -0.1, art_box)
        )
        section = carbin.Section("body", (0, 0, 0), (0, 0, 0), (1, 1, 1), self.verts, [], [emblem, art])
        with mock.patch.object(fm, "AtlasCache", lambda *a, **k: self.atlases):
            t = dm.Texturing(Path(), size=(48, 48)).plan([section], {1})
        return t, section, art

    def test_artwork_in_the_atlas(self):
        t, section, art = self._plan((40, 8, 44, 12))  # inside the emblem's region: free
        self.assertEqual(t.material(section, art), dm.CHASSIS)
        self.assertEqual(t.art_flat, set())
        _uvs, tag = t.triangle_uvs(section, art, 0, [section.lod_vertices[i].uv0 for i in art.indices[:3]])
        self.assertEqual(tag[0], fm.NODAMAGE)  # a region, not a solid cell

    def test_costly_artwork_drawn_flat(self):
        # its own 24 x 24 region would take the atlas scale from 1 to about 0.6
        t, section, art = self._plan((4, 4, 28, 28))
        self.assertEqual(t.art_flat, {id(art)})
        self.assertEqual(t.pack_scale, 1.0)
        self.assertEqual(t.material(section, art), dm.CHASSIS)
        uvs, tag = t.triangle_uvs(section, art, 0, [section.lod_vertices[i].uv0 for i in art.indices[:3]])
        self.assertEqual(tag[0], "swatch")
        self.assertEqual(tag[1:4], (240, 240, 240))  # the artwork's mean colour
        self.assertEqual(len(set(uvs)), 1)

    def test_flat_artwork_cells_in_carbin_order(self):
        # Two costly artworks drawn flat take a solid cell each, in the carbin's order. They
        # went in the order of a set of id()s, which changes from run to run: AST_DB9_05
        # converted to other bytes each time (issue #84). Here the id()s sort against the
        # carbin's order, as they may in a run.
        white = carbin.SubSection(
            trim_art.ART_TECHNIQUE[fm.NODAMAGE], 1, 4, self.transform, self.top(0.5, -0.3, -0.1, (4, 4, 28, 28))
        )
        dark = carbin.SubSection(
            trim_art.ART_TECHNIQUE[fm.NODAMAGE], 1, 4, self.transform, self.top(0.5, 0.1, 0.3, (36, 4, 60, 28))
        )
        section = carbin.Section("body", (0, 0, 0), (0, 0, 0), (1, 1, 1), self.verts, [], [white, dark])
        fake = {id(white): 2, id(dark): 1}

        def stand_in_id(obj, real=id):
            return fake.get(real(obj), real(obj))

        with (
            mock.patch.object(fm, "AtlasCache", lambda *a, **k: self.atlases),
            mock.patch.object(dm, "id", stand_in_id, create=True),
        ):
            t = dm.Texturing(Path(), size=(48, 48)).plan([section], {1})
            self.assertEqual(t.art_flat, {2, 1})
            cells = [key[1:] for key in t.solid_colours if key[0] == "swatch"]
            colours = [
                t.triangle_uvs(section, sub, 0, [section.lod_vertices[i].uv0 for i in sub.indices[:3]])[1][1:]
                for sub in (white, dark)
            ]
        self.assertEqual(cells, colours)
        self.assertGreater(colours[0][0], colours[1][0])  # white, then dark

    def test_new_section_keeps_the_lamp_plan(self):
        # The damage pass hands planned subsections in new section objects. Resolved again
        # there, a tiling lamp part sampled the Lights atlas after lenses.bake had written
        # into it: a colour without an atlas cell, so it drew as shared trim. The Mustang GT
        # 05's head lamp bowls did so in the exterior entry and never glowed (issue #158).
        tiling = self.top(0.5, 0.0, 0.2, (0, 0, 8, 8))
        for i in tiling:  # UVs well past 0..1, as the Mustang's bowls (-2.9..3.9)
            v = self.verts[i]
            self.verts[i] = dataclasses.replace(v, uv0=(v.uv0[0] * 40 - 2.9, v.uv0[1] * 40 - 2.9))
        bowl = carbin.SubSection("textured_reflector", 1, 4, self.transform, tiling)
        section = carbin.Section("body", (0, 0, 0), (0, 0, 0), (1, 1, 1), self.verts, [], [bowl])
        with mock.patch.object(fm, "AtlasCache", lambda *a, **k: self.atlases):
            t = dm.Texturing(Path(), size=(48, 48), bake_lenses=False).plan([section], {1})
        self.assertEqual(t.submaterial(section, bowl).kind, "swatch")
        self.assertEqual(t.material(section, bowl), dm.LIGHTS_MAT)
        _w, _h, data = self.atlases.get(fm.LIGHTS)
        data[:] = bytes(len(data))  # what the lens bake writes later
        again = copy.copy(section)
        self.assertEqual(t.material(again, bowl), dm.LIGHTS_MAT)

    def test_new_section_keeps_the_trim_plan(self):
        # Resolved again in a new section object, a swatch lost the colour plan() sampled into
        # it and flat artwork its flat colour: no atlas cell, so both drew as shared trim (and
        # never reached a gloss material) in the exterior entry on about 400 cars (issue #173).
        t, section, art = self._plan((4, 4, 28, 28))  # costly: drawn flat
        swatch = carbin.SubSection(
            "emblem", 1, 4, (0.25, 0.0, 0.25, 0.0) + self.transform[4:], self.top(0.5, 0.3, 0.4, (0, 0, 1, 1))
        )
        section.subsections.append(swatch)
        with mock.patch.object(fm, "AtlasCache", lambda *a, **k: self.atlases):
            t = dm.Texturing(Path(), size=(48, 48)).plan([section], {1})
        self.assertEqual(t.art_flat, {id(art)})
        again = copy.copy(section)
        self.assertEqual(t.material(section, art), dm.CHASSIS)
        self.assertTrue(t.material(section, swatch).startswith(dm.GLOSS_PREFIX))  # emblem: Forza's gloss
        for sub in (art, swatch):
            self.assertEqual(t.material(again, sub), t.material(section, sub))
            self.assertEqual(t.submaterial(again, sub), t.submaterial(section, sub))


class TrimTexels(unittest.TestCase):
    """Forza's trim shaders scale the texel (PaintScale / PristinePaintScale from the car's
    ShaderSettings.xml); dark trims with detail go to the atlas, flat mid-grey ones to a
    colour cell, flat near-black ones stay on the shared trim (the Cougar Eliminator's
    grille, research/forza-materials.md "Flat black grilles")."""

    def test_shader_settings(self):
        text = """<ShaderSettings>
          <misc>
            <SpecularAmount value="0.5" />
            <PaintScale value="0.2" />
            <SpecularColor r="1" g="1" b="1" a="1"/>
          </misc>
          <weatherstrip><PristinePaintScale value="0.05"/><PaintScale value="0.7"/></weatherstrip>
          <badge><FresnelIndex value="1"/></BADGE>
          <smooth_metal><PaintScale value="0.4"/></smooth_metal>
          <rubber><PaintScale value="nan"/></rubber>
          <frame><PristinePaintScale value="0.3"/></frame>
        </ShaderSettings>"""
        settings = fm.parse_shader_settings(text)
        # SpecularColor as its RGB mean (the gloss specular, #176)
        self.assertEqual(settings["misc"], {"SpecularAmount": 0.5, "PaintScale": 0.2, "SpecularColor": 1.0})
        self.assertEqual(settings["badge"], {"FresnelIndex": 1.0})  # mismatched close tag
        # misc is a paint shader: PaintScale scales the paint, the texel stays
        self.assertEqual(fm.texel_scale(settings, "misc"), 1.0)
        self.assertEqual(fm.texel_scale(settings, "MISC"), 1.0)
        self.assertEqual(fm.texel_scale(settings, "weatherstrip"), 0.05)  # PristinePaintScale
        self.assertEqual(fm.texel_scale(settings, "smooth_metal"), 0.4)  # a texel-scaling shader
        self.assertEqual(fm.texel_scale(settings, "rubber"), 1.0)  # not a number: the default
        self.assertEqual(fm.texel_scale(settings, "frame"), 0.3)
        self.assertEqual(fm.texel_scale(settings, "plastic2"), 1.0)

    def test_scaled_names(self):
        import numpy as np

        name = fm.with_scale(trim_art.DETAIL_TECHNIQUE, 0.2)
        self.assertEqual(fm.technique_scale(name), (trim_art.DETAIL_TECHNIQUE, 0.2))
        self.assertEqual(fm.classify(name), (fm.TEXTURED, fm.NODAMAGE))
        self.assertEqual(fm.with_scale("misc", 1.0), "misc")
        family = fm.scaled_family(fm.NODAMAGE, 0.2)
        self.assertEqual((fm.base_family(family), fm.family_scale(family)), (fm.NODAMAGE, 0.2))
        self.assertEqual(fm.scaled_family(fm.NODAMAGE, 1.0), fm.NODAMAGE)
        # 0.2 in linear light: sRGB 255 -> 124, 100 -> 47, 0 -> 0
        self.assertEqual(fm.scale_texels(np.array([255, 100, 0]), 0.2).tolist(), [124, 44, 0])

    def test_classes(self):
        import numpy as np

        def rows(rgb, n=8, contrast=None):
            px = np.array([list(rgb) + [255]] * n)
            hi, lo = contrast if contrast else (int(np.mean(rgb)), int(np.mean(rgb)))
            return np.concatenate([px, np.array([[hi, lo]] * n)], axis=1)

        self.assertIsNone(trim_art.classify_texels("misc", rows((12, 12, 12))))  # flat black: shared trim
        self.assertEqual(trim_art.classify_texels("misc", rows((73, 73, 73))), trim_art.GREY)  # the Volt's bumpers
        self.assertIsNone(trim_art.classify_texels("misc", rows((73, 73, 73)), 0.2))  # Forza draws it dark
        grille = rows((20, 20, 20), contrast=(90, 5))  # bars of 90 a texel or two away
        self.assertEqual(trim_art.classify_texels("misc", grille), trim_art.DETAIL)
        self.assertIsNone(trim_art.classify_texels("misc", grille, 0.05))
        self.assertEqual(trim_art.classify_texels("misc", rows((240, 240, 240))), trim_art.ART)
        self.assertEqual(trim_art.classify_texels("misc", rows((177, 177, 177)), 0.15), trim_art.METAL)
        self.assertIsNone(trim_art.classify_texels("misc", rows((200, 200, 200)) * [1, 1, 1, 0, 1, 1]))

    def test_local_contrast(self):
        import numpy as np

        image = np.zeros((9, 9, 4), np.uint8)
        image[..., 3] = 255
        image[4, :, :3] = 100  # one light bar
        image[0, 0] = (255, 255, 255, 0)  # transparent: ignored
        c = trim_art.local_contrast(image)
        self.assertEqual(c[2, 4].tolist(), [100, 0])  # two texels from the bar
        self.assertEqual(c[7, 4].tolist(), [0, 0])  # three texels away
        self.assertEqual(c[1, 1].tolist(), [0, 0])

    def test_grey_and_detail_in_the_atlas(self):
        import numpy as np

        size = 48
        px = np.zeros((size, size, 4), np.uint8)
        px[..., 3] = 255
        px[:24, :24, :3] = 80  # flat mid-grey
        px[24:, :24, :3] = 10
        px[24:, :24:3, :3] = 120  # dark with light bars
        atlas = (size, size, bytearray(px.tobytes()))

        class FakeAtlases:
            car_dir = None

            def get(self, family):
                return atlas

        atlases = FakeAtlases()
        verts, transform = [], (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0)

        def quad(y, x0, x1, box):
            u0, v0, u1, v1 = (c / size for c in box)
            base = len(verts)
            for p, uv in zip(
                [(x0, y, -0.1), (x1, y, -0.1), (x1, y, 0.1), (x0, y, 0.1)], ((u0, v0), (u1, v0), (u1, v1), (u0, v1))
            ):
                verts.append(carbin.Vertex(p, uv, (0.5, 0.5), (0.0, 1.0, 0.0), bytes((100, 0, 0, 0))))
            return [base, base + 1, base + 2, base, base + 2, base + 3]

        grey = quad(0.0, -0.3, -0.1, (2, 2, 22, 22))
        grille = quad(0.0, 0.1, 0.3, (2, 26, 22, 46))
        section = carbin.Section(
            "body",
            (0, 0, 0),
            (0, 0, 0),
            (1, 1, 1),
            verts,
            [],
            [carbin.SubSection("misc", 1, 4, transform, grey + grille)],
        )
        (out,), report = trim_art.split_trim_art([section], atlases, settings={"misc": {"PristinePaintScale": 0.8}})
        names = [s.material for s in out.subsections]
        self.assertEqual(
            names,
            [
                fm.with_source(fm.with_scale(trim_art.DETAIL_TECHNIQUE, 0.8), "misc"),
                fm.with_source(fm.with_scale(trim_art.GREY_TECHNIQUE, 0.8), "misc"),
            ],
        )
        self.assertEqual(report["body/misc"]["detail"], 2)
        self.assertEqual(report["body/misc"]["grey"], 2)
        detail, flat = out.subsections
        cache = fm.AtlasCache(Path())
        cache._cache[fm.NODAMAGE] = cache._cache[fm.LIGHTS] = atlas
        with mock.patch.object(fm, "AtlasCache", lambda *a, **k: cache):
            t = dm.Texturing(Path(), size=(64, 64)).plan([out], {1})
        family = fm.scaled_family(fm.NODAMAGE, 0.8)
        self.assertIn(family, t.families)
        self.assertTrue(t.material(out, detail).startswith(dm.GLOSS_PREFIX))  # misc's gloss
        _uvs, tag = t.triangle_uvs(out, detail, 0, [verts[i].uv0 for i in detail.indices[:3]])
        self.assertEqual(tag[0], family)  # a region of the scaled atlas
        _uvs, tag = t.triangle_uvs(out, flat, 0, [verts[i].uv0 for i in flat.indices[:3]])
        self.assertEqual(tag[0], "swatch")
        self.assertEqual(tag[1:4], (72, 72, 72))  # 80 at 0.8 in linear light
        # A grey subsection at a LOD the plan didn't lay out (the cockpit's shell) has no
        # colour cell: shared trim, not a KeyError in triangle_uvs.
        shell = carbin.SubSection(flat.material, 3, 4, transform, list(detail.indices))
        self.assertEqual(t.material(out, shell), fm.TRIM)


class WheelSky(unittest.TestCase):
    """Wheel vertices get a baked sky term like vanilla wheels (outward faces about
    0.64, the back of the disc and the barrel's inside dark), not a flat 0.42."""

    def disc(self, x, radius, normal_x, segments=24):
        """Triangles of a flat disc at axial x (DSF wheel frame), and its centre vertex."""
        import math

        ring = [
            (x, radius * math.sin(2 * math.pi * k / segments), radius * math.cos(2 * math.pi * k / segments))
            for k in range(segments)
        ]
        return [[(x, 0.0, 0.0), ring[k], ring[(k + 1) % segments]] for k in range(segments)]

    def barrel(self, x0, x1, radius, segments=24):
        import math

        out = []
        for k in range(segments):
            a, b = 2 * math.pi * k / segments, 2 * math.pi * (k + 1) / segments
            p = [(x, radius * math.sin(t), radius * math.cos(t)) for x in (x0, x1) for t in (a, b)]
            out += [[p[0], p[1], p[3]], [p[0], p[3], p[2]]]
        return out

    def test_faces_and_barrel(self):
        tris = self.disc(0.1, 0.2, 1.0) + self.barrel(-0.1, 0.1, 0.2)
        points = [(0.1, 0.05, 0.0), (0.1, 0.05, 0.0), (0.0, 0.199, 0.0)]
        normals = [(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, -1.0, 0.0)]
        face, back, inside = forza2dsf.wheel_sky(points, normals, tris)
        self.assertAlmostEqual(face, forza2dsf.WHEEL_SKY_OPEN, delta=0.03)  # open to the outboard side
        self.assertLess(back, 0.03)  # faces the car
        self.assertLess(inside, 0.1)  # closed barrel: the disc in front, the car behind

    def test_open_barrel_inside(self):
        # no disc: the barrel's inside sees out through the open outboard end
        tris = self.barrel(-0.1, 0.1, 0.2)
        (inside,) = forza2dsf.wheel_sky([(0.08, 0.199, 0.0)], [(0.0, -1.0, 0.0)], tris)
        self.assertGreater(inside, 0.1)
        self.assertLess(inside, forza2dsf.WHEEL_SKY_OPEN / 2)


class WheelSource(unittest.TestCase):
    def test_disc_and_pack(self):
        disc = Source("FH1", "FH1", "media/cars/FER_250GTO_64.zip", 0, 0, "car")
        self.assertEqual(port_car.wheel_source(disc, "FER_250GTO_64"), ("media/wheels/FER_250GTO_64.zip", ""))
        fm4 = Source("FM4", "FM4 disc 1", "Media/cars/ALF_Mito_09.zip", 0, 0, "car")
        self.assertEqual(port_car.wheel_source(fm4, "ALF_Mito_09"), ("Media/wheels/ALF_Mito_09.zip", ""))
        pack = Source(
            "FM4",
            "FM4 disc 2",
            "Media/DLCZips/0001_pri_65.zip::media/cars/dod_chargerdaytona_69/",
            0,
            0,
            "car",
            package="content/x",
        )
        self.assertEqual(
            port_car.wheel_source(pack, "DOD_ChargerDaytona_69"),
            ("Media/DLCZips/0001_pri_65.zip", "media/wheels/dod_chargerdaytona_69/"),
        )


if __name__ == "__main__":
    unittest.main()
