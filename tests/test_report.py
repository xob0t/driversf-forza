"""dsff report (src/dsfforza/build/report.py): what it collects from a fake release folder,
game folder and logs, the redaction of the user's folder, log trimming, how the install
was made, and `dsff install --report`. Offline, no game files, well under a second.

uv run python -m tests report
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from dsfforza.build import install, report

PROFILE = r"C:\Users\Jane Doe"


def _touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _exe(path: Path) -> None:
    data = bytearray(1024)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<I", data, 0x80 + 8, install.EXE_TIMESTAMP)
    struct.pack_into("<I", data, 0x80 + 24 + 56, install.EXE_IMAGE_SIZE)
    _touch(path, bytes(data))


# --- redaction, trimming ---------------------------------------------------------------------------


def test_redactor_replaces_the_profile_folder_in_every_spelling():
    r = report.Redactor(PROFILE, "Jane Doe")
    assert r(r"C:\Users\Jane Doe\Desktop\dsf") == r"%USERPROFILE%\Desktop\dsf"
    assert r("c:/users/jane doe/x.iso") == "%USERPROFILE%/x.iso"
    assert r(json.dumps({"p": PROFILE + r"\out"})) == '{"p": "%USERPROFILE%\\\\out"}'
    assert r(f"in {PROFILE}.") == "in %USERPROFILE%."
    assert r(rf"'{PROFILE}'") == "'%USERPROFILE%'"
    # another drive's Users folder: the name goes, the drive stays
    assert r(r"D:\Users\Jane Doe\Documents") == r"D:\Users\%USERNAME%\Documents"
    # not another user's folder that starts with the same name
    assert r(r"C:\Users\Jane Doe2\x") == r"C:\Users\Jane Doe2\x"
    assert r(r"C:\Users\Jane Doe.old\x") == r"C:\Users\Jane Doe.old\x"
    assert r(r"D:\Games\Driver San Francisco") == r"D:\Games\Driver San Francisco"


def test_trimmed_keeps_the_start_and_the_end_of_a_long_log():
    with tempfile.TemporaryDirectory() as tmp:
        short = _touch(Path(tmp) / "short.log", b"one\ntwo\n")
        assert report.trimmed(short) == "one\ntwo\n"
        long = _touch(Path(tmp) / "long.log", b"S" * 100 + b"m" * 10_000 + b"E" * 100)
        text = report.trimmed(long, limit=200, head=100)
        assert text.startswith("S" * 100) and text.endswith("E" * 100)
        assert "[... 10000 bytes left out by dsff report ...]" in text
        assert len(text) < 300


# --- version and install method --------------------------------------------------------------------


def test_version_of_a_release_a_checkout_and_a_source_zip():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        assert report.version_info(repo)["kind"] == "source zip"
        assert "not a release" in report.version_text(report.version_info(repo))
        _touch(repo / "VERSION", b"v0.2.0 0123456789abcdef\n")
        v = report.version_info(repo)
        assert (v["kind"], v["version"], v["commit"]) == ("release", "v0.2.0", "0123456789abcdef")
        assert report.version_text(v).startswith("v0.2.0 (release zip")


def _log(repo: Path, name: str, lines: list[str]) -> Path:
    return _touch(repo / "out" / "logs" / name, "\n".join(lines).encode())


def test_install_method_from_the_logs_and_the_record():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        path = _log(
            repo,
            "install-20261027-235500.log",
            [
                "[dsff install 23:55:00] dsff install ",
                "[dsff install 23:55:00] started by install.cmd",
                r"[dsff install 23:55:02] port_batch build --out x --iso C:\d\FH 1.iso --iso C:\d\b.ISO --jobs 2",
                "[dsff install 00:10:00] stage install",
                "[dsff install 00:11:00] stage install: done 3 cars into D:\\g",
            ],
        )
        run = report.log_run(path)
        assert run["launcher"] == "install.cmd" and run["outcome"] == "finished"
        assert run["installed_at"] == "2026-10-28T00:11:00"  # past midnight
        assert report.iso_args(run["port_batch"]) == [Path(r"C:\d\FH 1.iso"), Path(r"C:\d\b.ISO")]
        record = {"state": "installed", "installed": "2026-10-28T00:10:05", "build": str(repo / "out" / "build")}
        assert report.install_method(record, [run], repo) == "install.cmd (install-20261027-235500.log)"
        # tools/install.ps1 by hand: no log, its own build folder
        old = {"state": "installed", "installed": "2026-10-01T12:00:00", "build": str(repo / "out" / "install-build")}
        assert "old README" in report.install_method(old, [run], repo)
        assert report.install_method(None, [run], repo) == "not installed"
        assert report.install_method({"state": "uninstalled"}, [], repo) == "not installed (uninstalled)"
        failed = report.log_run(
            _log(
                repo,
                "install-20261029-100000.log",
                ["[dsff install 10:00:00] dsff install ", "[dsff install 10:00:01] FAILED in preflight: - no discs"],
            )
        )
        assert failed["outcome"] == "FAILED in preflight: - no discs" and failed["launcher"] is None


# --- the whole collection ---------------------------------------------------------------------------


def _fake_setup(root: Path) -> tuple[Path, Path, Path]:
    """A release folder with a build and logs, a game folder with a dsfforza install and
    other mods, and a Documents folder with a save. Returns (repo, game, documents)."""
    repo, game, docs = root / "release", root / "game", root / "docs"
    _touch(repo / "VERSION", b"v0.2.0 abc123\n")
    _touch(repo / "tools" / "game" / "lua" / "player" / "bootstrap.lua", b"-- ours\n")
    _touch(repo / "tools" / "game" / "lua" / "dsfforza" / "register.lua", b"-- register\n")
    build = repo / "out" / "build"
    _touch(build / "dngvehicles.sp", b"ARCHIVE")
    _touch(build / "dngvehicles.lua", b"-- ported\n")
    _touch(build / "gui" / "gui.sp", b"GUI")
    _touch(build / "gui" / "Fonts.fchunk", b"FONTS")
    _touch(build / "gui" / "hud.lua", b'[1] = { Picture1 = "dsfforza_bmw_m3e30_91" }\n')
    _touch(
        build / "batch-report.json",
        json.dumps(
            {
                "archive_bytes": 7,
                "jobs": 2,
                "ok": [{"spool_id": 1, "media": "BMW_M3E30_91", "game": "FH1", "host": 205, "lint": "ok"}],
                "failed": [{"media": "FER_F40_87", "cause": "convert", "error": "KeyError: 'x'"}],
                "failed_by_cause": {"convert": 1},
                "reuse": {"reused": [], "rebuilt": {}},
            }
        ).encode(),
    )
    _touch(build / "dngvehicles.json", json.dumps({"batch": {"id_policy": "asi"}, "cars": []}).encode())
    _touch(build / "work" / "FER_F40_87.log", f"Traceback in {PROFILE}\\src\nKeyError: 'x'\n".encode())
    for i in range(5):
        _log(repo, f"install-2026102{i}-100000.log", [f"[dsff install 10:00:00] dsff install --car {i}"])
    _touch(repo / "assets" / "notes.txt", b"hi")
    _touch(repo / "assets" / "fh1 image.iso", b"\0" * 4096)  # not an Xbox disc image

    _exe(game / "Driver.exe")
    installed = {
        "Install/dngvehicles.sp": b"ARCHIVE",
        "Install/gui.sp": b"GUI",
        "media/Fonts.fchunk": b"OTHER FONTS",  # not the build's
        "Common/UserLuaScripts/bootstrap.lua": b"-- ours\n",
        "Common/UserLuaScripts/dsfforza/hud.lua": b"changed after install",
    }
    entries = []
    for relative, data in installed.items():
        _touch(game / relative, data)
        entries.append(
            {
                "path": relative.replace("/", "\\"),
                "action": "replaced" if relative.startswith(("Install", "media")) else "created",
                "installed_sha256": _sha(b"[1] = {}\n" if relative.endswith("hud.lua") else data),
            }
        )
    entries.append({"path": "plugins\\dsfforza.asi", "action": "created", "installed_sha256": "00"})  # missing
    record = {
        "state": "installed",
        "installed": "2026-10-24T10:00:30",
        "build": str(build),
        "cars": ["BMW_M3E30_91"],
        "entries": entries,
    }
    _touch(game / "dsfforza-backup" / "install.json", json.dumps(record).encode())
    _touch(game / "dsfforza-backup" / "Install" / "dngvehicles.sp", b"VANILLA BACKUP")
    _touch(game / "plugins" / "dsfforza.log", b"dsfforza: patched 12 sites\n")
    _touch(game / "plugins" / "dsfforza.ini", b"[hands]\nenabled=1\n")
    _touch(game / "plugins" / "DriverNGHook.asi", b"hook")
    _touch(game / "plugins" / "other_mod.asi", b"other")
    _touch(game / "scripts" / "widescreen.asi", b"ws")
    _touch(game / "dinput8.dll", b"....InitializeASI....")
    _touch(game / "Driver.log", b"L" * 300_000 + f"\n[Lua] user file {PROFILE}\\x\n".encode())
    _touch(game / "Common" / "UserLuaScripts" / "othermod" / "init.lua", b"--")
    _touch(game / "Driver_20261023_145430.dmp", b"MDMP" + b"\0" * 60)
    save = docs / "Ubisoft" / "Driver San Francisco" / "reloaded" / "savegames"
    _touch(save / "autosave.dat", b"SECRET SAVE BYTES")
    _touch(docs / "Ubisoft" / "Driver San Francisco" / "graphics.ini", b"[Graphics]\nWidth=1920\n")
    return repo, game, docs


def test_other_mods_notes_a_chained_bootstrap():
    """install.ps1 -Force keeps another mod's bootstrap.lua as dsfforza/chained-bootstrap.lua."""
    with tempfile.TemporaryDirectory() as tmp:
        repo, game, _ = _fake_setup(Path(tmp))
        assert report.other_mods(game, None, repo)["bootstrap_lua"] == "dsfforza's"
        _touch(game / "Common" / "UserLuaScripts" / "dsfforza" / "chained-bootstrap.lua", b"-- Felony Rework")
        state = report.other_mods(game, None, repo)["bootstrap_lua"]
        assert state == "dsfforza's, chaining another mod's", state


