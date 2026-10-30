"""Rebuilding an entry: DRSP and MODL from the Builder, MODL parts, the host's wheels and
hubs, wheel collision and settling."""

from __future__ import annotations

import copy
import math
import struct
from dataclasses import dataclass, field

from dsfforza.convert.forza2dsf.builder import Builder, NewSub
from dsfforza.convert.forza2dsf.materials import MATERIAL_LAYOUT, TRIM
from dsfforza.convert.forza2dsf.vertices import encode_vertex
from dsfforza.formats.chnk import Entry
from dsfforza.formats.vehicle_mesh import child, payload


def lights_template(stream, full: Entry):
    """(Lights MACO, its texture UID) of the full entry, the template of the interior
    entry's gloss materials (cockpit.convert_interior; most host interiors have no Lights
    MACO of their own); None without one."""
    from dsfforza.convert import dsf_materials

    found = dsf_materials.host_materials(stream, child(child(full, "MDXN"), "DRSP")).get(dsf_materials.LIGHTS_MAT)
    if found is None or not found[1]["textures"]:
        return None
    return found[0], next(iter(found[1]["textures"]))


def entry_like(template: Entry, payload_bytes: bytes | None = None, children: list[Entry] | None = None) -> Entry:
    e = copy.deepcopy(template)
    e.payload = payload_bytes
    if children is not None:
        e.children = children
    return e


