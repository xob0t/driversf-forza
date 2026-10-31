"""Wheels: roles and materials, tyre maps, the speed-blur disc, brake rotors, baked wheel sky
and axle fits."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from dsfforza.convert.forza2dsf.materials import TYRE
from dsfforza.formats import carbin

# --- wheels ------------------------------------------------------------------------
#
# The Forza wheel section is one wheel in its own frame: hub at the origin, spin axis x,
# tyre centred on x = 0, and the rim face (spokes, lug nuts, centre cap, blur discs) at -x,
# which is the car's left in Forza space. DSF wheel models face +x: the UPVH wheel table gives
# the +x wheels the identity and the -x wheels a 180-degree turn about y ((0, 1, 0, 0)), so one
# model serves both sides (vanilla 227 and 266: centre cap and spoke disc at x +0.05..+0.12). The
# Forza wheel is turned 180 degrees about y like the body (Placement), which puts the face at +x
# (unturned, all four wheels would show the back of the rim outwards). Vanilla wheels use the shared tyre texture (GLOBAL_Tyre, 512x512 DXT1: red is the grey
# level, green the gloss, blue 255), never Forza's tiling tyre/rim UVs; the tyre is unwrapped onto
# the texture's tread strip and sidewall ring like vanilla tyres (TyreMap; the "tyre" swatch is only
# the fallback). Forza's rims sample the car's own wheel.xds (media/wheels/<car>, port_car.
# extract_wheel): with textures, rims go through the car's atlas on the Chassis material
# (wheel_material, dsf_materials.Texturing.plan(wheels=...)). Without the texture each rim
# material gets a uniform swatch of the tyre texture: the Desanne's rim grey, a dark inner-rim
# grey, bright chrome (research/wheels.md); every rim came out plain silver that way.
# The brake rotor (brake_rotor_section) always takes swatches: the disc the uniform mid grey
# square (red 106, gloss 58: duller than the rims' 131 / 190), the hub the inner-rim dark.
ROTOR_DISC = "dsf_rotor_disc"  # Forza's rotor / brake / drum subsections: the friction faces
ROTOR_HUB = "dsf_rotor_hub"  # hub hats, studs and whatever else the rotor carbin holds
WHEEL_UV = {
    "tyre": (0.5, 0.0078),
    "rim": (0.906, 0.0078),
    "inner": (0.4375, 0.0547),
    "chrome": (0.117, 0.0078),
    ROTOR_DISC: (0.07, 0.57),
    ROTOR_HUB: (0.4375, 0.0547),
}


def wheel_role(material: str) -> str | None:
    """Which tyre-texture swatch a Forza wheel subsection gets; None = leave out (blur discs,
    shadows, alpha-cut emblems)."""
    if material in (ROTOR_DISC, ROTOR_HUB):
        return material
    n = material.lower()
    if "blur" in n or "shadow" in n or "emblem" in n or n.startswith("ghost"):
        return None
    if n.startswith(("tire", "tyre", "tread", "sidewall")):
        return "tyre"
    if "inner" in n:
        return "inner"
    if "chrome" in n:
        return "chrome"
    return "rim"


def wheel_material(textures, section, sub, shared: dict) -> bytes | None:
    """DSF material of a Forza wheel subsection; None = leave it out. With the car's
    wheel texture in the atlas (dsf_materials.Texturing.plan(wheels=...)), rims,
    caps and wheel emblems use the entry's Chassis material and their own
    artwork, as vanilla centre caps do (the wheel parts of 110 of the 140 four-wheeled
    vanilla vehicles mix GLOBAL_Tyre with 4 to 244 triangles on the vehicle's own
    Chassis material, in both entries); the tyre stays on the shared tyre material
    (stride 20, like vanilla wheels). Without it, every part is on the shared tyre
    with a uniform swatch per wheel_role (WHEEL_UV)."""
    tyre = shared.get("tyre") or TYRE
    if is_rotor(sub):  # brake_rotor_section: swatches on the tyre material, never the atlas
        return tyre
    if textures is not None:
        from dsfforza.convert import dsf_materials

        kind = textures.texturing.material(section, sub)
        if dsf_materials.is_atlas_kind(kind):  # Chassis, a gloss copy of Lights, the blur disc's copy
            return textures.material(section, sub, shared)
        if kind == "skip" and not is_blur_disc(sub):
            return None
    if is_blur_disc(sub):  # no blur picture in the atlas: the shared disc and its texture
        return shared.get("spoke_disc") or SPOKE_DISC
    return None if wheel_role(sub.material) is None else tyre


def is_blur_disc(sub) -> bool:
    """A subsection of wheel_blur_section's disc."""
    from dsfforza.convert import forza_materials as fm

    return sub.material == fm.WHEEL_BLUR_TECHNIQUE


def wheel_point(p, fit=(1.0, 1.0)) -> tuple[float, float, float]:
    """Forza wheel frame -> DSF wheel frame: from the tyre's centre (fit[2:5], CarDims.
    tyre_centre; the origin when not given), 180 degrees about y, then the axle's size
    (fit[:2] = (axial scale, radial scale), 1 for the model's own size)."""
    cx, cy, cz = fit[2:5] if len(fit) >= 5 else (0.0, 0.0, 0.0)
    return (-(p[0] - cx) * fit[0], (p[1] - cy) * fit[1], -(p[2] - cz) * fit[1])


def wheel_normal(n, fit=(1.0, 1.0)) -> tuple[float, float, float]:
    return (-n[0] / fit[0], n[1] / fit[1], -n[2] / fit[1])  # inverse transpose; encoding normalises


def wheel_lod(sections, lod: int) -> int:
    """The Forza wheel LOD to use for body LOD `lod`: `lod` itself if it has a tyre
    subsection, else the nearest LOD that has one (the finer on a tie). Some Forza wheels
    have a LOD made of other materials: RX-7 '97 and Civic Type R '04 LOD 1 and M3 E30 LOD 2
    are the whole wheel on 'body' plus an 'interior' lip ring, which came out as one
    rim-grey blob; the Golf R and DMC-12 have a wheel at LOD 1 only (no wheels in the full
    entry), the Durango and Express LOD 5 is one 'wheel_LOD5' mesh."""
    have = {sub.lod for s in sections for sub in s.subsections if sub.material.lower().startswith(("tire", "tyre"))}
    if not have or lod in have:
        return lod
    return min(have, key=lambda l: (abs(l - lod), l))


