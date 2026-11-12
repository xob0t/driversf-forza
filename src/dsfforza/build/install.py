"""`dsff install`: build every Forza car and install it into Driver: San Francisco, in one command.

    uv run dsff install                     # check, prepare, plugin, build, pack, install
    uv run dsff install --build-only        # stop after the build (out/build)
    uv run dsff install --install-only      # install out/build (or --build DIR) as it is
    uv run dsff install --list my.txt       # other cars (--car NAME, repeatable, also works)
    uv run dsff install --status            # what is installed (tools/install.ps1 -Status)
    uv run dsff install --uninstall         # put the game back (tools/install.ps1 -Uninstall)
    uv run dsff install --report            # a zip for a bug report (src/dsfforza/build/report.py)

Stages, each skipped when there is nothing to do:

1. preflight: DSF_GAME_DIR (or --game) is Driver: San Francisco 1.04, with its vanilla
   vehicle archive when building (the build reads it), the Forza disc images are there
   (assets/*.iso or --iso), there is disk space, and for an install: the game isn't
   running, the VC++ x86 runtime, DriverNGHook and an ASI loader are there or can be
   downloaded, and another mod's bootstrap.lua is replaced only with --force.
2. prepare: src/dsfforza/game/game_data.py prepare, when its files under out/ are
   missing or older than the game's scripts.
3. plugin: out/asi/dsfcars.asi (tools/asi). Kept when out/asi/dsfcars.build.json
   says it was built from the current tools/asi sources, else built with
   tools/asi/build.ps1 -Test when Visual Studio Build Tools are installed, else a
   prebuilt one is used as it is. Every install needs it: its [archive] packages loads
   the cars.
4. build: src/dsfforza/build/port_batch.py build with --gui --wide-glyphs --id-policy
   asi --root-limit 3003 --packages (tools/batches/all.txt by default, then the cars of the FM4
   DLC packs under assets/: --fm4-dlc-cars; --no-fm4-dlc leaves them out) into out/build.new, reusing
   every car the last build (out/build), an interrupted one (out/build.partial) or
   --reuse DIR made from the same inputs and code, so a re-run only converts what
   changed. Without any of those, the earlier build under out/ with the most cars is
   used. When the build finishes, out/build.new becomes out/build.
5. pack: the merge into one archive (the in-game tests run it; the install doesn't), lint,
   garage pictures and HUD badges, and one car package per car (<build>/packages/*.dsfcar).
6. install: the save folder (Documents/Ubisoft/Driver San Francisco/reloaded/savegames)
   is copied to out/save-backups/<time>/, then tools/install.ps1 installs DriverNGHook,
   the plugin, the profile switch (-WriteProfile; --no-write-profile leaves it out) and
   the build's car packages through the car manager (src/dsfforza/build/car_manager.py),
   beside the game's files: no game file is replaced. An earlier dsfforza install is
   replaced; a merged one (up to v0.1.8) moves to packages on the same IDs.

The terminal shows the stages, a per-car progress bar and the cars each worker is
converting; everything the tools print goes to out/logs/install-<time>.log.
Builds run at Below Normal priority; with out/gentle at Idle with at most two workers.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime
import hashlib
import json
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza import REPO
from dsfforza.build import gentle
from dsfforza.build.install_display import (
    GIB,
    MIB,
    CarFailure,
    LiveReporter,
    Reporter,
    Summary,
    make_reporter,
    pack_text,
    rel,
    size_text,
)

# --- constants ------------------------------------------------------------------------------

VANILLA_ARCHIVE_SHA256 = (
    "0DDC885E23028F708A92A2426B643938A6934C99204C780E01755C5B7778AE35"  # Install\dngvehicles.sp 1.04
)
VANILLA_FILES = {  # the other files a merged install (up to v0.1.8) replaced (English install), for dsff report
    "Install/gui.sp": "AADB62925387307AF7E2BC5091D5E583823E553C23D4E71C5DD6BB71936DA11F",
    "media/Fonts.fchunk": "23B1724A878CB9937A764B55F7ADCF0E2D7403C0C63E8504A78566D8EE4602F2",
    "Locale/English/global.fchunk": "BA892E734B66A8AA08B98A19CEC32CE1A540792A02205D0AB3FB38BD599A96B8",
}
EXE_TIMESTAMP = 0x4EC0D92B  # Driver.exe 1.04 (tools/asi/src/patch.cpp's exe check)
EXE_IMAGE_SIZE = 0x019C0000
EXE_TEXT = (".text", 0x00001000, 0x009C7171)  # its first section: name, VirtualAddress, VirtualSize
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
ASI_OUT = REPO / "out" / "asi" / "dsfcars.asi"
BUNDLED_ASI = REPO / "plugin" / "dsfcars.asi"  # in a release zip (src/dsfforza/build/package.py)
DEFAULT_LIST = REPO / "tools" / "batches" / "all.txt"
DEFAULT_OUT = REPO / "out" / "build"
LOG_DIR = REPO / "out" / "logs"
SAVE_BACKUPS = REPO / "out" / "save-backups"
ASI_ROOT_LIMIT = 3003  # every base ID below 1000 (3 entries each) plus SSIC, DMWK, BINF
VS_INSTALLER = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer")
VCVARS_DEFAULT = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars32.bat")


class StageError(Exception):
    """A stage that can't go on: `message` says what is wrong, `hint` what to do."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


