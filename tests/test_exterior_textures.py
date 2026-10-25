"""Regression tests for the exterior textures: rim textures from the car's wheel.xds, the
alpha test of decal triangles and their cut-out geometry, and the wheel's baked sky term,
on synthetic data (no game or Forza files).

    uv run python tests/test_exterior_textures.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

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
    """Forza's chrome techniques go to DSF's shared chrome materials (stock: GLOBAL_LOD_Chrome
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
        self.assertEqual(t.material(self.wheel, self.rim), dm.CHASSIS)
        self.assertEqual(t.material(self.wheel, self.tire), fm.TYRE)
        self.assertEqual(t.material(self.wheel, self.blur), fm.SKIP)
        uvs, key = t.triangle_uvs(self.wheel, self.rim, 0, [self.wheel.vertices[i].uv0 for i in (0, 1, 2)])
        self.assertEqual(key, (fm.WHEEL, 0))
        rgba = t.render()
        x, y = (int(c * n) for c, n in zip(uvs[2], t.size))
        self.assertEqual(tuple(rgba[(y * t.size[0] + x) * 4 : (y * t.size[0] + x) * 4 + 3]), (200, 60, 10))

    def test_wheel_material(self):
        t = dm.Texturing(Path())
        t.plan([], {1}, wheels=([self.wheel], {1}))
        chassis, lights = b"C" * 8, b"L" * 8
        binding = dm.Binding(t, dm.Installed({dm.CHASSIS: chassis, dm.LIGHTS_MAT: lights}, b"T" * 8, {}))
        shared = forza2dsf.SHARED_EXTERIOR
        self.assertEqual(forza2dsf.wheel_material(binding, self.wheel, self.rim, shared), chassis)
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

    def test_visible_artwork_moves(self):
        paint = carbin.SubSection("body", 1, 4, self.transform, self.box(0.5))
        white = self.top(0.52, -0.3, -0.1, (4, 4, 28, 28))  # on the roof: a white badge
        dark = self.top(0.52, 0.1, 0.3, (36, 4, 60, 28))  # on the roof: dark trim
        filler = self.top(0.52, 0.32, 0.45, (4, 36, 28, 60))  # white but alpha 0
        hidden = self.top(0.0, -0.3, 0.3, (4, 4, 28, 28))  # white, inside the box
        misc = carbin.SubSection("misc", 1, 4, self.transform, white + dark + filler + hidden)
        black = carbin.SubSection("black", 1, 4, self.transform, list(white))
        lod3 = carbin.SubSection("misc", 3, 4, self.transform, list(white))
        section = carbin.Section("body", (0, 0, 0), (0, 0, 0), (1, 1, 1), self.verts, [], [paint, misc, black, lod3])
        (out,), report = trim_art.split_trim_art([section], self.atlases, lods={1})
        self.assertEqual(report, {"body/misc": {"triangles": 2, "area": unittest.mock.ANY}})
        self.assertAlmostEqual(report["body/misc"]["area"], 0.2 * 0.2, places=6)
        names = [s.material for s in out.subsections]
        self.assertEqual(names, ["body", "misc", trim_art.ART_TECHNIQUE[fm.NODAMAGE], "black", "misc"])
        self.assertIs(out.subsections[0], paint)
        self.assertIs(out.subsections[3], black)  # black* stays trim
        self.assertIs(out.subsections[4], lod3)  # not a requested LOD
        self.assertEqual(out.subsections[1].indices, dark + filler + hidden)
        self.assertEqual(out.subsections[2].indices, white)
        self.assertEqual(out.subsections[2].uv_transform, self.transform)

    def test_nothing_to_move(self):
        dark = self.top(0.0, 0.1, 0.3, (36, 4, 60, 28))
        section = carbin.Section(
            "body",
            (0, 0, 0),
            (0, 0, 0),
            (1, 1, 1),
            self.verts,
            [],
            [carbin.SubSection("misc", 1, 4, self.transform, dark)],
        )
        (out,), report = trim_art.split_trim_art([section], self.atlases)
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


class WheelSky(unittest.TestCase):
    """Wheel vertices get a baked sky term like stock wheels (outward faces about
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