# Vanilla tyres (research/wheels.md, 30 hosts surveyed): GLOBAL_Tyre has no normal map, the
# tread and sidewall detail is all in the diffuse atlas. Tread: the fine tread strip, u
# across the tyre (0.505..0.75 with the shoulders), v one tile along the circumference
# (0.897..0.989), repeated round the tyre. Sidewalls (both): the lower half-ring centred on
# (0.5, 1.0), each 180 degrees of the tyre on the whole half-ring, the rim edge at ring
# radius ~0.33 (lettering band 0.33..0.39) and the tread edge at ~0.49.
TYRE_TREAD_UV = (0.507, 0.749, 0.897, 0.989)
TYRE_RING_UV = (0.5, 1.0, 0.33, 0.492)  # centre u, v; ring radius at the rim edge, at the tread edge


@dataclass
class TyreMap:
    """UVs for the tyre triangles of one wheel model (DSF wheel frame, after wheel_point):
    tread triangles onto the tread strip, tiled round the circumference, sidewall
    triangles (face normal within ~55 degrees of the axle) onto the sidewall ring, 180
    degrees of tyre per ring. Tile edges are snapped to the model's own vertex angles, so a
    triangle never spans two tiles (a seam inside it would smear the whole strip)."""

    r_in: float
    r_out: float
    x_min: float
    x_max: float
    tread_edges: list  # degrees, ascending, last = first + 360
    side_edges: list

    @staticmethod
    def build(tris: list) -> TyreMap | None:
        pts = [p for t in tris for p in t]
        if len(tris) < 8:
            return None
        rs = [math.hypot(p[1], p[2]) for p in pts]
        xs = [p[0] for p in pts]
        r_in, r_out = min(rs), max(rs)
        if r_out - r_in < 1e-3 or max(xs) - min(xs) < 1e-3:
            return None
        angles = sorted(
            {
                round(math.degrees(math.atan2(p[1], p[2])) % 360.0, 2)
                for p, r in zip(pts, rs)
                if r > r_out - 0.25 * (r_out - r_in)
            }
        )
        if len(angles) < 3:
            return None
        u0, u1, v0, v1 = TYRE_TREAD_UV
        target = math.degrees((v1 - v0) * (max(xs) - min(xs)) / (u1 - u0) / r_out)  # tile at texture scale

        def edges(count: int) -> list:
            ring = angles + [a + 360.0 for a in angles]
            out = [angles[0]]
            for k in range(1, max(1, count)):
                want = angles[0] + k * 360.0 / count
                snap = min(ring, key=lambda a: abs(a - want))
                if snap > out[-1] + 1e-6 and snap < angles[0] + 360.0 - 1e-6:
                    out.append(snap)
            return out + [angles[0] + 360.0]

        return TyreMap(r_in, r_out, min(xs), max(xs), edges(round(360.0 / target)), edges(2))

    def uvs(self, tri) -> list[tuple[float, float]]:
        a, b, c = tri
        e1 = [b[k] - a[k] for k in range(3)]
        e2 = [c[k] - a[k] for k in range(3)]
        n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        length = math.sqrt(sum(q * q for q in n)) or 1.0
        side = abs(n[0]) / length > 0.55
        edges = self.side_edges if side else self.tread_edges
        cy, cz = sum(p[1] for p in tri), sum(p[2] for p in tri)
        centre = math.degrees(math.atan2(cy, cz)) if abs(cy) + abs(cz) > 1e-9 else 0.0
        centre = edges[0] + (centre - edges[0]) % 360.0
        j = max(i for i in range(len(edges) - 1) if edges[i] <= centre + 1e-6)
        lo, hi = edges[j], edges[j + 1]
        ts = []
        for p in tri:  # unwrapped next to the triangle's centre angle
            ang = math.degrees(math.atan2(p[1], p[2]))
            ang += 360.0 * round((centre - ang) / 360.0)
            ts.append(min(1.0, max(0.0, (ang - lo) / (hi - lo))))
        out = []
        if side:
            cu, cv, ring_in, ring_out = TYRE_RING_UV
            for p, t in zip(tri, ts):
                f = min(1.0, max(0.0, (math.hypot(p[1], p[2]) - self.r_in) / (self.r_out - self.r_in)))
                radius = ring_in + f * (ring_out - ring_in)
                phi = math.pi * (1.0 - t)
                out.append((cu + radius * math.cos(phi), cv - radius * math.sin(phi)))
        else:
            u0, u1, v0, v1 = TYRE_TREAD_UV
            for p, t in zip(tri, ts):
                f = (p[0] - self.x_min) / (self.x_max - self.x_min)
                out.append((u0 + f * (u1 - u0), v0 + t * (v1 - v0)))
        return out


