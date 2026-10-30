"""carbin.parse on synthetic FH1 carbins: an empty first section (FIA_595_68's
roof1) and a skinned section tail (the wheel archives' carbins).

    uv run python tests/test_carbin.py      # or every test: uv run python -m tests
"""

from __future__ import annotations

import math
import random
import struct

from dsfforza.formats import carbin

FLT_MAX = carbin.FLT_MAX


def _u32(*values: int) -> bytes:
    return struct.pack(f">{len(values)}I", *values)


def _vertex(x: int, y: int, z: int) -> bytes:
    # int16 x, y, z, s; u16 uv0, uv1; int16 quaternion (identity); 4 bytes
    return struct.pack(">4h4H4h", x, y, z, 32767, 0, 0, 0, 0, 0, 0, 0, 32767) + b"\0" * 4


def _subsection(name: str, lod: int, indices: list[int]) -> bytes:
    out = b"\0" * 5 + struct.pack(">8f", 0, 1, 0, 1, 0, 1, 0, 1) + b"\0" * 36
    out += struct.pack(">i", len(name)) + name.encode() + struct.pack(">iI", lod, 4)
    out += struct.pack(">14f", *([0.0] * 14)) + _u32(4, 0)
    out += struct.pack(">ii", len(indices), 2) + struct.pack(f">{len(indices)}H", *indices) + b"\0" * 4
    return out


def _section(name: str, lod: int, lod0: int, bounds: tuple, skinned: bool = False) -> bytes:
    """One FH1 (version 3) section with `lod` LOD1 vertices and `lod0` LOD0 vertices."""
    out = _u32(5) + struct.pack(">9f", 0.0, 0.0, 0.0, *bounds)
    out += b"\0" * 28 + _u32(0) + b"\0" * 4 + _u32(0) + b"\0" * 12
    out += bytes([len(name)]) + name.encode() + _u32(3)
    out += _u32(lod, 28 if lod else 0, 1 if skinned else 0) + (_u32(0x1234) if skinned else b"")
    out += b"".join(_vertex(i * 1000, 0, 0) for i in range(lod))
    subs = [_subsection("body", 1, [0, 1, 2])] if lod else []
    out += b"\0" * 4 + _u32(len(subs)) + b"".join(subs) + b"\0" * 4
    out += struct.pack(">iI", lod0, 28 if lod0 else 0)
    if lod0:
        out += _u32(1 if skinned else 0) + (_u32(0x5678) if skinned else b"")
        out += b"".join(_vertex(0, i * 1000, 0) for i in range(lod0))
    if skinned:
        # 3 bytes, 01 01, then u32 3, count, 16, 1, count * 16-byte records per pool
        out += b"\0" * 3 + b"\x01\x01"
        out += _u32(3, lod, 16, 1) + b"\x7f" * (16 * lod) + b"\0" * 4
        out += _u32(3, lod0, 16, 1) + b"\x7f" * (16 * lod0)
    else:
        # main carbin layout: 7 bytes, 01 01, u32 3, count, 4, 1, count * 4 bytes, then
        # an empty LOD0 block; the next section starts 8 bytes after its count
        out += b"\0" * 7 + b"\x01\x01" + _u32(3, lod, 4, 1) + b"\0" * (4 * lod) + _u32(0, 3, 0, 0, 1)
    return out


def _carbin(sections: list[bytes]) -> bytes:
    data = _u32(5) + b"\0" * (0x100 - 4) + _u32(len(sections)) + b"".join(sections)
    return data + _u32(1, 0, 1, 0, 1, 0)  # file footer


BOX = (-1.0, -0.5, -2.0, 1.0, 0.5, 2.0)
EMPTY = (FLT_MAX, FLT_MAX, FLT_MAX, -FLT_MAX, -FLT_MAX, -FLT_MAX)


