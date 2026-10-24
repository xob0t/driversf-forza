"""Clone a vehicle in dngvehicles.sp under a new spool ID.

Copies the source vehicle's three VEHC variants, rewrites the vehicle ID in
the four places variant 0 stores it, appends matching SSLP and VSCT records,
and writes a new archive. The original file is only read.

Variant 0 stores the ID as a u32 at:

    VSVH +0x00   vehicle sound header
    VO3D +0x00   handling data
    SPID +0x00   shader param vehicle id
    UPVH +0x14   hierarchy UID

By default the clone keeps the source's model and texture UIDs (MDID/UIDC)
and MODL part UIDs, which is fine while the data stays identical (confirmed in
game with both loaded). With --new-uids every resource and MODL part the three
entries define gets a fresh stock-shaped UID, and the UPVH's part references
follow, so the clone's data can change without clashing with the source in
the game's global UID tables (tools/port_car.py does this).

Example:
    python tools/clone_vehicle.py "$DSF_GAME_DIR/Install/dngvehicles.sp" \\
        out/dngvehicles.clone.sp --source 266 --id 304

The functions (make_clones, renew_uids, register_clones) work on an archive
tree in memory, so several clones can go into one write.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import struct
import sys
from pathlib import Path

from chnk import ENTRY_LIMIT, Entry, read_chunk, walk, write_chunk
from vehicles import SSLP_RECORD, VSCT_RECORD, find, parse_sslp

ID_FIELDS = {"VSVH": 0x00, "VO3D": 0x00, "SPID": 0x00, "UPVH": 0x14}
KIND_OFFSETS = (0, 1000, 2000)

# DRSP leaves that define or reference resource UIDs, plus the MODL submodel
# lists. A scan of the Desanne's three entries found its 285 DRSP-defined UIDs
# only in these, never in UPVH, VO3D, VSRT or SPSR.
UID_LEAVES = {"SBBH", "MDID", "VXHD", "VXDP", "IXHD", "IXDP", "MAHE", "MARP", "UIDC", "MODL"}
UID_HEADERS = {"SBMC": "SBBH", "VXCK": "VXHD", "IXCK": "IXHD", "TEXC": "UIDC", "MACO": "MAHE"}


def read_leaf(stream, entry: Entry) -> bytes:
    if entry.payload is not None:
        return entry.payload
    stream.seek(entry.offset)
    return stream.read(entry.size)


def stock_like_uid(prefix: bytes, counter: int) -> bytes:
    """A resource UID shaped like the stock ones: [counter<<4] 0x12 [counter>>4]
    prefix[0..2] prefix[3]&0x7F, then byte 7 = 0x2D. Byte 7 must keep its top
    bit clear (research/upvh.md); this is the layout that loads in game
    (forza2dsf.Builder.new_uid). Unique for counters 1..0xFFF per prefix."""
    if not 0 < counter <= 0xFFF:
        raise ValueError("UID counter out of range (1..4095 per prefix)")
    p = prefix
    return bytes([(counter << 4) & 0xFF, 0x12, counter >> 4, p[0], p[1], p[2], p[3] & 0x7F, 0x2D])


def patch_ids(stream, vehc: Entry, old_id: int, new_id: int) -> list[str]:
    """Rewrite the vehicle ID inside one VEHC; returns the tags patched."""
    patched = []
    for _, entry in walk(vehc.children):
        offset = ID_FIELDS.get(entry.tag)
        if offset is None or entry.children is not None:
            continue
        data = bytearray(read_leaf(stream, entry))
        found = struct.unpack_from("<I", data, offset)[0]
        if found != old_id:
            raise ValueError(f"{entry.tag}+{offset:#x} holds {found}, expected {old_id}")
        struct.pack_into("<I", data, offset, new_id)
        entry.payload = bytes(data)
        patched.append(entry.tag)
    return patched


def defined_uids(stream, vehc: Entry) -> list[bytes]:
    """UIDs of the resources (submodels, buffers, textures, materials) a VEHC's DRSPs define."""
    out = []
    for mdxn in (c for c in vehc.children or [] if c.tag == "MDXN"):
        for drsp in (c for c in mdxn.children if c.tag == "DRSP"):
            for c in drsp.children:
                if c.tag in UID_HEADERS:
                    head = next(k for k in c.children if k.tag == UID_HEADERS[c.tag])
                    out.append(read_leaf(stream, head)[:8])
    return out


def _remap(data: bytes, mapping: dict[bytes, bytes]) -> bytes:
    out = bytearray(data)
    i, end = 0, len(data) - 7
    while i < end:
        new = mapping.get(data[i:i + 8])
        if new is None:
            i += 1
        else:
            out[i:i + 8] = new
            i += 8
    return bytes(out)


