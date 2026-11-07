"""Traffic: the AI tables (game/traffic_ai.py), the settings' plan (build/traffic.py), the car
manager's files, and register.lua's interesting-vehicle settings. A synthetic
san_franciscoAI.fchunk with the game's container and sections (research/traffic.md 1b); no
game data."""

import json
import struct
import unittest

from tests import test_car_manager as tcm
from tests import test_register_asi as reg

from dsfforza.build import car_gui, traffic
from dsfforza.game import traffic_ai as ta
from dsfforza.game import traffic_sets

CH7, CH1, CHASE = "Chapter 7", "Chapter 1", "Exposition Chase"
COLUMNS = [5, 101, 102, 103, 104, 105]
# ranges: 0 spawns nothing; 1 and 2 are Chapter 7's (2 leaves 102 out); 3 is Chapter 1's and
# Exposition Chase's
RANGES = [
    {},
    {101: 4.0, 102: 2.0, 103: 2.0},
    {101: 1.0, 103: 1.0},
    {104: 1.0, 105: 3.0},
]
# per road: (Chapter 7's range, Chapter 1's, Exposition Chase's)
ROADS = [(1, 3, 3), (2, 3, 0), (0, 0, 3), (1, 3, 3)]
LISTS = {CH7: [101, 102, 103], CH1: [104, 105], CHASE: [104, 105]}
SET_ORDER = [CH7, CH1, CHASE]


def make_ai() -> bytes:
    """A san_franciscoAI.fchunk: file CHNK -> AIRT -> NONR -> AIEX, AIRF, with 59 sets (three used)."""
    ids, progs = [], []
    for i in range(ta.SETS):
        name = SET_ORDER[i] if i < len(SET_ORDER) else None
        lst = LISTS.get(name, [])
        h = traffic_sets.name_hash(name) if name else 0x1000 + i
        starts, counts = [len(ids), 0, 0, 0, 0], [len(lst), 0, 0, 0, 0]
        ids += lst
        progs.append(struct.pack("<I5H5HI", h, *starts, *counts, i))
    roads = bytearray()
    for road in ROADS:
        for i in range(ta.SETS):
            rec = bytearray(ta.ROAD_RECORD)
            rec[0x0D] = road[i] if i < len(SET_ORDER) else 0
            roads += rec
    ranges, pairs = b"", b""
    for r, weights in enumerate(RANGES):
        ranges += struct.pack("<IIf", r * len(COLUMNS), len(COLUMNS), sum(weights.values()))
        pairs += b"".join(struct.pack("<If", m, weights.get(m, 0.0)) for m in COLUMNS)
    sections = {
        ta.PROGRESSIONS: (len(progs), b"".join(progs)),
        ta.SPOOL_IDS: (len(ids), struct.pack(f"<{len(ids)}H", *ids)),
        ta.ROADS: (len(ROADS) * ta.SETS, bytes(roads)),
        ta.RANGES: (len(RANGES), ranges),
        ta.PAIRS: (len(RANGES) * len(COLUMNS), pairs),
    }
    aiex = bytearray(0x180)
    for slot, (count, data) in sections.items():
        aiex += b"\0" * (-len(aiex) % 16)
        struct.pack_into("<II", aiex, slot, count, len(aiex))
        aiex += data
    aiex += b"\0" * (-len(aiex) % 0x80)
    airf = b"route" * 8
    entry = struct.Struct("<4sIBBBBI")
    nonr = bytearray(b"CHNK" + struct.pack("<III", 0, 2, 3))
    nonr += entry.pack(b"AIEX", 0x80, 22, 0, 7, 0, len(aiex)) + entry.pack(
        b"AIRF", 0x80 + len(aiex), 1, 0, 4, 0, len(airf)
    )
    nonr += b">" * (0x80 - len(nonr)) + aiex + airf
    struct.pack_into("<I", nonr, 4, len(nonr))
    airt = bytearray(b"CHNK" + struct.pack("<III", 0, 1, 3) + entry.pack(b"NONR", 0x800, 0, 0, 11, 0, len(nonr)))
    airt += b">" * (0x800 - len(airt)) + nonr
    struct.pack_into("<I", airt, 4, len(airt))
    out = bytearray(b"CHNK" + struct.pack("<III", 0, 1, 3) + entry.pack(b"AIRT", 0x800, 22, 0, 11, 0, len(airt)))
    out += b">" * (0x800 - len(out)) + airt
    struct.pack_into("<I", out, 4, len(out))
    return bytes(out)


