"""Predict a vanilla DSF lamp's look from the decoded formulas (research/lights-re.md) and
compare it with frames dumped in game.

The model, per lamp texel (texel alpha a, linear texel colour t):

    C        = vehColourBoost x colour[b2] x scale[b3] x brightness      (0x008A78E0, c160)
    HDR      = D x t + a x C                       (GPCH 130/173 pixel shader, before fog)
    e        = LC.y + (MG - LC.y) x sat(1.5 - LC.w / (2 La))   (PostProcessing.fx vertex shader)
    c        = HDR x e (1 + e L / W^2) / (1 + e L), L = Rec. 709 luminance of HDR
    screen   = sRGB(LUT(c))                        (PostProcessing.fx pixel shader)

D is the light falling on the lamp (sun, sky, AO): one number per frame and lamp group,
fitted on the group's median (the diffuse part); the emission term has no free parameter.
LUT is colour grade 1 (ClearDay and the harness's dark mood), fitted per channel from
dumped frames (scene-end PFM against the final BMP); bloom is left out.

    uv run python tools/re/lamp_model.py levels                       the brightness table
    uv run python tools/re/lamp_model.py compare --dumps DIR --log TRACE.log [--frames F.json]

Frames (tools/re/lamp_frames.json): the dump name, the car, the lamps' brightness at that
moment (lampprobe log), vehColourBoost, and lamp groups (box in pixels, light ids).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

from dsfforza import REPO

# --- the game's numbers (research/lights-re.md) ---------------------------------------

# 0x00F3FB68: per-type scale, by the light table's type byte (flags b3).
TYPE_SCALE = [0, 2, 1, 2, 0.5, 0.5, 0.5, 0.5, 1, *[10] * 9, 3, 10, *[1] * 9, 10, 0.2, 1]
# Lights manager colour table (0x008E6F00: sRGB bytes / 255, then ~x^2.2 by 0x0049E160),
# by the colour byte (flags b2), as read back in game (lampprobe).
COLOURS = {
    0: (0, 0.0687686, 0.587755),
    1: (0, 0, 0.587755),
    2: (0.0591351, 0.411908, 0),
    3: (0, 0.587755, 0),
    4: (1, 0.229057, 0.00124854),
    5: (1, 0.36075, 0),
    6: (0.587755, 0, 0),
    7: (0.620376, 0, 0.0502853),
    8: (1, 0.957607, 0.760489),
    9: (0.875801, 0.891845, 1),
    10: (1, 0.640408, 0.000514284),
    11: (1, 0.916208, 0.000183946),
}
# Lua light masks per type (0x00E3A998) and the combined types' "secondary" set (0x00E3AA98).
HEAD, BRAKE, DRL, LIND, RIND, REV, SIREN, DIP, FOG = 1, 2, 4, 8, 16, 32, 64, 128, 256
TYPE_MASK = {
    1: HEAD,
    2: BRAKE,
    3: DRL,
    4: LIND,
    5: RIND,
    6: BRAKE | LIND,
    7: BRAKE | RIND,
    8: REV,
    9: SIREN,
    10: DIP,
    11: HEAD | SIREN,
    12: FOG,
    13: SIREN,
    14: SIREN,
    15: SIREN,
    16: 0x800,
    17: 0x1000,
    18: DRL,
    19: DRL | SIREN,
    20: DRL | LIND,
    21: DRL | RIND,
    22: DRL | LIND,
    23: DRL | RIND,
    24: BRAKE | DRL,
    25: BRAKE | DRL | LIND,
    26: BRAKE | DRL | RIND,
    27: BRAKE | DRL | LIND,
    28: BRAKE | DRL | RIND,
    29: HEAD | DRL,
}
COMBINED = {0x13, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D}
# 0x008E6C80: a combined lamp with only DaytimeRunning on goes to this absolute level.
DRL_ONLY = {**dict.fromkeys(range(0x18, 0x1D), 0.2), 0x13: 1.0, 0x1D: 1.0}


def level(type_byte: int, mask: int) -> float:
    """Steady colour multiplier (scale x brightness) of a lamp with the Lua mask `mask` on
    (blinking left out): 0x008E7AD0 sets the flags, 0x008E6640 picks the target."""
    m = TYPE_MASK.get(type_byte, 0) & mask
    if not m:
        return 0.0
    if type_byte in COMBINED and m == DRL:
        return DRL_ONLY[type_byte]
    return float(TYPE_SCALE[type_byte])


def emission(type_byte: int, colour: int, brightness_or_mask, tint: float, *, mask: bool = False) -> np.ndarray:
    """c160 entry: tint x colour x scale x brightness (brightness as read, or from a mask)."""
    rgb = np.array(COLOURS.get(colour, COLOURS[8]), np.float64)
    if mask:
        return tint * rgb * level(type_byte, brightness_or_mask)
    return tint * rgb * TYPE_SCALE[type_byte] * brightness_or_mask


# --- post chain -------------------------------------------------------------------------

REC709 = np.array([0.2125, 0.7154, 0.0721])
# Colour grade 1 (ClearDay, the harness dark mood), per channel: display-linear in (the tone
# map's output) -> display-linear out; binned medians of five dumped frames (vm1-20261030-142442,
# f02984 f03109 f04039 day, f03497 f05094 dark), central 780 x 420 px.
LUT_X = np.array([0.0, 0.013, 0.113, 0.213, 0.312, 0.413, 0.512, 0.613, 0.713, 0.812, 0.913, 1.0])
LUT_Y = np.array(
    [
        [0.012, 0.018, 0.056, 0.133, 0.270, 0.497, 0.640, 0.733, 0.818, 0.86, 0.87, 0.88],
        [0.016, 0.024, 0.060, 0.122, 0.234, 0.395, 0.573, 0.680, 0.777, 0.83, 0.85, 0.86],
        [0.024, 0.032, 0.068, 0.131, 0.233, 0.392, 0.556, 0.651, 0.758, 0.81, 0.85, 0.86],
    ]
)


def exposure(la: float, lc, mg: float) -> float:
    """e from the adapted luminance la, g_vLuminanceControl lc and g_fMiddleGrey mg."""
    return lc[1] + (mg - lc[1]) * min(1.0, max(0.0, 1.5 - lc[3] / (2 * la)))


def tonemap(hdr: np.ndarray, e: float, white: float) -> np.ndarray:
    lum = hdr @ REC709
    return hdr * (e * (1 + e * lum / white**2) / (1 + e * lum))[..., None]


def grade(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0, 1)
    return np.stack([np.interp(c[..., k], LUT_X, LUT_Y[k]) for k in range(3)], -1)


def srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def screen(hdr: np.ndarray, e: float, white: float) -> np.ndarray:
    return srgb(grade(tonemap(hdr, e, white)))


# --- inputs -----------------------------------------------------------------------------


def read_pfm(path: Path) -> np.ndarray:
    with Path(path).open("rb") as f:
        kind = f.readline().strip()
        w, h = map(int, f.readline().split())
        scale = float(f.readline())
        data = np.frombuffer(f.read(), "<f4" if scale < 0 else ">f4")
    return np.flipud(data.reshape(h, w, 3 if kind == b"PF" else 1)).copy()


def trace_constants(log: Path) -> dict[str, dict]:
    """Per dumped frame: the post constants DSF Enhanced's Trace logged (bright pass
    0CFF7616: g_vThreshold, its vertex shader's c2 = MG - LC.y and c3 = LC; tone map
    2F0359F4: bloom tint, LUT blend, W)."""

    def vec(s):
        return [float(x) for x in s.split(",")]

    lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
    frames, cur = {}, None
    for i, line in enumerate(lines):
        m = re.search(r"dump scene-end -> .*?(f\d+)-scene-end", line)
        if m:
            cur = m.group(1)
            frames.setdefault(cur, {})
            continue
        for ps, key in (("0CFF7616", "bright"), ("2F0359F4", "tone")):
            if cur and f"ps={ps}" in line and key not in frames[cur]:
                p = re.search(r"r\d: (.*)$", lines[i + 1]).group(1).split()
                v = re.search(r"vs c0-7: (.*)$", lines[i + 2]).group(1).split()
                frames[cur][key] = ([vec(x) for x in p], [vec(x) for x in v])
    out = {}
    for f, d in frames.items():
        if "bright" in d and "tone" in d:
            (bp, bv), (tp, _) = d["bright"], d["tone"]
            LC = bv[3]
            out[f] = {"threshold": bp[0][0], "LC": LC, "MG": bv[2][0] + LC[1], "bloom": tp[0][0], "W": tp[5][0]}
    return out


def lamp_texels(archive: Path, spool_id: int, facing=(0.0, 0.0, -1.0), per_m2: float = 4e6) -> dict:
    """{light id: (alpha (n,), linear rgb (n, 3), weights (n,), lamp info)} sampled
    area-weighted over the exterior entry's lamp triangles that face `facing`."""
    from dsfforza.game import car_render as cr

    scene = cr.load_scene(archive, spool_id, entry="exterior", physical=True)
    rng = np.random.default_rng(0)
    out = {}
    for li in np.unique(scene.lamp[scene.lamp >= 0]):
        tri = np.nonzero(scene.lamp == li)[0]
        p = scene.pos[tri]
        area = np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1) / 2
        keep = scene.nrm[tri].mean(1) @ np.asarray(facing) > 0.2
        tri, area = tri[keep], area[keep]
        if not len(tri):
            continue
        k = np.maximum(1, (area * per_m2 / 1000).astype(int))
        t = np.repeat(tri, k)
        w = np.repeat(area / k, k)
        s = np.sqrt(rng.random(len(t)))
        r2 = rng.random(len(t))
        b = np.stack([1 - s, s * (1 - r2), s * r2], 1)
        uv = np.einsum("nc,nci->ni", b, scene.uv[t])
        rgba = np.zeros((len(t), 4))
        for m in np.unique(scene.mat[t]):
            sel = scene.mat[t] == m
            if scene.materials[m].texture is not None:
                rgba[sel] = cr._sample(scene.materials[m].texture, uv[sel])
        lin = np.where(rgba[:, :3] <= 0.04045, rgba[:, :3] / 12.92, ((rgba[:, :3] + 0.055) / 1.055) ** 2.4)
        out[int(li)] = (rgba[:, 3], lin, w, scene.lamps[li])
    return out


