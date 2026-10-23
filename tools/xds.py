"""Convert Xbox 360 .xds textures (Forza) to PC DDS, and preview them as PNG.

An .xds is a 52-byte D3DBaseTexture header (big-endian) followed by the GPU
texture data: DXT blocks laid out in the Xbox 360 tiled order, byte-swapped
in 16-bit units. Converting to DDS needs no image decoding: untile the blocks
of the top mip level with the standard XGAddress2DTiledX/Y formulas, swap the
bytes back, and prepend a DDS header.

Header fields used (fetch constant at +0x1C, big-endian dwords):
    dword1 bits 0..5   data format (18 DXT1, 19 DXT2/3, 20 DXT4/5, 49 DXN;
                       51..53 are the "AS_16_16_16_16" aliases of 18..20)
    dword2 bits 0..12  width - 1;  bits 13..25 height - 1

    python tools/xds.py <in.xds> <out.dds> [--png preview.png]
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

HEADER_SIZE = 52
FORMATS = {18: (b"DXT1", 8), 19: (b"DXT3", 16), 20: (b"DXT5", 16), 49: (b"ATI2", 16)}
# Some FH1 textures use the "AS_16_16_16_16" variants of the same block formats.
FORMATS.update({51: FORMATS[18], 52: FORMATS[19], 53: FORMATS[20]})


def parse_header(data: bytes) -> tuple[int, int, int]:
    if len(data) < HEADER_SIZE:
        raise ValueError("too small for an xds header")
    dword1, dword2 = struct.unpack_from(">II", data, 0x1C + 4)
    fmt = dword1 & 0x3F
    width = (dword2 & 0x1FFF) + 1
    height = ((dword2 >> 13) & 0x1FFF) + 1
    return fmt, width, height


def _log2(n: int) -> int:
    return n.bit_length() - 1


def tiled_x(offset: int, width: int, texel_pitch: int) -> int:
    aligned_width = (width + 31) & ~31
    log_bpp = _log2(texel_pitch)
    offset_b = offset << log_bpp
    offset_t = ((offset_b & ~4095) >> 3) + ((offset_b & 1792) >> 2) + (offset_b & 63)
    offset_m = offset_t >> (7 + log_bpp)
    macro_x = (offset_m % (aligned_width >> 5)) << 2
    tile = ((((offset_t >> (5 + log_bpp)) & 2) + (offset_b >> 6)) & 3)
    macro = (macro_x + tile) << 3
    micro = ((((offset_t >> 1) & ~15) + (offset_t & 15)) & ((texel_pitch << 3) - 1)) >> log_bpp
    return macro + micro


def tiled_y(offset: int, width: int, texel_pitch: int) -> int:
    aligned_width = (width + 31) & ~31
    log_bpp = _log2(texel_pitch)
    offset_b = offset << log_bpp
    offset_t = ((offset_b & ~4095) >> 3) + ((offset_b & 1792) >> 2) + (offset_b & 63)
    offset_m = offset_t >> (7 + log_bpp)
    macro_y = (offset_m // (aligned_width >> 5)) << 2
    tile = ((offset_t >> (6 + log_bpp)) & 1) + ((offset_b & 2048) >> 10)
    macro = (macro_y + tile) << 3
    micro = ((((offset_t & (((texel_pitch << 6) - 1) & ~31)) + ((offset_t & 15) << 1)) >> (3 + log_bpp)) & ~1)
    return macro + micro + ((offset_t & 16) >> 4)


def untile(data: bytes) -> tuple[bytes, int, int, bytes]:
    """Return (linear top-level blocks, width, height, fourcc)."""
    fmt, width, height = parse_header(data)
    if fmt not in FORMATS:
        raise ValueError(f"unsupported xds format {fmt}")
    fourcc, block_size = FORMATS[fmt]
    payload = data[HEADER_SIZE:]
    blocks_w, blocks_h = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    aligned_w, aligned_h = (blocks_w + 31) & ~31, (blocks_h + 31) & ~31
    out = bytearray(blocks_w * blocks_h * block_size)
    for n in range(aligned_w * aligned_h):
        src = n * block_size
        if src + block_size > len(payload):
            break
        x, y = tiled_x(n, blocks_w, block_size), tiled_y(n, blocks_w, block_size)
        if x >= blocks_w or y >= blocks_h:
            continue
        block = bytearray(payload[src:src + block_size])
        block[0::2], block[1::2] = block[1::2], block[0::2]  # 16-bit byte swap
        dst = (y * blocks_w + x) * block_size
        out[dst:dst + block_size] = block
    return bytes(out), width, height, fourcc


def dds_bytes(blocks: bytes, width: int, height: int, fourcc: bytes) -> bytes:
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000  # caps, height, width, pixelformat, linearsize
    header = struct.pack("<4sIIIIIII44sIII4sIIIIIIIIII", b"DDS ", 124, flags, height, width,
                         len(blocks), 0, 1, bytes(44), 32, 0x4, 0, fourcc, 0, 0, 0, 0, 0,
                         0x1000, 0, 0, 0, 0)
    return header + blocks


def _rgb565(c: int) -> tuple[int, int, int]:
    return ((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31


def decode_rgba(blocks: bytes, width: int, height: int, fourcc: bytes) -> bytearray:
    """Minimal DXT1/DXT5 decoder for previews."""
    block_size = 8 if fourcc == b"DXT1" else 16
    blocks_w, blocks_h = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    out = bytearray(width * height * 4)
    for by in range(blocks_h):
        for bx in range(blocks_w):
            b = blocks[(by * blocks_w + bx) * block_size:(by * blocks_w + bx + 1) * block_size]
            alpha = [255] * 16
            if block_size == 16:
                a0, a1 = b[0], b[1]
                table = [a0, a1] + ([((6 - i) * a0 + (i + 1) * a1) // 7 for i in range(6)] if a0 > a1
                                    else [((4 - i) * a0 + (i + 1) * a1) // 5 for i in range(4)] + [0, 255])
                bits = int.from_bytes(b[2:8], "little")
                alpha = [table[(bits >> (3 * i)) & 7] for i in range(16)]
                b = b[8:]
            c0, c1, idx = struct.unpack_from("<HHI", b)
            p0, p1 = _rgb565(c0), _rgb565(c1)
            if c0 > c1 or block_size == 16:
                colours = [p0, p1, tuple((2 * x + y) // 3 for x, y in zip(p0, p1)),
                           tuple((x + 2 * y) // 3 for x, y in zip(p0, p1))]
            else:
                colours = [p0, p1, tuple((x + y) // 2 for x, y in zip(p0, p1)), (0, 0, 0)]
            for i in range(16):
                x, y = bx * 4 + i % 4, by * 4 + i // 4
                if x < width and y < height:
                    o = (y * width + x) * 4
                    out[o:o + 3] = bytes(colours[(idx >> (2 * i)) & 3])
                    out[o + 3] = alpha[i]
    return out


def write_png(path: Path, width: int, height: int, rgba: bytearray) -> None:
    rows = b"".join(b"\0" + bytes(rgba[y * width * 4:(y + 1) * width * 4]) for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("xds", type=Path)
    parser.add_argument("dds", type=Path, nargs="?")
    parser.add_argument("--png", type=Path)
    args = parser.parse_args(argv)
    blocks, width, height, fourcc = untile(args.xds.read_bytes())
    print(f"{args.xds.name}: {fourcc.decode()} {width}x{height}")
    if args.dds:
        args.dds.write_bytes(dds_bytes(blocks, width, height, fourcc))
    if args.png:
        if fourcc not in (b"DXT1", b"DXT5", b"DXT3"):
            raise SystemExit(f"preview not supported for {fourcc.decode()}")
        write_png(args.png, width, height, decode_rgba(blocks, width, height, fourcc))


if __name__ == "__main__":
    sys.exit(main())
