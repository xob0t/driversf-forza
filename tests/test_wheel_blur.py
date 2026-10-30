"""Regression tests for the wheel speed blur (research/wheels.md, "Speed blur"): the disc
forza2dsf.wheel_blur_section puts over the rim face, its vertex semantic 9 and material,
the blur picture's texels and tint (dsf_materials.wheel_blur_texels) and the wheel blur
colour (paint.set_wheel_blur_colour), on synthetic data (no game or Forza files).

    uv run python tests/test_wheel_blur.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import math
import struct
import unittest
from pathlib import Path
from unittest import mock

from dsfforza.convert import dsf_materials as dm
from dsfforza.convert import forza2dsf, paint
from dsfforza.convert import forza_materials as fm
from dsfforza.convert.forza2dsf import wheels as f2d_wheels
from dsfforza.formats import carbin
from dsfforza.formats.chnk import Entry

IDENTITY_UV = (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0)


def _e(tag, payload=None, children=None) -> Entry:
    return Entry(tag, 0, len(payload or b""), 0, 0, None, children=children, payload=payload)


def _ring(radius: float, x: float, count: int = 12):
    return [
        (x, radius * math.sin(2 * math.pi * k / count), radius * math.cos(2 * math.pi * k / count))
        for k in range(count)
    ]


def _wheel(lods=(1, 5), rim_at=(1,)):
    """A Forza wheel model (rim face at -x): spokes from a hub at x -0.06 to a lip at x -0.10,
    r 0.2; a tyre r 0.21..0.30; Forza's blur_rim disc at x -0.05, r 0..0.15, mapped onto its
    picture like Forza's (picture radius 0.5 = 0.2 m). `rim_at`: the LODs with a sharp rim."""
    verts, subs = [], []

    def add(points, uv=None):
        base = len(verts)
        for p in points:
            u = (0.5 + p[2] / 0.4, 0.5 - p[1] / 0.4) if uv is None else uv
            verts.append(carbin.Vertex(p, u, (0.0, 0.0), (-1.0, 0.0, 0.0)))
        return base

    for lod in lods:
        hub = add([(-0.06, 0.0, 0.0)])
        lip = add(_ring(0.2, -0.10))
        spokes = [i for k in range(0, 12, 3) for i in (hub, lip + k, lip + k + 1)]
        if lod in rim_at:
            subs.append(carbin.SubSection("rim", lod, 4, IDENTITY_UV, spokes))
        inner, outer = add(_ring(0.21, -0.11)), add(_ring(0.30, -0.10))
        tyre = []
        for k in range(12):
            a, b, c, d = inner + k, inner + (k + 1) % 12, outer + k, outer + (k + 1) % 12
            tyre += [a, c, d, a, d, b]
        subs.append(carbin.SubSection("tire", lod, 4, IDENTITY_UV, tyre))
        centre, ring = add([(-0.05, 0.0, 0.0)]), add(_ring(0.15, -0.05))
        blur = [i for k in range(12) for i in (centre, ring + k, ring + (k + 1) % 12)]
        subs.append(carbin.SubSection("blur_rim", lod, 4, IDENTITY_UV, blur))
    return carbin.Section("wheel", (0.0, 0.0, 0.0), (-0.1, -0.3, -0.3), (0.1, 0.3, 0.3), verts, [], subs, 3)


