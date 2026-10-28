"""Regression tests for the gloss materials: Forza's specular and reflection on trims and
rims (fm.gloss_params) drawn with copies of the host Lights material
(dsf_materials.GlossMaterial), on synthetic data (no game or Forza files).
research/forza-materials.md, "Gloss: Forza's specular and reflection on trims and rims".

    uv run python tests/test_gloss.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path
from unittest import mock

from dsfforza.convert import dsf_materials as dm
from dsfforza.convert import forza2dsf, trim_art
from dsfforza.convert import forza_materials as fm
from dsfforza.formats import carbin
from dsfforza.formats.chnk import Entry


class ForzaGloss(unittest.TestCase):
    def test_defaults_and_overrides(self):
        g = fm.gloss_params({}, "misc")
        self.assertEqual((g.specular, g.power, g.fresnel_scalar, g.cube), (0.2, 20.0, 0.1, 1.0))
        self.assertAlmostEqual(g.f0, (0.65 / 2.65) ** 2)
        # the Cougar Eliminator's own misc: a broad satin sheen
        cougar = {"misc": {"SpecularAmount": 0.5, "SpecularPower": 5.0, "FresnelScalar": 0.2, "FresnelIndex": 2.3}}
        g = fm.gloss_params(cougar, "MISC")
        self.assertEqual((g.specular, g.power, g.fresnel_scalar), (0.5, 5.0, 0.2))
        self.assertAlmostEqual(g.reflection, 0.2)
        # trim_art's subsections keep their source technique's gloss
        self.assertEqual(fm.gloss_params(cougar, "dsf_trim_detail@0.2~misc"), g)
        # techniques without a specular or a reflection in Forza
        self.assertIsNone(fm.gloss_params({}, "black"))
        self.assertIsNone(fm.gloss_params({}, "wheel_black"))
        self.assertEqual(fm.gloss_params({}, "frame").specular, 0.0)
        self.assertEqual(fm.gloss_params({}, "frame").reflection, 0.0)
        # chrome: the cube map at every angle, no specular
        chrome = fm.gloss_params({}, "chrome_rim")
        self.assertIsNone(chrome.fresnel_scalar)
        self.assertEqual((chrome.specular, chrome.reflection), (0.0, 1.0))
        self.assertEqual(fm.chrome_diffuse_scale({}, "chrome_rim"), 0.5)
        self.assertIsNone(fm.chrome_diffuse_scale({}, "rim"))
        self.assertEqual(fm.wheel_paint_scale("chrome_rim", {}), 0.5)

    def test_source_names(self):
        name = fm.with_source(fm.with_scale(trim_art.GLOSS_TECHNIQUE, 0.2), "plastic2")
        self.assertEqual(name, "dsf_trim_gloss@0.2~plastic2")
        self.assertEqual(fm.technique_scale(name), (trim_art.GLOSS_TECHNIQUE, 0.2))
        self.assertEqual(fm.technique_source(name), "plastic2")
        self.assertEqual(fm.technique_source("misc"), "misc")
        self.assertEqual(fm.classify(name), (fm.TEXTURED, fm.NODAMAGE))
        self.assertEqual(fm.technique_scale("dsf_trim_detail~misc"), ("dsf_trim_detail", 1.0))


class DsfGloss(unittest.TestCase):
    def test_mapping(self):
        satin = fm.Gloss(0.5, 5.0, 0.2, 1.0, 2.3)
        m = dm.gloss_material(satin, green=4)
        self.assertGreater(m.intensity, 0)
        self.assertLessEqual(m.power, 5.0)  # (N.H)^5 is a broad lobe: a lower (R.V) power
        self.assertEqual(m.fresnel_power, dm.GLOSS_FRESNEL_POWER)
        self.assertAlmostEqual(m.bias, round(satin.f0, 3))
        # the reflection amount makes up for the dark texel green that masks it
        lighter = dm.gloss_material(satin, green=64)
        self.assertGreater(m.reflection, lighter.reflection)
        # no specular and no reflection: no gloss material
        self.assertIsNone(dm.gloss_material(fm.Gloss(0.0, 128.0, 0.0, 1.0), green=20))
        # chrome: no Fresnel term
        chrome = dm.gloss_material(fm.gloss_params({}, "chrome_rim"), green=120)
        self.assertEqual((chrome.fresnel_power, chrome.bias), dm.CHROME_FRESNEL)
        self.assertEqual(chrome.intensity, 0.0)
        # the Boss 429's plastic2 (SpecularAmount 12) is capped at the vanilla Lights intensity
        self.assertLessEqual(dm.gloss_material(fm.Gloss(12.0, 14.0, 0.0, 1.0), 20).intensity, dm.GLOSS_INTENSITY_MAX)

    def test_merge(self):
        a = dm.GlossMaterial(0.5, 2.0, 0.0, 5.0, 0.06)
        b = dm.GlossMaterial(0.354, 2.0, 0.0, 5.0, 0.06)
        c = dm.GlossMaterial(0.0, 1.0, 4.0, 5.0, 0.06)
        merged = dm.merge_gloss({a: 10, b: 1, c: 5}, limit=2)
        self.assertEqual(merged, {a: a, b: a, c: c})
        self.assertEqual(dm.merge_gloss({a: 1, b: 1}, limit=8), {a: a, b: b})


def _marp(name: str, kind: int, value: bytes) -> Entry:
    data = name.encode().ljust(32, b"\0") + value.ljust(16, b"\0") + struct.pack("<I", kind) + b"\0" * 12
    return Entry("MARP", 0, len(data), 0, 0, None, payload=data)


def _maco(uid: bytes, path: str, params: dict[str, float], texture: bytes) -> Entry:
    marps = [_marp(k, 1, struct.pack("<f", v)) for k, v in params.items()]
    marps.append(_marp("g_texDiffuseMap", 9, texture))
    passes = [Entry("MARC", 0, 0, 0, 0, None, children=list(marps)) for _ in range(2)]
    passes.append(Entry("MARC", 0, 0, 0, 0, None, children=[_marp("g_shadowCastOffset", 1, struct.pack("<f", -5e-4))]))
    head = [
        Entry("MAHE", 0, 16, 0, 0, None, payload=uid + bytes(8)),
        Entry("MATX", 0, 0, 0, 0, None, payload=path.encode() + b"\0"),
    ]
    return Entry("MACO", 0, 0, 0, 0, None, children=head + passes)


LIGHTS = {  # the vanilla Lights material's
    "g_hSpecularIntensity": 1.0,
    "g_hSpecularPower": 150.0,
    "g_hReflectionAmount": 1.0,
    "g_hReflectionFresnelBias": -0.5,
    "g_hFresnelPower": 1.0,
}


class InstallGloss(unittest.TestCase):
    LIGHTS = LIGHTS

    def _drsp(self):
        tex = b"\x00\x12\x2b\x73\xd1\xfa\x12\x27"
        texc = Entry(
            "TEXC",
            0,
            0,
            0,
            0,
            None,
            children=[
                Entry("UIDC", 0, 8, 0, 0, None, payload=tex),
                Entry("DXTC", 0, 0, 0, 0, None, payload=b"DDS "),
            ],
        )
        chassis = _maco(b"C" * 8, r"R:\x\Car_Chassis.matxml", {"g_hSpecularIntensity": 0.03}, tex)
        lights = _maco(b"L" * 8, r"R:\x\Car_Lights.matxml", self.LIGHTS, tex)
        sbmc = Entry("SBMC", 0, 0, 0, 0, None, children=[])
        return Entry("DRSP", 0, 0, 0, 0, None, children=[texc, chassis, lights, sbmc])

    def test_install_full(self):
        drsp = self._drsp()
        gloss = [dm.GlossMaterial(0.125, 1.41, 2.0, 5.0, 0.155), dm.GlossMaterial(0.0, 1.0, 2.83, 16.0, 1.0)]
        installed = dm.install_full(None, drsp, b"DDS2", gloss=gloss, gloss_prefix=b"\x01\x02\x03\x84")
        self.assertEqual([e.tag for e in drsp.children], ["TEXC", "MACO", "MACO", "MACO", "MACO", "SBMC"])
        self.assertEqual(installed.materials[dm.CHASSIS], b"C" * 8)
        for n, mat in enumerate(gloss):
            uid = installed.materials[f"{dm.GLOSS_PREFIX}{n}"]
            self.assertEqual(uid, dm.gloss_uid(b"\x01\x02\x03\x84", n))
            self.assertEqual((uid[1], uid[7], uid[6] & 0x80), (0x12, 0x2D, 0))  # vanilla-looking, top bit clear
            self.assertEqual(installed.layout[uid], (32, 5))  # the Lights material's layout
            maco = drsp.children[3 + n]
            self.assertEqual(dm.material_info(None, maco)["uid"], uid)
            params = dm.material_params(None, maco)
            for name, value in mat.params().items():
                self.assertAlmostEqual(params[name], value, places=5)
        # the host's own materials are untouched
        host = dm.material_params(None, drsp.children[2])
        self.assertEqual({k: host[k] for k in self.LIGHTS}, self.LIGHTS)
        # nothing to add: no new MACO
        drsp = self._drsp()
        dm.install_full(None, drsp, b"DDS2")
        self.assertEqual(len(drsp.children), 4)


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


class TexturingGloss(unittest.TestCase):
    """Flat black trims with Forza's specular (trim_art.GLOSS_TECHNIQUE cells) and rims take
    gloss kinds; the cockpit's Texturing (a classifier) never does."""

    def setUp(self):
        size = 32
        nodamage = bytearray()
        for _ in range(size * size):
            nodamage += bytes((6, 6, 6, 255))  # opaque near black (the Cougar's scoop: 2 to 7)
        wheel = bytearray()
        for _ in range(size * size):
            wheel += bytes((180, 180, 180, 255))  # a silver rim
        atlases = {
            fm.NODAMAGE: (size, size, nodamage),
            fm.LIGHTS: (size, size, bytearray(size * size * 4)),
            fm.WHEEL: (size, size, wheel),
        }

        class FakeAtlases:
            def __init__(self, car_dir=None, lod0=False):
                self.car_dir = None

            def get(self, family):
                return atlases[family]

        self.patches = [
            mock.patch.object(fm, "AtlasCache", FakeAtlases),
            mock.patch.object(dm.Texturing, "wheel_texture", lambda self: True),
            mock.patch.object(fm, "shader_settings", lambda car_dir: {}),
        ]
        for p in self.patches:
            p.start()
        corners = [
            _Vertex((-0.1, 0.0, 0.0), (0.1, 0.1)),
            _Vertex((-0.1, 0.2, 0.0), (0.9, 0.1)),
            _Vertex((-0.1, 0.2, 0.2), (0.9, 0.9)),
        ]
        self.trim = _Sub(fm.with_source(trim_art.GLOSS_TECHNIQUE, "plastic2"), [(0, 1, 2)])
        self.flat = _Sub(fm.with_source(trim_art.GLOSS_TECHNIQUE, "frame"), [(0, 1, 2)])
        self.badge = _Sub("emblem", [(0, 1, 2)])
        self.body = _Section("body", corners, [self.trim, self.flat])
        self.rim = _Sub("rim", [(0, 1, 2)])
        self.chrome = _Sub("chrome_rim", [(0, 1, 2)], uv_transform=(0.0, 0.5, 0.0, 0.5))
        self.wheel = _Section("wheel", corners, [self.rim, self.chrome])

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_kinds(self):
        t = dm.Texturing(Path(), size=(64, 64))
        t.plan([self.body], {1}, wheels=([self.wheel], {1}))
        kinds = {sub.material: t.material(sec, sub) for sec, sub in ((self.body, self.trim), (self.body, self.flat))}
        self.assertTrue(kinds[self.trim.material].startswith(dm.GLOSS_PREFIX))
        self.assertEqual(kinds[self.flat.material], dm.CHASSIS)  # frame: no specular, no reflection in Forza
        rim, chrome = t.material(self.wheel, self.rim), t.material(self.wheel, self.chrome)
        self.assertTrue(rim.startswith(dm.GLOSS_PREFIX) and chrome.startswith(dm.GLOSS_PREFIX))
        self.assertNotEqual(rim, chrome)
        self.assertEqual(len(t.gloss_materials), 3)
        self.assertTrue(dm.is_atlas_kind(rim))
        # atlas UVs, as for the Chassis material
        _uvs, key = t.triangle_uvs(self.wheel, self.rim, 0, [v.uv0 for v in self.wheel.vertices])
        self.assertEqual(key[0], fm.WHEEL)
        _uvs, key = t.triangle_uvs(self.body, self.trim, 0, [v.uv0 for v in self.body.vertices])
        self.assertEqual(key[:4], ("swatch", 6, 6, 6))
        # the binding maps the kinds to the entry's gloss materials
        installed = dm.Installed(
            {dm.CHASSIS: b"C" * 8, dm.LIGHTS_MAT: b"L" * 8, rim: b"R" * 8, chrome: b"H" * 8}, b"T" * 8, {}
        )
        binding = dm.Binding(t, installed)
        self.assertEqual(forza2dsf.wheel_material(binding, self.wheel, self.rim, forza2dsf.SHARED), b"R" * 8)

    def test_disabled(self):
        t = dm.Texturing(Path(), size=(64, 64), gloss_enabled=False)
        t.plan([self.body], {1}, wheels=([self.wheel], {1}))
        self.assertEqual(t.material(self.wheel, self.rim), dm.CHASSIS)
        self.assertEqual(t.gloss_materials, [])


