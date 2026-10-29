"""Regression tests for src/dsfforza/convert/damage.py panel binding, on synthetic data (no game
or Forza files).

    uv run python tests/test_damage.py     # or every test: uv run python -m tests damage
"""

from __future__ import annotations

import struct
import unittest

from dsfforza.convert import damage, forza2dsf
from dsfforza.formats import carbin, upvh


def _plan(host_points: dict, records: dict) -> damage.DamagePlan:
    """A DamagePlan with only what panel lookup needs: host panels (pair ->
    role), a full-entry field with the given points per pair (crush (0.01, 0,
    -0.02) each), and a Forza car the size of the host."""
    plan = object.__new__(damage.DamagePlan)
    plan.panels = {pair: damage.Panel(pair, [], None, role) for pair, role in records.items()}
    plan.chassis = (0, 0)
    plan.swinging = set()
    plan.drawn = set()
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


def _record(
    type_: int, role: int, subtree: int, slot: int = -1, model: int = -1, flags: int = 0, hinge: int = -1, pops=(0, 0)
) -> bytes:
    rec = bytearray(upvh.RECORD_SIZE)
    rec[:8] = upvh.NONE_UID
    struct.pack_into("<HHH", rec, 0x08, type_, role, flags)
    struct.pack_into("<H", rec, 0x14, subtree)
    struct.pack_into("<HH", rec, 0x16, *pops)
    struct.pack_into("<hhh", rec, 0x1A, -1, hinge, -1)
    struct.pack_into("<h", rec, 0x2A, model)
    struct.pack_into("<h", rec, 0x34, slot)
    return bytes(rec)


TAIL = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)  # a table after the records, to check it stays


def _hierarchy() -> bytes:
    """Like host 182's front: root, chassis part (slot 0), the -x door (slot 6) with its
    mirror (slot 7, with a marker) and a door marker, then the bonnet (slot 8). +0x0C
    as host 182's: bit 0 on the records with part children."""
    records = [
        _record(4, 0, 7, flags=23),
        _record(1, 38, 6, 0, 0, 7),
        _record(1, 32, 4, 6, 7, 5),
        _record(1, 41, 2, 7, 8, 4),
        _record(3, 73, 1),
        _record(3, 67, 1),
        _record(1, 4, 1, 8, 9, 0),
    ]
    head = bytearray(upvh.HEADER_SIZE)
    head[:4] = b"AWHF"
    struct.pack_into("<6I", head, 4, 0, 0, 26, len(records), 1, 0)
    head[0x2D] = 1  # mirrors
    return bytes(head) + b"".join(records) + TAIL


def _flags(data: bytes) -> list:
    return [struct.unpack_from("<H", r.raw, 0x0C)[0] for r in upvh.parse(data).records]


def _tree(data: bytes) -> list:
    h = upvh.parse(data)
    par = damage._parents(h)
    return [(r.role, h.records[par[r.index]].role if par[r.index] is not None else None) for r in h.records]