class Disc(unittest.TestCase):
    def setUp(self):
        self.wheel = _wheel()
        self.disc = forza2dsf.wheel_blur_section([self.wheel], {1, 5})
        self.by_lod = {sub.lod: sub for sub in self.disc.subsections}

    def points(self, lod):
        return [self.disc.lod_vertices[i] for t in self.by_lod[lod].triangles() for i in t]

    def test_one_disc_per_lod(self):
        self.assertEqual(self.disc.name, forza2dsf.WHEEL_BLUR_SECTION)
        self.assertEqual(sorted(self.by_lod), [1, 5])
        for sub in self.disc.subsections:
            self.assertEqual(sub.material, fm.WHEEL_BLUR_TECHNIQUE)
            self.assertTrue(forza2dsf.is_blur_disc(sub))

    def test_fades_in_over_a_rim_and_stays_without_one(self):
        """Forza's LOD 5 (503 roster cars) has no sharp rim, only its blur disc: the disc is the
        rim there and is always drawn."""
        self.assertFalse(self.by_lod[1].always)
        self.assertTrue(self.by_lod[5].always)
        self.assertTrue(forza2dsf.wheel_has_rim([self.wheel], 1))
        self.assertFalse(forza2dsf.wheel_has_rim([self.wheel], 5))

    def test_in_front_of_the_rim_out_to_its_edge(self):
        pts = self.points(1)
        radii = [math.hypot(v.position[1], v.position[2]) for v in pts]
        self.assertAlmostEqual(max(radii), 0.2, places=3)  # the lip, inside the tyre (0.21)
        hub = min(pts, key=lambda v: math.hypot(v.position[1], v.position[2]))
        edge = max(pts, key=lambda v: math.hypot(v.position[1], v.position[2]))
        # the outermost rim point near each ring (the lip at -0.10 reaches the outer rings),
        # WHEEL_BLUR_LIFT in front; the face is at -x in Forza's wheel frame
        self.assertLess(hub.position[0], -0.06)
        self.assertAlmostEqual(edge.position[0], -0.10 - forza2dsf.WHEEL_BLUR_LIFT, places=4)
        for v in pts:
            self.assertEqual(v.normal, (-1.0, 0.0, 0.0))

    def test_flat_face_takes_one_ring(self):
        """LOD 5: the blur_rim and tyre only, a flat face: the centre fan alone."""
        self.assertEqual(len(self.by_lod[5].triangles()), forza2dsf.WHEEL_BLUR_SEGMENTS)

    def test_uvs(self):
        """UV0: Forza's own map of its picture (fitted to blur_rim); UV1: the vanilla disc map
        (picture radius 0.5 at the disc's edge), for the shared SpokeDisc texture."""
        self.assertIsNotNone(forza2dsf.blur_picture_map([self.wheel]))
        for v in self.points(1):
            _, y, z = v.position
            self.assertAlmostEqual(v.uv0[0], min(1.0, max(0.0, 0.5 + z / 0.4)), places=4)
            self.assertAlmostEqual(v.uv0[1], min(1.0, max(0.0, 0.5 - y / 0.4)), places=4)
            self.assertAlmostEqual(v.uv1[0], 0.5 + z / 0.4, places=4)  # the edge is at 0.2 m here too
            self.assertAlmostEqual(v.uv1[1], 0.5 - y / 0.4, places=4)

    def test_no_rim_geometry_no_disc(self):
        tyre_only = carbin.Section("wheel", (0, 0, 0), (0, 0, 0), (0, 0, 0), self.wheel.lod_vertices, [], [])
        tyre_only.subsections = [s for s in self.wheel.subsections if s.material == "tire"]
        self.assertIsNone(forza2dsf.wheel_blur_section([tyre_only], {1}))

    def test_rim_drawn_as_emblem(self):
        """DOD_StealthTurbo_96 draws its whole rim with wheel_emblem (from the wheel texture):
        a sharp rim, so the disc fades in; a lone centre cap is no rim."""
        for sub in self.wheel.subsections:
            if sub.material == "rim":
                sub.material = "wheel_emblem"
        self.assertTrue(forza2dsf.wheel_has_rim([self.wheel], 1))
        cap = carbin.Section("cap", (0, 0, 0), (0, 0, 0), (0, 0, 0), [], [], [])
        cap.lod_vertices = [carbin.Vertex(p, (0, 0), (0, 0), (-1, 0, 0)) for p in [(-0.1, 0, 0), *_ring(0.03, -0.1)]]
        cap.subsections = [carbin.SubSection("rim", 5, 4, IDENTITY_UV, [0, 1, 2, 0, 2, 3])]
        self.assertFalse(forza2dsf.wheel_has_rim([self.wheel, cap], 5))

    def test_tyre_closing_the_wheel(self):
        """DOD_ViperSRT10_03's tyre mesh also closes the wheel at r 0: the disc still ends at
        the tyre's inner radius, not at the axle."""
        verts = self.wheel.lod_vertices
        base = len(verts)
        verts.append(carbin.Vertex((0.1, 0.0, 0.0), (0, 0), (0, 0), (1, 0, 0)))
        tyre = next(s for s in self.wheel.subsections if s.material == "tire" and s.lod == 1)
        tyre.indices += [base, base - 1, base - 2]  # a fan triangle from the axle (inboard)
        disc = forza2dsf.wheel_blur_section([self.wheel], {1})
        radii = [math.hypot(v.position[1], v.position[2]) for v in disc.lod_vertices]
        self.assertAlmostEqual(max(radii), 0.2, places=3)

    def test_left_out_of_the_wheels_sky(self):
        """The disc must not shade the rim it covers (wheel_skies' voxel march)."""
        with mock.patch.object(f2d_wheels, "wheel_sky", side_effect=lambda pts, nrm, tris: [len(tris)] * len(pts)):
            f2d_wheels._wheel_sky_cache.clear()
            with_disc = forza2dsf.wheel_skies([self.wheel, self.disc], 1)
            f2d_wheels._wheel_sky_cache.clear()
            alone = forza2dsf.wheel_skies([self.wheel], 1)
        self.assertEqual(with_disc, alone)


