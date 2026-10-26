"""Working lamps for converted Forza cars: vertex light bytes and the UPVH light table.

How stock DSF lamps work (research/lights.md, confirmed-static):

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
_add(BRAKE_IND, "brinl", "L")
_add(BRAKE_IND, "brinr", "R")
_add(DRL_IND, "drlinlorange drlinlred drlinlwhite lindtailside", "L")
_add(DRL_IND, "drlinrorange drlinrred drlinrwhite rindtailside", "R")
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
# side (a brake-and-indicator lamp also its side's tail lamp: it still brakes),
# else stay unlit. Brake-and-indicator lamps come after the position lamps:
# red lenses over indicators (_red_lens_indicators) added two lamps to 58
# cars, and on full hosts (BMW_M5_12, VIP_Viper_13 on 147, 182) they took
# the front position lamps' entries.
PRIORITY = {HEAD: 0, TAIL: 1, BRAKE: 2, INDICATOR: 3, DRL_IND: 3, REVERSE: 4, POSITION: 5, BRAKE_IND: 6, FOG: 7}
FALLBACK = {BRAKE_IND: TAIL}  # an unassigned lamp of this function shares this one's entry


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
        return type_byte(self.function, self.side) << 24 | self.colour << 16 | variant_byte(self.function)


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
CLEAR_LENS_REACH = 0.35  # m: a clear lens lights with the nearest lamp this close
# Lens texel colour (texel_class): saturated texels whose hue (degrees from
# red towards yellow) is below LENS_RED_HUE are red, up to 60 amber. Fleet
# (finest LOD, mean texel per triangle): 99% of `detail_glass_red` triangles at
# the rear sit at -15 to +10, `detail_glass_amber` at the front mostly at 10 to
# 35 (out/brakes/lensfleet.py).
LENS_MIN_SATURATION = 0.35
LENS_RED_HUE = 10.0


def texel_class(texel) -> str | None:
    """ "red", "amber" or "white" (clear, grey or not red at all) for a mean
    texel (r, g, b, a); None without one."""
    if texel is None:
        return None
    r, g, b = texel[:3]
    hi, lo = max(r, g, b), min(r, g, b)
    if hi < 8 or (hi - lo) / hi < LENS_MIN_SATURATION or r < hi:
        return "white"
    return "red" if 60 * (g - b) / (hi - lo) < LENS_RED_HUE else "amber"


def texel_sampler(atlases):
    """(section, sub, triangle) -> the mean Forza texel (r, g, b, a) under one
    triangle of a Lights-texture subsection, else None. atlases: a
    forza_materials.AtlasCache of the car (dsf_materials.Texturing.atlases)."""
    from dsfforza.convert import forza_materials as fm
    from dsfforza.convert import trim_art

    images: dict = {}
    materials: dict = {}

    def sample(section, sub, tri):
        key = id(sub)
        if key not in materials:
            materials[key] = fm.resolve(section, sub, fm.classify)
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
        h, w = image.shape[:2]
        pool = section.vertices_for(sub)
        texels = trim_art.triangle_texels(image, [((u := m.uv(pool[i].uv0))[0] * w, u[1] * h) for i in tri])
        if not len(texels):
            return None
        return tuple(float(c) for c in texels.mean(0))

    return sample


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


def _red_lens_indicators(records, from_lens: set, red_indicators: list) -> None:
    """Red rear lens triangles that also indicate: a red lamp that brakes and
    indicates (BRAKE_IND, types 0x19/0x1A, as on stock US-style cars).

    - Over an indicator: the lamp triangle right behind the lens triangle
      (same LOD, not a lens; nearest across the view, x and y, within
      CLEAR_LENS_REACH along it) indicates. Stock lamps are opaque, so the
      bulb behind a lens never shows; the lens has to blink itself
      (FOR_GT_05's red lenses over its drlinl/drlinr lamps, MIN_Coopers_11's
      red glass over its indicator_left/right bulbs).
    - A red "amber" lens on a side without any other rear indicator
      (MIT_GTO_97: (76, 0, 0) texels, no amber at the rear)."""
    import numpy as np

    bulbs: dict = {}  # lod -> (centres, functions) of rear lamp triangles that are not lenses
    for n, r in enumerate(records):
        if n not in from_lens and not r.front:
            bulbs.setdefault(r.lod, ([], []))
            bulbs[r.lod][0].append(r.centre)
            bulbs[r.lod][1].append(r.function)
    bulbs = {lod: (np.array(c, dtype=float), f) for lod, (c, f) in bulbs.items()}

    def indicate(r) -> None:
        r.function = BRAKE_IND
        if r.side == "C":
            r.side = "L" if r.centre[0] > 0 else "R"

    for n in from_lens:
        r = records[n]
        if r.function != TAIL or r.lod not in bulbs:
            continue
        centres, functions = bulbs[r.lod]
        # behind: nearest across the view (x, y), within reach along it
        offset = centres - np.array(r.centre)
        d = np.where(np.abs(offset[:, 2]) < CLEAR_LENS_REACH, np.linalg.norm(offset[:, :2], axis=1), np.inf)
        i = int(d.argmin())
        if d[i] < CLEAR_LENS_REACH and functions[i] in INDICATING:
            indicate(r)
    indicating = {r.side for r in records if not r.front and r.function in INDICATING}
    for n in red_indicators:
        if records[n].side not in indicating:
            indicate(records[n])


REFLECTORS = ("textured_reflector", "reflector")
LAMP_COVERS = ("glass", "lights_glass", "lights_gls_noemit")  # = glass.LAMP_MATERIALS


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
                out.append(Tri(sub, k, HEAD, side, True, WHITE, area, centre, normal, sub.lod))
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
    def build(cls, sections, place, mid_z: float, lenses=frozenset(), texel=None) -> LampPlan:
        """sections: Forza body sections (all LODs). place: forza2dsf.Placement.
        mid_z: DSF z of the wheelbase midpoint (front lamps are ahead of it).
        lenses: id(sub) of lamp-cover glass subsections drawn as lamp texels
        (dsf_materials.Texturing.lens_subs); they light like clear lenses,
        with the lamp behind each triangle (the DMC-12's grid lens: tail,
        indicator and reverse cells over one reflector).
        texel: (section, sub, triangle) -> mean Forza texel or None
        (texel_sampler); rear lens triangles on red texels are tail lamps
        whatever their technique says (_lens_classes)."""
        plan = cls()
        records, clear = [], []  # clear lenses are placed after the lamps
        red_indicators = []  # records of amber-technique lens triangles on red texels
        from_lens = set()  # records of lens triangles
        for section in sections:
            for sub in section.subsections:
                entry = TECHNIQUE_FUNCTION.get(sub.material)
                if entry is None and id(sub) in lenses:
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
                if function == LENS:
                    colour = LENS_COLOUR.get(sub.material, WHITE)
                    if texel is not None:
                        classes, red_lens = _lens_classes(section, sub, tris, pts_all, texel, colour, mid_z)
                for k, (t, pts) in enumerate(zip(tris, pts_all)):
                    normals = [place.normal(pool[i].normal) for i in t]
                    area, centre, normal = _tri_geometry(pts, normals)
                    front = centre[2] > mid_z
                    if named_side:
                        side = named_side
                    elif max_x < CENTRE_X:
                        side = "C"
                    else:
                        side = "L" if centre[0] > 0 else "R"
                    f, c = function, colour
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
                        c = RED
                    if f == LENS:
                        # Amber lenses are indicators, red ones at the rear tail lamps;
                        # clear ones light with the lamp behind them.
                        if c == AMBER:
                            f = INDICATOR
                            side = side if side != "C" else ("L" if centre[0] > 0 else "R")
                        elif c == RED and not front:
                            f = TAIL
                        else:
                            clear.append(Tri(sub, k, LENS, side, front, c, area, centre, normal, sub.lod))
                            continue
                        from_lens.add(len(records))
                    records.append(Tri(sub, k, f, side, front, c, area, centre, normal, sub.lod))
        if not any(r.function == HEAD and r.front for r in records):
            # No head-lamp technique (CHE_CorvetteGS_10, DOD_RamSRT10_06,
            # FOR_MustangSVTCobraR_93 and several race cars draw the lamp as a
            # reflector under a clear lens): the clear lenses at the front are the
            # head lamps, else the headlight sections' reflectors.
            front_clear = [r for r in clear if r.front]
            if front_clear:
                clear = [r for r in clear if not r.front]
                records += [
                    Tri(r.sub, r.k, HEAD, r.side, True, WHITE, r.area, r.centre, r.normal, r.lod) for r in front_clear
                ]
            else:
                records += _headlight_reflectors(sections, place, mid_z)
        _red_lens_indicators(records, from_lens, red_indicators)
        _high_brake(records)
        plan._cluster(records)
        plan._place_clear(clear, records)
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

    def _place_clear(self, clear, records) -> None:
        """Clear lenses light with the lamp nearest behind them: the lamp of
        the nearest lamp triangle of the same LOD and end (within
        CLEAR_LENS_REACH), else stay unlit. By lamp centre a lens over a tail
        lamp went to the reverse lamp once the tail lamp's red lens joined it
        and moved its centre (JAG_XFR_10)."""
        import numpy as np

        points: dict = {}  # (lod, front) -> (centres, lamp keys)
        for r in records:
            key = self.tri_lamp.get((id(r.sub), r.k))
            if key is not None and self.lamps[key].area:
                points.setdefault((r.lod, r.front), ([], []))
                points[(r.lod, r.front)][0].append(r.centre)
                points[(r.lod, r.front)][1].append(key)
        arrays = {k: (np.array(c, dtype=float), keys) for k, (c, keys) in points.items()}
        for r in clear:
            near = arrays.get((r.lod, r.front))
            if near is None:  # no lamp at this LOD: any LOD at the same end
                cands = [v for (lod, front), v in arrays.items() if front == r.front]
                if not cands:
                    continue
                near = (np.concatenate([c for c, _ in cands]), [k for _, keys in cands for k in keys])
            d = np.linalg.norm(near[0] - np.array(r.centre), axis=1)
            i = int(d.argmin())
            if d[i] < CLEAR_LENS_REACH:
                self.tri_lamp[(id(r.sub), r.k)] = near[1][i]

    # -- host light entries --
    def assign(self, upvh: bytes) -> LampPlan:
        """Give lamps the host's light entries, most important first. Lamps
        left over share an entry of the same function and side if there is
        one (their geometry lights with it), else stay unlit."""
        from dsfforza.formats import upvh as uv

        h = uv.parse(upvh)
        base = h.offsets["lights"]
        entries = []
        for i in range(h.counts["lights"]):
            o = base + i * 0x40
            entries.append((o, struct.unpack_from("<I", h.data, o + 40)[0]))
        order = sorted(
            (l for l in self.lamps.values() if l.area),
            key=lambda l: (PRIORITY.get(l.function, 9) + 10 * min(l.cluster, 1), not l.front, l.side),
        )
        free = list(entries)
        taken = {}
        for lamp in order:
            if free:
                o, lid = free.pop(0)
                lamp.light_id = lid
                taken[(o, lid)] = lamp
        for lamp in order:
            if lamp.light_id is None:
                twin = next(
                    (
                        l
                        for l in order
                        if l.light_id is not None and l.function == lamp.function and l.side == lamp.side
                    ),
                    None,
                )
                if twin is None and lamp.function in FALLBACK:
                    twin = next(
                        (
                            l
                            for l in order
                            if l.light_id is not None
                            and l.function == FALLBACK[lamp.function]
                            and l.side == lamp.side
                            and l.front == lamp.front
                        ),
                        None,
                    )
                if twin is not None:
                    lamp.light_id = twin.light_id
                else:
                    self.unlit.append(lamp.key)
        self.slots = [(o, lid, taken.get((o, lid))) for o, lid in entries]
        return self

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
            mask = LIGHT_TYPE_MASK[type_byte(lamp.function, lamp.side)]
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
    p = sub.add_parser("plan", help="classify a Forza car's lamps on a stock host (default the Desanne)")
    p.add_argument("car", type=Path)
    p.add_argument("--dsf", type=Path, default=None, help="stock dngvehicles.sp (default: from $DSF_GAME_DIR)")
    p.add_argument("--host", type=int, default=266)
    t = sub.add_parser("table", help="print a vehicle's light table")
    t.add_argument("archive", type=Path)
    t.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "plan":
        args.dsf = game_paths.stock(args.dsf)  # the original when a dsfforza install replaced it

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
