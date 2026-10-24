"""Body variants: give every chassis record's MODL part the converted body.

Most stock vehicles have one chassis record (UPVH record type 5, role 256)
whose MODL part is part 0 of each entry. 39 stock vehicles have more:
roles 257, 258, 259, 513, 1280, 1281, 1282 or 1, each with its own MODL
part in the full entry and its own LOD link to an interior and an exterior
part. On host 172 (70's Muscle 04) the role-257 parts hold a second copy of
the body (all of part 0's triangles plus a few more), and the game draws
that copy: converted cars put their body only in part 0 and left the
variant parts empty, so the Cuda on host 172 showed only its wheels and
driver, body and cockpit invisible (sandbox runs 20261022-163835, -164332,
-164459; research/batch.md). The same car on host 193 (one chassis record)
rendered.

`fill(...)` runs after the entries are rebuilt: for each extra chassis part
of an entry it adds a copy of every part-0 submodel (a new SBMC with a new
UID on the same vertex and index ranges) and lists the copies in that part,
mask for mask. VXDP, IXDP and MDID gain the new UIDs.

    uv run dsff chassis_variants <archive>      # list vehicles with extra chassis records
"""

from __future__ import annotations

import argparse
import copy
import struct
import sys
from pathlib import Path

from dsfforza.formats import upvh as upvh_mod
from dsfforza.formats.chnk import Entry, read_chunk
from dsfforza.formats.vehicle_mesh import child, payload

CHASSIS = 5  # UPVH record type


def _leaf(stream, entry: Entry) -> bytes:
    return entry.payload if entry.payload is not None else payload(stream, entry)


def variant_parts(upvh_data: bytes, modl: dict[int, bytes]) -> dict[int, list[int]]:
    """Entry kind -> MODL part indices of chassis records other than part 0
    (kind 0: the records' own parts; kinds 1, 2: their LOD links)."""
    h = upvh_mod.parse(upvh_data)
    uids = {k: upvh_mod.modl_part_uids(d) for k, d in modl.items()}
    links = h.lod_links()
    out: dict[int, list[int]] = {k: [] for k in modl}
    for r in h.records:
        if r.type != CHASSIS:
            continue
        if 0 in uids and r.uid in uids[0]:
            out[0].append(uids[0].index(r.uid))
        if r.lod_link >= 0:
            for kind, uid in ((1, links[r.lod_link][0]), (2, links[r.lod_link][1])):
                if kind in uids and uid in uids[kind]:
                    out[kind].append(uids[kind].index(uid))
    return {k: sorted({p for p in v if p != 0}) for k, v in out.items()}


def _parts_lods(modl: bytes) -> list[list[tuple[int, list[bytes], list[int]]]]:
    part_count = struct.unpack_from("<I", modl)[0]
    parts = []
    for p in range(part_count):
        count, offset = struct.unpack_from("<II", modl, 0x10 + p * 0x60 + 0x48)
        lods = []
        for k in range(count):
            mask, n, uid_off, extra_off = struct.unpack_from("<4I", modl, offset + k * 16)
            lods.append(
                (
                    mask,
                    [modl[uid_off + i * 8 : uid_off + i * 8 + 8] for i in range(n)],
                    list(struct.unpack_from(f"<{n}I", modl, extra_off)) if n else [],
                )
            )
        parts.append(lods)
    return parts


def _write_modl(template: bytes, parts) -> bytes:
    """MODL with the template's header and part records, LOD tables from `parts`."""
    _part_count, _, lod_offset, _ = struct.unpack_from("<4I", template)
    out = bytearray(template[:lod_offset])
    lods = []
    for p, plods in enumerate(parts):
        struct.pack_into("<II", out, 0x10 + p * 0x60 + 0x48, len(plods), lod_offset + len(lods) * 16)
        lods += plods
    struct.pack_into("<I", out, 4, len(lods))
    uid_list = lod_offset + len(lods) * 16
    extra_list = uid_list + sum(len(u) for _, u, _ in lods) * 8
    entries, uid_bytes, extra_bytes = bytearray(), bytearray(), bytearray()
    for mask, uids, extras in lods:
        entries += struct.pack("<4I", mask, len(uids), uid_list + len(uid_bytes), extra_list + len(extra_bytes))
        for uid, flag in zip(uids, extras):
            uid_bytes += uid
            extra_bytes += struct.pack("<I", flag)
    return bytes(out) + bytes(entries) + bytes(uid_bytes) + bytes(extra_bytes)


