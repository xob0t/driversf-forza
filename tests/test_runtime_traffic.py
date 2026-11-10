"""Live pools retain all candidates and never emit a preload-every-model AI file."""

import json
import struct
import unittest
from copy import deepcopy
from types import SimpleNamespace
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

    def check_car_changes(self, m, a):
        b = m.install(self.package("CAR_B_10"))
        m.set_enabled(a, enabled=False)
        m.set_enabled(a, enabled=True)
        m.remove(b)
        saved = json.loads((m.cars_dir / car_manager.STATE).read_text())
        self.assertEqual(set(saved["cars"]), {a})
        self.assertTrue(saved["cars"][a]["enabled"])
        self.assertFalse((m.cars_dir / (b + ".removed")).exists())
        self.assertTrue((self.game / traffic.TRAFFIC_LUA).exists())
        self.assertTrue(any("live traffic update failed" in line for line in self.lines))

    def test_saved_pools_without_plugin_do_not_block_car_management(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        self.control(m).set_ambient(None, pool="all")  # settings retained from a newer plugin
        m.save_state()
        self.check_car_changes(m, a)
        self.assertFalse((self.game / traffic.AI_COPY).exists())

    def test_missing_ai_does_not_block_car_management(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        self.supports_live()
        tc = self.control(m)
        tc.set_ambient(None, pool="all")
        tc.game_ai().unlink()
        self.check_car_changes(m, a)

    def test_live_build_failures_do_not_block_car_management(self):
        for failure in (SystemExit("mission scripts missing"), ValueError("invalid AI"), OSError("pool locked")):
            with self.subTest(failure=type(failure).__name__):
                self.setUp()
                m = self.manager()
                a = m.install(self.package("CAR_A_10"))
                self.supports_live()
                self.control(m).set_ambient(None, pool="all")
                with mock.patch.object(live, "build", side_effect=failure):
                    self.check_car_changes(m, a)

    def test_traffic_failure_during_install_rollback_preserves_original_error(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        self.supports_live()
        self.control(m).set_ambient(None, pool="all")
        m.save_state()
        old_bytes = m.sp_path(a).read_bytes()
        upgrade = self.package("CAR_A_10", version=2)
        with (
            mock.patch.object(m, "generate_gui", side_effect=[OSError("GUI write failed"), None]),
            mock.patch.object(traffic.TrafficControl, "facts", side_effect=SystemExit("scripts missing")),
            self.assertRaisesRegex(OSError, "GUI write failed"),
        ):
            m.install(upgrade)
        self.assertEqual(m.sp_path(a).read_bytes(), old_bytes)
        self.assertEqual(m.state["cars"][a]["version"], 1)
        self.assertFalse((m.cars_dir / (a + ".old")).exists())
        self.assertFalse((m.cars_dir / (a + ".new")).exists())

    def test_optional_iv_write_failure_is_logged(self):
        m = self.manager()
        m.install(self.package("CAR_A_10"))
        self.supports_live()
        self.control(m).set_ambient(None, pool="all")
        with (
            mock.patch.object(live, "write", side_effect=OSError("pool locked")),
            mock.patch.object(traffic.TrafficControl, "write_lua", side_effect=OSError("IV locked")),
        ):
            m.save()
        self.assertTrue(any("pool locked" in line for line in self.lines))
        self.assertTrue(any("IV locked" in line for line in self.lines))

    def test_explicit_cli_failures_do_not_save_or_keep_failed_settings(self):
        for failure in ("plugin", "AI", "scripts"):
            with self.subTest(failure=failure):
                self.setUp()
                m = self.manager()
                m.install(self.package("CAR_A_10"))
                if failure != "plugin":
                    self.supports_live()
                if failure == "AI":
                    (self.game / traffic.AI_FILE).unlink()
                before = deepcopy(m.state["traffic"])
                path = m.cars_dir / car_manager.STATE
                saved = path.read_bytes()
                args = car_manager.build_parser().parse_args(["traffic", "ambient", "--pool", "all"])
                with (
                    mock.patch.object(
                        traffic.TrafficControl,
                        "facts",
                        return_value=(60, tt.POTS),
                        side_effect=SystemExit("scripts missing") if failure == "scripts" else None,
                    ),
                    self.assertRaises((SystemExit, OSError)),
                ):
                    car_manager.run_traffic(m, args)
                self.assertEqual(path.read_bytes(), saved)
                self.assertEqual(m.state["traffic"], before)
                self.assertFalse((self.game / live.POOL_FILE).exists())

    def test_gui_apply_rejects_old_plugin_before_mutation(self):
        from dsfforza.build.car_manager_gui import App

        tc = self.control(self.manager())
        before = deepcopy(tc.t)
        app = SimpleNamespace(tc=tc, ready=lambda: True, gather=mock.Mock(), run=mock.Mock())
        with mock.patch("dsfforza.build.car_manager_gui.messagebox.showerror") as shown:
            App.apply_set(app)
        shown.assert_called_once()
        app.gather.assert_not_called()
        app.run.assert_not_called()
        self.assertEqual(tc.t, before)

    def test_gui_apply_restores_settings_when_live_build_fails(self):
        from dsfforza.build.car_manager_gui import App

        m = self.manager()
        m.install(self.package("CAR_A_10"))
        self.supports_live()
        tc = self.control(m)
        before = deepcopy(tc.t)
        saved = (m.cars_dir / car_manager.STATE).read_bytes()
        tc.game_ai().unlink()
        app = SimpleNamespace(
            m=m,
            tc=tc,
            ready=lambda: True,
            gather=lambda: {"pool": "all", "ports": "fill"},
            current_set=lambda: tt.CH7,
            set_name=SimpleNamespace(get=lambda: tt.CH7),
            run=lambda _what, work: work(),
        )
        app.save_traffic = lambda change=None: App.save_traffic(app, change)
        with self.assertRaises(OSError):
            App.apply_set(app)
        self.assertEqual(tc.t, before)
        self.assertEqual((m.cars_dir / car_manager.STATE).read_bytes(), saved)

    def gui_app(self, m, tc, key, work):
        """Capture worker callbacks without creating a Tk window or thread."""
        from dsfforza.build.car_manager_gui import App

        app = SimpleNamespace(
            m=m,
            tc=tc,
            log=self.lines.append,
            ready=lambda: True,
            selected_keys=lambda: [key],
            current_set=lambda: tt.CH7,
            set_name=SimpleNamespace(get=lambda: tt.CH7),
            gather=lambda: {"pool": "all", "ports": "fill"},
            iv_ports=SimpleNamespace(get=lambda: False),
            per_region=SimpleNamespace(get=lambda: 5),
            iv_vanilla=SimpleNamespace(get=lambda: False),
            regions=SimpleNamespace(get=lambda: "2"),
            run=lambda _what, callback: work.append(callback),
        )
        app.save_traffic = lambda change=None: App.save_traffic(app, change)
        return app

    def test_gui_traffic_edits_restore_settings_on_strict_write_failures(self):
        from dsfforza.build.car_manager_gui import App

        m = self.manager()
        key = m.install(self.package("CAR_A_10"))
        self.supports_live()
        tc = self.control(m)
        tc.set_ambient(None, pool="all", ports="fill")
        tc.set_ambient(tt.CH7, pool=[key])
        tc.pin([key], [3])
        m.save_state()
        before = deepcopy(tc.t)
        path = m.cars_dir / car_manager.STATE
        saved = path.read_bytes()
        handlers = (
            ("apply_set", {}),
            ("reset_set", {}),
            ("shuffle", {}),
            ("ambient_off", {}),
            ("traffic_cars", {"on": False}),
            ("apply_iv", {}),
            ("pin", {"unpin": False}),
            ("pin", {"unpin": True}),
        )
        for handler, args in handlers:
            for error in (SystemExit, ValueError, OSError):
                with self.subTest(handler=handler, args=args, error=error.__name__):
                    work = []
                    app = self.gui_app(m, tc, key, work)

                    def fail_write(failure=error):
                        self.assertNotEqual(tc.t, before)  # the edit reached the worker
                        raise failure("write rejected")

                    with (
                        mock.patch("dsfforza.build.car_manager_gui.messagebox.askyesno", return_value=True),
                        mock.patch.object(tc, "write", side_effect=fail_write),
                    ):
                        getattr(App, handler)(app, **args)
                        self.assertEqual(tc.t, before)  # never mutate before the worker runs
                        self.assertEqual(len(work), 1)
                        with self.assertRaisesRegex(error, "write rejected"):
                            work[0]()
                    self.assertEqual(tc.t, before)
                    self.assertIs(m.state["traffic"], tc.t)
                    self.assertEqual(path.read_bytes(), saved)

    def test_rejected_gui_iv_edit_cannot_leak_into_later_car_save(self):
        from dsfforza.build.car_manager_gui import App

        m = self.manager()
        key = m.install(self.package("CAR_A_10"))
        tc = self.control(m)
        tc.set_ambient(None, pool="all")  # saved pools with an older installed plugin
        m.save_state()
        before = deepcopy(tc.t)
        path = m.cars_dir / car_manager.STATE
        saved = path.read_bytes()
        work = []
        app = self.gui_app(m, tc, key, work)
        App.apply_iv(app)
        with self.assertRaisesRegex(SystemExit, "updated dsfcars"):
            work[0]()
        self.assertEqual(tc.t, before)
        self.assertEqual(path.read_bytes(), saved)
        m.set_enabled(key, enabled=False)
        self.assertEqual(json.loads(path.read_text())["traffic"], before)

    def test_gui_iv_success_persists_the_requested_edit(self):
        from dsfforza.build.car_manager_gui import App

        m = self.manager()
        key = m.install(self.package("CAR_A_10"))
        tc = self.control(m)
        work = []
        app = self.gui_app(m, tc, key, work)
        App.apply_iv(app)
        with mock.patch.object(tc, "write", return_value=[]):
            work[0]()
        saved = json.loads((m.cars_dir / car_manager.STATE).read_text())
        for name, value in {"ports": False, "per_region": 5, "vanilla": False}.items():
            self.assertEqual(tc.t["iv"][name], value)
            self.assertEqual(saved["traffic"]["iv"][name], value)

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
