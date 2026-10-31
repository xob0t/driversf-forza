"""Per-car packages for dsfforza.asi's [archive] packages (experimental; research/car-packages.md).

    uv run dsff car_package make --chnk out/build/work/BMW_M3E30_91.chnk --id 700 \\
        --host 205 --out out/cp/game/dsfforza/cars/BMW_M3E30_91.sp --list out/cp/game/dsfforza/cars/cars.txt
    uv run dsff car_package show out/cp/game/dsfforza/cars/BMW_M3E30_91.sp

A package is a CHNK of four root entries: the car's three VEHC entries (kinds 0, 1, 2:
the full car, the interior, the clean exterior; port_batch's work/<media>.chnk) and a VSCT
entry with the car's three VSCT records (the host's, as clone_vehicle.register_clones
makes them). The model ID is written into the full VEHC (VSVH, VO3D, SPID, UPVH:
clone_vehicle.ID_FIELDS); the plugin writes it into the VSCT records and the SSLP
records itself, from the list file's "id path" line.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

from dsfforza.build.clone_vehicle import ID_FIELDS, patch_ids, read_leaf
from dsfforza.formats.chnk import Entry, read_chunk, walk, write_chunk
from dsfforza.formats.vehicles import VSCT_RECORD, find

KINDS = 3


def car_id(stream, vehc: Entry) -> int:
    """The model ID the full VEHC holds (its VO3D's first u32, clone_vehicle.ID_FIELDS)."""
    for _, entry in walk(vehc.children or []):
        if entry.tag == "VO3D" and entry.children is None:
            return struct.unpack_from("<I", read_leaf(stream, entry), ID_FIELDS["VO3D"])[0]
    raise ValueError("no VO3D in the full VEHC")


def host_vsct(archive: Path, host: int) -> bytes:
    """The three VSCT records of host in the vanilla archive."""
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        vsct = find(find(find(root.entries, "SSIC").children, "NONR").children, "VSCT")
        data = read_leaf(stream, vsct)
    count = struct.unpack_from("<H", data, 6)[0]
    size = VSCT_RECORD.size
    records = [data[8 + i * size : 8 + (i + 1) * size] for i in range(count)]
    mine = [r for r in records if struct.unpack_from("<H", r)[0] == host]
    if len(mine) != KINDS:
        raise ValueError(f"expected {KINDS} VSCT records for host {host}, found {len(mine)}")
    return b"".join(mine)


def make(chnk: Path, new_id: int, host: int, archive: Path, out: Path) -> int:
    """Write the package; returns the ID the car had in chnk."""
    if not 1 <= new_id <= 999:
        raise ValueError("a base model ID is 1..999 (the LOD request adds 1000 and 2000)")
    vsct = host_vsct(archive, host)
    with chnk.open("rb") as stream:
        root = read_chunk(stream, 0)
        entries = root.entries
        if len(entries) != KINDS or any(e.tag != "VEHC" for e in entries):
            raise ValueError(f"{chnk} is not three VEHC entries")
        old_id = car_id(stream, entries[0])
        if old_id != new_id:
            patch_ids(stream, entries[0], old_id, new_id)
        table = Entry(tag="VSCT", offset=0, size=len(vsct), version=0, align_log2=4, name=None, payload=vsct)
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_name(out.name + ".tmp")
        with temp.open("wb") as handle:
            write_chunk(handle, stream, [*entries, table], root.version)
        temp.replace(out)
    return old_id


def add_to_list(list_file: Path, new_id: int, package: Path) -> None:
    """Put "id path" in the list (path relative to the game folder, the list's grandparent's
    parent: <game>/dsfforza/cars/cars.txt); a line with the same ID is replaced."""
    game = list_file.resolve().parent.parent.parent
    rel = package.resolve().relative_to(game)
    lines = list_file.read_text(encoding="ascii").splitlines() if list_file.exists() else []
    lines = [line for line in lines if not line.split() or line.split()[0] != str(new_id)]
    lines.append(f"{new_id} {str(rel).replace('/', chr(92))}")
    list_file.write_text("\n".join(lines) + "\n", encoding="ascii")


def show(package: Path) -> None:
    with package.open("rb") as stream:
        root = read_chunk(stream, 0)
        for i, e in enumerate(root.entries):
            note = f"id {car_id(stream, e)}" if i == 0 and e.tag == "VEHC" else ""
            print(f"{i}: {e.tag} offset {e.offset:#x} size {e.size:#x} align {1 << e.align_log2} {note}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make", help="write one car's package")
    m.add_argument("--chnk", type=Path, required=True, help="port_batch's work/<media>.chnk")
    m.add_argument("--id", type=int, required=True, help="model ID the package gets (1..999)")
    m.add_argument("--host", type=int, required=True, help="vanilla host ID (its VSCT records are copied)")
    m.add_argument("--dsf", type=Path, default=Path("out/vanilla/Install/dngvehicles.sp"), help="vanilla archive")
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--list", type=Path, help="list file to add the package to (<game>/dsfforza/cars/cars.txt)")
    s = sub.add_parser("show", help="list a package's entries")
    s.add_argument("package", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "make":
        old = make(args.chnk, args.id, args.host, args.dsf, args.out)
        print(f"{args.out}: ID {old} -> {args.id}, host {args.host}")
        if args.list:
            add_to_list(args.list, args.id, args.out)
            print(f"{args.list}: {args.id} added")
    else:
        show(args.package)


if __name__ == "__main__":
    main()
