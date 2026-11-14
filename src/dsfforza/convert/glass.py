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
# host interior's matching window (host_windows), placed on the converted window's
# rim where the host has them on its own.
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
# Host crack and shard triangles whose corners are this close share a vertex and
# move as one piece. Vanilla windscreens that share these triangles keep such
# vertices together (99% within 1.1 mm across 5,789 pairs; research/vehicle-model.md,
# "Broken glass moves in pieces").
PIECE_TOL = 0.0005


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


@dataclass
class HostWindow:
    """One window of the host's vanilla interior (an s9 group of its glass):
    every triangle as (points, UVs, sky terms), which of them are its crack
    and shard layers (interior_block crack, shard_dull, shard_bright), and its
    kind (host_kinds: "front", "rear" or "side")."""

    s9: tuple[int, int]
    tris: list
    overlay: list
    kind: str = ""

    @cached_property
    def centre(self) -> tuple:
        pts = [p for t in self.tris for p in t[0]]
        return tuple(sum(p[a] for p in pts) / len(pts) for a in range(3))

    @cached_property
    def sideways(self) -> bool:
        return _sideways([t[0] for t in self.tris])


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


def host_windows(stream, vehc) -> list[HostWindow]:
    """The vanilla interior's glass (GLOBAL_Glass_LOD5 in part 0, mask 64) as
    windows, one per s9 group: vanilla binds each window to its own panel
    record (windscreen (0, 5) on the Desanne, rear (0, 20), doors)."""
    from dsfforza.convert.damage import load_mesh
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
        e0, e8, e9 = vb.elements[0], vb.element(8), vb.element(9)
        for t in range(sub.first_index, sub.first_index + sub.index_count, 3):
            idx = [sub.base_vertex + struct.unpack_from("<H", ib, (t + j) * 2)[0] for j in range(3)]
            pts = tuple(vm.position(vb, i) for i in idx)
            uvs = tuple(tuple(vb.read(e8, i)[:2]) for i in idx)
            skies = tuple(vb.read(e0, i)[3] / 4096 for i in idx)
            groups[tuple(vb.read(e9, idx[0])[:2])].append((pts, uvs, skies))
    return host_kinds(
        [
            HostWindow(s9, tris, [t for t in tris if interior_block(t[1]) not in (None, "surface")])
            for s9, tris in sorted(groups.items())
        ]
    )


