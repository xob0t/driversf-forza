"""Textured DSF materials for converted Forza cars: chassis atlas, TEXC, MACO.

A DSF car textures everything through one chassis texture (TEXC in the full
entry, 512x512 DXT5 with 4 mips on the Desanne) and two local materials that
sample it through UV0 (MACO, `g_texDiffuseMap` = the TEXC's UID):

    Chassis  stride-28 submodels, low specular (interior bits, badges)
    Lights   stride-32 submodels, reflective, decal normal map on UV1

The clean exterior entry (N+2000, the one drawn for the player's car) has its
own pair of materials that reference the full entry's TEXC UID, and the
shared paint material overlays the same texture through UV0. Texel alpha is
a specular / glow mask: it follows luminance on chassis texels
(about (lum - 45) * 0.55) and holds the bulb glow on lamp texels.

This module:

- packs the parts of the Forza atlases (nodamage, Lights) that textured
  subsections actually use into one DSF atlas (`Texturing`), with solid
  cells for colour swatches and for the plain paint texel;
- encodes it as DXT5 with mips (`encode_dxt5`, `dds_file`);
- installs it: by default it replaces the host TEXC's DDS and keeps every
  UID, so the host materials of all three entries pick it up
  (`install_full`, `install_exterior`); `mode="clone"` adds a new TEXC and
  clones the two materials instead;
- renders a textured preview of a VEHC entry (`preview`).

    python tools/dsf_materials.py atlas out/fh1/NIS_370Z_10 --png out/atlas.png
    python tools/dsf_materials.py preview out/370z-tex.sp 452 --png out/tex.png
"""

from __future__ import annotations

import argparse
import copy
import math
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

import forza_materials as fm
from chnk import Entry
from vehicle_mesh import child, payload

DDS_HEADER_SIZE = 128
# The Desanne's chassis texture: DXT5, 4 mip levels, and its plain paint texel.
DEFAULT_SIZE = (512, 512)
DEFAULT_MIPS = 4
# Paint UV0 does not sample the vehicle's texture: both paint materials
# (GLOBAL_Chassis, GLOBAL_Chassis_LOD1) sample the shared SSIC texture
# 00122b73d1fa1227, a 1024 x 512 atlas of masks and parts, and 57 to 91% of
# every stock car's paint triangles sit in its (240, 192, 240) block (50 x 50
# texels centred here). UV0 elsewhere draws the paint black or grey
# (research/lights.md, "Paint").
PAINT_TEXEL_UV = (0.8481, 0.7119)  # forza2dsf.PAINT_UV (0.835, 0.703) is inside the same block
# Lamp texel alpha (the Lights shader adds lamp colour x brightness x texel alpha,
# research/lights.md). "matched" (default): a score s = Forza glow mask x (0.6 + 0.4 x
# brightest channel), all 0..1, mapped through a curve per texel hue (red, amber, white) that
# puts the converted texels on stock DSF's lamp alpha distribution (quantile match of
# six FH1/FM4 cars against 146 stock vehicles, out/lamps/scripts/fit_lut.py). It keeps
# the order of the texels, so hot spots, reflector cells and dark edges stay where
# Forza has them, and a near-black lens only glows where its glow mask is bright.
# "raw": Forza's alpha as is (analysis builds). The earlier modes ("value", "luma",
# "gamma") are described in research/lights.md. Module-level so an A/B build can
# switch it; DSFFORZA_LIGHTS_ALPHA overrides it for port_batch workers.
LIGHTS_ALPHA_MODES = ("matched", "raw")
LIGHTS_ALPHA = os.environ.get("DSFFORZA_LIGHTS_ALPHA", "matched")
if LIGHTS_ALPHA not in LIGHTS_ALPHA_MODES:
    raise ValueError(f"DSFFORZA_LIGHTS_ALPHA={LIGHTS_ALPHA!r}: one of {', '.join(LIGHTS_ALPHA_MODES)}")
# "matched" curves: score knots -> stock alpha knots (stock percentiles 0, 5, 10, 20,
# ..., 90, 93, 95, 97, 98, 99, 99.5 of amber and of white lamps; red lamps also have
# 85 and 100, LAMP_PERCENTILES).
_LAMP_X_RED = (0.0, 0.199, 0.313, 0.458, 0.529, 0.586, 0.627, 0.672, 0.715, 0.768, 0.785, 0.798, 0.815,
               0.831, 0.853, 0.87, 0.896, 0.922, 0.961)
_LAMP_Y_RED = (0, 0, 0, 3, 8, 14, 21, 30, 43, 63, 78, 106, 130, 153, 187, 211, 239, 252, 255)
_LAMP_X_WHITE = (0.0, 0.117, 0.135, 0.177, 0.373, 0.557, 0.677, 0.755, 0.813, 0.898, 0.922, 0.936, 0.947,
                 0.951, 0.961, 0.97, 0.975)
_LAMP_Y_WHITE = (0, 0, 1, 9, 18, 29, 43, 60, 84, 119, 142, 173, 197, 216, 237, 248, 255)
_LAMP_Y_AMBER = (0, 0, 1, 7, 14, 22, 31, 44, 61, 84, 100, 124, 145, 167, 196, 217, 242)
LAMP_CURVES = {"red": (_LAMP_X_RED, _LAMP_Y_RED), "white": (_LAMP_X_WHITE, _LAMP_Y_WHITE),
               "amber": (_LAMP_X_WHITE, _LAMP_Y_AMBER)}


# "matched" also darkens red lamp lenses to stock's brightness: Forza's red lamp texels
# are brighter (brightest channel median 144, M3 E30 199, DMC-12 158) than stock's
# (103), so its lenses looked lit in daylight before any lamp was on. Brightest channel
# x -> at most y (quantile match like the alpha curves; red hue only, never brighter).
_LENS_X_RED = (0, 98, 106, 128, 144, 158, 172, 189, 222, 249, 255)
_LENS_Y_RED = (0, 73, 82, 93, 103, 114, 123, 139, 161, 180, 218)


def _interp(s: float, xs, ys) -> float:
    if s >= xs[-1]:
        return float(ys[-1])
    for i in range(1, len(xs)):
        if s < xs[i]:
            return ys[i - 1] + (s - xs[i - 1]) / (xs[i] - xs[i - 1]) * (ys[i] - ys[i - 1])
    return float(ys[-1])


# Stock red lamp alpha at the percentiles LAMP_PERCENTILES (= _LAMP_Y_RED).
LAMP_PERCENTILES = (0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 85, 90, 93, 95, 97, 98, 99, 99.5, 100)
RED_CURVE_MIN_TEXELS = 200      # fewer red lamp texels: the shared red curve
RED_CURVE_SAMPLES = 40000       # about this many texels sampled over a car's lights regions


def red_lamp_curve(scores: list[float]):
    """One car's red lamp curve: its own red texel scores' percentiles -> stock's red
    alpha percentiles, so every car's red lamps land on stock's distribution (with one
    curve for all cars a dim Forza mask gave the S2000 a tail median of 2 and the
    Challenger 73; in game the S2000's and S5S's lamps barely lit, run
    20261024-002212). None with too few texels (the shared curve is used)."""
    if len(scores) < RED_CURVE_MIN_TEXELS:
        return None
    ordered = sorted(scores)
    xs = []
    for q in LAMP_PERCENTILES:
        x = ordered[min(len(ordered) - 1, int(q / 100 * (len(ordered) - 1) + 0.5))]
        xs.append(max(x, xs[-1] + 1e-4) if xs else x)
    return tuple(xs), _LAMP_Y_RED


