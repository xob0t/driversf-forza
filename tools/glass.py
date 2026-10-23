"""DSF glass: the shared glass texture's panel slots, UVs for converted glass, and a lint.

The engine's damage-aware glass techniques (GLOBAL_Glass programs 47 and 50,
GLOBAL_Glass_LOD1 55; research/vehicle-model.md, "Glass") draw

    v' = v + 0.25 * floor(g_v4PerPanelData[s9.byte1].y)

so each glass vertex shows the crack state of the panel record it is bound
to (semantic 9 byte 1): band 0 clean, 1 light cracks, 2 heavy cracks, 3
shattered. In band 3 the texture is mostly holes (blue 0: the pixel shader's
texkill), with a shattered rim around each hole.

The texture (SSIC 00122b77dffa1227, 512 x 1024) is laid out as four panel
slots per band, and stock cars map each window over its own slot so the
cracks and the hole line up with the window:

    slot          u            v (band 0)   stock use (147, 176, 205, 227, 266)
    windscreen    0.13-0.665   0-0.125      windscreen, u along x, v down the glass
    side_front    0.67-1       0-0.125      front door glass (u along -z), lamp glass
    rear          0.17-0.62    0.125-0.25   rear window (defroster lines in band 0)
    side_rear     0.67-1       0.125-0.25   rear door / quarter glass

Converted glass used to squash Forza's own window UVs into u 0.14-0.98,
v 0.004-0.12: across both upper slots, in whatever orientation Forza's UVs
had. Clean glass looked right, but any crack state drew the slot layouts
across the window: on the M5 (FH1, host 147) in state 3 the rear window was a
hole with a band of shattered glass across its lower part, where the gap
between two hole slots fell (sandbox run 20261022-191859, observe-21..29).
`slot_uvs` maps each converted window by its own geometry onto its slot
instead, so state k looks like a stock window in state k.

    python tools/glass.py check <archive> --id N      # lint one converted vehicle
    python tools/glass.py survey <chunk-or-archive>... # state-3 look of every glass panel
"""

from __future__ import annotations

import argparse
import math
import random
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path

# Shared glass materials (SSIC): GLOBAL_Glass, _LOD1 (stock exteriors), _LOD5 (interiors).
EXTERIOR_GLASS = {bytes.fromhex("001283369afd1027"), bytes.fromhex("10122b9953b14e2a")}
INTERIOR_GLASS = bytes.fromhex("0012cc8576953429")
GLASS_TEXTURE = bytes.fromhex("00122b77dffa1227")
BAND = 0.25