def part_uids(stream, vehc: Entry) -> list[bytes]:
    """UIDs of the MODL part records of one VEHC (MODL byte order)."""
    from upvh import modl_part_uids
    modl = next(c for c in next(c for c in vehc.children if c.tag == "NONR").children if c.tag == "MODL")
    return modl_part_uids(read_leaf(stream, modl))


def renew_uids(stream, vehcs: list[Entry], prefix: bytes, parts: bool = True) -> dict[bytes, bytes]:
    """Give every resource the VEHC entries define a fresh UID and rewrite the
    references to it inside those entries (SBBH, buffer dependency lists, MDID,
    material texture parameters, MODL submodel lists). With `parts` (default)
    the MODL part UIDs get fresh ones too, and the UPVH references to them
    (header model UID, records, LOD links, attachments) follow. Only the
    shared engine materials, which no entry defines, keep theirs. Returns
    old -> new.

    Needed once the clone's data differs from its source's (textures,
    materials, geometry): the game keeps global tables keyed by UID, one for
    resources and one for MODL parts (FUN_00494390). In the stock archive all
    6,139 part UIDs are unique. Clones that kept the host's part UIDs rendered
    as the host as soon as the host's entries were loaded too (run
    20261022-024010: the ported Skyline turned into the Desanne once
    VehicleLodSpooler.RequestVehicle(266) re-spooled 266's exterior)."""
    old = []
    for vehc in vehcs:
        old += defined_uids(stream, vehc)
    if len(set(old)) != len(old):
        raise ValueError("duplicate resource UIDs inside the entries")
    old_parts = [u for vehc in vehcs for u in part_uids(stream, vehc)] if parts else []
    if len(set(old_parts)) != len(old_parts) or set(old_parts) & set(old):
        raise ValueError("duplicate part UIDs inside the entries")
    mapping = {u: stock_like_uid(prefix, n + 1) for n, u in enumerate(old + old_parts)}
    for vehc in vehcs:
        for _, entry in walk(vehc.children):
            if entry.children is None and entry.tag in UID_LEAVES:
                data = read_leaf(stream, entry)
                new = _remap(data, mapping)
                if new != data:
                    entry.payload = new
            elif entry.children is None and entry.tag == "UPVH" and old_parts:
                entry.payload = remap_upvh_parts(read_leaf(stream, entry), mapping)
    if old_parts:
        _check_no_old_parts(stream, vehcs, set(old_parts))
    return mapping


def remap_upvh_parts(data: bytes, mapping: dict[bytes, bytes]) -> bytes:
    """Rewrite the part UIDs a UPVH refers to (stored with the 32-bit halves
    swapped): the header's model UID (+0x04), every record's UID (+0x00), both
    halves of each LOD link, and each attachment's part UID (+0x10; the rest
    of an attachment is exporter junk, so only values in `mapping` change).
    These are all the places the stock Desanne's UPVH holds its 41 part UIDs."""
    import upvh
    h = upvh.parse(data)
    out = bytearray(data)

    def fix(offset: int) -> None:
        new = mapping.get(upvh.swap_uid(bytes(out[offset:offset + 8])))
        if new is not None:
            out[offset:offset + 8] = upvh.swap_uid(new)

    fix(0x04)
    for r in range(len(h.records)):
        fix(upvh.HEADER_SIZE + r * upvh.RECORD_SIZE)
    for n in range(h.counts["lod_links"]):
        fix(h.offsets["lod_links"] + n * 0x10)
        fix(h.offsets["lod_links"] + n * 0x10 + 8)
    for n in range(h.counts["attachments"]):
        fix(h.offsets["attachments"] + n * 0x20 + 0x10)
    return bytes(out)


def _check_no_old_parts(stream, vehcs: list[Entry], old: set[bytes]) -> None:
    """After renewal no leaf may still hold an old part UID, in either half order."""
    swapped = {u[4:] + u[:4] for u in old}
    for vehc in vehcs:
        for _, entry in walk(vehc.children):
            if entry.children is not None:
                continue
            data = read_leaf(stream, entry)
            for i in range(0, len(data) - 7, 4):
                word = data[i:i + 8]
                if word in old or word in swapped:
                    raise ValueError(f"{entry.tag}+{i:#x} still holds the host's part UID {word.hex()}")


def sslp_records(stream, entries: list[Entry]) -> list[tuple[int, int, int]]:
    nonr = find(find(entries, "SSIC").children, "NONR")
    return parse_sslp(read_leaf(stream, find(nonr.children, "SSLP")))


def taken_ids(stream, entries: list[Entry]) -> set[int]:
    """Base IDs (spool ID % 1000) already used in an archive."""
    return {spool_id % 1000 for spool_id, _, _ in sslp_records(stream, entries)}


