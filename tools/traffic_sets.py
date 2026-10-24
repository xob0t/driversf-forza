"""Traffic sets ("progressions") from media/san_franciscoAI.fchunk.

civilianTraffic.setTrafficSettingsName(name, frequency) selects one of these
by name; the engine logs "Unable to set progression '%s'. The progression was
not recognised in the city data." for unknown names. Layout (AIEX payload,
little-endian; see research/game-lua.md):

    AIEX +0x50       section directory: {u32 count, u32 offset from AIEX}
    section 0x118    progressions: count x 28 bytes
                       u32 name hash (com_hash variant, case-sensitive, no prefix)
                       u16 start[5]   index into the vehicle ID array
                       u16 count[5]   one list per traffic frequency (0..4)
                       u32 index
    section 0x120    vehicle IDs: count x u16

    python tools/traffic_sets.py [AI.fchunk] [--names-from DIR ...] [--tsv out.tsv]
    python tools/traffic_sets.py [AI.fchunk] --replace "Chapter 1:0:286=304" --out patched.fchunk

--replace swaps one model ID in place (same size, nothing moves): NAME is a
set name, its index or "*" for every set; FREQ a frequency index or "*". The
sets are packed back to back with only 7 spare u16 slots after the array, so
growing a list would mean moving AIEX sections; that isn't supported.

--names-from scans *.lua / *.txt under DIR for quoted strings and keeps any
whose hash matches a progression, so new names can be recovered.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chnk  # noqa: E402
from lua_scripts import crc  # noqa: E402

DIRECTORY = 0x50
PROGRESSIONS = 0x118
VEHICLE_IDS = 0x120
RECORD = struct.Struct("<I5H5HI")

# Every name the shipped Lua passes to setTrafficSettingsName (directly or via
# trafficSettings = "..."), plus the online sets in onlinemissionsync.lua.
KNOWN = [
    "Exposition", "Exposition Drive", "Exposition Chase", "Exposition Bullrun", "Exposition Shift",
    "Chapter 1", "Chapter 2", "Chapter 2 God", "Chapter 3", "Chapter 4", "Chapter 5", "Chapter 6",
    "Chapter 7", "Chapter 8", "Epilogue", "Challenge traffic", "Challenge traffic heavy",
    "Chase traffic 1", "Chase traffic 2", "Chase traffic 3", "Chase traffic 4", "Chase traffic 5",
    "Mind Control", "DeactivatingBombs", "Cut scene traffic",
    "Online Traffic ", "Online Traffic Easy", "Online Traffic Hard", "Online Traffic Pursuit",
    "Online Traffic Trailblazer", "Online Traffic Jump", "Online Traffic Sprint GP",
    "Online Traffic Takedown", "Online Traffic Survival",
]


def name_hash(text: str) -> int:
    """A progression name's hash: the game's CRC of the name as given (case-sensitive)."""
    return crc(text)


def aiex(path: Path) -> tuple[int, bytes]:
    with path.open("rb") as stream:
        root = chnk.read_chunk(stream)
        for _, entry in chnk.walk(root.entries):
            if entry.tag == "AIEX":
                stream.seek(entry.offset)
                return entry.offset, stream.read(entry.size)
    raise ValueError("no AIEX chunk")


def section(data: bytes, slot: int) -> tuple[int, int]:
    count, offset = struct.unpack_from("<II", data, slot)
    return count, offset


def read_sets(path: Path) -> tuple[int, list[dict], list[int], int]:
    base, data = aiex(path)
    count, offset = section(data, PROGRESSIONS)
    id_count, id_offset = section(data, VEHICLE_IDS)
    ids = list(struct.unpack_from(f"<{id_count}H", data, id_offset))
    sets = []
    for i in range(count):
        fields = RECORD.unpack_from(data, offset + i * RECORD.size)
        starts, counts = fields[1:6], fields[6:11]
        sets.append({
            "hash": fields[0],
            "index": fields[11],
            "record_offset": base + offset + i * RECORD.size,
            "lists": [ids[s:s + c] for s, c in zip(starts, counts)],   # one per frequency, maybe empty
            "starts": starts,
            "counts": counts,
        })
    return base, sets, ids, base + id_offset


def vehicle_names() -> dict[int, str]:
    tsv = Path(__file__).resolve().parent.parent / "research" / "vehicles.tsv"
    names = {}
    if tsv.exists():
        for line in tsv.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 4 and parts[1].isdigit():
                names.setdefault(int(parts[1]), parts[3].split("|")[-1])
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", nargs="?", default=None,
                        help="san_franciscoAI.fchunk (default: from $DSF_GAME_DIR)")
    parser.add_argument("--names-from", action="append", default=[])
    parser.add_argument("--tsv", help="write one row per set and frequency")
    parser.add_argument("--models", action="store_true", help="print model names next to IDs")
    parser.add_argument("--replace", action="append", default=[], metavar="NAME:FREQ:OLD=NEW")
    parser.add_argument("--out", help="patched copy to write (required with --replace)")
    args = parser.parse_args()
    if args.file is None:
        import game_paths
        args.file = game_paths.game_dir() / "media" / "san_franciscoAI.fchunk"
    if args.replace:
        return patch(Path(args.file), args.replace, args.out)

    base, sets, ids, ids_at = read_sets(Path(args.file))
    by_hash = {name_hash(n): n for n in KNOWN}
    pattern = re.compile(r'"([^"\\\n]{1,64})"')
    for folder in args.names_from:
        for path in Path(folder).rglob("*"):
            if path.suffix.lower() in (".lua", ".txt") and path.is_file():
                for text in pattern.findall(path.read_text(encoding="latin-1")):
                    by_hash.setdefault(name_hash(text), text)
    models = vehicle_names() if args.models else {}

    print(f"AIEX at {base:#x}; {len(sets)} sets; {len(ids)} vehicle IDs at file offset {ids_at:#x}")
    rows = []
    for s in sets:
        name = by_hash.get(s["hash"], f"?{s['hash']:08X}")
        print(f"[{s['index']:2}] {name!r} hash {s['hash']:08X} record @{s['record_offset']:#x}")
        for freq, lst in enumerate(s["lists"]):
            if not lst:
                continue
            shown = [f"{v}:{models.get(v, '?')}" for v in lst] if models else lst
            print(f"     freq {freq} ({len(lst)}): {shown}")
            rows.append(f"{s['index']}\t{name}\t{s['hash']:08X}\t{freq}\t{len(lst)}\t{','.join(map(str, lst))}")
    if args.tsv:
        Path(args.tsv).write_text("index\tname\thash\tfrequency\tcount\tmodel_ids\n" + "\n".join(rows) + "\n",
                                  encoding="utf-8")
    return 0


def patch(path: Path, specs: list[str], out: str | None) -> int:
    if not out:
        raise SystemExit("--replace needs --out")
    base, sets, ids, ids_at = read_sets(path)
    by_name = {name_hash(n): n for n in KNOWN}
    data = bytearray(path.read_bytes())
    for spec in specs:
        name, freq, change = spec.rsplit(":", 2)
        old, new = (int(v) for v in change.split("="))
        if name == "*":
            matches = sets
        elif name.isdigit():
            matches = [s for s in sets if s["index"] == int(name)]
        else:
            matches = [s for s in sets if s["hash"] == name_hash(name)]
        if not matches:
            raise SystemExit(f"no traffic set {name!r}")
        freqs = range(5) if freq == "*" else [int(freq)]
        total = 0
        for target in matches:
            done = 0
            for f in freqs:
                start, count = target["starts"][f], target["counts"][f]
                for i in range(start, start + count):
                    if ids[i] == old:
                        struct.pack_into("<H", data, ids_at + 2 * i, new)
                        ids[i] = new
                        done += 1
            if done:
                label = by_name.get(target["hash"], f"?{target['hash']:08X}")
                print(f"{label!r} freq {freq}: {old} -> {new} in {done} place(s)")
            total += done
        if not total:
            raise SystemExit(f"{old} not found in {name!r} freq {freq}")
    Path(out).write_bytes(bytes(data))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
