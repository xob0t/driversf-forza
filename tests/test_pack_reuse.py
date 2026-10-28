"""port_batch build's pack reuse (src/dsfforza/build/pack_reuse.py): the whole pack taken
from an earlier build, or only the changed cars linted and drawn, gives the files a full
pack gives. The merge, upvh lint and the garage pictures are stand-ins here (no game,
no disc images; a fraction of a second); the real ones run with --verify-pack.

uv run python tests/test_pack_reuse.py     # or every test: uv run python -m tests pack_reuse
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from dsfforza.build import fingerprint, pack_reuse, port_batch
from dsfforza.game import gui_assets

CARS = ["BMW_M3E30_91", "TOY_SupraRZ_98", "MAZ_RX7_97", "HON_S2000_03"]


class Stand:
    """Stand-ins for the merge, upvh lint and gui_assets.build, with what each was asked.

    A car's chunk is JSON: the UIDs it defines and references, its sound IDs, a version.
    The "archive" is the vanilla file plus every chunk, one per line. A car's lint fails
    when another car in the archive defines a UID it references but doesn't define, or
    shares a sound ID with it (the cross-vehicle checks of upvh lint)."""

    def __init__(self, work: Path):
        self.work = work
        self.merged = 0
        self.linted: list[list[str]] = []
        self.rendered: list[str] = []
        self.plates: list[str] = []

    def merge(self, dsf, out, results, work, log=print, root_limit=0):
        self.merged += 1
        data = bytearray(Path(dsf).read_bytes() + b"\n")
        cars = []
        for n, r in enumerate(sorted(results, key=lambda r: r.get("new_id", r["spool_id"]))):
            data += (Path(work) / f"{r['media']}.chnk").read_bytes().strip() + b"\n"
            c = r["entry"]
            c["vehc"] = [3 * n, 3 * n + 1, 3 * n + 2]
            cars.append(c)
        out.write_bytes(bytes(data))
        return cars, []

    def archive_cars(self, archive: Path) -> dict[str, dict]:
        lines = archive.read_bytes().split(b"\n")[1:]
        return {d["media"]: d for d in (json.loads(line) for line in lines if line.strip())}

    def lint(self, out, dsf, cars, parallel, log=print, facts=False, defined=(), records=None, record_dir=None):
        self.linted.append(sorted(c["media"] for c in cars))
        everyone = self.archive_cars(Path(out))
        found = {}
        for c in cars:
            me = everyone[c["media"]]
            others = [d for d in everyone.values() if d["media"] != c["media"]]
            refs = set(me["refs"]) - set(me["defines"])
            bad = [d["media"] for d in others if refs & set(d["defines"])]
            near = sorted(d["spool_id"] for d in others if set(me["sound"]) & set(d["sound"]))
            c["lint"] = "ok" if not bad and not near else f"FAIL references {bad} sound shared with {near}"
            if facts:
                found[c["media"]] = {"refs_ext": sorted(refs), "near": near}
                if c["spool_id"] in defined:
                    found[c["media"]]["defined"] = sorted(me["defines"])
        if records is not None:
            records.append({"complete": True, "tree": self.tree, "files": [], "dirs": [], "wanted": {}})
        return found

    @property
    def tree(self) -> str:
        return fingerprint.tree_digest(fingerprint.package_hashes())

    def gui(self, manifest, out_dir, log=print, archive=None, cache=None, record=None, **_):
        data = json.loads(Path(manifest).read_text())
        everyone = self.archive_cars(Path(archive))
        out_dir.mkdir(parents=True, exist_ok=True)
        # Nameplates: one image per model name, drawn unless an earlier build has it.
        plates = dict(cache.images()) if cache is not None else {}
        used = []
        for car in data["cars"]:
            key = json.dumps(["plate", car["media"].split("_")[1], 76, 0.22])
            if key not in plates:
                self.plates.append(car["media"])
                plates[key] = gui_assets.Image(2, 1, bytes(range(8)))
            used.append(bytes(plates[key].px) + key.encode())
        (out_dir / "plates.bin").write_bytes(b"".join(used))
        if cache is not None:
            cache.keep_images(plates)
        pictures = []
        for car in data["cars"]:
            name = f"dsfforza_{car['media'].lower()}"
            extra = {"text": {"model": car["media"].split("_")[1]}, "thumbnails": False}
            hit = cache.get(car, extra) if cache is not None else None
            if hit is None:
                self.rendered.append(car["media"])
                dds = hashlib.sha256(json.dumps(everyone[car["media"]], sort_keys=True).encode()).digest() * 4
                png, source = b"PNG" + dds[:16], "render"
            else:
                dds, png, source = hit
            target = out_dir / f"{name}.png"
            target.unlink(missing_ok=True)
            target.write_bytes(png)
            if hit is None and cache is not None:
                cache.put(car, extra, dds, png, source)
            pictures.append(dds)
        (out_dir / "gui.sp").write_bytes(b"".join(pictures))
        (out_dir / "hud.lua").write_text(f"return {len(pictures)}\n")
        (out_dir / "Fonts.fchunk").write_bytes(b"fonts")
        if record is not None:
            record.append({"complete": True, "tree": self.tree, "files": [], "dirs": [], "wanted": {}})


def write_car(work: Path, media: str, spool_id: int, defines, refs, sound, version=0) -> dict:
    chunk = {"media": media, "spool_id": spool_id, "defines": defines, "refs": refs, "sound": sound, "v": version}
    (work / f"{media}.chnk").write_text(json.dumps(chunk, sort_keys=True))
    entry = {"media": media, "spool_id": spool_id, "host": 205, "host_name": "x", "name": media, "game": "FH1"}
    return {"media": media, "game": "FH1", "spool_id": spool_id, "host": 205, "ok": True, "entry": entry}


def make_cars(work: Path) -> dict[str, dict]:
    work.mkdir(parents=True, exist_ok=True)
    return {
        media: write_car(work, media, n + 1, [f"d{n}a", f"d{n}b"], [f"d{n}a", "shared"], [100 + n])
        for n, media in enumerate(CARS)
    }


def args_for(iso: Path) -> argparse.Namespace:
    return argparse.Namespace(
        gui=True,
        wide_glyphs=False,
        root_limit=3003,
        jobs=1,
        id_policy="asi",
        iso=[iso],
        progress=None,
        clean_work=False,
        verify_pack=False,
    )


PORT_ARGS = argparse.Namespace(host=None, sound_host=None, textures="dsf", share_host_uids=False, no_cockpit=False)


def pack(root: Path, name: str, results: dict, sources=(), reuse=True, stand=None):
    """port_batch.finish of `results` into root/name (the cars' chunks in root/work)."""
    out = root / name
    out.mkdir(parents=True, exist_ok=True)
    args = args_for(root / "game.iso")
    dsf = root / "vanilla.sp"
    p = None
    if reuse:
        settings = pack_reuse.settings_of(args, dsf, 3003, [root / "game.iso"])
        p = pack_reuse.PackReuse(out, [root / s for s in sources], settings, log=lambda *a: None)
    ok = json.loads(json.dumps(list(results.values())))
    port_batch.finish(args, dsf, out, root / "work", ok, [], PORT_ARGS, [], {}, time.time(), {}, p)
    return p


def files_of(folder: Path) -> dict[str, bytes]:
    out = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if path.is_file() and not rel.startswith(("pack/", "batch-report")):
            data = path.read_bytes()
            if rel == "dngvehicles.json":
                manifest = json.loads(data)
                manifest.pop("created")
                manifest.pop("archive")
                data = json.dumps(manifest, sort_keys=True).encode()
            out[rel] = data
    return out


class Patched:
    def __init__(self, stand: Stand):
        self.stand = stand

    def __enter__(self):
        self.saved = (port_batch.merge, port_batch.lint_all, gui_assets.build)
        port_batch.merge, port_batch.lint_all, gui_assets.build = self.stand.merge, self.stand.lint, self.stand.gui
        return self.stand

    def __exit__(self, *exc):
        port_batch.merge, port_batch.lint_all, gui_assets.build = self.saved


def setup(root: Path) -> dict[str, dict]:
    (root / "vanilla.sp").write_bytes(b"VANILLA")
    (root / "game.iso").write_bytes(b"ISO")
    return make_cars(root / "work")


def test_an_unchanged_pack_is_taken_whole():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        results = setup(root)
        with Patched(Stand(root / "work")) as stand:
            pack(root, "a", results)
            assert stand.merged == 1 and stand.linted == [sorted(CARS)] and len(stand.rendered) == 4
            p = pack(root, "b", results, sources=["a"])
            assert stand.merged == 1 and len(stand.linted) == 1 and len(stand.rendered) == 4  # nothing again
            assert p.stats["whole"] == str(root / "a")
            # The archive is a hard link, gui/ files copies of their own.
            assert (root / "b" / "dngvehicles.sp").stat().st_ino == (root / "a" / "dngvehicles.sp").stat().st_ino
            assert (root / "b" / "gui" / "gui.sp").stat().st_ino != (root / "a" / "gui" / "gui.sp").stat().st_ino
            # Again in its own folder (a rerun with --resume): nothing to do either.
            pack(root, "b", results)
            assert stand.merged == 1
            pack(root, "full", results, reuse=False)
        assert files_of(root / "b") == files_of(root / "full")
        assert files_of(root / "a") == files_of(root / "full")


def test_only_changed_cars_are_linted_and_drawn():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        results = setup(root)
        with Patched(Stand(root / "work")) as stand:
            pack(root, "a", results)
            results["MAZ_RX7_97"] = write_car(root / "work", "MAZ_RX7_97", 3, ["d2a", "d2b"], ["d2a"], [102], 1)
            stand.linted.clear()
            stand.rendered.clear()
            p = pack(root, "b", results, sources=["a"])
            assert stand.merged == 2
            assert stand.linted == [["MAZ_RX7_97"]]
            assert stand.rendered == ["MAZ_RX7_97"]
            assert len(stand.plates) == 4  # all from the first pack's images
            assert p.stats["pictures_reused"] == 3 and p.stats["pictures_made"] == 1
            pack(root, "full", results, reuse=False)
        assert files_of(root / "b") == files_of(root / "full")


def test_a_change_relints_the_unchanged_cars_it_reaches():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        results = setup(root)
        with Patched(Stand(root / "work")) as stand:
            pack(root, "a", results)
            # Car 3 now defines "shared", which every car references: they all fail lint now.
            results["MAZ_RX7_97"] = write_car(root / "work", "MAZ_RX7_97", 3, ["d2a", "shared"], ["d2a"], [102], 1)
            stand.linted.clear()
            pack(root, "b", results, sources=["a"])
            assert stand.linted == [["MAZ_RX7_97"], sorted(set(CARS) - {"MAZ_RX7_97"})]
            pack(root, "full", results, reuse=False)
            assert files_of(root / "b") == files_of(root / "full")
            lint = {c["media"]: c["lint"] for c in json.loads((root / "b" / "dngvehicles.json").read_text())["cars"]}
            assert lint["BMW_M3E30_91"].startswith("FAIL") and lint["MAZ_RX7_97"] == "ok"
            # Car 4 now shares car 1's sound ID; then car 3 goes back: the failed ones are linted again.
            results["HON_S2000_03"] = write_car(root / "work", "HON_S2000_03", 4, ["d3a"], [], [100], 1)
            results["MAZ_RX7_97"] = write_car(root / "work", "MAZ_RX7_97", 3, ["d2a", "d2b"], ["d2a"], [102], 2)
            stand.linted.clear()
            pack(root, "c", results, sources=["b"])
            pack(root, "full2", results, reuse=False)
        assert files_of(root / "c") == files_of(root / "full2")
        lint = {c["media"]: c["lint"] for c in json.loads((root / "c" / "dngvehicles.json").read_text())["cars"]}
        assert lint["BMW_M3E30_91"].endswith("sound shared with [4]") and lint["TOY_SupraRZ_98"] == "ok"


def test_a_removed_car_relints_the_cars_whose_sound_it_shared():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        results = setup(root)
        results["HON_S2000_03"] = write_car(root / "work", "HON_S2000_03", 4, ["d3a"], [], [100], 1)
        with Patched(Stand(root / "work")) as stand:
            pack(root, "a", results)
            del results["HON_S2000_03"]
            stand.linted.clear()
            pack(root, "b", results, sources=["a"])
            assert stand.linted == [["BMW_M3E30_91"]]  # it failed: linted again, and passes now
            pack(root, "full", results, reuse=False)
        assert files_of(root / "b") == files_of(root / "full")


def test_other_settings_inputs_or_a_changed_output_mean_a_full_pack():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        results = setup(root)
        with Patched(Stand(root / "work")) as stand:
            pack(root, "a", results)
            (root / "vanilla.sp").write_bytes(b"VANILLA2")  # in the settings (its digest)
            stand.rendered.clear()
            pack(root, "b", results, sources=["a"])
            assert len(stand.rendered) == 4 and stand.merged == 2
            # A changed output: the archive isn't taken whole, the pictures still are.
            archive = root / "b" / "dngvehicles.sp"
            archive.write_bytes(archive.read_bytes() + b"x")
            stand.rendered.clear()
            p = pack(root, "c", results, sources=["b"])
            assert stand.merged == 3 and stand.rendered == [] and p.stats["whole"] is None
            # A recorded input changed: nothing is taken from that build.
            record = json.loads((root / "c" / "pack" / "pack.json").read_text())
            extra = root / "input.txt"
            extra.write_text("1")
            record["fingerprint"]["files"][fingerprint.norm(extra)] = fingerprint.file_digest(extra)
            (root / "c" / "pack" / "pack.json").write_text(json.dumps(record))
            extra.write_text("2")
            stand.rendered.clear()
            p = pack(root, "d", results, sources=["c"])
            assert len(stand.rendered) == 4 and p.earlier == []


def test_the_fingerprint_counts_the_subprocesses_records():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "in.bin").write_bytes(bytes(64))
        recorder = fingerprint.Recorder().start()
        try:
            from dsfforza.formats import xds

            xds.parse_header((root / "in.bin").read_bytes())
            drained = recorder.drain()
        finally:
            recorder.stop()
        assert fingerprint.norm(root / "in.bin") in drained["files"]
        assert "parse_header" in drained["wanted"]["dsfforza.formats.xds"]
        parent = fingerprint.Recorder().start()
        fp = parent.finish({}, [drained], skip=(fingerprint.norm(root) + os.sep,))
        assert "parse_header" in fp["code"]["dsfforza.formats.xds"]
        assert fingerprint.norm(root / "in.bin") not in fp["files"]  # skipped
        assert fp["complete"]
        assert not parent.finish({}, [{**drained, "tree": "other"}])["complete"]


