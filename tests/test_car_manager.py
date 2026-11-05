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


def plugin(game: Path, capabilities: str | None = "archive.packages archive.ids lua.packages") -> None:
    """A stand-in plugins/dsfforza.asi: some DLL-like bytes with the capability marker
    (tools/asi/src/patch.h); None: a plugin from before the marker."""
    path = game / car_manager.PLUGIN
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = b"" if capabilities is None else car_manager.CAPABILITIES_MARKER + capabilities.encode() + b"\0"
    path.write_bytes(b"MZ" + b"\0" * 200 + b"dsfforza.asi %s, ini %s\0" + marker + b"\0" * 64)


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.game = self.tmp / "game"
        archive(self.game / "Install" / "dngvehicles.sp", [1, 2, 4, 62])
        plugin(self.game)
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
            self.listed(),
            {3: r"dsfforza\cars\forza.fh1.CAR_A_10\car.sp", 5: r"dsfforza\cars\forza.fh1.CAR_B_10\car.sp"},
        )
        # car.sp is the package's, byte for byte: both hold their builder's ID 1, and the
        # plugin writes 3 and 5 into the data it serves.
        for key in (a, b):
            installed = (self.game / car_manager.CARS_DIR / key / "car.sp").read_bytes()
            self.assertEqual(installed, dsfcar.read(self.tmp / f"{key.split('.')[-1]}-v1.dsfcar")[1])
            self.assertEqual(dsfcar.sha256(installed), m.manifest(key)["car"]["sha256"])
            with (self.game / car_manager.CARS_DIR / key / "car.sp").open("rb") as f:
                from dsfforza.formats.chnk import read_chunk

                self.assertEqual(car_package.car_id(f, read_chunk(f, 0).entries[0]), 1)
        folder = self.game / car_manager.CARS_DIR / a
        self.assertEqual(
            sorted(p.name for p in folder.iterdir()), ["badge.png", "car.json", "car.sp", "picture.dds", "plate.png"]
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
        self.assertTrue((self.game / car_manager.CARS_DIR / a / "car.sp").exists())
        self.assertEqual(self.manager().state["cars"][a]["enabled"], False)  # the state is on disk
        m.set_enabled(a, enabled=True)
        self.assertIn(3, self.listed())
        m.install(self.package("CAR_A_10", version=3))
        self.assertTrue(m.state["cars"][a]["enabled"])

    def test_remove_keeps_the_id_reserved(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        m.remove(a)
        self.assertFalse((self.game / car_manager.CARS_DIR / a / "car.sp").exists())
        self.assertFalse((self.game / car_manager.CARS_DIR / a).exists())
        self.assertEqual(self.listed(), {})
        b = m.install(self.package("CAR_B_10"))
        self.assertEqual(m.state["cars"][b]["id"], 5)  # not 3: that is CAR_A_10's
        self.assertTrue(any("removed; ID reserved" in line for line in m.status()))
        self.assertEqual(m.state["cars"][m.install(self.package("CAR_A_10"))]["id"], 3)  # back on its own ID
        with self.assertRaises(SystemExit):
            m.remove("forza.fh1.NOT_THERE_10")

    def test_reserved_ids(self):
        m = self.manager()
        b = m.install(self.package("CAR_B_10"))
        self.assertEqual(m.state["cars"][b]["id"], 3)
        taken = m.taken()
        self.assertTrue({622, 623} <= taken and {1, 2, 3, 4, 62} <= taken)

    def test_verify(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        sp = self.game / car_manager.CARS_DIR / a / "car.sp"
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

    def test_ported_lua_is_read_only_by_seed(self):
        """A merged install's ported.lua (an earlier release's) counts only when `seed` reads
        it: loading the manager neither records nor refuses its cars, and a state written while
        it was there (its "merged" key) loads as before."""
        ported = self.write_ported({7: "CAR_A_10"})
        m = self.manager()
        self.assertEqual(m.state["ids"], {})
        a = m.install(self.package("CAR_A_10"))  # not refused: install.ps1 removes the merged install first
        self.assertEqual(m.state["cars"][a]["id"], 3)
        ported.unlink()
        state = self.game / car_manager.CARS_DIR / car_manager.STATE
        old = json.loads(state.read_text())
        old["merged"] = {"forza.fh1.CAR_Z_10": 9}  # manager.json of the coexistence code
        old["ids"]["forza.fh1.CAR_Z_10"] = 9
        state.write_text(json.dumps(old))
        m = self.manager()
        self.assertNotIn("merged", m.state)
        self.assertEqual(m.state["ids"]["forza.fh1.CAR_Z_10"], 9)  # its ID stays reserved
        m.save()
        self.assertNotIn("merged", json.loads(state.read_text()))
        self.assertEqual(m.verify(), [])

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
        sp = self.game / car_manager.CARS_DIR / a / "car.sp"
        before = sp.read_bytes()
        state = json.loads(json.dumps(m.state))
        with (
            mock.patch.object(car_manager.Manager, "_stage", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
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
            sorted([a, car_manager.LIST, car_manager.STATE]),
        )
        self.assertEqual(m.verify(), [])
        # A new key whose install fails doesn't keep the ID it was about to get.
        with (
            mock.patch.object(car_manager.Manager, "_stage", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            m.install(self.package("CAR_B_10"))
        self.assertNotIn("forza.fh1.CAR_B_10", m.state["ids"])

    def test_interrupted_swap_is_recovered(self):
        """A swap that stopped after the installed folder went to <key>.old: the next install
        puts it back before staging, so a failing one leaves the car installed."""
        from unittest import mock

        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        folder = self.game / car_manager.CARS_DIR / a
        before = (folder / "car.sp").read_bytes()
        folder.replace(folder.with_name(a + ".old"))
        with (
            mock.patch.object(car_manager.Manager, "_stage", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            m.install(self.package("CAR_A_10", version=2))
        self.assertEqual((folder / "car.sp").read_bytes(), before)
        self.assertFalse(folder.with_name(a + ".old").exists())
        self.assertEqual(m.verify(), [])

    def test_remove_of_a_folder_in_use_changes_nothing(self):
        """A folder that can't be moved (the game has a file in it open) stops remove and
        uninstall before the state changes."""
        from unittest import mock

        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        b = m.install(self.package("CAR_B_10"))
        real = Path.replace

        def busy(path, target):
            if path.name == b:
                raise PermissionError("in use")
            return real(path, target)

        with mock.patch.object(Path, "replace", busy):
            with self.assertRaises(PermissionError):
                m.remove(b)
            self.assertIn(b, m.state["cars"])
            with self.assertRaises(PermissionError):
                m.uninstall()
        # The car whose folder moved is uninstalled; the busy one stays installed and listed.
        self.assertEqual(set(m.state["cars"]), {b})
        self.assertFalse((self.game / car_manager.CARS_DIR / a).exists())
        self.assertEqual(m.verify(), [])
        m.uninstall()
        self.assertEqual(sorted(p.name for p in (self.game / car_manager.CARS_DIR).iterdir()), [car_manager.STATE])

    def test_verify_compares_the_list(self):
        m = self.manager()
        m.install(self.package("CAR_A_10"))
        path = self.game / car_manager.CARS_DIR / car_manager.LIST
        good = path.read_text()
        path.write_text(good.replace("CAR_A_10", "OTHER"))
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

    def write_ported(self, cars: dict[int, str]) -> Path:
        """A merged install's ported.lua (port_car.lua_snippet's rows)."""
        ported = self.game / car_manager.PORTED
        ported.parent.mkdir(parents=True, exist_ok=True)
        row = '\t\t[{i}] = {{ host = 205, media = "{m}", name = "A", make = "A", model = "A", year = "", game = "FH1" }},\n'
        rows = "".join(row.format(i=i, m=m) for i, m in cars.items())
        ported.write_text("local ported = {\n\tcars = {\n" + rows + "\t},\n}\n")
        return ported

    def test_seed_carries_a_merged_install_over(self):
        ported = self.write_ported({40: "CAR_A_10", 3: "CAR_C_10"})
        car_manager.main(["--game", str(self.game), "seed"])
        ported.unlink()  # the merged install is uninstalled
        m = self.manager()
        self.assertEqual(m.state["ids"], {"forza.fh1.CAR_A_10": 40, "forza.fh1.CAR_C_10": 3})
        a = m.install(self.package("CAR_A_10"))
        self.assertEqual(m.state["cars"][a]["id"], 40)  # the merged car's ID, so its save flags
        b = m.install(self.package("CAR_B_10"))
        self.assertEqual(m.state["cars"][b]["id"], 5)  # 3 is CAR_C_10's, 4 the archive's
        self.assertEqual(m.state["cars"][m.install(self.package("CAR_C_10"))]["id"], 3)

    def test_seed_keeps_the_ids_the_map_has(self):
        """A car the map already has (installed as a package, then a merged install over it)
        keeps the map's ID: seeding again or later changes nothing."""
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        self.write_ported({40: "CAR_A_10", 41: "CAR_B_10"})
        self.assertEqual(self.manager().seed(), 2)
        m = self.manager()
        self.assertEqual(m.state["ids"], {a: 3, "forza.fh1.CAR_B_10": 41})
        self.assertEqual(m.seed(), 2)
        self.assertEqual(self.manager().state["ids"], {a: 3, "forza.fh1.CAR_B_10": 41})

    def test_reassign_after_another_archive_takes_an_id(self):
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        sp = self.game / car_manager.CARS_DIR / a / "car.sp"
        before = sp.read_bytes()
        with self.assertRaises(SystemExit):  # no clash: nothing to do
            m.reassign(a)
        archive(self.game / "Install" / "dngvehicles.sp", [1, 2, 3, 4, 62])  # another mod's archive has 3
        m = self.manager()
        self.assertTrue(m.verify())
        car_manager.main(["--game", str(self.game), "reassign", a])
        m = self.manager()
        self.assertEqual(m.state["cars"][a]["id"], 5)
        self.assertEqual(m.verify(), [])
        self.assertEqual(sp.read_bytes(), before)  # only the list changes; the plugin writes 5 in memory
        self.assertEqual(self.listed(), {5: rf"dsfforza\cars\{a}\car.sp"})

    def test_two_packages_with_one_builder_id(self):
        """Packages from different builds can hold the same model ID: each is installed as it
        is, under its own ID (the plugin writes that one into the data it serves)."""
        m = self.manager()
        a = m.install(self.package("CAR_A_10", car=7))
        b = m.install(self.package("CAR_B_10", car=7))
        self.assertEqual((m.state["cars"][a]["id"], m.state["cars"][b]["id"]), (3, 5))
        self.assertEqual(sorted(self.listed()), [3, 5])
        self.assertEqual(m.verify(), [])

    def test_a_car_sp_patched_by_an_earlier_manager_still_verifies(self):
        """Installs from before the plugin wrote the ID in memory hold car.sp with the car's ID
        written in (state sha256 = that file's): verify takes it, and any other bytes not."""
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        sp = self.game / car_manager.CARS_DIR / a / "car.sp"
        patched = car_sp(self.tmp / "patched.sp", car=m.state["cars"][a]["id"])
        sp.write_bytes(patched)
        self.assertTrue(any("not the package's car.sp" in p for p in m.verify()))
        m.state["cars"][a]["sha256"] = dsfcar.sha256(patched)
        m.save()
        m = self.manager()
        self.assertEqual(m.verify(), [])
        sp.write_bytes(patched + b"x")
        self.assertTrue(any("not the package's car.sp" in p for p in m.verify()))

    def test_plugin_capabilities(self):
        self.assertEqual(car_manager.plugin_capabilities(self.game / car_manager.PLUGIN), car_manager.MANAGER_NEEDS)
        plugin(self.game, None)
        self.assertEqual(car_manager.plugin_capabilities(self.game / car_manager.PLUGIN), set())
        self.assertIsNone(car_manager.plugin_capabilities(self.tmp / "none" / "dsfforza.asi"))

    def test_marker_matches_the_plugin_source(self):
        """The marker and names the manager reads are the ones patch.h defines, and the plugin
        has every capability a package may ask for."""
        import re

        from dsfforza import REPO

        header = (REPO / "tools" / "asi" / "src" / "patch.h").read_text(encoding="utf-8")
        marker = re.search(r'#define DSF_CAPABILITIES_MARKER "([^"]*)"', header).group(1)
        names = set(re.search(r'#define DSF_CAPABILITIES "([^"]*)"', header).group(1).split())
        self.assertEqual(marker.encode(), car_manager.CAPABILITIES_MARKER)
        self.assertTrue(names >= car_manager.MANAGER_NEEDS)
        self.assertEqual(dsfcar.CAPABILITIES, names)

    def test_a_missing_or_older_plugin_is_refused_before_anything_is_written(self):
        """A plugin from before archive.ids serves car.sp's builder ID under the listed one:
        install, enable and reassign refuse, and nothing is written."""
        pkg = self.package("CAR_A_10")
        cars = self.game / car_manager.CARS_DIR
        for caps, words in ((False, "isn't there"), (None, "is older"), ("archive.packages", "is older")):
            with self.subTest(caps=caps):
                if caps is False:
                    (self.game / car_manager.PLUGIN).unlink(missing_ok=True)
                else:
                    plugin(self.game, caps)
                with self.assertRaises(SystemExit) as refused:
                    car_manager.main(["--game", str(self.game), "install", str(pkg)])
                self.assertIn(words, str(refused.exception))
                self.assertIn("install.ps1", str(refused.exception))
                self.assertIn("archive.ids", str(refused.exception))
                self.assertFalse(cars.exists())
        # Installed with a current plugin, then the plugin goes back to an older one.
        plugin(self.game)
        a = self.manager().install(pkg)
        self.manager().set_enabled(a, enabled=False)
        before = {p.name: p.read_bytes() for p in cars.iterdir() if p.is_file()}
        plugin(self.game, None)
        with self.assertRaises(SystemExit):
            car_manager.main(["--game", str(self.game), "enable", a])
        archive(self.game / "Install" / "dngvehicles.sp", [1, 2, 3, 4, 62])  # another archive takes 3
        with self.assertRaises(SystemExit):
            car_manager.main(["--game", str(self.game), "reassign", a])
        self.assertEqual({p.name: p.read_bytes() for p in cars.iterdir() if p.is_file()}, before)
        # Disabling and removing list no car, so an older plugin doesn't stop them.
        car_manager.main(["--game", str(self.game), "disable", a])
        car_manager.main(["--game", str(self.game), "remove", a])
        self.assertEqual(self.listed(), {})

    def test_missing_vehiclestats_is_refused_before_anything_is_written(self):
        """With the game's GUI files there but no decompiled vanilla vehicleStats (issue #544),
        install and enable refuse and write nothing; without GUI files there is nothing to make."""
        from unittest import mock

        from dsfforza.game import gui_assets

        pkg = self.package("CAR_A_10")
        cars = self.game / car_manager.CARS_DIR
        a = self.manager().install(pkg)  # no GUI files in the fake game: no check
        self.manager().set_enabled(a, enabled=False)
        before = {p.name: p.read_bytes() for p in cars.iterdir() if p.is_file()}
        for rel in ("Install/gui.sp", "media/Fonts.fchunk"):
            (self.game / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.game / rel).write_bytes(b"x")
        with mock.patch.object(gui_assets, "VEHICLE_STATS_LUA", self.tmp / "missing.lua"):
            for argv in (["install", str(self.package("CAR_B_11"))], ["enable", a]):
                with self.subTest(argv=argv[0]), self.assertRaises(SystemExit) as refused:
                    car_manager.main(["--game", str(self.game), *argv])
                self.assertIn("game_data prepare", str(refused.exception))
        self.assertEqual({p.name: p.read_bytes() for p in cars.iterdir() if p.is_file()}, before)
        self.assertFalse((self.game / "dsfforza" / "generated").exists())

    def test_check_plugin_command(self):
        """install.ps1's preflight: a plugin file is checked against a build's packages without
        reading or writing the manager's state."""
        folder = self.tmp / "packages"
        folder.mkdir()
        self.package("CAR_A_10").rename(folder / "forza.fh1.CAR_A_10.dsfcar")
        good = self.game / car_manager.PLUGIN
        car_manager.main(["--game", str(self.game), "check-plugin", "--plugin", str(good), str(folder)])
        older = self.tmp / "older.asi"
        older.write_bytes(b"MZ no marker")
        for plugin_file in (older, self.tmp / "none.asi"):
            with self.assertRaises(SystemExit):
                car_manager.main(["--game", str(self.game), "check-plugin", "--plugin", str(plugin_file), str(folder)])
        self.assertFalse((self.game / car_manager.CARS_DIR).exists())

    def test_a_package_asks_for_what_the_plugin_lacks(self):
        """A package's own `requires` is checked too (here a plugin that lists only
        archive.ids, so a package asking for archive.packages is refused)."""
        plugin(self.game, "archive.ids")
        with self.assertRaises(SystemExit) as refused:
            self.manager().install(self.package("CAR_A_10"))
        self.assertIn("archive.packages", str(refused.exception))

    def state_on_disk(self) -> dict:
        return json.loads((self.game / car_manager.CARS_DIR / car_manager.STATE).read_text())

    def test_state_keeps_what_the_manager_owns(self):
        """The state holds ID, version, enabled and car.sp's checksum per car; the manifest
        is the car's car.json, read where it is needed (packages.lua, status, verify)."""
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        state = self.state_on_disk()
        self.assertEqual(state["format"], 2)
        self.assertEqual(set(state["cars"][a]), {"id", "version", "enabled", "sha256"})
        folder = self.game / car_manager.CARS_DIR / a
        self.assertEqual(self.manager().manifest(a), json.loads((folder / "car.json").read_text()))
        self.assertIn("Test Car (2010)", self.manager().status()[0])
        # car.json gone: the car is left out of the outputs, and verify says why.
        (folder / "car.json").unlink()
        m = self.manager()
        m.save()
        self.assertEqual(self.listed(), {})
        self.assertNotIn("[3]", (self.game / car_manager.LUA).read_text())
        self.assertTrue(any("car.json is missing" in p for p in m.verify()))
        self.assertIn("(no car.json)", m.status()[0])
        m.install(self.package("CAR_A_10", version=2))  # installing it again puts it back
        self.assertEqual(self.listed(), {3: rf"dsfforza\cars\{a}\car.sp"})
        self.assertEqual(m.verify(), [])

    def test_format_1_state_is_upgraded_in_place(self):
        """A format-1 state (each car's manifest in it) loads as format 2 and is written back:
        the outputs stay the same, a car.json missing from a car's folder is written from the
        state's copy, and a patched car.sp (format 1's installs) still verifies."""
        m = self.manager()
        a = m.install(self.package("CAR_A_10"))
        b = m.install(self.package("CAR_B_10"))
        m.set_enabled(b, enabled=False)
        lua, listed = (self.game / car_manager.LUA).read_text(), self.listed()
        # The state as format 1 wrote it: the manifests in it, car.sp with the ID written in.
        state = self.state_on_disk()
        state["format"] = 1
        for key in (a, b):
            folder = self.game / car_manager.CARS_DIR / key
            state["cars"][key]["manifest"] = json.loads((folder / "car.json").read_text())
            patched = car_sp(self.tmp / f"{key}.sp", car=state["cars"][key]["id"])
            (folder / "car.sp").write_bytes(patched)
            state["cars"][key]["sha256"] = dsfcar.sha256(patched)
        manifest_b = (self.game / car_manager.CARS_DIR / b / "car.json").read_text()
        (self.game / car_manager.CARS_DIR / b / "car.json").unlink()
        (self.game / car_manager.CARS_DIR / car_manager.STATE).write_text(json.dumps(state))
        m = self.manager()
        self.assertTrue(any("state format 1 -> 2" in line for line in self.lines))
        on_disk = self.state_on_disk()
        self.assertEqual(on_disk["format"], 2)
        self.assertEqual(on_disk["ids"], state["ids"])
        for key in (a, b):
            expected = {k: state["cars"][key][k] for k in ("id", "version", "enabled", "sha256")}
            self.assertEqual(on_disk["cars"][key], expected)
        self.assertEqual(
            json.loads((self.game / car_manager.CARS_DIR / b / "car.json").read_text()), json.loads(manifest_b)
        )
        self.assertEqual(m.verify(), [])
        m.save()
        self.assertEqual(((self.game / car_manager.LUA).read_text(), self.listed()), (lua, listed))
        m.set_enabled(b, enabled=True)  # and it goes on working
        self.assertEqual(sorted(self.listed()), [3, 5])

    def test_unknown_state_format_refused(self):
        self.manager().install(self.package("CAR_A_10"))
        path = self.game / car_manager.CARS_DIR / car_manager.STATE
        state = json.loads(path.read_text())
        state["format"] = 3
        path.write_text(json.dumps(state))
        with self.assertRaises(SystemExit):
            self.manager()


if __name__ == "__main__":
    unittest.main()
