"""FM4 DLC packs in the build: which packs are used, and the FM4 gamedb with their rows.

The packs are the FM4 DLC packages under assets/ (forza_roster.dlc_packages, found by their
STFS header, any folder and file name). They are used by default; DSFF_FM4_DLC=0 in the
environment (or `--no-fm4-dlc` on port_batch and install, which set it) leaves them out, so
the roster, the car lists and the gamedb are the discs' again.

The merged gamedb is FM4's disc gamedb.slt plus every pack's media/db/patch/<N>_merge.slt
(SQLite, in the pack's Media/DLCZips/<N>_pri_65.zip), merged as FM4 merges them: it ATTACHes
the patch file and appends each of its tables to the database's, `ATTACH '%s' AS filedb`,
`INSERT INTO [%s%s] SELECT * FROM filedb.[%s]` (strings at 0x820D2170 and 0x820D281C of
FM4's default.xex, research/forza-sources.md "The DLC gamedb"). Where the disc's table has
a primary key a row already holds, the first row stays (what FM4 does on that constraint
error isn't decoded; on the user's 16 packs it changes no value the converter reads: every
such row of the tables it reads is the same as the disc's or the earlier pack's, and the one
table where a key meets other values, List_UpgradeCarBodyTireWidthRear, holds the DLC
SHE_GT500428CJ_69's upgrade rows, which the converter doesn't read). No pack row shares its
key with a row of a disc car's in Data_Car, Data_CarBody, Combo_Colors or Data_Engine.

It is written once per set of packs, to out/cache/fm4/dlc-<key>/gamedb.slt (the key: a hash
of the disc gamedb's size and each pack's display name and size). The disc's own copies
(out/cache/fm4/gamedb.slt, out/db/fm4_gamedb.slt) stay as they are. A disc car is built from
the disc gamedb (`for_source`); a DLC car from the merged one. Readers that look a car up by
its media name in "FM4's gamedb" (host selection, handling, wheel clearance, render_compare)
get the merged one (`gamedb_path`): it holds every disc row unchanged.

    uv run dsff fm4_dlc [--cache out/cache]       the packs found and the merged gamedb
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from dsfforza import REPO

ENV = "DSFF_FM4_DLC"  # "0": leave the packs out (port_batch/install --no-fm4-dlc)
FORMAT = 1  # of the merged database (bump when the merge changes)
FOLDER = "dlc-{key}"  # under <cache>/fm4/
PATCH = "media/db/patch/*_merge.slt"  # a pack's gamedb rows (research/forza-sources.md, "FM4 DLC packs")


def enabled() -> bool:
    """Whether the build uses the FM4 DLC packs found (on unless DSFF_FM4_DLC is 0/no/off)."""
    return os.environ.get(ENV, "").strip().lower() not in ("0", "no", "off", "false")


def disable() -> None:
    """--no-fm4-dlc: this process and the workers it starts leave the packs out."""
    os.environ[ENV] = "0"


def packages(root: Path | None = None) -> list:
    """The FM4 DLC packs the build uses: one package per pack, from assets/ (or `root`), in
    path order; none when turned off (DSFF_FM4_DLC=0)."""
    if not enabled():
        return []
    from dsfforza.build import forza_roster

    return forza_roster.unique_packages(forza_roster.dlc_packages(root))


def key(disc: Path, packs: list) -> str:
    """The merged gamedb's key: the disc gamedb's size, each pack's display name and size."""
    parts = [f"format {FORMAT}", f"disc {Path(disc).stat().st_size}"]
    parts += sorted(f"{p.name}|{p.path.stat().st_size}" for p in packs)
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12]


def patch_files(package) -> list[tuple[int, str, bytes]]:
    """(N, name, bytes) of a pack's media/db/patch/<N>_merge.slt files."""
    from dsfforza.formats.forzazip import ForzaZip
    from dsfforza.formats.stfs import Stfs

    out = []
    with package.path.open("rb") as stream:
        pkg = Stfs(stream)
        for inner in pkg.files():
            low = inner.path.lower()
            if inner.is_dir or not fnmatch.fnmatch(low, "media/dlczips/*.zip") or "stringtables" in low:
                continue
            archive = ForzaZip(pkg.open(inner))
            for info in archive.entries():
                name = info.filename.replace("\\", "/").lower()
                if fnmatch.fnmatch(name, PATCH):
                    stem = name.rsplit("/", 1)[-1].split("_", 1)[0]
                    out.append((int(stem) if stem.isdigit() else 0, name, archive.read(info)))
    return out


