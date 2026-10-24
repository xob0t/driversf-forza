"""Forza localised string tables (.str, "LSB2") and the "_&N" references in gamedb.slt.

FH1 media/stringtables/EN.zip and FM4 Media/StringTables/EN.zip (LOC.zip: id placeholders) hold one
.str per table (Data_Car.str, List_CarMake.str, ...). Layout (after Doliman100's
010 template, _ref/ForzaTech-extraction-tools/patterns/str.bt):

    "LSB2", u8 flags (bit 0: big-endian), 3 bytes, u16 version, 2 bytes,
    u32 header size, u32 section count, u32 section offsets[count] (0 = none)
    section {u32 size, u32 count, ...}:
      0 strings: (count + 1) x {u16 hash, u32 offset}, then UTF-16 text at
                 offset * 2 from the end of the table
      1 ids:     count x {u16 hash, u16 line, u32 offset}, then ASCII ids
      2 comments, 3 hashes in file order (unused here)

A gamedb value "_&N" is N = table hash << 16 | string hash, both 16-bit
rotate-left-7 xor hashes of the lower-cased table name and string id (init
0xFFFF; FM2 used 0xFFFFFFFF, detected from the file).

    uv run dsff forza_strings out/cache/fh1/strings/Data_Car.str [--grep 370Z]
    uv run dsff forza_strings names fh1 NIS_370Z_10 BMW_M5_12
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from dsfforza import REPO

STRING_FILES = ("Data_Car.str", "List_CarMake.str", "List_FamilyModel.str")
ISO_PATHS = {"fh1": (REPO / "assets" / "Forza.Horizon.XBOX360.iso", "media/stringtables/EN.zip"),
             "fm4": (REPO / "assets" / "Forza 4 [NTSCU][DVD1].iso", "Media/StringTables/EN.zip")}


def str_hash(text: str, version: int = 1, bits: int = 16) -> int:
    mask = (1 << bits) - 1
    value = mask if version else 0xFFFFFFFF
    for ch in text.lower():
        value ^= ord(ch)
        value = ((value << 7) | (value >> (bits - 7))) & mask
    return value


def parse(data: bytes) -> dict[int, tuple[str, str]]:
    """{string hash: (id, text)}."""
    if data[:4] not in (b"LSB2", b"LSBL"):
        raise ValueError("not an LSB string table")
    e = ">" if data[:4] == b"LSB2" and data[4] & 1 else "<"
    base = 12 if data[:4] == b"LSB2" else 4
    header_size, count = struct.unpack_from(e + "II", data, base)
    offsets = struct.unpack_from(f"{e}{count}I", data, base + 8)
    texts, ids = {}, {}
    if offsets[0]:
        o = offsets[0]
        size, n = struct.unpack_from(e + "II", data, o)
        table = o + 8
        p = table + 6 * (n + 1)
        for i in range(n + 1):
            h, off = struct.unpack_from(e + "HI", data, table + 6 * i)
            start = p + off * 2
            end = start
            while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
                end += 2
            texts[h] = data[start:end].decode("utf-16-be" if e == ">" else "utf-16-le", errors="replace")
    if len(offsets) > 1 and offsets[1]:
        o = offsets[1]
        size, n = struct.unpack_from(e + "II", data, o)
        table = o + 8
        p = table + 8 * n
        for i in range(n):
            h, _, off = struct.unpack_from(e + "HHI", data, table + 8 * i)
            end = data.index(b"\0", p + off)
            ids[h] = data[p + off:end].decode("latin-1")
    return {h: (ids.get(h, ""), t) for h, t in texts.items()}


class Strings:
    """Resolve "_&N" references against a game's string tables."""

    def __init__(self, game: str, cache: Path | None = None):
        self.tables: dict[int, dict[int, tuple[str, str]]] = {}
        folder = cache or (REPO / "out" / "cache" / game / "strings")
        missing = [f for f in STRING_FILES if not (folder / f).exists()]
        if missing:
            extract(game, folder)
        for name in STRING_FILES:
            path = folder / name
            if not path.exists():
                continue
            table = parse(path.read_bytes())
            stem = name.rsplit(".", 1)[0]
            # Pick the hash version that reproduces the first id's hash.
            version = 1
            for h, (sid, _) in table.items():
                if sid:
                    version = 1 if str_hash(sid, 1) == h else 0
                    break
            self.tables[str_hash(stem, version)] = table

    def get(self, ref: str | None) -> str | None:
        if not ref or not str(ref).startswith("_&"):
            return ref
        n = int(str(ref)[2:])
        entry = self.tables.get(n >> 16, {}).get(n & 0xFFFF)
        return entry[1] if entry else None


def extract(game: str, folder: Path) -> None:
    from dsfforza.formats.forzazip import ForzaZip
    from dsfforza.formats.xiso import XIso
    iso, path = ISO_PATHS[game]
    image = XIso(iso)
    try:
        z = ForzaZip(image.open(image.find(path)))
        folder.mkdir(parents=True, exist_ok=True)
        for info in z.entries():
            if info.filename in STRING_FILES:
                (folder / info.filename).write_bytes(z.read(info))
    finally:
        image.close()


def car_names(game: str, media: list[str], gamedb: Path | None = None) -> dict[str, dict]:
    """{media: {make, display, model_short, family, year}} from Data_Car, List_CarMake, List_FamilyModel."""
    import sqlite3
    gamedb = gamedb or REPO / "out" / "cache" / game / "gamedb.slt"
    strings = Strings(game)
    db = sqlite3.connect(f"file:{Path(gamedb).as_posix()}?mode=ro", uri=True)
    out = {}
    try:
        for name in media:
            row = db.execute("SELECT c.DisplayName, c.ModelShort, c.Year, m.DisplayName, f.DisplayName "
                             "FROM Data_Car c LEFT JOIN List_CarMake m ON m.ID = c.MakeID "
                             "LEFT JOIN List_FamilyModel f ON f.Id = c.FamilyModelID "
                             "WHERE lower(c.MediaName) = lower(?)", (name,)).fetchone()
            if row:
                out[name] = {"display": strings.get(row[0]), "model_short": strings.get(row[1]), "year": row[2],
                             "make": strings.get(row[3]), "family": strings.get(row[4])}
    finally:
        db.close()
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", help=".str file, or 'names'")
    parser.add_argument("rest", nargs="*", help="names: game (fh1/fm4) then media names")
    parser.add_argument("--grep")
    args = parser.parse_args(argv)
    if args.target == "names":
        for media, info in car_names(args.rest[0], args.rest[1:]).items():
            print(media, info)
        return
    for h, (sid, text) in sorted(parse(Path(args.target).read_bytes()).items()):
        if not args.grep or args.grep.lower() in (sid + text).lower():
            print(f"{h:04X} {sid} = {text!r}")


if __name__ == "__main__":
    sys.exit(main())
