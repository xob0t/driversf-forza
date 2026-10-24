"""Parse Xbox 360 Forza .carbin car models: FH1 (TypeId 5) and FM4 (TypeId 1/2/3).

Written from the format description in carbin-garage's docs
(docs/FH1_CARBIN_CONDENSED.md, FH1_CARBIN_MASTER.md, FM4_CARBIN_CONDENSED.md,
CARBIN_TRANSCODE.md; GPL-3.0 project, github.com/ItsNotPaths/carbin-garage).
No code is taken from it. Everything is big-endian.

File: header (variable; 0x4DC bytes in an FH1 main carbin, 0x1020 in the FM4
370Z main carbin), then u32 part_count, then part_count sections. The parser
finds the start by trying each plausible part_count position. FH1 layout
(section version 3):

    u32 marker (5), f32 offset[3], target_min[3], target_max[3]
    28 bytes, u32 perm_count + perm_count*16, 4, u32 cnt2 + cnt2*2, 12
    u8 name_len + name, u32 version (3)
    u32 lod_vcount, u32 lod_vsize, [u32 bone_weights, (u32 section_id if bone_weights)]
    LOD pool (LOD1..4 vertices), 4, u32 subpart_count, subsections
    4, i32 lod0_vcount, u32 lod0_vsize, [same bone block if lod0_vcount]
    LOD0 pool, tail

Subsection: 5, f32 uv_offset_scale[8], 36, i32 name_len + name, i32 lod,
u32 index_type (4 list, 6 strip), f32[6], f32[8], u32 version (4),
u32 0, i32 index_count, i32 index_size, indices, 4.

Vertex (28 bytes): int16 x,y,z,s (spos = ShortN(xyz) * ShortN(s)),
u16 uv0[2], u16 uv1[2], int16 quaternion[4] (normal = rotation row 0), 4 bytes.
World position = remap(spos, pool min/max, target_min/max) + offset.

FM4 differences (section version 2, CARBIN_TRANSCODE.md "four deltas"):
  * no bone-weight words before either vertex pool;
  * vertices are 32 bytes: the same first 24 bytes, then 8 opaque bytes
    (FH1 keeps 4);
  * subsections carry u32 3 before the index count, without FH1's extra u32 0;
  * the section tail is 9 bytes, u32 a, u32 b, a*b, 4, u32 c, u32 d, c*d
    (no padding probe needed);
  * the file ends with a 24-byte footer after the last section.
TypeIds: FH1 main/lod0/cockpit 5, FM4 ones 2; caliper/rotor carbins are 1
in both games, so the game is told apart by the section version.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAIN_BODY_START = 0x4DC  # FH1 main carbins
TYPE_IDS = (1, 2, 3, 5)  # 5: FH1 main/lod0/cockpit; 2 (3): FM4 ones; 1: caliper/rotor in both
SECTION_VERSIONS = {3: "FH1", 2: "FM4"}
VERTEX_SIZES = (28, 32)  # FH1, FM4


class CarbinError(ValueError):
    pass


@dataclass
class Vertex:
    position: tuple[float, float, float]
    uv0: tuple[float, float]
    uv1: tuple[float, float]
    normal: tuple[float, float, float]
    extra: bytes = b""  # trailing undecoded bytes: 4 in FH1, 8 in FM4


@dataclass
class SubSection:
    material: str
    lod: int
    index_type: int
    uv_transform: tuple[float, ...]  # (x_off, x_scale, y_off, y_scale) for uv0, then uv1
    indices: list[int]

    def triangles(self) -> list[tuple[int, int, int]]:
        idx = self.indices
        if self.index_type == 4:
            return [(idx[i], idx[i + 1], idx[i + 2]) for i in range(0, len(idx) - 2, 3)]
        if self.index_type == 6:
            # Strip restarts: 0xFFFF in 16-bit buffers; 0xFFFFFF (Xenos 24-bit index
            # limit, used by lod0/cockpit carbins) or 0xFFFFFFFF in 32-bit ones.
            restarts = {0xFFFFFF, 0xFFFFFFFF}
            if all(i <= 0xFFFF for i in idx if i not in restarts):
                restarts.add(0xFFFF)
            tris, run = [], []
            for i in idx + [0xFFFFFFFF]:
                if i in restarts:
                    for k in range(len(run) - 2):
                        a, b, c = run[k], run[k + 1], run[k + 2]
                        if a in (b, c) or b == c:
                            continue
                        tris.append((a, b, c) if k % 2 == 0 else (b, a, c))
                    run = []
                else:
                    run.append(i)
            return tris
        raise CarbinError(f"unknown index type {self.index_type}")


@dataclass
class Section:
    name: str
    offset: tuple[float, float, float]
    target_min: tuple[float, float, float]
    target_max: tuple[float, float, float]
    lod_vertices: list[Vertex] = field(default_factory=list)
    lod0_vertices: list[Vertex] = field(default_factory=list)
    subsections: list[SubSection] = field(default_factory=list)
    version: int = 0  # 3 = FH1, 2 = FM4

    def vertices_for(self, sub: SubSection) -> list[Vertex]:
        return self.lod0_vertices if sub.lod == 0 else self.lod_vertices


class _Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise CarbinError(f"read past end at {self.pos:#x}")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def skip(self, n: int) -> None:
        self.take(n)

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self.take(4))[0]

    def floats(self, n: int) -> tuple[float, ...]:
        return struct.unpack(f">{n}f", self.take(4 * n))


def _shortn(v: int) -> float:
    return v / 32767.0


def _quat_row0(x: float, y: float, z: float, w: float) -> tuple[float, float, float]:
    if w < 0:
        x, y, z, w = -x, -y, -z, -w
    n = (1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y))
    length = math.sqrt(sum(c * c for c in n)) or 1.0
    return (n[0] / length, n[1] / length, n[2] / length)


def _decode_pool(raw: bytes, count: int, size: int, section: Section) -> list[Vertex]:
    if count == 0:
        return []
    if size not in VERTEX_SIZES:
        raise CarbinError(f"{section.name}: unsupported vertex size {size}")
    spos, rest = [], []
    for i in range(count):
        base = i * size
        x, y, z, s, u0, v0, u1, v1, qx, qy, qz, qw = struct.unpack_from(">4h4H4h", raw, base)
        scale = _shortn(s)
        spos.append((_shortn(x) * scale, _shortn(y) * scale, _shortn(z) * scale))
        rest.append(
            (
                (u0 / 65535, v0 / 65535),
                (u1 / 65535, v1 / 65535),
                _quat_row0(_shortn(qx), _shortn(qy), _shortn(qz), _shortn(qw)),
                raw[base + 24 : base + size],
            )
        )
    lo = [min(p[a] for p in spos) for a in range(3)]
    hi = [max(p[a] for p in spos) for a in range(3)]
    vertices = []
    for p, (uv0, uv1, normal, extra) in zip(spos, rest):
        world = []
        for a in range(3):
            span = hi[a] - lo[a]
            t = (p[a] - lo[a]) / span if abs(span) > 1e-12 else 0.0
            world.append(
                section.target_min[a] + t * (section.target_max[a] - section.target_min[a]) + section.offset[a]
            )
        vertices.append(Vertex(tuple(world), uv0, uv1, normal, extra))
    return vertices


# An empty section (no vertices, e.g. FIA_595_68's roof1) has the bounds of an empty
# box: target_min FLT_MAX, target_max -FLT_MAX on every axis.
FLT_MAX = struct.unpack(">f", b"\x7f\x7f\xff\xff")[0]
EMPTY_BOUNDS = (FLT_MAX,) * 3 + (-FLT_MAX,) * 3


def _looks_like_section(data: bytes, pos: int) -> bool:
    if pos + 40 > len(data) or struct.unpack_from(">I", data, pos)[0] not in (2, 5):
        return False
    values = struct.unpack_from(">9f", data, pos + 4)
    if not all(math.isfinite(v) and abs(v) < 100 for v in values[:3]):
        return False
    return values[3:] == EMPTY_BOUNDS or all(math.isfinite(v) and abs(v) < 100 for v in values[3:])


def _section_header_ok(data: bytes, pos: int) -> bool:
    """Stricter check: walk the section header to the name and version."""
    if not _looks_like_section(data, pos):
        return False
    try:
        p = pos + 40 + 28
        p += 4 + struct.unpack_from(">I", data, p)[0] * 16 + 4
        p += 4 + struct.unpack_from(">I", data, p)[0] * 2 + 12
        name = data[p + 1 : p + 1 + data[p]]
        version = struct.unpack_from(">I", data, p + 1 + data[p])[0]
    except (struct.error, IndexError):
        return False
    return len(name) > 0 and all(32 < c < 127 for c in name) and version in (2, 3)


def _parse_subsection(r: _Reader, version: int) -> SubSection:
    r.skip(5)
    uv = r.floats(8)
    r.skip(36)
    name_len = r.i32()
    if not 0 <= name_len < 256:
        raise CarbinError(f"bad subsection name length {name_len} at {r.pos - 4:#x}")
    name = r.take(name_len).decode("latin-1")
    lod = r.i32()
    index_type = r.u32()
    r.floats(6)
    r.floats(8)
    r.u32()  # 4 in FH1, 3 in FM4 (subsection version)
    if version >= 3:
        r.u32()  # FH1 reserved, 0
    count = r.i32()
    size = r.i32()
    if size not in (2, 4) or not 0 <= count < 10_000_000:
        raise CarbinError(f"{name}: bad index buffer {count} x {size} at {r.pos - 8:#x}")
    fmt = f">{count:d}{'H' if size == 2 else 'I'}"
    indices = list(struct.unpack(fmt, r.take(count * size)))
    r.skip(4)
    return SubSection(name, lod, index_type, uv, indices)


def _parse_section(r: _Reader) -> Section:
    marker = r.u32()
    if marker not in (2, 5):
        raise CarbinError(f"bad section marker {marker} at {r.pos - 4:#x}")
    floats = r.floats(9)
    section = Section("", floats[0:3], floats[3:6], floats[6:9])
    r.skip(28)
    r.skip(r.u32() * 16)
    r.skip(4)
    r.skip(r.u32() * 2)
    r.skip(12)
    raw_name = r.take(r.u8())
    if not all(32 < c < 127 for c in raw_name):  # a wrong start (cf. _section_header_ok)
        raise CarbinError(f"section name {raw_name[:24]!r} is not text at {r.pos - len(raw_name):#x}")
    section.name = raw_name.decode("latin-1")
    version = section.version = r.u32()
    if version not in (2, 3):
        raise CarbinError(f"{section.name!r}: unsupported section version {version} at {r.pos - 4:#x}")
    lod_count, lod_size = r.u32(), r.u32()
    if version >= 3 and r.u32():
        r.u32()
    lod_raw = r.take(lod_count * lod_size)
    r.skip(4)
    subs = [_parse_subsection(r, version) for _ in range(r.u32())]
    r.skip(4)
    lod0_count, lod0_size = r.i32(), r.u32()
    if version >= 3 and lod0_count > 0 and r.u32():
        r.u32()
    lod0_raw = r.take(max(lod0_count, 0) * lod0_size)
    section.lod_vertices = _decode_pool(lod_raw, lod_count, lod_size, section)
    section.lod0_vertices = _decode_pool(lod0_raw, max(lod0_count, 0), lod0_size, section)
    section.subsections = subs
    _skip_tail(r, version, lod0_count)
    return section


def _skip_tail(r: _Reader, version: int, lod0_count: int) -> None:
    """Skip the section tail; ends where the next section (or EOF) begins."""
    if version >= 3:
        start = r.pos
        try:
            _skip_tail_v3(r, lod0_count)
        except (CarbinError, struct.error):
            r.pos = start
            _skip_tail_blocks(r)
    else:
        r.skip(9)
        a, b = r.u32(), r.u32()
        r.skip(a * b)
        r.skip(4)
        c, d = r.u32(), r.u32()
        r.skip(c * d)


def _skip_tail_v3(r: _Reader, lod0_count: int) -> None:
    """The FH1 tail of a main or cockpit carbin section: 13 bytes, then one
    per-vertex record block of the LOD pool (4 bytes a vertex), then the same
    for LOD0; the end is found by probing for the next section header."""
    r.skip(13)
    a, b = r.u32(), r.u32()
    r.skip(4)
    r.skip(a * b)
    r.skip(4)
    c, d = r.u32(), r.u32()
    r.skip(c * d)
    base = r.pos
    candidates = [base + extra for extra in (0, 4, 8)]
    candidates += [base + extra + lod0_count * 4 for extra in (0, 4, 8)] if lod0_count > 0 else []
    for pos in candidates:
        if _section_header_ok(r.data, pos) or pos >= len(r.data) - 32:
            r.pos = min(pos, len(r.data))
            return
    r.pos = base + 4


# Each per-vertex record block of an FH1 tail: u32 3, u32 count, u32 record size,
# u32 1, count * size bytes. Skinned pools (the wheel archives' carbins, whose LOD0
# pool has bone weights) carry 16-byte records, and their tail starts 4 bytes
# earlier than _skip_tail_v3's fixed 13, so it reads garbage counts there.
_TAIL_BLOCKS = b"\x01\x01\x00\x00\x00\x03"


def _skip_tail_blocks(r: _Reader) -> None:
    """Fallback for tails _skip_tail_v3 can't read: find the 01 01 marker in the
    first bytes, walk the record blocks, then probe for the next section header."""
    data = r.data
    at = data.find(_TAIL_BLOCKS, r.pos, r.pos + 16)
    if at < 0:
        raise CarbinError(f"section tail not recognised at {r.pos:#x}")
    pos = at + 2
    while pos + 16 <= len(data):
        if struct.unpack_from(">I", data, pos)[0] != 3:
            # a block may be followed by one more u32 before the next block
            if pos + 20 <= len(data) and struct.unpack_from(">I", data, pos + 4)[0] == 3:
                pos += 4
                continue
            break
        count, size = struct.unpack_from(">II", data, pos + 4)
        end = pos + 16 + count * size
        if (count and size not in (4, 8, 12, 16, 20, 24, 28, 32)) or end > len(data):
            raise CarbinError(f"bad tail block {count} x {size} at {pos:#x}")
        pos = end
    for candidate in range(pos, min(pos + 40, len(data)) + 1, 4):
        if _section_header_ok(data, candidate) or candidate >= len(data) - 32:
            r.pos = min(candidate, len(data))
            return
    raise CarbinError(f"no section after the tail at {pos:#x}")


def _check_type(data: bytes) -> int:
    type_id = struct.unpack_from(">I", data)[0]
    if type_id not in TYPE_IDS:
        raise CarbinError(
            f"TypeId {type_id} not supported (FH1: 5, FM4: 2, either: 1; 0 is a stripped header-only carbin)"
        )
    return type_id


def game_of(sections: list[Section]) -> str:
    """'FH1' or 'FM4', from the section version."""
    games = {SECTION_VERSIONS.get(s.version, "?") for s in sections}
    return games.pop() if len(games) == 1 else "/".join(sorted(games))


@dataclass
class ParseResult:
    sections: list[Section]
    start: int  # offset of the u32 part_count
    declared: int  # part_count
    end: int  # offset just past the last parsed section

    @property
    def complete(self) -> bool:
        return len(self.sections) == self.declared

    @property
    def game(self) -> str:
        return game_of(self.sections)


def parse_full(data: bytes) -> ParseResult:
    """Like parse(), but also report where the sections are and whether all parsed."""
    type_id = _check_type(data)
    best: ParseResult | None = None
    if type_id == 5 and _looks_like_section(data, MAIN_BODY_START + 4):
        start = MAIN_BODY_START
    else:
        start = 0x100
    for pos in range(start, min(len(data) - 44, start + 0x4000), 4):
        count = struct.unpack_from(">I", data, pos)[0]
        if not 1 <= count <= 255 or not _looks_like_section(data, pos + 4):
            continue
        r = _Reader(data, pos + 4)
        sections = []
        try:
            for _ in range(count):
                sections.append(_parse_section(r))
        except (CarbinError, struct.error):
            pass
        result = ParseResult(sections, pos, count, r.pos)
        if result.complete:
            return result
        if best is None or len(sections) > len(best.sections):
            best = result
    if best is None or not best.sections:
        raise CarbinError("no parsable sections found")
    return best


def parse(data: bytes) -> list[Section]:
    """All sections of a carbin (best partial result if some section fails)."""
    return parse_full(data).sections


def write_obj(sections: list[Section], path: Path, lod: int) -> int:
    count = 0
    with path.open("w") as out:
        base = 1
        for section in sections:
            for sub in section.subsections:
                if sub.lod != lod:
                    continue
                verts = section.vertices_for(sub)
                tris = sub.triangles()
                used = sorted({i for t in tris for i in t})
                remap = {v: n for n, v in enumerate(used)}
                out.write(f"o {section.name}.{sub.material}\n")
                for v in used:
                    out.write("v {:.5f} {:.5f} {:.5f}\n".format(*verts[v].position))
                for v in used:
                    x_off, x_scale, y_off, y_scale = sub.uv_transform[:4]
                    u, w = verts[v].uv0
                    out.write(f"vt {u * x_scale + x_off:.5f} {1 - (w * y_scale + y_off):.5f}\n")
                for v in used:
                    out.write("vn {:.4f} {:.4f} {:.4f}\n".format(*verts[v].normal))
                for t in tris:
                    a, b, c = (base + remap[i] for i in t)
                    out.write(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}\n")
                base += len(used)
                count += len(tris)
    return count


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("carbin", type=Path)
    parser.add_argument("--lod", type=int, default=1, help="LOD to export (0 high .. 4 low; main carbins hold 1..4)")
    parser.add_argument("--obj", type=Path)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)

    data = args.carbin.read_bytes()
    result = parse_full(data)
    sections = result.sections
    sizes = sorted({len(v.extra) + 24 for s in sections for v in (s.lod_vertices or s.lod0_vertices)[:1]})
    print(
        f"{args.carbin.name}: {result.game} TypeId {struct.unpack_from('>I', data)[0]}, "
        f"{len(sections)}/{result.declared} sections from {result.start:#x}, "
        f"{len(data) - result.end} trailing bytes, vertex size {sizes}"
    )
    if args.summary:
        for s in sections:
            lods = sorted({sub.lod for sub in s.subsections})
            mats = sorted({sub.material for sub in s.subsections})
            print(
                f"  {s.name:24} lodV {len(s.lod_vertices):6} lod0V {len(s.lod0_vertices):6} "
                f"subs {len(s.subsections):3} lods {lods} mats {mats}"
            )
    if args.obj:
        print(f"wrote {write_obj(sections, args.obj, args.lod)} triangles to {args.obj}")


if __name__ == "__main__":
    sys.exit(main())
