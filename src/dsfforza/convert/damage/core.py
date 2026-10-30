"""Shared pieces of the damage model: UPVH roles, vector helpers, Box and Grid (nearest-point
lookup), normals and load_mesh."""

from __future__ import annotations

import math
import struct
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from dsfforza.formats import vehicle_mesh as vm
from dsfforza.formats.vehicle_mesh import child, payload

# --- UPVH roles (record +0x0A) of part records, from a fleet survey of the 150
# vanilla vehicles (MODL part box centres; research/damage.md). "+x" is the car's
# left (DSF: +z forward, +y up, right-handed); vanilla names don't say which.
ROLES = {
    38: "chassis",
    37: "front structure",
    8: "glass",
    1: "steering wheel",
    185: "gauges",
    32: "door front -x",
    31: "door front +x",
    55: "door rear -x",
    54: "door rear +x",
    4: "bonnet",
    5: "boot",
    124: "hatch/spoiler",
    35: "front bumper",
    36: "rear bumper",
    34: "wing front -x",
    33: "wing front +x",
    51: "headlight -x",
    50: "headlight +x",
    41: "mirror -x",
    40: "mirror +x",
    188: "front suspension",
    190: "rear suspension",
    156: "front hub -x",
    155: "front hub +x",
    136: "exhaust -x",
    15: "exhaust +x",
    53: "side door -x",
    52: "side door +x",
    0: "misc",
}
# Side doors 52/53 hinge like the front doors (vertical axis, limit 0): the Vista
# Cruiser's rear doors (231, which has no 54/55), the vans' side doors (198, 200) and
# the airport fire truck's lockers (186).
DOORS = (31, 32, 54, 55, 52, 53)
# Roles that come in +x/-x pairs.
SIDE_PARTNER = {
    a: b
    for x, y in ((31, 32), (54, 55), (33, 34), (50, 51), (40, 41), (15, 136), (52, 53), (155, 156))
    for a, b in ((x, y), (y, x))
}
BODY = (38, 37, 33, 34) + DOORS
# Records whose hinges/bodies other code owns or that have no Forza geometry.
NOT_MOVED = {1, 185, 188, 190, 155, 156}

# Vertex layout of the converter's damage-capable strides: offsets of s9, s5, s12.
LAYOUT = {32: (16, 20, 24), 28: (12, 16, 20)}
K_NEAREST = 6
COLLISION_SCALE = (0.67, 1.5)  # clamp for a panel's collision box scale
# The root hull is mapped between boxes that leave out this share of the vertices at each
# end per axis: 8 stray vertices 0.4 m above the Spark's roof made its hull that tall.
HULL_TRIM = 0.002
FACING_WEIGHT = 0.15  # normalised-distance penalty per unit of normal mismatch (UV1 lookup)
MAX_HINGE_MOVE = 0.35  # m; a panel hinge mapped further than this keeps the host's
PANEL_FACING = 0.15  # normalised-distance penalty per unit of normal mismatch (panel vote)
NEAREST_RINGS = 2  # Grid.nearest_many: the rings looked in first
NEAREST_FEW = 32  # Grid.nearest_many: fewer query points than this go to Grid.nearest one by one
NEAREST_CUBE = 4  # Grid.nearest_many: every point instead of a cube of more than this many cells per occupied one
NEAREST_CHUNK = 400_000  # Grid.nearest_many: candidates measured at once
NEAREST_SLACK = 1e-9  # Grid.nearest_many: numpy distances this near a decision go to Grid.nearest
PAINT_REACH = 0.12  # normalised score within which a host paint triangle decides a paint triangle's panel
PAINT_MATERIALS = {bytes.fromhex("001283358afd1027"), bytes.fromhex("10122b9653b14e2a")}  # GLOBAL_Chassis(_LOD1)
ISLAND_AREA = 0.08  # m2; smaller connected patches of one panel inside another are merged into it
ISLAND_TRIANGLES = 3  # patches of this many triangles or fewer too (below ISLAND_AREA_MAX)
ISLAND_AREA_MAX = 0.3  # m2
SEAM_NEAR = 0.003  # m; positions of two rigid panels this close form a seam too (not only identical ones)
SEAM_BORDER = 0.015  # m; host vertices of two rigid panels this close are a host panel border
SEAM_STILL = 0.03  # m; host borders of a role pair whose median crush is below this are pinned
SEAM_REACH = 0.2  # m; crush fades in over this distance from a pinned seam
MIRRORS = (40, 41)
MOUNT_TOUCH = 0.02  # m; mirror points this close to a panel's count towards it as the mirror's mount
MOUNT_REACH = 0.06  # m; no panel touched: the nearest one within this is the mount
# Vertex positions are int16 in 1/4096 m (forza2dsf.POSITION_SCALE); the w
# component (baked sky term) uses the same scale, 4096 = 1.
POSITION_UNITS = 4096