def test_collect_and_zip_a_fake_setup():
    with tempfile.TemporaryDirectory() as tmp:
        repo, game, docs = _fake_setup(Path(tmp))
        save = docs / "Ubisoft" / "Driver San Francisco" / "reloaded" / "savegames"
        with (
            mock.patch.object(install, "documents_dir", lambda: docs),
            mock.patch.object(install, "default_save_dir", lambda: save),
            mock.patch.object(report, "find_uv", lambda: None),
        ):
            data, files = report.collect(repo, game)
        assert data["errors"] == {}
        assert data["version"]["version"] == "v0.2.0"
        g = data["game"]
        assert g["driver_exe"]["is_1_04"] and g["found_by"] == "--game"
        rows = {r["path"]: r for r in g["dsfforza"]["files"]}
        assert rows["Install\\dngvehicles.sp"]["now"] == "as installed"
        assert rows["Install\\dngvehicles.sp"]["matches_build"] is True
        assert rows["media\\Fonts.fchunk"]["matches_build"] is False
        assert rows["Common\\UserLuaScripts\\dsfforza\\hud.lua"]["now"] == "CHANGED since install"
        assert rows["plugins\\dsfforza.asi"]["now"] == "missing"
        assert g["archive"].startswith("the dsfforza build")
        mods = g["mods"]
        assert mods["asi_loader"] == "dinput8.dll"
        assert mods["bootstrap_lua"] == "dsfforza's"
        assert [a["path"] for a in mods["other_asi"]] == ["plugins\\other_mod.asi", "scripts\\widescreen.asi"]
        assert mods["user_lua_scripts"] == ["bootstrap.lua", str(Path("othermod/init.lua"))]
        assert [d["name"] for d in g["crash_dumps"]] == ["Driver_20261023_145430.dmp"]
        b = data["build"]
        assert b["cars"] == 1 and b["failed"][0]["media"] == "FER_F40_87"
        assert b["options"] == {"id_policy": "asi"}
        assert b["car_table"][0][:2] == [1, "BMW_M3E30_91"]
        discs = data["discs"]
        assert [i["is"] for i in discs["images"]] == ["not a Forza disc (not an Xbox 360 disc image)"]
        assert {a["name"] for a in discs["assets_folder"]} == {"notes.txt", "fh1 image.iso"}
        assert [f["name"] for f in data["save"]["files"]] == ["autosave.dat"]
        # the newest three install logs, the game's logs, the failed car's log, settings
        assert sorted(n for n in files if n.startswith("logs/install-")) == [
            f"logs/install-2026102{i}-100000.log" for i in (2, 3, 4)
        ]
        for name in (
            "logs/dsfforza.log",
            "logs/Driver.log",
            "logs/cars/FER_F40_87.log",
            "logs/Driver_20261023_145430.txt",
            "files/plugins/dsfforza.ini",
            "files/dsfforza-backup/install.json",
            "files/graphics.ini",
        ):
            assert name in files, name
        assert len(files["logs/Driver.log"]) < report.LOG_LIMIT + 200
        assert files["logs/Driver.log"].rstrip().endswith("\\x")

        lines = report.summary_lines(data)
        text = "\n".join(lines)
        assert "v0.2.0 (release zip" in text
        assert "Driver.exe 1.04" in text
        assert "changed or missing: Common\\UserLuaScripts\\dsfforza\\hud.lua, plugins\\dsfforza.asi" in text
        assert "not the files of the build it came from: media\\Fonts.fchunk" in text
        assert "FER_F40_87: convert" in text
        assert "ASI loader dinput8.dll" in text

        target = report.write_zip(data, files, lines, Path(tmp) / "out" / "r.zip", report.Redactor(PROFILE, "Jane Doe"))
        with zipfile.ZipFile(target) as z:
            names = z.namelist()
            blob = b"".join(z.read(n) for n in names)
        assert not (Path(tmp) / "out" / "r.zip.part").exists()
        assert "summary.txt" in names and "report.json" in names
        assert not [n for n in names if n.lower().endswith((".iso", ".dat", ".sp", ".dmp", ".exe"))]
        assert b"SECRET SAVE BYTES" not in blob and b"VANILLA BACKUP" not in blob
        assert b"Jane Doe" not in blob and b"%USERPROFILE%" in blob


