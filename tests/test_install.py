"""dsff install (src/dsfforza/build/install.py): stage skipping, reuse detection,
preflight errors, the progress model and the plain display; the pinned downloads
(src/dsfforza/build/thirdparty.py) and the release zip's file list
(src/dsfforza/build/package.py). Offline, no game files, well under a second.

uv run python -m tests install
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from dsfforza.build import forza_roster, install, package, thirdparty


def _tmp():
    return tempfile.TemporaryDirectory()


def _touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --- jobs, selection ---------------------------------------------------------------------------


def test_default_jobs_follow_the_cpus_and_gentle():
    assert install.default_jobs(16, False) == 8
    assert install.default_jobs(32, False) == 8
    assert install.default_jobs(6, False) == 3
    assert install.default_jobs(1, False) == 1
    assert install.default_jobs(None, False) == 1
    assert install.default_jobs(16, True) == 2


def test_count_cars_merges_lists_and_names():
    with _tmp() as tmp:
        lst = Path(tmp) / "cars.txt"
        lst.write_text("# comment\nNIS_370Z_10\nBMW_M5_12@fm4  # x\n\nnis_370z_10\n", encoding="utf-8")
        assert install.count_cars([lst], []) == 2
        assert install.count_cars([lst], ["FOR_GT_05@fh1", "BMW_M5_12"]) == 3


# --- prepare --------------------------------------------------------------------------------------


def _prepared(out: Path) -> None:
    for variant in ("LuaScripts", "LuaScripts.copy"):
        _touch(out / "lua-src" / variant / "vehiclestats" / "vehiclestats.lua")
    for name in ("fh1", "fm4"):
        _touch(out / "db" / f"{name}_gamedb.slt", b"db")
        _touch(out / "cache" / name / "gamedb.slt", b"db")


def test_prepare_runs_only_when_its_files_are_missing_or_stale():
    with _tmp() as tmp:
        game, out = Path(tmp) / "game", Path(tmp) / "out"
        package_file = _touch(game / "media" / "ScriptsLuaScripts.fchunk")
        os.utime(package_file, (1_000_000, 1_000_000))
        needed, force, why = install.prepare_status(game, out)
        assert needed and not force and "missing" in why
        _prepared(out)
        assert install.prepare_status(game, out) == (False, False, "up to date")
        os.utime(package_file, None)  # the game's scripts changed after the files were written
        lua = out / "lua-src" / "LuaScripts" / "vehiclestats" / "vehiclestats.lua"
        os.utime(lua, (1_000_000, 1_000_000))
        needed, force, why = install.prepare_status(game, out)
        assert needed and force and "newer" in why
        for variant in ("LuaScripts", "LuaScripts.copy"):  # written again after the scripts
            os.utime(out / "lua-src" / variant / "vehiclestats" / "vehiclestats.lua", None)
        (out / "db" / "fm4_gamedb.slt").write_bytes(b"cut short")
        needed, force, why = install.prepare_status(game, out)
        assert needed and not force and "fm4_gamedb.slt" in why
        # FH1 discs only (issue #102): FM4's database can't be written, so it isn't missing.
        (out / "db" / "fm4_gamedb.slt").unlink()
        assert install.prepare_status(game, out, {"FH1"}) == (False, False, "up to date")
        assert "fm4_gamedb.slt missing" in install.prepare_status(game, out, {"FH1", "FM4"})[2]


# --- plugin ----------------------------------------------------------------------------------------


def test_plugin_state_from_its_stamp():
    with _tmp() as tmp:
        asi = Path(tmp) / "dsfforza.asi"
        assert install.plugin_state(asi, "abc") == "missing"
        _touch(asi, b"MZ plugin")
        assert install.plugin_state(asi, "abc") == "unverified"
        stamp = {"sources": "abc", "sha256": install.sha256(asi)}
        install.stamp_path(asi).write_text(json.dumps(stamp), encoding="utf-8")
        assert install.plugin_state(asi, "abc") == "current"
        assert install.plugin_state(asi, "changed sources") == "stale"
        asi.write_bytes(b"MZ another build")
        assert install.plugin_state(asi, "abc") == "stale"


def test_plugin_action_builds_only_when_it_must_and_can():
    assert install.plugin_action("current", vs=True) == "keep"
    assert install.plugin_action("current", vs=False) == "keep"
    assert install.plugin_action("stale", vs=True) == "build"
    assert install.plugin_action("unverified", vs=True) == "build"
    assert install.plugin_action("missing", vs=True) == "build"
    assert install.plugin_action("stale", vs=False) == "use"
    assert install.plugin_action("unverified", vs=False) == "use"
    assert install.plugin_action("missing", vs=False) == "missing"
    assert install.plugin_action("current", vs=True, rebuild=True) == "build"
    assert install.plugin_action("current", vs=False, rebuild=True) == "missing"


def test_asi_sources_digest_ignores_line_ends():
    with _tmp() as tmp:
        root = Path(tmp)
        _touch(root / "src" / "a.cpp", b"int a;\r\nint b;\r\n")
        _touch(root / "build.ps1", b"x")
        first = install.asi_sources_digest(root)
        (root / "src" / "a.cpp").write_bytes(b"int a;\nint b;\n")
        assert install.asi_sources_digest(root) == first
        (root / "src" / "a.cpp").write_bytes(b"int a;\nint c;\n")
        assert install.asi_sources_digest(root) != first


# --- reuse and the build folders ------------------------------------------------------------------


def _build(folder: Path, cars: int) -> Path:
    _touch(folder / "batch-report.json", b"{}")
    (folder / "work").mkdir(parents=True, exist_ok=True)
    for n in range(cars):
        _touch(folder / "work" / f"CAR_{n}.fp.json", b"{}")
    return folder


def test_reuse_sources_prefer_the_last_build_then_an_interrupted_one():
    with _tmp() as tmp:
        out = Path(tmp) / "out"
        dirs = install.BuildDirs.of(out / "build")
        assert dirs.staging.name == "build.new" and dirs.partial.name == "build.partial"
        assert install.reuse_sources(dirs, [], no_reuse=False) == []
        older = _build(out / "final3", 3)
        bigger = _build(out / "final4", 5)
        assert install.reuse_sources(dirs, [], no_reuse=False) == [bigger]  # found under out/
        assert install.reuse_sources(dirs, [], no_reuse=False, discover=False) == []
        _build(dirs.out, 2)
        _build(dirs.partial, 1)
        assert install.reuse_sources(dirs, [older], no_reuse=False) == [dirs.out, dirs.partial, older]
        assert install.reuse_sources(dirs, [dirs.out], no_reuse=False) == [dirs.out, dirs.partial]
        assert install.reuse_sources(dirs, [older], no_reuse=True) == []


def test_earlier_build_breaks_ties_by_the_newest_report():
    with _tmp() as tmp:
        out = Path(tmp)
        a, b = _build(out / "a", 4), _build(out / "b", 4)
        os.utime(a / "batch-report.json", (1_000_000, 1_000_000))
        assert install.earlier_build(out, []) == b
        assert install.earlier_build(out, [b]) == a
        _build(out / "empty", 0)
        assert install.earlier_build(out / "nothing here", []) is None


def test_an_interrupted_build_becomes_partial_and_a_finished_one_replaces_the_last():
    with _tmp() as tmp:
        dirs = install.BuildDirs.of(Path(tmp) / "build")
        assert install.start_staging(dirs) is None
        _build(dirs.staging, 2)  # left by a run that was stopped
        note = install.start_staging(dirs)
        assert "interrupted" in note and (dirs.partial / "work" / "CAR_1.fp.json").exists()
        assert not dirs.staging.exists()
        _touch(dirs.staging / "nothing.txt")  # no work/: just removed
        assert install.start_staging(dirs) is None and not dirs.staging.exists()
        _build(dirs.out, 1)
        _touch(dirs.staging / "dngvehicles.sp", b"new")
        assert install.promote(dirs) == dirs.out
        assert (dirs.out / "dngvehicles.sp").read_bytes() == b"new"
        assert not dirs.staging.exists() and not dirs.partial.exists()
        assert not dirs.out.with_name("build.old").exists()


def test_space_needed_counts_converted_cars_more():
    build_reuse, game = install.space_needed(573, reusing=True)
    build_new, game2 = install.space_needed(573, reusing=False)
    assert build_new > build_reuse and game == game2
    assert 3 * install.GIB < build_reuse < 6 * install.GIB
    assert 12 * install.GIB < build_new < 20 * install.GIB


# --- preflight ----------------------------------------------------------------------------------------


def _exe(path: Path, stamp: int = install.EXE_TIMESTAMP, size: int = install.EXE_IMAGE_SIZE) -> None:
    data = bytearray(1024)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<I", data, 0x80 + 8, stamp)
    struct.pack_into("<I", data, 0x80 + 24 + 56, size)
    _touch(path, bytes(data))


def _args(*argv: str, out: Path):
    args = install.build_parser().parse_args([*argv, "--out", str(out)])
    args.jobs = 1
    return args


def _run(args, running=False, free=1 << 50, vc=True, loader="winmm.dll"):
    probes = install.Probes(
        running=lambda game: running, vc_runtime=lambda: vc, free=lambda path: free, loader=lambda game: loader
    )
    return install.Run(args, install.PlainReporter(io.StringIO()), probes)


def _fake_game(root: Path) -> Path:
    game = root / "game"
    _exe(game / "Driver.exe")
    _touch(game / "Install" / "dngvehicles.sp", b"not the vanilla archive")
    (game / "media").mkdir()
    return game


def _problems(run) -> str:
    try:
        run.preflight()
    except install.StageError as error:
        return str(error)
    return ""


def test_exe_build_reads_the_pe_header():
    with _tmp() as tmp:
        exe = Path(tmp) / "Driver.exe"
        _exe(exe)
        assert install.exe_build(exe) == (install.EXE_TIMESTAMP, install.EXE_IMAGE_SIZE)
        _touch(exe, b"not a PE file" * 10)
        assert install.exe_build(exe) is None
        assert install.exe_build(Path(tmp) / "missing.exe") is None


def test_preflight_needs_the_game_folder():
    with _tmp() as tmp, mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DSF_GAME_DIR", None)
        run = _run(_args("--build-only", out=Path(tmp) / "build"))
        try:
            run.preflight()
            raise AssertionError("no error")
        except install.StageError as error:
            assert "DSF_GAME_DIR is not set" in str(error) and "--game" in error.hint
        run = _run(_args("--build-only", "--game", tmp, out=Path(tmp) / "build"))
        assert "No Driver.exe" in _problems(run)


def test_preflight_lists_every_problem_at_once():
    with _tmp() as tmp:
        root = Path(tmp)
        game = _fake_game(root)
        _exe(game / "Driver.exe", stamp=0x12345678)
        args = _args("--game", str(game), "--iso", str(root / "missing.iso"), "--car", "NIS_370Z_10", out=root / "b")
        text = _problems(_run(args, running=True, free=0, vc=False, loader=None))
        assert "is not Driver: San Francisco 1.04" in text
        assert "not the vanilla 1.04 vehicle archive" in text
        assert "Disc image not found" in text
        assert "Driver.exe is running" in text
        assert "Visual C++" in text
        assert "Not enough disk space" in text
        assert text.count("\n- ") >= 5


def test_preflight_build_only_skips_the_install_checks():
    with _tmp() as tmp:
        root = Path(tmp)
        game = _fake_game(root)
        iso = _touch(root / "fh1.iso")
        args = _args("--build-only", "--game", str(game), "--iso", str(iso), "--car", "X_1", out=root / "b")
        text = _problems(_run(args, running=True, vc=False, loader=None))
        assert "running" not in text and "Visual C++" not in text
        assert "not the vanilla" in text  # the build reads the vanilla archive
        assert "fh1.iso: not an Xbox 360 disc image" in text  # an empty file: no Forza disc at all
        fh1 = forza_roster.Disc(iso, "FH1", 1, 1)  # what tests/test_discs.py's images are
        with (
            mock.patch.object(install, "VANILLA_ARCHIVE_SHA256", install.sha256(game / "Install" / "dngvehicles.sp")),
            mock.patch.object(forza_roster, "identify", return_value=fh1),
        ):
            run = _run(args, running=True)
            info = run.preflight()
            assert "DSF 1.04" in info and "discs FH1" in info and run.isos == [iso]
            assert "no FM4 disc: its cars are skipped" in info


def test_preflight_no_plugin_caps_the_selection_and_other_mods_need_force():
    with _tmp() as tmp:
        root = Path(tmp)
        game = _fake_game(root)
        iso = _touch(root / "fh1.iso")
        lst = root / "many.txt"
        lst.write_text("\n".join(f"CAR_{n}" for n in range(install.VANILLA_ROOM + 1)), encoding="utf-8")
        _touch(game / "Common" / "UserLuaScripts" / "bootstrap.lua", b"-- another mod")
        args = _args("--no-plugin", "--game", str(game), "--iso", str(iso), "--list", str(lst), out=root / "b")
        text = _problems(_run(args))
        assert f"at most {install.VANILLA_ROOM} added cars" in text
        assert "bootstrap.lua belongs to another mod" in text
        forced = _args("--force", "--game", str(game), "--iso", str(iso), "--list", str(lst), out=root / "b")
        assert "bootstrap.lua" not in _problems(_run(forced))
        missing = _args("--game", str(game), "--iso", str(iso), "--list", str(root / "none.txt"), out=root / "b")
        assert "Car list not found" in _problems(_run(missing))


def test_a_reinstall_supplies_the_hook_and_loader_its_uninstall_removes():
    """install.ps1 -Force uninstalls first: a DriverNGHook or ASI loader the earlier
    dsfforza install created is gone by the time it installs, so it must be passed again;
    ones the game had before stay."""
    with _tmp() as tmp:
        game = _fake_game(Path(tmp))
        _touch(game / "plugins" / "DriverNGHook.asi")
        _touch(game / "dinput8.dll")
        run = _run(_args("--game", str(game), out=Path(tmp) / "b"), loader="dinput8.dll")
        run.game = game
        assert run.game_has_hook() and run.game_has_loader()  # no install recorded: the user's own
        created = [
            {"path": "dinput8.dll", "action": "created"},
            {"path": "plugins\\DriverNGHook.asi", "action": "created"},
        ]
        run.record = {"state": "installed", "entries": created}
        assert not run.game_has_hook() and not run.game_has_loader()
        run.record = {"state": "uninstalled", "entries": created}
        assert run.game_has_hook() and run.game_has_loader()


def _gui_build(root: Path, cars: dict[int, str], hud: dict[int, str] | None, files=("gui.sp", "Fonts.fchunk")):
    cars_json = [{"spool_id": i, "media": m} for i, m in cars.items()]
    _touch(root / "dngvehicles.json", json.dumps({"cars": cars_json}).encode())
    for name in files:
        _touch(root / "gui" / name)
    if hud is not None:
        rows = "".join(
            f'\t[{i}] = {{ Manufacturer = 43, Model = 158, Picture1 = "dsfforza_{m.lower()}" }},\n'
            for i, m in hud.items()
        )
        _touch(root / "gui" / "hud.lua", f"return {{\n{rows}}}\n".encode())


def test_gui_problem_needs_the_builds_own_gui():
    """The Ford Ka showed "SHELBY GT500" (a ported car without its hud.lua entry):
    a build without gui/, or with another build's, isn't installed."""
    ka = {404: "FOR_Ka_11", 412: "FOR_ShelbyGT500_10"}
    with _tmp() as tmp:
        root = Path(tmp)
        _gui_build(root / "ok", ka, ka)
        assert install.gui_problem(root / "ok") is None
        _gui_build(root / "nogui", ka, None, files=())
        _touch(root / "nogui" / "batch-report.json", json.dumps({"timings_s": {"gui_error": "OSError: x"}}).encode())
        why = install.gui_problem(root / "nogui")
        assert "without --gui" in why and "gui/hud.lua" in why and "OSError: x" in why
        _gui_build(root / "nofont", ka, ka, files=("gui.sp",))
        assert "gui/Fonts.fchunk" in install.gui_problem(root / "nofont")
        # A subset build: the same IDs hold other cars.
        _gui_build(root / "other", ka, {404: "FOR_ShelbyGT500_10", 412: "FOR_ShelbyGT500_13"})
        why = install.gui_problem(root / "other")
        assert "ID 404: for_shelbygt500_10, not FOR_Ka_11" in why and "2 hud.lua entries" in why
        _gui_build(root / "fewer", ka, {412: "FOR_ShelbyGT500_10"})
        assert "no hud.lua entry for 1 cars (IDs 404)" in install.gui_problem(root / "fewer")


# --- progress -------------------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_tracker_counts_cars_rate_eta_and_failures():
    clock = Clock()
    t = install.Tracker(clock=clock)
    t.handle("plan", cars=["A", "B", "C", "D"], skipped=[{"media": "Z", "error": "not a car on the discs"}])
    assert t.total == 4 and t.failures[0].cause == "not selected"
    t.seconds_hint = 60.0
    t.handle("convert", todo=["A", "B", "C"], reused=["D"], rebuilt={"A": "code dsfforza.x.y", "B": "not built there"})
    assert t.done == 1 and t.eta() == 3 * 60.0 / 1 and t.rebuilt == {"code changed": 1, "not built there": 1}
    t.handle("car_start", media="A", spool_id=1)
    assert "A" in t.running
    clock.now = 60.0
    notes = t.handle("car_retry", media="A", why="without --damage", cause="damage.py: ValueError")
    assert notes[0][0] == "warn"
    t.handle("car_done", media="A", report={"ok": True, "seconds": 60})
    assert t.converted == 1 and t.rate() == 1.0 and t.eta() == 120.0
    t.handle("car_start", media="B", spool_id=2)
    notes = t.handle("car_done", media="B", report={"ok": False, "cause": "extraction", "error": "x"}, log="b.log")
    assert notes == [("error", "B: extraction (log b.log)")] and t.failures[-1].log == "b.log"
    t.handle("verify", cars=["D"])
    t.handle("car_start", media="D", spool_id=4, verify=True)
    t.handle("car_done", media="D", report={"ok": True})
    assert t.verify_done == 1 and t.converted == 2  # a reuse check is not a converted car
    # Issue #133: D came out different; dsff install goes on (its fresh copy, E converted again).
    t.reused = 2
    notes = t.handle("verify_mismatch", cars=["D (DIFFERENT)"], redo=["E"])
    assert notes[0][0] == "warn" and "D (DIFFERENT)" in notes[0][1] and t.reused == 1 and t.phase == "convert"
    assert t.stage == "build"
    t.handle("merge", cars=3)
    assert t.stage == "pack" and "merging 3" in t.pack_detail()
    t.handle("gui", cars=2)
    t.handle("gui_log", text="1 A: picture ...")
    assert t.pack_detail().startswith("garage pictures and badges 1/2")
    t.handle("done", report="r.json")
    assert t.phase == "done"


def test_summary_notes_a_verify_mismatch():
    """Issue #133: the build goes on and the summary says what happened."""
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp) / "build"
        build.mkdir()
        mismatch = {"different": ["BMW_Z4_11"], "failed": [], "converted_again": ["A", "B"], "fresh_copies": []}
        report = {
            "ok": [{"media": m} for m in ("BMW_Z4_11", "A", "B", "C")],
            "failed": [],
            "reuse": {"reused": ["C"], "verify_mismatch": mismatch},
        }
        (build / "batch-report.json").write_text(json.dumps(report), encoding="utf-8")
        run = _run(_args("--build-only", out=build))
        run.read_report(build)
        assert run.summary.reused == 1 and run.summary.built == 3
        assert any("BMW_Z4_11" in n and "2 other reused cars" in n for n in run.summary.notes)