# --- wheel speed blur (research/wheels.md, "Speed blur") ---------------------------------
#
# Vanilla wheels fade a blurred disc in as they spin. Their wheel parts draw the sharp rim on
# GLOBAL_Wheel_Blur and a disc over the rim face on GLOBAL_Wheel_Blur_SpokeDisc (or _Disc),
# both alpha-blended, MODL flag 7, stride 20. The blur programs (SSIC GPCH 21 to 40) take the
# opacity from vertex semantic 9 byte 3 and the wheel's blur amount b (1.15 x
# g_v4PerPanelData[s9 byte 1].y, set by the engine per wheel): byte 3 = 0 fades out
# (1.15 - b), 1 stays opaque, 2 fades in (2.5 b - 0.15, opaque from b ~ 0.46). Of the 140
# four-wheeled vanilla vehicles, 116 put their sharp rim on byte 3 = 0, 93 add a SpokeDisc
# and 26 a Disc on byte 3 = 2; caps on the vehicle's Chassis material stay at byte 3 = 1.
# The SpokeDisc pixel shader draws grey (texel red) x g_vec4WheelBlurColour (the vehicle's
# SPSR third set), gloss from green, alpha = blue x opacity.
#
# A port's rims draw their own artwork through the car's Chassis material, which cannot fade
# (only the blur programs read byte 3, and they draw grey). So the port keeps its sharp rim
# and gets the disc only: a dish just in front of the rim face (the face's outer envelope,
# WHEEL_BLUR_LIFT out), out to the rim's edge, on a copy of GLOBAL_Wheel_Blur_SpokeDisc that
# samples the car's atlas, where the middle of Forza's own spinning-wheel pictures
# (wheel2.xds, fm.WHEEL_BLUR) goes, laid over a dark backdrop so the disc is opaque once
# faded in: the rim under it is hidden as vanilla's faded-out rim is. Without that picture
# (no wheel archive, --textures none) the disc uses the shared SpokeDisc and its own texture,
# mapped like vanilla discs (u = 0.5 - z / 2R, v = 0.5 - y / 2R in the DSF wheel frame).
# Forza's own LOD 5 wheel (503 of the 573 roster cars; 17 at LOD 3) has no sharp rim at all,
# only its blur_rim disc; there the disc is the rim and stays (byte 3 = 1).
SPOKE_DISC = bytes.fromhex("001262258aa7c428")  # GLOBAL_Wheel_Blur_SpokeDisc (SSIC)
SPOKE_DISC_LOD1 = bytes.fromhex("10122b9e53b14e2a")  # GLOBAL_Wheel_Blur_SpokeDisc_LOD1
WHEEL_BLUR_SECTION = "dsf_wheel_blur"
WHEEL_BLUR_FADE_IN = bytes((0, 0, 0, 2))  # s9: fades in with the wheel's speed
WHEEL_BLUR_ALWAYS = bytes((0, 0, 0, 1))  # s9: always drawn (a LOD whose rim is the disc)
WHEEL_BLUR_RINGS = 6  # rings of the dish, centre to rim edge (fewer where the face is flat)
WHEEL_BLUR_FLAT = 0.001  # m: a ring this close to the line through its neighbours is left out
WHEEL_BLUR_SEGMENTS = 24
WHEEL_BLUR_LIFT = 0.003  # m in front of the rim face
# Baked sky of the disc's vertices, as on vanilla discs (0.9 to 1.0; their rims 0.64). With the
# converted rim's 0.56 the faded-in disc came out darker than the rim it covers (370Z in game:
# mean grey of the wheel face 90 at rest, 70 at 60 mph; the vanilla 370Z's 103 and 146).
WHEEL_BLUR_SKY = 0.9
# Barycentric samples of each triangle for the rim face's envelope: corners, edge midpoints,
# centroid.
_FACE_SAMPLES = (
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.5, 0.5, 0.0),
    (0.0, 0.5, 0.5),
    (0.5, 0.0, 0.5),
    (1 / 3, 1 / 3, 1 / 3),
)


def wheel_has_rim(sections, lod: int, centre=(0.0, 0.0, 0.0)) -> bool:
    """Whether the wheel model has a sharp rim at `lod`, not just its blur disc (Forza's
    LOD 5 on 503 roster cars): a subsection the converter draws from the wheel texture
    (fm.classify_wheel; DOD_StealthTurbo_96's whole rim is `wheel_emblem`), not the inner
    rim, reaching half the radius of the wheel's non-tyre geometry (a lone centre cap is
    not a rim)."""
    from dsfforza.convert import forza_materials as fm

    rim = whole = 0.0
    for s in sections:
        for sub in s.subsections:
            n = sub.material.lower()
            role, family = fm.classify_wheel(n)
            if sub.lod != lod or role == fm.TYRE or is_blur_disc(sub):
                continue
            pool = s.vertices_for(sub)
            reach = max(
                (
                    math.hypot(pool[i].position[1] - centre[1], pool[i].position[2] - centre[2])
                    for t in sub.triangles()
                    for i in t
                ),
                default=0.0,
            )
            whole = max(whole, reach)
            if role == fm.TEXTURED and family == fm.WHEEL and "inner" not in n:
                rim = max(rim, reach)
    return rim > 0 and rim >= 0.5 * whole


def blur_picture_map(sections):
    """((u0, u_y, u_z), (v0, v_y, v_z)): Forza's planar map of its spinning-wheel pictures,
    fitted to the blur_rim vertices (Forza wheel frame; residual 0 on all but a few roster
    cars), or None (no blur_rim, or one that is not a plain planar map)."""
    import numpy as np

    subs = sorted(
        ((s, sub) for s in sections for sub in s.subsections if sub.lod > 0 and sub.material.lower() == "blur_rim"),
        key=lambda item: item[1].lod,
    )
    for section, sub in subs:
        pool = section.vertices_for(sub)
        ids = sorted({i for t in sub.triangles() for i in t})
        if len(ids) < 3:
            continue
        xo, xs, yo, ys = sub.uv_transform[:4]
        a = np.array([(1.0, pool[i].position[1], pool[i].position[2]) for i in ids])
        uv = np.array([(pool[i].uv0[0] * xs + xo, pool[i].uv0[1] * ys + yo) for i in ids])
        cu = np.linalg.lstsq(a, uv[:, 0], rcond=None)[0]
        cv = np.linalg.lstsq(a, uv[:, 1], rcond=None)[0]
        rms = float(np.sqrt(((a @ cu - uv[:, 0]) ** 2 + (a @ cv - uv[:, 1]) ** 2).mean()))
        if rms <= 0.01 and abs(cu[1] * cv[2] - cu[2] * cv[1]) > 1.0:
            return tuple(float(c) for c in cu), tuple(float(c) for c in cv)
    return None


