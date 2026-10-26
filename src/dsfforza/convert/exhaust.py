"""Exhaust markers of a converted car, moved onto its Forza tailpipes (research/upvh.md, "Exhaust markers").

Where DSF puts exhaust effects (confirmed-static, FUN_0088E310): every UPVH
marker record (type 3) with role 0x4D (77) is one exhaust. The parser copies
its position (+ the model offset at spec+0x35F0) and quaternion into a
0x2C-byte slot at spec+0x39A0, with the record index and the body of its
nearest ancestor that has one; the slot count is a byte at spec+0x4931. The
next array (role 0x57) starts at spec+0x3A50, so there is room for 4 and no
bounds check. FUN_00881DD0 emits one particle effect per slot through
the marker's matrix, and the engine sound's emitter B sits at the first one
(engine-sound.md). The flow direction is the marker's local -z: stock markers
are identity on rear pipes, Classic 06's right-hand side exit turns -z onto +x.
Stock cars have 0 to 4 (150 cars: 13, 33, 80, 0, 24), one per tailpipe, as
children of the exhaust parts (roles 136 at -x, 15 at +x) or of the body.

A converted car kept the host's markers, so the smoke came out where the
host's pipes were (the Focus ST on host 127: two pipes 0.49 m either side of
the centre line and 15 cm inside its bumper, under a car with one twin-tip
outlet in the middle of the diffuser).

The fix finds the openings on the Forza `exhaust*` sections (the stock
variant the body uses) and moves the host's markers onto them:

1. Tips (find_tips): each connected piece of exhaust mesh flows straight
   back (DSF -z), or out of its side when it ends flush with the body's side
   (GT1 and touring cars' pipes behind the front wheels). Its triangles
   reaching into the outermost DEPTH along the flow are the opening, split
   where they don't connect (a twin tip modelled as one piece stays one wide
   opening). Of openings overlapping across the flow (rim, inner tube, the
   black disc at the bottom, a bracket behind two tips) the outermost stays.
   Openings much deeper inside the body than the outermost one (REACH), rear
   ones well ahead of the rearmost (REAR_REACH: pipe ends at the bumper's
   corners), flat ones and ones too big for a pipe don't count. The flow is
   straight back or straight out: stock markers are identity on rear pipes
   too, and the wall normals of these low-poly tips gave no stable axis.
2. Markers (patch_upvh): as many as the openings, up to the 4 the game has
   room for. A host with fewer gets copies of its own exhaust marker records
   (inserted next to the one copied, same parent); a host with more loses the
   extra records (innermost first). Markers go to the openings in x order; a
   wide opening (twin tip) gets two at the centres of its halves when the
   host has the markers; more tips than 4 share markers between neighbours.
3. No tip found: the host's markers are moved by the host-body-to-Forza-body
   box map the damage plan uses for its panels, or kept without one. A host
   with no exhaust marker keeps none (no exhaust part to hang them on).

The marker edits run last on the UPVH: inserting or dropping records moves
the record and marker indices after them, which the lamp, damage and cockpit
patches hold from their own parse.

    uv run dsff exhaust tips out/cache/fh1/FOR_FocusST_12 [--dy -0.165 --dz -0.05]
    uv run dsff exhaust host <archive> --id 127
    uv run dsff exhaust survey out/final/work --out out/exhaust/survey.json   # host markers vs Forza tips
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dsfforza.formats import upvh as upvh_mod

ROLE = 0x4D  # UPVH marker role of an exhaust (FUN_0088E310)
MAX_MARKERS = 4  # slots at spec+0x39A0, 0x2C each, up to the role-0x57 array at +0x3A50
MARKER_TYPE = 3
SECTION = re.compile(r"exhaus", re.IGNORECASE)  # exhaustLa, exhaustRa, exhausta; `exhausb` on five FM4 cars
WELD = 1e-3  # m: vertices this close are one (pieces are found by shared vertices)
DEPTH = 0.04  # m: the rear end of a piece that counts as its opening
REACH = 0.15  # m: openings this much deeper inside the body than the outermost one are not tailpipes (silencers)
REAR_REACH = 0.15  # m: rear openings this far ahead of the rearmost one are not tailpipes
MAX_GAP = 1.0  # m: an end this far inside the body is no tailpipe (the Leaf's `exhaust` panel 2 m in; 60s saloons' pipes end 0.6-0.8 m ahead of the bumper)
BODY_WINDOW = 0.2  # m: the body around a pipe that its gap is measured to
SIDE_MIN_X = 0.1  # m: pipes nearer the centre line than this exit at the back
SIDE_GAP = 0.08  # m: a side exit ends this close to the body's side (GT1 0.05, V8 Supercar 0.01, DTM 0.07)
BOX_DEPTH = 0.12  # m: the part of the opening's triangles that gives its cross-section
MIN_SIZE, MAX_SIZE = 0.02, 0.45  # m: the opening's larger side
MIN_THICK = 0.01  # m: its smaller side
TWIN_ASPECT = 1.8  # an opening this many times wider than tall can take two markers
BACK = (0.0, 0.0, -1.0)  # DSF model space: +z forward, so the flow of a rear pipe is -z


@dataclass
class Tip:
    """One tailpipe opening, DSF model space."""

    centre: tuple[float, float, float]
    direction: tuple[float, float, float]  # unit, the flow out of the pipe: -z, or +-x for a side exit
    size: tuple[float, float]  # width (x, or z on a side exit) and height of the opening
    gap: float = 0.0  # m from the opening out to the body's surface along the flow

    @property
    def across(self) -> tuple[float, float, float]:
        """The opening's horizontal axis: x, or z on a side exit."""
        return (1.0, 0.0, 0.0) if abs(self.direction[2]) >= abs(self.direction[0]) else (0.0, 0.0, 1.0)

    def report(self) -> dict:
        return {
            "centre": [round(c, 4) for c in self.centre],
            "direction": [round(c, 3) for c in self.direction],
            "size": [round(c, 3) for c in self.size],
            "gap": round(self.gap, 3),
        }


