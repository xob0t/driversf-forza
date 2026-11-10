"""Live pools retain all candidates and never emit a preload-every-model AI file."""

import struct
import unittest
from unittest import mock

from tests import test_traffic as tt

from dsfforza.build import car_gui, car_manager, traffic
from dsfforza.build import runtime_traffic as live
from dsfforza.game import traffic_sets


def records(blob: bytes):
    magic, count, pairs, packages = live.HEADER.unpack_from(blob)
    assert magic == live.MAGIC
    rows = [live.ROW.unpack_from(blob, live.HEADER.size + packages * 4 + i * live.ROW.size) for i in range(count)]
    base = live.HEADER.size + packages * 4 + count * live.ROW.size
    choices = [live.PAIR.unpack_from(blob, base + i * live.PAIR.size) for i in range(pairs)]
    return rows, choices


class RuntimeTrafficTest(unittest.TestCase):
    setup_manager = tt.TrafficControlTest.setup_manager
    package = tt.TrafficControlTest.package
    manager = tt.TrafficControlTest.manager
    setUp = tt.TrafficControlTest.setUp
    control = tt.TrafficControlTest.control

    def supports_live(self):
        with (self.game / car_manager.PLUGIN).open("ab") as stream:
            stream.write(live.MARKER)

    def test_full_721_pool_is_in_weight_rows_and_preview(self):
        m = self.manager()
        tc = self.control(m)
        tc.facts = lambda: (60, tt.POTS)
        ids = [i for i in range(1, 1000) if i not in {101, 102, 103, 104, 105}][:721]
        cars = {f"car.{mid}": {"id": mid, "enabled": True, "traffic": True} for mid in ids}
        with mock.patch.object(tc, "cars", return_value=cars):
            tc.set_ambient(None, pool="all")
            blob, reports = live.build(tc)
            _before, after, preview = live.preview(tc, tt.CH7, tc.t["ambient"])
        rows, pairs = records(blob)
        ch7 = [r for r in rows if r[0] == traffic_sets.name_hash(tt.CH7)]
        self.assertEqual(len(ch7), 2)
        for row in ch7:
            selected = pairs[row[5] : row[5] + row[6]]
            self.assertEqual({mid for mid, _weight, flags in selected if flags == 1}, set(ids))
            self.assertGreater(row[6], 721)
            self.assertLessEqual(row[7], live.MISSION_NODES - tt.POTS[tt.CH7] - 1 - live.MAX_IV_MODELS)
            self.assertLessEqual(row[7], 60 - 2 - tt.POTS[tt.CH7] - 1 - live.MAX_IV_MODELS)
        self.assertEqual(preview.candidates, 721)
        self.assertTrue(set(ids) <= set(after))
        self.assertEqual(next(r for r in reports if r.name == tt.CH7).candidates, 721)

    def test_shared_native_ranges_can_use_different_chapter_pools(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        b = m.install(self.package("CAR_B_10"))
        tc = self.control(m)
        tc.set_ambient(None, pool=[a])
        tc.set_ambient(tt.CHASE, pool=[b])
        blob, _reports = live.build(tc)
        rows, pairs = records(blob)
        selected = {}
        for name in (tt.CH1, tt.CHASE):
            row = next(r for r in rows if r[0] == traffic_sets.name_hash(name))
            selected[name] = (row[2:5], {mid for mid, _w, flag in pairs[row[5] : row[5] + row[6]] if flag == 1})
        self.assertEqual(selected[tt.CH1][0], selected[tt.CHASE][0])
        self.assertEqual(selected[tt.CH1][1], {m.state["cars"][a]["id"]})
        self.assertEqual(selected[tt.CHASE][1], {m.state["cars"][b]["id"]})

    def test_write_removes_static_batch_and_off_removes_live_file(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        tc = self.control(m)
        original = tc.game_ai().read_bytes()
        tc.set_ambient(None, ports="fill")
        tc.write()  # a legacy static preset before upgrading the plugin
        self.assertTrue((self.game / traffic.AI_COPY).exists())
        self.supports_live()
        tc.set_ambient(None, pool=[a])
        tc.write()
        self.assertTrue((self.game / live.POOL_FILE).exists())
        self.assertFalse((self.game / traffic.AI_COPY).exists())
        self.assertFalse((self.game / traffic.AI_SLOTS).exists())
        redirect = self.game / car_gui.GENERATED / car_gui.REDIRECT
        self.assertFalse(redirect.exists())
        self.assertEqual(tc.game_ai().read_bytes(), original)
        tc.ambient_off()
        tc.write()
        self.assertFalse((self.game / live.POOL_FILE).exists())

    def test_old_plugin_refuses_live_pool_instead_of_saving_a_batch(self):
        tc = self.control(self.manager())
        tc.set_ambient(None, pool="all")
        with self.assertRaisesRegex(SystemExit, "updated dsfcars"):
            tc.write()
        self.assertFalse((self.game / traffic.AI_COPY).exists())
        self.assertTrue(any("not active" in line for line in tc.status()))

    def test_explicit_share_and_disabled_candidates_use_existing_weight_math(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        b = m.install(self.package("CAR_B_10"))
        tc = self.control(m)
        tc.set_ambient(None, pool="all", share=0.25)
        tc.set_traffic([b], on=False)
        _native, shares, report = live.preview(tc, tt.CH7, tc.t["ambient"])
        self.assertEqual(report.candidates, 1)
        self.assertAlmostEqual(shares[m.state["cars"][a]["id"]], 0.25)
        self.assertNotIn(m.state["cars"][b]["id"], shares)

    def test_native_fingerprint_uses_all_zero_and_nonzero_columns(self):
        self.assertEqual(live.fingerprint(b"hello"), 0x4F9F2CAB)
        a = tt.ai()
        before = live.native_fingerprint(a, 1)
        start, count, total = a.ranges[1]
        native = struct.pack("<IIf", start, count, total)
        native += b"".join(struct.pack("<If", mid, w) for mid, w in zip(a.columns, a.weights[1]))
        self.assertEqual(before, live.fingerprint(native))
        a.weights[1][0] = 1.0
        self.assertNotEqual(before, live.native_fingerprint(a, 1))
