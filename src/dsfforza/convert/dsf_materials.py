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

- packs the parts of the Forza atlases (nodamage, Lights, and the car's
  wheel.xds for the rims) that textured subsections actually use into one
  DSF atlas (`Texturing`), with solid cells for colour swatches and for the
  plain paint texel;
- encodes it as DXT5 with mips (`encode_dxt5`, `dds_file`);
- installs it: by default it replaces the host TEXC's DDS and keeps every
  UID, so the host materials of all three entries pick it up
  (`install_full`, `install_exterior`); `mode="clone"` adds a new TEXC and
  clones the two materials instead;
- renders a textured preview of a VEHC entry (`preview`).

    uv run dsff dsf_materials atlas out/fh1/NIS_370Z_10 --png out/atlas.png
    uv run dsff dsf_materials preview out/370z-tex.sp 452 --png out/tex.png
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

import numpy as np

from dsfforza.convert import forza_materials as fm
from dsfforza.formats.chnk import Entry
from dsfforza.formats.vehicle_mesh import child, payload

DDS_HEADER_SIZE = 128
# The Desanne's chassis texture: DXT5, 4 mip levels, and its plain paint texel.
DEFAULT_SIZE = (512, 512)
DEFAULT_MIPS = 4
# Paint UV0 does not sample the vehicle's texture: both paint materials
# (GLOBAL_Chassis, GLOBAL_Chassis_LOD1) sample the shared SSIC texture
# 00122b73d1fa1227, a 1024 x 512 atlas of masks and parts, and 57 to 91% of
# every vanilla car's paint triangles sit in its (240, 192, 240) block (50 x 50
# texels centred here). UV0 elsewhere draws the paint black or grey
# (research/lights.md, "Paint").
PAINT_TEXEL_UV = (0.8481, 0.7119)  # forza2dsf.PAINT_UV (0.835, 0.703) is inside the same block
# Lamp texel alpha (the Lights shader adds lamp colour x brightness x texel alpha,
# research/lights.md). "matched" (default): a score s = Forza glow mask x (0.6 + 0.4 x
# brightest channel), all 0..1, mapped through a curve per texel hue (red, amber, white) that
# puts the converted texels on vanilla DSF's lamp alpha distribution (quantile match of
# six FH1/FM4 cars against 146 vanilla vehicles, out/lamps/scripts/fit_lut.py). It keeps
# the order of the texels, so hot spots, reflector cells and dark edges stay where
# Forza has them, and a near-black lens only glows where its glow mask is bright.
# "raw": Forza's alpha as is (analysis builds). The earlier modes ("value", "luma",
# "gamma") are described in research/lights.md. Module-level so an A/B build can
# switch it; DSFFORZA_LIGHTS_ALPHA overrides it for port_batch workers.
LIGHTS_ALPHA_MODES = ("matched", "raw")
LIGHTS_ALPHA = os.environ.get("DSFFORZA_LIGHTS_ALPHA", "matched")
if LIGHTS_ALPHA not in LIGHTS_ALPHA_MODES:
    raise ValueError(f"DSFFORZA_LIGHTS_ALPHA={LIGHTS_ALPHA!r}: one of {', '.join(LIGHTS_ALPHA_MODES)}")
# "matched" curves: score knots -> vanilla alpha knots (vanilla percentiles 0, 5, 10, 20,
# ..., 90, 93, 95, 97, 98, 99, 99.5 of amber and of white lamps; red lamps also have
# 85 and 100, LAMP_PERCENTILES).
_LAMP_X_RED = (
    0.0,
    0.199,
    0.313,
    0.458,
    0.529,
    0.586,
    0.627,
    0.672,
    0.715,
    0.768,
    0.785,
    0.798,
    0.815,
    0.831,
    0.853,
    0.87,
    0.896,
    0.922,
    0.961,
)
_LAMP_Y_RED = (0, 0, 0, 3, 8, 14, 21, 30, 43, 63, 78, 106, 130, 153, 187, 211, 239, 252, 255)
_LAMP_X_WHITE = (
    0.0,
    0.117,
    0.135,
    0.177,
    0.373,
    0.557,
    0.677,
    0.755,
    0.813,
    0.898,
    0.922,
    0.936,
    0.947,
    0.951,
    0.961,
    0.97,
    0.975,
)
_LAMP_Y_WHITE = (0, 0, 1, 9, 18, 29, 43, 60, 84, 119, 142, 173, 197, 216, 237, 248, 255)
_LAMP_Y_AMBER = (0, 0, 1, 7, 14, 22, 31, 44, 61, 84, 100, 124, 145, 167, 196, 217, 242)
LAMP_CURVES = {
    "red": (_LAMP_X_RED, _LAMP_Y_RED),
    "white": (_LAMP_X_WHITE, _LAMP_Y_WHITE),
    "amber": (_LAMP_X_WHITE, _LAMP_Y_AMBER),
}


# "matched" also darkens red lamp lenses to vanilla's brightness: Forza's red lamp texels
# are brighter (brightest channel median 144, M3 E30 199, DMC-12 158) than vanilla's
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


# Vanilla red lamp alpha at the percentiles LAMP_PERCENTILES (= _LAMP_Y_RED).
LAMP_PERCENTILES = (0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 85, 90, 93, 95, 97, 98, 99, 99.5, 100)
RED_CURVE_MIN_TEXELS = 200  # fewer red lamp texels: the shared red curve
RED_CURVE_SAMPLES = 40000  # about this many texels sampled over a car's lights regions
# Score knots of a car's red curve for different alphas stay at least this far apart. A car whose red texels are
# mostly one flat lens (SUB_WRXSTi_04: 60% of them at 0.69 +- DXT noise) had knots 1e-4
# apart there, so the noise of a flat red lens became alpha 8 to 30 in blotches and the
# lit lamp glowed mottled (user report on out/final4). 0.01 is about the score noise of a
# flat DXT texel (5 levels of the red channel).
RED_CURVE_MIN_GAP = 0.01


def red_lamp_curve(scores: list[float]):
    """One car's red lamp curve: its own red texel scores' percentiles -> vanilla's red
    alpha percentiles, so every car's red lamps land on vanilla's distribution (with one
    curve for all cars a dim Forza mask gave the S2000 a tail median of 2 and the
    Challenger 73; in game the S2000's and S5S's lamps barely lit, run
    20261024-002212). None with too few texels (the shared curve is used)."""
    if len(scores) < RED_CURVE_MIN_TEXELS:
        return None
    ordered = sorted(scores)
    xs = []
    for k, q in enumerate(LAMP_PERCENTILES):
        x = ordered[min(len(ordered) - 1, int(q / 100 * (len(ordered) - 1) + 0.5))]
        if xs:  # knots for one alpha (the zeros at the bottom) only need to stay in order
            x = max(x, xs[-1] + (RED_CURVE_MIN_GAP if _LAMP_Y_RED[k] != _LAMP_Y_RED[k - 1] else 1e-4))
        xs.append(x)
    return tuple(xs), _LAMP_Y_RED


# What a camera straight behind the car sees of vanilla red brake lamps: the brightest channel
# of the texels on the visible lamp surface, area-weighted (orthographic rear view z-buffered
# over the lamp geometry, full entries of 140 vanilla vehicles, pooled; out/emissive/rearlamps.py):
# p5 35, p25 71, median 92, p75 115, p95 153.
VISIBLE_RED_VANILLA_MEDIAN = 92
VISIBLE_RED_MIN_SAMPLES = 400  # fewer visible red lamp samples: the shared lens curve only
VISIBLE_STEP = 0.005  # metres per sample of the rear view


def _shared_lens(v: float) -> float:
    return min(float(v), _interp(v, _LENS_X_RED, _LENS_Y_RED))


