"""Resolve what each Forza Horizon 1 car subsection samples: role, texture, UVs.

A carbin subsection's name is the name of its shader technique in
media/shaders/Cars/shaders_v16.fxobj (257 techniques; custom ones in
CustomCarShaders.xml). The carbin stores no texture binding: every car
technique samples one texture, `baseSampler`, which the game binds to one of
the car's two atlases depending on the technique (`damageSampler` and
`DamageLightsSampler` carry the damaged variants):

    nodamage.xds (+ damage.xds)        interior, badges, emblems, the colour
                                       swatches of procedural trims, and the
                                       body area (white, alpha 0: paint shows)
    Lights.xds   (+ damagelights.xds)  every lamp technique, lamp glass,
                                       reflectors; alpha is the glow mask

The main carbin's `wheel` section is the exception: its rim techniques sample
the car's own wheel.xds from the wheel archive media/wheels/<car>
(classify_wheel, family WHEEL).

lod0/cockpit carbins use the _LOD0 versions (nodamage_LOD0, lights_LOD0). The
cockpit carbin's cabin sections draw with the cockpit_<technique> shaders,
which sample interior_LOD0 (CockpitInteriorHiSampler) for every trim, dial and
label; only its outside sections (A-pillars, bonnet, mirrors) use nodamage_LOD0
(classify_cockpit; research/cockpit.md). Verified by drawing each technique's
UV triangles over the atlases (research/forza-materials.md).

UVs: atlas uv = raw * scale + offset with the subsection's m_UVOffsetScale
(x_off, x_scale, y_off, y_scale); no Y flip. Three kinds of transform:

    textured   footprint covers real atlas content
    swatch     footprint is a few texels wide: the technique takes one flat
               colour from the atlas (black, frame, bottom, plastic2, fogred...)
    tiling     scale far above 1 (tire 31, carbon_fiber 246): a global tiling
               texture of the shader (treadSampler, CarbonFiberSampler), not
               the car atlas; scale ~0 is the same (procedural)

    uv run dsff forza_materials out/fh1/NIS_370Z_10 [--lod 1] [--colours]
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dsfforza.formats import carbin

# Roles, matching the DSF side's shared materials plus "textured".
PAINT, GLASS, TRIM, TYRE, TEXTURED, SKIP = "paint", "glass", "trim", "tyre", "textured", "skip"
# Exterior chrome (bumpers, window frames, drip rails...): DSF's shared chrome
# materials, as on stock cars (research/forza-materials.md, "Chrome trim").
CHROME = "chrome"
# Texture families (the atlas the technique's baseSampler is bound to).
NODAMAGE, LIGHTS = "nodamage", "lights"
# Cockpit carbins only: dial faces, needles, switch labels (interior_LOD0.xds;
# its alpha is the label cut-out mask).
INTERIOR = "interior"
# The wheel section's rim techniques (rim, chrome_rim, inner_rim, wheel_black,
# wheel_emblem...): the car's own wheel texture, wheel.xds in the wheel archive
# media/wheels/<car> (not in the car's zip; port_car.extract_wheel puts it in
# the car folder). It holds the spoke shading, centre cap logos and nuts; its
# alpha is a gloss mask on most cars, not a cut-out (classify_wheel).
WHEEL = "wheel"

FAMILY_FILES = {
    # family: (main-carbin file, lod0/cockpit file, damaged pair)
    NODAMAGE: ("nodamage.xds", "nodamage_LOD0.xds", "damage.xds"),
    LIGHTS: ("Lights.xds", "lights_LOD0.xds", "damagelights.xds"),
    INTERIOR: ("interior_LOD0.xds", "interior_LOD0.xds", "interior_LOD0.xds"),
    WHEEL: ("wheel.xds", "wheel_LOD0.xds", "wheel.xds"),
}

# Exact technique names -> (role, family). Family is the atlas baseSampler
# reads, even for roles the DSF side renders untextured.
TECHNIQUES: dict[str, tuple[str, str | None]] = {}


def _add(role: str, family: str | None, names: str) -> None:
    for name in names.split():
        TECHNIQUES[name] = (role, family)


_add(PAINT, NODAMAGE, "body body_2 body_3 body_traffic cockpit_body headlight_paint numberplate_body")
_add(PAINT, LIGHTS, "lights_body")
_add(
    GLASS,
    NODAMAGE,
    "window window_2 window_traffic window_traffic_dark frontwindow frontwindow_2 "
    "inside_window cockpit_glass glass_2 red_glass",
)
# `glass` on FH1 cars is the clear headlight cover; its UVs land on the Lights atlas.
_add(GLASS, LIGHTS, "glass lights_glass lights_gls_noemit")
_add(
    TRIM,
    NODAMAGE,
    "black frame bottom bottom_2 undercarriage misc engine cage rubber_trim rubber_trim_2 "
    "weatherstrip plastic plastic2 dullplastic embossed_plastic embossed_plastic2 mottled "
    "cloth cloth2 smooth_metal rubber matte matte_colors matte_colors2 bumper_frame "
    "black_glass black_glass_breakable race_kit_red "
    "carbon_fiber carbon_fiber2 carbon_fiber3 carbon_fiber_aniso grille1 grille1_alpha "
    "grille2 grille2_alpha defrost_lines mirrorLeft mirrorMiddle mirrorRight "
    "caliper rotor hub numberplate numplate scaling_text",
)
# Forza's chrome techniques (ChromeDiffuseScale 0.1, CubeScale 2 in the cars'
# ShaderSettings.xml: mostly cube-map reflection). As trim they drew in DSF's
# flat dark trim colour: the Mustang Boss 429's bumpers and window frames came
# out black (user report on out/testdrive11).
_add(CHROME, NODAMAGE, "chrome chrome_2 chrome_3 chrome_bump")
_add(CHROME, LIGHTS, "lights_chrome")
_add(
    TEXTURED,
    NODAMAGE,
    "interior interior_banner badge badge2 badge_chrome stripe_badge "
    "cockpit_badge emblem brake_badge gauge_red gauge_needle gauge_emissive "
    "gauge_emissive_unshadowed",
)
_add(
    TEXTURED,
    LIGHTS,
    "head_light head_light2 tail_light tail_light_lod0 tail_light_on tail_light2 "
    "reverse_light reverse_light_lod0 white_brake textured_reflector reflector xenonhead "
    "hidhead oldhead fullbeam fogwhite fogred taillightst taillight2S brkreverse linred "
    "rinred indicator_left indicator_right drlwhite drlwhiteswitch drlred drlorange "
    "drlbrakered drlbrakeorange drlinlorange drlinrorange drlinlred drlinrred drlinlwhite "
    "drlinrwhite brinl brinr lindtailside rindtailside slorange slinlorange slinrorange "
    "detail_glass_red detail_glass_clear detail_glass_amber lights_gls_tail_light "
    "lights_gls_reverse_light lights_gls_indicator_left lights_gls_indicator_right "
    "lights_gls_hidhead lights_gls_oldhead lights_gls_taillightst lights_gls_taillight2S",
)
# Synthetic plate sections (src/dsfforza/convert/plates.py): paint material, UVs on the
# shared atlas's plate (not a Forza technique).
_add(PAINT, None, "dsf_plate dsf_inner")  # dsf_inner: damage.split_exposed (panel undersides)
# Trim triangles on light artwork (trim_art.split_trim_art: the Alfa GTA's quadrifoglio
# badges are `misc` triangles on white and green texels): drawn through the atlas.
_add(TEXTURED, NODAMAGE, "dsf_trim_art")
_add(TEXTURED, LIGHTS, "dsf_trim_art_lights")
_add(CHROME, NODAMAGE, "dsf_trim_metal")  # bare-metal artwork (trim_art.is_metal): shared chrome
# Dark trims with detail (the Cougar Eliminator's grille bars) through the atlas, flat
# mid-grey ones as one colour cell (trim_art.classify_texels).
_add(TEXTURED, NODAMAGE, "dsf_trim_detail dsf_trim_grey")
# Flat dark trims with Forza's specular or reflection: a colour cell on a gloss material
# (trim_art.GLOSS_TECHNIQUE, dsf_materials.GLOSS_PREFIX).
_add(TEXTURED, NODAMAGE, "dsf_trim_gloss")
# trim_art names its subsections "<technique>@<scale>" when the Forza technique scales
# its texels (texel_scale): Texturing samples them from the scaled atlas.
SCALE_SUFFIX = "@"


# ... and "<technique>~<source>" with the Forza technique the triangles came from, whose
# gloss (gloss_params) they keep: "dsf_trim_detail@0.2~misc".
SOURCE_SUFFIX = "~"


def technique_scale(name: str) -> tuple[str, float]:
    """("dsf_trim_detail", 0.2) for "dsf_trim_detail@0.2" (and "dsf_trim_detail@0.2~misc");
    (name, 1.0) without a scale."""
    if name.startswith("dsf_") and SOURCE_SUFFIX in name:
        name = name.partition(SOURCE_SUFFIX)[0]
    if SCALE_SUFFIX in name:
        base, _, scale = name.partition(SCALE_SUFFIX)
        try:
            return base, float(scale)
        except ValueError:
            pass
    return name, 1.0


def with_scale(technique: str, scale: float) -> str:
    return technique if scale == 1.0 else f"{technique}{SCALE_SUFFIX}{scale:.4g}"


def with_source(technique: str, source: str) -> str:
    return f"{technique}{SOURCE_SUFFIX}{source}"


def technique_source(name: str) -> str:
    """The Forza technique a subsection's triangles came from: "misc" for
    "dsf_trim_detail@0.2~misc", the name itself for Forza's own techniques."""
    if name.startswith("dsf_") and SOURCE_SUFFIX in name:
        return name.partition(SOURCE_SUFFIX)[2]
    return name