def test_plain_display_is_ascii_and_moves_build_to_pack():
    stream = io.StringIO()
    r = install.PlainReporter(stream)
    r.stage("build", "running")
    r.stage("build", "running", "reusing cars from out\\build")
    r.event("plan", cars=["A"], skipped=[])
    r.event("convert", todo=["A"], reused=[], rebuilt={}, jobs=1, gentle=True)
    r.event("car_start", media="A", spool_id=1)
    r.event("car_done", media="A", report={"ok": True, "seconds": 5})
    r.event("merge", cars=1)
    assert r.states["build"][0] == "done" and r.states["pack"][0] == "running"
    r.note("✗ unicode → ascii", "error")
    text = stream.getvalue()
    text.encode("ascii")
    assert text.count("[build] Build cars...") == 1
    assert "done  A ok" in text and "out/gentle" in text and "merging 1 cars" in text


def test_summary_lines_name_failures_logs_and_uninstall():
    s = install.Summary(
        elapsed=3725,
        build_dir=Path("out/build"),
        in_archive=3,
        built=1,
        reused=2,
        failed=[install.CarFailure("X_1", "extraction", "boom", "out/build/work/X_1.log")],
        archive_bytes=3 * install.GIB,
        game=Path("D:/game"),
        replaced=["Install\\dngvehicles.sp"],
        created=["plugins\\dsfforza.asi"],
        save_backup=Path("out/save-backups/1/savegames"),
        log=Path("out/logs/x.log"),
        installed=True,
    )
    text = "\n".join(install.summary_lines(s))
    assert "1:02:05" in text and "3 in the archive (1 built, 2 reused), 1 failed" in text
    assert "X_1: extraction" in text and "out/build/work/X_1.log" in text.replace("\\", "/")
    assert "3.00 GiB" in text and "dsff install --uninstall" in text and "save-backups" in text