# --- the Forza tips ---------------------------------------------------------------


def exhaust_sections(body) -> list:
    return [s for s in body if SECTION.match(s.name)]


def _triangles(sections, place) -> np.ndarray:
    """(n, 3, 3) triangles of the sections at each one's finest LOD, DSF space."""
    tris = []
    for section in sections:
        lods = {sub.lod for sub in section.subsections}
        if not lods:
            continue
        use = min(lods)
        for sub in section.subsections:
            if sub.lod != use:
                continue
            pool = section.vertices_for(sub)
            tris.extend(tuple(place.point(pool[i].position) for i in tri) for tri in sub.triangles())
    return np.asarray(tris, dtype=float).reshape(-1, 3, 3)


def _welded(tris: np.ndarray) -> np.ndarray:
    """(n, 3) vertex ids, vertices within WELD of each other sharing one."""
    keys = np.round(tris.reshape(-1, 3) / WELD).astype(np.int64)
    _, vid = np.unique(keys, axis=0, return_inverse=True)
    return vid.reshape(-1, 3)


def _pieces(vid: np.ndarray) -> list[np.ndarray]:
    """Triangle index arrays of the connected pieces of a welded mesh."""
    if not len(vid):
        return []
    parent = np.arange(vid.max() + 1)

    def find(a):
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != root:
            parent[a], a = root, parent[a]
        return root

    for a, b, c in vid:
        ra = find(a)
        for v in (b, c):
            rv = find(v)
            if rv != ra:
                parent[rv] = ra
    roots = np.array([find(v) for v in vid[:, 0]])
    return [np.nonzero(roots == r)[0] for r in np.unique(roots)]


def _pieces_of(vid: np.ndarray, subset: np.ndarray) -> list[np.ndarray]:
    """_pieces over the triangles `subset` (indices into vid) alone."""
    return [subset[p] for p in _pieces(vid[subset])]


