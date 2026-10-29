"""Regression tests for the brake rotors in the wheel parts (research/wheels.md, "Brake
rotors"): forza2dsf.brake_rotor_section's placement at Forza's own size, its disc and hub
roles, the plain stand-in for heavy rotors, and the materials and swatches the rotor is
drawn with, on synthetic data (no game or Forza files).

    uv run python tests/test_brake_rotors.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dsfforza.convert import forza2dsf
from dsfforza.formats import carbin

IDENTITY_UV = (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0)


def _ring(radius: float, x: float, count: int):
    return [
        (x, radius * math.sin(2 * math.pi * k / count), radius * math.cos(2 * math.pi * k / count))
        for k in range(count)
    ]


def _annulus(verts, inner: float, outer: float, x: float, count: int, normal) -> list[int]:
    """Indices of a flat ring at x, wound counter-clockwise about `normal`."""
    a = len(verts)
    for p in _ring(inner, x, count) + _ring(outer, x, count):
        verts.append(carbin.Vertex(p, (0.0, 0.0), (0.0, 0.0), normal))
    out = []
    for k in range(count):
        i0, i1, o0, o1 = a + k, a + (k + 1) % count, a + count + k, a + count + (k + 1) % count
        tris = [(i0, o0, o1), (i0, o1, i1)]
        for t in tris:
            p = [verts[i].position for i in t]
            u = [p[1][j] - p[0][j] for j in range(3)]
            v = [p[2][j] - p[0][j] for j in range(3)]
            cross_x = u[1] * v[2] - u[2] * v[1]
            out.extend(t if (cross_x > 0) == (normal[0] > 0) else (t[0], t[2], t[1]))
    return out


def _rotor(count: int = 16, material_disc: str = "rotor", material_hub: str = "hub"):
    """A Forza rotor carbin's section (hub frame, the hub face at -x): a friction ring at
    x +0.02, r 0.09..0.14, and a hub face at x -0.03, r 0.01..0.07."""
    verts: list[carbin.Vertex] = []
    disc = _annulus(verts, 0.09, 0.14, 0.02, count, (1.0, 0.0, 0.0))
    hub = _annulus(verts, 0.01, 0.07, -0.03, count, (-1.0, 0.0, 0.0))
    subs = [
        carbin.SubSection(material_disc, 0, 4, IDENTITY_UV, disc),
        carbin.SubSection(material_hub, 0, 4, IDENTITY_UV, hub),
    ]
    return [carbin.Section("rotorLF", (0.0, 0.0, 0.0), (-0.03, -0.14, -0.14), (0.02, 0.14, 0.14), [], verts, subs, 3)]


class Section(unittest.TestCase):
    def test_forza_size_whatever_the_fit(self):
        """The wheel part's fit scales the wheel model, not the rotor: through wheel_point the
        rotor comes out at its own size, turned like the wheel, about the tyre's centre."""
        fit = (1.25, 0.9, 0.01, 0.0, 0.0)
        section = forza2dsf.brake_rotor_section(_rotor(), 1, fit)
        pts = [forza2dsf.wheel_point(v.position, fit) for v in section.lod_vertices]
        radius = max(math.hypot(p[1], p[2]) for p in pts)
        self.assertAlmostEqual(radius, 0.14, places=6)
        xs = sorted({round(p[0], 6) for p in pts})
        self.assertEqual(xs, [round(-(0.02 - 0.01), 6), round(-(-0.03 - 0.01), 6)])  # the hub face outboard (+x)

    def test_roles_by_radius_not_name(self):
        """The friction ring is the disc whatever Forza called it (201 rotors are all `hub`)."""
        section = forza2dsf.brake_rotor_section(_rotor(material_disc="hub"), 1)
        subs = {sub.material: sub for sub in section.subsections}
        self.assertEqual(set(subs), {forza2dsf.ROTOR_DISC, forza2dsf.ROTOR_HUB})
        for role, (lo, hi) in ((forza2dsf.ROTOR_DISC, (0.09, 0.14)), (forza2dsf.ROTOR_HUB, (0.01, 0.07))):
            r = [math.hypot(*section.lod_vertices[i].position[1:]) for i in subs[role].indices]
            self.assertGreaterEqual(min(r), lo - 1e-6)
            self.assertLessEqual(max(r), hi + 1e-6)
        self.assertTrue(all(sub.lod == 1 for sub in section.subsections))
        self.assertFalse(section.plain)

    def test_heavy_rotor_gets_a_plain_disc(self):
        rotor = _rotor(count=forza2dsf.BRAKE_ROTOR_MAX_TRIANGLES)  # 4 triangles per segment
        section = forza2dsf.brake_rotor_section(rotor, 1)
        self.assertTrue(section.plain)
        tris = sum(len(sub.triangles()) for sub in section.subsections)
        self.assertLess(tris, 400)
        pts = [v.position for v in section.lod_vertices]
        self.assertAlmostEqual(max(math.hypot(p[1], p[2]) for p in pts), 0.14, places=3)
        self.assertAlmostEqual(min(p[0] for p in pts), -0.03, places=6)
        self.assertAlmostEqual(max(p[0] for p in pts), 0.02, places=6)
        # wound like the carbin: every triangle counter-clockwise about its normal (_rotor's)
        for sub in section.subsections:
            pool = section.vertices_for(sub)
            for t in sub.triangles():
                self.assertTrue(forza2dsf._winding([([pool[i].position for i in t], [pool[i].normal for i in t])]))

    def test_nothing_drawn(self):
        empty = [carbin.Section("rotorLF", (0.0,) * 3, (0.0,) * 3, (0.0,) * 3, [], [], [], 3)]
        self.assertIsNone(forza2dsf.brake_rotor_section(empty, 1))

    def test_shadows_left_out(self):
        rotor = _rotor()
        rotor[0].subsections.append(carbin.SubSection("shadow", 0, 4, IDENTITY_UV, rotor[0].subsections[0].indices))
        section = forza2dsf.brake_rotor_section(rotor, 1)
        self.assertEqual(sum(len(sub.triangles()) for sub in section.subsections), 4 * 16)


class Materials(unittest.TestCase):
    def setUp(self):
        self.section = forza2dsf.brake_rotor_section(_rotor(), 1)

    def test_tyre_material_and_swatches(self):
        textures = mock.Mock()  # never consulted for the rotor
        for sub in self.section.subsections:
            self.assertTrue(forza2dsf.is_rotor(sub))
            self.assertEqual(forza2dsf.wheel_material(textures, self.section, sub, {}), forza2dsf.TYRE)
            self.assertIn(forza2dsf.wheel_role(sub.material), forza2dsf.WHEEL_UV)
        textures.texturing.material.assert_not_called()

    def test_drawn_on_the_tyre_material(self):
        builder = forza2dsf.Builder(bytes(4))
        subs = forza2dsf.add_geometry(builder, [self.section], 1, forza2dsf.Placement(0.0, 0.0), wheel=True)
        self.assertEqual({s.material for s in subs}, {forza2dsf.TYRE})
        self.assertEqual(sum(s.index_count for s in subs) // 3, 4 * 16)


class Loading(unittest.TestCase):
    def test_no_rotor_files(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(forza2dsf.load_rotors(Path(folder)), {})

    def test_unreadable_rotor_left_out(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / forza2dsf.ROTOR_CARBINS["front"]).write_bytes(b"not a carbin")
            self.assertEqual(forza2dsf.load_rotors(Path(folder)), {})


if __name__ == "__main__":
    unittest.main()
