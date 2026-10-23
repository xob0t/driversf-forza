"""Minimal PE32 reader: map virtual addresses of Driver.exe to file bytes."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Section:
    name: str
    va: int  # absolute virtual address
    vsize: int
    raw_offset: int
    raw_size: int


class PE:
    def __init__(self, path: str | Path):
        self.data = Path(path).read_bytes()
        pe_offset = struct.unpack_from("<I", self.data, 0x3C)[0]
        if self.data[pe_offset:pe_offset + 4] != b"PE\0\0":
            raise ValueError("not a PE file")
        sections, optional_size = struct.unpack_from("<2xH12xH", self.data, pe_offset + 4)
        optional = pe_offset + 24
        self.image_base = struct.unpack_from("<I", self.data, optional + 28)[0]
        self.sections = []
        for index in range(sections):
            base = optional + optional_size + index * 40
            name = self.data[base:base + 8].rstrip(b"\0").decode("latin-1")
            vsize, rva, raw_size, raw_offset = struct.unpack_from("<IIII", self.data, base + 8)
            self.sections.append(Section(name, self.image_base + rva, vsize, raw_offset, raw_size))

    def section(self, va: int) -> Section:
        for section in self.sections:
            if section.va <= va < section.va + max(section.vsize, section.raw_size):
                return section
        raise KeyError(f"{va:#x} is not in any section")

    def read(self, va: int, size: int) -> bytes:
        """Bytes at va; zero-filled where the section has no file data (e.g. .bss)."""
        section = self.section(va)
        start = va - section.va
        out = bytearray(size)
        available = max(0, min(size, section.raw_size - start))
        out[:available] = self.data[section.raw_offset + start:section.raw_offset + start + available]
        return bytes(out)

    def has_file_data(self, va: int) -> bool:
        section = self.section(va)
        return va - section.va < section.raw_size
