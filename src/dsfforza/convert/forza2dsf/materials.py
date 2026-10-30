"""DSF's shared materials the converter draws with, and the limits a converted entry must fit."""

from __future__ import annotations

import re

from dsfforza.formats import carbin

# --- DSF shared materials, identified by rendering the Desanne's submodels per material.
PAINT = bytes.fromhex("001283358afd1027")
GLASS = bytes.fromhex("001283369afd1027")
TRIM = bytes.fromhex("0012833b12fe1027")
TYRE = bytes.fromhex("00128337b2fd1027")
# Their *_LOD1 twins (GLOBAL_Chassis_LOD1, _Glass_LOD1, _Chassis_Simple_LOD1, _Tyre_LOD1):
# what vanilla clean exterior entries use (paint and trim in 149 of 150, tyres in 150,
# glass in 145; the full-entry materials in at most 8, for a few hundred triangles).
# Converted exteriors used the full-entry ones, and the player's car, which the
# chase camera draws from that entry, came out wrong where the full-entry shaders
# read per-car constants: the DMC-12's stainless entry (copied from vanilla 171, same
# shader maths) drew near black (research/lights.md, "The DMC-12 came out black").
PAINT_LOD1 = bytes.fromhex("10122b9653b14e2a")
GLASS_LOD1 = bytes.fromhex("10122b9953b14e2a")
TRIM_LOD1 = bytes.fromhex("10122b9753b14e2a")
TYRE_LOD1 = bytes.fromhex("10122b9b53b14e2a")
# Chrome (Forza's chrome techniques: bumpers, window frames, drip rails). Vanilla
# full entries draw their chrome with GLOBAL_LOD_Chrome (96 of 150 vehicles,
# 374k triangles; GLOBAL_Chassis_Chrome in 9) and clean exteriors with
# GLOBAL_Chassis_Chrome (148 of 150, 322k triangles; GLOBAL_LOD_Chrome in 8),
# both stride 32, MODL flag 5, sampling the shared chassis texture (reflection
# amount 1, specular power 150; the trim material has specular intensity 0.01).
CHROME = bytes.fromhex("a012f01306d57a2a")  # GLOBAL_LOD_Chrome
CHROME_LOD1 = bytes.fromhex("00128338d7fd1027")  # GLOBAL_Chassis_Chrome
# Vertex stride each material was used with in the Desanne, and MODL render flag
# (the _LOD1 twins: the same in vanilla exteriors, e.g. 147, 171, 227). Read only:
# each conversion adds its host's own materials to a copy (Builder.layout).
MATERIAL_LAYOUT = {
    PAINT: (32, 5),
    GLASS: (28, 7),
    TRIM: (28, 5),
    TYRE: (20, 5),
    CHROME: (32, 5),
    PAINT_LOD1: (32, 5),
    GLASS_LOD1: (28, 7),
    TRIM_LOD1: (28, 5),
    TYRE_LOD1: (20, 5),
    CHROME_LOD1: (32, 5),
    # GLOBAL_Wheel_Blur_SpokeDisc(_LOD1) (SPOKE_DISC below): alpha-blended, MODL flag 7 in
    # every vanilla wheel part that has it
    bytes.fromhex("001262258aa7c428"): (20, 7),
    bytes.fromhex("10122b9e53b14e2a"): (20, 7),
}

DEFAULT_LODS = "4:1,8:3,16:5"  # DSF MODL mask -> Forza LOD
SHADOW_MASK, DEFAULT_SHADOW_LOD = 1, 5
EXTERIOR_MASK = 2
POSITION_SCALE = 4096
# Largest MDXN among vanilla VEHC entries per kind. Spooling a bigger one crashed
# the game (MDXN 0x21D800), so the converter refuses.
MDXN_LIMIT = {0: 0x138800, 1: 0x12D000, 2: 0xDD800}
NONR_LIMIT = {0: 0x6D000, 1: 0x1000, 2: 0x1000}  # SSLP per-kind NONR buffers (spool-limits.md)

