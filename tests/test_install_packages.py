"""tools/install.ps1 end to end on a synthetic game folder (no game data): car packages are
the only install, a merged install of an earlier release (up to v0.1.8) moves to packages on
the same IDs, and -Uninstall leaves the game as it was but for the car manager's ID map.

Runs install.ps1 in Windows PowerShell and the car manager through uv, as a player's install
does; a few seconds per install.ps1 call. Skipped without powershell.exe or uv.
"""

from __future__ import annotations

import hashlib
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
CARS = {"CAR_A_10": 40, "CAR_B_10": 3}  # the merged install's cars and IDs (its ported.lua)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


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
        (self.game / "Driver.exe").parent.mkdir(parents=True)
        (self.game / "Driver.exe").write_bytes(b"MZ")
        archive(self.game / "Install" / "dngvehicles.sp", [1, 2, 4, 62])  # "vanilla": IDs 1, 2, 4, 62
        (self.game / "Install" / "gui.sp").write_bytes(b"vanilla gui.sp")
        self.vanilla = tree(self.game)
        self.build = self.make_build(["CAR_A_10", "CAR_B_10", "CAR_C_10"])
        plugin = self.tmp / "plugin"
        plugin.mkdir()
        (plugin / "dsfforza.asi").write_bytes(b"plugin")
        (plugin / "dsfforza.ini").write_text("[archive]\npackages=0\n", encoding="utf-8")
        self.asi = plugin / "dsfforza.asi"

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
        install.json without a "packages" field."""
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
                {"path": path, "action": "replaced", "original_sha256": original, "backup": path,
                 "installed_sha256": sha(target)}
            )
        rows = "".join(
            f'\t\t[{i}] = {{ host = 205, media = "{m}", name = "A", make = "A", model = "A", year = "", game = "FH1" }},\n'
            for m, i in CARS.items()
        )
        created = {
            "plugins/dsfforza.asi": b"old plugin",
            "plugins/dsfforza.ini": b"[archive]\n",
            "Common/UserLuaScripts/bootstrap.lua": (REPO / "tools/game/lua/player/bootstrap.lua").read_bytes(),
            "Common/UserLuaScripts/dsfforza/register.lua": b"-- old register.lua\n",
            "Common/UserLuaScripts/dsfforza/ported.lua": f"return {{\n\tcars = {{\n{rows}\t}},\n}}\n".encode(),
            "Common/UserLuaScripts/dsfforza/hud.lua": b"return {}\n",
        }
        for relative, data in created.items():
            target = self.game / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            entries.append(
                {"path": relative.replace("/", "\\"), "action": "created", "original_sha256": None, "backup": None,
                 "installed_sha256": sha(target)}
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
        command += ["-GamePath", str(self.game), *args]
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
        return json.loads((self.game / "dsfforza-backup" / "install.json").read_text(encoding="utf-8-sig"))

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
            self.assertFalse((self.game / LUA / "dsfforza" / name).exists(), name)
        lua = (self.game / car_manager.LUA).read_text(encoding="utf-8")
        self.assertIn('[40] = { host = 205, key = "forza.fh1.CAR_A_10"', lua)
        listed = (self.game / car_manager.CARS_DIR / car_manager.LIST).read_text(encoding="utf-8")
        self.assertIn("3 dsfforza\\cars\\forza.fh1.CAR_B_10\\car.sp", listed)
        record = self.record()
        self.assertTrue(record["packages"])
        self.assertEqual(record["state"], "installed")
        self.assertNotIn("replaced", {e["action"] for e in record["entries"]})  # no game file replaced
        ini = (self.game / "plugins" / "dsfforza.ini").read_text(encoding="utf-8")
        self.assertIn("packages=1", ini)
        self.assertEqual(
            (self.game / LUA / "dsfforza" / "register.lua").read_bytes(),
            (REPO / "tools/game/lua/dsfforza/register.lua").read_bytes(),
        )
        self.assertIn("car packages", self.run_ps1("-Status"))

        # A reinstall needs -Force and keeps every car's ID.
        self.assertIn("already installed", self.install(ok=False))
        self.install("-Force")
        self.assertEqual(self.ids(), {"CAR_A_10": 40, "CAR_B_10": 3, "CAR_C_10": 5})

        # Uninstall: everything goes but the ID map, and the game's files are as before.
        (self.game / "plugins" / "dsfforza.log").write_text("the plugin's log", encoding="utf-8")
        self.run_ps1("-Uninstall")
        now = tree(self.game)
        state = now.pop(str(car_manager.CARS_DIR / car_manager.STATE))
        self.assertTrue(state)
        self.assertEqual(now, self.vanilla)
        self.assertFalse((self.game / "dsfforza-backup").exists())
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
        (self.build / "packages" / "forza.fh1.CAR_C_10.dsfcar").unlink()
        text = self.install(ok=False)
        self.assertIn("1 cars without a package", text)
        self.assertEqual(tree(self.game), before)
        for gone in ("-Packages", "-Asi", "-IdPolicy"):
            self.assertIn("parameter", self.run_ps1("-Build", str(self.build), gone, ok=False).lower())


if __name__ == "__main__":
    unittest.main()
