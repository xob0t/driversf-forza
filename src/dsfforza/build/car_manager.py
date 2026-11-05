"""Car manager: installs, enables, disables and removes car packages (.dsfcar,
src/dsfforza/build/dsfcar.py) in the game folder, for dsfcars.asi [archive] packages
(research/car-packages.md, "Manager").

    uv run dsff car_manager install out/packages/*.dsfcar [--game <game>]
    uv run dsff car_manager status
    uv run dsff car_manager disable forza.fh1.BMW_M3E30_91
    uv run dsff car_manager enable forza.fh1.BMW_M3E30_91
    uv run dsff car_manager remove forza.fh1.BMW_M3E30_91
    uv run dsff car_manager verify

What it owns, all under the game folder (no game file is written):

    dsfcars\\cars\\manager.json       its state: the key -> model ID map and the installed cars
                                     (what the manager owns of each: ID, version, enabled, the
                                     installed car.sp's SHA-256; the rest is the car's car.json)
    dsfcars\\cars\\cars.txt           the plugin's list, "id path" per enabled car
    dsfcars\\cars\\<key>\\            one car: the package's car.sp, manifest and images as they
                                     are (car.sp, car.json, *.png, *.dds). car.sp holds its
                                     builder's model ID; dsfcars.asi writes the listed one into
                                     the data it serves (research/car-packages.md)
    Common\\UserLuaScripts\\dsfcars\\packages.lua   vehicleStats entries of the enabled cars

IDs. A key keeps its model ID for good: across upgrades, disable and enable, and after a
remove (the ID stays reserved, so a new car doesn't inherit the old one's save flags,
which the game and dsfcars.asi keep per model ID). A new key takes the lowest ID the
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

STATE_FORMAT = 2  # 2: a car's manifest is its car.json only, no longer copied into the state
CAR_FIELDS = ("id", "version", "enabled", "sha256")  # what the state keeps of an installed car
CARS_DIR = Path("dsfcars") / "cars"
STATE = "manager.json"
LIST = "cars.txt"
LUA = Path("Common") / "UserLuaScripts" / "dsfcars" / "packages.lua"
PORTED = Path("Common") / "UserLuaScripts" / "dsfcars" / "ported.lua"
ARCHIVE = Path("Install") / "dngvehicles.sp"
MAX_BASE_ID = dsfcar.MAX_BASE_ID
PLUGIN = Path("plugins") / "dsfcars.asi"
# The plugin's capability marker (tools/asi/src/patch.h DSF_CAPABILITIES_MARKER): then
# space-separated names up to a NUL. A plugin from before it has none.
CAPABILITIES_MARKER = b"DSFCARS-CAPABILITIES: "
# The game-side names before the runtime was renamed to dsfcars. An install of an older
# release has them; tools\install.ps1 moves such an install to the new names (-Migrate, and
# before an install or uninstall), with every car on its ID. This manager doesn't work on
# them: it refuses (old_layout) and names the installer.
LEGACY_CAPABILITIES_MARKER = b"DSFFORZA-CAPABILITIES: "  # a plugin that reads dsfforza\cars\cars.txt
LEGACY_PARTS = (
    Path("dsfforza"),  # cars\ (manager.json, the packages, cars.txt) and generated\
    Path("Common") / "UserLuaScripts" / "dsfforza",
    Path("plugins") / "dsfforza.asi",
    Path("plugins") / "dsfforza.ini",
    Path("dsfforza-backup"),  # install.ps1's record and backups
)
# What every car this manager lists needs of the plugin, whatever its package says: car.sp
# is installed as it is, so the plugin must write the listed ID into its data; and the
# register.lua installed with this checkout registers a package car only when the plugin
# says it loaded it (lua.packages, #511).
MANAGER_NEEDS = {"archive.packages", "archive.ids", "lua.packages"}


def plugin_capabilities(plugin: Path) -> set[str] | None:
    """The capabilities an installed dsfcars.asi names in its file; None when there is no
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