def rebuild_drsp(
    stream,
    drsp: Entry,
    builder: Builder,
    keep_host_materials: bool,
    keep_host_geometry: bool = False,
    templates: dict | None = None,
) -> None:
    """Replace SBMC/VXCK/IXCK/MDID in a DRSP with the builder's contents. The
    host's own textures and materials are dropped unless keep_host_materials;
    with keep_host_geometry the host's submodels and buffers stay as well and
    the builder's are added alongside. `templates` (stride -> VXCK) sets the
    vertex declaration of that stride's buffer whatever the host has."""
    old = drsp.children
    keep_tags = {"TEXC", "MACO"} if keep_host_materials or keep_host_geometry else set()
    if keep_host_geometry:
        keep_tags |= {"SBMC", "VXCK", "IXCK"}
    tex_mat = [e for e in old if e.tag in keep_tags]
    old_mdid = payload(stream, next(e for e in old if e.tag == "MDID")) if keep_host_geometry else b""
    sbmc_t = next(e for e in old if e.tag == "SBMC")
    vxck_t, buffers = {}, {}
    for n, e in enumerate(old):
        if e.tag == "VXCK":
            hd = payload(stream, child(e, "VXHD"))
            stride = struct.unpack_from("<I", hd, 12)[0]
            vxck_t[stride] = e
            buffers[hd[:8]] = (stride, n)
    # A buffer takes its vertex declaration from the host buffer its materials are
    # drawn from, where the host has one of that stride; else from the host's last
    # buffer of the stride. Hosts 119, 146 and 208 keep a second stride-20 layout
    # after the needles' in the interior: the needles got it, its semantic 8 has no
    # gauge id (DashboardInstrument), and they would never turn.
    own = {}  # (material, stride) -> index in `old` of the host buffer it is drawn from
    for e in old:
        if e.tag == "SBMC":
            d = payload(stream, child(e, "SBBH"))
            if d[16:24] in buffers:
                own.setdefault((d[8:16], buffers[d[16:24]][0]), buffers[d[16:24]][1])
    template = {}
    for stride in builder.buffers:
        mine = [own[s.material, stride] for s in builder.subs if s.stride == stride and (s.material, stride) in own]
        if stride in (templates or {}):  # a layout the caller needs (digital dials: the Volt's)
            template[stride] = templates[stride]
        else:
            template[stride] = old[max(mine, key=mine.count)] if mine else vxck_t[stride]
    ixck_t = next(e for e in old if e.tag == "IXCK")
    mdid_t = next(e for e in old if e.tag == "MDID")

    vb_uid = {stride: builder.new_uid() for stride in builder.buffers}
    ib_uid = builder.new_uid()

    new_children = list(tex_mat)
    for sub in builder.subs:
        sbbh = struct.pack(
            "<8s8s8s8sI12s8sII8s",
            sub.uid,
            sub.material,
            vb_uid[sub.stride],
            bytes(8),
            sub.base_vertex,
            bytes(12),
            ib_uid,
            sub.first_index,
            sub.index_count,
            bytes(8),
        )
        new_children.append(entry_like(sbmc_t, children=[entry_like(sbmc_t.children[0], sbbh)]))
    for stride, data in builder.buffers.items():
        t = template[stride]
        users = [s.uid for s in builder.subs if s.stride == stride]
        hd = vb_uid[stride] + struct.pack("<II", len(data) // stride, stride)
        dp = b"".join(users) + bytes(len(users))
        kids = []
        for c in t.children:
            body = {"VXHD": hd, "VXDP": dp, "VXDT": bytes(data)}.get(c.tag)
            kids.append(entry_like(c, body if body is not None else payload(stream, c)))
        new_children.append(entry_like(t, children=kids))
    template_ixhd = payload(stream, child(ixck_t, "IXHD"))
    ixhd = ib_uid + struct.pack("<II", len(builder.indices), 2) + template_ixhd[16:]
    ixdp = b"".join(s.uid for s in builder.subs)
    ixdt = struct.pack(f"<{len(builder.indices)}H", *builder.indices)
    kids = [entry_like(c, {"IXHD": ixhd, "IXDP": ixdp, "IXDT": ixdt}[c.tag]) for c in ixck_t.children]
    new_children.append(entry_like(ixck_t, children=kids))

    def current(e: Entry) -> bytes:  # an edited payload, else the source bytes
        return e.payload if e.payload is not None else payload(stream, e)

    texture_uids = [current(child(e, "UIDC"))[:8] for e in tex_mat if e.tag == "TEXC"]
    material_uids = [current(child(e, "MAHE"))[:8] for e in tex_mat if e.tag == "MACO"]
    if keep_host_geometry:
        # Host order is textures, materials, submodels, vertex buffers, index buffer;
        # slot the new resources in after each group.
        old_uids = [old_mdid[i : i + 8] for i in range(0, len(old_mdid), 8)]
        host_subs = {payload(stream, child(e, "SBBH"))[:8] for e in old if e.tag == "SBMC"}
        host_vbs = {payload(stream, child(e, "VXHD"))[:8] for e in old if e.tag == "VXCK"}
        head = [
            u
            for u in old_uids
            if u not in host_subs
            and u not in host_vbs
            and u not in {payload(stream, child(e, "IXHD"))[:8] for e in old if e.tag == "IXCK"}
        ]
        # Textures/materials added to the DRSP (dsf_materials clone mode) go after their kind.
        after_tex = max((n + 1 for n, u in enumerate(head) if u in texture_uids), default=0)
        head = (
            head[:after_tex]
            + [u for u in texture_uids if u not in old_uids]
            + head[after_tex:]
            + [u for u in material_uids if u not in old_uids]
        )
        mdid_uids = (
            head
            + [u for u in old_uids if u in host_subs]
            + [s.uid for s in builder.subs]
            + [u for u in old_uids if u in host_vbs]
            + [vb_uid[s] for s in builder.buffers]
            + [payload(stream, child(e, "IXHD"))[:8] for e in old if e.tag == "IXCK"]
            + [ib_uid]
        )
    else:
        mdid_uids = (
            texture_uids
            + material_uids
            + [s.uid for s in builder.subs]
            + [vb_uid[s] for s in builder.buffers]
            + [ib_uid]
        )
    new_children.append(entry_like(mdid_t, b"".join(mdid_uids)))
    drsp.children = new_children


def rebuild_modl(
    template: bytes, part_lods: dict[int, dict[int, list[NewSub]]], keep_other_parts: bool = False
) -> bytes:
    """Keep the part records; rewrite the LOD tables. part_lods maps part index
    -> {mask: submodels}. Other LOD entries are emptied, or keep the host's
    submodel lists when keep_other_parts."""
    part_count, _lod_count, lod_offset, _ = struct.unpack_from("<4I", template)
    out = bytearray(template[:lod_offset])
    lods = []  # (mask, [(uid, flag)])
    for p in range(part_count):
        r = 0x10 + p * 0x60
        count, offset = struct.unpack_from("<II", template, r + 0x48)
        host = {}
        for k in range(count):
            mask, subs, uid_offset, extra_offset = struct.unpack_from("<4I", template, offset + k * 16)
            host[mask] = [
                (
                    template[uid_offset + i * 8 : uid_offset + i * 8 + 8],
                    struct.unpack_from("<I", template, extra_offset + i * 4)[0],
                )
                for i in range(subs)
            ]
        wanted = {mask: [(s.uid, s.flag) for s in subs] for mask, subs in part_lods.get(p, {}).items()}
        masks = list(host) + [m for m in wanted if m not in host]
        struct.pack_into("<II", out, r + 0x48, len(masks), lod_offset + len(lods) * 16)
        for mask in masks:
            if mask in wanted:
                lods.append((mask, wanted[mask]))
            else:
                lods.append((mask, host[mask] if keep_other_parts else []))
    struct.pack_into("<I", out, 4, len(lods))
    uid_list = lod_offset + len(lods) * 16
    extra_list = uid_list + sum(len(s) for _, s in lods) * 8
    uid_bytes, extra_bytes, entries = bytearray(), bytearray(), bytearray()
    for mask, subs in lods:
        entries += struct.pack("<4I", mask, len(subs), uid_list + len(uid_bytes), extra_list + len(extra_bytes))
        for uid, flag in subs:
            uid_bytes += uid
            extra_bytes += struct.pack("<I", flag)
    return bytes(out) + bytes(entries) + bytes(uid_bytes) + bytes(extra_bytes)


def template_part_masks(modl: bytes) -> dict[int, list[int]]:
    """Part index -> masks of the LODs that had submodels in the host's MODL."""
    part_count = struct.unpack_from("<I", modl)[0]
    result = {}
    for p in range(part_count):
        count, offset = struct.unpack_from("<II", modl, 0x10 + p * 0x60 + 0x48)
        masks = []
        for k in range(count):
            mask, subs = struct.unpack_from("<II", modl, offset + k * 16)
            if subs:
                masks.append(mask)
        result[p] = masks
    return result


def fill_empty_parts(builder: Builder, modl: bytes, part_lods: dict[int, dict[int, list[NewSub]]]) -> int:
    """Give every (part, mask) that had geometry in the host one degenerate
    triangle, so nothing the hierarchy expects is empty."""
    filled = 0
    degenerate = encode_vertex(MATERIAL_LAYOUT[TRIM][0], (0.0, 0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 1.0, 0.0))
    for part, masks in template_part_masks(modl).items():
        for mask in masks:
            if not part_lods.get(part, {}).get(mask):
                sub = builder.add(TRIM, [degenerate] * 3, [(0, 1, 2)])
                part_lods.setdefault(part, {})[mask] = [sub]
                filled += 1
    return filled


@dataclass
class HostWheels:
    """The host's wheels, read from its own hierarchy (research/hosts.md). Hub
    height and axle midpoint differ per host (Desanne y -0.398, z 0.1155; 370Z
    -0.326; Skyline R33 and F-150 y 0), and 33 four-wheeled vanilla cars keep the front and
    rear wheels in two MODL parts."""

    hubs: list[tuple[float, float, float]]  # UPVH wheel table, in table order
    hub_y: float
    axle_mid: float
    full_parts: list[int]  # full-entry MODL parts the wheel records point at
    exterior_parts: list[int]  # exterior MODL parts the wheel records' LOD links point at
    axles: dict = field(default_factory=dict)  # (kind, part) -> "front", "rear" or "both"


def host_wheels(upvh_data: bytes, full_modl: bytes, exterior_modl: bytes | None = None) -> HostWheels:
    from dsfforza.formats import upvh as upvh_mod

    h = upvh_mod.parse(upvh_data)
    hubs = [w[:3] for w in h.vec4s("wheels")]
    if len(hubs) != 4:
        raise ValueError(f"host has {len(hubs)} wheels; the converter needs 4")
    zs = [w[2] for w in hubs]
    full_ids = upvh_mod.modl_part_uids(full_modl)
    wheels = [r for r in h.records if r.type == 2]
    full_parts = sorted({full_ids.index(r.uid) for r in wheels if r.uid in full_ids})
    ext_parts: list[int] = []
    if exterior_modl is not None:
        ext_ids = upvh_mod.modl_part_uids(exterior_modl)
        links = h.lod_links()
        ext_parts = sorted(
            {
                ext_ids.index(links[r.lod_link][1])
                for r in wheels
                if 0 <= r.lod_link < len(links) and links[r.lod_link][1] in ext_ids
            }
        )
    if not full_parts:
        raise ValueError("no MODL part for the host's wheel records")
    mid = sum(zs) / 4
    seen: dict = {}
    for r in wheels:
        if not 0 <= r.wheel < len(hubs):
            continue
        axle = "front" if hubs[r.wheel][2] > mid else "rear"
        if r.uid in full_ids:
            seen.setdefault((0, full_ids.index(r.uid)), set()).add(axle)
        if exterior_modl is not None and 0 <= r.lod_link < len(links) and links[r.lod_link][1] in ext_ids:
            seen.setdefault((2, ext_ids.index(links[r.lod_link][1])), set()).add(axle)
    axles = {k: (v.pop() if len(v) == 1 else "both") for k, v in seen.items()}
    return HostWheels(hubs, sum(w[1] for w in hubs) / 4, (max(zs) + min(zs)) / 2, full_parts, ext_parts, axles)


# A host with one wheel part for both axles gets a second, rear wheel part when the rear tyre
# it would draw at the front size is this far off Data_Car's rear tyre (research/wheels.md,
# "Rear wheel parts"): 5 mm of radius is 10 mm of arch gap.
REAR_PART_RADIUS = 0.005  # m
REAR_PART_WIDTH = 0.010  # m


def modl_part_lods(modl: bytes, part: int) -> list[tuple[int, list[tuple[bytes, int]]]]:
    """One MODL part's LODs: [(mask, [(submodel UID, flag)])]."""
    count, offset = struct.unpack_from("<II", modl, 0x10 + part * 0x60 + 0x48)
    out = []
    for k in range(count):
        mask, subs, uid_offset, extra_offset = struct.unpack_from("<4I", modl, offset + k * 16)
        out.append(
            (
                mask,
                [
                    (
                        modl[uid_offset + i * 8 : uid_offset + i * 8 + 8],
                        struct.unpack_from("<I", modl, extra_offset + i * 4)[0],
                    )
                    for i in range(subs)
                ],
            )
        )
    return out


def add_modl_part(modl: bytes, source: int, uid: bytes) -> tuple[bytes, int]:
    """The MODL with a copy of part `source` (its box and LOD lists) appended under a new
    part UID. Returns (MODL, the new part's index). Same layout as vanilla and rebuild_modl:
    part records, the LOD table, then all submodel UID lists, then their flags."""
    part_count, _, _, tail = struct.unpack_from("<4I", modl)
    records = [bytearray(modl[0x10 + p * 0x60 : 0x10 + (p + 1) * 0x60]) for p in range(part_count)]
    lods = [modl_part_lods(modl, p) for p in range(part_count)]
    record = bytearray(records[source])
    record[0x40:0x48] = uid
    records.append(record)
    lods.append([(mask, list(subs)) for mask, subs in lods[source]])
    lod_offset = 0x10 + len(records) * 0x60
    entries = [entry for part in lods for entry in part]
    uid_list = lod_offset + len(entries) * 16
    extra_list = uid_list + sum(len(subs) for _, subs in entries) * 8
    out = bytearray(struct.pack("<4I", len(records), len(entries), lod_offset, tail))
    n = 0
    for record, part in zip(records, lods):
        struct.pack_into("<II", record, 0x48, len(part), lod_offset + n * 16)
        out += record
        n += len(part)
    uid_bytes, extra_bytes = bytearray(), bytearray()
    for mask, subs in entries:
        out += struct.pack("<4I", mask, len(subs), uid_list + len(uid_bytes), extra_list + len(extra_bytes))
        for sub_uid, flag in subs:
            uid_bytes += sub_uid
            extra_bytes += struct.pack("<I", flag)
    return bytes(out + uid_bytes + extra_bytes), len(records) - 1


def split_rear_wheel(stream, full: Entry, exterior: Entry, uid_prefix: bytes) -> dict | None:
    """Give a host whose four wheel records share one wheel part a separate rear wheel part,
    as 33 vanilla hosts have (Lamborghinis, Corvette, GT...), so the converter can draw the rear
    wheels at the rear tyre's size.

    The shared part is copied (box and LOD lists) to the end of the full entry's MODL and the
    exterior wheel part to the end of the exterior's, each under a new part UID; the rear wheel
    records then point at the full copy and their LOD links (no vanilla vehicle shares one
    between axles) at the exterior copy. The engine finds parts by UID (research/upvh.md), and the wheel
    records keep their hinges, bodies and wheel indices, so spin and steering are unchanged.
    The edited payloads are set on the entries. Returns a report, or None when the host
    already splits its wheels or its records don't allow the copy."""
    from dsfforza.formats import upvh as upvh_mod

    nonr = child(full, "NONR")
    upvh_entry, modl_entry = child(nonr, "UPVH"), child(nonr, "MODL")
    ext_modl_entry = child(child(exterior, "NONR"), "MODL")
    data = payload(stream, upvh_entry)
    h = upvh_mod.parse(data)
    hubs = [w[:3] for w in h.vec4s("wheels")]
    mid = sum(w[2] for w in hubs) / len(hubs)
    wheels = [r for r in h.records if r.type == 2 and 0 <= r.wheel < len(hubs)]
    front = [r for r in wheels if hubs[r.wheel][2] > mid]
    rear = [r for r in wheels if hubs[r.wheel][2] <= mid]
    links = h.lod_links()
    shared = {r.uid for r in front} & {r.uid for r in rear}
    if not front or not rear or len(shared) != 1 or {r.uid for r in rear} != shared:
        return None  # already split (or not one shared part)
    if any(not 0 <= r.lod_link < len(links) or links[r.lod_link][1] is None for r in rear):
        return None
    if {r.lod_link for r in front} & {r.lod_link for r in rear}:
        return None  # a link shared across axles: no vanilla host has one
    ext_shared = {links[r.lod_link][1] for r in rear}
    if len(ext_shared) != 1 or {links[r.lod_link][1] for r in front} != ext_shared:
        return None
    full_modl, ext_modl = payload(stream, modl_entry), payload(stream, ext_modl_entry)
    full_ids, ext_ids = upvh_mod.modl_part_uids(full_modl), upvh_mod.modl_part_uids(ext_modl)
    (full_uid,), (ext_uid,) = shared, ext_shared
    if full_uid not in full_ids or ext_uid not in ext_ids:
        return None
    uids = Builder(uid_prefix)
    new_full, new_ext = uids.new_uid(), uids.new_uid()
    modl_entry.payload, full_part = add_modl_part(full_modl, full_ids.index(full_uid), new_full)
    ext_modl_entry.payload, ext_part = add_modl_part(ext_modl, ext_ids.index(ext_uid), new_ext)
    out = bytearray(data)
    for r in rear:
        at = upvh_mod.HEADER_SIZE + r.index * upvh_mod.RECORD_SIZE
        out[at : at + 8] = upvh_mod.swap_uid(new_full)
        link = h.offsets["lod_links"] + r.lod_link * 0x10 + 8
        out[link : link + 8] = upvh_mod.swap_uid(new_ext)
    upvh_entry.payload = bytes(out)
    return {
        "full": [full_ids.index(full_uid), full_part],
        "exterior": [ext_ids.index(ext_uid), ext_part],
        "rear_records": [r.index for r in rear],
    }


def patch_hubs(
    upvh: bytes,
    front_z: float,
    rear_z: float,
    half_track: float,
    hub_y: float,
    rear_half_track: float | None = None,
    rear_hub_y: float | None = None,
) -> tuple[bytes, int]:
    """Move the four wheel hubs to (+-half_track, hub_y, front_z/rear_z) in the
    host's hierarchy: the wheel table, every hinge pivot within 10 cm of a hub
    (the wheel hinges and the steering knuckles next to the front hubs, Desanne
    hinges 3 to 6, 14, 15), which move with their hub, and the wheel records'
    bodies, whose position is the negated hub (they move by minus the hub's
    offset). Hubs come from the host's own wheel table, so this works for any
    host.
    rear_half_track / rear_hub_y: the rear axle's, when it differs (Veyron: rear
    track outer 2.003 with 365 mm tyres against 1.999 with 265 mm at the front)."""
    from dsfforza.formats import upvh as upvh_mod

    h = upvh_mod.parse(upvh)
    data = bytearray(upvh)
    hubs = [w[:3] for w in h.vec4s("wheels")]
    moves = []  # (old hub, offset)
    for n, (x, y, z) in enumerate(hubs):
        front = z > sum(w[2] for w in hubs) / len(hubs)
        new = (
            math.copysign(half_track if front or rear_half_track is None else rear_half_track, x),
            hub_y if front or rear_hub_y is None else rear_hub_y,
            front_z if front else rear_z,
        )
        moves.append(((x, y, z), tuple(b - a for a, b in zip((x, y, z), new))))
        struct.pack_into("<3f", data, h.offsets["wheels"] + n * 0x20, *new)
    patched = len(hubs)

    def offset_near(p) -> tuple | None:
        best = min(moves, key=lambda m: math.dist(p, m[0]))
        return best[1] if math.dist(p, best[0]) < 0.1 else None

    for n in range(h.counts["hinges"]):
        at = h.offsets["hinges"] + n * 0x30
        pivot = struct.unpack_from("<3f", data, at)
        d = offset_near(pivot)
        if d is not None:
            struct.pack_into("<3f", data, at, *(p + o for p, o in zip(pivot, d)))
            patched += 1
    for r in h.records:
        if r.type == 2 and r.body >= 0 and r.hinge >= 0:
            pivot = struct.unpack_from("<3f", upvh, h.offsets["hinges"] + r.hinge * 0x30)
            d = offset_near(pivot)
            at = h.offsets["bodies"] + r.body * 0xB0 + 0x30
            pos = struct.unpack_from("<3f", data, at)
            if d is not None and math.dist(pos, tuple(-c for c in pivot)) < 0.1:
                struct.pack_into("<3f", data, at, *(p - o for p, o in zip(pos, d)))
                patched += 1
    return bytes(data), patched


def fit_wheel_collision(upvh: bytes, front: tuple[float, float], rear: tuple[float, float]) -> tuple[bytes, int]:
    """Size each wheel record's collision model (PDL, record +0x20: one cylinder primitive,
    extents (half width, radius, 0) about the hub) to the tyre drawn on that axle,
    front/rear = (radius, width). The engine places a drawn wheel where its cylinder meets
    the road, so a port drawing a Forza tyre on its host's cylinder had the tyre reach below
    the road by the radius difference: the Hummer H1 Alpha's 0.468 m tyres on the Audi Q7's
    0.386 m cylinders sank 8 cm (research/wheels.md, "Tyres in the
    road"). Vanilla sizes them to its own tyres: radius equal on 132 of 140 vehicles, width
    1.005 x the tyre's (median). The header radius is the primitive's bounding sphere,
    sqrt(x^2 + 2 y^2), as vanilla's. A model of another shape is left as it is. Returns the
    patched UPVH and the number of models sized."""
    from dsfforza.formats import upvh as upvh_mod

    h = upvh_mod.parse(upvh)
    data = bytearray(upvh)
    hubs = [w[:3] for w in h.vec4s("wheels")]
    mid = sum(w[2] for w in hubs) / len(hubs) if hubs else 0.0
    base = h.pdl_offset + 0x10
    done = set()
    for r in h.records:
        if r.wheel < 0 or r.wheel >= len(hubs) or r.collision < 0:
            continue
        at = base + r.collision
        if at in done:
            continue
        n = struct.unpack_from("<I", data, at + 12)[0]
        ext = struct.unpack_from("<3f", data, at + 0x10 + 0x50)
        if n != 1 or ext[2] != 0.0:
            continue
        radius, width = front if hubs[r.wheel][2] > mid else rear
        struct.pack_into("<2f", data, at + 0x10 + 0x50, width / 2, radius)
        struct.pack_into("<f", data, at + 8, math.sqrt((width / 2) ** 2 + 2 * radius**2))
        done.add(at)
    return bytes(data), len(done)


# y of the VO3D camera mounts (a vec3 per camera index, research/handling.md "Driver aids,
# steering, cameras"): chase 300..302, bumper 327..329, bonnet 535..537 and the unmeasured ones.
CAMERA_MOUNT_Y = (301, 304, 328, 338, 341, 395, 536, 550, 553)


def settle_wheels(stream, full: Entry, log=print) -> dict:
    """Lower each axle's hubs by the wheel offset the engine draws at rest, so that at rest
    the wheels sit where Forza has them (research/wheels.md, "Wheel clearance").

    The engine draws a wheel `up` above its hub-table position at rest
    (wheel_clearance.rest_offsets: the static compression against VO3D p25, the dead zone
    below p4, the axle's load and spring scale), and the body settles by the same amount.
    convert() puts the hubs on Forza's static pose, so a port sat that much low on its
    wheels: 39 mm at the front of the #2 Audi A4 DTM, whose race-car arches leave less room
    than that.

    Runs on the final VO3D (after the handling). The CoG height p30 (and p599) comes down
    with the hubs by the axles' mean, so the physics, which sees the hierarchy relative to
    the CoG, is unchanged and the body rises; the VO3D camera mounts rise with it."""
    from dsfforza.build import handling
    from dsfforza.convert import wheel_clearance
    from dsfforza.formats import upvh as upvh_mod

    nonr = child(full, "NONR")
    upvh_entry, vo3d_entry = child(nonr, "UPVH"), child(nonr, "VO3D")
    data = payload(stream, upvh_entry)
    vo3d = handling.VO3D.parse(payload(stream, vo3d_entry))
    hubs = [w[:3] for w in upvh_mod.parse(data).vec4s("wheels")]
    mid = sum(h[2] for h in hubs) / len(hubs)
    front = [h for h in hubs if h[2] > mid]
    rear = [h for h in hubs if h[2] <= mid]

    def mean(values):
        values = list(values)
        return sum(values) / len(values)

    front_z, rear_z = mean(h[2] for h in front), mean(h[2] for h in rear)
    up = wheel_clearance.rest_offsets(vo3d.params, front_z, rear_z)
    report = {"front_mm": round(up["front"] * 1000, 1), "rear_mm": round(up["rear"] * 1000, 1)}
    if max(abs(up["front"]), abs(up["rear"])) < 0.001:
        return report
    upvh_entry.payload, patched = patch_hubs(
        data,
        front_z,
        rear_z,
        mean(abs(h[0]) for h in front),
        mean(h[1] for h in front) - up["front"],
        mean(abs(h[0]) for h in rear),
        mean(h[1] for h in rear) - up["rear"],
    )
    drop = (up["front"] + up["rear"]) / 2
    vo3d.params[30] = vo3d.get(30) - drop  # the engine's default when the VO3D has none
    if 599 in vo3d.params:
        vo3d.params[599] -= drop
    # The car's frame (where the VO3D camera mounts are measured from) follows the physics:
    # it sinks against the body with the CoG, so the mounts the VO3D sets go up with the body
    # (the head camera, a hierarchy marker, logs 40 mm higher in that frame after a 36 mm
    # drop).
    for pid in CAMERA_MOUNT_Y:
        if pid in vo3d.params:
            vo3d.params[pid] += drop
    vo3d_entry.payload = vo3d.build()
    report.update({"hubs_patched": patched, "cog_drop_mm": round(drop * 1000, 1)})
    log(
        f"  wheels settled: hubs lowered {report['front_mm']:g} mm front, {report['rear_mm']:g} mm rear "
        f"(the engine's rest offset), CoG p30 by {report['cog_drop_mm']:g} mm"
    )
    return report