# The paint shader overlays the vehicle's chassis texture through UV0. Until
# Forza textures are converted, point every paint vertex at a plain texel the
# Desanne's large paint panels use.
PAINT_UV = (0.835, 0.703)
# Shared trim (GLOBAL_Chassis_Simple) UV: the uniform dark block of the global
# chassis texture (SSIC 00122b73d1fa1227, texels ~(944-1023, 236-268), RG ~24)
# that vanilla trims drawn from a single texel use most (46 hosts: 39% of that area).
TRIM_UV = (0.969, 0.496)
# Shared chrome UV0: the middle of the block of that texture vanilla chrome samples
# most (texels 972-980 x 404-424 on both chrome materials, 150 vehicles; red,
# the grey level, ~98). One texel: Forza's chrome UVs land on its own atlas.
CHROME_UV = (0.9536, 0.8135)
# Vanilla chrome takes its grey per part by where UV0 lands (149 vehicles: the mean R of a
# vehicle's chrome 43 to 156, median 80; most cars span over 30 levels between their
# darker and lighter parts; research/forza-materials.md, "Bare-metal greys"): columns
# x 972 to 1021 of rows 392 to 443, a ramp falling from R 103 (G 126) to 45 (G 58), and
# for its lightest parts a block of R 135, G 135 at x 904 to 941, y 229 to 266 (6 vehicles:
# the Impala taxi, the RS6 Avant...) or one of R 156, G 198 (103 vehicles, small parts).
# Bare-metal trims (trim_art.METAL) take the one nearest their Forza grey; G, the gloss and
# reflection, comes with it. Not 156: its G of 198 made large panels (the Veyron's roof
# scoops, the One-77's grille) bloom in the sky's reflection; 135's G is near CHROME_UV's
# 122. grey -> UV0 (texel centres; ramp columns whose neighbours are within 3).
CHROME_GREYS = {
    135: (922.5 / 1024, 247.5 / 512),
    103: (973.5 / 1024, 416.5 / 512),
    98: (976.5 / 1024, 416.5 / 512),
    92: (981.5 / 1024, 416.5 / 512),
    87: (984.5 / 1024, 416.5 / 512),
    82: (987.5 / 1024, 416.5 / 512),
    74: (992.5 / 1024, 416.5 / 512),
    68: (995.5 / 1024, 416.5 / 512),
    65: (997.5 / 1024, 416.5 / 512),
    62: (1000.5 / 1024, 416.5 / 512),
    57: (1005.5 / 1024, 416.5 / 512),
    49: (1014.5 / 1024, 416.5 / 512),
    45: (1019.5 / 1024, 416.5 / 512),
}


def chrome_grey(grey: float) -> int:
    """The CHROME_GREYS grey nearest an sRGB grey level (0..255)."""
    return min(CHROME_GREYS, key=lambda g: (abs(g - grey), g))


def chrome_uv(technique: str) -> tuple[float, float]:
    """UV0 of a shared-chrome subsection: its grey's texel (dsf_trim_metal#<grey>,
    trim_art), else CHROME_UV (Forza's chrome techniques)."""
    from dsfforza.convert import forza_materials as fm

    return CHROME_GREYS.get(fm.technique_grey(technique), CHROME_UV)


# Licence plates (src/dsfforza/convert/plates.py): vanilla plates are paint-material quads whose
# UV0 covers the plate in the same shared atlas; the synthetic plate sections
# carry those UVs and keep them (add_geometry).
PLATE_TECHNIQUE = "dsf_plate"
# Exposed inner geometry (src/dsfforza/convert/damage/ split_exposed): bonnet/boot undersides
# (technique INNER_TECHNIQUE, paint) and trims under the bonnet. Their
# subsections carry `exposed_uv`, a sentinel UV0 that add_geometry(exposed=True)
# writes and DamagePlan replaces with a projection onto the shared chassis
# texture and a baked sky.
INNER_TECHNIQUE = "dsf_inner"


def material_for(name: str) -> bytes | None:
    n = name.lower()
    if "blur" in n:  # blur_rim, blur_lip, chrome_blur_lip: Forza's spinning-wheel discs
        return None
    if n.startswith("body") or n in (PLATE_TECHNIQUE, INNER_TECHNIQUE):
        return PAINT
    if "glass" in n or "window" in n:
        return GLASS
    if n in ("tire", "rim", "inner_rim", "tyre"):
        return TYRE
    if n.startswith("chrome") or n == "lights_chrome":
        return CHROME
    return TRIM


def raised_lamp(name: str, names) -> bool:
    """A pop-up or hidden head lamp's raised (open) state: <base>up beside the
    lowered state <base>down or plain <base>, any case (headlightLup /
    headlightLdown, the NSX's headlightLUp, glassLHLup, the Stratos's and Elan's
    headlightRup next to headlightR). Forza draws the lowered state on a parked
    car and its cockpit carbins hold only that; DSF has no vanilla pop-up lamps
    to switch to (research/lights.md, "Pop-up head lamps")."""
    m = re.fullmatch(r"(.+)up", name, re.I)
    if not m:
        return False
    base = m.group(1).lower()
    lowered = {n.lower() for n in names}
    return base + "down" in lowered or base in lowered


def vanilla_sections(sections: list[carbin.Section]) -> list[carbin.Section]:
    """Drop upgrade variants: for parts named <base><letter>, keep variant 'a';
    drop <base>race and parts that only exist as non-'a' variants. Pop-up head
    lamps keep only their lowered state (raised_lamp)."""
    names = {s.name for s in sections}
    keep = []
    for s in sections:
        name = s.name
        if name.endswith("race") and name != "race":
            continue
        if raised_lamp(name, names):
            continue
        m = re.fullmatch(r"(.*?)([a-z])", name)  # FM4 goes past d: Skyline R32 bumperFa..Fg, hooda/hoodg
        if m and m.group(1) and (m.group(1) + "a" in names or m.group(1) + "b" in names) and m.group(2) != "a":
            continue
        keep.append(s)
    return keep