def check_plugin(plugin: Path, requires, what: str) -> None:
    """SystemExit unless the plugin file has MANAGER_NEEDS and everything in `requires`."""
    caps = plugin_capabilities(plugin)
    missing = (MANAGER_NEEDS | set(requires)) - (caps or set())
    if not missing:
        return
    state = "isn't there" if caps is None else f"is older (it has {sorted(caps) or 'no capability list'})"
    if caps == set() and LEGACY_CAPABILITIES_MARKER in plugin.read_bytes():
        state = r"is from before the rename to dsfcars (it reads dsfforza\cars\)"
    raise SystemExit(
        f"{what}: {plugin} {state}; these cars need {sorted(missing)}. Update dsfcars.asi with"
        r" tools\install.ps1 (it installs the plugin and the packages of one checkout), then run this again."
        " Nothing was changed."
    )


def old_layout(game: Path) -> list[Path]:
    """The parts of an install from before the rename to dsfcars that `game` has (LEGACY_PARTS)."""
    return [part for part in LEGACY_PARTS if (Path(game) / part).exists()]


def refuse_old_layout(game: Path) -> None:
    """SystemExit when the game folder has an install of the old names: the plugin and the Lua
    of that install read dsfforza\\, so this manager's files (dsfcars\\) would reach neither."""
    parts = old_layout(game)
    if parts:
        raise SystemExit(
            f"{game} has an install from before the rename to dsfcars ({', '.join(map(str, parts))}). Move it"
            rf" first: tools\install.ps1 -GamePath '{game}' -Migrate (every car keeps its ID). Nothing was changed."
        )


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
        self._manifests: dict[str, dict | None] = {}  # key -> its car.json, read once
        path = self.cars_dir / STATE
        if path.exists():
            self.state = json.loads(path.read_text(encoding="utf-8"))
            self.state.pop("merged", None)  # a state from before packages-only installs: its IDs are in "ids"
            if self.state.get("format") == 1:
                self._upgrade_from_1(path)
            if self.state.get("format") != STATE_FORMAT:
                raise SystemExit(f"{path}: state format {self.state.get('format')}, this manager knows {STATE_FORMAT}")
        else:
            self.state = {"format": STATE_FORMAT, "ids": {}, "cars": {}}

    # --- state ---

    def _upgrade_from_1(self, path: Path) -> None:
        """Format 1 kept each car's whole manifest in the state, a copy of the car's car.json.
        Format 2 keeps what the manager owns (CAR_FIELDS) and reads the rest from car.json. The
        upgrade writes car.json from the state where a car's folder lacks it, then the state."""
        for key, car in self.state["cars"].items():
            manifest = car.get("manifest")
            folder = self.cars_dir / key
            if isinstance(manifest, dict) and folder.is_dir() and not (folder / dsfcar.MANIFEST).exists():
                _write_text(folder / dsfcar.MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            self.state["cars"][key] = {k: car[k] for k in CAR_FIELDS if k in car}
        self.state["format"] = STATE_FORMAT
        _write_text(path, json.dumps(self.state, indent=1, sort_keys=True) + "\n")
        self.log(f"{path}: state format 1 -> {STATE_FORMAT} ({len(self.state['cars'])} cars; manifests in car.json)")

    def manifest(self, key: str) -> dict | None:
        """The installed car's manifest, its folder's car.json; None when it is missing or
        isn't JSON (verify reports it, and the car is left out of the outputs)."""
        if key not in self._manifests:
            try:
                m = json.loads((self.cars_dir / key / dsfcar.MANIFEST).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                m = None
            self._manifests[key] = m if isinstance(m, dict) else None
        return self._manifests[key]

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
        """{ID: path} the plugin's list should hold: the enabled packages, less those without a
        readable car.json (packages.lua and the GUI files are made from it)."""
        return {
            c["id"]: str(CARS_DIR / k / dsfcar.CAR).replace("/", "\\")
            for k, c in self.state["cars"].items()
            if c["enabled"] and self.manifest(k) is not None
        }

    def save_state(self) -> None:
        """manager.json alone (the traffic settings' changes need no other file of save's)."""
        _write_text(self.cars_dir / STATE, json.dumps(self.state, indent=1, sort_keys=True) + "\n")

    def save(self) -> None:
        """The state, the plugin's list and packages.lua."""
        for key in self.state["cars"]:
            if self.manifest(key) is None:
                self.log(f"{key}: no readable {dsfcar.MANIFEST} in its folder; left out (install the package again)")
        _write_text(self.cars_dir / STATE, json.dumps(self.state, indent=1, sort_keys=True) + "\n")
        listed = self.listed()
        lines = ["# written by dsff car_manager; one line per enabled car: model ID, package (game folder relative)"]
        lines += [f"{i} {path}" for i, path in sorted(listed.items())]
        _write_text(self.cars_dir / LIST, "\n".join(lines) + "\n")
        by_id = {c["id"]: k for k, c in self.state["cars"].items()}
        _write_text(self.game / LUA, self.lua([(i, by_id[i]) for i in sorted(listed)]))
        if not self.defer_gui:
            self.generate_gui([(i, by_id[i]) for i in sorted(listed)])
            self.write_traffic()

    def write_traffic(self) -> None:
        """The traffic settings' files (build/traffic.py): the AI copy and its redirect line,
        and traffic.lua. Cars enabled, disabled or removed change which ports are dealt."""
        from dsfforza.build import traffic

        for r in traffic.TrafficControl(self).write():
            for note in r.notes:
                self.log(f"traffic: {r.name}: {note}")

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

    def has_gui_files(self) -> bool:
        """Whether the game's GUI files are there for car_gui to start from."""
        from dsfforza.build import car_gui

        return (self.gui_base / car_gui.GUI).exists() and (self.gui_base / car_gui.FONTS).exists()

    def require_gui_data(self) -> None:
        """Stop before a change when the GUI copies would be made without the vanilla vehicleStats
        (gui_assets.require_vanilla_stats, issue #544), so a refusal writes nothing."""
        if self.has_gui_files():
            from dsfforza.game import gui_assets

            gui_assets.require_vanilla_stats()

    def generate_gui(self, enabled: list[tuple[int, str]]) -> None:
        """The enabled cars' pictures, badges, plates and names (car_gui), when the game's GUI
        files are there to start from."""
        from dsfforza.build import car_gui

        if not self.has_gui_files():
            self.log(f"no {car_gui.GUI} and {car_gui.FONTS} in {self.gui_base}: no garage pictures, badges or plates")
            return
        cars = [{"id": i, "key": k, "manifest": self.manifest(k), "folder": self.cars_dir / k} for i, k in enabled]
        car_gui.generate(self.game, cars, base=self.gui_base, log=self.log)

    def lua(self, enabled: list[tuple[int, str]]) -> str:
        """packages.lua: what ported.lua gives register.lua per car, for the enabled packages."""
        rows = []
        for i, key in enabled:
            m = self.manifest(key)
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
        """Refuse (before anything is written) unless the installed dsfcars.asi has what the
        cars need: MANAGER_NEEDS for every car, and what their packages' `requires` list. A
        plugin from before "archive.ids" would serve car.sp's builder ID under the listed one."""
        check_plugin(self.game / PLUGIN, requires, what)

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
            self._manifests[key] = manifest
            self.save()
        except BaseException:
            # The new folder goes if it got in place; the old one comes back.
            if not staged.exists():
                shutil.rmtree(folder, ignore_errors=True)
            if moved:
                backup.replace(folder)
            shutil.rmtree(staged, ignore_errors=True)
            self._manifests.pop(key, None)  # read again from the folder that is back
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
        self._car(key)
        return list((self.manifest(key) or {}).get("requires") or [])

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
        (autosave.dat below 304, dsfcars-garage.dat above), so a later install gives every
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
        from dsfforza.build import traffic

        traffic.TrafficControl(self).clear()
        car_gui.clear(self.game)
        state = self.cars_dir / STATE
        if forget_ids:
            state.unlink(missing_ok=True)
            self.log("car packages uninstalled; the ID map is gone too (--forget-ids)")
        else:
            _write_text(state, json.dumps(self.state, indent=1, sort_keys=True) + "\n")
            self.log(f"car packages uninstalled; {len(self.state['ids'])} IDs kept in {state} for a later install")
        for folder in (self.cars_dir, self.cars_dir.parent):  # dsfcars\cars, dsfcars: when empty
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
            m = self.manifest(key)
            name = m["names"]["display"] if m else f"(no {dsfcar.MANIFEST})"
            out.append(f"{car['id']:4} {flag} {key} v{car['version']}  {name}")
        removed = sorted(set(self.state["ids"]) - set(self.state["cars"]))
        out += [f"{self.state['ids'][k]:4} --  {k} (removed; ID reserved)" for k in removed]
        return out

    def good_sha256(self, key: str) -> set[str]:
        """The checksums an installed car.sp may have: the package's own (its car.json's
        car.sha256), or the one recorded at install by a manager that wrote the car's ID
        into the file (an install from before the plugin wrote it in memory)."""
        m = self.manifest(key) or {}
        return {m.get("car", {}).get("sha256"), self.state["cars"][key]["sha256"]} - {None}

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
            elif dsfcar.sha256(sp.read_bytes()) not in self.good_sha256(key):
                out.append(f"{key}: {sp} is not the package's car.sp (changed since it was installed)")
            if self.manifest(key) is None:
                out.append(f"{key}: {self.cars_dir / key / dsfcar.MANIFEST} is missing or not JSON (left out)")
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
    c = sub.add_parser(
        "check-plugin",
        parents=[common],
        help="check a dsfcars.asi against what the packages need (install.ps1, before the game folder changes)",
    )
    c.add_argument("--plugin", type=Path, help="the plugin file (default the game's plugins\\dsfcars.asi)")
    c.add_argument("packages", type=Path, nargs="*", help=".dsfcar files or folders whose requires count too")
    add_traffic_parser(sub, common)
    sub.add_parser("gui", parents=[common], help="the car manager window (tkinter)")
    return parser


def _ints(text: str) -> list[int]:
    return [int(x) for x in text.replace(";", ",").split(",") if x.strip()]


def add_traffic_parser(sub, common) -> None:
    t = sub.add_parser(
        "traffic",
        parents=[common],
        help="who drives in traffic: cars on or off, the traffic sets, interesting vehicles",
    )
    ts = t.add_subparsers(dest="action", required=True)
    ts.add_parser("status", help="the settings and what they do per traffic set")
    for name, text in (("on", "these cars in traffic"), ("off", "these cars out of traffic (both kinds)")):
        ts.add_parser(name, help=text).add_argument(
            "selectors", nargs="+", help="a key, make:<MAKE>, game:FH1|FM4, dlc, all"
        )
    pin = ts.add_parser("pin", help="these cars always in these interesting-vehicle regions (1 to 17)")
    pin.add_argument("selectors", nargs="+")
    pin.add_argument("--regions", required=True, type=_ints)
    ts.add_parser("unpin", help="these cars back to automatic regions").add_argument("selectors", nargs="+")
    a = ts.add_parser("ambient", help="the traffic sets: ports, share, weights, the pool")
    a.add_argument("--set", dest="set_name", help="one set (e.g. 'Chapter 7'); default: every story set's default")
    a.add_argument("--ports", help="how many ports join, or keys separated by commas")
    a.add_argument("--share", type=float, help="the added models' share of every road (0..1)")
    a.add_argument("--weights", choices=["vanilla", "equal", "custom"])
    a.add_argument("--blend", type=float, help="0 = the game's weights .. 1 = equal")
    a.add_argument("--custom", help="ID_or_key=factor, separated by commas")
    a.add_argument("--vanilla-off", type=_ints, help="vanilla model IDs out of the set")
    a.add_argument("--extra", type=_ints, help="vanilla model IDs of other sets added in")
    a.add_argument("--reset", nargs="+", help="these fields back to the default's (or the game's)")
    a.add_argument("--off", action="store_true", help="no traffic set changes at all")
    ts.add_parser("shuffle", help="deal the ports to the sets again")
    iv = ts.add_parser("iv", help="interesting vehicles: ports in their regions or not")
    iv.add_argument("state", choices=["on", "off"])
    iv.add_argument("--per-region", type=int)
    iv.add_argument("--vanilla", choices=["on", "off"], help="the game's own interesting vehicles")


def run_traffic(m: Manager, args) -> None:
    from dsfforza.build import traffic

    tc = traffic.TrafficControl(m)
    act = args.action
    if act == "status":
        print("\n".join(tc.status()))
        return
    if act in ("on", "off"):
        tc.set_traffic(tc.select(args.selectors), on=act == "on")
    elif act == "pin":
        tc.pin(tc.select(args.selectors), args.regions)
    elif act == "unpin":
        tc.pin(tc.select(args.selectors), None)
    elif act == "shuffle":
        tc.shuffle()
    elif act == "iv":
        tc.t["iv"]["ports"] = args.state == "on"
        if args.vanilla is not None:
            tc.t["iv"]["vanilla"] = args.vanilla == "on"
        if args.per_region is not None:
            tc.t["iv"]["per_region"] = args.per_region
    elif act == "ambient":
        if args.off:
            tc.ambient_off()
        else:
            tc.set_ambient(args.set_name, **ambient_fields(tc, args))
    m.save_state()
    for r in tc.write():
        for note in r.notes:
            m.log(f"traffic: {r.name}: {note}")
    print("\n".join(tc.status()))


def ambient_fields(tc, args) -> dict:
    fields = {}
    if args.ports is not None:
        fields["ports"] = int(args.ports) if args.ports.isdigit() else tc.select(args.ports.split(","))
    for name in ("share", "weights", "blend", "vanilla_off", "extra"):
        if getattr(args, name) is not None:
            fields[name] = getattr(args, name)
    if args.custom:
        pairs = (x.split("=", 1) for x in args.custom.split(",") if x.strip())
        fields["custom"] = {k.strip(): float(v) for k, v in pairs}
    for name in args.reset or []:
        fields[name.replace("-", "_")] = None
    return fields


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
    if args.cmd == "check-plugin":  # reads no state and writes nothing
        paths = package_paths(args.packages)
        plugin = args.plugin or Path(args.game) / PLUGIN
        check_plugin(plugin, {r for p in paths for r in dsfcar.requires_of(p)}, "check-plugin")
        print(f"{plugin}: {' '.join(sorted(plugin_capabilities(plugin)))}")
        return
    refuse_old_layout(args.game)
    reading = args.cmd in ("status", "verify", "gui") or (args.cmd == "traffic" and args.action == "status")
    if not reading:
        from dsfforza.build.install import game_running

        if game_running(args.game):
            raise SystemExit("Driver.exe is running from this folder; close the game first")
    m = Manager(args.game, args.archive, gui_base=args.gui_base)
    if args.cmd in ("install", "seed", *KEY_COMMANDS):  # the commands that make the GUI copies
        m.require_gui_data()
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
    elif args.cmd == "traffic":
        run_traffic(m, args)
    elif args.cmd == "gui":
        from dsfforza.build import car_manager_gui

        car_manager_gui.run(m)
    elif args.cmd == "status":
        print("\n".join(m.status()) or "no car packages installed")
    else:
        problems = m.verify()
        print("\n".join(problems) or "ok")
        sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
