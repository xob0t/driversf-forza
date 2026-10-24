"""Decode and export the geometry of one VEHC entry in dngvehicles.sp.

Layout inside VEHC/MDXN/DRSP (see research/vehicle-model.md):

    VXCK  VXHD {u64 uid, u32 vertex_count, u32 stride}
          VXDP dependency UIDs
          VXDT vertices
          VDCL {u32 count, count x {u32 semantic, u32 components, u32 type, u32 name_hash}}
    IXCK  IXHD {u64 uid, u32 index_count, u32 index_size, u64 ?}
          IXDP dependency UIDs
          IXDT indices (u16, triangle lists)
    SBMC  SBBH {u64 uid, u64 material_uid, u64 vertex_buffer_uid, u64 0,
                u32 base_vertex, u8[12] 0, u64 index_buffer_uid,
                u32 first_index, u32 index_count, u64 0}

Positions are the first VDCL element: int16 x, y, z (and w) in 1/4096 m.

Usage:
    uv run dsff vehicle_mesh <archive> <vehc_index> --obj out/car.obj --png out/car.png
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza.formats.chnk import Entry, read_chunk

POSITION_SCALE = 1 / 4096
# VDCL type code -> (struct format per component, bytes per component)
COMPONENT_TYPES = {1: ("h", 2), 2: ("B", 1), 6: ("B", 1), 8: ("e", 2)}


@dataclass
class VertexElement:
    semantic: int
    components: int
    type: int
    name_hash: int
    offset: int


@dataclass
class VertexBuffer:
    uid: bytes
    count: int
    stride: int
    elements: list[VertexElement]
    data: bytes

    def element(self, semantic: int) -> VertexElement | None:
        return next((e for e in self.elements if e.semantic == semantic), None)

    def read(self, element: VertexElement, index: int) -> tuple:
        fmt, _ = COMPONENT_TYPES[element.type]
        return struct.unpack_from(f"<{element.components}{fmt}", self.data, index * self.stride + element.offset)


@dataclass
class SubModel:
    uid: bytes
    material: bytes
    vertex_buffer: bytes
    base_vertex: int
    index_buffer: bytes
    first_index: int
    index_count: int


@dataclass
class Lod:
    mask: int  # 4, 8, 16 (high to low, presumably) or 1 (presumably shadow)
    submodels: list[int]  # indices into Mesh.submodels
    extra: list[int]  # parallel u32 per submodel, meaning unknown


@dataclass
class Part:
    uid: bytes
    box: tuple[float, ...]  # origin xyz, then three axis vectors (xyz each)
    lods: list[Lod]
    trailer: int  # u32 at record +0x5C, 5 in Saloon 15


@dataclass
class Mesh:
    vertex_buffers: dict[bytes, VertexBuffer] = field(default_factory=dict)
    index_buffers: dict[bytes, bytes] = field(default_factory=dict)
    submodels: list[SubModel] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)


def parse_modl(data: bytes, submodels: list[SubModel]) -> list[Part]:
    """MODL: {u32 parts, u32 lods, u32 lod_table, u32 0}, parts x 0x60,
    lods x {u32 mask, u32 count, u32 uid_list_offset, u32 extra_offset}, then
    the submodel UID list (u64) and a parallel u32 list."""
    part_count, _, _, _ = struct.unpack_from("<4I", data)
    by_uid = {sub.uid: n for n, sub in enumerate(submodels)}
    parts = []
    for p in range(part_count):
        r = 0x10 + p * 0x60
        floats = struct.unpack_from("<16f", data, r)
        box = floats[0:3] + floats[4:7] + floats[8:11] + floats[12:15]
        uid = data[r + 0x40:r + 0x48]
        lod_count, lod_offset = struct.unpack_from("<II", data, r + 0x48)
        trailer, = struct.unpack_from("<I", data, r + 0x5C)
        lods = []
        for k in range(lod_count):
            mask, count, uid_offset, extra_offset = struct.unpack_from("<4I", data, lod_offset + k * 16)
            subs = [by_uid[data[uid_offset + i * 8:uid_offset + i * 8 + 8]] for i in range(count)]
            extra = list(struct.unpack_from(f"<{count}I", data, extra_offset))
            lods.append(Lod(mask, subs, extra))
        parts.append(Part(uid, box, lods, trailer))
    return parts


def payload(stream, entry: Entry) -> bytes:
    """A leaf's bytes: its edited payload if one was set, else the source file's."""
    if entry.payload is not None:
        return entry.payload
    stream.seek(entry.offset)
    return stream.read(entry.size)


def child(entry: Entry, tag: str) -> Entry:
    return next(c for c in entry.children if c.tag == tag)


def parse_vdcl(data: bytes) -> list[VertexElement]:
    count, = struct.unpack_from("<I", data)
    elements, offset = [], 0
    for i in range(count):
        semantic, components, type_, name_hash = struct.unpack_from("<4I", data, 4 + i * 16)
        if type_ not in COMPONENT_TYPES:
            raise ValueError(f"unknown VDCL type {type_}")
        elements.append(VertexElement(semantic, components, type_, name_hash, offset))
        offset += components * COMPONENT_TYPES[type_][1]
    return elements


def load(archive: Path, vehc_index: int) -> tuple[Entry, Mesh]:
    mesh = Mesh()
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        vehc = root.entries[vehc_index]
        drsp = child(child(vehc, "MDXN"), "DRSP")
        for entry in drsp.children:
            if entry.tag == "VXCK":
                header = payload(stream, child(entry, "VXHD"))
                uid, count, stride = header[:8], *struct.unpack_from("<II", header, 8)
                elements = parse_vdcl(payload(stream, child(entry, "VDCL")))
                if elements[-1].offset + elements[-1].components * COMPONENT_TYPES[elements[-1].type][1] != stride:
                    raise ValueError(f"VDCL doesn't add up to stride {stride}")
                mesh.vertex_buffers[uid] = VertexBuffer(uid, count, stride, elements,
                                                        payload(stream, child(entry, "VXDT")))
            elif entry.tag == "IXCK":
                header = payload(stream, child(entry, "IXHD"))
                uid, count, size = header[:8], *struct.unpack_from("<II", header, 8)
                if size != 2:
                    raise ValueError(f"index size {size}")
                mesh.index_buffers[uid] = payload(stream, child(entry, "IXDT"))[:count * 2]
            elif entry.tag == "SBMC":
                d = payload(stream, child(entry, "SBBH"))
                base_vertex, = struct.unpack_from("<I", d, 0x20)
                first, count = struct.unpack_from("<II", d, 0x38)
                mesh.submodels.append(SubModel(d[0:8], d[8:16], d[16:24], base_vertex, d[0x30:0x38], first, count))
        modl = child(child(vehc, "NONR"), "MODL")
        mesh.parts = parse_modl(payload(stream, modl), mesh.submodels)
    return vehc, mesh


def lod_submodels(mesh: Mesh, mask: int) -> list[SubModel]:
    """Submodels of every part's LOD with this mask (parts without it are skipped)."""
    chosen = []
    for part in mesh.parts:
        for lod in part.lods:
            if lod.mask == mask:
                chosen.extend(mesh.submodels[n] for n in lod.submodels)
    return chosen