# Virtual dials drawn on blank digital displays, display faces or a virtual display
# (cockpit.virtual_gauges): flat colour cells of the interior atlas, no Forza texture
# behind them.
_add(TRIM, None, "dsf_dial_mark dsf_dial_mark_dark dsf_dial_red dsf_dial_display dsf_dial_bezel")
_add(TRIM, None, "dsf_needle dsf_needle_hub")
# Livery decal slots: their raw UV0 spans the whole unit square, i.e. a texture of
# their own (the player's livery), squeezed by m_UVOffsetScale onto an arbitrary patch
# of the car atlases. The KOE_Agera_11's rear-wing pair lands on a 3-texel column at the
# seat artwork's edge in nodamage (blue and white, half of it opaque), its cockpit
# carbin's dash-top strip on interior_LOD0's yellow and blue stitching bands; drawn from
# the atlas they showed as bright yellow/blue streaks on the dashboard and blue patches
# behind the windscreen (user report on out/testdrive9). No livery: nothing to draw.
_add(SKIP, None, "liveryBadge")
_add(
    TYRE,
    None,
    "tire tire_V2 wheel_black tread sidewall rim rim_V2 chrome_rim chrome_rim_V2 outer_rim "
    "inner_rim inner_rim_V2 wheel_emblem wheel_emblem_V2 rim_bump_metal rim_bump_metal2",
)


