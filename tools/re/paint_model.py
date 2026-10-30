"""DSF car paint from its palette entry to the screen (research/paint-re.md), and checks of
the model against frames dumped in game by tools/re/paintprobe.

The model is the paint pixel shader of the player car's clean exterior (GPCH 26,
GLOBAL_Chassis_LOD1 pass 0; traffic draws GPCH 14, the same paint maths with a cube map),
per pixel, in linear light:

    f      = bias + (1 - bias) x max(1e-4, 1 - sat(N.E))^power        (g_fresnel*)
    paint  = Tint2 + f x (Tint3 - Tint2)
    A      = tex.r x (1 + tex.b x (paint - 1))        tex = the atlas paint block (255, 198, 255)
    amb    = (lm.x x 0.4 + 0.6) x ao x (Lower x N.(-ambDir) + Upper)
    D      = shadow x (1 + occl x (ao - 1))           (sun: PSSM near, light map far)
    diff   = max(N.(-L), 0) x sunColour x D
    s      = max(R.E, 1e-4), R = reflect(specDir, N)
    spec   = D x tex.g x specColour x (s^1500 + s2i x s^s2p x tex.b x A)
    refl   = sat(tex.g x max(0.025, sat(1 - N.E)^cfp - 0.5) x reflAmount
                 x (D x sm.x + ambAmount x sm.y + sm.z) x texMul2.z x env(R) x boost)
    HDR    = min((amb + diff) x A + spec + refl, 6)                  then fog, final tint

and the post chain of research/lights-re.md (exposure, Reinhard on luminance with white W)
followed by the colour grade fitted across channels here (paint_grade.json) and sRGB: `screen`.

    uv run python tools/re/paint_model.py curve --sid 126 [--index 0]   one entry by angle
    uv run python tools/re/paint_model.py log PROBE.log                   the probe's constants
    uv run python tools/re/paint_model.py frames --log PROBE.log --dumps DIR --archive SP \\
        --sids 126,224,... [--out DIR]                                    model against frames
    uv run python tools/re/paint_model.py grade --dumps DIR               fit paint_grade.json
    uv run python tools/re/paint_model.py target '#c00000,#999999'        albedo for a swatch
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lamp_model as lm

from dsfforza import REPO

# --- the game's numbers ----------------------------------------------------------------

# The shared atlas's paint block (GLOBAL_Chassis*/g_texDiffuseMap 00122b73d1fa1227, DXT1, the
# 50 x 50 texels round PAINT_TEXEL_UV) decodes to (255, 198, 255), sampled with sRGB decoding
# (paintprobe: D3DSAMP_SRGBTEXTURE 1 on s0): (1, 0.565, 1). So A = paint exactly, and the
# specular and reflection mask tex.g is 0.565.
TEXEL = np.array([1.0, (198 / 255 + 0.055) ** 2.4 / 1.055**2.4, 1.0])
SPECULAR_POWER = 1500.0  # GLOBAL_Chassis(_LOD1) g_hSpecularPower
CUBEMAP_FRESNEL_MIN = 0.025  # g_fCubemapFresnelMin
REFLECTION_FRESNEL_BIAS = -0.5  # g_hReflectionFresnelBias
DIRECTIONAL_OCCLUSION = 0.66  # g_vDirectionalOcclusionAmount.x
LUMINANCE_BOOST = {"exterior": 1.0, "full": 1.2}  # g_hCubemapLuminanceBoost: _LOD1, full entry

# ClearDay (moods.lua) as GPCH 26 receives it (paintprobe, run vm1-20261030-152849): the sun and
# the vehicle specular as in the mood; the ambient as the hemisphere's half difference and mean,
# (upper - lower) / 2 and (upper + lower) / 2, round the sky axis amb_dir; the reflection times
# vehEnvmapmultiplier (g_fTexMul2.z); the shadow modifier and the post chain's constants.
CLEAR_DAY = {
    "sun": np.array([3.0, 2.922901, 2.553755]),
    "sun_dir": np.array([-0.25, -0.76, 0.6]),  # the way the light travels (world), normalised in the mood
    "spec": np.array([40.0, 39.12528, 30.19408]),  # vehSpecularLightColour
    "spec_dir": np.array([-0.384, -0.768, 0.512]),  # vehSpecularLightDirection, normalised
    "amb_lower": np.array([0.1474, 0.1702, 0.2]),
    "amb_upper": np.array([0.4541, 0.5171, 0.6]),
    "amb_dir": np.array([-0.25, -0.76, -0.6]),
    "shadow_mod": np.array([0.638, 0.112, 0.25]),
    "tex_mul2_z": 3.0,
    "LC": (0.0, 2.0, -1.0, 0.3),
    "MG": 1.5,
    "W": 5.0,
}

REC709 = np.array([0.2125, 0.7154, 0.0721])


def unit(v) -> np.ndarray:
    v = np.asarray(v, np.float64)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


# --- the palette ---------------------------------------------------------------------------


def palette(archive: Path, sid: int) -> list[dict]:
    """A vehicle's palette entries as {name: value} (paint.spsp_params)."""
    from dsfforza.convert import paint
    from dsfforza.formats import vehicles
    from dsfforza.formats.chnk import read_root
    from dsfforza.formats.vehicle_mesh import payload

    with Path(archive).open("rb") as s:
        entries = read_root(s).entries
        ssic = vehicles.find(entries, "SSIC")
        records = vehicles.parse_sslp(payload(s, vehicles.find(vehicles.find(ssic.children, "NONR").children, "SSLP")))
        for rsid, index, kind in records:
            if kind == 0 and rsid == sid:
                spss = paint.palette_set(s, entries[index])
                return [paint.spsp_params(s, p) for p in spss.children] if spss else []
    return []


def fresnel(entry: dict, ndv) -> np.ndarray:
    x = np.maximum(1e-4, 1 - np.clip(ndv, 0, 1))
    return entry["g_fresnelbias"] + (1 - entry["g_fresnelbias"]) * x ** entry["g_fresnelpower"]


