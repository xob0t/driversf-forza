"""The shared car store: converted cars kept in one folder all worktrees use, by the reuse
rule (src/dsfforza/build/builder_version.py: the builder version, the media and options,
the build ID and the car's own source), so a build anywhere on the PC at a commit someone
already built takes those cars instead of converting them again (research/batch.md, "Car
store").

    <store>/cars/<media>/<key>/      a car: work/<media>.chnk, .json, .job.json, .log
                                     (and .first-try.*), sound/<media>/, meta.json
    <store>/pictures/<key>-<version>.dds|png|json   a garage picture by its pack_reuse
                                                    picture key and builder version
    <store>/images/<name>-<version>.bin|json        a nameplate or logo (pack_reuse.PictureCache.images)

A car's <key> is the digest of builder_version.car_key of its job. Only cars built under a
reusable version (a commit with no uncommitted converter changes) are kept or taken.

The store is STORE_ENV, else %LOCALAPPDATA%\\dsfforza\\store. It is on in a git checkout
(developers' worktrees) and off in a player's copy, unless STORE_ENV names a folder;
"off" turns it off, and so does port_batch build --no-store. Files are hard-linked where
the volume allows (the work files are never written in place: port_batch replaces them),
the small ones copied.

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
from dsfforza.build import builder_version

STORE_ENV = "DSFFORZA_STORE"
CAP_ENV = "DSFFORZA_STORE_GB"
DEFAULT_CAP_GB = 25  # a full build's cars are about 4.4 GiB (721 cars, 2026-11-04): five or so builds
FORMAT = 2  # 2: keyed by the builder version (#634); format-1 entries (fingerprints) are not read
WORK_FILES = ("chnk", "json", "job.json", "log", "first-try.json", "first-try.log")
LINK_FROM = 1 << 20  # files from 1 MiB are hard-linked, smaller ones copied


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


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def version_tag(version: str) -> str:
    """A builder version as a file name part."""
    return hashlib.sha256(version.encode()).hexdigest()[:16]


def _tree_size(folder: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(folder):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(dirpath) / name).stat().st_size
    return total


class Entry:
    """One car in the store."""

    def __init__(self, folder: Path, meta: dict):
        self.folder, self.meta = folder, meta

    @property
    def media(self) -> str:
        return self.meta["media"]

    def work(self, name: str) -> Path:
        return self.folder / "work" / f"{self.media}.{name}"

    def report(self) -> dict | None:
        return read_json(self.work("json"))

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
        self.stats = {"hits": 0, "put": 0, "pictures_hit": 0, "pictures_put": 0, "images_hit": 0, "images_put": 0}

    @classmethod
    def default(cls, log=print) -> Store | None:
        root = default_root()
        return cls(root, log=log) if root is not None else None

    # --- cars -------------------------------------------------------------------------------------

    def folder(self, job: dict) -> Path:
        """Where the car of `job` is kept: by the digest of its reuse key."""
        return self.root / "cars" / job["media"].lower() / builder_version.digest(builder_version.car_key(job))

    def find(self, job: dict) -> tuple[Entry | None, str | None]:
        """(the entry the job's car can be taken from, None) or (None, why not)."""
        if not builder_version.reusable(job.get("builder")):
            return None, builder_version.why_not(None, job)
        folder = self.folder(job)
        meta = read_json(folder / "meta.json")
        if not meta or meta.get("format") != FORMAT or meta.get("media") != job["media"]:
            return None, "not in the store"
        e = Entry(folder, meta)
        why = builder_version.why_not(read_json(e.work("job.json")), job)
        return (e, None) if why is None else (None, f"store: {why}")

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
        """Keep a built car (its work files and sound folder) in the store, unless its job's
        builder version isn't reusable or isn't this checkout's now (the code changed while
        it was built), or the same car is there already (then it is marked used)."""
        report = read_json(work / f"{media}.json")
        job = read_json(work / f"{media}.job.json")
        if not (report and report.get("ok") and job) or not (work / f"{media}.chnk").exists():
            return None
        if not builder_version.reusable(job.get("builder")) or job.get("builder") != builder_version.read():
            return None
        final = self.folder(job)
        if (final / "meta.json").exists():
            Entry(final, {}).touch()
            return Entry(final, read_json(final / "meta.json") or {})
        temp = self.root / "tmp" / f"{final.name}-{os.getpid()}"
        try:
            shutil.rmtree(temp, ignore_errors=True)
            for name in WORK_FILES:
                if (work / f"{media}.{name}").exists():
                    link_or_copy(work / f"{media}.{name}", temp / "work" / f"{media}.{name}")
            sound = Path(sound_dir) / media if sound_dir is not None else None
            if sound is not None and sound.is_dir():
                shutil.copytree(sound, temp / "sound" / media, copy_function=link_or_copy)
            meta = {
                "format": FORMAT,
                "media": media,
                "game": report.get("game"),
                "spool_id": job.get("spool_id"),
                "builder": job.get("builder"),
                "source": source or os.path.normcase(os.path.realpath(work.parent)),
                "stored": round(time.time()),
                "size": _tree_size(temp),
            }
            (temp / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                temp.rename(final)
            except OSError:  # another build put the same car meanwhile
                shutil.rmtree(temp, ignore_errors=True)
                return Entry(final, read_json(final / "meta.json") or {})
        except OSError as error:
            self.log(f"car store: {media} not kept: {error}")
            shutil.rmtree(temp, ignore_errors=True)
            return None
        self.stats["put"] += 1
        return Entry(final, meta)

    # --- pack pictures and images ------------------------------------------------------------------

    def picture(self, key: str, version: str | None) -> tuple[Path, Path, str] | None:
        """(DDS, preview PNG, its source) of a garage picture kept under `key` by a pack of
        builder version `version`, or None."""
        if not builder_version.reusable(version):
            return None
        base = self.root / "pictures" / f"{key}-{version_tag(version)}"
        info = read_json(base.with_suffix(".json"))
        dds, png = base.with_suffix(".dds"), base.with_suffix(".png")
        if not info or not dds.exists() or not png.exists():
            return None
        with contextlib.suppress(OSError):
            base.with_suffix(".json").touch()
        self.stats["pictures_hit"] += 1
        return dds, png, info.get("source", "render")

    def put_picture(self, key: str, dds: Path, png: Path, source: str, version: str | None) -> None:
        if not builder_version.reusable(version):
            return
        base = self.root / "pictures" / f"{key}-{version_tag(version)}"
        if base.with_suffix(".json").exists():
            return
        try:
            link_or_copy(dds, base.with_suffix(".dds"))
            link_or_copy(png, base.with_suffix(".png"))
            _write(base.with_suffix(".json"), json.dumps({"key": key, "source": source, "builder": version}).encode())
            self.stats["pictures_put"] += 1
        except OSError as error:
            self.log(f"car store: picture {key} not kept: {error}")

    def images(self, version: str | None) -> dict[str, Path]:
        """{image key: its file} of the images kept by a pack of builder version `version`."""
        if not builder_version.reusable(version):
            return {}
        out = {}
        try:
            infos = sorted((self.root / "images").glob(f"*-{version_tag(version)}.json"))
        except OSError:
            return {}
        for path in infos:
            info = read_json(path)
            if not info or info.get("key") in out or not path.with_suffix(".bin").exists():
                continue
            out[info["key"]] = path.with_suffix(".bin")
            with contextlib.suppress(OSError):
                path.touch()
        return out

    def put_image(self, key: str, file: Path, version: str | None) -> None:
        if not builder_version.reusable(version):
            return
        base = self.root / "images" / f"{image_name(key)}-{version_tag(version)}"
        if base.with_suffix(".json").exists():
            return
        try:
            link_or_copy(file, base.with_suffix(".bin"))
            _write(base.with_suffix(".json"), json.dumps({"key": key, "builder": version}).encode())
            self.stats["images_put"] += 1
        except OSError as error:
            self.log(f"car store: image not kept: {error}")

    # --- size ------------------------------------------------------------------------------------

    def items(self) -> list[tuple[float, int, Path]]:
        """(last used, bytes, folder or file stem) of every car entry, picture and image."""
        out = []
        for media in _dirs(self.root / "cars"):
            for sub in _dirs(media):
                meta = read_json(sub / "meta.json") or {}
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
    """Put every car of an earlier build (its work/ and sound/) in the store, those built
    under this checkout's builder version; the count."""
    work = Path(build) / "work"
    n = 0
    for report in sorted(work.glob("*.json")):
        media = report.name[: -len(".json")]
        if "." in media:  # <media>.job.json, .first-try.json
            continue
        if store.put(work, media, Path(build) / "sound", source=os.path.normcase(os.path.realpath(build))) is not None:
            n += 1
    seed_pack(store, Path(build))
    return n


def seed_pack(store: Store, build: Path) -> None:
    """The garage pictures and images of a build's pack record (pack_reuse), when it was made
    under this checkout's builder version."""
    from dsfforza.build import pack_reuse

    record = read_json(build / pack_reuse.FOLDER / pack_reuse.RECORD)
    version = (record or {}).get("builder")
    if not record or record.get("gui_error") or version != builder_version.read():
        return
    for media, entry in record.get("pictures", {}).items():
        name = f"dsfforza_{media.lower()}"
        dds = build / pack_reuse.FOLDER / pack_reuse.PICTURES / f"{name}.dds"
        png = build / "gui" / f"{name}.png"
        if entry.get("key") and dds.is_file() and png.is_file():
            store.put_picture(entry["key"], dds, png, entry.get("source", "render"), version)
    for key, name in record.get("images", {}).items():
        path = build / pack_reuse.FOLDER / pack_reuse.IMAGES / name
        if path.is_file():
            store.put_image(key, path, version)


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
    kinds = [p.parent.name if not p.is_dir() else "cars" for _, _, p in items]
    total = sum(s for _, s, _ in items)
    counts = ", ".join(f"{kinds.count(k)} {k}" for k in ("cars", "pictures", "images"))
    print(f"{store.root}: {counts}, {total / (1 << 30):.2f} GiB of {store.cap / (1 << 30):.0f}")


if __name__ == "__main__":
    main()