def _outward(pts: np.ndarray, body: np.ndarray) -> list[tuple[tuple[float, float, float], float]]:
    """A piece's flow directions to try, each with how far the piece's end
    stays inside the body that way: straight back, or first out of the side
    it is on when its outer end is nearer the body's side there than its rear
    end is to the body's back (GT1 cars' pipes behind the front wheels)."""
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    centre = (lo + hi) / 2
    tall = (body[:, 1] >= lo[1] - BODY_WINDOW) & (body[:, 1] <= hi[1] + BODY_WINDOW)
    behind = body[tall & (body[:, 0] >= lo[0] - 0.1) & (body[:, 0] <= hi[0] + 0.1)]
    rear_gap = max(0.0, float(lo[2] - behind[:, 2].min())) if len(behind) else 0.0
    if abs(centre[0]) < SIDE_MIN_X:
        return [(BACK, rear_gap)]
    side = 1.0 if centre[0] > 0 else -1.0
    beside = body[tall & (body[:, 2] >= lo[2] - 0.1) & (body[:, 2] <= hi[2] + 0.1)]
    side_gap = max(0.0, float((side * beside[:, 0]).max() - (side * pts[:, 0]).max())) if len(beside) else 0.0
    if side_gap < min(rear_gap, SIDE_GAP):
        return [((side, 0.0, 0.0), side_gap), (BACK, rear_gap)]
    return [(BACK, rear_gap)]


def _openings(tris: np.ndarray, vid: np.ndarray, piece: np.ndarray, direction) -> list[list]:
    """[direction, lo, hi (3D box of the opening), outermost, gap] per opening
    of a piece flowing `direction`: its triangles reaching into its outer
    DEPTH, split where they don't connect (two tips of one piece). The box
    spans those triangles' corners within BOX_DEPTH of the end, so a tip cut at
    an angle still gets its whole cross-section."""
    d = np.asarray(direction)
    out = tris[piece] @ d  # (n, 3): how far out each corner is along the flow
    end = out.max()
    band = piece[out.max(axis=1) >= end - DEPTH]
    found = []
    for part in _pieces_of(vid, band):
        q = tris[part].reshape(-1, 3)
        q = q[q @ d >= end - BOX_DEPTH]
        found.append([direction, q.min(axis=0), q.max(axis=0), float((q @ d).max())])
    return found


def _across_axes(direction) -> list[int]:
    """The coordinates across the flow: (x, y) for a rear pipe, (z, y) for a side exit."""
    return [0, 1] if abs(direction[2]) >= abs(direction[0]) else [2, 1]


def _fits(extent, direction) -> bool:
    """A tailpipe's size across the flow: not flat (the Leaf's `exhaust` panel), not a sill."""
    size = np.asarray(extent)[_across_axes(direction)]
    return size.min() >= MIN_THICK and MIN_SIZE <= size.max() <= MAX_SIZE


def find_tips(body, place) -> list[Tip]:
    """The tailpipe openings of the Forza body's exhaust sections, DSF space,
    in x order."""
    tris = _triangles(exhaust_sections(body), place)
    if not len(tris):
        return []
    others = _triangles([s for s in body if not SECTION.match(s.name)], place).reshape(-1, 3)
    vid = _welded(tris)
    cands = []  # [direction, lo, hi (3D box of the opening), outermost, gap]
    for piece in _pieces(vid):
        # A pipe along the sill has its whole length at the side: it exits at its rear end.
        tries = _outward(tris[piece].reshape(-1, 3), others)
        for k, (direction, gap) in enumerate(tries):
            found = [[*c, gap] for c in _openings(tris, vid, piece, direction)]
            if k == len(tries) - 1 or any(_fits(c[2] - c[1], direction) for c in found):
                cands += found
                break
    # What lies behind an opening (the inner tube, the disc at its bottom, a
    # bracket or heat shield behind two tips) is not another opening: of
    # boxes overlapping across the flow, the outermost stays.
    kept = []
    for c in sorted(cands, key=lambda c: -c[3]):
        k = _across_axes(c[0])
        if not any(o[0] == c[0] and (c[1][k] < o[2][k]).all() and (o[1][k] < c[2][k]).all() for o in kept):
            kept.append(c)
    if not kept:
        return []
    nearest = min(c[4] for c in kept)
    tips = []
    for direction, lo, hi, end, gap in kept:
        size = (hi - lo)[_across_axes(direction)]
        if gap > min(nearest + REACH, MAX_GAP) or not _fits(hi - lo, direction):
            continue
        centre = (lo + hi) / 2
        a = int(np.argmax(np.abs(direction)))
        centre[a] = end * direction[a]  # the outermost point along the flow
        tips.append(Tip(tuple(float(c) for c in centre), direction, (float(size[0]), float(size[1])), gap))
    # Pipe ends at the rounded corners of the bumper are near the body there but
    # well ahead of the tailpipes (M5, Camaro SS, Legacy): rear tips must be near
    # the rearmost one.
    rearmost = min((t.centre[2] for t in tips if t.direction == BACK), default=0.0)
    tips = [t for t in tips if t.direction != BACK or t.centre[2] <= rearmost + REAR_REACH]
    return sorted(tips, key=lambda t: t.centre[0])


