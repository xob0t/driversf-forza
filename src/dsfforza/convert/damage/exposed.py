"""Exposed inner geometry: the zones (bays, boot, bumpers) a crushed panel opens, and
split_exposed, which makes the inner faces drawn there."""

from __future__ import annotations

import dataclasses
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from dsfforza.convert.damage.core import ISLAND_AREA, ISLAND_AREA_MAX, ISLAND_TRIANGLES, Grid, _cross, _dot, _norm, _sub

# --- exposed inner geometry -----------------------------------------------------------
#
# When the bonnet, boot lid or a bumper swings open or falls off, vanilla cars
# show structure drawn from the shared chassis texture (SSIC 00122b73d1fa1227,
# which every GLOBAL_Chassis* material samples: radiator, bulkhead, inner wings,
# strut towers, subframe, boot tub, crash beam and undercarriage pictures) and
# the panel's underside in body paint over the texture's inner-frame picture
# (GLOBAL_Chassis; its blue channel is the paint mask). Baked sky there follows
# the facing: bay floors 0.4-0.7, walls ~0.34, undersides ~0.03, bonnet
# undersides 0.01-0.24 (hosts 147, 193, 227, 251, 266); boot wells 0.23-0.34 and
# the structure behind the bumpers 0.09-0.41 (out/underside/vanillaref-ext.txt).
# Forza models have that geometry too (techniques frame, black, bottom,
# undercarriage...), but the converter put every trim on TRIM_UV, one dark
# texel (R 24), and Forza's AO is baked with the panels shut: a flat black void
# once a panel was gone (BMW M5 bay, testdrive3; M5 and Skyline boot wells and
# rear panels, testdrive4). split_exposed puts that geometry into subsections
# of its own:
#   inner: trim triangles of hood*/trunk* sections facing away from the
#          section's paint (the panel's underside): technique INNER_TECHNIQUE,
#          the shared paint material;
#   zones: trim triangles of other sections hidden by a removable panel
#          (EXPOSED_ZONES): under the bonnet ("bay") or the boot lid ("boot"),
#          behind the rear or front bumper skin ("rear", "front").
# With DamagePlan (add_geometry(exposed=True)) their vertices carry a sentinel
# UV0 per tag, which _expose replaces by a planar projection of the vertex
# (in the zone's or the panel's box) onto that tag's picture, and a
# facing-based sky. Forza bay triangles are large (the M5's floor is 44
# triangles over 1.65 m2), so copying the host's UVs per vertex stretched its
# pictures into streaks; a projection keeps them continuous.

INNER_TECHNIQUE = "dsf_inner"  # forza_materials: paint role
BAY_CELL = 0.05  # m; panel height/depth maps
BAY_CLEARANCE = 0.01  # m below the hood surface
BAY_DEPTH = 0.55  # m below the hood's lowest point that the bay box spans
BOOT_DEPTH = 0.55  # m below the boot lid's lowest point that the boot box spans
BUMPER_CLEARANCE = 0.02  # m inward of a bumper skin's outer surface
BUMPER_DEPTH = 0.6  # m inward of it that counts as behind the bumper
INNER_REACH = 0.2  # m; trim triangle to the nearest paint triangle of its panel
BAY_SKIP = (
    "hood",
    "headlight",
    "taillight",
    "glass",
    "window",
    "mirror",
    "steering",
    "interior",
    "seat",
    "wheel",
    "trunk",
)
ZONE_SKIP = BAY_SKIP + ("bumper", "exhaust", "numplate", "numberplate")


def _rear_bumper(n: str) -> bool:
    return n.startswith(("bumperr", "bumperframer"))


def _front_bumper(n: str) -> bool:
    return n.startswith("bumperf") and not n.startswith("bumperframer")


def _hood(n: str) -> bool:
    return n.startswith("hood")


def _trunk(n: str) -> bool:
    return n.startswith("trunk")


