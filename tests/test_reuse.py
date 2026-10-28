"""port_batch build --reuse: fingerprints (src/dsfforza/build/fingerprint.py) and the
reuse decision, without converting a car (a fraction of a second).

uv run python tests/test_reuse.py     # or every test: uv run python -m tests reuse
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from dsfforza.build import fingerprint, port_batch

SOURCE = '''"""Module docstring."""
import os
from pathlib import Path

LIMIT = 4
SCALE: float = 0.5


def area(w, h):
    """Docstring."""
    return w * h * SCALE


class Box:
    size = 3

    def volume(self):
        return self.size**3


if os.name == "nt":
    def windows_only():
        return 1
'''


def units(source: str) -> dict:
    return fingerprint.module_units(source)


def test_units_ignore_comments_docstrings_and_formatting():
    base = units(SOURCE)
    edited = SOURCE.replace('"""Docstring."""', '"""Another docstring."""').replace(
        "return w * h * SCALE", "return (w * h  *  SCALE)  # a comment"
    )
    assert units(edited) == base
    assert {"area", "Box", "Box.volume", "=LIMIT", "=SCALE", "=os", "=Path", "<module>"} <= set(base)


def test_a_change_moves_only_its_unit():
    base = units(SOURCE)
    for old, new, unit in (
        ("return w * h * SCALE", "return w * h * SCALE + 1", "area"),
        ("LIMIT = 4", "LIMIT = 5", "=LIMIT"),
        ("size = 3", "size = 4", "Box"),
        ("return self.size**3", "return self.size**2", "Box.volume"),
        ("return 1", "return 2", "<module>"),  # a def inside a top-level if
    ):
        changed = {k for k, v in units(SOURCE.replace(old, new)).items() if base.get(k) != v}
        assert changed == {unit}, (old, changed)
    added = units(SOURCE.replace("import os\n", "import os\nimport json\n"))
    assert {k for k, v in added.items() if base.get(k) != v} == {"=json"}


def test_unit_of_code_objects():
    assert fingerprint.unit_of("area") == "area"
    assert fingerprint.unit_of("Box.volume") == "Box.volume"
    assert fingerprint.unit_of("area.<locals>.inner") == "area"
    assert fingerprint.unit_of("Box.volume.<locals>.<lambda>") == "Box.volume"
    assert fingerprint.unit_of("<lambda>") == fingerprint.MODULE_UNIT
    assert fingerprint.unit_of("<genexpr>") == fingerprint.MODULE_UNIT
    assert fingerprint.module_name(str(fingerprint.PACKAGE / "formats" / "xds.py")) == "dsfforza.formats.xds"
    assert fingerprint.module_name(__file__) is None


def _job(folder: Path) -> dict:
    return {
        "media": "TEST_Car_10",
        "game": "FH1",
        "year": 2010,
        "games": "FH1",
        "source": {"iso": str(folder / "disc.iso"), "path": "media/cars/x.zip"},
        "spool_id": 7,
        "dsf": str(folder / "vanilla.sp"),
        "game_dir": str(folder),
        "cache": str(folder),
        "isos": [],
        "port_options": ["--damage"],
        "sound_dir": str(folder / "out" / "sound"),
        "chunk": str(folder / "out" / "work" / "TEST_Car_10.chnk"),
        "report": str(folder / "out" / "work" / "TEST_Car_10.json"),
    }


def _record(folder: Path, job: dict) -> dict:
    """A fingerprint of reading an input file and running a little package code."""
    from dsfforza.formats import xds

    data = folder / "car" / "input.bin"
    recorder = fingerprint.Recorder([folder / "out"]).start()
    try:
        xds.parse_header(data.read_bytes())
        (folder / "out").mkdir(exist_ok=True)
        (folder / "out" / "written.txt").write_text("output")
        sorted(p.name for p in (folder / "car").iterdir())
    finally:
        fp = recorder.finish(job)
    return fp


def test_fingerprint_records_inputs_and_code():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "car").mkdir()
        (folder / "car" / "input.bin").write_bytes(bytes(64))
        job = _job(folder)
        fp = _record(folder, job)
        files = set(fp["files"])
        assert fingerprint.norm(folder / "car" / "input.bin") in files
        assert not any("written.txt" in p for p in files)  # an output
        assert fingerprint.norm(folder / "car") in fp["dirs"]
        assert "parse_header" in fp["code"]["dsfforza.formats.xds"]
        assert "dsfforza.build.fingerprint" not in fp["code"]
        assert fp["complete"]
        checker = fingerprint.Checker()
        assert checker.check(fp, job) is None
        assert checker.check(fp, {**job, "spool_id": 8}) == "job: spool_id"
        assert checker.check(fp, {**job, "chunk": "elsewhere.chnk"}) is None  # where it goes doesn't count
        (folder / "car" / "input.bin").write_bytes(bytes(65))
        assert fingerprint.Checker().check(fp, job).startswith("input ")
        changed = json.loads(json.dumps(fp))
        changed["code"]["dsfforza.formats.xds"]["parse_header"] = "0" * 24
        assert fingerprint.Checker().check(changed, job, code=False).startswith("input ")
        (folder / "car" / "input.bin").write_bytes(bytes(64))
        assert fingerprint.Checker().check(changed, job) == "code dsfforza.formats.xds.parse_header"
        assert fingerprint.Checker().check(changed, job, code=False) is None


def test_a_moved_vanilla_archive_with_the_same_digest_is_the_same_input():
    """dsff install builds from the game's dngvehicles.sp the first time and from the install's
    backup of it after that (issue #95: every car was converted again on each later run)."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        game = folder / "game"
        vanilla = game / "Install" / "dngvehicles.sp"
        vanilla.parent.mkdir(parents=True)
        vanilla.write_bytes(b"vanilla archive")
        job = {**_job(folder), "dsf": str(vanilla), "game_dir": str(game)}
        recorder = fingerprint.Recorder([folder / "out"]).start()
        try:
            vanilla.read_bytes()
        finally:
            fp = recorder.finish(job)
        assert fingerprint.norm(vanilla) in fp["files"]
        # The install backs the archive up (time stamp kept, as Copy-Item does) and replaces it.
        backup = game / "dsfforza-backup" / "Install" / "dngvehicles.sp"
        backup.parent.mkdir(parents=True)
        shutil.copy2(vanilla, backup)
        vanilla.write_bytes(b"the installed build")
        after = {**job, "dsf": str(backup)}
        assert fingerprint.Checker().check(fp, after) is None
        assert fingerprint.Checker().moved(fp, after) == {fingerprint.norm(vanilla): fingerprint.norm(backup)}
        assert fingerprint.Checker().check(fp, job).startswith("input ")  # the game's file is the build now
        backup.write_bytes(b"another archive")
        assert fingerprint.Checker().check(fp, after) == "job: dsf"


def test_reuse_copies_unchanged_cars_and_honours_the_predicate():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "car").mkdir()
        (folder / "car" / "input.bin").write_bytes(bytes(64))
        old = folder / "old"
        (old / "work").mkdir(parents=True)
        (old / "sound" / "TEST_Car_10").mkdir(parents=True)
        (old / "sound" / "TEST_Car_10" / "sweep_new.wav").write_bytes(b"wav")
        job = _job(folder)
        job["sound_dir"] = str(folder / "new" / "sound")
        fp = _record(folder, job)
        media = job["media"]
        (old / "work" / f"{media}.fp.json").write_text(json.dumps(fp))
        (old / "work" / f"{media}.json").write_text(json.dumps({"media": media, "ok": True, "host": 198, "entry": {}}))
        (old / "work" / f"{media}.chnk").write_bytes(b"CHNK...")
        (old / "work" / f"{media}.job.json").write_text(json.dumps(job))
        work = folder / "new" / "work"
        work.mkdir(parents=True)

        def args(**kw):
            return argparse.Namespace(reuse=old, rebuild_where=None, trust_code=False, verify_reuse=0, **kw)

        reuse = port_batch.Reuse(args(), work)
        report = reuse.take(job)
        assert report is not None and report["reused_from"] == str(old)
        assert (work / f"{media}.chnk").read_bytes() == b"CHNK..."
        assert (folder / "new" / "sound" / media / "sweep_new.wav").read_bytes() == b"wav"
        again = port_batch.Reuse(argparse.Namespace(**{**vars(args()), "rebuild_where": "host == 198"}), work)
        assert again.take(job) is None and again.rebuilt[media] == "--rebuild-where"
        other = port_batch.Reuse(args(), work)
        assert other.take({**job, "port_options": ["--damage", "--no-cockpit"]}) is None
        assert other.rebuilt[media] == "job: options"


