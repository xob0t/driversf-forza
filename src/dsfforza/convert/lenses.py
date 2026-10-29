"""Forza lamp lenses baked over the lamp behind them, for DSF's opaque Lights material.

Forza draws its lamp lenses as glass: `detail_glass_clear`, `_red` and `_amber`
(fluted, dotted or tinted lens covers on the Lights atlas) are alpha-blended over
the reflector, bulb and housing behind them, with the texel's alpha as the lens's
opacity (research/lights.md, "Lamp lenses").
The converter draws them with the host's Lights material, which is opaque, so
each lens hid its lamp and showed its own texture as a flat plate: the Lancer
Evo VI's fluted headlamp covers as white frosted plates, the Lotus Carlton's as
white blobs, the Panoz Esperante GTLM's yellow lexan covers as solid yellow
(user reports on out/final4).

Vanilla DSF lamps are opaque Lights texels with the lens pattern painted in, so the
lens keeps its triangles and the atlas gets what Forza shows through it: for each
lens texel (finest planned LOD), a ray from its point on the lens straight in
along the lens normal composites the lens over every lens layer behind it and the
first opaque surface it meets (lamp, reflector, housing; their texels, or their
flat colour; a lens ray that meets no lamp that way looks in along the car's axis,
_axis_fallback), with Forza's blend:

    out = a * lens + (1 - a) * behind

a = the lens texel's alpha / 255 (capped by a GlassMaxOpacity in the car's
ShaderSettings.xml, if the technique has one). Coloured lenses (red, amber) glow as
before: the baked texel's alpha keeps the lens texel's lamp score
(dsf_materials.lamp_score). Lens texels that non-lens triangles also sample are left
alone, and so are rays that meet nothing within RAY_DEPTH (they keep the lens's own
texel).

Head lamps by day (issue #124) glow from the lamp, not from the lens: clear front lenses
get a raw glow from the lamp behind them (lens_glow), head lamp surfaces seen directly
from their own mask (lamp_glow), both shaped by the reflector's depth, and day_glow ranks
each lamp's raw glow, as a camera straight ahead sees it, onto vanilla's day-lit head lamps.

    bake(sections, lods, atlases, lens_ids, car_dir, glow=None) -> report
    glow = new_glow(h, w); lamp_glow(sections, lods, atlases, glow); day_glow(glow, view)
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from dsfforza.convert import forza_materials as fm

# Lens techniques on the Lights atlas that Forza alpha-blends with the texel alpha as the
# lens's opacity (glass with a lens texture: flutes, dots, a tint).
LENS_PREFIXES = ("detail_glass",)
# The other lamp glass on the Lights atlas is baked the same way, with Forza's own opacity
# (research/lights.md, "Lamp glass"). In a race Forza draws all its lamp glass with the glass
# shaders of shaders_v16.fxobj (CustomCarShaders.xml loads its custom detail_glass_* shaders in
# the UI only, inraceload false): colour from the texel, opacity max(texel alpha, GlassColor.w)
# (plus Fresnel and specular) capped by GlassMaxOpacity. The lamp glass (lights_gls_tail_light,
# _oldhead...) starts from 1 instead of the texel alpha, so its opacity is GlassMaxOpacity (0.8 on
# most cars) and its texel alpha is the lamp's glow mask; baked with the texel alpha as opacity,
# the Lancer Evo VI's red tail glass came out mostly white. The covers (lights_glass,
# lights_gls_noemit) have no glow of their own: the lamp behind glows through them.
COVERS = ("lights_glass", "lights_gls_noemit")  # = dsf_materials.TEXTURED_COVERS
LAMP_GLASS = "lights_gls_"
# technique prefix -> (GlassColor.w, GlassMaxOpacity) when the car's ShaderSettings.xml sets
# none: media/cars/Shared.zip ShaderSettings/Normal.xml, the same on FH1 and FM4. The lenses
# (detail_glass_*) keep their texel alpha, capped only by the car's own GlassMaxOpacity: the
# shared defaults (GlassColor.w 0.6 on clear and 0.8 on amber lenses, cap 0.7) go with a lens
# colour of texel x GlassColor x PaintScale (a twentieth of the texel) and Forza's reflection,
# which the bake doesn't draw; with the texel's own colour they would make milky plates.
GLASS_DEFAULTS = {"lights_glass": (0.0, 1.0), LAMP_GLASS: (1.0, 1.0)}
RAY_DEPTH = 0.4  # m: how deep a lamp housing can be behind its lens
SAME_LAYER = 0.004  # m: a lens hit this close to the ray's start is the lens's own other face
MAX_LAYERS = 4  # lens layers composited before the opaque surface
# Forza draws some lenses from a small tint swatch stretched over the whole lens (the BMW X5 M's
# red tail glass: 202 texels over 756 cm2, 19 mm a texel). Each texel then stands for lens points
# far apart, and the bake (one ray per block of texels) gave each a different lamp part: the
# stretched swatch showed them as dark blobs over the red (issue #141). A coarse lens (texels
# over COARSE_TEXEL on the car) keeps its own texels where points farther apart than
# SCRAMBLED_SPREAD sample one (|x|: the left and right lamps folded onto one side), and all of
# them when that is most of the lens: Forza's flat tint. Finer lenses whose texels repeat
# (tiled flutes) keep the bake: their texels are a millimetre or two on the car, so the mix
# reads as the lens's grain. Roster lenses: median 1.25 mm a texel, 98th percentile 5.8 mm.
SCRAMBLED_SPREAD = 0.03  # m
COARSE_TEXEL = 0.006  # m: sqrt(lens area / its distinct texels)
SWATCH_SHARE = 0.5  # a coarse lens with more of its texel samples scrambled keeps all its texels
# Such a swatch lens is left out and its tint laid over the Lights surfaces behind it
# (_tint_behind) when this share of its rays meets one; else it keeps its flat tint.
SWATCH_TINT = 0.5
# A rear lens whose texels are one colour (FLAT_SHARE of them within FLAT_SPREAD of the median,
# each channel) is a flat tint over the whole lamp too, however fine its texels: the BMW X6 M's
# red tail glass is 40,000 texels of (183, 0, 0, 118). Taken as a swatch lens (issue #145).
FLAT_SHARE = 0.9
FLAT_SPREAD = 6
RAY_CHUNK = 256
# A lens whose points face out of the car's end at least this much on average (the z component of
# their normals: the Panoz Esperante's laid-back head lamp lens 0.59, the Maserati MC12's tails 0.57)
# may look in along the axis (_axis_fallback). Not side markers (0), nor glass balls and domes
# (the Fiat 595's round lamps 0.0, the Dino 246's tails 0.2), whose texels other lamps share.
AXIS_MIN_FACING = 0.25
RAY_CELL = 0.04  # m: rays are chunked by these cells
RAY_STEP = 2  # texels per ray, each way, at least
RAYS_PER_LENS = 3000
DILATE = 2  # texels: baked values spread into unused neighbours (the atlas box filter reads them)
# Flat colours of what lies behind a lens when it isn't a textured surface (alpha: no glow).
HOUSING = (22, 22, 24, 0)  # nothing within RAY_DEPTH: a dark housing
ROLE_COLOUR = {
    fm.PAINT: (110, 110, 112, 0),
    fm.CHROME: (175, 175, 180, 0),
    fm.TRIM: (28, 28, 30, 0),
}

# Head lamps by day (lens_glow, lamp_glow, day_glow; research/lights.md, "Head lamp glow by
# day"). A Forza lens's alpha is its opacity, not a glow mask, so keeping the lens texel's lamp
# score made the glow follow the lens: the BMW M3 E30's half-clear round lenses (alpha 126 over
# an `oldhead` reflector at 255) glowed about 20 and looked unlit by day, where vanilla round
# 0x1D lamps glow white. Blending the reflector's glow mask through the lens alone fixed the
# M3 but made the Lotus Carlton a flat blob: its reflector's mask is 255 everywhere. Vanilla
# lamps glow from a bulb or reflector hot spot and stay dim at the lens edge. The reflector
# behind a lens is a bowl, deepest behind the bulb's axis and meeting the lens at its rim (M3
# E30 48 mm at the centre to 3 mm at the rim, Carlton 95 to 7), so the depth of what each
# texel's ray meets gives the lamp that shape whatever the Forza mask holds.
GLOW_FRONT_Z = 0.0  # lens subsections whose mean Forza z is below this (front is -z)
# What a ray meets first behind a clear front lens is the lamp when it is a Lights surface
# (bulb, reflector; atlas or tiling: the Firebird Trans Am 77's `oldhead`) or chrome (the chrome reflector bowls of the Honda Civic Si 99 and
# the Nissan Versa: 80% and 24% of their lens rays); its glow is its Forza mask, at least this
# (Forza leaves most reflectors at mask 0 to 10 and lights the bulb).
GLOW_REFLECTOR = 128
GLOW_DEPTH_REF = 90  # percentile of a lens's ray depths over its lamp taken as full depth
GLOW_BULB = 0.02  # m: a bulb (a dip in the depth behind the lens) up to about this wide is filled
GLOW_FLAT = 0.005  # m: a lamp shallower than this is flat (its mask alone)
GLOW_MIN_SAMPLES = 50  # fewer visible samples of a lamp (about 12 cm2): its converted alpha, as before
GLOW_MIN_SHARE = 0.1  # a lens with less of its texels over a lamp keeps its score
# The rim of a lamp (front view) fades in over this share of its edge-to-middle distance. The
# vanilla 0x1D lamps' visible alpha by thirds of that distance, outer to inner, is median 32 / 88
# / 131; without the fade the ports' was 50 / 71 / 101 (the depth of a box reflector like the
# Carlton's stays high out to the lens edge), 0.4 brings six of them to 18-37 / 77-101 / 125-171.
GLOW_RIM = 0.4
GLOW_SHARED = 0.01  # m: a texel whose surfaces lie further apart than this has no one place
# Structure (#286): vanilla's day-lit lamps glow along their lens pattern (flutes, the bulb, LED
# dots) with dark texels between, the ports' raw glow (mask x the reflector's depth) is a smooth
# blob, which by day reads as a white plate. Before the ranking, each lamp's raw glow is
# multiplied by (its visible texel's brightness against the mean over GLOW_DETAIL_RADIUS round
# it) ** GLOW_DETAIL_GAMMA, clipped to GLOW_DETAIL_CLIP: the brighter parts of the lamp's own
# picture rank high, the smooth stretches between them low.
GLOW_DETAIL_RADIUS = 0.015  # m
GLOW_DETAIL_GAMMA = 2.0
GLOW_DETAIL_CLIP = (0.25, 2.0)
# Vanilla DSF day-lit head lamps (type 0x1D) seen from straight ahead: the visible white lamp
# texels' alpha at dsf_materials.LAMP_PERCENTILES, pooled over the 32 vanilla vehicles that have
# them (out/dayglow/dsffront.py on the full entries; per-vehicle mean p10/50/90 41/74/102).
DAY_HEAD_ALPHA = (0, 0, 5, 14, 24, 37, 55, 74, 97, 135, 157, 180, 199, 215, 235, 247, 254, 255, 255)


def _resolve(section, sub, atlases: fm.AtlasCache | None = None) -> fm.SubMaterial:
    """A subsection's material, tiling lamp parts laid on the atlas as Texturing draws them."""
    from dsfforza.convert import dsf_materials

    return dsf_materials.resolve_lamp(section, sub, atlases=atlases)