class MirrorMount(unittest.TestCase):
    def test_move_subtree_keeps_the_slot_order(self):
        data = _hierarchy()
        out = damage.move_subtree(data, 3, 1)  # the mirror under the chassis
        self.assertEqual(_tree(out), [(0, None), (38, 0), (32, 38), (67, 32), (41, 38), (73, 41), (4, 38)])
        h = upvh.parse(out)
        self.assertEqual([r.subtree for r in h.records], [7, 6, 2, 1, 2, 1, 1])
        self.assertEqual([r.slot for r in h.records if r.type == 1], [0, 6, 7, 8])
        self.assertTrue(out.endswith(TAIL) and len(out) == len(data))
        self.assertEqual(_flags(out), [23, 7, 4, 0, 4, 0, 0])  # the door has no part child left
        # Under the bonnet (slot 8, after the mirror's 7) the slots can't stay in record order.
        self.assertIsNone(damage.move_subtree(data, 3, 6))
        with self.assertRaises(ValueError):
            damage.move_subtree(data, 2, 3)  # into its own subtree

    def test_mirror_follows_what_it_is_mounted_on(self):
        records = {(0, 0): 38, (6, 7): 32, (7, 8): 41, (8, 9): 4}
        plan = _plan({}, records)
        door = [(-0.9, y / 10, z / 10) for y in range(-3, 4) for z in range(-8, 5)]
        sail = [(-0.75, 0.3, 0.12), (-0.75, 0.35, 0.15)]  # the A-pillar: the chassis
        mirror = [(-0.76, 0.3, 0.12), (-0.91, 0.31, 0.1), (-1.0, 0.3, 0.08)]
        plan.boxes = {(0, 0): sail, (6, 7): door, (7, 8): mirror, (8, 9): [(0.0, 0.2, 1.2)]}
        self.assertEqual(plan._mirror_mount((7, 8)), (0, 0))
        out, report = plan._remount_mirrors(_hierarchy())
        self.assertEqual(_tree(out)[3:5], [(67, 32), (41, 38)])
        self.assertEqual(
            report["7,8 mirror -x"],
            {"mount": "0,0 chassis", "from": "door front -x", "to": "chassis"},
        )
        # On a door that swings (a Forza part) it stays under the door, as vanilla.
        plan.boxes[(0, 0)] = [(0.0, 0.0, 0.0)]
        plan.swinging = {(6, 7)}
        self.assertEqual(plan._mirror_mount((7, 8)), (6, 7))
        self.assertEqual(plan._remount_mirrors(_hierarchy()), (_hierarchy(), {}))
        # On a door held shut (drawn through the chassis' slot) it goes under the chassis.
        plan.swinging = set()
        self.assertEqual(_tree(plan._remount_mirrors(_hierarchy())[0])[4], (41, 38))
        # On the bonnet, which can't take it in slot order: under the chassis, not the door.
        plan.boxes[(8, 9)] = [(-0.92, 0.31, 0.1)]
        self.assertEqual(plan._mirror_mount((7, 8)), (8, 9))
        out, report = plan._remount_mirrors(_hierarchy())
        self.assertEqual(_tree(out)[4], (41, 38))
        self.assertEqual(report["7,8 mirror -x"]["to"], "chassis")

    def test_mirrors_leave_a_shared_parent_in_turn(self):
        # As on the DBR9's host: both mirrors under the bonnet (slot 5); each is on the body.
        records = [
            _record(4, 0, 6, flags=23),
            _record(1, 38, 5, 0, 0, 7),
            _record(1, 4, 3, 5, 5, 1),
            _record(1, 41, 1, 6, 6, 4),
            _record(1, 40, 1, 7, 7, 4),
            _record(1, 36, 1, 8, 8, 0),
        ]
        head = bytearray(upvh.HEADER_SIZE)
        head[:4] = b"AWHF"
        struct.pack_into("<6I", head, 4, 0, 0, 26, len(records), 1, 0)
        head[0x2D] = 1  # mirrors
        data = bytes(head) + b"".join(records) + TAIL
        self.assertIsNone(damage.move_subtree(data, 3, 1))  # -x first: +x is still in the way
        plan = _plan({}, {(0, 0): 38, (5, 5): 4, (6, 6): 41, (7, 7): 40, (8, 8): 36})
        plan.boxes = {
            (0, 0): [(-0.8, 0.3, 0.1), (0.8, 0.3, 0.1)],
            (5, 5): [(0.0, 0.2, 1.2)],
            (6, 6): [(-0.81, 0.3, 0.1)],
            (7, 7): [(0.81, 0.3, 0.1)],
            (8, 8): [(0.0, 0.0, -2.0)],
        }
        out, report = plan._remount_mirrors(data)
        self.assertEqual(_tree(out), [(0, None), (38, 0), (4, 38), (41, 38), (40, 38), (36, 38)])
        self.assertEqual(_flags(out), [23, 7, 0, 4, 4, 0])
        self.assertEqual(sorted(report), ["6,6 mirror -x", "7,7 mirror +x"])

    @staticmethod
    def _door_with_glass(glass_slot: int) -> bytes:
        """As on the DB9's host 133: the -x door holds its mirror (with a marker), a door
        marker and the door glass (no slot of its own there); then the +x door."""
        door, mirror = (8, 9) if glass_slot < 0 else (7, 8)
        records = [
            _record(4, 0, 8, flags=23),
            _record(1, 38, 7, 0, 0, 7),
            _record(1, 32, 5, door, 10, 5),
            _record(1, 41, 2, mirror, 11, 4),
            _record(3, 73, 1),
            _record(3, 67, 1),
            _record(1, 8, 1, glass_slot, 12, 0),
            _record(1, 31, 1, 10, 13, 0),
        ]
        head = bytearray(upvh.HEADER_SIZE)
        head[:4] = b"AWHF"
        struct.pack_into("<6I", head, 4, 0, 0, 26, len(records), 1, 0)
        head[0x2D] = 1  # mirrors
        return bytes(head) + b"".join(records) + TAIL

    def test_door_glass_after_the_mirror_leaves_first(self):
        # Door slot 8 / index 10, mirror 9 / 11, glass index 12, +x door 10 / 13: alone, the
        # mirror has no place under the chassis that keeps both orders (issue #193).
        data = self._door_with_glass(-1)
        self.assertIsNone(damage.move_subtree(data, 3, 1))
        plan = _plan({}, {(0, 0): 38, (8, 10): 32, (9, 11): 41, (0, 12): 8, (10, 13): 31})
        plan.boxes = {(0, 0): [(-0.75, 0.3, 0.12)], (8, 10): [(-0.9, 0.0, 0.0)], (9, 11): [(-0.76, 0.3, 0.12)]}
        out, report = plan._remount_mirrors(data)
        # The glass, then the mirror between the door and it; the door marker stays.
        self.assertEqual(_tree(out), [(0, None), (38, 0), (32, 38), (67, 32), (41, 38), (73, 41), (8, 38), (31, 38)])
        h = upvh.parse(out)
        self.assertEqual(
            [(r.slot, r.model) for r in h.records if r.type == 1][1:], [(8, 10), (9, 11), (-1, 12), (10, 13)]
        )
        self.assertEqual(_flags(out)[2], 4)  # the door has no part child left
        fields = damage._tree_fields([r.raw for r in h.records])
        self.assertEqual([damage._read_tree_fields(r.raw) for r in h.records], fields)
        self.assertEqual(report["9,11 mirror -x"]["with"], ["0,12 glass"])

    def test_door_glass_drawn_through_its_slot_stays(self):
        # Glass with a slot of its own (9) that draws a panel stays on the door, and so
        # does the mirror; with nothing drawn through it, both leave as above.
        data = self._door_with_glass(9)
        plan = _plan({}, {(0, 0): 38, (7, 10): 32, (8, 11): 41, (9, 12): 8, (10, 13): 31})
        plan.boxes = {(0, 0): [(-0.75, 0.3, 0.12)], (8, 11): [(-0.76, 0.3, 0.12)]}
        plan.drawn = {(9, 12)}
        out, report = plan._remount_mirrors(data)
        self.assertEqual(out, data)
        self.assertEqual(report["8,11 mirror -x"], {"mount": "0,0 chassis", "kept": "host parent"})
        plan.drawn = set()
        out, report = plan._remount_mirrors(data)
        self.assertEqual(_tree(out)[4:7], [(41, 38), (73, 41), (8, 38)])
        self.assertEqual(report["8,11 mirror -x"]["with"], ["9,12 glass"])

    def test_moved_records_get_vanilla_pop_counts(self):
        # Host 182's +x door: door (hinge), its marker, then its mirror and the mirror's
        # marker, which ends the door's subtree and so pops the door's hinge (+0x16/+0x18
        # = 1, 1). Moved under the chassis, the door's marker ends it instead (as on 197).
        records = [
            _record(4, 0, 7, flags=23),
            _record(1, 38, 6, 0, 0, 7),
            _record(1, 31, 4, 8, 9, 5, hinge=8, pops=(0, 1)),
            _record(3, 65, 1),
            _record(1, 40, 2, 9, 10, 4, pops=(0, 1)),
            _record(3, 74, 1, pops=(1, 1)),
            _record(3, 86, 1),
        ]
        head = bytearray(upvh.HEADER_SIZE)
        head[:4] = b"AWHF"
        struct.pack_into("<6I", head, 4, 0, 0, 26, len(records), 1, 0)
        head[0x2D] = 1  # mirrors
        data = bytes(head) + b"".join(records) + TAIL
        out = damage.move_subtree(data, 4, 1)
        h = upvh.parse(out)
        self.assertEqual(_tree(out), [(0, None), (38, 0), (31, 38), (65, 31), (40, 38), (74, 40), (86, 38)])
        self.assertEqual(
            [struct.unpack_from("<HH", r.raw, 0x16) for r in h.records][2:6], [(0, 1), (1, 1), (0, 0), (0, 0)]
        )
        self.assertEqual(_flags(out)[2], 4)
        fields = damage._tree_fields([r.raw for r in h.records])
        self.assertEqual([damage._read_tree_fields(r.raw) for r in h.records], fields)


if __name__ == "__main__":
    unittest.main()