# Techniques that cut out texels with alpha below one half (badge plates are
# drawn entirely from alpha-0 texels: only the emblem's letters show).
ALPHA_TESTED = {
    "emblem",
    "badge",
    "badge2",
    "badge_chrome",
    "stripe_badge",
    "liveryBadge",
    "cockpit_badge",
    "brake_badge",
    "wheel_emblem",
    "wheel_emblem_V2",
    "grille1_alpha",
    "grille2_alpha",
    "z_gauge_emissive_alpha",
}


def triangle_max_alpha(atlas: tuple[int, int, bytearray], uvs) -> int:
    """Largest alpha among the atlas texels under a UV triangle's bounding box."""
    width, height, rgba = atlas
    xs = [min(width - 1, max(0, int(u * width))) for u, _ in uvs]
    ys = [min(height - 1, max(0, int(v * height))) for _, v in uvs]
    best = 0
    for y in range(min(ys), max(ys) + 1):
        row = rgba[(y * width + min(xs)) * 4 + 3 : (y * width + max(xs) + 1) * 4 : 4]
        best = max(best, *row)
    return best


# An alpha-tested triangle stays when at least this share of the texels inside it
# (and at least one) pass Forza's test (alpha >= 128). Emblem and label triangles
# of the 40 testdrive cars keep 20 to 100% of theirs; the F-150 Raptor's
# stripe_badge mud-splash triangles, which its nodamage atlas hides (alpha 0
# under all of them; the graphic is in nodamagestripe.xds, the texture of every
# other factory colour), have at most 1 stray texel in 280.
ALPHA_KEEP_SHARE = 0.01


def triangle_alpha_cut(atlas: tuple[int, int, bytearray], uvs) -> bool:
    """True if Forza's alpha test (texel alpha < 128 discarded) removes the
    whole UV triangle: fewer than ALPHA_KEEP_SHARE of the texels whose centres
    lie inside it pass. A triangle too small to hold a texel centre is judged
    by the texels under its corners and centroid. (Testing the bounding box, as
    triangle_max_alpha does, kept long thin decal triangles next to opaque
    artwork: the Raptor's hidden stripe came out as black patches.)"""
    import numpy as np

    width, height, rgba = atlas
    alpha = np.frombuffer(rgba, dtype=np.uint8)[3::4].reshape(height, width)
    xs = [u * width for u, _ in uvs]
    ys = [v * height for _, v in uvs]
    x0, x1 = max(0, int(math.floor(min(xs)))), min(width, int(math.ceil(max(xs))) + 1)
    y0, y1 = max(0, int(math.floor(min(ys)))), min(height, int(math.ceil(max(ys))) + 1)
    inside = None
    area = (xs[1] - xs[0]) * (ys[2] - ys[0]) - (ys[1] - ys[0]) * (xs[2] - xs[0])
    if x0 < x1 and y0 < y1 and abs(area) > 1e-12:
        gy, gx = np.mgrid[y0:y1, x0:x1] + 0.5
        w0 = ((xs[1] - gx) * (ys[2] - gy) - (ys[1] - gy) * (xs[2] - gx)) / area
        w1 = ((xs[2] - gx) * (ys[0] - gy) - (ys[2] - gy) * (xs[0] - gx)) / area
        mask = (w0 >= 0) & (w1 >= 0) & (w0 + w1 <= 1)
        if mask.any():
            inside = alpha[y0:y1, x0:x1][mask]
    if inside is None:
        points = list(zip(xs, ys)) + [(sum(xs) / 3, sum(ys) / 3)]
        inside = np.array([alpha[min(height - 1, max(0, int(y))), min(width - 1, max(0, int(x)))] for x, y in points])
    passed = int((inside >= 128).sum())
    return passed < max(1, ALPHA_KEEP_SHARE * len(inside))


def classify(name: str) -> tuple[str, str | None]:
    """(role, texture family) for a technique name. Exact names first, then
    the prefix conventions of the technique list and CustomCarShaders.xml."""
    if name in TECHNIQUES:
        return TECHNIQUES[name]
    if name.startswith("dsf_") and (SCALE_SUFFIX in name or SOURCE_SUFFIX in name):  # trim_art's names
        return classify(technique_scale(name)[0])
    n = name.lower()
    if n.startswith(("blur", "chrome_blur", "ghost")) or "shadow" in n:
        return SKIP, None
    if n.startswith("body") or n.endswith(("_paint", "paint")):
        return PAINT, NODAMAGE
    if n.startswith(("lights_gls", "detail_glass", "drl", "tail", "head", "indicator", "fog", "reverse")):
        return TEXTURED, LIGHTS
    if n.startswith("lights_"):
        return TRIM, LIGHTS
    if "window" in n or n.endswith("glass"):
        return GLASS, NODAMAGE
    if n.startswith(("rim", "tire", "wheel")) or n in ("tyre",):
        return TYRE, None
    if n.startswith(("cockpit_", "gauge")) or "badge" in n or "emblem" in n:
        return TEXTURED, NODAMAGE
    if n.startswith("chrome"):
        return CHROME, NODAMAGE
    return TRIM, NODAMAGE


