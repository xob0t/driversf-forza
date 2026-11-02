"""Car manager: installs, enables, disables and removes car packages (.dsfcar,
src/dsfforza/build/dsfcar.py) in the game folder, for dsfforza.asi [archive] packages
(research/car-packages.md, "Manager").

    uv run dsff car_manager install out/packages/*.dsfcar [--game <game>]
    uv run dsff car_manager status
    uv run dsff car_manager disable forza.fh1.BMW_M3E30_91
    uv run dsff car_manager enable forza.fh1.BMW_M3E30_91
    uv run dsff car_manager remove forza.fh1.BMW_M3E30_91
    uv run dsff car_manager verify

What it owns, all under the game folder (no game file is written):

    dsfforza\\cars\\manager.json       its state: the key -> model ID map and the installed cars
    dsfforza\\cars\\cars.txt           the plugin's list, "id path" per enabled car
    dsfforza\\cars\\<key>\\            one car: the package's car.sp, manifest and images as they
                                     are (car.sp, car.json, *.png, *.dds). car.sp holds its
                                     builder's model ID; dsfforza.asi writes the listed one into
                                     the data it serves (research/car-packages.md)
    Common\\UserLuaScripts\\dsfforza\\packages.lua   vehicleStats entries of the enabled cars

IDs. A key keeps its model ID for good: across upgrades, disable and enable, and after a
remove (the ID stays reserved, so a new car doesn't inherit the old one's save flags,
which the game and dsfforza.asi keep per model ID). A new key takes the lowest ID the
installed vehicle archive's SSLP doesn't use (its own cars, a merged install's or another
mod's), that isn't RESERVED (other mods' vehicleStats entries) and that no key holds, in
port_batch's "asi" order (1..303, then 304 up; base IDs stay below 1000: the LOD requests
add 1000 and 2000). `seed` records a merged install's cars (its ported.lua, from a release
before packages) with their IDs, once, before install.ps1 removes that install: the same
cars then come back as packages on the IDs their save flags are for.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import sys
from pathlib import Path

from dsfforza.build import dsfcar

STATE_FORMAT = 1
CARS_DIR = Path("dsfforza") / "cars"
STATE = "manager.json"
LIST = "cars.txt"
LUA = Path("Common") / "UserLuaScripts" / "dsfforza" / "packages.lua"
PORTED = Path("Common") / "UserLuaScripts" / "dsfforza" / "ported.lua"
ARCHIVE = Path("Install") / "dngvehicles.sp"
MAX_BASE_ID = dsfcar.MAX_BASE_ID
PLUGIN = Path("plugins") / "dsfforza.asi"
# The plugin's capability marker (tools/asi/src/patch.h DSF_CAPABILITIES_MARKER): then
# space-separated names up to a NUL. A plugin from before it has none.
CAPABILITIES_MARKER = b"DSFFORZA-CAPABILITIES: "
# What every car this manager lists needs of the plugin, whatever its package says: car.sp
# is installed as it is, so the plugin must write the listed ID into its data.
MANAGER_NEEDS = {"archive.packages", "archive.ids"}


def plugin_capabilities(plugin: Path) -> set[str] | None:
    """The capabilities an installed dsfforza.asi names in its file; None when there is no
    plugin, an empty set for one from before the marker."""
    try:
        data = plugin.read_bytes()
    except OSError:
        return None
    at = data.find(CAPABILITIES_MARKER)
    if at < 0:
        return set()
    end = data.find(b"\0", at)
    return set(data[at + len(CAPABILITIES_MARKER) : end if end >= 0 else None].decode("ascii", "replace").split())


def reserved_ids() -> set[int]:
    from dsfforza.build.port_batch import RESERVED_IDS

    return set(RESERVED_IDS)


def id_order() -> list[int]:
    """The order free IDs are taken in: port_batch's "asi" policy (1..303, then 304 up)."""
    from dsfforza.build.port_batch import ASI_LOW_IDS

    return list(ASI_LOW_IDS) + [i for i in range(304, MAX_BASE_ID + 1) if i not in ASI_LOW_IDS]