def triangles(mesh: Mesh, submodel: SubModel):
    """Yield triangles as three (vertex_buffer, vertex_index) pairs."""
    vb = mesh.vertex_buffers[submodel.vertex_buffer]
    indices = mesh.index_buffers[submodel.index_buffer]
    for t in range(submodel.first_index, submodel.first_index + submodel.index_count, 3):
        a, b, c = struct.unpack_from("<3H", indices, t * 2)
        yield vb, (submodel.base_vertex + a, submodel.base_vertex + b, submodel.base_vertex + c)


def position(vb: VertexBuffer, index: int) -> tuple[float, float, float]:
    x, y, z = vb.read(vb.elements[0], index)[:3]
    return x * POSITION_SCALE, y * POSITION_SCALE, z * POSITION_SCALE


def write_obj(mesh: Mesh, path: Path, uv_semantic: int | None, submodels: list[SubModel]) -> None:
    with path.open("w") as out:
        out.write("# exported by src/dsfforza/formats/vehicle_mesh.py; positions in metres\n")
        base = 1
        for sub in submodels:
            n = mesh.submodels.index(sub)
            vb = mesh.vertex_buffers[sub.vertex_buffer]
            uv_element = vb.element(uv_semantic) if uv_semantic is not None else None
            remap: dict[int, int] = {}
            faces = []
            for _, tri in triangles(mesh, sub):
                for i in tri:
                    if i not in remap:
                        remap[i] = len(remap)
                faces.append(tri)
            out.write(f"o sub{n:03d}_{sub.uid.hex()}_mat{sub.material.hex()}\n")
            for i in remap:
                out.write("v {:.5f} {:.5f} {:.5f}\n".format(*position(vb, i)))
            if uv_element:
                for i in remap:
                    u, v = vb.read(uv_element, i)[:2]
                    out.write(f"vt {u:.5f} {1 - v:.5f}\n")
            for tri in faces:
                ids = [base + remap[i] for i in tri]
                if uv_element:
                    out.write("f {0}/{0} {1}/{1} {2}/{2}\n".format(*ids))
                else:
                    out.write("f {} {} {}\n".format(*ids))
            base += len(remap)