# --- pinned downloads ------------------------------------------------------------------------------------


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_checks_the_hash():
    data = b"release asset"
    good = hashlib.sha256(data).hexdigest().upper()
    with _tmp() as tmp:
        target = Path(tmp) / "x" / "asset.zip"
        thirdparty.download("https://example/asset.zip", target, good, opener=lambda url: Response(data))
        assert target.read_bytes() == data
        target.unlink()
        try:
            thirdparty.download("https://example/asset.zip", target, "0" * 64, opener=lambda url: Response(data))
            raise AssertionError("no error")
        except thirdparty.DownloadError as error:
            assert "SHA-256" in str(error)
        assert not target.exists() and not target.with_name("asset.zip.part").exists()


def test_downloads_use_the_os_certificate_check():
    # Python's own check failed on a fresh Windows (GitHub's release host's root not in the store
    # yet, issue #95); truststore asks Windows, which fetches missing roots.
    import truststore

    seen = {}

    def urlopen(url, context=None, timeout=None):
        seen.update(url=url, context=context, timeout=timeout)
        return Response(b"")

    assert thirdparty.download.__defaults__[-1] is thirdparty.open_url
    assert thirdparty.fetch.__defaults__[-2] is thirdparty.open_url
    with mock.patch.object(thirdparty.urllib.request, "urlopen", urlopen):
        thirdparty.open_url("https://example/asset.zip")
    assert seen["url"] == "https://example/asset.zip"
    assert isinstance(seen["context"], truststore.SSLContext) and seen["timeout"]


