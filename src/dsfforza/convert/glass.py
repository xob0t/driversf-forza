"""DSF glass: the shared glass texture's panel slots, UVs for converted glass, and a lint.

The engine's damage-aware glass techniques (GLOBAL_Glass programs 47 and 50,
GLOBAL_Glass_LOD1 55; research/vehicle-model.md, "Glass") draw

    v' = v + 0.25 * floor(g_v4PerPanelData[s9.byte1].y)

so each glass vertex shows the crack state of the panel record it is bound
to (semantic 9 byte 1): band 0 clean, 1 light cracks, 2 heavy cracks, 3
shattered. In band 3 the texture is mostly holes (blue 0: the pixel shader's
texkill), with a shattered rim around each hole.

The texture (SSIC 00122b77dffa1227, 512 x 1024) is laid out as four panel
slots per band, and vanilla cars map each window over its own slot so the
cracks and the hole line up with the window:

    slot          u            v (band 0)   vanilla use (147, 176, 205, 227, 266)
    windscreen    0.13-0.665   0-0.125      windscreen, u along x, v down the glass
    side_front    0.67-1       0-0.125      front door glass (u along -z), lamp glass
    rear          0.17-0.62    0.125-0.25   rear window (defroster lines in band 0)
    side_rear     0.67-1       0.125-0.25   rear door / quarter glass

`slot_uvs` maps each converted window by its own geometry onto its slot, so state k
looks like a vanilla window in state k. Forza's own window UVs, squashed into the
slots, draw the slot layouts across the window in any crack state (the M5's rear
window in state 3: a hole with a band of shattered glass where the gap between two
hole slots fell).

    uv run dsff glass check <archive> --id N      # lint one converted vehicle
    uv run dsff glass survey <chunk-or-archive>... # state-3 look of every glass panel
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import math
import re
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from dsfforza import REPO

# Shared glass materials (SSIC): GLOBAL_Glass, _LOD1 (vanilla exteriors), _LOD5 (interiors).
EXTERIOR_GLASS = {bytes.fromhex("001283369afd1027"), bytes.fromhex("10122b9953b14e2a")}
INTERIOR_GLASS = bytes.fromhex("0012cc8576953429")
GLASS_TEXTURE = bytes.fromhex("00122b77dffa1227")
BAND = 0.25

# Band-0 rectangles (u0, u1, v0, v1). Each covers its slot's band-3 hole
# (windscreen u 0.18-0.64, side_front 0.70-0.97, rear 0.16-0.63,
# side_rear 0.70-0.98) with a rim, like vanilla windows, and stays clear of
# the slot edges so bilinear filtering never reads the neighbour.
SLOTS = {
    "windscreen": (0.135, 0.665, 0.006, 0.122),
    "side_front": (0.672, 0.994, 0.006, 0.122),
    "rear": (0.170, 0.620, 0.128, 0.244),
    "side_rear": (0.672, 0.994, 0.128, 0.244),
}


# Interior glass (GLOBAL_Glass_LOD5): see "interior (cockpit) glass" below.


# --- which slot a converted glass triangle goes to ---------------------------------

LAMP_MATERIALS = ("glass", "lights_glass", "lights_gls_noemit")  # Forza lamp-cover techniques


def slot_for(section: str, centroid, normal, material: str | None = None) -> tuple[str, str]:
    """(slot, group) for a glass triangle in DSF car space (+z front, +x left).
    Forza names its glass sections glassF (windscreen), glassR (rear window),
    glassLF/RF (front doors), glassLM/RM, glassLR/RR (rear doors, quarter
    lights), glassT (roof), glassLHL/RHL/LTL/RTL (lamp covers); other
    sections (body, trunk, headlight*, bumper*) hold lamp covers or the odd
    window, which go by position and facing. `material` (the Forza technique)
    marks lamp covers outside the lamp sections (`glass` on a trunk lid; the
    same technique on a glassF section is a windscreen). `group` names the
    window the triangle belongs to: the triangles of one group share one
    mapping."""
    n = section.lower()
    side = "l" if centroid[0] > 0 else "r"
    named = {
        "glassf": "windscreen",
        "glassr": "rear",
        "glasslf": "side_front",
        "glassrf": "side_front",
        "glasslm": "side_rear",
        "glassrm": "side_rear",
        "glasslr": "side_rear",
        "glassrr": "side_rear",
        "glasst": "side_front",
    }
    if n in named:
        return named[n], f"{n}/{named[n]}"
    lamp = (
        re.fullmatch(r"glass[lr](hl|tl)", n)
        or "light" in n
        or n.startswith(("bumper", "hood", "mirror"))
        or (material or "").lower() in LAMP_MATERIALS
    )
    if lamp:
        return "side_front", f"{n}/lamp/{side}"
    if abs(normal[2]) >= abs(normal[0]) and abs(normal[2]) >= 0.25 * abs(normal[1]):
        slot = "windscreen" if centroid[2] > 0 else "rear"
        return slot, f"{n}/{slot}"
    if abs(normal[1]) > abs(normal[0]):
        return "side_front", f"{n}/roof"
    slot = "side_front" if centroid[2] > 0 else "side_rear"
    return slot, f"{n}/{slot}/{side}"


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(a):
    length = math.sqrt(_dot(a, a))
    return (a[0] / length, a[1] / length, a[2] / length) if length > 1e-12 else None


class SlotMapper:
    """Planar UVs for converted glass, one frame per window (slot_for group).

    Feed every glass triangle of a section first (`add`, all of them, so the
    damage converter's per-panel calls agree), then `uv(group, point)`.
    The frame: the window's area-weighted normal; u along the car's x
    (windscreen, rear) or -z (side windows, lamps, roof), v down the glass;
    the window's extent in that frame fills the slot rectangle."""

    def __init__(self):
        self._tris: dict[str, list] = defaultdict(list)
        self._slot: dict[str, str] = {}
        self._frames: dict[str, tuple] = {}
        self._normals: dict[str, tuple] = {}

    def classify(self, section: str, pts, material: str | None = None) -> tuple[str, str]:
        c = tuple(sum(p[a] for p in pts) / 3 for a in range(3))
        nrm = _cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0]))
        return slot_for(section, c, nrm, material)

    def add(self, section: str, pts, material: str | None = None) -> str:
        slot, group = self.classify(section, pts, material)
        self._slot[group] = slot
        self._tris[group].append(pts)
        self._frames.pop(group, None)
        return group

    def _frame(self, group: str):
        if group in self._frames:
            return self._frames[group]
        slot, tris = self._slot[group], self._tris[group]
        n = [0.0, 0.0, 0.0]
        for p in tris:
            c = _cross(_sub(p[1], p[0]), _sub(p[2], p[0]))
            # Consistent orientation: outward-ish = away from the car's centre line.
            centre = tuple(sum(q[a] for q in p) / 3 for a in range(3))
            if _dot(c, (centre[0], 0.3 * centre[1], 0.5 * centre[2])) < 0:
                c = (-c[0], -c[1], -c[2])
            for a in range(3):
                n[a] += c[a]
        normal = _unit(n) or (0.0, 1.0, 0.0)
        along = (1.0, 0.0, 0.0) if slot in ("windscreen", "rear") else (0.0, 0.0, -1.0)
        a_axis = _unit(_sub(along, tuple(normal[i] * _dot(along, normal) for i in range(3))))
        if a_axis is None:  # facing along `along` (a lamp cover facing forward): fall back to x
            along = (1.0, 0.0, 0.0)
            a_axis = _unit(_sub(along, tuple(normal[i] * _dot(along, normal) for i in range(3)))) or (1.0, 0.0, 0.0)
        b_axis = _unit(_cross(normal, a_axis)) or (0.0, -1.0, 0.0)
        down = -b_axis[1] if abs(b_axis[1]) > 0.2 else -b_axis[2]  # flat roofs: v toward the rear
        if down < 0:
            b_axis = (-b_axis[0], -b_axis[1], -b_axis[2])
        s = [_dot(q, a_axis) for p in tris for q in p]
        t = [_dot(q, b_axis) for p in tris for q in p]
        frame = (SLOTS[slot], a_axis, b_axis, min(s), max(s), min(t), max(t))
        self._frames[group] = frame
        self._normals[group] = normal
        return frame

    def normal(self, group: str) -> tuple:
        """The window's outward (away from the car's centre) unit normal."""
        self._frame(group)
        return self._normals[group]

    def slot(self, group: str) -> str:
        return self._slot[group]

    def uv(self, group: str, point) -> tuple[float, float]:
        (u0, u1, v0, v1), a_axis, b_axis, s0, s1, t0, t1 = self._frame(group)
        s, t = _dot(point, a_axis), _dot(point, b_axis)
        fs = (s - s0) / (s1 - s0) if s1 - s0 > 1e-6 else 0.5
        ft = (t - t0) / (t1 - t0) if t1 - t0 > 1e-6 else 0.5
        fs, ft = min(1.0, max(0.0, fs)), min(1.0, max(0.0, ft))
        return u0 + (u1 - u0) * fs, v0 + (v1 - v0) * ft