# Cockpit-carbin sections that are parts of the car's outside seen through
# the windows (A-pillars, bonnet variants, side mirrors, glass, headlamps).
# They use the exterior techniques and nodamage_LOD0; every other section
# (interior, seatL/R, steering_wheel, needles, cagerace) is the cabin.
COCKPIT_EXTERIOR_SECTIONS = re.compile(r"^(a_pillar|hood|mirror|glass|headlight|taillight)", re.I)


def classify_cockpit(name: str, section: str | None = None) -> tuple[str, str | None]:
    """classify() for <car>_cockpit.carbin subsections, by drawing each
    technique's UV triangles over every candidate texture
    (research/cockpit.md, "Cabin trims sample interior_LOD0"):

    - cabin sections (`section` not in COCKPIT_EXTERIOR_SECTIONS): every
      technique there has a `cockpit_<name>` variant in shaders_v16 (plastic,
      leather, metal, bump_*, headliner, reflector, gauge*, badge...), and
      those sample CockpitInteriorHiSampler = interior_LOD0: dial faces,
      labels, and the cabin's leather, plastic and metal artwork and colour
      swatches. Their UV islands land on matching artwork there (M5 dial
      bezels and pedal, 370Z seat leather band, Range Rover clock face) and
      on unrelated nodamage_LOD0 content (white filler, a brake disc, the
      Mustang's red badge swatch);
    - exterior sections (A-pillars, bonnets, side mirrors) keep the exterior
      techniques on nodamage_LOD0 (chrome window trim on its chrome swatch);
    - gauge faces, needles and labels sample interior_LOD0 in any section;
      badges in the cabin too, but badges on the outside sections (the
      Firebird's bonnet decal, the M5 E28's mirror badge, the SVT Raptor's
      F-150 lettering, the Agera's bonnet livery) land on their artwork in
      nodamage_LOD0 and on unrelated interior_LOD0 content, like the
      exterior's badges (research/cockpit.md, "Badges seen through the
      windscreen");
    - mirror glass (mirrorLeft/Middle/Right) is a render target in Forza:
      flat trim.
    Without `section` every non-gauge trim maps to nodamage_LOD0 (the old
    behaviour; callers that only need the role may omit it). Chrome counts as
    trim here: the interior entry draws it from the atlas like the other trims."""
    n = name.lower()
    if TECHNIQUES.get(name, (None,))[0] == SKIP:
        return SKIP, None
    outside = section is not None and bool(COCKPIT_EXTERIOR_SECTIONS.match(section))
    if n.startswith(("gauge", "z_gauge", "digital_gauge")) or ("badge" in n and not outside):
        return TEXTURED, INTERIOR
    if n.startswith("mirror"):
        return TRIM, None
    role, family = classify(name)
    if role == CHROME:
        role = TRIM
    if section is not None and not COCKPIT_EXTERIOR_SECTIONS.match(section):
        if role in (TRIM, TEXTURED) and family is not None:
            return TEXTURED, INTERIOR
        return role, family
    if role == TRIM and family is not None:
        return TEXTURED, family
    return role, family


classify_cockpit.section_aware = True


def classify_wheel(name: str) -> tuple[str, str | None]:
    """classify() for the main carbin's `wheel` section (research/wheels.md):
    every rim technique (rim, chrome_rim, outer_rim, inner_rim, wheel_black,
    wheel_emblem...) samples the car's wheel texture, wheel.xds; the tyre takes
    DSF's shared tyre (forza2dsf.TyreMap unwraps it onto the stock tread and
    sidewall); the spinning-blur discs, shadows and ghosts are left out.
    Drawing the 40 testdrive cars' rim UV triangles over wheel.xds lands them on
    matching artwork (spoke shading, the Nissan, Audi, Bugatti and SVT caps,
    nuts); inner_rim is a swatch of it on some cars (250 GTO, S5S)."""
    n = name.lower()
    if "blur" in n or "shadow" in n or n.startswith("ghost"):
        return SKIP, None
    if n.startswith(("tire", "tyre", "tread", "sidewall")):
        return TYRE, None
    return TEXTURED, WHEEL


# Rim shaders draw the wheel texture's colour times the technique's PaintScale
# (research/wheels.md, "Rim brightness"). Defaults from the shared
# media/cars/Shared.zip ShaderSettings/Normal.xml, the same on FH1 and FM4;
# the techniques listed are the wheel ones that have the parameter (chrome_rim,
# wheel_emblem and wheel_black have none). A car's ShaderSettings.xml
# overrides them: FOR_FocusRS500_10's near-black rim texels (about 4) at 7.5,
# BEN_CONTINENTALGT_11's near-white ones (213) at 0.5.
WHEEL_PAINT_SCALE = {
    "rim": 0.75,
    "rim_simple": 0.5,
    "rim_V2": 1.0,
    "rim_bump_metal": 1.0,
    "inner_rim": 1.0,
    "inner_rim_V2": 1.0,
    "outer_rim": 1.0,
    "caliper": 1.0,
}


def shader_settings(car_dir: Path) -> dict[str, dict[str, float]]:
    """The car's ShaderSettings.xml as {technique (lower case): {parameter: value}}
    (parse_shader_settings). {} without the file."""
    path = next((p for p in Path(car_dir).iterdir() if p.name.lower() == "shadersettings.xml"), None)
    if path is None:
        return {}
    return parse_shader_settings(path.read_text(encoding="utf-8", errors="replace"))


_SETTINGS_TOKEN = re.compile(r"<!--.*?-->|<\s*(/?)\s*([A-Za-z_][\w.]*)([^<>]*?)(/?)\s*>", re.S)
_SETTINGS_ATTR = re.compile(r'([A-Za-z_]\w*)\s*=\s*"([^"]*)"')