def wheel_blur_section(sections, lods, centre=(0.0, 0.0, 0.0)) -> carbin.Section | None:
    """The speed-blur disc of the Forza wheel model (Forza wheel frame, like the model):
    for each Forza wheel LOD in `lods`, a dish of WHEEL_BLUR_RINGS x WHEEL_BLUR_SEGMENTS
    quads round the axle through `centre` (CarDims.tyre_centre), WHEEL_BLUR_LIFT in front
    of the rim face (-x; per ring the outermost point of every non-tyre subsection within
    a ring of it, so the dish never cuts a spoke), out to the rim's edge (the rim's largest
    radius, at most the tyre's smallest). One subsection per LOD, technique
    fm.WHEEL_BLUR_TECHNIQUE: UV0 in Forza's blur picture (blur_picture_map, else the
    vanilla disc map), UV1 the vanilla disc map (the shared SpokeDisc's texture), and
    `always` when the LOD has no sharp rim (wheel_has_rim). None without a rim."""
    import numpy as np

    from dsfforza.convert import forza_materials as fm

    picture = blur_picture_map(sections)
    _, cy, cz = centre  # the face's x is the model's own
    weights = np.array(_FACE_SAMPLES)
    verts: list[carbin.Vertex] = []
    subs = []
    for lod in sorted(set(lods)):
        pts, tyre_r = [], []
        for s in sections:
            for sub in s.subsections:
                n = sub.material.lower()
                if sub.lod != lod or "shadow" in n or n.startswith("ghost"):
                    continue
                pool = s.vertices_for(sub)
                tris = np.array([[pool[i].position for i in t] for t in sub.triangles()], dtype=np.float64)
                if not len(tris):
                    continue
                if wheel_role(sub.material) == "tyre":
                    tr = np.hypot(tris[..., 1] - cy, tris[..., 2] - cz)
                    # its inner radius; DOD_ViperSRT10_03's tyre mesh also closes the wheel at r 0
                    tyre_r.append(float(tr[tr > 0.5 * tr.max()].min()))
                    continue
                pts.append(np.einsum("kc,tcd->tkd", weights, tris).reshape(-1, 3))
        if not pts:
            continue
        p = np.concatenate(pts)
        r = np.hypot(p[:, 1] - cy, p[:, 2] - cz)
        edge = float(r.max())
        if tyre_r:
            edge = min(edge, *tyre_r)
        if edge < 0.05:
            continue
        rings = np.linspace(0.0, edge, WHEEL_BLUR_RINGS + 1)
        step = rings[1] - rings[0]
        face = []
        for rk in rings:
            near = (r >= rk - step) & (r <= rk + step)
            face.append(float(p[near, 0].min()) if near.any() else None)
        known = [k for k, x in enumerate(face) if x is not None]
        face = [float(x) for x in np.interp(range(len(face)), known, [face[k] for k in known])]
        keep = list(range(len(rings)))
        while len(keep) > 2:  # drop the ring that bends the profile least, while under WHEEL_BLUR_FLAT
            bend = [
                abs(face[k] - np.interp(rings[k], [rings[a], rings[b]], [face[a], face[b]]))
                for a, k, b in zip(keep, keep[1:], keep[2:])
            ]
            n = int(np.argmin(bend))
            if bend[n] >= WHEEL_BLUR_FLAT:
                break
            del keep[n + 1]
        rings, face = rings[keep], [face[k] for k in keep]
        base = len(verts)

        def add(x, y, z, edge=edge):
            dy, dz = y - cy, z - cz
            plain = (0.5 + dz / (2 * edge), 0.5 - dy / (2 * edge))  # the vanilla map, before the turn
            uv0 = plain
            if picture is not None:
                (u0, uy, uz), (v0, vy, vz) = picture
                uv0 = (u0 + uy * y + uz * z, v0 + vy * y + vz * z)
            uv0 = tuple(min(1.0, max(0.0, c)) for c in uv0)  # the picture ends at the rim's edge
            verts.append(carbin.Vertex((x, y, z), uv0, plain, (-1.0, 0.0, 0.0)))

        add(face[0] - WHEEL_BLUR_LIFT, cy, cz)
        count = WHEEL_BLUR_SEGMENTS
        for k in range(1, len(rings)):
            for j in range(count):
                a = 2 * math.pi * j / count
                add(face[k] - WHEEL_BLUR_LIFT, cy + rings[k] * math.sin(a), cz + rings[k] * math.cos(a))
        idx = []
        for j in range(count):  # the centre fan
            idx += [base, base + 1 + j, base + 1 + (j + 1) % count]
        for k in range(1, len(rings) - 1):
            inner, outer = base + 1 + (k - 1) * count, base + 1 + k * count
            for j in range(count):
                a, b = inner + j, inner + (j + 1) % count
                c, d = outer + j, outer + (j + 1) % count
                idx += [a, c, d, a, d, b]
        sub = carbin.SubSection(fm.WHEEL_BLUR_TECHNIQUE, lod, 4, (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0), idx)
        sub.always = not wheel_has_rim(sections, lod, centre)
        subs.append(sub)
    if not subs:
        return None
    lo = tuple(min(v.position[k] for v in verts) for k in range(3))
    hi = tuple(max(v.position[k] for v in verts) for k in range(3))
    version = sections[0].version if sections else 0
    return carbin.Section(WHEEL_BLUR_SECTION, (0.0, 0.0, 0.0), lo, hi, verts, [], subs, version)


# The disc's brightness against the wheel it covers (research/wheels.md, "Blur disc
# brightness", #407). Faded in, the disc stands for the spinning wheel, so it should keep the
# wheel's mean brightness. Forza's picture alone doesn't give it: FOR_FocusRS500_10's rim
# texels are near black (about 4, PaintScale 7.5) but its wheel2.xds averages 77 grey where it
# covers; times the rim's 7.5 it saturated white. And the mid-grey backdrop between the
# blurred spokes makes the disc of a black rim grey. wheel_face_brightness measures both as
# drawn, seen from outside out to the disc's edge on a WHEEL_FACE_STEP grid: the face is the
# frontmost (most outboard) non-tyre triangle per cell, its texel (the atlas's, PaintScale
# applied) times its baked sky (wheel_skies); a cell with nothing in front shows the inside of
# the wheel, WHEEL_FACE_GAP (the brake rotor's grey, 106, at the 0.15 sky vanilla bakes inside
# the barrel). The disc is its texel times WHEEL_BLUR_SKY.
WHEEL_FACE_STEP = 0.003  # m
WHEEL_FACE_GAP = 0.15 * 106
LUMA = (0.2126, 0.7152, 0.0722)