def write_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    rows = b"".join(b"\0" + bytes(pixels[y * width * 3:(y + 1) * width * 3]) for y in range(height))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def render(mesh: Mesh, path: Path, submodels: list[SubModel], size: int = 360) -> None:
    """Flat-shaded orthographic side, top and front views, z-buffered."""
    tris = []
    for sub in submodels:
        for vb, tri in triangles(mesh, sub):
            tris.append([position(vb, i) for i in tri])
    if not tris:
        raise ValueError("nothing to render")
    xs = [p[0] for t in tris for p in t]; ys = [p[1] for t in tris for p in t]; zs = [p[2] for t in tris for p in t]
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) * 1.05
    centre = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2)
    # (horizontal axis, vertical axis, depth axis, depth sign) per view
    views = [(2, 1, 0, 1), (0, 2, 1, -1), (0, 1, 2, 1)]
    width, height = size * len(views), size
    pixels = bytearray([24, 24, 28] * width * height)
    light = (0.4, 0.8, 0.45)
    for view, (h, v, d, sign) in enumerate(views):
        depth = [math.inf] * (size * size)
        for tri in tris:
            ax, ay, az = (tri[1][i] - tri[0][i] for i in range(3))
            bx, by, bz = (tri[2][i] - tri[0][i] for i in range(3))
            nx, ny, nz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
            shade = 60 + int(180 * abs(nx * light[0] + ny * light[1] + nz * light[2]) / length)
            pts = [((p[h] - centre[h]) / span * size + size / 2,
                    size / 2 - (p[v] - centre[v]) / span * size,
                    sign * (p[d] - centre[d])) for p in tri]
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
                        pixels[o:o + 3] = bytes((shade, shade, min(255, shade + 12)))
    write_png(path, width, height, pixels)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument("vehc", type=int, help="root VEHC index, e.g. 3 for Saloon 15 variant 0")
    parser.add_argument("--obj", type=Path, help="write all submodels as one OBJ, one object per submodel")
    parser.add_argument("--uv", type=int, default=8, help="VDCL semantic to export as UVs (default 8)")
    parser.add_argument("--png", type=Path, help="render a side/top/front preview")
    parser.add_argument("--submodels", help="comma-separated submodel numbers (default all)")
    parser.add_argument("--lod", type=int, help="only submodels in LODs with this MODL mask (4, 8, 16, 1)")
    parser.add_argument("--summary", action="store_true", help="print buffers, submodels and parts")
    args = parser.parse_args(argv)

    vehc, mesh = load(args.archive, args.vehc)
    print(f"VEHC {args.vehc} '{vehc.name}': {len(mesh.vertex_buffers)} vertex buffers, "
          f"{len(mesh.index_buffers)} index buffers, {len(mesh.submodels)} submodels, {len(mesh.parts)} parts")
    chosen = mesh.submodels
    if args.lod is not None:
        chosen = lod_submodels(mesh, args.lod)
    if args.submodels:
        chosen = [mesh.submodels[int(n)] for n in args.submodels.split(",")]
    if args.summary:
        for p, part in enumerate(mesh.parts):
            size = [round(math.dist((0, 0, 0), part.box[i:i + 3]), 2) for i in (3, 6, 9)]
            lods = " ".join(f"m{lod.mask}:{len(lod.submodels)}" for lod in part.lods)
            print(f"  part{p:02d} {part.uid.hex()} origin {tuple(round(x, 2) for x in part.box[:3])} "
                  f"axes {size} lods [{lods}] trailer {part.trailer}")
        for vb in mesh.vertex_buffers.values():
            layout = ", ".join(f"s{e.semantic}:{e.components}x t{e.type}@{e.offset}" for e in vb.elements)
            print(f"  VB {vb.uid.hex()} {vb.count} x {vb.stride}B  [{layout}]")
        for n, sub in enumerate(mesh.submodels):
            print(f"  sub{n:03d} {sub.uid.hex()} mat {sub.material.hex()} vb {sub.vertex_buffer.hex()[:8]} "
                  f"base {sub.base_vertex} idx {sub.first_index}+{sub.index_count}")
    if args.obj:
        write_obj(mesh, args.obj, args.uv, chosen)
        print(f"wrote {args.obj}")
    if args.png:
        render(mesh, args.png, chosen)
        print(f"wrote {args.png}")


if __name__ == "__main__":
    sys.exit(main())