def role_name(role: int) -> str:
    return ROLES.get(role, f"role {role}")


# --- small vector helpers ---------------------------------------------------------


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    length = math.sqrt(_dot(a, a))
    return (a[0] / length, a[1] / length, a[2] / length) if length > 1e-12 else None


@dataclass
class Box:
    lo: tuple
    hi: tuple

    @staticmethod
    def of(points) -> Box | None:
        points = list(points)
        if not points:
            return None
        return Box(
            tuple(min(p[a] for p in points) for a in range(3)), tuple(max(p[a] for p in points) for a in range(3))
        )

    @property
    def centre(self):
        return tuple((self.lo[a] + self.hi[a]) / 2 for a in range(3))

    @property
    def size(self):
        return tuple(self.hi[a] - self.lo[a] for a in range(3))

    def to(self, other: Box, p):
        """Map a point by its relative position in this box to `other`; a flat
        axis maps by the centre offset."""
        out = []
        for a in range(3):
            s = self.hi[a] - self.lo[a]
            if s < 1e-3:
                out.append(p[a] - self.centre[a] + other.centre[a])
            else:
                out.append(other.lo[a] + (p[a] - self.lo[a]) / s * (other.hi[a] - other.lo[a]))
        return tuple(out)

    def scale_to(self, other: Box):
        return tuple((other.size[a] / self.size[a]) if self.size[a] > 1e-3 else 1.0 for a in range(3))


# Cell offsets at Chebyshev distance 0, 1, 2 (the shells of the first rings _vote visits).
_SHELLS = [
    [
        (dx, dy, dz)
        for dx in range(-r, r + 1)
        for dy in range(-r, r + 1)
        for dz in range(-r, r + 1)
        if max(abs(dx), abs(dy), abs(dz)) == r
    ]
    for r in range(3)
]