RELEASES_URL = "https://github.com/xob0t/driversf-forza/releases"


def missing_plugin_error(repo: Path) -> StageError:
    """No plugin\\dsfcars.asi and no git checkout. A release zip has a VERSION file
    (src/dsfforza/build/package.py), so without one this is GitHub's source zip
    ("Code > Download ZIP"), which has no plugin. With one, the release's plugin is
    missing from the unzipped folder."""
    asi = BUNDLED_ASI.relative_to(REPO)
    if not (repo / "VERSION").is_file():
        return StageError(
            "This folder is GitHub's source code download (Code > Download ZIP), not a release: it has no dsfcars.asi.",
            f"Download driversf-forza-<version>.zip from the Assets list on {RELEASES_URL}, unzip it, "
            "and run install.cmd there.",
        )
    return StageError(
        f"This release copy has no dsfcars.asi ({asi}), though the release zip holds it.",
        "Unzip the whole release zip again. If the file is still missing, check your antivirus's "
        "quarantine for dsfcars.asi. Or pass --asi FILE.",
    )


# --- small probes (each replaceable in tests) -------------------------------------------------


def sha256(path: Path) -> str:
    """A file's SHA-256 as upper-case hex (the form of VANILLA_ARCHIVE_SHA256 and
    VANILLA_FILES)."""
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
    except (OSError, struct.error):
        return None
    return stamp, size


def exe_text(path: Path) -> tuple[str, int, int] | None:
    """(Name, VirtualAddress, VirtualSize) of an exe's first section, or None."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(4096)
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe : pe + 4] != b"PE\0\0" or struct.unpack_from("<H", data, pe + 6)[0] == 0:
            return None
        section = pe + 24 + struct.unpack_from("<H", data, pe + 20)[0]  # after the optional header
        name = data[section : section + 8].rstrip(b"\0").decode("latin-1")
        size, va = struct.unpack_from("<II", data, section + 8)
    except (OSError, struct.error):
        return None
    return name, va, size


def exe_problem(path: Path) -> str | None:
    """Why dsfcars.asi would leave this Driver.exe unpatched, or None: the same checks as
    tools/asi/src/patch.cpp's dsf_exe_ok (PE timestamp, image size, the first section)."""
    build = exe_build(path)
    if build is None:
        return "not a Windows program file"
    if build != (EXE_TIMESTAMP, EXE_IMAGE_SIZE):
        return f"not version 1.04 (PE timestamp {build[0]:#x}, image size {build[1]:#x})"
    text = exe_text(path)
    if text != EXE_TEXT:
        found = f"{text[0]!r} at {text[1]:#x}, {text[2]:#x} bytes" if text else "none"
        return (
            "a 1.04 build with a different code section than the 1.04 release "
            f"(first section {found}; the release has '.text' at {EXE_TEXT[1]:#x}, {EXE_TEXT[2]:#x} bytes)"
        )
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
    """The game's save folder: Documents/Ubisoft/Driver San Francisco/reloaded/savegames."""
    return documents_dir() / "Ubisoft" / "Driver San Francisco" / "reloaded" / "savegames"


def free_bytes(path: Path) -> int:
    """Free space on the drive of `path` (its first existing parent)."""
    path = Path(path).resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(path).free


def drive_of(path: Path) -> str:
    """The drive a path is on (its anchor, lower case), to add up the space needed per
    drive."""
    return Path(path).resolve().anchor.lower()


@dataclass
class Probes:
    """What preflight asks the machine; tests swap in their own."""

    running: object = game_running
    vc_runtime: object = vc_runtime_x86
    free: object = free_bytes
    loader: object = find_asi_loader


# --- selection, jobs, reuse ---------------------------------------------------------------------


def default_jobs(cpus: int | None, *, gentle_on: bool, ram: int | None = None) -> int:
    """port_batch.default_jobs (a worker per logical CPU, 1.9 GB of RAM each), and at most
    gentle.CORES while out/gentle is there."""
    from dsfforza.build import port_batch

    jobs = port_batch.default_jobs(cpus or 1, ram)
    return min(jobs, gentle.CORES) if gentle_on else jobs


def dlc_preflight(packages: list, use: str = "build") -> list[str]:
    """Preflight lines for the FM4 DLC packages found under assets/ (forza_roster.dlc_packages,
    identified by their STFS headers). `use`: "build" (their cars are built with the default
    list: port_batch --fm4-dlc-cars), "named" (--list/--car: only the cars named), "off"
    (--no-fm4-dlc)."""
    from dsfforza.build import forza_roster

    found, notes = forza_roster.package_summary(packages)
    if not found:
        return notes
    how = {
        "build": "their cars are built too (--no-fm4-dlc leaves them out)",
        "named": "only the cars --list/--car names are built",
        "off": "left out (--no-fm4-dlc)",
    }[use]
    return [f"FM4 DLC packs: {len(found)} ({', '.join(found)}); {how}", *notes]


def count_cars(lists: list[Path], cars: list[str]) -> int:
    """Cars a --list/--car selection names (port_batch.read_list's format)."""
    from dsfforza.build.port_batch import read_list

    names = {c.partition("@")[0].lower() for c in cars}
    for path in lists:
        names |= {media.lower() for media, _ in read_list(path)}
    return len(names)


