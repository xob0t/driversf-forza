"""The damage CLI (`uv run dsff damage ...`): inspection, renders and checks."""

from __future__ import annotations

import argparse
import colorsys
import math
import struct
import sys
from collections import Counter
from pathlib import Path

from dsfforza.convert.damage.core import BODY, _cross, _dot, _sub, load_mesh, role_name
from dsfforza.convert.damage.host import host_field, host_panels
from dsfforza.convert.damage.host_cache import _set_idle_priority
from dsfforza.convert.damage.tree import _read_tree_fields, _tree_fields
from dsfforza.convert.damage.uv import s9_pair
from dsfforza.formats import upvh as upvh_mod
from dsfforza.formats import vehicle_mesh as vm
from dsfforza.formats.chnk import read_chunk
from dsfforza.formats.vehicle_mesh import child, payload

# --- CLI: inspection and checks ---------------------------------------------------


def _entries(archive: Path, spool_id: int):
    stream = archive.open("rb")
    root = read_chunk(stream, 0)
    return stream, root, upvh_mod.entries_for(stream, root.entries, spool_id, None)


def cmd_panels(args) -> None:
    stream, _root, kinds = _entries(args.archive, args.id)
    with stream:
        full = kinds[0]
        nonr = child(full, "NONR")
        data = payload(stream, child(nonr, "UPVH"))
        h = upvh_mod.parse(data)
        panels = host_panels(data, payload(stream, child(nonr, "MODL")))
        f = host_field(stream, full, 4)
    counts = Counter(f.pairs)
    print(f"{full.name}: {len(panels)} panels; part 0 mask 4: {len(f.points)} vertices, box {f.box}")
    for pair, p in sorted(panels.items()):
        r = p.record
        hinge = ""
        if r.hinge >= 0:
            piv = struct.unpack_from("<3f", h.table("hinges")[r.hinge])
            axis = struct.unpack_from("<3f", h.table("hinges")[r.hinge], 16)
            limit, param = struct.unpack_from("<2f", h.table("hinges")[r.hinge], 32)
            hinge = (
                f"hinge {r.hinge} pivot ({piv[0]:.2f}, {piv[1]:.2f}, {piv[2]:.2f}) axis "
                f"({axis[0]:.0f}, {axis[1]:.0f}, {axis[2]:.0f}) limit {limit:.3f} param {param:g}"
            )
        ns = [n for n, q in enumerate(f.pairs) if q == pair]
        crush = max((math.dist((0, 0, 0), f.disp[n]) for n in ns), default=0)
        print(
            f"  s9 {pair[0]:2d},{pair[1]:2d}  rec {','.join(str(x.index) for x in p.records):6s} "
            f"{role_name(p.role):18s} part {p.part if p.part is not None else '-':>2}  verts {counts[pair]:5d} "
            f"max crush {crush:.3f}  {hinge}"
        )


def _colour(k: int):
    r, g, b = colorsys.hsv_to_rgb((k * 0.61803) % 1.0, 0.65, 1.0)
    return r, g, b


