"""List every car in the user's Forza discs (FH1, FM4 disc 1 and 2), deduplicated by media name.

Sources scanned, all read in place (nothing is extracted):

  * loose car zips on a disc: media/cars/<NAME>.zip (FH1, FM4 disc 1);
  * FM4 disc 2 car packs: STFS (PIRS) packages under content/, each holding
    Media/DLCZips/000N_pri_65.zip, whose media/cars/<NAME>/ folders are cars.

Each image is identified by its content, never by its file name (`identify`): default.xex's
title ID (4D530910 = FM4, 4D5309C9 = FH1) and disc number, cached per process by path, size
and modification time. An image that isn't a Forza disc is skipped with a note. `game_discs`
and `discs_of` give the other tools "the FH1 disc", "FM4 disc 1" or "the FM4 discs" out of
the build's images (assets/*.iso or --iso, any names); a game without its disc is simply
absent, and its cars are skipped. A
zip or folder counts as a car if it holds a main carbin (<name>.carbin), and
as an Autovista model if it only has <name>_s_lod0.carbin. Everything else
(Shared.zip, Driver.zip, AppearancePresets.zip) is listed with --all. The
model year comes from each game's Media/db/gamedb.slt (SQLite, Data_Car).

    uv run dsff forza_roster [iso ...] [--tsv out.tsv] [--all]

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
from dsfforza.formats.stfs import StfsError, in_image
from dsfforza.formats.xiso import XIso

TITLES = {0x4D530910: "FM4", 0x4D5309C9: "FH1"}
XEX_EXECUTION_INFO = 0x00040006


@dataclass
class Source:
    game: str
    where: str  # e.g. "FM4 disc 1", "FM4 disc 2 / 1950-2000 Model Year Car Pack"
    path: str  # path of the zip or folder
    size: int  # bytes as stored (compressed)
    files: int
    kind: str  # car | autovista | placeholder | other
    iso: str = ""  # disc image the source is on
    package: str = ""  # STFS package path on the disc (disc 2 car packs), else ""


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
        if image.read(entry, 0, 4) not in (b"CON ", b"LIVE", b"PIRS"):
            continue
        try:
            package = in_image(image, entry)
        except StfsError as error:
            print(f"warning: {entry.path}: {error}", file=sys.stderr)
            continue
        label = f"{where} / {package.display_name or entry.path.rsplit('/', 1)[-1]}"
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
                        label,
                        f"{inner.path}::media/cars/{name}/",
                        sum(i.compress_size for i in infos),
                        len(infos),
                        classify(name, files),
                        package=entry.path,
                    )
                )
    return sources


def roster(isos: list[Path]) -> tuple[dict[str, Car], list[str]]:
    cars: dict[str, Car] = {}
    notes = []
    years: dict[str, dict[str, int]] = {}
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
    for key, car in cars.items():
        base = key[:-5] if key.endswith("_slod") else key
        for game in ("FM4", "FH1"):
            if car.year is None and base in years.get(game, {}):
                car.year = years[game][base]
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
    args = parser.parse_args(argv)
    isos = args.isos or default_images()
    cars, notes = roster(isos)
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