def albedo(entry: dict, ndv, texel=TEXEL) -> np.ndarray:
    """A: the paint's diffuse colour at N.E = ndv (..., 3)."""
    c2 = np.asarray(entry["g_vec3TintColour2"][:3])
    c3 = np.asarray(entry["g_vec3TintColour3"][:3])
    f = np.asarray(fresnel(entry, ndv))[..., None]
    paint = c2 + f * (c3 - c2)
    return texel[0] * (1 + texel[2] * (paint - 1))


# --- the paint shader ----------------------------------------------------------------------


def shade(
    entry: dict,
    n,
    e,
    light: dict = CLEAR_DAY,
    ao=1.0,
    lm_xy=(1.0, 0.5),
    shadow=1.0,
    env=0.0,
    texel=TEXEL,
    parts: bool = False,
):
    """HDR of paint points with unit normals n (..., 3) and unit eye vectors e (towards the
    camera), both world; ao the vertex term (position.w / 4096), lm_xy the light map texel,
    shadow the PSSM term (1 lit), env the environment map's colour along R (..., 3)."""
    n, e = unit(n), unit(e)
    ndv = np.sum(n * e, -1)
    a = albedo(entry, ndv, texel)
    amb_amount = (np.asarray(lm_xy[0]) * 0.4 + 0.6) * np.asarray(ao)
    d = (
        np.asarray(shadow)
        * np.minimum(1.0, 2 * np.asarray(lm_xy[1]))
        * (1 + DIRECTIONAL_OCCLUSION * (np.asarray(ao) - 1))
    )
    amb = amb_amount[..., None] * (
        light["amb_lower"] * np.sum(n * -unit(light["amb_dir"]), -1)[..., None] + light["amb_upper"]
    )
    diff = np.maximum(np.sum(n * -unit(light["sun_dir"]), -1), 0)[..., None] * light["sun"] * np.asarray(d)[..., None]
    sd = unit(light["spec_dir"])
    r = sd - 2 * np.sum(n * sd, -1)[..., None] * n
    s = np.maximum(np.sum(r * e, -1), 1e-4)
    spec1 = (texel[1] * s**SPECULAR_POWER)[..., None] * light["spec"]
    spec2 = (
        (texel[1] * entry["g_hSecondSpecularIntensity"] * s ** entry["g_hSecondSpecularPower"] * texel[2])[..., None]
        * light["spec"]
        * a
    )
    spec = np.asarray(d)[..., None] * (spec1 + spec2)
    fr = np.maximum(
        CUBEMAP_FRESNEL_MIN, np.clip(1 - ndv, 0, 1) ** entry["g_hCustomFresnelPower"] + REFLECTION_FRESNEL_BIAS
    )
    sm = light["shadow_mod"]
    coef = (
        texel[1]
        * fr
        * entry["g_hCustomReflectionAmount"]
        * (d * sm[0] + amb_amount * sm[1] + sm[2])
        * light["tex_mul2_z"]
    )
    refl = np.clip(np.asarray(coef)[..., None] * np.asarray(env) * LUMINANCE_BOOST["exterior"], 0, 1)
    out = np.minimum((amb + diff) * a + spec + refl, 6.0)
    if parts:
        return out, {"albedo": a, "ambient": amb * a, "sun": diff * a, "spec": spec, "refl": refl, "ndv": ndv}
    return out


# The colour grade (PostProcessing.fx: a 16^3 LUT lookup after the Reinhard curve), node
# [r, g, b] at input (r, g, b) / 15, output display-linear. The game's own: a 16 x 16 x 16
# A8R8G8B8 DDS in media/SfxMisc.fchunk (one of 64 there), the chunk with UID GRADE_UID,
# which the tone map samples as both g_samColourGradingFrom and To in ClearDay (paintprobe,
# run vm1-20261030-163744: the managed volume texture at samplers 3 and 4, byte for byte).
# Read from DSF_GAME_DIR when it is set; otherwise the fit across channels from dumped frame
# pairs (`grade`, paint_grade.json), within 10 / 255 of it. lamp_model's per-channel fit can't
# desaturate: a red paint's green and blue came out 25 to 35 / 255 too dark with it.
GRADE_FILE = Path(__file__).resolve().parent / "paint_grade.json"
GRADE_UID = bytes.fromhex("0012e109a4d88c2d")
_GRADE: list = []


def game_grade(game_dir: str | None = None) -> np.ndarray | None:
    """The ClearDay grade LUT from the game's media/SfxMisc.fchunk, as [r, g, b, 3] floats."""
    import os

    root = game_dir or os.environ.get("DSF_GAME_DIR")
    path = Path(root, "media", "SfxMisc.fchunk") if root else None
    if path is None or not path.exists():
        return None
    data = path.read_bytes()
    at = data.find(GRADE_UID)
    dds = data.find(b"DDS ", at) if at >= 0 else -1
    if dds < 0 or dds - at > 64:
        return None
    vol = np.frombuffer(data, np.uint8, 16 * 16 * 16 * 4, dds + 128).reshape(16, 16, 16, 4)  # [b, g, r, BGRA]
    return np.transpose(vol[..., [2, 1, 0]], (2, 1, 0, 3)).astype(np.float64) / 255


def grade3d(c: np.ndarray) -> np.ndarray:
    """Trilinear lookup in the 16^3 grade: the game's (game_grade), else the fitted one, else
    lamp_model's per-channel fit."""
    if not _GRADE:
        lut = game_grade()
        if lut is None and GRADE_FILE.exists():
            lut = np.array(json.loads(GRADE_FILE.read_text())["lut"])
        _GRADE.append(lut)
    lut = _GRADE[0]
    if lut is None:
        return lm.grade(c)
    n = lut.shape[0] - 1
    x = np.clip(np.asarray(c, np.float64), 0, 1) * n
    i0 = np.minimum(np.floor(x).astype(int), n - 1)
    f = x - i0
    out = 0
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = (
                    (f[..., 0] if dr else 1 - f[..., 0])
                    * (f[..., 1] if dg else 1 - f[..., 1])
                    * (f[..., 2] if db else 1 - f[..., 2])
                )
                out = out + w[..., None] * lut[i0[..., 0] + dr, i0[..., 1] + dg, i0[..., 2] + db]
    return out