def render(tris, path: Path, size: int = 300) -> None:
    """Side (both), top, front and rear views; tris: [(3 points, rgb 0..1)] or
    [(3 points, None, 3 uvs, texel function uv -> rgb 0..1)] for per-pixel texturing."""
    xs = [p[0] for t in tris for p in t[0]]
    ys = [p[1] for t in tris for p in t[0]]
    zs = [p[2] for t in tris for p in t[0]]
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) * 1.05
    centre = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2)
    # (horizontal axis, flip, vertical axis, depth axis, depth sign): -x side, +x side, top, front, rear
    views = [(2, 1, 1, 0, 1), (2, -1, 1, 0, -1), (0, 1, 2, 1, -1), (0, -1, 1, 2, -1), (0, 1, 1, 2, 1)]
    width = size * len(views)
    pixels = bytearray([24, 24, 28] * width * size)
    light = (0.4, 0.8, 0.45)
    for view, (h, flip, v, d, sign) in enumerate(views):
        depth = [math.inf] * (size * size)
        for item in tris:
            tri, col = item[0], item[1]
            n = _cross(_sub(tri[1], tri[0]), _sub(tri[2], tri[0]))
            length = math.sqrt(_dot(n, n)) or 1
            shade = 0.3 + 0.7 * abs(_dot(n, light)) / length
            c = bytes(int(255 * shade * k) for k in col) if col is not None else None
            pts = [
                (
                    flip * (p[h] - centre[h]) / span * size + size / 2,
                    size / 2 - (p[v] - centre[v]) / span * size,
                    sign * (p[d] - centre[d]),
                )
                for p in tri
            ]
            x0, x1 = max(int(min(p[0] for p in pts)), 0), min(int(max(p[0] for p in pts)) + 1, size)
            y0, y1 = max(int(min(p[1] for p in pts)), 0), min(int(max(p[1] for p in pts)) + 1, size)
            (px, py, pz), (qx, qy, qz), (rx, ry, rz) = pts
            area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
            if abs(area) < 1e-9:
                continue
            for y in range(y0, y1):
                for x in range(x0, x1):
                    w0 = ((qx - x) * (ry - y) - (qy - y) * (rx - x)) / area
                    w1 = ((rx - x) * (py - y) - (ry - y) * (px - x)) / area
                    w2 = 1 - w0 - w1
                    if w0 < 0 or w1 < 0 or w2 < 0:
                        continue
                    z = w0 * pz + w1 * qz + w2 * rz
                    k = y * size + x
                    if z < depth[k]:
                        depth[k] = z
                        o = (y * width + view * size + x) * 3
                        if c is None:
                            uvs, texel = item[2], item[3]
                            u = w0 * uvs[0][0] + w1 * uvs[1][0] + w2 * uvs[2][0]
                            t = w0 * uvs[0][1] + w1 * uvs[1][1] + w2 * uvs[2][1]
                            pixels[o : o + 3] = bytes(min(255, int(255 * (0.4 + 0.6 * shade) * k)) for k in texel(u, t))
                        else:
                            pixels[o : o + 3] = c
    vm.write_png(path, width, size, pixels)


DECAL_TEXTURE = bytes.fromhex("00122b6db3fa1227")  # SSIC: dent normals (RGB) + scratch mask (alpha)


def decal_texel(stream_path: Path):
    """uv -> rgb of a preview of the shared damage decal: grey paint, white where
    the scratch mask (alpha) is dark, tinted by the dent normal map."""
    from dsfforza.convert import dsf_materials
    from dsfforza.formats.chnk import walk

    with stream_path.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = next(e for e in root.entries if e.tag == "SSIC")
        dds = next(
            payload(stream, child(e, "DXTC"))
            for _, e in walk(ssic.children)
            if e.tag == "TEXC" and payload(stream, child(e, "UIDC"))[:8] == DECAL_TEXTURE
        )
    w, h, rgba = dsf_materials.decode_dds(dds)

    def texel(u, v):
        x = min(w - 1, max(0, int(u * w)))
        y = min(h - 1, max(0, int(v * h)))
        r, g, _b, a = rgba[(y * w + x) * 4 : (y * w + x) * 4 + 4]
        scratch = 1 - a / 255
        base = (0.45 + 0.25 * (r / 255 - 0.5), 0.45 + 0.25 * (g / 255 - 0.5), 0.5)
        return tuple(base[k] * (1 - scratch) + scratch for k in range(3))

    return texel


TRIM_TEXTURE = bytes.fromhex("00122b73d1fa1227")  # SSIC: GLOBAL_Chassis(_Simple) diffuse
TRIM_MATERIALS = {bytes.fromhex("0012833b12fe1027"), bytes.fromhex("10122b9753b14e2a")}
CLASS_COLOURS = {
    bytes.fromhex("001283358afd1027"): (0.75, 0.35, 0.1),
    bytes.fromhex("10122b9653b14e2a"): (0.75, 0.35, 0.1),
    bytes.fromhex("001283369afd1027"): (0.3, 0.5, 0.7),
    bytes.fromhex("10122b9953b14e2a"): (0.3, 0.5, 0.7),
}


