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

    out = a * lens + (1 - a) * behind        (RGB and the glow alpha alike)

a = the lens texel's alpha / 255 (capped by a GlassMaxOpacity in the car's
ShaderSettings.xml, if the technique has one). Behind lamps keep their glow mask, so a lit lamp
glows where its bulb is, seen through the lens. Lens texels that non-lens
triangles also sample are left alone, and so are lenses with nothing behind them
within RAY_DEPTH (they keep their own texel).

    bake(sections, lods, atlases, lens_ids, car_dir) -> report
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
        pts, uvs, kinds, colours, fams, alphas = [], [], [], [], [], []
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
                    colours.append(colour)
                    alphas.append(opacity.get(m.technique, 1.0))
        self.pts = np.array(pts, float).reshape(-1, 3, 3)
        self.uvs = np.array(uvs, float).reshape(-1, 3, 2)
        self.kind = np.array(kinds, int)  # 0 flat, 1 textured, 2 lens (textured on Lights)
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
    (HOUSING if none) composited front to back. Returns the RGBA and whether the ray
    met anything."""
    n = len(origins)
    acc = np.zeros((n, 4))
    trans = np.ones(n)
    hits = np.zeros(n, bool)
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
            a = np.where(surf.kind[tri[r]] == 2, np.minimum(rgba[:, 3] / 255.0, surf.max_opacity[tri[r]]), 1.0)
            acc[r] += (trans[r] * a)[:, None] * rgba
            trans[r] *= 1 - a
    acc += trans[:, None] * np.array(HOUSING, float)
    return acc, hits


def bake(sections, lods, atlases: fm.AtlasCache, lens_ids: set, car_dir: Path) -> dict:
    """Bake every lens (id(sub) in lens_ids, Lights atlas) of the finest of `lods` over
    what lies behind it, into atlases' Lights texels (in place). Returns a report:
    {"<section>/<technique>": {"texels", "behind" (share of rays that met something),
    "shared" (texels left alone)}}."""
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
        ix, iy, pos, nrm = ix[free], iy[free], pos[free], nrm[free]
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
        for c0 in range(0, len(first), RAY_CHUNK):
            c1 = min(len(first), c0 + RAY_CHUNK)
            ends = origins[c0:c1] + dirs[c0:c1] * RAY_DEPTH
            lo = np.minimum(origins[c0:c1].min(0), ends.min(0))
            hi = np.maximum(origins[c0:c1].max(0), ends.max(0))
            behind[c0:c1], met[c0:c1] = _behind(surf, origins[c0:c1], dirs[c0:c1], surf.near(lo, hi))
        hit_any = met[inverse]
        baked = lens_a[:, None] * lens_rgba + (1 - lens_a[:, None]) * behind[inverse]
        entry["texels"] += int(len(ix))
        entry["behind"] += int(hit_any.sum())
        # Rays that met nothing keep the lens texel: a lens with nothing behind it in
        # reach (a lamp outside the body's shell, a side marker) is drawn as Forza's texel.
        keep = ~hit_any
        baked[keep] = lens_rgba[keep]
        np.add.at(total, (iy, ix), baked)
        np.add.at(count, (iy, ix), 1)
    done = count > 0
    if not done.any():
        return report
    out = src.copy()
    out[done] = total[done] / count[done, None]
    # Spread baked texels into unused neighbours, so the atlas's box filter at the
    # region edges reads baked colour rather than the bare lens.
    grown = done.copy()
    for _ in range(DILATE):
        acc = np.zeros((h, w, 4))
        n = np.zeros((h, w))
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            shifted = np.roll(np.roll(grown, dy, 0), dx, 1)
            vals = np.roll(np.roll(out, dy, 0), dx, 1)
            take = shifted & ~grown & ~used
            acc[take] += vals[take]
            n[take] += 1
        new = n > 0
        out[new] = acc[new] / n[new, None]
        grown |= new
    rgba[:] = np.clip(np.rint(out), 0, 255).astype(np.uint8).tobytes()
    for v in report.values():
        v["behind"] = round(v["behind"] / v["texels"], 3) if v["texels"] else 0.0
    return report
