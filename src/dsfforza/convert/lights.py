"""Working lamps for converted Forza cars: vertex light bytes and the UPVH light table.

How vanilla DSF lamps work (research/lights.md, confirmed-static):

- The UPVH light table holds one 0x40-byte entry per lamp: vec4 position,
  vec4 direction (car space, +z forward, +x the car's left), f32 pi, f32 pi,
  u32 id, u32 flags, 16 constant bytes. Type-17 hierarchy records point at
  the entries (+0x30) and hang under the part the lamp belongs to.
- flags byte 3 is the lamp's function, an index into a table in Driver.exe
  (file 0xA38F98) that gives the Lua light-type mask
  (GameVehicleResource.setLights): 1 Headlights, 2 Brakes, 4 DaytimeRunning,
  8 LeftIndicator, 16 RightIndicator, 32 Reverse, 64 Siren, 128
  DippedHeadlights, 256 FogLights. Byte 2 is the colour (4 amber, 6 red,
  8 white, 9 white/xenon), byte 0 a small variant number (2 on indicators, 1
  on centre brake lamps, phases on sirens).
- Every vertex of a lamp carries the lamp in vertex semantic 9, byte 2:
  light id + 1 (0 = not a lamp). 247 of 247 (light byte, id) pairs in nine
  sampled vehicles sit within 0.25 m of the entry with that id. Lamp
  geometry uses the vehicle's Lights material (alpha = glow mask) in the
  full and exterior entries.

Converted cars used to give every lamp triangle byte 0, so no lamp could
light, and kept the host's lamp positions. `LampPlan` classifies each Forza
lamp triangle by its technique (head_light, tail_light, indicator_left...)
and side, groups them into lamps, gives each lamp one of the host's light
entries (same count, same ids, so the hierarchy is untouched), and rewrites
those entries' position, direction and function. Spare entries are switched
off (function 0) and parked on the nearest lamp of the same end.

    uv run dsff lights plan out/cache/fh1/NIS_370Z_10 [--host-upvh upvh.bin]
    uv run dsff lights table <archive> --id 304
"""

from __future__ import annotations

import argparse
import math
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza.game import game_paths

# Lamp functions: (type byte, colour byte, variant byte). Type bytes index the
# exe's table: 1 Headlights, 2 Brakes, 3 DaytimeRunning, 4/5 Left/Right
# indicator, 6/7 Brakes+Left/Right, 8 Reverse, 0x0C Fog, 0x14/0x15
# DRL+Left/Right, 0x18 DRL+Brakes (tail and stop), 0x19/0x1A DRL+Brakes+L/R.
WHITE, XENON, AMBER, RED = 8, 9, 4, 6
LIGHT_TYPE_MASK = [
    0x0,
    0x1,
    0x2,
    0x4,
    0x8,
    0x10,
    0xA,
    0x12,
    0x20,
    0x40,
    0x80,
    0x41,
    0x100,
    0x40,
    0x40,
    0x40,
    0x800,
    0x1000,
    0x4,
    0x44,
    0xC,
    0x14,
    0xC,
    0x14,
    0x6,
    0xE,
    0x16,
    0xE,
    0x16,
    0x5,
]
# How bright the game draws a lamp (research/lights-re.md, "Lamp state in Driver.exe"): the
# colour it uploads is vehColourBoost x LAMP_COLOUR[b2] x TYPE_SCALE[b3] x brightness, and
# the glow of a texel is its alpha times that.
TYPE_SCALE = [0, 2, 1, 2, 0.5, 0.5, 0.5, 0.5, 1, *[10] * 9, 3, 10, *[1] * 9, 10]  # 0x00F3FB68
# The lights manager's colour table (0x008E6F00), linear, as read back in game.
LAMP_COLOUR = {
    0: (0, 0.0687686, 0.587755),
    1: (0, 0, 0.587755),
    2: (0.0591351, 0.411908, 0),
    3: (0, 0.587755, 0),
    4: (1, 0.229057, 0.00124854),
    5: (1, 0.36075, 0),
    6: (0.587755, 0, 0),
    7: (0.620376, 0, 0.0502853),
    8: (1, 0.957607, 0.760489),
    9: (0.875801, 0.891845, 1),
    10: (1, 0.640408, 0.000514284),
    11: (1, 0.916208, 0.000183946),
}
# A combined type (0x00E3AA98) with only DaytimeRunning of its functions on is drawn at this
# level (scale x brightness; 0x008E6C80); with another of its functions on, at its scale.
RUNNING_LEVEL = {**dict.fromkeys(range(0x18, 0x1D), 0.2), 0x13: 1.0, 0x1D: 1.0}
VEH_COLOUR_BOOST = 5.0  # the mood's lamp tint: 5 in every gameplay mood (10 in Fog)


def lamp_level(type_byte: int, mask: int) -> float:
    """Scale x brightness of a lamp of this type with the Lua light mask `mask` on, steady
    (blinking left out). In free roam the mask is DaytimeRunning (4), plus Brakes (2) or
    Reverse (32) from the driver; nothing sets Headlights (1)."""
    if not 0 < type_byte < len(LIGHT_TYPE_MASK):
        return 0.0
    on = LIGHT_TYPE_MASK[type_byte] & mask
    if not on:
        return 0.0
    if type_byte in RUNNING_LEVEL and on == 4:
        return RUNNING_LEVEL[type_byte]
    return float(TYPE_SCALE[type_byte])


MASK_NAMES = {
    1: "Headlights",
    2: "Brakes",
    4: "DaytimeRunning",
    8: "LeftIndicator",
    16: "RightIndicator",
    32: "Reverse",
    64: "Siren",
    128: "DippedHeadlights",
    256: "FogLights",
}

HEAD, POSITION, TAIL, BRAKE, REVERSE, INDICATOR, FOG, BRAKE_IND, DRL_IND, LENS = (
    "head",
    "position",
    "tail",
    "brake",
    "reverse",
    "indicator",
    "fog",
    "brake_ind",
    "drl_ind",
    "lens",
)

# Forza technique -> lamp function. Side-specific names carry the side.
TECHNIQUE_FUNCTION: dict[str, tuple[str, str | None]] = {}


def _add(function: str, names: str, side: str | None = None) -> None:
    for name in names.split():
        TECHNIQUE_FUNCTION[name] = (function, side)


_add(HEAD, "head_light head_light2 hidhead xenonhead oldhead fullbeam lights_gls_hidhead lights_gls_oldhead")
_add(POSITION, "drlwhite drlwhiteswitch drlred drlorange slorange")
_add(
    TAIL,
    "tail_light tail_light_lod0 tail_light_on tail_light2 taillightst taillight2S drlbrakered "
    "drlbrakeorange lights_gls_tail_light lights_gls_taillightst lights_gls_taillight2S",
)
_add(BRAKE, "white_brake")
_add(REVERSE, "reverse_light reverse_light_lod0 brkreverse lights_gls_reverse_light")
_add(INDICATOR, "indicator_left slinlorange lights_gls_indicator_left", "L")
_add(INDICATOR, "indicator_right slinrorange lights_gls_indicator_right", "R")
# lindtailside/rindtailside (tail, brake and indicator in one: the Corvette ZR1 09's four round
# lamps) brake: the cars that set them in ShaderSettings.xml give them a BrakeColor (Camaro ZL1
# 12, Cuda 426 71, Charger R/T 69, Explorer 10...; issue #103). As DRL + indicator (0x14/0x15)
# the ZR1 braked with 50 of its 630 cm2 of red lamps.
_add(BRAKE_IND, "brinl lindtailside", "L")
_add(BRAKE_IND, "brinr rindtailside", "R")
_add(DRL_IND, "drlinlorange drlinlred drlinlwhite", "L")
_add(DRL_IND, "drlinrorange drlinrred drlinrwhite", "R")
_add(FOG, "fogwhite fogred")
# Lenses take their function from their colour and position: red at the rear
# is a tail lamp, amber an indicator, clear the nearest lamp of its cluster.
_add(LENS, "detail_glass_red detail_glass_amber detail_glass_clear linred rinred")

# Default colour per function (lenses and fog lamps use the technique's own).
FUNCTION_COLOUR = {
    HEAD: WHITE,
    POSITION: WHITE,
    TAIL: RED,
    BRAKE: RED,
    REVERSE: WHITE,
    INDICATOR: AMBER,
    FOG: WHITE,
    BRAKE_IND: RED,
    DRL_IND: AMBER,
}
TECHNIQUE_COLOUR = {
    "hidhead": XENON,
    "xenonhead": XENON,
    "drlred": RED,
    "drlorange": AMBER,
    "slorange": AMBER,
    "fogred": RED,
    "drlinlred": RED,
    "drlinrred": RED,
    "lindtailside": RED,
    "rindtailside": RED,
    "drlbrakeorange": AMBER,
    "drlinlwhite": WHITE,
    "drlinrwhite": WHITE,
}

