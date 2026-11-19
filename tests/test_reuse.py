"""port_batch build --reuse by the builder version (src/dsfforza/build/builder_version.py):
a car is reused when the builder version, its media and options, its build ID and its own
source (disc or DLC pack by identity) match. Without converting a car (a fraction of a
second, a few git calls).

uv run python tests/test_reuse.py     # or every test: uv run python -m tests reuse
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from dsfforza.build import builder_version, port_batch

VERSION = "git 0123456789abcdef"


def _fh1(folder: Path, image: str = "fh1.iso") -> dict:
    """A car's source on FH1's disc (forza_roster.Source as a job holds it)."""
    return {
        "game": "FH1",
        "where": "FH1",
        "path": "Media/Cars/TEST_Car_10.zip",
        "size": 1000,
        "files": 12,
        "kind": "car",
        "iso": str(folder / image),
        "package": "",
    }


def _dlc(folder: Path, pack: str = "Porsche Expansion Pack", file: str = "pack1") -> dict:
    """A car's source in an FM4 DLC pack file."""
    return {
        "game": "FM4",
        "where": f"FM4 DLC / {pack}",
        "path": "Media/DLCZips/1_pri_65.zip::media/cars/TEST_Dlc_11/",
        "size": 2000,
        "files": 30,
        "kind": "car",
        "iso": "",
        "package": str(folder / "assets" / file),
    }


def _job(folder: Path, media: str = "TEST_Car_10", spool_id: int = 7, source: dict | None = None, **kw) -> dict:
    return {
        "builder": VERSION,
        "media": media,
        "game": (source or {}).get("game", "FH1"),
        "year": 2010,
        "games": "FH1",
        "source": source or _fh1(folder),
        "spool_id": spool_id,
        "dsf": str(folder / "vanilla.sp"),
        "game_dir": str(folder),
        "cache": str(folder / "cache"),
        "isos": [str(folder / "fh1.iso")],
        "port_options": ["--damage"],
        "sound_dir": str(folder / "out" / "sound"),
        "chunk": str(folder / "out" / "work" / f"{media}.chnk"),
        "report": str(folder / "out" / "work" / f"{media}.json"),
        **kw,
    }


# --- the rule --------------------------------------------------------------------------------------


def test_same_version_is_reused_another_converts():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        job = _job(folder)
        assert builder_version.why_not(job, dict(job)) is None
        assert builder_version.why_not(job, {**job, "builder": "git fedcba9876543210"}) == "builder version"
        assert builder_version.why_not(job, {**job, "builder": "release v0.3.0"}) == "builder version"
        assert builder_version.why_not(job, {**job, "port_options": []}) == "options"
        assert builder_version.why_not(job, {**job, "spool_id": 8}) == "build ID"
        assert builder_version.why_not(job, {**job, "media": "TEST_Other_10"}) == "media"
        assert builder_version.why_not(None, job) == "no job record"
        # A build from before the builder version (no "builder" in its job) converts.
        assert builder_version.why_not({k: v for k, v in job.items() if k != "builder"}, job) == "builder version"


def test_a_dirty_tree_reuses_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        job = _job(Path(tmp), builder=VERSION + builder_version.DIRTY)
        assert not builder_version.reusable(job["builder"])
        assert builder_version.why_not(job, dict(job)).endswith("(no reuse)")  # not even its own cars
        assert builder_version.why_not(_job(Path(tmp), builder=None), _job(Path(tmp), builder=None))


def test_a_fallback_build_is_reused_by_the_options_asked_for():
    """A car built again without --damage after its first try failed: its job keeps the
    options it was asked for (requested_options), which a later build asks for again."""
    with tempfile.TemporaryDirectory() as tmp:
        job = _job(Path(tmp))
        retried = {**job, "requested_options": ["--damage"], "port_options": [], "fallback": "without --damage"}
        assert builder_version.why_not(retried, job) is None