# --- interior (cockpit) glass ------------------------------------------------------
#
# GLOBAL_Glass_LOD5, the interior's glass material, samples the same texture
# with the same crack-state shift (programs 61 and 63: v += 0.25 * floor(
# c96[s9.y].y); 60 and 62 have no shift), but vanilla interiors use the left
# strip u 0-0.125 only (64 texels of the 512-wide texture), cut into four
# blocks whose texels show in different states (blue >= 0.01 survives the
# texkill; share of each block's texels per state 0/1/2/3, measured on the
# texture, research/vehicle-model.md "Interior glass"):
#
#   surface      u 0-0.125,      v 0-0.0625      1.00 1.00 1.00 0.01  smear: a faint
#                reflective layer in states 0-2. Side and rear windows are covered;
#                the windscreen only along its top and pillars, its centre is open.
#   crack        u 0-0.0625,     v 0.125-0.25    0.01 0.55 1.00 0.00  crack lines:
#                rows v 0.18-0.25 from state 1, the rest in state 2.
#   shard_dull   u 0-0.125,      v 0.0625-0.125  0.04 0.03 0.04 0.96  rim shards,
#   shard_bright u 0.0625-0.125, v 0.125-0.25    0.01 0.03 0.09 0.94  state 3 only.
#
# Every vanilla interior window carries its own crack and shard geometry, made by
# DSF's artists (#459: on the vanilla Gallardo 212 and Charger 176 the shattered
# windscreen keeps a few irregular clusters of slivers at its corners and edges,
# 354 and 659 shard triangles, 3 mm wide at the median). InteriorGlass builds the
# surface from the converted window's shape and takes the cracks and shards of the
# vanilla interior window that matches it best (donor_library, mismatch), placed on
# the converted window's rim where the vanilla window has them on its own. The crack lines and bright shards are fins
# and slab walls standing 3 to 5 mm across the glass, not flat slivers, and
# their vertex normals are their own triangles' facing; both come along with them
# (research/vehicle-model.md, "Crack and shard fins", "Crack and shard normals").
INTERIOR_BLOCKS = {
    "surface": (0.0, 0.125, 0.0, 0.0625),
    "crack": (0.0, 0.0625, 0.125, 0.25),
    "shard_dull": (0.0, 0.125, 0.0625, 0.125),
    "shard_bright": (0.0625, 0.125, 0.125, 0.25),
}
SMEAR_RECT = (0.012, 0.108, 0.008, 0.052)  # surface, inset from the strip edges (bilinear and wrap bleed)
# The windscreen surface is kept only outside this box (normalised s across,
# t down the glass from the top): the vanilla frame of smear, open centre.
WINDSCREEN_OPEN = (0.06, 0.94, 0.22)
WINDSCREEN_OPEN_AREA = 0.3  # m2: smaller windscreens keep their whole surface
CELL = 0.01  # occupancy grid of a window in its own plane (m)
LIFT = 0.002  # cracks and shards sit this far inside the glass (m)
MIN_WINDOW_AREA = 0.02  # smaller panes (and lamp covers) get the surface only
RIM_BINS = 180  # directions round a window's centre in which its rim is measured (2 degrees each)
SIDE_SPLIT = 0.2  # m: a vanilla side-glass record with triangles this far out on both sides holds two panes
# Host crack and shard triangles that share a vertex move as one piece. Interior
# positions are int16 x, y, z in steps of 1/4096 m (vehicle_mesh.POSITION_SCALE). Two
# corners are one vertex when no coordinate differs by more than one step: on vanilla
# windscreens that share these triangles, corners that decode bit-identical (7.4M) or
# one step apart (67k) stay together on the partner window (in-plane median 0, p99
# 0.0 and 0.5 mm); two steps apart, 2.8% move more than 5 mm apart
# (research/vehicle-model.md, "Broken glass moves in pieces").
PIECE_STEP = 1 / 4096


def interior_block(uvs, tol: float = 0.001) -> str | None:
    """The INTERIOR_BLOCKS key all of a triangle's UVs fall in, else None."""
    for name, (u0, u1, v0, v1) in INTERIOR_BLOCKS.items():
        if all(u0 - tol <= u <= u1 + tol and v0 - tol <= v <= v1 + tol for u, v in uvs):
            return name
    return None


def _lerp(a, b, w):
    return tuple(a[i] + (b[i] - a[i]) * w for i in range(len(a)))


def _lerp_vertex(a, b, w):
    """(pos, normal, sky) vertices of a planar polygon, linear along an edge."""
    return (_lerp(a[0], b[0], w), _unit(_lerp(a[1], b[1], w)) or a[1], a[2] + (b[2] - a[2]) * w)


def _split(poly, f):
    """Cut a convex polygon of (pos, normal, sky) by the sign of f(pos):
    (part where f < 0, part where f >= 0), each None if empty."""
    neg, pos = [], []
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        fa, fb = f(a[0]), f(b[0])
        (neg if fa < 0 else pos).append(a)
        if (fa < 0) != (fb < 0):
            m = _lerp_vertex(a, b, fa / (fa - fb))
            neg.append(m)
            pos.append(m)
    return (neg if len(neg) >= 3 else None), (pos if len(pos) >= 3 else None)


def _bary(q, p):
    (ax, ay), (bx, by), (cx, cy) = q
    d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(d) < 1e-12:
        return None
    w0 = ((by - cy) * (p[0] - cx) + (cx - bx) * (p[1] - cy)) / d
    w1 = ((cy - ay) * (p[0] - cx) + (ax - cx) * (p[1] - cy)) / d
    return (w0, w1, 1.0 - w0 - w1)