def parse_shader_settings(text: str) -> dict[str, dict[str, float]]:
    """A ShaderSettings XML (a car's, or the shared Normal.xml) as {technique (lower
    case): {parameter: value}}: scalar `value` parameters only (colours and
    non-numbers are left out). Read leniently: a few cars' files don't nest their tags
    properly (`<badge>...</BADGE>`, a stray close tag), which Forza's reader takes."""
    out: dict[str, dict[str, float]] = {}
    stack: list[str] = []
    for m in _SETTINGS_TOKEN.finditer(text):
        if m.group(0).startswith("<!--"):
            continue
        close, name, attrs, selfclose = m.groups()
        if close:
            if stack and stack[-1] == name.lower():
                stack.pop()
            continue
        values = dict(_SETTINGS_ATTR.findall(attrs))
        if selfclose or values:
            if len(stack) >= 2 and "value" in values:  # <ShaderSettings><technique><Param value=.../>
                try:
                    value = float(values["value"])
                except ValueError:
                    continue
                if math.isfinite(value):
                    out.setdefault(stack[-1], {})[name] = value
            continue
        stack.append(name.lower())
    return out


def wheel_paint_scale(technique: str, settings: dict[str, dict[str, float]]) -> float:
    """The PaintScale Forza applies to a wheel technique's texel colour: the
    car's own setting, else WHEEL_PAINT_SCALE, else 1 (the technique has none)."""
    chrome = chrome_diffuse_scale(settings, technique)
    if chrome is not None:  # chrome_rim: the texel times ChromeDiffuseScale under its reflection
        return chrome
    default = next((v for k, v in WHEEL_PAINT_SCALE.items() if k.lower() == technique.lower()), None)
    if default is None:
        return 1.0
    return settings.get(technique.lower(), {}).get("PaintScale", default)


# --- per-subsection resolution -------------------------------------------------

SWATCH_TEXELS = 4.0  # footprint below this many texels (both axes): one flat colour
TILING_LIMIT = 1.05  # UVs beyond [-0.05, 1.05]: a tiling texture, not the atlas
REFERENCE_SIZE = 1024  # atlas size used to measure footprints (nodamage.xds)


@dataclass
class SubMaterial:
    """What one subsection samples.

    role:       paint / glass / trim / tyre / textured / skip
    family:     nodamage / lights / interior (cockpit) / None (atlas baseSampler reads)
    kind:       "textured", "swatch", "tiling" or "none" (no UVs used)
    uv_transform: (x_off, x_scale, y_off, y_scale) for UV0
    bounds:     (u0, v0, u1, v1) of the transformed UVs over the triangles used
    colour:     RGBA averaged over the footprint (swatches; filled by
                `sample_colours`)
    """

    technique: str
    role: str
    family: str | None
    kind: str
    uv_transform: tuple[float, float, float, float]
    bounds: tuple[float, float, float, float]
    colour: tuple[int, int, int, int] | None = None

    def uv(self, raw: tuple[float, float]) -> tuple[float, float]:
        x_off, x_scale, y_off, y_scale = self.uv_transform
        return raw[0] * x_scale + x_off, raw[1] * y_scale + y_off


def resolve(section: carbin.Section, sub: carbin.SubSection, classifier=classify) -> SubMaterial:
    if getattr(classifier, "section_aware", False):
        role, family = classifier(sub.material, section.name)
    else:
        role, family = classifier(sub.material)
    transform = tuple(sub.uv_transform[:4])
    pool = section.vertices_for(sub)
    used = {i for tri in sub.triangles() for i in tri}
    if not used:
        return SubMaterial(sub.material, role, family, "none", transform, (0, 0, 0, 0))
    x_off, x_scale, y_off, y_scale = transform
    us = [pool[i].uv0[0] * x_scale + x_off for i in used]
    vs = [pool[i].uv0[1] * y_scale + y_off for i in used]
    bounds = (min(us), min(vs), max(us), max(vs))
    if abs(x_scale) < 1e-5 and abs(y_scale) < 1e-5:
        kind = "swatch"  # every vertex samples one point
    elif min(bounds) < -(TILING_LIMIT - 1) or max(bounds) > TILING_LIMIT:
        kind = "tiling"
    elif (bounds[2] - bounds[0]) * REFERENCE_SIZE < SWATCH_TEXELS and (
        bounds[3] - bounds[1]
    ) * REFERENCE_SIZE < SWATCH_TEXELS:
        kind = "swatch"
    else:
        kind = "textured"
    return SubMaterial(sub.material, role, family, kind, transform, bounds)


def resolve_all(sections: list[carbin.Section], lod: int | None = None):
    """Yield (section, subsection, SubMaterial) for every subsection (at `lod`)."""
    for section in sections:
        for sub in section.subsections:
            if lod is None or sub.lod == lod:
                yield section, sub, resolve(section, sub)


def texture_path(car_dir: Path, family: str, lod0: bool = False, damaged: bool = False) -> Path:
    """The .xds file for a family in an extracted car folder (case-insensitive)."""
    main, high, damage = FAMILY_FILES[family]
    wanted = (damage if damaged else high if lod0 else main).lower()
    for path in car_dir.iterdir():
        if path.name.lower() == wanted:
            return path
    raise FileNotFoundError(f"{wanted} not in {car_dir}")