# Band-0 rectangles (u0, u1, v0, v1). Each covers its slot's band-3 hole
# (windscreen u 0.18-0.64, side_front 0.70-0.97, rear 0.16-0.63,
# side_rear 0.70-0.98) with a rim, like stock windows, and stays clear of
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
    named = {"glassf": "windscreen", "glassr": "rear", "glasslf": "side_front", "glassrf": "side_front",
             "glasslm": "side_rear", "glassrm": "side_rear", "glasslr": "side_rear", "glassrr": "side_rear",
             "glasst": "side_front"}
    if n in named:
        return named[n], f"{n}/{named[n]}"
    lamp = (re.fullmatch(r"glass[lr](hl|tl)", n) or "light" in n or n.startswith(("bumper", "hood", "mirror"))
            or (material or "").lower() in LAMP_MATERIALS)
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
# c96[s9.y].y); 60 and 62 have no shift), but stock interiors (147, 227, 229,
# 266; out/cockpitglass/stockparts.py, stockcracks.py) use the left strip
# u 0-0.12 only, where the four bands give each window three layers:
#
#   surface  the smear rows (u 0-0.12, v 0-0.06): a faint reflective layer in
#            states 0-2, a hole in 3. Side and rear windows are covered; the
#            windscreen only along its top and pillars, its centre is open.
#   cracks   3 mm slivers in 2-6 cm segments on the red block (u 0-0.06,
#            v 0.125-0.25): discarded in states 0 and 3; rows v 0.18-0.25 show
#            in states 1 and 2, rows 0.125-0.18 in state 2 only. Stock
#            windscreens carry 0.75-1.7 m of state-1 cracks plus 0.6-1.2 m more
#            for state 2, rear windows 0.3-0.4 m each, front doors about 0.1 m.
#   shards   jagged slivers round the rim on the black blocks (u 0-0.12,
#            v 0.06-0.125: smear-coloured; u 0.06-0.12, v 0.125-0.25: bright):
#            state 3 only, so a shattered window is an empty frame with shards.
#
# Converted interiors used to squash every window into a 10 x 10 texel smear
# patch: uniform in states 0-2, gone in 3, so cracks never showed from inside.
# InteriorGlass rebuilds that layout on each converted window.
SMEAR_RECT = (0.012, 0.108, 0.008, 0.052)    # surface, inset from the strip edges (bilinear and wrap bleed)
CRACK_LIGHT = (0.008, 0.052, 0.190, 0.244)   # visible in states 1 and 2
CRACK_HEAVY = (0.008, 0.052, 0.131, 0.174)   # visible in state 2 only
SHARD_DULL = (0.015, 0.115, 0.070, 0.118)    # visible in state 3 only, smear-coloured
SHARD_BRIGHT = (0.075, 0.110, 0.132, 0.245)  # visible in state 3 only, bright shards
INTERIOR_REGIONS = {"surface": SMEAR_RECT, "crack_light": CRACK_LIGHT, "crack_heavy": CRACK_HEAVY,
                    "shard_dull": SHARD_DULL, "shard_bright": SHARD_BRIGHT}
# The windscreen surface is kept only outside this box (normalised s across,
# t down the glass from the top): the stock frame of smear, open centre.
WINDSCREEN_OPEN = (0.06, 0.94, 0.22)
WINDSCREEN_OPEN_AREA = 0.3   # m2: smaller windscreens keep their whole surface
CRACK_BUDGET = {"windscreen": (1.0, 0.8), "rear": (0.5, 0.4),   # m of crack per sqrt(m2) of window:
                "side_front": (0.3, 0.25), "side_rear": (0.3, 0.25)}  # (state 1, added in state 2)
CRACK_LENGTH = {"windscreen": (0.15, 0.40), "rear": (0.10, 0.25), "side_front": (0.06, 0.16),
                "side_rear": (0.06, 0.16)}
SHARD_CLUSTERS = {"windscreen": 4, "rear": 3, "side_front": 2, "side_rear": 2}
CELL = 0.01              # occupancy grid of a window in its own plane (m)
LIFT = 0.002             # cracks and shards sit this far inside the glass (m)
MIN_WINDOW_AREA = 0.02   # smaller panes (and lamp covers) get the surface only


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
            # the empty grid used to raise).
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
        for (i, j) in self.filled:
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