class Grid:
    """k-nearest neighbours over 3D points (a uniform hash grid)."""

    def __init__(self, points, values, cell: float = 0.08):
        self.cell = cell
        self.cells: dict[tuple, list[int]] = defaultdict(list)
        self.points, self.values = points, values
        self._keys = None  # occupied cells as an array (nearest)
        self._arrays = None  # the grid as arrays (nearest_many)
        for n, p in enumerate(points):
            self.cells[self._key(p)].append(n)

    def _key(self, p):
        return (int(math.floor(p[0] / self.cell)), int(math.floor(p[1] / self.cell)), int(math.floor(p[2] / self.cell)))

    def nearest(self, p, k: int = K_NEAREST) -> list[tuple[float, int]]:
        """The k nearest points as sorted (distance, index) pairs: the points of the
        cells in rings 0..r around p's cell (Chebyshev distance in cells), for the
        first ring r at which the k-th nearest of them is within r * cell (then
        nothing outside is nearer), or all points. Rings 0 and 1 are looked up cell
        by cell; farther out only the occupied cells are visited (the rings in
        between add nothing, so the stop test is solved for them)."""
        if not self.points:
            return []
        total, want, cell = len(self.points), min(k, len(self.points)), self.cell
        key = self._key(p)
        found: list[tuple[float, int]] = []

        def stops(lo: int, hi: float) -> bool:
            """Does the ring loop stop at a ring t in lo..hi with the points found so far?"""
            if len(found) < want:
                return False
            kth = sorted(found)[want - 1][0]
            t = max(lo, math.ceil(kth / cell))
            while t > lo and kth <= (t - 1) * cell:
                t -= 1
            while not kth <= t * cell:
                t += 1
            return t <= hi

        for r in (0, 1):
            for dx, dy, dz in _SHELLS[r]:
                for n in self.cells.get((key[0] + dx, key[1] + dy, key[2] + dz), ()):
                    found.append((math.dist(p, self.points[n]), n))
            if stops(r, r) or len(found) == total:
                found.sort()
                return found[:k]
        if self._keys is None:
            self._members = list(self.cells.values())
            self._keys = np.array(list(self.cells), dtype=np.int64).reshape(-1, 3)
        ring = np.abs(self._keys - np.array(key, dtype=np.int64)).max(axis=1)
        far = np.flatnonzero(ring >= 2)
        far = far[ring[far].argsort()]
        rings, order = ring[far].tolist(), far.tolist()
        i, m, done = 0, len(order), 1  # rings 0..done are counted
        while i < m:
            r = rings[i]
            if stops(done + 1, r - 1):  # the empty rings before this one
                break
            while i < m and rings[i] == r:
                found.extend((math.dist(p, self.points[n]), n) for n in self._members[order[i]])
                i += 1
            done = r
            if len(found) == total or stops(r, rings[i] - 1 if i < m else math.inf):
                break
        found.sort()
        return found[:k]

    def nearest_many(self, ps, k: int = K_NEAREST) -> list[list[tuple[float, int]]]:
        """nearest for many points at once, with the same result for each.

        nearest's result is the k first by (distance, index) of the points in rings
        0..R, R the first ring at which at least want = min(k, all) points are within
        R * cell, or the ring by which every point is found. A point j counts toward
        that from ring s_j = max(its ring, the first t with d_j <= t * cell) on, so R
        is the want-th smallest s_j, capped by the last ring. Per query point: the
        distances to the points within rmax rings, in numpy; R by sorting s; the k
        nearest within ring R by sorting the distances; then their distances again
        with math.dist and sorted by (distance, index), as nearest has them. rmax
        runs through NEAREST_RINGS and then covers every point, each round with the
        points the last one didn't decide. numpy's distance differs from math.dist
        in the last bits, so a query point with a distance within NEAREST_SLACK of
        a ring's t * cell, or with points of another position than its k-th within
        2 * NEAREST_SLACK of the k-th's distance (ties included), goes to nearest.
        Copies of a position (the host's split vertices) have the same distance
        both ways and go by index."""
        if not self.points or len(ps) < NEAREST_FEW:
            return [self.nearest(p, k) for p in ps]
        out: list = [None] * len(ps)
        if self._arrays is None:
            members = list(self.cells.values())
            ccount = np.array([len(m) for m in members], dtype=np.int64)
            ckeys = np.array(list(self.cells), dtype=np.int64).reshape(-1, 3)
            corder = np.array([n for m in members for n in m], dtype=np.int64)
            pkeys = np.empty((len(self.points), 3), dtype=np.int64)
            pkeys[corder] = np.repeat(ckeys, ccount, axis=0)
            pts = np.array(self.points, dtype=np.float64).reshape(-1, 3)
            lo, hi = ckeys.min(axis=0), ckeys.max(axis=0)
            size = hi - lo + 1
            code = ((ckeys[:, 0] - lo[0]) * size[1] + (ckeys[:, 1] - lo[1])) * size[2] + (ckeys[:, 2] - lo[2])
            sort = np.argsort(code)
            self._arrays = (
                tuple(np.ascontiguousarray(pts[:, a]) for a in range(3)),
                tuple(np.ascontiguousarray(pkeys[:, a]) for a in range(3)),
                (ckeys, lo, hi, size, code[sort], sort),  # occupied cells, and by code (place in their box)
                (np.cumsum(ccount) - ccount, ccount, corder),  # their points
            )
        (px, py, pz), (kx, ky, kz), (ckeys, lo, hi, size, codes, sort), (cstart, ccount, corder) = self._arrays
        cell, total, slack, big = self.cell, len(self.points), NEAREST_SLACK, np.iinfo(np.int64).max
        want = min(k, total)
        q = np.array(ps, dtype=np.float64).reshape(-1, 3)
        qkey = np.floor(q / cell).astype(np.int64)
        qx, qy, qz = (np.ascontiguousarray(q[:, a]) for a in range(3))
        qkx, qky, qkz = (np.ascontiguousarray(qkey[:, a]) for a in range(3))
        last = np.maximum(np.abs(lo - qkey), np.abs(hi - qkey)).max(axis=1)  # every point found by this ring
        reach = np.full(len(ps), NEAREST_RINGS, dtype=np.int64)  # the rings to look in next
        todo, slow = np.arange(len(ps)), []
        while len(todo):
            undecided = []
            for rmax in np.unique(reach[todo]).tolist():
                part = todo[reach[todo] == rmax]
                every = rmax >= last[part].max()
                if every:  # every point (a superset of the rings): decides every query point
                    lens = np.full(len(part), total, dtype=np.int64)
                    ustart = np.zeros(len(part), dtype=np.int64)
                    upts = np.arange(total, dtype=np.int64)
                    cap = int(last[part].max()) + 1
                else:
                    # Per query cell, the occupied cells within rmax rings (its cube
                    # by rows: the occupied cells of a row along z are one run of
                    # the sorted codes; or for a large cube every occupied cell's
                    # ring), then their points.
                    ukeys, inv = np.unique(qkey[part], axis=0, return_inverse=True)
                    inv = inv.reshape(-1)
                    pu, pc = [], []
                    if (2 * rmax + 1) ** 3 <= NEAREST_CUBE * len(codes):
                        span = np.arange(-rmax, rmax + 1)
                        offs = np.stack(np.meshgrid(span, span, indexing="ij"), axis=-1).reshape(-1, 2)
                        step = max(1, NEAREST_CHUNK // len(offs))
                        for u0 in range(0, len(ukeys), step):
                            u = ukeys[u0 : u0 + step]
                            near = u[:, None, :2] + offs[None, :, :]
                            inside = ((near >= lo[:2]) & (near <= hi[:2])).all(axis=2)
                            near = near - lo[:2]
                            row = (near[:, :, 0] * size[1] + near[:, :, 1]) * size[2]
                            z0 = np.maximum(u[:, 2] - rmax, lo[2]) - lo[2]
                            z1 = np.minimum(u[:, 2] + rmax, hi[2]) - lo[2]
                            a = np.searchsorted(codes, row + z0[:, None], "left")
                            n = np.where(
                                inside & (z0 <= z1)[:, None], np.searchsorted(codes, row + z1[:, None], "right") - a, 0
                            )
                            pu.append(np.repeat(np.arange(u0, u0 + len(u)), n.sum(axis=1)))
                            pc.append(sort[_ranges(a.reshape(-1), n.reshape(-1))])
                    else:
                        step = max(1, NEAREST_CHUNK // len(ckeys))
                        for u0 in range(0, len(ukeys), step):
                            rings = np.abs(ckeys[None, :, :] - ukeys[u0 : u0 + step, None, :]).max(axis=2)
                            ku, kc = np.nonzero(rings <= rmax)
                            pu.append(ku + u0)
                            pc.append(kc)
                    pu, pc = np.concatenate(pu), np.concatenate(pc)
                    ucount = np.bincount(pu, weights=ccount[pc], minlength=len(ukeys)).astype(np.int64)
                    upts = corder[_ranges(cstart[pc], ccount[pc])]  # grouped by query cell, by index in a cell
                    ustart = (np.cumsum(ucount) - ucount)[inv]
                    lens = ucount[inv]
                    cap = rmax + 1
                ends = np.searchsorted(np.cumsum(lens), np.arange(NEAREST_CHUNK, int(lens.sum()), NEAREST_CHUNK))
                for rows in np.split(np.arange(len(part)), np.unique(ends)):
                    if not len(rows):
                        continue
                    sel, n, m = part[rows], lens[rows], len(rows)
                    g = np.repeat(np.arange(m), n)  # each candidate's query point (in sel)
                    j = upts[_ranges(ustart[rows], n)]
                    qg = sel[g]
                    dx, dy, dz = qx[qg] - px[j], qy[qg] - py[j], qz[qg] - pz[j]
                    d = np.sqrt(dx * dx + dy * dy + dz * dz)
                    ring = np.maximum(
                        np.maximum(np.abs(kx[j] - qkx[qg]), np.abs(ky[j] - qky[qg])), np.abs(kz[j] - qkz[qg])
                    )
                    # The first t with d <= t * cell, as nearest's stop test compares:
                    # exact unless d is within slack of a t * cell (then nearest),
                    # or 0 (a copy of the query point, 0 both ways).
                    t = np.ceil(d / cell)
                    bad = (
                        ~np.isfinite(d)
                        | ((np.abs(d - t * cell) <= slack) & ((dx != 0) | (dy != 0) | (dz != 0)))
                        | (np.abs(d - (t - 1) * cell) <= slack)
                    )
                    bad = np.bincount(g, weights=bad.astype(np.float64), minlength=m) > 0
                    t = np.where(bad[g], 0, t).astype(np.int64)
                    slow.append(sel[bad])
                    # The stop ring: the want-th smallest s (counted per ring up to cap).
                    s = np.minimum(np.maximum(ring, t), cap)
                    table = np.bincount(g * (cap + 1) + s, minlength=m * (cap + 1)).reshape(m, cap + 1)
                    reached = table.cumsum(axis=1) >= want
                    stop = np.where(reached.any(axis=1), reached.argmax(axis=1), big)
                    stop = np.where(stop >= cap, big, stop)
                    stop_ring = np.minimum(stop, last[sel])
                    done = ~bad & (every | (stop_ring <= rmax))
                    later = ~bad & ~done
                    if later.any():
                        # want points are within ring max(rmax, the want-th smallest t),
                        # so that is where the stop ring is at the latest.
                        tc = 4 * rmax + 4
                        table = np.bincount(g * (tc + 1) + np.minimum(t, tc), minlength=m * (tc + 1)).reshape(m, tc + 1)
                        reached = table.cumsum(axis=1) >= want
                        bound = np.where(reached[:, :tc].any(axis=1), reached.argmax(axis=1), 3 * rmax)
                        reach[sel[later]] = np.maximum(bound[later], rmax + 1)
                        undecided.append(sel[later])
                    # The k nearest within the stop ring (stopped by count: all within
                    # stop_ring * cell), by distance; a cell's points are by index.
                    limit = np.where(stop <= stop_ring, stop_ring * cell + 4 * slack, np.inf)
                    o = np.flatnonzero(done[g] & (ring <= stop_ring[g]) & (d <= limit[g]))
                    o = o[np.lexsort((d[o], g[o]))]
                    gk, dk, jk = g[o], d[o], j[o]
                    cnt = np.bincount(gk, minlength=m)
                    at = np.cumsum(cnt) - cnt
                    over = done & (cnt > k)
                    tie = np.zeros(m, dtype=bool)
                    tie[over] = dk[at[over] + k] - dk[at[over] + k - 1] <= 2 * slack
                    if tie.any():
                        # Copies of the k-th point (the same position: the same distance
                        # either way) are no tie: they go by index, as in nearest.
                        kth = np.where(tie, at + k - 1, 0)
                        e = np.flatnonzero(tie[gk])
                        ref = jk[kth[gk[e]]]
                        other = (np.abs(dk[e] - dk[kth[gk[e]]]) <= 2 * slack) & (
                            (px[jk[e]] != px[ref]) | (py[jk[e]] != py[ref]) | (pz[jk[e]] != pz[ref])
                        )
                        tie &= np.bincount(gk[e], weights=other.astype(np.float64), minlength=m) > 0
                    slow.append(sel[tie])
                    ok = np.flatnonzero(done & ~tie)
                    take = np.minimum(cnt[ok], k)
                    picked = jk[_ranges(at[ok], take)].tolist()
                    pos = 0
                    for i, c in zip(sel[ok].tolist(), take.tolist(), strict=True):
                        p = ps[i]
                        out[i] = sorted([(math.dist(p, self.points[n]), n) for n in picked[pos : pos + c]])
                        pos += c
            todo = np.concatenate(undecided) if undecided else todo[:0]
        for i in np.concatenate(slow).tolist() if slow else ():
            out[i] = self.nearest(ps[i], k)
        return out


def _ranges(starts, counts) -> np.ndarray:
    """The concatenated ranges starts[i] .. starts[i] + counts[i] - 1."""
    counts = np.asarray(counts, dtype=np.int64)
    ends = np.cumsum(counts)
    return np.arange(int(ends[-1]) if len(ends) else 0, dtype=np.int64) + np.repeat(
        np.asarray(starts, dtype=np.int64) - (ends - counts), counts
    )


# --- normals ----------------------------------------------------------------------


def _angles_of(b0: int, b1: int) -> tuple[float, float]:
    return b0 / 255 * 2 * math.pi - math.pi, b1 / 255 * 2 * math.pi - math.pi


def _normal_of(a0: float, a1: float):
    return (math.sin(a0) * math.sin(a1), math.cos(a0), math.sin(a0) * math.cos(a1))


def _rotate(v, a, b):
    """Rotate v by the rotation that takes unit a onto unit b (Rodrigues)."""
    axis = _cross(a, b)
    s = math.sqrt(_dot(axis, axis))
    c = _dot(a, b)
    if s < 1e-9:
        return v
    k = (axis[0] / s, axis[1] / s, axis[2] / s)
    kv = _cross(k, v)
    kd = _dot(k, v)
    return tuple(v[i] * c + kv[i] * s + k[i] * kd * (1 - c) for i in range(3))


def normal_delta(b0: int, b1: int, face0, face1) -> tuple[int, int]:
    """s5 bytes 2, 3: the polar/azimuth change that turns the stored normal
    (bytes 0, 1) the way the face normals turn from intact to crushed. The
    shader adds (byte / 255 * 2pi - pi) x fDamage to each angle; 127/128 is no change."""
    if face0 is None or face1 is None:
        return 128, 128
    a0, a1 = _angles_of(b0, b1)
    n = _rotate(_normal_of(a0, a1), face0, face1)
    polar = math.acos(max(-1.0, min(1.0, n[1])))
    azimuth = math.atan2(n[0], n[2])
    out = []
    for d in (polar - a0, azimuth - a1):
        d = (d + math.pi) % (2 * math.pi) - math.pi
        out.append(max(0, min(255, round((d + math.pi) / (2 * math.pi) * 255))))
    return out[0], out[1]


def load_mesh(stream, vehc) -> vm.Mesh:
    """One VEHC entry's mesh (its DRSP buffers and submodels, its MODL parts),
    read from `stream` (vehicle_mesh.load opens an archive by entry index)."""
    mesh = vm.Mesh()
    for e in child(child(vehc, "MDXN"), "DRSP").children:
        if e.tag == "VXCK":
            hd = payload(stream, child(e, "VXHD"))
            mesh.vertex_buffers[hd[:8]] = vm.VertexBuffer(
                hd[:8],
                *struct.unpack_from("<II", hd, 8),
                vm.parse_vdcl(payload(stream, child(e, "VDCL"))),
                payload(stream, child(e, "VXDT")),
            )
        elif e.tag == "IXCK":
            mesh.index_buffers[payload(stream, child(e, "IXHD"))[:8]] = payload(stream, child(e, "IXDT"))
        elif e.tag == "SBMC":
            d = payload(stream, child(e, "SBBH"))
            mesh.submodels.append(
                vm.SubModel(
                    d[0:8],
                    d[8:16],
                    d[16:24],
                    struct.unpack_from("<I", d, 0x20)[0],
                    d[0x30:0x38],
                    *struct.unpack_from("<II", d, 0x38),
                )
            )
    mesh.parts = vm.parse_modl(payload(stream, child(child(vehc, "NONR"), "MODL")), mesh.submodels)
    return mesh