def test_a_disc_renamed_or_moved_or_another_added_is_reused():
    """The disc counts by identity (its "where", from default.xex), not its image's path: the
    FM4 discs added, or FH1's image renamed or moved, converts no FH1 car."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        job = _job(folder)
        renamed = _job(folder, source=_fh1(folder / "elsewhere", "Forza Horizon (renamed).iso"))
        renamed["isos"] = [str(folder / "elsewhere" / "Forza Horizon (renamed).iso")]
        assert builder_version.why_not(job, renamed) is None
        added = {**job, "isos": [str(folder / "fm4a.iso"), str(folder / "fh1.iso"), str(folder / "fm4b.iso")]}
        assert builder_version.why_not(job, added) is None
        # Another disc: an FM4 disc-2 pack car of the same name is another source.
        other = {**job["source"], "game": "FM4", "where": "FM4 disc 2 / Car Pack", "package": "content/0/x"}
        assert builder_version.why_not(job, {**job, "source": other}) == "source"


def test_a_dlc_pack_counts_by_name_not_by_file():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        job = _job(folder, "TEST_Dlc_11", source=_dlc(folder))
        moved = _job(folder, "TEST_Dlc_11", source=_dlc(folder / "moved", file="Porsche (renamed).bin"))
        assert builder_version.why_not(job, moved) is None
        other = _job(folder, "TEST_Dlc_11", source=_dlc(folder, pack="Another Pack"))
        assert builder_version.why_not(job, other) == "source"
        # A pack on a disc keeps its path in the image (it is part of where the car is).
        on_disc = {**_fh1(folder), "game": "FM4", "where": "FM4 disc 2 / Pack", "package": "content/0/a"}
        assert builder_version.source_identity(on_disc)["package"] == "content/0/a"
        assert "iso" not in builder_version.source_identity(on_disc)


# --- the builder version --------------------------------------------------------------------------


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True)


def test_the_version_is_the_commit_or_dirty_or_the_release():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        (repo / "src" / "dsfforza").mkdir(parents=True)
        (repo / "src" / "dsfforza" / "a.py").write_text("A = 1\n")
        (repo / "README.md").write_text("readme\n")
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert builder_version.read(repo) == f"git {head}"
        (repo / "README.md").write_text("docs only\n")  # not what the converter reads
        assert builder_version.read(repo) == f"git {head}"
        (repo / "src" / "dsfforza" / "a.py").write_text("A = 2\n")
        assert builder_version.read(repo) == f"git {head}{builder_version.DIRTY}"
        _git(repo, "checkout", "-q", "--", "src")
        (repo / "src" / "dsfforza" / "new.py").write_text("B = 1\n")  # untracked: dirty too
        assert builder_version.read(repo) == f"git {head}{builder_version.DIRTY}"
        (repo / "src" / "dsfforza" / "new.py").unlink()
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "two")
        assert builder_version.read(repo) not in (f"git {head}", None)  # a new commit: another version
        # A release: the VERSION file package.build_zip writes, no git.
        release = Path(tmp) / "release"
        release.mkdir()
        (release / "VERSION").write_text("v0.3.0 0123abc\n")
        assert builder_version.read(release) == "release v0.3.0"
        assert builder_version.read(Path(tmp) / "nothing") is None


# --- port_batch --reuse -----------------------------------------------------------------------------


def _built(build: Path, job: dict, chunk: bytes | None = None) -> None:
    """A car `build` converted from `job`: its work files and sound folder."""
    media = job["media"]
    work = build / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / f"{media}.json").write_text(json.dumps({"media": media, "ok": True, "host": 198, "entry": {}}))
    (work / f"{media}.chnk").write_bytes(chunk or b"CHNK" + media.encode())
    (work / f"{media}.job.json").write_text(json.dumps(job))
    (work / f"{media}.log").write_text("log")
    (build / "sound" / media).mkdir(parents=True, exist_ok=True)
    (build / "sound" / media / "sweep_new.wav").write_bytes(b"wav")


def _args(reuse, **kw):
    return argparse.Namespace(reuse=reuse, verify_reuse=0, **kw)


def test_reuse_copies_a_car_of_the_same_version():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        old = folder / "old"
        job = _job(folder)
        _built(old, job)
        media = job["media"]
        work = folder / "new" / "work"
        work.mkdir(parents=True)
        now = {**job, "sound_dir": str(folder / "new" / "sound")}
        reuse = port_batch.Reuse(_args(old), work)
        report = reuse.take(now)
        assert report is not None and report["reused_from"] == str(old)
        assert (work / f"{media}.chnk").read_bytes() == b"CHNK" + media.encode()
        assert (folder / "new" / "sound" / media / "sweep_new.wav").read_bytes() == b"wav"
        # The refreshed report is this build's own file (remerge reads it), not written
        # through the hard link into the earlier build's.
        assert not (work / f"{media}.json").samefile(old / "work" / f"{media}.json")
        assert json.loads((work / f"{media}.json").read_text())["host"] == 198
        other = port_batch.Reuse(_args(old), work)
        assert other.take({**now, "builder": "git fedcba9876543210"}) is None
        assert other.rebuilt[media] == "builder version"
        dirty = port_batch.Reuse(_args(old), work)
        assert dirty.take({**now, "builder": VERSION + builder_version.DIRTY}) is None
        assert dirty.rebuilt[media].endswith("(no reuse)")
        assert other.take({**now, "port_options": ["--damage", "--no-cockpit"]}) is None
        assert other.rebuilt[media] == "options"


def test_reuse_takes_each_car_from_the_first_build_that_has_it():
    """--reuse given twice (dsff install: the last build, then an interrupted one): a car
    missing from the first is taken from the second; a car in neither is built."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        first, second = folder / "first", folder / "second"
        (first / "work").mkdir(parents=True)
        job = {**_job(folder), "sound_dir": str(folder / "new" / "sound")}
        _built(second, job, b"CHNK2")
        work = folder / "new" / "work"
        work.mkdir(parents=True)
        reuse = port_batch.Reuse(_args([first, second]), work)
        report = reuse.take(job)
        assert report is not None and report["reused_from"] == str(second)
        assert (work / f"{job['media']}.chnk").read_bytes() == b"CHNK2"
        assert reuse.record()["sources"] == [str(first), str(second)]
        other = port_batch.Reuse(_args([first, second]), work)
        assert other.take({**job, "media": "TEST_Other_11"}) is None
        assert other.rebuilt["TEST_Other_11"] == "not built there"


