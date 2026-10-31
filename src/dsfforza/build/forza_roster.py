"""List every car in the user's Forza discs (FH1, FM4 disc 1 and 2), deduplicated by media name.

Sources scanned, all read in place (nothing is extracted):

  * loose car zips on a disc: media/cars/<NAME>.zip (FH1, FM4 disc 1);
  * FM4 disc 2 car packs: STFS (PIRS) packages under content/, each holding
    Media/DLCZips/000N_pri_65.zip, whose media/cars/<NAME>/ folders are cars;
  * FM4 DLC packs (downloaded LIVE packages, files anywhere under assets/), laid out like the
    disc 2 packs. Only when asked for (`roster(..., packages=)`, --dlc): until the car manager
    (#170) installs cars beside the one vehicle archive, the default roster stays the discs'.

Each image is identified by its content, never by its file name (`identify`): default.xex's
title ID (4D530910 = FM4, 4D5309C9 = FH1) and disc number, cached per process by path, size
and modification time. An image that isn't a Forza disc is skipped with a note. `game_discs`
and `discs_of` give the other tools "the FH1 disc", "FM4 disc 1" or "the FM4 discs" out of
the build's images (assets/*.iso or --iso, any names); a game without its disc is simply
absent, and its cars are skipped. A
zip or folder counts as a car if it holds a main carbin (<name>.carbin), and
as an Autovista model if it only has <name>_s_lod0.carbin. Everything else
(Shared.zip, Driver.zip, AppearancePresets.zip) is listed with --all. The
model year comes from each game's Media/db/gamedb.slt (SQLite, Data_Car), a DLC car's from its
own pack's gamedb patch (media/db/patch/<N>_merge.slt, `pack_info`).

A DLC package is identified by its STFS header, never by its name (`identify_package`): magic
LIVE/CON/PIRS, title ID 4D530910 (FM4) and content type 2 (marketplace content). Where a car
is in several sources, `choose_source` picks one by its files (research/forza-sources.md,
"FM4 DLC packs").

    uv run dsff forza_roster [iso ...] [--tsv out.tsv] [--all] [--dlc [DIR]]

With no ISOs given, every *.iso in assets/ is used.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import os
import sqlite3
import struct
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza import REPO
from dsfforza.formats import forza_strings
from dsfforza.formats.forzazip import ForzaZip
from dsfforza.formats.stfs import MAGICS, Stfs, StfsError, in_image
from dsfforza.formats.xiso import XIso

TITLES = {0x4D530910: "FM4", 0x4D5309C9: "FH1"}
XEX_EXECUTION_INFO = 0x00040006
# DLC packages read as car sources: FM4's (the 16 surveyed packs, research/forza-sources.md,
# "FM4 DLC packs"). FH1's packs aren't surveyed.
DLC_TITLES = {0x4D530910: "FM4"}
MARKETPLACE_CONTENT = 0x00000002  # STFS header 0x344 of a downloaded add-on (all 16 FM4 packs)


@dataclass
class Source:
    game: str
    where: str  # e.g. "FM4 disc 1", "FM4 disc 2 / 1950-2000 Model Year Car Pack"
    path: str  # path of the zip or folder
    size: int  # bytes as stored (compressed)
    files: int
    kind: str  # car | autovista | placeholder | other
    iso: str = ""  # disc image the source is on; "" for a DLC package file
    # STFS package: its path on the disc (disc 2 car packs), or the package file itself when
    # iso is "" (DLC); "" for a loose car zip on a disc
    package: str = ""

    @property
    def dlc(self) -> bool:
        """A downloaded pack's car (its package is a file of its own, not on a disc)."""
        return bool(self.package) and not self.iso


def open_package(source: Source):
    """(handle to close, Stfs) of a pack car's package: on its disc image, or a DLC file."""
    if source.iso:
        image = XIso(source.iso)
        try:
            return image, in_image(image, image.find(source.package))
        except Exception:
            image.close()
            raise
    stream = Path(source.package).open("rb")  # noqa: SIM115 - the caller closes it
    try:
        return stream, Stfs(stream)
    except Exception:
        stream.close()
        raise