def exposed_zones(engine: str | None = None) -> tuple:
    """Zones a removable panel hides, in priority order: (kind, panel sections,
    map axis, inward sign along it, map value per cell ("inner": the panel's
    innermost point, "outer": its outermost), reach inward of it (None: any)).
    Placed (DSF) space: +y up, +z front. The engine bay ("bay") is under the
    bonnet, or under the boot lid (engine cover) of a mid- or rear-engined car
    (Forza Data_Car.EnginePlacementID; engine_placement), whose bonnet then
    covers a luggage well ("boot")."""
    front, rear = (_hood, _trunk) if engine not in ("mid", "rear") else (_trunk, _hood)
    return (
        ("bay", front, 1, -1, "inner", None),
        ("boot", rear, 1, -1, "inner", None),
        ("rear", _rear_bumper, 2, +1, "outer", BUMPER_DEPTH),
        ("front", _front_bumper, 2, -1, "outer", BUMPER_DEPTH),
    )


EXPOSED_ZONES = exposed_zones()


def engine_placement(name: str, gamedbs=(), folder=None) -> str | None:
    """'front', 'mid' or 'rear' from Forza's gamedb.slt (Data_Car.EnginePlacementID
    of the car's MediaName), from the first of `gamedbs` (else the one next to
    the car's folder) that lists it; None if unknown."""
    import sqlite3

    dbs = [Path(g) for g in gamedbs or () if g]
    if folder is not None:
        dbs.append(Path(folder).parent / "gamedb.slt")
    for db in dbs:
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            row = con.execute(
                "SELECT p.EnginePlacement FROM Data_Car c JOIN List_EnginePlacement p "
                "ON p.ID = c.EnginePlacementID WHERE c.MediaName = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
        except sqlite3.Error:
            continue  # a gamedb without these tables (the connection used to stay open)
        finally:
            con.close()
        if row and row[0]:
            return str(row[0]).lower()
    return None


# tag -> (picture in the shared chassis texture (u0, u1, v0, v1), axis along u, axis along v);
# an axis "y" runs up the picture. Pictures read off out/dmgfix/trimtex-grid-*.png and
# out/underside/atlas-boot.png.
_TUB = (0.420, 0.495, 0.170, 0.300)  # boot tub panels, the part vanilla boot wells use (147: R ~48)
_BEAM = (0.310, 0.500, 0.365, 0.530)  # rear panel and crash beam (vanilla 147/229 behind the bumper)
_SUBFRAME = (0.495, 0.598, 0.560, 0.875)  # subframe from below
_INNER_WING = (0.095, 0.172, 0.100, 0.550)  # inner wing, strut tower
_RADIATOR = (0.207, 0.300, 0.005, 0.135)
EXPOSED_MAP = {
    "bay+y": ((0.216, 0.312, 0.356, 0.534), 2, 0),  # floor: engine and wiring, from above
    "bay-y": (_SUBFRAME, 0, 2),  # underside: subframe from below
    "bay x": (_INNER_WING, 1, 2),  # side walls: inner wing, strut tower
    "bay+z": ((0.315, 0.495, 0.030, 0.330), 0, 1),  # facing forward: bulkhead panelling
    "bay-z": (_RADIATOR, 0, 1),  # facing back: radiator
    "inner y": ((0.354, 0.487, 0.562, 0.746), 0, 2),  # bonnet/boot lid undersides: inner frame (paint)
    "inner z": ((0.354, 0.487, 0.562, 0.746), 0, 1),  # hatch inner faces
    "inner x": ((0.354, 0.487, 0.562, 0.746), 2, 1),
    "boot+y": (_TUB, 0, 2),  # boot floor
    "boot-y": (_SUBFRAME, 0, 2),  # under the boot floor
    "boot x": (_TUB, 2, 1),  # boot side walls
    "boot+z": (_TUB, 0, 1),  # boot rear wall (faces forward)
    "boot-z": (_TUB, 0, 1),  # boot front wall (seat back side)
    "rear+y": (_SUBFRAME, 0, 2),
    "rear-y": (_SUBFRAME, 0, 2),
    "rear x": (_INNER_WING, 2, 1),
    "rear+z": (_BEAM, 0, 1),
    "rear-z": (_BEAM, 0, 1),  # rear panel / crash beam, facing back
    "front+y": (_SUBFRAME, 0, 2),
    "front-y": (_SUBFRAME, 0, 2),
    "front x": (_INNER_WING, 2, 1),
    "front+z": (_RADIATOR, 0, 1),  # facing forward: radiator
    "front-z": (_BEAM, 0, 1),
}
# Sentinel UV0 per tag (exact in half floats; add_geometry writes them, _expose replaces them).
EXPOSED_SENTINEL = {tag: (-2.0, -1.0 - n) for n, tag in enumerate(EXPOSED_MAP)}
INNER_SKY = 0.2  # vanilla bonnet undersides 0.01-0.24
BAY_SKY = (0.04, 0.35, 0.62)  # facing down, sideways, up (vanilla front structure)
ZONE_SKY = {
    "bay": BAY_SKY,
    "boot": (0.08, 0.28, 0.40),  # vanilla boot wells 0.23-0.34 (147, 227, 229, 266, 193)
    "rear": (0.08, 0.28, 0.40),  # vanilla behind the bumpers 0.09-0.41
    "front": (0.08, 0.28, 0.40),
}


def exposed_sky(tag: str, normal) -> float:
    kind = _kind_of(tag)
    if kind == "inner":
        return INNER_SKY
    ny = normal[1] if normal else 0.0
    lo, mid, hi = ZONE_SKY[kind]
    return mid + (hi - mid) * ny if ny >= 0 else mid + (mid - lo) * ny


def _kind_of(tag: str) -> str:
    return re.match(r"[a-z]+", tag).group(0)


def _axis_tag(kind: str, normal) -> str:
    """kind + the dominant axis of a face normal (zones: with its sign for y and z)."""
    a = max(range(3), key=lambda k: abs(normal[k]))
    if kind == "inner":
        return f"inner {'xyz'[a]}"
    if a == 0:
        return f"{kind} x"
    return f"{kind}{'+' if normal[a] > 0 else '-'}{'xyz'[a]}"


def _tri_samples(pts, step: float):
    """Points spread over a triangle about `step` apart (corners included)."""
    longest = max(math.dist(pts[a], pts[b]) for a, b in ((0, 1), (1, 2), (2, 0)))
    n = max(1, min(40, int(math.ceil(longest / step))))
    for i in range(n + 1):
        for j in range(n + 1 - i):
            a, b = i / n, j / n
            yield tuple(pts[0][k] * (1 - a - b) + pts[1][k] * a + pts[2][k] * b for k in range(3))


def _samples_of(tris, step: float) -> tuple[np.ndarray, np.ndarray]:
    """_tri_samples of many triangles (three placed corners each) at once: the
    points as an (m, 3) array, triangle by triangle in _tri_samples' order, with
    its float operations; and each triangle's number of points."""
    ns = [
        max(1, min(40, int(math.ceil(max(math.dist(pts[a], pts[b]) for a, b in ((0, 1), (1, 2), (2, 0))) / step))))
        for pts in tris
    ]
    corners = np.array(tris, dtype=np.float64).reshape(-1, 3, 3)
    ns = np.array(ns, dtype=np.int64)
    counts = (ns + 1) * (ns + 2) // 2
    start = np.cumsum(counts) - counts
    out = np.empty((int(counts.sum()), 3), dtype=np.float64)
    for n in np.unique(ns).tolist():
        ij = [(i, j) for i in range(n + 1) for j in range(n + 1 - i)]
        a = np.array([i / n for i, _ in ij])[None, :, None]
        b = np.array([j / n for _, j in ij])[None, :, None]
        sel = np.flatnonzero(ns == n)
        c = corners[sel]
        pts = c[:, 0, None, :] * (1 - a - b) + c[:, 1, None, :] * a + c[:, 2, None, :] * b
        out[(start[sel][:, None] + np.arange(len(ij))[None, :]).reshape(-1)] = pts.reshape(-1, 3)
    return out, counts


def _cell_codes(kx, ky) -> np.ndarray:
    """A zone map's (x, y) cell keys as one int64 each."""
    return (np.asarray(kx, dtype=np.int64) + (1 << 30)) * (1 << 31) + (np.asarray(ky, dtype=np.int64) + (1 << 30))


def zone_maps(body, place, lods, zones=EXPOSED_ZONES) -> dict:
    """Per (LOD, EXPOSED_ZONES kind): the panel sections' map over BAY_CELL
    cells across the zone's axis (inward coordinate of the panel's innermost or
    outermost point per cell). The points are _tri_samples (BAY_CELL / 2) of
    the sections' triangles; per cell the first innermost or outermost of them
    in that order, as a running max() or min() keeps it."""
    maps = {}
    for kind, is_panel, axis, sign, which, _ in zones:
        across = [a for a in range(3) if a != axis]
        secs = [s for s in body if is_panel(s.name.lower())]
        for lod in lods:
            tris = []
            for s in secs:
                for sub in s.subsections:
                    if sub.lod != lod:
                        continue
                    pool = s.vertices_for(sub)
                    tris += ([place.point(pool[i].position) for i in t] for t in sub.triangles())
            if not tris:
                continue
            pts, _ = _samples_of(tris, BAY_CELL / 2)
            if not np.isfinite(pts).all():
                raise ValueError(f"zone_maps: a {kind} panel point is not finite")
            kx = np.floor(pts[:, across[0]] / BAY_CELL).astype(np.int64)
            ky = np.floor(pts[:, across[1]] / BAY_CELL).astype(np.int64)
            d = sign * pts[:, axis]
            o = np.lexsort((np.arange(len(d)), -d if which == "inner" else d, ky, kx))
            code = _cell_codes(kx[o], ky[o])
            first = o[np.flatnonzero(np.r_[True, code[1:] != code[:-1]])]
            maps[(lod, kind)] = dict(
                zip(zip(kx[first].tolist(), ky[first].tolist(), strict=True), d[first].tolist(), strict=True)
            )
    return maps


def zone_of(maps: dict, lod: int, c, zones=EXPOSED_ZONES) -> str | None:
    """The first zone kind whose panel hides point c (placed) at this LOD."""
    for kind, _, axis, sign, which, reach in zones:
        cells = maps.get((lod, kind))
        if not cells:
            continue
        across = [a for a in range(3) if a != axis]
        d0 = cells.get((math.floor(c[across[0]] / BAY_CELL), math.floor(c[across[1]] / BAY_CELL)))
        if d0 is None:
            continue
        d = sign * c[axis] - d0
        if d > (BAY_CLEARANCE if which == "inner" else BUMPER_CLEARANCE) and (reach is None or d < reach):
            return kind
    return None


ZONE_STEP = 0.1  # m; sample spacing over a triangle for zone_vote
ZONE_SHARE = 0.25  # share of a triangle's samples a zone needs


def zone_vote(maps: dict, lod: int, pts, zones=EXPOSED_ZONES) -> str | None:
    """The zone hiding most of a triangle (corners, centroid and points about
    ZONE_STEP apart), if it hides at least ZONE_SHARE of them. Forza's
    structure behind the panels is a few large triangles (the M5's rear panel,
    the Skyline's boot walls) whose centroids alone often sat just outside the
    panel's map or reach."""
    c = tuple(sum(p[k] for p in pts) / 3 for k in range(3))
    samples = [c] + list(_tri_samples(pts, ZONE_STEP))
    votes = Counter(zone_of(maps, lod, q, zones) for q in samples)
    votes.pop(None, None)
    if not votes:
        return None
    kind, n = votes.most_common(1)[0]
    return kind if n >= ZONE_SHARE * len(samples) else None


def _vote_samples(tris) -> tuple[np.ndarray, np.ndarray]:
    """zone_vote's samples of many triangles at once: per triangle its centroid
    and then its _tri_samples (ZONE_STEP), as an (m, 3) array; and each
    triangle's number of samples. The centroid is zone_vote's own sum() / 3
    (Python's float sum is compensated, not left to right)."""
    centroid = [tuple(sum(p[k] for p in pts) / 3 for k in range(3)) for pts in tris]
    rest, counts = _samples_of(tris, ZONE_STEP)
    total = counts + 1
    start = np.cumsum(total) - total
    pts = np.empty((int(total.sum()), 3), dtype=np.float64)
    rest_at = np.ones(len(pts), dtype=bool)
    rest_at[start] = False
    pts[start] = np.array(centroid, dtype=np.float64).reshape(-1, 3)
    pts[rest_at] = rest
    return pts, total


def _zones_at(maps: dict, lod: int, pts: np.ndarray, zones=EXPOSED_ZONES) -> np.ndarray:
    """zone_of for many points at once: per point the index in `zones` of the
    first zone kind whose map cell hides it, or -1."""
    if not np.isfinite(pts).all():
        raise ValueError("zone_votes: a triangle point is not finite")
    zone = np.full(len(pts), -1, dtype=np.int64)
    for z, (kind, _, axis, sign, which, reach) in enumerate(zones):
        cells = maps.get((lod, kind))
        if not cells:
            continue
        across = [a for a in range(3) if a != axis]
        keys = np.array(list(cells), dtype=np.int64).reshape(-1, 2)
        code = _cell_codes(keys[:, 0], keys[:, 1])
        order = np.argsort(code)
        code, d0 = code[order], np.array(list(cells.values()), dtype=np.float64)[order]
        mine = np.flatnonzero(zone < 0)
        p = pts[mine]
        qk = _cell_codes(np.floor(p[:, across[0]] / BAY_CELL), np.floor(p[:, across[1]] / BAY_CELL))
        at = np.minimum(np.searchsorted(code, qk), len(code) - 1)
        d = sign * p[:, axis] - d0[at]
        hit = (code[at] == qk) & (d > (BAY_CLEARANCE if which == "inner" else BUMPER_CLEARANCE))
        if reach is not None:
            hit &= d < reach
        zone[mine[hit]] = z
    return zone


def zone_votes(maps: dict, lod: int, tris, zones=EXPOSED_ZONES) -> list:
    """zone_vote for many triangles at once, with the same result for each: the
    samples at once (_vote_samples), each one's zone as zone_of finds it
    (_zones_at), and per triangle the zone with the most samples, the one met
    first among equals (Counter.most_common keeps insertion order)."""
    if not tris:
        return []
    pts, total = _vote_samples(tris)
    zone = _zones_at(maps, lod, pts, zones)
    owner = np.repeat(np.arange(len(tris)), total)
    voted = np.flatnonzero(zone >= 0)
    out: list = [None] * len(tris)
    if not len(voted):
        return out
    key, at, cnt = np.unique(owner[voted] * len(zones) + zone[voted], return_index=True, return_counts=True)
    tri, z = key // len(zones), key % len(zones)
    o = np.lexsort((voted[at], -cnt, tri))
    best = o[np.flatnonzero(np.r_[True, tri[o][1:] != tri[o][:-1]])]
    for t, k, n in zip(tri[best].tolist(), z[best].tolist(), cnt[best].tolist(), strict=True):
        if n >= ZONE_SHARE * int(total[t]):
            out[t] = zones[k][0]
    return out


def split_exposed(body, place, log=None, engine: str | None = None, visible: bool = True) -> tuple[list, dict]:
    """Split the trim subsections of `body` that a missing bonnet, boot lid or
    bumper exposes into their own subsections, one per EXPOSED_MAP tag (inner
    faces of hood*/trunk*, technique INNER_TECHNIQUE; trims hidden by a panel,
    by EXPOSED_ZONES zone and facing). Each new subsection carries `exposed`
    (the tag) and `exposed_uv` (its EXPOSED_SENTINEL, which
    add_geometry(exposed=True) writes as UV0). Returns (new body, report).
    Untouched sections and subsections stay the same objects.

    A zone's map only says that a trim lies under the cover or inward of the
    bumper skin. With `visible` (default) a zone triangle that can be seen from
    outside the intact car (trim_art.seen_from_outside, every non-glass
    triangle of its LOD as occluders) keeps its trim: no panel hides it, and
    Forza draws it as the trim it is. FER_355Challenge_95's black tail panel
    lies under the rear edge of its engine cover and drew the bay pictures,
    blurred over the panel between the tail lamps (user report on out/final4;
    research/damage.md, "Visible trims keep their trim")."""
    from dsfforza.convert import forza_materials as fm
    from dsfforza.formats import carbin

    lods = sorted({sub.lod for s in body for sub in s.subsections if sub.lod > 0})
    zones = exposed_zones(engine)
    maps = zone_maps(body, place, lods, zones)
    report = Counter()
    tagged = {}  # (section index, subsection index) -> (triangles, [tag per triangle])
    for si, section in enumerate(body):
        name = section.name.lower()
        panel = name.startswith(("hood", "trunk"))
        bay_ok = not name.startswith(BAY_SKIP)
        zone_ok = not name.startswith(ZONE_SKIP)
        paint_grid = {}
        for ui, sub in enumerate(section.subsections):
            role = fm.classify(sub.material)[0]
            if sub.lod <= 0 or role != fm.TRIM or "plate" in sub.material.lower() or not (panel or bay_ok):
                continue
            pool = section.vertices_for(sub)
            if panel and sub.lod not in paint_grid:
                cents, normals = [], []
                for other in section.subsections:
                    if other.lod != sub.lod or fm.classify(other.material)[0] != fm.PAINT:
                        continue
                    opool = section.vertices_for(other)
                    for t in other.triangles():
                        pts = [place.point(opool[i].position) for i in t]
                        nrm = _norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])))
                        if nrm is not None:
                            cents.append(tuple(sum(p[k] for p in pts) / 3 for k in range(3)))
                            normals.append(nrm)
                paint_grid[sub.lod] = Grid(cents, normals, cell=0.1)
            tris = list(sub.triangles())
            placed = [[place.point(pool[i].position) for i in t] for t in tris]
            normals = [_norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0]))) for pts in placed]
            # The zone votes of the trim triangles at once (zone_votes).
            votes = iter(
                []
                if panel
                else zone_votes(maps, sub.lod, [pts for pts, nrm in zip(placed, normals) if nrm is not None], zones)
            )
            tags = []
            for pts, nrm in zip(placed, normals, strict=True):
                c = tuple(sum(p[k] for p in pts) / 3 for k in range(3))
                tag = None
                if panel and nrm is not None:
                    grid = paint_grid[sub.lod]
                    hits = grid.nearest(c, 1)
                    if hits and hits[0][0] < INNER_REACH and _dot(nrm, grid.values[hits[0][1]]) < -0.3:
                        tag = _axis_tag("inner", nrm)
                elif not panel and nrm is not None:
                    kind = next(votes)
                    # Bumper parts and exhausts (they leave with their panel or are
                    # their own) keep their trim outside the bay, as before.
                    if kind is not None and (kind == "bay" or zone_ok):
                        tag = _axis_tag(kind, nrm)
                tags.append(tag)
            if any(tags):
                tagged[(si, ui)] = (tris, tags)
    if visible and tagged:
        for key, picked in _seen_zone_triangles(body, tagged).items():
            tags = tagged[key][1]
            for k in picked:
                report[f"seen {_kind_of(tags[k])} lod {body[key[0]].subsections[key[1]].lod}"] += 1
                tags[k] = None
    out = []
    for si, section in enumerate(body):
        if not any(key[0] == si and any(tagged[key][1]) for key in tagged):
            out.append(section)
            continue
        new_subs = []
        for ui, sub in enumerate(section.subsections):
            if (si, ui) not in tagged or not any(tagged[(si, ui)][1]):
                new_subs.append(sub)
                continue
            groups = defaultdict(list)
            for t, tag in zip(*tagged[(si, ui)], strict=True):
                groups[tag].extend(t)
            for tag, idx in groups.items():
                material = INNER_TECHNIQUE if tag and tag.startswith("inner") else sub.material
                part = carbin.SubSection(material, sub.lod, 4, sub.uv_transform, idx)
                part.exposed, part.exposed_uv = tag, EXPOSED_SENTINEL.get(tag)
                new_subs.append(part)
                if tag:
                    report[f"{_kind_of(tag)} lod {sub.lod}"] += len(idx) // 3
        out.append(dataclasses.replace(section, subsections=new_subs))
    if log and report:
        log("exposed geometry: " + ", ".join(f"{k} {v}" for k, v in sorted(report.items())))
    return out, dict(report)