class InteriorGlass:
    """Interior (cockpit) glass laid out like stock interiors (see above).

    Feed every glass triangle of the entry with `add` (DSF positions, normals,
    sky terms, s9 bytes); `triangles()` then gives, per window (SlotMapper
    group): the surface (smear rows, planar per window; the windscreen's
    centre cut out), crack lines and rim shards on the glass, all bound to the
    window's own s9 so they follow its panel's crack state. With `sight`,
    cracks start and shards sit on the part of the rim the driver sees (a
    Forza windscreen runs well below the dash); windows the driver cannot see
    get the surface only. Deterministic for the same input."""

    def __init__(self, seed: str = "", sight=None):
        self.frames = SlotMapper()
        self.items: dict[str, list] = defaultdict(list)
        self.seed = seed
        self.sight = sight  # DSF point -> seen by the driver (cockpit.GlassSight); None: all
        self.report: dict[str, dict] = {}

    def add(self, section: str, pts, normals, skies, s9: bytes, material: str | None = None) -> str:
        group = self.frames.add(section, pts, material)
        self.items[group].append((tuple(pts), tuple(normals), tuple(skies), s9))
        return group

    def triangles(self) -> list[tuple[list, str]]:
        """[(three (position, uv, normal, sky, s9) vertices, region)], region a
        key of INTERIOR_REGIONS."""
        out = []
        for group in sorted(self.items):
            out += self._window(group)
        return out

    def _window(self, group: str) -> list:
        from collections import Counter
        items = self.items[group]
        _, a_axis, b_axis, s0, s1, t0, t1 = self.frames._frame(group)
        slot, outward = self.frames.slot(group), self.frames.normal(group)
        ws, wt = max(s1 - s0, 1e-6), max(t1 - t0, 1e-6)
        u0, u1, v0, v1 = SMEAR_RECT
        k = min((u1 - u0) / ws, (v1 - v0) / wt)  # isotropic: the smear's clouds keep their shape
        uc, vc, sc, tc = (u0 + u1) / 2, (v0 + v1) / 2, (s0 + s1) / 2, (t0 + t1) / 2

        def surface_uv(p):
            s, t = _dot(p, a_axis), _dot(p, b_axis)
            return (min(u1, max(u0, uc + (s - sc) * k)), min(v1, max(v0, vc + (t - tc) * k)))

        lamp = "/lamp/" in group
        pane = _Pane(items, a_axis, b_axis, outward)
        s9 = Counter(it[3] for it in items).most_common(1)[0][0]
        cut = slot == "windscreen" and not lamp and pane.area >= WINDSCREEN_OPEN_AREA
        lo, hi, top = WINDSCREEN_OPEN
        cuts = [lambda p: (_dot(p, a_axis) - s0) / ws - lo, lambda p: hi - (_dot(p, a_axis) - s0) / ws,
                lambda p: (_dot(p, b_axis) - t0) / wt - top]
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
        info = {"slot": slot, "s9": list(s9[:2]), "area": round(pane.area, 3), "surface_m2": round(kept, 3),
                "triangles_in": len(items)}
        self.report[group] = info
        if lamp or pane.area < MIN_WINDOW_AREA or not pane.rim or slot not in CRACK_BUDGET:
            return out
        rim = pane.rim
        if self.sight is not None:
            rim = [r for r in rim if (lambda hit: hit is not None and self.sight(hit[0]))(pane.lift(r[0]))]
            info["rim_seen"] = round(len(rim) / len(pane.rim), 2)
            if not rim:
                return out
        rng = random.Random(f"{self.seed}/{group}/{len(items)}/{pane.area:.3f}")
        cracks, length = self._cracks(rng, pane, rim, slot, s9)
        shards = self._shards(rng, pane, rim, slot, s9)
        info.update({"crack_m": [round(x, 2) for x in length], "crack_triangles": len(cracks),
                     "shard_triangles": len(shards)})
        return out + cracks + shards

    # cracks: random walks in from the rim, each segment a thin quad ----------------

    @staticmethod
    def _walk(rng, pane, p, d, length):
        path, travelled = [p], 0.0
        while travelled < length:
            step = min(rng.uniform(0.02, 0.05), length - travelled)
            d = _rotate(d, rng.gauss(0.0, 0.28))
            q = (path[-1][0] + d[0] * step, path[-1][1] + d[1] * step)
            if not pane.inside(q):
                break
            path.append(q)
            travelled += step
        return path, d, travelled

    @staticmethod
    def _strip(rng, pane, path, width, rect, s9):
        out = []
        u0, u1, v0, v1 = rect
        for a, b in zip(path, path[1:]):
            seg = math.hypot(b[0] - a[0], b[1] - a[1])
            if seg < 1e-4:
                continue
            e = ((b[0] - a[0]) / seg, (b[1] - a[1]) / seg)
            n = (-e[1] * width / 2, e[0] * width / 2)
            a2 = (a[0] - e[0] * width / 2, a[1] - e[1] * width / 2)  # overlap at the joints
            b2 = (b[0] + e[0] * width / 2, b[1] + e[1] * width / 2)
            corners = [(a2[0] + n[0], a2[1] + n[1]), (a2[0] - n[0], a2[1] - n[1]),
                       (b2[0] - n[0], b2[1] - n[1]), (b2[0] + n[0], b2[1] + n[1])]
            lifted = [pane.lift(c) for c in corners]
            if any(x is None for x in lifted):
                continue
            du = min(u1 - u0, max(0.008, min(0.035, seg * 0.45)))
            dv = min(v1 - v0, 0.006)
            cu, cv = rng.uniform(u0, u1 - du), rng.uniform(v0, v1 - dv)
            uvs = [(cu, cv), (cu, cv + dv), (cu + du, cv + dv), (cu + du, cv)]
            vs = [(p, uv, nrm, 1.0, s9) for (p, nrm), uv in zip(lifted, uvs)]
            out += [[vs[0], vs[1], vs[2]], [vs[0], vs[2], vs[3]]]
        return out

    def _cracks(self, rng, pane, rim, slot, s9):
        root = math.sqrt(pane.area)
        lo, hi = CRACK_LENGTH[slot]
        out, totals, ends = [], [], []
        for kind, budget, rect, width in (("crack_light", CRACK_BUDGET[slot][0] * root, CRACK_LIGHT, 0.003),
                                          ("crack_heavy", CRACK_BUDGET[slot][1] * root, CRACK_HEAVY, 0.0035)):
            used, tries = 0.0, 0
            # state 2 first lengthens most state-1 cracks, then adds new ones
            seeds = [e for e in ends if rng.random() < 0.7] if kind == "crack_heavy" else []
            while used < budget and tries < 400:
                tries += 1
                if seeds:
                    p, d = seeds.pop()
                    length = rng.uniform(0.4, 0.8) * rng.uniform(lo, hi)
                else:
                    p, inward = rim[rng.randrange(len(rim))]
                    d = _rotate(inward, rng.uniform(-0.7, 0.7))
                    length = rng.uniform(lo, hi)
                length = min(length, budget - used + 0.03)
                paths = []
                path, d_end, travelled = self._walk(rng, pane, p, d, length)
                if travelled < 0.015:
                    continue
                paths.append(path)
                if kind == "crack_light":
                    ends.append((path[-1], d_end))
                if len(path) >= 3 and rng.random() < 0.35:  # a branch
                    k = rng.randrange(1, len(path) - 1)
                    seg = (path[k + 1][0] - path[k][0], path[k + 1][1] - path[k][1])
                    norm = math.hypot(*seg) or 1.0
                    branch, _, bt = self._walk(rng, pane, path[k], _rotate((seg[0] / norm, seg[1] / norm),
                                               rng.choice((-1, 1)) * rng.uniform(0.4, 0.9)), length * rng.uniform(0.3, 0.6))
                    if bt > 0.01:
                        paths.append(branch)
                        travelled += bt
                for path in paths:
                    out += [(tri, kind) for tri in self._strip(rng, pane, path, width, rect, s9)]
                used += travelled
            totals.append(used)
        return out, totals

    # shards: jagged teeth along the rim, state 3 only -------------------------------

    def _shards(self, rng, pane, rim, slot, s9):
        clusters = [rim[rng.randrange(len(rim))][0] for _ in range(SHARD_CLUSTERS[slot])]
        boost = 0.06 if slot == "windscreen" else 0.035
        order = list(range(len(rim)))
        rng.shuffle(order)
        picked, out = [], []
        buckets: dict[tuple[int, int], list] = defaultdict(list)
        spacing = 0.035
        for n in order:
            c, inward = rim[n]
            key = (int(c[0] / spacing), int(c[1] / spacing))
            if any(math.hypot(c[0] - q[0], c[1] - q[1]) < spacing
                   for di in (-1, 0, 1) for dj in (-1, 0, 1) for q in buckets[(key[0] + di, key[1] + dj)]):
                continue
            buckets[key].append(c)
            picked.append(c)
            near = max((1.0 - math.hypot(c[0] - q[0], c[1] - q[1]) / 0.18 for q in clusters), default=0.0)
            for tooth in range(1 + (rng.random() < 0.35)):
                w = rng.uniform(0.012, 0.035)
                depth = rng.uniform(0.005, 0.025) + boost * max(0.0, near) * rng.uniform(0.5, 1.0)
                if tooth:
                    depth *= rng.uniform(0.4, 0.7)
                tang = (-inward[1], inward[0])
                shift = rng.uniform(-0.5, 0.5) * w * tooth
                base = (c[0] - inward[0] * 0.006 + tang[0] * shift, c[1] - inward[1] * 0.006 + tang[1] * shift)
                tip = rng.uniform(-0.5, 0.5) * w
                pts2 = [(base[0] + tang[0] * w / 2, base[1] + tang[1] * w / 2),
                        (base[0] - tang[0] * w / 2, base[1] - tang[1] * w / 2),
                        (c[0] + inward[0] * depth + tang[0] * tip, c[1] + inward[1] * depth + tang[1] * tip)]
                lifted = [pane.lift(q) for q in pts2]
                if any(x is None for x in lifted):
                    continue
                if rng.random() < 0.6:
                    u0, u1, v0, v1 = SHARD_BRIGHT
                    du, dv = 0.03, 0.022
                    cu, cv = rng.uniform(u0, u1 - du), rng.uniform(v0, v1 - dv)
                    uvs, region = [(cu, cv), (cu + du, cv), (cu + du * rng.uniform(0.3, 0.7), cv + dv)], "shard_bright"
                else:  # stock: one texel per sliver
                    u0, u1, v0, v1 = SHARD_DULL
                    uv = (rng.uniform(u0, u1), rng.uniform(v0, v1))
                    uvs, region = [uv, uv, uv], "shard_dull"
                out.append(([(p, uv, nrm, 1.0, s9) for (p, nrm), uv in zip(lifted, uvs)], region))
        return out


