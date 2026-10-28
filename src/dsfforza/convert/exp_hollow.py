"""Experiment: keep the host's submodel records but not their geometry.

Builds on forza2dsf's working layout (host submodels kept, Forza body added
to MODL part 0, other parts emptied) and then "hollows" the host: every host
SBBH is rewritten to draw one degenerate triangle from new tiny buffers of
the same stride, and the host's VXCK/IXCK are removed. If the game still
loads this, the engine needs the host's submodel records (count, order,
UIDs) but not their vertex data.

    uv run dsff exp_hollow out/dngvehicles.clone.sp out/hollow.sp --id 304 \\
        --car out/fh1/NIS_370Z_10 --name NIS_370Z_10
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from dsfforza.convert import forza2dsf as F
from dsfforza.formats.chnk import read_chunk, write_chunk
from dsfforza.formats.vehicle_mesh import child, payload
from dsfforza.formats.vehicles import find, parse_sslp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--id", type=int, required=True)
    parser.add_argument("--car", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--lods", default="4:3,8:4,16:5")
    parser.add_argument("--complete", action="store_true", help="also move wheel hubs and rebuild the exterior")
    parser.add_argument(
        "--textures",
        choices=("replace", "clone"),
        help="texture lamps, badges and interior (dsf_materials; replace keeps every UID)",
    )
    parser.add_argument("--swatch-trims", action="store_true", help="with --textures: colour swatch trims too")
    parser.add_argument(
        "--merge-submodels",
        action="store_true",
        help="one body submodel per material instead of per (section, material)",
    )
    args = parser.parse_args(argv)

    import xml.etree.ElementTree as ET

    from dsfforza.formats import carbin

    sections = carbin.parse((args.car / f"{args.name}.carbin").read_bytes())
    body = [s for s in F.vanilla_sections(sections) if s.name != "wheel"]
    misc = ET.parse(args.car / "Physics" / "MAXData.xml").getroot().find("Misc")
    mid_z = float(misc.find("BottomCenterWheelbasePos").get("z"))
    place = F.Placement(dy=F.HOST_HUB_Y - 0.339, dz=0.1155 - mid_z)
    lod_masks = {int(m): int(l) for m, l in (p.split(":") for p in args.lods.split(","))}

    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = find(root.entries, "SSIC")
        records = parse_sslp(payload(stream, find(find(ssic.children, "NONR").children, "SSLP")))
        full = root.entries[{s: i for s, i, _ in records}[args.id]]
        drsp = child(child(full, "MDXN"), "DRSP")

        tex_full = tex_ext = None
        layout = dict(F.MATERIAL_LAYOUT)
        if args.textures:
            from dsfforza.convert import dsf_materials

            exterior_vehc = root.entries[{s: i for s, i, _ in records}[args.id + 2000]]
            tex_full, tex_ext = dsf_materials.prepare(
                stream,
                full,
                exterior_vehc,
                args.car,
                body,
                set(lod_masks.values()) | {1},
                mode=args.textures,
                swatch_trims=args.swatch_trims,
            )
            layout.update(tex_full.layout)
            layout.update(tex_ext.layout)

        builder = F.Builder(b"\x00\x12\xde\xad", layout=layout)
        part_lods = {
            0: {
                mask: F.add_geometry(
                    builder, body, lod, place, wheel=False, textures=tex_full, merge=args.merge_submodels
                )
                for mask, lod in lod_masks.items()
            }
        }
        # One degenerate triangle per stride, for the hollowed host submodels.
        stubs = {}
        for material in (F.PAINT, F.GLASS, F.TYRE):
            stride = layout[material][0]
            v = F.encode_vertex(stride, (0.0, 0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 1.0, 0.0))
            stubs[stride] = builder.add(material, [v] * 3, [(0, 1, 2)])

        host_modl = payload(stream, child(child(full, "NONR"), "MODL"))
        F.rebuild_drsp(stream, drsp, builder, keep_host_materials=True, keep_host_geometry=True)

        # Hollow the host submodels: point them at the stubs, drop host buffers.
        new_vb = {}
        for e in drsp.children:
            if e.tag == "VXCK" and child(e, "VXHD").payload is not None:
                hd = child(e, "VXHD").payload
                new_vb[struct.unpack_from("<I", hd, 12)[0]] = hd[:8]
        new_ib = next(
            child(e, "IXHD").payload[:8]
            for e in drsp.children
            if e.tag == "IXCK" and child(e, "IXHD").payload is not None
        )
        host_strides = {}
        for e in drsp.children:
            if e.tag == "VXCK" and child(e, "VXHD").payload is None:
                hd = payload(stream, child(e, "VXHD"))
                host_strides[hd[:8]] = struct.unpack_from("<I", hd, 12)[0]
        hollowed = 0
        for e in drsp.children:
            if e.tag != "SBMC":
                continue
            sbbh = child(e, "SBBH")
            if sbbh.payload is not None:
                continue  # one of ours
            d = bytearray(payload(stream, sbbh))
            stride = host_strides[bytes(d[16:24])]
            stub = stubs[stride]
            d[16:24] = new_vb[stride]
            struct.pack_into("<I", d, 0x20, stub.base_vertex)
            d[0x30:0x38] = new_ib
            struct.pack_into("<II", d, 0x38, stub.first_index, stub.index_count)
            sbbh.payload = bytes(d)
            hollowed += 1
        host_vb_uids = set(host_strides)
        host_ib_uids = {
            payload(stream, child(e, "IXHD"))[:8]
            for e in drsp.children
            if e.tag == "IXCK" and child(e, "IXHD").payload is None
        }
        drsp.children = [
            e for e in drsp.children if not (e.tag in ("VXCK", "IXCK") and child(e, e.children[0].tag).payload is None)
        ]
        # The stubs' buffers must also list the host submodels as users.
        host_sub_uids = {}
        for e in drsp.children:
            if e.tag == "SBMC" and child(e, "SBBH").payload is not None:
                d = child(e, "SBBH").payload
                host_sub_uids.setdefault(bytes(d[16:24]), []).append(d[:8])
        for e in drsp.children:
            if e.tag == "VXCK":
                uid = child(e, "VXHD").payload[:8]
                users = host_sub_uids.get(uid, [])
                child(e, "VXDP").payload = b"".join(users) + bytes(len(users))
            if e.tag == "IXCK":
                users = [u for lst in host_sub_uids.values() for u in lst]
                child(e, "IXDP").payload = b"".join(users)
        mdid = next(e for e in drsp.children if e.tag == "MDID")
        uids = [mdid.payload[i : i + 8] for i in range(0, len(mdid.payload), 8)]
        mdid.payload = b"".join(u for u in uids if u not in host_vb_uids and u not in host_ib_uids)

        modl = child(child(full, "NONR"), "MODL")
        modl.payload = F.rebuild_modl(host_modl, part_lods, keep_other_parts=False)

        if args.complete:
            # Wheel hubs and the clean exterior entry, as in forza2dsf.
            wheel = [s for s in sections if s.name == "wheel"]
            wheelbase = float(misc.get("Wheelbase"))
            tyre = [
                s.vertices_for(sub)[i].position
                for s in wheel
                for sub in s.subsections
                if sub.lod == 1 and sub.material == "tire"
                for t in sub.triangles()
                for i in t
            ]
            width = max(p[0] for p in tyre) - min(p[0] for p in tyre)
            track = float(misc.get("FrontTrackOuter")) - width
            upvh = child(child(full, "NONR"), "UPVH")
            upvh.payload, hubs = F.patch_hubs(
                payload(stream, upvh), 0.1155 + wheelbase / 2, 0.1155 - wheelbase / 2, track / 2, F.HOST_HUB_Y
            )
            exterior = root.entries[{s: i for s, i, _ in records}[args.id + 2000]]
            ext_builder = F.Builder(b"\x00\x12\xbe\xef", layout=layout)
            ext_modl = child(child(exterior, "NONR"), "MODL")
            ext_template = payload(stream, ext_modl)
            ext_parts = struct.unpack_from("<I", ext_template)[0]
            ext_lods = {
                0: {
                    F.EXTERIOR_MASK: F.add_geometry(
                        ext_builder, body, 1, place, wheel=False, textures=tex_ext, merge=args.merge_submodels
                    )
                },
                ext_parts - 1: {F.EXTERIOR_MASK: F.add_geometry(ext_builder, wheel, 1, place, wheel=True)},
            }
            F.rebuild_drsp(
                stream, child(child(exterior, "MDXN"), "DRSP"), ext_builder, keep_host_materials=bool(args.textures)
            )
            ext_modl.payload = F.rebuild_modl(ext_template, ext_lods)
            print(f"complete: {hubs} hubs patched, exterior rebuilt")

        with args.out.open("wb") as out:
            write_chunk(out, stream, root.entries, root.version)
    print(f"hollowed {hollowed} host submodels; wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