CENTRE_X = 0.3  # a lamp group whose |x| stays below this is a centre lamp
HIGH_BRAKE_RISE = 0.08  # a centre tail lamp this far above the side tails is the high brake lamp

# Group priority when the host has fewer light entries than the car has lamps;
# lower first. Groups that miss out share an entry of the same function and
# side, else of their fallback() function (same side first, then any side at the
# same end), else stay unlit. Red lenses over indicators
# (_red_lens_indicators) add two lamps to 58 cars; on full hosts they come
# after the reverse lamps (AUD_RS4_06, TOY_Camry_07 lost theirs), and the
# position lamps they push out (BMW_M5_12, VIP_Viper_13 on 147, 182) light
# with the head or tail lamps.
PRIORITY = {HEAD: 0, TAIL: 1, BRAKE: 2, INDICATOR: 3, DRL_IND: 3, REVERSE: 4, BRAKE_IND: 4.5, POSITION: 5, FOG: 6}


# Functions whose table type doesn't depend on the side: a left and a right lamp of one of these
# can share an entry (assign). Indicators and the combined types with one are per side.
SIDELESS = (HEAD, POSITION, TAIL, BRAKE, REVERSE, FOG)


def fallback(function: str, front: bool) -> str | None:
    """The function whose entry a lamp left without one shares: a
    brake-and-indicator lamp still brakes with the tail lamp, a position lamp
    lights with the head lamp (front) or the tail lamp (rear)."""
    if function == BRAKE_IND:
        return TAIL
    if function == POSITION:
        return HEAD if front else TAIL
    return None


ENTRY_CENTRE_X = 0.15  # m: a host light entry this close to the centre line is a centre one


def entry_place(upvh: bytes, offset: int) -> tuple[bool, str]:
    """Where a host light entry sits as shipped: (front, side), front by its
    position's z (+z forward), side "L" (+x), "R" or "C" (|x| < ENTRY_CENTRE_X).
    The entry's record hangs under the part at that place (headlamp parts and
    the front bumper at the front; tails under the body, boot or rear bumper)."""
    x, _y, z = struct.unpack_from("<3f", upvh, offset)
    return z > 0, "C" if abs(x) < ENTRY_CENTRE_X else ("L" if x > 0 else "R")


def _best_entry(lamp, candidates, place):
    """The candidate entry on the lamp's side (centre entries next, then any), first in table
    order; None without candidates."""
    for want in (lamp.side, "C", None):
        pick = next((e for e in candidates if want is None or place[e][1] == want), None)
        if pick is not None:
            return pick
    return None


HEAD_DAYTIME = 0x1D  # Headlights + DaytimeRunning
DAYTIME_SHARE = 0.25  # a side's own position lamps at least this share of its head lamps' area keep them 0x01


def type_byte(function: str, side: str) -> int:
    if function == HEAD:
        return 0x01
    if function == POSITION:
        return 0x03
    if function == TAIL:
        return 0x18
    if function == BRAKE:
        return 0x02
    if function == REVERSE:
        return 0x08
    if function == FOG:
        return 0x0C
    if function == INDICATOR:
        return 0x05 if side == "R" else 0x04
    if function == BRAKE_IND:
        return 0x1A if side == "R" else 0x19
    if function == DRL_IND:
        return 0x15 if side == "R" else 0x14
    raise ValueError(function)


def variant_byte(function: str) -> int:
    if function in (INDICATOR, BRAKE_IND, DRL_IND):
        return 2
    if function == BRAKE:
        return 1
    return 0


@dataclass
class Lamp:
    """One lamp: the triangles of one function on one side at one end.
    Position and direction come from the finest Forza LOD that has it."""

    function: str
    side: str  # "L" (DSF +x, the car's left), "R" or "C"
    front: bool
    colour: int
    per_lod: dict = field(default_factory=dict)  # lod -> [area, centre sum xyz, normal sum xyz, triangles]
    light_id: int | None = None  # host light entry id; vertices get id + 1
    cluster: int = 0  # 0 = the largest lamp of its function, side and end
    daytime: bool = False  # a head lamp that also lights with DaytimeRunning (0x1D; LampPlan.assign)

    def measure(self, lod: int, area: float, centre, normal) -> None:
        acc = self.per_lod.setdefault(lod, [0.0, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0])
        acc[0] += area
        for i in range(3):
            acc[1][i] += centre[i] * area
            acc[2][i] += normal[i] * area  # area-weighted, so small facets don't tilt it
        acc[3] += 1

    @property
    def _best(self):
        return self.per_lod[min(self.per_lod)] if self.per_lod else [0.0, [0.0] * 3, [0.0] * 3, 0]

    @property
    def area(self) -> float:
        return self._best[0]

    @property
    def triangles(self) -> int:
        return self._best[3]

    @property
    def key(self) -> tuple:
        return (self.function, self.side, self.front, self.cluster)

    @property
    def position(self) -> tuple[float, float, float]:
        area, centre = self._best[0] or 1.0, self._best[1]
        return tuple(c / area for c in centre)

    @property
    def direction(self) -> tuple[float, float, float]:
        if self.function == HEAD:
            return (0.0, 0.0, 1.0)
        n = self._best[2]
        length = math.sqrt(sum(c * c for c in n))
        if length < 1e-6:
            return (0.0, 0.0, 1.0 if self.front else -1.0)
        return tuple(c / length for c in n)

    @property
    def flags(self) -> int:
        return self.type << 24 | self.colour << 16 | variant_byte(self.function)

    @property
    def type(self) -> int:
        return HEAD_DAYTIME if self.function == HEAD and self.daytime else type_byte(self.function, self.side)


def _tri_geometry(pts, normals):
    a = [pts[1][i] - pts[0][i] for i in range(3)]
    b = [pts[2][i] - pts[0][i] for i in range(3)]
    cross = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    area = 0.5 * math.sqrt(sum(c * c for c in cross))
    centre = tuple(sum(p[i] for p in pts) / 3 for i in range(3))
    normal = tuple(sum(n[i] for n in normals) for i in range(3))
    return area, centre, normal


LENS_COLOUR = {"detail_glass_red": RED, "detail_glass_amber": AMBER, "linred": RED, "rinred": RED}
CLUSTER_GAP = 0.2  # metres between triangle centres that still belong to one lamp
CLEAR_LENS_REACH = 0.35  # m: how deep a lamp may lie behind a clear lens (_place_clear)
CLEAR_PIECE_SNAP = 0.001  # m: clear lens, cover or reflector triangles with corners this close are one piece
CLEAR_SAME_PLANE = 0.01  # m: a lamp surface this far in front of a lens triangle still lies behind it
# Lens texel colour (texel_class): saturated texels whose hue (degrees from
# red towards yellow) is below LENS_RED_HUE are red, up to 60 amber. Fleet
# (finest LOD, mean texel per triangle): 99% of `detail_glass_red` triangles at
# the rear sit at -15 to +10, `detail_glass_amber` at the front mostly at 10 to
# 35 (out/brakes/lensfleet.py).
LENS_MIN_SATURATION = 0.35
# Texels this transparent are glass, whatever their colour: VAU_Agila_11's
# clear lens over its reverse lamps samples (123, 0, 0) at alpha 0, and Lights
# alpha is the glow mask, so they never glow red anyway.
LENS_MIN_ALPHA = 64
LENS_RED_HUE = 10.0
# A front `detail_glass_amber` lens this yellow (mean texel hue, degrees) is a selective-
# yellow head lamp cover, not an indicator: the Panoz Esperante GTLM's lexan (hue 49 to 51)
# over its head lamps. Front amber lenses over indicators sit at 0 to 35 in the roster (CHR_
# Cuda426_71's 43 next); as indicators the Panoz's covered head lamps never lit by day.
YELLOW_LENS_HUE = 45.0
YELLOW_COVER_REACH = 0.15  # m: a head lamp triangle this close to a yellow lens triangle


def texel_class(texel) -> str | None:
    """ "red", "amber" or "white" (clear, grey, transparent or not red at
    all) for a mean texel (r, g, b, a); None without one."""
    if texel is None:
        return None
    r, g, b = texel[:3]
    hi, lo = max(r, g, b), min(r, g, b)
    if hi < 8 or (hi - lo) / hi < LENS_MIN_SATURATION or r < hi or texel[3] < LENS_MIN_ALPHA:
        return "white"
    return "red" if 60 * (g - b) / (hi - lo) < LENS_RED_HUE else "amber"