# --- markers ----------------------------------------------------------------------


def quaternion(direction) -> tuple[float, float, float, float]:
    """(x, y, z, w) turning the marker's -z onto `direction` by the shortest arc
    (no roll): Classic 06's side exit (0, -0.71, 0, 0.71) turns -z onto +x."""
    a = BACK
    d = direction
    cross = (a[1] * d[2] - a[2] * d[1], a[2] * d[0] - a[0] * d[2], a[0] * d[1] - a[1] * d[0])
    w = 1.0 + (a[0] * d[0] + a[1] * d[1] + a[2] * d[2])
    if w < 1e-6:  # pointing forward: half a turn about y
        return (0.0, 1.0, 0.0, 0.0)
    norm = math.sqrt(sum(c * c for c in cross) + w * w)
    return (cross[0] / norm, cross[1] / norm, cross[2] / norm, w / norm)


@dataclass
class HostMarker:
    record: int  # record index
    marker: int  # marker table index
    position: tuple[float, float, float]
    quaternion: tuple[float, float, float, float]
    parent: int | None  # record index of the parent


def parents(h) -> dict[int, int | None]:
    """Record index -> its parent's (depth-first, subtree sizes)."""
    out, stack = {}, []
    for r in h.records:
        while stack and stack[-1][1] <= r.index:
            stack.pop()
        out[r.index] = stack[-1][0] if stack else None
        stack.append((r.index, r.index + r.subtree))
    return out


def host_markers(data: bytes) -> list[HostMarker]:
    h = upvh_mod.parse(data)
    par = parents(h)
    table = h.table("markers")
    out = []
    for r in h.records:
        if r.type == MARKER_TYPE and r.role == ROLE and r.marker >= 0:
            out.append(
                HostMarker(
                    r.index,
                    r.marker,
                    struct.unpack_from("<3f", table[r.marker]),
                    struct.unpack_from("<4f", table[r.marker], 16),
                    par[r.index],
                )
            )
    return out


def targets(tips: list[Tip], n: int) -> list[tuple[tuple, tuple]]:
    """n (position, direction) pairs over the tips, in x order: one per tip;
    with more markers than tips the widest openings take a second one at the
    centres of their halves, then the rest double up from the outside in;
    with fewer, the tips merge in neighbouring pairs (closest first)."""
    groups = [[t] for t in tips]
    while len(groups) > n:
        gaps = [math.dist(_mean(groups[i], "centre"), _mean(groups[i + 1], "centre")) for i in range(len(groups) - 1)]
        i = gaps.index(min(gaps))
        groups[i : i + 2] = [groups[i] + groups[i + 1]]
    slots = [[(_mean(g, "centre"), _unit(_mean(g, "direction")))] for g in groups]
    extra = n - len(groups)
    if extra > 0:
        # Twin tips modelled as one wide opening: split those first, widest first.
        wide = sorted(
            (i for i, g in enumerate(groups) if len(g) == 1 and g[0].size[0] >= TWIN_ASPECT * g[0].size[1]),
            key=lambda i: -groups[i][0].size[0],
        )
        for i in wide[:extra]:
            t = groups[i][0]
            q = tuple(c * t.size[0] / 4 for c in t.across)
            slots[i] = [
                (tuple(c - o for c, o in zip(t.centre, q)), t.direction),
                (tuple(c + o for c, o in zip(t.centre, q)), t.direction),
            ]
            extra -= 1
    k = 0
    order = _outside_in(len(slots))
    while extra > 0 and slots:
        i = order[k % len(order)]
        slots[i].append(slots[i][0])
        k += 1
        extra -= 1
    return [s for group in slots for s in group]


def _outside_in(n: int) -> list[int]:
    out, lo, hi = [], 0, n - 1
    while lo <= hi:
        out.append(lo)
        if hi != lo:
            out.append(hi)
        lo, hi = lo + 1, hi - 1
    return out


