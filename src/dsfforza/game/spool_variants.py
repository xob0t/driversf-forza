"""Build dngvehicles.sp variants that probe the spool size limits.

Each option changes one thing, so a crash points at one limit:

    --pad-vehc INDEX:SIZE   append a zero-filled 'PADD' child to VEHC INDEX so the
                            VEHC entry is exactly SIZE bytes. The game skips
                            children it doesn't know (FUN_00858A70), so only the
                            read size changes. SIZE > 0x27F000 forces the
                            heap-buffer read path (FUN_008FC910).
    --pad-nonr INDEX:SIZE   same, inside VEHC INDEX's NONR (SceneModelType2
                            memcpy's the whole NONR into the per-slot buffer;
                            LoadTaggedResources ignores the unknown tag).
    --sslp-nonr KIND:SIZE   set the per-kind NONR buffer size, SSLP header
                            +0x08 + 4*KIND (allocated per slot at package init).
    --sslp-slots A,B,C      set the per-kind slot counts, SSLP header +0x18..+0x1A.

Sizes accept hex (0x30A000). Example (the 2.5 MiB read-path test):

    uv run dsff spool_variants out/dngvehicles.clone.sp out/lim-pad-over.sp --pad-vehc 450:0x30A000
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

from dsfforza.formats.chnk import Entry, layout, read_chunk, write_chunk


def number(text: str) -> int:
    return int(text, 0)


def pair(text: str) -> tuple[int, int]:
    left, right = text.split(":")
    return number(left), number(right)


def child(entry: Entry, tag: str) -> Entry:
    return next(c for c in entry.children if c.tag == tag)


def pad_to(container: Entry, target: int) -> int:
    """Append a PADD leaf so layout(container.children) totals exactly target."""
    current = layout(container.children)[0]
    if target <= current:
        raise SystemExit(f"{container.tag} is already {current:#x} >= {target:#x}")
    pad = Entry("PADD", 0, 0, 0, 4, None, payload=b"")
    container.children.append(pad)
    size = max(0, target - current - 0x20)
    for _ in range(64):
        pad.payload = bytes(size)
        total = layout(container.children)[0]
        if total == target:
            return size
        size += (target - total) if total < target else -(total - target)
        size = max(0, size)
    raise SystemExit(f"could not pad {container.tag} to exactly {target:#x}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("archive", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--pad-vehc", type=pair, action="append", default=[])
    parser.add_argument("--pad-nonr", type=pair, action="append", default=[])
    parser.add_argument("--sslp-nonr", type=pair, action="append", default=[])
    parser.add_argument("--sslp-slots", type=lambda s: [number(x) for x in s.split(",")])
    args = parser.parse_args(argv)
    if args.out.resolve() == args.archive.resolve():
        raise SystemExit("refusing to overwrite the input")

    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        entries = root.entries
        for index, size in args.pad_nonr:
            nonr = child(entries[index], "NONR")
            before = layout(nonr.children)[0]
            pad = pad_to(nonr, size)
            print(f"VEHC {index} NONR {before:#x} -> {size:#x} (PADD {pad:#x})")
        for index, size in args.pad_vehc:
            vehc = entries[index]
            before = layout(vehc.children)[0]
            pad = pad_to(vehc, size)
            print(f"VEHC {index} {before:#x} -> {size:#x} (PADD {pad:#x})")
        if args.sslp_nonr or args.sslp_slots:
            ssic = next(e for e in entries if e.tag == "SSIC")
            sslp = child(child(ssic, "NONR"), "SSLP")
            stream.seek(sslp.offset)
            data = bytearray(stream.read(sslp.size))
            for kind, size in args.sslp_nonr:
                old = struct.unpack_from("<I", data, 8 + 4 * kind)[0]
                struct.pack_into("<I", data, 8 + 4 * kind, size)
                print(f"SSLP NONR buffer kind {kind}: {old:#x} -> {size:#x}")
            if args.sslp_slots:
                old = list(data[0x18:0x18 + len(args.sslp_slots)])
                data[0x18:0x18 + len(args.sslp_slots)] = bytes(args.sslp_slots)
                print(f"SSLP slots: {old} -> {args.sslp_slots}")
            sslp.payload = bytes(data)
        with args.out.open("wb") as output:
            write_chunk(output, stream, entries, root.version)
    print(f"wrote {args.out} ({args.out.stat().st_size:#x} bytes)")


if __name__ == "__main__":
    main()
