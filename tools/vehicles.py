"""List the vehicles in Driver: San Francisco's Install/dngvehicles.sp.

Joins four tables from the archive:

- root VEHC entries: one per vehicle variant, named with a display class
  ("Saloon 15");
- SSIC/NONR/SSLP: maps spool IDs to VEHC indices and variant kinds;
- SSIC/NONR/VSCT: maps spool IDs to a 16-character model name;
- BINF: the exporter's list of source .rimodel paths, used to expand the
  truncated VSCT names.

See research/dngvehicles.md for the layouts.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from chnk import Entry, read_chunk

SSLP_HEADER = struct.Struct("<HHIIIII")  # unknown, count, ...; 0x18 bytes
SSLP_RECORD = struct.Struct("<HHH")  # spool id, VEHC index, kind
VSCT_RECORD = struct.Struct("<HBBH16s6B")  # spool id, ?, ?, ?, name, 6 bytes


@dataclass
class Variant:
    index: int
    spool_id: int
    kind: int
    vehc: Entry
    model: str
    vsct_fields: tuple[int, ...]


def find(entries: list[Entry], tag: str) -> Entry:
    return next(entry for entry in entries if entry.tag == tag)


def read_payload(stream, entry: Entry) -> bytes:
    stream.seek(entry.offset)
    return stream.read(entry.size)


def parse_sslp(data: bytes) -> list[tuple[int, int, int]]:
    _, count, *_ = SSLP_HEADER.unpack_from(data)
    base = 0x1C  # 4 bytes after the header are not decoded yet
    return [SSLP_RECORD.unpack_from(data, base + i * SSLP_RECORD.size) for i in range(count)]


def parse_vsct(data: bytes) -> dict[int, tuple[str, tuple[int, ...]]]:
    if data[:4] != b"VSCT":
        raise ValueError("bad VSCT magic")
    _version, count = struct.unpack_from("<HH", data, 4)
    names = {}
    for i in range(count):
        spool_id, a, b, c, name, *rest = VSCT_RECORD.unpack_from(data, 8 + i * VSCT_RECORD.size)
        names[spool_id] = (name.split(b"\0", 1)[0].decode("latin-1"), (a, b, c, *rest))
    return names


def parse_binf(data: bytes) -> list[str]:
    text = data.decode("latin-1")
    return re.findall(r"\\Vehicles\\Vehicles\\([^\\]+)\\Model\\", text)


def expand(short: str, full_names: list[str]) -> str:
    """Expand a truncated VSCT name; ambiguous prefixes return all candidates."""
    if short in full_names:
        return short
    matches = [name for name in full_names if name.lower().startswith(short.lower())]
    return "|".join(matches) if matches else short


def load(path: Path) -> tuple[list[Variant], list[str]]:
    with path.open("rb") as stream:
        root = read_chunk(stream, 0, depth=2).entries
        ssic = find(root, "SSIC")
        nonr = find(ssic.children, "NONR")
        sslp = parse_sslp(read_payload(stream, find(nonr.children, "SSLP")))
        vsct = parse_vsct(read_payload(stream, find(nonr.children, "VSCT")))
        binf = parse_binf(read_payload(stream, find(root, "BINF")))
    variants = []
    for spool_id, index, kind in sslp:
        model, fields = vsct.get(spool_id, ("?", ()))
        variants.append(Variant(index, spool_id, kind, root[index], expand(model, binf), fields))
    return variants, binf


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path, help="path to Install/dngvehicles.sp")
    parser.add_argument("--variants", action="store_true", help="print every variant, not one row per vehicle")
    args = parser.parse_args(argv)

    variants, binf = load(args.archive)
    if args.variants:
        print("vehc\tspool_id\tkind\tsize\tclass\tmodel\tvsct_fields")
        for v in variants:
            print(f"{v.index}\t{v.spool_id}\t{v.kind}\t{v.vehc.size:#x}\t{v.vehc.name}\t{v.model}\t"
                  f"{' '.join(f'{x:02x}' for x in v.vsct_fields)}")
        return
    print("vehc\tspool_id\tclass\tmodel")
    for v in variants:
        if v.kind == 0:
            print(f"{v.index}\t{v.spool_id}\t{v.vehc.name}\t{v.model}")
    unmatched = sorted(set(binf) - {v.model for v in variants})
    if unmatched:
        print(f"# BINF models without an exact match: {', '.join(unmatched)}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