def _face_raster(tris, depth, colours, edge: float, cy: float, cz: float):
    """Per WHEEL_FACE_STEP cell of the square round the axle (face plane: y, z), the colour of
    the triangle nearest -x (the outboard side in Forza's frame) at its centre, NaN where no
    triangle covers it; and the mask of cells within `edge` of the axle."""
    import numpy as np

    n = max(1, int(math.ceil(2 * edge / WHEEL_FACE_STEP)))
    origin = np.array([cy - edge, cz - edge])
    grid = origin + (np.arange(n) + 0.5)[:, None] * WHEEL_FACE_STEP
    zbuf = np.full((n, n), np.inf)
    out = np.full((n, n, 3), np.nan)
    for t, d, v in zip(tris, depth, colours):
        lo = np.floor((t.min(0) - origin) / WHEEL_FACE_STEP).astype(int).clip(0, n - 1)
        hi = np.ceil((t.max(0) - origin) / WHEEL_FACE_STEP).astype(int).clip(0, n - 1)
        a, b, c = t
        den = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(den) < 1e-12:
            continue
        y, z = np.meshgrid(grid[lo[0] : hi[0] + 1, 0], grid[lo[1] : hi[1] + 1, 1], indexing="ij")
        w0 = ((b[1] - c[1]) * (y - c[0]) + (c[0] - b[0]) * (z - c[1])) / den
        w1 = ((c[1] - a[1]) * (y - c[0]) + (a[0] - c[0]) * (z - c[1])) / den
        w2 = 1 - w0 - w1
        dd = w0 * d[0] + w1 * d[1] + w2 * d[2]
        zs = zbuf[lo[0] : hi[0] + 1, lo[1] : hi[1] + 1]
        win = (w0 >= 0) & (w1 >= 0) & (w2 >= 0) & (dd < zs)
        if not win.any():
            continue
        zs[win] = dd[win]
        col = w0[..., None] * v[0] + w1[..., None] * v[1] + w2[..., None] * v[2]
        out[lo[0] : hi[0] + 1, lo[1] : hi[1] + 1][win] = col[win]
    y, z = np.meshgrid(grid[:, 0], grid[:, 1], indexing="ij")
    return out, np.hypot(y - cy, z - cz) <= edge


def wheel_face_brightness(sections, lod: int, centre, texel, disc_texel) -> tuple[float, float] | None:
    """(the wheel face's, the blur disc's) mean drawn luminance at Forza wheel LOD `lod` (see
    above), or None without a disc there. texel(section, sub, uvs (n, 2)) -> RGB (n, 3): the
    rim texels as the atlas holds them; disc_texel(uvs) -> RGB: the disc's converted texels."""
    import numpy as np

    disc = [
        (s, sub) for s in sections for sub in s.subsections if sub.lod == lod and is_blur_disc(sub) and sub.triangles()
    ]
    if not disc:
        return None
    _, cy, cz = centre
    section, _ = disc[0]
    edge = max(math.hypot(v.position[1] - cy, v.position[2] - cz) for v in section.vertices_for(disc[0][1]))
    skies = wheel_skies([s for s in sections if not any(is_blur_disc(sub) for sub in s.subsections)], lod)
    from dsfforza.convert import forza_materials as fm

    tris, depth, cols = [], [], []
    for s in sections:
        for sub in s.subsections:
            if sub.lod != lod or is_blur_disc(sub) or fm.classify_wheel(sub.material)[0] in (fm.SKIP, fm.TYRE):
                continue
            pool = s.vertices_for(sub)
            for t in sub.triangles():
                p = np.array([pool[i].position for i in t], dtype=np.float64)
                rgb = np.asarray(texel(s, sub, np.array([pool[i].uv0 for i in t], dtype=np.float64)), dtype=np.float64)
                sky = np.array([skies.get((id(pool), i), WHEEL_SKY_OPEN) for i in t])[:, None]
                tris.append(p[:, 1:])
                depth.append(p[:, 0])
                cols.append(rgb * sky)
    face, inside = _face_raster(tris, depth, cols, edge, cy, cz)
    face = np.where(np.isnan(face), WHEEL_FACE_GAP, face)
    tris, depth, cols = [], [], []
    for s, sub in disc:
        pool = s.vertices_for(sub)
        for t in sub.triangles():
            p = np.array([pool[i].position for i in t], dtype=np.float64)
            tris.append(p[:, 1:])
            depth.append(p[:, 0])
            uvs = np.array([pool[i].uv0 for i in t], dtype=np.float64)
            cols.append(np.asarray(disc_texel(uvs), dtype=np.float64) * WHEEL_BLUR_SKY)
    drawn, _ = _face_raster(tris, depth, cols, edge, cy, cz)
    covered = inside & ~np.isnan(drawn[..., 0])
    if not covered.any():
        return None
    luma = np.array(LUMA)
    return float(face[inside].mean(0) @ luma), float(drawn[covered].mean(0) @ luma)


