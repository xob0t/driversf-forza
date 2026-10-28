"""Pack reuse: port_batch's pack step (merge, lint, manifest, garage pictures and HUD
badges: port_batch.finish) redoes only what changed since an earlier build
(research/batch.md, "Pack reuse").

A build's pack record, <build>/pack/pack.json, holds:

- "settings": what shapes the pack besides the cars: the ID policy, the root entry limit,
  --gui, --wide-glyphs, the disc images and the vanilla archive's digest;
- "cars": [media, key] in the merge's input order. A car's key is the SHA-256 of its
  chunk (work/<media>.chnk) with its report's entry, host, game and spool IDs;
  "chunks" keeps each chunk's digest with its file's identity (file ID, size, mtime), so
  a hard-linked chunk (port_batch --reuse) isn't read again;
- "fingerprint": what the pack read and which code it ran, in this process, the lint
  processes and the render processes (src/dsfforza/build/fingerprint.py): the vanilla
  archive, the game's gui.sp, Fonts.fchunk and locale files, the disc images, string
  tables, and the package's code units;
- "lint": per car its key, its upvh lint result and the facts that tie the result to
  other cars (upvh.lint_vehicle: UIDs it references but doesn't define, the cars whose
  engine sound banks share an ID with its own);
- "pictures": per car the key of its garage picture (its car key, its VEHC records, its
  Forza names); the picture is pack/pictures/<name>.dds, its preview gui/<name>.png;
- "result": the manifest's cars (with their lint) and the cars the merge left out;
- "outputs": the size and mtime of every file the pack wrote, so a file changed since
  isn't taken for the pack's.

port_batch.finish, with this build's folder and its --reuse builds as the earlier ones:

1. when an earlier record has the same settings and cars, its fingerprint holds and its
   outputs are unchanged, the pack is that build's: the archive is hard-linked, gui/ and
   the pictures copied, the manifest and Lua written again (`whole`);
2. otherwise it merges (all cars, ~25 s), lints the cars whose key changed plus every
   car whose earlier result a change could alter (`lint`), and renders the garage
   pictures of the cars whose picture key changed, taking the others from the earlier
   builds (PictureCache). Fonts, badges, locale files and gui.sp are made again (they
   number every car's badges and names; seconds).

The result is the bytes a full pack makes; --verify-pack does a full pack next to it
and compares (port_batch.verify_pack).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zlib
from pathlib import Path

from dsfforza.build import fingerprint

FORMAT = 1
FOLDER = "pack"
RECORD = "pack.json"
PICTURES = "pictures"
IMAGES = "images"
MANIFEST = "dngvehicles.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(1 << 24):
            digest.update(block)
    return digest.hexdigest()


def identity(path: Path) -> list[int]:
    st = Path(path).stat()
    return [st.st_ino, st.st_size, st.st_mtime_ns]


def stat_of(path: Path) -> list[int]:
    st = Path(path).stat()
    return [st.st_size, st.st_mtime_ns]


def digest_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def car_key(result: dict, chunk_sha256: str) -> str:
    """What the merge takes from one car: its chunk and the report fields it reads."""
    return digest_json(
        {
            "chunk": chunk_sha256,
            "media": result["media"],
            "game": result.get("game"),
            "host": result.get("host"),
            "spool_id": result["spool_id"],
            "new_id": result.get("new_id", result["spool_id"]),
            "entry": result.get("entry"),
        }
    )


def settings_of(args, dsf: Path, root_limit: int, isos: list[Path]) -> dict:
    """The options that shape a pack besides its cars (port_batch build's arguments)."""
    return {
        "format": FORMAT,
        "id_policy": getattr(args, "id_policy", None),
        "root_limit": root_limit,
        "gui": bool(getattr(args, "gui", False)),
        "wide_glyphs": bool(getattr(args, "wide_glyphs", False)),
        "isos": [fingerprint.norm(p) for p in isos],
        "vanilla": fingerprint.file_digest(fingerprint.norm(dsf)),
    }


def link_or_copy(src: Path, dst: Path) -> None:
    """A hard link (the target is never written in place), else a copy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def copy_fresh(src: Path, dst: Path) -> None:
    """A copy of its own (gui/ files are written in place by the next pack)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    shutil.copy2(src, dst)


def same_folder(a: Path, b: Path) -> bool:
    return fingerprint.norm(a) == fingerprint.norm(b)


def moved_inputs(fp: dict, checker: fingerprint.Checker, game_dir: Path | None) -> dict[str, str]:
    r"""{recorded path: where the same file is now} for the files a pack read in the game
    folder `game_dir`: the game's own <game>\X before a dsfforza install, its backup
    <game>\dsfforza-backup\X after (gui_assets.vanilla_file reads the backup while an
    install is in place), and back after an uninstall. Without a game folder: none."""
    from dsfforza.game import game_paths

    if game_dir is None:
        return {}
    game = fingerprint.norm(game_dir) + os.sep
    backup = game + os.path.normcase(game_paths.BACKUP) + os.sep
    out = {}
    for path, digest in fp.get("files", {}).items():
        if path.startswith(backup):
            other = game + path[len(backup) :]
        elif path.startswith(game):
            other = backup + path[len(game) :]
        else:
            continue
        if checker.file(path) != digest and checker.file(other) == digest:
            out[path] = other
    return out


def rename_inputs(fp: dict, moved: dict[str, str]) -> dict:
    if not moved:
        return fp
    return {**fp, "files": dict(sorted((moved.get(p, p), d) for p, d in fp.get("files", {}).items()))}


class Earlier:
    """An earlier build's pack record."""

    def __init__(self, folder: Path, record: dict, moved: dict | None = None):
        self.folder, self.record = Path(folder), record
        self.moved = moved or {}  # its recorded game files that are elsewhere now (moved_inputs)

    def outputs_unchanged(self, names=None) -> bool:
        """Every recorded output (or those in `names`) has its recorded size and mtime."""
        outputs = self.record.get("outputs", {})
        for rel in outputs if names is None else names:
            if rel not in outputs:
                return False
            try:
                if stat_of(self.folder / rel) != outputs[rel]:
                    return False
            except OSError:
                return False
        return True


class PackReuse:
    """The pack's reuse of earlier builds' packs (see the module docstring)."""

    def __init__(self, out_dir: Path, sources: list[Path], settings: dict, log=print, game_dir: Path | None = None):
        """`game_dir`: the game folder the build reads its vanilla files from (moved_inputs)."""
        self.out_dir = Path(out_dir)
        self.game_dir = Path(game_dir) if game_dir is not None else None
        self.settings = settings
        self.log = log
        self.checker = fingerprint.Checker()
        self.earlier: list[Earlier] = []  # with this build's settings and a fingerprint that holds
        self.used: list[Earlier] = []  # the ones something was taken from
        self.keys: dict[str, str] = {}
        self.cars: list[list[str]] = []
        self.chunks: dict[str, dict] = {}
        self.pictures: dict[str, dict] = {}
        self.images: dict[str, str] = {}  # image key -> its file in pack/images (ImageStore)
        self.lint_entries: dict[str, dict] = {}
        self.records: list[dict] = []  # subprocesses' recordings (fingerprint.Recorder.drain)
        self.work: Path | None = None
        self.recorder = None
        self.stats = {"whole": None, "linted": None, "pictures_reused": 0, "pictures_made": 0, "earlier": []}
        seen = set()
        for folder in [self.out_dir, *[Path(s) for s in sources]]:
            key = fingerprint.norm(folder)
            if key in seen:
                continue
            seen.add(key)
            record = fingerprint.read(folder / FOLDER / RECORD)
            if record is None:
                continue
            why, moved = self._why_not(record)
            if why:
                log(f"pack reuse: not from {folder}: {why}")
                continue
            self.earlier.append(Earlier(folder, record, moved))
        self.stats["earlier"] = [str(e.folder) for e in self.earlier]

    def _why_not(self, record: dict) -> tuple[str | None, dict]:
        """(why a record can't be used, None when it can; its moved game files)."""
        if record.get("format") != FORMAT:
            return "a pack record of another format", {}
        if record.get("settings") != self.settings:
            old, new = record.get("settings") or {}, self.settings
            return "settings: " + ", ".join(k for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)), {}
        fp = record.get("fingerprint") or {}
        moved = moved_inputs(fp, self.checker, self.game_dir)
        why = self.checker.check(fp, None, moved=moved)
        return (why and f"its {why} changed"), moved

    # --- the cars ---------------------------------------------------------------------------

    def prepare(self, ok: list[dict], work: Path) -> None:
        """Each car's key (its chunk's digest: from an earlier record when the file is the
        same one, else read)."""
        self.work = Path(work)
        known = {}
        for e in reversed(self.earlier):
            known.update(e.record.get("chunks", {}))
        for r in ok:
            media = r["media"]
            path = Path(work) / f"{media}.chnk"
            try:
                ident = identity(path)
            except OSError:
                continue  # the merge reports it missing
            old = known.get(media)
            digest = old["sha256"] if old and old.get("identity") == ident else sha256_file(path)
            self.chunks[media] = {"identity": ident, "sha256": digest}
            self.keys[media] = car_key(r, digest)
        self.cars = [[r["media"], self.keys.get(r["media"])] for r in ok]

    # --- 1. the whole pack ------------------------------------------------------------------------

    def whole(self) -> Earlier | None:
        """The earlier build whose pack this one would be, byte for byte."""
        for e in self.earlier:
            r = e.record
            if r.get("cars") != self.cars or r.get("gui_error") or "merge_failed" not in r:
                continue
            if not e.outputs_unchanged():
                self.log(f"pack reuse: {e.folder}'s archive or gui/ changed since its pack")
                continue
            return e
        return None

    def take_whole(self, e: Earlier) -> tuple[list[dict], list[dict]]:
        """This build's outputs from `e` (the archive linked, gui/ copied; the manifest and
        the Lua are written again); returns (the manifest's cars, the cars the merge left
        out)."""
        cars = json.loads((e.folder / MANIFEST).read_text(encoding="utf-8"))["cars"]
        if not same_folder(e.folder, self.out_dir):
            for rel in e.record["outputs"]:
                src, dst = e.folder / rel, self.out_dir / rel
                if rel == MANIFEST:
                    continue
                if rel.startswith("gui/"):
                    copy_fresh(src, dst)
                else:
                    link_or_copy(src, dst)
        self.used.append(e)
        self.pictures = dict(e.record.get("pictures", {}))
        self.images = dict(e.record.get("images", {}))
        self.lint_entries = dict(e.record.get("lint", {}))
        self.stats["whole"] = str(e.folder)
        return cars, e.record["merge_failed"]

    # --- 2a. lint -----------------------------------------------------------------------------------

    def lint_base(self, cars: list[dict]) -> Earlier | None:
        """The earlier build with the most cars whose lint can be kept."""
        best, best_n = None, 0
        for e in self.earlier:
            lint = e.record.get("lint", {})
            n = sum(1 for c in cars if (lint.get(c["media"]) or {}).get("key") == self.keys.get(c["media"]))
            if n > best_n:
                best, best_n = e, n
        return best

    def lint(self, cars: list[dict], run) -> None:
        """c["lint"] for every car: upvh lint for the cars whose key changed and for the
        ones a change could reach, the earlier result for the rest. run(cars, defined)
        lints `cars` and returns {media: facts}; `defined`: the spool IDs whose defined
        UIDs it must report too.

        An unchanged car's earlier "ok" holds unless another car's change touches it: a
        changed, added or removed car whose sound bank shared an ID with it (its "near",
        before or after), or a changed or added car that now defines a UID it references
        but doesn't define. A failed result is always linted again."""
        base = self.lint_base(cars)
        old = base.record.get("lint", {}) if base is not None else {}
        keep = {
            c["media"]
            for c in cars
            if (old.get(c["media"]) or {}).get("key") == self.keys.get(c["media"])
            and old[c["media"]].get("text") == "ok"
            and "refs_ext" in old[c["media"]]
        }
        changed = [c for c in cars if c["media"] not in keep]
        gone = [m for m in old if m not in keep]  # the earlier versions of changed and removed cars
        if any("near" not in old[m] for m in gone):
            keep = set()  # an earlier result without facts: can't tell what it touched
            changed = list(cars)
        near_before = {sid for m in gone for sid in old[m].get("near", [])}
        first = changed + [c for c in cars if c["media"] in keep and c["spool_id"] in near_before]
        # The UIDs the changed cars define matter only to the cars kept.
        facts = run(first, {c["spool_id"] for c in changed} if keep else set()) if first else {}
        defined = {u for c in changed for u in (facts.get(c["media"]) or {}).get("defined", [])}
        near_after = {sid for c in changed for sid in (facts.get(c["media"]) or {}).get("near", [])}
        done = {c["media"] for c in first}
        second = [
            c
            for c in cars
            if c["media"] in keep
            and c["media"] not in done
            and (c["spool_id"] in near_after or defined & set(old[c["media"]].get("refs_ext", [])))
        ]
        if second:
            facts.update(run(second, set()))
            done |= {c["media"] for c in second}
        for c in cars:
            media = c["media"]
            if media in done:
                f = facts.get(media) or {}
                entry = {"key": self.keys.get(media), "text": c["lint"]}
                if "refs_ext" in f and "near" in f:
                    entry.update(refs_ext=f["refs_ext"], near=f["near"])
            else:
                entry = old[media]
                c["lint"] = entry["text"]
            self.lint_entries[media] = entry
        if base is not None and len(done) < len(cars):
            self.used.append(base)
        self.stats["linted"] = len(done)

    # --- 2b. garage pictures ------------------------------------------------------------------------

    def picture_cache(self) -> PictureCache:
        return PictureCache(self)

    def image_outputs(self) -> list[str]:
        return [f"{FOLDER}/{IMAGES}/{name}" for name in sorted(set(self.images.values()))]

    # --- the record ------------------------------------------------------------------------------------

    def start_recording(self) -> None:
        self.recorder = fingerprint.Recorder().start()

    def skip_prefixes(self) -> tuple[str, ...]:
        """Not inputs: this build's folder (its outputs), the cars' chunks (their keys cover
        them) and the earlier builds' folders (cache files)."""
        folders = [self.out_dir, *([self.work] if self.work else []), *[e.folder for e in self.earlier]]
        return tuple(fingerprint.norm(p) + os.sep for p in folders)

    def save(self, merge_failed: list[dict], outputs: list[str], gui_error: str | None = None) -> None:
        """Write this build's pack record. `outputs`: paths relative to the build folder
        (the manifest's stat is kept: a whole pack takes its cars from it)."""
        if self.recorder is not None:
            fp = self.recorder.finish({}, self.records, skip=self.skip_prefixes())
            self.recorder = None
        else:
            fp = None
        for e in self.used:
            old = rename_inputs(e.record.get("fingerprint") or {}, e.moved)
            fp = old if fp is None else fingerprint.combine(fp, old)
        record = {
            "format": FORMAT,
            "settings": self.settings,
            "cars": self.cars,
            "chunks": self.chunks,
            "fingerprint": fp or {},
            "lint": self.lint_entries,
            "pictures": self.pictures,
            "images": self.images,
            "merge_failed": merge_failed,
            "outputs": {
                rel: stat_of(self.out_dir / rel) for rel in sorted(set(outputs)) if (self.out_dir / rel).is_file()
            },
        }
        if gui_error:
            record["gui_error"] = gui_error
        path = self.out_dir / FOLDER / RECORD
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(RECORD + ".tmp")
        temp.write_text(json.dumps(record, default=str) + "\n", encoding="utf-8")
        temp.replace(path)

    def forget(self) -> None:
        """No record (the pack failed or is not reusable): a later build makes a full pack."""
        (self.out_dir / FOLDER / RECORD).unlink(missing_ok=True)


