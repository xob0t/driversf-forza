"""The DSF hierarchy and collision: moving subtrees, their fields, collision boxes, compact."""

from __future__ import annotations

import struct

import numpy as np

from dsfforza.convert.damage.core import HULL_TRIM, Box
from dsfforza.formats import upvh as upvh_mod


def _move_in_turn(data: bytes, records: list, parent) -> bytes | None:
    """move_subtree each record (and its subtree) in turn under `parent`, found again
    by type, role, slot and panel-data index after every move; None if one can't go."""

    def find(h, r):
        return next(
            x.index for x in h.records if (x.type, x.role, x.slot, x.model) == (r.type, r.role, r.slot, r.model)
        )

    for r in records:
        h = upvh_mod.parse(data)
        data = move_subtree(data, find(h, r), find(h, parent))
        if data is None:
            return None
    return data


def _parents(h) -> list[int | None]:
    """Record index -> its parent's (depth-first, subtree sizes)."""
    out, stack = [None] * len(h.records), []
    for r in h.records:
        while stack and stack[-1][1] <= r.index:
            stack.pop()
        out[r.index] = stack[-1][0] if stack else None
        stack.append((r.index, r.index + r.subtree))
    return out


def move_subtree(data: bytes, index: int, parent: int) -> bytes | None:
    """Move record `index` and its subtree to be a child of record `parent` (UPVH
    records are depth-first with subtree sizes). Every vanilla hierarchy numbers its
    part records' transform slots (+0x34) and panel-data indices (+0x2A) in record
    order, and vertices carry those numbers, so the subtree goes between the new
    parent's children where that order holds; None if there is no such place (a
    mirror moved under a bonnet after it crashed forceOpenPanels, VM run
    vm2-20261029-030631)."""
    if index <= parent < index + upvh_mod.parse(data).records[index].subtree:
        raise ValueError(f"record {parent} is inside record {index}'s subtree")
    h = upvh_mod.parse(data)
    par = _parents(h)
    size = upvh_mod.RECORD_SIZE
    count = h.records[index].subtree
    if any(r.type == 2 for r in h.records[index : index + count]):
        raise ValueError(f"record {index}'s subtree holds a wheel (+0x0C bit 1 isn't kept)")
    raw = [bytearray(r.raw) for r in h.records]
    sub = [r.subtree for r in h.records]
    p = par[index]
    while p is not None:
        sub[p] -= count
        p = par[p]
    moved = set(range(index, index + count))
    keep = [i for i in range(len(raw)) if i not in moved]
    where = {old: new for new, old in enumerate(keep)}
    ancestors = []
    p = parent
    while p is not None:
        ancestors.append(where[p])
        p = par[p]
    rest_raw, rest_sub = [raw[i] for i in keep], [sub[i] for i in keep]
    block_raw, block_sub = raw[index : index + count], sub[index : index + count]
    pp = where[parent]
    bounds, c = [], pp + 1
    while c < pp + rest_sub[pp]:
        bounds.append(c)
        c += rest_sub[c]
    bounds.append(pp + rest_sub[pp])

    def numbers(records):
        return [
            v
            for r in records
            if struct.unpack_from("<H", r, 0x08)[0] == 1
            for v in [(struct.unpack_from("<h", r, 0x34)[0], struct.unpack_from("<h", r, 0x2A)[0])]
        ]

    mine = numbers(block_raw)

    def fits(b) -> bool:
        before, after = numbers(rest_raw[:b]), numbers(rest_raw[b:])
        return all(
            all(v[a] < m[a] for v in before if v[a] >= 0 for m in mine if m[a] >= 0)
            and all(v[a] > m[a] for v in after if v[a] >= 0 for m in mine if m[a] >= 0)
            for a in (0, 1)
        )

    at = next((b for b in bounds if fits(b)), None)
    if at is None:
        return None
    for a in ancestors:
        rest_sub[a] += count
    new_raw = rest_raw[:at] + block_raw + rest_raw[at:]
    new_sub = rest_sub[:at] + block_sub + rest_sub[at:]
    for r, s in zip(new_raw, new_sub, strict=True):
        struct.pack_into("<H", r, 0x14, s)
    # Fields that follow the tree (_tree_fields) are written again for the new one, on
    # every record that followed them before (a few vanilla exporter quirks stay).
    before = _tree_fields(raw, sub_of=[r.subtree for r in h.records])
    by_id = {id(r): k for k, r in enumerate(raw)}
    for r, fields in zip(new_raw, _tree_fields(new_raw), strict=True):
        if _read_tree_fields(r) == before[by_id[id(r)]]:
            struct.pack_into("<H", r, 0x0C, (struct.unpack_from("<H", r, 0x0C)[0] & ~1) | fields[0])
            struct.pack_into("<HH", r, 0x16, *fields[1:])
    start, end = upvh_mod.HEADER_SIZE, upvh_mod.HEADER_SIZE + len(raw) * size
    return data[:start] + b"".join(bytes(r) for r in new_raw) + data[end:]