class InteriorGlass:
    """Interior (cockpit) glass laid out like vanilla interiors (see above).

    Feed every glass triangle of the entry with `add` (DSF positions, normals,
    sky terms, s9 bytes); `triangles()` then gives, per window (SlotMapper
    group): the surface (smear rows, planar per window; the windscreen's
    centre cut out), and the crack and shard layers of the host window it
    matches (`host`: host_windows of the host's interior), all bound to the
    window's own s9 so they follow its panel's crack state. Host windows a
    window takes are removed from `host`, so a list shared by several
    InteriorGlass (the cockpit's sections, then the shell's) gives each host
    window to one window. Every host triangle that lands on its window is
    kept, seen from the driver's eye or not: the host draws all its own
    window layers."""

    def __init__(self, host: list[HostWindow] | None = None):
        self.frames = SlotMapper()
        self.items: dict[str, list] = defaultdict(list)
        self.host = host if host is not None else []
        if any(not h.kind for h in self.host):
            host_kinds(self.host)
        self.report: dict[str, dict] = {}
        self._host_panes: dict[tuple, tuple] = {}

    def add(self, section: str, pts, normals, skies, s9: bytes, material: str | None = None) -> str:
        group = self.frames.add(section, pts, material)
        self.items[group].append((tuple(pts), tuple(normals), tuple(skies), s9))
        return group

    def triangles(self) -> list[tuple[list, str]]:
        """[(three (position, uv, normal, sky, s9) vertices, region)], region a
        key of INTERIOR_BLOCKS."""
        panes = {}
        for group in sorted(self.items):
            _, a_axis, b_axis, *_ = self.frames._frame(group)
            panes[group] = _Pane(self.items[group], a_axis, b_axis, self.frames.normal(group))
        hosts = self._match(
            [g for g, pane in panes.items() if "/lamp/" not in g and pane.area >= MIN_WINDOW_AREA and pane.rim]
        )
        out = []
        for group in sorted(self.items):
            out += self._window(group, panes[group], hosts.get(group))
        return out

    def _window(self, group: str, pane: _Pane, host: HostWindow | None) -> list:
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
        if host is None:
            return out
        overlay = self._overlay(pane, host, s9)
        if not overlay:
            return out
        regions = Counter(region for _, region in overlay)
        info.update(
            {
                "host_window": list(host.s9),
                "crack_triangles": regions["crack"],
                "shard_triangles": regions["shard_dull"] + regions["shard_bright"],
            }
        )
        return out + overlay

    def _match(self, groups: list[str]) -> dict[str, HostWindow]:
        """Window group -> the host window whose cracks and shards it takes. A
        pair is of one kind: side glass on the same side of the car, the
        windscreen (the converted windscreen slot) with the host's, the rear
        window with the host's rear glass; roof glass takes none. Nearest centres
        pair first, and each host window goes to one window only, as vanilla
        windows carry their own (a converted quarter light next to a door doesn't
        get the host door's too: the vanilla Gallardo 212's quarter lights have
        none). A window whose kind the host has no cracks and shards for keeps
        its surface only, as on the host. Both are in the host's car space."""
        pairs = []
        for group in groups:
            tris = [it[0] for it in self.items[group]]
            slot = self.frames.slot(group)
            kind = (
                "side"
                if _sideways(tris)
                else {"windscreen": "front", "rear": "rear"}.get(slot)
                if "/roof" not in group
                else None
            )
            if kind is None:
                continue
            pts = [p for t in tris for p in t]
            centre = tuple(sum(p[a] for p in pts) / len(pts) for a in range(3))
            for n, h in enumerate(self.host):
                hc = h.centre
                if h.overlay and h.kind == kind and (kind != "side" or (hc[0] > 0) == (centre[0] > 0)):
                    pairs.append((math.dist(hc, centre), group, n))
        out, taken = {}, set()
        for _, group, n in sorted(pairs):
            if group not in out and n not in taken:
                out[group] = self.host[n]
                taken.add(n)
        self.host[:] = [h for n, h in enumerate(self.host) if n not in taken]
        return out

    def _host_pane(self, host: HostWindow):
        """(frame axes, pane, rim) of a host window, framed like a converted
        window of its kind (SlotMapper: u along x for windscreens and rear
        windows, along -z for side glass; v down the glass)."""
        if host.s9 not in self._host_panes:
            frames = SlotMapper()
            section = "glasslf" if host.sideways else "glassf"
            for pts, _, _ in host.tris:
                group = frames.add(section, pts)
            _, a_axis, b_axis, *_ = frames._frame(group)
            outward = frames.normal(group)
            items = [(pts, (outward,) * 3, skies, b"") for pts, _, skies in host.tris]
            pane = _Pane(items, a_axis, b_axis, outward)
            self._host_panes[host.s9] = (a_axis, b_axis, _Rim(pane) if pane.filled else None)
        return self._host_panes[host.s9]

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
        more than one cell of the occupancy grid outside it)."""
        ha, hb, hrim = self._host_pane(host)
        if hrim is None:
            return []
        rim = _Rim(pane)
        out = []
        flat_tris = [[(_dot(p, ha), _dot(p, hb)) for p in pts] for pts, _, _ in host.overlay]
        for piece in _pieces([[(*q, 0.0) for q in tri] for tri in flat_tris]):
            corners_h = [c for n in piece for c in flat_tris[n]]
            g = (sum(x for x, _ in corners_h) / len(corners_h), sum(y for _, y in corners_h) / len(corners_h))
            theta, rho = hrim.polar(g)
            k = _Rim.bin(theta)
            ray_h, ray_p = hrim.ray(theta), rim.ray(theta)
            depth = (hrim.reach[k] - rho) * math.hypot(*ray_h)  # metres inside the host's rim
            along = rim.reach[k] - depth / (math.hypot(*ray_p) or 1.0)
            if along <= 0:
                continue  # deeper than this window reaches: it would cross to the far side
            gp = (rim.centre[0] + along * ray_p[0], rim.centre[1] + along * ray_p[1])
            turn = math.atan2(ray_p[1], ray_p[0]) - math.atan2(ray_h[1], ray_h[0])
            for n in piece:
                out += self._place(pane, host.overlay[n], flat_tris[n], g, gp, turn, s9)
        return out

    @staticmethod
    def _place(pane: _Pane, tri, q, g, gp, turn, s9) -> list:
        """One host triangle (plane points q) of a piece moved from g to gp and
        turned by turn, lifted onto the glass; [] where it lands outside."""
        _, uvs, skies = tri
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
        vertices = [(p, tuple(uv), nrm, sky, s9) for (p, nrm), uv, sky in zip(corners, uvs, skies)]
        return [(vertices, interior_block(uvs))]


def _pieces(tris, tol: float = PIECE_TOL) -> list[list[int]]:
    """Indices of tris (three points each) grouped into pieces: triangles that
    share a vertex (two corners within tol) are one piece."""
    parent = list(range(len(tris)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cells: dict[tuple, list] = defaultdict(list)
    for n, tri in enumerate(tris):
        for p in tri:
            key = tuple(math.floor(c / tol) for c in p)
            for d in itertools.product((-1, 0, 1), repeat=3):
                for m, other in cells.get(tuple(a + b for a, b in zip(key, d)), ()):
                    if math.dist(p, other) <= tol:
                        a, b = root(n), root(m)
                        if a != b:
                            parent[a] = b
            cells[key].append((n, p))
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
    cars and roadsters, or a host interior without them); then glass without
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