def ai() -> ta.AiTraffic:
    return ta.AiTraffic(make_ai(), ta.known_names())


POTS = {CH7: 13, CH1: 16, CHASE: 14}


class AiTablesTest(unittest.TestCase):
    def test_read_and_round_trip(self):
        a = ai()
        self.assertEqual(a.by_name(CH7).lists, {0: [101, 102, 103]})
        self.assertEqual(a.road_ranges(a.by_name(CH7), 0), {0, 1, 2})
        self.assertEqual(sorted(s.name for s, _ in a.users({3})), [CH1, CHASE])
        self.assertEqual(a.check(), [])
        b = ta.AiTraffic(a.to_bytes(), ta.known_names())
        self.assertEqual([s.lists for s in b.sets], [s.lists for s in a.sets])
        self.assertEqual(b.weights, a.weights)

    def test_share_counts_roads(self):
        # Chapter 7: roads 0 and 3 on range 1 (101 1/2, 102 1/4, 103 1/4), road 1 on range 2
        # (101 1/2, 103 1/2), road 2 spawns nothing
        share = ai().share(ai().by_name(CH7))
        self.assertAlmostEqual(share[101], 0.5)
        self.assertAlmostEqual(share[102], 2 * 0.25 / 3)
        self.assertAlmostEqual(share[103], (0.25 * 2 + 0.5) / 3)

    def test_grown_file_keeps_aiex_bytes_and_shifts_what_follows(self):
        a = ai()
        a.set_list(a.by_name(CH7), 0, [101, 102, 103, 7])
        a.set_range(1, {101: 4.0, 102: 2.0, 103: 2.0, 7: 2.0})
        raw = a.to_bytes()
        old = make_ai()
        delta = len(raw) - len(old)
        self.assertGreater(delta, 0)
        self.assertEqual(delta % ta.GROW_ALIGN, 0)
        self.assertEqual(struct.unpack_from("<I", raw, ta.AIEX_SIZE)[0], a.aiex_size + delta)
        airf = ta.NONR + struct.unpack_from("<I", raw, ta.AIRF_OFFSET)[0]
        self.assertEqual(raw[airf : airf + 8], b"routerou")
        b = ta.AiTraffic(raw, ta.known_names())
        self.assertEqual(b.by_name(CH7).lists[0], [101, 102, 103, 7])
        self.assertEqual(b.weighted(1)[7], 2.0)
        # the road records don't move
        roads = a.aiex_offset + a.road_offset
        self.assertEqual(raw[roads : roads + 0x100], old[roads : roads + 0x100])

    def test_check_refuses_a_weighted_model_not_loaded(self):
        a = ai()
        a.set_range(1, {101: 1.0, 9: 1.0})
        self.assertTrue(a.check())
        with self.assertRaises(ValueError):
            a.to_bytes()


