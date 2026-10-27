"""Read 7z archives with Python's own lzma module (LZMA, LZMA2, BCJ x86, Delta, Copy).

Usage:
    uv run dsff sevenzip list <archive.7z>
    uv run dsff sevenzip extract <archive.7z> <out-dir>

DriverNGHook ships as DNGHook_072.7z (LZMA2, solid, encoded header). tools/install.ps1
unpacked it with Windows' own tar.exe, which only decodes LZMA where its libarchive was
built with liblzma; on the others the install stopped with "tar.exe: LZMA codec is
unsupported". This reader needs nothing outside the standard library.

Not read: encrypted archives (7zAES), BCJ2 and other multi-input coders, PPMd, BZip2,
Deflate. Those raise SevenZipError. Every extracted file is checked against its CRC32.
"""

from __future__ import annotations

import argparse
import lzma
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

SIGNATURE = b"7z\xbc\xaf\x27\x1c"

K_END, K_HEADER, K_ARCHIVE_PROPERTIES, K_ADDITIONAL_STREAMS = 0x00, 0x01, 0x02, 0x03
K_MAIN_STREAMS, K_FILES_INFO, K_PACK_INFO, K_UNPACK_INFO = 0x04, 0x05, 0x06, 0x07
K_SUBSTREAMS_INFO, K_SIZE, K_CRC, K_FOLDER = 0x08, 0x09, 0x0A, 0x0B
K_CODERS_UNPACK_SIZE, K_NUM_UNPACK_STREAM, K_EMPTY_STREAM, K_EMPTY_FILE = 0x0C, 0x0D, 0x0E, 0x0F
K_NAME, K_WIN_ATTRIBUTES, K_ENCODED_HEADER, K_DUMMY = 0x11, 0x15, 0x17, 0x19

METHOD_COPY, METHOD_DELTA, METHOD_X86 = b"\x00", b"\x03", b"\x03\x03\x01\x03"
METHOD_LZMA, METHOD_LZMA2 = b"\x03\x01\x01", b"\x21"
ATTRIBUTE_DIRECTORY = 0x10


class SevenZipError(Exception):
    pass


@dataclass
class Coder:
    method: bytes
    props: bytes
    inputs: int = 1
    outputs: int = 1


@dataclass
class Folder:
    coders: list[Coder]
    bind_pairs: list[tuple[int, int]]  # (in index, out index)
    packed: list[int]  # in indexes fed by pack streams
    unpack_sizes: list[int] = field(default_factory=list)  # per out stream
    crc: int | None = None
    streams: int = 1  # files in it
    sizes: list[int] = field(default_factory=list)
    crcs: list[int | None] = field(default_factory=list)

    def size(self) -> int:
        """The folder's output: the out stream no bind pair consumes."""
        bound = {out for _, out in self.bind_pairs}
        return next(s for i, s in enumerate(self.unpack_sizes) if i not in bound)


@dataclass
class Streams:
    pack_pos: int = 0
    pack_sizes: list[int] = field(default_factory=list)
    folders: list[Folder] = field(default_factory=list)


@dataclass
class Member:
    name: str
    is_dir: bool = False
    has_stream: bool = True
    size: int = 0
    crc: int | None = None
    folder: int = -1
    offset: int = 0  # in the folder's output


class Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise SevenZipError("header ends early")
        self.pos += 1
        return self.data[self.pos - 1]

    def bytes(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise SevenZipError("header ends early")
        self.pos += n
        return self.data[self.pos - n : self.pos]

    def u32(self) -> int:
        return struct.unpack("<I", self.bytes(4))[0]

    def number(self) -> int:
        """7z's variable-length UINT64: the leading 1 bits of the first byte count the bytes that follow."""
        first, mask, value = self.byte(), 0x80, 0
        for i in range(8):
            if not first & mask:
                return value | ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
            mask >>= 1
        return value

    def bits(self, n: int) -> list[bool]:
        out, byte, mask = [], 0, 0
        for _ in range(n):
            if not mask:
                byte, mask = self.byte(), 0x80
            out.append(bool(byte & mask))
            mask >>= 1
        return out

    def defined(self, n: int) -> list[bool]:
        """An "all defined" byte, else a bit vector."""
        return [True] * n if self.byte() else self.bits(n)

    def digests(self, n: int) -> list[int | None]:
        return [self.u32() if d else None for d in self.defined(n)]

    def expect(self, prop: int) -> None:
        got = self.number()
        if got != prop:
            raise SevenZipError(f"header: property {got:#x} where {prop:#x} belongs")


def read_pack_info(r: Reader, s: Streams) -> None:
    s.pack_pos = r.number()
    count = r.number()
    while (prop := r.number()) != K_END:
        if prop == K_SIZE:
            s.pack_sizes = [r.number() for _ in range(count)]
        elif prop == K_CRC:
            r.digests(count)
        else:
            raise SevenZipError(f"pack info: property {prop:#x}")


def read_folder(r: Reader) -> Folder:
    coders = []
    for _ in range(r.number()):
        flags = r.byte()
        if flags & 0x80:
            raise SevenZipError("alternative coder methods are not read")
        method = r.bytes(flags & 0x0F)
        inputs, outputs = (r.number(), r.number()) if flags & 0x10 else (1, 1)
        props = r.bytes(r.number()) if flags & 0x20 else b""
        coders.append(Coder(method, props, inputs, outputs))
    total_in, total_out = sum(c.inputs for c in coders), sum(c.outputs for c in coders)
    pairs = [(r.number(), r.number()) for _ in range(total_out - 1)]
    count = total_in - len(pairs)
    if count == 1:
        bound = {i for i, _ in pairs}
        packed = [next(i for i in range(total_in) if i not in bound)]
    else:
        packed = [r.number() for _ in range(count)]
    return Folder(coders, pairs, packed)


def read_unpack_info(r: Reader, s: Streams) -> None:
    r.expect(K_FOLDER)
    count = r.number()
    if r.byte():
        raise SevenZipError("external folder data is not read")
    s.folders = [read_folder(r) for _ in range(count)]
    r.expect(K_CODERS_UNPACK_SIZE)
    for f in s.folders:
        f.unpack_sizes = [r.number() for _ in range(sum(c.outputs for c in f.coders))]
    while (prop := r.number()) != K_END:
        if prop == K_CRC:
            for f, crc in zip(s.folders, r.digests(len(s.folders)), strict=True):
                f.crc = crc
        else:
            raise SevenZipError(f"unpack info: property {prop:#x}")


def read_substreams_info(r: Reader, s: Streams) -> None:
    prop = r.number()
    if prop == K_NUM_UNPACK_STREAM:
        for f in s.folders:
            f.streams = r.number()
        prop = r.number()
    sizes_given = prop == K_SIZE
    for f in s.folders:
        if not f.streams:
            continue
        sizes = [r.number() for _ in range(f.streams - 1)] if sizes_given else []
        f.sizes = [*sizes, f.size() - sum(sizes)]
    if sizes_given:
        prop = r.number()
    # CRCs of the streams whose folder has no CRC of its own (a folder of one stream with one does).
    unknown = [(f, i) for f in s.folders for i in range(f.streams) if not (f.streams == 1 and f.crc is not None)]
    for f in s.folders:
        f.crcs = [f.crc] if f.streams == 1 and f.crc is not None else [None] * f.streams
    while prop != K_END:
        if prop == K_CRC:
            for (f, i), crc in zip(unknown, r.digests(len(unknown)), strict=True):
                f.crcs[i] = crc
        else:
            raise SevenZipError(f"substreams info: property {prop:#x}")
        prop = r.number()


def read_streams_info(r: Reader) -> Streams:
    s = Streams()
    substreams = False
    while (prop := r.number()) != K_END:
        if prop == K_PACK_INFO:
            read_pack_info(r, s)
        elif prop == K_UNPACK_INFO:
            read_unpack_info(r, s)
        elif prop == K_SUBSTREAMS_INFO:
            read_substreams_info(r, s)
            substreams = True
        else:
            raise SevenZipError(f"streams info: property {prop:#x}")
    if not substreams:
        for f in s.folders:
            f.sizes, f.crcs = [f.size()], [f.crc]
    return s


def stored_lzma2(data: bytes) -> bytes:
    """data as LZMA2 stored (uncompressed) chunks plus the end mark: what a BCJ or Delta
    filter runs over, since liblzma only runs them ahead of LZMA1/LZMA2, and LZMA2's end
    mark makes the filter flush its last bytes."""
    out = bytearray()
    for start in range(0, len(data), 0x10000):
        chunk = data[start : start + 0x10000]
        out += bytes([1 if start == 0 else 2]) + struct.pack(">H", len(chunk) - 1) + chunk
    return bytes(out + b"\x00")


def decode_coder(coder: Coder, data: bytes, size: int) -> bytes:
    """One coder's output (size bytes) from its input. Each runs on its own, to the size the
    header gives: raw LZMA1 without an end mark keeps its last bytes back otherwise."""
    p = coder.props
    if coder.method == METHOD_COPY:
        return data
    if coder.method == METHOD_LZMA:
        if len(p) != 5:
            raise SevenZipError("LZMA: bad properties")
        return lzma.LZMADecompressor(lzma.FORMAT_ALONE).decompress(p + struct.pack("<Q", size) + data, size)
    if coder.method == METHOD_LZMA2:
        if len(p) != 1 or p[0] > 40:
            raise SevenZipError("LZMA2: bad properties")
        dict_size = 0xFFFFFFFF if p[0] == 40 else (2 | (p[0] & 1)) << (p[0] // 2 + 11)
        return lzma.LZMADecompressor(
            lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": dict_size}]
        ).decompress(data, size)
    if coder.method == METHOD_X86:
        bcj = {"id": lzma.FILTER_X86}
    elif coder.method == METHOD_DELTA:
        bcj = {"id": lzma.FILTER_DELTA, "dist": (p[0] + 1) if p else 1}
    else:
        raise SevenZipError(
            f"compression method {coder.method.hex()} is not read (LZMA, LZMA2, BCJ x86, Delta, Copy are)"
        )
    chain = [bcj, {"id": lzma.FILTER_LZMA2, "dict_size": 1 << 16}]
    return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=chain).decompress(stored_lzma2(data), size)


def coder_chain(f: Folder) -> list[int]:
    """The folder's coders from the one that writes the output to the one that reads the pack
    stream. With one input and output each, coder i has in and out stream i; a bind pair
    feeds out stream `out` into in stream `in` (7-Zip writes LZMA as coder 0 and BCJ, which
    reads it, as coder 1)."""
    if any(c.inputs != 1 or c.outputs != 1 for c in f.coders):
        raise SevenZipError("multi-stream coders (BCJ2) are not read")
    feeds = dict(f.bind_pairs)  # in stream -> the out stream that feeds it
    bound = set(feeds.values())
    current = next(i for i in range(len(f.coders)) if i not in bound)
    chain = [current]
    while current in feeds:
        current = feeds[current]
        if current in chain:
            raise SevenZipError("coders bound in a loop")
        chain.append(current)
    if len(chain) != len(f.coders) or f.packed != [current]:
        raise SevenZipError("coders that are not a simple chain are not read")
    return chain


def decode_folder(f: Folder, packed: bytes) -> bytes:
    """One folder's output, checked against its CRC."""
    data = packed
    for i in reversed(coder_chain(f)):  # from the pack stream to the output
        size = f.unpack_sizes[i]
        try:
            data = decode_coder(f.coders[i], data, size)
        except lzma.LZMAError as error:
            raise SevenZipError(f"corrupt data: {error}") from error
        if len(data) < size:
            raise SevenZipError(f"a coder decodes to {len(data)} bytes, {size} expected")
        data = data[:size]
    if f.crc is not None and zlib.crc32(data) != f.crc:
        raise SevenZipError("CRC error in a folder")
    return data


class SevenZip:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.blob = self.path.read_bytes()
        if self.blob[:6] != SIGNATURE or len(self.blob) < 32:
            raise SevenZipError(f"{self.path.name} is not a 7z archive")
        offset, size, crc = struct.unpack_from("<QQI", self.blob, 12)
        if zlib.crc32(self.blob[12:32]) != struct.unpack_from("<I", self.blob, 8)[0]:
            raise SevenZipError(f"{self.path.name}: start header CRC error")
        header = self.blob[32 + offset : 32 + offset + size]
        if len(header) != size or zlib.crc32(header) != crc:
            raise SevenZipError(f"{self.path.name} is cut short or damaged (header CRC)")
        r = Reader(header)
        while (kind := r.number()) == K_ENCODED_HEADER:  # the header itself, compressed
            s = read_streams_info(r)
            r = Reader(self.folder_data(s, 0))
        if kind != K_HEADER:
            raise SevenZipError(f"{self.path.name}: header type {kind:#x}")
        self.streams, self.members = self.read_header(r)

    def folder_data(self, s: Streams, index: int) -> bytes:
        f = s.folders[index]
        first = sum(len(g.packed) for g in s.folders[:index])
        start = 32 + s.pack_pos + sum(s.pack_sizes[:first])
        packed = self.blob[start : start + s.pack_sizes[first]]
        if len(packed) != s.pack_sizes[first]:
            raise SevenZipError(f"{self.path.name} is cut short")
        return decode_folder(f, packed)

    def read_header(self, r: Reader) -> tuple[Streams, list[Member]]:
        s, members = Streams(), []
        prop = r.number()
        if prop == K_ARCHIVE_PROPERTIES:
            while r.number() != K_END:
                r.bytes(r.number())
            prop = r.number()
        if prop == K_ADDITIONAL_STREAMS:
            raise SevenZipError("additional streams are not read")
        if prop == K_MAIN_STREAMS:
            s = read_streams_info(r)
            prop = r.number()
        if prop == K_FILES_INFO:
            members = self.read_files(r, s)
            prop = r.number()
        if prop != K_END:
            raise SevenZipError(f"header: property {prop:#x}")
        return s, members

    def read_files(self, r: Reader, s: Streams) -> list[Member]:
        count = r.number()
        empty_stream, empty_file, names, attributes = [False] * count, [], [], [None] * count
        while (prop := r.number()) != K_END:
            size = r.number()
            end = r.pos + size
            if prop == K_EMPTY_STREAM:
                empty_stream = r.bits(count)
            elif prop == K_EMPTY_FILE:
                empty_file = r.bits(sum(empty_stream))
            elif prop == K_NAME:
                if r.byte():
                    raise SevenZipError("external names are not read")
                names = r.bytes(end - r.pos).decode("utf-16-le").split("\0")[:count]
            elif prop == K_WIN_ATTRIBUTES:
                defined = r.defined(count)
                if r.byte():
                    raise SevenZipError("external attributes are not read")
                attributes = [r.u32() if d else None for d in defined]
            r.pos = end  # times, kDummy, anti items and the rest: skipped
        if len(names) != count:
            raise SevenZipError("file names missing")
        streams = [(fi, i) for fi, f in enumerate(s.folders) for i in range(f.streams)]
        members, k, e = [], 0, 0
        for i in range(count):
            m = Member(names[i])
            if empty_stream[i]:
                m.has_stream = False
                is_file = empty_file[e] if e < len(empty_file) else False
                attr = attributes[i]
                m.is_dir = not is_file if attr is None else bool(attr & ATTRIBUTE_DIRECTORY)
                e += 1
            else:
                if k >= len(streams):
                    raise SevenZipError("more files than streams")
                m.folder, j = streams[k]
                f = s.folders[m.folder]
                m.offset, m.size, m.crc = sum(f.sizes[:j]), f.sizes[j], f.crcs[j]
                k += 1
            members.append(m)
        return members

    def extract(self, folder: str | Path) -> list[Path]:
        """Every member into folder; a name that would land outside it is refused before
        anything is written. Returns the files and folders written."""
        folder = Path(folder)
        base = folder.resolve()
        targets = []
        for m in self.members:
            parts = PurePosixPath(m.name.replace("\\", "/")).parts
            target = (folder.joinpath(*parts)).resolve() if parts else base
            if not parts or ":" in m.name or m.name.startswith(("/", "\\")) or base not in target.parents:
                raise SevenZipError(f"{self.path.name}: {m.name!r} points outside the folder")
            targets.append(target)
        decoded: dict[int, bytes] = {}
        for m, target in zip(self.members, targets, strict=True):
            if m.is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            data = b""
            if m.has_stream:
                if m.folder not in decoded:
                    decoded[m.folder] = self.folder_data(self.streams, m.folder)
                data = decoded[m.folder][m.offset : m.offset + m.size]
                if m.crc is not None and zlib.crc32(data) != m.crc:
                    raise SevenZipError(f"{self.path.name}: CRC error in {m.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the members").add_argument("archive", type=Path)
    x = sub.add_parser("extract", help="every member into a folder")
    x.add_argument("archive", type=Path)
    x.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    try:
        archive = SevenZip(args.archive)
        if args.cmd == "list":
            for m in archive.members:
                print(f"{'<dir>' if m.is_dir else m.size:>10}  {m.name}")
        else:
            print(f"{len(archive.extract(args.out))} entries into {args.out}")
    except (SevenZipError, OSError) as error:
        print(f"sevenzip: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
