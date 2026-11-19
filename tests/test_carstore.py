"""The car store every worktree shares (src/dsfforza/build/carstore.py): cars kept by
fingerprint and taken by any build, paths of another checkout relocated, DLC pack paths
through a junction, eviction, garage pictures per pack fingerprint, and which cars a
change affects. No car is converted (a fraction of a second).

uv run python -m tests carstore
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from dsfforza.build import carstore, fingerprint, port_batch

MEDIA = "TEST_Car_10"


def _job(folder: Path, **kw) -> dict:
    return {
        "media": MEDIA,
        "game": "FH1",
        "year": 2010,
        "games": "FH1",
        "source": {"iso": str(folder / "disc.iso"), "path": "media/cars/x.zip", "package": ""},
        "spool_id": 7,
        "dsf": str(folder / "vanilla.sp"),
        "game_dir": str(folder),
        "cache": str(folder),
        "isos": [],
        "port_options": ["--damage"],
        "sound_dir": str(folder / "out" / "sound"),
        "chunk": str(folder / "out" / "work" / f"{MEDIA}.chnk"),
        "report": str(folder / "out" / "work" / f"{MEDIA}.json"),
        **kw,
    }


def _built(folder: Path, build: Path, job: dict, chunk: bytes = b"CHNK" + bytes(2 << 20)) -> None:
    """A car `build` converted from `job`: its work files with a fingerprint that holds."""
    from dsfforza.formats import xds

    data = folder / "car" / "input.bin"
    data.parent.mkdir(parents=True, exist_ok=True)
    if not data.exists():
        data.write_bytes(bytes(64))
    work = build / "work"
    work.mkdir(parents=True, exist_ok=True)
    recorder = fingerprint.Recorder([build]).start()
    try:
        xds.parse_header(data.read_bytes())
    finally:
        fp = recorder.finish(job)
    media = job["media"]
    (work / f"{media}.fp.json").write_text(json.dumps(fp))
    (work / f"{media}.json").write_text(json.dumps({"media": media, "ok": True, "host": 198, "entry": {}}))
    (work / f"{media}.chnk").write_bytes(chunk)
    (work / f"{media}.job.json").write_text(json.dumps(job))
    (work / f"{media}.log").write_text("log")
    sound = build / "sound" / media
    sound.mkdir(parents=True, exist_ok=True)
    (sound / "sweep_new.wav").write_bytes(b"wav" * 1000)


def _args(**kw):
    return argparse.Namespace(reuse=None, rebuild_where=None, trust_code=False, verify_reuse=None, **kw)


def test_a_car_put_by_one_build_is_taken_by_another():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        store = carstore.Store(folder / "store", log=lambda *_: None)
        job = _job(folder)
        first = folder / "first"
        _built(folder, first, job)
        entry = store.put(first / "work", MEDIA, first / "sound")
        assert entry is not None and store.stats["put"] == 1
        assert store.put(first / "work", MEDIA, first / "sound").folder == entry.folder  # kept once
        # The chunk is a hard link (where the volume allows), the small files copies.
        chunk = entry.work("chnk")
        assert chunk.stat().st_nlink >= 1 and entry.work("log").read_text() == "log"

        work = folder / "second" / "work"
        work.mkdir(parents=True)
        second = {**job, "sound_dir": str(folder / "second" / "sound")}
        reuse = port_batch.Reuse(_args(), work, store)
        assert reuse.verify_count == 0  # store hits alone: no verification unless asked
        report = reuse.take(second)
        assert report is not None and report["reused_from"] == port_batch.STORE
        assert (work / f"{MEDIA}.chnk").read_bytes() == (first / "work" / f"{MEDIA}.chnk").read_bytes()
        assert (folder / "second" / "sound" / MEDIA / "sweep_new.wav").read_bytes() == b"wav" * 1000
        assert reuse.record()["from_store"] == [MEDIA]

        # Another spool ID, other options or a changed input: built again.
        other = port_batch.Reuse(_args(), work, store)
        assert other.take({**second, "spool_id": 8}) is None and other.rebuilt[MEDIA] == "store: job: spool_id"
        (folder / "car" / "input.bin").write_bytes(bytes(65))
        changed = port_batch.Reuse(_args(), work, store)
        assert changed.take(second) is None and changed.rebuilt[MEDIA].startswith("store: input ")


def test_paths_of_another_checkout_are_relocated():
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "wt-a", Path(tmp) / "wt-b"
        for wt in (a, b):
            (wt / "out" / "db").mkdir(parents=True)
            (wt / "out" / "db" / "fh1_gamedb.slt").write_bytes(b"gamedb")
        ra, rb = fingerprint.norm(a), fingerprint.norm(b)
        file_a = fingerprint.norm(a / "out" / "db" / "fh1_gamedb.slt")
        fp = {
            "format": fingerprint.FORMAT,
            "complete": True,
            "repo": ra,
            "job": {"cache": ra + os.sep + "cache", "isos": [], "source": {"package": ra + os.sep + "p.pkg"}},
            "env": fingerprint.environment(),
            "files": {file_a: fingerprint.file_digest(file_a)},
            "dirs": {},
            "code": {},
        }
        moved = fingerprint.relocate(fp, ra, rb)
        assert list(moved["files"]) == [fingerprint.norm(b / "out" / "db" / "fh1_gamedb.slt")]
        assert moved["job"]["cache"] == rb + os.sep + "cache"
        assert moved["job"]["source"]["package"] == fingerprint.norm(b / "p.pkg")
        assert fingerprint.Checker().inputs_changed(moved) is None
        (b / "out" / "db" / "fh1_gamedb.slt").write_bytes(b"another gamedb")  # this checkout's own copy
        assert fingerprint.Checker().inputs_changed(moved).startswith("input ")
        # The same car built in the two checkouts is one store entry.
        fp_b = json.loads(json.dumps(fp).replace(json.dumps(ra)[1:-1], json.dumps(rb)[1:-1]))
        assert fp_b["repo"] == rb and carstore.entry_id(fp, None, ra) == carstore.entry_id(fp_b, None, rb)


def test_relocate_moves_the_dlc_answer_paths():
    """The "dlc" discovery answer is pack paths: they move with the checkout, disc answers
    (tokens) stay (CodeRabbit on #636)."""
    old, new = fingerprint.norm(Path("/wt-a")), fingerprint.norm(Path("/wt-b"))
    pack = old + os.sep + "assets" + os.sep + "pack"
    fp = {"repo": old, "job": {}, "files": {}, "dirs": {}, "queries": {"dlc": [pack], "discs FH1": ["disc:FH1:1"]}}
    moved = fingerprint.relocate(fp, old, new)
    assert moved["queries"]["dlc"] == [fingerprint._resolve(new, "assets") + os.sep + "pack"]
    assert moved["queries"]["discs FH1"] == ["disc:FH1:1"]


def test_a_nested_root_that_did_not_move_keeps_its_paths():
    """With the cache inside the game folder unchanged and the game folder moved, a cache
    input stays at the cache's path; it doesn't take the parent's move (CodeRabbit on #636)."""
    g1, g2 = fingerprint.norm(Path("/game1")), fingerprint.norm(Path("/game2"))
    cache = g1 + os.sep + "cache"
    fp = {
        "job": {"cache": cache, "game_dir": g1, "dsf": None},
        "files": {cache + os.sep + "x.bin": "d", g1 + os.sep + "y.bin": "e"},
        "dirs": {},
    }
    moved = fingerprint.move_roots(fp, {"cache": cache, "game_dir": g2})
    assert set(moved["files"]) == {cache + os.sep + "x.bin", g2 + os.sep + "y.bin"}
    assert fingerprint.move_roots(fp, {"cache": cache, "game_dir": g1}) is fp


def test_a_dlc_pack_path_is_recorded_through_its_junction():
    """A DLC car's job names its pack file under the worktree's assets\\ (a junction to the
    main checkout's): recorded resolved, so another worktree's build matches (#501)."""
    with tempfile.TemporaryDirectory() as tmp:
        real = Path(tmp) / "main" / "assets"
        real.mkdir(parents=True)
        (real / "pack.bin").write_bytes(b"pack")
        link = Path(tmp) / "wt" / "assets"
        link.parent.mkdir()
        try:
            if os.name == "nt":
                import subprocess

                subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True, capture_output=True)
            else:
                link.symlink_to(real, target_is_directory=True)
        except (OSError, subprocess.CalledProcessError):
            return
        job = _job(Path(tmp), source={"iso": "", "path": "x.zip", "package": str(link / "pack.bin")})
        key = fingerprint.job_key(job)
        assert key["source"]["package"] == fingerprint.norm(real / "pack.bin")
        in_disc = _job(Path(tmp), source={"iso": "", "path": "x", "package": "content/0000/4d530910/00000002/x"})
        assert fingerprint.job_key(in_disc)["source"]["package"] == "content/0000/4d530910/00000002/x"
        # A fingerprint recorded before the packs were resolved still matches.
        old = {"format": fingerprint.FORMAT, "complete": True, "job": {**key, "source": job["source"]}}
        old.update(env=fingerprint.environment(), files={}, dirs={}, code={})
        assert fingerprint.Checker().check(old, job) is None