class PictureCache:
    """gui_assets.build's garage pictures from the earlier builds: a car's picture is
    the same when its picture key (its car key, its VEHC records and gui_assets'
    `extra`: its Forza names, thumbnails or renders) is."""

    def __init__(self, pack: PackReuse):
        self.pack = pack
        self.folder = pack.out_dir / FOLDER / PICTURES

    def key(self, car: dict, extra: dict) -> str | None:
        car_key_ = self.pack.keys.get(car["media"])
        if car_key_ is None:
            return None
        return digest_json({"car": car_key_, "spool_id": car["spool_id"], "vehc": car.get("vehc"), "extra": extra})

    def get(self, car: dict, extra: dict) -> tuple[bytes, bytes, str] | None:
        """(DDS, preview PNG, source) of the car's picture, or None."""
        key = self.key(car, extra)
        if key is None:
            return None
        name = f"dsfforza_{car['media'].lower()}"
        dds_rel, png_rel = f"{FOLDER}/{PICTURES}/{name}.dds", f"gui/{name}.png"
        for e in self.pack.earlier:
            entry = e.record.get("pictures", {}).get(car["media"])
            if not entry or entry.get("key") != key or not e.outputs_unchanged([dds_rel, png_rel]):
                continue
            try:
                dds, png = (e.folder / dds_rel).read_bytes(), (e.folder / png_rel).read_bytes()
            except OSError:
                continue
            if not same_folder(e.folder, self.pack.out_dir):
                link_or_copy(e.folder / dds_rel, self.pack.out_dir / dds_rel)
            if e not in self.pack.used:
                self.pack.used.append(e)
            self.pack.pictures[car["media"]] = entry
            self.pack.stats["pictures_reused"] += 1
            return dds, png, entry.get("source", "render")
        return None

    def put(self, car: dict, extra: dict, dds: bytes, png: bytes, source: str) -> None:
        key = self.key(car, extra)
        if key is None:
            return
        name = f"dsfforza_{car['media'].lower()}"
        path = self.folder / f"{name}.dds"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)  # maybe a hard link to an earlier build's
        path.write_bytes(dds)
        self.pack.pictures[car["media"]] = {"key": key, "source": source}
        self.pack.stats["pictures_made"] += 1

    # --- gui_assets' images (nameplates, logos) ---------------------------------------------------

    def images(self) -> dict[str, object]:
        """{image key: gui_assets.Image or None} the earlier builds made: gui_assets keys its
        chrome nameplates by text and size, the Forza logos by maker."""
        from dsfforza.game.gui_assets import Image

        out: dict[str, object] = {}
        for e in self.pack.earlier:
            for key, name in e.record.get("images", {}).items():
                if key in out:
                    continue
                rel = f"{FOLDER}/{IMAGES}/{name}"
                if not e.outputs_unchanged([rel]):
                    continue
                try:
                    out[key] = unpack_image(Image, (e.folder / rel).read_bytes())
                except (OSError, ValueError, zlib.error):
                    continue
                if not same_folder(e.folder, self.pack.out_dir):
                    link_or_copy(e.folder / rel, self.pack.out_dir / rel)
                self.pack.images[key] = name
                if e not in self.pack.used:
                    self.pack.used.append(e)
        self.pack.stats["images_reused"] = len(out)
        return out

    def keep_images(self, images: dict[str, object]) -> None:
        """Store the images the build made (the ones not taken from an earlier build)."""
        made = 0
        for key, image in images.items():
            if key in self.pack.images:
                continue
            name = hashlib.sha256(key.encode()).hexdigest()[:32] + ".bin"
            path = self.pack.out_dir / FOLDER / IMAGES / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
            path.write_bytes(pack_image(image))
            self.pack.images[key] = name
            made += 1
        self.pack.stats["images_made"] = made


def pack_image(image) -> bytes:
    """An Image (its size, pixels and attributes) or None, zlib-compressed."""
    if image is None:
        return zlib.compress(b"null\n", 1)
    attrs = {k: v for k, v in vars(image).items() if k not in ("w", "h", "px")}
    head = json.dumps({"w": image.w, "h": image.h, "attrs": attrs}).encode() + b"\n"
    return zlib.compress(head + bytes(image.px), 1)


def unpack_image(cls, data: bytes):
    raw = zlib.decompress(data)
    head, _, px = raw.partition(b"\n")
    info = json.loads(head)
    if info is None:
        return None
    if len(px) != info["w"] * info["h"] * 4:
        raise ValueError("image size")
    image = cls(info["w"], info["h"], px)
    for k, v in info["attrs"].items():
        setattr(image, k, tuple(v) if isinstance(v, list) else v)
    return image
