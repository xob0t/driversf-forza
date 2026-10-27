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

Stock DSF lamps are opaque Lights texels with the lens pattern painted in, so the
lens keeps its triangles and the atlas gets what Forza shows through it: for each
lens texel (finest planned LOD), a ray from its point on the lens straight in
along the lens normal composites the lens over every lens layer behind it and the
first opaque surface it meets (lamp, reflector, housing; their texels, or their
flat colour), with Forza's blend:

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
each lamp's raw glow, as a camera straight ahead sees it, onto stock's day-lit head lamps.

    bake(sections, lods, atlases, lens_ids, car_dir, glow=None) -> report
    glow = new_glow(h, w); lamp_glow(sections, lods, atlases, glow); day_glow(glow, view)
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from dsfforza.convert import forza_materials as fm

# Lens techniques on the Lights atlas that Forza alpha-blends with the texel alpha as the
# lens's opacity (glass with a lens texture: flutes, dots, a tint). The lights_gls_* lamp
# glass is left as it was: its opacity is a material setting (GlassMaxOpacity) and its
# texel alpha looks like a glow mask (the Lancer Evo VI's red tail glass, baked with it,
# came out mostly white), and the coloured covers Texturing.is_lens moves to the atlas
# (the DMC-12's grid lens) were checked in game as they are.
LENS_PREFIXES = ("detail_glass",)
RAY_DEPTH = 0.4  # m: how deep a lamp housing can be behind its lens
SAME_LAYER = 0.004  # m: a lens hit this close to the ray's start is the lens's own other face
MAX_LAYERS = 4  # lens layers composited before the opaque surface
RAY_CHUNK = 256
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
# an `oldhead` reflector at 255) glowed about 20 and looked unlit by day, where stock round
# 0x1D lamps glow white. Blending the reflector's glow mask through the lens alone fixed the
# M3 but made the Lotus Carlton a flat blob: its reflector's mask is 255 everywhere. Stock
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
# stock 0x1D lamps' visible alpha by thirds of that distance, outer to inner, is median 32 / 88
# / 131; without the fade the ports' was 50 / 71 / 101 (the depth of a box reflector like the
# Carlton's stays high out to the lens edge), 0.4 brings six of them to 18-37 / 77-101 / 125-171.
GLOW_RIM = 0.4
GLOW_SHARED = 0.01  # m: a texel whose surfaces lie further apart than this has no one place
# Stock DSF day-lit head lamps (type 0x1D) seen from straight ahead: the visible white lamp
# texels' alpha at dsf_materials.LAMP_PERCENTILES, pooled over the 32 stock vehicles that have
# them (out/dayglow/dsffront.py on the full entries; per-vehicle mean p10/50/90 41/74/102).
DAY_HEAD_ALPHA = (0, 0, 5, 14, 24, 37, 55, 74, 97, 135, 157, 180, 199, 215, 235, 247, 254, 255, 255)


def is_lens_technique(technique: str) -> bool:
    return technique.startswith(LENS_PREFIXES)