def screen(hdr, light: dict = CLEAR_DAY, la: float = 0.17) -> np.ndarray:
    """sRGB (0..1) on screen of HDR values at adapted luminance la: exposure, Reinhard on
    luminance (research/lights-re.md), the fitted 3D grade, sRGB."""
    e = lm.exposure(la, light["LC"], light["MG"])
    return lm.srgb(grade3d(lm.tonemap(np.asarray(hdr, np.float64), e, light["W"])))


# --- target: the albedo that shows a given colour ---------------------------------------------

# The light on sunlit paint (HDR of the sun and ambient terms over the albedo) in the 33 sunlit
# dumps of run vm1-20261030-152849 (out/re/lightfactor.py): p10 0.80, p25 1.27, median 2.13,
# p75 2.66, p90 2.91, max 3.25. A palette colour shows on the median sunlit panel as
# screen(LIGHT_REF x A); panels facing the sun get up to LIGHT_SUN x A, turned away LIGHT_AWAY.
LIGHT_REF = 2.13
LIGHT_SUN = 3.0
LIGHT_AWAY = 0.8
LA_REF = 0.2  # the adapted luminance of those frames (0.07 to 0.32; e = 1.6 at 0.2)


def _solve(t_lin: np.ndarray, light: float, la: float, iters: int) -> np.ndarray:
    a = np.clip(t_lin / max(light, 1e-3), 1e-4, 1)
    for _ in range(iters):
        s = screen(light * a, la=la)
        s_lin = np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)
        a = np.clip(a * ((t_lin + 1e-4) / (s_lin + 1e-4)) ** 0.7, 1e-4, 1)
    return a


def albedo_for(srgb, light: float = LIGHT_REF, la: float = LA_REF, iters: int = 60) -> np.ndarray:
    """The linear albedo A (0..1 per channel) whose screen colour at `light` x A is nearest the
    sRGB colour `srgb` (0..1): a fixed-point search through the exposure, Reinhard, the grade
    and sRGB. A colour the screen can't show (the solution passes the knee) is dimmed in linear
    light, its hue kept, until the solution fits under the knee."""
    target = np.asarray(srgb, np.float64)
    t_lin = np.where(target <= 0.04045, target / 12.92, ((target + 0.055) / 1.055) ** 2.4)
    a = _solve(t_lin, light, la, iters)
    if a.max() <= knee(a) + 1e-3:
        return a
    lo, hi = 0.0, 1.0
    best = a * knee(a) / a.max()
    for _ in range(14):
        k = (lo + hi) / 2
        b = _solve(t_lin * k, light, la, iters)
        if b.max() <= knee(b) + 1e-3:
            lo, best = k, b
        else:
            hi = k
    return best


def knee(a) -> float:
    """The largest albedo channel worth having: past it the median sunlit panel gets no brighter
    on screen, only the shading across the body flattens (a sun-facing panel and the median one
    show the same value). From the model's response (`paint_model.py target`): a neutral grey
    stops gaining near 0.5, a saturated colour (one strong channel) near 0.3."""
    a = np.asarray(a, np.float64)
    sat = 1 - a.min() / max(a.max(), 1e-6)
    return 0.5 - 0.2 * sat


def cmd_target(args) -> None:
    """For sRGB swatches: the albedo the model gives against today's SHOWN_GAIN x swatch, and
    what each shows on the median sunlit panel, a panel facing the sun and one turned away."""
    from dsfforza.convert import paint

    print(
        "swatch   albedo (model)          0.3 x swatch            on screen: model ref / sun / away;  "
        "SHOWN_GAIN ref / sun / away"
    )
    for hexrgb in args.colours.split(","):
        h = hexrgb.strip().lstrip("#")
        s = np.array([int(h[i : i + 2], 16) for i in (0, 2, 4)]) / 255
        a = albedo_for(s)
        g = np.array([paint.SHOWN_GAIN * paint.srgb_to_linear(x) for x in s])

        def show(x, light):
            return tuple(int(round(255 * v)) for v in screen(light * x, la=LA_REF))

        print(
            f"#{h}  {fmt(a)}  {fmt(g)}  {show(a, LIGHT_REF)} {show(a, LIGHT_SUN)} {show(a, LIGHT_AWAY)};  "
            f"{show(g, LIGHT_REF)} {show(g, LIGHT_SUN)} {show(g, LIGHT_AWAY)}"
        )


# --- curve: one entry by angle --------------------------------------------------------------

# Reference surfaces in the world frame (+y up): a roof or bonnet, a door seen from the side,
# a tail panel; the camera at the probe's chase height. The sun as in ClearDay.
SURFACES = {
    "roof": np.array([0.0, 1.0, 0.0]),
    "door": np.array([1.0, 0.0, 0.0]),
    "door_sunward": unit([1.25, 0.0, -3.0]),
    "tail": np.array([0.0, 0.0, -1.0]),
}


def curve_rows(entry: dict, light: dict = CLEAR_DAY, la: float = 0.17, env=0.15) -> list[dict]:
    rows = []
    for name, n in SURFACES.items():
        for ndv in (0.95, 0.7, 0.4, 0.15):
            # an eye vector at N.E = ndv in the plane of n and the vertical (or x for the roof)
            t = np.array([0.0, 0.0, 1.0]) if abs(n[1]) > 0.9 else np.array([0.0, 1.0, 0.0])
            t = unit(t - np.dot(t, n) * n)
            e = unit(ndv * n + math.sqrt(1 - ndv * ndv) * t)
            for sun in (1.0, 0.0):
                hdr, p = shade(entry, n, e, light, shadow=sun, env=np.full(3, env), parts=True)
                rows.append(
                    {
                        "surface": name,
                        "ndv": ndv,
                        "sun": sun,
                        "hdr": hdr,
                        "srgb": screen(hdr, light, la),
                        "albedo": p["albedo"],
                    }
                )
    return rows