class _Pane:
    """One window in its own plane (s, t metres along the frame axes): an
    occupancy grid, its rim, and points lifted back onto the glass."""

    def __init__(self, items, a_axis, b_axis, outward):
        self.a, self.b, self.out = a_axis, b_axis, outward
        self.tris = []
        for pts, normals, _, _ in items:
            q = [(_dot(p, a_axis), _dot(p, b_axis)) for p in pts]
            if abs((q[1][0] - q[0][0]) * (q[2][1] - q[0][1]) - (q[1][1] - q[0][1]) * (q[2][0] - q[0][0])) < 1e-9:
                continue  # edge-on (the glass's thickness rim)
            self.tris.append((q, pts, normals))
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        if not self.tris:
            self.s0 = self.t0 = 0.0
            self.area, self.rim, self.filled = 0.0, [], set()
            return
        self.s0 = min(x for q, _, _ in self.tris for x, _ in q) - 2 * CELL
        self.t0 = min(y for q, _, _ in self.tris for _, y in q) - 2 * CELL
        for k, (q, _, _) in enumerate(self.tris):
            i0, i1 = self._i(min(x for x, _ in q)), self._i(max(x for x, _ in q))
            j0, j1 = self._j(min(y for _, y in q)), self._j(max(y for _, y in q))
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    w = _bary(q, self._centre(i, j))
                    if w and min(w) >= -1e-6:
                        self.cells[(i, j)].append(k)
        if not self.cells:
            # Every triangle too small to cover a cell centre (a sliver of lamp
            # glass): no area, so the window gets its surface only (max() over
            # the empty grid would raise).
            self.area, self.rim, self.filled = 0.0, [], set()
            return
        # Outside = empty cells reachable from the grid's border; gaps inside the
        # pane (cracks between Forza triangles, cut-outs) count as glass, so
        # neither rim shards nor crack stops appear in the middle of a window.
        ni = max(i for i, _ in self.cells) + 3
        nj = max(j for _, j in self.cells) + 3
        outside, todo = set(), [(0, 0)]
        while todo:
            c = todo.pop()
            if c in outside or c in self.cells or not (-1 <= c[0] <= ni and -1 <= c[1] <= nj):
                continue
            outside.add(c)
            todo += [(c[0] + 1, c[1]), (c[0] - 1, c[1]), (c[0], c[1] + 1), (c[0], c[1] - 1)]
        self.filled = {(i, j) for i in range(ni + 1) for j in range(nj + 1) if (i, j) not in outside}
        self.area = len(self.filled) * CELL * CELL
        self.rim = []  # (cell centre, unit direction into the window)
        for i, j in self.filled:
            if all((i + di, j + dj) in self.filled for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                continue
            ds = dt = 0.0
            for di in range(-4, 5):
                for dj in range(-4, 5):
                    if (i + di, j + dj) in self.filled:
                        ds, dt = ds + di, dt + dj
            length = math.hypot(ds, dt)
            if length > 1e-6:
                self.rim.append((self._centre(i, j), (ds / length, dt / length)))
        self.rim.sort()

    def _i(self, s):
        return int((s - self.s0) / CELL)

    def _j(self, t):
        return int((t - self.t0) / CELL)

    def _centre(self, i, j):
        return (self.s0 + (i + 0.5) * CELL, self.t0 + (j + 0.5) * CELL)

    def inside(self, p) -> bool:
        return (self._i(p[0]), self._j(p[1])) in self.filled

    def near(self, p) -> bool:
        """Inside, or in a cell next to one inside: the grid knows the rim to a cell."""
        i, j = self._i(p[0]), self._j(p[1])
        return any((i + di, j + dj) in self.filled for di in (-1, 0, 1) for dj in (-1, 0, 1))

    def lift(self, p):
        """(position, normal) on the glass under plane point p, LIFT inside it;
        just outside the pane the nearest triangle's plane is extended. Where
        the glass has two layers, the inner one."""
        i, j = self._i(p[0]), self._j(p[1])
        best = None
        for r in range(4):
            seen = set()
            for di in range(-r, r + 1):
                for dj in range(-r, r + 1):
                    if max(abs(di), abs(dj)) == r:
                        seen.update(self.cells.get((i + di, j + dj), ()))
            for k in seen:
                q, pts, normals = self.tris[k]
                w = _bary(q, p)
                if w is None:
                    continue
                miss = max(0.0, -min(w))
                pos = tuple(sum(w[n] * pts[n][c] for n in range(3)) for c in range(3))
                wc = [max(0.0, x) for x in w]
                total = sum(wc) or 1.0
                nrm = _unit(tuple(sum(wc[n] / total * normals[n][c] for n in range(3)) for c in range(3)))
                key = (round(miss, 4), _dot(pos, self.out))
                if best is None or key < best[0]:
                    best = (key, pos, nrm or self.out)
            if best is not None and best[0][0] == 0:
                break
        if best is None:
            return None
        _, pos, nrm = best
        return tuple(pos[c] - LIFT * self.out[c] for c in range(3)), nrm


def _rotate(d, angle):
    c, s = math.cos(angle), math.sin(angle)
    return (d[0] * c - d[1] * s, d[0] * s + d[1] * c)


class _Rim:
    """A pane's rim seen from the centre of its box: in each of RIM_BINS
    directions, how far out the glass reaches, in units of the box's half
    extents (1 on the box's edges)."""

    def __init__(self, pane: _Pane):
        cells = [pane._centre(i, j) for i, j in pane.filled]
        s = [c[0] for c in cells]
        t = [c[1] for c in cells]
        half = CELL / 2  # cell centres: the glass reaches half a cell further
        self.centre = ((min(s) + max(s)) / 2, (min(t) + max(t)) / 2)
        self.half = ((max(s) - min(s)) / 2 + half, (max(t) - min(t)) / 2 + half)
        reach = [None] * RIM_BINS
        for c in cells:
            theta, rho = self.polar(c)
            k = self.bin(theta)
            reach[k] = max(rho, reach[k] or 0.0)
        known = [k for k in range(RIM_BINS) if reach[k] is not None]
        for k in range(RIM_BINS):  # directions with no cell (a notch, a gap): the nearest measured one
            if reach[k] is None:
                reach[k] = reach[min(known, key=lambda n: min(abs(n - k), RIM_BINS - abs(n - k)))]
        self.reach = reach

    def polar(self, p) -> tuple[float, float]:
        x = (p[0] - self.centre[0]) / self.half[0]
        y = (p[1] - self.centre[1]) / self.half[1]
        return math.atan2(y, x), math.hypot(x, y)

    @staticmethod
    def bin(theta: float) -> int:
        return int((theta + math.pi) / (2 * math.pi) * RIM_BINS) % RIM_BINS

    def ray(self, theta: float) -> tuple[float, float]:
        """Metres per unit of reach in direction theta."""
        return (math.cos(theta) * self.half[0], math.sin(theta) * self.half[1])

    @classmethod
    def of(cls, centre, half, reach) -> _Rim:
        """A rim from its stored centre, half extents and reach (DonorSet)."""
        rim = cls.__new__(cls)
        rim.centre, rim.half, rim.reach = tuple(centre), tuple(half), list(reach)
        return rim


def _carry(hrim: _Rim, rim: _Rim, g) -> tuple:
    """Where a host piece with centroid g (host plane metres) goes on a window with
    rim `rim`: the same direction from the box centre (in units of the box's half
    extents, so corners go to corners) and the same depth inside the rim, in
    metres. (along, centroid on the window, turn): along < 0 is past the window's
    centre; turn is the change of the direction's angle in metres, by which the
    piece turns as one body."""
    theta, rho = hrim.polar(g)
    k = _Rim.bin(theta)
    ray_h, ray_p = hrim.ray(theta), rim.ray(theta)
    depth = (hrim.reach[k] - rho) * math.hypot(*ray_h)  # metres inside the host's rim
    along = rim.reach[k] - depth / (math.hypot(*ray_p) or 1.0)
    gp = (rim.centre[0] + along * ray_p[0], rim.centre[1] + along * ray_p[1])
    turn = math.atan2(ray_p[1], ray_p[0]) - math.atan2(ray_h[1], ray_h[0])
    return along, gp, turn


@dataclass(eq=False)
class HostWindow:
    """One window of a vanilla interior (an s9 group of its glass): every
    triangle as (points, UVs, sky terms, normals), which of them are its crack
    and shard layers (interior_block crack, shard_dull, shard_bright), its kind
    (host_kinds: "front", "rear" or "side") and the vanilla car (spool ID) whose
    interior it is. Normals may be left out (three-element triangles): the
    triangle's own facing stands in, as vanilla stores it (_own_normals)."""

    s9: tuple[int, int]
    tris: list
    overlay: list
    kind: str = ""
    car: int | None = None

    @cached_property
    def centre(self) -> tuple:
        pts = [p for t in self.tris for p in t[0]]
        return tuple(sum(p[a] for p in pts) / len(pts) for a in range(3))

    @cached_property
    def sideways(self) -> bool:
        return _sideways([t[0] for t in self.tris])

    @cached_property
    def side(self) -> int | None:
        """+1 on the car's left (+x), -1 on its right, for side glass; 0 for the
        others; None for one record holding panes on both sides (quarter lights
        on the chassis record): its pieces would land on one window from two."""
        if not self.sideways:
            return 0
        xs = [sum(p[0] for p in t[0]) / 3 for t in self.tris]
        if min(xs) < -SIDE_SPLIT and max(xs) > SIDE_SPLIT:
            return None
        return 1 if self.centre[0] > 0 else -1

    @cached_property
    def frame(self) -> tuple:
        """(u axis, v axis, outward normal, _Pane) of the window, framed like a
        converted window of its kind (SlotMapper: u along x for windscreens and
        rear windows, along -z for side glass; v down the glass)."""
        frames = SlotMapper()
        section = "glasslf" if self.sideways else "glassf"
        for tri in self.tris:
            group = frames.add(section, tri[0])
        _, a_axis, b_axis, *_ = frames._frame(group)
        outward = frames.normal(group)
        items = [(tri[0], (outward,) * 3, tri[2], b"") for tri in self.tris]
        return a_axis, b_axis, outward, _Pane(items, a_axis, b_axis, outward)

    @cached_property
    def rim(self):
        pane = self.frame[3]
        return _Rim(pane) if pane.filled else None

    @cached_property
    def pieces(self) -> list[list[int]]:
        return _pieces([tri[0] for tri in self.overlay])

    @cached_property
    def fin_heights(self) -> list[list[float]]:
        return _fin_heights([tri[0] for tri in self.overlay], self.frame[2])


def _sideways(tris) -> bool:
    """A side window (facing along x) rather than a windscreen, rear window or
    roof glass: the larger part of its area projects onto the car's y-z plane."""
    area = [0.0, 0.0, 0.0]
    for p in tris:
        c = _cross(_sub(p[1], p[0]), _sub(p[2], p[0]))
        for a in range(3):
            area[a] += abs(c[a])
    return area[0] >= max(area[1], area[2])


def host_kinds(windows: list[HostWindow]) -> list[HostWindow]:
    """Set each host window's kind: "side" when it faces sideways (_sideways);
    of the others, the frontmost (largest centre z) is the windscreen ("front")
    and the rest are "rear". Vanilla interiors don't sit their windscreen on a
    fixed side of z = 0 (on the 212 at z +1.0, on the 248 at -0.04), so the order
    decides, not the sign."""
    ahead = [w for w in windows if not w.sideways]
    front = max(ahead, key=lambda w: w.centre[2], default=None)
    for w in windows:
        w.kind = "side" if w.sideways else "front" if w is front else "rear"
    return windows


def host_windows(stream, vehc, car: int | None = None) -> list[HostWindow]:
    """The vanilla interior's glass (GLOBAL_Glass_LOD5 in part 0, mask 64) as
    windows, one per s9 group: vanilla binds each window to its own panel
    record (windscreen (0, 5) on the Desanne, rear (0, 20), doors)."""
    from dsfforza.convert.damage import load_mesh
    from dsfforza.convert.damage.core import _angles_of, _normal_of
    from dsfforza.formats import vehicle_mesh as vm

    mesh = load_mesh(stream, vehc)
    chosen = {k for lod in mesh.parts[0].lods if lod.mask == 64 for k in lod.submodels}
    groups: dict[tuple, list] = defaultdict(list)
    for k in sorted(chosen):
        sub = mesh.submodels[k]
        vb = mesh.vertex_buffers.get(sub.vertex_buffer)
        if sub.material != INTERIOR_GLASS or vb is None:
            continue
        ib = mesh.index_buffers[sub.index_buffer]
        e0, e5, e8, e9 = vb.elements[0], vb.element(5), vb.element(8), vb.element(9)
        for t in range(sub.first_index, sub.first_index + sub.index_count, 3):
            idx = [sub.base_vertex + struct.unpack_from("<H", ib, (t + j) * 2)[0] for j in range(3)]
            pts = tuple(vm.position(vb, i) for i in idx)
            uvs = tuple(tuple(vb.read(e8, i)[:2]) for i in idx)
            skies = tuple(vb.read(e0, i)[3] / 4096 for i in idx)
            # Semantic 5 bytes 0, 1: the normal's angles, decoded as the vertex shader does.
            normals = tuple(_normal_of(*_angles_of(*vb.read(e5, i)[:2])) for i in idx) if e5 else None
            groups[tuple(vb.read(e9, idx[0])[:2])].append((pts, uvs, skies, normals))
    return host_kinds(
        [
            HostWindow(s9, tris, [t for t in tris if interior_block(t[1]) not in (None, "surface")], car=car)
            for s9, tris in sorted(groups.items())
        ]
    )


# --- donors: the vanilla windows whose crack and shard sets ports take (#459) ---------
#
# DSF's crack and shard sets are hand-made, one per window, and DSF reused them between
# windows by moving them as pieces (research/vehicle-model.md, "Broken glass moves in
# pieces"). A converted window takes the set of the vanilla window, of all vanilla
# interiors' windows of its kind, that the move changes least (mismatch; research/
# vehicle-model.md, "Which vanilla window a converted window takes its set from").

DONOR_CACHE_ENV = "DSFF_GLASS_DONOR_CACHE"  # a folder, or "off"; default out/cache/glass-donors
DONOR_CACHE_VERSION = 1  # raise when what donor_set makes changes through code outside this file


@dataclass(frozen=True)
class DonorSet:
    """What choosing a donor needs of a vanilla window with a crack and shard set,
    in the window's own frame (HostWindow.frame): its rim (centre and half extents
    of its box, reach per direction) and per piece (_pieces) the centroid (s, t
    metres), the number of corners and their mean squared distance from it."""

    car: int | None
    s9: tuple
    kind: str
    side: int
    centre: tuple
    half: tuple
    reach: tuple
    pieces: tuple

    @cached_property
    def rim(self) -> _Rim:
        return _Rim.of(self.centre, self.half, self.reach)


def donor_set(host: HostWindow) -> DonorSet | None:
    """The DonorSet of a vanilla window, None when it has no crack and shard set,
    no area, or holds panes on both sides of the car."""
    if not host.overlay or host.side is None or host.rim is None:
        return None
    ha, hb = host.frame[:2]
    flat = [[(_dot(p, ha), _dot(p, hb)) for p in tri[0]] for tri in host.overlay]
    pieces = []
    for piece in host.pieces:
        corners = [c for n in piece for c in flat[n]]
        gs, gt = sum(x for x, _ in corners) / len(corners), sum(y for _, y in corners) / len(corners)
        r2 = sum((x - gs) ** 2 + (y - gt) ** 2 for x, y in corners) / len(corners)
        pieces.append((gs, gt, len(corners), r2))
    rim = host.rim
    return DonorSet(
        host.car, tuple(host.s9), host.kind, host.side, rim.centre, rim.half, tuple(rim.reach), tuple(pieces)
    )


def mismatch(donor: DonorSet, rim: _Rim) -> tuple[float, float]:
    """(RMS distance, metres, by which moving the donor's set onto a window with rim
    `rim` moves its crack and shard corners; share of the corners whose piece goes
    past the window's centre and is left out).

    The move is _overlay's: each piece goes rigidly to the same direction and depth
    inside the rim (_carry). A corner at r from its piece's centroid moves by the
    centroid's shift d (relative to the windows' box centres) plus the turn phi of
    r, so over a piece's corners, whose r average 0, the mean squared move is
    |d|^2 + 2 (1 - cos phi) mean |r|^2. A window of the donor's own shape moves
    nothing (0); a larger, smaller or differently shaped one moves the pieces with
    its rim and turns them with its aspect. Left-out pieces count with the move
    that takes them past the centre."""
    hrim = donor.rim
    total = squared = dropped = 0.0
    for gs, gt, n, r2 in donor.pieces:
        along, gp, turn = _carry(hrim, rim, (gs, gt))
        ds = (gp[0] - rim.centre[0]) - (gs - hrim.centre[0])
        dt = (gp[1] - rim.centre[1]) - (gt - hrim.centre[1])
        squared += n * (ds * ds + dt * dt + 2.0 * (1.0 - math.cos(turn)) * r2)
        total += n
        dropped += n * (along < 0)
    return math.sqrt(squared / total), dropped / total


class Donors:
    """The vanilla windows a port's converted windows choose their crack and shard sets
    from (DonorSet), and which of them this port has taken: a set goes to one window of
    the port only, as no vanilla interior carries one set on two windows. One Donors per
    port, shared by its InteriorGlass (the cockpit's sections, then the shell's).
    `windows`: HostWindows to choose from (tests; host_kinds is applied per car);
    `sets` and `load` ((car, s9) -> HostWindow): donor_library's. `eye_z`: the
    driver's eye (InteriorGlass._kind); None puts no side glass behind it."""

    def __init__(
        self,
        windows: list[HostWindow] | None = None,
        sets: list[DonorSet] | None = None,
        load=None,
        eye_z: float | None = None,
    ):
        self.eye_z = eye_z
        self.taken: set[tuple] = set()
        self._windows: dict[tuple, HostWindow] = {}
        if windows is not None:
            by_car: dict = defaultdict(list)
            for w in windows:
                by_car[w.car].append(w)
            for ws in by_car.values():
                if any(not w.kind for w in ws):
                    host_kinds(ws)
            sets = []
            for w in windows:
                d = donor_set(w)
                if d is not None:
                    sets.append(d)
                    self._windows[(d.car, d.s9)] = w
        self.sets = sets or []
        self._load = load

    def fresh(self, eye_z: float | None = None) -> Donors:
        """The same windows, none taken, for one conversion attempt whose driver's eye
        is at z = eye_z (DSF model space, as the converted glass)."""
        out = Donors(sets=self.sets, load=self._load, eye_z=eye_z)
        out._windows = self._windows
        return out

    def candidates(self, kind: str, side: int) -> list[DonorSet]:
        return [d for d in self.sets if d.kind == kind and d.side == side and (d.car, d.s9) not in self.taken]

    def window(self, donor: DonorSet) -> HostWindow:
        key = (donor.car, donor.s9)
        if key not in self._windows:
            self._windows[key] = self._load(*key)
        return self._windows[key]


_LIBRARY: dict = {}  # cache key -> (DonorSets, {car: interior entry})


def _donor_cache_dir() -> Path | None:
    import os

    value = os.environ.get(DONOR_CACHE_ENV, "")
    if value.lower() == "off":
        return None
    return Path(value) if value else REPO / "out" / "cache" / "glass-donors"


def donor_library(stream) -> Donors:
    """Every vanilla interior window with a crack and shard set (DonorSet), from the
    vanilla archive `stream` (dngvehicles.sp). The sets take about a minute to make
    and depend only on the archive and this file, so they are kept in
    out/cache/glass-donors under a key of both (the archive by size and modification
    time), and in memory for the process. Files are written under a temporary name and
    renamed: parallel workers may write the same key but never read half a file."""
    import hashlib
    import os
    import pickle

    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import payload
    from dsfforza.formats.vehicles import find, parse_sslp

    st = os.fstat(stream.fileno())
    h = hashlib.blake2b(digest_size=12)
    h.update(f"{DONOR_CACHE_VERSION} {sys.version_info[:2]} {st.st_size} {st.st_mtime_ns}".encode())
    h.update(Path(__file__).read_bytes())
    key = h.hexdigest()
    if key not in _LIBRARY:
        root = read_chunk(stream, 0)
        ssic = find(root.entries, "SSIC")
        records = parse_sslp(payload(stream, find(find(ssic.children, "NONR").children, "SSLP")))
        interiors = {sid - 1000: root.entries[i] for sid, i, kind in records if kind == 1}
        folder = _donor_cache_dir()
        path = folder / f"{key}.pkl" if folder else None
        sets = None
        if path is not None and path.exists():
            with contextlib.suppress(OSError, pickle.UnpicklingError, EOFError):
                sets = pickle.loads(path.read_bytes())
        if sets is None:
            sets = [
                d
                for car in sorted(interiors)
                for w in host_windows(stream, interiors[car], car)
                if (d := donor_set(w)) is not None
            ]
            if path is not None:
                # The cache only saves time: a folder that can't be written leaves
                # the sets in memory and the conversion goes on.
                tmp = path.with_suffix(f".{os.getpid()}.tmp")
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    tmp.write_bytes(pickle.dumps(sets))
                    tmp.replace(path)
                except OSError:
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)
        _LIBRARY[key] = (sets, interiors)
    sets, interiors = _LIBRARY[key]
    loaded: dict = {}

    def load(car, s9):
        if car not in loaded:
            loaded[car] = {tuple(w.s9): w for w in host_windows(stream, interiors[car], car)}
        return loaded[car][s9]

    return Donors(sets=list(sets), load=load)