class Geometry(unittest.TestCase):
    """add_geometry writes the disc like vanilla discs: stride 20, MODL flag 7, semantic 9
    byte 3 = 2 (fades in) or 1 (always), the rim's outward sky."""

    def build(self, textures=None, shared=None):
        wheel = _wheel()
        disc = forza2dsf.wheel_blur_section([wheel], {1, 5})
        out = {}
        for lod in (1, 5):
            builder = forza2dsf.Builder(b"\x01\x02\x03\x04")
            subs = forza2dsf.add_geometry(
                builder, [wheel, disc], lod, forza2dsf.Placement(0.0, 0.0), wheel=True, textures=textures, shared=shared
            )
            out[lod] = (builder, subs)
        return out

    def disc_vertices(self, builder, subs, material):
        (sub,) = [s for s in subs if s.material == material]
        self.assertEqual((sub.stride, sub.flag), (20, 7))
        data = builder.buffers[20]
        first = min(builder.indices[sub.first_index : sub.first_index + sub.index_count])
        last = max(builder.indices[sub.first_index : sub.first_index + sub.index_count])
        return [data[(sub.base_vertex + i) * 20 : (sub.base_vertex + i + 1) * 20] for i in range(first, last + 1)]

    def test_shared_disc_without_textures(self):
        built = self.build()
        for lod, byte3 in ((1, 2), (5, 1)):
            builder, subs = built[lod]
            verts = self.disc_vertices(builder, subs, forza2dsf.SPOKE_DISC)
            for v in verts:
                x, _, _, w = struct.unpack_from("<4h", v)
                self.assertEqual(v[12:16], bytes((0, 0, 0, byte3)))  # semantic 9 after position and UV
                self.assertEqual(w, round(forza2dsf.WHEEL_BLUR_SKY * 4096))
                self.assertGreater(x, 0)  # turned to face +x, like vanilla discs

    def test_exterior_uses_the_lod1_disc(self):
        built = self.build(shared=forza2dsf.SHARED_EXTERIOR)
        builder, subs = built[1]
        self.assertTrue(self.disc_vertices(builder, subs, forza2dsf.SPOKE_DISC_LOD1))
        self.assertEqual(forza2dsf.MATERIAL_LAYOUT[forza2dsf.SPOKE_DISC_LOD1], (20, 7))