def _tables(db: sqlite3.Connection, schema: str = "main") -> dict[str, str]:
    rows = db.execute(f"SELECT name, sql FROM {schema}.sqlite_master WHERE type = 'table'")
    return {name: sql for name, sql in rows if not name.startswith("sqlite_")}


def _columns(db: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [row[1] for row in db.execute(f'PRAGMA {schema}.table_info("{table}")')]


def merge(disc: Path, patches: list[tuple[int, str, bytes]], target: Path) -> dict:
    """Write `target`: the disc gamedb with every patch's rows appended, table by table, as
    FM4's INSERT INTO [t] SELECT * FROM filedb.[t] does (by column name, so a patch with the
    columns in another order still lands right; a table the disc lacks is created from the
    patch's own CREATE). Where the disc's table has a primary key that a row already holds,
    the first row stays (OR IGNORE): the disc's, then the patches' in N order (the patch
    name's number). On the user's 16 packs every such row of a table the converter reads is
    the same as the one kept (List_FamilyModel, List_CarMake), so keeping or replacing gives
    the same values. Returns {patch name: rows added}."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temp.write_bytes(Path(disc).read_bytes())
    counts = {}
    scratch = target.parent / f"patch.{os.getpid()}.slt"
    try:
        db = sqlite3.connect(temp)
        try:
            for _n, name, data in sorted(patches):
                scratch.write_bytes(data)
                db.execute("ATTACH DATABASE ? AS filedb", (str(scratch),))
                try:
                    have = _tables(db)
                    added = 0
                    for table, sql in sorted(_tables(db, "filedb").items()):
                        if table not in have:
                            db.execute(sql)
                        mine = set(_columns(db, table))
                        cols = [c for c in _columns(db, table, "filedb") if c in mine]
                        names = ", ".join(f'"{c}"' for c in cols)
                        added += db.execute(
                            f'INSERT OR IGNORE INTO main."{table}" ({names}) SELECT {names} FROM filedb."{table}"'
                        ).rowcount
                    db.commit()
                    counts[name] = added
                finally:
                    db.execute("DETACH DATABASE filedb")
        finally:
            db.close()
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
        scratch.unlink(missing_ok=True)
    return counts


def merged_gamedb(isos: list[Path], cache: Path, packs: list | None = None, log=print) -> Path | None:
    """The merged FM4 gamedb for `packs` (default: packages()), written when missing; None
    without packs or without FM4's disc gamedb."""
    from dsfforza.build import port_car

    packs = packages() if packs is None else packs
    if not packs:
        return None
    disc = port_car.extract_gamedb(isos, "FM4", cache)
    if disc is None:
        return None
    folder = Path(cache) / "fm4" / FOLDER.format(key=key(disc, packs))
    target = folder / "gamedb.slt"
    if target.exists():
        return target
    patches = [p for pack in packs for p in patch_files(pack)]
    counts = merge(disc, patches, target)
    stamp = {"disc": str(disc), "packs": [p.name for p in packs], "patches": counts}
    port_car.write_atomic(folder / "merge.json", json.dumps(stamp, indent=1).encode())
    log(f"FM4 gamedb with {len(packs)} DLC packs: {sum(counts.values())} rows from {len(counts)} patches -> {target}")
    return target


def gamedb(isos: list[Path], game: str, cache: Path) -> Path | None:
    """A game's gamedb for lookups by media name: FM4's merged one when packs are used, else
    the disc's (port_car.extract_gamedb)."""
    from dsfforza.build import port_car

    if game.upper() == "FM4":
        merged = merged_gamedb(isos, cache)
        if merged is not None:
            return merged
    return port_car.extract_gamedb(isos, game, cache)


def for_source(source, isos: list[Path], cache: Path) -> Path | None:
    """The gamedb a car is built from: the merged one for a DLC car, the disc's otherwise (so
    a disc car's inputs are the same file as without the packs)."""
    from dsfforza.build import port_car

    if source.dlc:
        return merged_gamedb(isos, cache) or port_car.extract_gamedb(isos, source.game, cache)
    return port_car.extract_gamedb(isos, source.game, cache)


def existing(cache: Path | None = None) -> Path | None:
    """The merged FM4 gamedb already written for the packs found now, or None (nothing is
    extracted or written: for readers outside the build)."""
    cache = Path(cache) if cache else REPO / "out" / "cache"
    disc = cache / "fm4" / "gamedb.slt"
    packs = packages()
    if not packs or not disc.is_file():
        return None
    target = cache / "fm4" / FOLDER.format(key=key(disc, packs)) / "gamedb.slt"
    return target if target.is_file() else None


def gamedb_path(game: str, folder: Path | None = None) -> Path:
    """`game`'s gamedb for the readers of out/db/<game>_gamedb.slt (dsff game_data prepare):
    FM4's merged one when it has been written for the packs found, else that file."""
    if game.upper() == "FM4" and (merged := existing()) is not None:
        return merged
    return (Path(folder) if folder else REPO / "out" / "db") / f"{game.lower()}_gamedb.slt"


def beside(car_folder: Path) -> Path:
    """The gamedb next to an extracted car folder (<cache>/<game>/<car>/..): FM4's merged one
    when written for the packs found, else <cache>/<game>/gamedb.slt."""
    parent = Path(car_folder).parent
    if parent.name.lower() == "fm4" and (merged := existing(parent.parent)) is not None:
        return merged
    return parent / "gamedb.slt"


def car_names(media: list[str], isos: list[Path] | None = None, cache: Path | None = None) -> dict[str, dict]:
    """{media: {make, display, model_short, family, year}} of the DLC cars among `media`, from
    their own packs (forza_roster.pack_info: the pack's Data_Car rows and EN string tables, a
    maker or family the pack lacks from FM4's disc gamedb and string tables)."""
    from dsfforza.build import forza_roster
    from dsfforza.formats import forza_strings

    packs = packages()
    if not packs or not media:
        return {}
    disc_db = (Path(cache) if cache else REPO / "out" / "cache") / "fm4" / "gamedb.slt"
    try:
        disc_strings = forza_strings.Strings("fm4", isos=isos)
    except LookupError:  # no FM4 disc: the packs' own tables only
        disc_strings = None
    wanted = {m.lower(): m for m in media}
    out = {}
    for pack in packs:
        info = forza_roster.pack_info(pack, disc_db if disc_db.is_file() else None, disc_strings)
        out.update({wanted[k]: v for k, v in info.items() if k in wanted and wanted[k] not in out})
    return out


class PackUI:
    """The packs' UI textures (media/UI/Textures/ in each pack's Media/DLCZips/*.zip, laid out
    as the disc's Media/UI/Textures.zip) and their makers' logo keys: List_CarMake.IconPathBase
    of each maker row in a pack's gamedb patch ("AUS" for Austin-Healey, whose logo is
    UI4/Logos/Manufacturer_Logos/L_MAN_AUS_Logo.xds), by the maker's name (the pack's EN
    List_CarMake text, lower case). For gui_assets.ForzaUI.logo: the makers only the packs have."""

    def __init__(self, packs: list):
        from dsfforza.build import forza_roster

        self.files: dict[str, tuple[Path, str, str]] = {}  # name lower -> (package, inner zip, member)
        self.keys: dict[str, str] = {}
        for pack in packs:
            dbs, strings, ui = forza_roster.pack_contents(pack)
            try:
                for name, (inner, member) in ui.items():
                    self.files.setdefault(name, (pack.path, inner, member))
                for db in dbs:
                    try:
                        rows = db.execute("SELECT DisplayName, IconPathBase FROM List_CarMake").fetchall()
                    except sqlite3.OperationalError:  # a patch without maker rows
                        continue
                    for ref, key in rows:
                        text = next((t for table in strings if (t := table.get(ref)) is not None), None)
                        if text and key:
                            self.keys.setdefault(text.lower(), key)
            finally:
                for db in dbs:
                    db.close()

    def read(self, name: str) -> bytes | None:
        """A texture's bytes by its name under Textures/ (any case), or None."""
        from dsfforza.formats.forzazip import ForzaZip
        from dsfforza.formats.stfs import Stfs

        hit = self.files.get(name.lower())
        if hit is None:
            return None
        path, inner, member = hit
        with Path(path).open("rb") as stream:
            pkg = Stfs(stream)
            entry = next(f for f in pkg.files() if f.path == inner)
            archive = ForzaZip(pkg.open(entry))
            return archive.read(next(i for i in archive.entries() if i.filename == member))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, default=REPO / "out" / "cache")
    parser.add_argument("--iso", type=Path, action="append", help="disc images (default assets/*.iso)")
    args = parser.parse_args(argv)
    from dsfforza.build import forza_roster

    packs = packages()
    print(f"FM4 DLC packs: {len(packs)}" + ("" if enabled() else f" (turned off: {ENV}=0)"))
    for p in packs:
        print(f"  {p.name}: {p.path}")
    isos = [p.resolve() for p in (args.iso or forza_roster.default_images())]
    print(f"merged gamedb: {merged_gamedb(isos, args.cache, packs)}")


if __name__ == "__main__":
    main()
