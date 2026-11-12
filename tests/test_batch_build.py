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