def _tri_uv(m: fm.SubMaterial, pool, tri) -> list:
    """One triangle's UVs as Texturing lays them (dsf_materials.triangle_uv)."""
    from dsfforza.convert import dsf_materials

    return dsf_materials.triangle_uv(m, [pool[i].uv0 for i in tri])


def is_lens_technique(technique: str) -> bool:
    return technique.startswith(LENS_PREFIXES)


def is_glass_technique(technique: str) -> bool:
    """Lamp glass bake() takes: lenses, the lamp glass (lights_gls_*) and covers."""
    t = technique.lower()
    return is_lens_technique(technique) or t.startswith(LAMP_GLASS) or t in COVERS


def glass_settings(car_dir: Path) -> dict[str, dict]:
    """technique -> {"GlassMaxOpacity": 0..1 (values above 1: no cap), "GlassColor": (r, g, b,
    a), "DarkValues", "LightValues"} from the car's ShaderSettings.xml, the parameters it sets;
    {} without the file or when it doesn't parse."""
    import xml.etree.ElementTree as ET

    path = next((p for p in Path(car_dir).iterdir() if p.name.lower() == "shadersettings.xml"), None)
    if path is None:
        return {}
    try:
        root = ET.fromstring(path.read_bytes())
    except ET.ParseError:
        return {}
    out = {}
    for element in root.iter():
        got = {}
        setting = element.find("GlassMaxOpacity")
        colour = element.find("GlassColor")
        try:
            if setting is not None:
                got["GlassMaxOpacity"] = min(1.0, max(0.0, float(setting.get("value", ""))))
            for name in ("DarkValues", "LightValues"):
                if element.find(name) is not None:
                    got[name] = float(element.find(name).get("value", ""))
            if colour is not None:
                got["GlassColor"] = tuple(float(colour.get(c, "")) for c in "rgba")
        except ValueError:
            continue
        if got:
            out[element.tag] = got
    return out


def glass_max_opacity(car_dir: Path) -> dict[str, float]:
    """technique -> GlassMaxOpacity from the car's ShaderSettings.xml (glass_settings)."""
    return {t: v["GlassMaxOpacity"] for t, v in glass_settings(car_dir).items() if "GlassMaxOpacity" in v}


def opacity_rule(technique: str, settings: dict) -> tuple[float, float]:
    """(floor, cap) of a lamp glass technique's opacity, max(texel alpha, floor) capped by cap:
    lenses their texel alpha under the car's GlassMaxOpacity, the rest Forza's rule with the
    shared defaults (GLASS_DEFAULTS). settings: glass_settings."""
    own = settings.get(technique, {})
    if is_lens_technique(technique):
        return 0.0, own.get("GlassMaxOpacity", 1.0)
    t = technique.lower()
    floor, cap = next((v for k, v in GLASS_DEFAULTS.items() if t.startswith(k)), (0.0, 1.0))
    colour = own.get("GlassColor")
    return (min(1.0, max(0.0, colour[3])) if colour else floor), own.get("GlassMaxOpacity", cap)


