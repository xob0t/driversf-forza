"""tools/install.ps1 end to end on a synthetic game folder (no game data): car packages are
the only install, a merged install of an earlier release (up to v0.1.8) moves to packages on
the same IDs, and -Uninstall leaves the game as it was but for the car manager's ID map.

Runs install.ps1 in Windows PowerShell and the car manager through uv, as a player's install
does; a few seconds per install.ps1 call. Skipped without powershell.exe or uv.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_car_manager import archive
from tests.test_dsfcar import car_sp, dds, manifest, png

from dsfforza import REPO
from dsfforza.build import car_manager, dsfcar

INSTALL_PS1 = REPO / "tools" / "install.ps1"
LUA = Path("Common") / "UserLuaScripts"
# A player bootstrap of a release from before the rename to dsfcars (install.ps1's
# Test-OurBootstrap knows it by its first line).
OLD_BOOTSTRAP = (
    b"-- dsfforza player bootstrap: registers the added Forza cars with the game.\n"
    b'local Bootstrapper = {}\nfunction Bootstrapper.Launch() user_open("dsfforza\\\\register.lua") end\n'
    b"return Bootstrapper\n"
)
CARS = {"CAR_A_10": 40, "CAR_B_10": 3}  # the merged install's cars and IDs (its ported.lua)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def short_path(path: Path) -> str:
    """The 8.3 form of a path (C:\\Users\\RUNNER~1\\... on a CI runner, whose %TEMP% has it), or
    the path as it is where the volume makes no short names."""
    import ctypes

    size = ctypes.windll.kernel32.GetShortPathNameW(str(path), None, 0)
    buffer = ctypes.create_unicode_buffer(size)
    if not size or not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, size):
        return str(path)
    return buffer.value


def tools_missing() -> str | None:
    for tool in ("powershell", "uv"):
        if not shutil.which(tool):
            return f"no {tool} on PATH"
    return None


@unittest.skipIf(tools_missing(), tools_missing() or "")
class InstallPackagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsff-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.game = self.tmp / "game"
        self.game_arg = str(self.game)  # -GamePath as install.ps1 gets it
        (self.game / "Driver.exe").parent.mkdir(parents=True)
        (self.game / "Driver.exe").write_bytes(b"MZ")
        archive(self.game / "Install" / "dngvehicles.sp", [1, 2, 4, 62])  # "vanilla": IDs 1, 2, 4, 62
        (self.game / "Install" / "gui.sp").write_bytes(b"vanilla gui.sp")
        self.vanilla = tree(self.game)
        self.build = self.make_build(["CAR_A_10", "CAR_B_10", "CAR_C_10"])
        plugin = self.tmp / "plugin"
        plugin.mkdir()
        # A plugin that writes the model ID in memory and tells register.lua what it loaded
        # (its capability marker, tools/asi/src/patch.h).
        (plugin / "dsfcars.asi").write_bytes(
            b"plugin\0" + car_manager.CAPABILITIES_MARKER + b"archive.packages archive.ids lua.packages\0"
        )
        (plugin / "dsfcars.ini").write_text("[archive]\npackages=0\n", encoding="utf-8")
        self.asi = plugin / "dsfcars.asi"

    def make_build(self, media: list[str]) -> Path:
        """A port_batch build folder as install.ps1 reads it: dngvehicles.json and packages\\."""
        build = self.tmp / "build"
        (build / "packages").mkdir(parents=True)
        cars = []
        for n, name in enumerate(media):
            key = dsfcar.key_of("FH1", name)
            m = manifest(key=key)
            m["source"] = {"pipeline": "test", "game": "FH1", "media": name}
            sp = car_sp(self.tmp / f"{name}.sp")
            images = {dsfcar.PICTURE: dds(), dsfcar.BADGE: png(58, 58), dsfcar.PLATE: png(98, 34)}
            dsfcar.write(build / "packages" / f"{key}{dsfcar.SUFFIX}", m, sp, images)
            cars.append({"media": name, "game": "FH1", "spool_id": 700 + n})
        (build / "dngvehicles.json").write_text(json.dumps({"cars": cars}), encoding="utf-8")
        (build / "batch-report.json").write_text(json.dumps({"timings_s": {"packages": len(cars)}}), encoding="utf-8")
        return build

    def merged_install(self) -> None:
        """What install.ps1 left up to v0.1.8: the archive and gui.sp replaced (backups in
        dsfforza-backup), the plugin, the Lua and ported.lua / hud.lua created, recorded in
        install.json without a "packages" field; every name from before the rename to dsfcars."""
        backup = self.game / "dsfforza-backup"
        entries = []
        for relative, data in (("Install/dngvehicles.sp", None), ("Install/gui.sp", b"merged gui.sp")):
            target = self.game / relative
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, saved)
            original = sha(target)
            if data is None:
                archive(target, [1, 2, 4, 62, *CARS.values()])  # the merged archive
            else:
                target.write_bytes(data)
            path = relative.replace("/", "\\")
            entries.append(
                {
                    "path": path,
                    "action": "replaced",
                    "original_sha256": original,
                    "backup": path,
                    "installed_sha256": sha(target),
                }
            )
        rows = "".join(
            f'\t\t[{i}] = {{ host = 205, media = "{m}", name = "A", make = "A", model = "A", year = "", game = "FH1" }},\n'
            for m, i in CARS.items()
        )
        created = {
            "plugins/dsfforza.asi": b"old plugin",
            "plugins/dsfforza.ini": b"[archive]\n",
            "Common/UserLuaScripts/bootstrap.lua": OLD_BOOTSTRAP,
            "Common/UserLuaScripts/dsfforza/register.lua": b"-- old register.lua\n",
            "Common/UserLuaScripts/dsfforza/ported.lua": f"return {{\n\tcars = {{\n{rows}\t}},\n}}\n".encode(),
            "Common/UserLuaScripts/dsfforza/hud.lua": b"return {}\n",
        }
        for relative, data in created.items():
            target = self.game / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            entries.append(
                {
                    "path": relative.replace("/", "\\"),
                    "action": "created",
                    "original_sha256": None,
                    "backup": None,
                    "installed_sha256": sha(target),
                }
            )
        record = {
            "state": "installed", "installed": "2026-10-31T12:00:00", "build": "C:\\old\\build",
            "ids": ",".join(map(str, CARS.values())), "cars": list(CARS), "entries": entries,
            "created_dirs": ["Common\\UserLuaScripts\\dsfforza", "Common\\UserLuaScripts", "Common", "plugins"],
            "runtime_files": ["plugins\\dsfforza.log"],
        }  # fmt: skip
        (backup / "install.json").write_text(json.dumps(record, indent=1), encoding="utf-8")

    def run_ps1(self, *args: str, ok: bool = True) -> str:
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALL_PS1)]
        command += ["-GamePath", self.game_arg, *args]
        result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=REPO)
        text = result.stdout + result.stderr
        self.assertEqual(result.returncode == 0, ok, text)
        return text

    def install(self, *extra: str, ok: bool = True) -> str:
        return self.run_ps1("-Build", str(self.build), "-AsiPath", str(self.asi), "-SkipHook", *extra, ok=ok)

    def ids(self) -> dict[str, int]:
        state = json.loads((self.game / car_manager.CARS_DIR / car_manager.STATE).read_text(encoding="utf-8"))
        return {k[len("forza.fh1.") :]: c["id"] for k, c in state["cars"].items()}

    def record(self) -> dict:
        return json.loads((self.game / "dsfcars-backup" / "install.json").read_text(encoding="utf-8-sig"))

    def test_a_merged_install_moves_to_packages_on_its_ids(self):
        self.merged_install()
        hud = self.game / LUA / "dsfforza" / "hud.lua"
        hud.write_text("-- changed since its install: the merged uninstall keeps it\nreturn {}\n", encoding="utf-8")
        text = self.install()  # no -Force: the upgrade from a release that merged
        self.assertIn("Moving the merged install of 2 cars to car packages", text)
        self.assertEqual(self.ids(), {"CAR_A_10": 40, "CAR_B_10": 3, "CAR_C_10": 5})  # fresh: 3, 5, 6
        # The game's files are vanilla again; the merged install's Lua is gone.
        self.assertEqual(sha(self.game / "Install" / "dngvehicles.sp"), self.vanilla["Install\\dngvehicles.sp"])
        self.assertEqual(sha(self.game / "Install" / "gui.sp"), self.vanilla["Install\\gui.sp"])
        for name in ("ported.lua", "hud.lua"):
            self.assertFalse((self.game / LUA / "dsfcars" / name).exists(), name)
        lua = (self.game / car_manager.LUA).read_text(encoding="utf-8")
        self.assertIn('[40] = { host = 205, key = "forza.fh1.CAR_A_10"', lua)
        listed = (self.game / car_manager.CARS_DIR / car_manager.LIST).read_text(encoding="utf-8")
        self.assertIn("3 dsfcars\\cars\\forza.fh1.CAR_B_10\\car.sp", listed)
        record = self.record()
        self.assertTrue(record["packages"])
        self.assertEqual(record["state"], "installed")
        self.assertNotIn("replaced", {e["action"] for e in record["entries"]})  # no game file replaced
        ini = (self.game / "plugins" / "dsfcars.ini").read_text(encoding="utf-8")
        self.assertIn("packages=1", ini)
        self.assertEqual(
            (self.game / LUA / "dsfcars" / "register.lua").read_bytes(),
            (REPO / "tools/game/lua/dsfcars/register.lua").read_bytes(),
        )
        self.assertIn("car packages", self.run_ps1("-Status"))
        self.assertEqual(car_manager.old_layout(self.game), [])  # moved to the dsfcars names first

        # A reinstall needs -Force and keeps every car's ID.
        self.assertIn("already installed", self.install(ok=False))
        self.install("-Force")
        self.assertEqual(self.ids(), {"CAR_A_10": 40, "CAR_B_10": 3, "CAR_C_10": 5})

        # Uninstall: everything goes but the ID map, and the game's files are as before.
        (self.game / "plugins" / "dsfcars.log").write_text("the plugin's log", encoding="utf-8")
        self.run_ps1("-Uninstall")
        now = tree(self.game)
        state = now.pop(str(car_manager.CARS_DIR / car_manager.STATE))
        self.assertTrue(state)
        self.assertEqual(now, self.vanilla)
        self.assertFalse((self.game / "dsfcars-backup").exists())
        self.assertFalse((self.game / "plugins").exists())

        # A later install gives the cars the same IDs again (the save's flags are per ID).
        self.install()
        self.assertEqual(self.ids(), {"CAR_A_10": 40, "CAR_B_10": 3, "CAR_C_10": 5})

    def test_a_fresh_install_and_uninstall(self):
        self.install()
        self.assertEqual(self.ids(), {"CAR_A_10": 3, "CAR_B_10": 5, "CAR_C_10": 6})
        self.assertEqual(sha(self.game / "Install" / "dngvehicles.sp"), self.vanilla["Install\\dngvehicles.sp"])
        self.run_ps1("-Uninstall")
        now = tree(self.game)
        now.pop(str(car_manager.CARS_DIR / car_manager.STATE))
        self.assertEqual(now, self.vanilla)

    def test_uninstall_removes_a_merged_install(self):
        """-Uninstall of a merged install (no move to packages first) restores the game."""
        self.merged_install()
        self.run_ps1("-Uninstall")
        self.assertEqual(tree(self.game), self.vanilla)

    def test_refusals_change_nothing(self):
        self.merged_install()
        before = tree(self.game)
        self.run_ps1("-Build", str(self.build), "-AsiPath", str(self.tmp / "none.asi"), "-SkipHook", ok=False)
        # A plugin from before archive.ids (it would serve car.sp's builder ID): refused up front.
        older = self.tmp / "older"
        older.mkdir()
        (older / "dsfcars.asi").write_bytes(b"an older plugin, no capability list")
        shutil.copy(self.asi.with_suffix(".ini"), older / "dsfcars.ini")
        text = self.run_ps1("-Build", str(self.build), "-AsiPath", str(older / "dsfcars.asi"), "-SkipHook", ok=False)
        self.assertIn("archive.ids", text)
        self.assertEqual(tree(self.game), before)
        (self.build / "packages" / "forza.fh1.CAR_C_10.dsfcar").unlink()
        text = self.install(ok=False)
        self.assertIn("1 cars without a package", text)
        self.assertEqual(tree(self.game), before)
        for gone in ("-Packages", "-Asi", "-IdPolicy"):
            self.assertIn("parameter", self.run_ps1("-Build", str(self.build), gone, ok=False).lower())

    # --- the rename to dsfcars ---

    def age(self) -> None:
        """This install as a release from before the rename to dsfcars left it: the folders, the
        plugin's files, the record (its paths) and the plugin's list under the dsfforza names, and
        that release's bootstrap."""
        g = self.game
        (g / "dsfcars-backup").rename(g / "dsfforza-backup")
        (g / "dsfcars").rename(g / "dsfforza")
        (g / LUA / "dsfcars").rename(g / LUA / "dsfforza")
        for ext in ("asi", "ini"):
            (g / "plugins" / f"dsfcars.{ext}").rename(g / "plugins" / f"dsfforza.{ext}")
        (g / "plugins" / "dsfforza.log").write_text("the old plugin's log", encoding="utf-8")
        listed = g / "dsfforza" / "cars" / car_manager.LIST
        listed.write_text(listed.read_text(encoding="utf-8").replace("dsfcars\\", "dsfforza\\"), encoding="utf-8")
        (g / LUA / "bootstrap.lua").write_bytes(OLD_BOOTSTRAP)
        path = g / "dsfforza-backup" / "install.json"
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        for e in record["entries"]:
            e["path"] = e["path"].replace("dsfcars", "dsfforza")
            if e["path"].endswith("bootstrap.lua"):
                e["installed_sha256"] = sha(g / LUA / "bootstrap.lua")
        for field in ("runtime_files", "created_dirs"):
            record[field] = [p.replace("dsfcars", "dsfforza") for p in record[field]]
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
        self.assertEqual(len(car_manager.old_layout(g)), 5)
        # From here on install.ps1 gets the game folder in its 8.3 form where the volume has one, as
        # on a CI runner (C:\Users\RUNNER~1\...): Get-ChildItem gives long names under a short root.
        self.game_arg = short_path(self.game)

    def manager(self, *args: str) -> tuple[int, str]:
        """dsff car_manager in this process: (exit code, what it printed and its message)."""
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                car_manager.main(["--game", str(self.game), *args])
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else 1
            return code, (out.getvalue() + ("" if isinstance(stop.code, int) else str(stop.code))).strip()
        return 0, out.getvalue().strip()

    def assert_migrated(self, ids: dict[str, int]) -> None:
        g = self.game
        self.assertEqual(car_manager.old_layout(g), [])
        self.assertEqual(self.ids(), ids)
        code, problems = self.manager("verify")
        self.assertEqual(code, 0, problems)  # verify's problems, one per line
        self.assertEqual((g / car_manager.PLUGIN).read_bytes(), self.asi.read_bytes())
        self.assertIn("packages=1", (g / "plugins" / "dsfcars.ini").read_text(encoding="utf-8"))
        self.assertEqual(
            (g / LUA / "bootstrap.lua").read_bytes(), (REPO / "tools/game/lua/player/bootstrap.lua").read_bytes()
        )
        self.assertEqual(
            (g / LUA / "dsfcars" / "register.lua").read_bytes(),
            (REPO / "tools/game/lua/dsfcars/register.lua").read_bytes(),
        )
        listed = (g / car_manager.CARS_DIR / car_manager.LIST).read_text(encoding="utf-8")
        self.assertNotIn("dsfforza", listed)
        record = self.record()
        self.assertEqual(record["migration"], "done")
        live = [e for e in record["entries"] if not e.get("done")]
        self.assertFalse([e["path"] for e in live if "dsfforza" in e["path"]])
        for e in live:  # each file the record names is the one it installed
            self.assertEqual(sha(g / e["path"]), e["installed_sha256"], e["path"])
        self.assertEqual(record["runtime_files"], ["plugins\\dsfcars.log"])

    def test_an_old_named_install_migrates_on_its_ids(self):
        self.install()
        ids = self.ids()
        self.age()
        status = self.run_ps1("-Status")
        self.assertIn("Old names", status)
        self.assertIn("plugins\\dsfforza.asi: present", status)
        code, message = self.manager("status")  # the car manager names the installer and changes nothing
        self.assertEqual(code, 1)
        self.assertIn("-Migrate", message)
        before = tree(self.game)
        self.assertIn("migrated", self.run_ps1("-Migrate", "-AsiPath", str(self.asi)))
        self.assert_migrated(ids)
        self.assertNotIn("Old names", self.run_ps1("-Status"))
        # A second migration has nothing to do.
        after = tree(self.game)
        self.assertIn("Nothing to migrate", self.run_ps1("-Migrate", "-AsiPath", str(self.asi)))
        self.assertEqual(tree(self.game), after)
        self.assertNotEqual(after, before)
        # The uninstall of the migrated install leaves the game as it was but for the ID map.
        self.run_ps1("-Uninstall")
        now = tree(self.game)
        now.pop(str(car_manager.CARS_DIR / car_manager.STATE))
        self.assertEqual(now, self.vanilla)

    def test_an_interrupted_migration_finishes(self):
        self.install()
        ids = self.ids()
        # Stopped while dsfforza\ moved file by file (a folder of both names): the record already
        # moved and marked started, the old plugin deleted, one file in both folders.
        self.age()
        g = self.game
        (g / "dsfforza-backup").rename(g / "dsfcars-backup")
        path = g / "dsfcars-backup" / "install.json"
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        record["migration"] = "started"
        path.write_text(json.dumps(record), encoding="utf-8")
        for name in ("dsfforza.asi", "dsfforza.ini", "dsfforza.log"):
            (g / "plugins" / name).unlink()
        (g / car_manager.CARS_DIR).mkdir(parents=True)
        shutil.copy(g / "dsfforza" / "cars" / car_manager.STATE, g / car_manager.CARS_DIR / car_manager.STATE)
        self.run_ps1("-Migrate", "-AsiPath", str(self.asi))
        self.assert_migrated(ids)
        self.assertFalse((g / "dsfforza").exists())
        # Stopped after every folder moved, before the new plugin: the record still says started.
        record = self.record()
        record["migration"] = "started"
        (g / "dsfcars-backup" / "install.json").write_text(json.dumps(record), encoding="utf-8")
        (g / car_manager.PLUGIN).unlink()
        self.assertIn("migrated", self.run_ps1("-Migrate", "-AsiPath", str(self.asi)))
        self.assert_migrated(ids)
        # Different bytes under both names: the migration stops there and says which.
        self.age()
        (g / car_manager.CARS_DIR).mkdir(parents=True)
        (g / car_manager.CARS_DIR / car_manager.STATE).write_text("{}", encoding="utf-8")
        text = self.run_ps1("-Migrate", "-AsiPath", str(self.asi), ok=False)
        self.assertIn("both exist and differ", text)

    def test_an_old_named_install_uninstalls(self):
        self.install()
        self.age()
        self.run_ps1("-Uninstall")
        now = tree(self.game)
        self.assertTrue(now.pop(str(car_manager.CARS_DIR / car_manager.STATE)))  # the ID map, moved
        self.assertEqual(now, self.vanilla)

    def test_an_install_over_old_names_migrates_first(self):
        self.install()
        ids = self.ids()
        self.age()
        text = self.install(ok=False)  # without -Force: moved, then refused as installed
        self.assertIn("moved to the dsfcars names", text)
        self.assert_migrated(ids)
        self.install("-Force")
        self.assertEqual(self.ids(), ids)
        self.assertEqual(car_manager.old_layout(self.game), [])


if __name__ == "__main__":
    unittest.main()