# --- lint -------------------------------------------------------------------------

def _which_slot(u: float, v: float, tol: float = 0.004) -> str | None:
    for name, (u0, u1, v0, v1) in SLOTS.items():
        if u0 - tol <= u <= u1 + tol and v0 - tol <= v <= v1 + tol:
            return name
    return None


def _glass_triangles(stream, vehc, masks, materials=EXTERIOR_GLASS):
    """(uvs, s9 pairs, points) per glass triangle (`materials`) in part 0 of the given masks."""
    import vehicle_mesh as vm
    from damage import load_mesh
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
            yield ([tuple(vb.read(e8, i)[:2]) for i in idx], [tuple(vb.read(e9, i)[:2]) for i in idx],
                   [vm.position(vb, i) for i in idx])


def _interior_region(uvs, tol: float = 0.002) -> str | None:
    """The INTERIOR_REGIONS key (or "slot:<name>") all three UVs of an
    interior glass triangle fall in, else None."""
    for name, (u0, u1, v0, v1) in INTERIOR_REGIONS.items():
        if all(u0 - tol <= u <= u1 + tol and v0 - tol <= v <= v1 + tol for u, v in uvs):
            return name
    slots = {_which_slot(u, v) for u, v in uvs}
    if len(slots) == 1 and None not in slots:
        return "slot:" + slots.pop()
    return None


