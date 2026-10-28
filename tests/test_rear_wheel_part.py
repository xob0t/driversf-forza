"""Checks for the rear wheel part a shared-wheel host gets (forza2dsf.split_rear_wheel,
research/wheels.md "Rear wheel parts"); no game files needed.

    uv run python tests/test_rear_wheel_part.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import struct

from dsfforza.convert import forza2dsf
from dsfforza.formats import chnk, upvh

HUBS = [(-0.76, 0.0, 1.37), (0.76, 0.0, 1.37), (-0.76, 0.0, -1.37), (0.76, 0.0, -1.37)]
BODY, WHEEL, EXT_BODY, EXT_WHEEL = (bytes([n, 0x12, 0, 0, 0, 0, 0x28, 0x2D]) for n in (0x10, 0x20, 0x30, 0x40))


def _modl(parts: list[tuple[bytes, list[tuple[int, list[bytes]]]]]) -> bytes:
    """A MODL in the vanilla layout: part records, LOD table, submodel UIDs, flags."""
    lod_offset = 0x10 + len(parts) * 0x60
    entries = [(mask, subs) for _, lods in parts for mask, subs in lods]
    uid_list = lod_offset + len(entries) * 16
    extra_list = uid_list + sum(len(s) for _, s in entries) * 8
    out = bytearray(struct.pack("<4I", len(parts), len(entries), lod_offset, 0))
    n = 0
    for p, (uid, lods) in enumerate(parts):
        record = bytearray(0x60)
        struct.pack_into("<3f", record, 0, 0.0, 0.1 * p, 0.0)
        record[0x40:0x48] = uid
        struct.pack_into("<II", record, 0x48, len(lods), lod_offset + n * 16)
        struct.pack_into("<I", record, 0x5C, 5)
        out += record
        n += len(lods)
    uids, extras = bytearray(), bytearray()
    for mask, subs in entries:
        out += struct.pack("<4I", mask, len(subs), uid_list + len(uids), extra_list + len(extras))
        for k, s in enumerate(subs):
            uids += s
            extras += struct.pack("<I", 5 + 2 * (k % 2))
    return bytes(out + uids + extras)


def _sub(n: int) -> bytes:
    return bytes([n, 0x12, 1, 2, 3, 4, 0x28, 0x2D])


def _hierarchy(wheel_uids: list[bytes], ext_uids: list[bytes]) -> bytes:
    """A UPVH with four wheel records, each with its own LOD link, and the wheel table."""
    head = bytearray(upvh.HEADER_SIZE)
    head[:4] = b"AWHF"
    struct.pack_into("<6I", head, 4, 0, 0, 26, 4, 1, 0)
    struct.pack_into("<H", head, 0x36, 4)  # LOD links
    struct.pack_into("<H", head, 0x52, 4)  # wheels
    records = bytearray()
    for n, uid in enumerate(wheel_uids):
        r = bytearray(upvh.RECORD_SIZE)
        r[:8] = upvh.swap_uid(uid)
        struct.pack_into("<HH", r, 0x08, 2, 27 + n)
        struct.pack_into("<H", r, 0x14, 1)
        for off in (0x1A, 0x1C, 0x1E, 0x30, 0x34, 0x40):
            struct.pack_into("<h", r, off, -1)
        struct.pack_into("<i", r, 0x20, -1)
        struct.pack_into("<hh", r, 0x3E, n, -1)
        struct.pack_into("<h", r, 0x42, n)
        records += r
    links = b"".join(upvh.NONE_UID + upvh.swap_uid(u) for u in ext_uids)
    wheels = b"".join(struct.pack("<4f4f", *h, 1.0, 0, 0, 0, 1) for h in HUBS)
    return bytes(head + records) + links + wheels


def _leaf(tag: str, data: bytes) -> chnk.Entry:
    e = chnk.Entry(tag, 0, len(data), 1, 2, None)
    e.payload = data
    return e


def _vehc(*leaves: chnk.Entry) -> chnk.Entry:
    nonr = chnk.Entry("NONR", 0, 0, 2, 4, None, children=list(leaves))
    return chnk.Entry("VEHC", 0, 0, 2, 4, None, children=[nonr])


def _host(rear_wheel: bytes = WHEEL, rear_ext: bytes = EXT_WHEEL):
    full_parts = [
        (BODY, [(4, [_sub(1), _sub(2)]), (8, [_sub(3)]), (1, [_sub(4)])]),
        (WHEEL, [(4, [_sub(5), _sub(6)]), (8, [_sub(7)]), (16, [_sub(8)])]),
    ]
    ext_parts = [(EXT_BODY, [(2, [_sub(9)])]), (EXT_WHEEL, [(2, [_sub(10), _sub(11)])])]
    if rear_wheel != WHEEL:
        full_parts.append((rear_wheel, [(4, [_sub(12)])]))
    if rear_ext != EXT_WHEEL:
        ext_parts.append((rear_ext, [(2, [_sub(13)])]))
    data = _hierarchy([WHEEL, WHEEL, rear_wheel, rear_wheel], [EXT_WHEEL, EXT_WHEEL, rear_ext, rear_ext])
    full = _vehc(_leaf("UPVH", data), _leaf("MODL", _modl(full_parts)))
    exterior = _vehc(_leaf("MODL", _modl(ext_parts)))
    return full, exterior


def _payload(vehc: chnk.Entry, tag: str) -> bytes:
    return forza2dsf.child(forza2dsf.child(vehc, "NONR"), tag).payload


def test_add_modl_part_copies_the_part_and_keeps_the_others():
    modl = _modl([(BODY, [(4, [_sub(1)]), (8, [_sub(2), _sub(3)])]), (WHEEL, [(4, [_sub(4), _sub(5)])])])
    new_uid = bytes([0x70, 0x12, 0, 9, 9, 9, 0x09, 0x2D])
    out, part = forza2dsf.add_modl_part(modl, 1, new_uid)
    assert part == 2 and struct.unpack_from("<I", out)[0] == 3
    assert upvh.modl_part_uids(out) == [BODY, WHEEL, new_uid]
    for p in (0, 1):
        assert forza2dsf.modl_part_lods(out, p) == forza2dsf.modl_part_lods(modl, p)
    assert forza2dsf.modl_part_lods(out, 2) == forza2dsf.modl_part_lods(modl, 1)
    # the copy keeps the source's box and trailer
    assert out[0x10 + 2 * 0x60 : 0x10 + 2 * 0x60 + 0x40] == modl[0x10 + 0x60 : 0x10 + 0x60 + 0x40]
    assert out[0x10 + 2 * 0x60 + 0x5C : 0x10 + 3 * 0x60] == modl[0x10 + 0x60 + 0x5C : 0x10 + 2 * 0x60]
    # rebuild_modl takes it as a template: the copy gets its own submodels
    rebuilt = forza2dsf.rebuild_modl(out, {2: {4: [forza2dsf.NewSub(_sub(6), forza2dsf.TYRE, 20, 0, 0, 3, 5)]}})
    assert forza2dsf.modl_part_lods(rebuilt, 2) == [(4, [(_sub(6), 5)])]
    assert forza2dsf.modl_part_lods(rebuilt, 1) == [(4, [])]


def test_split_gives_the_rear_wheels_their_own_parts():
    full, exterior = _host()
    before = forza2dsf.host_wheels(_payload(full, "UPVH"), _payload(full, "MODL"), _payload(exterior, "MODL"))
    assert before.full_parts == [1] and before.exterior_parts == [1]
    assert set(before.axles.values()) == {"both"}
    report = forza2dsf.split_rear_wheel(None, full, exterior, b"\x01\x02\x03\x04")
    assert report == {"full": [1, 2], "exterior": [1, 2], "rear_records": [2, 3]}
    hw = forza2dsf.host_wheels(_payload(full, "UPVH"), _payload(full, "MODL"), _payload(exterior, "MODL"))
    assert hw.full_parts == [1, 2] and hw.exterior_parts == [1, 2]
    assert hw.axles == {(0, 1): "front", (0, 2): "rear", (2, 1): "front", (2, 2): "rear"}
    assert hw.hubs == before.hubs
    # new part UIDs: vanilla-shaped, byte 7 below 0x80, not the host's
    full_ids, ext_ids = upvh.modl_part_uids(_payload(full, "MODL")), upvh.modl_part_uids(_payload(exterior, "MODL"))
    new = {full_ids[2], ext_ids[2]}
    assert len(new) == 2 and not new & {BODY, WHEEL, EXT_BODY, EXT_WHEEL}
    assert all(u[1] == 0x12 and u[7] == 0x2D for u in new)
    # the rear copy starts with the front part's LODs (masks 4, 8, 16; convert() refills them)
    assert [m for m, _ in forza2dsf.modl_part_lods(_payload(full, "MODL"), 2)] == [4, 8, 16]
    # only the rear records' UIDs and LOD links changed: wheel indices, hinges, roles stay
    h = upvh.parse(_payload(full, "UPVH"))
    assert [r.uid for r in h.records] == [WHEEL, WHEEL, full_ids[2], full_ids[2]]
    assert [link[1] for link in h.lod_links()] == [EXT_WHEEL, EXT_WHEEL, ext_ids[2], ext_ids[2]]
    assert [(r.wheel, r.role, r.lod_link) for r in h.records] == [(n, 27 + n, n) for n in range(4)]


def test_split_leaves_split_hosts_alone():
    rear, rear_ext = bytes([0x50, 0x12, 0, 0, 0, 0, 0x28, 0x2D]), bytes([0x60, 0x12, 0, 0, 0, 0, 0x28, 0x2D])
    full, exterior = _host(rear, rear_ext)
    upvh_before, modl_before = _payload(full, "UPVH"), _payload(full, "MODL")
    assert forza2dsf.split_rear_wheel(None, full, exterior, b"\x01\x02\x03\x04") is None
    assert _payload(full, "UPVH") == upvh_before and _payload(full, "MODL") == modl_before


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