@dataclass
class Car:
    name: str
    sources: list[Source] = field(default_factory=list)
    year: int | None = None

    @property
    def games(self) -> str:
        return ",".join(sorted({s.game for s in self.sources}, reverse=True))

    @property
    def kind(self) -> str:
        kinds = {s.kind for s in self.sources}
        for k in ("car", "autovista", "placeholder", "other"):
            if k in kinds:
                return k
        return "?"


def xex_title(image: XIso) -> tuple[int, int, int]:
    """(title_id, disc_number, disc_count) from default.xex's execution info."""
    head = image.read(image.find("default.xex"), 0, 0x10000)
    if head[:4] != b"XEX2":
        raise ValueError("default.xex is not XEX2")
    for i in range(struct.unpack_from(">I", head, 0x14)[0]):
        key, value = struct.unpack_from(">II", head, 0x18 + i * 8)
        if key == XEX_EXECUTION_INFO:
            title = struct.unpack_from(">I", head, value + 12)[0]
            return title, head[value + 18], head[value + 19]
    raise ValueError("no execution info in default.xex")


def classify(name: str, files: list[str]) -> str:
    """Kind of a car archive from its file names (relative, any case)."""
    stem = name.lower()
    carbins = {Path(f).name.lower() for f in files if f.lower().endswith(".carbin")}
    if stem.startswith("nul_car"):
        return "placeholder"
    if f"{stem}.carbin" in carbins:
        return "car"
    if any(c.endswith("_s_lod0.carbin") for c in carbins):
        return "autovista"
    if any(
        not c.startswith("stripped_")
        and "_" in c
        and not any(t in c for t in ("_lod0", "_cockpit", "caliper", "rotor"))
        for c in carbins
    ):
        return "car"  # main carbin named differently from the zip
    return "other"


def model_years(image: XIso) -> dict[str, int]:
    for path in ("Media/db/gamedb.slt", "media/db/gamedb.slt"):
        try:
            entry = image.find(path)
        except KeyError:
            continue
        db = sqlite3.connect(":memory:")
        try:
            db.deserialize(image.read(entry))
            return {m.lower(): y for m, y in db.execute("SELECT MediaName, Year FROM Data_Car") if m}
        finally:
            db.close()
    return {}


def scan_disc_zips(image: XIso, game: str, where: str) -> list[Source]:
    sources = []
    for entry in image.files():
        parts = entry.path.split("/")
        if (
            entry.is_dir
            or len(parts) != 3
            or parts[0].lower() != "media"
            or parts[1].lower() != "cars"
            or not parts[2].lower().endswith(".zip")
        ):
            continue
        name = parts[2][:-4]
        with zipfile.ZipFile(image.open(entry)) as archive:
            files = archive.namelist()
        sources.append(Source(game, where, entry.path, entry.size, len(files), classify(name, files)))
    return sources


def scan_packages(image: XIso, game: str, where: str) -> list[Source]:
    sources = []
    for entry in image.files():
        if entry.is_dir or not entry.path.lower().startswith("content/") or entry.size < 0x1000:
            continue
        if image.read(entry, 0, 4) not in MAGICS:
            continue
        try:
            package = in_image(image, entry)
        except StfsError as error:
            print(f"warning: {entry.path}: {error}", file=sys.stderr)
            continue
        label = f"{where} / {package.display_name or entry.path.rsplit('/', 1)[-1]}"
        sources += package_sources(package, game, label, entry.path)
    return sources