def matched_lamp_texel(r: int, g: int, b: int, a: int, red_curve=None) -> tuple[int, int, int, int]:
    """Stock-like lamp texel (LIGHTS_ALPHA "matched"): alpha from the curves, red
    lenses darkened to stock's brightness."""
    alpha = matched_lamp_alpha(r, g, b, a, red_curve)
    if lamp_hue(r, g, b) == "red":
        v = max(r, g, b)
        k = min(1.0, _interp(v, _LENS_X_RED, _LENS_Y_RED) / v) if v else 1.0
        r, g, b = (int(c * k + 0.5) for c in (r, g, b))
    return r, g, b, alpha


def lamp_hue(r: int, g: int, b: int) -> str:
    """Lamp texel colour class for the "matched" curves."""
    if r > 1.6 * g and r > 1.6 * b:
        return "red"
    if r > 1.2 * b + 20 and g > 0.35 * r and g > 1.2 * b:
        return "amber"
    return "white"


def lamp_score(r: int, g: int, b: int, a: int) -> float:
    """The "matched" ranking: Forza's glow mask, weighted a little by brightness."""
    return (a / 255) * (0.6 + 0.4 * max(r, g, b) / 255)


def matched_lamp_alpha(r: int, g: int, b: int, a: int, red_curve=None) -> int:
    """Stock-like lamp alpha from a Forza lamp texel (LIGHTS_ALPHA "matched");
    red_curve: this car's own (score knots, alpha knots) for red texels."""
    hue = lamp_hue(r, g, b)
    xs, ys = red_curve if hue == "red" and red_curve else LAMP_CURVES[hue]
    return int(_interp(lamp_score(r, g, b, a), xs, ys) + 0.5)


DEFAULT_PLAIN = (74, 72, 74, 15)
# Chassis texels (not lamps): alpha follows luminance, as on stock chassis
# textures (module docstring): (lum - CHASSIS_ALPHA_LUM) x CHASSIS_ALPHA_GAIN.
CHASSIS_ALPHA_LUM, CHASSIS_ALPHA_GAIN = 45, 0.55
# nodamage texels this transparent and this bright are the paint-through body
# area (white, alpha 0): they get the plain texel, and no cockpit tone.
PAINT_THROUGH_ALPHA, PAINT_THROUGH_MIN_RGB = 16, 200


def _paint_through(family: str, r: int, g: int, b: int, a: int) -> bool:
    return family == fm.NODAMAGE and a < PAINT_THROUGH_ALPHA and min(r, g, b) > PAINT_THROUGH_MIN_RGB


# Rear lamp lenses (Texturing.is_lens): a lamp-cover glass subsection behind
# this Forza z (front is -z; the 40 testdrive cars' rear lenses sit at +1.5 to
# +2.5) whose mean texel is this much redder than green and blue, and this
# opaque. Matches the M3 E30, Silvia, 300C, M5 E28, 205 T16 and DMC-12 tail
# lenses (red minus max(green, blue) 98 to 135, alpha 138 to 241); clear and
# smoked covers (Supra, RX-7, IS F, Range Rover: 13 to 58) stay glass.
LENS_REAR_Z, LENS_MIN_RED, LENS_MIN_ALPHA = 0.5, 80, 128

CHASSIS, LIGHTS_MAT = "chassis", "lights"
DROP = "drop"  # triangle_uvs tag: leave this triangle out (alpha-tested away in Forza)


# --- DXT5 ------------------------------------------------------------------------