def shared_texel(stream_path: Path, uid: bytes):
    """uv -> grey (its red channel) of a shared SSIC texture, for --by trim."""
    from dsfforza.convert import dsf_materials
    from dsfforza.formats.chnk import walk

    with stream_path.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = next(e for e in root.entries if e.tag == "SSIC")
        dds = next(
            payload(stream, child(e, "DXTC"))
            for _, e in walk(ssic.children)
            if e.tag == "TEXC" and payload(stream, child(e, "UIDC"))[:8] == uid
        )
    w, h, rgba = dsf_materials.decode_dds(dds)

    def texel(u, v):
        x = min(w - 1, max(0, int(u % 1 * w)))
        y = min(h - 1, max(0, int(v % 1 * h)))
        r = rgba[(y * w + x) * 4] / 255
        return (r, r, r)

    return texel


def cmd_render(args) -> None:
    """Colour by MODL part or by s9 panel; --damage t draws lerp(position, s12, t)
    for every panel, as the shader does with fDamage = t."""
    stream, _root, kinds = _entries(args.archive, args.id)
    with stream:
        vehc = kinds[args.kind]
        mesh = load_mesh(stream, vehc)
    mask = args.mask or {0: 4, 1: 64, 2: 2}[args.kind]
    only = {int(x) for x in args.parts.split(",")} if args.parts else None
    skip = {int(x) for x in args.skip.split(",")} if args.skip else set()
    tris = []
    texel = decal_texel(stream_path=args.archive) if args.by == "decal" else None
    trim_texel = shared_texel(args.archive, TRIM_TEXTURE) if args.by == "trim" else None
    for p, part in enumerate(mesh.parts):
        if (only is not None and p not in only) or p in skip:
            continue
        for lod in part.lods:
            if lod.mask != mask:
                continue
            for n in lod.submodels:
                for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                    e12 = vb.element(12)
                    pts = []
                    for i in tri:
                        q = vm.position(vb, i)
                        if e12 is not None and args.damage:
                            d = vb.read(e12, i)
                            q = tuple(q[a] + args.damage * (d[a] - q[a]) for a in range(3))
                        pts.append(q)
                    if trim_texel is not None:
                        m = mesh.submodels[n].material
                        if m in TRIM_MATERIALS:
                            e8 = vb.element(8)
                            tris.append((pts, None, [vb.read(e8, i)[:2] for i in tri], trim_texel))
                        else:
                            tris.append((pts, CLASS_COLOURS.get(m, (0.55, 0.5, 0.45))))
                        continue
                    if texel is not None:
                        e8 = vb.element(8)
                        if e8 is not None and e8.components == 4:
                            tris.append((pts, None, [vb.read(e8, i)[2:4] for i in tri], texel))
                        else:
                            tris.append((pts, (0.35, 0.35, 0.4)))
                        continue
                    key = p if args.by == "part" else s9_pair(vb, tri[0])[1] * 7 + s9_pair(vb, tri[0])[0]
                    tris.append((pts, _colour(key)))
    if args.wheels:
        # The wheel model (20-byte buffers, centred on the origin) at every UPVH hub,
        # to see the body against its wheels.
        with args.archive.open("rb") as stream2:
            root2 = read_chunk(stream2, 0)
            kinds2 = upvh_mod.entries_for(stream2, root2.entries, args.id, None)
            h = upvh_mod.parse(payload(stream2, child(child(kinds2[0], "NONR"), "UPVH")))
        hubs = [v[:3] for v in h.vec4s("wheels")]
        wheel = []
        for part in mesh.parts:
            for lod in part.lods:
                if lod.mask != mask:
                    continue
                for n in lod.submodels:
                    for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                        if vb.stride == 20:
                            wheel.append([vm.position(vb, i) for i in tri])
        for hub in hubs:
            for t in wheel:
                tris.append(([tuple(p[a] + hub[a] for a in range(3)) for p in t], (0.25, 0.25, 0.25)))
    if not tris:
        raise SystemExit("nothing to render")
    render(tris, args.png, args.size)
    print(f"wrote {args.png} ({len(tris)} triangles)")