def lens_filter(technique: str, rgb, settings: dict) -> np.ndarray:
    """What a lens lets through per channel (0..1) as the custom detail_glass_* shaders draw it
    (detail_glass_red.fxobj, its first pass: the texel, red remapped as sqrt(DarkValues +
    (LightValues - DarkValues) x red^2), with no lighting and no texel alpha, multiplied over
    what lies behind; defaults 0 and 1). Used for swatch lenses (_tint_behind): their texel alpha
    is low (the X5 M's red tail glass 61), and blended by it a red lens over a pale reflector
    came out pink."""
    f = np.asarray(rgb[:3], float) / 255
    if technique.startswith("detail_glass_red"):
        own = settings.get(technique, {})
        dark, light = own.get("DarkValues", 0.0), own.get("LightValues", 1.0)
        f[0] = math.sqrt(max(0.0, dark + (light - dark) * f[0] ** 2))
    return np.clip(f, 0.0, 1.0)


def emits(technique: str) -> bool:
    """Whether the glass glows by its own texel alpha (lenses keep their lamp score, the lamp
    glass is a lamp); covers (COVERS) glow with the lamp behind them."""
    return technique.lower() not in COVERS


def _texels(uv_px: np.ndarray):
    """Texel centres (ix, iy) inside a UV triangle in pixel units and their barycentric
    weights (w0, w1, w2); a triangle holding no centre gives its centroid's texel."""
    xs, ys = uv_px[:, 0], uv_px[:, 1]
    area = (xs[1] - xs[0]) * (ys[2] - ys[0]) - (ys[1] - ys[0]) * (xs[2] - xs[0])
    x0, x1 = int(math.floor(xs.min())), int(math.ceil(xs.max()))
    y0, y1 = int(math.floor(ys.min())), int(math.ceil(ys.max()))
    if abs(area) > 1e-9 and x1 > x0 and y1 > y0:
        gy, gx = np.mgrid[y0:y1, x0:x1] + 0.5
        w0 = ((xs[1] - gx) * (ys[2] - gy) - (ys[1] - gy) * (xs[2] - gx)) / area
        w1 = ((xs[2] - gx) * (ys[0] - gy) - (ys[2] - gy) * (xs[0] - gx)) / area
        w2 = 1 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if inside.any():
            ix, iy = (gx[inside] - 0.5).astype(int), (gy[inside] - 0.5).astype(int)
            return ix, iy, np.stack([w0[inside], w1[inside], w2[inside]], 1)
    cx, cy = xs.mean(), ys.mean()
    return np.array([int(cx)]), np.array([int(cy)]), np.array([[1 / 3, 1 / 3, 1 / 3]])


def _intersect(origins, dirs, tris):
    """Möller-Trumbore for every ray against every triangle of `tris` (v0, e1, e2:
    (m, 3) each): distances (n, m), inf where a ray misses or the hit is beyond
    RAY_DEPTH, and the barycentrics (b1, b2) of the hits."""
    v0, e1, e2 = tris
    dx, dy, dz = (dirs[:, c : c + 1] for c in range(3))
    ax, ay, az = (e1[None, :, c] for c in range(3))
    bx, by, bz = (e2[None, :, c] for c in range(3))
    px, py, pz = dy * bz - dz * by, dz * bx - dx * bz, dx * by - dy * bx  # d x e2
    det = ax * px + ay * py + az * pz
    ok = np.abs(det) > 1e-12
    inv = 1.0 / np.where(ok, det, 1.0)
    sx, sy, sz = (origins[:, c : c + 1] - v0[None, :, c] for c in range(3))
    u = (sx * px + sy * py + sz * pz) * inv
    qx, qy, qz = sy * az - sz * ay, sz * ax - sx * az, sx * ay - sy * ax  # s x e1
    v = (dx * qx + dy * qy + dz * qz) * inv
    t = (bx * qx + by * qy + bz * qz) * inv
    hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0) & (t <= RAY_DEPTH)
    return np.where(hit, t, np.inf), u, v