def cmd_curve(args) -> None:
    entries = palette(Path(args.archive), args.sid)
    if not entries:
        sys.exit(f"no palette for {args.sid}")
    entry = entries[args.index]
    c2, c3 = entry["g_vec3TintColour2"][:3], entry["g_vec3TintColour3"][:3]
    print(
        f"sid {args.sid} entry {args.index}: c2 {fmt(c2)} c3 {fmt(c3)} power {entry['g_fresnelpower']:g} "
        f"bias {entry['g_fresnelbias']:g} refl {entry['g_hCustomReflectionAmount']:g} cfp "
        f"{entry['g_hCustomFresnelPower']:g} s2 {entry['g_hSecondSpecularIntensity']:g}/"
        f"{entry['g_hSecondSpecularPower']:g}"
    )
    print("surface        N.E  sun  albedo               HDR                  sRGB on screen")
    for r in curve_rows(entry, la=args.la, env=args.env):
        s = tuple(round(255 * x) for x in r["srgb"])
        print(f"{r['surface']:13s} {r['ndv']:.2f}  {r['sun']:.0f}   {fmt(r['albedo'])}  {fmt(r['hdr'])}  {s}")


def fmt(v) -> str:
    return "(" + ", ".join(f"{x:.3f}" for x in np.asarray(v).ravel()) + ")"


# --- the probe log --------------------------------------------------------------------------


def _num(x: str) -> float:
    try:
        return float(x)
    except ValueError:
        return float("nan")


def _vecs(text: str) -> dict[int, np.ndarray]:
    out = {}
    for tok in text.split():
        m = re.match(r"c(\d+)=(.*)", tok)
        if m:
            out[int(m.group(1))] = np.array([_num(x) for x in m.group(2).split(",")])
    return out


def read_probe(path: Path) -> dict:
    """{dumps: [{n, frame, t, wall, draws: [{name, ps, vs, prims, samplers, ps_c, vs_c, panels}],
    files: [...]}], summaries: [...]} from a paintprobe log."""
    dumps, sums, cur, draw = [], [], None, None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"dump (\d+) f=(\d+) t=([\d.]+) (\S+)", line)
        if m:
            cur = {
                "n": int(m.group(1)),
                "frame": int(m.group(2)),
                "t": float(m.group(3)),
                "wall": m.group(4),
                "draws": [],
                "files": [],
            }
            dumps.append(cur)
            continue
        m = re.match(
            r" draw f=(\d+) (\S+) ps=(\w+) vs=(\w+) prims=(\d+) verts=(\d+) start=(\d+) rt=(\w+) "
            r"srgbwrite=(\d+) blend=(\S+)(.*)",
            line,
        )
        if m and cur is not None:
            draw = {
                "frame": int(m.group(1)),
                "name": m.group(2),
                "ps": m.group(3),
                "vs": m.group(4),
                "prims": int(m.group(5)),
                "verts": int(m.group(6)),
                "rt": m.group(8),
                "srgbwrite": int(m.group(9)),
                "blend": m.group(10),
                "samplers": m.group(11).strip(),
            }
            cur["draws"].append(draw)
            continue
        m = re.match(r"  (ps|vs|panels) (.*)", line)
        if m:
            target = draw if m.group(1) != "ps" or draw is not None else None
            if sums and sums[-1].get("ps_c") is None and m.group(1) == "ps" and (draw is None or sums[-1]["late"]):
                sums[-1]["ps_c"] = _vecs(m.group(2))
                continue
            if target is not None:
                target[{"ps": "ps_c", "vs": "vs_c", "panels": "panels"}[m.group(1)]] = _vecs(m.group(2))
            continue
        m = re.match(r"  dump (\S+) (\S+)", line)
        if m and cur is not None:
            cur["files"].append((m.group(1), m.group(2)))
            continue
        m = re.match(r"sum t=([\d.]+) (\S+) f=(\d+) (\S+) prims=(\d+)", line)
        if m:
            sums.append(
                {
                    "t": float(m.group(1)),
                    "wall": m.group(2),
                    "frame": int(m.group(3)),
                    "name": m.group(4),
                    "prims": int(m.group(5)),
                    "ps_c": None,
                    "late": True,
                }
            )
            draw = None
            continue
        if line.startswith("end dump"):
            draw = None
    return {"dumps": dumps, "summaries": sums}


# GPCH 26 (GLOBAL_Chassis_LOD1 pass 0) pixel constants by register.
GP26 = {
    "sun": 5,
    "spec": 6,
    "sun_dir": 7,
    "spec_dir": 8,
    "occl": 9,
    "amb_dir": 10,
    "amb_lower": 11,
    "amb_upper": 12,
    "spec_power": 13,
    "s2i": 14,
    "s2p": 15,
    "tint2": 16,
    "tint3m2": 17,
    "cfp": 18,
    "refl_bias": 19,
    "fresnel_min": 20,
    "refl": 21,
    "final_tint": 22,
    "tex_mul2": 24,
    "boost": 41,
    "fbias": 42,
    "one_m_fbias": 43,
    "fpow": 44,
    "dirt": 45,
    "fog": 46,
    "shadow_mod": 47,
}
# GPCH 14 (GLOBAL_Chassis pass 0, the full entry: traffic) pixel constants by register.
GP14 = {
    "sun": 0,
    "spec": 1,
    "sun_dir": 2,
    "spec_dir": 3,
    "occl": 4,
    "amb_dir": 5,
    "amb_lower": 6,
    "amb_upper": 7,
    "spec_power": 8,
    "s2i": 9,
    "s2p": 10,
    "tint2": 11,
    "tint3m2": 12,
    "refl_bias": 13,
    "fresnel_min": 14,
    "refl": 15,
    "final_tint": 16,
    "tex_mul2": 17,
    "boost": 18,
    "fbias": 19,
    "one_m_fbias": 20,
    "fpow": 21,
    "dirt": 22,
    "fog": 23,
    "shadow_mod": 24,
}