def cmd_check(args) -> None:
    """Structural damage checks for one vehicle's full and exterior entries."""
    stream, _root, kinds = _entries(args.archive, args.id)
    problems = 0

    def report(ok: bool, text: str) -> None:
        nonlocal problems
        problems += not ok
        print(("ok    " if ok else "FAIL  ") + text)

    with stream:
        full = kinds[0]
        nonr = child(full, "NONR")
        upvh = payload(stream, child(nonr, "UPVH"))
        panels = host_panels(upvh, payload(stream, child(nonr, "MODL")))
        meshes = {k: load_mesh(stream, kinds[k]) for k in (0, 2) if k in kinds}
    h = upvh_mod.parse(upvh)
    # Hosts 210 and 217 keep a vanilla exporter quirk on three headlight records.
    stray = [
        r.index
        for r, fields in zip(h.records, _tree_fields([r.raw for r in h.records]), strict=True)
        if _read_tree_fields(r.raw) != fields and r.role not in (50, 51)
    ]
    report(not stray, f"records' +0x0C bit 0, +0x16, +0x18 follow the tree (_tree_fields; not: {stray})")
    # Rigid panels are drawn through the chassis' transform slot (DamagePlan.bound).
    chassis_slot = 0 if (0, 0) in panels else min(panels)[0]
    drawn = {pair: (chassis_slot, pair[1]) if panel.role in BODY else pair for pair, panel in panels.items()}
    known = set(panels) | set(drawn.values())
    for kind, mesh in meshes.items():
        mask = 4 if kind == 0 else 2
        part0 = set()
        pairs = Counter()
        moved = total = 0
        for lod in mesh.parts[0].lods:
            if lod.mask != mask:
                continue
            for n in lod.submodels:
                for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                    e12 = vb.element(12)
                    part0.add((vb.uid,) + tuple(sorted(tri)))
                    for i in tri:
                        pairs[s9_pair(vb, i)] += 1
                        if e12 is not None:
                            total += 1
                            moved += math.dist(vm.position(vb, i), vb.read(e12, i)[:3]) > 0.005
        unknown = {p: c for p, c in pairs.items() if p not in known}
        report(
            not unknown, f"kind {kind}: every part-0 s9 pair is a host panel ({len(pairs)} pairs, unknown {unknown})"
        )
        report(len(pairs) > 3, f"kind {kind}: part 0 uses {len(pairs)} panels")
        report(total and moved / total > 0.2, f"kind {kind}: {moved} of {total} part-0 vertex uses have a crush target")
        if kind != 0:
            continue
        for pair, panel in sorted(panels.items()):
            if panel.part in (None, 0):
                continue
            tris = set()
            bad = 0
            for lod in mesh.parts[panel.part].lods:
                if lod.mask != mask:
                    continue
                for n in lod.submodels:
                    for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                        tris.add((vb.uid,) + tuple(sorted(tri)))
                        bad += any(s9_pair(vb, i) not in (pair, drawn[pair]) for i in tri)
            if tris:
                report(
                    not bad and tris <= part0,
                    f"part {panel.part} ({role_name(panel.role)}): {len(tris)} triangles, all on panel {drawn[pair]} "
                    f"({bad} not) and all in part 0 ({len(tris - part0)} not)",
                )
    print(f"{problems} problem(s)")
    if problems:
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("panels", help="the host's panel records, pairs, parts, hinges and crush")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    p = sub.add_parser("render", help="render coloured by MODL part or s9 panel, optionally crushed")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--png", type=Path, required=True)
    p.add_argument("--kind", type=int, default=0, choices=(0, 1, 2))
    p.add_argument("--mask", type=int)
    p.add_argument(
        "--by",
        choices=("part", "panel", "decal", "trim"),
        default="part",
        help="decal: the shared scratch/dent decal at UV1 (white = scratch where fully damaged); "
        "trim: shared trim textured from the global chassis texture at UV0, paint orange, glass blue",
    )
    p.add_argument("--damage", type=float, default=0.0, help="fDamage for every panel (0 intact, 1 crushed)")
    p.add_argument("--parts", help="only these MODL parts")
    p.add_argument("--skip", help="leave these MODL parts out")
    p.add_argument("--size", type=int, default=300)
    p.add_argument("--wheels", action="store_true", help="also draw the wheel model at every UPVH hub")
    p = sub.add_parser("check", help="structural damage checks of a (converted) vehicle")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)
    _set_idle_priority()
    {"panels": cmd_panels, "render": cmd_render, "check": cmd_check}[args.command](args)
