"""Regression tests for dsff wheel_fit (src/dsfforza/convert/wheel_fit.py): how a wheel sits in
its arch, on synthetic geometry (a fender plate with a round arch cut out of it, a wheel-well
liner deeper inside, a tyre at a known place).

    uv run python tests/test_wheel_fit.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import math
import unittest
from itertools import pairwise

import numpy as np

from dsfforza.convert import wheel_clearance as wc
from dsfforza.convert import wheel_fit as wf

CELL = 0.02  # m: the synthetic surfaces' grid


def plate(x: float, zs, ys, keep) -> list:
    """Triangles of a plate at constant x over the grid zs x ys, the cells whose centre passes
    keep(z, y)."""
    out = []
    for z0, z1 in pairwise(zs):
        for y0, y1 in pairwise(ys):
            if keep((z0 + z1) / 2, (y0 + y1) / 2):
                a, b, c, d = (x, y0, z0), (x, y0, z1), (x, y1, z1), (x, y1, z0)
                out += [(a, b, c), (a, c, d)]
    return out


def body(arch_z: float, arch_y: float, arch_r: float, fender_x: float, liner_x: float) -> np.ndarray:
    """A fender at x = fender_x with a round opening (centre (arch_z, arch_y), radius arch_r)
    reaching down to y = 0, and behind the opening a liner at x = liner_x."""
    zs = np.arange(arch_z - 1.0, arch_z + 1.0 + 1e-9, CELL)
    ys = np.arange(0.0, 1.4 + 1e-9, CELL)

    def outside(z, y):
        return math.hypot(z - arch_z, y - arch_y) > arch_r

    def inside(z, y):
        return not outside(z, y)

    return np.array(plate(fender_x, zs, ys, outside) + plate(liner_x, zs, ys, inside), dtype=np.float64)


class Measures(unittest.TestCase):
    def test_wheel_in_its_arch(self):
        """A tyre 30 mm behind the arch's centre, 50 mm under its top and 20 mm inboard of the
        fender reads fore/aft +30, top gap 50 and lateral -20 (within a depth cell)."""
        radius, width = 0.32, 0.22
        arch_r = radius + 0.05
        centre = np.array([0.70, 0.34, -0.03])  # x: the tyre's centre plane; z 30 mm behind the arch
        fender = centre[0] + width / 2 + 0.02
        tris = body(0.0, centre[1], arch_r, fender, centre[0] - 0.05)
        fore, _, arch_radius, top, lateral, rms, n = wf.measure_wheel(tris, centre, radius, width, 1.0)
        tol = 2 * wf.ARCH_CELL
        self.assertAlmostEqual(fore, 0.03, delta=tol)
        self.assertAlmostEqual(top, 0.05, delta=tol)
        self.assertAlmostEqual(lateral, -0.02, delta=1e-6)
        self.assertAlmostEqual(arch_radius, arch_r, delta=tol)
        self.assertLess(rms, tol)
        self.assertGreaterEqual(n, wf.ARCH_MIN_LIPS)

    def test_mirrored_side(self):
        """A wheel on the -x side measures the same as its mirror image."""
        radius, width = 0.32, 0.22
        centre = np.array([0.70, 0.34, 0.0])
        tris = body(0.02, centre[1], radius + 0.04, centre[0] + width / 2, centre[0] - 0.05)
        left = wf.measure_wheel(tris, centre, radius, width, 1.0)
        mirrored = tris * [-1.0, 1.0, 1.0]
        right = wf.measure_wheel(mirrored, centre * [-1.0, 1.0, 1.0], radius, width, -1.0)
        for a, b in zip(left[:6], right[:6]):
            self.assertAlmostEqual(a, b, places=6)

    def test_liner_behind_the_tyre_is_hidden(self):
        """Body parts inside the tyre's outline and inboard of its outer face (the well's liner,
        suspension) are hidden by the tyre: a deep panel there doesn't move the arch's edge."""
        radius, width = 0.32, 0.22
        centre = np.array([0.70, 0.34, 0.0])
        tris = body(0.0, centre[1], radius + 0.05, centre[0] + width / 2 + 0.02, centre[0] - 0.05)
        clean = wf.measure_wheel(tris, centre, radius, width, 1.0)
        zs = np.arange(-0.2, 0.2 + 1e-9, CELL)
        ys = np.arange(centre[1] + 0.1, centre[1] + radius - 0.02, CELL)
        panel = np.array(plate(centre[0] + 0.05, zs, ys, lambda _z, _y: True))  # inboard of the face (0.81)
        cluttered = wf.measure_wheel(np.concatenate([tris, panel]), centre, radius, width, 1.0)
        self.assertAlmostEqual(cluttered[3], clean[3], places=6)  # top gap
        self.assertAlmostEqual(cluttered[0], clean[0], places=6)  # fore/aft

    def test_rest_pose(self):
        """measure() puts the wheel at rest: the hub moved by wheel_clearance.rest_offsets."""
        radius, width = 0.32, 0.22
        params = {0: 1200.0, 1: 2000.0, 3: 0.3, 4: 0.0, 25: 0.0, 32: 0.0, 505: 1.0, 507: 0.0}
        hubs = [(0.75, 0.30, 1.3), (-0.75, 0.30, 1.3), (0.75, 0.30, -1.3), (-0.75, 0.30, -1.3)]
        up = wc.rest_offsets(params, 1.3, -1.3)
        tris = []
        for hx, _, hz in hubs:
            side = math.copysign(1.0, hx)
            t = body(
                hz, 0.30 + up["front" if hz > 0 else "rear"], radius + 0.05, abs(hx) + width / 2 + 0.02, abs(hx) - 0.05
            )
            tris.append(t * [side, 1.0, 1.0])
        wheels = [
            wc.Wheel(
                ("F" if hz > 0 else "R") + ("L" if hx > 0 else "R"),
                np.array((hx, hy, hz)),
                hz > 0,
                radius,
                width,
                np.zeros(3),
            )
            for hx, hy, hz in hubs
        ]
        car = wc.Car(1, np.concatenate(tris), wheels, params)
        rows = wf.measure(car, "X")
        self.assertEqual([r.wheel for r in rows], ["FL", "FR", "RL", "RR"])
        for r in rows:
            self.assertAlmostEqual(r.rest_up_mm, 1000 * up["front" if r.wheel[0] == "F" else "rear"], places=6)
            self.assertAlmostEqual(r.top_gap_mm, 50.0, delta=2000 * wf.ARCH_CELL)
            self.assertAlmostEqual(r.fore_aft_mm, 0.0, delta=2000 * wf.ARCH_CELL)
            self.assertAlmostEqual(r.lateral_mm, -20.0, delta=1e-3)
        self.assertEqual(len(rows[0].cells()), len(wf.HEADER))


if __name__ == "__main__":
    unittest.main()