def make_clones(stream, entries: list[Entry], source_id: int, new_id: int, name: str | None) -> list[Entry]:
    """Copies of the source's three VEHC entries (kinds 0, 1, 2) with the
    vehicle ID rewritten. They are not in the archive yet (register_clones)."""
    records = sslp_records(stream, entries)
    by_id = {spool_id: (index, kind) for spool_id, index, kind in records}
    taken = {spool_id % 1000 for spool_id, _, _ in records}
    if source_id not in by_id or by_id[source_id][1] != 0:
        raise SystemExit(f"{source_id} is not a base vehicle ID")
    if new_id >= 1000 or new_id in taken:
        raise SystemExit(f"{new_id} is taken or out of range (base IDs are < 1000)")

    variants = [entries[by_id[source_id + k][0]] for k in KIND_OFFSETS]
    clones = [copy.deepcopy(v) for v in variants]
    for vehc in clones:
        if name is not None:
            vehc.name = name
            vehc.name_raw = name.encode("latin-1")
    patched = patch_ids(stream, clones[0], source_id, new_id)
    if sorted(patched) != sorted(ID_FIELDS):
        raise ValueError(f"expected to patch {sorted(ID_FIELDS)}, patched {patched}")
    for vehc in clones[1:]:
        if patch_ids(stream, vehc, source_id, new_id):
            raise ValueError("variants 1 and 2 were not expected to hold the ID")
    return clones


def copy_sound(stream, entries: list[Entry], full_clone: Entry, sound_id: int, new_id: int) -> dict:
    """Give a clone's full entry another stock vehicle's engine sound: replace its
    NONR/VSRT (VSVH header, SBAK Wwise bank, GRBK granular bank on 18 cars) with a
    copy of `sound_id`'s and write the clone's ID at VSVH+0. The VSRT is
    self-contained: the bank IDs and event hashes it uses live inside it, and
    VSVH+0 is the only vehicle ID in it (research/hosts.md). A clone already
    shares its host's bank ID with the host, so sharing it with the sound host is
    the same situation. Returns sizes for the report."""
    records = sslp_records(stream, entries)
    index = next((i for s, i, k in records if s == sound_id and k == 0), None)
    if index is None:
        raise SystemExit(f"--sound-host {sound_id} is not a base vehicle ID")
    source = find(find(entries[index].children, "NONR").children, "VSRT")
    nonr = find(full_clone.children, "NONR")
    at = next(n for n, c in enumerate(nonr.children) if c.tag == "VSRT")
    old_size = nonr.children[at].size
    vsrt = copy.deepcopy(source)
    vsvh = find(vsrt.children, "VSVH")
    data = bytearray(read_leaf(stream, vsvh))
    if struct.unpack_from("<I", data, 0)[0] != sound_id:
        raise ValueError(f"VSVH+0 of {sound_id} holds {struct.unpack_from('<I', data, 0)[0]}")
    struct.pack_into("<I", data, 0, new_id)
    vsvh.payload = bytes(data)
    nonr.children[at] = vsrt
    return {"sound_host": sound_id, "vsrt_size": source.size, "replaced_vsrt_size": old_size}


def register_clones(stream, entries: list[Entry], clones: list[Entry], source_id: int, new_id: int) -> range:
    """Insert three cloned VEHC entries after the last VEHC and add their SSLP
    and VSCT records. Returns the root indices of the new entries."""
    nonr = find(find(entries, "SSIC").children, "NONR")
    sslp_entry = find(nonr.children, "SSLP")
    vsct_entry = find(nonr.children, "VSCT")
    sslp_data = read_leaf(stream, sslp_entry)
    vsct_data = read_leaf(stream, vsct_entry)
    records = parse_sslp(sslp_data)

    # Insert after the last VEHC so the VEHC block stays contiguous.
    insert_at = max(i for i, e in enumerate(entries) if e.tag == "VEHC") + 1
    new_indices = range(insert_at, insert_at + len(clones))
    entries[insert_at:insert_at] = clones

    # SSLP: bump the count, shift indices at or past the insertion point, append records.
    header = bytearray(sslp_data[:0x1C])
    count = struct.unpack_from("<H", header, 2)[0]
    struct.pack_into("<H", header, 2, count + len(clones))
    body = bytearray()
    for spool_id, index, kind in records:
        body += SSLP_RECORD.pack(spool_id, index + len(clones) if index >= insert_at else index, kind)
    for k, index in zip(range(3), new_indices):
        body += SSLP_RECORD.pack(new_id + KIND_OFFSETS[k], index, k)
    sslp_entry.payload = bytes(header + body)

    # VSCT: three copies of the source record under the new ID.
    version, vsct_count = struct.unpack_from("<HH", vsct_data, 4)
    source_records = [
        vsct_data[8 + i * VSCT_RECORD.size: 8 + (i + 1) * VSCT_RECORD.size]
        for i in range(vsct_count)
        if struct.unpack_from("<H", vsct_data, 8 + i * VSCT_RECORD.size)[0] == source_id
    ]
    if len(source_records) != 3:
        raise ValueError(f"expected 3 VSCT records for {source_id}, found {len(source_records)}")
    added = b"".join(struct.pack("<H", new_id) + record[2:] for record in source_records)
    vsct_entry.payload = (vsct_data[:4] + struct.pack("<HH", version, vsct_count + 3)
                          + vsct_data[8:] + added)
    return new_indices