def _mean(tips, attr) -> tuple[float, float, float]:
    vals = [getattr(t, attr) for t in tips]
    return tuple(sum(v[k] for v in vals) / len(vals) for k in range(3))


def _unit(v) -> tuple[float, float, float]:
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


# --- editing the hierarchy ------------------------------------------------------------


def _header_counts(out: bytearray, records: int, markers: int) -> None:
    struct.pack_into("<I", out, 0x10, records)
    struct.pack_into("<H", out, 0x28, markers)


def insert_marker(data: bytes, after: int) -> bytes:
    """A copy of marker record `after` right after it (same parent), with a new
    marker table entry (a copy of its marker) appended to the marker table."""
    h = upvh_mod.parse(data)
    src = h.records[after]
    if src.type != MARKER_TYPE or src.subtree != 1:
        raise ValueError(f"record {after} is not a leaf marker")
    par = parents(h)
    new_marker = h.counts["markers"]
    rec = bytearray(src.raw)
    struct.pack_into("<h", rec, 0x1A, new_marker)
    records = bytearray(b"".join(r.raw for r in h.records))
    at = (after + 1) * upvh_mod.RECORD_SIZE
    records[at:at] = rec
    # Every ancestor's subtree grows by one.
    p = par[after]
    while p is not None:
        o = p * upvh_mod.RECORD_SIZE + 0x14
        struct.pack_into("<H", records, o, struct.unpack_from("<H", records, o)[0] + 1)
        p = par[p]
    tables_start = upvh_mod.HEADER_SIZE + len(h.records) * upvh_mod.RECORD_SIZE
    markers_end = h.offsets["markers"] + h.counts["markers"] * 0x20
    marker_entry = h.table("markers")[src.marker]
    out = bytearray(
        data[: upvh_mod.HEADER_SIZE] + records + data[tables_start:markers_end] + marker_entry + data[markers_end:]
    )
    _header_counts(out, len(h.records) + 1, new_marker + 1)
    return bytes(out)


def remove_marker(data: bytes, index: int) -> bytes:
    """Drop leaf marker record `index` and its marker table entry (later marker
    indices move down by one)."""
    h = upvh_mod.parse(data)
    src = h.records[index]
    if src.type != MARKER_TYPE or src.subtree != 1:
        raise ValueError(f"record {index} is not a leaf marker")
    par = parents(h)
    records = bytearray(b"".join(r.raw for r in h.records))
    p = par[index]
    while p is not None:
        o = p * upvh_mod.RECORD_SIZE + 0x14
        struct.pack_into("<H", records, o, struct.unpack_from("<H", records, o)[0] - 1)
        p = par[p]
    del records[index * upvh_mod.RECORD_SIZE : (index + 1) * upvh_mod.RECORD_SIZE]
    for i in range(len(h.records) - 1):
        o = i * upvh_mod.RECORD_SIZE + 0x1A
        m = struct.unpack_from("<h", records, o)[0]
        if m > src.marker:
            struct.pack_into("<h", records, o, m - 1)
    tables_start = upvh_mod.HEADER_SIZE + len(h.records) * upvh_mod.RECORD_SIZE
    entry = h.offsets["markers"] + src.marker * 0x20
    out = bytearray(data[: upvh_mod.HEADER_SIZE] + records + data[tables_start:entry] + data[entry + 0x20 :])
    _header_counts(out, len(h.records) - 1, h.counts["markers"] - 1)
    return bytes(out)


def set_count(data: bytes, n: int) -> bytes:
    """Make the host's exhaust markers n: copy the outermost ones (alternating
    sides) or drop from the inside out. A host without any keeps none."""
    while True:
        ms = host_markers(data)
        if not ms or len(ms) == n:
            return data
        by_x = sorted(ms, key=lambda m: m.position[0])
        if len(ms) < n:
            # Copy the marker on the side with fewer markers (left on a tie).
            left = sum(m.position[0] < 0 for m in ms)
            right = len(ms) - left
            src = by_x[0] if left <= right else by_x[-1]
            data = insert_marker(data, src.record)
        else:
            # Drop the innermost marker of the side with more.
            left = [m for m in by_x if m.position[0] < 0]
            right = [m for m in by_x if m.position[0] >= 0]
            side = left if len(left) >= len(right) else right
            drop = min(side, key=lambda m: abs(m.position[0]))
            data = remove_marker(data, drop.record)