def test_reuse_takes_each_car_from_the_first_build_that_has_it():
    """--reuse given twice (dsff install: the last build, then an interrupted one): a car
    missing from the first is taken from the second; a car in neither is built."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "car").mkdir()
        (folder / "car" / "input.bin").write_bytes(bytes(64))
        first, second = folder / "first", folder / "second"
        (first / "work").mkdir(parents=True)
        (second / "work").mkdir(parents=True)
        job = _job(folder)
        job["sound_dir"] = str(folder / "new" / "sound")
        fp = _record(folder, job)
        media = job["media"]
        (second / "work" / f"{media}.fp.json").write_text(json.dumps(fp))
        (second / "work" / f"{media}.json").write_text(json.dumps({"media": media, "ok": True, "host": 198}))
        (second / "work" / f"{media}.chnk").write_bytes(b"CHNK2")
        work = folder / "new" / "work"
        work.mkdir(parents=True)
        args = argparse.Namespace(reuse=[first, second], rebuild_where=None, trust_code=False, verify_reuse=0)
        reuse = port_batch.Reuse(args, work)
        report = reuse.take(job)
        assert report is not None and report["reused_from"] == str(second)
        assert (work / f"{media}.chnk").read_bytes() == b"CHNK2"
        assert reuse.record()["sources"] == [str(first), str(second)]
        other = port_batch.Reuse(args, work)
        assert other.take({**job, "media": "TEST_Other_11"}) is None
        assert other.rebuilt["TEST_Other_11"] == "not built there"


def _old_build(folder: Path, old: Path, cars: list[str], sound_dir: Path) -> list[dict]:
    """An earlier build in `old` with each of `cars` converted (fingerprints that hold)."""
    (old / "work").mkdir(parents=True)
    jobs = []
    for n, media in enumerate(cars):
        job = {**_job(folder), "media": media, "spool_id": 7 + n, "sound_dir": str(sound_dir)}
        fp = _record(folder, job)
        (old / "work" / f"{media}.fp.json").write_text(json.dumps(fp))
        (old / "work" / f"{media}.json").write_text(json.dumps({"media": media, "ok": True, "host": 198}))
        (old / "work" / f"{media}.chnk").write_bytes(b"CHNK" + media.encode())
        (old / "work" / f"{media}.job.json").write_text(json.dumps(job))
        jobs.append(job)
    return jobs


def test_verify_mismatch_stops_port_batch_and_rebuilds_for_dsff_install():
    """Issue #133: a reused car that converts differently stops a developer's build; in
    dsff install (--verify-mismatch rebuild) its fresh copy is kept and the other cars
    reused from the same build are converted again."""
    cars = ["TEST_A_10", "TEST_B_10", "TEST_C_10"]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "car").mkdir()
        (folder / "car" / "input.bin").write_bytes(bytes(64))
        old = folder / "old"
        jobs = _old_build(folder, old, cars, folder / "new" / "sound")
        work = folder / "new" / "work"
        work.mkdir(parents=True)

        def fake_run_workers(checks, where, parallel, log=print, progress=None):
            out = {}
            for check in checks:  # a fresh conversion that comes out different
                Path(check["chunk"]).write_bytes(b"FRESH " + check["media"].encode())
                report = {"media": check["media"], "ok": True, "host": 198, "fresh": True}
                Path(check["report"]).write_text(json.dumps(report))
                (where / "sound" / check["media"]).mkdir(parents=True, exist_ok=True)
                (where / "sound" / check["media"] / "sweep_new.wav").write_bytes(b"new")
                out[check["media"]] = report
            return out

        args = argparse.Namespace(reuse=old, rebuild_where=None, trust_code=False, verify_reuse=1, verify_seed=5)
        real = port_batch.run_workers
        port_batch.run_workers = fake_run_workers
        try:
            reuse = port_batch.Reuse(args, work)
            for job in jobs:
                assert reuse.take(job) is not None
            try:
                reuse.verify(jobs, 1)
                raise AssertionError("port_batch build must stop")
            except SystemExit as error:
                assert "came out different" in str(error)
            reuse = port_batch.Reuse(args, work)
            for job in jobs:
                reuse.take(job)
            redo = reuse.verify(jobs, 1, on_mismatch="rebuild")
        finally:
            port_batch.run_workers = real
        bad = reuse.mismatch["different"]
        assert len(bad) == 1 and sorted(redo + bad) == cars
        assert (work / f"{bad[0]}.chnk").read_bytes() == b"FRESH " + bad[0].encode()
        assert json.loads((work / f"{bad[0]}.json").read_text())["fresh"]
        assert (folder / "new" / "sound" / bad[0] / "sweep_new.wav").read_bytes() == b"new"
        assert not json.loads((work / f"{bad[0]}.job.json").read_text()).get("verify")
        assert (old / "work" / f"{bad[0]}.chnk").read_bytes() == b"CHNK" + bad[0].encode()  # the source is untouched
        record = reuse.record()
        assert record["reused"] == [] and record["verify_mismatch"]["converted_again"] == redo


def test_workers_run_with_one_blas_thread():
    """Issues #84, #133: numpy's threaded BLAS made a car convert a few hundred bytes
    differently each time (BMW_Z4_11: 3 runs, 3 different chunks; 3 runs with one thread:
    identical). Workers get fingerprint.WORKER_ENV."""
    seen = []

    class FakeProc:
        def poll(self):
            return 0

    def fake_popen(argv, **kw):
        seen.append(kw["env"])
        job = json.loads(Path(argv[-1]).read_text())
        Path(job["report"]).write_text(json.dumps({"media": job["media"], "ok": True}))
        return FakeProc()

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        job = {**_job(folder), "report": str(folder / "TEST_Car_10.json")}
        real = port_batch.subprocess.Popen
        port_batch.subprocess.Popen = fake_popen
        try:
            results = port_batch.run_workers([job], folder, 1, log=lambda t: None)
        finally:
            port_batch.subprocess.Popen = real
        assert results["TEST_Car_10"]["ok"]
        assert seen and all(seen[0].get(k) == v for k, v in fingerprint.WORKER_ENV.items())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