def check_root(entries: list[Entry], root_limit: int = ENTRY_LIMIT) -> None:
    """The game reads at most root_limit root entries (1100 unless dsfforza.asi's
    archive module is installed); more corrupts the reader."""
    if len(entries) > root_limit:
        raise SystemExit(f"{len(entries)} root entries (> {root_limit}); pass --root-limit N "
                         f"for an install with dsfforza.asi [archive]")


def parse_ids(text: str) -> list[int]:
    """'304', '304,310' or '1-61,304-999' -> a list of IDs, in order."""
    ids: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
        ids += range(int(low), int(high or low) + 1)
    return ids


def clone(archive: Path, out: Path, source_id: int | list[int], new_id: int | list[int], name: str | None,
          new_uids: bool = False, sound_id: int | None = None, root_limit: int = ENTRY_LIMIT) -> None:
    """One clone, or many: new_id may be a list, and source_id a list the new IDs
    cycle through (limit tests: tools/clone_vehicle.py ... --source 248,293 --id 1-61,304-999)."""
    sources = source_id if isinstance(source_id, list) else [source_id]
    new_ids = new_id if isinstance(new_id, list) else [new_id]
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        entries = root.entries
        for n, nid in enumerate(new_ids):
            sid = sources[n % len(sources)]
            clones = make_clones(stream, entries, sid, nid, name)
            if new_uids:
                prefix = hashlib.sha1(f"clone:{sid}:{nid}".encode()).digest()[:4]
                renewed = len(renew_uids(stream, clones, prefix))
                if len(new_ids) == 1:
                    print(f"renewed {renewed} resource UIDs")
            if sound_id is not None and sound_id != sid:
                report = copy_sound(stream, entries, clones[0], sound_id, nid)
                if len(new_ids) == 1:
                    print(f"engine sound from {sound_id}: {report}")
            new_indices = register_clones(stream, entries, clones, sid, nid)
            if len(new_ids) == 1:
                print(f"cloned {sid} as {nid} (variants {nid}, {nid + 1000}, {nid + 2000})"
                      f" at VEHC {new_indices.start}..{new_indices.stop - 1}")
        if len(new_ids) > 1:
            print(f"cloned {len(new_ids)} vehicles ({new_ids[0]}..{new_ids[-1]}) from {sources}; "
                  f"{len(entries)} root entries")
        check_root(entries, root_limit)
        with out.open("wb") as output:
            write_chunk(output, stream, entries, root.version)
    print(f"wrote {out} ({out.stat().st_size:#x} bytes)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path, help="path to Install/dngvehicles.sp (read only)")
    parser.add_argument("out", type=Path, help="where to write the new archive")
    parser.add_argument("--source", required=True,
                        help="base spool ID to copy, e.g. 266; a comma list is cycled through with several --id")
    parser.add_argument("--id", required=True, help="new base spool ID, or a list/ranges such as 1-61,304-999")
    parser.add_argument("--name", help="VEHC display class for the clone (default: the source's)")
    parser.add_argument("--new-uids", action="store_true",
                        help="give the clone's submodels, buffers, textures, materials and MODL parts "
                             "fresh UIDs")
    parser.add_argument("--sound-host", type=int, help="take the engine sound (NONR/VSRT) from this stock vehicle")
    parser.add_argument("--root-limit", type=int, default=ENTRY_LIMIT,
                        help=f"root entries allowed (default {ENTRY_LIMIT}; more needs dsfforza.asi [archive])")
    args = parser.parse_args(argv)
    if args.out.resolve() == args.archive.resolve():
        raise SystemExit("refusing to overwrite the source archive")
    sources, ids = parse_ids(args.source), parse_ids(args.id)
    clone(args.archive, args.out, sources if len(sources) > 1 else sources[0], ids if len(ids) > 1 else ids[0],
          args.name, args.new_uids, args.sound_host, args.root_limit)


if __name__ == "__main__":
    sys.exit(main())
