"""Checks for port_batch's worker handling and the cache writes (no game files or
disc images needed; a few seconds).

    python tools/tests/test_pipeline.py     # or every test: python -m tools.tests
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import port_batch  # noqa: E402
import port_car  # noqa: E402


def test_worker_report_survives_a_cut_short_report():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "CAR.log").write_text("step 1\ncockpit: eye (0, 1, 2)\n", encoding="utf-8")
        (work / "CAR.json").write_text('{"media": "CAR", "ok": tr', encoding="utf-8")   # killed mid-write
        report = port_batch.worker_report(work, "CAR", 1)
        assert report["ok"] is False and report["cause"] == "worker crash"
        assert "cockpit: eye" in report["error"]
        assert not (work / "CAR.json").exists()
        report = port_batch.worker_report(work, "CAR", None)
        assert report["cause"] == "timeout"
        (work / "CAR.json").write_text(json.dumps({"media": "CAR", "ok": True}), encoding="utf-8")
        assert port_batch.worker_report(work, "CAR", 0) == {"media": "CAR", "ok": True}


def test_no_option_fallback_for_a_dead_worker():
    # The log tail of a timed-out worker names every step it got through.
    tail = {"ok": False, "cause": "timeout", "error": "exit code None; log tail: cockpit: ... tools/damage.py"}
    assert port_batch.fallback_for(tail, ["--damage", "panels"]) is None
    assert port_batch.fallback_for({**tail, "cause": "worker crash"}, []) is None
    failed = {"ok": False, "cause": "damage.py: ValueError",
              "traceback": 'File "tools/damage.py", line 1\nValueError: x', "error": "ValueError: x"}
    assert port_batch.fallback_for(failed, ["--mdxn-limit", "1", "--damage", "panels"]) == (
        ["--mdxn-limit", "1"], "without --damage (tools/damage.py failed)")


def test_a_worker_that_dies_is_a_failed_car():
    """A worker that dies before it writes a report (here: a job without the keys it
    needs) is reported as a failed car; the batch carries on."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        jobs = [{"media": "DEAD", "spool_id": 7, "port_options": []}]
        results = port_batch.run_workers(jobs, work, 1, log=lambda text: None)
        assert results["DEAD"]["ok"] is False
        assert results["DEAD"]["cause"] == "worker crash"
        assert results["DEAD"]["spool_id"] == 7
        assert "KeyError" in results["DEAD"]["error"]
        assert not (work / "DEAD.first-try.log").exists()   # no option fallback was tried


def test_builders_keep_their_own_material_layouts():
    """forza2dsf.MATERIAL_LAYOUT is read only; each conversion's host materials live
    in its builders' layout (a second car in the same process saw the first's)."""
    import forza2dsf
    before = dict(forza2dsf.MATERIAL_LAYOUT)
    a, b = forza2dsf.Builder(b"\0\0\0\1"), forza2dsf.Builder(b"\0\0\0\2")
    a.layout[b"hostmat1"] = (32, 5)
    assert b"hostmat1" not in b.layout and forza2dsf.MATERIAL_LAYOUT == before
    a.add(b"hostmat1", [b"\0" * 32] * 3, [(0, 1, 2)])
    assert a.subs[0].stride == 32


def test_write_atomic_leaves_no_temp():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a.bin"
        path.write_bytes(b"old")
        port_car.write_atomic(path, b"new")
        assert path.read_bytes() == b"new"
        assert [p.name for p in Path(tmp).iterdir()] == ["a.bin"]


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"ok {name}")
