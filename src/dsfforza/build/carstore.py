"""The shared car store: every converted car, kept by its fingerprint in one folder all
worktrees use, so a build anywhere on the PC takes a car someone already converted from
the same job, inputs and code instead of converting it again (research/batch.md, "Car
store").

    <store>/cars/<media>/<id>/      a car: work/<media>.chnk, .json, .job.json, .log,
                                    .fp.json (and .first-try.*), sound/<media>/, meta.json
    <store>/pictures/<key>.dds|png|json   a garage picture by its pack_reuse picture key
    <store>/images/<name>.bin|json        a nameplate or logo (pack_reuse.PictureCache.images)
    <store>/packs/<digest>.json           the pack fingerprint the pictures and images were
                                          made under (a picture is used only while it holds)

The store is STORE_ENV, else %LOCALAPPDATA%\\dsfforza\\store. It is on in a git checkout
(developers' worktrees) and off in a player's copy, unless STORE_ENV names a folder;
"off" turns it off, and so does port_batch build --no-store. Files are hard-linked where
the volume allows (the work files are never written in place: port_batch replaces them),
the small ones copied.

A car's id is the digest of its fingerprints with the checkout's own folder written as
"<repo>": the same car built in two worktrees is one entry. Its fingerprint is checked as
port_batch --reuse checks one (fingerprint.Checker), after fingerprint.relocate moves the
paths under the checkout it was built in (out/db, each worktree's own) to this one.

The store holds at most CAP_ENV GiB (DEFAULT_CAP_GB); past it, the entries used longest
ago go first (evict), down to 90% of the cap.

    uv run dsff carstore status
    uv run dsff carstore seed <build> ...    # put an earlier build's cars in (hard links)
    uv run dsff carstore evict [--cap-gb N]
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import dsfforza
from dsfforza.build import fingerprint

STORE_ENV = "DSFFORZA_STORE"
CAP_ENV = "DSFFORZA_STORE_GB"
DEFAULT_CAP_GB = 25  # a full build's cars are about 4.4 GiB (721 cars, 2026-11-04): five or so builds
FORMAT = 1
WORK_FILES = ("chnk", "json", "job.json", "log", "fp.json", "first-try.json", "first-try.log", "first-try.fp.json")
LINK_FROM = 1 << 20  # files from 1 MiB are hard-linked, smaller ones copied
REPO_MARK = "<repo>"


def default_root() -> Path | None:
    """The store folder, or None when it is off (see the module docstring)."""
    value = os.environ.get(STORE_ENV)
    if value is not None:
        return None if value.strip().lower() in ("off", "0", "") else Path(value)
    if not (dsfforza.REPO / ".git").exists():  # a player's copy: no store unless asked for
        return None
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache")
    return Path(base) / "dsfforza" / "store"


def cap_bytes() -> int:
    try:
        gb = float(os.environ.get(CAP_ENV, DEFAULT_CAP_GB))
    except ValueError:
        gb = DEFAULT_CAP_GB
    return int(gb * (1 << 30))


def link_or_copy(src: Path, dst: Path) -> None:
    """A hard link for a large file, a copy for a small one (or when links fail)."""
    src, dst = Path(src), Path(dst)  # shutil.copytree passes strings
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    if src.stat().st_size >= LINK_FROM:
        with contextlib.suppress(OSError):
            os.link(src, dst)
    if not dst.exists():
        shutil.copy2(src, dst)


def _portable(value, repo: str):
    """`value` (a fingerprint or part) with the checkout's folder written as REPO_MARK."""
    text = json.dumps(value, sort_keys=True)
    folder = json.dumps(repo + os.sep)[1:-1]
    return text.replace(folder, REPO_MARK + json.dumps(os.sep)[1:-1])


def entry_id(fp: dict, first: dict | None, repo: str) -> str:
    return hashlib.sha256(_portable([fp, first], repo).encode()).hexdigest()[:24]


def _tree_size(folder: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(folder):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(dirpath) / name).stat().st_size
    return total