def _seen_zone_triangles(body, tagged: dict) -> dict:
    """{(section index, subsection index): {triangle index}} of the zone-tagged
    (not "inner") triangles in `tagged` that trim_art.seen_from_outside sees from
    outside the intact car, LOD by LOD (Forza space: the views only need +y up)."""
    from dsfforza.convert import trim_art

    found = defaultdict(list)  # lod -> [(key, triangle index, corners)]
    for key, (tris, tags) in tagged.items():
        section = body[key[0]]
        sub = section.subsections[key[1]]
        pool = section.vertices_for(sub)
        for k, (t, tag) in enumerate(zip(tris, tags, strict=True)):
            if tag and not tag.startswith("inner"):
                found[sub.lod].append((key, k, [pool[i].position for i in t]))
    out = defaultdict(set)
    for lod, items in sorted(found.items()):
        candidates = np.array([c for _, _, c in items], dtype=np.float64).reshape(-1, 3, 3)
        seen = trim_art.seen_from_outside(trim_art.lod_triangles(body, lod), candidates)
        for (key, k, _), s in zip(items, seen.tolist(), strict=True):
            if s:
                out[key].add(k)
    return dict(out)


def smooth_labels(labels: list, areas: list, corners: list, passes: int = 2) -> tuple[list, int]:
    """Clean up a per-triangle panel labelling over the mesh (triangles joined
    by shared edges, corners given as position keys): `passes` rounds of an
    area-weighted majority filter, then connected patches of one label smaller
    than ISLAND_AREA (or of at most ISLAND_TRIANGLES triangles, below
    ISLAND_AREA_MAX) take the label they share most edges with. Returns
    (labels, patches merged)."""
    n = len(labels)
    owners = defaultdict(list)
    for t, cs in enumerate(corners):
        for a, b in ((0, 1), (1, 2), (2, 0)):
            if cs[a] != cs[b]:
                owners[(min(cs[a], cs[b]), max(cs[a], cs[b]))].append(t)
    nbrs = [[] for _ in range(n)]
    for ts in owners.values():
        if 2 <= len(ts) <= 4:
            for a in ts:
                nbrs[a].extend(b for b in ts if b != a)
    labels = list(labels)
    for _ in range(passes):
        new = list(labels)
        for t in range(n):
            if nbrs[t]:
                w = Counter({labels[t]: areas[t]})
                for u in nbrs[t]:
                    w[labels[u]] += areas[u]
                new[t] = w.most_common(1)[0][0]
        labels = new
    merged = 0
    for _ in range(12):
        comp, patches = [-1] * n, []
        for t in range(n):
            if comp[t] >= 0:
                continue
            comp[t], members, stack = len(patches), [], [t]
            while stack:
                x = stack.pop()
                members.append(x)
                for u in nbrs[x]:
                    if comp[u] < 0 and labels[u] == labels[t]:
                        comp[u] = comp[t]
                        stack.append(u)
            patches.append((sum(areas[x] for x in members), members))
        changed = 0
        for area, members in sorted(patches, key=lambda p: p[0]):
            if area >= ISLAND_AREA_MAX:
                break
            if area >= ISLAND_AREA and len(members) > ISLAND_TRIANGLES:
                continue
            own = labels[members[0]]
            border = Counter(labels[u] for x in members for u in nbrs[x] if labels[u] != own)
            if not border:
                continue  # a separate piece of geometry: keep its vote
            new = border.most_common(1)[0][0]
            for x in members:
                labels[x] = new
            changed += 1
        merged += changed
        if not changed:
            break
    return labels, merged
