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

    python tools/forza_materials.py out/fh1/NIS_370Z_10 [--lod 1] [--colours]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import carbin

# Roles, matching the DSF side's shared materials plus "textured".
PAINT, GLASS, TRIM, TYRE, TEXTURED, SKIP = "paint", "glass", "trim", "tyre", "textured", "skip"
# Texture families (the atlas the technique's baseSampler is bound to).
NODAMAGE, LIGHTS = "nodamage", "lights"
# Cockpit carbins only: dial faces, needles, switch labels (interior_LOD0.xds;
# its alpha is the label cut-out mask).
INTERIOR = "interior"

FAMILY_FILES = {
    # family: (main-carbin file, lod0/cockpit file, damaged pair)
    NODAMAGE: ("nodamage.xds", "nodamage_LOD0.xds", "damage.xds"),
    LIGHTS: ("Lights.xds", "lights_LOD0.xds", "damagelights.xds"),
    INTERIOR: ("interior_LOD0.xds", "interior_LOD0.xds", "interior_LOD0.xds"),
}

# Exact technique names -> (role, family). Family is the atlas baseSampler
# reads, even for roles the DSF side renders untextured.
TECHNIQUES: dict[str, tuple[str, str | None]] = {}


def _add(role: str, family: str | None, names: str) -> None:
    for name in names.split():
        TECHNIQUES[name] = (role, family)


_add(PAINT, NODAMAGE, "body body_2 body_3 body_traffic cockpit_body headlight_paint numberplate_body")
_add(PAINT, LIGHTS, "lights_body")
_add(GLASS, NODAMAGE, "window window_2 window_traffic window_traffic_dark frontwindow frontwindow_2 "
                      "inside_window cockpit_glass glass_2 red_glass")
# `glass` on FH1 cars is the clear headlight cover; its UVs land on the Lights atlas.
_add(GLASS, LIGHTS, "glass lights_glass lights_gls_noemit")
_add(TRIM, NODAMAGE, "black frame bottom bottom_2 undercarriage misc engine cage rubber_trim rubber_trim_2 "
                     "weatherstrip plastic plastic2 dullplastic embossed_plastic embossed_plastic2 mottled "
                     "cloth cloth2 smooth_metal rubber matte matte_colors matte_colors2 bumper_frame "
                     "black_glass black_glass_breakable race_kit_red chrome chrome_2 chrome_3 chrome_bump "
                     "carbon_fiber carbon_fiber2 carbon_fiber3 carbon_fiber_aniso grille1 grille1_alpha "
                     "grille2 grille2_alpha defrost_lines mirrorLeft mirrorMiddle mirrorRight "
                     "caliper rotor hub numberplate numplate scaling_text")
_add(TRIM, LIGHTS, "lights_chrome")
_add(TEXTURED, NODAMAGE, "interior interior_banner badge badge2 badge_chrome stripe_badge "
                         "cockpit_badge emblem brake_badge gauge_red gauge_needle gauge_emissive "
                         "gauge_emissive_unshadowed")
_add(TEXTURED, LIGHTS, "head_light head_light2 tail_light tail_light_lod0 tail_light_on tail_light2 "
                       "reverse_light reverse_light_lod0 white_brake textured_reflector reflector xenonhead "
                       "hidhead oldhead fullbeam fogwhite fogred taillightst taillight2S brkreverse linred "
                       "rinred indicator_left indicator_right drlwhite drlwhiteswitch drlred drlorange "
                       "drlbrakered drlbrakeorange drlinlorange drlinrorange drlinlred drlinrred drlinlwhite "
                       "drlinrwhite brinl brinr lindtailside rindtailside slorange slinlorange slinrorange "
                       "detail_glass_red detail_glass_clear detail_glass_amber lights_gls_tail_light "
                       "lights_gls_reverse_light lights_gls_indicator_left lights_gls_indicator_right "
                       "lights_gls_hidhead lights_gls_oldhead lights_gls_taillightst lights_gls_taillight2S")
# Synthetic plate sections (tools/plates.py): paint material, UVs on the
# shared atlas's plate (not a Forza technique).
_add(PAINT, None, "dsf_plate dsf_inner")  # dsf_inner: damage.split_exposed (panel undersides)
# Livery decal slots: their raw UV0 spans the whole unit square, i.e. a texture of
# their own (the player's livery), squeezed by m_UVOffsetScale onto an arbitrary patch
# of the car atlases. The KOE_Agera_11's rear-wing pair lands on a 3-texel column at the
# seat artwork's edge in nodamage (blue and white, half of it opaque), its cockpit
# carbin's dash-top strip on interior_LOD0's yellow and blue stitching bands; drawn from
# the atlas they showed as bright yellow/blue streaks on the dashboard and blue patches
# behind the windscreen (user report on out/testdrive9). No livery: nothing to draw.
_add(SKIP, None, "liveryBadge")
_add(TYRE, None, "tire tire_V2 wheel_black tread sidewall rim rim_V2 chrome_rim chrome_rim_V2 outer_rim "
                 "inner_rim inner_rim_V2 wheel_emblem wheel_emblem_V2 rim_bump_metal rim_bump_metal2")


