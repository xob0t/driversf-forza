"""Read Xbox 360 game disc images (XDVDFS): list and extract files.

The game partition starts at a fixed offset that depends on the disc format
(XGD1/XGD2/XGD3, or 0 for a rebuilt "xiso"). Its volume descriptor sits at
partition + 0x10000:

    "MICROSOFT*XBOX*MEDIA", u32 root_dir_sector, u32 root_dir_size, ...

Directories are binary trees of entries (little-endian, 4-byte aligned):

    u16 left_offset, u16 right_offset   (in dwords; 0 = none, 0xFFFF = padding)
    u32 sector, u32 size, u8 attributes, u8 name_length, char name[]

Sectors are 2048 bytes, relative to the partition start.

Usage:
    python tools/xiso.py list  <image.iso> [pattern]
    python tools/xiso.py extract <image.iso> <path-in-image> <out-file>
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

SECTOR = 2048
MAGIC = b"MICROSOFT*XBOX*MEDIA"
PARTITION_OFFSETS = {"XGD3": 0x2080000, "XGD2": 0xFD90000, "XGD1": 0x18300000, "xiso": 0}
DIRECTORY = 0x10


@dataclass
class FileEntry:
    path: str
    sector: int
    size: int
    attributes: int

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & DIRECTORY)


class XIso:
    def __init__(self, path: str | Path):
        self.stream: BinaryIO = open(path, "rb")
        for name, offset in PARTITION_OFFSETS.items():
            self.stream.seek(offset + 0x10000)
            header = self.stream.read(28)
            if header[:20] == MAGIC:
                self.format, self.base = name, offset
                self.root_sector, self.root_size = struct.unpack_from("<II", header, 20)
                break
        else:
            self.stream.close()
            raise ValueError(f"{path}: no XDVDFS volume descriptor at any known offset")

    def close(self) -> None:
        self.stream.close()

    def read_at(self, sector: int, offset: int, size: int) -> bytes:
        self.stream.seek(self.base + sector * SECTOR + offset)
        return self.stream.read(size)

    def _directory(self, sector: int, size: int, prefix: str) -> Iterator[FileEntry]:
        if size == 0:
            return
        table = self.read_at(sector, 0, size)
        pending, seen = [0], set()
        while pending:
            offset = pending.pop()
            if offset in seen or offset + 14 > len(table):
                continue
            seen.add(offset)
            left, right, entry_sector, entry_size, attributes, name_length = struct.unpack_from("<HHIIBB", table, offset)
            if left == 0xFFFF and right == 0xFFFF:
                continue  # padding
            name = table[offset + 14:offset + 14 + name_length].decode("latin-1")
            entry = FileEntry(prefix + name, entry_sector, entry_size, attributes)
            yield entry
            if entry.is_dir:
                yield from self._directory(entry_sector, entry_size, entry.path + "/")
            if left:
                pending.append(left * 4)
            if right:
                pending.append(right * 4)

    def files(self) -> Iterator[FileEntry]:
        yield from self._directory(self.root_sector, self.root_size, "")

    def find(self, path: str) -> FileEntry:
        wanted = path.replace("\\", "/").lower()
        for entry in self.files():
            if entry.path.lower() == wanted:
                return entry
        raise KeyError(path)

    def read(self, entry: FileEntry, offset: int = 0, size: int | None = None) -> bytes:
        # Clamped: past the end, stream.read(-1) would return the rest of the image
        # and other negative sizes raise.
        size = max(0, entry.size - offset if size is None else min(size, entry.size - offset))
        return self.read_at(entry.sector, offset, size)

    def open(self, entry: FileEntry) -> io.BufferedReader:
        """A seekable file object over one file (e.g. for zipfile.ZipFile)."""
        return io.BufferedReader(_EntryFile(self, entry), buffer_size=64 * 1024)

    def extract(self, entry: FileEntry, out: Path, block: int = 1 << 20) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as target:
            for offset in range(0, entry.size, block):
                target.write(self.read(entry, offset, block))


class _EntryFile(io.RawIOBase):
    def __init__(self, image: XIso, entry: FileEntry):
        self.image, self.entry, self.pos = image, entry, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = max(0, (0, self.pos, self.entry.size)[whence] + offset)
        return self.pos

    def readinto(self, buffer) -> int:
        data = self.image.read(self.entry, self.pos, len(buffer)) if self.pos < self.entry.size else b""
        buffer[:len(data)] = data
        self.pos += len(data)
        return len(data)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    lst = sub.add_parser("list")
    lst.add_argument("image")
    lst.add_argument("pattern", nargs="?", default="*", help="glob on the path, e.g. 'media/cars/*'")
    ext = sub.add_parser("extract")
    ext.add_argument("image")
    ext.add_argument("path")
    ext.add_argument("out", type=Path)
    args = parser.parse_args(argv)

    image = XIso(args.image)
    try:
        if args.command == "list":
            print(f"# {image.format} partition at {image.base:#x}")
            for entry in image.files():
                if fnmatch.fnmatch(entry.path.lower(), args.pattern.lower()):
                    print(f"{'d' if entry.is_dir else '-'} {entry.size:>12} {entry.path}")
        else:
            entry = image.find(args.path)
            image.extract(entry, args.out)
            print(f"extracted {entry.path} ({entry.size} bytes) to {args.out}")
    finally:
        image.close()


if __name__ == "__main__":
    sys.exit(main())