def patch_upvh(data: bytes, tips: list[Tip], boxes=None) -> tuple[bytes, dict]:
    """Move the exhaust markers onto the tips (adding or dropping marker
    records to match their number, up to MAX_MARKERS). Without tips, map the
    host's markers from the host body's box to the Forza body's (`boxes`, two
    damage.Box), if given."""
    before = host_markers(data)
    report: dict = {"host": [[round(c, 4) for c in m.position] for m in before]}
    if not before:
        report["kept"] = "host has no exhaust marker"
        return data, report
    if not tips:
        if boxes is None:
            report["kept"] = "no Forza tip"
            return data, report
        out = bytearray(data)
        h = upvh_mod.parse(data)
        moved = []
        for m in before:
            p = boxes[0].to(boxes[1], m.position)
            struct.pack_into("<3f", out, h.offsets["markers"] + m.marker * 0x20, *p)
            moved.append([round(c, 4) for c in p])
        report["fallback"] = moved
        return bytes(out), report
    # One marker per tip; a twin opening modelled as one piece takes two if the
    # host has the markers.
    n = max(min(MAX_MARKERS, len(tips)), min(len(before), MAX_MARKERS, len(tips) + _twins(tips)))
    data = set_count(data, n)
    ms = sorted(host_markers(data), key=lambda m: m.position[0])
    goals = targets(tips, len(ms))
    out = bytearray(data)
    h = upvh_mod.parse(data)
    for m, (pos, direction) in zip(ms, goals):
        at = h.offsets["markers"] + m.marker * 0x20
        w = struct.unpack_from("<f", out, at + 12)[0]
        struct.pack_into("<4f", out, at, *pos, w)
        struct.pack_into("<4f", out, at + 16, *quaternion(direction))
    report["tips"] = [t.report() for t in tips]
    report["markers"] = [[round(c, 4) for c in pos] for pos, _ in goals]
    if len(ms) != len(before):
        report["count"] = [len(before), len(ms)]
    return bytes(out), report


def _twins(tips: list[Tip]) -> int:
    return sum(t.size[0] >= TWIN_ASPECT * t.size[1] for t in tips)


def describe(report: dict) -> str:
    """One log line for a patch_upvh report."""
    if "kept" in report:
        return f"exhaust: host's {len(report['host'])} markers kept ({report['kept']})"
    if "fallback" in report:
        return f"exhaust: no Forza tip; host's {len(report['host'])} markers moved with the body box"
    count = report.get("count", [len(report["host"])] * 2)
    return f"exhaust: {len(report['tips'])} Forza tips, markers {count[0]} -> {count[1]} at " + ", ".join(
        f"({p[0]:+.2f}, {p[1]:+.2f}, {p[2]:+.2f})" for p in report["markers"]
    )


# --- survey and CLI -------------------------------------------------------------------


def _nearest(points, others) -> list[float]:
    return [min(math.dist(p, q) for q in others) for p in points] if others else []


def survey(work: Path, cache: Path, archive: Path, log=print) -> dict:
    """Every car of a port_batch work folder: the host's exhaust markers, the
    Forza tips and the distance from each marker to the nearest tip, before
    (the host's markers as shipped) and after (patch_upvh)."""
    from dsfforza.convert import forza2dsf
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    cars = {}
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        hosts: dict[int, bytes] = {}
        for rep in sorted(work.glob("*.json")):
            if rep.name.endswith((".job.json", ".fp.json")):
                continue
            r = json.loads(rep.read_text())
            if not r.get("ok") or r.get("host") is None:
                continue
            media, game = r["media"], r["game"].lower()
            place_rep = r["entry"]["placement"]
            place = forza2dsf.Placement(dy=place_rep["dy"], dz=place_rep["dz"])
            try:
                car = forza2dsf.load_car(cache / game / media, media)
            except (FileNotFoundError, ValueError) as error:
                log(f"{media}: {error}")
                continue
            if r["host"] not in hosts:
                full = upvh_mod.entries_for(stream, root.entries, r["host"], None)[0]
                hosts[r["host"]] = payload(stream, child(child(full, "NONR"), "UPVH"))
            data = hosts[r["host"]]
            tips = find_tips(car.body, place)
            before = [m.position for m in host_markers(data)]
            new, rep_ = patch_upvh(data, tips)
            after = [m.position for m in host_markers(new)]
            centres = [t.centre for t in tips]
            cars[media] = {
                "host": r["host"],
                "sections": [s.name for s in exhaust_sections(car.body)],
                "tips": [t.report() for t in tips],
                "markers_before": len(before),
                "markers_after": len(after),
                "before": [round(d, 4) for d in _nearest(before, centres)],
                "after": [round(d, 4) for d in _nearest(after, centres)],
                "tips_uncovered_before": [round(d, 4) for d in _nearest(centres, before)],
                "kept": rep_.get("kept"),
            }
    return cars