# Techniques that cut out texels with alpha below one half (badge plates are
# drawn entirely from alpha-0 texels: only the emblem's letters show).
ALPHA_TESTED = set("emblem badge badge2 badge_chrome stripe_badge liveryBadge cockpit_badge brake_badge "
                   "wheel_emblem wheel_emblem_V2 grille1_alpha grille2_alpha z_gauge_emissive_alpha".split())


def triangle_max_alpha(atlas: tuple[int, int, bytearray], uvs) -> int:
    """Largest alpha among the atlas texels under a UV triangle's bounding box."""
    width, height, rgba = atlas
    xs = [min(width - 1, max(0, int(u * width))) for u, _ in uvs]
    ys = [min(height - 1, max(0, int(v * height))) for _, v in uvs]
    best = 0
    for y in range(min(ys), max(ys) + 1):
        row = rgba[(y * width + min(xs)) * 4 + 3:(y * width + max(xs) + 1) * 4:4]
        best = max(best, max(row))
    return best


def classify(name: str) -> tuple[str, str | None]:
    """(role, texture family) for a technique name. Exact names first, then
    the prefix conventions of the technique list and CustomCarShaders.xml."""
    if name in TECHNIQUES:
        return TECHNIQUES[name]
    n = name.lower()
    if n.startswith("blur") or n.startswith("chrome_blur") or "shadow" in n or n.startswith("ghost"):
        return SKIP, None
    if n.startswith("body") or n.endswith("_paint") or n.endswith("paint"):
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
    - mirror glass (mirrorLeft/Middle/Right) is a render target in Forza:
      flat trim.
    Without `section` every non-gauge trim maps to nodamage_LOD0 (the old
    behaviour; callers that only need the role may omit it)."""
    n = name.lower()
    if TECHNIQUES.get(name, (None,))[0] == SKIP:
        return SKIP, None
    if n.startswith(("gauge", "z_gauge", "digital_gauge")) or "badge" in n:
        return TEXTURED, INTERIOR
    if n.startswith("mirror"):
        return TRIM, None
    role, family = classify(name)
    if section is not None and not COCKPIT_EXTERIOR_SECTIONS.match(section):
        if role in (TRIM, TEXTURED) and family is not None:
            return TEXTURED, INTERIOR
        return role, family
    if role == TRIM and family is not None:
        return TEXTURED, family
    return role, family


classify_cockpit.section_aware = True


# --- per-subsection resolution -------------------------------------------------

SWATCH_TEXELS = 4.0     # footprint below this many texels (both axes): one flat colour
TILING_LIMIT = 1.05     # UVs beyond [-0.05, 1.05]: a tiling texture, not the atlas
REFERENCE_SIZE = 1024   # atlas size used to measure footprints (nodamage.xds)


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
    elif ((bounds[2] - bounds[0]) * REFERENCE_SIZE < SWATCH_TEXELS
          and (bounds[3] - bounds[1]) * REFERENCE_SIZE < SWATCH_TEXELS):
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


class AtlasCache:
    """Decoded Forza atlases (RGBA, top mip) for one car folder."""

    def __init__(self, car_dir: Path, lod0: bool = False):
        self.car_dir, self.lod0, self._cache = car_dir, lod0, {}

    def get(self, family: str) -> tuple[int, int, bytearray]:
        if family not in self._cache:
            import xds
            blocks, width, height, fourcc = xds.untile(texture_path(self.car_dir, family, self.lod0).read_bytes())
            self._cache[family] = (width, height, xds.decode_rgba(blocks, width, height, fourcc))
        return self._cache[family]


def footprint_colour(atlas: tuple[int, int, bytearray], bounds) -> tuple[int, int, int, int]:
    """Average RGBA of the atlas texels under a UV rectangle (at least one texel)."""
    width, height, rgba = atlas
    x0 = max(0, min(width - 1, int(bounds[0] * width)))
    y0 = max(0, min(height - 1, int(bounds[1] * height)))
    x1 = max(x0 + 1, min(width, int(bounds[2] * width + 0.999)))
    y1 = max(y0 + 1, min(height, int(bounds[3] * height + 0.999)))
    total, count = [0, 0, 0, 0], 0
    for y in range(y0, y1):
        row = rgba[(y * width + x0) * 4:(y * width + x1) * 4]
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
        b = [min(m.bounds[0] for m in ms), min(m.bounds[1] for m in ms),
             max(m.bounds[2] for m in ms), max(m.bounds[3] for m in ms)]
        colours = sorted({m.colour for m in ms if m.colour})
        kind_text = " ".join(f"{k}:{v}" for k, v in sorted(kinds.items()))
        extra = f"  swatch {colours[:3]}" if colours else ""
        print(f"{technique:22} {ms[0].role:9} {ms[0].family or '-':9} {len(ms):4}  {kind_text:26} "
              f"[{b[0]:.3f},{b[1]:.3f}]-[{b[2]:.3f},{b[3]:.3f}]{extra}")


if __name__ == "__main__":
    sys.exit(main())