def test_collect_without_a_game_folder_or_a_build():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DSF_GAME_DIR", None)
        with (
            mock.patch.object(report, "user_game_dir", lambda: None),
            mock.patch.object(report, "find_uv", lambda: None),
            mock.patch.object(install, "default_save_dir", lambda: Path(tmp) / "nosave"),
            mock.patch.object(install, "documents_dir", lambda: Path(tmp) / "nodocs"),
        ):
            data, files = report.collect(Path(tmp))
        assert data["errors"] == {}
        assert data["game"] == {"path": None, "found_by": "DSF_GAME_DIR is not set"}
        assert data["version"]["kind"] == "source zip"
        text = "\n".join(report.summary_lines(data))
        assert "Game:       not found (DSF_GAME_DIR is not set)" in text
        assert "Last build: none" in text
        assert files == {}


def test_a_failing_part_is_noted_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        with (
            mock.patch.object(report, "system_info", side_effect=RuntimeError("boom")),
            mock.patch.object(report, "find_uv", lambda: None),
            mock.patch.object(install, "default_save_dir", lambda: Path(tmp) / "nosave"),
            mock.patch.object(install, "documents_dir", lambda: Path(tmp) / "nodocs"),
        ):
            data, _ = report.collect(Path(tmp), Path(tmp) / "nogame")
        assert data["errors"] == {"system": "RuntimeError: boom"}
        assert "Report errors: system: RuntimeError: boom" in report.summary_lines(data)


def test_dsff_install_report_runs_the_report():
    with mock.patch.object(report, "main", return_value=0) as run:
        assert install.main(["--report", "--game", r"D:\g"]) == 0
        run.assert_called_once_with(["--game", r"D:\g"])
    with mock.patch.object(report, "main", return_value=0) as run:
        assert install.main(["--report"]) == 0
        run.assert_called_once_with([])