def glass_max_opacity(car_dir: Path) -> dict[str, float]:
    """technique -> GlassMaxOpacity from the car's ShaderSettings.xml (values above 1: no
    cap); {} without the file or when it doesn't parse."""
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
        setting = element.find("GlassMaxOpacity")
        if setting is not None:
            try:
                out[element.tag] = min(1.0, max(0.0, float(setting.get("value", ""))))
            except ValueError:
                continue
    return out


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

    def __init__(self, sections, lod: int, atlases: fm.AtlasCache, lens_ids: set, opacity: dict):
        self.atlases = atlases
        pts, uvs, kinds, colours, fams, alphas, lamps = [], [], [], [], [], [], []
        self.lens_subs = []  # (section, sub, SubMaterial)
        for section in sections:
            for sub in section.subsections:
                if sub.lod != lod:
                    continue
                m = fm.resolve(section, sub)
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
                    uvs.append([m.uv(pool[i].uv0) for i in t] if textured else [(0.0, 0.0)] * 3)
                    kinds.append(2 if lens else (1 if textured else 0))
                    fams.append(m.family if textured else None)
                    lamps.append(not lens and (m.family == fm.LIGHTS or m.role == fm.CHROME))
                    colours.append(colour)
                    alphas.append(opacity.get(m.technique, 1.0))
        self.pts = np.array(pts, float).reshape(-1, 3, 3)
        self.uvs = np.array(uvs, float).reshape(-1, 3, 2)
        self.kind = np.array(kinds, int)  # 0 flat, 1 textured, 2 lens (textured on Lights)
        self.lamp = np.array(lamps, bool)  # Lights surfaces (atlas or tiling) and chrome: a bulb or reflector
        self.family = fams
        self.colour = np.array(colours, float).reshape(-1, 4)
        self.max_opacity = np.array(alphas, float)
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
    anything, and the distance to the first opaque surface, its glow mask and whether it is
    a lamp (_Surfaces.lamp; RAY_DEPTH, 0 and False if none)."""
    n = len(origins)
    acc = np.zeros((n, 4))
    trans = np.ones(n)
    hits = np.zeros(n, bool)
    solid_at = np.full(n, RAY_DEPTH)
    solid_mask = np.zeros(n)
    solid_lamp = np.zeros(n, bool)
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
            a = np.where(solid, 1.0, np.minimum(rgba[:, 3] / 255.0, surf.max_opacity[tri[r]]))
            acc[r] += (trans[r] * a)[:, None] * rgba
            solid_at[r[solid]] = dist[r[solid]]
            solid_mask[r[solid]] = rgba[solid, 3]
            solid_lamp[r[solid]] = surf.lamp[tri[r[solid]]]
            trans[r] *= 1 - a
    acc += trans[:, None] * np.array(HOUSING, float)
    return acc, hits, solid_at, solid_mask, solid_lamp


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


def lamp_glow(sections, lods, atlases: fm.AtlasCache, glow: np.ndarray) -> int:
    """Raw glow (0..255, Forza's scale) of the front head lamp surfaces (lights.TECHNIQUE_FUNCTION
    head lamp techniques: reflectors and bulbs, seen directly or through DSF's clear glass) of
    the finest of `lods`, into `glow` (new_glow) where it is still NaN: the texel's Forza mask
    (at least GLOW_REFLECTOR) times the reflector's shape (_shape: how deep each point sits
    behind the subsection's front). One lamp per subsection. Returns the number of texels set."""
    from dsfforza.convert import lights

    head = {t for t, (f, _) in lights.TECHNIQUE_FUNCTION.items() if f == lights.HEAD}
    lod = min(lods)
    w, h, rgba = atlases.get(fm.LIGHTS)
    src = np.frombuffer(bytes(rgba), np.uint8).reshape(h, w, 4)
    acc = _GlowSum(h, w, glow)
    for section in sections:
        for sub in section.subsections:
            if sub.lod != lod or sub.material not in head:
                continue
            m = fm.resolve(section, sub)
            if m.family != fm.LIGHTS or m.kind != "textured":
                continue
            pool = section.vertices_for(sub)
            cols, rows, pos = [], [], []
            for t in sub.triangles():
                P = np.array([pool[i].position for i in t], float)
                ix, iy, bary = _texels(np.array([m.uv(pool[i].uv0) for i in t]) * (w, h))
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
            acc.add(iy, ix, np.maximum(src[iy, ix, 3], GLOW_REFLECTOR) * shape[inverse], pos)
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
    add() (after those already in `glow`)."""

    def __init__(self, h: int, w: int, glow: np.ndarray):
        self.total = np.zeros((h, w, 5))  # raw, |x|, y, |x|^2, y^2
        self.count = np.zeros((h, w))
        self.lamp = np.full((h, w), np.nan)
        lamps = glow[..., 1]
        self.next = int(np.nanmax(lamps)) + 1 if np.isfinite(lamps).any() else 0

    def add(self, iy, ix, raw, pos) -> None:
        x, y = np.abs(pos[:, 0]), pos[:, 1]
        np.add.at(self.total, (iy, ix), np.stack([raw, x, y, x * x, y * y], 1))
        np.add.at(self.count, (iy, ix), 1)
        self.lamp[iy, ix] = self.next
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


def day_glow(glow: np.ndarray, view) -> np.ndarray | None:
    """Glow alpha (DSF, 0..255) from a glow map (new_glow: raw glow from lens_glow and
    lamp_glow, lamps, positions). view: what a camera straight ahead sees (Texturing._lamp_view:
    the (rows, columns) of the texel each sample sees, the (H, W) mask of the samples, and the
    view's origin x, y and step in metres).

    Each lamp's rim as the camera sees it fades out: a texel GLOW_RIM or less of the way from
    the lamp's edge to its middle has its raw glow scaled down to 0 at the edge (stock lamps
    are dark at the rim). Then per lamp each value's rank among the lamp's visible samples
    goes onto stock's day-lit head lamp alpha (DAY_HEAD_ALPHA). So every head lamp glows as
    much as stock's, brightest where Forza's light and the reflector's shape are, and a bumper
    lamp doesn't push the main lamps down; raw 0 (a lens over a dark housing) stays 0. Lamps
    with fewer than GLOW_MIN_SAMPLES visible samples keep NaN (hidden reflectors behind a lens;
    the converted alpha). None when no lamp is mapped."""
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
        visible = (seen_lamp == g) & np.isfinite(raw[seen])
        pool = np.sort(raw[seen][visible])
        r = raw[mine]
        # The share of the lamp's visible samples below each value (ties: the middle).
        rank = (np.searchsorted(pool, r, "left") + np.searchsorted(pool, r, "right")) / (2 * len(pool))
        out[mine] = np.where(r > 0, np.interp(rank * 100, dm.LAMP_PERCENTILES, DAY_HEAD_ALPHA), 0.0)
    return out if np.isfinite(out).any() else None


def bake(
    sections,
    lods,
    atlases: fm.AtlasCache,
    lens_ids: set,
    car_dir: Path,
    glow: np.ndarray | None = None,
) -> dict:
    """Bake every lens (id(sub) in lens_ids, Lights atlas) of the finest of `lods` over
    what lies behind it, into atlases' Lights texels (in place). glow: a glow map (new_glow) that
    gets the raw glow of the clear front lens texels (lens_glow, one lamp per lens, for
    day_glow); without it they keep their lamp score like the coloured ones.
    Returns a report: {"<section>/<technique>": {"texels", "behind" (share of rays that met
    something), "shared" (texels left alone)}}."""
    lod = min(lods)
    opacity = glass_max_opacity(car_dir)
    surf = _Surfaces(sections, lod, atlases, lens_ids, opacity)
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
            m = fm.resolve(section, sub)
            if m.family != fm.LIGHTS or m.kind != "textured" or m.role not in (fm.TEXTURED, fm.TRIM, fm.PAINT):
                continue
            pool = section.vertices_for(sub)
            for t in sub.triangles():
                ix, iy, _ = _texels(np.array([m.uv(pool[i].uv0) for i in t]) * (w, h))
                used[iy.clip(0, h - 1), ix.clip(0, w - 1)] = True
    total = np.zeros((h, w, 4))
    count = np.zeros((h, w))
    lit = _GlowSum(h, w, glow) if glow is not None else None
    report = {}
    for section, sub, m in surf.lens_subs:
        pool = section.vertices_for(sub)
        cols, rows, pos, nrm = [], [], [], []
        for t in sub.triangles():
            P = np.array([pool[i].position for i in t], float)
            N = np.array([pool[i].normal for i in t], float)
            ix, iy, bary = _texels(np.array([m.uv(pool[i].uv0) for i in t]) * (w, h))
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
            continue
        ix, iy = np.concatenate(cols), np.concatenate(rows)
        pos, nrm = np.concatenate(pos), np.concatenate(nrm)
        free = ~used[iy, ix]
        key = f"{section.name}/{sub.material}"
        entry = report.setdefault(key, {"texels": 0, "behind": 0, "shared": 0})
        entry["shared"] += int((~free).sum())
        # A front lens's glow covers its shared texels too (the AST DB5 Vantage's lens shares
        # 95% of its texels with its reflector's coarser LODs): the same lamp.
        front = glow is not None and pos[:, 2].mean() < GLOW_FRONT_Z
        if not front:
            ix, iy, pos, nrm, free = ix[free], iy[free], pos[free], nrm[free], free[free]
        if not len(ix):
            continue
        lens_rgba = src[iy, ix]
        lens_a = np.minimum(lens_rgba[:, 3] / 255.0, opacity.get(sub.material, 1.0))
        # One ray per block of RAY_STEP x RAY_STEP texels or more (about RAYS_PER_LENS
        # rays a lens): what lies behind a lens is softer than the lens's own pattern
        # (flutes, dots), which stays per texel, and the DSF atlas holds the Lights texels
        # at about half their size anyway.
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
        for c0 in range(0, len(first), RAY_CHUNK):
            c1 = min(len(first), c0 + RAY_CHUNK)
            ends = origins[c0:c1] + dirs[c0:c1] * RAY_DEPTH
            lo = np.minimum(origins[c0:c1].min(0), ends.min(0))
            hi = np.maximum(origins[c0:c1].max(0), ends.max(0))
            behind[c0:c1], met[c0:c1], depth[c0:c1], mask[c0:c1], lamp[c0:c1] = _behind(
                surf, origins[c0:c1], dirs[c0:c1], surf.near(lo, hi)
            )
        hit_any = met[inverse]
        baked = lens_a[:, None] * lens_rgba + (1 - lens_a[:, None]) * behind[inverse]
        # The glow stays the lens's: the alpha is set so the converter's lamp score
        # (dsf_materials.lamp_score: alpha x (0.6 + 0.4 x brightest channel)) is the lens
        # texel's own. Blending in the reflector's glow mask too gave the Subaru WRX STI's
        # flat red tail lens (85% opaque) blotches of 7 to 32 when lit: the car's red
        # curve stretches small differences over stock's whole range.
        weight = 0.6 + 0.4 * lens_rgba[:, :3].max(1) / 255
        baked[:, 3] = lens_rgba[:, 3] * weight / (0.6 + 0.4 * baked[:, :3].max(1) / 255)
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
            sel = ~red & ~amber
            raw = lens_glow(lens_a[sel], lens_rgba[sel, 3], inverse[sel], origins, depth, mask, lamp)
            if raw is not None:
                lit.add(iy[sel], ix[sel], raw, pos[sel])
    glowing = lit.done if lit is not None else np.zeros((h, w), bool)
    if lit is not None:
        lit.write(glow, glowing)
    done = count > 0
    if not done.any():
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
            take = shifted & ~grown & ~used
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
    rgba[:] = np.clip(np.rint(out), 0, 255).astype(np.uint8).tobytes()
    for v in report.values():
        v["behind"] = round(v["behind"] / v["texels"], 3) if v["texels"] else 0.0
    return report
