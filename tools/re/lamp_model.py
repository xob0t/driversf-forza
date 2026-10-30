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
LUT is colour grade 1 (ClearDay), fitted per channel from dumped frames (scene-end PFM
against the final BMP); bloom is left out. The lamp colours, levels and the post chain are
car_render's (render_compare's renderer), so `compare` checks the renderer against the game.

    uv run python tools/re/lamp_model.py levels                       the brightness table
    uv run python tools/re/lamp_model.py compare --dumps DIR --log TRACE.log [--frames F.json]

Frames (tools/re/lamp_frames.json): the dump name, the car, the Lua light mask and the lamps'
brightness at that moment (lampprobe log), vehColourBoost, and lamp groups (box in pixels,
light ids).
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
from dsfforza.convert import lights
from dsfforza.game import car_render as cr

# --- the game's numbers (research/lights-re.md; dsfforza.convert.lights) -------------------
# The renderer the converter is measured with (car_render, render_compare) takes its lamp
# colours, levels and post chain from the same place, so `compare` checks both.

TYPE_SCALE = lights.TYPE_SCALE
COLOURS = lights.LAMP_COLOUR
HEAD, BRAKE, DRL, REV = 1, 2, 4, 32


def level(type_byte: int, mask: int) -> float:
    """Steady colour multiplier (scale x brightness) of a lamp with the Lua mask `mask` on."""
    return lights.lamp_level(type_byte, mask)


def emission(type_byte: int, colour: int, brightness: float, tint: float) -> np.ndarray:
    """c160 entry: tint x colour x scale x brightness (brightness as the probe read it)."""
    return tint * np.array(COLOURS.get(colour, COLOURS[8]), np.float64) * TYPE_SCALE[type_byte] * brightness


# --- post chain (car_render) ---------------------------------------------------------------

REC709 = cr.REC709.astype(np.float64)


def exposure(la: float, lc, mg: float) -> float:
    """e from the adapted luminance la, g_vLuminanceControl lc and g_fMiddleGrey mg."""
    return lc[1] + (mg - lc[1]) * min(1.0, max(0.0, 1.5 - lc[3] / (2 * la)))


def screen(hdr: np.ndarray, e: float, white: float) -> np.ndarray:
    return cr._linear_to_srgb(cr.display(hdr, cr.Lighting(exposure=e, white=white)))


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
        end = fr.get("facing", "rear")
        if (car, end) not in texels:
            texels[car, end] = lamp_texels(archive, car, (0.0, 0.0, 1.0) if end == "front" else (0.0, 0.0, -1.0))
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
                if lid not in texels[car, end]:
                    continue
                al, lin, ww, info = texels[car, end][lid]
                # The renderer's colour for the frame's light mask, at the frame's tint; the
                # probe's reading of the same lamp must agree.
                lit = cr.Lighting(lamps=fr["mask"])
                C = cr.dsf_lamp_rgb([info], lit)[0] * fr["tint"] / lights.VEH_COLOUR_BOOST
                probe = emission(info.type, info.colour, fr["brightness"].get(f"{info.type:#04x}", 0.0), fr["tint"])
                if not np.allclose(C, probe, rtol=1e-4, atol=1e-6):
                    print(f"{name} light {lid} ({info.type:#04x}): render {C.round(4)} != probe {probe.round(4)}")
                a.append(al)
                t.append(lin)
                w.append(ww)
                cs.append(np.broadcast_to(C, (len(al), 3)))
            a, t, w, cs = (np.concatenate(v) for v in (a, t, w, cs))
            # Under DSF window glass the lamp keeps (1 - opacity) of its glow (lights-re.md).
            emis = a[:, None] * cs * g.get("through", 1.0)
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
    for t in range(1, len(lights.LIGHT_TYPE_MASK)):
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