def red_lens_scale(visible: list[int]) -> float | None:
    """One car's extra darkening of its red lamp texels: the factor that brings the median
    brightness of its visible red lamp surface (Forza values seen from behind, through the
    shared curve) down to vanilla's visible median; never above 1. The shared curve is
    texel-weighted over every red lamp texel, hidden bulbs and reflectors included, so a car
    whose outer lens is one bright, flat red kept it well above vanilla: the Audi TT RS's tail
    lens (Forza 177, 128 after the shared curve, the vanilla A4 beside it 101) read flat pink in
    daylight whether braking or not (user report on out/testdrive9; run vm-20261024-051926,
    red lamp pixels 224 on the throttle against vanilla's 159 to 178). None with too few
    samples."""
    if len(visible) < VISIBLE_RED_MIN_SAMPLES:
        return None
    ordered = sorted(_shared_lens(v) for v in visible)
    median = ordered[len(ordered) // 2]
    return min(1.0, VISIBLE_RED_VANILLA_MEDIAN / median) if median > 0 else None


# What a camera straight ahead of the car sees of vanilla front lamps: the brightest channel of
# the white texels (lamp_hue) on the visible lamp surface, area-weighted (orthographic front
# view z-buffered over the lamp geometry of the full entries of 142 vanilla vehicles, pooled):
# p5 41, p25 93, median 137, p75 180, p95 230 (per-vehicle median p10 82, p50 134, p90 178).
# Forza's white lamp texels are brighter: the Lotus Carlton's head lamps (a light clear lens
# over a white reflector, baked by lenses.py) 206, the Lancer Evo VI's 173, the M3 E30's 197.
VISIBLE_WHITE_VANILLA_MEDIAN = 137


def white_lens_scale(visible: list[int]) -> float | None:
    """One car's darkening of its white lamp texels: the factor that brings the median
    brightness of its visible white front lamp surface (Forza values seen from ahead, lenses
    baked) down to vanilla's visible median; never above 1. Forza's head lamps are light grey
    lenses over white reflectors (the Carlton's lens 195 over a reflector at 205 to 221), so
    they looked lit in daylight with the lamps off, and lit by day (0x1D, research/lights.md)
    the glow on top of them blew them out into white blobs (issue #115). None with too few
    samples."""
    if len(visible) < VISIBLE_RED_MIN_SAMPLES:
        return None
    ordered = sorted(visible)
    median = ordered[len(ordered) // 2]
    return min(1.0, VISIBLE_WHITE_VANILLA_MEDIAN / median) if median > 0 else None


def matched_lamp_texel(
    r: int, g: int, b: int, a: int, red_curve=None, lens_scale=None, white_scale=None
) -> tuple[int, int, int, int]:
    """Vanilla-like lamp texel (LIGHTS_ALPHA "matched"): alpha from the curves, red
    lenses darkened to vanilla's brightness (the shared curve, then this car's
    red_lens_scale), white texels by this car's white_lens_scale (never brighter).
    Alpha is scored on the original texel."""
    alpha = matched_lamp_alpha(r, g, b, a, red_curve)
    hue = lamp_hue(r, g, b)
    if hue == "red":
        v = max(r, g, b)
        if v:
            k = _shared_lens(v) / v * (lens_scale if lens_scale is not None else 1.0)
            r, g, b = (int(c * k + 0.5) for c in (r, g, b))
    elif hue == "white" and white_scale is not None and white_scale < 1.0:
        r, g, b = (int(c * white_scale + 0.5) for c in (r, g, b))
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
    """Vanilla-like lamp alpha from a Forza lamp texel (LIGHTS_ALPHA "matched");
    red_curve: this car's own (score knots, alpha knots) for red texels."""
    hue = lamp_hue(r, g, b)
    xs, ys = red_curve if hue == "red" and red_curve else LAMP_CURVES[hue]
    return int(_interp(lamp_score(r, g, b, a), xs, ys) + 0.5)


DEFAULT_PLAIN = (74, 72, 74, 15)
# Chassis texels (not lamps): alpha follows luminance, as on vanilla chassis
# textures (module docstring): (lum - CHASSIS_ALPHA_LUM) x CHASSIS_ALPHA_GAIN.
CHASSIS_ALPHA_LUM, CHASSIS_ALPHA_GAIN = 45, 0.55
# nodamage texels this transparent and this bright are the paint-through body
# area (white, alpha 0): they get the plain texel, and no cockpit tone.
PAINT_THROUGH_ALPHA, PAINT_THROUGH_MIN_RGB = 16, 200


def _paint_through(family: str, r: int, g: int, b: int, a: int) -> bool:
    return fm.base_family(family) == fm.NODAMAGE and a < PAINT_THROUGH_ALPHA and min(r, g, b) > PAINT_THROUGH_MIN_RGB


# Rear lamp lenses (Texturing.is_lens): a lamp-cover glass subsection behind
# this Forza z (front is -z; the 40 testdrive cars' rear lenses sit at +1.5 to
# +2.5) whose mean texel is this much redder than green and blue, and this
# opaque. Matches the M3 E30, Silvia, 300C, M5 E28, 205 T16 and DMC-12 tail
# lenses (red minus max(green, blue) 98 to 135, alpha 138 to 241); clear and
# smoked covers (Supra, RX-7, IS F, Range Rover: 13 to 58) stay glass.
LENS_REAR_Z, LENS_MIN_RED, LENS_MIN_ALPHA = 0.5, 80, 128
# Lamp-cover techniques whose texels Forza draws (lights_glass: texel x GlassColor, alpha
# capped by GlassMaxOpacity). Plain `glass` samples no texture at all (shaders_v16.fxobj:
# its pixel shaders read only the environment and damage-glass maps; colour GlassColor,
# opacity from the Fresnel term), so its UVs' texels are never seen: the Devon GTX's clear
# tail covers sit on flat red texels, and moved to the atlas they hid the lamp as a flat red
# plate (issue #160).
TEXTURED_COVERS = ("lights_glass", "lights_gls_noemit")
# Plain glass lamp covers (Texturing.clear_cover): left out when a Lights-atlas surface lies up
# to COVER_DEPTH behind this share of their area; never in the window sections.
COVER_MIN_SHARE, COVER_DEPTH = 0.3, 0.3
WINDOW_SECTIONS = ("glassf", "glassr", "glasslf", "glassrf", "glasslm", "glassrm", "glasslr", "glassrr", "glasst")

CHASSIS, LIGHTS_MAT = "chassis", "lights"


def is_atlas_kind(kind) -> bool:
    """Texturing.material kinds drawn through the atlas: the host Chassis and Lights
    materials and the gloss copies of the Lights one (GLOSS_PREFIX)."""
    return kind in (CHASSIS, LIGHTS_MAT) or (isinstance(kind, str) and kind.startswith(GLOSS_PREFIX))


# Atlas weight of the wheel texture against nodamage/Lights (DSF texels per
# wheel.xds texel = WHEEL_WEIGHT x the atlas's uniform scale, at most 1). A
# wheel.xds bigger than WHEEL_TEXELS (512² on the Veyron and 300C; 256² on 36 of
# the 40 testdrive cars, 128² on two) is weighted down to that size, so one
# detailed wheel doesn't shrink the rest of the car's atlas (the Veyron's
# nodamage scale went from 0.59 to 0.49 without it).
WHEEL_WEIGHT = 1.0
WHEEL_TEXELS = 256
DROP = "drop"  # triangle_uvs tag: leave this triangle out (alpha-tested away in Forza)
# Trim artwork (src/dsfforza/convert/trim_art.py: the Alfa GTA's quadrifoglio badges,
# the Crossfire's vent strakes) goes through the atlas unless that leaves the car less
# than this share of the atlas scale it has without it; then each of its subsections
# takes one flat colour. 60 tiny `misc` triangles of the Jeep GC SRT8's headlamps
# (a 365 x 15 texel strip) took it from 0.479 to 0.324, the DB9's 4 bumper_frame
# triangles from 0.75 to 0.63 (research/forza-materials.md, "Trim artwork").
# Dark trim detail (trim_art.DETAIL_TECHNIQUE: the Cougar Eliminator's grille bars) goes
# first: past the share it takes one flat colour per subsection and the artwork is tried
# again on its own.
ART_TECHNIQUES = ("dsf_trim_art", "dsf_trim_art_lights")  # trim_art.ART_TECHNIQUE
DETAIL_TECHNIQUES = ("dsf_trim_detail",)  # trim_art.DETAIL_TECHNIQUE
# trim_art.GREY_TECHNIQUE and GLOSS_TECHNIQUE: one flat colour cell each
GREY_TECHNIQUES = ("dsf_trim_grey", "dsf_trim_gloss")
ART_MIN_SCALE_SHARE = 0.9

# --- gloss: Forza's specular and reflection on trims and rims ------------------------
#
# Forza adds a specular and a cube-map reflection to its trims and rims
# (fm.gloss_params; research/forza-materials.md, "Gloss"). The host Chassis material the
# atlas trims and rims draw with has almost none (g_hSpecularIntensity 0.03 x texel
# alpha, power 24, no reflection), the shared trim even less (0.01 x the shared
# texture's green). The host's Lights material, the vanilla cars' reflective one, runs a
# program (SSIC GPCH d3f06377, _LOD1: ef85326d) that draws the atlas RGB lit like the
# Chassis one plus
#     g_hSpecularIntensity x (R.V)^g_hSpecularPower x the sun's specular colour
#   + env x saturate(texel G x g_hReflectionAmount
#                    x max(g_fCubemapFresnelMin, (1 - N.V)^g_hFresnelPower + g_hReflectionFresnelBias))
#   + texel alpha x the colour of the vertex's lamp (semantic 9 byte 2; 0 = no lamp, none)
# so copies of it with their own parameters (GlossMaterial; installed next to the host's
# materials in the full and exterior entries, gloss_entries) draw Forza's terms: the
# specular unmasked like Forza's, (N.H)^p ~ (R.V)^(p/4), the reflection with Schlick's
# Fresnel (power 5, bias F0), divided by the texel's green that masks it.
GLOSS_PREFIX = "gloss"  # Texturing.material kinds "gloss0", "gloss1"...: GlossMaterial n
GLOSS_SPECULAR_SCALE = float(os.environ.get("DSFFORZA_GLOSS_SPEC", "0.01"))  # DSF intensity per Forza SpecularAmount
GLOSS_REFLECTION_SCALE = float(os.environ.get("DSFFORZA_GLOSS_REFL", "0.5"))  # DSF reflection per Forza's
GLOSS_POWER_RATIO = 4.0  # Forza's (N.H) power over DSF's (R.V) power for the same highlight
GLOSS_POWER_RANGE = (4.0, 150.0)  # broader lobes light whole panels in the sun
GLOSS_INTENSITY_MAX = 0.0625  # some cars set SpecularAmount 12 (the Boss 429's plastic2)
GLOSS_GREEN_FLOOR = 16  # texel green the reflection amount is scaled for at least: regions (texels vary)
GLOSS_CELL_GREEN_FLOOR = 2  # ... and colour cells (one exact colour: the Cougar's scoop sides, 4)
GLOSS_REFLECTION_MAX = 16.0
GLOSS_MIN = 0.005  # specular intensity and reflection below this: none (both: no gloss material)
GLOSS_MAX_MATERIALS = 12  # per entry; more distinct ones merge into the nearest
# Grid steps (ratios) of the parameters: few distinct materials, a reflection within a
# factor of 2 of Forza's.
GLOSS_STEPS = {"intensity": 2.0, "power": 2**0.5, "reflection": 4.0}
CHROME_FRESNEL = (16.0, 1.0)  # (power, bias): chrome reflects alike at every angle
GLOSS_FRESNEL_POWER = 5.0


def _q(value: float, step: float = 2**0.5) -> float:
    """value rounded to a geometric grid (steps of `step`), 3 significant digits."""
    if value <= 0:
        return 0.0
    return float(f"{step ** math.floor(math.log(value, step) + 0.5):.3g}")


@dataclass(frozen=True, order=True)
class GlossMaterial:
    """The parameters of one copy of the host Lights material (see GLOSS_PREFIX)."""

    intensity: float  # g_hSpecularIntensity
    power: float  # g_hSpecularPower
    reflection: float  # g_hReflectionAmount
    fresnel_power: float  # g_hFresnelPower
    bias: float  # g_hReflectionFresnelBias

    def params(self) -> dict[str, float]:
        return {
            "g_hSpecularIntensity": self.intensity,
            "g_hSpecularPower": self.power,
            "g_hReflectionAmount": self.reflection,
            "g_hFresnelPower": self.fresnel_power,
            "g_hReflectionFresnelBias": self.bias,
        }


def gloss_material(g: fm.Gloss, green: float, floor: float = GLOSS_GREEN_FLOOR) -> GlossMaterial | None:
    """The DSF material for a Forza technique's gloss `g` on texels whose green averages
    `green` (0..255, as drawn; at least `floor`); None when both terms are negligible."""
    specular = min(GLOSS_INTENSITY_MAX, GLOSS_SPECULAR_SCALE * g.specular)
    intensity = _q(specular, GLOSS_STEPS["intensity"]) if specular >= GLOSS_MIN else 0.0
    power = _q(min(GLOSS_POWER_RANGE[1], max(GLOSS_POWER_RANGE[0], g.power / GLOSS_POWER_RATIO)), GLOSS_STEPS["power"])
    reflection = GLOSS_REFLECTION_SCALE * g.reflection
    amount = _q(min(GLOSS_REFLECTION_MAX, reflection * 255 / max(green, floor)), GLOSS_STEPS["reflection"])
    if not intensity and reflection < GLOSS_MIN:
        return None
    if g.fresnel_scalar is None:
        fresnel_power, bias = CHROME_FRESNEL
    else:
        fresnel_power, bias = GLOSS_FRESNEL_POWER, round(g.f0, 3)
    if reflection < GLOSS_MIN:
        amount = 0.0
    return GlossMaterial(intensity, power if intensity else 1.0, amount, fresnel_power, bias)


def merge_gloss(counts: dict[GlossMaterial, int], limit: int = GLOSS_MAX_MATERIALS) -> dict:
    """{material: the material it is drawn with}: the `limit` most used (by triangles) as
    they are, the others the nearest of those (log distance over the parameters)."""
    kept = sorted(counts, key=lambda m: (-counts[m], m))[:limit]

    def distance(a: GlossMaterial, b: GlossMaterial) -> float:
        d = 0.0
        for x, y in ((a.intensity, b.intensity), (a.power, b.power), (a.reflection, b.reflection)):
            d += (math.log(x + 0.01) - math.log(y + 0.01)) ** 2
        return d + (a.bias - b.bias) ** 2 + (a.fresnel_power != b.fresnel_power)

    return {m: m if m in kept else min(kept, key=lambda k: (distance(m, k), k)) for m in counts}


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


def _blocks(rgba: bytes | bytearray, width: int, height: int):
    """RGBA8 (width, height multiples of 4) as (blocks, 16 texels, 4) int64, blocks row-major."""
    px = np.frombuffer(bytes(rgba), dtype=np.uint8, count=width * height * 4).reshape(height // 4, 4, width // 4, 4, 4)
    return px.transpose(0, 2, 1, 3, 4).reshape(-1, 16, 4).astype(np.int64)


def _colour_blocks(px) -> np.ndarray:
    """_colour_block for every block at once (px: (blocks, 16, 3) ints): (blocks, 8) uint8.
    The covariance sums are exact (multiples of 1/256), so their order doesn't matter."""
    lo, hi = px.min(axis=1), px.max(axis=1)
    axis = (hi - lo).argmax(axis=1)  # the first widest channel, as max(range(3), key=...)
    mean = px.sum(axis=1) / 16
    rows = np.arange(len(px))
    centred = px - mean[:, None, :]
    cov = (centred[rows, :, axis][:, :, None] * centred).sum(axis=1)  # (blocks, 3)
    flip = (cov < 0) & (np.arange(3)[None, :] != axis[:, None])
    e0, e1 = np.where(flip, lo, hi), np.where(flip, hi, lo)
    inset = (e0 - e1) / 16
    e0 = np.clip(np.rint(e0 - inset), 0, 255).astype(np.int64)  # round(): half to even, as np.rint
    e1 = np.clip(np.rint(e1 + inset), 0, 255).astype(np.int64)

    def q565(e):
        return (e[:, 0] * 31 + 127) // 255 << 11 | (e[:, 1] * 63 + 127) // 255 << 5 | (e[:, 2] * 31 + 127) // 255

    def x565(c):
        r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
        return np.stack(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)), axis=1)

    c0, c1 = q565(e0), q565(e1)
    c0, c1 = np.maximum(c0, c1), np.minimum(c0, c1)
    p0, p1 = x565(c0), x565(c1)
    pal = np.stack((p0, p1, (2 * p0 + p1) // 3, (p0 + 2 * p1) // 3), axis=1)  # (blocks, 4, 3)
    dist = ((px[:, :, None, :] - pal[:, None, :, :]) ** 2).sum(axis=3)  # (blocks, 16, 4)
    best = dist.argmin(axis=2)  # the first nearest, as min(range(4), key=...)
    bits = (best << (2 * np.arange(16))).sum(axis=1)
    bits = np.where(c0 == c1, 0, bits)
    out = np.zeros((len(px), 8), dtype=np.uint8)
    for k, (value, size) in enumerate(((c0, 2), (c1, 2), (bits, 4))):
        at = (0, 2, 4)[k]
        for i in range(size):
            out[:, at + i] = (value >> (8 * i)) & 0xFF
    return out


def _alpha_blocks(alpha) -> np.ndarray:
    """_alpha_block for every block at once (alpha: (blocks, 16) ints): (blocks, 8) uint8."""
    a0, a1 = alpha.max(axis=1), alpha.min(axis=1)
    k = np.arange(1, 7)
    pal = np.concatenate((a0[:, None], a1[:, None], ((7 - k) * a0[:, None] + k * a1[:, None]) // 7), axis=1)
    best = np.abs(alpha[:, :, None] - pal[:, None, :]).argmin(axis=2)  # the first nearest
    bits = (best << (3 * np.arange(16))).sum(axis=1)
    bits = np.where(a0 == a1, 0, bits)
    out = np.zeros((len(alpha), 8), dtype=np.uint8)
    out[:, 0], out[:, 1] = a0, a1
    for i in range(6):
        out[:, 2 + i] = (bits >> (8 * i)) & 0xFF
    return out


def encode_dxt5(rgba: bytes | bytearray, width: int, height: int) -> bytes:
    """Encode RGBA8 (width, height multiples of 4) as DXT5 blocks, row-major
    (_alpha_block + _colour_block per block, computed for all blocks at once)."""
    px = _blocks(rgba, width, height)
    return np.concatenate((_alpha_blocks(px[:, :, 3]), _colour_blocks(px[:, :, :3])), axis=1).tobytes()


def encode_dxt1(rgba: bytes | bytearray, width: int, height: int) -> bytes:
    """Encode RGB of RGBA8 as opaque DXT1 blocks (four-colour mode only;
    alpha is dropped). Vanilla interior entries ship DXT1 textures too."""
    return _colour_blocks(_blocks(rgba, width, height)[:, :, :3]).tobytes()


def downsample(rgba: bytes | bytearray, width: int, height: int) -> tuple[bytearray, int, int]:
    """2x2 box filter."""
    w2, h2 = max(1, width // 2), max(1, height // 2)
    if width < 2 or height < 2:  # a one-texel row or column: the texel loop as it was
        out = bytearray(w2 * h2 * 4)
        for y in range(h2):
            r0, r1 = (2 * y) * width * 4, (2 * y + 1) * width * 4
            for x in range(w2):
                a, b = r0 + 8 * x, r1 + 8 * x
                o = (y * w2 + x) * 4
                for c in range(4):
                    out[o + c] = (rgba[a + c] + rgba[a + 4 + c] + rgba[b + c] + rgba[b + 4 + c] + 2) // 4
        return out, w2, h2
    px = np.frombuffer(bytes(rgba), dtype=np.uint8, count=width * height * 4).reshape(height, width, 4)
    px = px[: 2 * h2, : 2 * w2].astype(np.int64)
    out = (px[0::2, 0::2] + px[0::2, 1::2] + px[1::2, 0::2] + px[1::2, 1::2] + 2) // 4
    return bytearray(out.astype(np.uint8).tobytes()), w2, h2


def dds_header(width: int, height: int, mips: int, template: bytes | None = None, fourcc: bytes = b"DXT5") -> bytes:
    """A DXT5 (or DXT1) DDS header; copies `template` (e.g. the host texture's)
    and patches the size fields, so flags and caps match what the game ships."""
    if template is not None and template[:4] == b"DDS " and template[84:88] in (b"DXT5", b"DXT1"):
        h = bytearray(template[:DDS_HEADER_SIZE])
    else:
        h = bytearray(
            struct.pack(
                "<4s7I44s2I4s5I5I",
                b"DDS ",
                124,
                0xA1007,
                0,
                0,
                0,
                0,
                0,
                bytes(44),
                32,
                0x4,
                b"DXT5",
                0,
                0,
                0,
                0,
                0,
                0x401008,
                0,
                0,
                0,
                0,
            )
        )
    linear = width * height // (2 if fourcc == b"DXT1" else 1)  # DXT1: half a byte per texel
    struct.pack_into("<IIII", h, 12, height, width, linear, 0)
    struct.pack_into("<I", h, 28, mips)
    h[84:88] = fourcc
    return bytes(h)


def dds_file(
    rgba: bytes | bytearray,
    width: int,
    height: int,
    mips: int = DEFAULT_MIPS,
    template: bytes | None = None,
    fourcc: bytes = b"DXT5",
) -> bytes:
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
    from dsfforza.formats import xds

    height, width = struct.unpack_from("<II", dds, 12)
    return width, height, xds.decode_rgba(dds[DDS_HEADER_SIZE:], width, height, dds[84:88])


# --- atlas layout -------------------------------------------------------------------

SOLID_CELL = 8  # destination texels per solid-colour cell
PAD = 4  # destination texels of real neighbouring content around each region
ALIGN = 4  # DXT block alignment
MIN_GAP = 2  # source texels of empty seam needed to cut regions apart


@dataclass
class Region:
    """A rectangle of a Forza atlas copied into the DSF atlas at `scale`."""

    family: str
    src: tuple[int, int, int, int]  # x0, y0, x1, y1 in source texels
    dst: tuple[int, int] = (0, 0)  # top-left of the padded block in the DSF atlas
    size: tuple[int, int] = (0, 0)  # padded block size in DSF texels

    def map(
        self, u: float, v: float, src_size: tuple[int, int], scale: float, atlas: tuple[int, int]
    ) -> tuple[float, float]:
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
    whole = (
        min(boxes[i][0] for i in ids),
        min(boxes[i][1] for i in ids),
        max(boxes[i][2] for i in ids),
        max(boxes[i][3] for i in ids),
    )
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
    rectangle index. With numpy (_find_regions_np) unless a UV is NaN or a box
    is inside out; then box by box (_find_regions_loop)."""
    width, height = src_size
    try:
        uv = np.asarray(triangles, dtype=np.float64)
    except (ValueError, TypeError):
        uv = None
    if uv is None or uv.ndim != 3 or uv.shape[1:] != (3, 2) or np.isnan(uv).any():
        return _find_regions_loop(triangles, src_size)
    # The loop's min(1.0, max(0.0, u)) for non-NaN u (a -0.0 can differ, but not its floor or ceil).
    us = np.minimum(1.0, np.maximum(0.0, uv[:, :, 0])) * width
    vs = np.minimum(1.0, np.maximum(0.0, uv[:, :, 1])) * height
    boxes = np.stack(
        (
            np.maximum(0, np.floor(us.min(axis=1)).astype(np.int64) - 1),
            np.maximum(0, np.floor(vs.min(axis=1)).astype(np.int64) - 1),
            np.minimum(width, np.ceil(us.max(axis=1)).astype(np.int64) + 1),
            np.minimum(height, np.ceil(vs.max(axis=1)).astype(np.int64) + 1),
        ),
        axis=1,
    )
    if not ((boxes[:, 2] >= boxes[:, 0]).all() and (boxes[:, 3] >= boxes[:, 1]).all()):
        return _find_regions_loop(triangles, src_size)
    return _find_regions_np(boxes)


def _groups_np(ids, boxes, axis: int) -> list:
    """_groups for a box array with x1 >= x0, y1 >= y0: in the loop's sweep, the
    running end of a group is then the running end of all boxes so far (a new
    group starts at or past every earlier end, and its own end is past its
    start), so the seams are where a start reaches the earlier boxes' largest
    end + MIN_GAP."""
    order = ids[np.argsort(boxes[ids, axis], kind="stable")]  # sorted() is stable
    start, end = boxes[order, axis], boxes[order, axis + 2]
    seam = np.flatnonzero(start[1:] >= np.maximum.accumulate(end)[:-1] + MIN_GAP) + 1
    return np.split(order, seam)


def _best_split_np(ids, boxes, gain: float = 0.85, min_area: int = 64 * 64):
    """_best_split on a box array: prefix and suffix boxes by accumulated min /
    max; per axis the smallest total, at the largest k among equals (the loop
    goes down from the end and keeps the first), y only if strictly smaller."""
    b = boxes[ids]
    whole = (int(b[:, 0].min()), int(b[:, 1].min()), int(b[:, 2].max()), int(b[:, 3].max()))
    if len(ids) < 2 or _area(whole) < min_area:
        return None
    best = None
    for axis in (0, 1):
        order = ids[np.argsort(b[:, axis] + b[:, axis + 2], kind="stable")]
        s = boxes[order]
        lo, hi = np.minimum.accumulate(s[:, :2], axis=0), np.maximum.accumulate(s[:, 2:], axis=0)
        rlo = np.minimum.accumulate(s[::-1, :2], axis=0)[::-1]
        rhi = np.maximum.accumulate(s[::-1, 2:], axis=0)[::-1]
        head = (hi[:-1] - lo[:-1]).prod(axis=1)  # area of the boxes [0, k), k = 1 .. n - 1
        tail = (rhi[1:] - rlo[1:]).prod(axis=1)  # area of the boxes [k, n)
        total = head + tail
        k = len(total) - int(np.argmin(total[::-1]))  # the largest k with the smallest total
        if best is None or int(total[k - 1]) < best[0]:
            best = (int(total[k - 1]), order[:k], order[k:])
    if best is None or best[0] > gain * _area(whole):
        return None
    return [best[1], best[2]]


def _find_regions_np(boxes) -> tuple[list[tuple[int, int, int, int]], list[int]]:
    """find_regions' cut on a box array (_groups_np, _best_split_np): the same
    rectangles in the same order."""
    rects, owner = [], np.zeros(len(boxes), dtype=np.int64)
    stack = [(np.arange(len(boxes)), 0)]
    while stack:
        ids, axis = stack.pop()
        if not len(ids):
            continue
        for a in (axis, 1 - axis):
            groups = _groups_np(ids, boxes, a)
            if len(groups) > 1:
                stack.extend((g, 1 - a) for g in groups)
                break
        else:
            halves = _best_split_np(ids, boxes)
            if halves:
                stack.extend((g, axis) for g in halves)
                continue
            b = boxes[ids]
            owner[ids] = len(rects)
            rects.append((int(b[:, 0].min()), int(b[:, 1].min()), int(b[:, 2].max()), int(b[:, 3].max())))
    return rects, owner.tolist()


def _find_regions_loop(triangles: list, src_size: tuple[int, int]) -> tuple[list[tuple[int, int, int, int]], list[int]]:
    """find_regions box by box."""
    width, height = src_size
    boxes = []
    for tri in triangles:
        us = [min(1.0, max(0.0, u)) * width for u, _ in tri]
        vs = [min(1.0, max(0.0, v)) * height for _, v in tri]
        boxes.append(
            (
                max(0, math.floor(min(us)) - 1),
                max(0, math.floor(min(vs)) - 1),
                min(width, math.ceil(max(us)) + 1),
                min(height, math.ceil(max(vs)) + 1),
            )
        )
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
            rect = (
                min(boxes[i][0] for i in ids),
                min(boxes[i][1] for i in ids),
                max(boxes[i][2] for i in ids),
                max(boxes[i][3] for i in ids),
            )
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
    swatch_trims: bool = False  # give swatch trims (black, frame...) their Forza colour
    plain: tuple[int, int, int, int] = DEFAULT_PLAIN
    weights: dict[str, float] = field(default_factory=lambda: {fm.NODAMAGE: 1.0, fm.LIGHTS: 1.0, fm.INTERIOR: 1.0})
    # Cockpit carbins (src/dsfforza/convert/cockpit.py): the _LOD0 atlases, fm.classify_cockpit,
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
    alpha_only: set = field(default_factory=set)  # (family, region index) used only by alpha-tested tris
    resolved: dict = field(default_factory=dict)  # (id(section), id(sub)) -> SubMaterial
    regions: dict = field(default_factory=dict)  # family -> [Region]
    owners: dict = field(default_factory=dict)  # (id(sub), triangle index) -> region index
    solids: dict = field(default_factory=dict)  # key -> (x, y) of a solid cell
    solid_colours: dict = field(default_factory=dict)  # key -> RGBA
    scale: dict = field(default_factory=dict)  # family -> DSF texels per source texel
    src_sizes: dict = field(default_factory=dict)
    atlases: fm.AtlasCache | None = None
    atlas_subs: set = field(default_factory=set)  # id(sub) with triangles in the atlas
    dropped: set = field(default_factory=set)  # (id(sub), triangle index) cut out by alpha
    cutout: set = field(default_factory=set)  # id(sub) entirely cut out by alpha
    lens_subs: set = field(default_factory=set)  # id(sub): rear lamp lenses moved from glass to the atlas (is_lens)
    lamp_tris: list = field(default_factory=list)  # (lod, positions, atlas uvs) of Lights-atlas triangles
    lamp_swatch_tris: list = field(default_factory=list)  # (lod, positions) of Lights swatch triangles
    _covers: dict = field(default_factory=dict)  # id(sub) -> clear_cover
    wheel_ids: set = field(default_factory=set)  # id(section) of the Forza wheel model (plan(wheels=...))
    art_flat: set = field(default_factory=set)  # id(sub): trim artwork drawn flat (ART_MIN_SCALE_SHARE)
    pack_scale: float = 0.0  # the uniform scale _pack found (before family weights)
    wheel_paint: dict = field(default_factory=dict)  # rim technique -> PaintScale applied (_wheel_paint_scale)
    # Exterior only: lamp lenses baked over what lies behind them (src/dsfforza/convert/lenses.py).
    bake_lenses: bool = True
    lens_report: dict = field(default_factory=dict)  # lenses.bake's report
    # (section name, technique) of swatch lenses left out, their tint on the lamp behind (lenses.bake)
    dropped_lenses: set = field(default_factory=set)
    # Exterior only: atlas trims and rims with Forza's specular and reflection draw with
    # copies of the host Lights material (GLOSS_PREFIX, _plan_gloss).
    gloss_enabled: bool = True
    gloss: dict = field(default_factory=dict)  # id(sub) -> index into gloss_materials
    gloss_materials: list = field(default_factory=list)  # [GlossMaterial]
    gloss_report: dict = field(default_factory=dict)  # technique -> {material index: triangles}
    # Head lamps by day (issue #124): the raw glow of the clear front lens texels and the head lamp
    # surfaces (lenses.new_glow, lens_glow, lamp_glow); None: none. _glow() turns it into glow
    # alpha, which render() takes over the converted alpha.
    lamp_raw: np.ndarray | None = None

    # -- planning --
    def plan(self, sections, lods, extra_solids: dict | None = None, wheels=None) -> Texturing:
        """Resolve every subsection at `lods` and lay out the atlas.
        extra_solids: key -> RGBA cells to reserve as well (`solid_uv(key)`).
        wheels: (wheel sections, their LODs): the Forza wheel model, whose rims
        then sample the car's wheel texture (fm.classify_wheel) through the
        atlas, like the centre caps vanilla wheels draw with the Chassis material.
        Without the wheel texture in the car folder the rims keep their
        swatches (forza2dsf.WHEEL_UV)."""
        self.atlases = fm.AtlasCache(self.car_dir, lod0=self.lod0)
        work = [(section, lods) for section in sections]
        if wheels is not None and self.wheel_texture():
            if fm.WHEEL not in self.families:
                self.families = tuple(self.families) + (fm.WHEEL,)
            side = max(self.atlases.get(fm.WHEEL)[:2])
            self.weights.setdefault(fm.WHEEL, WHEEL_WEIGHT * min(1.0, WHEEL_TEXELS / side))
            self.wheel_ids |= {id(section) for section in wheels[0]}
            work += [(section, set(wheels[1])) for section in wheels[0]]
            self.wheel_paint = self._wheel_paint_scale(wheels[0], set(wheels[1]))
        for family, rgb in self.filler.items():
            if family in self.families:
                fm.fill_transparent(self.atlases.get(family), rgb, below=PAINT_THROUGH_ALPHA)
        if self.bake_lenses and self.classifier is None and fm.LIGHTS in self.families:
            self._bake_lenses(work, lods)
        # Trims whose Forza technique scales its texels (trim_art, fm.with_scale) sample
        # the scaled atlas, a family of its own.
        for section, section_lods in work:
            for sub in section.subsections:
                if sub.lod in section_lods:
                    family = self._resolve(section, sub).family
                    if family and family not in self.families and fm.base_family(family) in self.families:
                        self.families = tuple(self.families) + (family,)
                        self.weights.setdefault(family, self.weights.get(fm.base_family(family), 1.0))
        tris: dict[str, list] = {f: [] for f in self.families}
        keys: dict[str, list] = {f: [] for f in self.families}
        tested: dict[str, list] = {f: [] for f in self.families}
        swatches: list[fm.SubMaterial] = []
        art_subs = {}  # id(sub) -> (section, sub, SubMaterial): trim artwork and detail (trim_art)
        detail_ids = set()  # id(sub) of the detail ones
        for section, section_lods in work:
            for sub in section.subsections:
                if sub.lod not in section_lods:
                    continue
                # Resolved once (_bake_lenses resolves first: is_lens reads the unbaked lens).
                m = self.resolved.get((id(section), id(sub))) or self._resolve(section, sub)
                self.resolved[(id(section), id(sub))] = m
                alpha_tested = m.technique in fm.ALPHA_TESTED
                if self._uses_atlas(m) and m.kind == "textured":
                    technique = fm.technique_scale(m.technique)[0]
                    if technique in ART_TECHNIQUES or technique in DETAIL_TECHNIQUES:
                        art_subs[id(sub)] = (section, sub, m)
                    if technique in DETAIL_TECHNIQUES:
                        detail_ids.add(id(sub))
                    pool = section.vertices_for(sub)
                    for k, tri in enumerate(sub.triangles()):
                        uvs = [m.uv(pool[i].uv0) for i in tri]
                        if alpha_tested and fm.triangle_alpha_cut(self.atlases.get(m.family), uvs):
                            self.dropped.add((id(sub), k))
                            continue
                        tris[m.family].append(uvs)
                        keys[m.family].append((id(sub), k))
                        if m.family == fm.LIGHTS:
                            self.lamp_tris.append((sub.lod, [pool[i].position for i in tri], uvs))
                        tested[m.family].append(alpha_tested)
                        self.atlas_subs.add(id(sub))
                    if id(sub) not in self.atlas_subs:
                        self.cutout.add(id(sub))
                elif self._uses_atlas(m) and m.kind == "swatch":
                    swatches.append((sub, m))
                    if fm.base_family(m.family) == fm.LIGHTS:
                        pool = section.vertices_for(sub)
                        self.lamp_swatch_tris += [(sub.lod, [pool[i].position for i in t]) for t in sub.triangles()]
        art = {key for f in self.families for key in keys[f] if key[0] in art_subs}
        self._layout(tris, keys, tested, swatches, extra_solids)
        if art:
            # Trim artwork and detail (trim_art) may not cost the rest of the car much
            # resolution: past ART_MIN_SCALE_SHARE the detail subsections take one flat
            # colour each, then the artwork's too.
            with_all = self.pack_scale
            flat = {sid: self._art_swatch(*art_subs[sid]) for sid in {k[0] for k in art}}

            def layout(flat_ids):
                kept = {f: [n for n, key in enumerate(keys[f]) if key[0] not in flat_ids] for f in self.families}
                self._layout(
                    {f: [tris[f][n] for n in kept[f]] for f in self.families},
                    {f: [keys[f][n] for n in kept[f]] for f in self.families},
                    {f: [tested[f][n] for n in kept[f]] for f in self.families},
                    swatches + [(art_subs[sid][1], flat[sid]) for sid in flat_ids],
                    extra_solids,
                )
                return self.pack_scale

            base = layout(set(flat))
            details = set(flat) & detail_ids
            if with_all >= ART_MIN_SCALE_SHARE * base:
                chosen = set()
            elif details and layout(details) >= ART_MIN_SCALE_SHARE * base:
                chosen = details
            else:
                chosen = set(flat)
            layout(chosen)
            for sid in chosen:
                section, sub = art_subs[sid][:2]
                self.resolved[(id(section), id(sub))] = flat[sid]
                self.atlas_subs.discard(sid)
            self.art_flat = chosen
        self._plan_gloss(work)
        return self

    def _plan_gloss(self, work) -> None:
        """Pick a gloss material (GlossMaterial) for every subsection drawn with the
        Chassis material from nodamage (trims, badges) or the wheel texture (rims) whose
        Forza technique has a specular or a reflection (fm.gloss_params with the car's
        ShaderSettings): Forza's terms on the green its texels average as drawn. At most
        GLOSS_MAX_MATERIALS distinct ones (merge_gloss). Exterior only (no classifier)."""
        if not self.gloss_enabled or self.classifier is not None or self.atlases is None:
            return
        from dsfforza.convert import trim_art

        try:
            settings = fm.shader_settings(self.car_dir)
        except OSError:
            settings = {}
        picks: dict[int, tuple[GlossMaterial, str, int]] = {}  # id(sub) -> (material, technique, triangles)
        images: dict[str, np.ndarray] = {}
        for section, section_lods in work:
            for sub in section.subsections:
                if sub.lod not in section_lods or id(sub) in picks or self.material(section, sub) != CHASSIS:
                    continue
                m = self.submaterial(section, sub)
                if fm.base_family(m.family) not in (fm.NODAMAGE, fm.WHEEL):
                    continue
                technique = fm.technique_source(m.technique)
                g = fm.gloss_params(settings, technique)
                if g is None:
                    continue
                floor = GLOSS_GREEN_FLOOR
                if m.kind == "swatch":
                    green, floor = float(self._convert_texel(m.family, *m.colour)[1]), GLOSS_CELL_GREEN_FLOOR
                else:
                    if m.family not in images:
                        w, h, rgba = self.atlases.get(m.family)
                        images[m.family] = np.frombuffer(bytes(rgba), dtype=np.uint8).reshape(h, w, 4)
                    image = images[m.family]
                    h, w = image.shape[:2]
                    pool = section.vertices_for(sub)
                    rows = [
                        trim_art.triangle_texels(image, [((u := m.uv(pool[i].uv0))[0] * w, u[1] * h) for i in tri])
                        for k, tri in enumerate(sub.triangles())
                        if (id(sub), k) not in self.dropped
                    ]
                    if not rows:
                        continue
                    texels = np.concatenate(rows)
                    if m.family != fm.WHEEL:  # paint-through filler is drawn as the plain texel
                        opaque = texels[texels[:, 3] >= trim_art.ALPHA_OPAQUE]
                        texels = opaque if len(opaque) else texels
                    green = float(texels[:, 1].mean())
                mat = gloss_material(g, green, floor)
                if mat is not None:
                    picks[id(sub)] = (mat, technique, len(sub.triangles()))
        counts: dict[GlossMaterial, int] = {}
        for mat, _, n in picks.values():
            counts[mat] = counts.get(mat, 0) + n
        merged = merge_gloss(counts)
        self.gloss_materials = sorted(set(merged.values()))
        index = {mat: n for n, mat in enumerate(self.gloss_materials)}
        self.gloss, self.gloss_report = {}, {}
        for sid, (mat, technique, n) in picks.items():
            k = index[merged[mat]]
            self.gloss[sid] = k
            per = self.gloss_report.setdefault(technique, {})
            per[k] = per.get(k, 0) + n

    def _wheel_paint_scale(self, sections, lods) -> dict[str, float]:
        """Scale, in place, the RGB of the wheel texture's texels by the PaintScale
        of the rim technique whose UV triangles cover them (fm.wheel_paint_scale):
        Forza's rim shaders multiply the texel by it, DSF's Chassis material draws
        the texel as is. Texels no triangle covers take the scale that covers the
        most texels. Without it a light grey rim texture (the Lotus Carlton's 182,
        the median rim 156) drew near white (research/wheels.md, "Rim
        brightness"). Returns {technique: scale} of the wheel's techniques."""
        settings = fm.shader_settings(self.car_dir)
        w, h, rgba = self.atlases.get(fm.WHEEL)
        scales: dict[str, float] = {}
        triangles: dict[float, list] = {}
        for section in sections:
            for sub in section.subsections:
                if sub.lod not in lods:
                    continue
                m = fm.resolve(section, sub, fm.classify_wheel)
                if m.family != fm.WHEEL or m.kind not in ("textured", "swatch"):
                    continue
                k = fm.wheel_paint_scale(sub.material, settings)
                scales[sub.material] = k
                pool = section.vertices_for(sub)
                triangles.setdefault(k, []).extend(
                    [(u * w, v * h) for u, v in (m.uv(pool[i].uv0) for i in t)] for t in sub.triangles()
                )
        if all(k == 1.0 for k in triangles):
            return scales
        from dsfforza.convert import trim_art

        label = np.full((h, w), -1, dtype=np.int64)
        order = sorted(triangles, key=lambda k: -len(triangles[k]))  # the fewer triangles drawn last, on top
        for n, k in enumerate(order):
            for pts in triangles[k]:
                ys, xs = trim_art.triangle_cells((h, w), pts)
                label[ys, xs] = n
        covered = label[label >= 0]
        base = int(np.bincount(covered).argmax()) if len(covered) else 0
        label[label < 0] = base
        factor = np.array(order, dtype=np.float64)[label]
        px = np.frombuffer(rgba, dtype=np.uint8).reshape(h, w, 4)
        px[:, :, :3] = np.clip(np.rint(px[:, :, :3] * factor[:, :, None]), 0, 255).astype(np.uint8)
        return scales

    def _bake_lenses(self, work, lods) -> None:
        """Forza's lamp lenses and lamp glass are glass; the Lights material is opaque. Each
        lens drawn from the Lights atlas (lenses.is_lens_technique), each lamp glass
        subsection Forza draws less than opaque (lights_gls_*, GlassMaxOpacity under 1) and
        each textured lamp cover over a lamp (lamp_cover) gets what Forza shows through it
        baked into its texels (src/dsfforza/convert/lenses.py). Swatch lenses whose tint
        goes onto the lamp behind them are left out (dropped_lenses)."""
        import dataclasses

        from dsfforza.convert import lenses

        settings = lenses.glass_settings(self.car_dir)
        ids = set()
        body = []
        for section, section_lods in work:
            if id(section) in self.wheel_ids:
                continue
            body.append(section)
            for sub in section.subsections:
                if sub.lod in section_lods:
                    self.resolved[(id(section), id(sub))] = self._resolve(section, sub)
        shapes = self._lamp_shapes(body, set(lods))
        for section in body:
            for sub in section.subsections:
                m = self.resolved.get((id(section), id(sub)))
                if m is None or m.family != fm.LIGHTS or m.kind != "textured":
                    continue
                technique = m.technique.lower()
                if self.lamp_cover(section, sub, m, shapes):
                    if m.role != fm.TEXTURED:
                        m = dataclasses.replace(m, role=fm.TEXTURED)
                        self.resolved[(id(section), id(sub))] = m
                    self.lens_subs.add(id(sub))
                    ids.add(id(sub))
                elif m.role == fm.TEXTURED and (
                    lenses.is_lens_technique(m.technique)
                    or (
                        technique.startswith(lenses.LAMP_GLASS)
                        and technique not in TEXTURED_COVERS
                        and lenses.opacity_rule(m.technique, settings)[1] < 1.0
                    )
                ):
                    ids.add(id(sub))
        # The head lamps' raw glow (clear front lenses and head lamp surfaces), for _glow.
        w, h, _ = self.atlases.get(fm.LIGHTS)
        glow = lenses.new_glow(h, w)
        if ids:
            self.lens_report = lenses.bake(
                body, set(lods), self.atlases, ids, self.car_dir, glow=glow, dropped=self.dropped_lenses
            )
        lenses.lamp_glow(body, set(lods), self.atlases, glow)
        self.lamp_raw = glow if np.isfinite(glow[..., 0]).any() else None

    def _lamp_shapes(self, sections, lods) -> list:
        """(lod, positions) of the Lights surfaces of `sections` (atlas or swatch; not lenses or covers),
        for _over_lamp before plan() has collected lamp_tris."""
        from dsfforza.convert import lenses

        out = []
        for section in sections:
            for sub in section.subsections:
                m = self.resolved.get((id(section), id(sub)))
                if (
                    sub.lod not in lods
                    or m is None
                    or m.role != fm.TEXTURED
                    or fm.base_family(m.family or "") != fm.LIGHTS
                    or lenses.is_lens_technique(m.technique)
                    or m.technique.lower() in TEXTURED_COVERS
                ):
                    continue
                pool = section.vertices_for(sub)
                out += [(sub.lod, [pool[i].position for i in t]) for t in sub.triangles()]
        return out

    def lamp_cover(self, section, sub, m: fm.SubMaterial, shapes=None) -> bool:
        """A textured lamp cover (TEXTURED_COVERS: lights_glass, lights_gls_noemit) on the
        Lights atlas, outside the window sections, with a Lights surface behind at least
        COVER_MIN_SHARE of its area: baked over the lamp (_bake_lenses) instead of drawn as
        DSF's dark window glass, which hid the lamp (127 cars, issue #89), or moved to the
        atlas as an opaque plate (is_lens). Forza draws it as the texel with an opacity of
        max(texel alpha, GlassColor.w) capped by GlassMaxOpacity (lenses.opacity_rule)."""
        if (
            self.classifier is not None
            or fm.base_family(m.family or "") != fm.LIGHTS
            or m.kind != "textured"
            or m.technique.lower() not in TEXTURED_COVERS
            or section.name.lower() in WINDOW_SECTIONS
        ):
            return False
        key = ("textured", id(sub))
        if key not in self._covers:
            self._covers[key] = self._over_lamp(section, sub, shapes) >= COVER_MIN_SHARE
        return self._covers[key]

    def _art_swatch(self, section, sub, m: fm.SubMaterial) -> fm.SubMaterial:
        """A trim-artwork subsection as one flat colour: the mean of the opaque
        texels under its triangles (trim_art.triangle_texels)."""
        import dataclasses

        from dsfforza.convert import trim_art

        w, h, rgba = self.atlases.get(m.family)
        image = np.frombuffer(bytes(rgba), dtype=np.uint8).reshape(h, w, 4)
        pool = section.vertices_for(sub)
        texels = [
            trim_art.triangle_texels(image, [((u := m.uv(pool[i].uv0))[0] * w, u[1] * h) for i in tri])
            for tri in sub.triangles()
        ]
        texels = np.concatenate(texels)
        opaque = texels[texels[:, 3] >= trim_art.ALPHA_OPAQUE]
        colour = tuple(int(round(c)) for c in (opaque if len(opaque) else texels).mean(0))
        return dataclasses.replace(m, kind="swatch", colour=colour)

    def _tiling_swatch(self, section, sub, m: fm.SubMaterial) -> fm.SubMaterial:
        """A lamp part that tiles the Lights atlas (UVs past 0..1: the FM4 Mustang GT's
        and 300C's reflector bowls, `textured_reflector`) as one flat Lights colour: the
        mean texel at its corners and centres, UVs wrapped as Forza's sampler does. The
        atlas regions can't hold a tiling footprint, and as untextured trim the bowls
        drew in the shared trim's dark texel: the Mustang's head lamps were a black strip
        (issue #158)."""
        import dataclasses

        w, h, rgba = self.atlases.get(m.family)
        image = np.frombuffer(bytes(rgba), dtype=np.uint8).reshape(h, w, 4)
        pool = section.vertices_for(sub)
        uvs = np.array([[m.uv(pool[i].uv0) for i in tri] for tri in sub.triangles()], float)
        if not len(uvs):
            return m
        uvs = np.concatenate([uvs.reshape(-1, 2), uvs.mean(1)]) % 1.0
        texels = image[(uvs[:, 1] * h).astype(int).clip(0, h - 1), (uvs[:, 0] * w).astype(int).clip(0, w - 1)]
        colour = tuple(int(round(c)) for c in texels.astype(float).mean(0))
        return dataclasses.replace(m, kind="swatch", colour=colour)

    def _layout(self, tris, keys, tested, swatches, extra_solids) -> None:
        """Regions, swatch cells and packing for these atlas triangles and swatches."""
        self.regions, self.owners, self.alpha_only = {}, {}, set()
        self.solids, self.solid_colours, self.scale = {}, {}, {}
        self._red_curve_done = self._lens_scale_done = self._white_scale_done = self._glow_done = False
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
        fm.sample_colours([m for _, m in swatches if m.colour is None], self.atlases)  # art swatches have theirs
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

    def wheel_texture(self) -> bool:
        """Whether the car folder holds the wheel texture (port_car.extract_wheel)."""
        try:
            fm.texture_path(self.car_dir, fm.WHEEL, self.lod0)
        except (FileNotFoundError, OSError):
            return False
        return True

    def _resolve(self, section, sub) -> fm.SubMaterial:
        if id(section) in self.wheel_ids:
            return fm.resolve(section, sub, fm.classify_wheel)
        m = fm.resolve(section, sub, self.classifier or fm.classify)
        technique, scale = fm.technique_scale(m.technique)
        if scale != 1.0 and m.family:
            import dataclasses

            m = dataclasses.replace(m, family=fm.scaled_family(m.family, scale))
        if technique in GREY_TECHNIQUES and self.atlases is not None and m.kind == "textured":
            return self._art_swatch(section, sub, m)
        tiling_lamp = m.role == fm.TEXTURED and fm.base_family(m.family) == fm.LIGHTS and m.kind == "tiling"
        if tiling_lamp and self.atlases is not None and self.classifier is None:
            return self._tiling_swatch(section, sub, m)
        if self.classifier is None and self.is_lens(section, sub, m):
            import dataclasses

            m = dataclasses.replace(m, role=fm.TEXTURED)
            self.lens_subs.add(id(sub))
        return m

    def clear_cover(self, section, sub, m: fm.SubMaterial) -> bool:
        """A lamp cover of Forza's plain `glass` technique, left out. Forza draws it as
        texture-less, nearly clear glass (TEXTURED_COVERS); DSF's shared glass is a dark
        pane (glass texture blue = alpha 192) and hid the lamp behind it: the Mustang GT
        05's head lamps were a black strip (issue #158), and at night the Veyron's and
        300C's tail lamps barely glowed through it while they glowed red without it (VM
        runs vm1-20261028-092017 and -092416; issue #160). Vanilla lamps are opaque Lights
        texels, so the lamp itself is what shows. A cover is plain glass on the Lights
        atlas, outside the window sections, with a Lights-atlas lamp surface behind at
        least COVER_MIN_SHARE of its area: some cars draw their windows, windscreen
        included, with the same technique (BUS_TOUR_12, DOD_RamSRT10_06, TOY_Prius_11)."""
        if (
            self.classifier is not None
            or m.role != fm.GLASS
            or fm.base_family(m.family) != fm.LIGHTS
            or m.technique.lower() != "glass"
            or section.name.lower() in WINDOW_SECTIONS
        ):
            return False
        key = id(sub)
        if key not in self._covers:
            self._covers[key] = self._over_lamp(section, sub) >= COVER_MIN_SHARE
        return self._covers[key]

    def _over_lamp(self, section, sub, shapes=None) -> float:
        """Share of a subsection's area whose triangle centres, seen along z towards the car,
        lie on a Lights triangle (shapes: (lod, positions); default lamp_tris and
        lamp_swatch_tris; same LOD) 0 to COVER_DEPTH behind."""
        if shapes is None:
            shapes = [(t[0], t[1]) for t in self.lamp_tris] + self.lamp_swatch_tris
        lods = {t[0] for t in shapes}
        if not lods:
            return 0.0
        lod = sub.lod if sub.lod in lods else min(lods)
        lamps = np.array([t[1] for t in shapes if t[0] == lod], float)  # (n, 3, 3)
        pool = section.vertices_for(sub)
        tris = np.array([[pool[i].position for i in t] for t in sub.triangles()], float)
        if not len(tris):
            return 0.0
        area = 0.5 * np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1)
        if area.sum() <= 0:
            return 0.0
        centres = tris.mean(1)
        outward = -1.0 if centres[:, 2].mean() < 0 else 1.0  # Forza's front is -z
        a, b, c = lamps[:, 0], lamps[:, 1], lamps[:, 2]
        hit = np.zeros(len(centres), bool)
        for i, (x, y, z) in enumerate(centres):

            def side(p, q, x=x, y=y):
                return (q[:, 0] - p[:, 0]) * (y - p[:, 1]) - (q[:, 1] - p[:, 1]) * (x - p[:, 0])

            d1, d2, d3 = side(a, b), side(b, c), side(c, a)
            inside = ((d1 >= 0) & (d2 >= 0) & (d3 >= 0)) | ((d1 <= 0) & (d2 <= 0) & (d3 <= 0))
            depth = (z - lamps[inside][:, :, 2].mean(1)) * outward
            hit[i] = bool(((depth > 0) & (depth < COVER_DEPTH)).any())
        return float(area[hit].sum() / area.sum())

    def is_lens(self, section, sub, m: fm.SubMaterial) -> bool:
        """A coloured rear lamp lens drawn with a textured lamp-cover glass technique
        (lights_glass, lights_gls_noemit on the Lights atlas; not plain glass,
        TEXTURED_COVERS). DSF's
        shared glass would draw it clear, with the glass texture's crack
        bands, over whatever lies behind: the DMC-12's tail lamps are a white
        reflector under a red, amber and white grid lens, and came out white
        with crack lines (runs host-20261023-082346, -083742). Vanilla tail lamps
        are opaque Lights-material texels, so such lenses go to the atlas.
        Front covers stay glass: on several FH1 cars (Challenger, S65, 300C,
        Golf R) their UVs land on red texels Forza doesn't show."""
        if (
            m.role != fm.GLASS
            or m.family != fm.LIGHTS
            or m.kind != "textured"
            or m.technique.lower() not in TEXTURED_COVERS
            or self.atlases is None
            or fm.LIGHTS not in self.families
        ):
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
            sizes = [
                (
                    _round_up((r.src[2] - r.src[0]) * self._family_scale(s, r.family)) + 2 * PAD,
                    _round_up((r.src[3] - r.src[1]) * self._family_scale(s, r.family)) + 2 * PAD,
                )
                for r in regions
            ]
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
        for key, p in zip(solid_keys, pos[len(regions) :]):
            self.solids[key] = p
        self.scale = {family: self._family_scale(s, family) for family in self.families}
        self.pack_scale = s

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
        if id(sub) in self.cutout or self.clear_cover(section, sub, m) or self.dropped_lens(section, sub):
            return fm.SKIP
        if self._uses_atlas(m) and (
            (m.kind == "textured" and id(sub) in self.atlas_subs) or (m.kind == "swatch" and self._has_cell(m))
        ):
            if m.family == fm.LIGHTS:
                return LIGHTS_MAT
            if id(sub) in self.gloss:
                return f"{GLOSS_PREFIX}{self.gloss[id(sub)]}"
            return CHASSIS
        if m.role == fm.TEXTURED:
            return fm.TRIM  # tiling/procedural lamp parts: untextured
        return m.role

    def dropped_lens(self, section, sub) -> bool:
        """A swatch lens left out at every LOD: lenses.bake laid its tint over the lamp behind."""
        return (section.name, sub.material) in self.dropped_lenses

    def _has_cell(self, m: fm.SubMaterial) -> bool:
        """Whether a swatch has its colour cell: a subsection at a LOD plan() didn't lay
        out (the cockpit's shell of exterior sections) has none; a trim_art grey cell
        there gets a colour from _resolve all the same, and draws as shared trim."""
        return m.colour is not None and ("swatch",) + self._convert_texel(m.family, *m.colour) in self.solids

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
        if not is_atlas_kind(self.material(section, sub)):
            return uvs, None
        if m.kind == "swatch":
            key = ("swatch",) + self._convert_texel(m.family, *m.colour)
            return [self.solid_uv(key)] * len(uvs), key
        n = self.owners[(id(sub), triangle_index)]
        region = self.regions[m.family][n]
        mapped = [
            region.map(
                min(1.0, max(0.0, u)), min(1.0, max(0.0, v)), self.src_sizes[m.family], self.scale[m.family], self.size
            )
            for u, v in uvs
        ]
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

    def visible_red(self) -> list[int]:
        """Brightest channel of the red Lights-atlas texels a camera straight behind the car
        sees (orthographic, z-buffered over the Lights-atlas triangles of the finest planned
        LOD, one sample per VISIBLE_STEP; Forza's rear is +z). Hidden bulbs, reflectors and
        inner lenses don't count. Clear lamp-cover glass is drawn transparent, so it doesn't
        hide anything here either."""
        px = self._visible_lamp_texels(front=False)
        if px is None:
            return []
        r, g, b = px[:, 0], px[:, 1], px[:, 2]
        red = (r > 1.6 * g) & (r > 1.6 * b)  # lamp_hue "red"
        return px[red, :3].max(1).tolist()

    def visible_white(self) -> list[int]:
        """Brightest channel of the white (lamp_hue) Lights-atlas texels a camera straight
        ahead of the car sees: as visible_red, from the front (Forza -z), over the
        triangles in the front half of the car only."""
        px = self._visible_lamp_texels(front=True)
        if px is None:
            return []
        r, g, b = px[:, 0], px[:, 1], px[:, 2]
        red = (r > 1.6 * g) & (r > 1.6 * b)
        amber = (r > 1.2 * b + 20) & (g > 0.35 * r) & (g > 1.2 * b)
        return px[~red & ~amber, :3].max(1).tolist()

    def _visible_lamp_texels(self, front: bool):
        """RGBA (n, 4) of the Lights-atlas texels an orthographic camera straight behind
        (front: ahead of) the car sees, one per VISIBLE_STEP; None without lamps."""
        seen = self._lamp_view(front)
        if seen is None:
            return None
        sw, sh, src = self.atlases.get(fm.LIGHTS)
        return np.frombuffer(bytes(src), np.uint8).reshape(sh, sw, 4)[seen].astype(int)

    def _lamp_view(self, front: bool, grid: bool = False):
        """(rows, columns) of the Lights-atlas texels _visible_lamp_texels sees, one per sample
        (grid: ((rows, columns), the (H, W) mask of the view's samples in their order, the
        view's origin x, y, its step)); None without lamps."""
        if not self.lamp_tris or self.atlases is None:
            return None
        import numpy as np

        lod = min(t[0] for t in self.lamp_tris)
        tris = [t for t in self.lamp_tris if t[0] == lod]
        if front:  # Forza's front is -z; the camera looks down +z, nearest = largest -z
            tris = [(t[0], [(x, y, -z) for x, y, z in t[1]], t[2]) for t in tris if sum(p[2] for p in t[1]) < 0]
            if not tris:
                return None
        P = np.array([t[1] for t in tris], float)  # (n, 3, 3)
        U = np.array([t[2] for t in tris], float)  # (n, 3, 2)
        x0, y0 = P[..., 0].min(), P[..., 1].min()
        W = int((P[..., 0].max() - x0) / VISIBLE_STEP) + 2
        H = int((P[..., 1].max() - y0) / VISIBLE_STEP) + 2
        if W * H > 4_000_000:
            return None
        depth = np.full((H, W), -np.inf)
        uv = np.zeros((H, W, 2))
        for n in range(len(tris)):
            X = (P[n, :, 0] - x0) / VISIBLE_STEP
            Y = (P[n, :, 1] - y0) / VISIBLE_STEP
            Z = P[n, :, 2]
            area = (X[1] - X[0]) * (Y[2] - Y[0]) - (Y[1] - Y[0]) * (X[2] - X[0])
            if abs(area) < 1e-9:
                continue
            gx, gy = np.meshgrid(
                np.arange(int(X.min()), int(np.ceil(X.max())) + 1) + 0.5,
                np.arange(int(Y.min()), int(np.ceil(Y.max())) + 1) + 0.5,
            )
            w0 = ((X[1] - gx) * (Y[2] - gy) - (Y[1] - gy) * (X[2] - gx)) / area
            w1 = ((X[2] - gx) * (Y[0] - gy) - (Y[2] - gy) * (X[0] - gx)) / area
            w2 = 1 - w0 - w1
            z = w0 * Z[0] + w1 * Z[1] + w2 * Z[2]
            ix, iy = gx.astype(int), gy.astype(int)
            ok = (w0 >= 0) & (w1 >= 0) & (w2 >= 0) & (ix < W) & (iy < H)
            ok &= z > depth[iy.clip(0, H - 1), ix.clip(0, W - 1)]
            ix, iy = ix[ok], iy[ok]
            depth[iy, ix] = z[ok]
            uv[iy, ix, 0] = (w0 * U[n, 0, 0] + w1 * U[n, 1, 0] + w2 * U[n, 2, 0])[ok]
            uv[iy, ix, 1] = (w0 * U[n, 0, 1] + w1 * U[n, 1, 1] + w2 * U[n, 2, 1])[ok]
        hit = np.isfinite(depth)
        sw, sh, _ = self.atlases.get(fm.LIGHTS)
        u = (uv[hit, 0].clip(0, 1) * sw).astype(int).clip(0, sw - 1)
        v = (uv[hit, 1].clip(0, 1) * sh).astype(int).clip(0, sh - 1)
        return ((v, u), hit, (x0, y0), VISIBLE_STEP) if grid else (v, u)

    def _red_lens_scale(self):
        """This car's red lens darkening (red_lens_scale); None until planned."""
        if getattr(self, "_lens_scale_done", False):
            return self._lens_scale
        if self.atlases is None or not self.regions:
            return None
        self._lens_scale, self._lens_scale_done = red_lens_scale(self.visible_red()), True
        return self._lens_scale

    def _white_lens_scale(self):
        """This car's white lamp texel darkening (white_lens_scale); None until planned."""
        if getattr(self, "_white_scale_done", False):
            return self._white_scale
        if self.atlases is None or not self.regions:
            return None
        self._white_scale, self._white_scale_done = white_lens_scale(self.visible_white()), True
        return self._white_scale

    def _glow(self):
        """Glow alpha of the head lamps by day (lenses.day_glow of lamp_raw, ranked per lamp
        over the surface a camera straight ahead sees), a float array the size of the Lights
        atlas, NaN where the converted alpha stays; None when there is none, until planned, and
        under LIGHTS_ALPHA "raw" (Forza's alpha as is)."""
        if LIGHTS_ALPHA != "matched" or self.lamp_raw is None:
            return None
        if getattr(self, "_glow_done", False):
            return self._lamp_glow
        if self.atlases is None or not self.regions:
            return None
        from dsfforza.convert import lenses

        view = self._lamp_view(front=True, grid=True)
        self._lamp_glow = None if view is None else lenses.day_glow(self.lamp_raw, view)
        self._glow_done = True
        return self._lamp_glow

    def _convert_texel(self, family: str, r: int, g: int, b: int, a: int) -> tuple[int, int, int, int]:
        """Forza texel -> DSF conventions (alpha = specular / glow mask)."""
        floor = self.lift.get(fm.base_family(family))
        if floor:
            r, g, b = (int(floor + c * (255 - floor) / 255) for c in (r, g, b))
        tone = self.tone.get(fm.base_family(family))
        if tone and not _paint_through(family, r, g, b, a):
            k, sat = tone
            grey = (r + g + b) / 3
            r, g, b = (max(0, min(255, int((grey + (c - grey) * sat) * k))) for c in (r, g, b))
        if family == fm.LIGHTS:
            if LIGHTS_ALPHA == "raw":  # analysis only: Forza's alpha as is
                return r, g, b, a
            return matched_lamp_texel(
                r, g, b, a, self._red_lamp_curve(), self._red_lens_scale(), self._white_lens_scale()
            )
        if _paint_through(family, r, g, b, a):
            return self.plain  # paint-through body texel
        lum = (r + g + b) / 3
        return r, g, b, max(0, min(255, int((lum - CHASSIS_ALPHA_LUM) * CHASSIS_ALPHA_GAIN)))

    def render(self) -> bytearray:
        """The atlas as RGBA8 (plain paint colour where nothing is placed). Each
        region texel is the box average of the source texels under it, converted
        (_convert_texel); the sums come from a summed-area table and each distinct
        average is converted once."""
        width, height = self.size
        inside = all(
            r.dst[0] >= 0 and r.dst[1] >= 0 and r.dst[0] + r.size[0] <= width and r.dst[1] + r.size[1] <= height
            for family in self.families
            for r in self.regions[family]
        ) and all(
            x >= 0 and y >= 0 and x + SOLID_CELL <= width and y + SOLID_CELL <= height for x, y in self.solids.values()
        )
        if not inside:  # a region past the atlas edge: the texel loop (it wraps into the next row)
            return self._render_texels()
        out = np.empty((height, width, 4), dtype=np.uint8)
        out[:, :] = self.plain
        for family in self.families:
            sw, sh, src = self.atlases.get(family)
            s = self.scale[family]
            if not self.regions[family]:
                continue
            texels_in = np.frombuffer(src, dtype=np.uint8).reshape(sh, sw, 4)
            converted: dict[int, tuple] = {}  # packed RGBA average -> texel
            for n_region, r in enumerate(self.regions[family]):
                if r.size[0] <= 0 or r.size[1] <= 0:
                    continue
                cut = self.cutout_colour is not None and (family, n_region) in self.alpha_only
                rows, cols = [], []
                for dy in range(r.size[1]):
                    fy0 = r.src[1] + (dy - PAD) / s
                    y0 = max(0, min(sh - 1, int(math.floor(fy0))))
                    rows.append((y0, max(y0 + 1, min(sh, int(math.floor(fy0 + 1 / s))))))
                for dx in range(r.size[0]):
                    fx0 = r.src[0] + (dx - PAD) / s
                    x0 = max(0, min(sw - 1, int(math.floor(fx0))))
                    cols.append((x0, max(x0 + 1, min(sw, int(math.floor(fx0 + 1 / s))))))
                (y0, y1), (x0, x1) = np.array(rows).T, np.array(cols).T
                # The summed-area table of the source texels under this region only:
                # the same integer box sums as a table of the whole atlas.
                top, left = int(y0.min()), int(x0.min())
                table = np.zeros((int(y1.max()) - top + 1, int(x1.max()) - left + 1, 4), dtype=np.int64)
                table[1:, 1:] = texels_in[top : int(y1.max()), left : int(x1.max())].cumsum(0).cumsum(1)
                y0, y1, x0, x1 = y0[:, None] - top, y1[:, None] - top, x0[None, :] - left, x1[None, :] - left
                acc = table[y1, x1] - table[y0, x1] - table[y1, x0] + table[y0, x0]  # (h, w, 4)
                n = ((y1 - y0) * (x1 - x0))[:, :, None]
                mean = acc // n
                packed = mean[:, :, 0] << 24 | mean[:, :, 1] << 16 | mean[:, :, 2] << 8 | mean[:, :, 3]
                keys, index = np.unique(packed.reshape(-1), return_inverse=True)
                texels = []
                for key in keys.tolist():
                    if key not in converted:
                        converted[key] = tuple(
                            self._convert_texel(family, key >> 24, key >> 16 & 255, key >> 8 & 255, key & 255)
                        )
                    texels.append(converted[key])
                block = np.array(texels, dtype=np.uint8)[index.reshape(-1)].reshape(mean.shape)
                if family == fm.LIGHTS and self._glow() is not None:
                    # Clear front lens texels: their glow (lens_glow) over the converted alpha,
                    # averaged over the source texels under each DSF texel like the colour.
                    g = self._glow()[top : top + table.shape[0] - 1, left : left + table.shape[1] - 1]
                    lit = np.isfinite(g)
                    if lit.any():
                        sums = np.zeros(table.shape[:2] + (2,))
                        sums[1:, 1:] = np.stack([np.where(lit, g, 0.0), lit], -1).cumsum(0).cumsum(1)
                        box = sums[y1, x1] - sums[y0, x1] - sums[y1, x0] + sums[y0, x0]
                        k = box[:, :, 1] > 0
                        a = (box[:, :, 0] + block[:, :, 3] * (n[:, :, 0] - box[:, :, 1])) / n[:, :, 0]
                        block[:, :, 3][k] = np.clip(np.rint(a[k]), 0, 255).astype(np.uint8)
                if cut:
                    block[mean[:, :, 3] < 128] = self.cutout_colour
                out[r.dst[1] : r.dst[1] + r.size[1], r.dst[0] : r.dst[0] + r.size[0]] = block
        for key, (x0, y0) in self.solids.items():
            out[y0 : y0 + SOLID_CELL, x0 : x0 + SOLID_CELL] = self.solid_colours[key]
        return bytearray(out.tobytes())

    def _render_texels(self) -> bytearray:
        """render() texel by texel (for layouts past the atlas edge)."""
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
                            row = src[o : o + (x1 - x0) * 4]
                            for c in range(4):
                                acc[c] += sum(row[c::4])
                        n = (y1 - y0) * (x1 - x0)
                        texel = self._convert_texel(family, *(v // n for v in acc))
                        if family == fm.LIGHTS and self._glow() is not None:
                            g = self._glow()[y0:y1, x0:x1]
                            lit = np.isfinite(g)
                            if lit.any():
                                a = (g[lit].sum() + texel[3] * (n - lit.sum())) / n
                                texel = (*texel[:3], max(0, min(255, int(round(a)))))
                        if cut and acc[3] // n < 128:
                            texel = self.cutout_colour
                        o = (y * width + x) * 4
                        out[o : o + 4] = bytes(texel)
        for key, (x0, y0) in self.solids.items():
            colour = bytes(self.solid_colours[key])
            for y in range(y0, y0 + SOLID_CELL):
                o = (y * width + x0) * 4
                out[o : o + SOLID_CELL * 4] = colour * SOLID_CELL
        return out

    def dds(self, template: bytes | None = None) -> bytes:
        return dds_file(self.render(), *self.size, self.mips, template, self.fourcc)

    def summary(self) -> str:
        parts = []
        for family in self.families:
            parts.append(f"{family}: {len(self.regions[family])} regions at {self.scale[family]:.3f}x")
        paint = {t: k for t, k in self.wheel_paint.items() if k != 1.0}
        if paint:
            parts.append("rim PaintScale " + ", ".join(f"{t} {round(k, 3):g}" for t, k in sorted(paint.items())))
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


def set_material_params(stream, maco: Entry, values: dict[str, float]) -> int:
    """Set, in place, the float parameters named in `values` in every pass (MARC) of a
    MACO that has them (type-1 MARPs; the other parameters and passes are left alone).
    Returns how many MARPs changed."""
    changed = 0
    for marc in maco.children:
        if marc.tag != "MARC":
            continue
        for p in marc.children:
            if p.tag != "MARP":
                continue
            data = current(stream, p)
            param = parse_marp(data)
            if param.type == 1 and param.name in values:
                p.payload = data[:32] + struct.pack("<f", values[param.name]) + data[36:]
                changed += 1
    return changed


def material_params(stream, maco: Entry) -> dict[str, float]:
    """The float (type 1) parameters of a MACO's first pass that has each."""
    out: dict[str, float] = {}
    for marc in maco.children:
        if marc.tag != "MARC":
            continue
        for p in marc.children:
            if p.tag == "MARP":
                param = parse_marp(current(stream, p))
                if param.type == 1:
                    out.setdefault(param.name, struct.unpack_from("<f", param.value)[0])
    return out


def gloss_uid(prefix: bytes, n: int) -> bytes:
    """UID of gloss material n: forza2dsf.Builder.new_uid's vanilla-looking layout (byte 1
    0x12, byte 7 0x2D with its top bit clear) on its own 4-byte prefix."""
    n += 1
    return bytes([(n << 4) & 0xFF, 0x12, n >> 4, prefix[0], prefix[1], prefix[2], prefix[3] & 0x7F, 0x2D])


def add_gloss(stream, drsp: Entry, lights: Entry, gloss: list, prefix: bytes, textures=None) -> dict[str, bytes]:
    """Add one copy of the Lights MACO `lights` per GlossMaterial to `drsp` (after its
    last TEXC/MACO), each with its parameters and a UID from `prefix` (gloss_uid);
    `textures` remaps their texture UIDs (retarget_material). Returns {kind: UID}."""
    kids = list(drsp.children)
    at = max([i for i, e in enumerate(kids) if e.tag in ("TEXC", "MACO")], default=-1) + 1
    out = {}
    for n, mat in enumerate(gloss):
        uid = gloss_uid(prefix, n)
        e = retarget_material(stream, lights, textures or {}, uid)
        set_material_params(stream, e, mat.params())
        kids.insert(at + n, e)
        out[f"{GLOSS_PREFIX}{n}"] = uid
    drsp.children = kids
    return out


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

    materials: dict[str, bytes]  # "chassis"/"lights" -> material UID to use in SBBH
    texture: bytes  # TEXC UID the materials sample
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


def _with_gloss(installed: Installed, gloss: dict[str, bytes]) -> Installed:
    installed.materials.update(gloss)
    installed.layout.update(dict.fromkeys(gloss.values(), STRIDES[LIGHTS_MAT]))
    return installed


def install_full(
    stream,
    drsp: Entry,
    dds: bytes,
    mode: str = "replace",
    uid_seed: bytes = b"\x00\x12\xfe\xed",
    gloss: list | None = None,
    gloss_prefix: bytes = b"\x00\x00\x5a\x3c",
) -> Installed:
    """Install the atlas in a full (kind 0) entry's DRSP, in place.

    replace: the host TEXC that the Chassis/Lights materials sample gets the
             new DDS; every UID stays (exterior/interior references and the
             paint overlay keep working). The entry's size grows by the
             difference in DDS size only.
    clone:   a new TEXC (new UID) and copies of the Chassis and Lights
             materials (new UIDs) pointing at it are added; the host's stay.
    gloss:   GlossMaterials (Texturing.gloss_materials): copies of the Lights
             material, UIDs from `gloss_prefix` (add_gloss), kinds "gloss<n>".
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
        installed = Installed(
            {k: v[1]["uid"] for k, v in found.items()}, host_tex, {v[1]["uid"]: STRIDES[k] for k, v in found.items()}
        )
        return _with_gloss(installed, add_gloss(stream, drsp, found[LIGHTS_MAT][0], gloss or [], gloss_prefix))
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
    installed = Installed(new_mats, new_tex, {new_mats[k]: STRIDES[k] for k in new_mats})
    textures = {host_tex: new_tex}
    return _with_gloss(installed, add_gloss(stream, drsp, found[LIGHTS_MAT][0], gloss or [], gloss_prefix, textures))


def install_exterior(
    stream,
    drsp: Entry,
    full: Installed,
    mode: str = "replace",
    uid_seed: bytes = b"\x00\x12\xfe\xee",
    fallback: list[Entry] | None = None,
    gloss: list | None = None,
    gloss_prefix: bytes = b"\x00\x00\x5a\xc3",
) -> Installed:
    """Use the clean exterior (kind 2) entry's own Chassis/Lights materials;
    they sample the full entry's TEXC by UID. In clone mode, copies pointing
    at the new texture are added.

    Most vanilla vehicles define one or both of their exterior's _LOD1
    materials in the interior or the full entry instead (the Desanne and 370Z
    keep both in the exterior); the `fallback` DRSPs (interior, full) are
    searched for the missing *_LOD1 ones, which the exterior then uses by
    UID, as vanilla exteriors do."""
    found = host_materials(stream, drsp)
    for other in fallback or []:
        for kind, value in host_materials(stream, other, lod1=True).items():
            found.setdefault(kind, value)
    if set(found) != {CHASSIS, LIGHTS_MAT}:
        raise ValueError(f"exterior entry needs a Chassis and a Lights material, found {sorted(found)}")
    lights_tex = next(iter(found[LIGHTS_MAT][1]["textures"]), None)
    remap = {lights_tex: full.texture} if lights_tex is not None and mode != "replace" else {}
    gloss_uids = add_gloss(stream, drsp, found[LIGHTS_MAT][0], gloss or [], gloss_prefix, remap)
    if mode == "replace":
        installed = Installed(
            {k: v[1]["uid"] for k, v in found.items()},
            full.texture,
            {v[1]["uid"]: STRIDES[k] for k, v in found.items()},
        )
        return _with_gloss(installed, gloss_uids)
    new_mats = {CHASSIS: uid_seed + b"\x00\x00\x00\x02", LIGHTS_MAT: uid_seed + b"\x00\x00\x00\x03"}
    kids = list(drsp.children)
    last_mat = max(i for i, e in enumerate(kids) if e.tag == "MACO")
    for n, kind in enumerate((CHASSIS, LIGHTS_MAT)):
        old_tex = next(iter(found[kind][1]["textures"]))
        kids.insert(
            last_mat + 1 + n, retarget_material(stream, found[kind][0], {old_tex: full.texture}, new_mats[kind])
        )
    drsp.children = kids
    return _with_gloss(Installed(new_mats, full.texture, {new_mats[k]: STRIDES[k] for k in new_mats}), gloss_uids)


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
        if is_atlas_kind(kind):
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


def prepare(
    stream,
    full_vehc: Entry,
    exterior_vehc: Entry | None,
    car_dir: Path,
    sections,
    lods,
    mode: str = "replace",
    size: tuple[int, int] | None = None,
    mips: int | None = None,
    swatch_trims: bool = False,
    log=print,
    interior_vehc: Entry | None = None,
    wheels=None,
    uid_seed: bytes | None = None,
) -> tuple[Binding, Binding | None]:
    """Plan and encode the car's atlas and install it in a host vehicle's full
    entry (and its clean exterior entry). Call before rebuilding the DRSPs,
    and keep the entries' TEXC/MACO (forza2dsf.rebuild_drsp with
    keep_host_materials=True). Atlas size and mip count default to the host
    chassis texture's, so replace mode leaves the entry size unchanged.
    wheels: (wheel sections, LODs), see Texturing.plan."""
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
    t.plan(sections, set(lods), wheels=wheels)
    if t.lens_report:
        log(
            "lenses baked over their lamps: "
            + ", ".join(
                f"{k} {v['texels']} texels ({v['behind']:.0%} with something behind)"
                for k, v in sorted(t.lens_report.items())
            )
        )
    if wheels is not None and fm.WHEEL not in t.families:
        log(f"no {fm.FAMILY_FILES[fm.WHEEL][0]} in {car_dir}: rims keep flat swatches")
    dds = t.dds(template=host)
    # The gloss materials' UIDs: the car's own (uid_seed, forza2dsf.Builder's), apart for each entry.
    seed = uid_seed or b"\x00\x00\x00\x00"
    full = install_full(
        stream, full_drsp, dds, mode, gloss=t.gloss_materials, gloss_prefix=seed[:3] + bytes([seed[3] ^ 0x3C])
    )
    if t.gloss_materials:
        log(
            "gloss materials (Lights copies; intensity/power/reflection/fresnel power/bias): "
            + "; ".join(
                f"{n}: {m.intensity:g}/{m.power:g}/{m.reflection:g}/{m.fresnel_power:g}/{m.bias:g}"
                for n, m in enumerate(t.gloss_materials)
            )
            + " for "
            + ", ".join(f"{k} {v}" for k, v in sorted(t.gloss_report.items()))
        )
    ext = None
    if exterior_vehc is not None:
        fallback = ([child(child(interior_vehc, "MDXN"), "DRSP")] if interior_vehc is not None else []) + [full_drsp]
        ext = install_exterior(
            stream,
            child(child(exterior_vehc, "MDXN"), "DRSP"),
            full,
            mode,
            fallback=fallback,
            gloss=t.gloss_materials,
            gloss_prefix=seed[:3] + bytes([seed[3] ^ 0xC3]),
        )
    log(
        f"{t.summary()}; {mode}: texture {full.texture.hex()}, DDS {len(dds):#x} bytes "
        f"(host {len(host) if host else 0:#x})"
    )
    return Binding(t, full), (Binding(t, ext) if ext else None)


# --- textured preview ---------------------------------------------------------------

FLAT = {  # shared materials (forza2dsf constants), preview colours
    "001283358afd1027": (150, 24, 28),  # paint
    "001283369afd1027": (90, 110, 130),  # glass
    "0012833b12fe1027": (40, 40, 44),  # trim
    "00128337b2fd1027": (22, 22, 22),  # tyre
}


VIEWS = {"side": (2, 1, 0, 1, 1), "top": (0, 2, 1, -1, 1), "front": (0, 1, 2, 1, 1), "rear": (0, 1, 2, -1, -1)}


def preview(
    archive: Path,
    vehc_index: int,
    png: Path,
    mask: int | None = None,
    size: int = 400,
    views: tuple[str, ...] = ("side", "top", "front", "rear"),
) -> dict:
    """Render side, top, front and rear views of one VEHC with its local
    materials textured (nearest texel of the top mip; textures are looked up
    by UID across the whole archive) and shared materials flat-coloured."""
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.chnk import read_chunk

    _, mesh = vm.load(archive, vehc_index)
    textures, materials = {}, {}
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        wanted = {vehc_index} | {
            i
            for i in range(max(0, vehc_index - 2), min(len(root.entries), vehc_index + 3))
            if root.entries[i].tag == "VEHC"
        }
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
    from dsfforza.formats import vehicle_mesh as vm

    xs = [p[0] for t in tris for p in t[0]]
    ys = [p[1] for t in tris for p in t[0]]
    zs = [p[2] for t in tris for p in t[0]]
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
            pts = [
                (
                    flip * (p[h] - centre[h]) / span * size + size / 2,
                    size / 2 - (p[v] - centre[v]) / span * size,
                    sign * (p[d] - centre[d]),
                )
                for p in pts3
            ]
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
                        colour = trgba[o : o + 3]
                    else:
                        colour = flat
                    o = (y * width + view * size + x) * 3
                    pixels[o : o + 3] = bytes(min(255, int(c * shade)) for c in colour)
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
        from dsfforza.formats import carbin

        sections = carbin.parse((args.car / (args.carbin or f"{args.car.name}.carbin")).read_bytes())
        w, h = (int(x) for x in args.size.split("x"))
        t = Texturing(args.car, size=(w, h), mips=args.mips, swatch_trims=args.swatch_trims)
        t.plan(sections, {int(x) for x in args.lods.split(",")})
        print(t.summary())
        rgba = t.render()
        if args.png:
            from dsfforza.formats import xds

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