def check(stream, kinds: dict) -> list[tuple[bool, str]]:
    """Lint rows (ok, message) for the converted vehicle's glass: exterior
    (full entry part 0 mask 4, exterior entry mask 2) in one panel slot per
    triangle; interior (mask 64) in the stock interior layout, with crack
    lines and rim shards (InteriorGlass)."""
    rows = []
    for kind, masks in ((0, (4,)), (2, (2,))):
        if kind not in kinds:
            continue
        tris = list(_glass_triangles(stream, kinds[kind], masks))
        outside = sum(1 for uvs, _, _ in tris if any(not (-0.002 <= v <= BAND + 0.002) for _, v in uvs))
        rows.append((outside == 0, f"kind {kind}: glass UVs in the clean band v 0-0.25 "
                                   f"({outside} of {len(tris)} triangles outside)"))
        straddle = sum(1 for uvs, _, _ in tris if len({_which_slot(u, v) for u, v in uvs}) != 1
                       or _which_slot(*uvs[0]) is None)
        rows.append((straddle == 0, f"kind {kind}: every glass triangle inside one panel slot of the glass "
                                    f"texture ({straddle} of {len(tris)} straddle or miss; tools/glass.py)"))
    if 1 in kinds:
        tris = list(_glass_triangles(stream, kinds[1], (64,), {INTERIOR_GLASS}))
        regions = defaultdict(int)
        stray = 0
        for uvs, _, _ in tris:
            name = _interior_region(uvs)
            if name is None:
                stray += 1
            else:
                regions[name] += 1
        rows.append((stray == 0, f"kind 1: interior glass triangles each inside one stock interior region of the "
                                 f"glass texture (smear surface, crack rows, shard blocks; tools/glass.py) or a "
                                 f"panel slot ({stray} of {len(tris)} straddle or miss)"))
        if regions.get("surface"):
            have = {k: regions.get(k, 0) for k in ("crack_light", "crack_heavy", "shard_dull", "shard_bright")}
            ok = have["crack_light"] > 0 and have["crack_heavy"] > 0 and have["shard_dull"] + have["shard_bright"] > 0
            rows.append((ok, "kind 1: interior glass has crack lines for states 1-2 and rim shards for state 3, "
                             "so damage shows from the cockpit (" + ", ".join(f"{k} {v}" for k, v in have.items())
                        + f", surface {regions['surface']})"))
    return rows


