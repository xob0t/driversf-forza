"""Extract compiled Lua scripts from Driver: San Francisco script packages.

A script package (media/Scripts*.fchunk) is a CHNK holding:

    SCRR  Script Package Root (nested CHNK)
      SCRH  header: u32 version (1), u32 count, u32 checksum, u32 flags (PC: 2)
      SCRC  lookup: count x {u32 offset into SCRS, u32 hash, u32 unused}
      SCRS  the compiled chunks, back to back in lookup order
    BINF  build info (UTF-16 text; missing in repacked packages)

Each chunk is Lua 5.1 bytecode with 4-byte float numbers (see
research/game-lua.md). The hash is a CRC-32 variant (com_hash) of the
lowercased "COM:luaScripts\\<path>"; the path comes from the
main function's source name, "@r:\\resources\\luascripts\\common\\\\<path>",
where the doubled backslash marks the package root.

    uv run dsff lua_scripts list <package.fchunk>
    uv run dsff lua_scripts extract <package.fchunk> <out dir>
    uv run dsff lua_scripts extract-all <media dir> <out dir>

extract writes <out dir>/<path>.luac; extract-all puts each package in
<out dir>/<package name>/.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from dsfforza.formats import chnk

# Vanilla Lua 5.1 on x86 except byte 10: sizeof(lua_Number) is 4 (float, not double).
LUA51_HEADER = b"\x1bLua\x51\x00\x01\x04\x04\x04\x04\x00"


def _crc_table() -> list[int]:
    # Reflected CRC-32 (0xEDB88320) table, indexed by the complemented byte and
    # with the top byte flipped; no final complement. Matches the game's hashes.
    table = []
    for index in range(256):
        value = index ^ 0xFF
        for _ in range(8):
            value = (value >> 1) ^ (0xEDB88320 if value & 1 else 0)
        table.append(value ^ 0xFF000000)
    return table


CRC_TABLE = _crc_table()


def find(entries, tag):
    for entry in entries:
        if entry.tag == tag:
            return entry
    raise ValueError(f"no {tag}")


def crc(text: str) -> int:
    """The game's CRC-32 variant of `text` as given (callers lower-case where the game does)."""
    value = 0xFFFFFFFF
    for char in text:
        value = (value >> 8) ^ CRC_TABLE[(ord(char) ^ value) & 0xFF]
    return value


def com_hash(path: str) -> int:
    return crc(("COM:luaScripts\\" + path).lower())


def script_path(source: str, hash_: int) -> str | None:
    """Package-relative path from a chunk's source name, or None."""
    name = source.lstrip("@=")
    root = name.find("\\\\")
    if root != -1 and com_hash(name[root + 2 :]) == hash_:
        return name[root + 2 :]
    # No root marker: try ever longer suffixes against the hash.
    parts = [p for p in name.replace("/", "\\").split("\\") if p]
    for count in range(1, len(parts) + 1):
        candidate = "\\".join(parts[-count:])
        if com_hash(candidate) == hash_:
            return candidate
    # Packaged under another name (filelist.lua for competitivefilelist.lua).
    return name[root + 2 :] if root != -1 else None


def read_package(path: Path) -> list[dict]:
    with path.open("rb") as stream:
        root = chnk.read_chunk(stream)
        scrr = find(root.entries, "SCRR")
        scrh, scrc, scrs = (find(scrr.children, t) for t in ("SCRH", "SCRC", "SCRS"))
        stream.seek(scrh.offset)
        version, count, _checksum, flags = struct.unpack("<4I", stream.read(16))
        if version != 1:
            raise ValueError(f"{path}: SCRH version {version:#x} (big-endian package?)")
        stream.seek(scrc.offset)
        lookup = [struct.unpack("<3I", stream.read(12)) for _ in range(count)]
        stream.seek(scrs.offset)
        blob = stream.read(scrs.size)
    scripts = []
    bounds = [offset for offset, _, _ in lookup] + [len(blob)]
    for i, (offset, hash_, _) in enumerate(lookup):
        data = blob[offset : bounds[i + 1]]
        source = None
        if len(data) >= 16 and data[:4] == b"\x1bLua":
            (length,) = struct.unpack_from("<I", data, 12)
            if length:
                source = data[16 : 16 + length - 1].decode("latin-1")
        scripts.append(
            {
                "hash": hash_,
                "data": data,
                "header": data[:12],
                "source": source,
                "path": script_path(source, hash_) if source else None,
                "flags": flags,
            }
        )
    return scripts


def cmd_list(args) -> None:
    for s in read_package(Path(args.file)):
        std = "std" if s["header"] == LUA51_HEADER else s["header"].hex()
        print(f"{s['hash']:08X}\t{len(s['data'])}\t{std}\t{s['path'] or s['source']}")


def extract(package: Path, out: Path) -> tuple[int, int]:
    count = nonstd = 0
    for s in read_package(package):
        rel = s["path"] or f"${s['hash']:08X}"
        # Keep every file inside `out`: one SplitScreen script is named
        # "..\..\common\gamelogic\onlinescreenmanager\ss_menu.lua".
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        rel = "/".join(parts)
        target = out / (rel + "c" if rel.lower().endswith(".lua") else rel + ".luac")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(s["data"])
        count += 1
        if s["header"] != LUA51_HEADER:
            nonstd += 1
            print(f"non-standard header: {package.name} {rel}: {s['header'].hex()}", file=sys.stderr)
    return count, nonstd


def cmd_extract(args) -> None:
    count, nonstd = extract(Path(args.file), Path(args.out))
    print(f"{count} scripts, {nonstd} with a non-standard header")


def cmd_extract_all(args) -> None:
    for package in sorted(Path(args.media).glob("Scripts*.fchunk")):
        name = package.stem.removeprefix("Scripts").replace(" - Copy", ".copy").replace(" ", "")
        count, nonstd = extract(package, Path(args.out) / name)
        print(f"{package.name}\t{name}\t{count}\t{nonstd}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("list")
    p.add_argument("file")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("extract")
    p.add_argument("file")
    p.add_argument("out")
    p.set_defaults(func=cmd_extract)
    p = sub.add_parser("extract-all")
    p.add_argument("media")
    p.add_argument("out")
    p.set_defaults(func=cmd_extract_all)
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