# --- the trim shaders' texel scale -------------------------------------------------
#
# What Forza's car shaders do with the base texel (research/forza-materials.md,
# "Forza's trim shaders and flat black grilles"; shaders_v16.fxobj through dsff
# fxobj), by the parameters the technique has in the shared Shared.zip
# ShaderSettings/Normal.xml (FH1 and FM4 alike for these):
# - with PaintScale and no PaintColor (rim, badge, smooth_metal, rubber...): the texel
#   times PaintScale (WHEEL_PAINT_SCALE for the wheel ones);
# - with PaintColor (PAINT_TECHNIQUES: misc, plastic2, matte_colors, weatherstrip...):
#   the paint shaders, texel x PristinePaintScale + paint x PaintScale where the
#   texel's alpha is low. PaintScale there scales the paint: the Cougar Eliminator's
#   `misc` 0.2 doesn't darken its grille;
# - neither (frame, black, rubber_trim, bumper_frame): no scale in the shared file;
#   some cars set PristinePaintScale on `frame` (the Veyron 0.3), taken as set.
PAINT_TECHNIQUES = frozenset(
    [
        "badge2",
        "body",
        "body_2",
        "body_3",
        "body_sh",
        "body_simple",
        "body_traffic",
        "cage",
        "cloth",
        "cloth2",
        "cockpit_body",
        "debugbody",
        "debugbody_2",
        "debugspecular2",
        "dullplastic",
        "emblem",
        "embossed_plastic",
        "embossed_plastic2",
        "engine",
        "engine_body",
        "engine_reflective",
        "headlight_paint",
        "lights_body",
        "liverybadge",
        "matte",
        "matte_colors",
        "matte_colors2",
        "misc",
        "mottled",
        "plastic",
        "plastic2",
        "plastic_simple",
        "race_kit_red",
        "weatherstrip",
    ]
)
# Normal.xml's PaintScale for the trim-role techniques with no PaintColor.
TRIM_PAINT_SCALE = {"smooth_metal": 1.0, "rubber": 1.0, "hub": 1.0, "scaling_text": 1.0}
TEXEL_SCALE_MAX = 4.0


def texel_scale(settings: dict[str, dict[str, float]], technique: str) -> float:
    """The factor Forza's shader applies to this technique's opaque texels (linear
    light), from the car's ShaderSettings (shader_settings) over the shared defaults:
    PristinePaintScale for the paint shaders (PAINT_TECHNIQUES) and the techniques
    without a scale, PaintScale for the others (TRIM_PAINT_SCALE, wheel_paint_scale);
    1 when nothing applies."""
    n = technique.lower()
    params = settings.get(n, {})
    if n in PAINT_TECHNIQUES or n not in TRIM_PAINT_SCALE:
        value = params.get("PristinePaintScale", 1.0)
    else:
        value = params.get("PaintScale", TRIM_PAINT_SCALE[n])
    return min(TEXEL_SCALE_MAX, max(0.0, value))


# --- the trim and rim shaders' specular and reflection -------------------------------
#
# Besides the texel, Forza's car shaders add a specular term, SpecularAmount x the
# directional light x (N.H)^SpecularPower, and a cube-map reflection: CubeScale x
# FresnelScalar x a Fresnel term of FresnelIndex in the paint and trim shaders, CubeScale
# alone in the chrome ones (research/forza-materials.md, "Gloss: Forza's specular and
# reflection on trims and rims"). The defaults per technique are those of the shared
# media/cars/Shared.zip ShaderSettings/Normal.xml (FH1 and FM4 alike for these); a
# technique the file gives none of them to (black, wheel_black, grille1) has no
# specular and no reflection. A car's ShaderSettings.xml overrides them (the Cougar
# Eliminator's `misc`: SpecularAmount 0.5, SpecularPower 5, FresnelScalar 0.2).
# technique: (SpecularAmount, SpecularPower, FresnelScalar, CubeScale, FresnelIndex)
GLOSS_DEFAULTS = {
    "misc": (0.2, 20.0, 0.1, 1.0, 1.65),
    "plastic": (1.0, 120.0, 0.0, 1.0, 1.65),
    "plastic2": (2.0, 8.0, 0.0, 1.0, 1.65),
    "plastic_simple": (1.0, 120.0, 0.0, 1.0, 1.65),
    "dullplastic": (0.3, 8.0, 0.0, 1.0, 1.65),
    "embossed_plastic": (4.0, 128.0, 0.0, 1.0, 1.65),
    "embossed_plastic2": (4.0, 128.0, 0.0, 1.0, 1.65),
    "matte": (1.5, 20.0, 0.0, 1.0, 1.65),
    "matte_colors": (0.75, 20.0, 0.0, 1.0, 1.65),
    "matte_colors2": (4.0, 128.0, 0.0, 1.0, 1.65),
    "rubber_trim": (1.0, 10.0, 0.0, 1.0, 1.65),
    "rubber_trim_2": (1.0, 10.0, 0.0, 1.0, 1.65),
    "weatherstrip": (0.3, 5.5, 0.0, 1.0, 1.65),
    "bumper_frame": (4.0, 128.0, 0.0, 1.0, 1.65),
    "bottom": (4.0, 128.0, 1.0, 1.0, 1.65),
    "bottom_2": (4.0, 128.0, 1.0, 1.0, 1.65),
    "frame": (0.0, 128.0, 0.0, 1.0, 1.65),
    "undercarriage": (0.0, 128.0, 0.0, 1.0, 1.65),
    "engine": (4.0, 128.0, 0.0, 1.0, 1.65),
    "cage": (4.0, 128.0, 1.0, 1.0, 1.65),
    "race_kit_red": (4.0, 128.0, 1.0, 1.0, 1.65),
    "defrost_lines": (4.0, 128.0, 0.0, 1.0, 1.65),
    "hub": (0.3, 25.0, 0.0, 1.0, 1.65),
    "rotor": (2.0, 10.0, 0.0, 1.0, 1.65),
    "caliper": (0.0, 128.0, 0.0, 1.0, 1.65),
    "badge": (4.0, 128.0, 1.0, 1.0, 1.65),
    "badge2": (4.0, 128.0, 1.0, 1.0, 1.65),
    "brake_badge": (4.0, 128.0, 0.0, 1.0, 1.65),
    "emblem": (0.0, 75.0, 1.0, 0.8, 1.65),
    # wheel techniques (classify_wheel)
    "rim": (0.0, 1.5, 1.0, 1.5, 1.65),
    "rim_v2": (4.0, 128.0, 1.0, 1.5, 1.65),
    "rim_bump_metal": (4.0, 128.0, 1.0, 1.0, 1.65),
    "rim_bump_metal2": (4.0, 128.0, 1.0, 1.0, 1.65),
    "rim_simple": (4.0, 9.0, 0.0, 1.0, 1.65),
    "inner_rim": (4.0, 128.0, 0.0, 1.5, 1.65),
    "inner_rim_v2": (4.0, 128.0, 0.0, 1.5, 1.65),
    "outer_rim": (4.0, 128.0, 1.0, 1.5, 1.65),
    "wheel_emblem": (4.0, 128.0, 1.0, 1.0, 1.65),
    "wheel_emblem_v2": (4.0, 128.0, 1.0, 1.0, 1.65),
}
# Chrome shaders: no specular, the cube map times CubeScale at every angle, the texel
# times ChromeDiffuseScale. technique: (CubeScale, ChromeDiffuseScale)
CHROME_GLOSS_DEFAULTS = {
    "chrome_rim": (1.0, 0.5),
    "chrome_rim_v2": (1.0, 0.5),
}