def fingerprinted_cars(build: Path) -> int:
    """How many cars of a build have a fingerprint (work/*.fp.json), so --reuse can take
    them."""
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
    """A dsff install build's folders: the finished build, this run's staging folder and an
    interrupted run's."""

    out: Path  # the finished build (out/build)
    staging: Path  # this run's (out/build.new)
    partial: Path  # an interrupted run's (out/build.partial)

    @classmethod
    def of(cls, out: Path) -> BuildDirs:
        """The folders around the finished build `out` (out/build: out/build.new,
        out/build.partial)."""
        out = Path(out)
        return cls(out, out.with_name(out.name + ".new"), out.with_name(out.name + ".partial"))


def reuse_sources(dirs: BuildDirs, extra: list[Path], *, no_reuse: bool, discover: bool = True) -> list[Path]:
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


def space_needed(cars: int, *, reusing: bool) -> tuple[int, int]:
    """(bytes on the build drive, bytes on the game drive), roughly: the archive is about
    0.5 GiB plus 4.2 MiB a car, the garage assets 0.4 MiB a car; a converted car takes about
    25 MiB more (work/ chunk, sound previews, the extraction cache); the install copies
    the archive and backs up 0.6 GiB of vanilla files."""
    build = GIB // 2 + cars * 5 * MIB + (cars * 4 * MIB if reusing else cars * 25 * MIB)
    game = GIB // 2 + cars * 5 * MIB + 6 * GIB // 10
    return build, game


# --- stage decisions -------------------------------------------------------------------------------


def prepare_status(game: Path, out_root: Path, games: set[str] | None = None) -> tuple[bool, bool, str]:
    """(run game_data prepare, with --force, why) from the files it writes under out/.
    `games` ({"FH1", "FM4"}, those with a disc among the images): a game whose disc isn't
    there has no database to write, so its missing one doesn't count."""
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
            if games is not None and name.upper() not in games:
                continue
            return True, False, f"db/{db.name} missing"
        if cached.is_file() and cached.stat().st_size != db.stat().st_size:
            return True, False, f"db/{db.name} differs from the cache"
    return False, False, "up to date"