def _q565(r: int, g: int, b: int) -> int:
    return ((r * 31 + 127) // 255) << 11 | ((g * 63 + 127) // 255) << 5 | ((b * 31 + 127) // 255)


def _x565(c: int) -> tuple[int, int, int]:
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def _colour_block(px: list[tuple[int, int, int]]) -> bytes:
    lo = [min(p[c] for p in px) for c in range(3)]
    hi = [max(p[c] for p in px) for c in range(3)]
    # Pick the bounding-box diagonal that follows the colours' correlation.
    axis = max(range(3), key=lambda c: hi[c] - lo[c])
    mean = [sum(p[c] for p in px) / 16 for c in range(3)]
    e0, e1 = list(hi), list(lo)
    for c in range(3):
        if c != axis and sum((p[axis] - mean[axis]) * (p[c] - mean[c]) for p in px) < 0:
            e0[c], e1[c] = lo[c], hi[c]
    inset = [(e0[c] - e1[c]) / 16 for c in range(3)]
    e0 = [max(0, min(255, round(e0[c] - inset[c]))) for c in range(3)]
    e1 = [max(0, min(255, round(e1[c] + inset[c]))) for c in range(3)]
    c0, c1 = _q565(*e0), _q565(*e1)
    if c0 < c1:
        c0, c1 = c1, c0
    if c0 == c1:
        return struct.pack("<HHI", c0, c1, 0)
    p0, p1 = _x565(c0), _x565(c1)
    pal = [p0, p1, tuple((2 * a + b) // 3 for a, b in zip(p0, p1)), tuple((a + 2 * b) // 3 for a, b in zip(p0, p1))]
    bits = 0
    for i, p in enumerate(px):
        best = min(range(4), key=lambda k: (p[0] - pal[k][0]) ** 2 + (p[1] - pal[k][1]) ** 2 + (p[2] - pal[k][2]) ** 2)
        bits |= best << (2 * i)
    return struct.pack("<HHI", c0, c1, bits)


def _alpha_block(alpha: list[int]) -> bytes:
    a0, a1 = max(alpha), min(alpha)
    if a0 == a1:
        return bytes((a0, a1)) + bytes(6)
    pal = [a0, a1] + [((7 - k) * a0 + k * a1) // 7 for k in range(1, 7)]
    bits = 0
    for i, a in enumerate(alpha):
        bits |= min(range(8), key=lambda k: abs(a - pal[k])) << (3 * i)
    return bytes((a0, a1)) + bits.to_bytes(6, "little")


def encode_dxt5(rgba: bytes | bytearray, width: int, height: int) -> bytes:
    """Encode RGBA8 (width, height multiples of 4) as DXT5 blocks, row-major."""
    out = bytearray()
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            px, alpha = [], []
            for y in range(4):
                o = ((by + y) * width + bx) * 4
                row = rgba[o:o + 16]
                for x in range(4):
                    px.append((row[x * 4], row[x * 4 + 1], row[x * 4 + 2]))
                    alpha.append(row[x * 4 + 3])
            out += _alpha_block(alpha) + _colour_block(px)
    return bytes(out)


def encode_dxt1(rgba: bytes | bytearray, width: int, height: int) -> bytes:
    """Encode RGB of RGBA8 as opaque DXT1 blocks (four-colour mode only;
    alpha is dropped). Stock interior entries ship DXT1 textures too."""
    out = bytearray()
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            px = []
            for y in range(4):
                o = ((by + y) * width + bx) * 4
                row = rgba[o:o + 16]
                px.extend((row[x * 4], row[x * 4 + 1], row[x * 4 + 2]) for x in range(4))
            out += _colour_block(px)
    return bytes(out)


def downsample(rgba: bytes | bytearray, width: int, height: int) -> tuple[bytearray, int, int]:
    """2x2 box filter."""
    w2, h2 = max(1, width // 2), max(1, height // 2)
    out = bytearray(w2 * h2 * 4)
    for y in range(h2):
        r0, r1 = (2 * y) * width * 4, (2 * y + 1) * width * 4
        for x in range(w2):
            a, b = r0 + 8 * x, r1 + 8 * x
            o = (y * w2 + x) * 4
            for c in range(4):
                out[o + c] = (rgba[a + c] + rgba[a + 4 + c] + rgba[b + c] + rgba[b + 4 + c] + 2) // 4
    return out, w2, h2


def dds_header(width: int, height: int, mips: int, template: bytes | None = None,
               fourcc: bytes = b"DXT5") -> bytes:
    """A DXT5 (or DXT1) DDS header; copies `template` (e.g. the host texture's)
    and patches the size fields, so flags and caps match what the game ships."""
    if template is not None and template[:4] == b"DDS " and template[84:88] in (b"DXT5", b"DXT1"):
        h = bytearray(template[:DDS_HEADER_SIZE])
    else:
        h = bytearray(struct.pack("<4s7I44s2I4s5I5I", b"DDS ", 124, 0xA1007, 0, 0, 0, 0, 0,
                                  bytes(44), 32, 0x4, b"DXT5", 0, 0, 0, 0, 0, 0x401008, 0, 0, 0, 0))
    linear = width * height // (2 if fourcc == b"DXT1" else 1)  # DXT1: half a byte per texel
    struct.pack_into("<IIII", h, 12, height, width, linear, 0)
    struct.pack_into("<I", h, 28, mips)
    h[84:88] = fourcc
    return bytes(h)


def dds_file(rgba: bytes | bytearray, width: int, height: int, mips: int = DEFAULT_MIPS,
             template: bytes | None = None, fourcc: bytes = b"DXT5") -> bytes:
    """DXT5 (or DXT1) DDS with `mips` levels (top level included), box-filtered."""
    encode = encode_dxt1 if fourcc == b"DXT1" else encode_dxt5
    levels, level = [], (rgba, width, height)
    for n in range(mips):
        levels.append(encode(*level))
        if n + 1 < mips:
            level = downsample(*level)
            if level[1] < 4 or level[2] < 4:
                mips = n + 1
                break
    return dds_header(width, height, mips, template, fourcc) + b"".join(levels[:mips])


def dds_size(width: int, height: int, mips: int) -> int:
    return DDS_HEADER_SIZE + sum(max(4, width >> n) * max(4, height >> n) for n in range(mips))


def decode_dds(dds: bytes) -> tuple[int, int, bytearray]:
    import xds
    height, width = struct.unpack_from("<II", dds, 12)
    return width, height, xds.decode_rgba(dds[DDS_HEADER_SIZE:], width, height, dds[84:88])


# --- atlas layout -------------------------------------------------------------------

SOLID_CELL = 8     # destination texels per solid-colour cell
PAD = 4            # destination texels of real neighbouring content around each region
ALIGN = 4          # DXT block alignment
MIN_GAP = 2        # source texels of empty seam needed to cut regions apart


@dataclass
class Region:
    """A rectangle of a Forza atlas copied into the DSF atlas at `scale`."""
    family: str
    src: tuple[int, int, int, int]  # x0, y0, x1, y1 in source texels
    dst: tuple[int, int] = (0, 0)   # top-left of the padded block in the DSF atlas
    size: tuple[int, int] = (0, 0)  # padded block size in DSF texels

    def map(self, u: float, v: float, src_size: tuple[int, int], scale: float,
            atlas: tuple[int, int]) -> tuple[float, float]:
        x = self.dst[0] + PAD + (u * src_size[0] - self.src[0]) * scale
        y = self.dst[1] + PAD + (v * src_size[1] - self.src[1]) * scale
        return x / atlas[0], y / atlas[1]


def _groups(ids: list[int], boxes, axis: int) -> list[list[int]]:
    """Split box ids along one axis wherever an empty seam of MIN_GAP texels
    separates them."""
    order = sorted(ids, key=lambda i: boxes[i][axis])
    groups, end = [], None
    for i in order:
        if end is None or boxes[i][axis] >= end + MIN_GAP:
            groups.append([])
            end = boxes[i][axis + 2]
        groups[-1].append(i)
        end = max(end, boxes[i][axis + 2])
    return groups


def _area(b) -> int:
    return (b[2] - b[0]) * (b[3] - b[1])


def _bbox(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _best_split(ids: list[int], boxes, gain: float = 0.85, min_area: int = 64 * 64) -> list[list[int]] | None:
    """For a group with no empty seam (an L shape, say): split the boxes by
    centre along x or y where the two halves' bounding boxes have the least
    total area. The halves may overlap (texels get duplicated). None unless it
    saves at least 1 - `gain` of the area."""
    whole = (min(boxes[i][0] for i in ids), min(boxes[i][1] for i in ids),
             max(boxes[i][2] for i in ids), max(boxes[i][3] for i in ids))
    if len(ids) < 2 or _area(whole) < min_area:
        return None
    best = None
    for axis in (0, 1):
        order = sorted(ids, key=lambda i: boxes[i][axis] + boxes[i][axis + 2])
        prefix, acc = [], None
        for i in order:
            acc = boxes[i] if acc is None else _bbox(acc, boxes[i])
            prefix.append(acc)
        acc = None
        for k in range(len(order) - 1, 0, -1):
            acc = boxes[order[k]] if acc is None else _bbox(acc, boxes[order[k]])
            total = _area(prefix[k - 1]) + _area(acc)
            if best is None or total < best[0]:
                best = (total, order[:k], order[k:])
    if best is None or best[0] > gain * _area(whole):
        return None
    return [best[1], best[2]]


def find_regions(triangles: list, src_size: tuple[int, int]) -> tuple[list[tuple[int, int, int, int]], list[int]]:
    """Recursive XY cut of the UV triangles' boxes (in source texels): split
    along empty seams until none is left, so every triangle lies inside one
    rectangle. Returns the rectangles (x0, y0, x1, y1) and each triangle's
    rectangle index."""
    width, height = src_size
    boxes = []
    for tri in triangles:
        us = [min(1.0, max(0.0, u)) * width for u, _ in tri]
        vs = [min(1.0, max(0.0, v)) * height for _, v in tri]
        boxes.append((max(0, math.floor(min(us)) - 1), max(0, math.floor(min(vs)) - 1),
                      min(width, math.ceil(max(us)) + 1), min(height, math.ceil(max(vs)) + 1)))
    rects, owner = [], [0] * len(boxes)
    stack = [(list(range(len(boxes))), 0)]
    while stack:
        ids, axis = stack.pop()
        if not ids:
            continue
        for a in (axis, 1 - axis):
            groups = _groups(ids, boxes, a)
            if len(groups) > 1:
                stack.extend((g, 1 - a) for g in groups)
                break
        else:
            halves = _best_split(ids, boxes)
            if halves:
                stack.extend((g, axis) for g in halves)
                continue
            rect = (min(boxes[i][0] for i in ids), min(boxes[i][1] for i in ids),
                    max(boxes[i][2] for i in ids), max(boxes[i][3] for i in ids))
            for i in ids:
                owner[i] = len(rects)
            rects.append(rect)
    return rects, owner


def _round_up(n: float, step: int = ALIGN) -> int:
    return -(-int(math.ceil(n)) // step) * step


def skyline_pack(sizes: list[tuple[int, int]], width: int, height: int) -> list[tuple[int, int]] | None:
    """Skyline bottom-left packing, largest first: each rectangle goes where
    its far edge ends up lowest. None if they don't fit."""
    order = sorted(range(len(sizes)), key=lambda i: (-max(sizes[i]), -sizes[i][0] * sizes[i][1]))
    sky = [(0, 0, width)]  # segments (x, y, w), left to right
    pos: list[tuple[int, int] | None] = [None] * len(sizes)
    for i in order:
        w, h = sizes[i]
        best = None
        for k, (x, _, _) in enumerate(sky):
            if x + w > width:
                break
            y, end, j = 0, x + w, k
            while j < len(sky) and sky[j][0] < end:
                y = max(y, sky[j][1])
                j += 1
            if y + h <= height and (best is None or (y + h, x) < (best[1] + h, best[0])):
                best = (x, y)
        if best is None:
            return None
        x, y = best
        pos[i] = best
        new = []
        for sx, sy, sw in sky:
            if sx + sw <= x or sx >= x + w:
                new.append((sx, sy, sw))
                continue
            if sx < x:
                new.append((sx, sy, x - sx))
            if sx + sw > x + w:
                new.append((x + w, sy, sx + sw - x - w))
        new.append((x, y + h, w))
        new.sort()
        sky = []
        for seg in new:
            if sky and sky[-1][1] == seg[1] and sky[-1][0] + sky[-1][2] == seg[0]:
                sky[-1] = (sky[-1][0], seg[1], sky[-1][2] + seg[2])
            else:
                sky.append(seg)
    return pos  # type: ignore[return-value]


# --- the converter-facing object ------------------------------------------------------

@dataclass
class Texturing:
    """Decides, for one Forza car, which DSF material and UVs each subsection
    gets, and builds the chassis atlas they sample.

    Build it from the sections and LODs that will be converted, then:
      - `material(section, sub)` -> "chassis" / "lights" (textured through the
        atlas) or a forza_materials role (paint, glass, trim, tyre, skip) for
        DSF's shared materials;
      - `triangle_uvs(section, sub, raw_uvs)` -> atlas UVs for one triangle
        and a tag to key shared vertices with;
      - `paint_uv` -> the plain texel paint vertices should point at;
      - `dds(template)` -> the encoded atlas.
    """
    car_dir: Path
    size: tuple[int, int] = DEFAULT_SIZE
    mips: int = DEFAULT_MIPS
    swatch_trims: bool = False    # give swatch trims (black, frame...) their Forza colour
    plain: tuple[int, int, int, int] = DEFAULT_PLAIN
    weights: dict[str, float] = field(default_factory=lambda: {fm.NODAMAGE: 1.0, fm.LIGHTS: 1.0, fm.INTERIOR: 1.0})
    # Cockpit carbins (tools/cockpit.py): the _LOD0 atlases, fm.classify_cockpit,
    # and interior_LOD0 as a third family.
    lod0: bool = False
    classifier: object = None
    families: tuple = (fm.NODAMAGE, fm.LIGHTS)
    fourcc: bytes = b"DXT5"
    # family -> floor: RGB = floor + c * (255 - floor) / 255 (cockpit trims are
    # dark detail maps that Forza's cockpit shaders tint and light).
    lift: dict = field(default_factory=dict)
    # family -> (brightness, saturation) multipliers applied after the lift
    # (cockpit: Forza's flat interior swatches, e.g. the Mustang's pure red,
    # come out far brighter and more saturated under DSF's interior lighting).
    tone: dict = field(default_factory=dict)
    # Opt-in (cockpit): in regions only alpha-tested techniques use, texels
    # below half alpha get this RGBA, since the target material has no alpha
    # test (label decals on light backgrounds otherwise show as plates).
    cutout_colour: tuple | None = None
    # Opt-in (cockpit): family -> RGB written over source texels with alpha
    # below PAINT_THROUGH_ALPHA before anything samples the atlas (alpha kept, so alpha tests
    # still see them as transparent). nodamage_LOD0's alpha-0 texels are
    # paint-through filler (white on most cars, a grey badge background on
    # the Jeep GC SRT8), not trim colour (research/cockpit.md, "Light cockpit
    # trims" and "Cabin trims sample interior_LOD0").
    filler: dict = field(default_factory=dict)
    alpha_only: set = field(default_factory=set)       # (family, region index) used only by alpha-tested tris
    resolved: dict = field(default_factory=dict)       # (id(section), id(sub)) -> SubMaterial
    regions: dict = field(default_factory=dict)        # family -> [Region]
    owners: dict = field(default_factory=dict)         # (id(sub), triangle index) -> region index
    solids: dict = field(default_factory=dict)         # key -> (x, y) of a solid cell
    solid_colours: dict = field(default_factory=dict)  # key -> RGBA
    scale: dict = field(default_factory=dict)          # family -> DSF texels per source texel
    src_sizes: dict = field(default_factory=dict)
    atlases: fm.AtlasCache | None = None
    atlas_subs: set = field(default_factory=set)       # id(sub) with triangles in the atlas
    dropped: set = field(default_factory=set)          # (id(sub), triangle index) cut out by alpha
    cutout: set = field(default_factory=set)           # id(sub) entirely cut out by alpha
    lens_subs: set = field(default_factory=set)        # id(sub): rear lamp lenses moved from glass to the atlas (is_lens)

    # -- planning --
    def plan(self, sections, lods, extra_solids: dict | None = None) -> "Texturing":
        """Resolve every subsection at `lods` and lay out the atlas.
        extra_solids: key -> RGBA cells to reserve as well (`solid_uv(key)`)."""
        self.atlases = fm.AtlasCache(self.car_dir, lod0=self.lod0)
        for family, rgb in self.filler.items():
            if family in self.families:
                fm.fill_transparent(self.atlases.get(family), rgb, below=PAINT_THROUGH_ALPHA)
        tris: dict[str, list] = {f: [] for f in self.families}
        keys: dict[str, list] = {f: [] for f in self.families}
        tested: dict[str, list] = {f: [] for f in self.families}
        swatches: list[fm.SubMaterial] = []
        for section in sections:
            for sub in section.subsections:
                if sub.lod not in lods:
                    continue
                m = self._resolve(section, sub)
                self.resolved[(id(section), id(sub))] = m
                alpha_tested = m.technique in fm.ALPHA_TESTED
                if self._uses_atlas(m) and m.kind == "textured":
                    pool = section.vertices_for(sub)
                    for k, tri in enumerate(sub.triangles()):
                        uvs = [m.uv(pool[i].uv0) for i in tri]
                        if alpha_tested and fm.triangle_max_alpha(self.atlases.get(m.family), uvs) < 128:
                            self.dropped.add((id(sub), k))
                            continue
                        tris[m.family].append(uvs)
                        keys[m.family].append((id(sub), k))
                        tested[m.family].append(alpha_tested)
                        self.atlas_subs.add(id(sub))
                    if id(sub) not in self.atlas_subs:
                        self.cutout.add(id(sub))
                elif self._uses_atlas(m) and m.kind == "swatch":
                    swatches.append((sub, m))
        for family in self.families:
            w, h, _ = self.atlases.get(family)
            self.src_sizes[family] = (w, h)
            rects, owner = find_regions(tris[family], (w, h)) if tris[family] else ([], [])
            self.regions[family] = [Region(family, r) for r in rects]
            for key, n in zip(keys[family], owner):
                self.owners[key] = n
            mixed = {n for n, t in zip(owner, tested[family]) if not t}
            self.alpha_only |= {(family, n) for n in set(owner) - mixed}
        # Swatch cells only once the regions are known: a red lamp swatch's
        # alpha comes from the car's own red curve (_red_lamp_curve, sampled
        # over the lights regions), and triangle_uvs converts the colour again
        # to find its cell. Converted before the regions, the swatch got the
        # shared curve here and the car's curve there: KeyError for the FH1
        # M5's and Gallardo's red tail lamp swatches.
        fm.sample_colours([m for _, m in swatches], self.atlases)
        for sub, m in swatches:
            if m.technique in fm.ALPHA_TESTED and m.colour[3] < 128:
                self.cutout.add(id(sub))
                continue
            if m.family in self.filler and m.colour[3] < 128:
                # Mostly filler (the Jeep's wheel leather: a 4-texel footprint
                # a third on badge letters): all filler.
                m.colour = tuple(self.filler[m.family][:3]) + (m.colour[3],)
            colour = self._convert_texel(m.family, *m.colour)
            self.solid_colours[("swatch",) + colour] = colour
        self.solid_colours[("paint",)] = self.plain
        for key, colour in (extra_solids or {}).items():
            self.solid_colours[key] = tuple(colour)
        self._pack()
        return self

    def _resolve(self, section, sub) -> fm.SubMaterial:
        m = fm.resolve(section, sub, self.classifier or fm.classify)
        if self.classifier is None and self.is_lens(section, sub, m):
            import dataclasses
            m = dataclasses.replace(m, role=fm.TEXTURED)
            self.lens_subs.add(id(sub))
        return m

    def is_lens(self, section, sub, m: fm.SubMaterial) -> bool:
        """A coloured rear lamp lens drawn with a lamp-cover glass technique
        (glass, lights_glass, lights_gls_noemit on the Lights atlas). DSF's
        shared glass would draw it clear, with the glass texture's crack
        bands, over whatever lies behind: the DMC-12's tail lamps are a white
        reflector under a red, amber and white grid lens, and came out white
        with crack lines (runs host-20261023-082346, -083742). Stock tail lamps
        are opaque Lights-material texels, so such lenses go to the atlas.
        Front covers stay glass: on several FH1 cars (Challenger, S65, 300C,
        Golf R) their UVs land on red texels Forza doesn't show."""
        if (m.role != fm.GLASS or m.family != fm.LIGHTS or m.kind != "textured" or self.atlases is None
                or fm.LIGHTS not in self.families):
            return False
        pool = section.vertices_for(sub)
        used = {i for tri in sub.triangles() for i in tri}
        if not used or sum(pool[i].position[2] for i in used) / len(used) < LENS_REAR_Z:
            return False  # Forza's front is -z
        w, h, rgba = self.atlases.get(fm.LIGHTS)
        total = [0, 0, 0, 0]
        for i in used:
            u, v = m.uv(pool[i].uv0)
            x = min(w - 1, max(0, int(u * w)))
            y = min(h - 1, max(0, int(v * h)))
            for c in range(4):
                total[c] += rgba[(y * w + x) * 4 + c]
        r, g, b, a = (t / len(used) for t in total)
        return r - max(g, b) >= LENS_MIN_RED and a >= LENS_MIN_ALPHA

    def _uses_atlas(self, m: fm.SubMaterial) -> bool:
        if m.family is None or m.family not in self.families:
            return False
        return m.role == fm.TEXTURED or (self.swatch_trims and m.role == fm.TRIM and m.kind == "swatch")

    def _pack(self) -> None:
        width, height = self.size
        regions = [r for family in self.families for r in self.regions[family]]
        solid_keys = list(self.solid_colours)

        def attempt(s: float):
            sizes = [(_round_up((r.src[2] - r.src[0]) * self._family_scale(s, r.family)) + 2 * PAD,
                      _round_up((r.src[3] - r.src[1]) * self._family_scale(s, r.family)) + 2 * PAD) for r in regions]
            sizes += [(SOLID_CELL, SOLID_CELL)] * len(solid_keys)
            pos = skyline_pack(sizes, width, height)
            return (s, sizes, pos) if pos is not None else None

        # Largest uniform scale (never above the Forza resolution) that packs.
        best = attempt(1.0)
        lo, hi = 0.0, 1.0
        while best is None or (best[0] < 1.0 and hi - lo > 0.002):
            s = (lo + hi) / 2
            if s < 0.02:
                raise ValueError(f"textured regions don't fit a {width}x{height} atlas")
            result = attempt(s)
            if result is not None:
                best, lo = result, s
            else:
                hi = s
        s, sizes, pos = best
        for r, size, p in zip(regions, sizes, pos):
            r.dst, r.size = p, size
        for key, p in zip(solid_keys, pos[len(regions):]):
            self.solids[key] = p
        self.scale = {family: self._family_scale(s, family) for family in self.families}

    def _family_scale(self, s: float, family: str) -> float:
        """DSF texels per source texel for a family: the weighted uniform
        scale, never above the Forza resolution (a weighted family would
        otherwise be upscaled while the others still need room)."""
        return min(1.0, s * self.weights[family])

    # -- per-subsection answers --
    def submaterial(self, section, sub) -> fm.SubMaterial:
        key = (id(section), id(sub))
        if key not in self.resolved:
            self.resolved[key] = self._resolve(section, sub)
        return self.resolved[key]

    def material(self, section, sub) -> str:
        m = self.submaterial(section, sub)
        if id(sub) in self.cutout:
            return fm.SKIP
        if self._uses_atlas(m) and (m.kind == "textured" and id(sub) in self.atlas_subs
                                    or m.kind == "swatch" and m.colour is not None):
            return LIGHTS_MAT if m.family == fm.LIGHTS else CHASSIS
        if m.role == fm.TEXTURED:
            return fm.TRIM  # tiling/procedural lamp parts: untextured
        return m.role

    def solid_uv(self, key) -> tuple[float, float]:
        x, y = self.solids[key]
        return (x + SOLID_CELL / 2) / self.size[0], (y + SOLID_CELL / 2) / self.size[1]

    @property
    def paint_uv(self) -> tuple[float, float]:
        return self.solid_uv(("paint",))

    def triangle_uvs(self, section, sub, triangle_index: int, raw_uvs) -> tuple[list, object]:
        """Atlas UVs for one triangle of a textured subsection (forza UVs
        transformed for anything else), plus a tag for vertex sharing; the
        tag is DROP for triangles to leave out."""
        if (id(sub), triangle_index) in self.dropped:
            return None, DROP
        m = self.submaterial(section, sub)
        uvs = [m.uv(uv) for uv in raw_uvs]
        if self.material(section, sub) not in (CHASSIS, LIGHTS_MAT):
            return uvs, None
        if m.kind == "swatch":
            key = ("swatch",) + self._convert_texel(m.family, *m.colour)
            return [self.solid_uv(key)] * len(uvs), key
        n = self.owners[(id(sub), triangle_index)]
        region = self.regions[m.family][n]
        mapped = [region.map(min(1.0, max(0.0, u)), min(1.0, max(0.0, v)), self.src_sizes[m.family],
                             self.scale[m.family], self.size) for u, v in uvs]
        return mapped, (m.family, n)

    # -- rendering --
    def _red_lamp_curve(self):
        """This car's red lamp curve (red_lamp_curve) from the red texels inside its
        lights regions; None until the regions are known."""
        if getattr(self, "_red_curve_done", False):
            return self._red_curve
        regions = self.regions.get(fm.LIGHTS) if self.regions else None
        if not regions or self.atlases is None:
            return None
        sw, sh, src = self.atlases.get(fm.LIGHTS)
        area = sum((r.src[2] - r.src[0]) * (r.src[3] - r.src[1]) for r in regions)
        step = max(1, int((area / RED_CURVE_SAMPLES) ** 0.5))
        scores = []
        for reg in regions:
            for y in range(reg.src[1], min(sh, reg.src[3]), step):
                for x in range(reg.src[0], min(sw, reg.src[2]), step):
                    o = (y * sw + x) * 4
                    r, g, b, a = src[o], src[o + 1], src[o + 2], src[o + 3]
                    if lamp_hue(r, g, b) == "red":
                        scores.append(lamp_score(r, g, b, a))
        self._red_curve, self._red_curve_done = red_lamp_curve(scores), True
        return self._red_curve

    def _convert_texel(self, family: str, r: int, g: int, b: int, a: int) -> tuple[int, int, int, int]:
        """Forza texel -> DSF conventions (alpha = specular / glow mask)."""
        floor = self.lift.get(family)
        if floor:
            r, g, b = (int(floor + c * (255 - floor) / 255) for c in (r, g, b))
        tone = self.tone.get(family)
        if tone and not _paint_through(family, r, g, b, a):
            k, sat = tone
            grey = (r + g + b) / 3
            r, g, b = (max(0, min(255, int((grey + (c - grey) * sat) * k))) for c in (r, g, b))
        if family == fm.LIGHTS:
            if LIGHTS_ALPHA == "raw":  # analysis only: Forza's alpha as is
                return r, g, b, a
            return matched_lamp_texel(r, g, b, a, self._red_lamp_curve())
        if _paint_through(family, r, g, b, a):
            return self.plain  # paint-through body texel
        lum = (r + g + b) / 3
        return r, g, b, max(0, min(255, int((lum - CHASSIS_ALPHA_LUM) * CHASSIS_ALPHA_GAIN)))

    def render(self) -> bytearray:
        """The atlas as RGBA8 (plain paint colour where nothing is placed)."""
        width, height = self.size
        out = bytearray(bytes(self.plain) * (width * height))
        for family in self.families:
            sw, sh, src = self.atlases.get(family)
            s = self.scale[family]
            for n_region, r in enumerate(self.regions[family]):
                cut = self.cutout_colour is not None and (family, n_region) in self.alpha_only
                for dy in range(r.size[1]):
                    y = r.dst[1] + dy
                    fy0 = r.src[1] + (dy - PAD) / s
                    y0 = max(0, min(sh - 1, int(math.floor(fy0))))
                    y1 = max(y0 + 1, min(sh, int(math.floor(fy0 + 1 / s))))
                    for dx in range(r.size[0]):
                        x = r.dst[0] + dx
                        fx0 = r.src[0] + (dx - PAD) / s
                        x0 = max(0, min(sw - 1, int(math.floor(fx0))))
                        x1 = max(x0 + 1, min(sw, int(math.floor(fx0 + 1 / s))))
                        acc = [0, 0, 0, 0]
                        for yy in range(y0, y1):
                            o = (yy * sw + x0) * 4
                            row = src[o:o + (x1 - x0) * 4]
                            for c in range(4):
                                acc[c] += sum(row[c::4])
                        n = (y1 - y0) * (x1 - x0)
                        texel = self._convert_texel(family, *(v // n for v in acc))
                        if cut and acc[3] // n < 128:
                            texel = self.cutout_colour
                        o = (y * width + x) * 4
                        out[o:o + 4] = bytes(texel)
        for key, (x0, y0) in self.solids.items():
            colour = bytes(self.solid_colours[key])
            for y in range(y0, y0 + SOLID_CELL):
                o = (y * width + x0) * 4
                out[o:o + SOLID_CELL * 4] = colour * SOLID_CELL
        return out

    def dds(self, template: bytes | None = None) -> bytes:
        return dds_file(self.render(), *self.size, self.mips, template, self.fourcc)

    def summary(self) -> str:
        parts = []
        for family in self.families:
            parts.append(f"{family}: {len(self.regions[family])} regions at {self.scale[family]:.3f}x")
        return f"atlas {self.size[0]}x{self.size[1]}, " + ", ".join(parts) + f", {len(self.solids)} solid cells"


# --- DSF materials and textures in a DRSP ------------------------------------------

@dataclass
class Param:
    name: str
    value: bytes
    type: int


def parse_marp(data: bytes) -> Param:
    return Param(data[:32].split(b"\0", 1)[0].decode("latin-1"), data[32:48], struct.unpack_from("<I", data, 48)[0])


def current(stream, entry: Entry) -> bytes:
    """An entry's edited payload if it has one, else its bytes in the source."""
    return entry.payload if entry.payload is not None else payload(stream, entry)


def material_info(stream, maco: Entry) -> dict:
    """{uid, path, textures (set of UIDs in type-9 params), params (names)}."""
    uid = current(stream, child(maco, "MAHE"))[:8]
    path = current(stream, child(maco, "MATX")).split(b"\0", 1)[0].decode("latin-1")
    textures, names = set(), set()
    for marc in maco.children:
        if marc.tag != "MARC":
            continue
        for p in marc.children:
            if p.tag == "MARP":
                param = parse_marp(current(stream, p))
                names.add(param.name)
                if param.type == 9 and param.name == "g_texDiffuseMap":
                    textures.add(param.value[:8])
    return {"uid": uid, "path": path, "textures": textures, "params": names}


def material_kind(info: dict) -> str | None:
    """'chassis' or 'lights' for a host vehicle material, by its MATX name,
    else by the decal normal map only the lights material has."""
    path = info["path"].lower().replace("\\", "/").rsplit("/", 1)[-1]
    if "_lights" in path:
        return LIGHTS_MAT
    if "_chassis" in path:
        return CHASSIS
    if "g_texDecalNormalMap" in info["params"]:
        return LIGHTS_MAT
    return None


def retarget_material(stream, maco: Entry, textures: dict[bytes, bytes], new_uid: bytes | None = None) -> Entry:
    """Copy of a MACO whose g_texDiffuseMap UIDs are remapped through
    `textures` (old -> new) and, if given, whose own UID is `new_uid`."""
    e = copy.deepcopy(maco)
    mahe = child(e, "MAHE")
    if new_uid is not None:
        mahe.payload = new_uid + current(stream, mahe)[8:]
    for marc in e.children:
        if marc.tag != "MARC":
            continue
        for p in marc.children:
            if p.tag != "MARP":
                continue
            data = current(stream, p)
            param = parse_marp(data)
            if param.type == 9 and param.value[:8] in textures:
                p.payload = data[:32] + textures[param.value[:8]] + data[40:]
    return e


def texture_entry(stream, texc: Entry, dds: bytes, new_uid: bytes | None = None) -> Entry:
    """Copy of a TEXC holding `dds` (and `new_uid`, if given)."""
    e = copy.deepcopy(texc)
    child(e, "DXTC").payload = dds
    if new_uid is not None:
        child(e, "UIDC").payload = new_uid
    return e


@dataclass
class Installed:
    """What a converter needs after installing the atlas in one entry."""
    materials: dict[str, bytes]   # "chassis"/"lights" -> material UID to use in SBBH
    texture: bytes                # TEXC UID the materials sample
    layout: dict[bytes, tuple[int, int]]  # material UID -> (vertex stride, MODL flag)


STRIDES = {CHASSIS: (28, 5), LIGHTS_MAT: (32, 5)}


def host_materials(stream, drsp: Entry, lod1: bool = False) -> dict[str, tuple[Entry, dict]]:
    """Chassis/Lights materials a DRSP defines, first of each kind. With `lod1`,
    only the exterior's *_LOD1 ones (a full entry may define both)."""
    found = {}
    for e in drsp.children:
        if e.tag == "MACO":
            info = material_info(stream, e)
            kind = material_kind(info)
            if lod1 and "_lod1" not in info["path"].lower():
                continue
            if kind and kind not in found:
                found[kind] = (e, info)
    return found


def install_full(stream, drsp: Entry, dds: bytes, mode: str = "replace", uid_seed: bytes = b"\x00\x12\xfe\xed") -> Installed:
    """Install the atlas in a full (kind 0) entry's DRSP, in place.

    replace: the host TEXC that the Chassis/Lights materials sample gets the
             new DDS; every UID stays (exterior/interior references and the
             paint overlay keep working). The entry's size grows by the
             difference in DDS size only.
    clone:   a new TEXC (new UID) and copies of the Chassis and Lights
             materials (new UIDs) pointing at it are added; the host's stay.
    """
    found = host_materials(stream, drsp)
    if set(found) != {CHASSIS, LIGHTS_MAT}:
        raise ValueError(f"host entry needs a Chassis and a Lights material, found {sorted(found)}")
    texcs = {payload(stream, child(e, "UIDC"))[:8]: e for e in drsp.children if e.tag == "TEXC"}
    host_tex = next(iter(found[CHASSIS][1]["textures"]))
    if host_tex not in texcs:
        raise ValueError(f"chassis texture {host_tex.hex()} not in this entry")
    if mode == "replace":
        child(texcs[host_tex], "DXTC").payload = dds
        return Installed({k: v[1]["uid"] for k, v in found.items()}, host_tex,
                         {v[1]["uid"]: STRIDES[k] for k, v in found.items()})
    if mode != "clone":
        raise ValueError(mode)
    new_tex = uid_seed + b"\x00\x00\x00\x01"
    new_mats = {CHASSIS: uid_seed + b"\x00\x00\x00\x02", LIGHTS_MAT: uid_seed + b"\x00\x00\x00\x03"}
    kids = list(drsp.children)
    last_tex = max(i for i, e in enumerate(kids) if e.tag == "TEXC")
    kids.insert(last_tex + 1, texture_entry(stream, texcs[host_tex], dds, new_tex))
    last_mat = max(i for i, e in enumerate(kids) if e.tag == "MACO")
    for n, kind in enumerate((CHASSIS, LIGHTS_MAT)):
        kids.insert(last_mat + 1 + n, retarget_material(stream, found[kind][0], {host_tex: new_tex}, new_mats[kind]))
    drsp.children = kids
    return Installed(new_mats, new_tex, {new_mats[k]: STRIDES[k] for k in new_mats})


def install_exterior(stream, drsp: Entry, full: Installed, mode: str = "replace",
                     uid_seed: bytes = b"\x00\x12\xfe\xee", fallback: list[Entry] | None = None) -> Installed:
    """Use the clean exterior (kind 2) entry's own Chassis/Lights materials;
    they sample the full entry's TEXC by UID. In clone mode, copies pointing
    at the new texture are added.

    Most stock vehicles define one or both of their exterior's _LOD1
    materials in the interior or the full entry instead (the Desanne and 370Z
    keep both in the exterior); the `fallback` DRSPs (interior, full) are
    searched for the missing *_LOD1 ones, which the exterior then uses by
    UID, as stock exteriors do."""
    found = host_materials(stream, drsp)
    for other in fallback or []:
        for kind, value in host_materials(stream, other, lod1=True).items():
            found.setdefault(kind, value)
    if set(found) != {CHASSIS, LIGHTS_MAT}:
        raise ValueError(f"exterior entry needs a Chassis and a Lights material, found {sorted(found)}")
    if mode == "replace":
        return Installed({k: v[1]["uid"] for k, v in found.items()}, full.texture,
                         {v[1]["uid"]: STRIDES[k] for k, v in found.items()})
    new_mats = {CHASSIS: uid_seed + b"\x00\x00\x00\x02", LIGHTS_MAT: uid_seed + b"\x00\x00\x00\x03"}
    kids = list(drsp.children)
    last_mat = max(i for i, e in enumerate(kids) if e.tag == "MACO")
    for n, kind in enumerate((CHASSIS, LIGHTS_MAT)):
        old_tex = next(iter(found[kind][1]["textures"]))
        kids.insert(last_mat + 1 + n, retarget_material(stream, found[kind][0], {old_tex: full.texture},
                                                        new_mats[kind]))
    drsp.children = kids
    return Installed(new_mats, full.texture, {new_mats[k]: STRIDES[k] for k in new_mats})


def host_texture(stream, drsp: Entry) -> bytes | None:
    """The DDS the host's Chassis material samples (its header, size and mip count)."""
    found = host_materials(stream, drsp)
    if CHASSIS not in found:
        return None
    tex = next(iter(found[CHASSIS][1]["textures"]))
    for e in drsp.children:
        if e.tag == "TEXC" and payload(stream, child(e, "UIDC"))[:8] == tex:
            return payload(stream, child(e, "DXTC"))
    return None


@dataclass
class Binding:
    """Texturing bound to one entry's materials: what add_geometry needs."""
    texturing: Texturing
    installed: Installed

    def material(self, section, sub, shared: dict[str, bytes | None]) -> bytes | None:
        """DSF material UID for a subsection; `shared` maps the roles paint,
        glass, trim, tyre (and skip -> None) to DSF's shared materials."""
        kind = self.texturing.material(section, sub)
        if kind in (CHASSIS, LIGHTS_MAT):
            return self.installed.materials[kind]
        return shared.get(kind)

    def triangle_uvs(self, section, sub, triangle_index: int, raw_uvs):
        return self.texturing.triangle_uvs(section, sub, triangle_index, raw_uvs)

    @property
    def paint_uv(self) -> tuple[float, float]:
        # The paint block of the shared paint texture, whatever the mode (the
        # atlas's plain cell was wrong: paint doesn't sample the vehicle texture).
        return PAINT_TEXEL_UV

    @property
    def layout(self) -> dict[bytes, tuple[int, int]]:
        return self.installed.layout


def prepare(stream, full_vehc: Entry, exterior_vehc: Entry | None, car_dir: Path, sections, lods,
            mode: str = "replace", size: tuple[int, int] | None = None, mips: int | None = None,
            swatch_trims: bool = False, log=print,
            interior_vehc: Entry | None = None) -> tuple[Binding, Binding | None]:
    """Plan and encode the car's atlas and install it in a host vehicle's full
    entry (and its clean exterior entry). Call before rebuilding the DRSPs,
    and keep the entries' TEXC/MACO (forza2dsf.rebuild_drsp with
    keep_host_materials=True). Atlas size and mip count default to the host
    chassis texture's, so replace mode leaves the entry size unchanged."""
    full_drsp = child(child(full_vehc, "MDXN"), "DRSP")
    host = host_texture(stream, full_drsp)
    if host is not None:
        height, width = struct.unpack_from("<II", host, 12)
        size = size or (width, height)
        mips = mips or struct.unpack_from("<I", host, 28)[0] or DEFAULT_MIPS
    else:
        size, mips = size or DEFAULT_SIZE, mips or DEFAULT_MIPS
    # The atlas's background and plain cell: the Desanne's neutral texel (paint
    # itself samples the shared paint texture, PAINT_TEXEL_UV).
    plain = DEFAULT_PLAIN
    t = Texturing(Path(car_dir), size=size, mips=mips, swatch_trims=swatch_trims, plain=plain)
    t.plan(sections, set(lods))
    dds = t.dds(template=host)
    full = install_full(stream, full_drsp, dds, mode)
    ext = None
    if exterior_vehc is not None:
        fallback = ([child(child(interior_vehc, "MDXN"), "DRSP")] if interior_vehc is not None else []) + [full_drsp]
        ext = install_exterior(stream, child(child(exterior_vehc, "MDXN"), "DRSP"), full, mode, fallback=fallback)
    log(f"{t.summary()}; {mode}: texture {full.texture.hex()}, DDS {len(dds):#x} bytes "
        f"(host {len(host) if host else 0:#x})")
    return Binding(t, full), (Binding(t, ext) if ext else None)


# --- textured preview ---------------------------------------------------------------

FLAT = {  # shared materials (forza2dsf constants), preview colours
    "001283358afd1027": (150, 24, 28),   # paint
    "001283369afd1027": (90, 110, 130),  # glass
    "0012833b12fe1027": (40, 40, 44),    # trim
    "00128337b2fd1027": (22, 22, 22),    # tyre
}


VIEWS = {"side": (2, 1, 0, 1, 1), "top": (0, 2, 1, -1, 1), "front": (0, 1, 2, 1, 1), "rear": (0, 1, 2, -1, -1)}


def preview(archive: Path, vehc_index: int, png: Path, mask: int | None = None, size: int = 400,
            views: tuple[str, ...] = ("side", "top", "front", "rear")) -> dict:
    """Render side, top, front and rear views of one VEHC with its local
    materials textured (nearest texel of the top mip; textures are looked up
    by UID across the whole archive) and shared materials flat-coloured."""
    from chnk import read_chunk
    import vehicle_mesh as vm
    _, mesh = vm.load(archive, vehc_index)
    textures, materials = {}, {}
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        wanted = {vehc_index} | {i for i in range(max(0, vehc_index - 2), min(len(root.entries), vehc_index + 3))
                                 if root.entries[i].tag == "VEHC"}
        for i in wanted:
            drsp = child(child(root.entries[i], "MDXN"), "DRSP")
            for e in drsp.children:
                if e.tag == "TEXC":
                    textures[payload(stream, child(e, "UIDC"))[:8]] = decode_dds(payload(stream, child(e, "DXTC")))
                elif e.tag == "MACO":
                    info = material_info(stream, e)
                    materials[info["uid"]] = info
    subs = vm.lod_submodels(mesh, mask) if mask is not None else mesh.submodels
    stats = {"textured_triangles": 0, "flat_triangles": 0, "missing_texture": 0}
    tris = []
    for sub in subs:
        info = materials.get(sub.material)
        tex = None
        if info and info["textures"]:
            tex = textures.get(next(iter(info["textures"])))
            if tex is None:
                stats["missing_texture"] += 1
        flat = FLAT.get(sub.material.hex(), (200, 0, 200))
        for vb, tri in vm.triangles(mesh, sub):
            uv_el = vb.element(8)
            pts = [vm.position(vb, i) for i in tri]
            uvs = [vb.read(uv_el, i)[:2] for i in tri] if tex else None
            tris.append((pts, uvs, tex, flat))
            stats["textured_triangles" if tex else "flat_triangles"] += 1
    _render(tris, png, size, [VIEWS[v] for v in views])
    return stats


def _render(tris, path: Path, size: int, views: list) -> None:
    import vehicle_mesh as vm
    xs = [p[0] for t in tris for p in t[0]]; ys = [p[1] for t in tris for p in t[0]]; zs = [p[2] for t in tris for p in t[0]]
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) * 1.05
    centre = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2)
    width = size * len(views)
    pixels = bytearray([24, 24, 28] * width * size)
    light = (0.4, 0.8, 0.45)
    for view, (h, v, d, sign, flip) in enumerate(views):
        depth = [math.inf] * (size * size)
        for pts3, uvs, tex, flat in tris:
            ax, ay, az = (pts3[1][i] - pts3[0][i] for i in range(3))
            bx, by, bz = (pts3[2][i] - pts3[0][i] for i in range(3))
            nx, ny, nz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
            shade = 0.35 + 0.65 * abs(nx * light[0] + ny * light[1] + nz * light[2]) / length
            pts = [(flip * (p[h] - centre[h]) / span * size + size / 2,
                    size / 2 - (p[v] - centre[v]) / span * size, sign * (p[d] - centre[d])) for p in pts3]
            (px, py, pz), (qx, qy, qz), (rx, ry, rz) = pts
            area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
            if abs(area) < 1e-9:
                continue
            x0, x1 = max(int(min(p[0] for p in pts)), 0), min(int(max(p[0] for p in pts)) + 1, size)
            y0, y1 = max(int(min(p[1] for p in pts)), 0), min(int(max(p[1] for p in pts)) + 1, size)
            for y in range(y0, y1):
                for x in range(x0, x1):
                    w0 = ((qx - x) * (ry - y) - (qy - y) * (rx - x)) / area
                    w1 = ((rx - x) * (py - y) - (ry - y) * (px - x)) / area
                    w2 = 1 - w0 - w1
                    if w0 < 0 or w1 < 0 or w2 < 0:
                        continue
                    z = w0 * pz + w1 * qz + w2 * rz
                    k = y * size + x
                    if z >= depth[k]:
                        continue
                    depth[k] = z
                    if tex:
                        tw, th, trgba = tex
                        u = w0 * uvs[0][0] + w1 * uvs[1][0] + w2 * uvs[2][0]
                        t = w0 * uvs[0][1] + w1 * uvs[1][1] + w2 * uvs[2][1]
                        o = ((min(th - 1, max(0, int(t * th)))) * tw + min(tw - 1, max(0, int(u * tw)))) * 4
                        colour = trgba[o:o + 3]
                    else:
                        colour = flat
                    o = (y * width + view * size + x) * 3
                    pixels[o:o + 3] = bytes(min(255, int(c * shade)) for c in colour)
    vm.write_png(path, width, size, pixels)


# --- CLI ---------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("atlas", help="build a car's DSF chassis atlas")
    a.add_argument("car", type=Path)
    a.add_argument("--carbin")
    a.add_argument("--lods", default="1,2,3,4,5")
    a.add_argument("--size", default="512x512")
    a.add_argument("--mips", type=int, default=DEFAULT_MIPS)
    a.add_argument("--swatch-trims", action="store_true")
    a.add_argument("--png", type=Path, help="RGB preview")
    a.add_argument("--dds", type=Path)
    p = sub.add_parser("preview", help="render a VEHC entry with its textures")
    p.add_argument("archive", type=Path)
    p.add_argument("vehc", type=int)
    p.add_argument("--lod", type=int, help="MODL mask (4, 8, 16, 1, 2, 64)")
    p.add_argument("--png", type=Path, required=True)
    p.add_argument("--size", type=int, default=400)
    p.add_argument("--views", default="side,top,front,rear")
    args = parser.parse_args(argv)

    if args.cmd == "atlas":
        import carbin
        sections = carbin.parse((args.car / (args.carbin or f"{args.car.name}.carbin")).read_bytes())
        w, h = (int(x) for x in args.size.split("x"))
        t = Texturing(args.car, size=(w, h), mips=args.mips, swatch_trims=args.swatch_trims)
        t.plan(sections, {int(x) for x in args.lods.split(",")})
        print(t.summary())
        rgba = t.render()
        if args.png:
            import xds
            opaque = bytearray(rgba)
            opaque[3::4] = b"\xff" * (w * h)
            xds.write_png(args.png, w, h, opaque)
        if args.dds:
            args.dds.write_bytes(dds_file(rgba, w, h, args.mips))
            print(f"wrote {args.dds} ({dds_size(w, h, args.mips):#x} bytes)")
    else:
        stats = preview(args.archive, args.vehc, args.png, args.lod, args.size, tuple(args.views.split(",")))
        print(stats)


if __name__ == "__main__":
    sys.exit(main())