def package_sources(package: Stfs, game: str, where: str, path: str) -> list[Source]:
    """The cars of a car pack (disc 2's or a DLC one): the media/cars/<NAME>/ folders of its
    Media/DLCZips/*.zip. `path`: the package's path on its disc, or the DLC file."""
    sources = []
    for inner in package.files():
        if inner.is_dir or not fnmatch.fnmatch(inner.path.lower(), "media/dlczips/*.zip"):
            continue
        with zipfile.ZipFile(package.open(inner)) as archive:
            groups: dict[str, list[zipfile.ZipInfo]] = defaultdict(list)
            for info in archive.infolist():
                p = info.filename.split("/")
                if len(p) >= 4 and p[0].lower() == "media" and p[1].lower() == "cars":
                    groups[p[2]].append(info)
        for name, infos in groups.items():
            files = ["/".join(i.filename.split("/")[3:]) for i in infos]
            sources.append(
                Source(
                    game,
                    where,
                    f"{inner.path}::media/cars/{name}/",
                    sum(i.compress_size for i in infos),
                    len(infos),
                    classify(name, files),
                    package=path,
                )
            )
    return sources


# --- DLC packages ------------------------------------------------------------------------


@dataclass(frozen=True)
class Package:
    """A downloaded content package, from its STFS header."""

    path: Path
    game: str | None = None  # "FM4"; None: not a car pack we read (error says why)
    name: str = ""  # the header's display name ("Porsche Expansion Pack")
    title_id: int = 0
    content_type: int = 0
    error: str = ""

    @property
    def where(self) -> str:
        """ "FM4 DLC / Porsche Expansion Pack", or why the package isn't read."""
        if self.game is None:
            return f"not an FM4 DLC package ({self.error})"
        return f"{self.game} DLC / {self.name or self.path.name}"


_PACKAGES: dict[str, tuple[int, int, Package | None]] = {}


def _read_package(path: Path) -> Package | None:
    with path.open("rb") as stream:
        if stream.read(4) not in MAGICS:
            return None
        stream.seek(0)
        try:
            package = Stfs(stream)
        except (StfsError, struct.error) as error:
            return Package(path, error=f"unreadable STFS package: {error}")
    game = DLC_TITLES.get(package.title_id)
    if game is None:
        error = f"title ID {package.title_id:08X}"
    elif package.content_type != MARKETPLACE_CONTENT:
        error = f"content type {package.content_type:#x}, not marketplace content"
    else:
        error = ""
    return Package(path, None if error else game, package.display_name, package.title_id, package.content_type, error)