def asi_sources_digest(source: Path = ASI_SOURCE) -> str:
    """The plugin's sources (tools/asi/src, build.ps1, dsfcars.ini), line ends ignored."""
    digest = hashlib.sha256()
    files = sorted(p for p in (Path(source) / "src").rglob("*") if p.is_file())
    files += [Path(source) / "build.ps1", Path(source) / "dsfcars.ini"]
    for path in files:
        if not path.is_file():
            continue
        digest.update(path.relative_to(source).as_posix().encode() + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def stamp_path(asi: Path) -> Path:
    """dsfcars.build.json beside a plugin: the sources digest and the file hash it was built
    from."""
    return Path(asi).with_name("dsfcars.build.json")


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


def plugin_action(state: str, *, vs: bool, rebuild: bool = False) -> str:
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


# Where tools/install.ps1 keeps its record: dsfcars-backup, and in an install from before the
# runtime was renamed to dsfcars, dsfforza-backup (install.ps1 moves it when it migrates).
RECORD_FOLDERS = ("dsfcars-backup", "dsfforza-backup")


def read_install_record(game: Path) -> dict | None:
    """<game>/dsfcars-backup/install.json (written by tools/install.ps1), else an older
    install's dsfforza-backup/install.json, or None."""
    for folder in RECORD_FOLDERS:
        try:
            return json.loads((Path(game) / folder / "install.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
    return None


def hook_source(path: Path) -> bool:
    """--hook-archive is usable: an archive file, or a folder holding plugins/DriverNGHook.asi."""
    return path.is_file() or (path / "plugins" / "DriverNGHook.asi").is_file()


def installed(record: dict | None) -> bool:
    """Whether an install record is of a dsfforza install that is still in place (not
    uninstalled)."""
    return bool(record) and record.get("state") != "uninstalled"


GUI_FILES = ("gui.sp", "Fonts.fchunk", "hud.lua")  # gui_assets: pictures, glyphs, the codes that use them


def package_problem(build: Path) -> str | None:
    """Why a build's car packages can't be installed (tools/install.ps1 Get-BuildPackages
    checks the same), or None: its packaging failed (batch-report.json timings packages_error), or
    <build>/packages doesn't hold exactly one package per car of its manifest."""
    from dsfforza.build import dsfcar

    build = Path(build)
    try:
        report = json.loads((build / "batch-report.json").read_text(encoding="utf-8"))
        error = (report.get("timings_s") or {}).get("packages_error")
    except (OSError, ValueError, AttributeError):
        error = None
    if error:
        return f"the build's car packages failed ({error})"
    try:
        cars = json.loads((build / "dngvehicles.json").read_text(encoding="utf-8"))["cars"]
        want = {dsfcar.key_of(str(c["game"]), c["media"]) for c in cars}
    except (OSError, ValueError, KeyError, TypeError):
        return f"{build / 'dngvehicles.json'} is missing or unreadable"
    have = {p.name[: -len(dsfcar.SUFFIX)] for p in (build / "packages").glob(f"*{dsfcar.SUFFIX}")}
    if not have:
        return f"{build} has no car packages (built without --packages)"
    if have != want:
        extra, missing = sorted(have - want), sorted(want - have)
        return (
            f"{build / 'packages'} doesn't match the build: {len(missing)} cars without a package"
            f"{' (' + ', '.join(missing[:3]) + ')' if missing else ''}, {len(extra)} packages of other cars"
            f"{' (' + ', '.join(extra[:3]) + ')' if extra else ''}"
        )
    return None


def gui_problem(build: Path) -> str | None:
    """Why `build`'s gui/ doesn't go with its cars, or None. Without gui/ (built without
    --gui, or its GUI step failed) the build's merged archive, as the in-game tests run it
    (tools/wsb/guest/run-test.ps1), has no hud.lua for its cars, and with one made for
    other cars (another build's gui/) the HUD would show their names. dsff report shows it."""
    gui = Path(build) / "gui"
    missing = [name for name in GUI_FILES if not (gui / name).is_file()]
    if missing:
        why = ""
        try:
            report = json.loads((Path(build) / "batch-report.json").read_text(encoding="utf-8"))
            error = (report.get("timings_s") or {}).get("gui_error")
            why = f" (its GUI step failed: {error})" if error else ""
        except (OSError, ValueError, AttributeError):
            pass
        return (
            f"{build} has no {', '.join('gui/' + m for m in missing)}{why}: it was built without --gui or its "
            "GUI step failed, so the cars would have no garage pictures, badges or nameplates of their own"
        )
    try:
        cars = json.loads((Path(build) / "dngvehicles.json").read_text(encoding="utf-8"))["cars"]
        want = {int(c["spool_id"]): str(c["media"]) for c in cars}
    except (OSError, ValueError, KeyError, TypeError):
        return None  # the install checks the manifest itself
    have: dict[int, str | None] = {}
    for line in (gui / "hud.lua").read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*\[(\d+)\]\s*=\s*\{(.*)\}", line)
        if m:
            picture = re.search(r'Picture1\s*=\s*"([^"]*)"', m.group(2))
            have[int(m.group(1))] = picture.group(1) if picture else None
    absent = sorted(i for i in want if i not in have)
    other = sorted(
        i
        for i, media in want.items()
        if (have.get(i) or "").startswith("dsfforza_") and have[i] != f"dsfforza_{media.lower()}"
    )
    if absent or other:
        parts = []
        if absent:
            parts.append(f"no hud.lua entry for {len(absent)} cars (IDs {', '.join(map(str, absent[:8]))})")
        if other:
            i = other[0]
            parts.append(f"{len(other)} hud.lua entries are for other cars (ID {i}: {have[i][9:]}, not {want[i]})")
        return f"{build}/gui doesn't match its cars: {'; '.join(parts)}. It is another build's gui/"
    return None


# --- the log -----------------------------------------------------------------------------


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


# --- the run -------------------------------------------------------------------------------------------


def log(text: str) -> None:
    """A line for the log file (stdout points there while a run is on)."""
    print(f"[dsff install {datetime.datetime.now():%H:%M:%S}] {text}", flush=True)


@dataclass
class Run:
    """One dsff install run: its arguments, display and probes, what preflight found (the
    game, the disc images, the vanilla archive, the install record) and what the stages
    made."""

    args: argparse.Namespace
    reporter: Reporter
    probes: Probes = field(default_factory=Probes)
    game: Path | None = None
    isos: list = field(default_factory=list)
    vanilla_archive: Path | None = None
    record: dict | None = None
    plugin: Path | None = None
    build_dir: Path | None = None
    summary: Summary = field(default_factory=Summary)

    @property
    def building(self) -> bool:
        """Whether this run builds (not --install-only)."""
        return not self.args.install_only

    @property
    def installing(self) -> bool:
        """Whether this run installs (not --build-only)."""
        return not self.args.build_only

    # 1. preflight ------------------------------------------------------------------------------

    def preflight(self) -> str:
        """Stage 1: what the run needs is there (the game, its vanilla archive, the disc
        images and car lists for a build, the install's prerequisites, disk space). Returns
        the stage's detail; a StageError lists every problem found."""
        game = self.find_game()
        self.game = game
        problems, info = self.check_game(game)
        cars = None
        if self.building:
            found, notes, cars = self.check_build_inputs()
            problems += found
            info += notes
        if self.installing:
            problems += self.install_checks()
        found, notes = self.check_space(cars if cars is not None else len(self.installed_cars()))
        problems += found
        info += notes
        if problems:
            raise StageError("\n".join(f"- {p}" for p in problems))
        return ", ".join(info)

    def find_game(self) -> Path:
        """The game folder: --game, else DSF_GAME_DIR; a StageError when unset or without Driver.exe."""
        args = self.args
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
        return game

    def check_game(self, game: Path) -> tuple[list[str], list[str]]:
        """(problems, info) of the game folder: Driver.exe is the 1.04 release dsfcars.asi
        patches, and the vehicle archive is vanilla when building (the build reads it).
        Reads the install record and finds the vanilla archive (self.record,
        self.vanilla_archive)."""
        problems, info = [], []
        if problem := exe_problem(game / "Driver.exe"):
            problems.append(
                f"{game / 'Driver.exe'} is {problem}. dsfcars.asi patches only the 1.04 release's "
                "Driver.exe and leaves any other unpatched, and the game then crashes while loading "
                "the cars. Install the game's 1.04 update, or restore the Driver.exe it installed."
            )
        from dsfforza.game import game_paths

        self.record = read_install_record(game)
        self.vanilla_archive = game_paths.vanilla(game / "Install" / "dngvehicles.sp", quiet=True)
        if not self.vanilla_archive.is_file():
            problems.append(f"{game / 'Install' / 'dngvehicles.sp'} is missing.")
        elif sha256(self.vanilla_archive) != VANILLA_ARCHIVE_SHA256:
            if not self.building:  # the cars install beside the archive: another mod's can stay
                info.append("archive not vanilla (another mod's: kept)")
            else:  # the build reads the vanilla archive
                problems.append(
                    f"{self.vanilla_archive} is not the vanilla 1.04 vehicle archive (another mod?). "
                    "Restore the original (Steam/Uplay: verify files) and run again."
                )
        else:
            info.append("DSF 1.04")
        return problems, info

    def check_build_inputs(self) -> tuple[list[str], list[str], int]:
        """(problems, info, how many cars the build names at most) of a build's inputs: the
        Forza disc images (self.isos; each identified by its content), the FM4 DLC packs and
        the car lists."""
        from dsfforza.build import forza_roster

        args, problems, info = self.args, [], []
        self.isos = [Path(p) for p in (args.iso or forza_roster.default_images())]
        missing = [p for p in self.isos if not p.is_file()]
        if not self.isos:
            problems.append(
                "No Forza disc images: put the FH1 and FM4 (disc 1 and 2) .iso files in "
                f"{REPO / 'assets'}, or pass --iso FILE for each."
            )
        elif missing:
            problems.append(f"Disc image not found: {', '.join(map(str, missing))}")
        else:
            # Each image by its content (default.xex), whatever it is called.
            discs = forza_roster.identify_all(self.isos)
            found, notes = forza_roster.disc_summary(discs)
            if not found:
                problems.append(
                    "None of the disc images is a Forza Horizon or Forza Motorsport 4 disc: "
                    + "; ".join(f"{d.path.name}: {d.error}" for d in discs)
                )
            else:
                info.append("discs " + " + ".join(found))
                info += notes
        info += dlc_preflight(forza_roster.dlc_packages(), self.dlc_use())
        lists = self.lists()
        gone = [p for p in lists if not Path(p).is_file()]
        if gone:
            problems.append(f"Car list not found: {', '.join(map(str, gone))}")
        # the DLC list in full: an upper bound when some packs are missing (for the disk space)
        from dsfforza.build.port_batch import DLC_LIST

        dlc = [DLC_LIST] if self.dlc_cars() else []
        return problems, info, count_cars([p for p in lists if p not in gone] + dlc, args.car or [])

    def check_space(self, cars: int) -> tuple[list[str], list[str]]:
        """(problems, info) of the disk space for `cars` cars (space_needed): the build
        folder's drive and the game's, added up when they are one drive."""
        args, problems, info = self.args, [], []
        reusing = bool(reuse_sources(BuildDirs.of(args.out), args.reuse or [], no_reuse=args.no_reuse))
        need_build, need_game = space_needed(cars, reusing=reusing)
        needs: dict[str, tuple[Path, int]] = {}
        if self.building:
            needs[drive_of(args.out)] = (args.out, need_build)
        if self.installing:
            path, n = needs.get(drive_of(self.game), (self.game, 0))
            needs[drive_of(self.game)] = (path, n + need_game)
        for path, need in needs.values():
            free = self.probes.free(path)
            if free < need:
                problems.append(
                    f"Not enough disk space on {Path(path).resolve().anchor}: about {size_text(need)} needed, "
                    f"{size_text(free)} free."
                )
            else:
                info.append(f"{size_text(free)} free on {Path(path).resolve().anchor.rstrip(chr(92))}")
        return problems, info

    def lists(self) -> list[Path]:
        """The car lists the build gets: --list, else the default list unless --car names
        the cars."""
        if self.args.list:
            return list(self.args.list)
        return [] if self.args.car else [DEFAULT_LIST]

    def dlc_use(self) -> str:
        """How the FM4 DLC packs found are used: "off" (--no-fm4-dlc), "named" (--list/--car
        pick the cars) or "build" (the default list, plus the packs' cars)."""
        if getattr(self.args, "no_fm4_dlc", False):
            return "off"
        return "named" if self.args.list or self.args.car else "build"

    def dlc_cars(self) -> bool:
        """Whether the build adds the DLC packs' cars (port_batch --fm4-dlc-cars)."""
        from dsfforza.build import fm4_dlc

        return self.dlc_use() == "build" and bool(fm4_dlc.packages())

    def installed_cars(self) -> list:
        """The cars of the build that would be installed (its manifest's), [] when there is
        none."""
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

            if args.hook_archive and not hook_source(Path(args.hook_archive)):
                problems.append(
                    f"--hook-archive {args.hook_archive}: no such file, nor a folder with plugins\\DriverNGHook.asi in it."
                )
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
                    "Common\\UserLuaScripts\\bootstrap.lua belongs to another mod. Add --force to replace it: it is "
                    "backed up and put back by --uninstall, and ours runs it first, so that mod still loads."
                )
        return problems

    # 2. prepare ------------------------------------------------------------------------------------

    def prepare(self) -> tuple[str, str]:
        """game_data prepare when needed, and vgmstream (Forza's engine sound) when missing."""
        from dsfforza.build import thirdparty
        from dsfforza.build.forza_roster import game_discs

        needed, force, why = prepare_status(self.game, REPO / "out", set(game_discs(self.isos)))
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
        """Stage 3: the dsfcars.asi to install: --asi, the release's, or out/asi's, kept,
        used as it is or built (plugin_action)."""
        args = self.args
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
            raise missing_plugin_error(REPO)
        digest = asi_sources_digest()
        state = plugin_state(ASI_OUT, digest)
        vcvars = find_vcvars()
        action = plugin_action(state, vs=vcvars is not None, rebuild=args.rebuild_plugin)
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
                "dsfcars.asi is needed (its [archive] packages loads the cars), and it can't be built here: "
                "Visual Studio 2022 Build Tools with the C++ workload were not found.",
                "Install them (winget install Microsoft.VisualStudio.2022.BuildTools --override "
                '"--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"), or copy a '
                f"built dsfcars.asi to {ASI_OUT.parent}, or pass --asi FILE.",
            )
        self.build_plugin(vcvars)
        self.plugin = ASI_OUT
        return "done", f"built {rel(ASI_OUT)} and tested it against Driver.exe"

    def build_plugin(self, vcvars: Path) -> None:
        """Build and test dsfcars.asi (tools/asi/build.ps1 -Test with vcvars32.bat), then
        stamp what it was built from."""
        env = {**os.environ, "DSF_GAME_DIR": str(self.game)}
        env["PATH"] = str(VS_INSTALLER) + os.pathsep + env.get("PATH", "")
        command = powershell_script(ASI_SOURCE / "build.ps1", "-VcVars", str(vcvars), "-Test")
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
        """Stages 4 and 5: port_batch build into out/build.new with the install's settings,
        reusing the earlier builds' cars, then out/build.new becomes out/build."""
        from dsfforza.build import port_batch
        from dsfforza.game import game_paths

        args = self.args
        dirs = BuildDirs.of(args.out)
        note = start_staging(dirs)
        if note:
            self.reporter.note(note)
        sources = reuse_sources(dirs, args.reuse or [], no_reuse=args.no_reuse)
        hint = None
        if sources:
            self.reporter.stage("build", "running", "reusing cars from " + ", ".join(rel(s) for s in sources))
            hint = seconds_per_car(sources[0])
        argv = self.port_batch_argv(dirs.staging, sources)
        build_args = port_batch.build_parser().parse_args(argv)
        build_args.dsf = game_paths.vanilla(build_args.dsf, quiet=True)
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

    def port_batch_argv(self, staging: Path, sources: list[Path]) -> list[str]:
        """port_batch build's command line for this run: into `staging`, the cars of the lists
        (and the FM4 DLC packs' unless --no-fm4-dlc) and --car, reusing `sources`."""
        args = self.args
        # The ID policy, root limit and wide glyphs are for the merged archive the build also writes
        # (the in-game tests run it: tools/wsb/run-series.ps1 -Archives); the car packages
        # (--packages, <build>/packages/*.dsfcar) get their IDs from the car manager at install.
        argv = ["build", "--out", str(staging), "--dsf", str(self.vanilla_archive), "--gui", "--packages"]
        argv += ["--id-policy", "asi", "--root-limit", str(ASI_ROOT_LIMIT), "--wide-glyphs"]
        for path in self.lists():
            argv += ["--list", str(path)]
        if self.dlc_use() == "off":
            argv += ["--no-fm4-dlc"]
        elif self.dlc_cars():
            argv += ["--fm4-dlc-cars"]  # the FM4 DLC packs' cars, after the list (new IDs)
        for car in args.car or []:
            argv += ["--car", car]
        for iso in self.isos:
            argv += ["--iso", str(iso)]
        for source in sources:
            argv += ["--reuse", str(source)]
        argv += ["--jobs", str(args.jobs), "--verify-reuse", str(args.verify_reuse if sources else 0)]
        # A reused car that comes out different when built again doesn't stop a player's build:
        # its fresh copy is used and its build's other cars are converted again.
        argv += ["--verify-mismatch", "rebuild"]
        if args.verify_pack:
            argv += ["--verify-pack"]
        return argv

    def read_report(self, build: Path) -> None:
        """The summary's cars, failures and notes from a build's batch-report.json."""
        s = self.summary
        s.build_dir = build
        try:
            report = json.loads((build / "batch-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        reused = set((report.get("reuse") or {}).get("reused") or [])
        mismatch = (report.get("reuse") or {}).get("verify_mismatch")
        if mismatch:
            bad = mismatch.get("different", []) + mismatch.get("failed", [])
            s.notes.append(
                f"{len(bad)} reused car(s) came out different when converted again ({', '.join(bad)}): their new copies "
                f"were used and {len(mismatch.get('converted_again', []))} other reused cars were converted again. "
                "Nothing to do; please mention it in a bug report (install.cmd --report) if something looks wrong."
            )
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
        s.pack = pack_text((report.get("timings_s") or {}).get("pack_reuse_stats"), s.in_archive)

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
        """DriverNGHook is in the game folder, and not as a file our earlier install put
        there."""
        hook = self.game / "plugins" / "DriverNGHook.asi"
        return hook.is_file() and "plugins\\DriverNGHook.asi".lower() not in self.ours()

    def game_has_loader(self) -> bool:
        """An ASI loader is in the game folder, and not as a file our earlier install put
        there."""
        name = self.probes.loader(self.game)
        return bool(name) and name.lower() not in self.ours()

    def hook_files(self) -> tuple[Path | None, Path | None]:
        """(DNGHook folder, ASI loader) for install.ps1: given, else the pinned downloads
        (src/dsfforza/build/thirdparty.py) when the game doesn't have them yet. The hook's
        .7z is unpacked here, not by install.ps1's tar.exe (no LZMA in some Windows builds)."""
        from dsfforza.build import thirdparty

        args = self.args
        if args.skip_hook:
            return None, None
        hook, loader = args.hook_archive, args.asi_loader
        try:
            if hook and Path(hook).is_file():
                hook = thirdparty.unpack(Path(hook), Path(tempfile.mkdtemp(prefix="dsfforza-hook-")))
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
        """Copy the save folder (--save-dir, else the game's) to out/save-backups/<time>/;
        None when there is none."""
        save = Path(self.args.save_dir) if self.args.save_dir else default_save_dir()
        if not save.is_dir():
            log(f"no save folder at {save}; nothing to back up")
            return None
        target = SAVE_BACKUPS / f"{datetime.datetime.now():%Y%m%d-%H%M%S}" / save.name
        shutil.copytree(save, target)
        log(f"save folder {save} copied to {target}")
        return target

    def install_flags(self, hook, loader) -> list[str]:
        """tools/install.ps1's switches for this run, after -GamePath and -Build."""
        args, flags = self.args, []
        if self.plugin is not None:
            flags += ["-AsiPath", str(self.plugin)]
        if hook:
            flags += ["-HookArchive", str(hook)]
        if loader:
            flags += ["-AsiLoader", str(loader)]
        if args.skip_hook:
            flags += ["-SkipHook"]
        if not args.no_write_profile:
            flags += ["-WriteProfile"]
        if args.force or installed(self.record):
            flags += ["-Force"]  # replaces the earlier dsfforza install (and, with --force, another mod's bootstrap)
        return flags

    def install(self) -> str:
        """Stage 6: tools/install.ps1 installs the build's car packages, the plugin, the
        hook and the loader; the summary gets what it replaced and added."""
        args, game = self.args, self.game
        build = Path(args.build) if args.build else (self.build_dir or args.out)
        # The car packages, through the car manager: none of the build's archive, Lua or GUI files.
        if not (build / "dngvehicles.json").is_file():
            raise StageError(
                f"No finished build in {build} (dngvehicles.json missing).",
                f"Run `{command_name()}` (or with --build-only) first, or pass --build DIR.",
            )
        if self.build_dir is None:
            self.read_report(build)
        problem = package_problem(build)
        if problem:
            raise StageError(
                problem + ".",
                f"Build again with `{command_name()}` (it builds them), or `port_batch build --gui --packages` "
                "for your own list.",
            )
        if self.probes.running(game):
            raise StageError("Driver.exe is running from the game folder.", "Close the game and run --install-only.")
        self.reporter.stage("install", "running", "backing up the save")
        self.summary.save_backup = None if args.no_save_backup else self.backup_save()
        hook, loader = self.hook_files()
        self.reporter.stage("install", "running", "tools/install.ps1: the plugin, the Lua and the car packages")
        command = powershell_script(INSTALL_PS1, "-GamePath", str(game), "-Build", str(build))
        command += self.install_flags(hook, loader)
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


def command_name() -> str:
    """This command as the user typed it: install.cmd (the release launcher sets DSFF_LAUNCHER) or uv."""
    return r".\install.cmd" if os.environ.get("DSFF_LAUNCHER") else "uv run dsff install"


def uninstall_command(game: Path | None) -> str:
    """How to undo the install."""
    return f"{command_name()} --uninstall" + (f' --game "{game}"' if game else "")


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
    """Windows PowerShell: the one on PATH, else its usual place."""
    found = shutil.which("powershell")
    if found:
        return found
    return str(
        Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )


def powershell_script(script: Path, *args: str) -> list[str]:
    """The command line that runs a PowerShell script with its arguments (no profile, any
    execution policy)."""
    return [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]


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
                "pack",
                "done",
                f"{s.in_archive} cars, {size_text(s.archive_bytes)} archive, {s.pack or 'garage pictures, badges'}",
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
    except Exception as error:  # noqa: BLE001 - the run's last stop: shown with its traceback and logged
        traceback.print_exc()
        reporter.stage(current, "failed", f"{type(error).__name__}: {error}"[:100])
        reporter.note(f"{type(error).__name__}: {error} (traceback in {run.summary.log})", "error")
        return 1
    run.summary.elapsed = time.monotonic() - started
    reporter.summary(run.summary)
    return 0


# --- command line -------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """dsff install's command line."""
    parser = argparse.ArgumentParser(
        prog=command_name(),  # the usage line as the user types it
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n", 2)[2],
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--build-only", action="store_true", help="stop after the build (nothing is installed)")
    mode.add_argument("--install-only", action="store_true", help="install the last build (out/build or --build)")
    mode.add_argument("--status", action="store_true", help="show what is installed (install.ps1 -Status)")
    mode.add_argument("--uninstall", action="store_true", help="restore the game's files (install.ps1 -Uninstall)")
    mode.add_argument(
        "--migrate",
        action="store_true",
        help="move an install from before the rename to dsfcars to the new names, every car on its ID (install.ps1 -Migrate)",
    )
    mode.add_argument(
        "--report",
        action="store_true",
        help="write driversf-forza-report-<time>.zip for a bug report (setup, install state, logs) and print a summary",
    )
    parser.add_argument("--game", type=Path, help="the game folder (default: $DSF_GAME_DIR)")
    pick = parser.add_argument_group("cars")
    pick.add_argument("--list", type=Path, action="append", help="car list, one per line (default: all.txt)")
    pick.add_argument("--car", action="append", metavar="NAME[@fh1|@fm4]", help="a car (repeatable)")
    pick.add_argument(
        "--no-fm4-dlc",
        action="store_true",
        help="leave out the FM4 DLC packs under assets/ (by default the default list's build adds their cars)",
    )
    pick.add_argument(
        "--iso", type=Path, action="append", help="Forza disc images, any file names (default: assets/*.iso)"
    )
    build = parser.add_argument_group("build")
    build.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="cars converted at a time (default: one per logical CPU, as many as 1.9 GB of RAM each fits: "
        f"{default_jobs(os.cpu_count(), gentle_on=False)} here; out/gentle caps it at 2)",
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
        "--verify-pack",
        action="store_true",
        help="after a pack that reused the last build's, make a full pack too and stop if they differ",
    )
    build.add_argument("--asi", type=Path, help="install this dsfcars.asi instead of out/asi's")
    build.add_argument(
        "--rebuild-plugin", action="store_true", help="build dsfcars.asi here (needs Visual Studio Build Tools)"
    )
    inst = parser.add_argument_group("install")
    inst.add_argument("--build", type=Path, help="with --install-only: the build folder to install")
    inst.add_argument("--save-dir", type=Path, help="the savegames folder to back up (default: the game's)")
    inst.add_argument("--no-save-backup", action="store_true", help="don't copy the save folder first")
    inst.add_argument(
        "--no-write-profile", action="store_true", help="don't let the mod mark garage cars owned in the save"
    )
    inst.add_argument("--force", action="store_true", help="replace another mod's bootstrap.lua (ours runs it first)")
    inst.add_argument(
        "--no-download", action="store_true", help="download nothing (vgmstream, DriverNGHook, the ASI loader)"
    )
    inst.add_argument(
        "--hook-archive", type=Path, help="DNGHook_072.7z, or its unpacked folder (default: the pinned download)"
    )
    inst.add_argument("--asi-loader", type=Path, help="the Ultimate ASI Loader's dinput8.dll or its zip")
    inst.add_argument("--skip-hook", action="store_true", help="don't install DriverNGHook or an ASI loader")
    inst.add_argument("--keep-backup", action="store_true", help="with --uninstall: keep dsfcars-backup")
    parser.add_argument("--plain", action="store_true", help="plain text progress (the default when not a terminal)")
    parser.add_argument("--log", type=Path, help="the log file (default: out/logs/install-<time>.log)")
    return parser


def passthrough(args) -> int:
    """--status, --uninstall and --migrate: tools/install.ps1 with its own output."""
    game = args.game or os.environ.get("DSF_GAME_DIR")
    if not game:
        print("Set DSF_GAME_DIR to the game folder, or pass --game.", file=sys.stderr)
        return 2
    command = powershell_script(INSTALL_PS1, "-GamePath", str(game))
    if args.status:
        command.append("-Status")
    else:
        if game_running(Path(game)):
            print("Driver.exe is running from the game folder: close the game first.", file=sys.stderr)
            return 1
        if args.migrate:
            command.append("-Migrate")
            if args.asi:
                command += ["-AsiPath", str(args.asi)]
        else:
            command.append("-Uninstall")
            if args.keep_backup:
                command.append("-KeepBackup")
    return subprocess.run(command, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    """dsff install: the stages, with a display on the terminal and everything the tools
    print in the log; --status, --uninstall, --migrate and --report run their own tools.
    Returns the exit code."""
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.status or args.uninstall or args.migrate:
        return passthrough(args)
    if args.report:
        from dsfforza.build import report

        return report.main(["--game", str(args.game)] if args.game else [])
    if args.jobs is None:
        args.jobs = default_jobs(os.cpu_count(), gentle_on=gentle.enabled())
    args.out = Path(args.out).resolve()
    log_path = args.log or LOG_DIR / f"install-{datetime.datetime.now():%Y%m%d-%H%M%S}.log"
    with to_log(log_path) as (term, term_fd):
        reporter = make_reporter(term, term_fd, plain=args.plain)
        run = Run(args, reporter)
        run.summary.log = log_path
        log(f"dsff install {' '.join(sys.argv[1:] if argv is None else argv)}")
        log(f"started by {os.environ.get('DSFF_LAUNCHER') or 'dsff install'}")  # for dsff report
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
