"""Decode the UPVH vehicle hierarchy (AWHF format) in dngvehicles.sp.

UPVH sits in VEHC/NONR of a vehicle's full entry (spool ID N). Layout,
little-endian, checked against all 150 stock vehicles (see research/upvh.md):

    header (0x60)
      0x00 "AWHF"
      0x04 u32 model UID high dword, 0x08 u32 low dword (MODL part 0's UID; DPL kept a
           format byte in the low byte of 0x04, DSF doesn't)
      0x0C u32 type = 26          0x10 u32 record count
      0x14 u32 hierarchy ID = spool ID    0x18 u32 PDL size (PDL runs to the end)
      0x26 u16 anim_size: 0x14-byte units of the animation blob (13 vehicles)
      0x28 u16 markers  0x2A u8 hinges  0x2B u8 bodies  0x2C u8 seats  0x2D u8 mirrors
      0x32 u16 lights   0x34 u16 0x0CE8   0x36 u16 lod_links
      0x50 u16 attachments  0x52 u16 wheels  0x54 12 constant bytes
    records   count x 0x50, depth-first; see Record
    hinges    x 0x30 {vec4 pivot, vec4 axis, f32 limit, f32 param, 8 bytes 0}
    bodies    x 0xB0 {3x4 rotation, vec4 position, vec4 inertia xyz + mass,
                      4x4 matrix (identity), 0x20 bytes 0}
    markers   x 0x20 {vec4 position, vec4 quaternion}
    seats     x 0x30 {vec4 position, vec4 quaternion, u32, u32, 8 bytes 0}
    mirrors   x 0x10 {vec4 position}
    anim      anim_size x 0x14 bytes, undecoded
    lights    x 0x40 {vec4 position, vec4 direction, f32, f32, u32 id, u32 flags, vec4 junk}
    lod_links x 0x10 {u64 interior part UID, u64 exterior part UID} (halves swapped, -1 = none)
    attach    x 0x20 {vec4 position, 16 bytes partly junk}
    wheels    x 0x20 {vec4 hub position, vec4 quaternion}
    PDL       {u32 1, u32 model count, u32 size, u32 0x10}, then collision models:
              {u32 size = 0x10 + n * 0x60, u32 n * 0x60, f32 radius, u32 n}, n x 0x60
              primitives {4x4 matrix, u32 0x60, u32 flags, 8 bytes, vec4 extents, ...}

Part UIDs are stored with their two 32-bit halves swapped relative to MODL.

Usage:
    uv run dsff upvh summary <archive> 3            # VEHC index of a full entry
    uv run dsff upvh summary <archive> --id 304     # or a spool ID
    uv run dsff upvh lint <archive> --id 304        # cross-checks for converted cars
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza.formats.chnk import Entry, read_chunk
from dsfforza.formats.vehicle_mesh import child, payload
from dsfforza.formats.vehicles import find, parse_sslp

RECORD_SIZE = 0x50
HEADER_SIZE = 0x60
NONE_UID = b"\xff" * 8

RECORD_TYPES = {1: "part", 2: "wheel", 3: "marker", 4: "root", 5: "chassis", 17: "light", 18: "seat"}

# (name, entry size) in file order; counts come from the header.
TABLES = [("hinges", 0x30), ("bodies", 0xB0), ("markers", 0x20), ("seats", 0x30), ("mirrors", 0x10),
          ("anim", 0x14), ("lights", 0x40), ("lod_links", 0x10), ("attachments", 0x20), ("wheels", 0x20)]


def swap_uid(uid: bytes) -> bytes:
    """UPVH <-> MODL UID byte order (the two 32-bit halves are swapped)."""
    return uid[4:] + uid[:4]


@dataclass
class Record:
    index: int
    uid: bytes | None  # in MODL byte order
    type: int
    role: int  # +0x0A, part role enum (wheels 27..30, marker kinds ...)
    subtree: int  # +0x14, records in this subtree, self included
    marker: int  # +0x1A -> markers
    hinge: int  # +0x1C -> hinges
    body: int  # +0x1E -> bodies
    collision: int  # +0x20, offset of the collision model from PDL + 0x10, or -1
    seat: int  # +0x26 (i8) -> seats
    mirror: int  # +0x27 (i8) -> mirrors
    model: int  # +0x2A, running number over part/wheel records
    light: int  # +0x30 -> lights
    slot: int  # +0x34, running number over most part records (meaning unknown)
    lod_link: int  # +0x3E -> lod_links
    attachment: int  # +0x40 -> attachments
    wheel: int  # +0x42 -> wheels
    raw: bytes

    @property
    def type_name(self) -> str:
        return RECORD_TYPES.get(self.type, f"type{self.type}")


@dataclass
class Hierarchy:
    model_uid: bytes  # MODL byte order
    format: int
    type: int
    hierarchy_id: int
    pdl_size: int
    counts: dict[str, int]
    offsets: dict[str, int]  # table name -> file offset
    records: list[Record] = field(default_factory=list)
    data: bytes = b""

    def table(self, name: str) -> list[bytes]:
        size = dict(TABLES)[name]
        start = self.offsets[name]
        return [self.data[start + i * size:start + (i + 1) * size] for i in range(self.counts[name])]

    def vec4s(self, name: str, at: int = 0) -> list[tuple[float, ...]]:
        return [struct.unpack_from("<4f", entry, at) for entry in self.table(name)]

    def lod_links(self) -> list[tuple[bytes | None, bytes | None]]:
        out = []
        for entry in self.table("lod_links"):
            pair = []
            for raw in (entry[:8], entry[8:]):
                pair.append(None if raw == NONE_UID else swap_uid(raw))
            out.append(tuple(pair))
        return out

    @property
    def pdl_offset(self) -> int:
        return len(self.data) - self.pdl_size


def parse(data: bytes) -> Hierarchy:
    if data[:4] != b"AWHF":
        raise ValueError(f"not AWHF: {data[:4]!r}")
    word1, word2, type_, count, hierarchy_id, pdl_size = struct.unpack_from("<6I", data, 4)
    model_uid = struct.pack("<II", word2, word1)
    counts = {
        "hinges": data[0x2A], "bodies": data[0x2B],
        "markers": struct.unpack_from("<H", data, 0x28)[0],
        "seats": data[0x2C], "mirrors": data[0x2D],
        "anim": struct.unpack_from("<H", data, 0x26)[0],
        "lights": struct.unpack_from("<H", data, 0x32)[0],
        "lod_links": struct.unpack_from("<H", data, 0x36)[0],
        "attachments": struct.unpack_from("<H", data, 0x50)[0],
        "wheels": struct.unpack_from("<H", data, 0x52)[0],
    }
    offsets, offset = {}, HEADER_SIZE + count * RECORD_SIZE
    for name, size in TABLES:
        offsets[name] = offset
        offset += counts[name] * size
    if offset + pdl_size != len(data):
        raise ValueError(f"tables end at {offset:#x}, but PDL ({pdl_size:#x}) starts at {len(data) - pdl_size:#x}")
    h = Hierarchy(model_uid, word1 & 0xFF, type_, hierarchy_id, pdl_size, counts, offsets, data=data)
    for i in range(count):
        r = HEADER_SIZE + i * RECORD_SIZE
        raw = data[r:r + RECORD_SIZE]
        def i16(off, raw=raw):
            return struct.unpack_from("<h", raw, off)[0]

        def i8(off, raw=raw):
            return struct.unpack_from("<b", raw, off)[0]

        h.records.append(Record(
            index=i, uid=None if raw[:8] == NONE_UID else swap_uid(raw[:8]),
            type=struct.unpack_from("<H", raw, 0x08)[0], role=struct.unpack_from("<H", raw, 0x0A)[0],
            subtree=struct.unpack_from("<H", raw, 0x14)[0],
            marker=i16(0x1A), hinge=i16(0x1C), body=i16(0x1E),
            collision=struct.unpack_from("<i", raw, 0x20)[0],
            seat=i8(0x26), mirror=i8(0x27), model=i16(0x2A), light=i16(0x30), slot=i16(0x34),
            lod_link=i16(0x3E), attachment=i16(0x40), wheel=i16(0x42), raw=raw))
    return h


# --- archive access -------------------------------------------------------------

def entries_for(stream, root_entries: list[Entry], spool_id: int | None, vehc: int | None) -> dict[int, Entry]:
    """Kind -> VEHC entry for a vehicle, from its spool ID (or the full entry's VEHC index)."""
    ssic = find(root_entries, "SSIC")
    records = parse_sslp(payload(stream, find(find(ssic.children, "NONR").children, "SSLP")))
    if spool_id is None:
        spool_id = next(sid for sid, index, kind in records if index == vehc and kind == 0)
    by_id = {sid: index for sid, index, _ in records}
    return {kind: root_entries[by_id[spool_id + kind * 1000]] for kind in (0, 1, 2)
            if spool_id + kind * 1000 in by_id}


def modl_part_uids(data: bytes) -> list[bytes]:
    count = struct.unpack_from("<I", data)[0]
    return [data[0x10 + p * 0x60 + 0x40:0x10 + p * 0x60 + 0x48] for p in range(count)]


def drsp_uids(stream, vehc: Entry) -> list[tuple[str, int, str, bytes]]:
    """(tag, child index, field, uid) for every UID a DRSP defines or references."""
    out = []
    header = {"SBMC": "SBBH", "VXCK": "VXHD", "IXCK": "IXHD", "TEXC": "UIDC", "MACO": "MAHE"}
    for n, c in enumerate(child(child(vehc, "MDXN"), "DRSP").children):
        if c.tag not in header:
            continue
        sub = child(c, header[c.tag])
        d = sub.payload if sub.payload is not None else payload(stream, sub)
        out.append((c.tag, n, "uid", d[:8]))
        if c.tag == "SBMC":
            out += [(c.tag, n, "material", d[8:16]), (c.tag, n, "vertex_buffer", d[16:24]),
                    (c.tag, n, "index_buffer", d[0x30:0x38])]
        if c.tag == "MACO":
            # MARP: {char name[32], u8 value[16], u32 type}; type 9 holds a texture UID.
            for marc in (m for m in c.children if m.tag == "MARC"):
                for marp in (m for m in marc.children if m.tag == "MARP"):
                    p = marp.payload if marp.payload is not None else payload(stream, marp)
                    for k in range(0, len(p) - 51, 52):
                        if struct.unpack_from("<I", p, k + 48)[0] == 9:
                            out.append((c.tag, n, "texture", p[k + 32:k + 40]))
    return out


def shared_uids(stream, root_entries: list[Entry]) -> set[bytes]:
    """Material (MAHE) and texture (UIDC) UIDs the archive's shared chunk SSIC defines."""
    from dsfforza.formats.chnk import walk
    out = set()
    for e in root_entries:
        if e.tag == "SSIC":
            for _, leaf in walk(e.children):
                if leaf.children is None and leaf.tag in ("MAHE", "UIDC"):
                    out.add(_leaf(stream, leaf)[:8])
    return out


def _leaf(stream, entry: Entry) -> bytes:
    return entry.payload if entry.payload is not None else payload(stream, entry)


def defined_by(stream, vehc: Entry) -> tuple[set[bytes], set[bytes]]:
    """(resource UIDs the VEHC's DRSP defines, its MODL part UIDs)."""
    resources = {u for _, _, f, u in drsp_uids(stream, vehc) if f == "uid"}
    return resources, set(modl_part_uids(_leaf(stream, child(child(vehc, "NONR"), "MODL"))))


def references(stream, kinds: dict[int, Entry]) -> list[tuple[str, bytes]]:
    """(where, uid) for every UID a vehicle's entries refer to: SBBH material and
    buffers, material texture parameters, MDID, buffer dependency lists, MODL LOD
    submodel lists, and the UPVH's part references (header, records, LOD links,
    attachments). Part UIDs come back in MODL byte order."""
    out = []
    for kind, vehc in kinds.items():
        out += [(f"kind {kind} {tag} {f}", u) for tag, _, f, u in drsp_uids(stream, vehc) if f != "uid"]
        drsp = child(child(vehc, "MDXN"), "DRSP")
        for c in drsp.children:
            if c.tag == "MDID":
                d = _leaf(stream, c)
                out += [(f"kind {kind} MDID", d[i:i + 8]) for i in range(0, len(d) - 7, 8)]
            elif c.tag in ("VXCK", "IXCK"):
                dep = next(k for k in c.children if k.tag in ("VXDP", "IXDP"))
                d = _leaf(stream, dep)
                # VXDP: n UIDs then n zero bytes; IXDP: n UIDs (stock layout)
                n = len(d) // 9 if dep.tag == "VXDP" else len(d) // 8
                out += [(f"kind {kind} {dep.tag}", d[i * 8:i * 8 + 8]) for i in range(n)]
        modl = _leaf(stream, child(child(vehc, "NONR"), "MODL"))
        part_count = struct.unpack_from("<I", modl)[0]
        for p in range(part_count):
            count, offset = struct.unpack_from("<II", modl, 0x10 + p * 0x60 + 0x48)
            for k in range(count):
                mask, n, uid_offset, _ = struct.unpack_from("<4I", modl, offset + k * 16)
                out += [(f"kind {kind} MODL part {p} mask {mask}", modl[uid_offset + i * 8:uid_offset + i * 8 + 8])
                        for i in range(n)]
    if 0 in kinds:
        h = parse(_leaf(stream, child(child(kinds[0], "NONR"), "UPVH")))
        out.append(("UPVH model UID", h.model_uid))
        out += [(f"UPVH record {r.index}", r.uid) for r in h.records if r.uid]
        for n, (interior, exterior) in enumerate(h.lod_links()):
            out += [(f"UPVH lod_link {n} {which}", u) for which, u in (("interior", interior), ("exterior", exterior))
                    if u]
        for n, a in enumerate(h.table("attachments")):
            out.append((f"UPVH attachment {n}", swap_uid(a[0x10:0x18])))
    return [(where, u) for where, u in out if u not in (bytes(8), NONE_UID)]


def foreign_uids(stream, root_entries: list[Entry], kinds: dict[int, Entry]) -> list[str]:
    """Cross-vehicle UID problems for one vehicle (its three VEHC entries):

    - a UID it references that another vehicle defines but it doesn't (the
      game resolves UIDs through global tables, so that reference draws the
      other vehicle's data whenever that vehicle is loaded);
    - a resource or MODL part UID it defines that another vehicle defines too.

    UIDs no vehicle defines (the engine's shared materials) are fine. Stock
    vehicles never share part or resource UIDs; clones that kept the host's
    part UIDs turned into the host in game (run 20261022-024010)."""
    mine = {id(e) for e in kinds.values()}
    own_res, own_parts = set(), set()
    for e in kinds.values():
        r, p = defined_by(stream, e)
        own_res |= r
        own_parts |= p
    others: dict[bytes, str] = {}
    for e in root_entries:
        if e.tag != "VEHC" or id(e) in mine:
            continue
        r, p = defined_by(stream, e)
        for u in r | p:
            others.setdefault(u, e.name or "?")
    problems = []
    for what, own in (("resource", own_res), ("MODL part", own_parts)):
        shared = sorted(u.hex() for u in own if u in others)
        if shared:
            problems.append(f"{len(shared)} {what} UIDs are also defined by other vehicles, e.g. {shared[:3]}")
    foreign: dict[str, list[str]] = {}
    for where, u in references(stream, kinds):
        if u in others and u not in own_res and u not in own_parts:
            foreign.setdefault(where.split(" ", 2)[0] + " " + where.split(" ", 2)[1], []).append(
                f"{where}: {u.hex()} ({others[u]})")
    for group, items in foreign.items():
        problems.append(f"{len(items)} references only another vehicle defines ({group}), e.g. {items[:2]}")
    return problems


def borrowed_from_full(stream, kinds: dict[int, Entry]) -> set[bytes]:
    """Material and texture UIDs that the interior/exterior entries use but only the
    full entry defines."""
    uids = {kind: drsp_uids(stream, e) for kind, e in kinds.items()}
    full = {u for _, _, f, u in uids.get(0, []) if f == "uid"}
    out = set()
    for kind in (1, 2):
        local = {u for _, _, f, u in uids.get(kind, []) if f == "uid"}
        out |= {u for _, _, f, u in uids.get(kind, []) if f in ("material", "texture")
                and u not in local and u in full}
    return out


# --- commands -----------------------------------------------------------------

def _part_submodels(modl: bytes, p: int) -> int:
    count, offset = struct.unpack_from("<II", modl, 0x10 + p * 0x60 + 0x48)
    return sum(struct.unpack_from("<I", modl, offset + k * 16 + 4)[0] for k in range(count))


def fmt_index(value: int) -> str:
    return "-" if value < 0 else str(value)


def cmd_summary(args: argparse.Namespace) -> None:
    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        kinds = entries_for(stream, root.entries, args.id, args.vehc)
        full = kinds[0]
        h = parse(payload(stream, child(child(full, "NONR"), "UPVH")))
        modl = modl_part_uids(payload(stream, child(child(full, "NONR"), "MODL")))
    print(f"UPVH '{full.name}': model {h.model_uid.hex()}, type {h.type}, {len(h.records)} records, "
          f"hierarchy ID {h.hierarchy_id}, PDL {h.pdl_size:#x} at {h.pdl_offset:#x}")
    for name, _ in TABLES:
        print(f"  {name:12s} {h.counts[name]:3d} at {h.offsets[name]:#06x}")
    print("  rec  off   type     role sub  uid               modl  mk hg bd  coll   st mr mdl lt slot ll at wh")
    for r in h.records:
        part = modl.index(r.uid) if r.uid in modl else None
        print(f"  {r.index:3d} {HEADER_SIZE + r.index * RECORD_SIZE:#06x} {r.type_name:8s} {r.role:4d} {r.subtree:3d}  "
              f"{r.uid.hex() if r.uid else '-':16s}  {'-' if part is None else part:>4}  "
              + " ".join(f"{fmt_index(v):>2}" for v in (r.marker, r.hinge, r.body))
              + f" {r.collision if r.collision >= 0 else '-':>5}  "
              + " ".join(f"{fmt_index(v):>2}" for v in (r.seat, r.mirror, r.model, r.light, r.slot,
                                                         r.lod_link, r.attachment, r.wheel)))
    if args.tables:
        for i, (pivot, axis) in enumerate(zip(h.vec4s("hinges"), h.vec4s("hinges", 0x10))):
            limit, param = struct.unpack_from("<2f", h.table("hinges")[i], 0x20)
            print(f"  hinge {i:2d} at {h.offsets['hinges'] + i * 0x30:#06x}: pivot {fmt3(pivot)} "
                  f"axis {fmt3(axis)} limit {limit:.4f} param {param:g}")
        for i, pos in enumerate(h.vec4s("wheels")):
            print(f"  wheel {i} at {h.offsets['wheels'] + i * 0x20:#06x}: hub {fmt3(pos)}")
        for i, (interior, exterior) in enumerate(h.lod_links()):
            print(f"  lod_link {i}: interior {interior.hex() if interior else '-'} "
                  f"exterior {exterior.hex() if exterior else '-'}")


def fmt3(v) -> str:
    return "({:.4f}, {:.4f}, {:.4f})".format(*v[:3])


def cmd_lint(args: argparse.Namespace) -> None:
    """Checks a converted vehicle against the rules in research/upvh.md."""
    problems = 0

    def report(ok: bool, message: str) -> None:
        nonlocal problems
        problems += not ok
        if not ok or args.verbose:
            print(("ok    " if ok else "FAIL  ") + message)

    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        kinds = entries_for(stream, root.entries, args.id, args.vehc)
        full = kinds[0]
        h = parse(payload(stream, child(child(full, "NONR"), "UPVH")))
        parts = {kind: modl_part_uids(payload(stream, child(child(e, "NONR"), "MODL"))) for kind, e in kinds.items()}
        uids = {kind: drsp_uids(stream, e) for kind, e in kinds.items()}
        cross = None if args.allow_shared else foreign_uids(stream, root.entries, kinds)
        modls = {kind: payload(stream, child(child(e, "NONR"), "MODL")) for kind, e in kinds.items()}
        # Converted glass must sit in its window's slot of the shared glass
        # texture, or the damage states draw cracks and holes across the window.
        from dsfforza.convert import glass
        glass_rows = glass.check(stream, kinds)
        # Engine sound: bank round trip, VSVH events, Wwise IDs across the archive (src/dsfforza/convert/engine_sound.py).
        sound_rows = []
        if not args.no_sound:
            from dsfforza.convert import engine_sound
            spool = args.id if args.id is not None else next(
                sid for sid, index, kind in parse_sslp(payload(stream, find(find(find(
                    root.entries, "SSIC").children, "NONR").children, "SSLP"))) if index == args.vehc and kind == 0)
            reserved = engine_sound.stock_index(args.stock)["reserved"] if args.stock else None
            sound_rows = engine_sound.lint(stream, root.entries, spool, reserved)

    report(True, f"UPVH layout parses ({len(h.records)} records)")
    if cross is not None:
        report(not cross, "no UID shared with, or only defined by, another vehicle"
                          + ("" if not cross else ": " + "; ".join(cross)))
    report(h.model_uid == parts[0][0], f"AWHF model UID {h.model_uid.hex()} is MODL part 0")
    missing = [r.index for r in h.records if r.uid and r.uid not in parts[0]]
    report(not missing, f"every record UID is a MODL part of the full entry (missing: records {missing})")
    # Hosts with several chassis records (172: roles 257 and 256) draw a variant
    # part; an empty one made the body invisible (src/dsfforza/convert/chassis_variants.py).
    from dsfforza.convert import chassis_variants
    for kind, plist in chassis_variants.variant_parts(h.data, modls).items():
        empty = [p for p in plist if not _part_submodels(modls[kind], p)]
        if plist and _part_submodels(modls[kind], 0):
            report(not empty, f"kind {kind}: chassis variant parts {plist} have geometry (empty: {empty})")
    for kind, which in ((1, "interior"), (2, "exterior")):
        if kind not in parts:
            continue
        refs = {link[kind - 1] for link in h.lod_links() if link[kind - 1]}
        bad = sorted(u.hex() for u in refs - set(parts[kind]))
        report(not bad, f"lod_links resolve to {which} MODL parts ({len(refs)} UIDs, unresolved {bad})")
    for kind, items in uids.items():
        signed = sorted({u.hex() for _, _, _, u in items if u not in (bytes(8), NONE_UID) and u[7] & 0x80})
        report(not signed, f"kind {kind}: no DRSP UID has bit 63 set ({len(signed)} do, e.g. {signed[:2]})")
    defined = {kind: {u for tag, _, f, u in items if f == "uid"} for kind, items in uids.items()}
    for kind, items in uids.items():
        refs = {(f, u) for tag, _, f, u in items if f in ("vertex_buffer", "index_buffer")}
        bad = sorted(u.hex() for f, u in refs if u not in defined[kind])
        report(not bad, f"kind {kind}: every SBMC buffer UID is defined in the same DRSP (unresolved {bad[:3]})")
    if args.stock:
        with args.stock.open("rb") as stream:
            root = read_chunk(stream, 0)
            host = entries_for(stream, root.entries, args.source, None)
            needed = borrowed_from_full(stream, host)
            host_uids = [item for e in host.values() for item in drsp_uids(stream, e)]
            shared = shared_uids(stream, root.entries)
        # Materials/textures the host uses that none of its entries define, and every
        # material/texture SSIC defines: the engine's shared vehicle materials
        # (research/vehicle-model.md).
        host_defined = {u for _, _, f, u in host_uids if f == "uid"}
        engine = {u for _, _, f, u in host_uids if f in ("material", "texture") and u not in host_defined} | shared
        if needed <= defined.get(0, set()):
            report(True, f"the {len(needed)} materials/textures the stock interior/exterior take from "
                         f"the full entry are still defined there")
        else:
            # Renewed UIDs (clone_vehicle --new-uids): check the car's own borrowing instead.
            # Most stock exteriors use _LOD1 materials their interior or full entry defines
            # (the Desanne's are local), so any of the car's three entries will do.
            own = set().union(*defined.values())
            for kind in (1, 2):
                refs = {u for _, _, f, u in uids.get(kind, []) if f in ("material", "texture")}
                bad = sorted(u.hex() for u in refs - own - engine)
                report(not bad, f"kind {kind}: every material/texture is defined by one of the car's entries "
                                f"or is a shared engine material ({len(refs)} referenced, unresolved {bad[:3]})")
        refs = {u for _, _, f, u in uids.get(0, []) if f in ("material", "texture")}
        bad = sorted(u.hex() for u in refs - defined.get(0, set()) - engine)
        report(not bad, f"kind 0: every material/texture is local or a shared engine material the host "
                        f"uses ({len(refs)} referenced, unresolved {bad[:3]})")
    for ok, message in glass_rows:
        report(ok, message)
    for ok, message in sound_rows:
        report(ok, message)
    print(f"{problems} problem(s)")
    if problems:
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("summary", "print the header, tables and records"),
                            ("lint", "check a (converted) vehicle's UPVH and UID rules")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("archive", type=Path)
        p.add_argument("vehc", type=int, nargs="?", help="VEHC index of the full entry")
        p.add_argument("--id", type=int, help="spool ID instead of a VEHC index")
        if name == "summary":
            p.add_argument("--tables", action="store_true", help="also print hinges, wheels and LOD links")
        else:
            p.add_argument("--verbose", "-v", action="store_true", help="print passing checks too")
            p.add_argument("--stock", type=Path, help="stock archive, to check what the interior and "
                                                      "exterior borrow from the full entry")
            p.add_argument("--source", type=int, default=266, help="host's stock spool ID (default 266)")
            p.add_argument("--no-sound", action="store_true",
                           help="skip the engine sound checks (Wwise bank round trip, VSVH events, IDs)")
            p.add_argument("--allow-shared", action="store_true",
                           help="skip the cross-vehicle UID check (clones made without --new-uids "
                                "share the host's UIDs on purpose)")
    args = parser.parse_args(argv)
    if args.id is None and args.vehc is None:
        parser.error("give a VEHC index or --id")
    {"summary": cmd_summary, "lint": cmd_lint}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