class InteriorGlass:
    """Interior (cockpit) glass laid out like vanilla interiors (see above).

    Feed every glass triangle of the entry with `add` (DSF positions, normals,
    sky terms, s9 bytes); `triangles()` then gives, per window (SlotMapper
    group): the surface (smear rows, planar per window; the windscreen's
    centre cut out), and the crack and shard layers of the vanilla window it
    takes them from (`host`: a Donors, donor_library's, or a list of
    HostWindows to choose from), all bound to the window's own s9 so they
    follow its panel's crack state. A Donors shared by several InteriorGlass
    (the cockpit's sections, then the shell's) gives each vanilla window to one
    window. Every donor triangle that lands on its window is kept, seen from
    the driver's eye or not: the donor draws all its own window layers."""

    def __init__(self, host: Donors | list[HostWindow] | None = None):
        self.frames = SlotMapper()
        self.items: dict[str, list] = defaultdict(list)
        self.donors = host if isinstance(host, Donors) else Donors(windows=list(host or []))
        self.report: dict[str, dict] = {}

    def add(self, section: str, pts, normals, skies, s9: bytes, material: str | None = None) -> str:
        group = self.frames.add(section, pts, material)
        self.items[group].append((tuple(pts), tuple(normals), tuple(skies), s9))
        return group

    def panes(self) -> dict[str, _Pane]:
        """Each window (group) in its own plane."""
        panes = {}
        for group in sorted(self.items):
            _, a_axis, b_axis, *_ = self.frames._frame(group)
            panes[group] = _Pane(self.items[group], a_axis, b_axis, self.frames.normal(group))
        return panes

    def triangles(self) -> list[tuple[list, str]]:
        """[(three (position, uv, normal, sky, s9) vertices, region)], region a
        key of INTERIOR_BLOCKS."""
        panes = self.panes()
        donors = self._match(
            {g: pane for g, pane in panes.items() if "/lamp/" not in g and pane.area >= MIN_WINDOW_AREA and pane.rim}
        )
        out = []
        for group in sorted(self.items):
            out += self._window(group, panes[group], donors.get(group))
        return out

    def _window(self, group: str, pane: _Pane, donor: tuple | None) -> list:
        from collections import Counter

        items = self.items[group]
        _, a_axis, b_axis, s0, s1, t0, t1 = self.frames._frame(group)
        slot = self.frames.slot(group)
        ws, wt = max(s1 - s0, 1e-6), max(t1 - t0, 1e-6)
        u0, u1, v0, v1 = SMEAR_RECT
        k = min((u1 - u0) / ws, (v1 - v0) / wt)  # isotropic: the smear's clouds keep their shape
        uc, vc, sc, tc = (u0 + u1) / 2, (v0 + v1) / 2, (s0 + s1) / 2, (t0 + t1) / 2

        def surface_uv(p):
            s, t = _dot(p, a_axis), _dot(p, b_axis)
            return (min(u1, max(u0, uc + (s - sc) * k)), min(v1, max(v0, vc + (t - tc) * k)))

        lamp = "/lamp/" in group
        s9 = Counter(it[3] for it in items).most_common(1)[0][0]
        cut = slot == "windscreen" and not lamp and pane.area >= WINDSCREEN_OPEN_AREA
        lo, hi, top = WINDSCREEN_OPEN
        cuts = [
            lambda p: (_dot(p, a_axis) - s0) / ws - lo,
            lambda p: hi - (_dot(p, a_axis) - s0) / ws,
            lambda p: (_dot(p, b_axis) - t0) / wt - top,
        ]
        out, kept = [], 0.0
        for pts, normals, skies, tri_s9 in items:
            polys = [list(zip(pts, normals, skies))]
            if cut:  # keep the frame: left of lo, right of hi, above top; drop the rest
                frame, rest = [], polys[0]
                for f in cuts:
                    if rest is None:
                        break
                    piece, rest = _split(rest, f)
                    if piece:
                        frame.append(piece)
                polys = frame
            for poly in polys:
                for n in range(1, len(poly) - 1):
                    tri = (poly[0], poly[n], poly[n + 1])
                    out.append(([(p, surface_uv(p), nrm, sky, tri_s9) for p, nrm, sky in tri], "surface"))
                    c = _cross(_sub(tri[1][0], tri[0][0]), _sub(tri[2][0], tri[0][0]))
                    kept += 0.5 * math.sqrt(_dot(c, c))
        info = {
            "slot": slot,
            "s9": list(s9[:2]),
            "area": round(pane.area, 3),
            "surface_m2": round(kept, 3),
            "triangles_in": len(items),
        }
        self.report[group] = info
        if donor is None:
            return out
        chosen, rms = donor
        overlay = self._overlay(pane, self.donors.window(chosen), s9)
        if not overlay:
            return out
        regions = Counter(region for _, region in overlay)
        info.update(
            {
                "host_window": list(chosen.s9),
                "donor_car": chosen.car,
                "donor_rms_mm": round(rms * 1000, 1),
                "crack_triangles": regions["crack"],
                "shard_triangles": regions["shard_dull"] + regions["shard_bright"],
            }
        )
        return out + overlay

    def _kind(self, group: str) -> tuple[str, int] | None:
        """(kind, side) of the vanilla windows a converted window takes its set from:
        ("side", +1 left or -1 right) for side glass, ("front", 0) for the windscreen,
        ("rear", 0) for the rear window; None for roof glass and for side glass whose
        front edge is behind the driver's eye (Donors.eye_z), which takes none.
        Vanilla gives a set to 267 of the 286 side windows that reach the eye or lie
        ahead of it (the 19 others are mostly passenger doors of vans and buses whose
        driver's door has one) and to 2 of the 74 wholly behind it, which mostly
        sample the exterior glass's side-rear slot instead; 141 of 144 windscreens and
        117 of 138 rear windows have one (research/vehicle-model.md, "Which vanilla
        window a converted window takes its set from"). Forza's section names don't
        say it: a vent window is glassLF and the door beside the driver glassLM."""
        tris = [it[0] for it in self.items[group]]
        if "/roof" in group:
            return None
        if _sideways(tris):
            eye = self.donors.eye_z
            if eye is not None and max(p[2] for t in tris for p in t) < eye:
                return None
            x = sum(p[0] for t in tris for p in t)
            return "side", 1 if x > 0 else -1
        kind = {"windscreen": "front", "rear": "rear"}.get(self.frames.slot(group))
        return (kind, 0) if kind else None

    def _match(self, panes: dict[str, _Pane]) -> dict[str, tuple[DonorSet, float]]:
        """Window group -> (the vanilla window whose cracks and shards it takes, the
        RMS move in metres): of the vanilla windows of its kind and side not yet
        taken (_kind, Donors), the one whose set the move onto this window changes
        least (mismatch). Pairs go best first, and each vanilla window to one window."""
        pairs = []
        for group, pane in panes.items():
            kind = self._kind(group)
            if kind is None:
                continue
            rim = _Rim(pane)
            for d in self.donors.candidates(*kind):
                pairs.append((mismatch(d, rim)[0], group, -1 if d.car is None else d.car, d.s9, d))
        out = {}
        for rms, group, _, _, d in sorted(pairs, key=lambda t: t[:4]):
            if group not in out and (d.car, d.s9) not in self.donors.taken:
                out[group] = (d, rms)
                self.donors.taken.add((d.car, d.s9))
        return out

    def _overlay(self, pane: _Pane, host: HostWindow, s9) -> list:
        """The host window's crack and shard triangles on this window. They move
        as their host pieces (_pieces: triangles joined by shared vertices), each
        piece as one rigid body, as vanilla moves them between windows. A piece
        goes to the same direction from the window's centre (in units of the
        box's half extents, so corners go to corners) at the same depth inside
        the rim, in metres, both taken at the piece's centroid, turned with that
        direction; then its triangles are lifted onto the glass. A piece deeper
        than a smaller window reaches (past its centre) is left out; so is a
        triangle that lands outside the window (its centroid outside, or a corner
        more than one cell of the occupancy grid outside it).

        Each vertex keeps the host's normal, turned with its piece (_turn_normal):
        vanilla stores on every crack and shard vertex the facing of its own
        triangle, which is the inward side on two thirds of the windscreen
        shards, and the glass shader's lighting, Fresnel and reflection all
        read it. And each vertex keeps its height above its fin's root
        (_fin_heights): vanilla's crack lines and bright shards are fins and slab
        walls standing 3 to 5 mm across the glass, which lifting every corner
        onto the glass flattened to lines (research/vehicle-model.md, "Crack and
        shard fins")."""
        ha, hb, hn, _ = host.frame
        hrim, fin_heights = host.rim, host.fin_heights
        if hrim is None:
            return []
        rim = _Rim(pane)
        # Frames of opposite handedness mirror the plane coordinates, and with them
        # each triangle's facing (_turn_normal).
        flip = _dot(ha, _cross(hb, hn)) * _dot(pane.a, _cross(pane.b, pane.out)) < 0
        host_frame, frame = (ha, hb, hn), (pane.a, pane.b, pane.out)
        out = []
        flat_tris = [[(_dot(p, ha), _dot(p, hb)) for p in tri[0]] for tri in host.overlay]
        for piece in host.pieces:
            corners_h = [c for n in piece for c in flat_tris[n]]
            g = (sum(x for x, _ in corners_h) / len(corners_h), sum(y for _, y in corners_h) / len(corners_h))
            along, gp, turn = _carry(hrim, rim, g)
            if along < 0:
                continue  # past the centre of this window: it would cross to the far side
            for n in piece:
                tri = host.overlay[n]
                normals = [
                    _turn_normal(m, host_frame, frame, turn, flip) for m in (_host_normals(tri) or _own_normals(tri[0]))
                ]
                out += self._place(pane, tri, flat_tris[n], g, gp, turn, s9, normals, fin_heights[n])
        return out

    @staticmethod
    def _place(pane: _Pane, tri, q, g, gp, turn, s9, normals, heights) -> list:
        """One host triangle (plane points q) of a piece moved from g to gp and
        turned by turn, lifted onto the glass and raised off it by each corner's
        height (metres, along the window's outward normal), with the given vertex
        normals; [] where it lands outside."""
        uvs, skies = tri[1], tri[2]
        flat = []
        for x, y in q:
            d = _rotate((x - g[0], y - g[1]), turn)
            flat.append((gp[0] + d[0], gp[1] + d[1]))
        centroid = (sum(x for x, _ in flat) / 3, sum(y for _, y in flat) / 3)
        if not pane.inside(centroid) or not all(pane.near(c) for c in flat):
            return []
        corners = [pane.lift(c) for c in flat]
        if any(c is None for c in corners):
            return []
        vertices = [
            (tuple(p[i] + h * pane.out[i] for i in range(3)), tuple(uv), nrm, sky, s9)
            for (p, _), uv, sky, nrm, h in zip(corners, uvs, skies, normals, heights)
        ]
        return [(vertices, interior_block(uvs))]


