"""port_batch build's steps that need no conversion: the jobs, --resume, the earlier cars
taken, the convert line and the results split (no game files or disc images needed)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from dsfforza.build import port_batch


def _picks() -> list[port_batch.Pick]:
    picks = [
        port_batch.Pick("AAA_ONE_01", "FM4", 2001, "FM4", {"game": "FM4"}),
        port_batch.Pick("BBB_TWO_02", "FH1", 2002, "FH1,FM4", {"game": "FH1"}),
    ]
    for n, p in enumerate(picks):
        p.spool_id = 304 + n
    return picks


def test_build_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        dsf = Path(tmp) / "Game" / "Install" / "dngvehicles.sp"
        jobs = port_batch.build_jobs(_picks(), dsf, [Path("a.iso")], Path("cache"), ["--damage"], out)
        assert [j["media"] for j in jobs] == ["AAA_ONE_01", "BBB_TWO_02"]
        assert jobs[1] == {
            "media": "BBB_TWO_02",
            "game": "FH1",
            "year": 2002,
            "games": "FH1,FM4",
            "source": {"game": "FH1"},
            "spool_id": 305,
            "dsf": str(dsf),
            "game_dir": str(port_batch.game_from_dsf(dsf)),
            "cache": "cache",
            "isos": ["a.iso"],
            "port_options": ["--damage"],
            "sound_dir": str(out / "sound"),
            "chunk": str(out / "work" / "BBB_TWO_02.chnk"),
            "report": str(out / "work" / "BBB_TWO_02.json"),
        }


def _earlier(work: Path, job: dict, report: dict, **changed) -> None:
    """An earlier run's job, report and chunk for the job's car in work/."""
    (work / f"{job['media']}.job.json").write_text(json.dumps({**job, **changed}), encoding="utf-8")
    Path(job["report"]).write_text(json.dumps(report), encoding="utf-8")
    Path(job["chunk"]).write_text("chunk", encoding="utf-8")


def test_resume_takes_a_car_built_the_same_way():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "work").mkdir()
        work = out / "work"
        first, second = port_batch.build_jobs(_picks(), Path("v.sp"), [], Path("c"), ["--damage"], out)
        ok = {"media": "AAA_ONE_01", "ok": True}
        _earlier(work, first, ok)
        assert port_batch.resumed_report(first, work) == ok
        assert port_batch.resumed_report(second, work) is None  # never built here
        # The options it was asked for count, not those of a fallback.
        _earlier(work, first, ok, port_options=[], requested_options=["--damage"])
        assert port_batch.resumed_report(first, work) == ok
        for changed in ({"spool_id": 1}, {"game": "FH1"}, {"dsf": "other.sp"}, {"port_options": []}):
            _earlier(work, first, ok, **changed)
            assert port_batch.resumed_report(first, work) is None, changed
        _earlier(work, first, {**ok, "ok": False})
        assert port_batch.resumed_report(first, work) is None
        (work / "AAA_ONE_01.job.json").write_text('{"spool', encoding="utf-8")  # cut short
        assert port_batch.resumed_report(first, work) is None


def test_take_earlier_resumes_and_clears_the_rest():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        work = out / "work"
        work.mkdir()
        first, second = port_batch.build_jobs(_picks(), Path("v.sp"), [], Path("c"), ["--damage"], out)
        ok = {"media": "AAA_ONE_01", "ok": True}
        _earlier(work, first, ok)
        _earlier(work, second, {"media": "BBB_TWO_02", "ok": True}, spool_id=1)  # another ID then
        (work / "BBB_TWO_02.json.tmp").write_text("half", encoding="utf-8")
        assert port_batch.take_earlier([first, second], work, resume=True, reuse=None) == {"AAA_ONE_01": ok}
        assert sorted(p.name for p in work.iterdir()) == ["AAA_ONE_01.chnk", "AAA_ONE_01.job.json", "AAA_ONE_01.json"]
        # Without --resume nothing of this folder's is taken.
        assert port_batch.take_earlier([first, second], work, resume=False, reuse=None) == {}
        assert list(work.iterdir()) == []


def test_convert_message():
    line = port_batch.convert_message(3, 4, {}, None)
    assert line.startswith("building 3 cars, 4 at a time (") and line.endswith("priority)")
    assert port_batch.convert_message(3, 4, {"A": {}}, None).endswith("; 1 reused from the last run")

    reuse = SimpleNamespace(taken={"A": {}}, sources=[Path("out/build")], store=object())
    assert port_batch.convert_message(0, 4, {"A": {}}, reuse).endswith(
        f"; 1 reused from {Path('out/build')}, {port_batch.STORE}"
    )


def test_split_results_keeps_plan_order():
    picks = _picks()
    skipped = [port_batch.Pick("CCC_SKIP_03", "FM4", None, "", {}, notes=["not a car", "x"])]
    results = {"BBB_TWO_02": {"media": "BBB_TWO_02", "ok": True}, "AAA_ONE_01": {"media": "AAA_ONE_01", "ok": False}}
    ok, failed = port_batch.split_results(picks, skipped, results)
    assert ok == [results["BBB_TWO_02"]]
    assert failed == [
        results["AAA_ONE_01"],
        {"media": "CCC_SKIP_03", "ok": False, "cause": "not selected", "error": "not a car; x"},
    ]


def test_a_failed_gui_step_moves_the_old_gui_aside():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        timings: dict = {}
        port_batch.move_stale_gui(out, timings)  # no gui/: nothing to move
        assert timings == {} and list(out.iterdir()) == []
        (out / "gui").mkdir()
        (out / "gui" / "hud.lua").write_text("other cars", encoding="utf-8")
        port_batch.move_stale_gui(out, timings)
        [stale] = list(out.iterdir())
        assert stale.name.startswith("gui-stale-") and timings == {"gui_stale": str(stale)}
        assert (stale / "hud.lua").read_text(encoding="utf-8") == "other cars"


def test_build_resume_keeps_the_before_flag():
    """cmd_build --resume with an earlier car in work/: the earlier car is taken, the other
    one converted. On master before #599, the earlier job's JSON overwrote the --before flag
    (a shadowed local), so a resume that found a car converted nothing."""
    from unittest import mock

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        work = out / "work"
        work.mkdir()
        picks = _picks()
        first, _ = port_batch.build_jobs(picks, Path("v.sp"), [], Path("c"), ["--damage"], out)
        ok = {"media": "AAA_ONE_01", "ok": True}
        _earlier(work, first, ok)
        converted, finished = [], []

        def run_workers(todo, *_args, **_kw):
            converted.extend(j["media"] for j in todo)
            return {j["media"]: {"media": j["media"], "ok": True} for j in todo}

        def finish(*args):
            ok, failed = args[4], args[5]
            finished.append(([r["media"] for r in ok], failed))

        args = SimpleNamespace(
            out=out, port="--damage", progress=None, cache=Path("c"), resume=True, jobs=1, before=False
        )
        with mock.patch.multiple(
            port_batch,
            common=lambda *_a: (Path("v.sp"), [], {}, {}),
            port_car_args=lambda argv: argv,
            plan_build=lambda *_a: (picks, [], 0),
            warm_caches=lambda *_a: None,
            open_reuse=lambda *_a: (None, None, False),
            remembered_attempts=lambda *_a: None,
            remember_seconds=lambda *_a: None,
            remember_attempts=lambda *_a: None,
            run_workers=run_workers,
            open_pack_reuse=lambda *_a: None,
            finish=finish,
        ):
            port_batch.cmd_build(args)
        assert converted == ["BBB_TWO_02"]
        assert finished == [(["AAA_ONE_01", "BBB_TWO_02"], [])]
