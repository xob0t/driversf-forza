"""`dsff install`: build every Forza car and install it into Driver: San Francisco, in one command.

    uv run dsff install                     # check, prepare, plugin, build, pack, install
    uv run dsff install --build-only        # stop after the build (out/build)
    uv run dsff install --install-only      # install out/build (or --build DIR) as it is
    uv run dsff install --list my.txt       # other cars (--car NAME, repeatable, also works)
    uv run dsff install --status            # what is installed (tools/install.ps1 -Status)
    uv run dsff install --uninstall         # put the game back (tools/install.ps1 -Uninstall)

Stages, each skipped when there is nothing to do:

1. preflight: DSF_GAME_DIR (or --game) is Driver: San Francisco 1.04 with its stock
   vehicle archive, the Forza disc images are there (assets/*.iso or --iso), there is
   disk space, and for an install: the game isn't running, the VC++ x86 runtime,
   DriverNGHook and an ASI loader are there or can be downloaded, and no other mod's
   files would be replaced (--force replaces them; -Uninstall restores them).
2. prepare: src/dsfforza/game/game_data.py prepare, when its files under out/ are
   missing or older than the game's scripts.
3. plugin: out/asi/dsfforza.asi (tools/asi). Kept when out/asi/dsfforza.build.json
   says it was built from the current tools/asi sources, else built with
   tools/asi/build.ps1 -Test when Visual Studio Build Tools are installed, else a
   prebuilt one is used as it is. The whole roster needs it (--id-policy asi and
   more than the stock reader's 1100 root entries and 2 GiB); --no-plugin builds for
   the stock game instead (at most 215 cars, garage IDs 1..61).
4. build: src/dsfforza/build/port_batch.py build with --gui --wide-glyphs --id-policy
   asi --root-limit 3003 (tools/batches/all.txt by default) into out/build.new, reusing
   every car the last build (out/build), an interrupted one (out/build.partial) or
   --reuse DIR made from the same inputs and code, so a re-run only converts what
   changed. Without any of those, the earlier build under out/ with the most cars is
   used. When the build finishes, out/build.new becomes out/build.
5. pack: the merge into one archive, lint, garage pictures and HUD badges (same run).
6. install: the save folder (Documents/Ubisoft/Driver San Francisco/reloaded/savegames)
   is copied to out/save-backups/<time>/, then tools/install.ps1 installs the build,
   DriverNGHook, the plugin and the profile switch (-WriteProfile; --no-write-profile
   leaves it out). An earlier dsfforza install is replaced.

The terminal shows the stages, a per-car progress bar and the cars each worker is
converting; everything the tools print goes to out/logs/install-<time>.log.
Builds run at Below Normal priority; with out/gentle at Idle on two CPUs.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime
import hashlib
import json
import os
import shutil
import statistics
import struct
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from dsfforza import REPO
from dsfforza.build import gentle

# --- constants ------------------------------------------------------------------------------

STOCK_ARCHIVE_SHA256 = "0DDC885E23028F708A92A2426B643938A6934C99204C780E01755C5B7778AE35"  # Install\dngvehicles.sp 1.04
STOCK_FILES = {  # the other files install.ps1 replaces (English install), as tools/install.ps1 $StockHashes
    "Install/gui.sp": "AADB62925387307AF7E2BC5091D5E583823E553C23D4E71C5DD6BB71936DA11F",
    "media/Fonts.fchunk": "23B1724A878CB9937A764B55F7ADCF0E2D7403C0C63E8504A78566D8EE4602F2",
    "Locale/English/global.fchunk": "BA892E734B66A8AA08B98A19CEC32CE1A540792A02205D0AB3FB38BD599A96B8",
}
EXE_TIMESTAMP = 0x4EC0D92B  # Driver.exe 1.04 (tools/asi/src/patch.cpp's exe check)
EXE_IMAGE_SIZE = 0x019C0000
ASI_LOADER_NAMES = (  # tools/install.ps1 $LoaderNames
    "dinput8.dll",
    "winmm.dll",
    "version.dll",
    "d3d9.dll",
    "dsound.dll",
    "xinput1_3.dll",
    "wininet.dll",
    "winhttp.dll",
    "dinput.dll",
    "vorbisFile.dll",
    "binkw32.dll.asi",
)
INSTALL_PS1 = REPO / "tools" / "install.ps1"
ASI_SOURCE = REPO / "tools" / "asi"
ASI_OUT = REPO / "out" / "asi" / "dsfforza.asi"
BUNDLED_ASI = REPO / "plugin" / "dsfforza.asi"  # in a release zip (src/dsfforza/build/package.py)
DEFAULT_LIST = REPO / "tools" / "batches" / "all.txt"
DEFAULT_OUT = REPO / "out" / "build"
LOG_DIR = REPO / "out" / "logs"
SAVE_BACKUPS = REPO / "out" / "save-backups"
ASI_ROOT_LIMIT = 3003  # every base ID below 1000 (3 entries each) plus SSIC, DMWK, BINF
STOCK_ROOM = 215  # (1100 - 453 stock root entries) // 3: cars the stock archive reader takes
MIB = 1 << 20
GIB = 1 << 30
VS_INSTALLER = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer")
VCVARS_DEFAULT = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars32.bat")

STAGES = (
    ("preflight", "Preflight"),
    ("prepare", "Prepare"),
    ("plugin", "Plugin"),
    ("build", "Build cars"),
    ("pack", "Pack"),
    ("install", "Install"),
)


class StageError(Exception):
    """A stage that can't go on: `message` says what is wrong, `hint` what to do."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


# --- small probes (each replaceable in tests) -------------------------------------------------


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(1 << 22):
            digest.update(block)
    return digest.hexdigest().upper()