# Brake rotors (research/wheels.md, "Brake rotors"). Forza draws the disc and hub behind the
# spokes from separate carbins, <car>_rotor{LF,RF,LR,RR}_LOD0.carbin (LOD 0 only, in the hub's
# frame: spin axis x, the hub face at -x like the wheel's rim face; 567 of the 573 roster cars
# have them); vanilla DSF wheels have none, so through open spokes a port showed the dark inside
# of its barrel. The rotor turns with the wheel, so it goes into the wheel part: the front rotor
# into the front (or only) wheel part, the rear rotor into a rear one. One model serves both
# sides there, so the left rotors stand for the right ones (their mirror images). It keeps
# Forza's own size (the wheel part's fit scales the wheel model, not the rotor) and its place
# against the tyre's centre. It is drawn on the shared tyre material with plain swatches of the
# tyre texture (Forza's shared rotor0..4.xds aren't in the car's atlas): the friction ring
# (triangles beyond ROTOR_RING of the rotor's radius; Forza's technique names don't tell, 201
# rotors are all `hub` or `misc`) mid grey, the hat and hub dark. It takes the wheel's baked sky
# (wheel_skies, with the rotor among the occluders), so it lies in the wheel's shadow like the
# barrel. A rotor of more than BRAKE_ROTOR_MAX_TRIANGLES (drums, drilled discs: 145 of the 1146
# front and rear rotors, up to 9410 triangles) is replaced by a plain disc of its size
# (plain_rotor). Only on each entry's finest LOD (the exterior, the full entry's mask 4). The calipers, which
# don't turn, are left out.
ROTOR_CARBINS = {"front": "rotorLF_LOD0.carbin", "rear": "rotorLR_LOD0.carbin"}  # port_car.extract_brakes
BRAKE_ROTOR_SECTION = "dsf_brake_rotor"
BRAKE_ROTOR_MAX_TRIANGLES = 2000
ROTOR_RING = 0.6  # share of the rotor's radius where the friction ring starts
PLAIN_ROTOR_SEGMENTS = 32


def _rotor_triangles(rotor) -> list:
    """[(3 positions, 3 normals)] of the rotor carbin's drawn triangles (shadows and blur
    left out), in the rotor's own frame."""
    out = []
    for section in rotor:
        for sub in section.subsections:
            if "shadow" in sub.material.lower() or "blur" in sub.material.lower():
                continue
            pool = section.vertices_for(sub)
            for tri in sub.triangles():
                out.append(([pool[i].position for i in tri], [pool[i].normal for i in tri]))
    return out