def _junction(link: Path, target: Path) -> bool:
    """`link` as a junction (Windows) or symlink to `target`; False where neither can be made."""
    import subprocess

    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        else:
            link.symlink_to(target, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def test_an_entry_the_main_checkout_made_holds_in_a_worktree():
    """#588: a worktree's out/cache is a junction to the main checkout's
    (tools/worktree-setup.ps1). An entry recorded in the main checkout names its cache files
    under the main checkout; relocated to the worktree they stay there, as the worktree's own
    recorder writes them, and its own out/db moves to the worktree's."""
    with tempfile.TemporaryDirectory() as tmp:
        main, wt = Path(tmp) / "main", Path(tmp) / "wt"
        for repo in (main, wt):
            (repo / "out" / "db").mkdir(parents=True)
            (repo / "out" / "db" / "fh1_gamedb.slt").write_bytes(b"gamedb")
        (main / "out" / "cache" / "fh1").mkdir(parents=True)
        (main / "out" / "cache" / "fh1" / "car.xds").write_bytes(b"extracted")
        if not _junction(wt / "out" / "cache", main / "out" / "cache"):
            return
        rm, rw = fingerprint.norm(main), fingerprint.norm(wt)
        job_main = _job(Path(tmp), cache=str(main / "out" / "cache"))  # the game and discs outside
        recorder = fingerprint.Recorder([main / "built"]).start()
        try:
            (main / "out" / "cache" / "fh1" / "car.xds").read_bytes()
            (main / "out" / "db" / "fh1_gamedb.slt").read_bytes()
        finally:
            fp = recorder.finish(job_main)
        fp["repo"] = rm
        moved = fingerprint.relocate(fp, rm, rw)
        cache_file = fingerprint.norm(main / "out" / "cache" / "fh1" / "car.xds")
        assert cache_file in moved["files"]  # through the junction: the main checkout's file
        assert fingerprint.norm(wt / "out" / "db" / "fh1_gamedb.slt") in moved["files"]
        assert (
            moved["job"]["cache"] == fingerprint.norm(wt / "out" / "cache") == fingerprint.norm(main / "out" / "cache")
        )
        job_wt = {**job_main, "cache": str(wt / "out" / "cache")}  # what the worktree's build passes
        assert fingerprint.Checker().check(moved, job_wt) is None
        (wt / "out" / "db" / "fh1_gamedb.slt").write_bytes(b"the worktree's own gamedb differs")
        assert fingerprint.Checker().check(moved, job_wt).startswith("input ")


def test_the_entries_used_longest_ago_go_first():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        store = carstore.Store(folder / "store", log=lambda *_: None)
        entries = []
        for n in range(3):
            job = _job(folder, media=f"TEST_Car{n}_10", spool_id=10 + n)
            build = folder / f"b{n}"
            _built(folder, build, job, chunk=bytes(1 << 20))
            entries.append(store.put(build / "work", job["media"], build / "sound"))
        now = time.time()
        for n, e in enumerate(entries):  # car 1 used longest ago, then 0, then 2
            e.touch()
            os.utime(e.folder / "used", (now - [200, 300, 100][n],) * 2)
        size = entries[0].meta["size"]
        removed, _ = store.evict(int(size * 2.5))  # down to 90%: 2.25 entries
        assert removed == 1 and not entries[1].folder.exists() and entries[0].folder.exists()
        removed, _ = store.evict(int(size * 1.5))
        assert removed == 1 and not entries[0].folder.exists() and entries[2].folder.exists()


def test_pictures_are_kept_per_pack_fingerprint():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        store = carstore.Store(folder / "store", log=lambda *_: None)
        (folder / "input").write_bytes(b"x")
        holds = {"format": fingerprint.FORMAT, "complete": True, "env": fingerprint.environment(), "code": {}}
        holds.update(files={fingerprint.norm(folder / "input"): fingerprint.file_digest(str(folder / "input"))})
        holds["dirs"] = {}
        stale = {**holds, "files": {fingerprint.norm(folder / "input"): "sha256:0"}}
        dds, png = folder / "p.dds", folder / "p.png"
        dds.write_bytes(b"DDS")
        png.write_bytes(b"PNG")
        store.put_picture("k1", dds, png, "render", store.put_pack(stale))
        assert store.picture("k1", fingerprint.Checker()) is None  # made under code or inputs that changed
        store.put_picture("k1", dds, png, "render", store.put_pack(holds))  # the same key, another pack
        hit = store.picture("k1", fingerprint.Checker())
        assert hit is not None and hit[0].read_bytes() == b"DDS" and hit[2] == "render"
        image = folder / "i.bin"
        image.write_bytes(b"IMG")
        store.put_image('["plate", "X"]', image, store.put_pack(holds))
        found = store.images(fingerprint.Checker())
        assert list(found) == ['["plate", "X"]'] and found['["plate", "X"]'][0].stem.startswith(
            carstore.image_name('["plate", "X"]')
        )


class Units:
    """Code units by module, as CodeUnits gives them."""

    def __init__(self, table: dict):
        self.table = table

    def get(self, module):
        return self.table.get(module)


def test_affected_lists_the_cars_whose_recorded_units_changed():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        store = carstore.Store(folder / "store", log=lambda *_: None)
        fps = {}
        for n, media in enumerate(("TEST_A_10", "TEST_B_10")):
            job = _job(folder, media=media, spool_id=20 + n)
            build = folder / f"b{n}"
            _built(folder, build, job)
            fp = json.loads((build / "work" / f"{media}.fp.json").read_text())
            # Car A ran lamp() as well as parse_header; car B didn't.
            fp["code"] = {"m.x": {"parse": "p1", **({"lamp": "l1"} if media == "TEST_A_10" else {})}}
            (build / "work" / f"{media}.fp.json").write_text(json.dumps(fp))
            store.put(build / "work", media, build / "sound")
            fps[media] = fp
        base = Units({"m.x": {"parse": "p1", "lamp": "l1"}})
        cars = [("TEST_A_10", 20), ("TEST_B_10", 21), ("TEST_C_10", 22)]
        saved = fingerprint.CodeUnits.get
        try:
            fingerprint.CodeUnits.get = lambda _self, m: {"m.x": {"parse": "p1", "lamp": "l2"}}.get(m)
            found = carstore.affected(store, cars, "base", base_units=base)
        finally:
            fingerprint.CodeUnits.get = saved
        assert found["TEST_A_10"]["status"] == "affected" and found["TEST_A_10"]["why"] == ["code m.x.lamp"]
        assert found["TEST_B_10"]["status"] == "same"
        assert found["TEST_C_10"]["status"] == "unknown"
        # An entry made by other code than the base's is no base entry.
        other = carstore.affected(store, cars[:1], "base", base_units=Units({"m.x": {"parse": "p0", "lamp": "l1"}}))
        assert other["TEST_A_10"]["status"] == "unknown"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