def named(draw: dict) -> dict:
    table = GP26 if "LOD1" in draw["name"] else GP14
    return {k: draw["ps_c"].get(r) for k, r in table.items()}


def cmd_log(args) -> None:
    probe = read_probe(Path(args.log))
    seen = set()
    for d in probe["dumps"]:
        for dr in d["draws"]:
            if "chassis" not in dr["name"] or dr.get("ps_c") is None:
                continue
            c = named(dr)
            key = (dr["name"], tuple(np.round(c["tint2"], 3)), tuple(np.round(c["sun"], 3)))
            if key in seen and not args.all:
                continue
            seen.add(key)
            print(f"dump {d['n']} {d['wall']} {dr['name']} prims {dr['prims']} {dr['samplers']}")
            for k, v in c.items():
                print(f"    {k:12s} {fmt(v) if v is not None else '-'}")


# --- frames: the model against dumped frames ------------------------------------------------


def read_tex(path: Path) -> np.ndarray | None:
    """A paintprobe `.tex` dump's first face as float RGB (A8R8G8B8 / X8R8G8B8, A16B16G16R16F,
    DXT1/5 through dsf_materials), or None."""
    raw = Path(path).read_bytes()
    head, data = raw.split(b"\n", 1)
    f = dict(kv.split("=") for kv in head.decode().split()[1:])
    fmt, w, h, pitch, rows = (int(f[k]) for k in ("fmt", "w", "h", "pitch", "rows"))
    face = np.frombuffer(data[: pitch * rows], np.uint8).reshape(rows, pitch)
    if fmt in (21, 22):
        px = face[:, : w * 4].reshape(h, w, 4).astype(np.float64) / 255
        return px[..., [2, 1, 0]]
    if fmt == 113:
        return face[:, : w * 8].copy().view("<f2").reshape(h, w, 4)[..., :3].astype(np.float64)
    if fmt in (0x31545844, 0x35545844):
        from dsfforza.convert import dsf_materials as dm

        kind = b"DXT1" if fmt == 0x31545844 else b"DXT5"
        dds = _dds(kind, w, h, face[:, : (w // 4) * (8 if kind == b"DXT1" else 16)].tobytes())
        ww, hh, px = dm.decode_dds(dds)
        return np.frombuffer(bytes(px), np.uint8).reshape(hh, ww, 4)[..., :3].astype(np.float64) / 255
    return None


def _dds(kind: bytes, w: int, h: int, data: bytes) -> bytes:
    import struct

    head = struct.pack("<4sIIIIIII44xII4s20xI16x", b"DDS ", 124, 0x1007, h, w, len(data), 0, 1, 32, 4, kind, 0x1000)
    return head + data


class ClipView:
    """A car_render view whose projection is the game's: object position p -> panel transform
    P (rows c0..c2) -> clip = M (rows c96..c99) [P p, 1]."""

    def __init__(self, draw: dict, width: int, height: int):
        vs, pn = draw["vs_c"], draw["panels"]
        self.P = np.array([pn[0], pn[1], pn[2]])
        self.M = np.array([vs[96], vs[97], vs[98], vs[99]])
        self.T = np.array([vs[100][3], vs[101][3], vs[102][3]])
        self.cam = np.array(vs[108][:3])
        self.W2C = np.array([vs[103][:3], vs[104][:3], vs[105][:3]])
        self.lm = np.array(vs[109])
        self.width, self.height = width, height
        rot = self.P[:, :3]
        self.eye = np.linalg.solve(rot, self.cam - self.T - self.P[:, 3])  # the camera in object space

    def world(self, p: np.ndarray) -> np.ndarray:
        return p @ self.P[:, :3].T + self.P[:, 3] + self.T

    def project(self, p: np.ndarray):
        q = p @ self.P[:, :3].T + self.P[:, 3]
        clip = np.concatenate([q, np.ones(q.shape[:-1] + (1,))], -1) @ self.M.T
        w = clip[..., 3]
        x = (clip[..., 0] / w + 1) / 2 * self.width
        y = (1 - clip[..., 1] / w) / 2 * self.height
        return x, y, w


def sphere_env(env: np.ndarray, view: ClipView, ps_c: dict, n_w: np.ndarray, e_w: np.ndarray) -> np.ndarray:
    """GPCH 26's environment lookup: the reflection in camera space onto the spherical map
    (o5 = eye, o6 = normal through g_WorldToCam; uv = R.xy / 2 + 1/2, v flipped, less
    0.5 x sat(WorldToCam[2].y), then g_vec4ViewportBoundaries)."""
    nv = unit(n_w @ view.W2C.T)
    ev = unit(e_w @ view.W2C.T)
    d = np.sum(-ev * nv, -1)[..., None]
    r = -ev - 2 * d * nv  # mad r0.yz, N, -2 (-E.N), -E
    u = r[..., 0] * 0.5 + 0.5
    v = 1 - (r[..., 1] * 0.5 + 0.5) - 0.5 * min(1.0, max(0.0, view.W2C[2][1]))
    vp = ps_c[4]
    u, v = u * vp[2] + vp[0], v * vp[3] + vp[1]
    h, w = env.shape[:2]
    x = np.clip((u * w).astype(int), 0, w - 1)
    y = np.clip((v * h).astype(int), 0, h - 1)
    return env[y, x]


def light_from(c: dict) -> dict:
    out = dict(CLEAR_DAY)
    out.update(
        {
            "sun": c["sun"][:3],
            "sun_dir": c["sun_dir"][:3],
            "spec": c["spec"][:3],
            "spec_dir": c["spec_dir"][:3],
            "amb_lower": c["amb_lower"][:3],
            "amb_upper": c["amb_upper"][:3],
            "amb_dir": c["amb_dir"][:3],
            "shadow_mod": c["shadow_mod"][:3],
            "tex_mul2_z": c["tex_mul2"][2],
        }
    )
    return out


def entry_from(c: dict) -> dict:
    """The palette entry as the shader received it (c16/c17/c42..c44...)."""
    t2 = c["tint2"][:3]
    return {
        "g_vec3TintColour2": t2,
        "g_vec3TintColour3": t2 + c["tint3m2"][:3],
        "g_fresnelpower": c["fpow"][0],
        "g_fresnelbias": c["fbias"][0],
        "g_hCustomFresnelPower": c["cfp"][0],
        "g_hCustomReflectionAmount": c["refl"][0],
        "g_hSecondSpecularIntensity": c["s2i"][0],
        "g_hSecondSpecularPower": c["s2p"][0],
    }


ANGLE_BINS = ((0.75, 1.01, "facing"), (0.45, 0.75, "oblique"), (0.2, 0.45, "steep"), (0.0, 0.2, "edge-on"))


def frame_pixels(dump: dict, dumps_dir: Path, archive: Path, sid: int) -> dict | None:
    """The player car's paint pixels in one dump: observed HDR and final sRGB, and the model's
    prediction lit and in shade, with each pixel's N.E, N.L and highlight term."""
    from dsfforza.game import car_render as cr

    draws = [d for d in dump["draws"] if d["name"].startswith("gp26") and d.get("panels")]
    if not draws:
        return None
    draw = max(draws, key=lambda d: d["prims"])
    pfm = dumps_dir / f"f{dump['frame']:06d}-scene.pfm"
    bmp = dumps_dir / f"f{dump['frame']:06d}-final.bmp"
    if not pfm.exists():
        return None
    hdr = lm.read_pfm(pfm)
    from PIL import Image

    h, w = hdr.shape[:2]
    final = None
    if bmp.exists():  # the back buffer (1600 x 900 in the VMs) holds the 3D view unscaled, centred
        bb = np.asarray(Image.open(bmp).convert("RGB"), np.float64) / 255
        oy, ox = (bb.shape[0] - h) // 2, (bb.shape[1] - w) // 2
        final = bb[oy : oy + h, ox : ox + w]
    scene = cr.load_scene(archive, sid, interior=False, entry="exterior")
    view = ClipView(draw, w, h)
    frame = cr.rasterize(scene, view)
    paint = np.array([m.kind in cr.PAINT_KINDS for m in scene.materials])
    k = np.flatnonzero(frame.tri >= 0)
    k = k[paint[scene.mat[frame.tri[k]]]]
    if len(k) == 0:
        return None
    tri = frame.tri[k]
    lam = cr._barycentrics(view, frame.sx, frame.sy, frame.sw, k, tri)
    p = np.einsum("nc,nci->ni", lam, scene.pos[tri])
    n = unit(np.einsum("nc,nci->ni", lam, scene.nrm[tri]))
    ao = np.einsum("nc,nc->n", lam, scene.ao[tri])
    pw = view.world(p)
    nw = unit(n @ view.P[:, :3].T)
    ew = unit(view.cam - pw)
    flip = np.sum(nw * ew, -1) < 0  # a pixel can't show a back face: double-sided normal
    nw[flip] *= -1
    c = named(draw)
    light = light_from(c)
    entry = entry_from(c)
    env = None
    s1 = [f for f, _ in dump["files"] if "-s1-" in f]
    if s1:
        tex = read_tex(dumps_dir / Path(s1[0].replace("\\", "/")).name)
        if tex is not None:
            env = sphere_env(tex, view, draw["ps_c"], nw, ew)
    if env is None:
        env = np.zeros_like(nw)
    lmx = (1.0, 0.5)
    s2 = [f for f, _ in dump["files"] if "-s2-" in f]
    if s2:
        tex = read_tex(dumps_dir / Path(s2[0].replace("\\", "/")).name)
        if tex is not None:
            uvl = view.lm[:2] * pw[:, [0, 2]] + view.lm[2:]
            th, tw = tex.shape[:2]
            px = tex[
                np.clip((uvl[:, 1] % 1 * th).astype(int), 0, th - 1),
                np.clip((uvl[:, 0] % 1 * tw).astype(int), 0, tw - 1),
            ]
            lmx = (px[:, 0], px[:, 1])
    lit, parts = shade(entry, nw, ew, light, ao=ao, lm_xy=lmx, shadow=1.0, env=env, parts=True)
    dark = shade(entry, nw, ew, light, ao=ao, lm_xy=lmx, shadow=0.0, env=env)
    yy, xx = k // w, k % w
    obs = hdr[yy, xx]
    sd = unit(light["spec_dir"])
    r = sd - 2 * np.sum(nw * sd, -1)[:, None] * nw
    return {
        "k": k,
        "w": w,
        "h": h,
        "obs": obs,
        "final": None if final is None else final[yy, xx],
        "lit": lit,
        "shade": dark,
        "ndv": np.sum(nw * ew, -1),
        "ndl": np.sum(nw * -unit(light["sun_dir"]), -1),
        "rdote": np.sum(r * ew, -1),
        "entry": entry,
        "light": light,
        "hdr": hdr,
        "final_img": final,
        "draw": draw,
        "lm": lmx,
        "parts": parts,
    }


def classify(px: dict) -> np.ndarray:
    """Per pixel: 1 lit (observed luminance within x0.67..1.5 of the lit prediction), 0 in shade
    (within that of the shade prediction), -1 neither (a highlight or reflection the model puts
    elsewhere, a mis-projected edge). The PSSM term isn't dumped, so lit or not is read here."""
    lum = lambda x: x @ REC709 + 1e-4  # noqa: E731
    o = lum(px["obs"])
    rl, rd = np.abs(np.log(o / lum(px["lit"]))), np.abs(np.log(o / lum(px["shade"])))
    out = np.full(len(o), -1)
    out[(rd < rl) & (rd < math.log(1.5))] = 0
    out[(rl <= rd) & (rl < math.log(1.5))] = 1
    return out


def compare_frame(px: dict, la: float | None = None) -> dict:
    """Per angle bin and light class: observed against predicted, HDR and on screen."""
    obs = px["obs"]
    cls = classify(px)
    pred = np.where((cls == 0)[:, None], px["shade"], px["lit"])
    if la is None:
        la = float(np.exp(np.mean(np.log(px["hdr"] @ REC709 + 1e-5))))
    pred_srgb = screen(pred, px["light"], la)
    rows = []
    for lo, hi, name in ANGLE_BINS:
        for c in (1, 0):
            sel = (px["ndv"] >= lo) & (px["ndv"] < hi) & (cls == c)
            if sel.sum() < 30:
                continue
            row = {
                "bin": name,
                "sun": c == 1,
                "n": int(sel.sum()),
                "obs": np.median(obs[sel], 0),
                "pred": np.median(pred[sel], 0),
                "ratio": float(np.median((obs[sel] @ REC709 + 1e-4) / (pred[sel] @ REC709 + 1e-4))),
            }
            if px["final"] is not None:
                row["obs_srgb"] = np.median(px["final"][sel], 0)
                row["pred_srgb"] = np.median(pred_srgb[sel], 0)
            rows.append(row)
    shares = {k: float((cls == v).mean()) for k, v in (("lit", 1), ("shade", 0), ("other", -1))}
    return {"rows": rows, "shares": shares, "sun_share": shares["lit"], "la": la}


def car_of(draw: dict, palettes: dict[int, list[dict]]) -> int | None:
    """The spool ID whose palette holds the colour 2 the draw received (c16)."""
    t2 = named(draw)["tint2"][:3]
    best, dist = None, 1e-3
    for sid, entries in palettes.items():
        for e in entries:
            d = float(np.abs(np.asarray(e["g_vec3TintColour2"][:3]) - t2).max())
            if d < dist:
                best, dist = sid, d
    return best


def cmd_frames(args) -> None:
    probe = read_probe(Path(args.log))
    sids = [int(x) for x in args.sids.split(",")]
    palettes = {sid: palette(Path(args.archive), sid) for sid in sids}
    dumps_dir = Path(args.dumps)
    results = []
    last = {}
    for d in probe["dumps"]:
        draws = [x for x in d["draws"] if x["name"].startswith("gp26") and x.get("panels") and x.get("ps_c")]
        if not draws:
            continue
        draw = max(draws, key=lambda x: x["prims"])
        sid = car_of(draw, palettes)
        if sid is None or draw["prims"] < args.min_prims:
            continue
        cam = tuple(np.round(draw["vs_c"][108][:3], 1))
        if not args.every and last.get(sid) == cam:
            continue  # the same view of the same car as the last dump taken
        last[sid] = cam
        px = frame_pixels(d, dumps_dir, Path(args.archive), sid)
        if px is None or len(px["k"]) < 500:
            continue
        res = compare_frame(px)
        sh = res["shares"]
        print(
            f"== sid {sid} f{d['frame']:06d} {d['wall']}: {len(px['k'])} paint px: lit {sh['lit']:.0%}, shade "
            f"{sh['shade']:.0%}, neither {sh['other']:.0%}; La {res['la']:.3f}"
        )
        for r in res["rows"]:
            line = (
                f"  {r['bin']:8s} {'sun ' if r['sun'] else 'shade'} n={r['n']:6d}  HDR obs {fmt(r['obs'])} "
                f"pred {fmt(r['pred'])} x{r['ratio']:.2f}"
            )
            if "obs_srgb" in r:
                line += (
                    f"  sRGB obs {tuple(int(round(255 * x)) for x in r['obs_srgb'])} "
                    f"pred {tuple(int(round(255 * x)) for x in r['pred_srgb'])}"
                )
            print(line)
        results.append(
            {
                "sid": sid,
                "frame": d["frame"],
                "wall": d["wall"],
                "camera": list(cam),
                "rows": [
                    {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in r.items()} for r in res["rows"]
                ],
                "sun_share": res["sun_share"],
                "la": res["la"],
            }
        )
        if args.out:
            save_overlay(px, Path(args.out) / f"sid{sid}-f{d['frame']:06d}.png")
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / "frames.json").write_text(json.dumps(results, indent=1))


def save_overlay(px: dict, path: Path) -> None:
    """The final frame beside the model's prediction pasted over it (paint pixels only)."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    w, h = px["w"], px["h"]
    final = px["final_img"] if px["final_img"] is not None else np.zeros((h, w, 3))
    cls = classify(px)
    pred = np.where((cls == 0)[:, None], px["shade"], px["lit"])
    la = float(np.exp(np.mean(np.log(px["hdr"] @ REC709 + 1e-5))))
    model = final.copy()
    model.reshape(-1, 3)[px["k"]] = screen(pred, px["light"], la)
    ys, xs = px["k"] // w, px["k"] % w
    y0, y1 = max(0, ys.min() - 20), min(h, ys.max() + 20)
    x0, x1 = max(0, xs.min() - 20), min(w, xs.max() + 20)
    pair = np.concatenate([final[y0:y1, x0:x1], model[y0:y1, x0:x1]], 1)
    Image.fromarray((np.clip(pair, 0, 1) * 255).astype(np.uint8)).save(path)


def fit_grade(pairs: list[tuple[np.ndarray, np.ndarray]], n: int = 16, smooth: float = 0.05) -> np.ndarray:
    """Least-squares 3D LUT (n^3 nodes, trilinear) from (tone-mapped input, display-linear output)
    samples, with a Laplacian smoothness term so that nodes no sample reaches follow their
    neighbours."""
    m = n**3
    ata = np.zeros((m, m))
    atb = np.zeros((m, 3))
    for x, y in pairs:
        x = np.clip(x, 0, 1) * (n - 1)
        i0 = np.minimum(np.floor(x).astype(int), n - 2)
        f = x - i0
        idx, wts = [], []
        for dr in (0, 1):
            for dg in (0, 1):
                for db in (0, 1):
                    idx.append(((i0[:, 0] + dr) * n + i0[:, 1] + dg) * n + i0[:, 2] + db)
                    wts.append(
                        (f[:, 0] if dr else 1 - f[:, 0])
                        * (f[:, 1] if dg else 1 - f[:, 1])
                        * (f[:, 2] if db else 1 - f[:, 2])
                    )
        idx, wts = np.stack(idx, 1), np.stack(wts, 1)
        for a in range(8):
            np.add.at(atb, idx[:, a], wts[:, a, None] * y)
            for b in range(8):
                np.add.at(ata, (idx[:, a], idx[:, b]), wts[:, a] * wts[:, b])
    # smoothness: each node near the mean of its axis neighbours
    lap = np.zeros((m, m))
    g = np.arange(m).reshape(n, n, n)
    for axis in range(3):
        a0 = np.take(g, range(n - 1), axis=axis).ravel()
        a1 = np.take(g, range(1, n), axis=axis).ravel()
        np.add.at(lap, (a0, a0), 1)
        np.add.at(lap, (a1, a1), 1)
        np.add.at(lap, (a0, a1), -1)
        np.add.at(lap, (a1, a0), -1)
    scale = np.trace(ata) / m
    sol = np.linalg.solve(ata + smooth * scale * lap + 1e-9 * np.eye(m), atb)
    return sol.reshape(n, n, n, 3)


def cmd_grade(args) -> None:
    """Fit the 3D grade from every dump with a scene PFM and a final BMP (central part of the
    frame: the vignette darkens the edges), and report it against the per-channel fit."""
    from PIL import Image

    rng = np.random.default_rng(1)
    pairs, held = [], []
    dumps = sorted(Path(args.dumps).glob("f*-scene.pfm"))
    for k, pfm in enumerate(dumps):
        bmp = pfm.with_name(pfm.name.replace("-scene.pfm", "-final.bmp"))
        if not bmp.exists():
            continue
        hdr = lm.read_pfm(pfm)
        h, w = hdr.shape[:2]
        bb = np.asarray(Image.open(bmp).convert("RGB"), np.float64) / 255
        oy, ox = (bb.shape[0] - h) // 2, (bb.shape[1] - w) // 2
        fin = bb[oy : oy + h, ox : ox + w]
        la = float(np.exp(np.mean(np.log(hdr @ REC709 + 1e-5))))
        e = lm.exposure(la, CLEAR_DAY["LC"], CLEAR_DAY["MG"])
        y0, y1, x0, x1 = h // 8, h * 7 // 8, w // 8, w * 7 // 8
        x = lm.tonemap(hdr[y0:y1, x0:x1], e, CLEAR_DAY["W"]).reshape(-1, 3)
        y = fin[y0:y1, x0:x1].reshape(-1, 3)
        ylin = np.where(y <= 0.04045, y / 12.92, ((y + 0.055) / 1.055) ** 2.4)
        keep = hdr[y0:y1, x0:x1].reshape(-1, 3).max(1) < 5.9
        x, ylin = x[keep], ylin[keep]
        sel = rng.choice(len(x), min(len(x), args.per_frame), replace=False)
        (held if k % 5 == 4 else pairs).append((x[sel], ylin[sel]))
    print(f"{len(pairs)} frames to fit, {len(held)} held out, {args.per_frame} samples each")
    lut = fit_grade(pairs, smooth=args.smooth)
    lut = np.clip(lut, 0, 1)
    Path(args.out).write_text(
        json.dumps(
            {
                "source": "paint_model grade: least squares over dumped frame pairs (scene PFM, final BMP), "
                f"{len(pairs)} frames; input Reinhard(e x HDR), output display-linear",
                "lut": np.round(lut, 4).tolist(),
            }
        )
    )
    _GRADE.clear()
    _GRADE.append(lut)
    xs = np.concatenate([p[0] for p in held])
    ys = np.concatenate([p[1] for p in held])
    sat = (xs.max(1) - xs.min(1)) / np.maximum(xs.max(1), 1e-4) > 0.6
    for name, sel in (("all", np.ones(len(xs), bool)), ("saturated", sat)):
        for label, fn in (("3D fit", grade3d), ("per-channel", lm.grade)):
            pred = lm.srgb(fn(xs[sel]))
            obs = lm.srgb(ys[sel])
            err = np.abs(pred - obs) * 255
            print(
                f"  held-out {name:9s} ({sel.sum():7d} px) {label:11s}: mean |error| {fmt(err.mean(0))} /255, "
                f"p90 {fmt(np.percentile(err, 90, axis=0))}"
            )


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("curve")
    c.add_argument("--archive", default=str(REPO / "out" / "vanilla" / "Install" / "dngvehicles.sp"))
    c.add_argument("--sid", type=int, required=True)
    c.add_argument("--index", type=int, default=0)
    c.add_argument("--la", type=float, default=0.17)
    c.add_argument("--env", type=float, default=0.15)
    g = sub.add_parser("log")
    g.add_argument("log")
    g.add_argument("--all", action="store_true")
    f = sub.add_parser("frames")
    f.add_argument("--log", required=True)
    f.add_argument("--dumps", required=True)
    f.add_argument("--archive", required=True)
    f.add_argument(
        "--sids", required=True, help="spool IDs of the probed cars (each dump's car is found by its colour)"
    )
    f.add_argument("--every", action="store_true", help="every dump, not one per camera position")
    f.add_argument("--min-prims", type=int, default=3000, help="smaller player draws are the cockpit view's")
    f.add_argument("--out")
    t = sub.add_parser("target")
    t.add_argument("colours", help="sRGB swatches, comma separated: '#c00000,#fcfcfc'")
    gr = sub.add_parser("grade")
    gr.add_argument("--dumps", required=True)
    gr.add_argument("--out", default=str(GRADE_FILE))
    gr.add_argument("--per-frame", type=int, default=20000)
    gr.add_argument("--smooth", type=float, default=0.05)
    args = ap.parse_args(argv)
    {"curve": cmd_curve, "log": cmd_log, "frames": cmd_frames, "grade": cmd_grade, "target": cmd_target}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