def test_discs_or_a_dlc_pack_added_convert_only_their_cars():
    """FH1's cars built alone; then the FM4 discs and a DLC pack added (more images, FH1's
    renamed, the jobs' disc lists longer): the FH1 car and the first pack's car are taken,
    the new FM4 and second pack's cars are converted."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        old = folder / "old"
        fh1 = _job(folder, "TEST_Fh1_10", 7)
        dlc1 = _job(folder, "TEST_Dlc_11", 8, source=_dlc(folder))
        for job in (fh1, dlc1):
            _built(old, job)
        isos = [str(folder / "renamed" / "Forza Horizon.iso"), str(folder / "fm4a.iso"), str(folder / "fm4b.iso")]
        new_fh1 = {**_job(folder, "TEST_Fh1_10", 7, source=_fh1(folder / "renamed", "Forza Horizon.iso")), "isos": isos}
        new_dlc1 = {**_job(folder, "TEST_Dlc_11", 8, source=_dlc(folder / "moved")), "isos": isos}
        fm4 = {**_fh1(folder, "fm4a.iso"), "game": "FM4", "where": "FM4 disc 1"}
        new_fm4 = {**_job(folder, "TEST_Fm4_11", 9, source=fm4), "isos": isos}
        new_dlc2 = {**_job(folder, "TEST_Dlc2_12", 10, source=_dlc(folder, "Another Pack", "pack2")), "isos": isos}
        work = folder / "new" / "work"
        work.mkdir(parents=True)
        reuse = port_batch.Reuse(_args(old), work)
        taken = {j["media"] for j in (new_fh1, new_dlc1, new_fm4, new_dlc2) if reuse.take(j) is not None}
        assert taken == {"TEST_Fh1_10", "TEST_Dlc_11"}
        assert reuse.rebuilt == {"TEST_Fm4_11": "not built there", "TEST_Dlc2_12": "not built there"}


def test_resume_takes_only_cars_of_the_same_version():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        job = _job(folder)
        _built(folder / "out", job)
        work = folder / "out" / "work"
        assert port_batch.resumed_report(job, work)["ok"]
        assert port_batch.resumed_report({**job, "builder": "git another"}, work) is None
        assert port_batch.resumed_report({**job, "builder": VERSION + builder_version.DIRTY}, work) is None


def _old_build(folder: Path, old: Path, cars: list[str], sound_dir: Path) -> list[dict]:
    """An earlier build in `old` with each of `cars` converted."""
    jobs = []
    for n, media in enumerate(cars):
        job = {**_job(folder, media, 7 + n), "sound_dir": str(sound_dir)}
        _built(old, job)
        jobs.append(job)
    return jobs


def test_verify_mismatch_stops_port_batch_and_rebuilds_for_dsff_install():
    """Issue #133: a reused car that converts differently stops a developer's build; in
    dsff install (--verify-mismatch rebuild) its fresh copy is kept and the other cars
    reused from the same build are converted again."""
    cars = ["TEST_A_10", "TEST_B_10", "TEST_C_10"]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
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

        args = argparse.Namespace(reuse=old, verify_reuse=1, verify_seed=5)
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
    identical). Workers get port_batch.WORKER_ENV."""
    seen = []

    class FakeProc:
        pid = 0  # jobpool.adopt names it in the worker's slot

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
        assert seen and all(seen[0].get(k) == v for k, v in port_batch.WORKER_ENV.items())


