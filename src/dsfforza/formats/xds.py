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

    uv run dsff xds <in.xds> <out.dds> [--png preview.png]
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

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
    tile = (((offset_t >> (5 + log_bpp)) & 2) + (offset_b >> 6)) & 3
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
    micro = (((offset_t & (((texel_pitch << 6) - 1) & ~31)) + ((offset_t & 15) << 1)) >> (3 + log_bpp)) & ~1
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
    out = np.zeros((blocks_w * blocks_h, block_size), dtype=np.uint8)
    # Every whole block of the payload, up to the aligned surface (as the block loop read them).
    count = min(aligned_w * aligned_h, len(payload) // block_size)
    n = np.arange(count, dtype=np.int64)
    x, y = tiled_x(n, blocks_w, block_size), tiled_y(n, blocks_w, block_size)
    inside = (x < blocks_w) & (y < blocks_h)
    src = np.frombuffer(payload, dtype=np.uint8, count=count * block_size).reshape(count, block_size)[inside]
    dst = (y * blocks_w + x)[inside]
    # A later block wins where two land on one place (never seen; the loop's order).
    _, last = np.unique(dst[::-1], return_index=True)
    keep = len(dst) - 1 - last
    swapped = src[keep].reshape(-1, block_size // 2, 2)[:, :, ::-1].reshape(-1, block_size)  # 16-bit byte swap
    out[dst[keep]] = swapped
    return out.tobytes(), width, height, fourcc


def dds_bytes(blocks: bytes, width: int, height: int, fourcc: bytes) -> bytes:
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000  # caps, height, width, pixelformat, linearsize
    # "DDS ", DDS_HEADER (124 bytes): size, flags, height, width, linear size, depth,
    # mip count, 11 reserved; pixel format {size 32, flags FOURCC, fourcc, bit count,
    # 4 masks}; caps (TEXTURE), caps2..4, reserved.
    header = struct.pack(
        "<4s7I44s2I4s5I5I",
        b"DDS ",
        124,
        flags,
        height,
        width,
        len(blocks),
        0,
        1,
        bytes(44),
        32,
        0x4,
        fourcc,
        0,
        0,
        0,
        0,
        0,
        0x1000,
        0,
        0,
        0,
        0,
    )
    return header + blocks


def _rgb565(c: int) -> tuple[int, int, int]:
    return ((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31


def decode_rgba(blocks: bytes, width: int, height: int, fourcc: bytes) -> bytearray:
    """Minimal DXT1/DXT5 decoder for previews (DXT3 and ATI2 blocks are read as DXT5)."""
    block_size = 8 if fourcc == b"DXT1" else 16
    blocks_w, blocks_h = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    b = np.frombuffer(blocks, dtype=np.uint8, count=blocks_w * blocks_h * block_size)
    b = b.reshape(blocks_h * blocks_w, block_size).astype(np.int64)
    shifts = np.arange(16, dtype=np.int64)
    if block_size == 16:
        a0, a1 = b[:, 0:1], b[:, 1:2]
        i6, i4 = np.arange(6, dtype=np.int64), np.arange(4, dtype=np.int64)
        table = np.where(
            a0 > a1,
            np.concatenate((a0, a1, ((6 - i6) * a0 + (i6 + 1) * a1) // 7), axis=1),
            np.concatenate(
                (a0, a1, ((4 - i4) * a0 + (i4 + 1) * a1) // 5, np.zeros_like(a0), np.full_like(a0, 255)), axis=1
            ),
        )
        bits = (b[:, 2:8] << (8 * np.arange(6, dtype=np.int64))).sum(axis=1, keepdims=True)
        alpha = np.take_along_axis(table, (bits >> (3 * shifts)) & 7, axis=1)
        b = b[:, 8:]
    else:
        alpha = np.full((len(b), 16), 255, dtype=np.int64)
    c0 = b[:, 0] | b[:, 1] << 8
    c1 = b[:, 2] | b[:, 3] << 8
    idx = b[:, 4] | b[:, 5] << 8 | b[:, 6] << 16 | b[:, 7] << 24

    def rgb565(c):
        return np.stack((((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31), axis=1)

    p0, p1 = rgb565(c0), rgb565(c1)
    four = ((c0 > c1) | (block_size == 16))[:, None]
    colours = np.stack(
        (
            p0,
            p1,
            np.where(four, (2 * p0 + p1) // 3, (p0 + p1) // 2),
            np.where(four, (p0 + 2 * p1) // 3, 0),
        ),
        axis=1,
    )  # (blocks, 4, 3)
    texel = (idx[:, None] >> (2 * shifts)) & 3  # (blocks, 16)
    rgb = np.take_along_axis(colours, texel[:, :, None], axis=1)  # (blocks, 16, 3)
    rgba = np.concatenate((rgb, alpha[:, :, None]), axis=2).astype(np.uint8)
    # (by, bx, row, column, channel) -> rows of texels, cropped to the image.
    image = rgba.reshape(blocks_h, blocks_w, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(blocks_h * 4, blocks_w * 4, 4)
    return bytearray(image[:height, :width].tobytes())


def write_png(path: Path, width: int, height: int, rgba: bytearray) -> None:
    rows = b"".join(b"\0" + bytes(rgba[y * width * 4 : (y + 1) * width * 4]) for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


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