def plain_rotor(triangles) -> list:
    """A plain stand-in of the rotor's size: the friction ring (both faces, its rim and its inner
    edge) over the ring triangles' axial extent, and a hat (a cylinder and its face) over the
    rest's, PLAIN_ROTOR_SEGMENTS round. Same format as _rotor_triangles."""
    radius = lambda p: math.hypot(p[1], p[2])  # noqa: E731
    pts = [p for ps, _ in triangles for p in ps]
    outer = max(radius(p) for p in pts)
    ring = [p for ps, _ in triangles if radius(_centroid(ps)) >= ROTOR_RING * outer for p in ps]
    hat = [p for ps, _ in triangles if radius(_centroid(ps)) < ROTOR_RING * outer for p in ps] or ring
    r0, r1 = ROTOR_RING * outer, outer
    rx = (
        (min(p[0] for p in ring), max(p[0] for p in ring)) if ring else (min(p[0] for p in pts), max(p[0] for p in pts))
    )
    hx = (min(p[0] for p in hat), max(p[0] for p in hat))
    hub_face = hx[0] if hx[0] < rx[0] else hx[1]  # the hat stands out of the ring towards the hub face
    ring_face = rx[0] if hub_face < rx[0] else rx[1]
    rh = 0.85 * r0
    n = PLAIN_ROTOR_SEGMENTS
    ang = [2 * math.pi * k / n for k in range(n + 1)]
    out = []

    def tri(a, b, c, normal):
        u = [b[k] - a[k] for k in range(3)]
        v = [c[k] - a[k] for k in range(3)]
        if (
            math.dist(
                (0.0, 0.0, 0.0), (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            )
            > 1e-12
        ):
            out.append(([a, b, c], [normal] * 3))  # no flat ones (a ring of no thickness has no rim)

    def quad(a, b, c, d, normal):
        tri(a, b, c, normal)
        tri(a, c, d, normal)

    def at(x, r, t):
        return (x, r * math.cos(t), r * math.sin(t))

    for k in range(n):
        t0, t1 = ang[k], ang[k + 1]
        tm = (t0 + t1) / 2
        radial = (0.0, math.cos(tm), math.sin(tm))
        for x, nx in ((rx[0], -1.0), (rx[1], 1.0)):  # the ring's two faces
            quad(at(x, r0, t0), at(x, r1, t0), at(x, r1, t1), at(x, r0, t1), (nx, 0.0, 0.0))
        quad(at(rx[0], r1, t0), at(rx[1], r1, t0), at(rx[1], r1, t1), at(rx[0], r1, t1), radial)  # rim
        inward = (0.0, -radial[1], -radial[2])
        quad(at(rx[0], r0, t0), at(rx[1], r0, t0), at(rx[1], r0, t1), at(rx[0], r0, t1), inward)
        quad(at(ring_face, rh, t0), at(hub_face, rh, t0), at(hub_face, rh, t1), at(ring_face, rh, t1), radial)
        face = (-1.0 if hub_face < ring_face else 1.0, 0.0, 0.0)
        tri(at(hub_face, 0.0, t0), at(hub_face, rh, t0), at(hub_face, rh, t1), face)
    return out


def _winding(triangles) -> bool:
    """Whether most of `triangles` wind counter-clockwise about their vertex normals."""
    votes = 0
    for ps, ns in triangles:
        a, b, c = ps
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        cross = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        n = [sum(n[k] for n in ns) for k in range(3)]
        votes += 1 if sum(cross[k] * n[k] for k in range(3)) > 0 else -1
    return votes >= 0


def _centroid(ps):
    return tuple(sum(p[k] for p in ps) / 3 for k in range(3))


def brake_rotor_section(rotor, lod: int, fit=(1.0, 1.0)) -> carbin.Section | None:
    """The rotor carbin's sections as one wheel section at Forza `lod`, its vertices placed so
    that wheel_point(p, fit) lands them at Forza's own size: the wheel part's fit scales the
    wheel model, not the rotor. Subsections ROTOR_DISC (the friction ring, ROTOR_RING) and
    ROTOR_HUB; plain_rotor when the rotor has more than BRAKE_ROTOR_MAX_TRIANGLES. The
    section's `plain` says which. None without triangles."""
    triangles = _rotor_triangles(rotor)
    if not triangles:
        return None
    plain = len(triangles) > BRAKE_ROTOR_MAX_TRIANGLES
    if plain:  # wound like the carbin's own triangles
        front = _winding(triangles)
        triangles = [
            (ps if _winding([(ps, ns)]) == front else [ps[0], ps[2], ps[1]], ns) for ps, ns in plain_rotor(triangles)
        ]
    ax, rad = fit[0], fit[1]
    cx, cy, cz = fit[2:5] if len(fit) >= 5 else (0.0, 0.0, 0.0)
    outer = max(math.hypot(p[1], p[2]) for ps, _ in triangles for p in ps)
    verts: list[carbin.Vertex] = []
    remap: dict = {}
    indices: dict[str, list[int]] = {}
    for ps, ns in triangles:
        c = _centroid(ps)
        role = ROTOR_DISC if math.hypot(c[1], c[2]) >= ROTOR_RING * outer else ROTOR_HUB
        out = indices.setdefault(role, [])
        for (x, y, z), (nx, ny, nz) in zip(ps, ns):
            key = (x, y, z, nx, ny, nz)
            if key not in remap:
                remap[key] = len(verts)
                verts.append(
                    carbin.Vertex(
                        (cx + (x - cx) / ax, cy + y / rad, cz + z / rad),
                        (0.0, 0.0),
                        (0.0, 0.0),
                        (nx * ax, ny * rad, nz * rad),
                    )
                )
            out.append(remap[key])
    identity = (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0)
    subs = [carbin.SubSection(role, lod, 4, identity, idx) for role, idx in sorted(indices.items())]
    lo = tuple(min(v.position[k] for v in verts) for k in range(3))
    hi = tuple(max(v.position[k] for v in verts) for k in range(3))
    section = carbin.Section(BRAKE_ROTOR_SECTION, (0.0, 0.0, 0.0), lo, hi, verts, verts, subs, rotor[0].version)
    section.plain = plain
    return section


def is_rotor(sub) -> bool:
    """A subsection of brake_rotor_section's rotor."""
    return sub.material in (ROTOR_DISC, ROTOR_HUB)


# Baked sky term (s0.w) of the wheel model. Forza's wheel vertices carry no AO byte
# (all 0 on the 40 testdrive cars), so vertex_sky gave every wheel vertex
# DEFAULT_AO x the hemisphere factor of its wheel-local normal: about 0.42 on the rim
# face, the back of the spokes and the inside of the barrel alike. Vanilla wheels bake
# their own occlusion (wheel parts of the 140 four-wheeled vanilla vehicles, exterior
# entry, median of per-vehicle medians): outward faces 0.64, inward faces 0.02, the
# barrel's inside 0.15, the tread 0.29, with no up/down term (the wheel turns). With
# the flat 0.42 the inside of a wheel lit like its face: the 205 T16's white rim read
# as a plain disc, its holes showing the white barrel behind (research/wheels.md,
# "Wheel shading"). wheel_sky gives each vertex WHEEL_SKY_OPEN x the share of a
# cosine-weighted hemisphere that escapes: rays into the wheel's own triangles are
# blocked (marched through a WHEEL_AO_VOXEL grid of them; hits closer than
# WHEEL_AO_SKIP voxels are the vertex's own surface), rays heading inboard (-x
# in the DSF wheel frame) end on the car, and rays leaving past the tyre's radius
# count WHEEL_SKY_SIDE (the arch above, the road below): the tread comes out at
# about 0.3 like vanilla instead of 0.45.
WHEEL_SKY_OPEN = 0.64
WHEEL_SKY_SIDE = 0.5
WHEEL_AO_RAYS = 24
WHEEL_AO_VOXEL = 0.003  # metres
WHEEL_AO_SKIP = 3  # voxels
WHEEL_AO_BLOCK = 16  # ray steps at a time


def _hemisphere(count: int):
    """Cosine-weighted unit vectors about +z (Hammersley points)."""
    import numpy as np

    k = np.arange(count)
    u = (k + 0.5) / count
    v = np.array([int(f"{n:016b}"[::-1], 2) / 65536.0 for n in k])
    r, phi = np.sqrt(u), 2 * np.pi * v
    return np.stack([r * np.cos(phi), r * np.sin(phi), np.sqrt(1 - u)], 1)


def wheel_sky(points, normals, triangles) -> list[float]:
    """s0.w for wheel vertices (DSF wheel frame: outboard +x): WHEEL_SKY_OPEN x the
    unoccluded share of each vertex's hemisphere, the wheel's own `triangles`
    ((n, 3, 3) positions) as occluders and the inboard half-space closed."""
    import numpy as np

    pts = np.asarray(points, float).reshape(-1, 3)
    if not len(pts):
        return []
    nrm = np.asarray(normals, float).reshape(-1, 3)
    nrm = nrm / np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
    tri = np.asarray(triangles, float).reshape(-1, 3, 3)
    h = WHEEL_AO_VOXEL
    corners = np.concatenate([tri.reshape(-1, 3), pts])
    lo = corners.min(0) - 2 * h
    dims = np.ceil((corners.max(0) + 2 * h - lo) / h).astype(int) + 1
    grid = np.zeros(dims, bool)
    solid = grid.reshape(-1)
    if len(tri):  # each triangle sampled at half-voxel spacing
        a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
        longest = np.max(
            [np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1)], 0
        )
        steps = np.maximum(1, np.ceil(longest / (0.5 * h)).astype(int))
        for n in np.unique(steps):
            sel = steps == n
            i, j = np.mgrid[0 : n + 1, 0 : n + 1]
            keep = i + j <= n
            fu, fv = (i[keep] / n)[None, :], (j[keep] / n)[None, :]
            # Per axis, a + fu (b - a) + fv (c - a) and its voxel, in place.
            cell = None
            for x in range(3):
                ax, bx, cx = a[sel, x][:, None], b[sel, x][:, None], c[sel, x][:, None]
                s = fu * (bx - ax)
                s += ax
                s += fv * (cx - ax)
                s -= lo[x]
                s /= h
                k = np.floor(s, out=s).astype(np.int64)
                cell = k if cell is None else cell * dims[x] + k
            solid[cell.reshape(-1)] = True
    hemi = _hemisphere(WHEEL_AO_RAYS)
    helper = np.where(np.abs(nrm[:, :1]) < 0.9, [[1.0, 0.0, 0.0]], [[0.0, 1.0, 0.0]])
    t = np.cross(nrm, helper)
    t /= np.linalg.norm(t, axis=1, keepdims=True)
    bt = np.cross(nrm, t)
    dirs = (
        hemi[None, :, :1] * t[:, None] + hemi[None, :, 1:2] * bt[:, None] + hemi[None, :, 2:] * nrm[:, None]
    ).reshape(-1, 3)
    pos = np.repeat(pts + nrm * h, WHEEL_AO_RAYS, 0)
    light = np.zeros(len(dirs))  # per ray: 1 out of the wheel's face, WHEEL_SKY_SIDE out of its side
    # Each ray steps h along its direction until it leaves the grid, or (once
    # WHEEL_AO_SKIP voxels out) enters a wheel voxel, for at most the grid's
    # diagonal; inboard rays are the car's (closed). The steps go WHEEL_AO_BLOCK
    # at a time: add.accumulate adds `step` in order, as one step after another.
    ticks, travelled, length = [], 0.0, float(np.linalg.norm(dims * h))
    while travelled < length:
        ticks.append(travelled >= WHEEL_AO_SKIP * h)  # per step: hits count
        travelled += h
    ticks = np.array(ticks, bool)
    idx = np.flatnonzero(dirs[:, 0] >= 0)
    pos = [np.ascontiguousarray(pos[idx, x]) for x in range(3)]
    step = [dirs[idx, x] * h for x in range(3)]
    for s0 in range(0, len(ticks), WHEEL_AO_BLOCK):
        if not len(idx):
            break
        m = min(WHEEL_AO_BLOCK, len(ticks) - s0)
        inside, cell, front = np.ones((len(idx), m), bool), np.zeros((len(idx), m)), None
        for x in range(3):  # per axis: the positions of the next m steps, their voxel
            path = np.empty((len(idx), m))
            path[:, 0], path[:, 1:] = pos[x], step[x][:, None]
            np.add.accumulate(path, axis=1, out=path)
            pos[x] = path[:, m - 1] + step[x]
            path -= lo[x]
            path /= h
            np.floor(path, out=path)
            inside &= (path >= 0) & (path < dims[x])
            if x == 0:
                front = path >= dims[0]
            cell *= dims[x]
            cell += path
        hit = solid[np.where(inside, cell, 0).astype(np.int64)]
        end = ~inside | (hit & inside & ticks[None, s0 : s0 + m])
        stops = end.any(1)
        rays = np.flatnonzero(stops)
        at = end.argmax(1)[rays]
        out = ~inside[rays, at]
        light[idx[rays[out]]] = np.where(front[rays[out], at[out]], 1.0, WHEEL_SKY_SIDE)
        go = ~stops
        idx, pos, step = idx[go], [c[go] for c in pos], [c[go] for c in step]
    vis = light.reshape(-1, WHEEL_AO_RAYS).mean(1)
    return [round(float(WHEEL_SKY_OPEN * v), 4) for v in vis]