def yellow_lens(texel) -> bool:
    """Whether a mean lens texel (r, g, b, a) is a selective-yellow lens (YELLOW_LENS_HUE)."""
    if texel_class(texel) != "amber":
        return False
    r, g, b = texel[:3]
    return 60 * (g - b) / (max(r, g, b) - min(r, g, b)) >= YELLOW_LENS_HUE


def texel_sampler(atlases, reduce=None, of_mean=None):
    """(section, sub, triangle) -> the mean Forza texel (r, g, b, a) under one
    triangle of a Lights-texture subsection, else None. atlases: a
    forza_materials.AtlasCache of the car (dsf_materials.Texturing.atlases).
    reduce: texels (n, 4) -> the result instead of their mean; of_mean: the mean (r, g, b, a)
    -> the result instead of it."""
    import numpy as np

    from dsfforza.convert import dsf_materials, trim_art
    from dsfforza.convert import forza_materials as fm

    images: dict = {}
    materials: dict = {}
    # (id(section), id(sub)) -> (section, sub, {triangle: (its texels, their mean)}): the texels
    # of all a subsection's triangles from one lookup (trim_art.triangles_texels: the same texels
    # as triangle_texels one by one), the first time one of them is asked for, and their means
    # (integer sums, exact in any order, over the count: what texels.mean(0) gives).
    found: dict = {}

    def _texels(section, sub, m, image, tri):
        """(texels, their mean as floats or None) of one triangle."""
        h, w = image.shape[:2]
        pool = section.vertices_for(sub)
        key = (id(section), id(sub))
        if key not in found or found[key][0] is not section or found[key][1] is not sub:
            tris = [tuple(t) for t in sub.triangles()]
            pts = [[(u * w, v * h) for u, v in dsf_materials.triangle_uv(m, [pool[i].uv0 for i in t])] for t in tris]
            every, at = trim_art.triangles_texels(image, pts)
            counts = np.diff(at)
            full = np.nonzero(counts)[0]
            means = {}
            if len(full):
                sums = np.add.reduceat(every, at[:-1][full], axis=0).astype(np.float64)
                means = dict(zip(full.tolist(), (sums / counts[full, None]).tolist(), strict=True))
            found[key] = (
                section,
                sub,
                {t: (every[at[k] : at[k + 1]], means.get(k)) for k, t in enumerate(tris)},
            )
        got = found[key][2].get(tri)
        if got is None:  # not one of the subsection's own triangles
            uvs = dsf_materials.triangle_uv(m, [pool[i].uv0 for i in tri])
            texels = trim_art.triangle_texels(image, [(u * w, v * h) for u, v in uvs])
            got = (texels, texels.mean(0).tolist() if len(texels) else None)
        return got

    def sample(section, sub, tri):
        key = id(sub)
        if key not in materials:
            materials[key] = dsf_materials.resolve_lamp(section, sub, fm.classify, atlases)
        m = materials[key]
        if m.family != fm.LIGHTS or m.kind not in ("textured", "swatch"):
            return None
        if fm.LIGHTS not in images:
            try:
                w, h, rgba = atlases.get(fm.LIGHTS)
            except (FileNotFoundError, OSError):
                images[fm.LIGHTS] = None
            else:
                import numpy as np

                images[fm.LIGHTS] = np.frombuffer(bytes(rgba), dtype=np.uint8).reshape(h, w, 4)
        image = images[fm.LIGHTS]
        if image is None:
            return None
        texels, mean = _texels(section, sub, m, image, tuple(tri))
        if not len(texels):
            return None
        if reduce is not None:
            return reduce(texels)
        return tuple(mean) if of_mean is None else of_mean(mean)

    return sample


def _seen(texels) -> str | None:
    """texel_class of a triangle's mean texel, whatever its alpha (the Lights alpha is the glow
    mask; a baked lens texel already holds what shows through the glass). The mean, as the eye
    averages a lamp's pattern: most texels of the Ford Raptor's red tail bowls are pale
    highlights, while a triangle across its white reverse window and the red is red on the
    whole, as before."""
    return texel_class((*texels[:, :3].mean(0), 255))


def _seen_mean(mean) -> str | None:
    """_seen from the triangle's mean texel (texel_sampler's: the same mean)."""
    return texel_class((*mean[:3], 255))


def seen_sampler(atlases):
    """(section, sub, triangle) -> the class (red, amber, white) of most of the texels under one
    triangle of a Lights-texture subsection, else None (LampPlan.build `seen`). atlases: the
    ones the car is drawn from, lenses baked (dsf_materials.Texturing.atlases)."""
    return texel_sampler(atlases, of_mean=_seen_mean)


@dataclass
class Tri:
    sub: object
    k: int
    function: str
    side: str
    front: bool
    colour: int
    area: float
    centre: tuple
    normal: tuple
    lod: int
    pts: tuple = ()  # corners (lamp-technique triangles; _red_lens_indicators)
    seen: str | None = None  # texel class of the drawn texels at the rear (LampPlan.build `seen`)


def _hull_xy(points):
    """Convex hull (counter-clockwise, (k, 2)) of points (n, 2): Andrew's monotone chain."""
    import numpy as np

    pts = sorted(set(map(tuple, np.round(np.asarray(points, float), 5).tolist())))
    if len(pts) < 3:
        return np.array(pts, float).reshape(-1, 2)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1], float)


def _in_hull(hull, xy):
    """Which points xy (n, 2) lie inside a counter-clockwise convex hull (k, 2)."""
    a, b = hull, hull[[*range(1, len(hull)), 0]]
    ex, ey = (b - a)[:, 0][None], (b - a)[:, 1][None]
    return (ex * (xy[:, 1:2] - a[:, 1][None]) - ey * (xy[:, 0:1] - a[:, 0][None]) >= 0).all(1)


def _inside_xy(corners, x: float, y: float):
    """Which triangles (corners (n, 3, 3)) hold the point (x, y) seen along z."""
    a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]

    def side(p, q):
        return (q[:, 0] - p[:, 0]) * (y - p[:, 1]) - (q[:, 1] - p[:, 1]) * (x - p[:, 0])

    d1, d2, d3 = side(a, b), side(b, c), side(c, a)
    return ((d1 >= 0) & (d2 >= 0) & (d3 >= 0)) | ((d1 <= 0) & (d2 <= 0) & (d3 <= 0))


