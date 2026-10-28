"""Regression tests for src/dsfforza/convert/damage.py panel binding, on synthetic data (no game
or Forza files).

    uv run python tests/test_damage.py     # or every test: uv run python -m tests damage
"""

from __future__ import annotations

import unittest

from dsfforza.convert import damage, forza2dsf
from dsfforza.formats import carbin


def _plan(host_points: dict, records: dict) -> damage.DamagePlan:
    """A DamagePlan with only what panel lookup needs: host panels (pair ->
    role), a full-entry field with the given points per pair (crush (0.01, 0,
    -0.02) each), and a Forza car the size of the host."""
    plan = object.__new__(damage.DamagePlan)
    plan.panels = {pair: damage.Panel(pair, [], None, role) for pair, role in records.items()}
    plan.chassis = (0, 0)
    plan.swinging = set()
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
        # Doors and wings swing on vanilla cars (forceOpenPanels, crashes); a
        # converted door is a patch of the one-piece Forza skin, so it keeps
        # its damage index but takes the chassis' transform slot.
        plan = _plan({}, {(0, 0): 38, (8, 9): 32, (14, 16): 34, (17, 19): 4, (9, 10): 41})
        self.assertEqual(plan.bound((8, 9)), (0, 9))
        self.assertEqual(plan.bound((14, 16)), (0, 16))
        self.assertEqual(plan.bound((17, 19)), (17, 19))  # bonnet: swings, as vanilla
        self.assertEqual(plan.bound((9, 10)), (9, 10))  # mirror: detaches, as vanilla
        # A door that is a Forza part (its border the Forza shut line) swings.
        plan.swinging = {(8, 9)}
        self.assertEqual(plan.bound((8, 9)), (8, 9))
        self.assertEqual(plan.bound((14, 16)), (0, 16))

    def test_doors_take_whole_forza_parts(self):
        # Two components (vertex keys never shared between them): a door skin
        # (triangles 0-3) the host vote mostly calls door D, and the quarter
        # panel (4-7) the vote calls chassis C except one triangle at the
        # border it called D. The door keeps exactly its component.
        D, C = (8, 9), (0, 0)
        keys = [
            [("a", 0), ("a", 1), ("a", 2)],
            [("a", 1), ("a", 2), ("a", 3)],
            [("a", 2), ("a", 3), ("a", 4)],
            [("a", 3), ("a", 4), ("a", 5)],
            [("b", 0), ("b", 1), ("b", 2)],
            [("b", 1), ("b", 2), ("b", 3)],
            [("b", 2), ("b", 3), ("b", 4)],
            [("b", 3), ("b", 4), ("b", 5)],
        ]
        comps = damage.part_components(keys)
        self.assertEqual(len(set(comps)), 2)
        labels = [D, D, D, C, C, C, C, D]
        areas = [1.0] * 8
        clean, total = damage.part_stats(labels, areas, comps, {D})
        self.assertEqual(clean[D], 4.0)
        self.assertEqual(total[D], 4.0)
        self.assertEqual(damage.part_relabel(labels, areas, comps, {D}, C), [D, D, D, D, C, C, C, C])

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
        # A host twin exhaust spanning both sides (vanilla 278) keeps both.
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


def _quad_section(name: str, quads: list) -> carbin.Section:
    """A section with one subsection per (technique, four corners) quad (LOD 1)."""
    verts, subs = [], []
    for technique, corners in quads:
        base = len(verts)
        verts += [carbin.Vertex(p, (0.5, 0.5), (0.0, 0.0), (0.0, 1.0, 0.0)) for p in corners]
        idx = [base, base + 1, base + 2, base, base + 2, base + 3]
        subs.append(carbin.SubSection(technique, 1, 4, (0.0, 1.0, 0.0, 1.0), idx))
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (1, 1, 1), verts, [], subs)


class ExposedVisible(unittest.TestCase):
    """split_exposed's zones say only that a trim lies under a cover or inward of a
    bumper skin. FER_355Challenge_95's black tail panel lies under the rear edge of
    its engine cover and drew the bay pictures between the tail lamps; a zone trim
    seen from outside the intact car keeps its trim."""

    def setUp(self):
        # A closed box under a bonnet (Forza space, +y up): side walls, a low rear wall,
        # a black panel closing the rest of the rear just inside the bonnet's edge, and
        # a frame inside the box.
        self.body = [
            _quad_section("hooda", [("body", [(-1, 1, -1), (1, 1, -1), (1, 1, 1), (-1, 1, 1)])]),
            _quad_section(
                "body",
                [
                    ("body", [(-1, 0, -1), (-1, 1, -1), (-1, 1, 1), (-1, 0, 1)]),
                    ("body", [(1, 0, -1), (1, 0, 1), (1, 1, 1), (1, 1, -1)]),
                    ("body", [(-1, 0, -1), (1, 0, -1), (1, 1, -1), (-1, 1, -1)]),
                    ("body", [(-1, 0, 1), (-1, 0.5, 1), (1, 0.5, 1), (1, 0, 1)]),
                    ("black", [(-1, 0.5, 0.98), (-1, 0.99, 0.98), (1, 0.99, 0.98), (1, 0.5, 0.98)]),
                    ("frame", [(-0.5, 0.3, -0.5), (-0.5, 0.3, 0.5), (0.5, 0.3, 0.5), (0.5, 0.3, -0.5)]),
                ],
            ),
        ]
        self.place = forza2dsf.Placement(dy=0.0, dz=0.0)

    @staticmethod
    def tags(body):
        return {sub.material: getattr(sub, "exposed", None) for s in body if s.name == "body" for sub in s.subsections}

    def test_seen_trim_keeps_its_trim(self):
        before = self.tags(damage.split_exposed(self.body, self.place, visible=False)[0])
        self.assertTrue(before["black"].startswith("bay"))
        self.assertTrue(before["frame"].startswith("bay"))
        body, report = damage.split_exposed(self.body, self.place)
        after = self.tags(body)
        self.assertIsNone(after["black"])  # the tail panel: seen from behind
        self.assertEqual(after["frame"], before["frame"])  # inside the box: still the bay's
        self.assertEqual(report["seen bay lod 1"], 2)


if __name__ == "__main__":
    unittest.main()