_wheel_sky_cache: dict = {}


def wheel_skies(sections, lod: int, fit=(1.0, 1.0)) -> dict:
    """{(id(pool), index): s0.w} for the vertices of the wheel model's drawn
    subsections at `lod` (wheel_sky over those triangles, in the fitted frame).
    Cached per (sections, lod, fit): the full and exterior entries draw the same LODs."""
    from dsfforza.convert import forza_materials as fm

    key = (tuple(id(s) for s in sections), lod, tuple(fit))
    hit = _wheel_sky_cache.get(key)
    if hit is not None and all(a is b for a, b in zip(hit[0], sections)):
        return hit[1]
    tris, verts = [], {}
    for section in sections:
        for sub in section.subsections:
            if sub.lod != lod or fm.classify_wheel(sub.material)[0] == fm.SKIP or is_blur_disc(sub):
                continue
            pool = section.vertices_for(sub)
            for t in sub.triangles():
                tris.append([wheel_point(pool[i].position, fit) for i in t])
                for i in t:
                    verts.setdefault((id(pool), i), pool[i])
    keys = list(verts)
    skies = wheel_sky(
        [wheel_point(verts[k].position, fit) for k in keys], [wheel_normal(verts[k].normal, fit) for k in keys], tris
    )
    out = dict(zip(keys, skies))
    if len(_wheel_sky_cache) > 32:
        _wheel_sky_cache.clear()
    _wheel_sky_cache[key] = (list(sections), out)
    return out


def axle_tyres(name: str, gamedbs) -> dict | None:
    """{"front"|"rear": (diameter m, width m)} from gamedb.slt Data_Car tyre sizes."""
    import sqlite3

    for db in gamedbs or []:
        if not db or not Path(db).exists():
            continue
        con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT FrontTireWidthMM, FrontTireAspect, FrontWheelDiameterIN, RearTireWidthMM, "
                "RearTireAspect, RearWheelDiameterIN FROM Data_Car WHERE lower(MediaName) = lower(?)",
                (name,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            con.close()
        if row and all(row):
            fw, fa, fd, rw, ra, rd = row
            return {
                "front": ((fd * 25.4 + 2 * fw * fa / 100) / 1000, fw / 1000),
                "rear": ((rd * 25.4 + 2 * rw * ra / 100) / 1000, rw / 1000),
            }
    return None


def axle_fits(radius: float, width: float, tyres: dict | None) -> dict[str, tuple[float, float]]:
    """{"front"|"rear": (axial scale, radial scale)} that bring the wheel model (radius,
    width) to each axle's Data_Car tyre (axle_tyres), as Forza draws it; (1, 1) without
    tyre sizes. Clamped to 0.6..1.6 across and 0.77..1.3 radially."""
    fits = {"front": (1.0, 1.0), "rear": (1.0, 1.0)}
    for axle in ("front", "rear") if tyres else ():
        diameter, tyre_width = tyres[axle]
        fits[axle] = (min(1.6, max(0.6, tyre_width / width)), min(1.3, max(0.77, diameter / 2 / radius)))
    return fits
