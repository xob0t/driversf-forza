"""Checks for port_batch's worker handling and the cache writes (no game files or
disc images needed; a few seconds).

    uv run python tests/test_pipeline.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from dsfforza.build import (
    port_batch,
    port_car,
)


def test_worker_report_survives_a_cut_short_report():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "CAR.log").write_text("step 1\ncockpit: eye (0, 1, 2)\n", encoding="utf-8")
        (work / "CAR.json").write_text('{"media": "CAR", "ok": tr', encoding="utf-8")  # killed mid-write
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
    tail = {
        "ok": False,
        "cause": "timeout",
        "error": "exit code None; log tail: cockpit: ... src/dsfforza/convert/damage.py",
    }
    assert port_batch.fallback_for(tail, ["--damage", "panels"]) is None
    assert port_batch.fallback_for({**tail, "cause": "worker crash"}, []) is None
    failed = {
        "ok": False,
        "cause": "damage.py: ValueError",
        "traceback": 'File "src/dsfforza/convert/damage.py", line 1\nValueError: x',
        "error": "ValueError: x",
    }
    assert port_batch.fallback_for(failed, ["--mdxn-limit", "1", "--damage", "panels"]) == (
        ["--mdxn-limit", "1"],
        "without --damage (src/dsfforza/convert/damage.py failed)",
    )
    # A frame in a package's submodule counts as the package (convert/damage/plan.py).
    text = 'File "C:\\r\\src\\dsfforza\\convert\\damage\\plan.py", line 9, in _votes\nValueError: x'
    assert port_batch.cause_of(text) == "damage.py: ValueError"
    assert port_batch.fallback_for({**failed, "traceback": text, "error": ""}, ["--damage"])[0] == []
    assert port_batch.cause_of('File "/r/src/dsfforza/convert/lights.py", line 3\nKeyError: k') == "lights.py: KeyError"
    assert port_batch.cause_of('File "/r/src/dsfforza/cli.py", line 3\nKeyError: k') == "cli.py: KeyError"


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
        assert not (work / "DEAD.first-try.log").exists()  # no option fallback was tried


def test_longest_cars_start_first():
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "car-seconds.json"
        jobs = [{"media": m, "game": "FH1"} for m in ("A", "B", "C", "D")]
        assert port_batch.longest_first(jobs, cache) == jobs  # nothing known: list order
        port_batch.remember_seconds(
            [
                {"media": "B", "game": "FH1", "ok": True, "seconds": 300.0},
                {"media": "C", "game": "FH1", "ok": True, "seconds": 50.0},
                {"media": "D", "game": "FH1", "ok": True, "seconds": 100.0},
                {"media": "A", "game": "FH1", "ok": False, "seconds": 999.0},  # failed: not kept
            ],
            cache,
        )
        assert json.loads(cache.read_text()) == {"b@fh1": 300.0, "c@fh1": 50.0, "d@fh1": 100.0}
        # A, never built, counts as the median (100): after B, before C, beside D in list order
        assert [j["media"] for j in port_batch.longest_first(jobs, cache)] == ["B", "A", "D", "C"]
        port_batch.remember_seconds([{"media": "C", "game": "FH1", "ok": True, "seconds": 400.0}], cache)
        # the median is now 300: A ties with B and keeps its place in the list
        assert [j["media"] for j in port_batch.longest_first(jobs, cache)] == ["C", "A", "B", "D"]


def test_attempts_are_remembered_only_for_full_entry_retries():
    def report(media, problems, ok=True):
        attempts = [{"problems": p} for p in problems]
        return {"media": media, "game": "FH1", "ok": ok, "entry": {"attempts": attempts}}

    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "car-attempts.json"
        port_batch.remember_attempts(
            [
                report("A", [{"0": ["over"]}, {"0": ["over"]}, {}]),  # the full entry retried: kept
                report("B", [{}]),  # one attempt: nothing to keep
                report("C", [{"2": ["over"]}, {}]),  # the exterior was over too: not kept
                report("D", [{"0": ["x"]}, {}], ok=False),  # failed: not kept
            ],
            cache,
        )
        assert json.loads(cache.read_text()) == {"a@fh1": 3}
        assert port_batch.remembered_attempts({"media": "A", "game": "FH1"}, cache) == 3
        assert port_batch.remembered_attempts({"media": "B", "game": "FH1"}, cache) is None
        port_batch.remember_attempts([report("A", [{}])], cache)  # fits at once now: forgotten
        assert json.loads(cache.read_text()) == {}


def test_merge_leaves_out_cars_with_uid_clashes():
    """A UID clash fails the clashing cars, not the batch: the rest are merged again."""
    calls = []

    def register(dsf, out, results, work, log, root_limit):
        media = [r["media"] for r in results]
        calls.append(media)
        if "B" in media:
            return [{"media": m} for m in media], [], {"B": ["aa", "bb"]}
        return [{"media": m} for m in media], [], {}

    real = port_batch.register_cars
    port_batch.register_cars = register
    try:
        results = [{"media": m, "spool_id": i} for i, m in enumerate("ABC", 1)]
        cars, failed = port_batch.merge(Path("vanilla.sp"), Path("out.sp"), results, Path(), log=lambda t: None)
    finally:
        port_batch.register_cars = real
    assert calls == [["A", "B", "C"], ["A", "C"]]
    assert [c["media"] for c in cars] == ["A", "C"]
    assert [(f["media"], f["cause"]) for f in failed] == [("B", "UID clash")]


def test_builders_keep_their_own_material_layouts():
    """forza2dsf.MATERIAL_LAYOUT is read only; each conversion's host materials live
    in its builders' layout (a second car in the same process saw the first's)."""
    from dsfforza.convert import forza2dsf

    before = dict(forza2dsf.MATERIAL_LAYOUT)
    a, b = forza2dsf.Builder(b"\0\0\0\1"), forza2dsf.Builder(b"\0\0\0\2")
    a.layout[b"hostmat1"] = (32, 5)
    assert b"hostmat1" not in b.layout and before == forza2dsf.MATERIAL_LAYOUT
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
