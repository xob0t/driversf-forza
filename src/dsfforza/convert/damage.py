"""Vanilla-like damage for converted Forza cars: panels, crush morph, hinges.

How a vanilla DSF car damages (research/damage.md, confirmed-static from the
shared vehicle shaders in SSIC and the vanilla geometry):

- Vertex semantic 9 bytes 0 and 1 pick the panel. Byte 0 (UPVH record +0x34)
  indexes g_PanelTransforms (a 3x4 matrix per panel: hinge swing, detached
  debris); byte 1 (record +0x2A) indexes g_v4PerPanelData, the panel's damage
  state. Byte 2 is the lamp, byte 3 is 1.
- Semantic 12 ("vec3DamagePos", half4) is the panel's fully crushed position.
  The vertex shader draws lerp(position, s12, fDamage), with
  fDamage = PerPanelData[byte 1].x - offset * x (a per-panel amount with a
  left-right gradient). Vanilla cars push the nose back by about 6% of the car
  length and the tail forward by about 2%, buckle the bonnet up and dent the
  doors by a few centimetres.
- Semantic 5 bytes 2 and 3 are the normal's polar/azimuth change at full
  damage (angle = byte / 255 * 2pi - pi), so dents shade.
- MODL part 0 of the full entry holds every panel's triangles in one
  submodel per material (the intact car, skinned by panel); each panel's own
  MODL part holds the same vertices again with its own index lists. The
  exterior entry holds the merged part 0 only.

Converted cars had every vertex on the chassis panel with s12 = position: no
crush, no swinging bonnet/boot/bumpers, nothing to fall off. This module
assigns each Forza triangle to one of the host's panels by role (Forza
section name first, then the nearest host panel), copies the host's crush
field onto the Forza vertices (inverse-distance weighted, in car-normalised
coordinates, scaled to the Forza car), writes the normal deltas, builds the
per-panel MODL parts, and moves the host's panel hinges, rigid bodies and
collision boxes onto the Forza panels.

Use: forza2dsf.ConvertOptions(damage=True) (port_car.py --damage). CLI:

    uv run dsff damage panels <archive> --id 266          # the host's panel records
    uv run dsff damage render <archive> --id 304 --png out/x.png [--kind 0|2] [--by part|panel]
                                  [--damage 1] [--parts 2,4,11,12]
    uv run dsff damage check <archive> --id 304           # structural checks of a converted car
"""

from __future__ import annotations

import argparse
import colorsys
import dataclasses
import itertools
import math
import re
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from dsfforza.formats import upvh as upvh_mod
from dsfforza.formats import vehicle_mesh as vm
from dsfforza.formats.chnk import read_chunk
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
    53: "sill -x",
    52: "sill +x",
    0: "misc",
}
DOORS = (31, 32, 54, 55)
# Roles that come in +x/-x pairs.
SIDE_PARTNER = {
    a: b
    for x, y in ((31, 32), (54, 55), (33, 34), (50, 51), (40, 41), (15, 136), (52, 53), (155, 156))
    for a, b in ((x, y), (y, x))
}
BODY = (38, 37, 33, 34, 52, 53) + DOORS
# Records whose hinges/bodies other code owns or that have no Forza geometry.
NOT_MOVED = {1, 185, 188, 190, 155, 156}