def _read_tree_fields(r) -> tuple[int, int, int]:
    return (struct.unpack_from("<H", r, 0x0C)[0] & 1,) + struct.unpack_from("<HH", r, 0x16)


def _tree_fields(records, sub_of=None) -> list[tuple[int, int, int]]:
    """Per raw record, what vanilla hierarchies write from the tree's shape (10,944 of
    10,959 vanilla records; the rest are the list's last record and three headlights):
    +0x0C bit 0, set if the record has a part (type 1) child; +0x18, how many hinged
    records among it and its ancestors end their subtree where its own ends (the hinge
    transforms the engine pops there); +0x16, the same on a leaf but the list's last,
    else 0. A door left with bit 0 and no mirror crashed forceOpenPanels, and so did a
    mirror moved under the chassis with its old pop counts (research/damage.md)."""
    sub = sub_of or [struct.unpack_from("<H", r, 0x14)[0] for r in records]
    par, stack = [None] * len(records), []
    for k in range(len(records)):
        while stack and stack[-1][1] <= k:
            stack.pop()
        par[k] = stack[-1][0] if stack else None
        stack.append((k, k + sub[k]))
    out = []
    for k in range(len(records)):
        kids, c = [], k + 1
        while c < k + sub[k]:
            kids.append(struct.unpack_from("<H", records[c], 0x08)[0])
            c += sub[c]
        end, pops, x = k + sub[k], 0, k
        while x is not None:
            pops += struct.unpack_from("<h", records[x], 0x1C)[0] >= 0 and x + sub[x] == end
            x = par[x]
        leaf = pops if sub[k] == 1 and end < len(records) else 0
        out.append((int(1 in kids), leaf, pops))
    return out


def robust_box(points, trim: float = 0.02) -> Box | None:
    """Box over the 2nd to 98th percentile per axis (a few stray triangles
    assigned to a panel don't stretch it)."""
    points = list(points)
    if not points:
        return None
    lo, hi = [], []
    for a in range(3):
        v = sorted(p[a] for p in points)
        k = int(len(v) * trim)
        lo.append(v[k])
        hi.append(v[len(v) - 1 - k])
    return Box(tuple(lo), tuple(hi))


def trimmed_box(points, trim: float = HULL_TRIM) -> Box | None:
    """Box leaving out `trim` of the points at each end per axis (stray vertices)."""
    p = np.array(list(points), dtype=float).reshape(-1, 3)
    if not len(p):
        return None
    lo, hi = np.percentile(p, (100 * trim, 100 * (1 - trim)), axis=0)
    return Box(tuple(float(c) for c in lo), tuple(float(c) for c in hi))


