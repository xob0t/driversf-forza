"""The car store every worktree shares (src/dsfforza/build/carstore.py): cars kept by the
reuse rule (builder version, media and options, build ID, own source) and taken by any
build of the same version wherever its folders are, nothing kept from a dirty tree,
eviction, garage pictures per builder version. No car is converted (a fraction of a second).

uv run python -m tests carstore
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

from dsfforza.build import builder_version, carstore, port_batch

MEDIA = "TEST_Car_10"
VERSION = "git 0123456789abcdef"


def _job(folder: Path, **kw) -> dict:
    return {
        "builder": VERSION,
        "media": MEDIA,
        "game": "FH1",
        "year": 2010,
        "games": "FH1",
        "source": {"game": "FH1", "where": "FH1", "iso": str(folder / "disc.iso"), "path": "media/cars/x.zip"},
        "spool_id": 7,
        "dsf": str(folder / "vanilla.sp"),
        "game_dir": str(folder),
        "cache": str(folder / "cache"),
        "isos": [str(folder / "disc.iso")],
        "port_options": ["--damage"],
        "sound_dir": str(folder / "out" / "sound"),
        "chunk": str(folder / "out" / "work" / f"{MEDIA}.chnk"),
        "report": str(folder / "out" / "work" / f"{MEDIA}.json"),
        **kw,
    }


def _built(build: Path, job: dict, chunk: bytes = b"CHNK" + bytes(2 << 20)) -> None:
    """A car `build` converted from `job`: its work files and sound folder."""
    media = job["media"]
    work = build / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / f"{media}.json").write_text(json.dumps({"media": media, "ok": True, "host": 198, "entry": {}}))
    (work / f"{media}.chnk").write_bytes(chunk)
    (work / f"{media}.job.json").write_text(json.dumps(job))
    (work / f"{media}.log").write_text("log")
    sound = build / "sound" / media
    sound.mkdir(parents=True, exist_ok=True)
    (sound / "sweep_new.wav").write_bytes(b"wav" * 1000)


def _args(**kw):
    return argparse.Namespace(reuse=None, verify_reuse=None, **kw)


def _checkout(version: str = VERSION):
    """This checkout's builder version, as builder_version.read gives it."""
    return mock.patch.object(builder_version, "read", lambda _repo=None: version)


def test_a_car_put_by_one_build_is_taken_by_another():
    """A second worktree at the same commit: its folders (out/, the cache, the disc images)
    are elsewhere, its cars are the store's."""
    with tempfile.TemporaryDirectory() as tmp, _checkout():
        folder = Path(tmp)
        store = carstore.Store(folder / "store", log=lambda *_: None)
        main = folder / "main"
        _built(main / "out", _job(main))
        entry = store.put(main / "out" / "work", MEDIA, main / "out" / "sound")
        assert entry is not None and store.stats["put"] == 1
        assert store.put(main / "out" / "work", MEDIA, main / "out" / "sound").folder == entry.folder  # kept once
        worktree = folder / "worktree"
        job = _job(worktree, source={**_job(worktree)["source"], "iso": str(worktree / "Forza Horizon.iso")})
        work = worktree / "out" / "work"
        work.mkdir(parents=True)
        reuse = port_batch.Reuse(_args(), work, store)
        report = reuse.take(job)
        assert report is not None and report["reused_from"] == port_batch.STORE
        assert (work / f"{MEDIA}.chnk").read_bytes()[:4] == b"CHNK"
        assert (worktree / "out" / "sound" / MEDIA / "sweep_new.wav").exists()
        assert store.stats["hits"] == 1
        # Another version (another commit), ID or options: not the store's car.
        for changed in ({"builder": "git fedcba9876543210"}, {"spool_id": 8}, {"port_options": []}):
            found, why = store.find({**job, **changed})
            assert found is None and why == "not in the store", changed


def test_nothing_is_kept_or_taken_from_a_dirty_tree():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        store = carstore.Store(folder / "store", log=lambda *_: None)
        dirty = VERSION + builder_version.DIRTY
        _built(folder / "out", _job(folder, builder=dirty))
        with _checkout(dirty):
            assert store.put(folder / "out" / "work", MEDIA, folder / "out" / "sound") is None
            found, why = store.find(_job(folder, builder=dirty))
            assert found is None and why.endswith("(no reuse)")
        # Nor a car whose version isn't this checkout's now (the code changed while it was built).
        _built(folder / "out", _job(folder))
        with _checkout("git another"):
            assert store.put(folder / "out" / "work", MEDIA, folder / "out" / "sound") is None
        assert not (folder / "store" / "cars").exists()


def test_the_entries_used_longest_ago_go_first():
    with tempfile.TemporaryDirectory() as tmp, _checkout():
        folder = Path(tmp)
        store = carstore.Store(folder / "store", log=lambda *_: None)
        entries = []
        for n in range(3):
            job = _job(folder, media=f"TEST_Car{n}_10", spool_id=10 + n)
            build = folder / f"b{n}"
            _built(build, job, chunk=bytes(1 << 20))
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


def test_pictures_and_images_are_kept_per_builder_version():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        store = carstore.Store(folder / "store", log=lambda *_: None)
        dds, png = folder / "p.dds", folder / "p.png"
        dds.write_bytes(b"DDS")
        png.write_bytes(b"PNG")
        store.put_picture("k1", dds, png, "render", VERSION)
        assert store.picture("k1", "git another") is None  # made by other code
        hit = store.picture("k1", VERSION)
        assert hit is not None and hit[0].read_bytes() == b"DDS" and hit[2] == "render"
        store.put_picture("k2", dds, png, "render", VERSION + builder_version.DIRTY)
        assert store.picture("k2", VERSION + builder_version.DIRTY) is None  # a dirty tree's: never kept
        image = folder / "i.bin"
        image.write_bytes(b"IMG")
        store.put_image('["plate", "X"]', image, VERSION)
        found = store.images(VERSION)
        assert list(found) == ['["plate", "X"]'] and found['["plate", "X"]'].stem.startswith(
            carstore.image_name('["plate", "X"]')
        )
        assert store.images("git another") == {}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
