"""Edit the vertex data of one vehicle in dngvehicles.sp and write a new archive.

An experiment tool for proving the geometry path end to end: scale or offset
positions, or overwrite one vertex element with a constant, for every vertex
buffer in the chosen kinds of a vehicle (by spool ID), then rebuild the
archive with tools/chnk.py's writer.

Examples:
    # stretch vehicle 304 1.3x in width (x), body and clean exterior
    python tools/edit_vertices.py <in.sp> out/wide.sp --id 304 --scale 1.3 1 1
    # set element semantic 5 to a constant in every vertex
    python tools/edit_vertices.py <in.sp> out/s5.sp --id 304 --fill 5 127,127,255,127
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from chnk import Entry, read_chunk, write_chunk
from vehicles import find, parse_sslp
from vehicle_mesh import COMPONENT_TYPES, parse_vdcl, payload, child

KIND_OFFSETS = (0, 1000, 2000)


def edit_buffer(stream, vxck: Entry, scale, offset, fill) -> int:
    header = payload(stream, child(vxck, "VXHD"))
    count, stride = struct.unpack_from("<II", header, 8)
    elements = parse_vdcl(payload(stream, child(vxck, "VDCL")))
    vxdt = child(vxck, "VXDT")
    data = bytearray(payload(stream, vxdt))
    position = elements[0]
    damage = next((e for e in elements if e.semantic == 12), None)
    target = next((e for e in elements if fill and e.semantic == fill[0]), None)
    for i in range(count):
        base = i * stride
        if scale or offset:
            x, y, z, w = struct.unpack_from("<4h", data, base + position.offset)
            p = [x / 4096, y / 4096, z / 4096]
            p = [p[k] * scale[k] + offset[k] for k in range(3)]
            struct.pack_into("<3h", data, base + position.offset,
                             *(max(-32768, min(32767, round(v * 4096))) for v in p))
            if damage:
                fmt = f"<{damage.components}e"
                d = list(struct.unpack_from(fmt, data, base + damage.offset))
                for k in range(3):
                    d[k] = d[k] * scale[k] + offset[k]
                struct.pack_into(fmt, data, base + damage.offset, *d)
        if target:
            code, _ = COMPONENT_TYPES[target.type]
            values = fill[1][:target.components]
            struct.pack_into(f"<{target.components}{code}", data, base + target.offset, *values)
    vxdt.payload = bytes(data)
    return count


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--id", type=int, required=True, help="base spool ID of the vehicle to edit")
    parser.add_argument("--kinds", default="0,2", help="which entries to edit: 0 full, 1 interior, 2 exterior")
    parser.add_argument("--scale", type=float, nargs=3, metavar=("X", "Y", "Z"))
    parser.add_argument("--offset", type=float, nargs=3, metavar=("X", "Y", "Z"))
    parser.add_argument("--fill", nargs=2, metavar=("SEMANTIC", "VALUES"),
                        help="overwrite a VDCL element, e.g. 5 127,127,255,127")
    args = parser.parse_args(argv)
    if args.out.resolve() == args.archive.resolve():
        raise SystemExit("refusing to overwrite the input archive")
    scale = offset = None
    if args.scale or args.offset:
        scale = args.scale or (1.0, 1.0, 1.0)
        offset = args.offset or (0.0, 0.0, 0.0)
    fill = None
    if args.fill:
        fill = (int(args.fill[0]), [float(v) if "." in v else int(v) for v in args.fill[1].split(",")])

    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = find(root.entries, "SSIC")
        records = parse_sslp(payload(stream, find(find(ssic.children, "NONR").children, "SSLP")))
        index_by_id = {spool_id: index for spool_id, index, _ in records}
        for kind in (int(k) for k in args.kinds.split(",")):
            vehc = root.entries[index_by_id[args.id + KIND_OFFSETS[kind]]]
            drsp = child(child(vehc, "MDXN"), "DRSP")
            for vxck in (e for e in drsp.children if e.tag == "VXCK"):
                n = edit_buffer(stream, vxck, scale, offset, fill)
                print(f"kind {kind} VEHC '{vehc.name}': edited {n} vertices")
        with args.out.open("wb") as out:
            write_chunk(out, stream, root.entries, root.version)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