def repo_of(folder: Path) -> str:
    """The checkout a build folder is in (the nearest folder with .git), as recorded (norm)."""
    here = Path(fingerprint.norm(folder))
    for parent in [here, *here.parents]:
        if (parent / ".git").exists():
            return fingerprint.norm(parent)
    return fingerprint.norm(dsfforza.REPO)


class Entry:
    """One car in the store."""

    def __init__(self, folder: Path, meta: dict):
        self.folder, self.meta = folder, meta

    @property
    def media(self) -> str:
        return self.meta["media"]

    def work(self, name: str) -> Path:
        return self.folder / "work" / f"{self.media}.{name}"

    def fingerprints(self) -> tuple[dict | None, dict | None]:
        """(the car's fingerprint, its first try's for a fallback build), relocated to this
        checkout."""
        repo = self.meta.get("repo")
        fp = fingerprint.read(self.work("fp.json"))
        first = fingerprint.read(self.work("first-try.fp.json")) if self.work("first-try.fp.json").exists() else None
        return (
            fingerprint.relocate(fp, repo) if fp else None,
            fingerprint.relocate(first, repo) if first else None,
        )

    def report(self) -> dict | None:
        return fingerprint.read(self.work("json"))

    def touch(self) -> None:
        with contextlib.suppress(OSError):
            (self.folder / "used").touch()

    def used(self) -> float:
        for name in ("used", "meta.json"):
            with contextlib.suppress(OSError):
                return (self.folder / name).stat().st_mtime
        return 0.0


