"""Anark Gameface scenes of Forza's digital dashboards (FM4, FH1).

Each car's DigitalGauge/ folder holds Dash_<car>.bsg (scene graph), .fbf (objects,
materials, meshes) and .bgf (behaviours, slides, animation tracks), plus per-car
textures (.xds); shared dashboard textures are in Media/cars/Shared.zip
(dashboards/) and the vector fonts (dg1..dg5_vector_aa.dt) in Media/UI/Fonts.zip.
All big-endian. The game draws the scene into the render target the cockpit's
digital_gauge shader samples (research/cockpit.md, "Digital displays").

Name hashes: h = (h + c) * 65599 over the bytes from 0 (h32); property keys keep
the low 24 bits (h24) with a type tag in the top byte.

.bsg, .fbf:
The three files share one object-id space (u32 ids); the .bgf's element table
repeats the tree with the behaviour/slide data.

.bsg  scene graph ("FGBkranA" = "AnarkBGF" reversed; parse_bsg, all fields confirmed by full parse
      of 31 files):
      0x00 u8[8] (01 04 00 00 00 00 00 18) | "FGBkranA" | u32 2, 2, 0, 0, 8 | u32 n_nodes |
      u32 n_extra | u32 n_nodes*0x60 | n_nodes x 0x60-byte node | u32 n_extra*5 | n_extra x (u32 id, u8 kind)
      node: i32 parent INDEX (-1 root) | u32 id | u8 kind (1 group, 2 model, 3 camera, 4 light,
      5 text, 8 layer) + 3 pad | f32 pos[3] | f32 rot[3] (radians, Euler) | f32 scale[3] |
      f32 zero[3] | f32 bbox_min[3] | f32 bbox_max[3] (empty box = (1,1,1)..(-1,-1,-1)) |
      f32 opacity (0..1) | u8 flags[8] (byte 7 = 1 on text and some models, meaning unknown).
      extra: materials (kind 6) and images (kind 7) that are not nodes.
      NB: 'pos' is the bgf position when the pivot is 0, else -pivot; the bgf values win.

.fbf  objects + meshes (parse_fbf): u32 0x3f0 | u32 n | n objects | mesh section.
      object: u32 type | u32 id | u32 len + name | body by type:
        0 Group   (no body)
        1 Text    u32 0, str text (authored placeholder, e.g. '888'), str (empty; 'NLOC' once), f32 0.25, f32 0.25,
                  str font ('dg2_lcd bold', 'dg5_quartzms', ...), u32 size, u32 halign (0 left,
                  1 centre, 2 right), u32 valign (1 = centre, all files), u32 d, u32 e,
                  f32 tracking, f32 g, u32 h, u8 1, [f32 if text non-empty], u8 0
        2 Camera  f32[16] (identity-ish, 600 at [14])      3 Layer f32[6] (0,0,0,0,1,1)
        4 Model   u32 mesh index, f32 (= opacity)
        5 Material f32 colour0[4] (THE authored colour, Anark 'diffuse'; bgf diffuse.r/g/b 0..255
                  override it), f32[4] (always 0.5 grey), f32[4] (0), f32[4] (0,0,0,1), u32 0,
                  u32 owner model id, u32[8] (0,2,0,4,6,7,0,2 in all 529)
        6 Light   u32 3, f32[25]
        7 Image   u32 material id, str 'GAME:\\MEDIA\\CARS\\...\\X.TGA' (on disc: X.xds in the car's
                  DigitalGauge/ or Media/cars/Shared.zip dashboards/), f32[16] uv matrix
                  (1,0,0,0, 0,-1,0,0, 0,0,1,0, 0,1,0,1: v' = 1 - v), f32[8] (?, positionu, positionv,
                  scaleu, scalev, ...; the matrix is s = scaleu * (u + positionu), confirmed on the
                  FH1 Zagato's animated tach mask), u32[5] (3|4, 2, 1, wrapU, wrapV)
      mesh section: u32 n | u32 total verts | u32 vertex+index bytes | n x 96-byte header (u32 vertex
      count, ..., bbox) | n x 36-byte index header | vertex/index data. 528 of 529 meshes are the
      same 100 x 100 rectangle (x,y in -50..50, 3x3 grid, both sides, pos3 normal3 uv2, v up);
      others: an old Celica needle (8 verts, 3D) and FH1 Zagato's tach arc (184 verts). Header word 7
      (& ~3) = vertex offset in the data blob, word 8 = format (0x10000242 pos3 nrm3 uv2); index
      header: u32 count, ..., u32 byte offset, u32 bytes (u16 triangle list). Model.mesh indexes it.

.bgf:
Behaviours, slides and animation (parse_bgf reads all 376 scenes on the discs to
the last byte).

header  u8 01 04 00 00 | u16 26 | "AnarkBGF" | u32 3 | u32 0 | u16 1280, u16 720 (presentation size,
        the same in every file) | u16 0x0100 | u32[7] (sizes/counts) ... | @0x88 u32 string-table
        bytes | strings (u32 len + bytes): binding paths ("parent.Layer.TEXT.TEXT_SPEED"),
        behaviour params ("contract", "UIContract", "PROGRESS_BAR", "parent"), fonts, texts.
elements u32 bytes | records: u32 name hash | i32 parent ELEMENT INDEX | u32 object id (fbf/bsg) |
        u8 type (1 node/group/layer/model, 2 camera, 3 light, 4 text, 5 material, 6 image,
        7 component = a node with its own timeline, 8 behaviour) | type 7: u8 5, u8 2, u32 time,
        u32 n | else: u8 1, u32 n | n x (u32 property hash, 4-byte value).
        Values: f32 for transforms/colours (opacity and colours 0..100 / 0..255), u32 string index
        for string-typed keys, ints for enums.
tracks  u32 bytes + (u32 element index, u32 0) per behaviour | u32 bytes | tracks: u32 element
        index | u32 property hash | u16 0x0100 | u16 n keys | n x (u32 time ms, f32 value, u32 0,
        f32 out-handle, u32 0, f32 in-handle of the next key): cubic Bezier in value, linear in time.
events  u32 bytes | per element: u32 elem, u32 n | (u32 event hash, u32 n | n x (u32 src, u32 dst,
        u32 action hash, u32 arg hash, u32 arg)). Only on the shared MFD template scenes (Panoz,
        Viper, M3 GTR, FXX, CCGT); names not cracked.
slides  u32 0 | u32 bytes | slides: char name[16] ("Master Slide", "Slide1", "PROGRESS", "FLASH",
        "ON", "OFF") | u32 component element index | u8 mode (0 = plays/loops, 9 = held) | u32 start |
        u32 duration ms | u32 n | entries: u32 elem | u8 in-slide | u32 n props + (hash, value) |
        u32 n tracks + (u16 enabled, u16 track index).
Name hashes (h32, h24): h = (h + c) * 65599 over the bytes; property keys keep the low 24 bits
(top byte = a type tag).

Fonts:
Forza's vector fonts (FM4 Media/UI/Fonts.zip, FH1 media/ui/Fonts.zip:
dg1..dg5_vector_aa.dt; fontmap.xml maps 'DG2_lcd' -> DG2 etc.; the dash scenes name them
'dg1_arial bold', 'dg2_lcd bold', 'dg3_moire bold', 'dg4_pf_ronda_seven', 'dg5_quartzms').

'CAFF21.11.05.0034' container: section table (.data size at 0x60, .gpu after it); .data starts
with 'vfont 12.07.06.0035'. vfont: ... u32 vertex bytes, u32 vertex count, u32 index count, f32
space width, f32 14.0, f32 'baseline offset', i32 win/typo/mac ascent/descent, line gap, EM size
(2048 or 1000; the FH1 .dt.log.xml files name these), u32 32 | char map (u16 char, u16 glyph) |
glyph records (40 bytes): u32 char, f32 advance (em), u16 0xffff, u16, u32 vertex BYTE offset,
u32 vertex count, u32 first index, u32 triangle count, f32, f32 0, f32 1.
.gpu: vertices (4 x f16: x, y in em with y up, u, v) then u16 indices. Character set of every DG
font: space , - . / 0-9 : D N P R.
Each glyph mesh has two halves: x > 0 is the glyph (drawn), x < 0 a mirrored complement inside
the glyph box (not drawn here; purpose unknown). (u, v): flat triangles u = 0, v a 0..1 edge
ramp (v = 0 on the outline); curve triangles (u, v) = (-1,1), (0,-1), (1,1): quadratic, inside
where u*u - v <= 0. That rule is exact for the straight-edged LCD fonts (dg2, dg4, dg5); the
curved ones (dg1, dg3) keep small artifacts in counters (not solved). The space glyph holds a
large box: not drawn."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np


def h32(s: str) -> int:
    h = 0
    for c in s.encode("latin-1"):
        h = ((h + c) * 65599) & 0xFFFFFFFF
    return h


def h24(s: str) -> int:
    return h32(s) & 0xFFFFFF


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.d = data
        self.p = pos

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from(">H", self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self):
        v = struct.unpack_from(">I", self.d, self.p)[0]
        self.p += 4
        return v

    def i32(self):
        v = struct.unpack_from(">i", self.d, self.p)[0]
        self.p += 4
        return v

    def f32(self, n=None):
        if n is None:
            v = struct.unpack_from(">f", self.d, self.p)[0]
            self.p += 4
            return v
        v = struct.unpack_from(f">{n}f", self.d, self.p)
        self.p += 4 * n
        return list(v)

    def str(self):
        n = self.u32()
        if n > 4096:
            raise ValueError(f"string length {n} at {self.p - 4:#x}")
        s = self.d[self.p : self.p + n].decode("latin-1")
        self.p += n
        return s

    def bytes(self, n):
        v = self.d[self.p : self.p + n]
        self.p += n
        return v

    def eof(self):
        return self.p >= len(self.d)


# ---------------------------------------------------------------- .bsg (scene graph)

BSG_KIND = {1: "Group", 2: "Model", 3: "Camera", 4: "Light", 5: "Text", 8: "Layer"}


@dataclass
class Node:
    index: int
    parent: int  # index into the node list, -1 for the root
    id: int
    kind: int
    pos: list
    rot: list  # radians, (x, y, z)
    scale: list
    pivot: list
    bmin: list
    bmax: list
    opacity: float
    flags: bytes
    children: list = field(default_factory=list)


def parse_bsg(data: bytes):
    r = Reader(data)
    hdr = r.bytes(8)
    magic = r.bytes(8)
    assert magic == b"FGBkranA", magic
    h = [r.u32() for _ in range(5)]  # 2, 2, 0, 0, 8
    n_nodes = r.u32()
    n_extra = r.u32()
    nodes_size = r.u32()
    assert nodes_size == n_nodes * 0x60, (nodes_size, n_nodes)
    nodes = []
    for i in range(n_nodes):
        parent = r.i32()
        nid = r.u32()
        kind = r.u8()
        r.bytes(3)
        pos = r.f32(3)
        rot = r.f32(3)
        scale = r.f32(3)
        pivot = r.f32(3)
        bmin = r.f32(3)
        bmax = r.f32(3)
        opacity = r.f32()
        flags = r.bytes(8)
        nodes.append(Node(i, parent, nid, kind, pos, rot, scale, pivot, bmin, bmax, opacity, flags))
    extra_size = r.u32()
    assert extra_size == n_extra * 5, (extra_size, n_extra)
    extras = []
    for _ in range(n_extra):
        extras.append((r.u32(), r.u8()))
    assert r.eof(), (r.p, len(data))
    for n in nodes:
        if n.parent >= 0:
            nodes[n.parent].children.append(n.index)
    return {"header": (hdr, h), "nodes": nodes, "extras": extras}


# ---------------------------------------------------------------- .fbf (objects, materials, meshes)

FBF_TYPE = {0: "Group", 1: "Text", 2: "Camera", 3: "Layer", 4: "Model", 5: "Material", 6: "Light", 7: "Image"}


@dataclass
class Obj:
    type: int
    id: int
    name: str
    off: int
    props: dict = field(default_factory=dict)

    @property
    def type_name(self):
        return FBF_TYPE.get(self.type, f"t{self.type}")


def _text(r: Reader):
    p = {}
    p["a"] = r.u32()
    p["text"] = r.str()
    p["loc"] = r.str()  # usually empty; "NLOC" on FH1 AST_V12Zagato_12 (no-localise tag?)
    p["f1"] = r.f32(2)
    p["font"] = r.str()
    p["size"] = r.u32()
    p["halign"] = r.u32()
    p["valign"] = r.u32()
    p["d"] = r.u32()
    p["e"] = r.u32()
    p["tracking"] = r.f32()
    p["g"] = r.f32()
    p["h"] = r.u32()
    p["flag"] = r.u8()
    # An f32 follows (a cached text width?) when the authored text is non-empty, and on a few
    # empty ones (FH1 PAG_ZondaR_10): take it when what follows would not start an object.
    if p["text"] or not _object_at(r.d, r.p + 1):
        p["text_width"] = r.f32()
    p["tail"] = r.u8()
    return p


def _object_at(d: bytes, p: int) -> bool:
    if p == len(d):
        return True
    if p + 12 > len(d):
        return False
    t, oid, n = struct.unpack_from(">III", d, p)
    if t <= 7 and oid < 1 << 20 and 0 < n < 200 and p + 12 + n <= len(d):
        return all(32 <= c < 127 for c in d[p + 12 : p + 12 + n])
    # the mesh section follows the last object: u32 count, u32 vertices, u32 bytes
    return t < 1000 and oid < 1 << 20 and n < 1 << 24 and t > 0 and False


def _material(r: Reader):
    p = {}
    p["ambient"] = r.f32(4)
    p["diffuse"] = r.f32(4)
    p["specular"] = r.f32(4)
    p["emissive"] = r.f32(4)
    p["u0"] = r.u32()
    p["owner"] = r.u32()
    p["ints"] = [r.u32() for _ in range(8)]
    return p


def _image(r: Reader):
    p = {}
    p["material"] = r.u32()
    p["path"] = r.str()
    p["matrix"] = r.f32(16)
    p["f"] = r.f32(8)
    p["ints"] = [r.u32() for _ in range(5)]
    return p


def _light(r: Reader):
    return {"raw": r.f32(26)}


def _camera(r: Reader):
    return {"raw": r.f32(16)}


def _layer(r: Reader):
    return {"raw": r.f32(6)}


def _model(r: Reader):
    return {"mesh": r.u32(), "f": r.f32()}


FBF_BODY = {0: lambda r: {}, 1: _text, 2: _camera, 3: _layer, 4: _model, 5: _material, 6: _light, 7: _image}


def parse_fbf(data: bytes):
    r = Reader(data)
    ver = r.u32()
    n = r.u32()
    objs = []
    for _ in range(n):
        off = r.p
        t = r.u32()
        oid = r.u32()
        name = r.str()
        o = Obj(t, oid, name, off)
        if t not in FBF_BODY:
            raise ValueError(f"unknown type {t} at {off:#x} ({name})")
        o.props = FBF_BODY[t](r)
        objs.append(o)
    mesh_off = r.p
    meshes = parse_meshes(r)
    return {"version": ver, "objects": objs, "mesh_off": mesh_off, "meshes": meshes, "end": r.p, "size": len(data)}


def parse_meshes(r: Reader):
    n = r.u32()
    nv = r.u32()
    vbytes = r.u32()
    heads = []
    for _ in range(n):
        h = {}
        h["a"] = r.u32()
        h["b"] = r.u32()
        h["c"] = r.u32()
        h["bmin"] = r.f32(4)
        h["bmax"] = r.f32(4)
        h["raw"] = r.bytes(96 - 44)
        w = struct.unpack(">13I", h["raw"])
        h["voff"] = w[7] & ~3  # byte offset of the vertices in the data blob
        h["fmt"] = w[8]
        heads.append(h)
    # word 1 of a mesh header = its number of index buffers (2 on some Reventon meshes)
    iheads = []
    for _ in range(sum(h["b"] for h in heads)):
        w = [r.u32() for _ in range(9)]
        iheads.append({"nidx": w[0], "ioff": w[7], "ibytes": w[8]})
    blob = r.bytes(vbytes)
    k = 0
    for h in heads:
        mine = iheads[k : k + h["b"]]
        k += h["b"]
        nvert = h["a"]
        first = min(ih["ioff"] for ih in mine) if mine else h["voff"]
        stride = (first - h["voff"]) // nvert if nvert else 0
        h["stride"] = stride
        verts = []
        for i in range(nvert if stride >= 12 else 0):
            f = struct.unpack_from(f">{stride // 4}f", blob, h["voff"] + i * stride)
            uv = f[-2:] if stride >= 20 else (0.0, 0.0)
            nrm = f[3:6] if stride >= 32 else (0.0, 0.0, 1.0)
            verts.append((f[0], f[1], f[2], uv[0], uv[1], nrm[2]))
        h["verts"] = verts
        h["tris"] = []
        for ih in mine:
            idx = struct.unpack_from(f">{ih['nidx']}H", blob, ih["ioff"])
            h["tris"] += [idx[i : i + 3] for i in range(0, len(idx) - 2, 3)]
    return {"n": n, "nverts": nv, "vbytes": vbytes, "heads": heads, "rest_off": r.p}


# ---------------------------------------------------------------- .bgf


@dataclass
class Elem:
    index: int
    off: int
    hash: int
    parent: int
    id: int
    type: int
    a: int
    b: int = 0
    t: int = 0
    props: list = field(default_factory=list)  # (hash, raw4)


def read_strings(d):
    (size,) = struct.unpack_from(">I", d, 0x88)
    p = 0x8C
    end = p + size
    out = []
    while p < end:
        (n,) = struct.unpack_from(">I", d, p)
        out.append(d[p + 4 : p + 4 + n].decode("latin-1"))
        p += 4 + n
    assert p == end
    return out, end


def _parse_elements(d: bytes):
    assert d[6:14] == b"AnarkBGF", d[:16]
    strs, p = read_strings(d)
    (size,) = struct.unpack_from(">I", d, p)
    p += 4
    stop = p + size
    elems = []
    while p < stop:
        h, par, oid, typ = struct.unpack_from(">IiIB", d, p)
        e = Elem(len(elems), p, h, par, oid, typ, 0)
        q = p + 13
        if typ == 7:
            e.a, e.b, e.t, n = struct.unpack_from(">BBII", d, q)
            q += 10
        else:
            e.a, n = struct.unpack_from(">BI", d, q)
            q += 5
        for _ in range(n):
            e.props.append((struct.unpack_from(">I", d, q)[0], d[q + 4 : q + 8]))
            q += 8
        elems.append(e)
        p = q
    assert p == stop, (hex(p), hex(stop))
    return {"strings": strs, "elems": elems, "rest": p}


def parse_tracks(d: bytes, p: int):
    """After the element table: u32 nbytes + (u32 elem, u32 x) pairs, u32 nbytes + tracks."""
    (n,) = struct.unpack_from(">I", d, p)
    p += 4
    behaviours = [struct.unpack_from(">II", d, p + 8 * i) for i in range(n // 8)]
    p += n
    (size,) = struct.unpack_from(">I", d, p)
    p += 4
    stop = p + size
    tracks = []
    while p < stop:
        elem, prop, interp, nk = struct.unpack_from(">IIHH", d, p)
        p += 12
        keys = []
        for _ in range(nk):
            t, v, a, h1, b, h2 = struct.unpack_from(">IfIfIf", d, p)
            keys.append((t, v, a, h1, b, h2))
            p += 24
        tracks.append({"elem": elem, "prop": prop, "interp": interp, "keys": keys})
    assert p == stop, (hex(p), hex(stop))
    return behaviours, tracks, p


def parse_slides(d: bytes, p: int):
    """Slide section: u32 a, u32 b (lists, empty so far), u32 nbytes, then slides back to back:
    name[16], u32 component elem index, u8 mode, u32 start, u32 duration, u32 n entries,
    entry = u32 elem, u8 active, u32 nprops, props (hash, raw4), u32 ntracks, (u16 on, u16 track)."""
    (nev,) = struct.unpack_from(">I", d, p)
    p += 4
    evend = p + nev
    pre = []  # events: elem -> [(event hash, [(src, dst, action hash, arg hash, arg)])]
    while p < evend:
        elem, ne = struct.unpack_from(">II", d, p)
        p += 8
        evs = []
        for _ in range(ne):
            eh, na = struct.unpack_from(">II", d, p)
            p += 8
            acts = [struct.unpack_from(">IIIII", d, p + 20 * i) for i in range(na)]
            p += 20 * na
            evs.append((eh, acts))
        pre.append((elem, evs))
    assert p == evend
    b, size = struct.unpack_from(">II", d, p)
    head = (nev, b)
    p += 8
    stop = p + size
    slides = []
    while p < stop:
        name = d[p : p + 16].split(b"\0")[0].decode("latin-1")
        comp, mode, start, dur, n = struct.unpack_from(">IBIII", d, p + 16)
        q = p + 16 + 17
        entries = []
        for _ in range(n):
            elem, active, np_ = struct.unpack_from(">IBI", d, q)
            q += 9
            props = []
            for _ in range(np_):
                props.append((struct.unpack_from(">I", d, q)[0], d[q + 4 : q + 8]))
                q += 8
            (nt,) = struct.unpack_from(">I", d, q)
            q += 4
            trk = [struct.unpack_from(">HH", d, q + 4 * i) for i in range(nt)]
            q += 4 * nt
            entries.append({"elem": elem, "active": active, "props": props, "tracks": trk})
        slides.append(
            {"name": name, "comp": comp, "mode": mode, "start": start, "dur": dur, "entries": entries, "off": p}
        )
        p = q
    assert p == stop, (hex(p), hex(stop))
    return head, pre, slides, p


def parse_bgf(d: bytes):
    r = _parse_elements(d)
    beh, tracks, p = parse_tracks(d, r["rest"])
    head, _events, slides, p = parse_slides(d, p)
    r.update(behaviours=beh, tracks=tracks, slides=slides, slide_head=head, end=p, size=len(d))
    return r


# ---------------------------------------------------------------- vector fonts


@dataclass
class Glyph:
    char: int
    advance: float
    verts: np.ndarray  # (n, 4) float: x, y (em units, y up), a, b
    tris: np.ndarray  # (m, 3) int
    extra: tuple


@dataclass
class VFont:
    name: str
    glyphs: dict
    header: tuple
    em: int


def load_font(data: bytes, name: str = "") -> VFont:
    assert data[:4] == b"CAFF", data[:8]
    vf = data.find(b"vfont")
    (datasize,) = struct.unpack_from(">I", data, 0x60)
    gpu = vf + datasize
    # vfont header: version string then fields; the triangle buffers: total verts at a fixed spot
    # found by searching the (vbytes, nverts, nidx) triple: vbytes == nverts * 8.
    hdr = None
    for i in range(vf, gpu - 12, 4):
        vb, nv, ni = struct.unpack_from(">III", data, i)
        if nv and vb == nv * 8 and 0 < ni < 1 << 20:
            hdr = (i, vb, nv, ni)
            break
    assert hdr, "no buffer header"
    _, vbytes, nverts, nidx = hdr
    vbuf = np.frombuffer(data, dtype=">f2", count=nverts * 4, offset=gpu).astype(np.float32).reshape(nverts, 4)
    ibuf = np.frombuffer(data, dtype=">u2", count=nidx, offset=gpu + vbytes).astype(np.int64)
    metrics = struct.unpack_from(">fff11i", data, hdr[0] + 12)
    glyphs = {}
    # glyph records: u32 char, f32 advance, u16 0xffff, u16 ?, u32 vertex byte offset, u32 nverts,
    # u32 first index, u32 ntris, f32, f32, f32
    p = hdr[0]
    while p < gpu - 40:
        ch, adv, ff, _, voff, nv, ioff, nt, f1, f2, f3 = struct.unpack_from(">IfHHIIIIfff", data, p)
        if (
            ff == 0xFFFF
            and 0x20 <= ch < 0x10000
            and 0 < adv < 4
            and voff % 8 == 0
            and voff + nv * 8 <= vbytes
            and ioff + 3 * nt <= nidx
        ):
            v0 = voff // 8
            idx = ibuf[ioff : ioff + 3 * nt].reshape(-1, 3)
            glyphs[ch] = Glyph(ch, adv, vbuf[v0 : v0 + nv], idx, (f1, f2, f3))
            p += 40
        else:
            p += 4
    return VFont(name, glyphs, metrics, 2048)