class TrimArtGloss(unittest.TestCase):
    """Visible flat black trims whose technique has a specular go to GLOSS cells, swatch
    subsections too; techniques without one stay on the shared trim."""

    def test_split(self):
        import numpy as np

        size = 16
        px = np.zeros((size, size, 4), np.uint8)
        px[..., :3] = 5
        px[..., 3] = 255
        atlas = (size, size, bytearray(px.tobytes()))

        class FakeAtlases:
            car_dir = None

            def get(self, family):
                return atlas

        verts = []

        def quad(y, x0, x1, uv):
            base = len(verts)
            for p in [(x0, y, -0.1), (x1, y, -0.1), (x1, y, 0.1), (x0, y, 0.1)]:
                verts.append(carbin.Vertex(p, uv, (0.5, 0.5), (0.0, 1.0, 0.0), bytes((100, 0, 0, 0))))
            return [base, base + 1, base + 2, base, base + 2, base + 3]

        textured = (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0)
        swatch = (0.5, 0.0, 0.5, 0.0, 0.0, 1.0, 0.0, 1.0)  # every vertex samples (0.5, 0.5)
        misc = carbin.SubSection("misc", 1, 4, textured, quad(0.0, -0.3, -0.1, (0.1, 0.1)) + [])
        # textured UVs: spread the quad's UVs over the atlas
        for i, uv in zip(misc.indices[:4], ((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9))):
            verts[i] = carbin.Vertex(verts[i].position, uv, verts[i].uv1, verts[i].normal, verts[i].extra)
        plastic2 = carbin.SubSection("plastic2", 1, 4, swatch, quad(0.0, 0.1, 0.3, (0.0, 0.0)))
        frame = carbin.SubSection("frame", 1, 4, swatch, quad(0.0, 0.35, 0.45, (0.0, 0.0)))
        section = carbin.Section("body", (0, 0, 0), (0, 0, 0), (1, 1, 1), verts, [], [misc, plastic2, frame])
        (out,), report = trim_art.split_trim_art([section], FakeAtlases(), settings={})
        names = [s.material for s in out.subsections]
        self.assertEqual(
            names,
            [
                fm.with_source(trim_art.GLOSS_TECHNIQUE, "misc"),
                fm.with_source(trim_art.GLOSS_TECHNIQUE, "plastic2"),
                "frame",
            ],
        )
        self.assertEqual(report["body/misc"]["gloss"], 2)
        self.assertEqual(report["body/plastic2"]["gloss"], 2)
        self.assertNotIn("body/frame", report)
        # the swatch keeps its UV transform: Texturing samples its one texel
        self.assertEqual(out.subsections[1].uv_transform, swatch)


if __name__ == "__main__":
    unittest.main()