def wquantile(v: np.ndarray, w: np.ndarray, q) -> np.ndarray:
    o = np.argsort(v)
    c = np.cumsum(w[o])
    return np.interp(np.asarray(q) * c[-1], c, v[o])


# --- comparison -------------------------------------------------------------------------

QUANTILES = (0.5, 0.9, 0.99)


def compare(args) -> None:
    frames = json.loads(Path(args.frames).read_text())
    consts = trace_constants(Path(args.log))
    dumps = Path(args.dumps)
    archive = Path(args.archive)
    texels: dict[int, dict] = {}
    rows = []
    for fr in frames:
        name, car = fr["frame"], fr["car"]
        if car not in texels:
            texels[car] = lamp_texels(archive, car)
        hdr = read_pfm(dumps / f"{name}-scene-end.pfm")
        from PIL import Image

        ldr = np.asarray(Image.open(dumps / f"{name}-p29-after-d01.bmp").convert("RGB"), np.float64) / 255
        k = consts[name]
        La = float(np.exp(np.mean(np.log(hdr @ REC709 + 1e-5))))
        e = exposure(La, k["LC"], k["MG"])
        for g in fr["groups"]:
            x0, y0, x1, y1 = g["box"]
            obs = hdr[y0:y1, x0:x1].reshape(-1, 3)
            obs_s = ldr[y0:y1, x0:x1].reshape(-1, 3)
            ch = {"R": 0, "G": 1, "L": None}[g.get("channel", "R")]
            # The lamp's own pixels: red-dominant for red lamps (no glints, no paint).
            if ch == 0:
                sel = obs[:, 0] > 2.5 * np.maximum(obs[:, 1], obs[:, 2])
            else:
                sel = np.ones(len(obs), bool)
            vals = obs[sel, 0] if ch == 0 else obs[sel] @ REC709
            vals_s = obs_s[sel, 0] if ch == 0 else obs_s[sel] @ REC709
            # Predicted per texel: D x t + a x C, the lamps of the group pooled by area.
            a, t, w, cs = [], [], [], []
            for lid in g["lamps"]:
                if lid not in texels[car]:
                    continue
                al, lin, ww, info = texels[car][lid]
                C = emission(info.type, info.colour, fr["brightness"].get(f"{info.type:#04x}", 0.0), fr["tint"])
                a.append(al)
                t.append(lin)
                w.append(ww)
                cs.append(np.broadcast_to(C, (len(al), 3)))
            a, t, w, cs = (np.concatenate(v) for v in (a, t, w, cs))
            emis = a[:, None] * cs
            ec = emis[:, 0] if ch == 0 else emis @ REC709
            tc = t[:, 0] if ch == 0 else t @ REC709
            # D: the diffuse light, fitted so the predicted median matches the observed one.
            target = float(np.median(vals)) if len(vals) else 0.0
            lo, hi = 0.0, 20.0
            for _ in range(50):
                mid = (lo + hi) / 2
                if wquantile(mid * tc + ec, w, 0.5) < target:
                    lo = mid
                else:
                    hi = mid
            D = (lo + hi) / 2
            pred = D * tc + ec
            pq = wquantile(pred, w, QUANTILES)
            oq = np.quantile(vals, QUANTILES) if len(vals) else np.zeros(3)
            # On screen: the post chain applied to the predicted HDR (grey channel trick:
            # the lamp's colour ratio from the emission and diffuse means).
            mean_rgb = (D * (t * w[:, None]).sum(0) + (emis * w[:, None]).sum(0)) / w.sum()
            ratio = mean_rgb / max(mean_rgb[ch] if ch is not None else mean_rgb @ REC709, 1e-9)
            ps = [float(screen((ratio * v)[None], e, k["W"])[0][ch if ch is not None else 0]) for v in pq]
            os_ = np.quantile(vals_s, QUANTILES) if len(vals_s) else np.zeros(3)
            rows.append(
                {
                    "frame": name,
                    "car": car,
                    "mood": fr["mood"],
                    "group": g["name"],
                    "pixels": int(sel.sum()),
                    "La": round(La, 4),
                    "e": round(e, 3),
                    "D": round(D, 3),
                    "hdr_obs": [round(float(v), 4) for v in oq],
                    "hdr_pred": [round(float(v), 4) for v in pq],
                    "screen_obs": [round(float(v) * 255) for v in os_],
                    "screen_pred": [round(v * 255) for v in ps],
                }
            )
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))
    print(
        "frame  car  mood  group        px    La      e     D    | HDR p50/p90/p99 obs -> pred        | screen p50/p90/p99 obs -> pred"
    )
    for r in rows:
        ho, hp, so, sp = r["hdr_obs"], r["hdr_pred"], r["screen_obs"], r["screen_pred"]
        print(
            f"{r['frame']} {r['car']:3d} {r['mood']:4s} {r['group']:12s} {r['pixels']:4d} {r['La']:.4f} {r['e']:.2f} {r['D']:5.2f} | "
            f"{ho[0]:.3f} {ho[1]:.3f} {ho[2]:.3f} -> {hp[0]:.3f} {hp[1]:.3f} {hp[2]:.3f} | "
            f"{so[0]:3d} {so[1]:3d} {so[2]:3d} -> {sp[0]:3d} {sp[1]:3d} {sp[2]:3d}"
        )
    tail = [(r["hdr_obs"][2], r["hdr_pred"][2]) for r in rows]
    err = [abs(math.log(max(p, 1e-6) / max(o, 1e-6))) for o, p in tail]
    print(
        f"HDR p99: median |log(pred/obs)| {np.median(err):.3f} (x{math.exp(np.median(err)):.2f}), worst x{math.exp(max(err)):.2f}"
    )
    s_err = [abs(r["screen_pred"][2] - r["screen_obs"][2]) for r in rows]
    print(f"screen p99: median |pred - obs| {np.median(s_err):.0f}/255, worst {max(s_err)}/255")


def cmd_levels(_args) -> None:
    masks = {"DRL (free roam)": DRL, "DRL+Brake": DRL | BRAKE, "DRL+Head": DRL | HEAD, "DRL+Rev": DRL | REV}
    print("type  " + "  ".join(f"{k:>16s}" for k in masks))
    for t in sorted(TYPE_MASK):
        print(f"{t:#04x}  " + "  ".join(f"{level(t, m):16g}" for m in masks.values()))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("levels")
    c = sub.add_parser("compare")
    c.add_argument(
        "--dumps", required=True, help="DSF Enhanced TraceDumpDir with fNNNNN-scene-end.pfm and -p29-after-d01.bmp"
    )
    c.add_argument("--log", required=True, help="the run's dsf_enhanced.log (Trace=1)")
    c.add_argument("--frames", default=str(Path(__file__).with_name("lamp_frames.json")))
    c.add_argument("--archive", default=str(REPO / "out" / "vanilla" / "Install" / "dngvehicles.sp"))
    c.add_argument("--json", help="write the rows here")
    args = ap.parse_args(argv)
    {"levels": cmd_levels, "compare": compare}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
