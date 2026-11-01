"""car_manager: install, upgrade, enable, disable, remove and verify car packages in a
fake game folder (synthetic archive and packages, no game data)."""

import json
import struct
import tempfile
import unittest
from pathlib import Path

from tests.test_dsfcar import car_sp, dds, leaf, manifest, node, png

from dsfforza.build import car_manager, car_package, dsfcar
from dsfforza.formats.chnk import write_chunk
from dsfforza.formats.vehicles import SSLP_RECORD


def archive(path: Path, ids: list[int]) -> None:
    """A vehicle archive whose SSLP has kind 0, 1 and 2 records for each base ID."""
    records = b"".join(SSLP_RECORD.pack(i + 1000 * k, 3 * n + k, k) for n, i in enumerate(ids) for k in range(3))
    head = struct.pack("<HH", 0, 3 * len(ids)) + struct.pack("<III", 0x6D000, 0x1000, 0x1000) + b"\0" * 12
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        write_chunk(f, None, [node("SSIC", [node("NONR", [leaf("SSLP", head + records)], align=4)])])


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.game = self.tmp / "game"
        archive(self.game / "Install" / "dngvehicles.sp", [1, 2, 4, 62])
        self.lines = []

    def package(self, media: str, version: int = 1, car: int = 1, host: int = 205) -> Path:
        sp = car_sp(self.tmp / f"{media}.sp", car=car, host=host)
        m = manifest(key=f"forza.fh1.{media}", version=version)
        m["car"].update(model_id=car, host=host)
        m["source"] = {"pipeline": "test", "game": "FH1", "media": media}
        images = {dsfcar.PICTURE: dds(), dsfcar.BADGE: png(58, 58), dsfcar.PLATE: png(98, 34)}
        return dsfcar.write(self.tmp / f"{media}-v{version}.dsfcar", m, sp, images)

    def manager(self) -> car_manager.Manager:
        return car_manager.Manager(self.game, log=self.lines.append)

    def listed(self) -> dict[int, str]:
        text = (self.game / car_manager.CARS_DIR / car_manager.LIST).read_text()
        return {int(a): b for a, b in (line.split(maxsplit=1) for line in text.splitlines() if line[:1].isdigit())}

    def test_install_allocates_free_ids(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        b = m.install(self.package("CAR_B_10"))
        self.assertEqual((m.state["cars"][a]["id"], m.state["cars"][b]["id"]), (3, 5))  # 1, 2, 4 are the archive's
        self.assertEqual(
            self.listed(), {3: r"dsfforza\cars\forza.fh1.CAR_A_10.sp", 5: r"dsfforza\cars\forza.fh1.CAR_B_10.sp"}
        )
        with (self.game / car_manager.CARS_DIR / f"{a}.sp").open("rb") as f:
            from dsfforza.formats.chnk import read_chunk

            self.assertEqual(car_package.car_id(f, read_chunk(f, 0).entries[0]), 3)
        folder = self.game / car_manager.CARS_DIR / a
        self.assertEqual(
            sorted(p.name for p in folder.iterdir()), ["badge.png", "car.json", "picture.dds", "plate.png"]
        )
        lua = (self.game / car_manager.LUA).read_text()
        self.assertIn('[3] = { host = 205, key = "forza.fh1.CAR_A_10", media = "CAR_A_10"', lua)
        self.assertIn("speed = 0.5, strength = 0.25, drift = 1 }", lua)
        self.assertEqual(m.verify(), [])

    def test_ids_are_kept(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        m.install(self.package("CAR_B_10"))
        m.install(self.package("CAR_A_10", version=2))  # upgrade: same ID
        self.assertEqual(m.state["cars"][a]["id"], 3)
        self.assertEqual(m.state["cars"][a]["version"], 2)
        with self.assertRaises(SystemExit):  # an older version
            m.install(self.package("CAR_A_10", version=1))
        m.install(self.package("CAR_A_10", version=1), force=True)
        m.set_enabled(a, enabled=False)
        self.assertNotIn(3, self.listed())
        self.assertNotIn("[3]", (self.game / car_manager.LUA).read_text())
        self.assertTrue((self.game / car_manager.CARS_DIR / f"{a}.sp").exists())
        self.assertEqual(self.manager().state["cars"][a]["enabled"], False)  # the state is on disk
        m.set_enabled(a, enabled=True)
        self.assertIn(3, self.listed())
        m.install(self.package("CAR_A_10", version=3))
        self.assertTrue(m.state["cars"][a]["enabled"])

    def test_remove_keeps_the_id_reserved(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        m.remove(a)
        self.assertFalse((self.game / car_manager.CARS_DIR / f"{a}.sp").exists())
        self.assertFalse((self.game / car_manager.CARS_DIR / a).exists())
        self.assertEqual(self.listed(), {})
        b = m.install(self.package("CAR_B_10"))
        self.assertEqual(m.state["cars"][b]["id"], 5)  # not 3: that is CAR_A_10's
        self.assertTrue(any("removed; ID reserved" in line for line in m.status()))
        self.assertEqual(m.state["cars"][m.install(self.package("CAR_A_10"))]["id"], 3)  # back on its own ID
        with self.assertRaises(SystemExit):
            m.remove("forza.fh1.NOT_THERE_10")

    def test_reserved_and_merged(self):
        lua = self.game / car_manager.PORTED
        lua.parent.mkdir(parents=True)
        lua.write_text(
            "local ported = {\n\tcars = {\n"
            '\t\t[3] = { host = 205, media = "CAR_A_10", name = "A", make = "A", model = "A", year = "2010", game = "FH1" },\n'
            '\t\t[5] = { host = 205, media = "CAR_M_10", name = "M", make = "M", model = "M", year = "", game = "FM4" },\n'
            "\t},\n}\n"
        )
        m = self.manager()
        self.assertEqual(m.state["merged"], {"forza.fh1.CAR_A_10": 3, "forza.fm4.CAR_M_10": 5})
        with self.assertRaises(SystemExit):  # that car is in the merged archive already
            m.install(self.package("CAR_A_10"))
        b = m.install(self.package("CAR_B_10"))
        self.assertEqual(m.state["cars"][b]["id"], 6)
        taken = m.taken()
        self.assertTrue({622, 623} <= taken and {1, 2, 3, 4, 5, 6, 62} <= taken)

    def test_verify(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        sp = self.game / car_manager.CARS_DIR / f"{a}.sp"
        sp.write_bytes(sp.read_bytes() + b"x")
        self.assertTrue(any("changed" in p for p in m.verify()))
        sp.unlink()
        self.assertTrue(any("missing" in p for p in m.verify()))
        archive(self.game / "Install" / "dngvehicles.sp", [1, 2, 3, 4, 62])  # another mod's archive took 3
        self.assertTrue(any("is in" in p for p in m.verify()))
        with self.assertRaises(SystemExit):
            m.install(self.package("CAR_A_10", version=2))

    def test_bad_package_refused(self):
        bad = self.tmp / "bad.dsfcar"
        bad.write_bytes(b"x")
        with self.assertRaises(ValueError):
            self.manager().install(bad)
        self.assertFalse((self.game / car_manager.CARS_DIR / car_manager.STATE).exists())

    def test_cli(self):
        pkg = self.package("CAR_A_10")
        car_manager.main(["--game", str(self.game), "install", str(pkg)])
        with self.assertRaises(SystemExit) as done:
            car_manager.main(["--game", str(self.game), "verify"])
        self.assertEqual(done.exception.code, 0)
        car_manager.main(["--game", str(self.game), "disable", "forza.fh1.CAR_A_10"])
        state = json.loads((self.game / car_manager.CARS_DIR / car_manager.STATE).read_text())
        self.assertFalse(state["cars"]["forza.fh1.CAR_A_10"]["enabled"])
        # --game after the command, as the usage lines write it
        car_manager.main(["enable", "forza.fh1.CAR_A_10", "--game", str(self.game)])
        state = json.loads((self.game / car_manager.CARS_DIR / car_manager.STATE).read_text())
        self.assertTrue(state["cars"]["forza.fh1.CAR_A_10"]["enabled"])

    def test_install_from_a_folder(self):
        """A folder stands for every .dsfcar in it (install.ps1 passes the build's packages
        folder: a roster's paths don't fit on one Windows command line)."""
        folder = self.tmp / "packages"
        folder.mkdir()
        for c in "AB":
            pkg = self.package(f"CAR_{c}_10")
            pkg.rename(folder / pkg.name)
        (folder / "notes.txt").write_text("not a package")
        (folder / "old.dsfcar").mkdir()  # a folder named like a package is not one
        car_manager.main(["--game", str(self.game), "install", str(folder)])
        state = json.loads((self.game / car_manager.CARS_DIR / car_manager.STATE).read_text())
        self.assertEqual(sorted(state["cars"]), ["forza.fh1.CAR_A_10", "forza.fh1.CAR_B_10"])
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(SystemExit):
            car_manager.main(["--game", str(self.game), "install", str(empty)])

    def test_lua_strings(self):
        self.assertEqual(car_manager._lua_string('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(car_manager._lua_string("x\r\ny\t1"), '"x\\013\\010y\\0091"')
        m = self.manager()
        pkg = self.package("CAR_A_10")
        m.install(pkg)
        lua = (self.game / car_manager.LUA).read_text()
        self.assertNotIn("\r", lua)

    def test_merged_install_changes(self):
        ported = self.game / car_manager.PORTED
        ported.parent.mkdir(parents=True)
        row = '\t\t[{i}] = {{ host = 205, media = "{m}", name = "A", make = "A", model = "A", year = "", game = "FH1" }},\n'
        ported.write_text("local ported = {\n\tcars = {\n" + row.format(i=7, m="CAR_A_10") + "\t},\n}\n")
        self.manager().save()
        ported.unlink()  # the merged install is gone
        m = self.manager()
        self.assertEqual(m.state["merged"], {})
        a = m.install(self.package("CAR_A_10"))  # no longer refused, and on the ID it had
        self.assertEqual(m.state["cars"][a]["id"], 7)
        # A merged install that now has an installed package's car (at another ID): the
        # archive's copy wins, the package is left out of the outputs and verify says so.
        b = m.install(self.package("CAR_B_10"))
        ported.write_text("local ported = {\n\tcars = {\n" + row.format(i=40, m="CAR_B_10") + "\t},\n}\n")
        m = self.manager()
        m.save()
        self.assertNotIn(m.state["cars"][b]["id"], self.listed())
        self.assertIn(7, self.listed())
        self.assertTrue(any("left out" in p for p in m.verify()))

    def test_case_folded_keys(self):
        m = self.manager()
        m.install(self.package("CAR_A_10"))
        with self.assertRaises(SystemExit):
            m.install(self.package("car_a_10"))
        self.assertEqual(len(m.state["cars"]), 1)

    def test_upgrade_failure_keeps_the_installed_package(self):
        from unittest import mock

        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        sp = self.game / car_manager.CARS_DIR / f"{a}.sp"
        before = sp.read_bytes()
        state = json.loads(json.dumps(m.state))
        with mock.patch.object(car_package, "install_sp", side_effect=OSError("disk full")), self.assertRaises(OSError):
            m.install(self.package("CAR_A_10", version=2))
        self.assertEqual(sp.read_bytes(), before)
        self.assertEqual(m.state, state)
        # A failure after the swap (writing the state) puts the old files and state back.
        real_save, calls = m.save, []

        def failing_save():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk full")
            real_save()

        with mock.patch.object(m, "save", side_effect=failing_save), self.assertRaises(OSError):
            m.install(self.package("CAR_A_10", version=2))
        self.assertEqual(sp.read_bytes(), before)
        self.assertEqual(m.state["cars"][a]["version"], 1)
        self.assertEqual(
            sorted(p.name for p in (self.game / car_manager.CARS_DIR).iterdir()),
            sorted([a, f"{a}.sp", car_manager.LIST, car_manager.STATE]),
        )
        self.assertEqual(m.verify(), [])
        # A new key whose install fails doesn't keep the ID it was about to get.
        with mock.patch.object(car_package, "install_sp", side_effect=OSError("disk full")), self.assertRaises(OSError):
            m.install(self.package("CAR_B_10"))
        self.assertNotIn("forza.fh1.CAR_B_10", m.state["ids"])

    def test_verify_compares_the_list(self):
        m = self.manager()
        m.install(self.package("CAR_A_10"))
        path = self.game / car_manager.CARS_DIR / car_manager.LIST
        good = path.read_text()
        path.write_text(good.replace("CAR_A_10.sp", "OTHER.sp"))
        self.assertTrue(any("ID 3 lists" in p for p in m.verify()))
        path.write_text(good + "9 dsfforza\\cars\\stray.sp\n")
        self.assertTrue(any("ID 9 lists" in p for p in m.verify()))
        path.write_text(good)
        self.assertEqual(m.verify(), [])

    def test_batch_generates_the_gui_once(self):
        """Installing, disabling or removing several cars makes the GUI copies once (each
        is a rewrite of gui.sp), after the last change, and also when one fails."""
        from unittest import mock

        pkgs = [str(self.package(f"CAR_{c}_10")) for c in "ABC"]
        with mock.patch.object(car_manager.Manager, "generate_gui") as gen:
            car_manager.main(["--game", str(self.game), "install", *pkgs])
            self.assertEqual(gen.call_count, 1)
            self.assertEqual([i for i, _ in gen.call_args.args[0]], [3, 5, 6])
            car_manager.main(["--game", str(self.game), "disable", "forza.fh1.CAR_A_10", "forza.fh1.CAR_B_10"])
            self.assertEqual(gen.call_count, 2)
            self.assertEqual([i for i, _ in gen.call_args.args[0]], [6])
            bad = self.tmp / "bad.dsfcar"
            bad.write_bytes(b"x")
            with self.assertRaises(ValueError):
                car_manager.main(["--game", str(self.game), "install", pkgs[0], str(bad)])
            self.assertEqual(gen.call_count, 3)  # still made once, for what was saved

    def test_uninstall_keeps_the_ids(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        b = m.install(self.package("CAR_B_10"))
        (self.game / "dsfforza" / "generated").mkdir(parents=True)
        (self.game / "dsfforza" / "generated" / "gui.sp").write_bytes(b"x")
        car_manager.main(["--game", str(self.game), "uninstall"])
        cars = self.game / car_manager.CARS_DIR
        self.assertEqual(sorted(p.name for p in cars.iterdir()), [car_manager.STATE])
        self.assertFalse((self.game / "dsfforza" / "generated").exists())
        self.assertFalse((self.game / car_manager.LUA).exists())
        # A later install: the same IDs, so the save's flags are still each car's.
        m = self.manager()
        self.assertEqual(m.state["cars"], {})
        m.install(self.package("CAR_B_10"))
        m.install(self.package("CAR_A_10"))
        self.assertEqual((m.state["cars"][a]["id"], m.state["cars"][b]["id"]), (3, 5))
        car_manager.main(["--game", str(self.game), "uninstall", "--forget-ids"])
        self.assertFalse((self.game / "dsfforza").exists())  # nothing of ours left

    def test_seed_carries_a_merged_install_over(self):
        ported = self.game / car_manager.PORTED
        ported.parent.mkdir(parents=True)
        row = '\t\t[{i}] = {{ host = 205, media = "{m}", name = "A", make = "A", model = "A", year = "", game = "FH1" }},\n'
        ported.write_text("local ported = {\n\tcars = {\n" + row.format(i=40, m="CAR_A_10") + "\t},\n}\n")
        car_manager.main(["--game", str(self.game), "seed"])
        ported.unlink()  # the merged install is uninstalled
        m = self.manager()
        self.assertEqual(m.state["merged"], {})
        a = m.install(self.package("CAR_A_10"))
        self.assertEqual(m.state["cars"][a]["id"], 40)  # the merged car's ID, so its save flags
        b = m.install(self.package("CAR_B_10"))
        self.assertEqual(m.state["cars"][b]["id"], 3)

    def test_reassign_after_another_archive_takes_an_id(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        with self.assertRaises(SystemExit):  # no clash: nothing to do
            m.reassign(a)
        archive(self.game / "Install" / "dngvehicles.sp", [1, 2, 3, 4, 62])  # another mod's archive has 3
        m = self.manager()
        self.assertTrue(m.verify())
        car_manager.main(["--game", str(self.game), "reassign", a])
        m = self.manager()
        self.assertEqual(m.state["cars"][a]["id"], 5)
        self.assertEqual(m.verify(), [])
        with (self.game / car_manager.CARS_DIR / f"{a}.sp").open("rb") as f:
            from dsfforza.formats.chnk import read_chunk

            self.assertEqual(car_package.car_id(f, read_chunk(f, 0).entries[0]), 5)


if __name__ == "__main__":
    unittest.main()