class _Surfaces:
    """The finest LOD's triangles a ray from a lens can meet: lenses (composited) and
    opaque surfaces (their texel or flat colour). Glass, skipped techniques and tyres
    are not in it (rays pass through them)."""

    def __init__(self, sections, lod: int, atlases: fm.AtlasCache, lens_ids: set, settings: dict):
        self.atlases = atlases
        pts, uvs, kinds, colours, fams, rules, lamps = [], [], [], [], [], [], []
        self.lens_subs = []  # (section, sub, SubMaterial)
        for section in sections:
            for sub in section.subsections:
                if sub.lod != lod:
                    continue
                m = _resolve(section, sub, atlases)
                tris = sub.triangles()
                if not tris:
                    continue
                lens = id(sub) in lens_ids
                if not lens and m.role in (fm.GLASS, fm.SKIP, fm.TYRE):
                    continue
                pool = section.vertices_for(sub)
                if lens:
                    self.lens_subs.append((section, sub, m))
                textured = m.kind == "textured" and m.family in (fm.LIGHTS, fm.NODAMAGE)
                colour = ROLE_COLOUR.get(m.role, ROLE_COLOUR[fm.TRIM])
                if not textured and m.kind == "swatch" and m.family is not None and m.role != fm.PAINT:
                    fm.sample_colours([m], atlases)
                    if m.colour is not None:
                        colour = (*m.colour[:3], 0)
                for t in tris:
                    pts.append([pool[i].position for i in t])
                    uvs.append(_tri_uv(m, pool, t) if textured else [(0.0, 0.0)] * 3)
                    kinds.append(2 if lens else (1 if textured else 0))
                    fams.append(m.family if textured else None)
                    lamps.append(not lens and (m.family == fm.LIGHTS or m.role == fm.CHROME))
                    colours.append(colour)
                    rules.append(opacity_rule(m.technique, settings))
        self.pts = np.array(pts, float).reshape(-1, 3, 3)
        self.uvs = np.array(uvs, float).reshape(-1, 3, 2)
        self.kind = np.array(kinds, int)  # 0 flat, 1 textured, 2 lens (textured on Lights)
        self.lamp = np.array(lamps, bool)  # Lights surfaces (atlas or tiling) and chrome: a bulb or reflector
        self.family = fams
        self.colour = np.array(colours, float).reshape(-1, 4)
        self.opacity = np.array(rules, float).reshape(-1, 2)  # per triangle: (floor, cap), opacity_rule
        self.lo = self.pts.min(1) if len(self.pts) else np.zeros((0, 3))
        self.hi = self.pts.max(1) if len(self.pts) else np.zeros((0, 3))
        self.images = {}

    def image(self, family: str) -> np.ndarray:
        if family not in self.images:
            w, h, rgba = self.atlases.get(family)
            self.images[family] = np.frombuffer(bytes(rgba), np.uint8).reshape(h, w, 4).astype(float)
        return self.images[family]

    def sample(self, tri: np.ndarray, b1: np.ndarray, b2: np.ndarray) -> np.ndarray:
        """RGBA (n, 4) of hits on triangles `tri` at barycentrics (b1, b2): texels of
        textured surfaces (nodamage alpha is no glow mask: 0; paint-through texels are
        paint), flat colours otherwise."""
        out = self.colour[tri].copy()
        for family in (fm.LIGHTS, fm.NODAMAGE):
            sel = np.array([self.family[k] == family for k in tri], bool) & (self.kind[tri] > 0)
            if not sel.any():
                continue
            img = self.image(family)
            h, w = img.shape[:2]
            t = tri[sel]
            uv = (
                (1 - b1[sel] - b2[sel])[:, None] * self.uvs[t, 0]
                + b1[sel][:, None] * self.uvs[t, 1]
                + b2[sel][:, None] * self.uvs[t, 2]
            )
            px = img[(uv[:, 1] * h).astype(int).clip(0, h - 1), (uv[:, 0] * w).astype(int).clip(0, w - 1)]
            if family == fm.NODAMAGE:
                through = (px[:, 3] < 16) & (px[:, :3].min(1) > 200)
                px = px.copy()
                px[through] = ROLE_COLOUR[fm.PAINT]
                px[:, 3] = 0
            out[sel] = px
        return out

    def near(self, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        """Indices of triangles whose bounding box meets the box (lo, hi)."""
        return np.nonzero(np.all(self.hi >= lo, 1) & np.all(self.lo <= hi, 1))[0]


def _behind(surf: _Surfaces, origins, dirs, cand):
    """What each ray sees behind its lens: the lens layers and the first opaque surface
    (HOUSING if none) composited front to back. Returns the RGBA, whether the ray met
    anything, and the distance to the first opaque surface, its glow mask, whether it is
    a lamp (_Surfaces.lamp; RAY_DEPTH, 0 and False if none) and its triangle (-1 if none)."""
    n = len(origins)
    acc = np.zeros((n, 4))
    trans = np.ones(n)
    hits = np.zeros(n, bool)
    solid_at = np.full(n, RAY_DEPTH)
    solid_mask = np.zeros(n)
    solid_lamp = np.zeros(n, bool)
    solid_tri = np.full(n, -1)
    if len(cand):
        tris = (surf.pts[cand, 0], surf.pts[cand, 1] - surf.pts[cand, 0], surf.pts[cand, 2] - surf.pts[cand, 0])
        t, u, v = _intersect(origins, dirs, tris)
        depth = min(MAX_LAYERS + 2, t.shape[1])
        near = (
            np.argpartition(t, depth - 1, axis=1)[:, :depth]
            if t.shape[1] > depth
            else np.tile(np.arange(depth), (n, 1))
        )
        rows = np.arange(n)[:, None]
        order = np.argsort(t[rows, near], axis=1)
        near = near[rows, order]
        for j in range(depth):
            k = near[:, j]
            dist = t[np.arange(n), k]
            tri = cand[k]
            live = np.isfinite(dist) & (trans > 0.01)
            # The lens's own other face (or its neighbour on the same sheet): skip it.
            live &= ~((surf.kind[tri] == 2) & (dist < SAME_LAYER))
            if not live.any():
                continue
            r = np.nonzero(live)[0]
            rgba = surf.sample(tri[r], u[r, k[r]], v[r, k[r]])
            hits[r] = True
            solid = surf.kind[tri[r]] != 2
            rule = surf.opacity[tri[r]]
            a = np.where(solid, 1.0, np.minimum(np.maximum(rgba[:, 3] / 255.0, rule[:, 0]), rule[:, 1]))
            acc[r] += (trans[r] * a)[:, None] * rgba
            solid_at[r[solid]] = dist[r[solid]]
            solid_mask[r[solid]] = rgba[solid, 3]
            solid_lamp[r[solid]] = surf.lamp[tri[r[solid]]]
            solid_tri[r[solid]] = tri[r[solid]]
            trans[r] *= 1 - a
    acc += trans[:, None] * np.array(HOUSING, float)
    return acc, hits, solid_at, solid_mask, solid_lamp, solid_tri


def _axis_fallback(surf: _Surfaces, origins, dirs, found, front: bool = True) -> int:
    """A lens's rays that met no lamp along the lens normal, again along the car's axis into
    the car (+z from the front, -z from the rear: Forza's front is -z): a laid-back head lamp
    lens (the Panoz Esperante's, its normal 65% up) sees the housing floor along its normal
    and the reflector bowls a camera ahead of the car sees through it along the axis; a tail
    lamp lens wrapped round the wing or laid along the boot lid, the same from behind. A ray's
    results (`found`: _behind's arrays, in place) take the axis ray's where that meets a lamp.
    Only a lens facing out of the car's end (AXIS_MIN_FACING on average over its rays: along the
    axis, a side marker's would run along the flank), and only its points that face out. Returns
    the rays changed."""
    outward = dirs[:, 2] if front else -dirs[:, 2]  # dirs point in: -normal
    if not len(outward) or outward.mean() < AXIS_MIN_FACING:
        return 0
    redo = np.nonzero(~found[4] & (outward > 0))[0]
    if not len(redo):
        return 0
    axis = np.tile([0.0, 0.0, 1.0 if front else -1.0], (len(redo), 1))
    start = origins[redo] + dirs[redo] * 0.001 - axis * 0.002  # off the lens along the axis
    changed = 0
    for c0 in range(0, len(redo), RAY_CHUNK):
        rows = redo[c0 : c0 + RAY_CHUNK]
        o, d = start[c0 : c0 + RAY_CHUNK], axis[: len(rows)]
        ends = o + d * RAY_DEPTH
        got = _behind(surf, o, d, surf.near(np.minimum(o.min(0), ends.min(0)), np.maximum(o.max(0), ends.max(0))))
        take = got[4]
        for out, new in zip(found, (got[0], got[1], got[2], got[3], got[4], got[5])):
            out[rows[take]] = new[take]
        changed += int(take.sum())
    return changed


def _closing(pos: np.ndarray, values: np.ndarray, radius: float) -> np.ndarray:
    """Morphological closing of per-ray values over the lens (max, then min, of the rays
    within `radius` of each): fills dips narrower than the radius, keeps the rim's falloff."""
    out = values
    for reduce, fill in ((np.max, -np.inf), (np.min, np.inf)):
        res = np.empty_like(out)
        for c0 in range(0, len(pos), RAY_CHUNK):
            near = np.linalg.norm(pos[c0 : c0 + RAY_CHUNK, None] - pos[None], axis=2) < radius
            res[c0 : c0 + RAY_CHUNK] = reduce(np.where(near, out[None], fill), axis=1)
        out = res
    return out


def _shape(pos: np.ndarray, depth: np.ndarray, lamp: np.ndarray) -> np.ndarray:
    """The reflector's shape at each sample: the depth of the lamp there (0 where no lamp),
    its bulb filled (_closing, GLOW_BULB), against the lamp's full depth (GLOW_DEPTH_REF);
    0..1. A lamp under GLOW_FLAT deep is flat: 1 everywhere over it."""
    closed = _closing(pos, np.where(lamp, depth, 0.0), GLOW_BULB)
    ref = np.percentile(closed[lamp], GLOW_DEPTH_REF) if lamp.any() else 0.0
    if ref < GLOW_FLAT:
        return lamp.astype(float)
    return np.clip(closed / ref, 0.0, 1.0)


def lens_glow(a, lens_mask, ray, ray_pos, depth, mask, lamp) -> np.ndarray | None:
    """Raw glow (0..255, Forza's scale) of one clear front lens's texels from the lamp behind
    them, or None when too little of the lens lies over a lamp (it keeps its own score).
    Per texel: lens opacity a, lens glow mask, ray index; per ray: start point, the distance to
    its first opaque surface, that surface's glow mask and whether it is a lamp (bulb,
    reflector: _Surfaces.lamp).

    The lamp's glow (its mask, at least GLOW_REFLECTOR) as the lens passes it, (1 - a), and
    scatters it by its own mask, a x lens; times the reflector's shape (_shape: the depth of the
    lamp behind each texel). Over a housing, paint or trim it is 0."""
    if len(ray) == 0 or lamp[ray].mean() < GLOW_MIN_SHARE:
        return None
    shape = _shape(ray_pos, depth, lamp)
    light = np.where(lamp, np.maximum(mask, GLOW_REFLECTOR), 0.0) * shape
    return light[ray] * (a * lens_mask / 255 + (1 - a))


def lamp_glow(sections, lods, atlases: fm.AtlasCache, glow: np.ndarray, owners: dict | None = None) -> int:
    """Raw glow (0..255, Forza's scale) of the front head lamp surfaces (lights.TECHNIQUE_FUNCTION
    head lamp techniques: reflectors and bulbs, seen directly or through DSF's clear glass) of
    the finest of `lods`, into `glow` (new_glow) where it is still NaN: the texel's Forza mask
    (at least GLOW_REFLECTOR) times the reflector's shape (_shape: how deep each point sits
    behind the subsection's front). One lamp per subsection (owners: _GlowSum). Returns the number
    of texels set."""
    from dsfforza.convert import lights

    head = {t for t, (f, _) in lights.TECHNIQUE_FUNCTION.items() if f == lights.HEAD}
    lod = min(lods)
    w, h, rgba = atlases.get(fm.LIGHTS)
    src = np.frombuffer(bytes(rgba), np.uint8).reshape(h, w, 4)
    acc = _GlowSum(h, w, glow, owners)
    for section in sections:
        for sub in section.subsections:
            if sub.lod != lod or sub.material not in head:
                continue
            m = _resolve(section, sub, atlases)
            if m.family != fm.LIGHTS or m.kind != "textured":
                continue
            pool = section.vertices_for(sub)
            cols, rows, pos = [], [], []
            for t in sub.triangles():
                P = np.array([pool[i].position for i in t], float)
                ix, iy, bary = _texels(np.array(_tri_uv(m, pool, t)) * (w, h))
                ok = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
                cols.append(ix[ok])
                rows.append(iy[ok])
                pos.append(bary[ok] @ P)
            if not cols:
                continue
            ix, iy, pos = np.concatenate(cols), np.concatenate(rows), np.concatenate(pos)
            if not len(ix) or pos[:, 2].mean() >= GLOW_FRONT_Z:
                continue
            step = max(RAY_STEP, math.ceil(math.sqrt(len(ix) / RAYS_PER_LENS)))
            _, first, inverse = np.unique((iy // step) * w + ix // step, return_index=True, return_inverse=True)
            # Forza's front is -z: depth behind the subsection's frontmost point.
            depth = pos[first, 2] - pos[:, 2].min()
            shape = _shape(pos[first], depth, np.ones(len(first), bool))
            acc.add(iy, ix, np.maximum(src[iy, ix, 3], GLOW_REFLECTOR) * shape[inverse], pos, (section, sub))
    new = acc.done & np.isnan(glow[..., 0])
    acc.write(glow, new)
    return int(new.sum())


def new_glow(h: int, w: int) -> np.ndarray:
    """An empty glow map for bake and lamp_glow, the size of the Lights atlas: per texel the raw
    glow, its lamp (a number per lens or head lamp subsection) and where the surface it covers
    sits in a front view: |x| (the left and right lamps often share their texels) and y, in
    Forza model space; NaN where none."""
    return np.full((h, w, 4), np.nan)


class _GlowSum:
    """Raw glow and positions summed over the triangles sampling each texel, one lamp number per
    add() (after those already in `glow`). owners: gets lamp number -> (section, sub) each lamp
    comes from (dsf_materials.Binding.set_daytime)."""

    def __init__(self, h: int, w: int, glow: np.ndarray, owners: dict | None = None):
        self.total = np.zeros((h, w, 5))  # raw, |x|, y, |x|^2, y^2
        self.count = np.zeros((h, w))
        self.lamp = np.full((h, w), np.nan)
        self.owners = owners if owners is not None else {}
        lamps = glow[..., 1]
        self.next = int(np.nanmax(lamps)) + 1 if np.isfinite(lamps).any() else 0

    def add(self, iy, ix, raw, pos, owner=None) -> None:
        x, y = np.abs(pos[:, 0]), pos[:, 1]
        np.add.at(self.total, (iy, ix), np.stack([raw, x, y, x * x, y * y], 1))
        np.add.at(self.count, (iy, ix), 1)
        self.lamp[iy, ix] = self.next
        self.owners[self.next] = owner
        self.next += 1

    @property
    def done(self) -> np.ndarray:
        return self.count > 0

    def write(self, glow: np.ndarray, where: np.ndarray) -> None:
        mean = self.total[where] / self.count[where, None]
        glow[where, 0] = mean[:, 0]
        glow[where, 1] = self.lamp[where]
        # A texel that lamps in different places share (the Mercedes SLR's four projector
        # lenses, two a side) has no one place: no rim fade (NaN).
        spread = mean[:, 3] - mean[:, 1] ** 2 + mean[:, 4] - mean[:, 2] ** 2
        one = spread < GLOW_SHARED**2
        glow[where, 2:] = np.where(one[:, None], mean[:, 1:3], np.nan)


def _rim(mask: np.ndarray) -> np.ndarray:
    """Each pixel of `mask`'s distance to its edge (chessboard, in pixels; 0 outside)."""
    dist = np.zeros(mask.shape)
    inside = mask.copy()
    k = 0
    while inside.any():
        k += 1
        dist[inside] = k
        core = inside.copy()
        core[1:] &= inside[:-1]
        core[:-1] &= inside[1:]
        core[:, 1:] &= inside[:, :-1]
        core[:, :-1] &= inside[:, 1:]
        core[0] = core[-1] = False
        core[:, 0] = core[:, -1] = False
        inside = core
    return dist


def _detail(image: np.ndarray, mask: np.ndarray, radius: int) -> np.ndarray:
    """image / (its mean over the (2 radius + 1)^2 box within mask), 1 outside mask."""
    v = np.where(mask, image, 0.0)
    n = mask.astype(float)
    for a in (0, 1):
        for arr in (v, n):
            c = np.cumsum(np.pad(arr, [(radius + 1, radius) if k == a else (0, 0) for k in (0, 1)]), axis=a)
            arr[...] = np.take(c, np.arange(2 * radius + 1, c.shape[a]), axis=a) - np.take(
                c, np.arange(0, c.shape[a] - 2 * radius - 1), axis=a
            )
    mean = np.where(n > 0, v / np.maximum(n, 1e-9), 1.0)
    return np.where(mask & (mean > 1e-6), image / np.maximum(mean, 1e-6), 1.0)


def day_glow(glow: np.ndarray, view, colour: np.ndarray | None = None) -> np.ndarray | None:
    """Glow alpha (DSF, 0..255) from a glow map (new_glow: raw glow from lens_glow and
    lamp_glow, lamps, positions). view: what a camera straight ahead sees (Texturing._lamp_view:
    the (rows, columns) of the texel each sample sees, the (H, W) mask of the samples, and the
    view's origin x, y and step in metres).

    Each lamp's rim as the camera sees it fades out: a texel GLOW_RIM or less of the way from
    the lamp's edge to its middle has its raw glow scaled down to 0 at the edge (vanilla lamps
    are dark at the rim). Then per lamp each value's rank among the lamp's visible samples
    goes onto vanilla's day-lit head lamp alpha (DAY_HEAD_ALPHA). So every head lamp glows as
    much as vanilla's, brightest where Forza's light and the reflector's shape are, and a bumper
    lamp doesn't push the main lamps down; raw 0 (a lens over a dark housing) stays 0. Lamps
    with fewer than GLOW_MIN_SAMPLES visible samples keep NaN (hidden reflectors behind a lens;
    the converted alpha). None when no lamp is mapped.

    colour: the Lights atlas (h, w, 3+) the car is drawn with: each lamp's raw glow follows the
    detail of its visible picture (GLOW_DETAIL_*) before the ranking (#286)."""
    from dsfforza.convert import dsf_materials as dm

    seen, grid, (x0, y0), step = view
    raw, lamps = glow[..., 0].copy(), glow[..., 1]
    seen_lamp = lamps[seen]
    image = np.full(grid.shape, np.nan)
    image[grid] = np.where(np.isfinite(raw[seen]), seen_lamp, np.nan)
    out = np.full(raw.shape, np.nan)
    for g in np.unique(image[np.isfinite(image)]):
        mask = image == g
        if mask.sum() < GLOW_MIN_SAMPLES:
            continue
        mine = (lamps == g) & np.isfinite(raw)
        if GLOW_RIM > 0:
            rim = _rim(mask)
            row = np.floor(np.nan_to_num(glow[..., 3][mine] - y0) / step).astype(int)
            placed = np.isfinite(glow[..., 3][mine])
            row = np.where(placed, row, 0)
            d = np.zeros(len(row))
            for side in (1, -1):  # |x| on either side: the lamp's own
                col = np.floor(np.nan_to_num(side * glow[..., 2][mine] - x0) / step).astype(int)
                inside = placed & (row >= 0) & (row < grid.shape[0]) & (col >= 0) & (col < grid.shape[1])
                d[inside] = np.maximum(d[inside], rim[row[inside], col[inside]])
            raw[mine] *= np.where(placed, np.minimum(1.0, d / (GLOW_RIM * rim.max())), 1.0)
        if colour is not None and GLOW_DETAIL_GAMMA:
            # The lamp's own picture as the camera sees it: each seen texel's brightness against
            # its neighbourhood's; a texel seen by several samples takes their mean.
            lum = colour[..., :3].max(-1).astype(float)
            picture = np.zeros(grid.shape)
            picture[grid] = lum[seen]
            ratio = _detail(picture, mask, max(1, round(GLOW_DETAIL_RADIUS / step)))
            rows, cols = seen
            pick = mask[grid]
            acc = np.zeros(raw.shape)
            cnt = np.zeros(raw.shape)
            np.add.at(acc, (rows[pick], cols[pick]), ratio[mask])
            np.add.at(cnt, (rows[pick], cols[pick]), 1)
            factor = np.where(cnt > 0, acc / np.maximum(cnt, 1), 1.0)
            factor = np.clip(factor, *GLOW_DETAIL_CLIP) ** GLOW_DETAIL_GAMMA
            raw[mine] *= factor[mine]
        visible = (seen_lamp == g) & np.isfinite(raw[seen])
        pool = np.sort(raw[seen][visible])
        r = raw[mine]
        # The share of the lamp's visible samples below each value (ties: the middle).
        rank = (np.searchsorted(pool, r, "left") + np.searchsorted(pool, r, "right")) / (2 * len(pool))
        alpha = np.where(r > 0, np.interp(rank * 100, dm.LAMP_PERCENTILES, DAY_HEAD_ALPHA), 0.0)
        out[mine] = alpha
    return out if np.isfinite(out).any() else None


def _lens_samples(section, sub, m, w: int, h: int):
    """A lens subsection's texel samples on the (w, h) Lights atlas: (ix, iy, position,
    normal) arrays, one row per texel centre inside each triangle, and the size of its texels
    on the car (m, sqrt(area / distinct texels)); None without any."""
    pool = section.vertices_for(sub)
    cols, rows, pos, nrm = [], [], [], []
    area = 0.0
    for t in sub.triangles():
        P = np.array([pool[i].position for i in t], float)
        N = np.array([pool[i].normal for i in t], float)
        area += 0.5 * float(np.linalg.norm(np.cross(P[1] - P[0], P[2] - P[0])))
        ix, iy, bary = _texels(np.array(_tri_uv(m, pool, t)) * (w, h))
        ok = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
        ix, iy, bary = ix[ok], iy[ok], bary[ok]
        if not len(ix):
            continue
        face = np.cross(P[1] - P[0], P[2] - P[0])
        n = bary @ N
        length = np.linalg.norm(n, axis=1)
        fn = face / (np.linalg.norm(face) or 1.0)
        if fn @ N.sum(0) < 0:
            fn = -fn
        n = np.where(length[:, None] > 1e-6, n / np.maximum(length, 1e-12)[:, None], fn)
        cols.append(ix)
        rows.append(iy)
        pos.append(bary @ P)
        nrm.append(n)
    if not cols:
        return None
    ix, iy = np.concatenate(cols), np.concatenate(rows)
    texel = math.sqrt(area / len(np.unique(iy * w + ix)))
    return ix, iy, np.concatenate(pos), np.concatenate(nrm), texel


def _scrambled(samples, h: int, w: int) -> np.ndarray:
    """(h, w) mask of the texels left as the lens's own: the texels of a coarse lens (texels
    over COARSE_TEXEL) that lens points more than SCRAMBLED_SPREAD apart sample (positions
    with x folded to |x|, so mirrored left and right lamps on one texel agree), and all its
    texels when they are most of it (a swatch stretched over the lens keeps its flat tint).
    `samples`: (section, sub, m, _lens_samples or None) of every lens."""
    lo = np.full((h * w, 3), np.inf)
    hi = np.full((h * w, 3), -np.inf)
    for *_, got in samples:
        if got is None:
            continue
        ix, iy, pos, _, _ = got
        folded = pos.copy()
        folded[:, 0] = np.abs(folded[:, 0])
        k = iy * w + ix
        for a in range(3):
            np.minimum.at(lo[:, a], k, folded[:, a])
            np.maximum.at(hi[:, a], k, folded[:, a])
    spread = np.where(np.isfinite(lo).all(1), (hi - lo).max(1), 0.0)
    out = np.zeros(h * w, bool)
    for *_, got in samples:
        if got is None:
            continue
        ix, iy, _, _, texel = got
        k = iy * w + ix
        far = spread[k] > SCRAMBLED_SPREAD
        if texel > COARSE_TEXEL and far.any():
            out[k if far.mean() > SWATCH_SHARE else k[far]] = True
    return out.reshape(h, w)


def _flat(rgba: np.ndarray) -> bool:
    """Whether a lens's texel samples (n, 4) are one colour: FLAT_SHARE of them within
    FLAT_SPREAD of the median in every channel."""
    if not len(rgba):
        return False
    median = np.median(rgba, 0)
    return float((np.abs(rgba - median).max(1) <= FLAT_SPREAD).mean()) >= FLAT_SHARE


def _tint_behind(surf: _Surfaces, tris, tint: np.ndarray, out, w: int, h: int, tinted) -> int:
    """A swatch lens's flat tint (lens_filter: per channel, 0..1) multiplied into every texel of
    the Lights surfaces behind it (surf triangles `tris`), into `out` (h, w, 4), once a texel
    (`tinted` marks those). The texels keep their alpha (their glow mask). Returns the texels
    tinted."""
    count = 0
    for k in tris:
        if surf.kind[k] != 1 or surf.family[k] != fm.LIGHTS:
            continue
        ix, iy, _ = _texels(surf.uvs[k] * (w, h))
        ok = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
        ix, iy = ix[ok], iy[ok]
        new = ~tinted[iy, ix]
        ix, iy = ix[new], iy[new]
        out[iy, ix, :3] = out[iy, ix, :3] * tint
        tinted[iy, ix] = True
        count += len(ix)
    return count


def _weight(rgb: np.ndarray) -> np.ndarray:
    """The brightness factor of dsf_materials.lamp_score: 0.6 + 0.4 x brightest channel."""
    return 0.6 + 0.4 * rgb[:, :3].max(1) / 255


def bake(
    sections,
    lods,
    atlases: fm.AtlasCache,
    lens_ids: set,
    car_dir: Path,
    glow: np.ndarray | None = None,
    dropped: set | None = None,
    owners: dict | None = None,
) -> dict:
    """Bake every lamp glass subsection (id(sub) in lens_ids, Lights atlas: lenses, lamp glass,
    covers; is_glass_technique) of the finest of `lods` over what lies behind it, into atlases'
    Lights texels (in place). glow: a glow map (new_glow) that gets the raw glow of the clear
    front lens and cover texels (lens_glow, one lamp per lens, for day_glow); without it they
    keep their lamp score like the coloured ones; owners: gets their lamps' subsections
    (_GlowSum). dropped: gets (section name, technique) of the
    swatch lenses left out (their tint goes onto the lamp behind them, SWATCH_TINT).
    Returns a report: {"<section>/<technique>": {"texels", "behind" (share of rays that met
    something), "shared" (texels left alone)}}."""
    from dsfforza.convert import lights

    lod = min(lods)
    settings = glass_settings(car_dir)
    surf = _Surfaces(sections, lod, atlases, lens_ids, settings)
    if not surf.lens_subs:
        return {}
    w, h, rgba = atlases.get(fm.LIGHTS)
    src = np.frombuffer(bytes(rgba), np.uint8).reshape(h, w, 4).astype(float)
    # Texels non-lens triangles sample (any planned LOD): left as they are.
    used = np.zeros((h, w), bool)
    for section in sections:
        for sub in section.subsections:
            if sub.lod not in lods or id(sub) in lens_ids:
                continue
            m = _resolve(section, sub, atlases)
            if m.family != fm.LIGHTS or m.kind != "textured" or m.role not in (fm.TEXTURED, fm.TRIM, fm.PAINT):
                continue
            if getattr(m, "drifted", False):
                continue  # a coarse LOD's drifted UVs (dsf_materials.unwrap_tiles): the Ferrari F50's lens kept them
            pool = section.vertices_for(sub)
            for t in sub.triangles():
                ix, iy, _ = _texels(np.array(_tri_uv(m, pool, t)) * (w, h))
                used[iy.clip(0, h - 1), ix.clip(0, w - 1)] = True
    lamp_tris = np.nonzero((surf.kind == 1) & np.array([f == fm.LIGHTS for f in surf.family], bool))[0]
    total = np.zeros((h, w, 4))
    count = np.zeros((h, w))
    lit = _GlowSum(h, w, glow, owners) if glow is not None else None
    report = {}
    samples = [(section, sub, m, _lens_samples(section, sub, m, w, h)) for section, sub, m in surf.lens_subs]
    scrambled = _scrambled(samples, h, w)
    tints = []  # (behind triangles, lens_filter) of the swatch lenses left out
    for section, sub, _m, got in samples:
        if got is None:
            continue
        ix, iy, pos, nrm, _ = got
        free = ~used[iy, ix] & ~scrambled[iy, ix]
        key = f"{section.name}/{sub.material}"
        entry = report.setdefault(key, {"texels": 0, "behind": 0, "shared": 0})
        entry["shared"] += int(used[iy, ix].sum())
        mixed = scrambled[iy, ix] & ~used[iy, ix]
        if mixed.any():
            entry["scrambled"] = entry.get("scrambled", 0) + int(mixed.sum())
        floor, cap = opacity_rule(sub.material, settings)
        # A swatch lens (most of its texels scrambled) is one flat tint over the whole lamp, as
        # Forza draws it over the lamp's graphics. Kept as the lens's own texels it hid them and
        # their glow (the BMW X5 M's tails: only the LED strip lit, issue #145), so its filter
        # (lens_filter) goes onto the Lights surfaces behind it and the lens is left out.
        # So is a rear lens of one flat colour (_flat: the BMW X6 M's red tail glass).
        swatch = (
            dropped is not None
            and is_lens_technique(sub.material)
            and (scrambled[iy, ix].mean() > SWATCH_SHARE or (pos[:, 2].mean() >= GLOW_FRONT_Z and _flat(src[iy, ix])))
        )
        # A front lens's glow covers its shared texels too (the AST DB5 Vantage's lens shares
        # 95% of its texels with its reflector's coarser LODs): the same lamp. Not its
        # scrambled ones: they hold no one lamp. The lamp glass is a lamp itself (lamp_glow).
        front = glow is not None and pos[:, 2].mean() < GLOW_FRONT_Z and not sub.material.lower().startswith(LAMP_GLASS)
        keep = np.ones(len(ix), bool) if swatch else (~scrambled[iy, ix] if front else free)
        ix, iy, pos, nrm, free = ix[keep], iy[keep], pos[keep], nrm[keep], free[keep]
        if not len(ix):
            continue
        lens_rgba = src[iy, ix]
        lens_a = np.minimum(np.maximum(lens_rgba[:, 3] / 255.0, floor), cap)
        # One ray per block of RAY_STEP x RAY_STEP texels or more (about RAYS_PER_LENS
        # rays a lens): what lies behind a lens is softer than the lens's own pattern
        # (flutes, dots), which stays per texel, and the DSF atlas holds the Lights texels
        # at about half their size anyway. A swatch lens's texels stand for points far
        # apart: one ray per lens point, RAYS_PER_LENS at most.
        if swatch:
            first = np.arange(0, len(ix), max(1, math.ceil(len(ix) / RAYS_PER_LENS)))
            inverse = np.zeros(len(ix), int)
        else:
            step = max(RAY_STEP, math.ceil(math.sqrt(len(ix) / RAYS_PER_LENS)))
            block = (iy // step) * w + ix // step
            _, first, inverse = np.unique(block, return_index=True, return_inverse=True)
        # Rays in compact spatial chunks, so each chunk tests few triangles.
        cell = np.floor(pos[first] / RAY_CELL).astype(np.int64)
        order = np.lexsort((cell[:, 2], cell[:, 1], cell[:, 0]))
        first = first[order]
        inverse = np.argsort(order)[inverse]
        dirs = -nrm[first]
        origins = pos[first] + nrm[first] * 0.001
        behind = np.zeros((len(first), 4))
        met = np.zeros(len(first), bool)
        depth = np.full(len(first), RAY_DEPTH)
        mask = np.zeros(len(first))
        lamp = np.zeros(len(first), bool)
        solid = np.full(len(first), -1)
        for c0 in range(0, len(first), RAY_CHUNK):
            c1 = min(len(first), c0 + RAY_CHUNK)
            ends = origins[c0:c1] + dirs[c0:c1] * RAY_DEPTH
            lo = np.minimum(origins[c0:c1].min(0), ends.min(0))
            hi = np.maximum(origins[c0:c1].max(0), ends.max(0))
            behind[c0:c1], met[c0:c1], depth[c0:c1], mask[c0:c1], lamp[c0:c1], solid[c0:c1] = _behind(
                surf, origins[c0:c1], dirs[c0:c1], surf.near(lo, hi)
            )
        axis = _axis_fallback(
            surf, origins, dirs, (behind, met, depth, mask, lamp, solid), front=pos[:, 2].mean() < GLOW_FRONT_Z
        )
        if axis:
            entry["axis"] = entry.get("axis", 0) + axis
            entry["front"] = bool(pos[:, 2].mean() < GLOW_FRONT_Z)
        flat = is_lens_technique(sub.material) and _flat(src[iy, ix])
        if flat:
            entry["flat"] = True
        if swatch:
            on_lamp = solid[np.isin(solid, lamp_tris)]
            # A clear one over a lamp goes whatever else lies behind it: kept, it was an opaque
            # grey plate over the lamp, which glowed white by day (the Hennessey Venom GT's lexan
            # head lamp covers over the projectors and their housing, #250).
            clear = lights.texel_class(tuple(lens_rgba.mean(0))) == "white"
            if len(on_lamp) >= SWATCH_TINT * len(first) or (clear and len(on_lamp)):
                tints.append((np.unique(on_lamp), lens_filter(sub.material, lens_rgba.mean(0), settings)))
                dropped.add((section.name, sub.material))
                entry["dropped"] = True
            continue
        hit_any = met[inverse]
        baked = lens_a[:, None] * lens_rgba + (1 - lens_a[:, None]) * behind[inverse]
        if flat and pos[:, 2].mean() < GLOW_FRONT_Z:
            # A front lens of one flat colour (the Audi Q7's bumper lens, the Ferrari 599's amber
            # head lamp lenses; issue #189) stays: its clear texels carry the head lamps' day glow
            # (lens_glow). Blended by its alpha it was a flat plate over the lamp, and its glow the
            # lens's flat alpha: its colour is a filter over what lies behind (lens_filter, as a
            # swatch lens's over its lamp), and it glows with the lamp behind, as a cover does.
            baked[:, :3] = behind[inverse][:, :3] * lens_filter(sub.material, lens_rgba.mean(0), settings)
            baked[:, 3] = mask[inverse] * _weight(behind[inverse]) / _weight(baked)
        elif emits(sub.material):
            # The glow stays the lens's: the alpha is set so the converter's lamp score
            # (dsf_materials.lamp_score: alpha x (0.6 + 0.4 x brightest channel)) is the lens
            # texel's own. Blending in the reflector's glow mask too gave the Subaru WRX STI's
            # flat red tail lens (85% opaque) blotches of 7 to 32 when lit: the car's red
            # curve stretches small differences over vanilla's whole range.
            baked[:, 3] = lens_rgba[:, 3] * _weight(lens_rgba) / _weight(baked)
        else:
            # A cover has no glow of its own: the lamp behind glows through it. Forza's lamp
            # emission is HDR (BrakeColor 2 to 4 times its mask) and outshines a cover at 0.9;
            # passing only (1 - opacity) of it left the VW GTI VR6's tails darker in the dark mood
            # than under DSF's window glass (VM run vm1-20261028-103705). The lamp's whole glow.
            baked[:, 3] = mask[inverse] * _weight(behind[inverse]) / _weight(baked)
        entry["texels"] += int(free.sum())
        entry["behind"] += int(hit_any[free].sum())
        # Rays that met nothing keep the lens texel: a lens with nothing behind it in
        # reach (a lamp outside the body's shell, a side marker) is drawn as Forza's texel.
        keep = ~hit_any
        baked[keep] = lens_rgba[keep]
        np.add.at(total, (iy[free], ix[free]), baked[free])
        np.add.at(count, (iy[free], ix[free]), 1)
        if front:
            # Clear texels (dsf_materials.lamp_hue white).
            r, g, b = lens_rgba[:, 0], lens_rgba[:, 1], lens_rgba[:, 2]
            red = (r > 1.6 * g) & (r > 1.6 * b)
            amber = (r > 1.2 * b + 20) & (g > 0.35 * r) & (g > 1.2 * b)
            # A selective-yellow lens covers a head lamp (lights.yellow_lens: the Panoz
            # Esperante GTLM's lexan) and glows with it like a clear one.
            if lights.yellow_lens(tuple(lens_rgba.mean(0))):
                amber[:] = False
            sel = ~red & ~amber
            lens_mask = lens_rgba[sel, 3] if emits(sub.material) else np.zeros(int(sel.sum()))
            raw = lens_glow(lens_a[sel], lens_mask, inverse[sel], origins, depth, mask, lamp)
            if raw is not None:
                lit.add(iy[sel], ix[sel], raw, pos[sel], (section, sub))
    glowing = lit.done if lit is not None else np.zeros((h, w), bool)
    if lit is not None:
        lit.write(glow, glowing)
    done = count > 0
    if not done.any() and not tints:
        return report
    out = src.copy()
    out[done] = total[done] / count[done, None]
    # Spread baked texels into unused neighbours, so the atlas's box filter at the
    # region edges reads baked colour (and glow) rather than the bare lens.
    grown = done.copy()
    for _ in range(DILATE):
        acc = np.zeros((h, w, 4))
        n = np.zeros((h, w))
        g_acc, g_n = np.zeros((h, w, 4)), np.zeros((h, w))
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            shifted = np.roll(np.roll(grown, dy, 0), dx, 1)
            vals = np.roll(np.roll(out, dy, 0), dx, 1)
            take = shifted & ~grown & ~used & ~scrambled
            acc[take] += vals[take]
            n[take] += 1
            if glow is not None:
                near = take & np.roll(np.roll(glowing, dy, 0), dx, 1)
                g_acc[near] = np.roll(np.roll(glow, dy, 0), dx, 1)[near]  # a neighbour's (one lamp)
                g_n[near] = 1
        new = n > 0
        out[new] = acc[new] / n[new, None]
        if glow is not None:
            near = g_n > 0
            glow[near] = g_acc[near]
            glowing |= near
        grown |= new
    tinted = np.zeros((h, w), bool)
    for tris, tint in tints:
        report.setdefault("tinted", {"texels": 0, "behind": 0, "shared": 0})["texels"] += _tint_behind(
            surf, tris, tint, out, w, h, tinted
        )
    rgba[:] = np.clip(np.rint(out), 0, 255).astype(np.uint8).tobytes()
    for v in report.values():
        v["behind"] = round(v["behind"] / v["texels"], 3) if v["texels"] else 0.0
    return report