def _fin_heights(tris, outward) -> list[list[float]]:
    """How far each corner of tris (host crack and shard triangles, three points
    each) stands outward (along the window's normal) of its fin's root: of the
    nearest corner it shares a triangle with, 0 where that corner is not lower.

    Vanilla's crack lines and bright shards are strips standing across the glass
    (slab walls: the shards are extruded slabs, one thickness per window, 3.0 to
    5.2 mm on 132, 147, 153, 176, 201 and 286), whose across-glass edge is each
    triangle's shortest: a top's nearest corner is the root under it, a root's
    the top over it. That reference is local to the fin, so it holds between
    sparse vertices and where the window wraps away from its own plane, where
    the glass under a point is not a host vertex near it (research/vehicle-
    model.md, "Crack and shard fins"). Corners within one position step are the
    same vertex (PIECE_STEP, as _pieces)."""
    keys = [[tuple(round(c / PIECE_STEP) for c in p) for p in pts] for pts in tris]
    cells: dict[tuple, list[int]] = defaultdict(list)
    for n, ks in enumerate(keys):
        for k in ks:
            cells[k].append(n)
    out = []
    for pts, ks in zip(tris, keys):
        heights = []
        for p, k in zip(pts, ks):
            best = None
            for d in itertools.product((-1, 0, 1), repeat=3):
                for m in cells.get(tuple(a + b for a, b in zip(k, d)), ()):
                    for q, kq in zip(tris[m], keys[m]):
                        if all(abs(a - b) <= 1 for a, b in zip(k, kq)):
                            continue  # p itself
                        dist = math.dist(p, q)
                        if best is None or dist < best[0]:
                            best = (dist, q)
            heights.append(max(0.0, _dot(p, outward) - _dot(best[1], outward)) if best else 0.0)
        out.append(heights)
    return out