@dataclass(frozen=True)
class Gloss:
    """A technique's specular and reflection (GLOSS_DEFAULTS, a car's ShaderSettings).
    fresnel_scalar None: a chrome shader (reflection at every angle, no Fresnel term)."""

    specular: float
    power: float
    fresnel_scalar: float | None
    cube: float
    fresnel_index: float = 1.65

    @property
    def f0(self) -> float:
        """Reflectance at normal incidence of the Fresnel term (Schlick, FresnelIndex)."""
        n = self.fresnel_index
        return ((n - 1) / (n + 1)) ** 2

    @property
    def reflection(self) -> float:
        """Strength of the reflection at grazing angles (CubeScale x FresnelScalar)."""
        return self.cube * (1.0 if self.fresnel_scalar is None else self.fresnel_scalar)


def gloss_params(settings: dict[str, dict[str, float]], technique: str) -> Gloss | None:
    """The Forza specular and reflection of a technique (its source technique for
    trim_art's subsections, technique_source), the car's ShaderSettings (shader_settings)
    over GLOSS_DEFAULTS; None for a technique without either (black, wheel_black, the
    cockpit and lamp techniques: not handled here)."""
    n = technique_source(technique).lower()
    own = settings.get(n, {})
    if n in CHROME_GLOSS_DEFAULTS:
        cube, _ = CHROME_GLOSS_DEFAULTS[n]
        return Gloss(0.0, 1.0, None, max(0.0, own.get("CubeScale", cube)))
    if n not in GLOSS_DEFAULTS:
        return None
    spec, power, scalar, cube, index = GLOSS_DEFAULTS[n]
    return Gloss(
        max(0.0, own.get("SpecularAmount", spec)),
        max(0.0, own.get("SpecularPower", power)),
        max(0.0, own.get("FresnelScalar", scalar)),
        max(0.0, own.get("CubeScale", cube)),
        max(1.0, own.get("FresnelIndex", index)),
    )


def chrome_diffuse_scale(settings: dict[str, dict[str, float]], technique: str) -> float | None:
    """ChromeDiffuseScale of a chrome wheel technique (the texel's share of its colour),
    None for the others."""
    n = technique.lower()
    if n not in CHROME_GLOSS_DEFAULTS:
        return None
    return max(0.0, settings.get(n, {}).get("ChromeDiffuseScale", CHROME_GLOSS_DEFAULTS[n][1]))