def test_images_keep_their_pixels_and_attributes():
    image = gui_assets.Image(3, 2, bytes(range(24)))
    image.kind, image.text_badge = "plate", True
    back = pack_reuse.unpack_image(gui_assets.Image, pack_reuse.pack_image(image))
    assert (back.w, back.h, bytes(back.px), back.kind, back.text_badge) == (3, 2, bytes(range(24)), "plate", True)
    assert pack_reuse.unpack_image(gui_assets.Image, pack_reuse.pack_image(None)) is None


def test_game_files_read_from_the_backup_after_an_install_are_the_same_inputs():
    from dsfforza.game import game_paths

    with tempfile.TemporaryDirectory() as tmp:
        game = Path(tmp)
        (game / "Install").mkdir()
        (game / "Install" / "gui.sp").write_bytes(b"vanilla gui")
        gui = game / "Install" / "gui.sp"
        fp = {"files": {fingerprint.norm(gui): fingerprint.file_digest(gui)}}
        # An install: the vanilla file moves to the backup, the game gets ours.
        (game / game_paths.BACKUP / "Install").mkdir(parents=True)
        (game / game_paths.BACKUP / "Install" / "gui.sp").write_bytes(b"vanilla gui")
        gui.write_bytes(b"our gui")
        checker = fingerprint.Checker()
        assert pack_reuse.moved_inputs(fp, checker, None) == {}  # no game folder: nothing moved
        moved = pack_reuse.moved_inputs(fp, checker, game)
        assert list(moved.values()) == [fingerprint.norm(game / game_paths.BACKUP / "Install" / "gui.sp")]
        fp = {"format": fingerprint.FORMAT, "complete": True, "env": fingerprint.environment(), **fp}
        assert checker.check(fp, None).startswith("input ")
        assert checker.check(fp, None, moved=moved) is None


def test_compare_packs_names_what_differs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        results = setup(root)
        with Patched(Stand(root / "work")):
            pack(root, "a", results)
            pack(root, "full", results, reuse=False)
        assert port_batch.compare_packs(root / "a", root / "full", True) == []
        (root / "a" / "gui" / "hud.lua").write_text("other\n")
        (root / "a" / "gui" / "dsfforza_other_car_00.png").write_bytes(b"x")
        assert port_batch.compare_packs(root / "a", root / "full", True) == ["gui/hud.lua"]


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("ok", name)