class Picture(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(fm.classify_wheel(fm.WHEEL_BLUR_TECHNIQUE), (fm.TEXTURED, fm.WHEEL_BLUR))
        self.assertEqual(fm.classify_wheel("blur_rim")[0], fm.SKIP)  # Forza's own discs stay out
        self.assertTrue(dm.is_atlas_kind(dm.WHEEL_BLUR_MAT))

    def test_texels(self):
        """Grey in red (over the tint), gloss in green at the vanilla disc's ratio, opaque blue:
        the picture laid over the dark backdrop so the rim under the disc is hidden."""
        px = bytearray()
        px += bytes((200, 200, 200, 255))  # a spoke, opaque
        px += bytes((200, 200, 200, 0))  # between the spokes: the backdrop
        px += bytes((200, 200, 200, 128))  # blurred spoke: half each
        out, tint = dm.wheel_blur_texels(px, paint_scale=1.0)
        self.assertEqual([round(c, 3) for c in tint], [1.0, 1.0, 1.0])
        back = dm.WHEEL_BLUR_BACKDROP
        half = 200 * 128 / 255 + back * (1 - 128 / 255)
        self.assertEqual(list(out[0:4]), [200, round(200 * dm.WHEEL_BLUR_GLOSS), 255, 255])
        self.assertEqual(list(out[4:8]), [back, round(back * dm.WHEEL_BLUR_GLOSS), 255, 255])
        self.assertEqual(out[8], round(half))

    def test_paint_scale_and_tint(self):
        """An orange picture (the Veyron's): the tint carries the hue, red the brightness, so
        red x tint gives the colour back; the rim's PaintScale scales it first."""
        px = bytearray(bytes((240, 80, 0, 255)) * 4)
        out, tint = dm.wheel_blur_texels(px, paint_scale=0.75)
        self.assertAlmostEqual(tint[0], 1.0)
        self.assertAlmostEqual(tint[1], 60 / 180, places=2)
        self.assertAlmostEqual(out[0] * tint[0], 180, delta=1)
        self.assertAlmostEqual(out[0] * tint[1], 60, delta=1)


class Atlas(unittest.TestCase):
    """With the shared SpokeDisc materials at hand, the disc samples the picture through the
    atlas on its copy of the SpokeDisc (WHEEL_BLUR_MAT); without the picture, the shared disc."""

    def setUp(self):
        picture = bytearray(bytes((120, 120, 120, 255)) * (64 * 64))
        wheel = bytearray(bytes((200, 200, 200, 0)) * (32 * 32))
        self.atlases = {
            fm.WHEEL: (32, 32, wheel),
            fm.WHEEL_BLUR: (64, 64, picture),
            fm.NODAMAGE: (16, 16, bytearray(16 * 16 * 4)),
            fm.LIGHTS: (16, 16, bytearray(16 * 16 * 4)),
        }
        atlases = self.atlases

        class FakeAtlases:
            def __init__(self, car_dir, lod0=False):
                pass

            def get(self, family):
                return atlases[family]

        self.patches = [mock.patch.object(fm, "AtlasCache", FakeAtlases)]
        for p in self.patches:
            p.start()
        self.wheel = _wheel()
        self.disc = forza2dsf.wheel_blur_section([self.wheel], {1, 5})

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def plan(self, has):
        with mock.patch.object(dm.Texturing, "_has", lambda self, family: family in has):
            t = dm.Texturing(Path())
            t.plan([], {1}, wheels=([self.wheel, self.disc], {1, 5}), wheel_blur=True)
        return t

    def test_on_the_atlas(self):
        t = self.plan({fm.WHEEL, fm.WHEEL_BLUR})
        self.assertIn(fm.WHEEL_BLUR, t.families)
        sub = self.disc.subsections[0]
        self.assertEqual(t.material(self.disc, sub), dm.WHEEL_BLUR_MAT)
        self.assertEqual(t.wheel_blur_tint, (1.0, 1.0, 1.0))
        self.assertLessEqual(t.scale[fm.WHEEL_BLUR], dm.WHEEL_BLUR_TEXELS / 64)
        binding = dm.Binding(t, dm.Installed({dm.WHEEL_BLUR_MAT: b"B" * 8}, b"T" * 8, {}))
        self.assertEqual(forza2dsf.wheel_material(binding, self.disc, sub, forza2dsf.SHARED), b"B" * 8)
        uvs, key = t.triangle_uvs(self.disc, sub, 0, [self.disc.lod_vertices[i].uv0 for i in sub.triangles()[0]])
        self.assertEqual(key, (fm.WHEEL_BLUR, 0))
        rgba = t.render()
        x, y = (int(c * n) for c, n in zip(uvs[0], t.size))
        o = (y * t.size[0] + x) * 4
        grey = 120 * fm.WHEEL_PAINT_SCALE["rim"]  # the rim's PaintScale, as the rim's texels
        self.assertEqual(tuple(rgba[o : o + 4]), (round(grey), round(grey * dm.WHEEL_BLUR_GLOSS), 255, 255))

    def test_without_the_picture(self):
        t = self.plan({fm.WHEEL})
        self.assertNotIn(fm.WHEEL_BLUR, t.families)
        self.assertIsNone(t.wheel_blur_tint)
        sub = self.disc.subsections[0]
        binding = dm.Binding(t, dm.Installed({}, b"T" * 8, {}))
        self.assertEqual(forza2dsf.wheel_material(binding, self.disc, sub, forza2dsf.SHARED), forza2dsf.SPOKE_DISC)


class Material(unittest.TestCase):
    def test_copy_samples_the_atlas(self):
        """The disc's material: the shared SpokeDisc MACO with the atlas texture and its own UID."""

        def marp(name, value, kind):
            return _e("MARP", payload=name.encode().ljust(32, b"\0") + value.ljust(16, b"\0") + struct.pack("<I", kind))

        source = _e(
            "MACO",
            children=[
                _e("MAHE", payload=dm.SPOKE_DISC_MATERIALS[0] + bytes(8)),
                _e("MATX", payload=b"x.matxml\0"),
                _e("MARC", children=[marp("g_texDiffuseMap", dm.SPOKE_DISC_TEXTURE, 9), marp("SortMode", b"\1", 10)]),
            ],
        )
        drsp = _e("DRSP", children=[_e("TEXC", children=[]), _e("SBMC", children=[])])
        uid = dm.blur_disc_material(None, drsp, source, b"T" * 8, b"U" * 8)
        self.assertEqual(uid, b"U" * 8)
        self.assertEqual([e.tag for e in drsp.children], ["TEXC", "MACO", "SBMC"])
        info = dm.material_info(None, drsp.children[1])
        self.assertEqual((info["uid"], info["textures"]), (b"U" * 8, {b"T" * 8}))
        installed = dm._with_blur(None, drsp, dm.Installed({}, b"T" * 8, {}), (source, b"V" * 8))
        self.assertEqual(installed.materials[dm.WHEEL_BLUR_MAT], b"V" * 8)
        self.assertEqual(installed.layout[b"V" * 8], dm.WHEEL_BLUR_LAYOUT)


class Colour(unittest.TestCase):
    def test_set_wheel_blur_colour(self):
        def marp(name, values):
            return _e(
                "MARP", payload=name.encode().ljust(32, b"\0") + struct.pack("<4f", *values) + struct.pack("<I", 3)
            )

        blur = marp("g_vec4WheelBlurColour", (0.5, 0.5, 0.5, 1.0))
        spsr = _e(
            "SPSR",
            children=[
                _e("SPID", payload=bytes(4)),
                _e("SPSS", children=[_e("SPSP", children=[marp("g_vec3TintColour2", (1, 0, 0, 0))])]),
                _e("SPSS", children=[_e("SPSP", children=[blur])]),
            ],
        )
        full = _e("VEHC", children=[_e("NONR", children=[spsr])])
        self.assertTrue(paint.set_wheel_blur_colour(None, full, (1.0, 0.25, 0.0)))
        self.assertEqual(paint.marp_value(blur.payload), (1.0, 0.25, 0.0, 1.0))
        empty = _e("VEHC", children=[_e("NONR", children=[])])
        self.assertFalse(paint.set_wheel_blur_colour(None, empty, (1.0, 1.0, 1.0)))


if __name__ == "__main__":
    unittest.main()