class PlanTest(unittest.TestCase):
    def plan(self, ambient, ports=(1, 2, 3), slots=41):
        # the sets the test doesn't set: no ports (the default would fill them)
        ambient = {"default": {"ports": 0, "share": 0.2}} | ambient
        a = ai()
        reports = traffic.plan(a, ambient, list(ports), {"k.one": 1}, slots=slots, pots=POTS)
        return a, {r.name: r for r in reports}

    def test_vanilla_settings_change_nothing(self):
        a, reports = self.plan({"default": {"ports": 0}})
        self.assertEqual(reports, {})
        self.assertEqual(a.weights, ai().weights)

    def test_default_fills_each_set_at_the_mean_weight(self):
        a = ai()
        reports = {r.name: r for r in traffic.plan(a, {}, [1, 2, 3], {}, slots=41, pots=POTS)}
        self.assertEqual(len(reports[CH7].ports), 3)  # room for 24, three cars in traffic
        for r, allowed in ((1, 3), (2, 2)):
            w = a.weighted(r)
            ports = sum(w[m] for m in reports[CH7].ports) / sum(w.values())
            self.assertAlmostEqual(ports, 3 / (allowed + 3))  # each port as likely as an average vanilla car
            # the vanilla models keep their relative weights
            vanilla = {m: x for m, x in w.items() if m not in reports[CH7].ports}
            for m, x in vanilla.items():
                self.assertAlmostEqual(x / sum(vanilla.values()), RANGES[r][m] / sum(RANGES[r].values()))

    def test_default_fill_stops_at_the_budget(self):
        a = ai()
        # Chapter 7: 18 slots - 1 - pot 13 = 4 models, 3 vanilla: room for one port
        reports = {r.name: r for r in traffic.plan(a, {}, [1, 2, 3], {}, slots=18, pots=POTS)}
        self.assertEqual(len(reports[CH7].ports), 1)
        self.assertEqual(len(a.by_name(CH7).lists[0]), 4)

    def test_ports_at_a_share_spread_over_the_roads_that_spawn(self):
        a, reports = self.plan({"sets": {CH7: {"ports": 2, "share": 0.25}}})
        ch7 = a.by_name(CH7)
        added = reports[CH7].ports
        self.assertEqual(len(added), 2)
        self.assertEqual(ch7.lists[0], [101, 102, 103, *added])
        for r in (1, 2):
            w = a.weighted(r)
            total = sum(w.values())
            self.assertAlmostEqual(total, sum(RANGES[r].values()))  # the range's total kept
            self.assertAlmostEqual(sum(w[m] for m in added) / total, 0.25)
        self.assertEqual(a.weighted(0), {})  # the road that spawns nothing still doesn't
        self.assertNotIn(102, a.weighted(2))  # a model the road leaves out stays out
        self.assertEqual(a.check(), [])

    def test_equal_weights_among_what_each_road_allows(self):
        a, _ = self.plan({"sets": {CH7: {"weights": "equal"}}})
        self.assertEqual(sorted(a.weighted(1)), [101, 102, 103])
        for w in a.weighted(1).values():
            self.assertAlmostEqual(w, 8 / 3)
        self.assertAlmostEqual(a.weighted(2)[101], a.weighted(2)[103])
        self.assertNotIn(102, a.weighted(2))

    def test_blend_is_between_vanilla_and_equal(self):
        a, _ = self.plan({"sets": {CH7: {"blend": 0.5}}})
        w = a.weighted(1)
        # vanilla shares 1/2, 1/4, 1/4; equal 1/3 each; half way, times the total 8
        self.assertAlmostEqual(w[101], 8 * (0.25 + 1 / 6))
        self.assertAlmostEqual(w[102], 8 * (0.125 + 1 / 6))

    def test_custom_factor_and_vanilla_off(self):
        a, reports = self.plan({"sets": {CH7: {"weights": "custom", "custom": {"103": 3.0}, "vanilla_off": [102]}}})
        self.assertEqual(a.by_name(CH7).lists[0], [101, 103])
        w = a.weighted(1)
        self.assertNotIn(102, w)
        self.assertAlmostEqual(w[103] / w[101], 3 * 2 / 4)
        self.assertEqual(reports[CH7].off, [102])

    def test_shared_ranges_reach_the_other_sets(self):
        a, reports = self.plan({"sets": {CH1: {"extra": [101], "share": 0.5}}})
        self.assertEqual(reports[CH1].reaches, [CHASE])
        self.assertIn(101, a.by_name(CHASE).lists[0])  # it weights 101 on range 3 now, so it loads it
        self.assertEqual(a.check(), [])

    def test_budget_leaves_ports_out(self):
        # Chapter 1 holds 2 models; slots 20 - 1 - pot 16 = 3: one more, then the budget
        a, reports = self.plan({"sets": {CH1: {"ports": 3}}}, slots=20)
        self.assertEqual(len(a.by_name(CH1).lists[0]), 3)
        self.assertTrue(reports[CH1].notes)
        self.assertEqual(traffic.budget(CH1, 41, POTS), 24)
        self.assertEqual(traffic.budget(None, 41, POTS), ta.MAX_SET_MODELS)
        self.assertEqual(traffic.budget("Chapter 1 Kidnapped", 41, POTS), 41 - 1 - traffic.POT_CAP)

    def test_ports_by_key(self):
        a, reports = self.plan({"sets": {CH7: {"ports": ["k.one"]}}})
        self.assertEqual(reports[CH7].ports, [1])
        self.assertIn(1, a.by_name(CH7).lists[0])


