"""Read Forza car archives (zip files whose entries use method 21, XMemCompress LZX).

Usage:
    uv run dsff forzazip list <car.zip>
    uv run dsff forzazip extract <car.zip> <out-dir> [pattern]

Every extracted entry is checked against the zip's CRC32.
"""

from __future__ import annotations

import argparse
import fnmatch
import struct
import sys
import zipfile
import zlib
from pathlib import Path

from dsfforza.formats.lzx import xmem_decompress

LZX_METHOD = 21


class ForzaZip:
    def __init__(self, path: str | Path):
        self.zip = zipfile.ZipFile(path)

    def entries(self) -> list[zipfile.ZipInfo]:
        return self.zip.infolist()

    def _raw(self, info: zipfile.ZipInfo) -> bytes:
        stream = self.zip.fp
        stream.seek(info.header_offset)
        header = stream.read(30)
        name_len, extra_len = struct.unpack_from("<HH", header, 26)
        stream.seek(info.header_offset + 30 + name_len + extra_len)
        return stream.read(info.compress_size)

    def read(self, name: str | zipfile.ZipInfo) -> bytes:
        info = name if isinstance(name, zipfile.ZipInfo) else self.zip.getinfo(name)
        if info.compress_type == LZX_METHOD:
            data = xmem_decompress(self._raw(info), info.file_size)
        else:
            data = self.zip.read(info)
        if zlib.crc32(data) & 0xFFFFFFFF != info.CRC:
            raise ValueError(f"{info.filename}: CRC mismatch")
        return data


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    lst = sub.add_parser("list")
    lst.add_argument("zip", type=Path)
    ext = sub.add_parser("extract")
    ext.add_argument("zip", type=Path)
    ext.add_argument("out", type=Path)
    ext.add_argument("pattern", nargs="?", default="*")
    args = parser.parse_args(argv)

    archive = ForzaZip(args.zip)
    if args.command == "list":
        for info in archive.entries():
            print(f"{info.compress_type:3} {info.file_size:>10} {info.filename}")
        return
    for info in archive.entries():
        if info.is_dir() or not fnmatch.fnmatch(info.filename.lower(), args.pattern.lower()):
            continue
        target = args.out / info.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(info))
        print(f"{info.file_size:>10} {info.filename}")


if __name__ == "__main__":
    sys.exit(main())
