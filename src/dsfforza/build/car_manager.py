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
    dsfforza\\cars\\<key>.sp           a car's installed form (its ID written in)
    dsfforza\\cars\\<key>\\            the package's manifest and images (car.json, *.png, *.dds)
    Common\\UserLuaScripts\\dsfforza\\packages.lua   vehicleStats entries of the enabled cars

IDs. A key keeps its model ID for good: across upgrades, disable and enable, and after a
remove (the ID stays reserved, so a new car doesn't inherit the old one's save flags,
which the game and dsfforza.asi keep per model ID). A new key takes the lowest ID the
installed vehicle archive's SSLP doesn't use (its own cars, a merged install's or another
mod's), that isn't RESERVED (other mods' vehicleStats entries) and that no key holds, in
port_batch's "asi" order (1..303, then 304 up; base IDs stay below 1000: the LOD requests
add 1000 and 2000). A merged install's cars (ported.lua) are seeded into the map with
their IDs; their keys are "in the archive" and a package of the same car is refused.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from dsfforza.build import car_package, dsfcar

STATE_FORMAT = 1
CARS_DIR = Path("dsfforza") / "cars"
STATE = "manager.json"
LIST = "cars.txt"
LUA = Path("Common") / "UserLuaScripts" / "dsfforza" / "packages.lua"
PORTED = Path("Common") / "UserLuaScripts" / "dsfforza" / "ported.lua"
ARCHIVE = Path("Install") / "dngvehicles.sp"
MAX_BASE_ID = dsfcar.MAX_BASE_ID


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
    """{key: ID} of a merged install's cars (port_car.lua_snippet's ported.lua rows)."""
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

    def __init__(self, game: Path, archive: Path | None = None, log=print):
        self.game = Path(game)
        self.cars_dir = self.game / CARS_DIR
        self.archive = archive or self.game / ARCHIVE
        self.log = log
        path = self.cars_dir / STATE
        if path.exists():
            self.state = json.loads(path.read_text(encoding="utf-8"))
            if self.state.get("format") != STATE_FORMAT:
                raise SystemExit(f"{path}: state format {self.state.get('format')}, this manager knows {STATE_FORMAT}")
        else:
            self.state = {"format": STATE_FORMAT, "ids": {}, "cars": {}, "merged": {}}
        self._seed_merged()

    # --- state ---

    def _seed_merged(self) -> None:
        """The merged install's cars as it is now (its ported.lua): a car no longer there
        is no longer refused. Every key keeps the ID it was given (save flags are keyed by
        ID), so a merged car's ID stays reserved after the merged install goes."""
        self.state["merged"] = merged_cars(self.game / PORTED)
        for key, i in self.state["merged"].items():
            self.state["ids"].setdefault(key, i)

    def merged_conflicts(self) -> dict[str, tuple[int, int]]:
        """{key: (package ID, archive ID)}: installed packages whose car the merged install
        now has too. The archive's copy wins; the package is left out of the outputs."""
        merged = self.state["merged"]
        return {k: (c["id"], merged[k]) for k, c in self.state["cars"].items() if k in merged}

    def key_clash(self, key: str) -> str | None:
        """A known key that differs from `key` only in case: on Windows both name the same
        package folder and .sp file."""
        folded = key.casefold()
        known = set(self.state["ids"]) | set(self.state["cars"]) | set(self.state["merged"])
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
        return self.cars_dir / f"{key}.sp"

    def listed(self) -> dict[int, str]:
        """{ID: path} the plugin's list should hold: the enabled packages, less those whose car
        the merged install has too (merged_conflicts)."""
        conflicts = self.merged_conflicts()
        return {
            c["id"]: str(CARS_DIR / f"{k}.sp").replace("/", "\\")
            for k, c in self.state["cars"].items()
            if c["enabled"] and k not in conflicts
        }

    def save(self) -> None:
        """The state, the plugin's list and packages.lua."""
        for key, (pid, aid) in self.merged_conflicts().items():
            self.log(f"{key}: the merged install has this car as ID {aid}; its package (ID {pid}) is left out")
        _write_text(self.cars_dir / STATE, json.dumps(self.state, indent=1, sort_keys=True) + "\n")
        listed = self.listed()
        lines = ["# written by dsff car_manager; one line per enabled car: model ID, package (game folder relative)"]
        lines += [f"{i} {path}" for i, path in sorted(listed.items())]
        _write_text(self.cars_dir / LIST, "\n".join(lines) + "\n")
        by_id = {c["id"]: k for k, c in self.state["cars"].items()}
        _write_text(self.game / LUA, self.lua([(i, by_id[i]) for i in sorted(listed)]))

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

    # --- commands ---

    def install(self, package: Path, *, force: bool = False) -> str:
        """Install or upgrade one package; returns its key."""
        manifest, car_sp, images = dsfcar.read(package)
        key = manifest["key"]
        if key in self.state["merged"]:
            raise SystemExit(f"{key} is already in the installed archive as ID {self.state['merged'][key]}")
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
        """Stage the package next to the installed one (<key>.new, <key>.sp.new), then swap
        them in and save the state; on any failure the installed files and state are put back."""
        folder, sp = self.cars_dir / key, self.sp_path(key)
        staged, staged_sp = folder.with_name(key + ".new"), sp.with_name(sp.name + ".new")
        backup, backup_sp = folder.with_name(key + ".old"), sp.with_name(sp.name + ".old")
        for p in (staged, backup):
            shutil.rmtree(p, ignore_errors=True)
        for p in (staged_sp, backup_sp):
            p.unlink(missing_ok=True)
        staged.mkdir(parents=True)
        try:
            (staged / dsfcar.CAR).write_bytes(car_sp)
            car_package.install_sp(staged / dsfcar.CAR, entry["id"], staged_sp)
            (staged / dsfcar.CAR).unlink()
            for name, data in images.items():
                (staged / name).write_bytes(data)
            _write_text(staged / dsfcar.MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            entry["sha256"] = dsfcar.sha256(staged_sp.read_bytes())
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            staged_sp.unlink(missing_ok=True)
            raise
        moved = []
        try:
            for live, aside in ((folder, backup), (sp, backup_sp)):
                if live.exists():
                    live.replace(aside)
                    moved.append((live, aside))
            staged.replace(folder)
            staged_sp.replace(sp)
            self.state["cars"][key] = entry
            self.save()
        except BaseException:
            # Whatever of the new package is in place goes; what was moved aside comes back.
            shutil.rmtree(folder, ignore_errors=True)
            sp.unlink(missing_ok=True)
            for live, aside in moved:
                aside.replace(live)
            shutil.rmtree(staged, ignore_errors=True)
            staged_sp.unlink(missing_ok=True)
            if old is not None:
                self.state["cars"][key] = old
            else:
                self.state["cars"].pop(key, None)
            self.save()
            raise
        shutil.rmtree(backup, ignore_errors=True)
        backup_sp.unlink(missing_ok=True)

    def _car(self, key: str) -> dict:
        if key not in self.state["cars"]:
            raise SystemExit(f"{key} is not installed")
        return self.state["cars"][key]

    def set_enabled(self, key: str, *, enabled: bool) -> None:
        self._car(key)["enabled"] = enabled
        self.save()
        self.log(f"{key}: {'enabled' if enabled else 'disabled'} (ID {self.state['cars'][key]['id']})")

    def remove(self, key: str) -> None:
        car = self._car(key)
        self.sp_path(key).unlink(missing_ok=True)
        shutil.rmtree(self.cars_dir / key, ignore_errors=True)
        del self.state["cars"][key]
        self.save()
        self.log(f"{key}: removed; ID {car['id']} stays reserved for it")

    def status(self) -> list[str]:
        out = []
        for key, car in sorted(self.state["cars"].items(), key=lambda kv: kv[1]["id"]):
            flag = "on " if car["enabled"] else "off"
            out.append(f"{car['id']:4} {flag} {key} v{car['version']}  {car['manifest']['names']['display']}")
        removed = sorted(set(self.state["ids"]) - set(self.state["cars"]) - set(self.state["merged"]))
        out += [f"{self.state['ids'][k]:4} --  {k} (removed; ID reserved)" for k in removed]
        if self.state["merged"]:
            out.append(f"{len(self.state['merged'])} cars of a merged install (ported.lua) keep their IDs")
        return out

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
            elif dsfcar.sha256(sp.read_bytes()) != car["sha256"]:
                out.append(f"{key}: {sp} changed since it was installed")
            if car["id"] in in_archive:
                out.append(f"{key}: ID {car['id']} is in {self.archive} now (the plugin leaves the package out)")
        for key, (pid, aid) in self.merged_conflicts().items():
            out.append(f"{key}: the merged install has this car as ID {aid}; its package (ID {pid}) is left out")
        expected = self.listed()
        for i in sorted(set(expected) | set(listed)):
            if expected.get(i) != listed.get(i):
                out.append(
                    f"{LIST}: ID {i} lists {listed.get(i) or 'nothing'}, the state says {expected.get(i) or 'nothing'}"
                    "; run any command to write it again"
                )
        return out


def main(argv: list[str] | None = None) -> None:
    # --game and --archive before or after the command: the commands' copies default to
    # SUPPRESS, so they don't overwrite a value given before the command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--game", type=Path, default=argparse.SUPPRESS, help="the game folder (default $DSF_GAME_DIR)")
    common.add_argument(
        "--archive", type=Path, default=argparse.SUPPRESS, help="the vehicle archive to keep IDs apart from"
    )
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", type=Path, help="the game folder (default $DSF_GAME_DIR)")
    parser.add_argument("--archive", type=Path, help="the vehicle archive to keep IDs apart from (default the game's)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", parents=[common], help="install or upgrade .dsfcar packages")
    i.add_argument("packages", type=Path, nargs="+")
    i.add_argument("--force", action="store_true", help="install over a newer version")
    for name in ("enable", "disable", "remove"):
        sub.add_parser(name, parents=[common], help=f"{name} installed cars by key").add_argument("keys", nargs="+")
    sub.add_parser("status", parents=[common], help="list the installed cars")
    sub.add_parser("verify", parents=[common], help="check the installed cars")
    args = parser.parse_args(argv)
    if args.game is None:
        from dsfforza.game import game_paths

        args.game = game_paths.game_dir()
    if args.cmd in ("install", "enable", "disable", "remove"):
        from dsfforza.build.install import game_running

        if game_running(args.game):
            raise SystemExit("Driver.exe is running from this folder; close the game first")
    m = Manager(args.game, args.archive)
    if args.cmd == "install":
        for path in args.packages:
            m.install(path, force=args.force)
    elif args.cmd in ("enable", "disable"):
        for key in args.keys:
            m.set_enabled(key, enabled=args.cmd == "enable")
    elif args.cmd == "remove":
        for key in args.keys:
            m.remove(key)
    elif args.cmd == "status":
        print("\n".join(m.status()) or "no car packages installed")
    else:
        problems = m.verify()
        print("\n".join(problems) or "ok")
        sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