# Vertex layout of the converter's damage-capable strides: offsets of s9, s5, s12.
LAYOUT = {32: (16, 20, 24), 28: (12, 16, 20)}
K_NEAREST = 6
VOTES = 7  # host vertices voting on a body triangle's panel
COLLISION_SCALE = (0.67, 1.5)  # clamp for a panel's collision box scale
DEGENERATE_UV = 0.004  # host UV1 triangles smaller than this are hidden faces
FACING_WEIGHT = 0.15  # normalised-distance penalty per unit of normal mismatch (UV1 lookup)
MAX_HINGE_MOVE = 0.35  # m; a panel hinge mapped further than this keeps the host's
PANEL_FACING = 0.15  # normalised-distance penalty per unit of normal mismatch (panel vote)
VOTE_RINGS = (1, 2, 3, 5, 7)  # DamagePlan._votes: rings (at most 7) up to which points are decided in turn
VOTE_BLOCK = 4  # DamagePlan._votes: candidates are found per block of this many cells a side
VOTE_CHUNK = 400_000  # DamagePlan._votes: candidates scored at once
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
                    # of offsets looked up by code, or for a large cube every
                    # occupied cell's ring), then their points.
                    ukeys, inv = np.unique(qkey[part], axis=0, return_inverse=True)
                    inv = inv.reshape(-1)
                    pu, pc = [], []
                    if (2 * rmax + 1) ** 3 <= NEAREST_CUBE * len(codes):
                        span = np.arange(-rmax, rmax + 1)
                        offs = np.stack(np.meshgrid(span, span, span, indexing="ij"), axis=-1).reshape(-1, 3)
                        step = max(1, NEAREST_CHUNK // len(offs))
                        for u0 in range(0, len(ukeys), step):
                            near = ukeys[u0 : u0 + step, None, :] + offs[None, :, :]
                            inside = ((near >= lo) & (near <= hi)).all(axis=2)
                            near = near - lo
                            c = (near[:, :, 0] * size[1] + near[:, :, 1]) * size[2] + near[:, :, 2]
                            at = np.minimum(np.searchsorted(codes, c), len(codes) - 1)
                            ku, ko = np.nonzero(inside & (codes[at] == c))
                            pu.append(ku + u0)
                            pc.append(sort[at[ku, ko]])
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


# --- decal / scratch UVs ------------------------------------------------------------
#
# The vehicle pixel shader samples the shared damage-decal texture (SSIC
# 00122b6db3fa1227, 1024x1024 DXT5: dent normals in RGB, scratch mask in
# alpha) and the dirt texture (001251560b05e528) at UV1, semantic 8
# components 2-3 of the 32-byte buffers (paint and lamps). Vanilla cars unwrap
# each panel onto its own tile of that atlas (front bumper top left, rear
# bumper top right, bonnet, boot, sides in the rows below; lamps on the
# small broken-lamp tile). Converted vertices carried Forza's UV1 (paint) or
# the lamp atlas UV, so damaged cars showed scratch tiles in the wrong places
# as bright white shapes (run 20261022-040545). Uv1Field projects each new
# vertex onto the nearest host triangle of the same panel and material and
# interpolates the host's UV1 there.


def closest_on_triangle(p, a, b, c):
    """Closest point to p on triangle abc: (squared distance, barycentric (u, v, w))."""
    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        bary = (1.0, 0.0, 0.0)
    else:
        bp = _sub(p, b)
        d3, d4 = _dot(ab, bp), _dot(ac, bp)
        cp = _sub(p, c)
        d5, d6 = _dot(ab, cp), _dot(ac, cp)
        vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
        if d3 >= 0 and d4 <= d3:
            bary = (0.0, 1.0, 0.0)
        elif d6 >= 0 and d5 <= d6:
            bary = (0.0, 0.0, 1.0)
        elif vc <= 0 and d1 >= 0 and d3 <= 0:
            t = d1 / (d1 - d3) if d1 != d3 else 0.0
            bary = (1 - t, t, 0.0)
        elif vb <= 0 and d2 >= 0 and d6 <= 0:
            t = d2 / (d2 - d6) if d2 != d6 else 0.0
            bary = (1 - t, 0.0, t)
        elif va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
            t = (d4 - d3) / ((d4 - d3) + (d5 - d6)) if (d4 - d3) + (d5 - d6) else 0.0
            bary = (0.0, 1 - t, t)
        else:
            denom = va + vb + vc
            v, w = (vb / denom, vc / denom) if denom else (0.0, 0.0)
            bary = (1 - v - w, v, w)
    q = tuple(bary[0] * a[k] + bary[1] * b[k] + bary[2] * c[k] for k in range(3))
    d = _sub(p, q)
    return _dot(d, d), bary


def closest_on_triangles(p, corners):
    """closest_on_triangle for many triangles at once (corners: (n, 3, 3) array of
    a, b, c), with its float operations and branch order: (squared distances,
    barycentric (n, 3))."""
    corners = np.asarray(corners, dtype=np.float64)
    d, b0, b1, b2 = _closest(p, *(np.ascontiguousarray(corners[:, i // 3, i % 3]) for i in range(9)))
    return d, np.stack((b0, b1, b2), axis=1)


def _closest(p, ax, ay, az, bx, by, bz, cx, cy, cz):
    """closest_on_triangle over component arrays: (squared distance, barycentric u, v, w)."""
    px, py, pz = p
    abx, aby, abz, acx, acy, acz = bx - ax, by - ay, bz - az, cx - ax, cy - ay, cz - az
    apx, apy, apz = px - ax, py - ay, pz - az
    d1 = abx * apx + aby * apy + abz * apz
    d2 = acx * apx + acy * apy + acz * apz
    bpx, bpy, bpz = px - bx, py - by, pz - bz
    d3 = abx * bpx + aby * bpy + abz * bpz
    d4 = acx * bpx + acy * bpy + acz * bpz
    cpx, cpy, cpz = px - cx, py - cy, pz - cz
    d5 = abx * cpx + aby * cpy + abz * cpz
    d6 = acx * cpx + acy * cpy + acz * cpz
    vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
    e, f = d4 - d3, d5 - d6
    denom = va + vb + vc
    t3 = np.divide(d1, d1 - d3, out=np.zeros_like(d1), where=d1 != d3)
    t4 = np.divide(d2, d2 - d6, out=np.zeros_like(d1), where=d2 != d6)
    t5 = np.divide(e, e + f, out=np.zeros_like(d1), where=(e + f) != 0)
    v = np.divide(vb, denom, out=np.zeros_like(d1), where=denom != 0)
    w = np.divide(vc, denom, out=np.zeros_like(d1), where=denom != 0)
    # The branches from the last to the first, so the first that applies wins.
    b0, b1, b2 = 1 - v - w, v, w
    for cond, (x0, x1, x2) in (
        ((va <= 0) & (e >= 0) & (f >= 0), (0.0, 1 - t5, t5)),
        ((vb <= 0) & (d2 >= 0) & (d6 <= 0), (1 - t4, 0.0, t4)),
        ((vc <= 0) & (d1 >= 0) & (d3 <= 0), (1 - t3, t3, 0.0)),
        ((d6 >= 0) & (d5 <= d6), (0.0, 0.0, 1.0)),
        ((d3 >= 0) & (d4 <= d3), (0.0, 1.0, 0.0)),
        ((d1 <= 0) & (d2 <= 0), (1.0, 0.0, 0.0)),
    ):
        if cond.any():
            b0, b1, b2 = np.where(cond, x0, b0), np.where(cond, x1, b1), np.where(cond, x2, b2)
    dx = px - (b0 * ax + b1 * bx + b2 * cx)
    dy = py - (b0 * ay + b1 * by + b2 * cy)
    dz = pz - (b0 * az + b1 * bz + b2 * cz)
    return dx * dx + dy * dy + dz * dz, b0, b1, b2


def _tri_arrays(f) -> tuple:
    """(corner components ax, ay, az, bx, ..., cz, normal components nx, ny, nz) of a
    HostField's triangles as arrays, made once."""
    cached = f.__dict__.get("_tri_arrays")
    if cached is None or cached[0] != len(f.tris):
        corners = np.array([t[0] for t in f.tris], dtype=np.float64).reshape(-1, 3, 3)
        normals = np.array([t[2] for t in f.tris], dtype=np.float64).reshape(-1, 3)
        cached = f._tri_arrays = (
            len(f.tris),
            tuple(np.ascontiguousarray(corners[:, i // 3, i % 3]) for i in range(9)),
            tuple(np.ascontiguousarray(normals[:, i]) for i in range(3)),
        )
    return cached[1], cached[2]


class Uv1Field:
    """The host's UV1 (decal-atlas) triangles of one entry's MODL part 0, in
    coordinates normalised by the host car box, grouped for lookups by
    (panel pair, material), material class and all."""

    def __init__(self, tris, box: Box):
        self.box = box
        self.tris = tris  # (normalised a, b, c), (uv a, b, c), pair, material, local, unit normal
        self.groups: dict[tuple, Grid] = {}
        keys = defaultdict(list)
        for n, t in enumerate(tris):
            uvs = t[1]
            if max(abs(uvs[i][k] - uvs[0][k]) for i in (1, 2) for k in (0, 1)) < DEGENERATE_UV:
                # Hidden faces (wheel arches, undersides) all sit on one texel in a
                # corner of the atlas; a vertex projected there drags its triangles
                # across the whole atlas.
                keys[("degenerate",)].append(n)
                continue
            if any(not -0.01 <= uv[k] <= 1.01 for uv in uvs for k in (0, 1)):
                # Tiled UV1 (vanilla interiors' GLOBAL_LOD_Chrome runs to -4..5): not the
                # panel atlas layout.
                keys[("tiled",)].append(n)
                continue
            keys[("pm", t[2], t[3])].append(n)
            keys[("pc", t[2], t[4])].append(n)
            keys[("c", t[4])].append(n)
            keys[("all",)].append(n)
        # Charts: triangles joined through vertices with the same position and UV1.
        parent = list(range(len(tris)))

        def find(n):
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n

        owner = {}
        for n, t in enumerate(tris):
            for p, uv in zip(t[0], t[1]):
                key = (tuple(round(c, 4) for c in p), round(uv[0], 4), round(uv[1], 4))
                if key in owner:
                    parent[find(n)] = find(owner[key])
                else:
                    owner[key] = n
        self.chart = [find(n) for n in range(len(tris))]
        for n in keys[("all",)]:
            keys[("chart", self.chart[n])].append(n)
        for key, ns in keys.items():
            self.groups[key] = Grid(
                [tuple(sum(v[a] for v in tris[n][0]) / 3 for a in range(3)) for n in ns], ns, cell=0.06
            )

    @staticmethod
    def read(stream, vehc, mask: int, local_materials: set) -> Uv1Field | None:
        mesh = load_mesh(stream, vehc)
        raw = []
        for lod in mesh.parts[0].lods:
            if lod.mask != mask:
                continue
            for n in lod.submodels:
                sub = mesh.submodels[n]
                for vb, tri in vm.triangles(mesh, sub):
                    e8 = vb.element(8)
                    if e8 is None or e8.components != 4:
                        continue
                    pts = [vm.position(vb, i) for i in tri]
                    uvs = [tuple(vb.read(e8, i)[2:4]) for i in tri]
                    normal = _norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])))
                    if normal is None:
                        continue
                    raw.append((pts, uvs, s9_pair(vb, tri[0]), sub.material, sub.material in local_materials, normal))
        if not raw:
            return None
        box = Box.of(p for t in raw for p in t[0])
        c, s = box.centre, box.size

        def norm(p):
            return tuple((p[a] - c[a]) / (s[a] / 2) for a in range(3))

        return Uv1Field([(tuple(norm(p) for p in t[0]),) + t[1:] for t in raw], box)

    def lookup(
        self, q, pair, material: bytes, local: bool, any_panel: bool = False, normal=None, chart: int | None = None
    ):
        """(UV1, chart) at normalised point q from the nearest host triangle
        facing the same way (`normal`, car space): same panel and material, else
        same panel and material class, else same class; only `chart` if given."""
        for key in self._keys(pair, material, local, any_panel, chart):
            grid = self.groups.get(key)
            if grid is None:
                continue
            best = None
            for _, n in grid.nearest(q, 16):
                t = self.tris[grid.values[n]]
                d, bary = closest_on_triangle(q, *t[0])
                score = math.sqrt(d) + (FACING_WEIGHT * (1 - _dot(normal, t[5])) if normal else 0.0)
                if best is None or score < best[0]:
                    best = (score, bary, t[1], self.chart[grid.values[n]])
            if best is not None:
                bary, uvs = best[1], best[2]
                return (
                    (sum(bary[k] * uvs[k][0] for k in range(3)), sum(bary[k] * uvs[k][1] for k in range(3))),
                    best[3],
                )
        return None

    @staticmethod
    def _keys(pair, material: bytes, local: bool, any_panel: bool, chart: int | None) -> list:
        """The groups lookup tries, in order."""
        return (
            ([("chart", chart)] if chart is not None else [])
            + ([] if any_panel else [("pm", pair, material), ("pc", pair, local)])
            + [("c", local), ("all",), ("degenerate",), ("tiled",)]
        )

    def lookup_many(self, items) -> list:
        """lookup for many (q, pair, material, local, any_panel, normal, chart) at
        once, with the same result for each. lookup's result comes from the first
        group it has that has points: the candidates are that group's 16 nearest
        (Grid.nearest_many), each scored as lookup does (_closest, the same floats
        as closest_on_triangle), and the first lowest score in nearest's order wins
        (lookup keeps a best only on a strictly lower score). The winner's
        barycentric coordinates and UV1 are lookup's own code. An item with a
        non-finite score goes to lookup."""
        out: list = [None] * len(items)
        by_group = defaultdict(list)
        for i, (_, pair, material, local, any_panel, _, chart) in enumerate(items):
            for key in self._keys(pair, material, local, any_panel, chart):
                grid = self.groups.get(key)
                if grid is not None and grid.points:
                    by_group[key].append(i)
                    break
        if not by_group:
            return out
        if self.__dict__.get("_arrays") is None:
            corners = np.array([t[0] for t in self.tris], dtype=np.float64).reshape(-1, 3, 3)
            normals = np.array([t[5] for t in self.tris], dtype=np.float64).reshape(-1, 3)
            self._arrays = (
                tuple(np.ascontiguousarray(corners[:, i // 3, i % 3]) for i in range(9)),
                tuple(np.ascontiguousarray(normals[:, i]) for i in range(3)),
            )
        corners, tnormals = self._arrays
        slow = []
        for key, idx in by_group.items():
            grid = self.groups[key]
            hits = grid.nearest_many([items[i][0] for i in idx], 16)
            values = grid.values
            tri = np.array([values[n] for h in hits for _, n in h], dtype=np.int64)
            lens = np.array([len(h) for h in hits], dtype=np.int64)
            g = np.repeat(np.arange(len(idx)), lens)
            q = np.array([items[i][0] for i in idx], dtype=np.float64).reshape(-1, 3)[g]
            normals = [items[i][5] for i in idx]
            has = np.array([bool(n) for n in normals])[g]
            nrm = np.array([n if n else (0.0, 0.0, 0.0) for n in normals], dtype=np.float64).reshape(-1, 3)[g]
            d = _closest((q[:, 0], q[:, 1], q[:, 2]), *(c[tri] for c in corners))[0]
            dot = nrm[:, 0] * tnormals[0][tri] + nrm[:, 1] * tnormals[1][tri] + nrm[:, 2] * tnormals[2][tri]
            score = np.sqrt(d) + np.where(has, FACING_WEIGHT * (1 - dot), 0.0)
            first = np.cumsum(lens) - lens
            bad = np.bincount(g, weights=(~np.isfinite(score)).astype(np.float64), minlength=len(idx)) > 0
            low = np.minimum.reduceat(np.where(np.isfinite(score), score, 0.0), first)
            at = np.flatnonzero(score == low[g])
            _, firsts = np.unique(g[at], return_index=True)
            win = dict(zip(g[at[firsts]].tolist(), tri[at[firsts]].tolist(), strict=True))
            for m, i in enumerate(idx):
                if bad[m]:
                    slow.append(i)
                    continue
                n = win[m]
                t = self.tris[n]
                _, bary = closest_on_triangle(items[i][0], *t[0])
                uvs = t[1]
                out[i] = (
                    (sum(bary[k] * uvs[k][0] for k in range(3)), sum(bary[k] * uvs[k][1] for k in range(3))),
                    self.chart[n],
                )
        for i in slow:
            out[i] = self.lookup(*items[i])
        return out


def s9_pair(vb, index: int) -> tuple[int, int]:
    """Semantic 9 bytes 0, 1 (panel transform, panel data) of a vertex; (0, 0),
    the chassis, for buffers without semantic 9 (some vanilla interiors, e.g.
    173 and 184, have 32-byte buffers without it)."""
    e9 = vb.element(9)
    if e9 is None:
        return (0, 0)
    s9 = vb.read(e9, index)
    return (s9[0], s9[1])


def stored_normal(buf, at: int):
    """Unit normal from semantic 5 bytes 0, 1 at `at` (as the shader decodes them)."""
    a0, a1 = _angles_of(buf[at], buf[at + 1])
    return _normal_of(a0, a1)


def local_materials(stream, vehc) -> set:
    """Material UIDs an entry's own DRSP defines (the rest are SSIC shared ones)."""
    return {
        payload(stream, child(e, "MAHE"))[:8] for e in child(child(vehc, "MDXN"), "DRSP").children if e.tag == "MACO"
    }


# --- the host's panels ------------------------------------------------------------


@dataclass
class Panel:
    pair: tuple[int, int]  # s9 bytes 0, 1
    records: list  # upvh.Record(s) with this pair (normally one)
    part: int | None  # full-entry MODL part of the record
    role: int

    @property
    def record(self):
        return self.records[0]


@dataclass
class HostField:
    """Vanilla vertices of one entry's part 0: position, s12 - position, panel pair."""

    points: list
    disp: list
    pairs: list
    box: Box
    by_pair: dict = field(default_factory=dict)  # pair -> Grid (normalised coordinates)
    all_grid: Grid | None = None
    uv1: Uv1Field | None = None  # decal-atlas UVs (32-byte vertices)
    local: set = field(default_factory=set)  # material UIDs the entry defines itself
    tris: list = field(default_factory=list)  # (normalised corners, pair, unit face normal, paint), part 0

    def normalise(self, p):
        c, s = self.box.centre, self.box.size
        return tuple((p[a] - c[a]) / (s[a] / 2) for a in range(3))


def read_part0(stream, vehc, mask: int, mesh: vm.Mesh | None = None) -> list[tuple]:
    """(position, s12 - position, (s9 byte 0, byte 1)) for every vertex in the
    entry's MODL part 0 at `mask` that has semantic 12 (`mesh`: the entry's,
    if already loaded)."""
    mesh = load_mesh(stream, vehc) if mesh is None else mesh
    seen = {}
    for lod in mesh.parts[0].lods:
        if lod.mask != mask:
            continue
        for n in lod.submodels:
            for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                e12 = vb.element(12)
                if e12 is None:
                    continue
                for i in tri:
                    if (vb.uid, i) not in seen:
                        p = vm.position(vb, i)
                        q = vb.read(e12, i)
                        s9 = s9_pair(vb, i)
                        seen[(vb.uid, i)] = (p, (q[0] - p[0], q[1] - p[1], q[2] - p[2]), (s9[0], s9[1]))
    return list(seen.values())


def host_field(stream, vehc, mask: int) -> HostField:
    mesh = load_mesh(stream, vehc)
    rows = read_part0(stream, vehc, mask, mesh)
    if not rows:
        raise ValueError(f"{vehc.name}: no semantic-12 vertices in MODL part 0 mask {mask}")
    f = HostField([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], Box.of(r[0] for r in rows))
    groups = defaultdict(list)
    for n, pair in enumerate(f.pairs):
        groups[pair].append(n)
    for pair, ns in groups.items():
        f.by_pair[pair] = Grid([f.normalise(f.points[n]) for n in ns], ns)
    f.all_grid = Grid([f.normalise(p) for p in f.points], list(range(len(f.points))))
    for lod in mesh.parts[0].lods:
        if lod.mask != mask:
            continue
        for n in lod.submodels:
            for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                if vb.element(12) is None:
                    continue
                pts = [vm.position(vb, i) for i in tri]
                normal = _norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])))
                if normal is not None:
                    f.tris.append(
                        (
                            tuple(f.normalise(p) for p in pts),
                            s9_pair(vb, tri[0]),
                            normal,
                            mesh.submodels[n].material in PAINT_MATERIALS,
                        )
                    )
    return f


def host_panels(upvh_data: bytes, modl_data: bytes) -> dict[tuple, Panel]:
    """Panel pair -> Panel, from the records that carry a +0x2A index. The
    chassis is (0, 0) (role 38); records without a +0x34 slot use transform 0."""
    h = upvh_mod.parse(upvh_data)
    parts = upvh_mod.modl_part_uids(modl_data)
    out: dict[tuple, Panel] = {}
    for r in h.records:
        if r.model < 0 or r.type != 1:
            continue
        pair = (max(r.slot, 0) & 0xFF, r.model & 0xFF)
        part = parts.index(r.uid) if r.uid in parts else None
        if pair in out:
            out[pair].records.append(r)
        else:
            out[pair] = Panel(pair, [r], part, r.role)
    return out


# --- section rules ----------------------------------------------------------------


def section_rule(name: str) -> tuple[str, tuple]:
    """How a Forza main-carbin section is put on host panels:
    ("fixed", roles) the first role the host has; ("side", roles) the record of
    those roles on the section's side; ("glass", (front|rear)); ("majority",
    roles) the panel most of its triangles are nearest to; ("nearest", roles)
    per triangle."""
    n = name.lower()
    plate = re.fullmatch(r"(.*)_plate_(front|rear)", n)
    if plate:
        # Licence plates (src/dsfforza/convert/plates.py): the panel of the section they sit
        # on (bumper, boot lid), whole: one panel per plate, like vanilla. On a
        # plain body section: at the front the bumper (vanilla: 121 of 131 front
        # plates), at the rear the nearest of rear bumper, boot lid, chassis
        # (vanilla: 73, 34, 32 of 141).
        kind, roles = section_rule(plate.group(1))
        if kind == "nearest":
            if plate.group(2) == "front":
                return "fixed", (35, 37, 38)
            return "majority", (36, 5, 38)
        return kind, roles
    if n == "steering_wheel":
        return "fixed", (1,)
    # The bumper frames sit right behind the bumper skins. On the chassis or
    # front structure they crushed less than the skin and poked through it
    # (the M5's rear frame showed as a white chevron on the damaged bumper,
    # run 20261022-062852), so they go with their bumper, as vanilla bumper
    # parts carry their own trim.
    if n.startswith("bumperframef"):
        return "fixed", (35, 37, 38)
    if n.startswith("bumperframer"):
        return "fixed", (36, 38)
    if n.startswith("bumperf"):
        return "fixed", (35, 37, 38)
    if n.startswith("bumperr"):
        return "fixed", (36, 38)
    if n.startswith("hood"):
        return "fixed", (4, 37, 38)
    if n.startswith("trunk"):
        return "fixed", (5, 124, 38)
    if n.startswith("headlight") or n in ("glasslhl", "glassrhl"):
        return "side", (50, 51)
    if n.startswith("mirror"):
        return "side", (40, 41)
    if n.startswith("exhaust"):
        return "side", (136, 15)
    if n == "glassf":
        return "glass", ("front",)
    if n == "glassr":
        return "glass", ("rear",)
    if n.startswith("taillight") or n in ("glassltl", "glassrtl"):
        return "majority", (5, 38, 36)
    if n.startswith(("glass", "window")):
        return "majority", DOORS + (38,)  # side windows: their door, or fixed glass
    if n.startswith(("wing", "spoiler")):
        return "majority", (5, 124, 38)
    if n in ("interior", "cagerace") or n.startswith(("seat", "undercarriage")):
        return "fixed", (38,)
    return "nearest", BODY


def mirror_key(name: str) -> str:
    """Section name with its side letter blanked: taillightL/R -> taillight?,
    glassLTL/glassRTL -> glass?TL."""
    return re.sub(r"(?<=[a-z])[LR](?=[A-Z]|$)", "?", name)


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
    from pathlib import Path

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


# --- Forza parts along real shut lines ------------------------------------------------
#
# Forza's `body` section is one surface, but its mesh is cut along the real shut
# lines: the vertices at a door's or wing's edge are not shared with the panel
# next to it (the M5's front and rear doors, wings and rear quarters are each a
# component of triangles joined through shared vertex indices; the S5S Raptor's
# door too, while its wings, sills and rear quarters are one shell). The panel
# vote follows the host's layout, so a door patch spilled over those edges and
# had to be held shut (DamagePlan.bound). A door or wing whose triangles are
# mostly whole components that voted for it takes exactly those components: its
# border is then the Forza shut line, and it can swing like vanilla.

SWINGS = DOORS + (33, 34)  # roles that swing on vanilla cars (doors, front wings)
PART_SHARE = 0.6  # share of a component's area its panel vote needs to take it whole
PART_MIN_AREA = 0.15  # m2 of whole components a door or wing needs to be a Forza part


def part_components(keys: list) -> list[int]:
    """Component id per triangle: triangles joined through shared vertex keys."""
    parent: dict = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for t, ks in enumerate(keys):
        for k in ks:
            a, b = find(("t", t)), find(k)
            if a != b:
                parent[a] = b
    roots = [find(("t", t)) for t in range(len(keys))]
    ids = {}
    return [ids.setdefault(r, len(ids)) for r in roots]


def _component_votes(labels, areas, comps) -> dict:
    votes = defaultdict(Counter)
    for lab, area, c in zip(labels, areas, comps):
        votes[c][lab] += area
    return votes


def part_stats(labels, areas, comps, swings) -> tuple[Counter, Counter]:
    """Per swinging panel: area of whole components it wins with PART_SHARE,
    and area labelled with it."""
    clean, total = Counter(), Counter()
    for lab, area in zip(labels, areas):
        if lab in swings:
            total[lab] += area
    for votes in _component_votes(labels, areas, comps).values():
        win, w = votes.most_common(1)[0]
        if win in swings and w >= PART_SHARE * sum(votes.values()):
            clean[win] += sum(votes.values())
    return clean, total


def part_relabel(labels, areas, comps, parts: set, fallback) -> list:
    """Panels in `parts` keep exactly the components they win with PART_SHARE;
    their other triangles take the best other panel of their component (or
    `fallback`), and a component won by another panel takes that one whole."""
    if not parts:
        return list(labels)
    votes = _component_votes(labels, areas, comps)
    whole, other = {}, {}
    for c, v in votes.items():
        win, w = v.most_common(1)[0]
        if w >= PART_SHARE * sum(v.values()) and (win in parts or any(lab in parts for lab in v)):
            whole[c] = win
        else:
            others = [lab for lab, _ in v.most_common() if lab not in parts]
            other[c] = others[0] if others else fallback
    return [whole[c] if c in whole else other[c] if lab in parts else lab for lab, c in zip(labels, comps)]


def tear_length(labels, keys, swings) -> float:
    """Length (m) of edges inside one Forza surface (two triangles sharing both
    vertices of the edge) whose two sides are on different panels, one of them
    in `swings`: where a swinging door or wing would tear the surface open.
    keys: per triangle (vertex keys, position keys at 1/2000 m)."""
    owners = defaultdict(list)
    for t, (vk, pk) in enumerate(keys):
        for a, b in ((0, 1), (1, 2), (2, 0)):
            e = (min(vk[a], vk[b]), max(vk[a], vk[b]))
            owners[e].append((t, pk[a], pk[b]))
    total = 0.0
    for ts in owners.values():
        if len(ts) != 2:
            continue
        (t, p, q), (u, _, _) = ts
        if labels[t] != labels[u] and (labels[t] in swings or labels[u] in swings):
            total += math.dist(p, q) / 2000
    return total


PART_MARGIN = 0.05  # m; along z, beyond a part's largest component, that still belongs to it


def part_spans(items, parts: set) -> dict:
    """Part -> (z min, z max, side): the length and side (sign of x) of its
    largest component. items: (tris (sub id, index, centroid), areas,
    components, labels) per section."""
    best = {}
    for n, (_tris, areas, comps, labels) in enumerate(items):
        size = Counter()
        for lab, area, c in zip(labels, areas, comps):
            if lab in parts:
                size[(lab, n, c)] += area
        for (lab, _, c), area in size.items():
            if area > best.get(lab, (0, None))[0]:
                best[lab] = (area, (n, c))
    span = {}
    for lab, (_, (n, c)) in best.items():
        tris, _, comps, _ = items[n]
        pts = [t[2] for t, cc in zip(tris, comps) if cc == c]
        side = 1.0 if sum(p[0] for p in pts) >= 0 else -1.0
        span[lab] = (
            min(p[2] for p in pts),
            max(p[2] for p in pts),
            side,
            min(abs(p[0]) for p in pts),
            min(p[1] for p in pts),
            max(p[1] for p in pts),
        )
    return span


def within_span(c, span) -> bool:
    return span[0] - PART_MARGIN <= c[2] <= span[1] + PART_MARGIN and c[0] * span[2] > 0


CARD_DEPTH = 0.2  # m; inward of a door's skin that its door card (Forza `interior` triangles) may sit


def in_door_card(c, normal, span) -> bool:
    """Is an interior triangle (centroid c) the door card of the door with this
    span: within its length and height, on its side, at most CARD_DEPTH inward
    of its skin, facing into the cabin."""
    return (
        span[0] <= c[2] <= span[1]
        and c[0] * span[2] > 0
        and abs(c[0]) >= span[3] - CARD_DEPTH
        and span[4] - PART_MARGIN <= c[1] <= span[5] + PART_MARGIN
        and normal is not None
        and normal[0] * span[2] < -0.3
    )


def part_trim(tris, areas, comps, labels, span: dict, fallback) -> list:
    """Components on a part that lie mostly (by area) outside its span go to `fallback`."""
    inside, total = Counter(), Counter()
    for t, lab, area, c in zip(tris, labels, areas, comps):
        if lab in span:
            total[(lab, c)] += area
            inside[(lab, c)] += area if within_span(t[2], span[lab]) else 0.0
    drop = {k for k in total if inside[k] < PART_SHARE * total[k]}
    return [fallback if (lab, c) in drop else lab for lab, c in zip(labels, comps)]


# --- the plan for one car ---------------------------------------------------------


def _set_idle_priority() -> None:
    if sys.platform == "win32":
        import ctypes

        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), 0x40)  # IDLE_PRIORITY_CLASS


@dataclass
class DamageOptions:
    hull: bool = False  # also scale the root collision hull (PDL) to the Forza body
    k: int = K_NEAREST


class DamagePlan:
    """Everything the damage conversion needs from the host, read before the
    converter replaces the host's geometry."""

    def __init__(
        self,
        stream,
        full,
        exterior,
        car,
        place,
        opts: DamageOptions | None = None,
        log=print,
        interior=None,
        engine: str | None = None,
    ):
        from dsfforza.convert import forza2dsf

        self.f2d = forza2dsf
        self.opts = opts or DamageOptions()
        self.place = place
        self.car = car
        self.log = log
        nonr = child(full, "NONR")
        self.upvh = payload(stream, child(nonr, "UPVH"))
        self.panels = host_panels(self.upvh, payload(stream, child(nonr, "MODL")))
        self.full_field = host_field(stream, full, 4)
        self.ext_field = host_field(stream, exterior, 2) if exterior is not None else self.full_field
        self.chassis = (0, 0) if (0, 0) in self.panels else min(self.panels)
        for f, vehc, mask in ((self.full_field, full, 4), (self.ext_field, exterior, 2)):
            if vehc is not None:
                f.local = local_materials(stream, vehc)
                f.uv1 = Uv1Field.read(stream, vehc, mask, f.local)
        self.int_uv1 = None
        if interior is not None:
            self.int_local = local_materials(stream, interior)
            self.int_uv1 = Uv1Field.read(stream, interior, 64, self.int_local)
        # The Forza car's box (placed), over its finest body LOD.
        lods = sorted({sub.lod for s in car.body for sub in s.subsections if sub.lod > 0})
        pts = [
            place.point(v.position)
            for s in car.body
            for sub in s.subsections
            if sub.lod == lods[0]
            for t in sub.triangles()
            for v in (s.vertices_for(sub)[i] for i in t)
        ]
        self.forza_box = Box.of(pts)
        # Projection boxes for exposed inner geometry (split_exposed, _expose).
        self.exposed_boxes: dict[str, Box] = {}
        for name in ("hood", "trunk"):
            box = Box.of(
                place.point(v.position)
                for s in car.body
                if s.name.lower().startswith(name)
                for sub in s.subsections
                if sub.lod == lods[0]
                for t in sub.triangles()
                for v in (s.vertices_for(sub)[i] for i in t)
            )
            if box is not None:
                self.exposed_boxes[name] = box
        # Zones (exposed_zones): under a cover, its box deepened by BAY_DEPTH or
        # BOOT_DEPTH; behind a bumper, its box deepened inward by BUMPER_DEPTH.
        for kind, is_panel, axis, sign, _, reach in exposed_zones(engine):
            box = Box.of(
                place.point(v.position)
                for s in car.body
                if is_panel(s.name.lower())
                for sub in s.subsections
                if sub.lod == lods[0]
                for t in sub.triangles()
                for v in (s.vertices_for(sub)[i] for i in t)
            )
            if box is None:
                continue
            lo, hi = list(box.lo), list(box.hi)
            if kind in ("bay", "boot"):
                lo[1] -= BAY_DEPTH if kind == "bay" else BOOT_DEPTH
            elif sign > 0:
                hi[axis] = lo[axis] + reach
            else:
                lo[axis] = hi[axis] - reach
            self.exposed_boxes[kind] = Box(tuple(lo), tuple(hi))
        self._assign: dict[tuple, dict] = {}
        self._members: dict[tuple, dict] = {}
        self._grids: dict[tuple, Grid] = {}
        self.boxes: dict[tuple, list] = defaultdict(list)  # pair -> placed points (finest LOD built)
        self._boxes_lod: int | None = None
        self._seam: dict = {}  # quantised position -> rigid panels sharing it (current LOD)
        self._seam_written: set = set()  # (field, position) given a shared crushed position
        self._seam_rep: dict = {}  # quantised position -> its near-coincident group's position
        self._seam_paint: set = set()  # quantised positions on body paint (the outer skin)
        self._pins: dict = {}  # pair -> Grid of its pinned seam positions (current LOD)
        self._motion: dict = {}  # id(HostField) -> {frozenset of roles: median host border crush}
        self.swinging: set = set()  # doors/wings that are Forza parts (assignment): own transform slot
        self.report: dict = {"panels": {}}

    # -- panel lookup --
    def panels_with(self, roles) -> list[Panel]:
        return [p for p in self.panels.values() if p.role in roles]

    def _partner(self, pair) -> tuple | None:
        """The other side's panel of a sided role (mirror +x for mirror -x)."""
        p = self.panels.get(pair)
        role = SIDE_PARTNER.get(p.role) if p is not None else None
        cands = sorted(q.pair for q in self.panels_with((role,))) if role is not None else []
        return cands[0] if cands else None

    def _source(self, f: HostField, pair) -> tuple[tuple | None, bool]:
        """(the panel whose host vertices in `f` stand for `pair`, mirrored in x).
        A sided panel the host has a record for but no vertices in this entry's
        part 0 (278's full entry has no +x mirror; its exterior has) borrows its
        partner's, mirrored."""
        if pair in f.by_pair:
            return pair, False
        partner = self._partner(pair)
        if partner is not None and partner in f.by_pair:
            return partner, True
        return None, False

    def _host_box(self, pair, f: HostField | None = None) -> Box | None:
        f = f or self.full_field
        src, flip = self._source(f, pair)
        if src is None:
            return None
        box = Box.of(f.points[n] for n in f.by_pair[src].values)
        return Box((-box.hi[0], box.lo[1], box.lo[2]), (-box.lo[0], box.hi[1], box.hi[2])) if flip else box

    def _forza_norm(self, p):
        c, s = self.forza_box.centre, self.forza_box.size
        return tuple((p[a] - c[a]) / (s[a] / 2) for a in range(3))

    def _nearest_pair(self, p_dsf, allowed: set, f: HostField, normal=None, paint: bool = False) -> tuple | None:
        """The panel of the host surface nearest to a placed point: a vote of
        the 3 host triangles (of the allowed panels) closest to it in
        normalised coordinates, by surface distance plus PANEL_FACING per unit
        of normal mismatch. Host triangles, not vertices: the host's door skins
        are large triangles with vertices only at their edges, so a point
        mid-door was nearer to vertices of the chassis behind it (door jambs,
        cabin), and the vertex vote put chassis islands into the M5's door
        skins, which tore open when crushed (testdrive3). A paint triangle
        votes among the host's paint triangles first: the outer skins are
        shared paint, while the structure a few centimetres under them (the
        front structure under the M5's wings) is trim or the host's own
        chassis texture."""
        return self._nearest_pairs([p_dsf], allowed, f, [normal], [paint])[0]

    def _nearest_pairs(self, points, allowed: set, f: HostField, normals, paints) -> list:
        """_nearest_pair for many points at once (_votes)."""
        out = [None] * len(points)
        todo = list(range(len(points)))
        ask = [i for i in todo if paints[i]]
        if ask:
            index = self._tri_index(f, allowed, paint=True)
            got = self._votes(index, f, [points[i] for i in ask], [normals[i] for i in ask], PAINT_REACH)
            for i, pair in zip(ask, got, strict=True):
                out[i] = pair
            todo = [i for i in todo if out[i] is None]
        if todo:
            got = self._votes(self._tri_index(f, allowed), f, [points[i] for i in todo], [normals[i] for i in todo])
            for i, pair in zip(todo, got, strict=True):
                out[i] = pair
        return out

    def _votes(self, index, f: HostField, points, normals, reach: float | None = None) -> list:
        """_vote for many points at once, with the same result for each.

        _vote's result depends only on the scores of the triangles it meets and on
        the rings it meets them in. A triangle binned into the cells of a box meets
        the rings dmin..dmax around a point's cell (the Chebyshev distances of a
        box's cells from a cell are contiguous), so by ring r _vote has scored it
        max(0, min(dmax, r) - dmin + 1) times. The third best is within r * cell
        exactly when that many scores, counted so, are within r * cell; and then the
        three best are among them. So per point: score the triangles with dmin <=
        rmax all at once (_closest, the same floats as closest_on_triangle), find the
        first ring r <= rmax that stops by counting, and sort only the scores within
        r * cell by (score, panel), as _vote's tuples sort. rmax runs through
        VOTE_RINGS, each round with the points the last one didn't stop; a point
        that never stops (its result is all of rings 0..7), or that has a
        non-finite score (Python's sort and numpy's differ on NaN), goes to _vote.
        The candidates of a point's cell are found per block of cells (VOTE_BLOCK)
        and scored VOTE_CHUNK at a time."""
        out: list = [None] * len(points)
        if index is None or not points:
            return out
        cell, _, _, _, tri_ids, lo, hi, rank, pairs = index
        q = np.array([self._forza_norm(p) for p in points], dtype=np.float64).reshape(-1, 3)
        has = np.array([bool(n) for n in normals])
        nrm = np.array([n if n else (0.0, 0.0, 0.0) for n in normals], dtype=np.float64).reshape(-1, 3)
        qkey = np.floor(q / cell).astype(np.int64)
        corners, tnormals = _tri_arrays(f)
        corners = tuple(c[tri_ids] for c in corners)
        tnormals = tuple(c[tri_ids] for c in tnormals)
        limit = np.array([r * cell for r in range(8)])  # _vote's r * cell
        todo, slow = np.arange(len(points)), []
        for rmax in VOTE_RINGS:
            undecided = []
            # The points' cells, sorted by block of VOTE_BLOCK^3 cells. Per block, the
            # (cell, triangle) pairs with the triangle's box within rmax rings of the cell.
            ukeys, inv = np.unique(np.hstack((qkey[todo] // VOTE_BLOCK, qkey[todo])), axis=0, return_inverse=True)
            inv = inv.reshape(-1)
            bounds = np.flatnonzero((ukeys[1:, :3] != ukeys[:-1, :3]).any(axis=1)) + 1
            ukeys = ukeys[:, 3:]
            found = []
            for k0, k1 in itertools.pairwise([0, *bounds.tolist(), len(ukeys)]):
                kk = ukeys[k0:k1]
                near = np.flatnonzero(((lo <= kk.max(axis=0) + rmax) & (hi >= kk.min(axis=0) - rmax)).all(axis=1))
                d_lo = lo[near][None, :, :] - kk[:, None, :]
                d_hi = kk[:, None, :] - hi[near][None, :, :]
                dmin = np.maximum(np.maximum(d_lo, d_hi), 0).max(axis=2)
                kp, tp = np.nonzero(dmin <= rmax)  # grouped by cell
                dmax = np.maximum(np.abs(d_lo[kp, tp]), np.abs(d_hi[kp, tp])).max(axis=1)
                found.append((kp + k0, near[tp], dmin[kp, tp], dmax))
            kp, ct, cdlo, cdhi = (np.concatenate(a) for a in zip(*found, strict=True))
            cnt = np.bincount(kp, minlength=len(ukeys))
            kstart = np.cumsum(cnt) - cnt
            # The points by cell, in chunks of about VOTE_CHUNK candidates.
            rows = np.argsort(inv, kind="stable")
            lens = cnt[inv[rows]]
            ends = np.searchsorted(np.cumsum(lens), np.arange(VOTE_CHUNK, int(lens.sum()), VOTE_CHUNK))
            for rs in np.split(rows, np.unique(ends)):
                if not len(rs):
                    continue
                sel, lk = todo[rs], inv[rs]
                lens = cnt[lk]
                first = np.cumsum(lens) - lens
                g = np.repeat(np.arange(len(sel)), lens)  # each candidate's point (in sel)
                at = np.arange(int(lens.sum())) - np.repeat(first - kstart[lk], lens)
                t, dlo, dhi = ct[at], cdlo[at], cdhi[at]
                pt = sel[g]
                d = _closest((q[pt, 0], q[pt, 1], q[pt, 2]), *(c[t] for c in corners))[0]
                dot = nrm[pt, 0] * tnormals[0][t] + nrm[pt, 1] * tnormals[1][t] + nrm[pt, 2] * tnormals[2][t]
                score = np.sqrt(d) + np.where(has[pt], PANEL_FACING * (1 - dot), 0.0)
                bad = np.zeros(len(sel), dtype=bool)
                bad[g[~np.isfinite(score)]] = True
                # The ring loop's stop: at least 3 scores within r * cell, each counted
                # once per ring up to r that the triangle touches.
                stop = np.full(len(sel), -1)
                for r in range(rmax + 1):
                    mult = np.clip(np.minimum(dhi, r) - dlo + 1, 0, None) * (score <= limit[r])
                    stop[(stop < 0) & (np.bincount(g, weights=mult, minlength=len(sel)) >= 3)] = r
                (undecided if rmax < VOTE_RINGS[-1] else slow).append(sel[(stop < 0) & ~bad])
                slow.append(sel[bad])
                # The three best at the stop ring in (score, panel) order, each as often
                # as _vote has it, within reach.
                ring = stop[g]
                mult = np.clip(np.minimum(dhi, ring) - dlo + 1, 0, None)
                pick = np.flatnonzero((ring >= 0) & ~bad[g] & (mult > 0) & (score <= limit[ring]))
                pick = pick[np.lexsort((rank[t[pick]], score[pick], g[pick]))]
                gp, mp, sp, tp = g[pick], mult[pick], score[pick], t[pick]
                acc = np.cumsum(mp)
                start = np.searchsorted(gp, gp)  # each point's first entry
                take = np.minimum(mp, 3 - (acc - mp - (acc[start] - mp[start])))
                keep = take > 0
                if reach is not None:
                    keep &= sp <= reach
                best: dict[int, list] = defaultdict(list)
                for j, s, n, c in zip(
                    gp[keep].tolist(), sp[keep].tolist(), tp[keep].tolist(), take[keep].tolist(), strict=True
                ):
                    best[j] += [(s, pairs[n])] * c
                for j, picks in best.items():
                    votes = Counter()
                    for s, pair in picks:
                        votes[pair] += 1.0 / (s + 0.005)
                    out[int(sel[j])] = votes.most_common(1)[0][0]
            todo = np.concatenate(undecided) if undecided else todo[:0]
            if not len(todo):
                break
        for i in np.concatenate(slow).tolist() if slow else ():
            out[i] = self._vote(index, f, points[i], normals[i], reach)
        return out

    def _vote(self, index, f: HostField, p_dsf, normal, reach: float | None = None) -> tuple | None:
        """The panel of the host triangles nearest to p_dsf (normalised distance plus a
        facing penalty), its three best scores weighted: the triangles of the grid
        cells ring by ring around p's cell (a triangle once per ring it touches), up
        to the first ring at which the third best is within reach of the ring."""
        if index is None:
            return None
        cell, cells, keys, members = index[:4]
        q = self._forza_norm(p_dsf)
        key = tuple(int(math.floor(q[a] / cell)) for a in range(3))
        order = at = None
        scores: dict[int, tuple] = {}  # triangle -> (score, pair)
        scored = []
        for r in range(8):
            seen = set()
            if r < 3:  # the cells of the ring's shell
                for dx, dy, dz in _SHELLS[r]:
                    seen.update(cells.get((key[0] + dx, key[1] + dy, key[2] + dz), ()))
            else:  # farther out: the occupied cells by ring (Chebyshev distance), order[at[r]:at[r + 1]]
                if order is None:
                    ring = np.abs(keys - np.array(key, dtype=np.int64)).max(axis=1)
                    near = np.flatnonzero((ring >= 3) & (ring < 8))
                    near = near[ring[near].argsort()]
                    at = (np.searchsorted(ring[near], np.arange(9))).tolist()
                    order = near.tolist()
                for i in order[at[r] : at[r + 1]]:
                    seen.update(members[i])
            new = [n for n in seen if n not in scores]
            if new:
                self._score_triangles(f, q, normal, new, scores)
            scored += (scores[n] for n in seen)
            # Cells within r of q's cell cover every triangle closer than r * cell.
            if len(scored) >= 3 and sorted(scored)[2][0] <= r * cell:
                break
        if reach is not None:
            scored = [sc for sc in scored if sc[0] <= reach]
        if not scored:
            return None
        votes = Counter()
        for score, pair in sorted(scored)[:3]:
            votes[pair] += 1.0 / (score + 0.005)
        return votes.most_common(1)[0][0]

    @staticmethod
    def _score_triangles(f: HostField, q, normal, ns: list[int], out: dict) -> None:
        """out[n] = (distance from q to triangle n + facing penalty, its pair); a few
        triangles one by one, more at once (closest_on_triangles, the same floats)."""
        if len(ns) < 24:
            for n in ns:
                t = f.tris[n]
                d, _ = closest_on_triangle(q, *t[0])
                out[n] = (math.sqrt(d) + (PANEL_FACING * (1 - _dot(normal, t[2])) if normal else 0.0), t[1])
            return
        corners, normals = _tri_arrays(f)
        index = np.array(ns, dtype=np.int64)
        d = _closest(q, *(c[index] for c in corners))[0]
        score = np.sqrt(d)
        if normal:
            nx, ny, nz = (c[index] for c in normals)
            score = score + PANEL_FACING * (1 - (normal[0] * nx + normal[1] * ny + normal[2] * nz))
        else:
            score = score + 0.0
        for n, value in zip(ns, score.tolist(), strict=True):
            out[n] = (value, f.tris[n][1])

    def _tri_index(self, f: HostField, allowed: set, paint: bool = False):
        """Host part-0 triangles of the allowed panels (paint: only the shared
        paint ones), binned by every grid cell their (normalised) bounding box
        touches."""
        key = (id(f), frozenset(allowed), "tris", paint)
        if key not in self._grids:
            cell = 0.08
            cells = defaultdict(list)
            ids, boxes = [], []
            for n, (pts, pair, _, is_paint) in enumerate(f.tris):
                if pair not in allowed or (paint and not is_paint):
                    continue
                lo = [int(math.floor(min(p[a] for p in pts) / cell)) for a in range(3)]
                hi = [int(math.floor(max(p[a] for p in pts) / cell)) for a in range(3)]
                ids.append(n)
                boxes.append(lo + hi)
                for i in range(lo[0], hi[0] + 1):
                    for j in range(lo[1], hi[1] + 1):
                        for k in range(lo[2], hi[2] + 1):
                            cells[(i, j, k)].append(n)
            # (cell, cells, the occupied cells' keys as an array, their triangle lists) for
            # _vote; for _votes the triangles (f.tris indices), their cell boxes (lo, hi),
            # the rank of their panel among the index's panels, and their panels.
            cells = dict(cells)
            boxes = np.array(boxes, dtype=np.int64).reshape(-1, 6)
            pairs = [f.tris[n][1] for n in ids]
            rank = {p: i for i, p in enumerate(sorted(set(pairs)))}
            self._grids[key] = (
                (
                    cell,
                    cells,
                    np.array(list(cells), dtype=np.int64).reshape(-1, 3),
                    list(cells.values()),
                    np.array(ids, dtype=np.int64),
                    boxes[:, :3],
                    boxes[:, 3:],
                    np.array([rank[p] for p in pairs], dtype=np.int64),
                    pairs,
                )
                if cells
                else None
            )
        return self._grids[key]

    def _side_panel(self, roles, centroid) -> tuple | None:
        """The record of `roles` on the section's side (or a centred one: a
        twin exhaust); None (the chassis) if the host has none there. A part
        never goes on the other side's record: both mirrors on one panel came
        off together, as one body centred between them (testdrive9: the S2000's
        mirror hung in the air, host 278 has no +x mirror in its full entry)."""
        cands = [p for p in self.panels_with(roles) if self._host_box(p.pair) is not None]
        if not cands:
            return None
        if abs(centroid[0]) > 0.1 * self.forza_box.size[0] / 2:
            reach = 0.1 * self.full_field.box.size[0] / 2

            def fits(p):
                box = self._host_box(p.pair)
                return (box.lo[0] < -reach and box.hi[0] > reach) or (box.centre[0] > 0) == (centroid[0] > 0)

            cands = [p for p in cands if fits(p)]
            if not cands:
                return None
        q = self._forza_norm(centroid)
        return min(cands, key=lambda p: math.dist(q, self.full_field.normalise(self._host_box(p.pair).centre))).pair

    def _glass_panel(self, which: str) -> tuple | None:
        """The host's centred glass panel (role 8) in the front (z > 0) or rear
        half: windscreen or rear window. None if the host has none there (the
        vanilla 370Z's rear window is part of its hatch)."""
        cands = [
            p
            for p in self.panels_with((8,))
            if p.pair in self.full_field.by_pair
            and abs(self._host_box(p.pair).centre[0]) < 0.3
            and (self._host_box(p.pair).centre[2] > 0) == (which == "front")
        ]
        if not cands:
            return None
        return (max if which == "front" else min)(cands, key=lambda p: self._host_box(p.pair).centre[2]).pair

    def _first(self, roles) -> tuple | None:
        for role in roles:
            for p in self.panels_with((role,)):
                if p.pair in self.full_field.by_pair or role == 1:
                    return p.pair
        return None

    def assignment(self, sections, lod: int) -> dict:
        """{(id(sub), triangle index): panel pair} for every body triangle at `lod`."""
        from dsfforza.convert import forza_materials as fm

        key = (id(sections), lod)
        if key in self._assign:
            return self._assign[key]
        f = self.full_field
        finest = min(sub.lod for s in sections for sub in s.subsections if sub.lod > 0)
        if lod != finest and (id(sections), finest) not in self._assign:
            self.assignment(sections, finest)  # decides which doors and wings are Forza parts
        out = {}
        per_section = {}
        majority: dict[str, list] = {}
        pending = []  # "nearest" sections: (name, tris, areas, components, labels)
        door_groups = set()  # majority groups of side windows
        keys_of = {}  # "nearest" section -> per triangle (vertex keys, position keys)
        cabin = []  # the `interior` section: (name, tris, normals), door cards
        for section in sections:
            kind, roles = section_rule(section.name)
            tris, shape = [], []  # (sub id, index, centroid); (unit normal, area, corner keys, paint)
            for sub in section.subsections:
                if sub.lod != lod:
                    continue
                pool = section.vertices_for(sub)
                is_paint = fm.classify(sub.material)[0] == fm.PAINT
                for k, t in enumerate(sub.triangles()):
                    pts = [self.place.point(pool[i].position) for i in t]
                    tris.append((id(sub), k, tuple(sum(p[a] for p in pts) / 3 for a in range(3))))
                    cross = _cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0]))
                    shape.append(
                        (
                            _norm(cross),
                            math.sqrt(_dot(cross, cross)) / 2,
                            [tuple(round(c * 2000) for c in p) for p in pts],
                            is_paint,
                            [(id(pool), i) for i in t],
                        )
                    )
            if not tris:
                continue
            centroid = tuple(sum(t[2][a] for t in tris) / len(tris) for a in range(3))
            pair = None
            if kind == "fixed":
                pair = self._first(roles)
            elif kind == "side":
                pair = self._side_panel(roles, centroid)
            elif kind == "glass":
                pair = self._glass_panel(roles[0])
            if kind == "glass" and pair is None:
                kind, roles = "majority", ((4, 37, 38) if roles[0] == "front" else (124, 5, 38))
            off = 0.1 * self.forza_box.size[0] / 2
            if kind == "side" and min(t[2][0] for t in tris) < -off and max(t[2][0] for t in tris) > off:
                # One section with parts on both sides (a twin exhaust, both
                # headlights in one mesh): each side's triangles take that
                # side's record, so they come off one at a time.
                halves = {s: [t for t in tris if (t[2][0] > 0) == s] for s in (True, False)}
                votes = Counter()
                for half in halves.values():
                    c = tuple(sum(t[2][a] for t in half) / len(half) for a in range(3))
                    p = self._side_panel(roles, c) or self.chassis
                    for sid, k, _ in half:
                        out[(sid, k)] = p
                    votes[p] += len(half)
                per_section[section.name] = votes
                continue
            if kind in ("fixed", "side", "glass"):
                pair = pair or self.chassis
                for sid, k, _ in tris:
                    out[(sid, k)] = pair
                per_section[section.name] = Counter({pair: len(tris)})
                if section.name.lower() == "interior":
                    cabin.append((section.name, tris, [s[0] for s in shape]))
                continue
            allowed = {p.pair for p in self.panels_with(roles)} & set(f.by_pair)
            allowed = allowed or {self.chassis}
            labels = [
                pair or self.chassis
                for pair in self._nearest_pairs(
                    [t[2] for t in tris], allowed, f, [s[0] for s in shape], [s[3] for s in shape]
                )
            ]
            if kind == "nearest":
                # Panels as connected patches of the Forza surface: a seam
                # between two panels opens when they crush differently, so
                # stray triangles and small islands join the panel around them.
                labels, merged = smooth_labels(labels, [s[1] for s in shape], [s[2] for s in shape])
                self.report.setdefault("islands_merged", {})[str(lod)] = (
                    self.report.get("islands_merged", {}).get(str(lod), 0) + merged
                )
                # Doors and wings become the Forza parts they are, once every
                # section is labelled (part_components, swinging_parts below).
                comps = part_components([s[4] for s in shape])
                pending.append((section.name, tris, [s[1] for s in shape], comps, labels))
                keys_of[section.name] = [(s[4], s[2]) for s in shape]
                continue
            votes = Counter(labels)
            picks = [(sid, k, pair, c, sh[0]) for (sid, k, c), pair, sh in zip(tris, labels, shape)]
            if kind == "majority":
                # Centred panels (boot, chassis, bumper): a left/right pair of
                # sections (taillightL/R) votes together, so both lamps land on
                # the same panel.
                group = mirror_key(section.name) if not set(roles) & set(DOORS) else section.name
                if set(roles) & set(DOORS):
                    door_groups.add(group)
                majority.setdefault(group, []).append((section.name, picks, votes))
                continue
            for sid, k, pair, _, _ in picks:
                out[(sid, k)] = pair
            per_section[section.name] = votes
        if pending:
            if lod == finest:
                swings = {p.pair for p in self.panels_with(SWINGS)}
                stats = Counter(), Counter()
                for _, _, areas, comps, labels in pending:
                    clean, total = part_stats(labels, areas, comps, swings)
                    stats[0].update(clean)
                    stats[1].update(total)
                found = {p for p in swings if stats[0][p] >= PART_MIN_AREA and stats[0][p] >= PART_SHARE * stats[1][p]}
                # Left and right alike: a door swings only if its partner on
                # the other side (if the host has one) is a Forza part too.
                self.swinging = {p for p in found if self._partner(p) is None or self._partner(p) in found}
                self.report["shut_line_parts"] = {
                    self.label(p): {
                        "part m2": round(stats[0][p], 3),
                        "labelled m2": round(stats[1][p], 3),
                        "swings": p in self.swinging,
                    }
                    for p in sorted(swings)
                    if stats[1][p]
                }
            relabelled = [
                part_relabel(labels, areas, comps, self.swinging, self.chassis)
                for _, _, areas, comps, labels in pending
            ]
            # A part's extent: its largest component. Components (and side
            # windows) that won the part but lie mostly outside its length (the
            # S5S Raptor's intake surround behind the door) stay on the body.
            span = part_spans([(t, a, c, lab) for (_, t, a, c, _), lab in zip(pending, relabelled)], self.swinging)
            swings = {p.pair for p in self.panels_with(SWINGS)}
            for (name, tris, areas, comps, before), labels in zip(pending, relabelled):
                labels = part_trim(tris, areas, comps, labels, span, self.chassis)
                tears = self.report.setdefault("door_tears_m", {}).setdefault(str(lod), [0.0, 0.0])
                tears[0] += tear_length(before, keys_of[name], swings)
                tears[1] += tear_length(labels, keys_of[name], self.swinging)
                for (sid, k, _), pair in zip(tris, labels):
                    out[(sid, k)] = pair
                per_section[name] = Counter(labels)
        else:
            span = {}
        # Door cards go with their swinging door, as vanilla door cards are
        # skinned to the door records (cockpit.md): an open door shows its
        # card, and no card is left standing in the opening.
        doors = [d for d in span if self.panels[d].role in DOORS]
        for name, tris, normals in cabin:
            for (sid, k, c), normal in zip(tris, normals):
                hit = [d for d in doors if in_door_card(c, normal, span[d])]
                if hit:
                    out[(sid, k)] = hit[0]
            per_section[name] = Counter(out[(sid, k)] for sid, k, _ in tris)
        for group, members_ in majority.items():
            total = sum((v for _, _, v in members_), Counter())
            pair = total.most_common(1)[0][0]
            # Side windows: the part of the glass within a swinging door's
            # length goes with that door (the S5S Raptor's door window and
            # quarter window are one Forza section), the rest stays put.
            doors = [d for d in span if self.panels[d].role in DOORS] if group in door_groups else []
            for name, picks, _ in members_:
                for sid, k, _, c, normal in picks:
                    side = normal is not None and abs(normal[0]) >= 0.5  # not a sunroof or T-top
                    inside = [d for d in doors if side and within_span(c, span[d])]
                    if inside:
                        out[(sid, k)] = inside[0]
                    elif pair in span and not (side and within_span(c, span[pair])):
                        out[(sid, k)] = self.chassis
                    else:
                        out[(sid, k)] = pair
                per_section[name] = Counter(out[(sid, k)] for sid, k, *_ in picks)
        self._assign[key] = out
        members: dict[tuple, set] = defaultdict(set)
        sub_owner = {id(sub): id(section) for section in sections for sub in section.subsections}
        for (sid, _), pair in out.items():
            members[pair].add(sub_owner[sid])
        self._members[key] = members
        self.report.setdefault("sections", {})[str(lod)] = {
            name: {self.label(p): n for p, n in votes.items()} for name, votes in per_section.items()
        }
        return out

    def label(self, pair) -> str:
        p = self.panels.get(pair)
        return f"{pair[0]},{pair[1]} {role_name(p.role) if p else '?'}"

    # -- geometry --
    def _geometry(self, builder, sections, lod, f: HostField, record_box: bool = False, **kwargs) -> dict[tuple, list]:
        """Per panel: add_geometry over that panel's triangles, then s9, s12 and
        normal deltas written into the new vertices."""
        assign = self.assignment(sections, lod)
        by_pair = self._members[(id(sections), lod)]
        kwargs.pop("glass_links", None)  # panels replace the glass links
        out = {}
        if record_box and self._boxes_lod is not None and lod > self._boxes_lod:
            record_box = False  # keep the boxes of the finest full-entry LOD
        if record_box and self._boxes_lod != lod:
            self.boxes.clear()
            self._boxes_lod = lod
        self._seam = self._seam_map(sections, lod, assign)
        self._pins = self._pin_map(f)
        kwargs["exposed"] = True  # split_exposed's subsections: UV0/sky from the host (_expose)
        for pair in sorted(by_pair):
            secs = [s for s in sections if id(s) in by_pair[pair]]
            # binds this pair now, not the loop variable
            keep = (lambda pr: lambda section, sub, k, tri: assign.get((id(sub), k)) == pr)(pair)  # noqa: PLC3002
            subs = self.f2d.add_geometry(builder, secs, lod, self.place, wheel=False, keep=keep, **kwargs)
            pts = self._finish(builder, subs, pair, f)
            if record_box:
                self.boxes[pair] += pts
            if subs:
                out[pair] = subs
        self.report["seam_positions"] = len(self._seam_written)
        return out

    def _rigid(self, pair) -> bool:
        """A panel that only crushes on a converted car (chassis, front
        structure, doors, wings, sills), so its seams with others can be kept
        shut; bound() keeps it from swinging."""
        p = self.panels.get(pair)
        return p is not None and p.role in BODY

    # Doors and wings swing on vanilla cars. Their hinge limit is 0, but
    # forceOpenPanels opens vanilla doors wide (run host-20261023-093807: 227,
    # 266) and the converted RX-7's and Mustang's door panels too (run
    # host-20261023-101158), and a crash knocks them ajar. A converted door or
    # wing is a patch of the one-piece Forza skin cut along the host's panel
    # layout, not a Forza shut line, with no jamb or door card behind it (the
    # door cards are on the chassis): swung, its border opens into a jagged
    # slit onto the hollow body (testdrive9: the S5S Raptor behind its door,
    # the 250 GTO across its nose, where both wings meet with no bumper
    # section between them). Pinned seams (_pin_map) only hold while the
    # panels share a transform. So every rigid panel's vertices take the
    # chassis' transform slot (s9 byte 0) and keep their own panel-data index
    # (byte 1): they still dent with their own damage amount, and never swing.
    # A door or wing that is a Forza part (self.swinging, assignment) has the
    # Forza shut line as its border and keeps its own slot: it swings as vanilla.

    def bound(self, pair) -> tuple:
        """s9 bytes 0, 1 written on a panel's vertices: (transform slot, panel
        data index). Rigid panels use the chassis' slot, unless they swing."""
        return (self.chassis[0], pair[1]) if self._rigid(pair) and pair not in self.swinging else pair

    def _seam_map(self, sections, lod: int, assign: dict) -> dict:
        """Quantised position (int16 x3, as encoded) -> the rigid panels whose
        triangles use it, for positions on more than one."""
        from dsfforza.convert import forza_materials as fm

        users = defaultdict(set)
        self._seam_paint = set()
        for section in sections:
            for sub in section.subsections:
                if sub.lod != lod:
                    continue
                pool = section.vertices_for(sub)
                paint = fm.classify(sub.material)[0] == fm.PAINT
                for k, t in enumerate(sub.triangles()):
                    pair = assign.get((id(sub), k))
                    if pair is None or not self._rigid(pair):
                        continue
                    for i in t:
                        p = self.place.point(pool[i].position)
                        key = tuple(max(-32768, min(32767, round(c * POSITION_UNITS))) for c in p)
                        users[key].add(pair)
                        if paint:
                            self._seam_paint.add(key)
        # Near-coincident positions of different panels (separate Forza shells
        # that touch) are seams too: such positions are grouped, and every
        # member takes the group's panels and its first position's crush.
        near = SEAM_NEAR * POSITION_UNITS
        cell = defaultdict(list)
        for key in users:
            cell[tuple(int(c // near) for c in key)].append(key)
        parent = {}

        def find(k):
            while parent.get(k, k) != k:
                parent[k] = parent.get(parent[k], parent[k])
                k = parent[k]
            return k

        for key, pairs in users.items():
            ck = tuple(int(c // near) for c in key)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for other in cell.get((ck[0] + dx, ck[1] + dy, ck[2] + dz), ()):
                            if other != key and users[other] != pairs and math.dist(key, other) <= near:
                                a, b = find(key), find(other)
                                if a != b:
                                    parent[max(a, b)] = min(a, b)
        groups = defaultdict(set)
        for key in list(parent):
            root = find(key)
            groups[root].update((key, root))
        self._seam_rep = {}
        for root, members in groups.items():
            pairs = set().union(*(users[k] for k in members))
            paint = any(k in self._seam_paint for k in members)
            for k in members:
                users[k] = pairs
                self._seam_rep[k] = root
                if paint:
                    self._seam_paint.add(k)
        return {k: tuple(sorted(v)) for k, v in users.items() if len(v) > 1}

    # Seams that stay shut whatever the damage amounts (testdrive7: the S5S
    # Raptor's door panel border ran through the rear quarter and the front
    # wing as a sawtooth of Forza triangle edges, and opened into jagged black
    # slits after a side hit). A seam vertex is drawn at p + f * d on each
    # side, f being that panel's damage amount; sharing d closes the seam only
    # when both panels get the same f, which fullVisualDamage does and a real
    # impact doesn't. Only d = 0 keeps it shut for any f. Vanilla cars do that:
    # the vertices where doors, wings, sills and the chassis meet barely move
    # (median 0-3 mm on 147, 163, 227), while their middles dent; where they
    # move (wings against the front structure, 0.2-0.3 m on 163), the seam is
    # under the bonnet and backed by structure. Forza skins have no backing,
    # so seams between rigid panels on body paint are always pinned (d = 0;
    # 163's wing/front-structure border sits on the S5S Raptor's arch flare),
    # trim seams where the host's border stays still too, and the crush
    # fades in over SEAM_REACH. Other trim seams keep the shared d.

    def _border_motion(self, f: HostField) -> dict:
        """{frozenset of the two roles: median crush (m) of the host's vertices
        where panels of those roles meet} over the host's rigid panels."""
        key = id(f)
        if key in self._motion:
            return self._motion[key]
        cells = defaultdict(list)
        rigid = [n for n, pr in enumerate(f.pairs) if self._rigid(pr)]
        for n in rigid:
            cells[tuple(int(math.floor(c / SEAM_BORDER)) for c in f.points[n])].append(n)
        moved = defaultdict(list)
        for n in rigid:
            ck = tuple(int(math.floor(c / SEAM_BORDER)) for c in f.points[n])
            best = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for m in cells.get((ck[0] + dx, ck[1] + dy, ck[2] + dz), ()):
                            if f.pairs[m] == f.pairs[n]:
                                continue
                            d = math.dist(f.points[m], f.points[n])
                            if d <= SEAM_BORDER and (best is None or d < best[0]):
                                best = (d, m)
            if best is not None:
                roles = frozenset((self.panels[f.pairs[n]].role, self.panels[f.pairs[best[1]]].role))
                moved[roles].append(math.sqrt(_dot(f.disp[n], f.disp[n])))
        out = {k: sorted(v)[len(v) // 2] for k, v in moved.items()}
        self._motion[key] = out
        self.report.setdefault(
            "host_border_motion", {" | ".join(sorted(role_name(r) for r in k)): round(v, 3) for k, v in out.items()}
        )
        return out

    def _pinned(self, a, b, motion: dict) -> bool:
        """Is the seam between rigid panels a and b pinned: the host's border
        of these two roles stays still, or the host has no such border."""
        m = motion.get(frozenset((self.panels[a].role, self.panels[b].role)))
        return m is None or m <= SEAM_STILL

    def _pin_map(self, f: HostField) -> dict:
        """Pair -> (cells of SEAM_REACH: placed points) of its pinned seam positions."""
        motion = self._border_motion(f)
        pins = defaultdict(lambda: defaultdict(list))
        count = 0
        for key, pairs in self._seam.items():
            if key not in self._seam_paint and not any(
                self._pinned(a, b, motion) for a in pairs for b in pairs if a < b
            ):
                continue
            p = tuple(c / POSITION_UNITS for c in key)
            ck = tuple(int(math.floor(c / SEAM_REACH)) for c in p)
            for pair in pairs:
                pins[pair][ck].append(p)
            count += 1
        self.report.setdefault("pinned_seam_positions", []).append(count)
        return pins

    def _pin_weight(self, pair, p) -> float:
        """0 on a pinned seam of `pair`, rising smoothly to 1 at SEAM_REACH from it."""
        cells = self._pins.get(pair)
        if not cells:
            return 1.0
        ck = tuple(int(math.floor(c / SEAM_REACH)) for c in p)
        best = SEAM_REACH
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for q in cells.get((ck[0] + dx, ck[1] + dy, ck[2] + dz), ()):
                        d = math.dist(p, q)
                        best = min(best, d)
        t = best / SEAM_REACH
        return t * t * (3 - 2 * t)

    def _disp(self, f: HostField, pair, p) -> tuple:
        """Crush displacement (s12 - position) at placed point p: the host
        panel's, inverse-distance weighted over its nearest vertices in
        normalised coordinates, scaled by the car size ratio."""
        cache = self.__dict__.setdefault("_disp_cache", {})
        key = (id(f), pair, p)
        if key not in cache:
            src, flip = self._source(f, pair)
            grid = f.by_pair[src] if src is not None else f.all_grid
            q = self._forza_norm(p)
            hits = grid.nearest((-q[0], q[1], q[2]) if flip else q, self.opts.k)
            cache[key] = self._weighted_disp(f, grid, flip, hits, f.box.scale_to(self.forza_box))
        return cache[key]

    @staticmethod
    def _weighted_disp(f: HostField, grid: Grid, flip: bool, hits, scale) -> tuple:
        """_disp from the nearest host vertices (grid.nearest's hits)."""
        wsum, d = 0.0, [0.0, 0.0, 0.0]
        for dist, n in hits:
            w = 1.0 / (dist + 0.02) ** 2
            disp = f.disp[grid.values[n]]
            if flip:
                disp = (-disp[0], disp[1], disp[2])
            for a in range(3):
                d[a] += w * disp[a]
            wsum += w
        return tuple(d[a] / wsum * scale[a] for a in range(3)) if wsum else (0.0, 0.0, 0.0)

    def _disp_prefetch(self, f: HostField, keys) -> None:
        """_disp for many (pair, p) at once into its cache (Grid.nearest_many per
        host grid; the weighting is _disp's)."""
        cache = self.__dict__.setdefault("_disp_cache", {})
        groups = defaultdict(dict)  # (source panel, flip) -> {p: [pairs]}
        for pair, p in keys:
            if (id(f), pair, p) not in cache:
                groups[self._source(f, pair)].setdefault(p, []).append(pair)
        scale = f.box.scale_to(self.forza_box) if groups else None
        for (src, flip), todo in groups.items():
            grid = f.by_pair[src] if src is not None else f.all_grid
            ps = list(todo)
            qs = [self._forza_norm(p) for p in ps]
            hits = grid.nearest_many([(-q[0], q[1], q[2]) if flip else q for q in qs], self.opts.k)
            for p, h in zip(ps, hits, strict=True):
                value = self._weighted_disp(f, grid, flip, h, scale)
                for pair in todo[p]:
                    cache[(id(f), pair, p)] = value

    def _exposed_box(self, tag: str, pair, p) -> Box | None:
        """Projection box of an exposed vertex: the bay's, or its panel's (bonnet,
        else boot lid, by the panel's role or the nearer one)."""
        kind = _kind_of(tag)
        if kind != "inner":
            return self.exposed_boxes.get(kind)
        role = self.panels[pair].role if pair in self.panels else None
        name = "trunk" if role in (5, 124) else "hood" if role == 4 else None
        boxes = {k: b for k, b in self.exposed_boxes.items() if k in ("hood", "trunk")}
        if name is None and boxes:
            name = min(boxes, key=lambda k: abs(boxes[k].centre[2] - p[2]))
        return boxes.get(name)

    def _expose(self, builder, sub, pair, f: HostField) -> None:
        """UV0 and baked sky for the vertices add_geometry marked with an
        EXPOSED_SENTINEL (split_exposed's inner faces and bay trims): a planar
        projection of the vertex in its box onto the tag's picture of the shared
        chassis texture (EXPOSED_MAP), and exposed_sky."""
        stride, buf = sub.stride, builder.buffers[sub.stride]
        tags = {struct.pack("<2e", *uv): tag for tag, uv in EXPOSED_SENTINEL.items()}
        first, count = sub.first_index, sub.index_count
        stats = self.report.setdefault("exposed", Counter())
        for v in sorted({sub.base_vertex + i for i in builder.indices[first : first + count]}):
            at = v * stride
            tag = tags.get(bytes(buf[at + 8 : at + 12]))
            if tag is None:
                continue
            p = tuple(c / POSITION_UNITS for c in struct.unpack_from("<3h", buf, at))
            box = self._exposed_box(tag, pair, p)
            rect, au, av = EXPOSED_MAP[tag]
            if box is None:
                uv = ((rect[0] + rect[1]) / 2, (rect[2] + rect[3]) / 2)
            else:
                t = [
                    min(1.0, max(0.0, (p[a] - box.lo[a]) / box.size[a])) if box.size[a] > 1e-3 else 0.5
                    for a in (au, av)
                ]
                uv = (
                    rect[0] + t[0] * (rect[1] - rect[0]),
                    rect[3] - t[1] * (rect[3] - rect[2]) if av == 1 else rect[2] + t[1] * (rect[3] - rect[2]),
                )
            struct.pack_into("<2e", buf, at + 8, *uv)
            sky = exposed_sky(tag, stored_normal(buf, at + LAYOUT[stride][1]))
            struct.pack_into("<h", buf, at + 6, round(sky * POSITION_UNITS))
            stats[_kind_of(tag) + " vertices"] += 1

    def _is_local(self, f: HostField, material: bytes) -> bool:
        """A material of the entry itself (Chassis/Lights) rather than a shared one."""
        if material in f.local:
            return True
        cached = f.__dict__.get("_shared_materials")  # (uv1 it was made from, set)
        if cached is None or cached[0] is not f.uv1:
            shared = set(self.f2d.SHARED.values()) | set(self.f2d.SHARED_EXTERIOR.values())
            if f.uv1 is not None:
                shared |= {t[3] for t in f.uv1.tris if not t[4]}
            cached = f._shared_materials = (f.uv1, shared)
        return material not in cached[1]

    def _uv1(
        self, f: HostField, p, pair, material: bytes, any_panel: bool = False, normal=None, chart: int | None = None
    ):
        """(UV1, host chart) or None."""
        key = (id(f), pair, material, p, any_panel, normal, chart)
        cache = self.__dict__.setdefault("_uv1_cache", {})
        if key not in cache:
            cache[key] = f.uv1.lookup(
                self._forza_norm(p), pair, material, self._is_local(f, material), any_panel, normal, chart
            )
        return cache[key]

    def _uv1_prefetch(self, f: HostField, items) -> None:
        """_uv1 for many (p, pair, material, any_panel, normal, chart) at once into
        its cache (Uv1Field.lookup_many)."""
        cache = self.__dict__.setdefault("_uv1_cache", {})
        todo = {}
        for p, pair, material, any_panel, normal, chart in items:
            key = (id(f), pair, material, p, any_panel, normal, chart)
            if key not in cache and key not in todo:
                todo[key] = (
                    self._forza_norm(p),
                    pair,
                    material,
                    self._is_local(f, material),
                    any_panel,
                    normal,
                    chart,
                )
        if todo:
            for key, hit in zip(todo, f.uv1.lookup_many(list(todo.values())), strict=True):
                cache[key] = hit

    def _prefetch(self, builder, subs, pair, f: HostField) -> None:
        """Fill the _disp and _uv1 caches with what _finish asks of them, at once.
        _finish only reads positions, s9 byte 2 and the s5 normal (bytes 0, 1)
        before it writes them, and writes none of these."""
        rigid = self._rigid(pair)
        disp, uv1 = [], []
        for sub in subs:
            if sub.stride not in LAYOUT:
                continue
            o9, o5, _ = LAYOUT[sub.stride]
            buf, stride = builder.buffers[sub.stride], sub.stride
            idx = builder.indices[sub.first_index : sub.first_index + sub.index_count]
            for v in sorted({sub.base_vertex + i for i in idx}):
                at = v * stride
                raw = struct.unpack_from("<3h", buf, at)
                p = tuple(c / POSITION_UNITS for c in raw)
                others = self._seam.get(raw) if rigid else None
                if others and pair in others:
                    at_p = tuple(c / POSITION_UNITS for c in self._seam_rep.get(raw, raw))
                    disp += ((q, at_p) for q in others)
                else:
                    disp.append((pair, p))
                if stride == 32 and f.uv1 is not None:
                    material = sub.material
                    if buf[at + o9 + 2] == 0 and self._is_local(f, material):
                        material = self.f2d.PAINT
                    uv1.append((p, pair, material, False, stored_normal(buf, at + o5), None))
        self._disp_prefetch(f, disp)
        if uv1:
            self._uv1_prefetch(f, uv1)

    def _finish(self, builder, subs, pair, f: HostField) -> list:
        """Write s9 bytes 0-1, s12, the s5 normal delta and (32-byte vertices) the
        decal-atlas UV1 of the vertices `subs` use."""
        points = []
        rigid = self._rigid(pair)
        bound = self.bound(pair)
        self._prefetch(builder, subs, pair, f)
        for sub in subs:
            if sub.stride not in LAYOUT:
                continue
            self._expose(builder, sub, pair, f)
            o9, o5, o12 = LAYOUT[sub.stride]
            buf = builder.buffers[sub.stride]
            stride = sub.stride
            idx = builder.indices[sub.first_index : sub.first_index + sub.index_count]
            verts = sorted({sub.base_vertex + i for i in idx})
            pos, dmg, charts = {}, {}, {}
            for v in verts:
                at = v * stride
                raw = struct.unpack_from("<3h", buf, at)
                p = tuple(c / POSITION_UNITS for c in raw)
                pos[v] = p
                # A position shared with other never-swinging panels (a seam inside
                # one Forza surface) gets the same crushed position on each, so the
                # seam stays shut when the panels crush alike.
                others = self._seam.get(raw) if rigid else None
                if others and pair in others:
                    at_p = tuple(c / POSITION_UNITS for c in self._seam_rep.get(raw, raw))
                    ds = [self._disp(f, q, at_p) for q in others]
                    disp = tuple(sum(d[a] for d in ds) / len(ds) for a in range(3))
                    self._seam_written.add((id(f), raw))
                else:
                    disp = self._disp(f, pair, p)
                if rigid:
                    # Every copy of a seam position takes the same weight (the
                    # smallest of its panels'), so unpinned seams stay shut too.
                    w = (
                        min(self._pin_weight(q, at_p) for q in others)
                        if others and pair in others
                        else self._pin_weight(pair, p)
                    )
                    disp = tuple(w * c for c in disp)
                q = tuple(p[a] + disp[a] for a in range(3))
                dmg[v] = q
                buf[at + o9], buf[at + o9 + 1] = bound
                struct.pack_into("<4e", buf, at + o12, q[0], q[1], q[2], 1.0)
                if stride == 32 and f.uv1 is not None:
                    # Lights-material triangles that aren't lamps (badges, plate,
                    # reflector trim; s9 byte 2 = 0) take the panel's paint layout:
                    # the lamp layout puts them on the broken-lamp tile, whose dark
                    # blobs strip the paint to white when damaged.
                    material = sub.material
                    if buf[at + o9 + 2] == 0 and self._is_local(f, material):
                        material = self.f2d.PAINT
                    hit = self._uv1(f, p, pair, material, normal=stored_normal(buf, at + o5))
                    if hit is not None:
                        struct.pack_into("<2e", buf, at + 12, *hit[0])  # semantic 8 components 2, 3
                        charts[v] = hit[1]
            points += pos.values()
            # Normal deltas: rotate the stored normal by the change of the
            # area-weighted face normal from the intact to the crushed mesh.
            n0 = defaultdict(lambda: [0.0, 0.0, 0.0])
            n1 = defaultdict(lambda: [0.0, 0.0, 0.0])
            for t in range(0, len(idx) - 2, 3):
                tri = [sub.base_vertex + idx[t + j] for j in range(3)]
                a = _cross(_sub(pos[tri[1]], pos[tri[0]]), _sub(pos[tri[2]], pos[tri[0]]))
                b = _cross(_sub(dmg[tri[1]], dmg[tri[0]]), _sub(dmg[tri[2]], dmg[tri[0]]))
                for v in tri:
                    for k in range(3):
                        n0[v][k] += a[k]
                        n1[v][k] += b[k]
            for v in verts:
                at = v * stride
                b0, b1 = buf[at + o5], buf[at + o5 + 1]
                delta = normal_delta(b0, b1, _norm(n0[v]), _norm(n1[v]))
                buf[at + o5 + 2], buf[at + o5 + 3] = delta
            if charts:
                self._split_seams(builder, sub, f, pair, pos, charts)
        return points

    def _split_seams(self, builder, sub, f: HostField, pair, pos, charts) -> None:
        """A triangle whose corners took UV1 from different host charts would
        stretch across the decal atlas (scratch rows between the tiles show as
        long streaks). Put the triangle on the chart most of its corners use:
        the other corners get a copy of their vertex with UV1 from that chart."""
        buf = builder.buffers[sub.stride]
        stride, o5 = sub.stride, LAYOUT[sub.stride][1]
        # The lookups below at once (_uv1_prefetch): the loop only appends vertices
        # and changes the indices of the triangle it is at.
        asks = {}
        for t in range(sub.first_index, sub.first_index + sub.index_count, 3):
            tri = [sub.base_vertex + builder.indices[t + j] for j in range(3)]
            cs = [charts.get(v) for v in tri]
            if None in cs or cs[0] == cs[1] == cs[2]:
                continue
            chart = Counter(cs).most_common(1)[0][0]
            for j, v in enumerate(tri):
                if cs[j] != chart and (v, chart) not in asks:
                    material = sub.material
                    if buf[v * stride + LAYOUT[stride][0] + 2] == 0 and self._is_local(f, material):
                        material = self.f2d.PAINT
                    asks[(v, chart)] = (pos[v], pair, material, False, stored_normal(buf, v * stride + o5), chart)
        if asks:
            self._uv1_prefetch(f, list(asks.values()))
        copies: dict[tuple, int | None] = {}
        for t in range(sub.first_index, sub.first_index + sub.index_count, 3):
            tri = [sub.base_vertex + builder.indices[t + j] for j in range(3)]
            cs = [charts.get(v) for v in tri]
            if None in cs or cs[0] == cs[1] == cs[2]:
                continue
            chart = Counter(cs).most_common(1)[0][0]
            for j, v in enumerate(tri):
                if cs[j] == chart:
                    continue
                key = (v, chart)
                if key not in copies:
                    copies[key] = None
                    material = sub.material
                    if buf[v * stride + LAYOUT[stride][0] + 2] == 0 and self._is_local(f, material):
                        material = self.f2d.PAINT  # as in _finish: non-lamp Lights triangles
                    hit = self._uv1(f, pos[v], pair, material, normal=stored_normal(buf, v * stride + o5), chart=chart)
                    new = len(buf) // stride
                    why = (
                        "no hit"
                        if hit is None
                        else "other chart"
                        if hit[1] != chart
                        else "16-bit range"
                        if new - sub.base_vertex > 0xFFFF
                        else None
                    )
                    if why:
                        stats = self.report.setdefault("seam_skipped", {})
                        stats[why] = stats.get(why, 0) + 1
                    if why is None:
                        buf += buf[v * stride : (v + 1) * stride]
                        struct.pack_into("<2e", buf, new * stride + 12, *hit[0])
                        copies[key] = new
                        self.report["seam_vertices"] = self.report.get("seam_vertices", 0) + 1
                if copies[key] is not None:
                    builder.indices[t + j] = copies[key] - sub.base_vertex

    def _merge(self, builder, panel_subs: dict[tuple, list]) -> list:
        """Part-0 submodels: every panel's triangles again, one submodel per
        material (split when the vertex span passes 16 bits), on the same vertices."""
        by_material = defaultdict(list)
        for pair in sorted(panel_subs):
            for s in panel_subs[pair]:
                by_material[s.material].append(s)
        merged = []
        for material, subs in by_material.items():
            groups, current = [], []
            lo = hi = None
            for s in sorted(subs, key=lambda s: s.base_vertex):
                idx = builder.indices[s.first_index : s.first_index + s.index_count]
                s_lo, s_hi = s.base_vertex + min(idx), s.base_vertex + max(idx)
                if current and max(hi, s_hi) - min(lo, s_lo) > 0xFFFF:
                    groups.append((lo, current))
                    current, lo, hi = [], None, None
                current.append(s)
                lo = s_lo if lo is None else min(lo, s_lo)
                hi = s_hi if hi is None else max(hi, s_hi)
            if current:
                groups.append((lo, current))
            for base, group in groups:
                first = len(builder.indices)
                for s in group:
                    for i in builder.indices[s.first_index : s.first_index + s.index_count]:
                        builder.indices.append(s.base_vertex + i - base)
                new = self.f2d.NewSub(
                    builder.new_uid(),
                    material,
                    group[0].stride,
                    base,
                    first,
                    len(builder.indices) - first,
                    group[0].flag,
                )
                builder.subs.append(new)
                merged.append(new)
        return merged

    def full_lod(self, builder, part_lods: dict, mask: int, lod: int, **kwargs) -> None:
        """Full entry, one detail mask: per-panel MODL parts plus the merged part 0."""
        panel_subs = self._geometry(builder, self.car.body, lod, self.full_field, record_box=True, **kwargs)
        for pair, subs in panel_subs.items():
            part = self.panels[pair].part if pair in self.panels else None
            if part is not None and part != 0:
                part_lods.setdefault(part, {}).setdefault(mask, []).extend(subs)
        part_lods.setdefault(0, {})[mask] = self._merge(builder, panel_subs)
        self.report["panels"].setdefault(
            str(mask), {self.label(p): sum(s.index_count // 3 for s in subs) for p, subs in panel_subs.items()}
        )

    def exterior(self, builder, lod: int, **kwargs) -> list:
        """Exterior entry: the merged body only (vanilla exteriors have no panel parts)."""
        panel_subs = self._geometry(builder, self.car.body, lod, self.ext_field, **kwargs)
        merged = self._merge(builder, panel_subs)
        drop = {id(s) for subs in panel_subs.values() for s in subs}
        compact(builder, [s for s in builder.subs if id(s) not in drop])
        return merged

    def interior_uv1(self, stream, interior) -> int:
        """Decal-atlas UV1 for the rebuilt interior entry's 32-byte vertices (its
        outer shell's paint: the bonnet seen from the cockpit), from the host
        interior's shell by nearest triangle of the same material class, any
        panel. Call after the interior is rebuilt. Returns vertices written."""
        from dsfforza.convert.cockpit import DASHBOARD_INSTRUMENT

        if self.int_uv1 is None:
            return 0
        f = HostField([], [], [], self.int_uv1.box, uv1=self.int_uv1, local=self.int_local)
        drsp = child(child(interior, "MDXN"), "DRSP")
        buffers, index_buffers, users = {}, {}, defaultdict(dict)
        for e in drsp.children:
            if e.tag == "VXCK":
                hd = payload(stream, child(e, "VXHD"))
                stride = struct.unpack_from("<I", hd, 12)[0]
                elements = vm.parse_vdcl(payload(stream, child(e, "VDCL")))
                e8 = next((x for x in elements if x.semantic == 8), None)
                e5 = next((x for x in elements if x.semantic == 5), None)
                # Not the needle buffers: their semantic 8 is DashboardInstrument
                # (u, v, gauge id, 0), and a UV1 there overwrites the gauge id,
                # so the engine never turns the needles.
                if (
                    e8 is not None
                    and e8.components == 4
                    and e8.type == 8
                    and elements[0].semantic == 0
                    and e8.name_hash != DASHBOARD_INSTRUMENT
                ):
                    # Offsets from the VDCL, not the converter's layout: the
                    # interior may keep host-shaped buffers (some lack semantic 9).
                    buffers[hd[:8]] = (child(e, "VXDT"), stride, e8.offset + 4, e5.offset if e5 else None)
            elif e.tag == "IXCK":
                index_buffers[payload(stream, child(e, "IXHD"))[:8]] = payload(stream, child(e, "IXDT"))
        for e in drsp.children:
            if e.tag != "SBMC":
                continue
            d = payload(stream, child(e, "SBBH"))
            if d[16:24] not in buffers:
                continue
            (base,) = struct.unpack_from("<I", d, 0x20)
            first, count = struct.unpack_from("<II", d, 0x38)
            ib = index_buffers[d[0x30:0x38]]
            for t in range(first, first + count):
                users[d[16:24]][base + struct.unpack_from("<H", ib, t * 2)[0]] = d[8:16]
        written = 0
        for uid, (vxdt, stride, o_uv1, o5) in buffers.items():
            buf = bytearray(payload(stream, vxdt))
            asks = []  # the lookups below at once (they write UV1 only, which none reads)
            for v, material in users[uid].items():
                at = v * stride
                p = tuple(c / POSITION_UNITS for c in struct.unpack_from("<3h", buf, at))
                normal = stored_normal(buf, at + o5) if o5 is not None else None
                asks.append((p, (0, 0), material, True, normal, None))
            self._uv1_prefetch(f, asks)
            for v, material in users[uid].items():
                at = v * stride
                p = tuple(c / POSITION_UNITS for c in struct.unpack_from("<3h", buf, at))
                normal = stored_normal(buf, at + o5) if o5 is not None else None
                hit = self._uv1(f, p, (0, 0), material, any_panel=True, normal=normal)
                if hit is not None:
                    struct.pack_into("<2e", buf, at + o_uv1, *hit[0])
                    written += 1
            vxdt.payload = bytes(buf)
        self.report["interior_uv1"] = written
        return written

    # -- hierarchy --
    def patch_upvh(self, data: bytes) -> tuple[bytes, dict]:
        """Move each panel's hinge pivot, rigid body (centre, inertia) and
        collision boxes from the host panel's box to the Forza panel's, by
        relative position in the box. With opts.hull, scale the root hull too."""
        h = upvh_mod.parse(data)
        out = bytearray(data)
        report = {}
        pdl = h.pdl_offset
        for pair, pts in sorted(self.boxes.items()):
            panel = self.panels.get(pair)
            hb, fb = self._host_box(pair), robust_box(pts)
            if panel is None or hb is None or fb is None:
                continue
            for r in panel.records:
                if r.role in NOT_MOVED or r.type != 1:
                    continue
                if r.role == 38 and not self.opts.hull:
                    continue  # the chassis body/box is the car's; only with the hull option
                entry = {"role": role_name(r.role)}
                # Centred hinges and bodies stay centred while the Forza panel
                # spans the centre line too; a host twin exhaust taken by the
                # RX-7's one-sided pipe gets its body moved onto the pipe.
                centred = fb.lo[0] < 0 < fb.hi[0]
                if r.hinge >= 0:
                    at = h.offsets["hinges"] + r.hinge * 0x30
                    old = struct.unpack_from("<3f", out, at)
                    new = hb.to(fb, old)
                    if abs(old[0]) < 0.02 and centred:
                        new = (old[0],) + new[1:]  # keep centred hinges centred
                    if math.dist(old, new) > MAX_HINGE_MOVE:
                        entry["hinge_kept"] = round(math.dist(old, new), 3)  # implausible: keep the host's
                        new = old
                    struct.pack_into("<3f", out, at, *new)
                    entry["hinge"] = [round(c, 3) for c in new]
                    entry["hinge_moved"] = round(math.dist(old, new), 3)
                scale = tuple(min(COLLISION_SCALE[1], max(COLLISION_SCALE[0], c)) for c in hb.scale_to(fb))
                if r.body >= 0:
                    at = h.offsets["bodies"] + r.body * 0xB0
                    m = struct.unpack_from("<12f", out, at)
                    rows = (m[0:3], m[4:7], m[8:11])
                    pos = struct.unpack_from("<3f", out, at + 0x30)
                    centre = tuple(-_dot(rows[a], pos) for a in range(3))
                    new_c = hb.to(fb, centre)
                    if abs(centre[0]) < 0.02 and centred:
                        new_c = (centre[0],) + new_c[1:]
                    new_pos = tuple(-sum(rows[k][a] * new_c[k] for k in range(3)) for a in range(3))
                    struct.pack_into("<3f", out, at + 0x30, *new_pos)
                    inertia = struct.unpack_from("<3f", out, at + 0x40)
                    new_i = (
                        inertia[0] * (scale[1] ** 2 + scale[2] ** 2) / 2,
                        inertia[1] * (scale[0] ** 2 + scale[2] ** 2) / 2,
                        inertia[2] * (scale[0] ** 2 + scale[1] ** 2) / 2,
                    )
                    struct.pack_into("<3f", out, at + 0x40, *new_i)
                    entry["centre"] = [round(c, 3) for c in new_c]
                if r.collision >= 0:
                    _scale_collision(out, pdl + 0x10 + r.collision, scale)
                    entry["collision_scale"] = [round(s, 3) for s in scale]
                report[f"record {r.index}"] = entry
        out, report["mirrors"] = self._remount_mirrors(bytes(out))
        out = bytearray(out)
        if self.opts.hull:
            root = h.records[0]
            if root.collision >= 0:
                host_box, forza_box = self.full_field.box, self.forza_box
                _map_collision(out, pdl + 0x10 + root.collision, host_box, forza_box)
                report["hull"] = {"host": [host_box.lo, host_box.hi], "forza": [forza_box.lo, forza_box.hi]}
        self.report["upvh"] = report
        return bytes(out), report

    # A side mirror's record hangs under the record of what the mirror is mounted on,
    # and moves with it: vanilla mirrors sit on the door skin and hang under the door
    # record (253 of 277), the rest under the front structure, the chassis or the
    # bonnet (276, 197, 232...). Forza mirrors are often mounted on the A-pillar or the
    # sail ahead of the window, which the converted car draws through the chassis
    # (or on a door held shut, DamagePlan.bound). Under the host's door record they
    # swung out with the door whenever a crash knocked it ajar, and floated beside the
    # body (issue #187: Devon GTX, Lancer Evo X). Such a mirror goes under the record
    # that moves its mount as drawn.

    def _mirror_mount(self, pair) -> tuple | None:
        """The panel a mirror panel is mounted on: the one with the most mirror points
        within MOUNT_TOUCH of its own (finest full-entry LOD), else the nearest within
        MOUNT_REACH; None if no panel is that close."""
        pts = np.array(self.boxes.get(pair, ()), dtype=float).reshape(-1, 3)
        if not len(pts):
            return None
        lo, hi = pts.min(0) - MOUNT_REACH, pts.max(0) + MOUNT_REACH
        best = None
        for other, opts in self.boxes.items():
            panel = self.panels.get(other)
            if other == pair or panel is None or panel.role in MIRRORS:
                continue
            q = np.array(opts, dtype=float).reshape(-1, 3)
            q = q[((q >= lo) & (q <= hi)).all(1)]
            if not len(q):
                continue
            d = np.full(len(pts), np.inf)
            for k in range(0, len(q), 2048):
                d = np.minimum(d, np.sqrt(((pts[:, None, :] - q[None, k : k + 2048, :]) ** 2).sum(-1)).min(1))
            if d.min() > MOUNT_REACH:
                continue
            score = (int((d <= MOUNT_TOUCH).sum()), -float(d.min()))
            if best is None or score > best[0]:
                best = (score, other)
        return best[1] if best else None

    def _remount_mirrors(self, data: bytes) -> tuple[bytes, dict]:
        """Put each mirror record under the record whose transform slot draws its
        mount (DamagePlan.bound), where its host parent is another (move_subtree)."""
        report = {}
        for pair, panel in sorted(self.panels.items()):
            if panel.role not in MIRRORS or pair not in self.boxes:
                continue
            mount = self._mirror_mount(pair)
            if mount is None:
                continue
            slot = self.bound(mount)[0]
            h = upvh_mod.parse(data)
            par = _parents(h)
            mirror = next((r for r in h.records if r.type == 1 and (r.slot, r.model) == pair), None)
            if mirror is None or par[mirror.index] is None:
                continue
            parent = h.records[par[mirror.index]]
            # A record without a slot of its own (the front structure) moves with the chassis.
            if (parent.slot if parent.slot >= 0 else self.chassis[0]) == slot:
                continue
            targets = [r for r in h.records if r.type == 1 and r.slot == slot]
            target = next((r for r in targets if (r.slot, r.model) == mount), None)
            target = target or next((r for r in targets if r.role == 38), None) or (targets[0] if targets else None)
            if target is None or mirror.index <= target.index < mirror.index + mirror.subtree:
                continue
            data, ordered = move_subtree(data, mirror.index, target.index)
            report[self.label(pair)] = {
                "mount": self.label(mount),
                "from": role_name(parent.role),
                "to": role_name(target.role),
                "in_order": ordered,
            }
        return data, report


def _parents(h) -> list[int | None]:
    """Record index -> its parent's (depth-first, subtree sizes)."""
    out, stack = [None] * len(h.records), []
    for r in h.records:
        while stack and stack[-1][1] <= r.index:
            stack.pop()
        out[r.index] = stack[-1][0] if stack else None
        stack.append((r.index, r.index + r.subtree))
    return out


def move_subtree(data: bytes, index: int, parent: int) -> tuple[bytes, bool]:
    """Move record `index` and its subtree to be a child of record `parent` (UPVH
    records are depth-first with subtree sizes). Vanilla hierarchies number their part
    records' transform slots (+0x34) and panel-data indices (+0x2A) in record order,
    and vertices carry those numbers, so the subtree goes between the new parent's
    children where that order holds; if there is no such place, after them
    (returns False)."""
    h = upvh_mod.parse(data)
    par = _parents(h)
    size = upvh_mod.RECORD_SIZE
    count = h.records[index].subtree
    if index <= parent < index + count:
        raise ValueError(f"record {parent} is inside record {index}'s subtree")
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
    ordered = at is not None
    at = bounds[-1] if at is None else at
    for a in ancestors:
        rest_sub[a] += count
    new_raw = rest_raw[:at] + block_raw + rest_raw[at:]
    new_sub = rest_sub[:at] + block_sub + rest_sub[at:]
    for r, s in zip(new_raw, new_sub, strict=True):
        struct.pack_into("<H", r, 0x14, s)
    start, end = upvh_mod.HEADER_SIZE, upvh_mod.HEADER_SIZE + len(raw) * size
    return data[:start] + b"".join(bytes(r) for r in new_raw) + data[end:], ordered


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


def compact(builder, keep: list) -> None:
    """Keep only `keep` submodels in a forza2dsf.Builder and rebuild its index list."""
    indices = []
    for s in keep:
        seg = builder.indices[s.first_index : s.first_index + s.index_count]
        s.first_index = len(indices)
        indices.extend(seg)
    builder.indices = indices
    builder.subs = list(keep)


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


# --- CLI: inspection and checks ---------------------------------------------------


def _entries(archive: Path, spool_id: int):
    stream = archive.open("rb")
    root = read_chunk(stream, 0)
    return stream, root, upvh_mod.entries_for(stream, root.entries, spool_id, None)


def cmd_panels(args) -> None:
    stream, _root, kinds = _entries(args.archive, args.id)
    with stream:
        full = kinds[0]
        nonr = child(full, "NONR")
        data = payload(stream, child(nonr, "UPVH"))
        h = upvh_mod.parse(data)
        panels = host_panels(data, payload(stream, child(nonr, "MODL")))
        f = host_field(stream, full, 4)
    counts = Counter(f.pairs)
    print(f"{full.name}: {len(panels)} panels; part 0 mask 4: {len(f.points)} vertices, box {f.box}")
    for pair, p in sorted(panels.items()):
        r = p.record
        hinge = ""
        if r.hinge >= 0:
            piv = struct.unpack_from("<3f", h.table("hinges")[r.hinge])
            axis = struct.unpack_from("<3f", h.table("hinges")[r.hinge], 16)
            limit, param = struct.unpack_from("<2f", h.table("hinges")[r.hinge], 32)
            hinge = (
                f"hinge {r.hinge} pivot ({piv[0]:.2f}, {piv[1]:.2f}, {piv[2]:.2f}) axis "
                f"({axis[0]:.0f}, {axis[1]:.0f}, {axis[2]:.0f}) limit {limit:.3f} param {param:g}"
            )
        ns = [n for n, q in enumerate(f.pairs) if q == pair]
        crush = max((math.dist((0, 0, 0), f.disp[n]) for n in ns), default=0)
        print(
            f"  s9 {pair[0]:2d},{pair[1]:2d}  rec {','.join(str(x.index) for x in p.records):6s} "
            f"{role_name(p.role):18s} part {p.part if p.part is not None else '-':>2}  verts {counts[pair]:5d} "
            f"max crush {crush:.3f}  {hinge}"
        )


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


def _colour(k: int):
    r, g, b = colorsys.hsv_to_rgb((k * 0.61803) % 1.0, 0.65, 1.0)
    return r, g, b


def render(tris, path: Path, size: int = 300) -> None:
    """Side (both), top, front and rear views; tris: [(3 points, rgb 0..1)] or
    [(3 points, None, 3 uvs, texel function uv -> rgb 0..1)] for per-pixel texturing."""
    xs = [p[0] for t in tris for p in t[0]]
    ys = [p[1] for t in tris for p in t[0]]
    zs = [p[2] for t in tris for p in t[0]]
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) * 1.05
    centre = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2)
    # (horizontal axis, flip, vertical axis, depth axis, depth sign): -x side, +x side, top, front, rear
    views = [(2, 1, 1, 0, 1), (2, -1, 1, 0, -1), (0, 1, 2, 1, -1), (0, -1, 1, 2, -1), (0, 1, 1, 2, 1)]
    width = size * len(views)
    pixels = bytearray([24, 24, 28] * width * size)
    light = (0.4, 0.8, 0.45)
    for view, (h, flip, v, d, sign) in enumerate(views):
        depth = [math.inf] * (size * size)
        for item in tris:
            tri, col = item[0], item[1]
            n = _cross(_sub(tri[1], tri[0]), _sub(tri[2], tri[0]))
            length = math.sqrt(_dot(n, n)) or 1
            shade = 0.3 + 0.7 * abs(_dot(n, light)) / length
            c = bytes(int(255 * shade * k) for k in col) if col is not None else None
            pts = [
                (
                    flip * (p[h] - centre[h]) / span * size + size / 2,
                    size / 2 - (p[v] - centre[v]) / span * size,
                    sign * (p[d] - centre[d]),
                )
                for p in tri
            ]
            x0, x1 = max(int(min(p[0] for p in pts)), 0), min(int(max(p[0] for p in pts)) + 1, size)
            y0, y1 = max(int(min(p[1] for p in pts)), 0), min(int(max(p[1] for p in pts)) + 1, size)
            (px, py, pz), (qx, qy, qz), (rx, ry, rz) = pts
            area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
            if abs(area) < 1e-9:
                continue
            for y in range(y0, y1):
                for x in range(x0, x1):
                    w0 = ((qx - x) * (ry - y) - (qy - y) * (rx - x)) / area
                    w1 = ((rx - x) * (py - y) - (ry - y) * (px - x)) / area
                    w2 = 1 - w0 - w1
                    if w0 < 0 or w1 < 0 or w2 < 0:
                        continue
                    z = w0 * pz + w1 * qz + w2 * rz
                    k = y * size + x
                    if z < depth[k]:
                        depth[k] = z
                        o = (y * width + view * size + x) * 3
                        if c is None:
                            uvs, texel = item[2], item[3]
                            u = w0 * uvs[0][0] + w1 * uvs[1][0] + w2 * uvs[2][0]
                            t = w0 * uvs[0][1] + w1 * uvs[1][1] + w2 * uvs[2][1]
                            pixels[o : o + 3] = bytes(min(255, int(255 * (0.4 + 0.6 * shade) * k)) for k in texel(u, t))
                        else:
                            pixels[o : o + 3] = c
    vm.write_png(path, width, size, pixels)


DECAL_TEXTURE = bytes.fromhex("00122b6db3fa1227")  # SSIC: dent normals (RGB) + scratch mask (alpha)


def decal_texel(stream_path: Path):
    """uv -> rgb of a preview of the shared damage decal: grey paint, white where
    the scratch mask (alpha) is dark, tinted by the dent normal map."""
    from dsfforza.convert import dsf_materials
    from dsfforza.formats.chnk import walk

    with stream_path.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = next(e for e in root.entries if e.tag == "SSIC")
        dds = next(
            payload(stream, child(e, "DXTC"))
            for _, e in walk(ssic.children)
            if e.tag == "TEXC" and payload(stream, child(e, "UIDC"))[:8] == DECAL_TEXTURE
        )
    w, h, rgba = dsf_materials.decode_dds(dds)

    def texel(u, v):
        x = min(w - 1, max(0, int(u * w)))
        y = min(h - 1, max(0, int(v * h)))
        r, g, _b, a = rgba[(y * w + x) * 4 : (y * w + x) * 4 + 4]
        scratch = 1 - a / 255
        base = (0.45 + 0.25 * (r / 255 - 0.5), 0.45 + 0.25 * (g / 255 - 0.5), 0.5)
        return tuple(base[k] * (1 - scratch) + scratch for k in range(3))

    return texel


TRIM_TEXTURE = bytes.fromhex("00122b73d1fa1227")  # SSIC: GLOBAL_Chassis(_Simple) diffuse
TRIM_MATERIALS = {bytes.fromhex("0012833b12fe1027"), bytes.fromhex("10122b9753b14e2a")}
CLASS_COLOURS = {
    bytes.fromhex("001283358afd1027"): (0.75, 0.35, 0.1),
    bytes.fromhex("10122b9653b14e2a"): (0.75, 0.35, 0.1),
    bytes.fromhex("001283369afd1027"): (0.3, 0.5, 0.7),
    bytes.fromhex("10122b9953b14e2a"): (0.3, 0.5, 0.7),
}


def shared_texel(stream_path: Path, uid: bytes):
    """uv -> grey (its red channel) of a shared SSIC texture, for --by trim."""
    from dsfforza.convert import dsf_materials
    from dsfforza.formats.chnk import walk

    with stream_path.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = next(e for e in root.entries if e.tag == "SSIC")
        dds = next(
            payload(stream, child(e, "DXTC"))
            for _, e in walk(ssic.children)
            if e.tag == "TEXC" and payload(stream, child(e, "UIDC"))[:8] == uid
        )
    w, h, rgba = dsf_materials.decode_dds(dds)

    def texel(u, v):
        x = min(w - 1, max(0, int(u % 1 * w)))
        y = min(h - 1, max(0, int(v % 1 * h)))
        r = rgba[(y * w + x) * 4] / 255
        return (r, r, r)

    return texel


def cmd_render(args) -> None:
    """Colour by MODL part or by s9 panel; --damage t draws lerp(position, s12, t)
    for every panel, as the shader does with fDamage = t."""
    stream, _root, kinds = _entries(args.archive, args.id)
    with stream:
        vehc = kinds[args.kind]
        mesh = load_mesh(stream, vehc)
    mask = args.mask or {0: 4, 1: 64, 2: 2}[args.kind]
    only = {int(x) for x in args.parts.split(",")} if args.parts else None
    skip = {int(x) for x in args.skip.split(",")} if args.skip else set()
    tris = []
    texel = decal_texel(stream_path=args.archive) if args.by == "decal" else None
    trim_texel = shared_texel(args.archive, TRIM_TEXTURE) if args.by == "trim" else None
    for p, part in enumerate(mesh.parts):
        if (only is not None and p not in only) or p in skip:
            continue
        for lod in part.lods:
            if lod.mask != mask:
                continue
            for n in lod.submodels:
                for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                    e12 = vb.element(12)
                    pts = []
                    for i in tri:
                        q = vm.position(vb, i)
                        if e12 is not None and args.damage:
                            d = vb.read(e12, i)
                            q = tuple(q[a] + args.damage * (d[a] - q[a]) for a in range(3))
                        pts.append(q)
                    if trim_texel is not None:
                        m = mesh.submodels[n].material
                        if m in TRIM_MATERIALS:
                            e8 = vb.element(8)
                            tris.append((pts, None, [vb.read(e8, i)[:2] for i in tri], trim_texel))
                        else:
                            tris.append((pts, CLASS_COLOURS.get(m, (0.55, 0.5, 0.45))))
                        continue
                    if texel is not None:
                        e8 = vb.element(8)
                        if e8 is not None and e8.components == 4:
                            tris.append((pts, None, [vb.read(e8, i)[2:4] for i in tri], texel))
                        else:
                            tris.append((pts, (0.35, 0.35, 0.4)))
                        continue
                    key = p if args.by == "part" else s9_pair(vb, tri[0])[1] * 7 + s9_pair(vb, tri[0])[0]
                    tris.append((pts, _colour(key)))
    if args.wheels:
        # The wheel model (20-byte buffers, centred on the origin) at every UPVH hub,
        # to see the body against its wheels.
        with args.archive.open("rb") as stream2:
            root2 = read_chunk(stream2, 0)
            kinds2 = upvh_mod.entries_for(stream2, root2.entries, args.id, None)
            h = upvh_mod.parse(payload(stream2, child(child(kinds2[0], "NONR"), "UPVH")))
        hubs = [v[:3] for v in h.vec4s("wheels")]
        wheel = []
        for part in mesh.parts:
            for lod in part.lods:
                if lod.mask != mask:
                    continue
                for n in lod.submodels:
                    for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                        if vb.stride == 20:
                            wheel.append([vm.position(vb, i) for i in tri])
        for hub in hubs:
            for t in wheel:
                tris.append(([tuple(p[a] + hub[a] for a in range(3)) for p in t], (0.25, 0.25, 0.25)))
    if not tris:
        raise SystemExit("nothing to render")
    render(tris, args.png, args.size)
    print(f"wrote {args.png} ({len(tris)} triangles)")


def cmd_check(args) -> None:
    """Structural damage checks for one vehicle's full and exterior entries."""
    stream, _root, kinds = _entries(args.archive, args.id)
    problems = 0

    def report(ok: bool, text: str) -> None:
        nonlocal problems
        problems += not ok
        print(("ok    " if ok else "FAIL  ") + text)

    with stream:
        full = kinds[0]
        nonr = child(full, "NONR")
        panels = host_panels(payload(stream, child(nonr, "UPVH")), payload(stream, child(nonr, "MODL")))
        meshes = {k: load_mesh(stream, kinds[k]) for k in (0, 2) if k in kinds}
    # Rigid panels are drawn through the chassis' transform slot (DamagePlan.bound).
    chassis_slot = 0 if (0, 0) in panels else min(panels)[0]
    drawn = {pair: (chassis_slot, pair[1]) if panel.role in BODY else pair for pair, panel in panels.items()}
    known = set(panels) | set(drawn.values())
    for kind, mesh in meshes.items():
        mask = 4 if kind == 0 else 2
        part0 = set()
        pairs = Counter()
        moved = total = 0
        for lod in mesh.parts[0].lods:
            if lod.mask != mask:
                continue
            for n in lod.submodels:
                for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                    e12 = vb.element(12)
                    part0.add((vb.uid,) + tuple(sorted(tri)))
                    for i in tri:
                        pairs[s9_pair(vb, i)] += 1
                        if e12 is not None:
                            total += 1
                            moved += math.dist(vm.position(vb, i), vb.read(e12, i)[:3]) > 0.005
        unknown = {p: c for p, c in pairs.items() if p not in known}
        report(
            not unknown, f"kind {kind}: every part-0 s9 pair is a host panel ({len(pairs)} pairs, unknown {unknown})"
        )
        report(len(pairs) > 3, f"kind {kind}: part 0 uses {len(pairs)} panels")
        report(total and moved / total > 0.2, f"kind {kind}: {moved} of {total} part-0 vertex uses have a crush target")
        if kind != 0:
            continue
        for pair, panel in sorted(panels.items()):
            if panel.part in (None, 0):
                continue
            tris = set()
            bad = 0
            for lod in mesh.parts[panel.part].lods:
                if lod.mask != mask:
                    continue
                for n in lod.submodels:
                    for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                        tris.add((vb.uid,) + tuple(sorted(tri)))
                        bad += any(s9_pair(vb, i) not in (pair, drawn[pair]) for i in tri)
            if tris:
                report(
                    not bad and tris <= part0,
                    f"part {panel.part} ({role_name(panel.role)}): {len(tris)} triangles, all on panel {drawn[pair]} "
                    f"({bad} not) and all in part 0 ({len(tris - part0)} not)",
                )
    print(f"{problems} problem(s)")
    if problems:
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("panels", help="the host's panel records, pairs, parts, hinges and crush")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    p = sub.add_parser("render", help="render coloured by MODL part or s9 panel, optionally crushed")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--png", type=Path, required=True)
    p.add_argument("--kind", type=int, default=0, choices=(0, 1, 2))
    p.add_argument("--mask", type=int)
    p.add_argument(
        "--by",
        choices=("part", "panel", "decal", "trim"),
        default="part",
        help="decal: the shared scratch/dent decal at UV1 (white = scratch where fully damaged); "
        "trim: shared trim textured from the global chassis texture at UV0, paint orange, glass blue",
    )
    p.add_argument("--damage", type=float, default=0.0, help="fDamage for every panel (0 intact, 1 crushed)")
    p.add_argument("--parts", help="only these MODL parts")
    p.add_argument("--skip", help="leave these MODL parts out")
    p.add_argument("--size", type=int, default=300)
    p.add_argument("--wheels", action="store_true", help="also draw the wheel model at every UPVH hub")
    p = sub.add_parser("check", help="structural damage checks of a (converted) vehicle")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)
    _set_idle_priority()
    {"panels": cmd_panels, "render": cmd_render, "check": cmd_check}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
