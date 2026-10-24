"""Round-trip and edge-case checks for the file-format modules in tools/, on
synthetic data only (no game or Forza files needed).

    uv run python tests/test_formats.py     # or every test: uv run python -m tests

Each test_* function is a plain assert-based check (pytest collects them too).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import math
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from dsfforza.build import handling
from dsfforza.formats import (
    chnk,
    forzazip,
    lua_scripts,
    lzx,
    minidump,
    stfs,
    xds,
    xiso,
)
from dsfforza.formats import wwise_bank as wb
from dsfforza.game import (
    game_data,
    gui_assets,
    traffic_sets,
)

# --- CHNK ---------------------------------------------------------------------------

def _leaf(tag: str, payload: bytes, align: int = 2, name: bytes = b"") -> chnk.Entry:
    e = chnk.Entry(tag, 0, len(payload), 1, align, name.decode() or None, name_raw=name)
    e.payload = payload
    return e


def test_chnk_round_trip() -> None:
    nested = chnk.Entry("NONR", 0, 0, 2, 4, None, children=[_leaf("VO3D", b"\1" * 13), _leaf("UPVH", b"\2" * 7, 3)])
    entries = [_leaf("SSIC", b"abc", 2, b"named"), nested, _leaf("BINF", b"x" * 40, 6)]
    out = io.BytesIO()
    total = chnk.write_chunk(out, None, entries)
    raw = out.getvalue()
    assert len(raw) == total and total % 64 == 0          # rounded to the largest alignment
    root = chnk.read_chunk(io.BytesIO(raw))
    assert [e.tag for e in root.entries] == ["SSIC", "NONR", "BINF"]
    assert root.entries[0].name == "named"
    assert [c.tag for c in root.entries[1].children] == ["VO3D", "UPVH"]
    for e in root.entries:
        assert e.offset % (1 << e.align_log2) == 0
    nonr = root.entries[1]
    assert nonr.size == struct.unpack_from("<I", raw, nonr.offset + 4)[0]   # nested entry size = its total_size
    again = io.BytesIO()
    chnk.write_chunk(again, io.BytesIO(raw), root.entries, root.version)
    assert again.getvalue() == raw


# --- LZX (a minimal verbatim-block encoder) -------------------------------------------

class _BitWriter:
    """LZX bit order: 16-bit little-endian words, most significant bit first."""

    def __init__(self):
        self.out, self.word, self.n = bytearray(), 0, 0

    def write(self, value: int, bits: int) -> None:
        for i in range(bits - 1, -1, -1):
            self.word = (self.word << 1) | ((value >> i) & 1)
            self.n += 1
            if self.n == 16:
                self.out += struct.pack("<H", self.word)
                self.word, self.n = 0, 0

    def bytes(self) -> bytes:
        if self.n:
            self.write(0, 16 - self.n)
        return bytes(self.out)


def _lzx_verbatim(tokens: list, total: int) -> bytes:
    """One verbatim block for window 2^17: every main-tree symbol 10 bits long
    (code = symbol), no length tree. Tokens: an int (literal) or (offset,
    length) with length 2..8."""
    w = _BitWriter()
    w.write(0, 1)                      # no E8 translation
    w.write(lzx.VERBATIM, 3)
    w.write(total >> 8, 16)
    w.write(total & 0xFF, 8)
    main_elements = lzx.NUM_CHARS + lzx.POSITION_SLOTS[17] * 8

    def lengths(count: int, z: int) -> None:
        # Pre-tree with one 1-bit symbol z (code 0): every length becomes (0 - z) % 17.
        for sym in range(lzx.PRETREE_SYMBOLS):
            w.write(1 if sym == z else 0, 4)
        for _ in range(count):
            w.write(0, 1)
    lengths(256, 7)                    # length 10
    lengths(main_elements - 256, 7)
    lengths(249, 0)                    # length tree empty
    for t in tokens:
        if isinstance(t, int):
            w.write(t, 10)
            continue
        offset, length = t
        formatted = offset + 2
        slot = max(s for s in range(3, lzx.POSITION_SLOTS[17]) if lzx.POSITION_BASE[s] <= formatted)
        w.write(lzx.NUM_CHARS + (slot << 3) + (length - 2), 10)
        w.write(formatted - lzx.POSITION_BASE[slot], lzx.EXTRA_BITS[slot])
    return w.bytes()


def _xmem(chunks: list[tuple[int, bytes]]) -> bytes:
    return b"".join(b"\xff" + struct.pack(">HH", frame, len(data)) + data for frame, data in chunks)


def test_lzx_literals_and_match() -> None:
    stream = _lzx_verbatim([ord("a"), ord("b"), ord("c"), (3, 6), ord("z")], 10)
    assert lzx.xmem_decompress(_xmem([(10, stream)]), 10) == b"abcabcabcz"


def test_lzx_match_across_frames() -> None:
    # The match (offset 3, length 5) starts in frame 1 (4 bytes) and ends in frame 2;
    # frame 2 must start with the match's 2nd byte, not skip what it already decoded.
    stream = _lzx_verbatim([ord("a"), ord("b"), ord("c"), (3, 5)], 8)
    data = lzx.xmem_decompress(_xmem([(4, stream), (4, b"\0\0")]), 8)
    assert data == b"abcabcab", data


def test_lzx_short_uncompressed_block() -> None:
    w = _BitWriter()
    w.write(0, 1)
    w.write(lzx.UNCOMPRESSED, 3)
    w.write(0, 16)
    w.write(16, 8)
    stream = w.bytes() + struct.pack("<3I", 1, 1, 1) + b"0123456789"   # 6 of the 16 bytes missing
    try:
        lzx.xmem_decompress(_xmem([(16, stream)]), 16)
    except lzx.LzxError:
        return
    raise AssertionError("a truncated uncompressed block decoded")


def test_forzazip_lzx_entry() -> None:
    """A method-21 entry reads back through ForzaZip (LZX + CRC check)."""
    content = b"abcabcabcz"
    packed = _xmem([(len(content), _lzx_verbatim([97, 98, 99, (3, 6), ord("z")], len(content)))])
    name = b"media/cars/x.bin"
    crc = zlib.crc32(content)
    local = struct.pack("<4sHHHHHIIIHH", b"PK\3\4", 20, 0, forzazip.LZX_METHOD, 0, 0, crc, len(packed),
                        len(content), len(name), 0) + name + packed
    central = struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\1\2", 20, 20, 0, forzazip.LZX_METHOD, 0, 0, crc, len(packed),
                          len(content), len(name), 0, 0, 0, 0, 0, 0) + name
    end = struct.pack("<4sHHHHIIH", b"PK\5\6", 0, 0, 1, 1, len(central), len(local), 0)
    archive = forzazip.ForzaZip(io.BytesIO(local + central + end))
    assert archive.read(name.decode()) == content


# --- XDVDFS image holding an STFS package ----------------------------------------------

def _stfs_package(payload: bytes) -> bytes:
    """A female (single hash table) PIRS package: block 0 the file table, block 1
    the file. The table has an empty slot 0, so slot numbers and entry order differ."""
    header_size = 0x971A
    first = (header_size + 0xFFF) & ~0xFFF
    pkg = bytearray(first + 3 * stfs.BLOCK)
    pkg[0:4] = b"PIRS"
    struct.pack_into(">II", pkg, 0x340, header_size, 0x2)
    table = bytearray(stfs.BLOCK)

    def entry(slot: int, name: bytes, flags: int, start: int, parent: int, size: int) -> None:
        e = bytearray(0x40)
        e[:len(name)] = name
        e[0x28] = len(name) | flags
        e[0x29:0x2C] = e[0x2C:0x2F] = (1 if size else 0).to_bytes(3, "little")
        e[0x2F:0x32] = start.to_bytes(3, "little")
        struct.pack_into(">hI", e, 0x32, parent, size)
        table[slot * 0x40:(slot + 1) * 0x40] = e
    entry(1, b"Media", 0x80, 0, -1, 0)
    entry(2, b"a.bin", 0x40, 1, 1, len(payload))
    data = payload.ljust(stfs.BLOCK, b"\0")
    hashes = bytearray(stfs.BLOCK)
    for block, content in enumerate((bytes(table), data)):
        hashes[block * 0x18:block * 0x18 + 0x18] = hashlib.sha1(content).digest() + b"\x80" + b"\xff\xff\xff"
    pkg[first:first + stfs.BLOCK] = hashes
    pkg[first + stfs.BLOCK:first + 2 * stfs.BLOCK] = table
    pkg[first + 2 * stfs.BLOCK:first + 3 * stfs.BLOCK] = data
    vd = bytearray(0x24)
    vd[0], vd[2] = 0x24, 1                         # size, flags: female
    struct.pack_into("<H", vd, 3, 1)               # file table: 1 block from block 0
    vd[8:28] = hashlib.sha1(bytes(hashes)).digest()
    struct.pack_into(">II", vd, 0x1C, 2, 0)
    pkg[0x379:0x379 + 0x24] = vd
    return bytes(pkg)


def _xiso_image(files: dict[str, bytes]) -> bytes:
    """A rebuilt-xiso style image (partition at 0): root directory with one
    subdirectory per first path part. Paths have exactly two parts."""
    sector = xiso.SECTOR
    image = bytearray(0x10000 + sector)
    data_sector = 64

    def add_dir(entries: list[tuple[str, int, int, int]]) -> tuple[int, int]:
        """Entries chained through their right pointers; returns (sector, size)."""
        table = bytearray()
        for i, (name, sec, size, attr) in enumerate(entries):
            raw = struct.pack("<HHIIBB", 0, 0, sec, size, attr, len(name)) + name.encode()
            raw += b"\xff" * (-len(raw) % 4)
            nxt = (len(table) + len(raw)) // 4 if i + 1 < len(entries) else 0
            table += struct.pack("<HH", 0, nxt) + raw[4:]
        return place(bytes(table)), len(table)

    def place(blob: bytes) -> int:
        nonlocal data_sector
        start = data_sector
        end = start * sector + len(blob)
        if len(image) < end:
            image.extend(b"\0" * (end - len(image)))
        image[start * sector:end] = blob
        data_sector += -(-len(blob) // sector) or 1
        return start

    groups: dict[str, list] = {}
    for path, blob in files.items():
        folder, name = path.split("/")
        groups.setdefault(folder, []).append((name, place(blob), len(blob), 0x80))
    root = [(folder, *add_dir(items), xiso.DIRECTORY) for folder, items in groups.items()]
    root_sector, root_size = add_dir(root)
    image[0x10000:0x10000 + 28] = xiso.MAGIC + struct.pack("<II", root_sector, root_size)
    image.extend(b"\0" * (-len(image) % sector))
    return bytes(image)


def test_stfs_parent_slots_and_data() -> None:
    payload = bytes(range(256)) * 9
    pkg = stfs.Stfs(io.BytesIO(_stfs_package(payload)))
    assert [(e.index, e.path) for e in pkg.files()] == [(1, "Media"), (2, "Media/a.bin")]
    assert pkg.read("media/A.BIN", verify=True) == payload
    assert pkg.verify_tree(data=True) == (1, 2)
    f = pkg.open("Media/a.bin")
    f.seek(1000)
    assert f.read(10) == payload[1000:1010]


def test_xiso_with_package() -> None:
    payload = b"hello stfs" * 50
    blob = _stfs_package(payload)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "disc.iso"
        path.write_bytes(_xiso_image({"content/pkg": blob, "media/readme.txt": b"0123456789"}))
        image = xiso.XIso(path)
        try:
            assert image.format == "xiso"
            names = sorted(e.path for e in image.files())
            assert names == ["content", "content/pkg", "media", "media/readme.txt"], names
            readme = image.find("MEDIA/README.TXT")
            assert image.read(readme) == b"0123456789"
            assert image.read(readme, 4, 3) == b"456"
            assert image.read(readme, 20) == b""          # past the end: nothing, not the rest of the image
            pkg = stfs.in_image(image, image.find("content/pkg"))
            assert pkg.read("Media/a.bin") == payload
        finally:
            image.close()
        pkg = stfs.open_package(f"{path}::content/pkg")
        assert pkg.read("Media/a.bin") == payload
        pkg.src.stream.close()


# --- minidump ---------------------------------------------------------------------------

def test_minidump_access_violation() -> None:
    blob = bytearray(0x1000)
    blob[0:4] = b"MDMP"
    struct.pack_into("<II", blob, 8, 3, 0x20)                      # 3 streams, directory at 0x20
    mod, exc, mem, ctx, name, stack = 0x100, 0x200, 0x300, 0x400, 0x500, 0x600
    struct.pack_into("<III", blob, 0x20, minidump.MODULE_LIST, 4 + 108, mod)
    struct.pack_into("<III", blob, 0x2C, minidump.EXCEPTION, 168, exc)
    struct.pack_into("<III", blob, 0x38, minidump.MEMORY_LIST, 4 + 16, mem)
    struct.pack_into("<IQI", blob, mod, 1, 0x400000, 0x100000)
    struct.pack_into("<I", blob, mod + 4 + 20, name)
    text = "C:\\Game\\Driver.exe".encode("utf-16-le")
    struct.pack_into("<I", blob, name, len(text))
    blob[name + 4:name + 4 + len(text)] = text
    struct.pack_into("<IQII", blob, mem, 1, 0x19F000, 16, stack)
    struct.pack_into("<4I", blob, stack, 0, 0x401234, 7, 0)
    struct.pack_into("<IIIIQQI", blob, exc, 42, 0, 0xC0000005, 0, 0, 0x4CD230, 2)
    struct.pack_into("<2Q", blob, exc + 8 + 32, 0, 0x43938357)     # read of 0x43938357
    struct.pack_into("<II", blob, exc + 8 + 152, 0xCC, ctx)
    struct.pack_into("<11I", blob, ctx + 0x9C, 1, 2, 3, 4, 5, 6, 7, 0x4CD230, 0x23, 0x202, 0x19F000)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "crash.dmp"
        path.write_bytes(blob)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            minidump.main([str(path), "--stack", "4"])
    text = out.getvalue()
    assert "Driver.exe+0xcd230" in text, text
    assert "access violation: read of 0x43938357" in text, text
    assert "[esp+0x004] 0x00401234 Driver.exe+0x1234" in text, text


# --- VO3D, CRC, textures, Wwise -----------------------------------------------------------

def test_vo3d_round_trip() -> None:
    v = handling.VO3D(304, {18: 420.5, 0: 1500.0, 345: 0.51})
    raw = v.build()
    again = handling.VO3D.parse(raw)
    assert again.spool_id == 304 and list(again.params) == [18, 0, 345]
    assert again.build() == raw


def test_name_hashes_share_the_crc() -> None:
    assert gui_assets.name_hash("Nissan_370Z") == lua_scripts.crc("nissan_370z")
    assert lua_scripts.com_hash("Common\\A.lua") == lua_scripts.crc("com:luascripts\\common\\a.lua")
    assert lua_scripts.crc("") == 0xFFFFFFFF
    assert traffic_sets.name_hash("Chapter 1") == lua_scripts.crc("Chapter 1")   # case kept


def test_vehiclestats_read_back() -> None:
    """game_data.read_vehiclestats (handling.lua_stats, gui_assets.stock_stats) on the
    layout game_data.vehiclestats_text writes."""
    table = {62: {"Manufacturer": game_data.Num(41, 4), "Year": "1970", "Picture1": "dodge, challenger",
                  "Price": game_data.Num(1.5, 4)},
             63: {"Selectable": True, "Drivetrain": "RWD"}}
    text = game_data.format_table("vehicleStats", table).replace("\n", "\r\n")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "vehiclestats.lua"
        path.write_bytes(text.encode("utf-8"))
        expected = {62: {"Manufacturer": "41", "Year": "1970", "Picture1": "dodge, challenger", "Price": "1.5"},
                    63: {"Selectable": "true", "Drivetrain": "RWD"}}
        assert game_data.read_vehiclestats(path) == expected
        assert handling.lua_stats(path) == expected


def test_xds_tiling_is_a_permutation() -> None:
    for width, pitch in ((32, 8), (32, 16), (64, 16), (64, 4)):
        seen = {(xds.tiled_x(n, width, pitch), xds.tiled_y(n, width, pitch)) for n in range(width * width)}
        assert seen == {(x, y) for x in range(width) for y in range(width)}, (width, pitch)


def test_xds_untile_places_blocks() -> None:
    width = height = 128                 # 32 x 32 DXT1 blocks
    header = bytearray(xds.HEADER_SIZE)
    struct.pack_into(">II", header, 0x20, 18, (width - 1) | ((height - 1) << 13))
    payload = bytearray(32 * 32 * 8)
    for n in range(32 * 32):
        x, y = xds.tiled_x(n, 32, 8), xds.tiled_y(n, 32, 8)
        block = struct.pack("<HH4x", x, y)
        payload[n * 8:n * 8 + 8] = bytes(b for pair in zip(block[1::2], block[0::2]) for b in pair)
    blocks, w, h, fourcc = xds.untile(bytes(header + payload))
    assert (w, h, fourcc) == (width, height, b"DXT1")
    for y in range(32):
        for x in range(32):
            assert struct.unpack_from("<HH", blocks, (y * 32 + x) * 8) == (x, y)


def test_xds_dds_header() -> None:
    from dsfforza.convert import dsf_materials
    blocks = bytes(range(8)) * 4                 # 8 x 8 texels, DXT1
    dds = xds.dds_bytes(blocks, 8, 8, b"DXT1")
    assert len(dds) == dsf_materials.DDS_HEADER_SIZE + len(blocks)
    assert dds[84:88] == b"DXT1" and struct.unpack_from("<I", dds, 4)[0] == 124
    assert dsf_materials.decode_dds(dds) == (8, 8, xds.decode_rgba(blocks, 8, 8, b"DXT1"))


def _wem(samples: list[int]) -> bytes:
    data = wb.encode_ima(samples)
    fmt = struct.pack("<HHIIHH", 2, 1, 24000, 24000 * wb.BLOCK // wb.BLOCK_SAMPLES, wb.BLOCK, 4)
    return wb.Wem(fmt, [(b"fmt ", fmt), (b"data", data)]).build()


def test_ima_round_trip() -> None:
    x = [int(12000 * math.sin(i / 7)) for i in range(640)]
    w = wb.Wem.parse(_wem(x))
    y = w.pcm()[0]
    assert len(y) == 640 and w.rate == 24000
    noise = sum((a - b) ** 2 for a, b in zip(x, y))
    signal = sum(a * a for a in x)
    assert 10 * math.log10(signal / noise) > 30


def test_ima_first_block_starts_at_its_step() -> None:
    """A loud start: the first block picks its own step index instead of climbing
    from the smallest one (9 dB SNR from index 0 here)."""
    x = [int(20000 * math.sin(i / 3)) for i in range(256)]
    data = wb.encode_ima(x)
    assert data[2] > 40   # the first block header's step index
    y = wb.decode_ima(data)[0]
    noise = sum((a - b) ** 2 for a, b in zip(x[:64], y[:64]))
    assert 10 * math.log10(sum(a * a for a in x[:64]) / noise) > 25


def test_bank_round_trip_and_media_swap() -> None:
    a, b = _wem([0] * 64), _wem([1000] * 128)
    bkhd = struct.pack("<IIII", wb.VERSION, 0x1234, 0, 0) + b"\0" * 8
    didx = struct.pack("<III", 1, 0, len(a)) + struct.pack("<III", 2, -(-len(a) // 16) * 16, len(b))
    data = a.ljust(-(-len(a) // 16) * 16, b"\0") + b

    def section(tag: bytes, payload: bytes) -> bytes:
        return tag + struct.pack("<I", len(payload)) + payload
    raw = section(b"BKHD", bkhd) + section(b"DIDX", didx) + section(b"DATA", data)
    bank = wb.Bank.parse(raw)
    assert bank.build() == raw and bank.bank_id == 0x1234
    bank.replace_media({1: b})
    again = wb.Bank.parse(bank.build())
    assert [m for _, m in again.media] == [b, b]
    try:
        wb.Bank.parse(raw[:-10])
    except ValueError:
        pass
    else:
        raise AssertionError("a truncated bank parsed")


def test_read_strings_unterminated() -> None:
    """gui_assets.read_strings stops at the end of SLRS when the last string has no 0."""
    slrc = struct.pack("<II", 0, 7) + struct.pack("<II", 6, 9)
    slrs = "AB\0".encode("utf-16-le") + "CD".encode("utf-16-le")
    slrr = chnk.Entry("SLRR", 0, 0, 1, 2, None, children=[_leaf("SLRC", slrc), _leaf("SLRS", slrs)])
    spoo = chnk.Entry("SPOO", 0, 0, 1, 2, None, children=[chnk.Entry("NONR", 0, 0, 1, 2, None, children=[slrr])])
    out = io.BytesIO()
    chnk.write_chunk(out, None, [spoo])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "global.fchunk"
        path.write_bytes(out.getvalue())
        assert gui_assets.read_strings(path) == {7: "AB", 9: "CD"}


def main() -> int:
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as error:
            failed += 1
            print(f"FAIL  {name}: {type(error).__name__}: {error}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