def summarise(cars: dict) -> dict:
    def stats(vals):
        if not vals:
            return {}
        v = sorted(vals)
        return {
            "n": len(v),
            "median": round(v[len(v) // 2], 3),
            "p90": round(v[int(len(v) * 0.9)], 3),
            "max": round(v[-1], 3),
            "over_10cm": sum(x > 0.10 for x in v),
        }

    with_tips = {k: c for k, c in cars.items() if c["tips"]}
    tip_counts: dict[int, int] = {}
    for c in cars.values():
        tip_counts[len(c["tips"])] = tip_counts.get(len(c["tips"]), 0) + 1
    count_changes: dict[str, int] = {}
    for c in with_tips.values():
        if c["markers_before"] != c["markers_after"]:
            key = f"{c['markers_before']}->{c['markers_after']}"
            count_changes[key] = count_changes.get(key, 0) + 1
    worst = max(with_tips.items(), key=lambda kv: max(kv[1]["before"] or [0]), default=None)
    return {
        "cars": len(cars),
        "no_tip": sorted(k for k, c in cars.items() if not c["tips"]),
        "no_host_marker": sorted(k for k, c in cars.items() if not c["markers_before"]),
        "tips_per_car": dict(sorted(tip_counts.items())),
        # Per car, the worst marker: how far it is from the nearest tip.
        "before": stats([max(c["before"]) for c in with_tips.values() if c["before"]]),
        "after": stats([max(c["after"]) for c in with_tips.values() if c["after"]]),
        "count_changes": count_changes,
        "worst_before": worst[0] if worst else None,
    }


def main(argv: list[str] | None = None) -> None:
    from dsfforza import REPO

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tips", help="the Forza tailpipe openings of a cached car")
    t.add_argument("folder", type=Path)
    t.add_argument("--dy", type=float, default=0.0)
    t.add_argument("--dz", type=float, default=0.0)
    h = sub.add_parser("host", help="a stock vehicle's exhaust markers")
    h.add_argument("archive", type=Path)
    h.add_argument("--id", type=int, required=True)
    s = sub.add_parser("survey", help="markers vs Forza tips over a port_batch work folder")
    s.add_argument("work", type=Path)
    s.add_argument("--cache", type=Path, default=REPO / "out/cache")
    s.add_argument("--archive", type=Path, default=REPO / "out/stock/Install/dngvehicles.sp")
    s.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "tips":
        from dsfforza.convert import forza2dsf

        car = forza2dsf.load_car(args.folder, args.folder.name)
        for tip in find_tips(car.body, forza2dsf.Placement(args.dy, args.dz)):
            print(json.dumps(tip.report()))
    elif args.cmd == "host":
        from dsfforza.formats.chnk import read_chunk
        from dsfforza.formats.vehicle_mesh import child, payload

        with args.archive.open("rb") as stream:
            full = upvh_mod.entries_for(stream, read_chunk(stream, 0).entries, args.id, None)[0]
            data = payload(stream, child(child(full, "NONR"), "UPVH"))
        h_ = upvh_mod.parse(data)
        for m in host_markers(data):
            parent = h_.records[m.parent] if m.parent is not None else None
            print(
                f"record {m.record} marker {m.marker} parent {m.parent} (role {parent.role if parent else '-'}): "
                f"position {tuple(round(c, 3) for c in m.position)} quaternion {tuple(round(c, 3) for c in m.quaternion)}"
            )
    else:
        cars = survey(args.work, args.cache, args.archive)
        summary = summarise(cars)
        print(json.dumps(summary, indent=1))
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps({"summary": summary, "cars": cars}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