def _host_normals(tri):
    """The host vertex normals of a HostWindow triangle, None when it has none."""
    return tri[3] if len(tri) > 3 and tri[3] is not None else None


def _own_normals(pts):
    """The triangle's own facing (by its winding) at each corner: what vanilla stores
    on its crack and shard vertices (research/vehicle-model.md, "Crack and shard normals")."""
    n = _unit(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0]))) or (0.0, 1.0, 0.0)
    return (n, n, n)


def _turn_normal(m, host_frame, frame, turn, flip):
    """A host normal moved with its piece: its components along the host window's
    frame (u axis, v axis, outward normal), the in-plane part turned by `turn`,
    put back on the converted window's frame. With `flip` (frames of opposite
    handedness: the move is a mirror, which turns every triangle's winding round)
    the normal is reversed too, so it keeps agreeing with its triangle's facing."""
    (ha, hb, hn), (a, b, n) = host_frame, frame
    s, t = _rotate((_dot(m, ha), _dot(m, hb)), turn)
    w = _dot(m, hn)
    sign = -1.0 if flip else 1.0
    return _unit(tuple(sign * (s * a[i] + t * b[i] + w * n[i]) for i in range(3))) or n


def _pieces(tris, step: float = PIECE_STEP) -> list[list[int]]:
    """Indices of tris (three points each, decoded host positions) grouped into
    pieces: triangles that share a vertex (two corners no coordinate of which
    differs by more than one position step) are one piece."""
    parent = list(range(len(tris)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cells: dict[tuple, list[int]] = defaultdict(list)
    for n, tri in enumerate(tris):
        for p in tri:
            key = tuple(round(c / step) for c in p)
            for d in itertools.product((-1, 0, 1), repeat=3):
                for m in cells.get(tuple(a + b for a, b in zip(key, d)), ()):
                    a, b = root(n), root(m)
                    if a != b:
                        parent[a] = b
            cells[key].append(n)
    groups: dict[int, list[int]] = defaultdict(list)
    for n in range(len(tris)):
        groups[root(n)].append(n)
    return list(groups.values())


# --- lint -------------------------------------------------------------------------


def _which_slot(u: float, v: float, tol: float = 0.004) -> str | None:
    for name, (u0, u1, v0, v1) in SLOTS.items():
        if u0 - tol <= u <= u1 + tol and v0 - tol <= v <= v1 + tol:
            return name
    return None


def _glass_triangles(stream, vehc, masks, materials=EXTERIOR_GLASS):
    """(uvs, s9 pairs, points) per glass triangle (`materials`) in part 0 of the given masks."""
    from dsfforza.convert.damage import load_mesh
    from dsfforza.formats import vehicle_mesh as vm

    mesh = load_mesh(stream, vehc)
    buffers, index_buffers, subs = mesh.vertex_buffers, mesh.index_buffers, mesh.submodels
    chosen = {k for lod in mesh.parts[0].lods if lod.mask in masks for k in lod.submodels}
    for k in sorted(chosen):
        sub = subs[k]
        vb = buffers.get(sub.vertex_buffer)
        if sub.material not in materials or vb is None:
            continue
        ib = index_buffers[sub.index_buffer]
        e8, e9 = vb.element(8), vb.element(9)
        for t in range(sub.first_index, sub.first_index + sub.index_count, 3):
            idx = [sub.base_vertex + struct.unpack_from("<H", ib, (t + j) * 2)[0] for j in range(3)]
            yield (
                [tuple(vb.read(e8, i)[:2]) for i in idx],
                [tuple(vb.read(e9, i)[:2]) for i in idx],
                [vm.position(vb, i) for i in idx],
            )


def _interior_region(uvs, tol: float = 0.002) -> str | None:
    """The INTERIOR_BLOCKS key (or "slot:<name>") all three UVs of an
    interior glass triangle fall in, else None."""
    name = interior_block(uvs, tol)
    if name is not None:
        return name
    slots = {_which_slot(u, v) for u, v in uvs}
    if len(slots) == 1 and None not in slots:
        return "slot:" + slots.pop()
    return None


def _uv_signature(tris) -> list:
    return sorted(tuple(sorted((round(u, 4), round(v, 4)) for u, v in uvs)) for uvs, _, _ in tris)


def check(stream, kinds: dict, host: tuple | None = None, window_cracks: bool | None = None) -> list[tuple[bool, str]]:
    """Lint rows (ok, message) for the converted vehicle's glass: exterior
    (full entry part 0 mask 4, exterior entry mask 2) in one panel slot per
    triangle; interior (mask 64) in the vanilla interior layout, with crack
    lines and rim shards (InteriorGlass).

    host: (vanilla stream, the host's {kind: entry}). An interior whose glass is
    the host's own (a stub cockpit keeps the host's interior, cockpit.md) is
    vanilla data, not converted: its layout isn't checked (vanilla interiors don't
    follow it either: the GMC Savana's straddles 448 of 1678 triangles).
    window_cracks: False when the cockpit report says no window took cracks and
    shards (only lamp covers and panes under MIN_WINDOW_AREA, as in open race
    cars and roadsters, or side glass behind the driver's eye); then glass without
    them is expected."""
    rows = []
    for kind, masks in ((0, (4,)), (2, (2,))):
        if kind not in kinds:
            continue
        tris = list(_glass_triangles(stream, kinds[kind], masks))
        outside = sum(1 for uvs, _, _ in tris if any(not (-0.002 <= v <= BAND + 0.002) for _, v in uvs))
        rows.append(
            (
                outside == 0,
                f"kind {kind}: glass UVs in the clean band v 0-0.25 ({outside} of {len(tris)} triangles outside)",
            )
        )
        straddle = sum(
            1 for uvs, _, _ in tris if len({_which_slot(u, v) for u, v in uvs}) != 1 or _which_slot(*uvs[0]) is None
        )
        rows.append(
            (
                straddle == 0,
                f"kind {kind}: every glass triangle inside one panel slot of the glass "
                f"texture ({straddle} of {len(tris)} straddle or miss; src/dsfforza/convert/glass.py)",
            )
        )
    if 1 in kinds:
        tris = list(_glass_triangles(stream, kinds[1], (64,), {INTERIOR_GLASS}))
        if tris and host is not None and 1 in host[1]:
            host_tris = list(_glass_triangles(host[0], host[1][1], (64,), {INTERIOR_GLASS}))
            if _uv_signature(tris) == _uv_signature(host_tris):
                rows.append(
                    (
                        True,
                        f"kind 1: interior glass is the host's own vanilla interior ({len(tris)} triangles, "
                        "a stub cockpit keeps it; vanilla layout, not checked)",
                    )
                )
                return rows
        regions = defaultdict(int)
        stray = 0
        for uvs, _, _ in tris:
            name = _interior_region(uvs)
            if name is None:
                stray += 1
            else:
                regions[name] += 1
        rows.append(
            (
                stray == 0,
                f"kind 1: interior glass triangles each inside one block of the glass texture's "
                f"interior strip (smear surface, cracks, shards; src/dsfforza/convert/glass.py) or a "
                f"panel slot ({stray} of {len(tris)} straddle or miss)",
            )
        )
        if regions.get("surface") and window_cracks is False:
            rows.append(
                (
                    True,
                    f"kind 1: no interior window took cracks and shards (cockpit report: lamp covers, panes "
                    f"under {MIN_WINDOW_AREA} m2, or none on the host's interior), so its glass is surface only "
                    f"(surface {regions['surface']})",
                )
            )
        elif regions.get("surface"):
            have = {k: regions.get(k, 0) for k in ("crack", "shard_dull", "shard_bright")}
            ok = sum(have.values()) > 0
            rows.append(
                (
                    ok,
                    "kind 1: interior glass has the host's crack lines (states 1-2) or rim shards (state 3), "
                    "so damage shows from the cockpit ("
                    + ", ".join(f"{k} {v}" for k, v in have.items())
                    + f", surface {regions['surface']})",
                )
            )
    return rows


# --- survey: what each glass panel shows in the shattered state -------------------


def _texture():
    from PIL import Image

    here = REPO
    png = here / "out" / "dmg" / "glass" / "tex_00122b77dffa1227.png"
    if png.exists():
        im = Image.open(png).convert("RGBA")
        return im.width, im.height, im.tobytes()
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats.chnk import read_chunk, walk
    from dsfforza.formats.vehicle_mesh import child, payload
    from dsfforza.game import game_paths

    with (game_paths.REPO_VANILLA / "Install" / "dngvehicles.sp").open("rb") as s:
        root = read_chunk(s, 0)
        ssic = next(e for e in root.entries if e.tag == "SSIC")
        for _, e in walk(ssic.children):
            if e.tag == "TEXC" and payload(s, child(e, "UIDC"))[:8] == GLASS_TEXTURE:
                return dm.decode_dds(payload(s, child(e, "DXTC")))
    raise FileNotFoundError("glass texture")


def panel_looks(tris, tex, state: int = 3, steps: int = 6) -> dict:
    """Per s9 group (panel record): share of the window's central area (inner
    70% x 60% of its own extent) that is NOT a hole in `state`. Vanilla-like
    mapping: ~0 (the hole fills the middle, shattered glass only at the rim)."""
    w, h, rgba = tex
    by_pair = defaultdict(list)
    for uvs, s9s, pts in tris:
        by_pair[s9s[0]].append((uvs, pts))
    groups = {}
    for pair, items in by_pair.items():
        # One record can hold two separate panes (left and right quarter
        # lights on the chassis): split at x = 0 when nothing crosses the middle.
        xs = [p[0] for _, ps in items for p in ps]
        if min(xs) < 0 < max(xs) and not any(abs(x) < 0.2 for x in xs):
            for sign in (-1, 1):
                groups[(pair, sign)] = [it for it in items if sum(p[0] for p in it[1]) * sign > 0]
        else:
            groups[(pair, 0)] = items
    out = {}
    for pair, items in groups.items():
        us = [uv for uvs, _ in items for uv in uvs]
        if max(u for u, _ in us) - min(u for u, _ in us) < 0.02:
            continue  # parked on one texel (hidden or blacked-out glass)
        if sum(p[1] for _, ps in items for p in ps) / (3 * len(items)) < 0.15:
            continue  # below the beltline: lamp covers, not windows
        pts = [p for _, ps in items for p in ps]
        lo = [min(p[a] for p in pts) for a in range(3)]
        hi = [max(p[a] for p in pts) for a in range(3)]
        span = [hi[a] - lo[a] for a in range(3)]
        thin = span.index(min(span))  # the window's thickness axis
        axes = [a for a in range(3) if a != thin]
        inner = total = 0
        for uvs, ps in items:
            for i in range(steps + 1):
                for j in range(steps + 1 - i):
                    b = (i / steps, j / steps, 1 - i / steps - j / steps)
                    p = [sum(b[k] * ps[k][a] for k in range(3)) for a in range(3)]
                    if not all(
                        abs(p[a] - (lo[a] + hi[a]) / 2) <= (0.35 if n == 0 else 0.30) * span[a]
                        for n, a in enumerate(axes)
                    ):
                        continue
                    u = sum(b[k] * uvs[k][0] for k in range(3))
                    v = sum(b[k] * uvs[k][1] for k in range(3)) + BAND * state
                    x = min(w - 1, max(0, int(u * w)))
                    y = min(h - 1, max(0, int(v * h)))
                    total += 1
                    inner += rgba[(y * w + x) * 4 + 2] >= 3
        if total:
            out[pair] = (inner / total, len(items), tuple(round((lo[a] + hi[a]) / 2, 2) for a in range(3)))
    return out


def cmd_check(args) -> None:
    from dsfforza.formats import upvh as upvh_mod
    from dsfforza.formats.chnk import read_chunk

    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        rows = check(stream, upvh_mod.entries_for(stream, root.entries, args.id, None))
    for ok, message in rows:
        print(("ok    " if ok else "FAIL  ") + message)
    sys.exit(0 if all(ok for ok, _ in rows) else 1)


def cmd_survey(args) -> None:
    """For each archive/chunk (all VEHC entries of kind 0 found, or --id):
    per glass panel group, the non-hole share of its middle in state 3."""
    from dsfforza.formats import upvh as upvh_mod
    from dsfforza.formats.chnk import read_chunk

    tex = _texture()
    for path in args.archives:
        with path.open("rb") as stream:
            root = read_chunk(stream, 0)
            if args.id is not None:
                vehcs = [upvh_mod.entries_for(stream, root.entries, args.id, None)[args.kind]]
            else:
                vehcs = [e for e in root.entries if e.tag == "VEHC"]
                vehcs = vehcs[2:3] if len(vehcs) == 3 and args.kind == 2 else vehcs[:1]
            for vehc in vehcs:
                tris = list(_glass_triangles(stream, vehc, (args.mask,)))
                looks = panel_looks(tris, tex, args.state)
                bad = {p: r for p, r in looks.items() if r[0] > args.threshold and r[1] >= 4}
                worst = max((r[0] for p, r in looks.items() if r[1] >= 4), default=0.0)
                print(
                    f"{path.stem}: {len(tris)} glass triangles, worst middle shatter {worst:.0%}"
                    + (
                        ""
                        if not bad
                        else "  MISALIGNED "
                        + ", ".join(f"{p} at {r[2]} {r[0]:.0%}" for p, r in sorted(bad.items(), key=lambda t: -t[1][0]))
                    )
                )
                if args.verbose:
                    for p, r in sorted(looks.items()):
                        print(f"    s9 {p}: {r[1]} tris, centre {r[2]}, middle not a hole {r[0]:.0%}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="lint a converted vehicle's exterior glass UVs")
    c.add_argument("archive", type=Path)
    c.add_argument("--id", type=int, required=True)
    s = sub.add_parser("survey", help="state-3 look of each glass panel (middle that is not a hole)")
    s.add_argument("archives", type=Path, nargs="+")
    s.add_argument("--id", type=int, help="spool ID inside a full archive (default: a port_batch chunk)")
    s.add_argument("--kind", type=int, default=2, choices=(0, 2))
    s.add_argument("--mask", type=int, default=None)
    s.add_argument("--state", type=int, default=3)
    s.add_argument("--threshold", type=float, default=0.15)
    s.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "survey" and args.mask is None:
        args.mask = 2 if args.kind == 2 else 4
    {"check": cmd_check, "survey": cmd_survey}[args.command](args)


if __name__ == "__main__":
    main()