class TrafficControlTest(unittest.TestCase):
    setup_manager = tcm.ManagerTest.setUp
    package = tcm.ManagerTest.package
    manager = tcm.ManagerTest.manager

    def setUp(self):
        self.setup_manager()
        (self.game / traffic.AI_FILE).parent.mkdir(parents=True, exist_ok=True)
        (self.game / traffic.AI_FILE).write_bytes(make_ai())

    def control(self, m):
        tc = traffic.TrafficControl(m)
        tc.facts = lambda: (41, POTS)
        return tc

    def test_selectors_and_files(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        b = m.install(self.package("CAR_B_10"))
        tc = self.control(m)
        self.assertEqual(tc.select(["all"]), [a, b])
        self.assertEqual(tc.select(["game:FH1"]), [a, b])
        self.assertEqual(tc.select(["dlc"]), [])
        tc.set_traffic([a], on=False)
        tc.pin([b], [9, 6])
        tc.set_ambient(CH7, ports=1, share=0.3)
        tc.write()
        lua = (self.game / traffic.TRAFFIC_LUA).read_text()
        self.assertIn("[3] = { traffic = false },", lua)
        self.assertIn("[5] = { regions = { 6, 9 } },", lua)
        copy = ta.AiTraffic.read(self.game / traffic.AI_COPY, ta.known_names())
        self.assertEqual(copy.by_name(CH7).lists[0], [101, 102, 103, 5])  # only B is in traffic
        redirect = (self.game / car_gui.GENERATED / car_gui.REDIRECT).read_text()
        self.assertIn(r"media\san_franciscoAI.fchunk|dsfcars\traffic\san_franciscoAI.fchunk", redirect)
        self.assertTrue(any("NOT vanilla" in line or "vanilla" in line for line in tc.status()))
        tc.ambient_off()
        tc.write()
        self.assertFalse((self.game / traffic.AI_COPY).exists())
        self.assertFalse((self.game / car_gui.GENERATED / car_gui.REDIRECT).exists())

    def test_settings_survive_reinstall(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        tc = self.control(m)
        tc.set_traffic([a], on=False)
        m.save_state()
        m.uninstall()
        self.assertFalse((self.game / traffic.TRAFFIC_LUA).exists())
        m2 = self.manager()
        m2.install(self.package("CAR_A_10"))
        self.assertFalse(self.control(m2).cars()[a]["traffic"])


@unittest.skipUnless(reg.STATS.exists(), "needs out/lua-src (uv run dsff game_data prepare); CI has no game")
class RegisterTrafficTest(unittest.TestCase):
    PACKAGES = (
        "return { cars = { "
        '[1] = { host = 266, key = "k.a", media = "A", name = "A", make = "Make1", model = "A", year = "2010" }, '
        '[2] = { host = 266, key = "k.b", media = "B", name = "B", make = "Make1", model = "B", year = "2010" }, '
        '[3] = { host = 266, key = "k.c", media = "C", name = "C", make = "Make1", model = "C", year = "2010" } } }'
    )

    def regions(self, traffic_lua: str, vanilla: list[list[int]]) -> list[list[int]]:
        bs = chr(92)
        files = {f"dsfcars{bs}packages.lua": self.PACKAGES, f"dsfcars{bs}traffic.lua": traffic_lua}
        lua, _out = reg.run(True, cars="return { cars = {} }", files=files, packages={1, 2, 3})
        table = "{" + ",".join("{vehicleList = {" + ",".join(map(str, v)) + "}}" for v in vanilla) + "}"
        lua.execute(f"r = {table}; InterestingVehicleManager.SetInterestingVehicleRegions(r)")
        r = lua.eval("r")
        return [list(r[i + 1].vehicleList.values()) for i in range(len(vanilla))]

    def test_off_pinned_and_ports_off(self):
        lua = "return { ports = true, cars = { [1] = { traffic = false }, [2] = { regions = { 2 } } } }"
        out = self.regions(lua, [[100], [100]])
        self.assertEqual(out, [[100, 3], [2, 100, 3]])  # pinned: at the front
        out = self.regions("return { ports = false, cars = { [2] = { regions = { 1 } } } }", [[100], [100]])
        self.assertEqual(out, [[2, 100], [100]])

    def test_vanilla_off_leaves_only_the_cars(self):
        out = self.regions(
            "return { ports = false, vanilla = false, cars = { [2] = { regions = { 1 } } } }", [[100, 101], [100]]
        )
        self.assertEqual(out, [[2], []])

    def test_regions_stop_at_128(self):
        out = self.regions("return { ports = true }", [list(range(200, 327)), [100]])
        self.assertEqual(len(out[0]), 128)  # 127 vanilla IDs, room for one (#562)
        self.assertEqual(out[1], [100, 1, 2, 3])


class SlotsTest(unittest.TestCase):
    def test_slots_from_the_plugin_ini(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            ini = Path(d) / traffic.PLUGIN_INI
            ini.parent.mkdir(parents=True)
            for text, want in (
                ("[archive]\n", traffic.MAX_SLOTS),  # the plugin's default
                ("[archive]\nvehicle_slots=48\n", 48),
                ("[archive]\nvehicle_slots=70\n", traffic.MAX_SLOTS),  # the plugin clamps it to 60
                ("[archive]\nvehicle_slots=20\n", traffic.VANILLA_SLOTS),
            ):
                ini.write_text(text, encoding="utf-8")
                self.assertEqual(traffic.plugin_slots(Path(d), {"archive.slots"}), want, text)
            self.assertEqual(traffic.plugin_slots(Path(d), {"archive.packages"}), traffic.VANILLA_SLOTS)


class PotsTest(unittest.TestCase):
    def test_no_pot_table_writes_no_sets(self):
        from unittest import mock

        from dsfforza.formats import lua_scripts

        # without the mission pots every budget would be 40: sets the chapter can't load
        with mock.patch.object(lua_scripts, "read_package", return_value=[]), self.assertRaises(ValueError):
            traffic.lua_traffic_uses(None)


class GuiSmokeTest(unittest.TestCase):
    setup_manager = tcm.ManagerTest.setUp
    package = tcm.ManagerTest.package
    manager = tcm.ManagerTest.manager
    setUp = TrafficControlTest.setUp

    def test_window_builds_and_previews(self):
        import tkinter as tk

        from dsfforza.build import car_manager_gui

        m = self.manager()
        m.install(self.package("CAR_A_10"))
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"no display for Tk: {e}")
        try:
            root.withdraw()
            app = car_manager_gui.App(root, m)
            app.tc.facts = lambda: (41, POTS)
            app.set_name.set(CH7)
            self.assertEqual((app.gather()["ports"], app.gather()["share"]), ("fill", "mean"))  # the default
            app.fill.set(False)
            app.ports.set(1)
            app.preview()
            self.assertIn(CH7, app.budget.cget("text"))
            self.assertEqual(len(app.tree.get_children()), 1)
            self.assertIn("101", app.pool.get_children())
            app.tc.set_ambient(CH7, ports=["forza.fh1.CAR_A_10"])
            app.load_set()
            self.assertEqual(app.gather()["ports"], ["forza.fh1.CAR_A_10"])  # keys kept
            app.fill.set(False)
            app.ports.set(2)
            self.assertEqual(app.gather()["ports"], 2)
            # a change while the last one is written is refused before any setting changes
            from unittest import mock

            app.busy = True
            before = json.dumps(app.tc.t, sort_keys=True)
            with mock.patch.object(car_manager_gui.messagebox, "showinfo") as shown:
                app.shuffle()
                app.apply_set()
                app.apply_iv()
            self.assertEqual(shown.call_count, 3)
            self.assertEqual(json.dumps(app.tc.t, sort_keys=True), before)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