def test_normal_sections():
    result = carbin.parse_full(_carbin([_section("body", 3, 0, BOX), _section("hooda", 4, 0, BOX)]))
    assert result.complete, (len(result.sections), result.declared)
    assert [s.name for s in result.sections] == ["body", "hooda"]
    assert [len(s.lod_vertices) for s in result.sections] == [3, 4]


def test_triangles_are_kept_per_index_list():
    import dataclasses

    sub = carbin.SubSection("body", 1, 6, (0.0, 1.0) * 4, [0, 1, 2, 3, 0xFFFF, 4, 5, 6])
    first = sub.triangles()
    assert first == [(0, 1, 2), (2, 1, 3), (4, 5, 6)]
    first.append((9, 9, 9))  # the caller's own list
    assert sub.triangles() == [(0, 1, 2), (2, 1, 3), (4, 5, 6)] and sub.triangles() is not sub.triangles()
    sub.index_type = 4
    assert sub.triangles() == [(0, 1, 2), (3, 0xFFFF, 4)]
    sub.indices = [5, 6, 7]
    assert sub.triangles() == [(5, 6, 7)]
    other = dataclasses.replace(sub, indices=[1, 2, 3])
    assert other.triangles() == [(1, 2, 3)] and sub.triangles() == [(5, 6, 7)]
    assert other == dataclasses.replace(other)  # the kept triangles don't count in ==


def test_empty_first_section():
    # FIA_595_68: roof1 has no vertices and the bounds of an empty box. Without
    # accepting those bounds no start position looks like a section.
    data = _carbin([_section("roof1", 0, 0, EMPTY), _section("body", 3, 0, BOX), _section("trunk", 3, 0, BOX)])
    result = carbin.parse_full(data)
    assert result.complete, (len(result.sections), result.declared)
    assert [s.name for s in result.sections] == ["roof1", "body", "trunk"]
    assert result.sections[0].lod_vertices == [] and result.sections[0].subsections == []
    assert len(result.sections[1].lod_vertices) == 3


def test_skinned_tail():
    # A wheel archive's carbin: one section whose LOD and LOD0 pools are skinned
    # (bone flag 1 + an id), with 16-byte per-vertex tail records.
    data = _carbin([_section("wheel", 5, 6, BOX, skinned=True)])
    result = carbin.parse_full(data)
    assert result.complete, (len(result.sections), result.declared)
    (wheel,) = result.sections
    assert wheel.name == "wheel" and len(wheel.lod_vertices) == 5 and len(wheel.lod0_vertices) == 6
    assert result.end == len(data) - 24  # stops at the footer


def test_skinned_tail_between_sections():
    data = _carbin([_section("wheel", 5, 6, BOX, skinned=True), _section("body", 3, 0, BOX)])
    result = carbin.parse_full(data)
    assert [s.name for s in result.sections] == ["wheel", "body"]


def _load(main: list[bytes], wheel_archive: list[bytes] | None = None):
    import tempfile
    from pathlib import Path

    from dsfforza.convert import forza2dsf

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "CAR_Test_10.carbin").write_bytes(_carbin(main))
        if wheel_archive is not None:
            (folder / forza2dsf.WHEEL_CARBIN).write_bytes(_carbin(wheel_archive))
        return forza2dsf.load_car(folder, "CAR_Test_10")


def test_load_car_wheel_section():
    car = _load([_section("body", 3, 0, BOX), _section("wheel", 4, 0, BOX)], [_section("wheel", 5, 6, BOX, True)])
    assert [s.name for s in car.wheel] == ["wheel"] and len(car.wheel[0].lod_vertices) == 4  # the main carbin's
    assert [s.name for s in car.body] == ["body"]


def test_load_car_wheel_variants():
    # AUS_MiniCooperS_65: wheela (vanilla) and wheelb (upgrade), no wheel
    car = _load([_section("body", 3, 0, BOX), _section("wheela", 4, 0, BOX), _section("wheelb", 5, 0, BOX)])
    assert [s.name for s in car.wheel] == ["wheela"]
    assert [s.name for s in car.body] == ["body"]  # neither variant is drawn as body


