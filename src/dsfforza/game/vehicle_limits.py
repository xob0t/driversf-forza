"""Measure every VEHC against the spool limits found in Driver.exe.

    uv run dsff vehicle_limits <archive>                 # vanilla maxima per kind
    uv run dsff vehicle_limits <archive> --vehc 450 451  # these entries vs the limits

Limits (see research/spool-limits.md):

- NONR must fit the per-kind NONR buffer from the SSLP header (+0x08 + 4*kind);
  the game memcpy's the entry without a size check.
- A VEHC larger than 0x280000 bytes is read into a heap buffer instead of the
  static 2.5 MiB spool buffer (handled by the game, reported for information).
- Per-slot graphics pool budgets (SYS bytes and range nodes) bound the number
  of render objects (submodels, materials, textures, buffers) per vehicle.
- full_lod_subs: submodels in the full-detail LOD (MODL mask 4), the car's draws
  per pass; a frame's draw queue holds 16384 in all.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

from dsfforza.formats.chnk import read_chunk

STATIC_SPOOL_BUFFER = 0x280000
# MODL LOD mask of the full-detail mesh: one draw per submodel per pass. A frame queues at most
# 16384 draws in all (research/spool-limits.md, "Draw queue"); vanilla full entries have 38 to 129.
FULL_DETAIL_MASK = 4


def child(entry, tag):
    return next((c for c in entry.children or [] if c.tag == tag), None)


def read(stream, entry, size=None):
    stream.seek(entry.offset)
    return stream.read(entry.size if size is None else size)


def measure(stream, vehc) -> dict:
    mdxn, nonr = child(vehc, "MDXN"), child(vehc, "NONR")
    m = {
        "vehc": vehc.size,
        "mdxn": mdxn.size if mdxn else 0,
        "nonr": nonr.size if nonr else 0,
        "vb": 0,
        "vb_max_verts": 0,
        "verts": 0,
        "ib": 0,
        "indices": 0,
        "sbmc": 0,
        "maco": 0,
        "marc": 0,
        "texc": 0,
        "tex_bytes": 0,
        "parts": 0,
        "lod_max_subs": 0,
        "full_lod_subs": 0,
        "modl": 0,
        "upvh": 0,
    }
    drsp = child(mdxn, "DRSP") if mdxn else None
    for e in drsp.children if drsp else []:
        if e.tag == "VXCK":
            _, count, _ = struct.unpack("<QII", read(stream, child(e, "VXHD"), 16))
            m["vb"] += 1
            m["verts"] += count
            m["vb_max_verts"] = max(m["vb_max_verts"], count)
        elif e.tag == "IXCK":
            _, count = struct.unpack("<QI", read(stream, child(e, "IXHD"), 12))
            m["ib"] += 1
            m["indices"] += count
        elif e.tag == "SBMC":
            m["sbmc"] += 1
        elif e.tag == "MACO":
            m["maco"] += 1
            m["marc"] += sum(1 for c in e.children if c.tag == "MARC")
        elif e.tag == "TEXC":
            m["texc"] += 1
            dxtc = child(e, "DXTC")
            m["tex_bytes"] += dxtc.size if dxtc else 0
    modl = child(nonr, "MODL") if nonr else None
    if modl:
        data = read(stream, modl)
        m["modl"] = modl.size
        (parts,) = struct.unpack_from("<I", data)
        m["parts"] = parts
        for p in range(parts):
            lod_count, lod_offset = struct.unpack_from("<II", data, 0x10 + p * 0x60 + 0x48)
            for k in range(lod_count):
                mask, count = struct.unpack_from("<II", data, lod_offset + k * 16)
                m["lod_max_subs"] = max(m["lod_max_subs"], count)
                if mask == FULL_DETAIL_MASK:
                    m["full_lod_subs"] += count
    upvh = child(nonr, "UPVH") if nonr else None
    m["upvh"] = upvh.size if upvh else 0
    return m


def sslp_header(stream, root) -> bytes:
    ssic = next(e for e in root.entries if e.tag == "SSIC")
    nonr = child(ssic, "NONR")
    return read(stream, child(nonr, "SSLP"), 0x1C)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--vehc", type=int, nargs="*", help="VEHC root indices to check")
    args = parser.parse_args(argv)
    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        header = sslp_header(stream, root)
        nonr_limit = struct.unpack_from("<4I", header, 8)
        slots = header[0x18:0x1C]
        print(
            f"SSLP: +0x04 {struct.unpack_from('<I', header, 4)[0]:#x}, NONR buffers per kind "
            f"{[hex(x) for x in nonr_limit]}, slots {list(slots)}"
        )
        vehcs = [(i, e) for i, e in enumerate(root.entries) if e.tag == "VEHC"]
        kind_of = {}
        sslp = child(child(next(e for e in root.entries if e.tag == "SSIC"), "NONR"), "SSLP")
        records = read(stream, sslp)[0x1C:]
        for n in range(struct.unpack_from("<H", header, 2)[0]):
            _, index, kind = struct.unpack_from("<3H", records, n * 6)
            kind_of[index] = kind
        if args.vehc:
            for index in args.vehc:
                m = measure(stream, root.entries[index])
                kind = kind_of.get(index, -1)
                flags = []
                if kind >= 0 and m["nonr"] > nonr_limit[kind]:
                    flags.append(f"NONR {m['nonr']:#x} > buffer {nonr_limit[kind]:#x} (heap overflow)")
                if m["vehc"] > STATIC_SPOOL_BUFFER:
                    flags.append("VEHC > 0x280000: read into a heap buffer")
                print(
                    f"VEHC {index} kind {kind}: "
                    + ", ".join(
                        f"{k} {v:#x}" if k in ("vehc", "mdxn", "nonr", "modl", "upvh", "tex_bytes") else f"{k} {v}"
                        for k, v in m.items()
                    )
                )
                for flag in flags:
                    print("   ! " + flag)
            return
        maxima: dict[int, dict] = {}
        for index, entry in vehcs:
            kind = kind_of.get(index, -1)
            m = measure(stream, entry)
            best = maxima.setdefault(kind, {})
            for key, value in m.items():
                if value > best.get(key, (-1, 0))[0]:
                    best[key] = (value, index)
        for kind in sorted(maxima):
            print(f"kind {kind} maxima (value @ VEHC index):")
            for key, (value, index) in maxima[kind].items():
                shown = f"{value:#x}" if key in ("vehc", "mdxn", "nonr", "modl", "upvh", "tex_bytes") else str(value)
                print(f"  {key:13} {shown:>10} @ {index}")


if __name__ == "__main__":
    main()