def _pieces(triangles) -> list[list[int]]:
    """Indices of `triangles` (corner points) grouped into connected pieces: triangles with a
    corner within CLEAR_PIECE_SNAP of each other's are one piece."""
    parent = list(range(len(triangles)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict = {}
    for n, pts in enumerate(triangles):
        for p in pts:
            key = tuple(round(c / CLEAR_PIECE_SNAP) for c in p)
            parent[root(n)] = root(seen.setdefault(key, n))
    groups: dict[int, list[int]] = {}
    for n in range(len(triangles)):
        groups.setdefault(root(n), []).append(n)
    return list(groups.values())


def _single_link(points, gap: float) -> list[list[int]]:
    """Indices of `points` grouped so that points closer than `gap` share a group."""
    cells: dict[tuple, list[int]] = {}
    for i, p in enumerate(points):
        cells.setdefault(tuple(int(math.floor(c / gap)) for c in p), []).append(i)
    parent = list(range(len(points)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for (cx, cy, cz), members in cells.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in cells.get((cx + dx, cy + dy, cz + dz), ()):
                        for i in members:
                            if i < j and math.dist(points[i], points[j]) < gap:
                                parent[root(i)] = root(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(points)):
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())


def _high_brake(records) -> None:
    """A centre tail lamp above the side tail lamps (or the only tail lamp in
    the middle) is the high-mounted stop lamp: brakes only."""

    def mean_y(rs):
        finest = min(r.lod for r in rs)
        rs = [r for r in rs if r.lod == finest]
        return sum(r.centre[1] * r.area for r in rs) / (sum(r.area for r in rs) or 1.0)

    centre = [r for r in records if r.function == TAIL and r.side == "C" and not r.front]
    sides = [r for r in records if r.function == TAIL and r.side != "C" and not r.front]
    if centre and sides and mean_y(centre) > mean_y(sides) + HIGH_BRAKE_RISE:
        for r in centre:
            r.function = BRAKE


def _mean_texel(section, sub, tris, texel):
    """Mean of texel (texel_sampler) over a subsection's triangles; None without one."""
    got = [t for tri in tris if (t := texel(section, sub, tri)) is not None]
    return tuple(sum(t[c] for t in got) / len(got) for c in range(4)) if got else None


def _yellow_head_covers(records, yellow_front: list, from_lens: set) -> None:
    """A front selective-yellow lens (yellow_lens) lights as a head lamp when a head lamp
    triangle that isn't a lens lies within YELLOW_COVER_REACH of it (the Panoz Esperante
    GTLM's lexan); one with no head lamp near (FER_83_F430GT_10's bumper lens) stays an
    indicator."""
    heads = [r.centre for n, r in enumerate(records) if r.function == HEAD and r.front and n not in from_lens]
    for n in yellow_front:
        r = records[n]
        if any(math.dist(r.centre, h) < YELLOW_COVER_REACH for h in heads):
            r.function, r.colour = HEAD, WHITE


def _lens_classes(section, sub, tris, pts_all, texel, colour: int, mid_z: float):
    """Texel class per triangle of a lens subsection (texel_class), and whether
    a rear amber-technique lens is a red one: more than half its rear area on
    red texels. The FM4 Lotus Carlton's tail lamps are one
    `detail_glass_amber` lens on red texels (mean (140, 40, 40)) with the
    `tail_light` bulb hidden behind it; taken by name it lit as an indicator
    only and the car showed no brake light (user report on out/final4)."""
    classes = [texel_class(texel(section, sub, t)) for t in tris]
    if colour != AMBER:
        return classes, False
    red = total = 0.0
    for cls, pts in zip(classes, pts_all):
        if sum(p[2] for p in pts) / 3 <= mid_z:
            area = _tri_geometry(pts, [(0, 0, 0)] * 3)[0]
            total += area
            red += area if cls == "red" else 0.0
    return classes, red > total / 2


INDICATING = (INDICATOR, BRAKE_IND, DRL_IND)
# Rear red lamps ranked per planned lamp onto vanilla's visible tail alpha (LampPlan.rear_tail_lamp).
REAR_TAIL_FUNCTIONS = (TAIL, BRAKE, BRAKE_IND)


def _red_lens_indicators(records, from_lens: set, red_indicators: list, red_by_texel: set) -> None:
    """What a red rear lens triangle lights with, when the lamp behind it
    isn't a tail lamp. Vanilla lamps are opaque, so a bulb behind a lens never
    shows: the lens has to light for it.

    The bulb behind a lens triangle is the nearest lamp triangle (same LOD,
    not a lens, within CLEAR_LENS_REACH along the view) whose outline, seen
    from behind, holds the lens triangle's centre; failing that, the nearest
    across the view (x, y).

    - A reverse bulb (outline only), under a lens that is red by its texels
      rather than its technique: a reverse lamp, as these lenses lit before
      the texels decided. Forza draws some reverse sections on red lens
      texels of clear or lamp-cover lenses (PEU_207RC_07, TOY_CelicaSSI_03,
      CHR_300C_08); as tail lamps they hid the bulb and the car showed no
      reverse light. `detail_glass_red` lenses stay tail lamps, as before.
    - An indicator: a red lamp that brakes and indicates (BRAKE_IND, types
      0x19/0x1A, as on vanilla US-style cars): FOR_GT_05's red lenses over its
      drlinl/drlinr lamps, MIN_Coopers_11's red glass over its
      indicator_left/right bulbs.
    - A red "amber" lens on a side without any other rear indicator
      (MIT_GTO_97: (76, 0, 0) texels, no amber at the rear) also brakes and
      indicates."""
    import numpy as np

    bulbs: dict = {}  # lod -> [(corners, centre, function)] of rear lamp triangles that are not lenses
    for n, r in enumerate(records):
        if n not in from_lens and not r.front and r.pts:
            bulbs.setdefault(r.lod, []).append((r.pts, r.centre, r.function))
    arrays = {
        lod: (np.array([b[0] for b in bs], dtype=float), np.array([b[1] for b in bs], dtype=float), [b[2] for b in bs])
        for lod, bs in bulbs.items()
    }

    def behind(r) -> tuple[str | None, bool]:
        """(function of the bulb behind r, whether its outline holds r's centre)."""
        if r.lod not in arrays:
            return None, False
        corners, centres, functions = arrays[r.lod]
        x, y, z = r.centre
        dz = np.abs(centres[:, 2] - z)
        reach = dz < CLEAR_LENS_REACH
        a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]

        def side(p, q):  # which side of edge p -> q the centre lies on, seen from behind
            return (q[:, 0] - p[:, 0]) * (y - p[:, 1]) - (q[:, 1] - p[:, 1]) * (x - p[:, 0])

        d1, d2, d3 = side(a, b), side(b, c), side(c, a)
        inside = reach & (((d1 >= 0) & (d2 >= 0) & (d3 >= 0)) | ((d1 <= 0) & (d2 <= 0) & (d3 <= 0)))
        if inside.any():
            return functions[int(np.where(inside, dz, np.inf).argmin())], True
        across = np.where(reach, np.linalg.norm(centres[:, :2] - (x, y), axis=1), np.inf)
        i = int(across.argmin())
        return (functions[i], False) if across[i] < CLEAR_LENS_REACH else (None, False)

    def indicate(r) -> None:
        r.function = BRAKE_IND
        if r.side == "C":
            r.side = "L" if r.centre[0] > 0 else "R"

    for n in from_lens:
        r = records[n]
        if r.function != TAIL:
            continue
        function, outline = behind(r)
        if function == REVERSE and outline and n in red_by_texel:
            r.function, r.colour = REVERSE, WHITE
        elif function in INDICATING:
            indicate(r)
    indicating = {r.side for r in records if not r.front and r.function in INDICATING}
    for n in red_indicators:
        if records[n].function == TAIL and records[n].side not in indicating:
            indicate(records[n])


# A rear indicator lamp with this share of its area drawn red is a red indicator that brakes too
# (LampPlan._place_clear); with less, its clear or amber part stays an amber indicator (the
# Mercedes S65 AMG's clear strip).
RED_INDICATOR_SHARE = 0.9


REFLECTORS = ("textured_reflector", "reflector")
LAMP_COVERS = ("glass", "lights_glass", "lights_gls_noemit")  # = glass.LAMP_MATERIALS
HEAD_UNIT_DEPTH = 0.3  # m behind a headlight section's cover (dsf_materials.COVER_DEPTH)
HEAD_UNIT_PAD = 0.02  # m round its outline


def _headlight_reflectors(sections, place, mid_z: float) -> list[Tri]:
    """Head-lamp triangles for a car without head-lamp techniques or clear front
    lenses: the reflectors of its headlight sections (headlightL/R, glassLHL/RHL)
    ahead of the wheelbase midpoint, else their lamp-cover glass (MIT_HKS_06,
    PEU_3_905EVO_93). Race cars whose headlights are decals (badge) get none."""
    lamp_sections = [s for s in sections if re.match(r"headlight|glass[lr]hl", s.name.lower())]
    for materials in (REFLECTORS, LAMP_COVERS):
        out = _front_tris(lamp_sections, materials, place, mid_z)
        if out:
            return out
    return []


def _bare_head_units(sections, place, mid_z: float, records) -> list[Tri]:
    """Head-lamp triangles for the head lamp units of a car that draws its head lamps elsewhere
    with a head lamp technique: the front reflectors (any section) behind a headlight
    section's cover (headlightL/R, glassLHL/RHL; up to HEAD_UNIT_DEPTH behind it, inside its
    outline) with no head lamp triangle in it. The Ford Mustang GT 05's round bowls are a
    tiling textured_reflector in `body` under the plain glass of headlightL/R, and its only
    head lamp technique is the head_light under the amber bumper lenses, so the bowls got no
    light byte and never glowed (issue #158)."""
    import numpy as np

    heads = np.array([r.centre for r in records if r.function == HEAD and r.front]).reshape(-1, 3)
    boxes = []
    for section in sections:
        if not re.match(r"headlight|glass[lr]hl", section.name.lower()):
            continue
        for sub in section.subsections:
            if sub.material not in LAMP_COVERS:
                continue
            pool = section.vertices_for(sub)
            pts = np.array([place.point(pool[i].position) for t in sub.triangles() for i in t]).reshape(-1, 3)
            if not len(pts) or pts[:, 2].mean() <= mid_z:
                continue
            # One box per side: a section may hold both covers.
            for side in (pts[:, 0] > 0, pts[:, 0] <= 0):
                if side.sum() < 3:
                    continue
                q = pts[side]
                lo = q.min(0) - (HEAD_UNIT_PAD, HEAD_UNIT_PAD, HEAD_UNIT_DEPTH)
                hi = q.max(0) + HEAD_UNIT_PAD
                if not (((heads >= lo) & (heads <= hi)).all(1)).any():
                    boxes.append((lo, hi))
    if not boxes:
        return []
    out = []
    for r in _front_tris(sections, REFLECTORS, place, mid_z):
        c = np.array(r.centre)
        if any(((c >= lo) & (c <= hi)).all() for lo, hi in boxes):
            side = "L" if c[0] > 0 else "R"
            out.append(Tri(r.sub, r.k, HEAD, side, True, WHITE, r.area, r.centre, r.normal, r.lod, r.pts))
    return out


def _front_tris(sections, materials, place, mid_z: float) -> list[Tri]:
    out = []
    for section in sections:
        for sub in section.subsections:
            if sub.material not in materials:
                continue
            pool = section.vertices_for(sub)
            tris = list(sub.triangles())
            if not tris:
                continue
            pts_all = [[place.point(pool[i].position) for i in t] for t in tris]
            max_x = max(abs(p[0]) for pts in pts_all for p in pts)
            for k, (t, pts) in enumerate(zip(tris, pts_all)):
                area, centre, normal = _tri_geometry(pts, [place.normal(pool[i].normal) for i in t])
                if centre[2] <= mid_z:
                    continue
                side = "C" if max_x < CENTRE_X else ("L" if centre[0] > 0 else "R")
                out.append(Tri(sub, k, HEAD, side, True, WHITE, area, centre, normal, sub.lod, tuple(pts)))
    return out


@dataclass
class LampPlan:
    """Lamp per Forza triangle, and the host light entries the lamps use.

    Build with `LampPlan.build(sections, place, mid_z)`, then `assign(upvh)`.
    `s9(sub, k, s9)` gives a triangle's vertex semantic-9 bytes with the light
    byte set; `patch_upvh(upvh)` rewrites the light table."""

    lamps: dict = field(default_factory=dict)  # key -> Lamp
    tri_lamp: dict = field(default_factory=dict)  # (id(sub), triangle index) -> lamp key
    unlit: list = field(default_factory=list)  # lamp keys that got no entry
    slots: list = field(default_factory=list)  # per host light entry: (offset, id, Lamp | None)

    @classmethod
    def build(cls, sections, place, mid_z: float, lenses=frozenset(), texel=None, seen=None) -> LampPlan:
        """sections: Forza body sections (all LODs). place: forza2dsf.Placement.
        mid_z: DSF z of the wheelbase midpoint (front lamps are ahead of it).
        lenses: id(sub) of lamp-cover glass subsections drawn as lamp texels
        (dsf_materials.Texturing.lens_subs); they light like clear lenses,
        with the lamp behind each triangle (the DMC-12's grid lens: tail,
        indicator and reverse cells over one reflector).
        texel: (section, sub, triangle) -> mean Forza texel or None
        (texel_sampler); rear lens triangles on red texels are tail lamps
        whatever their technique says (_lens_classes).
        seen: (section, sub, triangle) -> the class of the texels the car draws it with
        (seen_sampler over the lens bake's atlas, dsf_materials.Texturing.atlases): rear lamp
        triangles on red ones light only with a red lamp (_place_clear)."""
        plan = cls()
        records, clear = [], []  # clear lenses are placed after the lamps
        front_reflectors = []  # unlit, but a lamp a lens in front of them shows (_place_clear)
        red_indicators = []  # records of amber-technique lens triangles on red texels
        red_by_texel = set()  # records of lens triangles red by their texels, not their technique
        from_lens = set()  # records of lens triangles
        yellow_front = []  # records of front selective-yellow lens triangles (yellow_lens)
        for section in sections:
            for sub in section.subsections:
                entry = TECHNIQUE_FUNCTION.get(sub.material)
                if entry is None and id(sub) in lenses:
                    entry = (LENS, None)
                rear_reflector = entry is None and sub.material in REFLECTORS
                if rear_reflector:
                    entry = (LENS, None)
                if entry is None:
                    continue
                function, named_side = entry
                pool = section.vertices_for(sub)
                tris = list(sub.triangles())
                if not tris:
                    continue
                pts_all = [[place.point(pool[i].position) for i in t] for t in tris]
                max_x = max(abs(p[0]) for pts in pts_all for p in pts)
                colour = TECHNIQUE_COLOUR.get(sub.material, FUNCTION_COLOUR.get(function, WHITE))
                classes, red_lens = None, False
                yellow = False
                if function == LENS:
                    colour = LENS_COLOUR.get(sub.material, WHITE)
                    if texel is not None:
                        classes, red_lens = _lens_classes(section, sub, tris, pts_all, texel, colour, mid_z)
                        if colour == AMBER:
                            yellow = yellow_lens(_mean_texel(section, sub, tris, texel))
                for k, (t, pts) in enumerate(zip(tris, pts_all)):
                    normals = [place.normal(pool[i].normal) for i in t]
                    area, centre, normal = _tri_geometry(pts, normals)
                    front = centre[2] > mid_z
                    shown = None if front or seen is None else seen(section, sub, t)
                    if rear_reflector:
                        # A rear lamp's reflector lights with the lamp it belongs to, like a
                        # clear lens: under a clear cover it is what the player sees (the Devon
                        # GTX's red reflectors round a 15-triangle tail_light), and unlit the
                        # tail lamps were dark at night (issue #160). Front reflectors stay
                        # unlit: head lamps glow by day from their lens or bulb (#124).
                        reflector = Tri(
                            sub, k, LENS, "C", front, WHITE, area, centre, normal, sub.lod, tuple(pts), shown
                        )
                        (front_reflectors if front else clear).append(reflector)
                        continue
                    if named_side:
                        side = named_side
                    elif max_x < CENTRE_X:
                        side = "C"
                    else:
                        side = "L" if centre[0] > 0 else "R"
                    f, c = function, colour
                    if f == HEAD and not front:
                        # A head lamp technique behind the wheelbase midpoint is no head lamp:
                        # vanilla has no rear 0x01 entry and lights no plate. The 510's coarse
                        # LOD 2 tail lamps, the Mini 65's plate lamp on its boot, the A4 Avant's
                        # `fullbeam` tail lamp bulbs (#264): as rear head lamps they took rear
                        # light entries from the real rear lamps and lit white with the head
                        # lamps. Placed like a clear lens, with the lamp it lies in or over.
                        clear.append(
                            Tri(sub, k, LENS, side, front, WHITE, area, centre, normal, sub.lod, tuple(pts), shown)
                        )
                        continue
                    if f == POSITION:
                        c = WHITE if front else RED  # front parking / DRL, rear tail marker
                    # What the player sees at the rear is the lens texture: red
                    # texels are a tail lamp whatever the technique, and a rear
                    # "amber" lens that is mostly red texels is a red lens.
                    if (
                        f == LENS
                        and classes is not None
                        and not front
                        and (classes[k] == "red" or (red_lens and classes[k] != "amber"))
                    ):
                        if c == AMBER:
                            side = side if side != "C" else ("L" if centre[0] > 0 else "R")
                            red_indicators.append(len(records))
                        if c != RED:
                            red_by_texel.add(len(records))
                        c = RED
                    if f == LENS:
                        # Amber lenses are indicators (a yellow one at the front may cover a
                        # head lamp, _yellow_head_covers), red ones at the rear tail lamps;
                        # clear ones light with the lamp behind them.
                        if c == AMBER:
                            f = INDICATOR
                            if yellow and front:
                                yellow_front.append(len(records))
                            side = side if side != "C" else ("L" if centre[0] > 0 else "R")
                        elif c == RED and not front:
                            f = TAIL
                        else:
                            clear.append(
                                Tri(sub, k, LENS, side, front, c, area, centre, normal, sub.lod, tuple(pts), shown)
                            )
                            continue
                        from_lens.add(len(records))
                    records.append(Tri(sub, k, f, side, front, c, area, centre, normal, sub.lod, tuple(pts), shown))
        if not any(r.function == HEAD and r.front for r in records):
            # No head-lamp technique (CHE_CorvetteGS_10, DOD_RamSRT10_06,
            # FOR_MustangSVTCobraR_93 and several race cars draw the lamp as a
            # reflector under a clear lens): the clear lenses at the front are the
            # head lamps, else the headlight sections' reflectors.
            front_clear = [r for r in clear if r.front]
            if front_clear:
                clear = [r for r in clear if not r.front]
                records += [
                    Tri(r.sub, r.k, HEAD, r.side, True, WHITE, r.area, r.centre, r.normal, r.lod, r.pts)
                    for r in front_clear
                ]
            else:
                records += _headlight_reflectors(sections, place, mid_z)
        else:
            records += _bare_head_units(sections, place, mid_z, records)
        _yellow_head_covers(records, yellow_front, from_lens)
        _red_lens_indicators(records, from_lens, red_indicators, red_by_texel)
        _high_brake(records)
        plan._cluster(records)
        plan._place_clear(
            clear, records, red_by_texel, {id(sub): s.name for s in sections for sub in s.subsections}, front_reflectors
        )
        return plan

    def _cluster(self, records) -> None:
        """Group triangles by (function, side, end) and split each group into
        lamps CLUSTER_GAP apart on its finest LOD: a side repeater is not the
        headlamp's indicator, a bumper driving lamp not the headlamp. Other
        LODs' triangles join the nearest lamp of their group."""
        groups: dict[tuple, list] = {}
        for r in records:
            groups.setdefault((r.function, r.side, r.front), []).append(r)
        for (function, side, front), rs in groups.items():
            finest = min(r.lod for r in rs)
            base = [r for r in rs if r.lod == finest]
            lamps = []
            for members in _single_link([r.centre for r in base], CLUSTER_GAP):
                lamp = Lamp(function, side, front, base[members[0]].colour)
                for m in members:
                    lamp.measure(finest, base[m].area, base[m].centre, base[m].normal)
                lamps.append(lamp)
            # The main lamp first: the largest, but for indicators the one at the
            # car's end (the headlamp or tail-lamp indicator, not the side repeater).
            lamps.sort(key=lambda l: -abs(l.position[2]) if function == INDICATOR else -l.area)
            for n, lamp in enumerate(lamps):
                lamp.cluster = n
                self.lamps[lamp.key] = lamp
            anchors = [(lamp, lamp.position) for lamp in lamps]
            for r in rs:
                lamp = min(anchors, key=lambda a: math.dist(a[1], r.centre))[0]
                if r.lod != finest:
                    lamp.measure(r.lod, r.area, r.centre, r.normal)
                self.tri_lamp[(id(r.sub), r.k)] = lamp.key

    def _place_clear(self, clear, records, skip=frozenset(), sections=None, front_reflectors=()) -> None:
        """What clear lenses, lamp covers baked as lenses and rear reflectors light with: a lamp
        that lies behind them, seen along the car's axis. They used to light with the nearest
        lamp within CLEAR_LENS_REACH, so a cover over a reflector with no lamp of its own lit
        with a lamp beside it: the VW GTI VR6's bumper fog covers glowed with its head lamps,
        which Forza never lights (issue #174).

        - A rear reflector piece (triangles joined by their corners, CLEAR_PIECE_SNAP) lights
          with the lamps whose triangles lie inside its outline along z (its convex hull: a bowl
          often has a hole where the bulb sits), within CLEAR_LENS_REACH either way along z,
          each triangle with the nearest of them.
        - A lens or cover triangle lights with the lamp surface right behind it (inside its
          outline along z, up to CLEAR_LENS_REACH deep; lamp triangles and the reflectors lit
          above), the rest of its piece with the nearest of the lamps the piece has behind it.
          Front reflectors (`front_reflectors`) take a lamp by the reflector rule too, but only
          as what lies behind a lens: they stay unlit themselves (head lamps glow by day from
          their lens or bulb, #124), while a lens over a projector bowl beside the bulb's
          lights with it (the Alfa Brera's three bowls, one bulb).
        - A piece with no lamp behind it or in it lights with the nearest lamp of its own Forza
          section within CLEAR_LENS_REACH (the VW GTI VR6's small reflector cups beside its tail
          bulbs); a lens or cover piece, else with one whose outline along z overlaps its own
          within CLEAR_LENS_REACH (the Dodge SRT-4's head lamp lens, wrapped round its bulb);
          else it stays unlit.

        Lamp centres for "nearest" leave out the lens triangles that are red by their texels
        (`skip`, record indices): with them a tail lamp's centre moved onto its red lens and
        clear lenses that lit with it went to the reverse lamp (JAG_XFR_10). sections: id(sub)
        -> its section's name."""
        import numpy as np

        centres = {}  # lamp key -> [area, weighted centre sum] on the lamp's finest LOD
        surfaces: dict = {}  # (lod, front) -> ([corners], [lamp key]) of lamp triangles
        own: dict = {}  # (section, lod, front) -> lamp keys of its lamp triangles
        sections = sections or {}
        for n, r in enumerate(records):
            key = self.tri_lamp.get((id(r.sub), r.k))
            if key is None:
                continue
            own.setdefault((sections.get(id(r.sub)), r.lod, r.front), set()).add(key)
            if r.pts:
                pts, keys = surfaces.setdefault((r.lod, r.front), ([], []))
                pts.append(r.pts)
                keys.append(key)
            if n in skip or r.lod != min(self.lamps[key].per_lod):
                continue
            acc = centres.setdefault(key, [0.0, [0.0, 0.0, 0.0]])
            acc[0] += r.area
            for i in range(3):
                acc[1][i] += r.centre[i] * r.area

        def centre(key):
            acc = centres.get(key)
            return tuple(c / acc[0] for c in acc[1]) if acc and acc[0] else self.lamps[key].position

        def nearest(keys, r):
            return min(keys, key=lambda k: (math.dist(centre(k), r.centre), k))

        def same_section(rs):
            """The piece's own section's lamps whose centre is within CLEAR_LENS_REACH of it."""
            keys = own.get((sections.get(id(rs[0].sub)), rs[0].lod, rs[0].front), set())
            return {k for k in keys if any(math.dist(centre(k), r.centre) < CLEAR_LENS_REACH for r in rs)}

        def overlapping(rs, corners, keys):
            """Lamps whose triangles' outline along z overlaps the piece's, within CLEAR_LENS_REACH."""
            if not keys:
                return set()
            pts = np.array([r.pts for r in rs], float).reshape(-1, 3)
            lo, hi = pts.min(0), pts.max(0)
            c_lo, c_hi = corners.min(1), corners.max(1)
            hit = (
                (c_lo[:, 0] < hi[0])
                & (c_hi[:, 0] > lo[0])
                & (c_lo[:, 1] < hi[1])
                & (c_hi[:, 1] > lo[1])
                & (c_hi[:, 2] > lo[2] - CLEAR_LENS_REACH)
                & (c_lo[:, 2] < hi[2] + CLEAR_LENS_REACH)
            )
            return {keys[i] for i in np.nonzero(hit)[0]}

        def surface(lod, front):
            pts, keys = surfaces.get((lod, front), ([], []))
            return np.array(pts, float).reshape(-1, 3, 3), keys

        def red(key) -> bool:
            return self.lamps[key].colour == RED

        def rename(key, function=None, cluster=None) -> None:
            """Lamp `key` becomes a red `function` lamp (joining one of that key if there is), or
            cluster `cluster` of its group."""
            lamp = self.lamps.pop(key)
            if function is not None:
                lamp.function, lamp.colour = function, RED
            if cluster is not None:
                lamp.cluster = cluster
            new = lamp.key
            into = self.lamps.setdefault(new, lamp)
            if into is not lamp:
                for lod, acc in lamp.per_lod.items():
                    dst = into.per_lod.setdefault(lod, [0.0, [0.0] * 3, [0.0] * 3, 0])
                    dst[0] += acc[0]
                    for i in range(3):
                        dst[1][i] += acc[1][i]
                        dst[2][i] += acc[2][i]
                    dst[3] += acc[3]
            for t, k in self.tri_lamp.items():
                if k == key:
                    self.tri_lamp[t] = new
            for _, keys in surfaces.values():
                keys[:] = [new if k == key else k for k in keys]
            for keys in own.values():
                if key in keys:
                    keys.discard(key)
                    keys.add(new)
            for k in list(options):
                inside, rs = options[k]
                options[k] = ({new if x == key else x for x in inside}, rs)
            centres.pop(key, None)

        def fit(r, key, keys, rs):
            """key, unless r is a rear triangle on red texels (Tri.seen) and key's lamp isn't
            red: then the nearest red one of `keys` (the lamps behind or inside its piece), else
            of its section's (same_section), else the red lamp with a triangle nearest r within
            CLEAR_LENS_REACH, else None (unlit). Forza draws some reverse and indicator bulbs,
            the reflectors round them and lenses and covers over them on red texels (the
            Mercedes S65 AMG's lower tail lamp halves, the Lexus IS F's boot lid lamps, the
            Dodge Durango's covers). Lit with the reverse or an amber indicator lamp, the red lit
            white or amber when reversing or indicating (a lamp glows in its entry's colour)
            and never braked; vanilla draws its reverse lamps white and its indicators amber
            and lights its red rear lamp texels with the tail and brake lamps (issue #103)."""
            if key is None or r.front or r.seen != "red" or red(key):
                return key
            reds = {k for k in keys if red(k)} or {k for k in same_section(rs) if red(k)}
            if reds:
                return nearest(reds, r)
            corners, all_keys = surface(r.lod, r.front)
            if not all_keys:
                return None
            d = np.linalg.norm(corners.mean(1) - r.centre, axis=1)
            d = np.where([red(k) for k in all_keys], d, np.inf)
            i = int(d.argmin())
            return all_keys[i] if d[i] < CLEAR_LENS_REACH else None

        options: dict = {}  # id(Tri) -> (the lamps behind or inside its piece, the piece): for fit
        by_sub: dict = {}
        for r in clear:
            side = "L" if r.centre[0] > 0 else "R"
            by_sub.setdefault((id(r.sub), r.front, side), []).append(r)
        pieces = []
        for rs in by_sub.values():
            pieces += [[rs[m] for m in members] for members in _pieces([r.pts for r in rs])]
        by_front: dict = {}
        for r in front_reflectors:
            by_front.setdefault((id(r.sub), "L" if r.centre[0] > 0 else "R"), []).append(r)
        for rs in by_front.values():
            pieces += [[rs[m] for m in members] for members in _pieces([r.pts for r in rs])]
        reflectors = [rs for rs in pieces if rs[0].sub.material in REFLECTORS]
        lit = []
        for rs in reflectors:
            corners, keys = surface(rs[0].lod, rs[0].front)
            pts = np.array([r.pts for r in rs], float).reshape(-1, 3)
            hull = _hull_xy(pts[:, :2])
            inside = set()
            if keys and len(hull) >= 3:
                c = corners.mean(1)
                z0, z1 = pts[:, 2].min() - CLEAR_LENS_REACH, pts[:, 2].max() + CLEAR_LENS_REACH
                near = (c[:, 2] > z0) & (c[:, 2] < z1) & _in_hull(hull, c[:, :2])
                inside = {keys[i] for i in np.nonzero(near)[0]}
            inside = inside or same_section(rs)
            if inside:
                for r in rs:
                    key = nearest(inside, r)
                    if not r.front:
                        self.tri_lamp[(id(r.sub), r.k)] = key
                        options[id(r)] = (inside, rs)
                    lit.append((r, key))
        for r, key in lit:
            pts, keys = surfaces.setdefault((r.lod, r.front), ([], []))
            pts.append(r.pts)
            keys.append(key)
        for rs in pieces:
            if rs[0].sub.material in REFLECTORS:
                continue
            corners, keys = surface(rs[0].lod, rs[0].front)
            z = corners[:, :, 2].mean(1)
            got = {}
            for r in rs:
                x, y, rz = r.centre
                depth = rz - z if r.front else z - rz  # +z is forward: behind a front lens is lower z
                hit = (depth > -CLEAR_SAME_PLANE) & (depth < CLEAR_LENS_REACH) & _inside_xy(corners, x, y)
                if hit.any():
                    got[id(r)] = keys[int(np.where(hit, depth, np.inf).argmin())]
            behind = set(got.values()) or same_section(rs) or overlapping(rs, corners, keys)
            for r in rs:
                key = got.get(id(r)) or (nearest(behind, r) if behind else None)
                if key is not None:
                    self.tri_lamp[(id(r.sub), r.k)] = key
                    options[id(r)] = (behind | set(got.values()), rs)
        # What each lamp triangle, lens, cover and reflector is drawn with, last: lenses found the
        # lamp behind them by what they lie over (the Honda Integra Type R Mugen's white lenses
        # over its red-drawn reverse bulbs light with the reverse lamp; the Lamborghini
        # Countach's amber lenses with the indicator whose bulbs are drawn red).
        drawn = [r for r in (*records, *clear) if not r.front and r.seen is not None]
        indicators: dict = {}
        for r in drawn:
            key = self.tri_lamp.get((id(r.sub), r.k))
            if key is not None and key[0] == INDICATOR:
                indicators.setdefault(key, []).append(r)
        for key, rs in indicators.items():
            finest = min(r.lod for r in rs)
            red_area = sum(r.area for r in rs if r.lod == finest and r.seen == "red")
            if red_area >= RED_INDICATOR_SHARE * sum(r.area for r in rs if r.lod == finest):
                rename(key, BRAKE_IND)
        # The indicators left keep their order, the first cluster 0 (assign ranks the others last:
        # the Ford Sierra RS500's main amber indicator lost its entry when its red side lamp,
        # cluster 0, went).
        for side in ("L", "R"):
            left = sorted(k for k in self.lamps if k[0] == INDICATOR and k[1] == side and not k[2])
            for n, k in enumerate(left):
                if k[3] != n:
                    rename(k, cluster=n)
        for r in drawn:
            key = self.tri_lamp.get((id(r.sub), r.k))
            if key is None or r.seen != "red" or red(key):
                continue
            key = fit(r, key, *options.get(id(r), (set(), [r])))
            if key is None:
                del self.tri_lamp[(id(r.sub), r.k)]
            else:
                self.tri_lamp[(id(r.sub), r.k)] = key

    # -- host light entries --
    def assign(self, upvh: bytes) -> LampPlan:
        """Give lamps the host's light entries, most important first, each an
        entry at its own end of the host and on its own side (entry_place). A
        lamp breaks with its entry's panel: the game breaks a light when the part
        its record hangs under is damaged, so a head lamp on an entry the host
        used for a tail lamp broke in rear collisions and not in front ones
        (issue #132). Lamps left over share an entry of the same function, end
        and side if there is one (their geometry lights with it), else a
        fallback lamp's at their end; only then a free entry at the other end;
        else they stay unlit."""
        from dsfforza.formats import upvh as uv

        h = uv.parse(upvh)
        base = h.offsets["lights"]
        entries = []
        for i in range(h.counts["lights"]):
            o = base + i * 0x40
            entries.append((o, struct.unpack_from("<I", h.data, o + 40)[0]))
        place = {e: entry_place(h.data, e[0]) for e in entries}
        # Lamps the red-drawn texel move emptied (_place_clear: the BMW M3 E30's, Audi RS6's and
        # Peugeot 207s' reverse lamps, all their triangles red) take no entry (#264).
        drawn = set(self.tri_lamp.values())
        order = sorted(
            (l for k, l in self.lamps.items() if l.area and k in drawn),
            key=lambda l: (PRIORITY.get(l.function, 9) + 10 * min(l.cluster, 1), not l.front, l.side),
        )
        free = list(entries)
        taken = {}

        def take(lamp, entry):
            free.remove(entry)
            lamp.light_id = entry[1]
            taken[entry] = lamp

        def partner(lamp):
            """A lit lamp whose entry this one can share: same function, end and side, else the
            same function at its end on the other side when the type has no side (SIDELESS: the
            right reverse lamp on the left one's entry), else its fallback function's at its end
            (same side first)."""
            lit = [l for l in order if l.light_id is not None and l.front == lamp.front]
            twin = next((l for l in lit if l.function == lamp.function and l.side == lamp.side), None)
            if twin is None and lamp.function in SIDELESS:
                twin = next((l for l in lit if l.function == lamp.function), None)
            other = fallback(lamp.function, lamp.front)
            if twin is None and other is not None:
                near = [l for l in lit if l.function == other]
                twin = next((l for l in near if l.side == lamp.side), near[0] if near else None)
            return twin

        def pair_entry(lamp):
            """An entry at a rear lamp's end freed for it: a left and a right lamp there of one
            SIDELESS function (not head lamps) with an entry each share the left one's, and the
            right one's goes to `lamp`; the least important such pair (PRIORITY), else None.
            The two keep their type and light as before (issue #146)."""
            if lamp.front:
                return None
            pairs = []
            for a in order:
                if a.light_id is None or a.front != lamp.front or a.function not in SIDELESS or a.function == HEAD:
                    continue
                for b in order:
                    if (
                        b is not a
                        and b.light_id is not None
                        and b.light_id != a.light_id
                        and b.front == a.front
                        and b.function == a.function
                        and (a.side, b.side) == ("L", "R")
                        and taken.get(next((e for e in entries if e[1] == b.light_id), None)) is b
                    ):
                        pairs.append((a, b))
            if not pairs:
                return None
            a, b = max(pairs, key=lambda p: (PRIORITY.get(p[0].function, 9), -p[0].area - p[1].area))
            entry = next(e for e in entries if e[1] == b.light_id)
            del taken[entry]
            free.append(entry)
            b.light_id = a.light_id
            return entry

        # Most important first, each an entry at its end. A rear lamp with none left there takes one
        # a left and right pair frees by sharing (pair_entry), else one at the other end (lit, though
        # it breaks with that end's panel) unless it can share a lamp's at its own end: a rear
        # reverse lamp outranks a front fog lamp, as before. A fog lamp never does: the game's Lua
        # never sets FogLights (only Siren), so on an entry at the other end it would only break with
        # that end's panel (issue #146); it stays unlit.
        for lamp in order:
            pick = _best_entry(lamp, [e for e in free if place[e][0] == lamp.front], place)
            if pick is None and partner(lamp) is None and lamp.function != FOG:
                pick = pair_entry(lamp) or _best_entry(lamp, free, place)
            if pick is not None:
                take(lamp, pick)
        for lamp in order:
            if lamp.light_id is None:
                twin = partner(lamp)
                if twin is not None:
                    lamp.light_id = twin.light_id
                else:
                    self.unlit.append(lamp.key)
        self.slots = [(o, lid, taken.get((o, lid))) for o, lid in entries]
        # Day-time running: a front side without a lamp of its own that lights
        # with DaytimeRunning gets head lamps that do (0x1D, Headlights +
        # DaytimeRunning), as 31 vanilla vehicles draw theirs; the other vanilla
        # cars but 10 have white position lamps (0x03) in their head-lamp
        # clusters. Forza's lamps mostly have no position-lamp technique, so
        # ported head lamps (0x01) stayed dark by day while vanilla cars' glowed
        # (player report; 66 of 573 ported cars had a front DRL lamp).
        # A position lamp a fraction the head lamp's size doesn't count
        # (CHR_Cuda426_71: 10 triangles beside 109).
        entered = {id(l) for _, _, l in self.slots if l is not None}

        def area(functions, side, own=False):
            return sum(
                l.area
                for l in order
                if l.front and l.side == side and l.function in functions and (not own or id(l) in entered)
            )

        for lamp in order:
            if (
                lamp.function == HEAD
                and lamp.front
                and area((POSITION, DRL_IND), lamp.side, own=True) < DAYTIME_SHARE * area((HEAD,), lamp.side)
            ):
                lamp.daytime = True
        return self

    def rear_tail_lamp(self, sub, k) -> int | None:
        """The number of triangle k of `sub`'s lamp when it is a rear red lamp with an entry (tail,
        brake, brake and indicator: 0x18, 0x02, 0x19 / 0x1A), for dsf_materials.Binding.set_rear_lamps; None
        for any other triangle."""
        key = self.tri_lamp.get((id(sub), k))
        lamp = self.lamps.get(key)
        if lamp is None or lamp.front or lamp.light_id is None or lamp.function not in REAR_TAIL_FUNCTIONS:
            return None
        return list(self.lamps).index(key)

    def light_byte(self, sub, k) -> int:
        key = self.tri_lamp.get((id(sub), k))
        if key is None:
            return 0
        lamp = self.lamps.get(key)
        return 0 if lamp is None or lamp.light_id is None else lamp.light_id + 1

    def s9(self, sub, k, s9: bytes) -> bytes:
        b = self.light_byte(sub, k)
        if not b:
            return s9
        return s9[:2] + bytes([b]) + s9[3:]

    def patch_upvh(self, upvh: bytes) -> bytes:
        """Rewrite position, direction and flags of every host light entry:
        used ones from their lamp, spare ones off (flags 0) at the nearest lamp
        of the same end (or where they were)."""
        data = bytearray(upvh)
        used = [lamp for _, _, lamp in self.slots if lamp is not None]
        for o, _lid, lamp in self.slots:
            if lamp is not None:
                struct.pack_into("<3f", data, o, *lamp.position)
                struct.pack_into("<3f", data, o + 16, *lamp.direction)
                struct.pack_into("<I", data, o + 44, lamp.flags)
                continue
            old = struct.unpack_from("<3f", data, o)
            front = old[2] > 0
            near = [l for l in used if l.front == front] or used
            if near:
                park = min(near, key=lambda l: math.dist(l.position, old))
                struct.pack_into("<3f", data, o, *park.position)
                struct.pack_into("<3f", data, o + 16, *park.direction)
            struct.pack_into("<I", data, o + 44, 0)
        return bytes(data)

    def report(self) -> dict:
        lamps = []
        for lamp in sorted(self.lamps.values(), key=lambda l: (not l.front, l.function, l.side)):
            mask = LIGHT_TYPE_MASK[lamp.type]
            lamps.append(
                {
                    "function": lamp.function,
                    "side": lamp.side,
                    "front": lamp.front,
                    "light_id": lamp.light_id,
                    "flags": f"{lamp.flags:#010x}",
                    "lua": "+".join(n for b, n in MASK_NAMES.items() if mask & b),
                    "position": [round(c, 3) for c in lamp.position],
                    "triangles": lamp.triangles,
                }
            )
        return {
            "lamps": lamps,
            "unlit": [list(k) for k in self.unlit],
            "spare_entries": sum(1 for _, _, l in self.slots if l is None),
        }


def read_table(upvh: bytes) -> list[dict]:
    from dsfforza.formats import upvh as uv

    h = uv.parse(upvh)
    out = []
    for i in range(h.counts["lights"]):
        o = h.offsets["lights"] + i * 0x40
        pos = struct.unpack_from("<3f", h.data, o)
        direction = struct.unpack_from("<3f", h.data, o + 16)
        lid, flags = struct.unpack_from("<2I", h.data, o + 40)
        t = flags >> 24
        mask = LIGHT_TYPE_MASK[t] if t < len(LIGHT_TYPE_MASK) else None
        out.append(
            {
                "index": i,
                "id": lid,
                "flags": flags,
                "position": pos,
                "direction": direction,
                "lua": "+".join(n for b, n in MASK_NAMES.items() if mask and mask & b) or "-",
            }
        )
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="classify a Forza car's lamps on a vanilla host (default the Desanne)")
    p.add_argument("car", type=Path)
    p.add_argument("--dsf", type=Path, default=None, help="vanilla dngvehicles.sp (default: from $DSF_GAME_DIR)")
    p.add_argument("--host", type=int, default=266)
    t = sub.add_parser("table", help="print a vehicle's light table")
    t.add_argument("archive", type=Path)
    t.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "plan":
        args.dsf = game_paths.vanilla(args.dsf)  # the original when a dsfforza install replaced it

    from dsfforza.formats import upvh as uv
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.chnk import read_chunk

    archive = args.dsf if args.cmd == "plan" else args.archive
    spool = args.host if args.cmd == "plan" else args.id
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        full = uv.entries_for(stream, root.entries, spool, None)[0]
        upvh = vm.payload(stream, vm.child(vm.child(full, "NONR"), "UPVH"))
        modl = vm.payload(stream, vm.child(vm.child(full, "NONR"), "MODL"))
    if args.cmd == "table":
        for e in read_table(upvh):
            print(
                f"{e['index']:2} id {e['id']:2} flags {e['flags']:#010x} {e['lua']:28} "
                f"pos ({', '.join(f'{c:+.2f}' for c in e['position'])}) "
                f"dir ({', '.join(f'{c:+.2f}' for c in e['direction'])})"
            )
        return
    from dsfforza.convert import forza2dsf

    car = forza2dsf.load_car(args.car, args.car.name)
    dims = forza2dsf.car_dims(car)
    # Placed on the host's own wheels, as forza2dsf.convert does (this used the
    # Desanne's hub height and axle midpoint whatever --host was).
    hw = forza2dsf.host_wheels(upvh, modl)
    place = forza2dsf.Placement(dy=hw.hub_y - dims.radius + dims.ride_height, dz=hw.axle_mid + dims.mid_z)
    from dsfforza.convert import forza_materials as fm

    plan = LampPlan.build(car.body, place, hw.axle_mid, texel=texel_sampler(fm.AtlasCache(car.folder))).assign(upvh)
    import json

    print(json.dumps(plan.report(), indent=1))


if __name__ == "__main__":
    sys.exit(main())