def test_load_car_wheel_archive():
    # FER_360_99, LAM_Gallardo_05...: no wheel in the main carbin, only in the wheel archive's
    car = _load([_section("body", 3, 0, BOX)], [_section("wheel", 5, 6, BOX, True)])
    assert [s.name for s in car.wheel] == ["wheel"] and len(car.wheel[0].lod_vertices) == 5
    assert [s.name for s in car.body] == ["body"]


def test_load_car_no_wheel():
    try:
        _load([_section("body", 3, 0, BOX)])
    except ValueError as error:
        assert "no wheel section" in str(error)
    else:
        raise AssertionError("a car without any wheel must fail")


# --- carbin._decode_pool ----------------------------------------------------------------


def _decode_pool_reference(raw: bytes, count: int, size: int, section: carbin.Section) -> list:
    """The original per-vertex loop."""
    import struct

    def shortn(v):
        return v / 32767.0

    def quat_row0(x, y, z, w):
        if w < 0:
            x, y, z, w = -x, -y, -z, -w
        n = (1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y))
        length = math.sqrt(sum(c * c for c in n)) or 1.0
        return (n[0] / length, n[1] / length, n[2] / length)

    if count == 0:
        return []
    spos, rest = [], []
    for i in range(count):
        base = i * size
        x, y, z, s, u0, v0, u1, v1, qx, qy, qz, qw = struct.unpack_from(">4h4H4h", raw, base)
        scale = shortn(s)
        spos.append((shortn(x) * scale, shortn(y) * scale, shortn(z) * scale))
        rest.append(
            (
                (u0 / 65535, v0 / 65535),
                (u1 / 65535, v1 / 65535),
                quat_row0(shortn(qx), shortn(qy), shortn(qz), shortn(qw)),
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
        vertices.append(carbin.Vertex(tuple(world), uv0, uv1, normal, extra))
    return vertices


def _bits(vertices: list) -> list:
    """Every float as its bit pattern (0.0 and -0.0 differ), and the extra bytes."""
    import struct

    out = []
    for v in vertices:
        floats = (*v.position, *v.uv0, *v.uv1, *v.normal)
        assert all(type(f) is float for f in floats)
        out.append((struct.pack(f"<{len(floats)}d", *floats), v.extra))
    return out


def test_decode_pool_matches_the_vertex_loop():
    import struct

    rng = random.Random(5)
    picks = (0, 1, -1, 32767, -32767, -32768, 16384, -16384)

    def short(spread: int) -> int:
        return rng.choice(picks) if rng.random() < 0.3 else rng.randint(-spread, spread)

    for trial in range(60):
        size = rng.choice(carbin.VERTEX_SIZES)
        count = rng.choice((1, 2, 3, 17, 400))
        flat = rng.random() < 0.2  # an axis with a single value (span 0)
        raw = b""
        for _ in range(count):
            x, y, z = (0 if flat and a == 1 else short(rng.choice((3, 32768))) for a in range(3))
            s = rng.choice((32767, -32767, 0, 1, rng.randint(-32768, 32767)))
            uv = [rng.choice((0, 65535, rng.randint(0, 65535))) for _ in range(4)]
            quat = [short(32768) for _ in range(4)]
            if rng.random() < 0.1:
                quat = [0, 0, 0, 0]
            raw += struct.pack(">4h4H4h", x, y, z, s, *uv, *quat) + rng.randbytes(size - 24)
        bounds = [rng.uniform(-3, 3) for _ in range(6)] if trial % 5 else [0.0, -0.0, 1.0, -0.0, 0.0, 2.0]
        section = carbin.Section(
            "body", tuple(rng.uniform(-1, 1) for _ in range(3)), tuple(bounds[:3]), tuple(bounds[3:])
        )
        assert _bits(carbin._decode_pool(raw, count, size, section)) == _bits(
            _decode_pool_reference(raw, count, size, section)
        ), trial


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