class Store:
    def __init__(self, root: Path, cap: int | None = None, log=print):
        self.root = Path(root)
        self.cap = cap_bytes() if cap is None else cap
        self.log = log
        self._packs: dict[str, str | None] = {}  # pack fingerprint digest -> why it doesn't hold now
        self.stats = {"hits": 0, "put": 0, "pictures_hit": 0, "pictures_put": 0, "images_hit": 0, "images_put": 0}

    @classmethod
    def default(cls, log=print) -> Store | None:
        root = default_root()
        return cls(root, log=log) if root is not None else None

    # --- cars -------------------------------------------------------------------------------------

    def entries(self, media: str) -> list[Entry]:
        out = []
        folder = self.root / "cars" / media.lower()
        try:
            subs = sorted(folder.iterdir())
        except OSError:
            return []
        for sub in subs:
            meta = fingerprint.read(sub / "meta.json")
            if meta and meta.get("format") == FORMAT and meta.get("media") == media:
                out.append(Entry(sub, meta))
        return sorted(out, key=lambda e: -e.used())  # the most recently used first

    def find(self, job: dict, checker: fingerprint.Checker, *, code: bool = True) -> tuple[Entry | None, str | None]:
        """(the entry the job's car can be taken from, None) or (None, why the most recently
        used one can't: the first difference)."""
        current = {**job, "requested_options": job["port_options"]}
        first_why = None
        for e in self.entries(job["media"]):
            if e.meta.get("spool_id") != job["spool_id"] or e.meta.get("options") != list(job["port_options"]):
                first_why = first_why or "store: job: " + (
                    "spool_id" if e.meta.get("spool_id") != job["spool_id"] else "options"
                )
                continue
            fp, first = e.fingerprints()
            if fp is None:
                continue
            why = checker.check(fp, current, code=code)
            if why is None and first is not None:
                why = checker.check(first, None, code=code, moved=checker.moved(fp, current))
                why = why and f"first try: {why}"
            if why is None:
                return e, None
            first_why = first_why or f"store: {why}"
        return None, first_why or "not in the store"

    def take(self, e: Entry, work: Path, sound_dir: Path | None) -> dict | None:
        """The entry's files into a build's work/ (and sound/); its report, or None when
        the entry went meanwhile (evicted by another build)."""
        media = e.media
        try:
            for name in WORK_FILES:
                src = e.work(name)
                if src.exists():
                    link_or_copy(src, work / f"{media}.{name}")
            sound = e.folder / "sound" / media
            if sound_dir is not None and sound.is_dir():
                dst = Path(sound_dir) / media
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(sound, dst, copy_function=link_or_copy)
            report = json.loads((work / f"{media}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            for name in WORK_FILES:
                (work / f"{media}.{name}").unlink(missing_ok=True)
            return None
        e.touch()
        self.stats["hits"] += 1
        return report

    def put(self, work: Path, media: str, sound_dir: Path | None, source: str = "") -> Entry | None:
        """Keep a built car (its work files and sound folder) in the store, unless its
        fingerprint is incomplete or the same car is there already (then it is marked used)."""
        report = fingerprint.read(work / f"{media}.json")
        fp = fingerprint.read(work / f"{media}.fp.json")
        if not (report and report.get("ok") and fp and fp.get("complete")) or not (work / f"{media}.chnk").exists():
            return None
        first_path = work / f"{media}.first-try.fp.json"
        first = fingerprint.read(first_path) if first_path.exists() else None
        if first_path.exists() and not (first and first.get("complete")):
            return None
        repo = fp.get("repo") or repo_of(work)
        key = entry_id(fp, first, repo)
        final = self.root / "cars" / media.lower() / key
        if (final / "meta.json").exists():
            Entry(final, {}).touch()
            return Entry(final, fingerprint.read(final / "meta.json") or {})
        temp = self.root / "tmp" / f"{key}-{os.getpid()}"
        try:
            shutil.rmtree(temp, ignore_errors=True)
            for name in WORK_FILES:
                if (work / f"{media}.{name}").exists():
                    link_or_copy(work / f"{media}.{name}", temp / "work" / f"{media}.{name}")
            sound = Path(sound_dir) / media if sound_dir is not None else None
            if sound is not None and sound.is_dir():
                shutil.copytree(sound, temp / "sound" / media, copy_function=link_or_copy)
            job = fp.get("job") or {}
            meta = {
                "format": FORMAT,
                "media": media,
                "game": report.get("game"),
                "spool_id": job.get("spool_id"),
                "options": job.get("options"),
                "repo": repo,
                "source": source or fingerprint.norm(work.parent),
                "stored": round(time.time()),
                "size": _tree_size(temp),
            }
            (temp / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                temp.rename(final)
            except OSError:  # another build put the same car meanwhile
                shutil.rmtree(temp, ignore_errors=True)
                return Entry(final, fingerprint.read(final / "meta.json") or {})
        except OSError as error:
            self.log(f"car store: {media} not kept: {error}")
            shutil.rmtree(temp, ignore_errors=True)
            return None
        self.stats["put"] += 1
        return Entry(final, meta)

    # --- pack pictures and images ------------------------------------------------------------------

    def put_pack(self, fp: dict) -> str:
        """Keep a pack fingerprint; its digest."""
        repo = fp.get("repo") or fingerprint.norm(dsfforza.REPO)
        fp = {**fp, "repo": repo}
        digest = hashlib.sha256(_portable(fp, repo).encode()).hexdigest()[:24]
        path = self.root / "packs" / f"{digest}.json"
        if not path.exists():
            _write(path, json.dumps(fp).encode())
        return digest

    def pack_holds(self, digest: str, checker: fingerprint.Checker, moved=None) -> dict | None:
        """The pack fingerprint `digest` (relocated) when it holds now, else None."""
        fp = fingerprint.read(self.root / "packs" / f"{digest}.json")
        if fp is None:
            return None
        fp = fingerprint.relocate(fp, fp.get("repo"))
        if digest not in self._packs:
            extra = moved(fp) if moved is not None else {}
            self._packs[digest] = checker.check(fp, None, moved=extra)
        return fp if self._packs[digest] is None else None

    def picture(self, key: str, checker, moved=None) -> tuple[Path, Path, str, dict] | None:
        """(DDS, preview PNG, its source, the pack fingerprint it was made under) of a
        stored garage picture whose pack fingerprint holds, or None. A picture is kept once
        per pack fingerprint (pictures/<key>-<pack>.*): the same picture made by other code
        is another copy."""
        try:
            infos = sorted((self.root / "pictures").glob(f"{key}-*.json"), key=_mtime, reverse=True)
        except OSError:
            return None
        for path in infos:
            info = fingerprint.read(path)
            if not info:
                continue
            fp = self.pack_holds(info.get("pack", ""), checker, moved)
            dds, png = path.with_suffix(".dds"), path.with_suffix(".png")
            if fp is None or not dds.exists() or not png.exists():
                continue
            with contextlib.suppress(OSError):
                path.touch()
            self.stats["pictures_hit"] += 1
            return dds, png, info.get("source", "render"), fp
        return None

    def put_picture(self, key: str, dds: Path, png: Path, source: str, pack: str) -> None:
        base = self.root / "pictures" / f"{key}-{pack}"
        if base.with_suffix(".json").exists():
            return
        try:
            link_or_copy(dds, base.with_suffix(".dds"))
            link_or_copy(png, base.with_suffix(".png"))
            _write(base.with_suffix(".json"), json.dumps({"key": key, "source": source, "pack": pack}).encode())
            self.stats["pictures_put"] += 1
        except OSError as error:
            self.log(f"car store: picture {key} not kept: {error}")

    def images(self, checker, moved=None) -> dict[str, tuple[Path, dict]]:
        """{image key: (its file, the pack fingerprint)} of the stored images whose pack
        fingerprint holds (images/<name>-<pack>.*, as pictures)."""
        out = {}
        try:
            infos = sorted((self.root / "images").glob("*.json"), key=_mtime, reverse=True)
        except OSError:
            return {}
        for path in infos:
            info = fingerprint.read(path)
            if not info or info.get("key") in out:
                continue
            fp = self.pack_holds(info.get("pack", ""), checker, moved)
            if fp is not None and path.with_suffix(".bin").exists():
                out[info["key"]] = (path.with_suffix(".bin"), fp)
                with contextlib.suppress(OSError):
                    path.touch()
        return out

    def put_image(self, key: str, file: Path, pack: str) -> None:
        base = self.root / "images" / f"{image_name(key)}-{pack}"
        if base.with_suffix(".json").exists():
            return
        try:
            link_or_copy(file, base.with_suffix(".bin"))
            _write(base.with_suffix(".json"), json.dumps({"key": key, "pack": pack}).encode())
            self.stats["images_put"] += 1
        except OSError as error:
            self.log(f"car store: image not kept: {error}")

    # --- size ------------------------------------------------------------------------------------

    def items(self) -> list[tuple[float, int, Path]]:
        """(last used, bytes, folder or file stem) of every car entry, picture and image."""
        out = []
        for media in _dirs(self.root / "cars"):
            for sub in _dirs(media):
                meta = fingerprint.read(sub / "meta.json") or {}
                size = meta.get("size") if isinstance(meta.get("size"), int) else _tree_size(sub)
                out.append((Entry(sub, meta).used(), size, sub))
        for folder, suffixes in (("pictures", (".dds", ".png", ".json")), ("images", (".bin", ".json"))):
            with contextlib.suppress(OSError):
                for info in (self.root / folder).glob("*.json"):
                    size = sum(_size(info.with_suffix(x)) for x in suffixes)
                    out.append((_mtime(info), size, info.with_suffix("")))
        return out

    def evict(self, cap: int | None = None) -> tuple[int, int]:
        """Remove the entries used longest ago while the store is over `cap` (default
        self.cap), down to 90% of it; (entries removed, bytes freed)."""
        cap = self.cap if cap is None else cap
        items = self.items()
        total = sum(s for _, s, _ in items)
        if total <= cap:
            return 0, 0
        removed, freed = 0, 0
        for _, size, path in sorted(items, key=lambda i: i[0]):
            if total - freed <= cap * 0.9:
                break
            if path.is_dir():
                trash = self.root / "tmp" / f"evicted-{path.name}-{os.getpid()}"
                try:
                    path.rename(trash)
                except OSError:
                    continue
                shutil.rmtree(trash, ignore_errors=True)
            else:
                for suffix in (".json", ".dds", ".png", ".bin"):
                    path.with_suffix(suffix).unlink(missing_ok=True)
            removed += 1
            freed += size
        # The pack fingerprints no picture or image names any more.
        used = {p.stem.rsplit("-", 1)[-1] for f in ("pictures", "images") for p in (self.root / f).glob("*.json")}
        for pack in (self.root / "packs").glob("*.json"):
            if pack.stem not in used:
                pack.unlink(missing_ok=True)
        self.log(
            f"car store: {removed} entries removed ({freed / (1 << 30):.1f} GiB), over the cap of {cap / (1 << 30):.0f} GiB"
        )
        return removed, freed


def image_name(key: str) -> str:
    """An image's file name in a pack (pack_reuse.PictureCache.keep_images), without .bin."""
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _dirs(folder: Path) -> list[Path]:
    try:
        return [p for p in folder.iterdir() if p.is_dir()]
    except OSError:
        return []


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(data)
    temp.replace(path)


def seed(store: Store, build: Path) -> int:
    """Put every car of an earlier build (its work/ and sound/) in the store; the count."""
    work = Path(build) / "work"
    n = 0
    for report in sorted(work.glob("*.json")):
        media = report.name[: -len(".json")]
        if "." in media:  # <media>.job.json, .fp.json, .first-try.json
            continue
        if store.put(work, media, Path(build) / "sound", source=fingerprint.norm(build)) is not None:
            n += 1
    seed_pack(store, Path(build))
    return n


def seed_pack(store: Store, build: Path) -> None:
    """The garage pictures and images of a build's pack record (pack_reuse), under its
    fingerprint, when the record is complete."""
    from dsfforza.build import pack_reuse

    record = fingerprint.read(build / pack_reuse.FOLDER / pack_reuse.RECORD)
    fp = (record or {}).get("fingerprint") or {}
    if not record or record.get("gui_error") or not fp.get("complete"):
        return
    digest = store.put_pack({**fp, "repo": fp.get("repo") or repo_of(build)})
    for media, entry in record.get("pictures", {}).items():
        name = f"dsfforza_{media.lower()}"
        dds = build / pack_reuse.FOLDER / pack_reuse.PICTURES / f"{name}.dds"
        png = build / "gui" / f"{name}.png"
        if entry.get("key") and dds.is_file() and png.is_file():
            store.put_picture(entry["key"], dds, png, entry.get("source", "render"), digest)
    for key, name in record.get("images", {}).items():
        path = build / pack_reuse.FOLDER / pack_reuse.IMAGES / name
        if path.is_file():
            store.put_image(key, path, digest)


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="dsff carstore", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="where the store is, how many cars and how big")
    s = sub.add_parser("seed", help="put an earlier build's cars in the store (hard links)")
    s.add_argument("builds", type=Path, nargs="+")
    e = sub.add_parser("evict", help="remove the entries used longest ago past the cap")
    e.add_argument("--cap-gb", type=float)
    args = parser.parse_args(argv)
    store = Store.default()
    if store is None:
        sys.exit(f"car store off ({STORE_ENV})")
    if args.cmd == "seed":
        for build in args.builds:
            print(f"{build}: {seed(store, build)} cars in the store")
        store.evict()
    elif args.cmd == "evict":
        store.evict(int(args.cap_gb * (1 << 30)) if args.cap_gb is not None else None)
    items = store.items()
    cars = sum(1 for _, _, p in items if p.is_dir())
    total = sum(s for _, s, _ in items)
    print(
        f"{store.root}: {cars} cars, {len(items) - cars} pictures, {total / (1 << 30):.2f} GiB of {store.cap / (1 << 30):.0f}"
    )


if __name__ == "__main__":
    main()