def exe_build(path: Path) -> tuple[int, int] | None:
    """(PE TimeDateStamp, SizeOfImage) of an exe, or None when it isn't a PE file."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(4096)
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe : pe + 4] != b"PE\0\0":
            return None
        stamp = struct.unpack_from("<I", data, pe + 8)[0]
        size = struct.unpack_from("<I", data, pe + 24 + 56)[0]  # OptionalHeader.SizeOfImage
        return stamp, size
    except (OSError, struct.error):
        return None


def running_processes(name: str) -> list[Path | None]:
    """The image paths of running processes called `name` (None where Windows won't say)."""
    if os.name != "nt":
        return []
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD),
            ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD),
            ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD),
            ("priority", ctypes.c_long),
            ("flags", wintypes.DWORD),
            ("exe", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    pids = []
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(Entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.exe.lower() == name.lower():
                pids.append(entry.pid)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    out: list[Path | None] = []
    for pid in pids:
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        path = None
        if handle:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                path = Path(buffer.value)
            k32.CloseHandle(handle)
        out.append(path)
    return out


def game_running(game: Path, processes=running_processes) -> bool:
    """Driver.exe runs from this game folder (one whose path Windows won't give counts too,
    as in tools/install.ps1)."""
    folder = str(Path(game).resolve()).lower()
    return any(path is None or str(path).lower().startswith(folder) for path in processes("Driver.exe"))


def vc_runtime_x86() -> bool:
    """The Visual C++ 2015-2022 x86 runtime DriverNGHook needs (tools/install.ps1 Test-VcRuntimeX86)."""
    if os.name != "nt":
        return True
    import winreg

    for key in (
        r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X86",
        r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X86",
    ):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                if winreg.QueryValueEx(handle, "Installed")[0] == 1:
                    return True
        except OSError:
            continue
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    system = windir / "SysWOW64" if (windir / "SysWOW64").is_dir() else windir / "System32"
    return (system / "vcruntime140.dll").is_file() and (system / "msvcp140.dll").is_file()


def find_asi_loader(game: Path) -> str | None:
    """The ASI loader DLL in the game folder (tools/install.ps1 Find-AsiLoader)."""
    for name in ASI_LOADER_NAMES:
        path = Path(game) / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"InitializeASI" in data or b"asi-loader" in data.lower():
            return name
    return None


def documents_dir() -> Path:
    """The user's Documents folder (wherever Windows has it, OneDrive included)."""
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class Guid(ctypes.Structure):
                _fields_ = [
                    ("a", wintypes.DWORD),
                    ("b", wintypes.WORD),
                    ("c", wintypes.WORD),
                    ("d", ctypes.c_ubyte * 8),
                ]

            documents = Guid(  # FOLDERID_Documents
                0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7)
            )
            path = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(documents), 0, None, ctypes.byref(path)) == 0:
                try:
                    return Path(path.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(path)
        except (OSError, AttributeError):
            pass
    return Path.home() / "Documents"


def default_save_dir() -> Path:
    return documents_dir() / "Ubisoft" / "Driver San Francisco" / "reloaded" / "savegames"


def free_bytes(path: Path) -> int:
    """Free space on the drive of `path` (its first existing parent)."""
    path = Path(path).resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(path).free


def drive_of(path: Path) -> str:
    return Path(path).resolve().anchor.lower()


@dataclass
class Probes:
    """What preflight asks the machine; tests swap in their own."""

    running: object = game_running
    vc_runtime: object = vc_runtime_x86
    free: object = free_bytes
    loader: object = find_asi_loader


# --- selection, jobs, reuse ---------------------------------------------------------------------


def default_jobs(cpus: int | None, gentle_on: bool) -> int:
    """Half the logical CPUs, 1..8 (each worker needs ~1.5 GB of RAM; port_batch waits for
    it), and at most gentle.CORES while out/gentle is there."""
    jobs = max(1, min(8, (cpus or 2) // 2))
    return min(jobs, gentle.CORES) if gentle_on else jobs


def count_cars(lists: list[Path], cars: list[str]) -> int:
    """Cars a --list/--car selection names (port_batch.read_list's format)."""
    from dsfforza.build.port_batch import read_list

    names = {c.partition("@")[0].lower() for c in cars}
    for path in lists:
        names |= {media.lower() for media, _ in read_list(path)}
    return len(names)


def fingerprinted_cars(build: Path) -> int:
    work = Path(build) / "work"
    return sum(1 for _ in work.glob("*.fp.json")) if work.is_dir() else 0


def earlier_build(out_root: Path, exclude: list[Path]) -> Path | None:
    """The build under out/ (a folder with work/*.fp.json and a batch-report.json) with
    the most fingerprinted cars, the newest of those, for a first `dsff install` on a
    machine that built by hand."""
    skip = {Path(p).resolve() for p in exclude}
    best, best_key = None, (0, 0.0)
    if not Path(out_root).is_dir():
        return None
    for folder in Path(out_root).iterdir():
        report = folder / "batch-report.json"
        if not folder.is_dir() or folder.resolve() in skip or not report.is_file():
            continue
        key = (fingerprinted_cars(folder), report.stat().st_mtime)
        if key[0] and key > best_key:
            best, best_key = folder, key
    return best


@dataclass
class BuildDirs:
    out: Path  # the finished build (out/build)
    staging: Path  # this run's (out/build.new)
    partial: Path  # an interrupted run's (out/build.partial)

    @classmethod
    def of(cls, out: Path) -> BuildDirs:
        out = Path(out)
        return cls(out, out.with_name(out.name + ".new"), out.with_name(out.name + ".partial"))


def reuse_sources(dirs: BuildDirs, extra: list[Path], no_reuse: bool, discover: bool = True) -> list[Path]:
    """The builds port_batch --reuse may copy cars from, in order: the last finished build,
    an interrupted one, the ones named with --reuse, else the biggest earlier build under out/."""
    if no_reuse:
        return []
    sources = [d for d in (dirs.out, dirs.partial) if (d / "work").is_dir()]
    sources += [Path(e) for e in extra if Path(e).resolve() not in {s.resolve() for s in sources}]
    if not sources and discover:
        found = earlier_build(dirs.out.parent, [dirs.out, dirs.staging, dirs.partial])
        if found is not None:
            sources.append(found)
    return sources


def start_staging(dirs: BuildDirs) -> str | None:
    """Before a build: an out/build.new left by an interrupted run becomes out/build.partial
    (reused from, deleted after the next finished build). Returns a note or None."""
    if not dirs.staging.exists():
        return None
    if not (dirs.staging / "work").is_dir():
        shutil.rmtree(dirs.staging)
        return None
    if dirs.partial.exists():
        shutil.rmtree(dirs.partial)
    dirs.staging.rename(dirs.partial)
    return f"continuing an interrupted build ({fingerprinted_cars(dirs.partial)} cars kept in {dirs.partial.name})"


def promote(dirs: BuildDirs) -> Path:
    """After a finished build: out/build.new becomes out/build (the old one and an
    interrupted one are deleted; their reused files are hard links in the new one).
    Returns the folder the build is in (out/build.new when the rename is refused)."""
    old = dirs.out.with_name(dirs.out.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    try:
        if dirs.out.exists():
            dirs.out.rename(old)
        dirs.staging.rename(dirs.out)
    except OSError:
        if old.exists() and not dirs.out.exists():
            old.rename(dirs.out)
        return dirs.staging
    shutil.rmtree(old, ignore_errors=True)
    if dirs.partial.exists():
        shutil.rmtree(dirs.partial, ignore_errors=True)
    return dirs.out


def space_needed(cars: int, reusing: bool) -> tuple[int, int]:
    """(bytes on the build drive, bytes on the game drive), roughly: the archive is about
    0.5 GiB plus 4.2 MiB a car, the garage assets 0.4 MiB a car; a converted car takes about
    25 MiB more (work/ chunk, sound previews, the extraction cache); the install copies
    the archive and backs up 0.6 GiB of stock files."""
    build = GIB // 2 + cars * 5 * MIB + (cars * 4 * MIB if reusing else cars * 25 * MIB)
    game = GIB // 2 + cars * 5 * MIB + 6 * GIB // 10
    return build, game


# --- stage decisions -------------------------------------------------------------------------------


def prepare_status(game: Path, out_root: Path) -> tuple[bool, bool, str]:
    """(run game_data prepare, with --force, why) from the files it writes under out/."""
    package = Path(game) / "media" / "ScriptsLuaScripts.fchunk"
    for variant in ("LuaScripts", "LuaScripts.copy"):
        target = Path(out_root) / "lua-src" / variant / "vehiclestats" / "vehiclestats.lua"
        if not target.is_file():
            return True, False, f"{target.relative_to(out_root).as_posix()} missing"
        if package.is_file() and package.stat().st_mtime > target.stat().st_mtime:
            return True, True, "the game's scripts are newer than vehiclestats.lua"
    for name in ("fh1", "fm4"):
        db = Path(out_root) / "db" / f"{name}_gamedb.slt"
        cached = Path(out_root) / "cache" / name / "gamedb.slt"
        if not db.is_file():
            return True, False, f"db/{db.name} missing"
        if cached.is_file() and cached.stat().st_size != db.stat().st_size:
            return True, False, f"db/{db.name} differs from the cache"
    return False, False, "up to date"


def asi_sources_digest(source: Path = ASI_SOURCE) -> str:
    """The plugin's sources (tools/asi/src, build.ps1, dsfforza.ini), line ends ignored."""
    digest = hashlib.sha256()
    files = sorted(p for p in (Path(source) / "src").rglob("*") if p.is_file())
    files += [Path(source) / "build.ps1", Path(source) / "dsfforza.ini"]
    for path in files:
        if not path.is_file():
            continue
        digest.update(path.relative_to(source).as_posix().encode() + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def stamp_path(asi: Path) -> Path:
    return Path(asi).with_name("dsfforza.build.json")


def plugin_state(asi: Path, digest: str) -> str:
    """missing, current (stamp matches the sources and the file), stale, or unverified (no stamp)."""
    asi = Path(asi)
    if not asi.is_file():
        return "missing"
    try:
        stamp = json.loads(stamp_path(asi).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unverified"
    if stamp.get("sources") == digest and stamp.get("sha256") == sha256(asi):
        return "current"
    return "stale"


def plugin_action(state: str, vs: bool, rebuild: bool = False) -> str:
    """keep (current), build, use (as it is, unverified), or missing (nothing to install)."""
    if rebuild:
        return "build" if vs else "missing"
    if state == "current":
        return "keep"
    if vs:
        return "build"
    return "missing" if state == "missing" else "use"


def find_vcvars() -> Path | None:
    """vcvars32.bat of Visual Studio (Build Tools) 2022 with the C++ tools, or None."""
    if VCVARS_DEFAULT.is_file():
        return VCVARS_DEFAULT
    vswhere = VS_INSTALLER / "vswhere.exe"
    if not vswhere.is_file():
        return None
    try:
        text = subprocess.run(
            [str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    for line in text.splitlines():
        candidate = Path(line.strip()) / "VC" / "Auxiliary" / "Build" / "vcvars32.bat"
        if candidate.is_file():
            return candidate
    return None


def read_install_record(game: Path) -> dict | None:
    """<game>/dsfforza-backup/install.json (written by tools/install.ps1), or None."""
    try:
        return json.loads((Path(game) / "dsfforza-backup" / "install.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def installed(record: dict | None) -> bool:
    return bool(record) and record.get("state") != "uninstalled"


# --- the build's progress -----------------------------------------------------------------------


@dataclass
class CarFailure:
    media: str
    cause: str
    error: str = ""
    log: str | None = None


@dataclass
class Tracker:
    """port_batch's events (port_batch.emit) folded into what the display shows."""

    clock: object = time.monotonic
    phase: str = "plan"
    total: int = 0
    reused: int = 0
    todo: int = 0
    converted: int = 0
    jobs: int = 1
    gentle: bool = False
    seconds_hint: float | None = None  # a car's conversion time in the last build
    convert_started: float | None = None
    running: dict = field(default_factory=dict)  # media -> {"started", "note", "verify"}
    failures: list = field(default_factory=list)
    rebuilt: dict = field(default_factory=dict)  # why -> count
    verify: set = field(default_factory=set)
    verify_done: int = 0
    merge_cars: int = 0
    lint_failed: int = 0
    gui_total: int = 0
    gui_done: int = 0
    gui_last: str = ""

    @property
    def stage(self) -> str:
        return "build" if self.phase in ("plan", "convert", "verify") else "pack"

    @property
    def done(self) -> int:
        return self.reused + self.converted

    def rate(self) -> float | None:
        """Converted cars per minute so far."""
        if not self.converted or self.convert_started is None:
            return None
        return self.converted / max(1e-6, self.clock() - self.convert_started) * 60

    def eta(self) -> float | None:
        """Seconds until every car is converted: the rate so far, else the last build's
        time per car over the workers."""
        left = self.todo - self.converted
        if left <= 0:
            return 0.0
        rate = self.rate()
        if rate:
            return left / rate * 60
        if self.seconds_hint:
            return left * self.seconds_hint / max(1, self.jobs)
        return None

    def handle(self, event: str, **data) -> list[tuple[str, str]]:
        """Update from one event; returns (level, text) lines to show above the display."""
        notes: list[tuple[str, str]] = []
        now = self.clock()
        if event == "plan":
            self.total = len(data.get("cars", []))
            for s in data.get("skipped", []):
                failure = CarFailure(s["media"], "not selected", s.get("error", ""))
                self.failures.append(failure)
                notes.append(("error", f"{failure.media}: {failure.error or failure.cause}"))
        elif event == "convert":
            self.phase = "convert"
            self.todo = len(data.get("todo", []))
            self.reused = len(data.get("reused", []))
            self.jobs = data.get("jobs", self.jobs)
            self.gentle = data.get("gentle", False)
            self.convert_started = now
            for why in (data.get("rebuilt") or {}).values():
                key = reason_kind(why)
                self.rebuilt[key] = self.rebuilt.get(key, 0) + 1
        elif event == "car_start":
            note = "verify" if data.get("verify") else (data.get("fallback") or "")
            self.running[data["media"]] = {"started": now, "note": note, "verify": bool(data.get("verify"))}
        elif event == "car_retry":
            notes.append(("warn", f"{data['media']}: {data.get('cause') or 'failed'}; building again {data['why']}"))
        elif event == "car_done":
            media, report = data["media"], data.get("report") or {}
            info = self.running.pop(media, {})
            if info.get("verify") or media in self.verify:
                self.verify_done += 1
                if not report.get("ok"):
                    notes.append(("error", f"{media} (reuse check): {report.get('cause')}"))
            else:
                self.converted += 1
                if not report.get("ok"):
                    failure = CarFailure(
                        media, report.get("cause") or "error", str(report.get("error") or "")[:300], data.get("log")
                    )
                    self.failures.append(failure)
                    notes.append(
                        ("error", f"{media}: {failure.cause}" + (f" (log {failure.log})" if failure.log else ""))
                    )
        elif event == "verify":
            self.phase = "verify"
            self.verify = set(data.get("cars", []))
        elif event == "merge":
            self.phase = "merge"
            self.merge_cars = data.get("cars", 0)
        elif event == "car_failed":
            failure = CarFailure(data["media"], data.get("cause") or "merge", str(data.get("error") or "")[:300])
            self.failures.append(failure)
            notes.append(("error", f"{failure.media}: {failure.cause} ({failure.error[:120]})"))
        elif event == "lint":
            self.phase = "lint"
        elif event == "lint_failed":
            self.lint_failed += 1
            notes.append(("warn", f"{data['media']}: lint found problems (kept in the archive; see the log)"))
        elif event == "gui":
            self.phase = "gui"
            self.gui_total = data.get("cars", 0)
        elif event == "gui_log":
            self.gui_done = min(self.gui_total, self.gui_done + 1) if self.gui_total else self.gui_done + 1
            self.gui_last = " ".join(str(data.get("text", "")).split()[:2]).rstrip(":")
        elif event == "gui_failed":
            notes.append(("warn", f"garage pictures and badges failed: {data.get('error')} (the cars still work)"))
        elif event == "done":
            self.phase = "done"
        return notes

    def pack_detail(self) -> str:
        if self.phase == "merge":
            return f"merging {self.merge_cars} cars into one archive"
        if self.phase == "lint":
            return "checking every car (upvh lint)"
        if self.phase == "gui":
            return f"garage pictures and badges {self.gui_done}/{self.gui_total}" + (
                f"  {self.gui_last}" if self.gui_last else ""
            )
        return ""


def reason_kind(why: str) -> str:
    """port_batch.Reuse's reason a car is converted again, in a few words."""
    for prefix, kind in (
        ("code ", "code changed"),
        ("input ", "input changed"),
        ("folder ", "input changed"),
        ("job: ", "options changed"),
        ("first try: ", "code or input changed"),
        ("Python", "Python or numpy changed"),
    ):
        if why.startswith(prefix):
            return kind
    return why


def clock_text(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def size_text(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / GIB:.2f} GiB" if n >= GIB else f"{n / MIB:.0f} MiB"


# --- the display ------------------------------------------------------------------------------------


@dataclass
class Summary:
    elapsed: float = 0.0
    build_dir: Path | None = None
    in_archive: int = 0
    built: int = 0
    reused: int = 0
    failed: list = field(default_factory=list)  # CarFailure
    archive_bytes: int | None = None
    game: Path | None = None
    replaced: list = field(default_factory=list)
    created: list = field(default_factory=list)
    save_backup: Path | None = None
    log: Path | None = None
    notes: list = field(default_factory=list)
    installed: bool = False
    uninstall: str = "uv run dsff install --uninstall"


class Reporter:
    """Stage states and the build's events; LiveReporter and PlainReporter draw them."""

    def __init__(self):
        self.states = {key: ("pending", "") for key, _ in STAGES}
        self.started = {}
        self.tracker = Tracker()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stage(self, key: str, state: str, detail: str = "") -> None:
        if state == "running" and self.states[key][0] != "running":
            self.started[key] = time.monotonic()
        self.states[key] = (state, detail)
        self.show_stage(key)

    def show_stage(self, key: str) -> None:
        pass

    def note(self, text: str, level: str = "info") -> None:
        pass

    def event(self, event: str, **data) -> None:
        before = self.tracker.stage
        for level, text in self.tracker.handle(event, **data):
            self.note(text, level)
        after = self.tracker.stage
        if event == "convert":
            t = self.tracker
            why = ", ".join(f"{n} {kind}" for kind, n in sorted(t.rebuilt.items(), key=lambda kv: -kv[1])[:3])
            self.states["build"] = (
                "running",
                f"{t.todo} to convert, {t.reused} reused"
                + (f" ({why})" if why else "")
                + f"; {t.jobs} at a time"
                + (", Idle on 2 CPUs (out/gentle)" if t.gentle else ""),
            )
        elif event == "verify":
            self.states["build"] = (
                "running",
                f"checking {len(self.tracker.verify)} reused cars by building them again",
            )
        if before == "build" and after == "pack":
            t = self.tracker
            self.stage("build", "done", f"{t.converted} converted, {t.reused} reused, {len(t.failures)} failed")
            self.stage("pack", "running", t.pack_detail())
        elif after == "pack" and self.states["pack"][0] == "running":
            self.states["pack"] = ("running", self.tracker.pack_detail())
        self.show_event(event, data)

    def show_event(self, event: str, data: dict) -> None:
        pass

    def summary(self, summary: Summary) -> None:
        pass


class PlainReporter(Reporter):
    """One line per change, ASCII only: for pipes, logs and old consoles."""

    def __init__(self, stream):
        super().__init__()
        self.stream = stream
        self.shown: dict[str, tuple[str, str]] = {}

    def line(self, text: str) -> None:
        self.stream.write(text.encode("ascii", "replace").decode("ascii") + "\n")
        self.stream.flush()

    def show_stage(self, key: str) -> None:
        state, detail = self.states[key]
        label = dict(STAGES)[key]
        before = self.shown.get(key, ("pending", ""))
        self.shown[key] = (state, detail)
        if state == "running" and before[0] == "running":
            if detail and detail != before[1]:
                self.line(f"  {detail}")
        elif state == "running":
            self.line(f"[{key}] {label}..." + (f" {detail}" if detail else ""))
        elif state in ("done", "skipped", "failed"):
            self.line(f"[{key}] {state}" + (f": {detail}" if detail else ""))

    def note(self, text: str, level: str = "info") -> None:
        prefix = {"error": "  FAILED ", "warn": "  warning: "}.get(level, "  ")
        self.line(prefix + text)

    def show_event(self, event: str, data: dict) -> None:
        t = self.tracker
        if event == "convert":
            mode = "Idle priority on 2 CPUs (out/gentle)" if t.gentle else "Below Normal priority"
            self.line(f"  {t.total} cars: {t.reused} reused, {t.todo} to convert, {t.jobs} at a time ({mode})")
        elif event == "car_start" and not data.get("verify"):
            self.line(f"  start {data['media']}")
        elif event == "car_done" and data["media"] not in t.verify:
            report = data.get("report") or {}
            state = "ok" if report.get("ok") else "failed"
            rate = t.rate()
            self.line(
                f"  done  {data['media']} {state} ({report.get('seconds') or 0:.0f} s)  "
                f"{t.converted}/{t.todo}" + (f", {rate:.1f}/min" if rate else "") + f", ETA {clock_text(t.eta())}"
            )
        elif event == "verify":
            self.line(f"  checking {len(t.verify)} reused cars by building them again")
        elif event in ("merge", "lint", "gui"):
            self.line(f"  {t.pack_detail()}")

    def summary(self, s: Summary) -> None:
        for text in summary_lines(s):
            self.line(text)


def summary_lines(s: Summary) -> list[str]:
    lines = ["", f"Finished in {clock_text(s.elapsed)}."]
    if s.build_dir is not None:
        lines.append(
            f"  Cars:      {s.in_archive} in the archive ({s.built} built, {s.reused} reused), {len(s.failed)} failed"
        )
        lines.append(f"  Archive:   {s.build_dir / 'dngvehicles.sp'} ({size_text(s.archive_bytes)})")
    for f in s.failed:
        lines.append(f"    {f.media}: {f.cause}" + (f" - {f.error[:100]}" if f.error else ""))
        if f.log:
            lines.append(f"      log {f.log}")
    if s.installed:
        lines.append(
            f"  Installed: into {s.game}: {len(s.replaced)} files replaced (originals in dsfforza-backup), "
            f"{len(s.created)} added"
        )
        if s.save_backup:
            lines.append(f"  Save:      backed up to {s.save_backup}")
        lines.append(f"  Uninstall: {s.uninstall}")
    lines += [f"  Note: {n}" for n in s.notes]
    if s.log:
        lines.append(f"  Log:       {s.log}")
    return lines


class LiveReporter(Reporter):
    """rich: the stage list, the per-car bar and the running workers, redrawn in place."""

    ICONS: ClassVar[dict[str, str]] = {
        "done": "[green]✓[/]",
        "skipped": "[dim]-[/]",
        "failed": "[red]✗[/]",
        "pending": "[dim]·[/]",
    }

    def __init__(self, console, term_fd: int | None = None):
        super().__init__()
        from rich.live import Live
        from rich.spinner import Spinner

        self.console = console
        self.term_fd = term_fd
        self.spinner = Spinner("dots", style="cyan")
        self.live = Live(
            get_renderable=self.render,
            console=console,
            refresh_per_second=4,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )

    def __enter__(self):
        self.live.start()
        return self

    def __exit__(self, *exc):
        self.live.stop()
        return False

    def note(self, text: str, level: str = "info") -> None:
        from rich.text import Text

        style = {"error": "red", "warn": "yellow"}.get(level, "")
        mark = {"error": "✗ ", "warn": "! "}.get(level, "  ")
        self.console.print(Text(mark + text, style=style))

    def render(self):
        from rich.console import Group
        from rich.progress_bar import ProgressBar
        from rich.table import Table

        if self.term_fd is not None:  # follow the window's size (fd 1 is the log file now)
            with contextlib.suppress(OSError, ValueError):
                size = os.get_terminal_size(self.term_fd)
                self.console.width, self.console.height = size.columns, size.lines
        stages = Table.grid(padding=(0, 1))
        stages.add_column(width=1)
        stages.add_column(style="bold", min_width=11)
        stages.add_column(overflow="ellipsis", no_wrap=True)
        stages.add_column(justify="right", style="dim")
        now = time.monotonic()
        for key, label in STAGES:
            state, detail = self.states[key]
            icon = self.spinner if state == "running" else self.ICONS.get(state, " ")
            elapsed = clock_text(now - self.started[key]) if state == "running" and key in self.started else ""
            stages.add_row(icon, label, f"[dim]{detail}[/]" if state != "running" else detail, elapsed)
        parts = [stages]
        t = self.tracker
        if self.states["build"][0] == "running" and t.phase in ("convert", "verify"):
            rate = t.rate()
            bar = Table.grid(padding=(0, 1))
            bar.add_column(style="bold")
            bar.add_column(width=max(10, min(40, self.console.width - 70)))
            bar.add_column(no_wrap=True)
            stats = f"{t.done}/{t.total} cars"
            stats += f" · {t.reused} reused" if t.reused else ""
            stats += f" · {rate:.1f}/min" if rate else ""
            stats += f" · ETA {clock_text(t.eta())}" if t.phase == "convert" else " · checking reused cars"
            stats += f" · [red]{len(t.failures)} failed[/]" if t.failures else ""
            bar.add_row(
                " Cars",
                ProgressBar(total=max(1, t.total), completed=t.done, complete_style="green", finished_style="green"),
                stats,
            )
            parts.append(bar)
            workers = Table.grid(padding=(0, 1))
            workers.add_column(width=3)
            workers.add_column(no_wrap=True)
            workers.add_column(justify="right", style="dim")
            workers.add_column(style="yellow", no_wrap=True, overflow="ellipsis")
            running = sorted(t.running.items(), key=lambda kv: kv[1]["started"])
            room = max(3, self.console.height - len(STAGES) - 4)  # a small window: the oldest only
            for media, info in running[:room]:
                workers.add_row("  ↳", media, clock_text(now - info["started"]), info["note"])
            queued = t.todo - t.converted - sum(1 for i in t.running.values() if not i["verify"])
            more = f"{len(running) - room} more running, " if len(running) > room else ""
            if queued > 0 or more:
                workers.add_row("", f"[dim]{more}{max(0, queued)} queued, {t.jobs} at a time[/]", "", "")
            parts.append(workers)
        return Group(*parts)

    def summary(self, s: Summary) -> None:
        from rich.table import Table
        from rich.text import Text

        self.console.print()
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold")
        grid.add_column()
        if s.build_dir is not None:
            failed = f", [red]{len(s.failed)} failed[/]" if s.failed else ", 0 failed"
            grid.add_row("Cars", f"{s.in_archive} in the archive ({s.built} built, {s.reused} reused){failed}")
            grid.add_row("Archive", f"{s.build_dir / 'dngvehicles.sp'} ({size_text(s.archive_bytes)})")
        if s.installed:
            grid.add_row(
                "Installed",
                f"into {s.game}: {len(s.replaced)} files replaced (originals in dsfforza-backup), "
                f"{len(s.created)} added",
            )
            if s.save_backup:
                grid.add_row("Save backup", str(s.save_backup))
            grid.add_row("Uninstall", s.uninstall)
        for n in s.notes:
            grid.add_row("[yellow]Note[/]", n)
        if s.log:
            grid.add_row("Log", str(s.log))
        self.console.print(Text(f"Finished in {clock_text(s.elapsed)}", style="bold green"))
        self.console.print(grid)
        if s.failed:
            table = Table(title="Failed cars", title_justify="left", show_edge=False, header_style="bold red")
            table.add_column("car")
            table.add_column("cause")
            table.add_column("log", overflow="fold")
            for f in s.failed:
                table.add_row(f.media, f.cause + (f": {f.error[:80]}" if f.error else ""), f.log or "")
            self.console.print(table)


# --- output redirection -----------------------------------------------------------------------------


def enable_vt(fd: int) -> bool:
    """ANSI escape sequences on the console behind fd (Windows 10+); False when it can't."""
    if os.name != "nt":
        return True
    try:
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(fd)
        mode = wintypes.DWORD()
        k32 = ctypes.windll.kernel32
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (OSError, AttributeError, ValueError):
        return False


@contextlib.contextmanager
def to_log(log_path: Path):
    """Point stdout and stderr (the file descriptors, so child processes too) at the log
    file; yields (a text stream to the terminal, its fd)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout.flush()
    sys.stderr.flush()
    log = log_path.open("a", encoding="utf-8", buffering=1, errors="replace")
    term_fd = os.dup(1)
    err_fd = os.dup(2)
    term = open(term_fd, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)  # noqa: SIM115
    saved = sys.stdout, sys.stderr
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    sys.stdout = sys.stderr = log
    try:
        yield term, term_fd
    finally:
        with contextlib.suppress(OSError, ValueError):
            log.flush()
            term.flush()
        os.dup2(term_fd, 1)
        os.dup2(err_fd, 2)
        sys.stdout, sys.stderr = saved
        os.close(term_fd)
        os.close(err_fd)
        log.close()


def make_reporter(term, term_fd: int, plain: bool) -> Reporter:
    if plain or not term.isatty() or not enable_vt(term_fd):
        return PlainReporter(term)
    from rich.console import Console

    width = 100
    with contextlib.suppress(OSError):
        width = os.get_terminal_size(term_fd).columns
    console = Console(file=term, force_terminal=True, legacy_windows=False, width=width, highlight=False)
    return LiveReporter(console, term_fd)


# --- the run -------------------------------------------------------------------------------------------


def log(text: str) -> None:
    """A line for the log file (stdout points there while a run is on)."""
    print(f"[dsff install {datetime.datetime.now():%H:%M:%S}] {text}", flush=True)


@dataclass
class Run:
    args: argparse.Namespace
    reporter: Reporter
    probes: Probes = field(default_factory=Probes)
    game: Path | None = None
    isos: list = field(default_factory=list)
    stock_archive: Path | None = None
    record: dict | None = None
    plugin: Path | None = None
    build_dir: Path | None = None
    summary: Summary = field(default_factory=Summary)

    @property
    def building(self) -> bool:
        return not self.args.install_only

    @property
    def installing(self) -> bool:
        return not self.args.build_only

    # 1. preflight ------------------------------------------------------------------------------

    def preflight(self) -> str:
        args, problems, info = self.args, [], []
        game = args.game or (Path(os.environ["DSF_GAME_DIR"]) if os.environ.get("DSF_GAME_DIR") else None)
        if game is None:
            raise StageError(
                "DSF_GAME_DIR is not set.",
                "Set it to the Driver: San Francisco folder (the one with Driver.exe), e.g. "
                "$env:DSF_GAME_DIR = 'D:\\Games\\Driver San Francisco' (setx DSF_GAME_DIR ... keeps it), or pass --game.",
            )
        game = Path(game)
        if not (game / "Driver.exe").is_file():
            raise StageError(f"No Driver.exe in {game}.", "DSF_GAME_DIR (or --game) must be the game folder.")
        self.game = game
        build = exe_build(game / "Driver.exe")
        if build != (EXE_TIMESTAMP, EXE_IMAGE_SIZE):
            text = f"{game / 'Driver.exe'} is not Driver: San Francisco 1.04" + (
                f" (PE timestamp {build[0]:#x}, image size {build[1]:#x})" if build else ""
            )
            if not args.no_plugin:
                problems.append(text + "; dsfforza.asi only patches 1.04. Update the game to 1.04.")
            else:
                info.append("not 1.04?")
        from dsfforza.game import game_paths

        self.record = read_install_record(game)
        self.stock_archive = game_paths.stock(game / "Install" / "dngvehicles.sp", quiet=True)
        if not self.stock_archive.is_file():
            problems.append(f"{game / 'Install' / 'dngvehicles.sp'} is missing.")
        elif sha256(self.stock_archive) != STOCK_ARCHIVE_SHA256:
            problems.append(
                f"{self.stock_archive} is not the stock 1.04 vehicle archive (another mod?). "
                "Restore the original (Steam/Uplay: verify files) and run again."
            )
        else:
            info.append("DSF 1.04")
        cars = None
        if self.building:
            self.isos = [Path(p) for p in (args.iso or sorted((REPO / "assets").glob("*.iso")))]
            missing = [p for p in self.isos if not p.is_file()]
            if not self.isos:
                problems.append(
                    "No Forza disc images: put the FH1 and FM4 (disc 1 and 2) .iso files in "
                    f"{REPO / 'assets'}, or pass --iso FILE for each."
                )
            elif missing:
                problems.append(f"Disc image not found: {', '.join(map(str, missing))}")
            else:
                info.append(f"{len(self.isos)} disc images")
                if len(self.isos) < 3:
                    info.append("cars on a missing disc are skipped")
            lists = self.lists()
            gone = [p for p in lists if not Path(p).is_file()]
            if gone:
                problems.append(f"Car list not found: {', '.join(map(str, gone))}")
            cars = count_cars([p for p in lists if p not in gone], args.car or [])
            if args.no_plugin and cars > STOCK_ROOM:
                problems.append(
                    f"--no-plugin: the stock game reads at most {STOCK_ROOM} added cars, the selection has {cars}. "
                    "Pass a shorter --list, or leave out --no-plugin."
                )
        if self.installing:
            problems += self.install_checks()
        # disk space: the build folder's drive and the game's
        cars = cars if cars is not None else len(self.installed_cars())
        reusing = bool(reuse_sources(BuildDirs.of(args.out), args.reuse or [], args.no_reuse))
        need_build, need_game = space_needed(cars, reusing)
        needs: dict[str, tuple[Path, int]] = {}
        if self.building:
            needs[drive_of(args.out)] = (args.out, need_build)
        if self.installing:
            path, n = needs.get(drive_of(game), (game, 0))
            needs[drive_of(game)] = (path, n + need_game)
        for path, need in needs.values():
            free = self.probes.free(path)
            if free < need:
                problems.append(
                    f"Not enough disk space on {Path(path).resolve().anchor}: about {size_text(need)} needed, "
                    f"{size_text(free)} free."
                )
            else:
                info.append(f"{size_text(free)} free on {Path(path).resolve().anchor.rstrip(chr(92))}")
        if problems:
            raise StageError("\n".join(f"- {p}" for p in problems))
        return ", ".join(info)

    def lists(self) -> list[Path]:
        if self.args.list:
            return list(self.args.list)
        return [] if self.args.car else [DEFAULT_LIST]

    def installed_cars(self) -> list:
        try:
            build = self.args.build or self.args.out
            return json.loads((Path(build) / "dngvehicles.json").read_text(encoding="utf-8"))["cars"]
        except (OSError, ValueError, KeyError):
            return []

    def install_checks(self) -> list[str]:
        """What tools/install.ps1 would stop at, checked before an hour of building."""
        args, game, problems = self.args, self.game, []
        if self.probes.running(game):
            problems.append("Driver.exe is running from the game folder: close the game first.")
        ours = installed(self.record)
        if not args.skip_hook:
            if not self.probes.vc_runtime():
                problems.append(
                    "DriverNGHook needs the Microsoft Visual C++ 2015-2022 Redistributable, x86 (32-bit): "
                    "https://aka.ms/vs/17/release/vc_redist.x86.exe"
                )
            from dsfforza.build import thirdparty

            if args.hook_archive and not Path(args.hook_archive).is_file():
                problems.append(f"--hook-archive {args.hook_archive}: no such file.")
            hook = self.game_has_hook() or args.hook_archive or thirdparty.ready("dnghook")
            if not hook and args.no_download:
                tool = thirdparty.TOOLS["dnghook"]
                problems.append(
                    f"DriverNGHook {tool.version} is needed: download {tool.url} and pass --hook-archive FILE "
                    "(or leave out --no-download)."
                )
            loader = self.game_has_loader() or args.asi_loader or thirdparty.ready("asi-loader")
            if not loader and args.no_download:
                problems.append(
                    f"No ASI loader in the game folder: download {thirdparty.TOOLS['asi-loader'].url} and pass "
                    "--asi-loader FILE (or leave out --no-download)."
                )
        if not ours and not args.force:
            bootstrap = game / "Common" / "UserLuaScripts" / "bootstrap.lua"
            ours_lua = REPO / "tools" / "game" / "lua" / "player" / "bootstrap.lua"
            if bootstrap.is_file() and sha256(bootstrap) != sha256(ours_lua):
                problems.append(
                    "Common\\UserLuaScripts\\bootstrap.lua belongs to another mod. Add --force to replace it "
                    "(it is backed up and put back by --uninstall)."
                )
            for relative, digest in STOCK_FILES.items():
                path = game / relative
                if path.is_file() and sha256(path) != digest:
                    problems.append(
                        f"{relative} is not the stock 1.04 file (another mod?). Add --force to replace it "
                        "(it is backed up and put back by --uninstall), or restore the original first."
                    )
        return problems

    # 2. prepare ------------------------------------------------------------------------------------

    def prepare(self) -> tuple[str, str]:
        """game_data prepare when needed, and vgmstream (Forza's engine sound) when missing."""
        from dsfforza.build import thirdparty

        needed, force, why = prepare_status(self.game, REPO / "out")
        done = []
        if needed:
            from dsfforza.game import game_data

            log(f"game_data prepare ({why})")
            self.reporter.stage("prepare", "running", f"game data ({why})")
            game_data.prepare(self.game, self.isos, log=lambda t: log(f"prepare: {t}"), force=force)
            done.append(f"game data written ({why})")
        tool = thirdparty.TOOLS["vgmstream"]
        if not thirdparty.ready("vgmstream"):
            if self.args.no_download:
                self.summary.notes.append(
                    f"vgmstream is not in {thirdparty.TOOLS_DL} (--no-download): the cars keep their host's engine sound"
                )
            else:
                self.reporter.stage("prepare", "running", f"downloading {tool.title} {tool.version}")
                try:
                    thirdparty.fetch("vgmstream", log=log)
                except thirdparty.DownloadError as error:
                    raise StageError(str(error), "Check the internet connection, or run with --no-download.") from error
                done.append(f"{tool.title} {tool.version} downloaded")
        if not done:
            return "skipped", "game data and tools up to date"
        return "done", "; ".join(done)

    # 3. plugin ----------------------------------------------------------------------------------------

    def plugin_stage(self) -> tuple[str, str]:
        args = self.args
        if args.no_plugin:
            return "skipped", "--no-plugin: stock limits, no dsfforza.asi"
        if args.asi:
            if not Path(args.asi).is_file():
                raise StageError(f"--asi {args.asi}: no such file.")
            self.plugin = Path(args.asi)
            return "done", f"{self.plugin} (--asi)"
        if not self.installing:
            return "skipped", "not needed for the build"
        if BUNDLED_ASI.is_file() and not args.rebuild_plugin:
            self.plugin = BUNDLED_ASI
            return "skipped", f"{rel(BUNDLED_ASI)} (comes with this release)"
        if not (REPO / ".git").exists() and not args.rebuild_plugin:
            raise StageError(
                f"This copy has no dsfforza.asi ({rel(BUNDLED_ASI)}).",
                "Download the release zip again (it holds the plugin), pass --asi FILE, or --no-plugin.",
            )
        digest = asi_sources_digest()
        state = plugin_state(ASI_OUT, digest)
        vcvars = find_vcvars()
        action = plugin_action(state, vcvars is not None, args.rebuild_plugin)
        log(f"plugin: {state}, Visual Studio {'found' if vcvars else 'not found'} -> {action}")
        if action == "keep":
            self.plugin = ASI_OUT
            return "skipped", f"{rel(ASI_OUT)} is up to date"
        if action == "use":
            self.plugin = ASI_OUT
            self.summary.notes.append(
                f"{rel(ASI_OUT)} could not be checked against tools/asi ({state}); Visual Studio Build Tools "
                "would rebuild it."
            )
            return "done", f"{rel(ASI_OUT)} (prebuilt, {state}: no Visual Studio to rebuild it)"
        if action == "missing":
            raise StageError(
                "dsfforza.asi is needed for the whole roster (--id-policy asi, an archive past the stock 1100 entries "
                "and 2 GiB), and it can't be built here: Visual Studio 2022 Build Tools with the C++ workload "
                "were not found.",
                "Install them (winget install Microsoft.VisualStudio.2022.BuildTools --override "
                '"--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"), or copy a '
                f"built dsfforza.asi to {ASI_OUT.parent} (or pass --asi FILE), or build for the stock game: "
                f"--no-plugin with a --list of at most {STOCK_ROOM} cars.",
            )
        self.build_plugin(vcvars)
        self.plugin = ASI_OUT
        return "done", f"built {rel(ASI_OUT)} and tested it against Driver.exe"

    def build_plugin(self, vcvars: Path) -> None:
        env = {**os.environ, "DSF_GAME_DIR": str(self.game)}
        env["PATH"] = str(VS_INSTALLER) + os.pathsep + env.get("PATH", "")
        command = [
            powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ASI_SOURCE / "build.ps1"),
            "-VcVars",
            str(vcvars),
            "-Test",
        ]
        log("plugin: " + " ".join(command))
        result = subprocess.run(command, env=env, cwd=str(REPO), check=False)
        if result.returncode != 0 or not ASI_OUT.is_file():
            raise StageError(
                f"tools\\asi\\build.ps1 -Test failed (exit code {result.returncode}).",
                f"See the log ({self.summary.log}); run .\\tools\\asi\\build.ps1 -Test by hand for details.",
            )
        stamp_path(ASI_OUT).write_text(
            json.dumps({"sources": asi_sources_digest(), "sha256": sha256(ASI_OUT)}, indent=1) + "\n", encoding="utf-8"
        )

    # 4-5. build and pack -------------------------------------------------------------------------------

    def build(self) -> None:
        from dsfforza.build import port_batch
        from dsfforza.game import game_paths

        args = self.args
        dirs = BuildDirs.of(args.out)
        note = start_staging(dirs)
        if note:
            self.reporter.note(note)
        sources = reuse_sources(dirs, args.reuse or [], args.no_reuse)
        hint = None
        if sources:
            self.reporter.stage("build", "running", "reusing cars from " + ", ".join(rel(s) for s in sources))
            hint = seconds_per_car(sources[0])
        argv = ["build", "--out", str(dirs.staging), "--dsf", str(self.stock_archive), "--gui"]
        if args.no_plugin:
            argv += ["--id-policy", "garage"]
        else:
            argv += ["--id-policy", "asi", "--root-limit", str(ASI_ROOT_LIMIT), "--wide-glyphs"]
        for path in self.lists():
            argv += ["--list", str(path)]
        for car in args.car or []:
            argv += ["--car", car]
        for iso in self.isos:
            argv += ["--iso", str(iso)]
        for source in sources:
            argv += ["--reuse", str(source)]
        argv += ["--jobs", str(args.jobs), "--verify-reuse", str(args.verify_reuse if sources else 0)]
        build_args = port_batch.build_parser().parse_args(argv)
        build_args.dsf = game_paths.stock(build_args.dsf, quiet=True)
        build_args.progress = self.reporter.event
        self.reporter.tracker.seconds_hint = hint
        os.environ["DSF_GAME_DIR"] = str(self.game)
        log("port_batch " + " ".join(argv))
        try:
            port_batch.cmd_build(build_args)
        except SystemExit as error:
            raise StageError(
                f"The build stopped: {error.code}",
                f"The cars built so far are kept in {dirs.staging} and reused by the next run.",
            ) from error
        self.build_dir = promote(dirs)
        if self.build_dir != dirs.out:
            self.summary.notes.append(f"{dirs.out} could not be replaced (in use?); this build is in {self.build_dir}")
        self.read_report(self.build_dir)

    def read_report(self, build: Path) -> None:
        s = self.summary
        s.build_dir = build
        try:
            report = json.loads((build / "batch-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        reused = set((report.get("reuse") or {}).get("reused") or [])
        ok = [c["media"] for c in report.get("ok", [])]
        s.in_archive = len(ok)
        s.reused = sum(1 for m in ok if m in reused)
        s.built = len(ok) - s.reused
        s.archive_bytes = report.get("archive_bytes")
        s.failed = []
        for f in report.get("failed", []):
            log_path = build / "work" / f"{f['media']}.log"
            s.failed.append(
                CarFailure(
                    f["media"],
                    f.get("cause") or "?",
                    str(f.get("error") or "")[:300],
                    str(log_path) if log_path.is_file() else None,
                )
            )
        if (report.get("timings_s") or {}).get("gui_error"):
            s.notes.append(f"garage pictures and badges failed: {report['timings_s']['gui_error']}")

    # 6. install ---------------------------------------------------------------------------------------

    def ours(self) -> set[str]:
        """Game files (lower case, backslashes) the recorded dsfforza install created: a
        reinstall removes them first (install.ps1 -Force), so they don't count as there."""
        if not installed(self.record):
            return set()
        return {
            str(e.get("path", "")).lower().replace("/", "\\")
            for e in self.record.get("entries", [])
            if isinstance(e, dict) and e.get("action") == "created"
        }

    def game_has_hook(self) -> bool:
        hook = self.game / "plugins" / "DriverNGHook.asi"
        return hook.is_file() and "plugins\\DriverNGHook.asi".lower() not in self.ours()

    def game_has_loader(self) -> bool:
        name = self.probes.loader(self.game)
        return bool(name) and name.lower() not in self.ours()

    def hook_files(self) -> tuple[Path | None, Path | None]:
        """(DNGHook archive, ASI loader) for install.ps1: given, else the pinned downloads
        (src/dsfforza/build/thirdparty.py) when the game doesn't have them yet."""
        from dsfforza.build import thirdparty

        args = self.args
        if args.skip_hook:
            return None, None
        hook, loader = args.hook_archive, args.asi_loader
        try:
            if not hook and not self.game_has_hook():
                self.reporter.stage("install", "running", "DriverNGHook")
                hook = thirdparty.fetch("dnghook", log=log)
            if not loader and not self.game_has_loader():
                self.reporter.stage("install", "running", "Ultimate ASI Loader")
                loader = thirdparty.fetch("asi-loader", log=log)
        except thirdparty.DownloadError as error:
            raise StageError(
                str(error), "Check the internet connection, or pass --hook-archive / --asi-loader."
            ) from error
        return hook, loader

    def backup_save(self) -> Path | None:
        save = Path(self.args.save_dir) if self.args.save_dir else default_save_dir()
        if not save.is_dir():
            log(f"no save folder at {save}; nothing to back up")
            return None
        target = SAVE_BACKUPS / f"{datetime.datetime.now():%Y%m%d-%H%M%S}" / save.name
        shutil.copytree(save, target)
        log(f"save folder {save} copied to {target}")
        return target

    def install(self) -> str:
        args, game = self.args, self.game
        build = Path(args.build) if args.build else (self.build_dir or args.out)
        for name in ("dngvehicles.sp", "dngvehicles.json", "dngvehicles.lua"):
            if not (build / name).is_file():
                raise StageError(
                    f"No finished build in {build} ({name} missing).",
                    "Run `uv run dsff install` (or --build-only) first, or pass --build DIR.",
                )
        if self.build_dir is None:
            self.read_report(build)
        if self.probes.running(game):
            raise StageError("Driver.exe is running from the game folder.", "Close the game and run --install-only.")
        try:
            policy = json.loads((build / "dngvehicles.json").read_text(encoding="utf-8"))["batch"]["id_policy"]
        except (OSError, ValueError, KeyError, TypeError):
            policy = "garage" if args.no_plugin else "asi"
        if policy == "asi" and self.plugin is None:
            raise StageError(f"{build} was built for dsfforza.asi (--id-policy asi), but --no-plugin was given.")
        self.reporter.stage("install", "running", "backing up the save")
        self.summary.save_backup = None if args.no_save_backup else self.backup_save()
        hook, loader = self.hook_files()
        self.reporter.stage("install", "running", "tools/install.ps1: backing up and copying the game files")
        command = [
            powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALL_PS1),
            "-GamePath",
            str(game),
            "-Build",
            str(build),
            "-IdPolicy",
            policy,
        ]
        if self.plugin is not None:
            command += ["-Asi", "-AsiPath", str(self.plugin)]
        if hook:
            command += ["-HookArchive", str(hook)]
        if loader:
            command += ["-AsiLoader", str(loader)]
        if args.skip_hook:
            command += ["-SkipHook"]
        if not args.no_write_profile:
            command += ["-WriteProfile"]
        if args.force or installed(self.record):
            command += ["-Force"]  # replaces the earlier dsfforza install (and, with --force, other mods' files)
        log("install: " + " ".join(command))
        result = subprocess.run(command, cwd=str(REPO), capture_output=True, text=True, check=False)
        print(result.stdout, end="")
        print(result.stderr, end="")
        if result.returncode != 0:
            message = next(
                (line.strip() for line in result.stderr.splitlines() if line.strip()), f"exit code {result.returncode}"
            )
            raise StageError(f"tools/install.ps1 failed: {message}", f"The full output is in {self.summary.log}.")
        record = read_install_record(game) or {}
        entries = [e for e in record.get("entries", []) if isinstance(e, dict)]
        s = self.summary
        s.game, s.installed = game, True
        s.uninstall = uninstall_command(args.game)
        s.replaced = [e["path"] for e in entries if e.get("action") == "replaced"]
        s.created = [e["path"] for e in entries if e.get("action") == "created"]
        return f"{len(record.get('cars') or [])} cars into {game}"


def uninstall_command(game: Path | None) -> str:
    """How to undo the install: install.cmd (the release launcher sets DSFF_LAUNCHER) or uv."""
    command = r".\install.cmd --uninstall" if os.environ.get("DSFF_LAUNCHER") else "uv run dsff install --uninstall"
    return command + (f' --game "{game}"' if game else "")


def rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def seconds_per_car(build: Path) -> float | None:
    """The median conversion time of a car in an earlier build (batch-report.json), for the
    first ETA before this run has finished a car."""
    try:
        report = json.loads((Path(build) / "batch-report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    times = [c["seconds"] for c in report.get("ok", []) if c.get("seconds")]
    return statistics.median(times) if times else None


def powershell() -> str:
    found = shutil.which("powershell")
    if found:
        return found
    return str(
        Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )


def run_stage(run: Run, key: str, step) -> None:
    """Run one stage: its function returns a detail text, or (state, detail)."""
    reporter = run.reporter
    reporter.stage(key, "running")
    log(f"stage {key}")
    result = step()
    state, detail = result if isinstance(result, tuple) else ("done", result or "")
    reporter.stage(key, state, detail)
    log(f"stage {key}: {state} {detail}")


def execute(run: Run) -> int:
    """The stages in order; returns the exit code."""
    reporter = run.reporter
    started = time.monotonic()
    current = "preflight"
    try:
        current = "preflight"
        run_stage(run, "preflight", run.preflight)
        current = "prepare"
        if run.building:
            run_stage(run, "prepare", run.prepare)
        else:
            reporter.stage("prepare", "skipped", "--install-only")
        current = "plugin"
        run_stage(run, "plugin", run.plugin_stage)
        current = "build"
        if run.building:
            reporter.stage("build", "running")
            run.build()
            t = reporter.tracker
            if reporter.states["build"][0] == "running":
                reporter.stage("build", "done", f"{t.converted} converted, {t.reused} reused")
            current = "pack"
            s = run.summary
            reporter.stage(
                "pack", "done", f"{s.in_archive} cars, {size_text(s.archive_bytes)} archive, garage pictures, badges"
            )
        else:
            reporter.stage("build", "skipped", "--install-only")
            reporter.stage("pack", "skipped", "--install-only")
        current = "install"
        if run.installing:
            run_stage(run, "install", run.install)
        else:
            reporter.stage("install", "skipped", "--build-only")
    except StageError as error:
        if current == "build" and reporter.tracker.stage == "pack":
            current = "pack"
        reporter.stage(current, "failed", str(error).splitlines()[0][:100])
        reporter.note(str(error), "error")
        if error.hint:
            reporter.note(error.hint, "warn")
        reporter.note(f"Log: {run.summary.log}")
        log(f"FAILED in {current}: {error}\n{error.hint}")
        return 1
    except KeyboardInterrupt:
        reporter.stage(current, "failed", "interrupted")
        reporter.note("Interrupted. Run the same command again to continue (built cars are kept).", "warn")
        log("interrupted")
        return 130
    except Exception as error:
        traceback.print_exc()
        reporter.stage(current, "failed", f"{type(error).__name__}: {error}"[:100])
        reporter.note(f"{type(error).__name__}: {error} (traceback in {run.summary.log})", "error")
        return 1
    run.summary.elapsed = time.monotonic() - started
    reporter.summary(run.summary)
    return 0


# --- command line -------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsff install",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n", 2)[2],
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--build-only", action="store_true", help="stop after the build (nothing is installed)")
    mode.add_argument("--install-only", action="store_true", help="install the last build (out/build or --build)")
    mode.add_argument("--status", action="store_true", help="show what is installed (install.ps1 -Status)")
    mode.add_argument("--uninstall", action="store_true", help="restore the game's files (install.ps1 -Uninstall)")
    parser.add_argument("--game", type=Path, help="the game folder (default: $DSF_GAME_DIR)")
    pick = parser.add_argument_group("cars")
    pick.add_argument("--list", type=Path, action="append", help="car list, one per line (default: all.txt)")
    pick.add_argument("--car", action="append", metavar="NAME[@fh1|@fm4]", help="a car (repeatable)")
    pick.add_argument("--iso", type=Path, action="append", help="Forza disc images (default: assets/*.iso)")
    build = parser.add_argument_group("build")
    build.add_argument(
        "--jobs",
        type=int,
        default=None,
        help=f"cars converted at a time (default: half the CPUs, at most 8: {default_jobs(os.cpu_count(), False)} "
        "here; out/gentle caps it at 2)",
    )
    build.add_argument("--out", type=Path, default=DEFAULT_OUT, help="the build folder (default: out/build)")
    build.add_argument("--reuse", type=Path, action="append", metavar="DIR", help="an earlier build to reuse cars from")
    build.add_argument("--no-reuse", action="store_true", help="convert every car again")
    build.add_argument(
        "--verify-reuse",
        type=int,
        default=2,
        metavar="N",
        help="build N reused cars again and stop if one differs (default 2; 0: off)",
    )
    build.add_argument(
        "--no-plugin",
        action="store_true",
        help=f"build and install for the stock game, without dsfforza.asi (at most {STOCK_ROOM} cars)",
    )
    build.add_argument("--asi", type=Path, help="install this dsfforza.asi instead of out/asi's")
    build.add_argument(
        "--rebuild-plugin", action="store_true", help="build dsfforza.asi here (needs Visual Studio Build Tools)"
    )
    inst = parser.add_argument_group("install")
    inst.add_argument("--build", type=Path, help="with --install-only: the build folder to install")
    inst.add_argument("--save-dir", type=Path, help="the savegames folder to back up (default: the game's)")
    inst.add_argument("--no-save-backup", action="store_true", help="don't copy the save folder first")
    inst.add_argument(
        "--no-write-profile", action="store_true", help="don't let the mod mark garage cars owned in the save"
    )
    inst.add_argument("--force", action="store_true", help="replace other mods' bootstrap.lua, gui.sp, fonts")
    inst.add_argument(
        "--no-download", action="store_true", help="download nothing (vgmstream, DriverNGHook, the ASI loader)"
    )
    inst.add_argument("--hook-archive", type=Path, help="DNGHook_072.7z (default: the pinned download)")
    inst.add_argument("--asi-loader", type=Path, help="the Ultimate ASI Loader's dinput8.dll or its zip")
    inst.add_argument("--skip-hook", action="store_true", help="don't install DriverNGHook or an ASI loader")
    inst.add_argument("--keep-backup", action="store_true", help="with --uninstall: keep dsfforza-backup")
    parser.add_argument("--plain", action="store_true", help="plain text progress (the default when not a terminal)")
    parser.add_argument("--log", type=Path, help="the log file (default: out/logs/install-<time>.log)")
    return parser


def passthrough(args) -> int:
    """--status and --uninstall: tools/install.ps1 with its own output."""
    game = args.game or os.environ.get("DSF_GAME_DIR")
    if not game:
        print("Set DSF_GAME_DIR to the game folder, or pass --game.", file=sys.stderr)
        return 2
    command = [
        powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(INSTALL_PS1),
        "-GamePath",
        str(game),
    ]
    if args.status:
        command.append("-Status")
    else:
        if game_running(Path(game)):
            print("Driver.exe is running from the game folder: close the game first.", file=sys.stderr)
            return 1
        command.append("-Uninstall")
        if args.keep_backup:
            command.append("-KeepBackup")
    return subprocess.run(command, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.status or args.uninstall:
        return passthrough(args)
    if args.jobs is None:
        args.jobs = default_jobs(os.cpu_count(), gentle.enabled())
    args.out = Path(args.out).resolve()
    log_path = args.log or LOG_DIR / f"install-{datetime.datetime.now():%Y%m%d-%H%M%S}.log"
    with to_log(log_path) as (term, term_fd):
        reporter = make_reporter(term, term_fd, args.plain)
        run = Run(args, reporter)
        run.summary.log = log_path
        log(f"dsff install {' '.join(sys.argv[1:] if argv is None else argv)}")
        log(f"display: {'rich (live)' if isinstance(reporter, LiveReporter) else 'plain text'}")
        with reporter:
            code = execute(run)
        with contextlib.suppress(OSError, ValueError):
            term.flush()
    return code


if __name__ == "__main__":
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
