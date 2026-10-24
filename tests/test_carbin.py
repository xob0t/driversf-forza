"""carbin.parse on synthetic FH1 carbins: an empty first section (FIA_595_68's
roof1) and a skinned section tail (the wheel archives' carbins).

    uv run python tests/test_carbin.py      # or every test: uv run python -m tests
"""

from __future__ import annotations

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
    # AUS_MiniCooperS_65: wheela (stock) and wheelb (upgrade), no wheel
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