# --- survey: what each glass panel shows in the shattered state -------------------

def _texture():
    from PIL import Image
    here = Path(__file__).resolve().parents[1]
    png = here / "out" / "dmg" / "glass" / "tex_00122b77dffa1227.png"
    if png.exists():
        im = Image.open(png).convert("RGBA")
        return im.width, im.height, im.tobytes()
    import dsf_materials as dm
    import game_paths
    from chnk import read_chunk, walk
    from vehicle_mesh import child, payload
    with (game_paths.REPO_STOCK / "Install" / "dngvehicles.sp").open("rb") as s:
        root = read_chunk(s, 0)
        ssic = next(e for e in root.entries if e.tag == "SSIC")
        for _, e in walk(ssic.children):
            if e.tag == "TEXC" and payload(s, child(e, "UIDC"))[:8] == GLASS_TEXTURE:
                return dm.decode_dds(payload(s, child(e, "DXTC")))
    raise FileNotFoundError("glass texture")


def panel_looks(tris, tex, state: int = 3, steps: int = 6) -> dict:
    """Per s9 group (panel record): share of the window's central area (inner
    70% x 60% of its own extent) that is NOT a hole in `state`. Stock-like
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
                    if not all(abs(p[a] - (lo[a] + hi[a]) / 2) <= (0.35 if n == 0 else 0.30) * span[a]
                               for n, a in enumerate(axes)):
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
    import upvh as upvh_mod
    from chnk import read_chunk
    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        rows = check(stream, upvh_mod.entries_for(stream, root.entries, args.id, None))
    for ok, message in rows:
        print(("ok    " if ok else "FAIL  ") + message)
    sys.exit(0 if all(ok for ok, _ in rows) else 1)


def cmd_survey(args) -> None:
    """For each archive/chunk (all VEHC entries of kind 0 found, or --id):
    per glass panel group, the non-hole share of its middle in state 3."""
    import upvh as upvh_mod
    from chnk import read_chunk
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
                print(f"{path.stem}: {len(tris)} glass triangles, worst middle shatter {worst:.0%}"
                      + ("" if not bad else "  MISALIGNED " + ", ".join(
                          f"{p} at {r[2]} {r[0]:.0%}" for p, r in sorted(bad.items(), key=lambda t: -t[1][0]))))
                if args.verbose:
                    for p, r in sorted(looks.items()):
                        print(f"    s9 {p}: {r[1]} tris, centre {r[2]}, middle not a hole {r[0]:.0%}")


def main(argv: list[str] | None = None) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
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
