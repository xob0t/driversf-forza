"""`dsff report`: one zip with what a bug report needs, and a short summary to paste.

    .\\install.cmd --report             # in the release folder (dsff install --report)
    uv run dsff report                  # the same from a checkout
    uv run dsff report --out DIR        # write the zip to DIR instead of this folder

Writes driversf-forza-report-<date-time>.zip into this folder (the unzipped release or
the checkout) and prints a summary. The zip holds:

    summary.txt   the summary the console shows
    report.json   everything below, as data
    logs/         the last install logs (out/logs/install-*.log), plugins/dsfforza.log,
                  the DriverNGHook log (Driver.log in the game folder), the failed cars'
                  build logs, each cut to its first and last part when long
    files/        small settings: plugins/dsfforza.ini, the install record
                  (dsfforza-backup/install.json), the game's graphics.ini

report.json: this copy's version (the release's VERSION, or the git commit and its
changes), how the cars were installed (install.cmd, dsff install, or tools/install.ps1 by
hand), Windows, locale, CPU, RAM, free disk space, uv and Python, the VC++ x86 runtime;
the game folder: Driver.exe (1.04 or not), each installed file as tools/install.ps1
-Status checks it and whether it is the last build's, other mods (ASI loaders,
DriverNGHook, other .asi files, another bootstrap.lua, changed stock files), crash dumps
(a summary of the newest, never the dump); the disc images (names, sizes and what
each one is, never the images); the last build (cars, failed cars and why, options).

Only reads: nothing is written but the zip. No save files, disc images or game files go
in; the user's profile folder in every path and log is replaced by %USERPROFILE%.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from dsfforza import REPO
from dsfforza.build import install

LOG_LIMIT = 256 * 1024  # bytes of one log kept: its start and its end
LOG_HEAD = 32 * 1024
CAR_LOG_LIMIT = 64 * 1024
INSTALL_LOGS = 3  # the newest out/logs/install-*.log
FAILED_CAR_LOGS = 10
HASH_LIMIT = 600 * install.MIB  # bigger files (a ported archive) are compared by size and date
STOCK_ARCHIVE_SIZE = 483_110_912  # Install\dngvehicles.sp 1.04
DUMP_LIMIT = 64 * install.MIB
DSF_LOGS = ("Driver.log",)  # DriverNGHook's log (with the game's Lua output), in the game folder
SETTINGS = ("plugins/dsfforza.ini", "dsfforza-backup/install.json")
ASI_FOLDERS = ("", "plugins", "scripts")  # where the Ultimate ASI Loader looks for .asi files
OURS_ASI = {"dsfforza.asi", "drivernghook.asi"}


# --- privacy --------------------------------------------------------------------------------------


class Redactor:
    """Replaces the user's profile folder (C:\\Users\\<name>, any case, \\ / or JSON's \\\\)
    with %USERPROFILE%, and any other X:\\Users\\<name> path with X:\\Users\\%USERNAME%."""

    def __init__(self, profile: str | None = None, username: str | None = None):
        profile = profile if profile is not None else os.environ.get("USERPROFILE") or str(Path.home())
        username = username if username is not None else os.environ.get("USERNAME") or Path(profile).name
        self.patterns: list[tuple[re.Pattern, str]] = []
        sep = r"(?:\\\\|\\|/)+"
        end = r"(?![\w-]|\.\w)"  # not C:\Users\name2 or C:\Users\name.old
        if profile and len(profile) > 3:
            parts = [re.escape(p) for p in re.split(r"[\\/]+", profile.rstrip("\\/")) if p]
            self.patterns.append((re.compile(sep.join(parts) + end, re.IGNORECASE), "%USERPROFILE%"))
        if username:
            self.patterns.append(
                (re.compile(r"(Users" + sep + r")" + re.escape(username) + end, re.IGNORECASE), r"\1%USERNAME%")
            )

    def __call__(self, text: str) -> str:
        for pattern, replacement in self.patterns:
            text = pattern.sub(replacement, text)
        return text


# --- small readers ------------------------------------------------------------------------------


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def trimmed(path: Path, limit: int = LOG_LIMIT, head: int = LOG_HEAD) -> str:
    """A text file, or its first `head` and last `limit - head` bytes when longer."""
    with Path(path).open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(0)
        if size <= limit:
            data = handle.read()
            return data.decode("utf-8", "replace")
        start = handle.read(head)
        handle.seek(size - (limit - head))
        end = handle.read()
    cut = size - len(start) - len(end)
    return (
        start.decode("utf-8", "replace")
        + f"\n\n[... {cut} bytes left out by dsff report ...]\n\n"
        + end.decode("utf-8", "replace")
    )


def stamp(path: Path) -> str:
    return datetime.datetime.fromtimestamp(Path(path).stat().st_mtime).isoformat(timespec="seconds")


def file_entry(path: Path) -> dict:
    st = Path(path).stat()
    return {"name": Path(path).name, "bytes": st.st_size, "modified": stamp(path)}


def run_text(command: list[str], cwd: Path | None = None, timeout: float = 20) -> str | None:
    """A command's stdout (stripped), or None when it can't run or fails."""
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def size_text(n: int | None) -> str:
    if n is None:
        return "?"
    if n >= install.GIB:
        return f"{n / install.GIB:.1f} GB"
    if n >= install.MIB:
        return f"{n / install.MIB:.0f} MB"
    return f"{n / 1024:.0f} KB"


# --- this copy ----------------------------------------------------------------------------------


def version_info(repo: Path) -> dict:
    """The release's VERSION, else the git commit and its local changes, else neither (a
    source zip from GitHub's Code button: not a release)."""
    repo = Path(repo)
    version = repo / "VERSION"
    if version.is_file():
        words = version.read_text(encoding="utf-8", errors="replace").split()
        return {
            "kind": "release",
            "version": words[0] if words else "?",
            "commit": words[1] if len(words) > 1 else None,
            "plugin": read_json(repo / "plugin" / "dsfforza.build.json"),
        }
    if (repo / ".git").exists():
        git = shutil.which("git")
        out: dict = {"kind": "checkout", "version": None, "commit": None}
        if git:
            # --no-optional-locks: status must not refresh (write) the index.
            out["commit"] = run_text([git, "--no-optional-locks", "rev-parse", "HEAD"], repo)
            out["branch"] = run_text([git, "--no-optional-locks", "rev-parse", "--abbrev-ref", "HEAD"], repo)
            out["describe"] = run_text([git, "--no-optional-locks", "describe", "--tags", "--always"], repo)
            status = run_text([git, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no"], repo)
            out["changed_files"] = status.splitlines() if status else []
        return out
    return {"kind": "source zip", "version": None, "commit": None}


def version_text(v: dict) -> str:
    if v["kind"] == "release":
        return f"{v['version']} (release zip, commit {(v.get('commit') or '?')[:10]})"
    if v["kind"] == "checkout":
        changed = len(v.get("changed_files") or [])
        return f"git checkout {v.get('describe') or (v.get('commit') or '?')[:10]} on {v.get('branch') or '?'}" + (
            f", {changed} files changed" if changed else ", no local changes"
        )
    return "no VERSION file and no git: a source zip, not a release (use the release zip)"


def install_logs(repo: Path) -> list[Path]:
    """out/logs/install-<date>-<time>.log, newest first (by the name: its start time)."""
    folder = Path(repo) / "out" / "logs"
    return sorted(folder.glob("install-*.log"), key=lambda p: p.name, reverse=True) if folder.is_dir() else []


LOG_LINE = re.compile(r"^\[dsff install (\d\d:\d\d:\d\d)\] (.*)$")


def log_run(path: Path) -> dict:
    """What one dsff install log says: when, by which command, the outcome, the build's
    port_batch arguments, the install.ps1 command."""
    out: dict = {"log": path.name, "started": None, "launcher": None, "outcome": "unfinished or interrupted"}
    m = re.search(r"install-(\d{8})-(\d{6})", path.name)
    if m:
        out["started"] = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").isoformat()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = LOG_LINE.match(line)
        if not m:
            continue
        body = m.group(2)
        if body.startswith("dsff install") and "arguments" not in out:
            out["arguments"] = body[len("dsff install") :].strip()
        elif body.startswith("started by "):
            out["launcher"] = body[len("started by ") :]
        elif body.startswith("port_batch "):
            out["port_batch"] = body[len("port_batch ") :]
        elif body.startswith("install: "):
            out["install_ps1"] = body[len("install: ") :]
        elif body.startswith("stage ") and ": " in body:
            key, _, rest = body[len("stage ") :].partition(": ")
            out.setdefault("stages", {})[key] = rest
            if key == "install" and rest.startswith("done") and out["started"]:
                start = datetime.datetime.fromisoformat(out["started"])
                done = datetime.datetime.combine(start.date(), datetime.time.fromisoformat(m.group(1)))
                if done < start:  # past midnight
                    done += datetime.timedelta(days=1)
                out["installed_at"] = done.isoformat()
        elif body.startswith("FAILED in "):
            out["outcome"] = body
        elif body == "interrupted":
            out["outcome"] = "interrupted (Ctrl+C)"
    stages = out.get("stages", {})
    if out["outcome"] == "unfinished or interrupted" and stages:
        last = list(stages)[-1]
        if last == "install" or (last == "plugin" and "--build-only" in out.get("arguments", "")):
            out["outcome"] = "finished"
    if "--status" in out.get("arguments", "") or "--uninstall" in out.get("arguments", ""):
        out["outcome"] = "status/uninstall"
    return out


def install_method(record: dict | None, runs: list[dict], repo: Path) -> str:
    """How the installed cars got there: by install.cmd, dsff install, or tools/install.ps1 run
    by hand (the old README's way, which builds into out/install-build)."""
    if not install.installed(record):
        state = (record or {}).get("state")
        return "not installed" + (f" ({state})" if state else "")
    built = str(record.get("build") or "")
    try:
        when = datetime.datetime.fromisoformat(str(record.get("installed")))
    except ValueError:
        when = None
    for run in runs:
        if when is None or not run.get("installed_at"):
            continue
        done = datetime.datetime.fromisoformat(run["installed_at"])
        if done - datetime.timedelta(minutes=30) <= when <= done:  # install.ps1 records the start
            by = run.get("launcher") or "dsff install or install.cmd"
            return f"{by} ({run['log']})"
    if built.replace("/", "\\").lower().endswith("\\out\\install-build"):
        return "tools\\install.ps1 run by hand (it built into out\\install-build: the old README's way)"
    note = ""
    if built and not Path(built).resolve().is_relative_to(Path(repo).resolve()):
        note = f"; from a build in another folder, {built}"
    return "tools\\install.ps1 run by hand, or dsff install from another folder (no log of it here)" + note


# --- the PC ------------------------------------------------------------------------------------------


def _reg(root, key: str, name: str):
    try:
        import winreg

        with winreg.OpenKey(root, key) as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except (OSError, ImportError):
        return None


def windows_info() -> dict:
    out = {"platform": platform.platform()}
    if os.name != "nt":
        return out
    import winreg

    nt = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    for name in ("ProductName", "DisplayVersion", "CurrentBuild", "UBR", "EditionID"):
        out[name] = _reg(winreg.HKEY_LOCAL_MACHINE, nt, name)
    with contextlib.suppress(TypeError, ValueError):  # ProductName still says Windows 10 on 11
        if int(out["CurrentBuild"]) >= 22000 and str(out["ProductName"]).startswith("Windows 10"):
            out["ProductName"] = "Windows 11" + str(out["ProductName"])[len("Windows 10") :]
    return out


def locale_info() -> dict:
    out = {"python": locale.getlocale(), "preferred_encoding": locale.getpreferredencoding(False)}
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(85)
        if k32.GetUserDefaultLocaleName(buffer, 85):
            out["user"] = buffer.value
        if k32.GetSystemDefaultLocaleName(buffer, 85):
            out["system"] = buffer.value
        out["ansi_code_page"] = k32.GetACP()
        out["oem_code_page"] = k32.GetOEMCP()
    return out


def memory_bytes() -> tuple[int | None, int | None]:
    """(total, available) RAM."""
    if os.name != "nt":
        return None, None
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("load", ctypes.c_ulong),
            ("total", ctypes.c_ulonglong),
            ("avail", ctypes.c_ulonglong),
            ("total_page", ctypes.c_ulonglong),
            ("avail_page", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("avail_virtual", ctypes.c_ulonglong),
            ("avail_ext", ctypes.c_ulonglong),
        ]

    status = Status()
    status.length = ctypes.sizeof(Status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None, None
    return status.total, status.avail


def cpu_name() -> str:
    if os.name == "nt":
        import winreg

        name = _reg(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
        if name:
            return str(name).strip()
    return platform.processor() or "?"


def find_uv() -> str | None:
    """uv as tools/release/launcher.ps1 finds it."""
    found = shutil.which("uv")
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for folder in (home / ".local" / "bin", home / ".cargo" / "bin"):
        if (folder / "uv.exe").is_file():
            return str(folder / "uv.exe")
    return None


def free_space(paths: dict[str, Path | None]) -> dict[str, dict]:
    out = {}
    for label, path in paths.items():
        if path is None:
            continue
        try:
            anchor = Path(path).resolve().anchor or str(path)
            free = install.free_bytes(Path(path))
        except OSError:
            continue
        out.setdefault(anchor, {"free": free, "for": []})["for"].append(label)
    return out


def system_info(repo: Path, game: Path | None) -> dict:
    total, avail = memory_bytes()
    uv = find_uv()
    return {
        "windows": windows_info(),
        "locale": locale_info(),
        "cpu": cpu_name(),
        "logical_cpus": os.cpu_count(),
        "ram_bytes": total,
        "ram_free_bytes": avail,
        "free_space": free_space({"this folder": Path(repo), "game": game}),
        "python": sys.version.split()[0],
        "python_bits": 64 if sys.maxsize > 2**32 else 32,
        "python_exe": sys.executable,
        "uv": run_text([uv, "--version"]) if uv else None,
        "uv_path": uv,
        "vc_runtime_x86": install.vc_runtime_x86(),
        "launcher": os.environ.get("DSFF_LAUNCHER"),
        "gentle": (Path(repo) / "out" / "gentle").exists(),
        "dsf_game_dir": {
            "this window": os.environ.get("DSF_GAME_DIR"),
            "user": user_game_dir(),
        },
    }


def user_game_dir() -> str | None:
    if os.name != "nt":
        return None
    import winreg

    return _reg(winreg.HKEY_CURRENT_USER, "Environment", "DSF_GAME_DIR")


def game_folder(given: Path | None) -> tuple[Path | None, str]:
    """The game folder: --game, $DSF_GAME_DIR, else the one saved for the user."""
    if given:
        return Path(given), "--game"
    if os.environ.get("DSF_GAME_DIR"):
        return Path(os.environ["DSF_GAME_DIR"]), "DSF_GAME_DIR"
    saved = user_game_dir()
    if saved:
        return Path(saved), "DSF_GAME_DIR (saved for the user)"
    return None, "DSF_GAME_DIR is not set"


# --- the game folder -------------------------------------------------------------------------------


def exe_info(game: Path) -> dict:
    exe = Path(game) / "Driver.exe"
    if not exe.is_file():
        return {"present": False}
    build = install.exe_build(exe)
    return {
        "present": True,
        "bytes": exe.stat().st_size,
        "sha256": install.sha256(exe),
        "pe_timestamp": f"{build[0]:#x}" if build else None,
        "image_size": f"{build[1]:#x}" if build else None,
        "is_1_04": build == (install.EXE_TIMESTAMP, install.EXE_IMAGE_SIZE),
    }


def build_source(relative: str, build: Path | None, repo: Path) -> Path | None:
    """The file of the build (or of this copy) that install.ps1 installs as `relative`."""
    rel = relative.replace("/", "\\").lower()
    gui = build / "gui" if build else None
    table = {
        "install\\dngvehicles.sp": build / "dngvehicles.sp" if build else None,
        "install\\gui.sp": gui / "gui.sp" if gui else None,
        "media\\fonts.fchunk": gui / "Fonts.fchunk" if gui else None,
        "common\\userluascripts\\dsfforza\\ported.lua": build / "dngvehicles.lua" if build else None,
        "common\\userluascripts\\dsfforza\\hud.lua": gui / "hud.lua" if gui else None,
        "common\\userluascripts\\bootstrap.lua": repo / "tools" / "game" / "lua" / "player" / "bootstrap.lua",
        "common\\userluascripts\\dsfforza\\register.lua": repo / "tools" / "game" / "lua" / "dsfforza" / "register.lua",
    }
    if rel in table:
        return table[rel]
    if rel.startswith("locale\\") and rel.endswith("\\global.fchunk") and gui:
        return gui / Path(*relative.replace("\\", "/").split("/"))
    if rel == "plugins\\dsfforza.asi":
        for path in (repo / "plugin" / "dsfforza.asi", repo / "out" / "asi" / "dsfforza.asi"):
            if path.is_file():
                return path
    return None


def same_file(a: Path, b: Path, a_hash: str | None = None) -> bool | None:
    """Same bytes (by SHA-256; by size and date past HASH_LIMIT), None when b is missing."""
    if not b or not Path(b).is_file():
        return None
    sa, sb = Path(a).stat(), Path(b).stat()
    if sa.st_size != sb.st_size:
        return False
    if sa.st_size > HASH_LIMIT:
        return int(sa.st_mtime) == int(sb.st_mtime)  # install.ps1's Copy-Item keeps the date
    return (a_hash or install.sha256(a)) == install.sha256(b)


def install_status(game: Path, record: dict | None, build: Path | None, repo: Path) -> dict:
    """tools/install.ps1 -Status: each recorded file as installed, changed or missing; and
    whether it is the build's (or this copy's) file."""
    out: dict = {
        "state": (record or {}).get("state") or ("installed" if record else "not installed"),
        "installed": (record or {}).get("installed"),
        "cars": len((record or {}).get("cars") or []),
        "from_build": (record or {}).get("build"),
        "files": [],
    }
    for e in (record or {}).get("entries") or []:
        if not isinstance(e, dict):
            continue
        relative = str(e.get("path", ""))
        target = Path(game) / Path(*relative.replace("\\", "/").split("/"))
        row = {"path": relative, "action": e.get("action")}
        if not target.is_file():
            row["now"] = "missing"
        else:
            size = target.stat().st_size
            row["bytes"] = size
            digest = None
            if size > HASH_LIMIT:
                row["now"] = "not hashed (large; compared by size and date)"
            else:
                digest = install.sha256(target)
                row["now"] = "as installed" if digest == e.get("installed_sha256") else "CHANGED since install"
            source = build_source(relative, build, repo)
            if source is not None:
                same = same_file(target, source, digest)
                row["matches_build"] = same
        out["files"].append(row)
    return out


def archive_state(game: Path, status: dict) -> str:
    archive = Path(game) / "Install" / "dngvehicles.sp"
    if not archive.is_file():
        return "missing"
    size = archive.stat().st_size
    if size == STOCK_ARCHIVE_SIZE:
        return "stock 1.04" if install.sha256(archive) == install.STOCK_ARCHIVE_SHA256 else "stock size, other bytes"
    for row in status["files"]:
        if row["path"].lower() == "install\\dngvehicles.sp":
            if row.get("matches_build"):
                return f"the dsfforza build ({size_text(size)})"
            return f"a dsfforza install ({size_text(size)}; not its build's archive)"
    return f"not stock, not installed by dsfforza ({size_text(size)}): another mod?"


def other_mods(game: Path, record: dict | None, repo: Path) -> dict:
    game = Path(game)
    created = {
        str(e.get("path", "")).lower().replace("/", "\\")
        for e in (record or {}).get("entries") or []
        if isinstance(e, dict) and e.get("action") == "created" and install.installed(record)
    }
    loaders = []
    for name in install.ASI_LOADER_NAMES:
        path = game / name
        if path.is_file():
            data = path.read_bytes()
            loaders.append(
                {
                    "name": name,
                    "bytes": len(data),
                    "asi_loader": b"InitializeASI" in data or b"asi-loader" in data.lower(),
                    "installed_by_dsfforza": name.lower() in created,
                }
            )
    asis = []
    for folder in ASI_FOLDERS:
        where = game / folder if folder else game
        if not where.is_dir():
            continue
        for path in sorted(where.glob("*.asi")):
            rel = f"{folder}\\{path.name}" if folder else path.name
            if folder == "plugins" and path.name.lower() in OURS_ASI:
                continue
            asis.append({"path": rel, "bytes": path.stat().st_size, "modified": stamp(path)})
    hook = game / "plugins" / "DriverNGHook.asi"
    bootstrap = game / "Common" / "UserLuaScripts" / "bootstrap.lua"
    ours_lua = Path(repo) / "tools" / "game" / "lua" / "player" / "bootstrap.lua"
    bootstrap_state = "none"
    if bootstrap.is_file():
        if ours_lua.is_file() and install.sha256(bootstrap) == install.sha256(ours_lua):
            bootstrap_state = "dsfforza's"
        elif "common\\userluascripts\\bootstrap.lua" in created:
            bootstrap_state = "dsfforza's (another version)"
        else:
            bootstrap_state = "another mod's"
    user_lua = game / "Common" / "UserLuaScripts"
    stock = {}
    for relative, digest in install.STOCK_FILES.items():
        path = game / relative
        if not path.is_file():
            stock[relative] = "missing"
        elif path.stat().st_size > HASH_LIMIT:
            stock[relative] = "not stock (size)"
        else:
            stock[relative] = "stock" if install.sha256(path) == digest else "not stock"
    return {
        "asi_loader": install.find_asi_loader(game),
        "loader_dlls": loaders,
        "drivernghook": {
            "present": hook.is_file(),
            "sha256": install.sha256(hook) if hook.is_file() else None,
            "installed_by_dsfforza": "plugins\\drivernghook.asi" in created,
        },
        "other_asi": asis,
        "bootstrap_lua": bootstrap_state,
        "user_lua_scripts": sorted(
            str(p.relative_to(user_lua))
            for p in user_lua.rglob("*")
            if p.is_file() and "dsfforza" not in p.relative_to(user_lua).parts
        )
        if user_lua.is_dir()
        else [],
        "stock_files": stock,
    }


def root_listing(game: Path) -> list[dict]:
    """The game folder's top level (names, sizes): mod folders and files show up here."""
    out = []
    for path in sorted(Path(game).iterdir(), key=lambda p: p.name.lower()):
        try:
            if path.is_dir():
                out.append({"name": path.name + "\\"})
            else:
                out.append(file_entry(path))
        except OSError:
            continue
    return out


def dump_summary(path: Path) -> str:
    """formats/minidump's summary of a crash dump (the exception and the faulting module)."""
    from dsfforza.formats import minidump

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            minidump.main([str(path), "--stack", "32"])
    except (SystemExit, Exception) as error:  # a damaged dump must not stop the report
        buffer.write(f"\n(could not read the dump: {type(error).__name__}: {error})\n")
    return buffer.getvalue()


def game_info(game: Path | None, how: str, repo: Path, build: Path | None, files: dict[str, str]) -> dict:
    if game is None:
        return {"path": None, "found_by": how}
    out: dict = {"path": str(game), "found_by": how, "exists": Path(game).is_dir()}
    if not (Path(game) / "Driver.exe").is_file():
        out["problem"] = f"no Driver.exe in {game}"
        return out
    record = install.read_install_record(game)
    status = install_status(game, record, build, repo)
    out.update(
        {
            "driver_exe": exe_info(game),
            "archive": archive_state(game, status),
            "dsfforza": status,
            "mods": other_mods(game, record, repo),
            "root": root_listing(game),
            "under_program_files": "program files" in str(Path(game).resolve()).lower(),
        }
    )
    asi_log = Path(game) / "plugins" / "dsfforza.log"
    out["dsfforza_log"] = file_entry(asi_log) if asi_log.is_file() else None
    if asi_log.is_file():
        files["logs/dsfforza.log"] = trimmed(asi_log)
    out["logs"] = []
    for name in DSF_LOGS:
        path = Path(game) / name
        if path.is_file():
            out["logs"].append(file_entry(path))
            files[f"logs/{name}"] = trimmed(path)
    dumps = sorted(Path(game).glob("*.dmp"), key=lambda p: p.stat().st_mtime, reverse=True)
    out["crash_dumps"] = [file_entry(p) for p in dumps[:10]]
    if dumps and dumps[0].stat().st_size <= DUMP_LIMIT:
        files[f"logs/{dumps[0].stem}.txt"] = dump_summary(dumps[0])
    for relative in SETTINGS:
        path = Path(game) / relative
        if path.is_file() and path.stat().st_size < 4 * install.MIB:
            files["files/" + relative] = path.read_text(encoding="utf-8-sig", errors="replace")
    return out


def save_info() -> dict:
    """The save folder: where it is and its files' names and sizes (never the files)."""
    save = install.default_save_dir()
    out: dict = {"path": str(save), "exists": save.is_dir()}
    if save.is_dir():
        out["files"] = [file_entry(p) for p in sorted(save.iterdir()) if p.is_file()]
    return out


def graphics_ini() -> Path:
    return install.documents_dir() / "Ubisoft" / "Driver San Francisco" / "graphics.ini"


# --- the discs and the build ------------------------------------------------------------------------


def iso_args(port_batch: str | None) -> list[Path]:
    """The --iso paths of a logged port_batch command line."""
    if not port_batch:
        return []
    return [Path(m) for m in re.findall(r"--iso (.+?\.iso)(?= --|$)", port_batch, re.IGNORECASE)]


def disc_info(repo: Path, extra: list[Path]) -> dict:
    """The disc images: assets/ (every file, by name and size) and those an install log
    names with --iso, each identified by its content."""
    from dsfforza.build import forza_roster

    assets = Path(repo) / "assets"
    listing = [file_entry(p) for p in sorted(assets.iterdir()) if p.is_file()] if assets.is_dir() else []
    images = sorted(assets.glob("*.iso")) if assets.is_dir() else []
    seen = {os.path.normcase(p.resolve()) for p in images}
    images += [p for p in extra if p.is_file() and os.path.normcase(p.resolve()) not in seen]
    discs = forza_roster.identify_all(images)
    found, notes = forza_roster.disc_summary(discs)
    return {
        "assets_folder": listing,
        "images": [
            {
                "path": str(d.path),
                "bytes": d.path.stat().st_size if d.path.is_file() else None,
                "is": d.where,
            }
            for d in discs
        ],
        "found": found,
        "notes": notes,
    }


def build_info(build: Path, files: dict[str, str]) -> dict:
    out: dict = {"path": str(build), "exists": Path(build).is_dir()}
    for name in ("build.new", "build.partial"):
        other = Path(build).with_name(name)
        if other.is_dir():
            out.setdefault("interrupted", []).append(other.name)
    report = read_json(Path(build) / "batch-report.json")
    if report is None:
        return out
    ok = [c for c in report.get("ok") or [] if isinstance(c, dict)]
    failed = [f for f in report.get("failed") or [] if isinstance(f, dict)]
    reuse = report.get("reuse") or {}
    out.update(
        {
            "finished": stamp(Path(build) / "batch-report.json"),
            "cars": len(ok),
            "failed": [
                {"media": f.get("media"), "cause": f.get("cause"), "error": str(f.get("error") or "")[:500]}
                for f in failed
            ],
            "failed_by_cause": report.get("failed_by_cause"),
            "fallbacks": report.get("fallbacks"),
            "archive_bytes": report.get("archive_bytes"),
            "jobs": report.get("jobs"),
            "timings_s": report.get("timings_s"),
            "reused": len(reuse.get("reused") or []),
            "rebuilt": len(reuse.get("rebuilt") or {}),
            "host_sound": (report.get("forza_sound") or {}).get("host_sound"),
            "gui_problem": install.gui_problem(Path(build)),
            # spool id -> car, to find the car of a garage ID or a log line
            "car_table": [
                [c.get("spool_id"), c.get("media"), c.get("game"), c.get("host"), c.get("lint"), c.get("fallback")]
                for c in ok
            ],
        }
    )
    manifest = read_json(Path(build) / "dngvehicles.json")
    if manifest:
        out["options"] = manifest.get("batch")
        out["created"] = manifest.get("created")
    for f in failed[:FAILED_CAR_LOGS]:
        log = Path(build) / "work" / f"{f.get('media')}.log"
        if log.is_file():
            files[f"logs/cars/{log.name}"] = trimmed(log, CAR_LOG_LIMIT, 8 * 1024)
    return out


def plugin_info(repo: Path) -> dict:
    out = {}
    for path in (Path(repo) / "plugin" / "dsfforza.asi", Path(repo) / "out" / "asi" / "dsfforza.asi"):
        if path.is_file():
            out[str(path.relative_to(repo))] = {
                "sha256": install.sha256(path),
                "stamp": read_json(install.stamp_path(path)),
            }
    return out


def tools_info(repo: Path) -> dict:
    from dsfforza.build import thirdparty

    root = Path(repo) / "out" / "tools-dl"
    out = {}
    for name in thirdparty.TOOLS:
        try:
            out[name] = thirdparty.ready(name, root, Path(repo))
        except OSError:
            out[name] = None
    return out


# --- the report -------------------------------------------------------------------------------------


def collect(repo: Path = REPO, game: Path | None = None, now: datetime.datetime | None = None) -> tuple[dict, dict]:
    """(report.json's data, {name in the zip: text}). Each part that fails is noted, not fatal."""
    repo = Path(repo)
    now = now or datetime.datetime.now()
    files: dict[str, str] = {}
    errors: dict[str, str] = {}

    def part(name: str, fn, *args, default=None):
        try:
            return fn(*args)
        except Exception as error:  # a report is wanted most when something is broken
            errors[name] = f"{type(error).__name__}: {error}"
            return default

    game, how = game_folder(game)
    logs = install_logs(repo)
    runs = [log_run(p) for p in logs]
    for path in logs[:INSTALL_LOGS]:
        files[f"logs/{path.name}"] = part(path.name, trimmed, path, default="")
    record = install.read_install_record(game) if game else None
    build = repo / "out" / "build"
    recorded = Path(record["build"]) if record and record.get("build") else None
    compare = recorded if recorded and recorded.is_dir() else build
    extra_isos = [p for run in runs[:5] for p in iso_args(run.get("port_batch"))]
    report = {
        "report": {"created": now.isoformat(timespec="seconds"), "folder": str(repo)},
        "version": part("version", version_info, repo, default={"kind": "?"}),
        "installed_by": part("installed_by", install_method, record, runs, repo, default="?"),
        "install_runs": runs[:10],
        "system": part("system", system_info, repo, game, default={}),
        "game": part("game", game_info, game, how, repo, compare, files, default={"path": str(game)}),
        "save": part("save", save_info, default={}),
        "discs": part("discs", disc_info, repo, extra_isos, default={}),
        "build": part("build", build_info, build, files, default={}),
        "plugin": part("plugin", plugin_info, repo, default={}),
        "downloads": part("downloads", tools_info, repo, default={}),
    }
    if recorded and compare != build:
        report["build"]["installed_from"] = str(recorded)
    ini = part("graphics.ini", graphics_ini)
    if ini and ini.is_file():
        files["files/graphics.ini"] = part("graphics.ini", ini.read_text, "utf-8", "replace", default="")
    report["errors"] = errors
    return report, files


def summary_lines(r: dict) -> list[str]:
    """The short plain-text summary (also summary.txt in the zip)."""
    lines = [f"driversf-forza report, {r['report']['created'].replace('T', ' ')}"]
    lines.append(f"Version:    {version_text(r['version']) if r['version'].get('kind') != '?' else '?'}")
    lines.append(f"Installed:  {r['installed_by']}")
    s = r.get("system") or {}
    w = s.get("windows") or {}
    loc = s.get("locale") or {}
    if s:
        build = f"{w.get('CurrentBuild')}.{w.get('UBR')}" if w.get("CurrentBuild") else ""
        lines.append(
            f"Windows:    {w.get('ProductName') or w.get('platform')} {w.get('DisplayVersion') or ''} {build}".rstrip()
            + f"; locale {loc.get('user') or loc.get('python')}, code page {loc.get('ansi_code_page', '?')}"
        )
        lines.append(
            f"PC:         {s.get('cpu')}, {s.get('logical_cpus')} threads, {size_text(s.get('ram_bytes'))} RAM; free: "
            + ", ".join(f"{d.rstrip(chr(92))} {size_text(v['free'])}" for d, v in (s.get("free_space") or {}).items())
        )
        lines.append(
            f"Tools:      Python {s.get('python')}, {s.get('uv') or 'uv not found'}, VC++ x86 runtime "
            + ("present" if s.get("vc_runtime_x86") else "MISSING")
        )
    g = r.get("game") or {}
    if not g.get("path"):
        lines.append(f"Game:       not found ({g.get('found_by')})")
    elif g.get("problem"):
        lines.append(f"Game:       {g['path']}: {g['problem']}")
    else:
        exe = g.get("driver_exe") or {}
        version = "1.04" if exe.get("is_1_04") else f"NOT 1.04 (PE timestamp {exe.get('pe_timestamp')})"
        lines.append(
            f"Game:       {g['path']} ({g.get('found_by')}); Driver.exe {version}; archive: {g.get('archive')}"
        )
        d = g.get("dsfforza") or {}
        rows = d.get("files") or []
        if rows:
            changed = [x["path"] for x in rows if x.get("now") in ("missing", "CHANGED since install")]
            other = [x["path"] for x in rows if x.get("matches_build") is False]
            text = f"{d.get('state')} {d.get('installed') or ''}, {d.get('cars')} cars, {len(rows)} files"
            text += f"; changed or missing: {', '.join(changed)}" if changed else "; all as installed"
            text += (
                f"; not the files of the build it came from: {', '.join(other)}"
                if other
                else "; same as the build it came from"
            )
            lines.append(f"dsfforza:   {text}")
        else:
            lines.append(f"dsfforza:   {d.get('state', 'not installed')}")
        m = g.get("mods") or {}
        if m:
            parts = [f"ASI loader {m.get('asi_loader') or 'none'}"]
            hook = m.get("drivernghook") or {}
            parts.append("DriverNGHook " + ("present" if hook.get("present") else "missing"))
            parts.append(f"bootstrap.lua {m.get('bootstrap_lua')}")
            if m.get("other_asi"):
                parts.append("other .asi: " + ", ".join(a["path"] for a in m["other_asi"]))
            changed = [k for k, v in (m.get("stock_files") or {}).items() if v != "stock"]
            if changed and not rows:
                parts.append("not stock: " + ", ".join(changed))
            lines.append(f"Mods:       {'; '.join(parts)}")
        logs = [f"dsfforza.log {stamp_text(g.get('dsfforza_log'))}" if g.get("dsfforza_log") else "no dsfforza.log"]
        logs += [f"{x['name']} {stamp_text(x)}" for x in g.get("logs") or []]
        dumps = g.get("crash_dumps") or []
        if dumps:
            logs.append(f"crash dumps: {len(dumps)}, newest {dumps[0]['modified'][:16].replace('T', ' ')}")
        lines.append(f"Game logs:  {', '.join(logs)}")
    discs = r.get("discs") or {}
    images = discs.get("images") or []
    if images:
        lines.append(f"Discs:      {len(images)} images:")
        lines += [f"              {Path(i['path']).name} ({size_text(i.get('bytes'))}): {i['is']}" for i in images]
        lines += [f"              note: {n}" for n in discs.get("notes") or []]
    elif discs:
        lines.append(
            f"Discs:      no .iso in assets (other files there: {len(discs.get('assets_folder') or [])})"
            if discs.get("assets_folder")
            else "Discs:      none in assets"
        )
    b = r.get("build") or {}
    if b.get("cars") is not None:
        failed = b.get("failed") or []
        options = b.get("options") or {}
        lines.append(
            f"Last build: {b.get('finished', '').replace('T', ' ')}: {b['cars']} cars, {len(failed)} failed; "
            f"id policy {options.get('id_policy', '?')}"
            + (f"; interrupted build left: {', '.join(b['interrupted'])}" if b.get("interrupted") else "")
        )
        lines += [f"              {f['media']}: {f['cause']}" for f in failed[:10]]
        if len(failed) > 10:
            lines.append(f"              ... {len(failed) - 10} more in report.json")
        if b.get("gui_problem"):
            lines.append(f"              GUI: {b['gui_problem']}")
    else:
        lines.append("Last build: none in out\\build")
    runs = r.get("install_runs") or []
    if runs:
        last = runs[0]
        lines.append(f"Last run:   {last['log']}: {last['outcome']}")
    if r.get("errors"):
        lines.append("Report errors: " + "; ".join(f"{k}: {v}" for k, v in r["errors"].items()))
    return lines


def stamp_text(entry: dict | None) -> str:
    if not entry:
        return ""
    return f"({size_text(entry.get('bytes'))}, {str(entry.get('modified', ''))[:16].replace('T', ' ')})"


def write_zip(report: dict, files: dict[str, str], summary: list[str], target: Path, redact: Redactor) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("summary.txt", redact("\r\n".join(summary) + "\r\n"))
        z.writestr("report.json", redact(json.dumps(report, indent=1, ensure_ascii=False, default=str)) + "\n")
        for name, text in sorted(files.items()):
            z.writestr(name, redact(text or ""))
    part.replace(target)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsff report",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n", 2)[2],
    )
    parser.add_argument("--game", type=Path, help="the game folder (default: $DSF_GAME_DIR)")
    parser.add_argument("--out", type=Path, help="the folder for the zip (default: this folder)")
    parser.add_argument("--repo", type=Path, default=REPO, help=argparse.SUPPRESS)  # another copy to report on
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="replace")
    repo = Path(args.repo).resolve()
    now = datetime.datetime.now()
    print("Collecting the report (this takes a few seconds)...", flush=True)
    report, files = collect(repo, args.game, now)
    redact = Redactor()
    lines = summary_lines(report)
    target = Path(args.out or repo) / f"driversf-forza-report-{now:%Y%m%d-%H%M%S}.zip"
    write_zip(report, files, lines, target, redact)
    print()
    for line in lines:
        print(redact(line))
    print()
    print(f"Report: {redact(str(target.resolve()))}")
    print("Attach this zip to your bug report (GitHub issue or Discord), with a screenshot and the car's name.")
    print("It holds no save files, disc images or game files; your user folder is replaced by %USERPROFILE%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