def archive_ids(archive: Path) -> set[int]:
    """Base model IDs (kind 0 records) of a vehicle archive's SSLP; empty when there is none."""
    from dsfforza.build.clone_vehicle import read_leaf
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicles import find, parse_sslp

    if not archive.exists():
        return set()
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0, depth=3)  # root, SSIC, NONR: the SSLP leaf
        nonr = find(find(root.entries, "SSIC").children, "NONR")
        records = parse_sslp(read_leaf(stream, find(nonr.children, "SSLP")))
    return {spool_id for spool_id, _, kind in records if kind == 0}


_PORTED_ROW = re.compile(r'\[(\d+)\]\s*=\s*\{[^}]*?media\s*=\s*"([^"]+)"[^}]*?game\s*=\s*"([^"]*)"')


def merged_cars(ported: Path) -> dict[str, int]:
    """{key: ID} of a merged install's cars (port_car.lua_snippet's ported.lua rows; a merged
    install is what install.ps1 did before packages)."""
    if not ported.exists():
        return {}
    text = ported.read_text(encoding="utf-8", errors="replace")
    return {dsfcar.key_of(game or "fh1", media): int(i) for i, media, game in _PORTED_ROW.findall(text)}


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    temp.replace(path)


def _lua_string(text) -> str:
    """A Lua string literal: backslash, quote and every control character escaped (a raw
    CR or LF inside a quoted string is a Lua syntax error; \\ddd is Lua's decimal escape,
    always three digits so a following digit isn't read into it)."""
    out = []
    for ch in str(text):
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\{ord(ch):03d}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


