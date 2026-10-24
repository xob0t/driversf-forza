"""Read Xbox 360 STFS content packages (CON / LIVE / PIRS): list and extract files.

Written from the public STFS descriptions (Free60 wiki, Velocity docs). All
multi-byte fields are big-endian unless noted.

Header (0x22C license block onwards is common to all three magics):
    0x000 magic "CON " | "LIVE" | "PIRS"
    0x340 u32 header_size        0x344 u32 content_type
    0x360 u32 title_id           0x379 volume descriptor (0x24 bytes)
    0x411 display name (UTF-16BE, 0x80 bytes per locale)
    0xD11 description (UTF-16BE)       0x1691 title name (UTF-16BE)

STFS volume descriptor:
    u8 size (0x24), u8 reserved, u8 flags,
    u16 file_table_block_count (LE), int24 file_table_block (LE),
    20-byte top hash, u32 allocated_blocks, u32 unallocated_blocks

Data is stored in 4 KiB blocks starting at round_up(header_size, 0x1000).
Every 170 (0xAA) data blocks are preceded by a level-0 hash table; every
170*170 blocks by a level-1 table; one level-2 table covers the rest. Each
table entry is SHA-1 (20 bytes), u8 status, u24 next_block (the block chain).
The flags bit 0 picks the layout:

    bit 0 set   "female" (read-only, e.g. marketplace/disc packages): one
                copy of each hash table;
    bit 0 clear "male" (read-write, e.g. saves): two copies of each table,
                the active one chosen by status bit 0x40 in the parent
                table entry (flags bit 1 for the top table).

The file table is a chain of blocks of 64-byte entries:
    char name[40], u8 flags (len = flags & 0x3F, 0x40 consecutive blocks,
    0x80 directory), int24 LE valid_blocks, int24 LE allocated_blocks,
    int24 LE start_block, i16 parent (-1 = root), u32 size, u32 mtime, u32 atime

Packages can be read straight out of a disc image: pass "image.iso::path/in/iso".

    uv run dsff stfs info    <package>
    uv run dsff stfs list    <package> [pattern]
    uv run dsff stfs extract <package> <out-dir> [pattern] [--verify]
    uv run dsff stfs verify  <package> [--data]
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import struct
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

BLOCK = 0x1000
PER_TABLE = 0xAA  # entries per hash table
PER_L1 = PER_TABLE * PER_TABLE  # 0x70E4 blocks covered by one level-1 table
MAGICS = (b"CON ", b"LIVE", b"PIRS")
END_OF_CHAIN = 0xFFFFFF


class StfsError(ValueError):
    pass


@dataclass
class StfsEntry:
    index: int
    path: str
    flags: int
    valid_blocks: int
    allocated_blocks: int
    start_block: int
    parent: int
    size: int

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & 0x80)

    @property
    def consecutive(self) -> bool:
        return bool(self.flags & 0x40)


class _Window:
    """A read-only view of [base, base + size) in a seekable stream."""

    def __init__(self, stream: BinaryIO, base: int = 0, size: int | None = None):
        self.stream, self.base = stream, base
        if size is None:
            stream.seek(0, 2)
            size = stream.tell() - base
        self.size = size

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or offset + size > self.size:
            raise StfsError(f"read {offset:#x}+{size:#x} outside package ({self.size:#x} bytes)")
        self.stream.seek(self.base + offset)
        return self.stream.read(size)


def _utf16(raw: bytes) -> str:
    return raw.decode("utf-16-be", "replace").split("\0")[0]


class Stfs:
    def __init__(self, stream: BinaryIO, base: int = 0, size: int | None = None):
        self.src = _Window(stream, base, size)
        head = self.src.read(0, 0x1800)
        self.magic = head[:4]
        if self.magic not in MAGICS:
            raise StfsError(f"not an STFS package (magic {self.magic!r})")
        self.header_size, self.content_type = struct.unpack_from(">II", head, 0x340)
        self.content_size = struct.unpack_from(">Q", head, 0x34C)[0]
        self.title_id = struct.unpack_from(">I", head, 0x360)[0]
        self.display_name = _utf16(head[0x411:0x491])
        self.description = _utf16(head[0xD11:0xD91])
        self.title_name = _utf16(head[0x1691:0x1711])
        vd = head[0x379 : 0x379 + 0x24]
        if vd[0] != 0x24:
            raise StfsError(f"volume descriptor size {vd[0]:#x}, expected 0x24 (SVOD packages are not supported)")
        self.vd_flags = vd[2]
        self.file_table_blocks = struct.unpack_from("<H", vd, 3)[0]
        self.file_table_start = int.from_bytes(vd[5:8], "little")
        self.top_hash = vd[8:28]
        self.allocated_blocks, self.unallocated_blocks = struct.unpack_from(">II", vd, 0x1C)
        # 0 = female (one table copy), 1 = male (two copies per table)
        self.sex = 0 if self.vd_flags & 1 else 1
        self.steps = (0xAB, 0x718F) if self.sex == 0 else (0xAC, 0x723A)
        self.first_table = (self.header_size + 0xFFF) & ~0xFFF
        n = self.allocated_blocks
        self.top_level = 0 if n <= PER_TABLE else 1 if n <= PER_L1 else 2
        self._table_cache: dict[tuple[int, int], int] = {}
        self.entries = self._read_file_table()

    # -- block addressing -------------------------------------------------

    def _backing_data_block(self, block: int) -> int:
        """Position of data block `block` counting every 4 KiB block, hash tables included."""
        sex = self.sex
        result = (((block + PER_TABLE) // PER_TABLE) << sex) + block
        if block < PER_TABLE:
            return result
        result += ((block + PER_L1) // PER_L1) << sex
        if block < PER_L1:
            return result
        return result + (1 << sex)

    def _backing_table_block(self, block: int, level: int) -> int:
        """Position of the level-N hash table that covers data block `block`."""
        sex = self.sex
        if level == 0:
            if block < PER_TABLE:
                return 0
            pos = (block // PER_TABLE) * self.steps[0] + (((block // PER_L1) + 1) << sex)
            return pos if block < PER_L1 else pos + (1 << sex)
        if level == 1:
            if block < PER_L1:
                return self.steps[0]
            return (1 << sex) + (block // PER_L1) * self.steps[1]
        return self.steps[1]

    def _offset(self, backing: int) -> int:
        return self.first_table + backing * BLOCK

    def block_offset(self, block: int) -> int:
        return self._offset(self._backing_data_block(block))

    def _table_offset(self, block: int, level: int) -> int:
        key = (block // (PER_TABLE ** (level + 1)), level)
        if key in self._table_cache:
            return self._table_cache[key]
        offset = self._offset(self._backing_table_block(block, level))
        if self.sex == 1:
            # Male packages hold two copies of each table; pick the active one.
            if level >= self.top_level:
                second = bool(self.vd_flags & 2)
            else:
                second = bool(self.src.read(self._entry_offset(block, level + 1) + 0x14, 1)[0] & 0x40)
            offset += BLOCK if second else 0
        self._table_cache[key] = offset
        return offset

    def _entry_offset(self, block: int, level: int) -> int:
        slot = (block // (PER_TABLE**level)) % PER_TABLE
        return self._table_offset(block, level) + slot * 0x18

    def hash_entry(self, block: int) -> tuple[bytes, int, int]:
        """(sha1, status, next_block) for data block `block`."""
        raw = self.src.read(self._entry_offset(block, 0), 0x18)
        return raw[:20], raw[20], int.from_bytes(raw[21:24], "big")

    def verify_tree(self, data: bool = False) -> tuple[int, int]:
        """Check the hash tree: top table against the volume descriptor, each table
        against its parent entry, and (with data=True) every data block.
        Returns (tables checked, data blocks checked); raises StfsError on a mismatch."""
        n = self.allocated_blocks
        top = self.src.read(self._table_offset(0, self.top_level), BLOCK)
        if hashlib.sha1(top).digest() != self.top_hash:
            raise StfsError("top hash table does not match the volume descriptor")
        tables = 1
        for level in range(self.top_level - 1, -1, -1):
            span = PER_TABLE ** (level + 1)
            for block in range(0, n, span):
                table = self.src.read(self._table_offset(block, level), BLOCK)
                expected = self.src.read(self._entry_offset(block, level + 1), 20)
                if hashlib.sha1(table).digest() != expected:
                    raise StfsError(f"level-{level} table for block {block}: SHA-1 mismatch")
                tables += 1
        blocks = 0
        if data:
            for block in range(n):
                self.read_block(block, verify=True)
                blocks += 1
        return tables, blocks

    def read_block(self, block: int, verify: bool = False) -> bytes:
        if block >= self.allocated_blocks:
            raise StfsError(f"block {block} beyond allocated count {self.allocated_blocks}")
        data = self.src.read(self.block_offset(block), BLOCK)
        if verify and hashlib.sha1(data).digest() != self.hash_entry(block)[0]:
            raise StfsError(f"block {block}: SHA-1 mismatch")
        return data

    def chain(self, start: int, count: int, consecutive: bool) -> list[int]:
        if consecutive:
            return list(range(start, start + count))
        blocks, block = [], start
        while len(blocks) < count:
            if block == END_OF_CHAIN or block >= self.allocated_blocks:
                raise StfsError(f"block chain from {start} ends after {len(blocks)} of {count} blocks")
            blocks.append(block)
            block = self.hash_entry(block)[2]
        return blocks

    # -- file table -------------------------------------------------------

    def _read_file_table(self) -> list[StfsEntry]:
        # An entry's index is its slot in the table, empty slots included: that
        # is what the parent field refers to.
        entries = []
        slot = 0
        for block in self.chain(self.file_table_start, self.file_table_blocks, False):
            data = self.read_block(block)
            for i in range(0, BLOCK, 0x40):
                e = data[i : i + 0x40]
                flags = e[0x28]
                if flags & 0x3F:
                    name = e[: flags & 0x3F].decode("latin-1")
                    parent, size = struct.unpack_from(">hI", e, 0x32)
                    entries.append(
                        StfsEntry(
                            slot,
                            name,
                            flags,
                            int.from_bytes(e[0x29:0x2C], "little"),
                            int.from_bytes(e[0x2C:0x2F], "little"),
                            int.from_bytes(e[0x2F:0x32], "little"),
                            parent,
                            size,
                        )
                    )
                slot += 1
        by_slot = {entry.index: entry for entry in entries}
        names = {entry.index: entry.path for entry in entries}
        for entry in entries:  # resolve full paths
            parts, seen, p = [entry.path], set(), entry.parent
            while p in by_slot and p not in seen:
                seen.add(p)
                parts.append(names[p])
                p = by_slot[p].parent
            entry.path = "/".join(reversed(parts))
        return entries

    def files(self) -> Iterator[StfsEntry]:
        return iter(self.entries)

    def find(self, path: str) -> StfsEntry:
        wanted = path.replace("\\", "/").lower()
        for entry in self.entries:
            if entry.path.lower() == wanted:
                return entry
        raise KeyError(path)

    def iter_data(self, entry: StfsEntry, verify: bool = False) -> Iterator[bytes]:
        """File contents in pieces; contiguous blocks are read in one go."""
        if entry.is_dir:
            raise StfsError(f"{entry.path} is a directory")
        count = (entry.size + BLOCK - 1) // BLOCK
        blocks = self.chain(entry.start_block, count, entry.consecutive)
        remaining, i = entry.size, 0
        while i < len(blocks):
            j = i + 1
            while (
                j < len(blocks)
                and j - i < 256
                and self.block_offset(blocks[j]) == self.block_offset(blocks[j - 1]) + BLOCK
            ):
                j += 1
            data = self.src.read(self.block_offset(blocks[i]), (j - i) * BLOCK)
            if verify:
                for k in range(i, j):
                    chunk = data[(k - i) * BLOCK : (k - i + 1) * BLOCK]
                    if hashlib.sha1(chunk).digest() != self.hash_entry(blocks[k])[0]:
                        raise StfsError(f"{entry.path}: block {blocks[k]} SHA-1 mismatch")
            piece = data[:remaining]
            remaining -= len(piece)
            yield piece
            i = j

    def blocks_of(self, entry: StfsEntry) -> list[int]:
        count = (entry.size + BLOCK - 1) // BLOCK
        return self.chain(entry.start_block, count, entry.consecutive)

    def open(self, entry: StfsEntry | str) -> io.BufferedReader:
        """A seekable binary file object over one file (e.g. for zipfile.ZipFile)."""
        entry = self.find(entry) if isinstance(entry, str) else entry
        return io.BufferedReader(_StfsFile(self, entry), buffer_size=64 * 1024)

    def read(self, entry: StfsEntry | str, verify: bool = False) -> bytes:
        entry = self.find(entry) if isinstance(entry, str) else entry
        return b"".join(self.iter_data(entry, verify))

    def extract(self, entry: StfsEntry, out: Path, verify: bool = False) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as target:
            for piece in self.iter_data(entry, verify):
                target.write(piece)


class _StfsFile(io.RawIOBase):
    def __init__(self, pkg: Stfs, entry: StfsEntry):
        self.pkg, self.entry, self.pos = pkg, entry, 0
        self.blocks = pkg.blocks_of(entry)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        base = (0, self.pos, self.entry.size)[whence]
        self.pos = max(0, base + offset)
        return self.pos

    def readinto(self, buffer) -> int:
        size = min(len(buffer), self.entry.size - self.pos)
        if size <= 0:
            return 0
        index, within = divmod(self.pos, BLOCK)
        # read up to the end of this run of physically contiguous blocks
        start = self.pkg.block_offset(self.blocks[index]) + within
        span = BLOCK - within
        while (
            span < size
            and index + 1 < len(self.blocks)
            and self.pkg.block_offset(self.blocks[index + 1]) == self.pkg.block_offset(self.blocks[index]) + BLOCK
        ):
            index += 1
            span += BLOCK
        size = min(size, span)
        buffer[:size] = self.pkg.src.read(start, size)
        self.pos += size
        return size


def in_image(image, entry) -> Stfs:
    """The package stored as file `entry` of an xiso.XIso disc image, read in place."""
    from dsfforza.formats.xiso import SECTOR

    return Stfs(image.stream, image.base + entry.sector * SECTOR, entry.size)


def open_package(spec: str) -> Stfs:
    """Open "pkg" or "image.iso::path/in/image" (an XDVDFS disc image)."""
    if "::" in spec:
        image_path, inner = spec.split("::", 1)
        from dsfforza.formats.xiso import XIso

        image = XIso(image_path)
        return in_image(image, image.find(inner))
    return Stfs(Path(spec).open("rb"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    info = sub.add_parser("info")
    info.add_argument("package")
    ver = sub.add_parser("verify")
    ver.add_argument("package")
    ver.add_argument("--data", action="store_true", help="also hash every data block (reads the whole package)")
    lst = sub.add_parser("list")
    lst.add_argument("package")
    lst.add_argument("pattern", nargs="?", default="*")
    ext = sub.add_parser("extract")
    ext.add_argument("package")
    ext.add_argument("out", type=Path)
    ext.add_argument("pattern", nargs="?", default="*")
    ext.add_argument("--verify", action="store_true", help="check every block against its SHA-1 hash entry")
    args = parser.parse_args(argv)

    pkg = open_package(args.package)
    if args.command == "info":
        print(f"magic          {pkg.magic.decode()}")
        print(f"title          {pkg.title_name} ({pkg.title_id:08X})")
        print(f"display name   {pkg.display_name}")
        print(f"description    {pkg.description}")
        print(f"content type   {pkg.content_type:#010x}, content size {pkg.content_size}")
        print(f"header size    {pkg.header_size:#x}, first block at {pkg.first_table:#x}")
        print(
            f"layout         {'female (single hash tables)' if pkg.sex == 0 else 'male (paired hash tables)'}, "
            f"top hash level {pkg.top_level}"
        )
        print(f"blocks         {pkg.allocated_blocks} allocated, {pkg.unallocated_blocks} free")
        print(
            f"file table     {pkg.file_table_blocks} block(s) from block {pkg.file_table_start}, "
            f"{len(pkg.entries)} entries"
        )
        return
    if args.command == "verify":
        tables, blocks = pkg.verify_tree(args.data)
        print(f"hash tree OK: {tables} tables" + (f", {blocks} data blocks" if args.data else ""))
        return
    if args.command == "list":
        for e in pkg.files():
            if fnmatch.fnmatch(e.path.lower(), args.pattern.lower()):
                print(f"{'d' if e.is_dir else '-'} {e.size:>12} {e.path}")
        return
    for e in pkg.files():
        if e.is_dir or not fnmatch.fnmatch(e.path.lower(), args.pattern.lower()):
            continue
        pkg.extract(e, args.out / e.path, args.verify)
        print(f"{e.size:>12} {e.path}")


if __name__ == "__main__":
    sys.exit(main())
