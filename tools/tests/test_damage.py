"""Regression tests for tools/damage.py panel binding, on synthetic data (no game
or Forza files).

    python tools/tests/test_damage.py     # or every test: python -m tools.tests damage
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import damage  # noqa: E402


def _plan(host_points: dict, records: dict) -> damage.DamagePlan:
    """A DamagePlan with only what panel lookup needs: host panels (pair ->
    role), a full-entry field with the given points per pair (crush (0.01, 0,
    -0.02) each), and a Forza car the size of the host."""
    plan = object.__new__(damage.DamagePlan)
    plan.panels = {pair: damage.Panel(pair, [], None, role) for pair, role in records.items()}
    plan.chassis = (0, 0)
    points, disp, pairs = [], [], []
    for pair, pts in host_points.items():
        for p in pts:
            points.append(p)
            disp.append((0.01, 0.0, -0.02))
            pairs.append(pair)
    box = damage.Box((-1.0, -0.5, -2.0), (1.0, 0.5, 2.0))
    f = damage.HostField(points, disp, pairs, box)
    for pair in host_points:
        ns = [n for n, q in enumerate(pairs) if q == pair]
        f.by_pair[pair] = damage.Grid([f.normalise(points[n]) for n in ns], ns)
    f.all_grid = damage.Grid([f.normalise(p) for p in points], list(range(len(points))))
    plan.full_field = plan.ext_field = f
    plan.forza_box = box
    plan.opts = damage.DamageOptions()
    return plan


MIRROR_LEFT = [(-0.85, 0.25, 0.0), (-0.8, 0.3, 0.05), (-0.9, 0.28, -0.05)]


class PanelBinding(unittest.TestCase):
    def test_rigid_panels_use_the_chassis_transform(self):
        # Doors and wings swing on stock cars (forceOpenPanels, crashes); a
        # converted door is a patch of the one-piece Forza skin, so it keeps
        # its damage index but takes the chassis' transform slot.
        plan = _plan({}, {(0, 0): 38, (8, 9): 32, (14, 16): 34, (17, 19): 4, (9, 10): 41})
        self.assertEqual(plan.bound((8, 9)), (0, 9))
        self.assertEqual(plan.bound((14, 16)), (0, 16))
        self.assertEqual(plan.bound((17, 19)), (17, 19))  # bonnet: swings, as stock
        self.assertEqual(plan.bound((9, 10)), (9, 10))    # mirror: detaches, as stock

    def test_side_part_never_takes_the_other_sides_record(self):
        # Host 278: a +x mirror record but no +x mirror vertices in its full
        # entry. The S2000's left mirror went onto the -x record with the right
        # one, and the pair came off as one body centred between them.
        plan = _plan({(0, 0): [(0.0, 0.0, 0.0)], (8, 10): MIRROR_LEFT}, {(0, 0): 38, (8, 10): 41, (11, 13): 40})
        self.assertEqual(plan._side_panel((40, 41), (0.85, 0.27, 0.0)), (11, 13))
        self.assertEqual(plan._side_panel((40, 41), (-0.85, 0.27, 0.0)), (8, 10))
        box = plan._host_box((11, 13))
        self.assertAlmostEqual(box.lo[0], 0.8)
        self.assertAlmostEqual(box.hi[0], 0.9)
        # No record on that side at all: None (the caller uses the chassis).
        plan = _plan({(0, 0): [(0.0, 0.0, 0.0)], (8, 10): MIRROR_LEFT}, {(0, 0): 38, (8, 10): 41})
        self.assertIsNone(plan._side_panel((40, 41), (0.85, 0.27, 0.0)))

    def test_centred_record_takes_both_sides(self):
        # A host twin exhaust spanning both sides (stock 278) keeps both.
        pipes = [(-0.45, -0.4, -1.9), (0.45, -0.4, -1.9), (0.0, -0.4, -1.9)]
        plan = _plan({(0, 0): [(0.0, 0.0, 0.0)], (2, 3): pipes}, {(0, 0): 38, (2, 3): 15})
        self.assertEqual(plan._side_panel((136, 15), (-0.45, -0.4, -1.9)), (2, 3))

    def test_borrowed_crush_is_mirrored(self):
        plan = _plan({(0, 0): [(0.0, 0.0, 0.0)], (8, 10): MIRROR_LEFT}, {(0, 0): 38, (8, 10): 41, (11, 13): 40})
        left = plan._disp(plan.full_field, (8, 10), (-0.85, 0.27, 0.0))
        right = plan._disp(plan.full_field, (11, 13), (0.85, 0.27, 0.0))
        self.assertAlmostEqual(left[0], 0.01)
        self.assertAlmostEqual(right[0], -0.01)
        self.assertAlmostEqual(right[2], left[2])


if __name__ == "__main__":
    unittest.main()
