"""Chapter candidate pools are independent of native resident-model limits."""

import tkinter as tk
import unittest

from tests import test_traffic as tt

from dsfforza.build import car_manager, traffic


class PoolPlanTest(unittest.TestCase):
    def test_each_chapter_draws_only_from_its_pool(self):
        pools = {tt.CH1: ["a", "c"], tt.CH7: ["b"]}
        ambient = {"sets": {name: {"pool": keys, "ports": "fill"} for name, keys in pools.items()}}
        a = tt.ai()
        reports = traffic.plan(a, ambient, [1, 2, 3], {"a": 1, "b": 2, "c": 3}, slots=60, pots=tt.POTS)
        selected = {r.name: r.ports for r in reports}
        self.assertEqual(set(selected[tt.CH1]), {1, 3})
        self.assertEqual(selected[tt.CH7], [2])
        self.assertEqual(a.check(), [])

    def test_large_pool_keeps_native_budget_and_can_select_beyond_first_entries(self):
        ports = [i for i in range(1000) if i not in {101, 102, 103, 104, 105}][:721]
        keys = {f"car.{mid}": mid for mid in ports}
        pool = list(keys)
        ambient = {"default": {"pool": pool, "ports": "fill"}}
        a = tt.ai()
        reports = traffic.plan(a, ambient, ports, keys, slots=60, pots=tt.POTS)
        self.assertEqual(len(ambient["default"]["pool"]), 721)
        for report in reports:
            self.assertLessEqual(report.models, report.budget)
            self.assertLessEqual(report.models, 40)
            self.assertTrue(set(report.ports) <= set(ports))
        ch7 = next(r for r in reports if r.name == tt.CH7)
        self.assertTrue(set(ch7.ports) - set(ports[:40]))
        self.assertEqual(a.check(), [])

    def test_pool_filters_legacy_explicit_ports_and_traffic_exclusions(self):
        a = tt.ai()
        reports = traffic.plan(
            a,
            {"default": {"pool": ["a", "b", "missing"], "ports": ["a", "a", "b", "c"]}},
            [1, 3],
            {"a": 1, "b": 2, "c": 3},
            slots=60,
            pots=tt.POTS,
        )
        self.assertTrue(all(r.ports == [1] for r in reports))

    def test_pool_inheritance_override_and_validation(self):
        ambient = {"default": {"pool": ["a"]}, "sets": {tt.CH7: {"pool": []}}}
        self.assertEqual(traffic.set_settings(ambient, tt.CH1)["pool"], ["a"])
        self.assertEqual(traffic.set_settings(ambient, tt.CH7)["pool"], [])
        for bad in (None, 721, "fill", [1]):
            with self.subTest(pool=bad), self.assertRaisesRegex(ValueError, "pool"):
                traffic.set_settings({"default": {"pool": bad}}, tt.CH1)


class PoolSettingsTest(unittest.TestCase):
    setup_manager = tt.TrafficControlTest.setup_manager
    package = tt.TrafficControlTest.package
    manager = tt.TrafficControlTest.manager
    setUp = tt.TrafficControlTest.setUp
    control = tt.TrafficControlTest.control

    def test_all_includes_future_cars_custom_pool_survives_reload(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        tc = self.control(m)
        tc.set_ambient(None, pool="all")
        b = m.install(self.package("CAR_B_10"))
        self.assertEqual(tc.pool_keys(), [a, b])
        tc.set_ambient(tt.CH7, pool=[b], ports="fill")
        m.save_state()
        reloaded = self.control(self.manager())
        settings = traffic.set_settings(reloaded.t["ambient"], tt.CH7)
        self.assertEqual(reloaded.pool_keys(settings["pool"]), [b])
        reloaded.set_traffic([b], on=False)
        self.assertEqual(reloaded.pool_keys(settings["pool"]), [])
        self.assertEqual(settings["pool"], [b])  # membership survives temporarily disabling traffic

    def test_cli_pool_selectors_all_none_and_reset(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        tc = self.control(m)
        for arg, expected in (("game:FH1", [a]), ("all", "all"), ("none", [])):
            args = car_manager.build_parser().parse_args(["traffic", "ambient", "--pool", arg])
            self.assertEqual(car_manager.ambient_fields(tc, args)["pool"], expected)
        tc.set_ambient(None, pool=[a])
        tc.set_ambient(tt.CH7, pool=[])
        tc.set_ambient(tt.CH7, pool=None)
        self.assertEqual(traffic.set_settings(tc.t["ambient"], tt.CH7)["pool"], [a])


class PoolDialogTest(unittest.TestCase):
    def test_filtering_preserves_edits_and_cancel_does_not_apply(self):
        from dsfforza.build.traffic_pool_gui import PoolDialog

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"no display for Tk: {error}")
        root.withdraw()
        cars = {
            key: {"name": key, "make": "TEST", "game": "FH1", "dlc": False, "enabled": True, "traffic": True}
            for key in ("Alpha", "Beta", "Gamma")
        }
        try:
            dialog = PoolDialog(root, cars, "all")
            dialog.window.withdraw()
            dialog.search.set("Alpha")
            dialog.tree.selection_set("Alpha")
            dialog.edit(include=False)
            dialog.search.set("Beta")
            self.assertEqual(dialog.keys, {"Beta", "Gamma"})
            dialog.tree.selection_set("Beta")
            dialog.edit(include=False)
            dialog.search.set("")
            self.assertEqual(dialog.keys, {"Gamma"})
            self.assertIsNone(dialog.result)
            dialog.accept()
            self.assertEqual(dialog.result, ["Gamma"])
            all_dialog = PoolDialog(root, cars, [])
            all_dialog.window.withdraw()
            all_dialog.replace(None)
            all_dialog.accept()
            self.assertEqual(all_dialog.result, "all")
            cancelled = PoolDialog(root, cars, "all")
            cancelled.window.withdraw()
            cancelled.replace(set())
            cancelled.window.destroy()
            self.assertIsNone(cancelled.result)
        finally:
            root.destroy()