class Manager:
    """The state of one game folder; every change writes the state and the outputs again."""

    def __init__(self, game: Path, archive: Path | None = None, log=print, gui_base: Path | None = None):
        self.game = Path(game)
        self.cars_dir = self.game / CARS_DIR
        self.archive = archive or self.game / ARCHIVE
        self.gui_base = Path(gui_base) if gui_base else self.game  # the game files car_gui starts from
        self.defer_gui = False  # batch(): the GUI copies are made once, at its end
        self.log = log
        path = self.cars_dir / STATE
        if path.exists():
            self.state = json.loads(path.read_text(encoding="utf-8"))
            if self.state.get("format") != STATE_FORMAT:
                raise SystemExit(f"{path}: state format {self.state.get('format')}, this manager knows {STATE_FORMAT}")
        else:
            self.state = {"format": STATE_FORMAT, "ids": {}, "cars": {}}
        self.state.pop("merged", None)  # a state from before packages-only installs: its IDs are in "ids"

    # --- state ---

    def key_clash(self, key: str) -> str | None:
        """A known key that differs from `key` only in case: on Windows both name the same
        package folder."""
        folded = key.casefold()
        known = set(self.state["ids"]) | set(self.state["cars"])
        return next((k for k in sorted(known) if k != key and k.casefold() == folded), None)

    def taken(self) -> set[int]:
        return archive_ids(self.archive) | reserved_ids() | set(self.state["ids"].values())

    def allocate(self, key: str) -> int:
        if key in self.state["ids"]:
            return self.state["ids"][key]
        taken = self.taken()
        for i in id_order():
            if i not in taken:
                self.state["ids"][key] = i
                return i
        raise SystemExit(f"no free model ID below {MAX_BASE_ID + 1} for {key}")

    def sp_path(self, key: str) -> Path:
        """The installed form, in the car's folder under the package's name for it."""
        return self.cars_dir / key / dsfcar.CAR

    def listed(self) -> dict[int, str]:
        """{ID: path} the plugin's list should hold: the enabled packages."""
        return {
            c["id"]: str(CARS_DIR / k / dsfcar.CAR).replace("/", "\\")
            for k, c in self.state["cars"].items()
            if c["enabled"]
        }

    def save(self) -> None:
        """The state, the plugin's list and packages.lua."""
        _write_text(self.cars_dir / STATE, json.dumps(self.state, indent=1, sort_keys=True) + "\n")
        listed = self.listed()
        lines = ["# written by dsff car_manager; one line per enabled car: model ID, package (game folder relative)"]
        lines += [f"{i} {path}" for i, path in sorted(listed.items())]
        _write_text(self.cars_dir / LIST, "\n".join(lines) + "\n")
        by_id = {c["id"]: k for k, c in self.state["cars"].items()}
        _write_text(self.game / LUA, self.lua([(i, by_id[i]) for i in sorted(listed)]))
        if not self.defer_gui:
            self.generate_gui([(i, by_id[i]) for i in sorted(listed)])

    @contextlib.contextmanager
    def batch(self):
        """Several changes, one generation of the GUI copies (gui.sp alone is 80 to 160 MB): the
        state and the plugin's list are still saved after each change, the copies at the end,
        also when a change fails (they then match what was saved)."""
        self.defer_gui = True
        try:
            yield self
        finally:
            self.defer_gui = False
            self.save()

    def generate_gui(self, enabled: list[tuple[int, str]]) -> None:
        """The enabled cars' pictures, badges, plates and names (car_gui), when the game's GUI
        files are there to start from."""
        from dsfforza.build import car_gui

        if not ((self.gui_base / car_gui.GUI).exists() and (self.gui_base / car_gui.FONTS).exists()):
            self.log(f"no {car_gui.GUI} and {car_gui.FONTS} in {self.gui_base}: no garage pictures, badges or plates")
            return
        cars = [
            {"id": i, "key": k, "manifest": self.state["cars"][k]["manifest"], "folder": self.cars_dir / k}
            for i, k in enabled
        ]
        car_gui.generate(self.game, cars, base=self.gui_base, log=self.log)

    def lua(self, enabled: list[tuple[int, str]]) -> str:
        """packages.lua: what ported.lua gives register.lua per car, for the enabled packages."""
        rows = []
        for i, key in enabled:
            m = self.state["cars"][key]["manifest"]
            n, g, src = m["names"], m.get("garage", {}), m.get("source", {})
            fields = [
                f"host = {m['car']['host']}",
                f"key = {_lua_string(key)}",
                f"media = {_lua_string(src.get('media') or key)}",
                f"name = {_lua_string(n['display'])}",
                f"make = {_lua_string(n['make'])}",
                f"model = {_lua_string(n['model'])}",
                f"year = {_lua_string(n.get('year') or '')}",
                f"game = {_lua_string(src.get('game') or '')}",
            ] + [f"{k} = {g[k]}" for k in ("speed", "strength", "drift") if k in g]
            rows.append(f"\t\t[{i}] = {{ " + ", ".join(fields) + " },")
        return "\n".join(
            [
                "-- Written by dsff car_manager (src/dsfforza/build/car_manager.py); do not edit.",
                "-- vehicleStats entries of the enabled car packages, as ported.lua's (register.lua",
                "-- copies each host's entry and sets these fields).",
                "return {",
                "\tcars = {",
                *rows,
                "\t},",
                "}",
                "",
            ]
        )

    # --- the plugin ---

    def require_plugin(self, requires, what: str) -> None:
        """Refuse (before anything is written) unless the installed dsfforza.asi has what the
        cars need: MANAGER_NEEDS for every car, and what their packages' `requires` list. A
        plugin from before "archive.ids" would serve car.sp's builder ID under the listed one."""
        needs = MANAGER_NEEDS | set(requires)
        caps = plugin_capabilities(self.game / PLUGIN)
        missing = needs - (caps or set())
        if not missing:
            return
        state = "isn't installed" if caps is None else f"is older (it has {sorted(caps) or 'no capability list'})"
        raise SystemExit(
            f"{what}: {self.game / PLUGIN} {state}; these cars need {sorted(missing)}. Update dsfforza.asi with"
            r" tools\install.ps1 -Asi -Packages (the plugin and the packages of one checkout), then run this again."
            " Nothing was changed."
        )

    # --- commands ---

    def install(self, package: Path, *, force: bool = False) -> str:
        """Install or upgrade one package; returns its key."""
        manifest, car_sp, images = dsfcar.read(package)
        key = manifest["key"]
        self.require_plugin(manifest["requires"], key)
        clash = self.key_clash(key)
        if clash:
            raise SystemExit(f"{key}: {clash} differs only in case and would share its files; not installed")
        old = self.state["cars"].get(key)
        if old and old["version"] > manifest["version"] and not force:
            raise SystemExit(f"{key}: version {old['version']} is installed, the package is {manifest['version']}")
        new_key = key not in self.state["ids"]
        model_id = self.allocate(key)
        if model_id in archive_ids(self.archive):
            if new_key:
                del self.state["ids"][key]
            raise SystemExit(f"{key}: ID {model_id} is in {self.archive} (another mod's car?); not installed")
        entry = {
            "id": model_id,
            "version": manifest["version"],
            "enabled": old["enabled"] if old else True,
            "sha256": "",
            "manifest": manifest,
        }
        try:
            self._commit(key, manifest, car_sp, images, entry, old)
        except BaseException:
            if new_key:
                self.state["ids"].pop(key, None)
            raise
        how = f"upgraded from version {old['version']}" if old else "installed"
        self.log(f"{key}: {how} as ID {model_id} ({manifest['names']['display']})")
        return key

    def _commit(self, key: str, manifest: dict, car_sp: bytes, images: dict, entry: dict, old: dict | None) -> None:
        """Stage the car's folder next to the installed one (<key>.new), then swap it in and
        save the state; on any failure the installed folder and state are put back."""
        folder = self.cars_dir / key
        staged, backup = folder.with_name(key + ".new"), folder.with_name(key + ".old")
        if backup.exists() and not folder.exists():
            backup.replace(folder)  # an earlier swap stopped half-way: the backup is the installed car
        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True)
        try:
            self._stage(staged, manifest, car_sp, images)
            entry["sha256"] = dsfcar.sha256(car_sp)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        moved = False
        try:
            if folder.exists():
                folder.replace(backup)
                moved = True
            staged.replace(folder)
            self.state["cars"][key] = entry
            self.save()
        except BaseException:
            # The new folder goes if it got in place; the old one comes back.
            if not staged.exists():
                shutil.rmtree(folder, ignore_errors=True)
            if moved:
                backup.replace(folder)
            shutil.rmtree(staged, ignore_errors=True)
            if old is not None:
                self.state["cars"][key] = old
            else:
                self.state["cars"].pop(key, None)
            self.save()
            raise
        shutil.rmtree(backup, ignore_errors=True)

    @staticmethod
    def _stage(folder: Path, manifest: dict, car_sp: bytes, images: dict) -> None:
        """The package's files as they are. car.sp keeps its builder's model ID (the plugin
        writes the listed ID into the data it serves), so it stays the package's own bytes
        and its car.sha256 checks it."""
        (folder / dsfcar.CAR).write_bytes(car_sp)
        for name, data in images.items():
            (folder / name).write_bytes(data)
        _write_text(folder / dsfcar.MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    def _car(self, key: str) -> dict:
        if key not in self.state["cars"]:
            raise SystemExit(f"{key} is not installed")
        return self.state["cars"][key]

    def requires(self, key: str) -> list[str]:
        """What an installed car's package asks of the plugin (its manifest's `requires`)."""
        return list(self._car(key)["manifest"].get("requires") or [])

    def set_enabled(self, key: str, *, enabled: bool) -> None:
        if enabled:
            self.require_plugin(self.requires(key), key)
        self._car(key)["enabled"] = enabled
        self.save()
        self.log(f"{key}: {'enabled' if enabled else 'disabled'} (ID {self.state['cars'][key]['id']})")

    def _set_aside(self, key: str) -> Path | None:
        """Move the car's folder out of the way (<key>.removed) for deleting; returns it. The
        move fails as a whole while the game has a file in it open, before anything changes."""
        folder = self.cars_dir / key
        if not folder.exists():
            return None
        gone = folder.with_name(key + ".removed")
        shutil.rmtree(gone, ignore_errors=True)  # an earlier delete's leftover
        folder.replace(gone)
        return gone

    def remove(self, key: str) -> None:
        car = self._car(key)
        gone = self._set_aside(key)
        del self.state["cars"][key]
        self.save()
        if gone:
            shutil.rmtree(gone)
        self.log(f"{key}: removed; ID {car['id']} stays reserved for it")

    def uninstall(self, *, forget_ids: bool = False) -> None:
        """Every file the manager wrote goes: the cars' folders, the plugin's
        list, the generated GUI copies, packages.lua and packages_hud.lua. The key -> ID map
        stays in manager.json (unless `forget_ids`): the save keeps its flags per model ID
        (autosave.dat below 304, dsfforza-garage.dat above), so a later install gives every
        car the ID those flags are for."""
        from dsfforza.build import car_gui

        set_aside = []
        try:
            for key in list(self.state["cars"]):
                set_aside.append(self._set_aside(key))
                del self.state["cars"][key]
        except BaseException:
            self.save()  # the cars still in place stay installed and listed
            raise
        finally:
            for gone in filter(None, set_aside):
                shutil.rmtree(gone)
        (self.cars_dir / LIST).unlink(missing_ok=True)
        (self.game / LUA).unlink(missing_ok=True)
        car_gui.clear(self.game)
        state = self.cars_dir / STATE
        if forget_ids:
            state.unlink(missing_ok=True)
            self.log("car packages uninstalled; the ID map is gone too (--forget-ids)")
        else:
            _write_text(state, json.dumps(self.state, indent=1, sort_keys=True) + "\n")
            self.log(f"car packages uninstalled; {len(self.state['ids'])} IDs kept in {state} for a later install")
        for folder in (self.cars_dir, self.cars_dir.parent):  # dsfforza\cars, dsfforza: when empty
            with contextlib.suppress(OSError):
                folder.rmdir()

    def seed(self) -> int:
        """Record a merged install's IDs (its ported.lua) in the map, so they outlive it:
        install.ps1 runs this once before it removes a merged install (an earlier release's),
        and the same cars then come back as packages on the same IDs, keeping their save flags
        (kept per model ID). A key the map already has keeps its ID. Returns the number of
        the merged install's cars."""
        merged = merged_cars(self.game / PORTED)
        for key, i in merged.items():
            self.state["ids"].setdefault(key, i)
        _write_text(self.cars_dir / STATE, json.dumps(self.state, indent=1, sort_keys=True) + "\n")
        self.log(f"{len(merged)} IDs of the merged install recorded ({len(self.state['ids'])} in the map)")
        return len(merged)

    def reassign(self, key: str) -> int:
        """A new free ID for an installed car whose ID another archive took (verify reports
        it): the plugin leaves a clashing package out. The car's save flags stay with the old
        ID, which isn't ours any more. Only the state and the outputs change: the plugin
        writes the listed ID into the car's data. Returns the new ID."""
        car = self._car(key)
        self.require_plugin(self.requires(key), key)
        old = car["id"]
        if old not in archive_ids(self.archive):
            raise SystemExit(f"{key}: ID {old} doesn't clash with {self.archive}; nothing to do")
        del self.state["ids"][key]
        try:
            new = self.allocate(key)
        except BaseException:
            self.state["ids"][key] = old
            raise
        car["id"] = new
        self.save()
        self.log(f"{key}: ID {old} -> {new} (another archive has {old}; its save flags stay with {old})")
        return new

    def status(self) -> list[str]:
        out = []
        for key, car in sorted(self.state["cars"].items(), key=lambda kv: kv[1]["id"]):
            flag = "on " if car["enabled"] else "off"
            out.append(f"{car['id']:4} {flag} {key} v{car['version']}  {car['manifest']['names']['display']}")
        removed = sorted(set(self.state["ids"]) - set(self.state["cars"]))
        out += [f"{self.state['ids'][k]:4} --  {k} (removed; ID reserved)" for k in removed]
        return out

    @staticmethod
    def good_sha256(car: dict) -> set[str]:
        """The checksums an installed car.sp may have: the package's own (its manifest's
        car.sha256), or the one recorded at install by a manager that wrote the car's ID
        into the file (an install from before the plugin wrote it in memory)."""
        return {car["manifest"]["car"]["sha256"], car["sha256"]}

    def verify(self) -> list[str]:
        """What is wrong with the installed packages (empty: all is well)."""
        out = []
        in_archive = archive_ids(self.archive)
        listed = {}
        list_path = self.cars_dir / LIST
        if list_path.exists():
            for line in list_path.read_text(encoding="utf-8").splitlines():
                parts = line.split(maxsplit=1)
                if parts and parts[0].isdigit() and len(parts) == 2:
                    listed[int(parts[0])] = parts[1].strip()
        for key, car in self.state["cars"].items():
            sp = self.sp_path(key)
            if not sp.exists():
                out.append(f"{key}: {sp} is missing")
            elif dsfcar.sha256(sp.read_bytes()) not in self.good_sha256(car):
                out.append(f"{key}: {sp} is not the package's car.sp (changed since it was installed)")
            if car["id"] in in_archive:
                out.append(f"{key}: ID {car['id']} is in {self.archive} now (the plugin leaves the package out)")
        expected = self.listed()
        for i in sorted(set(expected) | set(listed)):
            if expected.get(i) != listed.get(i):
                out.append(
                    f"{LIST}: ID {i} lists {listed.get(i) or 'nothing'}, the state says {expected.get(i) or 'nothing'}"
                    "; run any command to write it again"
                )
        return out


def build_parser() -> argparse.ArgumentParser:
    # --game and --archive before or after the command: the commands' copies default to
    # SUPPRESS, so they don't overwrite a value given before the command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--game", type=Path, default=argparse.SUPPRESS, help="the game folder (default $DSF_GAME_DIR)")
    common.add_argument(
        "--archive", type=Path, default=argparse.SUPPRESS, help="the vehicle archive to keep IDs apart from"
    )
    common.add_argument("--gui-base", type=Path, default=argparse.SUPPRESS, help="the GUI files' source folder")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", type=Path, help="the game folder (default $DSF_GAME_DIR)")
    parser.add_argument("--archive", type=Path, help="the vehicle archive to keep IDs apart from (default the game's)")
    parser.add_argument("--gui-base", type=Path, help="the folder whose GUI files the generated ones start from")
    sub = parser.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", parents=[common], help="install or upgrade .dsfcar packages")
    i.add_argument(
        "packages",
        type=Path,
        nargs="+",
        help=".dsfcar files, or folders: every .dsfcar in them (a roster's 573"
        " paths don't fit on one Windows command line)",
    )
    i.add_argument("--force", action="store_true", help="install over a newer version")
    for name, text in KEY_COMMANDS.items():
        sub.add_parser(name, parents=[common], help=text).add_argument("keys", nargs="+")
    sub.add_parser("status", parents=[common], help="list the installed cars")
    sub.add_parser("verify", parents=[common], help="check the installed cars")
    u = sub.add_parser("uninstall", parents=[common], help="remove every car package and file the manager wrote")
    u.add_argument(
        "--forget-ids", action="store_true", help="also drop the key -> ID map (save flags then may not match)"
    )
    sub.add_parser(
        "seed", parents=[common], help="record a merged install's IDs (its ported.lua) before it is uninstalled"
    )
    return parser


KEY_COMMANDS = {
    "enable": "enable installed cars by key",
    "disable": "disable installed cars by key (they keep their IDs)",
    "remove": "remove installed cars by key (their IDs stay reserved)",
    "reassign": "new IDs for installed cars whose IDs another archive took",
}


def package_paths(paths: list[Path]) -> list[Path]:
    """The .dsfcar files to install: each file as given, each folder's *.dsfcar (sorted).
    install.ps1 passes a build's packages folder, as 573 file paths are longer than a
    Windows command line can be (32767 characters)."""
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(p for p in path.glob("*" + dsfcar.SUFFIX) if p.is_file())
            if not found:
                raise SystemExit(f"{path}: no {dsfcar.SUFFIX} packages in this folder")
            out += found
        else:
            out.append(path)
    return out


def run_key_command(m: Manager, cmd: str, keys: list[str]) -> None:
    """enable, disable, remove or reassign each key; one GUI generation for all of them.
    enable and reassign check the plugin for all of them first, so a refusal writes nothing."""
    if cmd in ("enable", "reassign"):
        known = [k for k in keys if k in m.state["cars"]]
        m.require_plugin({r for k in known for r in m.requires(k)}, cmd)
    with m.batch():
        for key in keys:
            if cmd in ("enable", "disable"):
                m.set_enabled(key, enabled=cmd == "enable")
            elif cmd == "remove":
                m.remove(key)
            else:
                m.reassign(key)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.game is None:
        from dsfforza.game import game_paths

        args.game = game_paths.game_dir()
    if args.cmd not in ("status", "verify"):
        from dsfforza.build.install import game_running

        if game_running(args.game):
            raise SystemExit("Driver.exe is running from this folder; close the game first")
    m = Manager(args.game, args.archive, gui_base=args.gui_base)
    if args.cmd == "install":
        paths = package_paths(args.packages)
        # The plugin first, for every package: a refusal then writes nothing.
        m.require_plugin({r for p in paths for r in dsfcar.requires_of(p)}, "install")
        with m.batch():
            for path in paths:
                m.install(path, force=args.force)
    elif args.cmd in KEY_COMMANDS:
        run_key_command(m, args.cmd, args.keys)
    elif args.cmd == "uninstall":
        m.uninstall(forget_ids=args.forget_ids)
    elif args.cmd == "seed":
        m.seed()
    elif args.cmd == "status":
        print("\n".join(m.status()) or "no car packages installed")
    else:
        problems = m.verify()
        print("\n".join(problems) or "ok")
        sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