def test_fetch_unpacks_a_pinned_zip_once():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("vgmstream-cli.exe", b"MZ")
        z.writestr("COPYING", b"license")
    data = buffer.getvalue()
    tool = thirdparty.Tool(
        "vgm", "r1", "https://example/vgm.zip", hashlib.sha256(data).hexdigest().upper(), "vgm", "vgmstream-cli.exe"
    )
    calls = []

    def opener(url):
        calls.append(url)
        return Response(data)

    with _tmp() as tmp, mock.patch.dict(thirdparty.TOOLS, {"vgm": tool}):
        root = Path(tmp)
        assert not thirdparty.ready("vgm", root, root)
        folder = thirdparty.fetch("vgm", root, root, opener=opener, log=lambda t: None)
        assert (folder / "vgmstream-cli.exe").read_bytes() == b"MZ"
        assert json.loads((folder / "pinned.json").read_text())["version"] == "r1"
        assert thirdparty.ready("vgm", root, root)
        thirdparty.fetch("vgm", root, root, opener=opener, log=lambda t: None)
        assert len(calls) == 1


def test_unpack_refuses_paths_outside_the_folder():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("../evil.txt", b"x")
    with _tmp() as tmp:
        archive = _touch(Path(tmp) / "a.zip", buffer.getvalue())
        try:
            thirdparty.unpack_zip(archive, Path(tmp) / "out")
            raise AssertionError("no error")
        except thirdparty.DownloadError:
            pass
        assert not (Path(tmp) / "evil.txt").exists()