def identify_package(path: str | Path) -> Package | None:
    """What a file is, from its STFS header, whatever it is called: None if it isn't an STFS
    package (a disc image, a text file), else a Package (game None when it belongs to another
    title or isn't marketplace content). Cached by path, size and modification time."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return None
    key = os.path.normcase(path.resolve())
    hit = _PACKAGES.get(key)
    if hit and hit[:2] == (st.st_size, st.st_mtime_ns):
        return dataclasses.replace(hit[2], path=path) if hit[2] else None
    try:
        found = _read_package(path)
    except OSError:
        return None
    _PACKAGES[key] = (st.st_size, st.st_mtime_ns, found)
    return found


def default_dlc_root() -> Path:
    """Where DLC packages are looked for: assets/, in any folders and under any file names."""
    return REPO / "assets"


def dlc_packages(root: Path | None = None) -> list[Package]:
    """Every STFS package under `root` (a folder, searched recursively, or one file; default
    assets/), by its header. Disc images (*.iso) are identify()'s. Packages that aren't FM4 DLC
    are included with game None, so a summary can say why they are skipped."""
    root = Path(root) if root is not None else default_dlc_root()
    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() != ".iso")
    else:
        files = []
    return [found for p in files if (found := identify_package(p)) is not None]


def package_summary(packages: list[Package]) -> tuple[list[str], list[str]]:
    """(the FM4 DLC packs' display names; notes: packages that aren't read, a second copy of
    a pack)."""
    found, notes, seen = [], [], {}
    for p in packages:
        if p.game is None:
            notes.append(f"{p.path.name} is {p.where}; skipped")
        elif (p.game, p.name) in seen:
            notes.append(f"{p.path.name} is {p.where} again (also {seen[p.game, p.name].name}); skipped")
        else:
            seen[p.game, p.name] = p.path
            found.append(p.name or p.path.name)
    return found, notes


def unique_packages(packages: list[Package]) -> list[Package]:
    """The FM4 DLC packages of `packages`, one per pack (the first of two copies)."""
    out, seen = [], set()
    for p in packages:
        if p.game is not None and (p.game, p.name) not in seen:
            seen.add((p.game, p.name))
            out.append(p)
    return out


def scan_dlc(package: Package) -> list[Source]:
    """The cars of one DLC package file (none for a pack without cars, e.g. the track pack)."""
    with package.path.open("rb") as stream:
        return package_sources(Stfs(stream), package.game, package.where, str(package.path))


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


PACK_STRINGS = ("Data_Car.str", "List_CarMake.str", "List_FamilyModel.str")


def pack_info(package: Package, disc_db: Path | None = None, disc_strings=None) -> dict[str, dict]:
    """{media lower: {display, model_short, year, make, family}} of a pack's cars, from the pack
    itself: Data_Car of its media/db/patch/*_merge.slt (SQLite, the rows the pack adds to
    gamedb.slt, in a Media/DLCZips/*.zip) and the EN/ tables of its StringTables_pri_*.zip.
    A maker or family whose row the pack doesn't carry (Porsche's is in the Porsche pack,
    Alfa Romeo's only on the disc) comes from `disc_db` (FM4's gamedb.slt) and its text from
    `disc_strings` (forza_strings.Strings of FM4's disc), when given."""
    dbs, strings = [], []
    try:
        with package.path.open("rb") as stream:
            pkg = Stfs(stream)
            for inner in pkg.files():
                low = inner.path.lower()
                if inner.is_dir or not fnmatch.fnmatch(low, "media/dlczips/*.zip"):
                    continue
                archive = ForzaZip(pkg.open(inner))
                entries = {i.filename.replace("\\", "/").lower(): i for i in archive.entries()}
                if fnmatch.fnmatch(low, "media/dlczips/stringtables_*.zip"):
                    tables = {
                        n: archive.read(entries[f"en/{n.lower()}"])
                        for n in PACK_STRINGS
                        if f"en/{n.lower()}" in entries
                    }
                    if tables:
                        strings.append(forza_strings.Strings.of(tables))
                    continue
                for name, entry in sorted(entries.items()):
                    if fnmatch.fnmatch(name, "media/db/patch/*_merge.slt"):
                        db = sqlite3.connect(":memory:")
                        db.deserialize(archive.read(entry))
                        dbs.append(db)
        media = [
            m for db in dbs if _has_table(db, "Data_Car") for (m,) in db.execute("SELECT MediaName FROM Data_Car") if m
        ]
        if disc_db is not None and Path(disc_db).is_file():
            dbs.append(sqlite3.connect(f"file:{Path(disc_db).as_posix()}?mode=ro", uri=True))
        if disc_strings is not None:
            strings.append(disc_strings)
        names = forza_strings.names_from(dbs, strings, media)
    finally:
        for db in dbs:
            db.close()
    return {m.lower(): v for m, v in names.items()}


def roster(isos: list[Path], packages: list[Package] | None = None) -> tuple[dict[str, Car], list[str]]:
    """{media lower: Car} of the disc images, plus the cars of the DLC `packages` when given
    (dlc_packages(); none by default: the default roster is the discs')."""
    cars: dict[str, Car] = {}
    notes = []
    years: dict[str, dict[str, int]] = {}
    dlc_years: dict[str, int] = {}
    for disc in identify_all(isos):
        iso = disc.path
        if disc.game is None:
            notes.append(f"{iso.name}: {disc.where}; skipped")
            continue
        image = XIso(iso)
        try:
            game, where = disc.game, disc.where
            found = scan_disc_zips(image, game, where) + scan_packages(image, game, where)
            for source in found:
                source.iso = str(iso)
            years.setdefault(game, {}).update(model_years(image))
            notes.append(
                f"{iso.name}: {where}, {sum(s.kind == 'car' for s in found)} cars, "
                f"{sum(s.kind == 'autovista' for s in found)} Autovista models, "
                f"{sum(s.kind not in ('car', 'autovista') for s in found)} other"
            )
        finally:
            image.close()
        for source in found:
            name = source.path.rstrip("/").rsplit("/", 1)[-1]
            name = name[:-4] if name.lower().endswith(".zip") else name
            car = cars.setdefault(name.lower(), Car(name))
            car.sources.append(source)
    for package in unique_packages(packages or []):
        found = scan_dlc(package)
        dlc_years.update({k: v["year"] for k, v in pack_info(package).items() if v.get("year")})
        notes.append(
            f"{package.path.name}: {package.where}, {sum(s.kind == 'car' for s in found)} cars, "
            f"{sum(s.kind == 'autovista' for s in found)} Autovista models"
        )
        for source in found:
            name = source.path.rstrip("/").rsplit("/", 1)[-1]
            cars.setdefault(name.lower(), Car(name)).sources.append(source)
    for key, car in cars.items():
        base = key[:-5] if key.endswith("_slod") else key
        for game in ("FM4", "FH1"):
            if car.year is None and base in years.get(game, {}):
                car.year = years[game][base]
        if car.year is None:  # a DLC car: its pack's own Data_Car row
            car.year = dlc_years.get(base)
    return cars, notes


@dataclass(frozen=True)
class Disc:
    """What a disc image is, from its content."""

    path: Path
    game: str | None = None  # "FH1" / "FM4"; None: not a Forza disc (error says why)
    disc: int = 0  # 1-based
    discs: int = 0  # discs in the set
    error: str = ""

    @property
    def where(self) -> str:
        """ "FH1", "FM4 disc 1", or why the image isn't a Forza disc."""
        if self.game is None:
            return f"not a Forza disc ({self.error})"
        return f"{self.game} disc {self.disc}" if self.discs > 1 else self.game


_IDENTIFIED: dict[str, tuple[int, int, Disc]] = {}


def default_images() -> list[Path]:
    """The disc images when none are given: every *.iso in assets/, any names."""
    return sorted((REPO / "assets").glob("*.iso"))


def identify(iso: str | Path) -> Disc:
    """The game and disc of an image, read from its default.xex (a few sectors), whatever
    the file is called; cached by path, size and modification time."""
    path = Path(iso)
    try:
        st = path.stat()
    except OSError as error:
        return Disc(path, error=f"can't read it: {error.strerror or error}")
    key = os.path.normcase(path.resolve())
    hit = _IDENTIFIED.get(key)
    if hit and hit[:2] == (st.st_size, st.st_mtime_ns):
        return dataclasses.replace(hit[2], path=path)
    try:
        image = XIso(path)
    except ValueError:
        found = Disc(path, error="not an Xbox 360 disc image")
    except OSError as error:
        return Disc(path, error=f"can't read it: {error.strerror or error}")
    else:
        try:
            title, disc, discs = xex_title(image)
        except KeyError:
            found = Disc(path, error="no default.xex")
        except (ValueError, struct.error) as error:
            found = Disc(path, error=f"default.xex: {error}")
        else:
            game = TITLES.get(title)
            found = Disc(path, game, disc, discs, "" if game else f"title ID {title:08X}, another game")
        finally:
            image.close()
    _IDENTIFIED[key] = (st.st_size, st.st_mtime_ns, found)
    return found


def identify_all(isos: list[Path] | None = None) -> list[Disc]:
    """`identify` for each image (default: assets/*.iso), each file once, in order."""
    out, seen = [], set()
    for iso in default_images() if isos is None else isos:
        key = os.path.normcase(Path(iso).resolve())
        if key not in seen:
            seen.add(key)
            out.append(identify(iso))
    return out


def game_discs(isos: list[Path] | None = None) -> dict[str, Path]:
    """{"FH1": image, "FM4": disc 1, "FM4 disc 2": ...} of the given disc images (default:
    assets/*.iso), by default.xex's title and disc number, whatever the files are called.
    Of two images of one disc, the first; images that aren't Forza discs are left out."""
    out: dict[str, Path] = {}
    for d in identify_all(isos):
        if d.game:
            out.setdefault(d.game if d.disc <= 1 else f"{d.game} disc {d.disc}", d.path)
    return out


def discs_of(isos: list[Path] | None, game: str) -> list[Path]:
    """Every disc image of `game` ("FH1"/"fm4"), disc 1 first."""
    game = game.upper()
    found = [(d.disc, d.path) for d in identify_all(isos) if d.game == game]
    by_disc: dict[int, Path] = {}
    for n, path in found:
        by_disc.setdefault(n, path)
    return [by_disc[n] for n in sorted(by_disc)]


def disc_summary(discs: list[Disc]) -> tuple[list[str], list[str]]:
    """(what the images are, e.g. ["FH1", "FM4 disc 1", "FM4 disc 2"]; notes: images that
    aren't Forza discs, a second image of one disc, a game or a disc of a set that isn't
    there)."""
    found, notes, seen = [], [], {}
    for d in discs:
        if d.game is None:
            notes.append(f"{d.path.name} is {d.where}; skipped")
        elif (d.game, d.disc) in seen:
            notes.append(f"{d.path.name} is {d.where} again (also {seen[d.game, d.disc].name}); skipped")
        else:
            seen[d.game, d.disc] = d.path
            found.append(d.where)
    for game in sorted(set(TITLES.values()) - {d.game for d in discs}):
        notes.append(f"no {game} disc: its cars are skipped")
    for game in sorted({d.game for d in discs if d.game}):
        count = max(d.discs for d in discs if d.game == game)
        for n in range(1, count + 1):
            if (game, n) not in seen:
                notes.append(f"no {game} disc {n}: its cars are skipped")
    return found, notes


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("isos", nargs="*", type=Path)
    parser.add_argument("--tsv", type=Path, help="write the roster as TSV")
    parser.add_argument("--all", action="store_true", help="include non-car archives (Shared, Driver, ...)")
    parser.add_argument(
        "--dlc",
        type=Path,
        nargs="?",
        const=default_dlc_root(),
        help="also the FM4 DLC packages under DIR (default assets/), found by their STFS headers",
    )
    args = parser.parse_args(argv)
    isos = args.isos or default_images()
    packages = dlc_packages(args.dlc) if args.dlc else None
    cars, notes = roster(isos, packages)
    if packages is not None:
        found, package_notes = package_summary(packages)
        notes += [f"FM4 DLC packs: {len(found)}", *package_notes]
    shown = sorted(
        (c for c in cars.values() if args.all or c.kind in ("car", "autovista")), key=lambda c: c.name.lower()
    )

    rows = []
    for car in shown:
        where = "; ".join(f"{s.where} ({s.size / 1e6:.1f} MB)" for s in car.sources)
        rows.append((car.name, car.kind, car.games, str(car.year or ""), where))
    if args.tsv:
        with args.tsv.open("w", encoding="utf-8", newline="\n") as out:
            out.write("media_name\tkind\tgames\tyear\tsources\n")
            for row in rows:
                out.write("\t".join(row) + "\n")
    else:
        for name, kind, games, year, where in rows:
            print(f"{name:34} {kind:9} {games:8} {year:4}  {where}")

    print("", file=sys.stderr)
    for note in notes:
        print(note, file=sys.stderr)
    real = [c for c in cars.values() if c.kind == "car"]
    auto = [c for c in cars.values() if c.kind == "autovista"]
    by_games = defaultdict(int)
    for c in real:
        by_games[c.games] += 1
    print(
        f"unique cars: {len(real)} ({', '.join(f'{g}: {n}' for g, n in sorted(by_games.items()))}); "
        f"Autovista models: {len(auto)}",
        file=sys.stderr,
    )
    per_source = defaultdict(int)
    for c in real:
        for s in c.sources:
            per_source[s.where] += 1
    for where, n in sorted(per_source.items()):
        print(f"  {where}: {n}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