def srgb_to_linear(c):
    """sRGB 0..1 -> linear (numpy arrays or floats)."""
    import numpy as np

    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c):
    import numpy as np

    c = np.clip(np.asarray(c, dtype=np.float64), 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def scale_texels(rgb, scale: float):
    """8-bit sRGB values (any shape, numpy) multiplied by `scale` in linear light."""
    import numpy as np

    if scale == 1.0:
        return np.asarray(rgb)
    return np.rint(linear_to_srgb(srgb_to_linear(np.asarray(rgb) / 255.0) * scale) * 255).astype(np.int64)


# A family's atlas with its RGB scaled (texel_scale): "<family>*<scale>". Texturing gives
# trims whose technique scales its texels their own regions in it (trim_art).
SCALED_SEP = "*"


def scaled_family(family: str, scale: float) -> str:
    return family if scale == 1.0 else f"{family}{SCALED_SEP}{scale:.4g}"


def base_family(family: str | None) -> str | None:
    return family.split(SCALED_SEP, 1)[0] if family else family


def family_scale(family: str) -> float:
    return float(family.split(SCALED_SEP, 1)[1]) if SCALED_SEP in family else 1.0


class AtlasCache:
    """Decoded Forza atlases (RGBA, top mip) for one car folder. Each cache has
    its own copy of the texels (callers change them: fill_transparent); the
    decoding is shared by every cache of the process (_decoded). A scaled family
    (scaled_family) is its base family's atlas with the RGB scaled (alpha kept)."""

    def __init__(self, car_dir: Path, lod0: bool = False):
        self.car_dir, self.lod0, self._cache = car_dir, lod0, {}

    def get(self, family: str) -> tuple[int, int, bytearray]:
        if family not in self._cache:
            base = base_family(family)
            if base != family:
                import numpy as np

                width, height, rgba = self.get(base)
                px = np.frombuffer(bytes(rgba), dtype=np.uint8).reshape(-1, 4).copy()
                lut = scale_texels(np.arange(256), family_scale(family)).astype(np.uint8)
                px[:, :3] = lut[px[:, :3]]
                self._cache[family] = (width, height, bytearray(px.tobytes()))
            else:
                width, height, rgba = _decoded(texture_path(self.car_dir, family, self.lod0))
                self._cache[family] = (width, height, bytearray(rgba))
        return self._cache[family]


_DECODED: dict = {}  # (path, size, mtime) -> (width, height, RGBA bytes), most recent last
DECODED_KEEP = 8  # textures kept decoded (a car's exterior, wheel and cockpit atlases)


def _decoded(path: Path) -> tuple[int, int, bytes]:
    """xds.untile + xds.decode_rgba of a texture file, decoded once per process
    while the file is unchanged (the cockpit and the exterior read some atlases
    two or three times)."""
    from dsfforza.formats import xds

    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if key in _DECODED:
        _DECODED[key] = _DECODED.pop(key)
    else:
        blocks, width, height, fourcc = xds.untile(path.read_bytes())
        _DECODED[key] = (width, height, bytes(xds.decode_rgba(blocks, width, height, fourcc)))
        while len(_DECODED) > DECODED_KEEP:
            del _DECODED[next(iter(_DECODED))]
    return _DECODED[key]


def footprint_colour(atlas: tuple[int, int, bytearray], bounds) -> tuple[int, int, int, int]:
    """Average RGBA of the atlas texels under a UV rectangle (at least one texel)."""
    width, height, rgba = atlas
    x0 = max(0, min(width - 1, int(bounds[0] * width)))
    y0 = max(0, min(height - 1, int(bounds[1] * height)))
    x1 = max(x0 + 1, min(width, int(bounds[2] * width + 0.999)))
    y1 = max(y0 + 1, min(height, int(bounds[3] * height + 0.999)))
    total, count = [0, 0, 0, 0], 0
    for y in range(y0, y1):
        row = rgba[(y * width + x0) * 4 : (y * width + x1) * 4]
        for c in range(4):
            total[c] += sum(row[c::4])
        count += x1 - x0
    return tuple(t // count for t in total)


def fill_transparent(atlas: tuple[int, int, bytearray], rgb, below: int = 16) -> int:
    """Overwrite, in place, the RGB of texels whose alpha is under `below`
    (alpha kept). Returns how many texels changed."""
    import numpy as np

    _, _, rgba = atlas
    px = np.frombuffer(rgba, dtype=np.uint8).reshape(-1, 4)
    mask = px[:, 3] < below
    px[mask, :3] = np.asarray(rgb[:3], dtype=np.uint8)
    return int(mask.sum())


def sample_colours(materials: list[SubMaterial], atlases: AtlasCache) -> None:
    """Fill `colour` for swatch subsections from their family's atlas."""
    for m in materials:
        if m.kind == "swatch" and m.family:
            m.colour = footprint_colour(atlases.get(m.family), m.bounds)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("car", type=Path, help="extracted car folder")
    parser.add_argument("--carbin", help="carbin file name (default <folder name>.carbin)")
    parser.add_argument("--lod", type=int, default=1)
    parser.add_argument("--colours", action="store_true", help="sample swatch colours (decodes the atlases)")
    args = parser.parse_args(argv)

    name = args.carbin or f"{args.car.name}.carbin"
    sections = carbin.parse((args.car / name).read_bytes())
    rows = [m for _, _, m in resolve_all(sections, args.lod)]
    if args.colours:
        sample_colours(rows, AtlasCache(args.car, lod0="lod0" in name.lower() or "cockpit" in name.lower()))
    by_name: dict[str, list[SubMaterial]] = {}
    for m in rows:
        by_name.setdefault(m.technique, []).append(m)
    print(f"{'technique':22} {'role':9} {'atlas':9} subs  kinds                      uv bounds")
    for technique in sorted(by_name):
        ms = by_name[technique]
        kinds = {}
        for m in ms:
            kinds[m.kind] = kinds.get(m.kind, 0) + 1
        b = [
            min(m.bounds[0] for m in ms),
            min(m.bounds[1] for m in ms),
            max(m.bounds[2] for m in ms),
            max(m.bounds[3] for m in ms),
        ]
        colours = sorted({m.colour for m in ms if m.colour})
        kind_text = " ".join(f"{k}:{v}" for k, v in sorted(kinds.items()))
        extra = f"  swatch {colours[:3]}" if colours else ""
        print(
            f"{technique:22} {ms[0].role:9} {ms[0].family or '-':9} {len(ms):4}  {kind_text:26} "
            f"[{b[0]:.3f},{b[1]:.3f}]-[{b[2]:.3f},{b[3]:.3f}]{extra}"
        )


if __name__ == "__main__":
    sys.exit(main())