def test_pinned_tools_have_versions_and_hashes():
    for tool in thirdparty.TOOLS.values():
        assert tool.url.startswith("https://github.com/") and tool.version in tool.url
        assert len(tool.sha256) == 64 and tool.sha256 == tool.sha256.upper()


# --- release zip ------------------------------------------------------------------------------------------


def test_package_takes_the_player_files_and_refuses_game_data():
    tracked = [
        *package.FILES,
        "src/dsfforza/__init__.py",
        "src/dsfforza/build/install.py",
        "tools/batches/all.txt",
        "tools/release/launcher.ps1",
        "tools/wsb/run-series.ps1",
        "research/batch.md",
        "tests/test_install.py",
        "tools/game/lua/dsfforza/garagetest.lua",
    ]
    files = package.select(tracked)
    assert "tools/install.ps1" in files and "src/dsfforza/build/install.py" in files and "install.cmd" in files
    assert not any(f.startswith(("tests/", "tools/wsb/")) for f in files)
    assert "research/batch.md" not in files and "research/forza-roster.tsv" in files
    assert "tools/game/lua/dsfforza/garagetest.lua" not in files
    for bad in ("src/dsfforza/x.iso", "tools/batches/dngvehicles.sp"):
        try:
            package.select([*tracked, bad])
            raise AssertionError(f"{bad} packaged")
        except SystemExit as error:
            assert bad in str(error)
    try:
        package.select([f for f in tracked if f != "uv.lock"])
        raise AssertionError("missing file not noticed")
    except SystemExit as error:
        assert "uv.lock" in str(error)