def test_a_reused_cars_manifest_entry_follows_this_job():
    """The roster's games and the disc image's name are not in the reuse rule (the worker
    puts them in its report alone): a car built with only FH1's disc, reused once the FM4
    discs are there, says it is on both games, and names the disc as it is called now."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        job = _job(folder)
        report = {
            "media": job["media"],
            "ok": True,
            "host": 198,
            "host_name": "Host",
            "entry": {"spool_id": 7, "host": 198},
        }
        report["entry"].update(name="Test Car (2010)", year=2010, games_available="FH1", batch_fallback="x")
        now = {**job, "games": "FM4,FH1", "source": {**job["source"], "iso": str(folder / "renamed.iso")}}
        entry = port_batch.refresh_entry(report, now)["entry"]
        assert entry["games_available"] == "FM4,FH1" and entry["source"]["iso"] == "renamed.iso"
        assert entry["batch_fallback"] == "x" and entry["spool_id"] == 7  # the rest is the build's
        assert report["entry"]["games_available"] == "FH1"  # the earlier report stays as it was
        assert builder_version.why_not(job, now) is None


# --- results kept per version --------------------------------------------------------------------


def test_memo_keeps_a_result_per_builder_version():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "memo.json"
        calls = []

        def compute():
            calls.append(1)
            return 4

        real = builder_version.read
        builder_version.read = lambda _repo=None: VERSION
        try:
            memo = builder_version.Memo(path, VERSION)
            assert memo.get("car", compute) == 4 and len(calls) == 1
            memo.save()
            assert builder_version.Memo(path, VERSION).get("car", compute) == 4 and len(calls) == 1
            assert builder_version.Memo(path, "git another").get("car", compute) == 4 and len(calls) == 2
            dirty = builder_version.Memo(path, VERSION + builder_version.DIRTY)
            assert dirty.get("car", compute) == 4 and len(calls) == 3
            dirty.save()  # not kept
            assert set(json.loads(path.read_text())["versions"]) == {VERSION}
        finally:
            builder_version.read = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