def _scale_collision(data: bytearray, at: int, scale) -> None:
    """Scale every primitive of one PDL collision model (in its body's frame):
    translation and extents per axis, radius by the largest factor."""
    _size, _nbytes, radius, n = struct.unpack_from("<IIfI", data, at)
    struct.pack_into("<f", data, at + 8, radius * max(scale))
    for k in range(n):
        p = at + 0x10 + k * 0x60
        t = struct.unpack_from("<3f", data, p + 0x30)
        struct.pack_into("<3f", data, p + 0x30, *(t[a] * scale[a] for a in range(3)))
        e = struct.unpack_from("<3f", data, p + 0x50)
        struct.pack_into("<3f", data, p + 0x50, *(e[a] * scale[a] for a in range(3)))


def _map_collision(data: bytearray, at: int, host: Box, forza: Box) -> None:
    """Root hull: primitive centres mapped host box -> Forza box, extents scaled."""
    scale = host.scale_to(forza)
    _size, _nbytes, radius, n = struct.unpack_from("<IIfI", data, at)
    struct.pack_into("<f", data, at + 8, radius * max(scale))
    for k in range(n):
        p = at + 0x10 + k * 0x60
        t = struct.unpack_from("<3f", data, p + 0x30)
        struct.pack_into("<3f", data, p + 0x30, *host.to(forza, t))
        e = struct.unpack_from("<3f", data, p + 0x50)
        struct.pack_into("<3f", data, p + 0x50, *(e[a] * scale[a] for a in range(3)))


def _primitive_bottom(data, p) -> tuple[float, int]:
    """(lowest y of one PDL box primitive's corners, the box axis nearest vertical)."""
    m = struct.unpack_from("<12f", data, p)
    rows = (m[0:3], m[4:7], m[8:11])
    t = struct.unpack_from("<3f", data, p + 0x30)
    e = struct.unpack_from("<3f", data, p + 0x50)
    return t[1] - sum(abs(e[a] * rows[a][1]) for a in range(3)), max(range(3), key=lambda a: abs(rows[a][1]))


def _collision_bottom(data, at: int) -> float:
    """Lowest corner (y) over every primitive of one PDL collision model."""
    n = struct.unpack_from("<I", data, at + 12)[0]
    return min(_primitive_bottom(data, at + 0x10 + k * 0x60)[0] for k in range(n))


def _floor_collision(data: bytearray, at: int, floor: float) -> float:
    """Raise the bottom of every primitive that reaches below `floor` to it, keeping its top:
    the box shrinks and moves up along its most vertical axis. Returns the largest raise.
    The mapped root hull may end no lower than the host's own (mapped onto a low
    race car's sill, the R8 LMS's ended 5 cm above the ground, lower than any vanilla
    hull's 8 cm)."""
    n = struct.unpack_from("<I", data, at + 12)[0]
    most = 0.0
    for k in range(n):
        p = at + 0x10 + k * 0x60
        low, a = _primitive_bottom(data, p)
        if low >= floor - 1e-6:
            continue
        row = struct.unpack_from("<3f", data, p + a * 0x10)
        e = list(struct.unpack_from("<3f", data, p + 0x50))
        half = min((floor - low) / 2 / abs(row[1]), e[a])  # along the axis: half the raise each side
        t = struct.unpack_from("<3f", data, p + 0x30)
        up = 1.0 if row[1] > 0 else -1.0
        struct.pack_into("<3f", data, p + 0x30, *(t[c] + up * half * row[c] for c in range(3)))
        e[a] -= half
        struct.pack_into("<3f", data, p + 0x50, *e)
        most = max(most, floor - low)
    return most


def compact(builder, keep: list) -> None:
    """Keep only `keep` submodels in a forza2dsf.Builder and rebuild its index list."""
    indices = []
    for s in keep:
        seg = builder.indices[s.first_index : s.first_index + s.index_count]
        s.first_index = len(indices)
        indices.extend(seg)
    builder.indices = indices
    builder.subs = list(keep)
