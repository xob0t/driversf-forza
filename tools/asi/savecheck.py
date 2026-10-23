"""Read a DSF save (autosave.dat) and dsfforza.asi's side file (dsfforza-garage.dat).

    python tools/asi/savecheck.py <savegames folder or autosave.dat> [--ids 305,500]

Checks the stock block (size, ten guard words, version) and lists what it
holds for vehicles: the 304-slot {id, flags} list, the four 320-bit arrays and
the 304-slot owned list, flagging any ID of 304 and up (dsfforza.asi never
writes those into the block; older test saves may have some). Then lists the
side file's entries. Offsets: research/asi.md ("Save layout").
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

BLOCK = 0x4CF8
GUARD = 0x278A106F
GUARD_WORDS = (0, 4, 6, 8, 0x29, 0x6C0, 0x11D9, 0x12DB, 0x1328, 0x133B)
VERSION_AT = 0x4CF0          # profile manager +0x9A8C, relative to the save buffer at +0x4D9C
FLAG_LIST = 0x184 - 0xA4     # {id, flags} x 304 (bit 0 unlocked, 1 owned, 2 new)
BITS = {"unlocked-ever": 0x2088 - 0xA4, "driven": 0x20B0 - 0xA4, "array-3": 0x20D8 - 0xA4,
        "take-a-ride": 0x2100 - 0xA4}
OWNED_LIST = 0x2784 - 0xA4   # id x 304
FLAG_NAMES = ("unlocked", "owned", "new")


def flags_text(flags: int) -> str:
    names = [n for i, n in enumerate(FLAG_NAMES) if flags >> i & 1]
    return ",".join(names) + (f" (+0x{flags & ~7:X})" if flags & ~7 else "") if flags else "-"


def read_block(path: Path) -> dict:
    data = path.read_bytes()
    out: dict = {"size": len(data), "problems": []}
    if len(data) != BLOCK:
        out["problems"].append(f"size {len(data)}, expected {BLOCK}")
        return out
    words = struct.unpack_from(f"<{BLOCK // 4}I", data)
    bad = [i for i in GUARD_WORDS if words[i] != GUARD]
    if bad:
        out["problems"].append("guard words differ at dword " + ", ".join(hex(i) for i in bad))
    out["version"] = struct.unpack_from("<i", data, VERSION_AT)[0]
    if out["version"] != 1:
        out["problems"].append(f"version {out['version']}")
    slots = [struct.unpack_from("<iI", data, FLAG_LIST + 8 * i) for i in range(304)]
    out["flags"] = {vid: f for vid, f in slots if vid != -1}
    out["bits"] = {}
    for name, off in BITS.items():
        raw = int.from_bytes(data[off:off + 40], "little")
        out["bits"][name] = [i for i in range(320) if raw >> i & 1]
    owned = struct.unpack_from("<304i", data, OWNED_LIST)
    out["owned"] = [v for v in owned if v != -1]
    return out


def read_side(path: Path) -> dict:
    data = path.read_bytes()
    magic, version, count, digest = struct.unpack_from("<4sIII", data)
    if magic != b"DSFG" or version != 1:
        return {"problems": [f"not a version 1 side file ({magic!r}, {version})"]}
    entries = {}
    for i in range(count):
        vid, what, flags = struct.unpack_from("<HHI", data, 16 + 8 * i)
        entries[vid] = (what, flags)
    return {"count": count, "entries": entries, "problems": []}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path)
    ap.add_argument("--ids", help="comma-separated IDs to show in full")
    args = ap.parse_args()
    folder = args.path if args.path.is_dir() else args.path.parent
    save = args.path if args.path.is_file() else folder / "autosave.dat"
    block = read_block(save)
    print(f"{save}: {block['size']} bytes, version {block.get('version')}, "
          f"{'OK' if not block['problems'] else 'PROBLEMS: ' + '; '.join(block['problems'])}")
    if "flags" not in block:
        sys.exit(1)
    high = sorted(v for v in block["flags"] if v >= 304)
    print(f"  flag list: {len(block['flags'])} of 304 slots used; IDs >= 304: {high or 'none'}")
    for name, ids in block["bits"].items():
        print(f"  {name}: {len(ids)} bits; IDs >= 304: {[i for i in ids if i >= 304] or 'none'}")
    print(f"  owned list: {len(block['owned'])} of 304; IDs >= 304: {[v for v in block['owned'] if v >= 304] or 'none'}")
    side_path = folder / "dsfforza-garage.dat"
    side = read_side(side_path) if side_path.exists() else None
    if side is None:
        print(f"{side_path}: none")
    elif side["problems"]:
        print(f"{side_path}: {'; '.join(side['problems'])}")
    else:
        print(f"{side_path}: {side['count']} entries")
    want = [int(x) for x in args.ids.split(",")] if args.ids else []
    for vid in want:
        what, flags = side["entries"].get(vid, (0, 0)) if side and not side["problems"] else (0, 0)
        bits = [n for k, n in enumerate(BITS) if what >> k & 1]
        print(f"  id {vid}: block flags {flags_text(block['flags'].get(vid, 0))}; side flags "
              f"{flags_text(flags) if what & 0x10 else '-'}, bits {bits or '-'}, owned list {bool(what & 0x20)}")


if __name__ == "__main__":
    main()