def fill_entry(stream, vehc: Entry, parts_to_fill: list[int], new_uid) -> int:
    """Copy part 0's submodels into each listed part (same buffers and index
    ranges, new UIDs). Returns the submodels added."""
    if not parts_to_fill:
        return 0
    modl_entry = child(child(vehc, "NONR"), "MODL")
    modl = _leaf(stream, modl_entry)
    parts = _parts_lods(modl)
    drsp = child(child(vehc, "MDXN"), "DRSP")
    sbmc = {}
    for e in drsp.children:
        if e.tag == "SBMC":
            sbmc[_leaf(stream, child(e, "SBBH"))[:8]] = e
    added: list[tuple[bytes, bytes]] = []  # (new uid, source uid)
    for p in parts_to_fill:
        if p >= len(parts):
            continue
        new_lods = []
        for mask, uids, extras in parts[0]:
            copies = []
            for uid in uids:
                fresh = new_uid()
                added.append((fresh, uid))
                copies.append(fresh)
            new_lods.append((mask, copies, list(extras)))
        # Keep LODs the variant part already had that part 0 doesn't (none on stock data).
        masks = {m for m, _, _ in new_lods}
        parts[p] = new_lods + [lod for lod in parts[p] if lod[0] not in masks]
    if not added:
        return 0
    source_sbbh = {uid: _leaf(stream, child(sbmc[uid], "SBBH")) for _, uid in added}
    new_children, vb_users, ib_users = [], {}, {}
    for fresh, uid in added:
        d = source_sbbh[uid]
        e = copy.deepcopy(sbmc[uid])
        sb = child(e, "SBBH")
        sb.payload = fresh + d[8:]
        new_children.append(e)
        vb_users.setdefault(d[16:24], []).append(fresh)
        ib_users.setdefault(d[0x30:0x38], []).append(fresh)
    # Insert the new SBMCs after the last SBMC (DRSP order doesn't matter to the loader).
    last = max(i for i, e in enumerate(drsp.children) if e.tag == "SBMC")
    drsp.children = drsp.children[: last + 1] + new_children + drsp.children[last + 1 :]
    for e in drsp.children:
        if e.tag == "VXCK":
            uid = _leaf(stream, child(e, "VXHD"))[:8]
            if uid in vb_users:
                dp = child(e, "VXDP")
                d = _leaf(stream, dp)
                n = len(d) // 9
                users = [d[i * 8 : i * 8 + 8] for i in range(n)] + vb_users[uid]
                dp.payload = b"".join(users) + bytes(len(users))
        elif e.tag == "IXCK":
            uid = _leaf(stream, child(e, "IXHD"))[:8]
            if uid in ib_users:
                dp = child(e, "IXDP")
                dp.payload = _leaf(stream, dp) + b"".join(ib_users[uid])
    mdid = next(e for e in drsp.children if e.tag == "MDID")
    old = _leaf(stream, mdid)
    ids = [old[i : i + 8] for i in range(0, len(old), 8)]
    sub_positions = [i for i, u in enumerate(ids) if u in sbmc]
    at = (max(sub_positions) + 1) if sub_positions else len(ids)
    ids = ids[:at] + [f for f, _ in added] + ids[at:]
    mdid.payload = b"".join(ids)
    modl_entry.payload = _write_modl(modl, parts)
    return len(added)


def fill(
    stream,
    full: Entry,
    interior: Entry | None,
    exterior: Entry | None,
    seed: bytes,
    log=print,
    full_rebuilt: bool = True,
) -> dict:
    """Fill the extra chassis parts of the full, interior and exterior entries
    (call after they are rebuilt; pass None for an entry that kept the host's
    geometry, and full_rebuilt=False if the full entry did). Returns {kind:
    parts and submodels added}."""
    from dsfforza.build.clone_vehicle import stock_like_uid

    nonr = child(full, "NONR")
    entries = {0: full if full_rebuilt else None, 1: interior, 2: exterior}
    modl = {k: _leaf(stream, child(child(e, "NONR"), "MODL")) for k, e in entries.items() if e is not None}
    if not modl:
        return {}
    todo = variant_parts(_leaf(stream, child(nonr, "UPVH")), modl)
    counter = [0]
    prefix = bytes([seed[0] ^ 0x5A, seed[1], seed[2], seed[3] ^ 0x33])

    def new_uid() -> bytes:
        counter[0] += 1
        return stock_like_uid(prefix, counter[0])

    report = {}
    for kind, parts in todo.items():
        if parts and entries.get(kind) is not None:
            n = fill_entry(stream, entries[kind], parts, new_uid)
            report[str(kind)] = {"parts": parts, "submodels": n}
    if report:
        log(f"chassis variants: body copied into {report}")
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument("--ids", help="comma-separated spool IDs (default: every vehicle)")
    args = parser.parse_args(argv)
    from dsfforza.formats.vehicles import find, parse_sslp

    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        records = parse_sslp(payload(stream, find(find(find(root.entries, "SSIC").children, "NONR").children, "SSLP")))
        ids = [int(x) for x in args.ids.split(",")] if args.ids else [sid for sid, _, kind in records if kind == 0]
        for sid in ids:
            kinds = upvh_mod.entries_for(stream, root.entries, sid, None)
            modl = {k: payload(stream, child(child(e, "NONR"), "MODL")) for k, e in kinds.items()}
            h = upvh_mod.parse(payload(stream, child(child(kinds[0], "NONR"), "UPVH")))
            roles = [r.role for r in h.records if r.type == CHASSIS]
            todo = variant_parts(payload(stream, child(child(kinds[0], "NONR"), "UPVH")), modl)
            if any(todo.values()):
                print(f"{sid:4d} {kinds[0].name:24s} chassis roles {roles}  variant parts {todo}")


if __name__ == "__main__":
    sys.exit(main())
