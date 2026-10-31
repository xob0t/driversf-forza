"""Forza's wheel speed blur: where the game sets the blur constants (research/forza-lamps-blur-re.md).

    uv run --with capstone python tools/re/forza_blur.py stores fh1    stores and offsets to c40 / c56
    uv run python tools/re/forza_blur.py extract FOR_FocusRS500_10 ...   the wheel archive to out/re4/wheels/<car>
    uv run python tools/re/forza_blur.py constants --rpm 200 [--game fm4]   c40 / c56 the game sets
    uv run python tools/re/forza_blur.py verify                            blur_rim's closed form vs the emulator
    uv run python tools/re/forza_blur.py stats FOR_FocusRS500_10 ...       rim face, disc, settings per car

The decoded game side (research/forza-lamps-blur-re.md, confirmed-static in both default.xex):
`b = clamp(|wheel rpm| / MaxRPM, 0, 1)` (FH1 0x82d91e70); the car draw (FH1 0x824352b8, FM4
0x82316b00) sets c40 `blurRimGeomTexBlend` = (geom, frac, t, t x BlurRimAlphaAdjustment) and
c56 `blurRimTexBlendAmounts` = the weights of the blur pictures, with
`geom = linstep(GeomBlendStart, GeomBlendEnd, b)` (1 on a far LOD), `t = linstep(GeomBlendEnd, 1,
b)`, `n x t` split into an image index and its fraction (n = 2 in FH1, 3 in FM4).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ppc

from dsfforza import REPO

WHEELS = REPO / "out" / "re4" / "wheels"

PS_CONST = 0x1780  # device + 0x1780 + 16 x n: the shadow of pixel shader constant c<n> (forza-lenses-re.md section 7)


def const_sites(img: ppc.Image, regs: list[int]) -> list[tuple[int, int, str]]:
    """Instructions whose 16-bit immediate is device + PS_CONST + 16 x reg (+0..15): (site, reg, text)."""
    want = {PS_CONST + 16 * r + k: r for r in regs for k in range(16)}
    out = []
    for i, w in enumerate(img.words):
        op = w >> 26
        if op in (14, 32, 36, 48, 52, 38, 44, 54, 50):
            imm = w & 0xFFFF
            if imm in want:
                out.append((img.text[0] + 4 * i, want[imm], f"op {op} imm {imm:#x}"))
    return out


# media/cars/shared/GlobalCarAttribs.xml <BlurRim>, the same in FH1 and FM4
BLUR_RIM = {"GeomBlendStart": 0.5, "GeomBlendEnd": 0.5, "MaxRPM": 350.0, "BlurRimAlphaAdjustment": 0.3}
# blurRimSampler1..4 -> the wheel archive's picture: FH1 loads wheel0, wheel1, wheel3 (0x82da2680:
# file index 0, 1, then 3 for 2) into consecutive slots; FM4 wheel0..3 (0x828fbb38). Which slot each
# sampler reads is inferred from that order (both games end on wheel3 at full blur).
PICTURES = {"fh1": ("wheel0", "wheel1", "wheel3", None), "fm4": ("wheel0", "wheel1", "wheel2", "wheel3")}
STEPS = {"fh1": 2.0, "fm4": 3.0}  # the image-index multiplier (FH1 0x82435918 2.0, FM4 0x82317178 3.0)


def linstep(lo: float, hi: float, x: float) -> float:
    """The games' fsel form: 0 at or below lo, 1 at or above hi, linear between."""
    lo, hi = min(lo, hi), max(lo, hi)
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    return (x - lo) / (hi - lo)


def blur_amount(rpm: float, g: dict = BLUR_RIM) -> float:
    """FH1 0x82d91e70: out[0] = rpm x 2 pi / 60 (rad/s), out[1] = this."""
    return linstep(0.0, g["MaxRPM"], abs(rpm))


def constants(b: float, game: str = "fh1", far_lod: bool = False, g: dict = BLUR_RIM) -> dict[str, list[float]]:
    """c40 and c56 for a wheel at blur amount b (FH1 0x82435850..0x82435b48; FM4 the same code
    at 0x823170b0). far_lod: the model's LOD index 4 or more (b at least 0.25, geom 1)."""
    if far_lod:
        b = max(b, 0.25)
    end = min(g["GeomBlendEnd"], 1.0)
    start = max(g["GeomBlendStart"], 0.0)
    t = linstep(end, 1.0, b)
    k = STEPS[game] * t
    i = int(k)
    f = k - i
    w = [0.0, 0.0, 0.0, 0.0]
    if i < 3:
        w[i], w[i + 1] = 1.0 - f, f
    else:
        w[3] = 1.0
    geom = 1.0 if far_lod else linstep(start, end, b)
    return {"blurRimGeomTexBlend": [geom, f, t, t * g["BlurRimAlphaAdjustment"]], "blurRimTexBlendAmounts": w}


SAMPLERS = ("blurRimSampler1", "blurRimSampler2", "blurRimSampler3", "blurRimSampler4")


def verify(game: str = "fh1", trials: int = 200, seed: int = 1) -> dict:
    """blur_rim (and rim's alpha) against the closed forms, in the emulator:
    alpha = alphaDirt x geom x sat(sum w_i A_i + t x BlurRimAlphaAdjustment);
    rgb (linear) = K x sum w_i L_i + R with K proportional to PaintScale x TintColor, K and R
    from the same scene (the texture-free part: specular, reflection). rim: alpha = 1 - geom."""
    import random

    import forza_lens as fl

    rng = random.Random(seed)
    fz = fl.Forza.load(game)
    worst = {"alpha": 0.0, "rgb": 0.0, "paintscale": 0.0, "rim_alpha": 0.0}
    for _ in range(trials):
        b = rng.random()
        c = constants(b, game, far_lod=rng.random() < 0.2)
        ps = rng.uniform(0.05, 3.0)
        tint = [rng.uniform(0.2, 1.5) for _ in range(3)] + [0.0]
        scene = fl.Scene(normal=fl._norm((rng.uniform(-1, 1), rng.uniform(-1, 1), 1.0)), sun_colour=(1.0, 0.9, 0.8))
        tex = {n: tuple(rng.random() for _ in range(4)) for n in SAMPLERS}
        p = {"PaintScale": [ps, 0, 0, 0], "TintColor": tint}

        def rgb(textures, params=p, scene=scene, c=c):
            r = fz.run("blur_rim", (1, 1, 1, 1), params, scene, textures=textures, consts=c)[0]
            return [x * x for x in r.rgb], r.alpha

        got, alpha = rgb(tex)
        w = c["blurRimTexBlendAmounts"]
        g = c["blurRimGeomTexBlend"]
        want_a = g[0] * min(max(sum(w[i] * tex[n][3] for i, n in enumerate(SAMPLERS)) + g[3], 0.0), 1.0)
        worst["alpha"] = max(worst["alpha"], abs(alpha - want_a))
        zero, _ = rgb(dict.fromkeys(SAMPLERS, (0, 0, 0, 1)))
        one, _ = rgb(dict.fromkeys(SAMPLERS, (1, 1, 1, 1)))
        for ch in range(3):
            t = sum(w[i] * fl.pwl(tex[n][ch]) for i, n in enumerate(SAMPLERS))
            want = zero[ch] + (one[ch] - zero[ch]) * t
            worst["rgb"] = max(worst["rgb"], abs(got[ch] - want) / max(1e-6, one[ch]))
        p2 = dict(p, PaintScale=[2 * ps, 0, 0, 0])
        one2, _ = rgb(dict.fromkeys(SAMPLERS, (1, 1, 1, 1)), p2)
        for ch in range(3):
            k1, k2 = one[ch] - zero[ch], one2[ch] - zero[ch]
            worst["paintscale"] = max(worst["paintscale"], abs(k2 - 2 * k1) / max(1e-6, abs(k2)))
        r = fz.run("rim", (0.5, 0.5, 0.5, 1.0), {}, scene, consts=c)[0]
        worst["rim_alpha"] = max(worst["rim_alpha"], abs(r.alpha - (1 - g[0])))
    return worst


def tire_settings(game: str) -> Path:
    """media/cars/shared.zip ShaderSettings/Tire/Normal.xml of the game (out/re3, forza_lens.py
    extract), written to out/re4/<game>_Tire_Normal.xml: the first file of every wheel's settings
    (FH1 0x82543558, FM4 0x82407848)."""
    from dsfforza.formats.forzazip import ForzaZip

    dest = REPO / "out" / "re4" / f"{game}_Tire_Normal.xml"
    if not dest.exists():
        z = ForzaZip((REPO / "out" / "re3" / game / "media" / "cars" / "shared.zip").open("rb"))
        e = next(e for e in z.entries() if e.filename.lower().replace("\\", "/") == "shadersettings/tire/normal.xml")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(z.read(e))
    return dest


def wheel_settings(media: str, game: str) -> dict:
    """The wheel techniques' parameters as the game layers them: Tire/Normal.xml, then the wheel
    archive's own ShaderSettings.xml (media/Wheels/<car>/), as uploaded (forza_lens.parse_settings)."""
    import forza_lens as fl

    out = fl.parse_settings(tire_settings(game).read_bytes().decode("latin-1"))
    own = WHEELS / media / "shadersettings.xml"
    if own.exists():
        for tech, params in fl.parse_settings(own.read_bytes().decode("latin-1")).items():
            out.setdefault(tech, {}).update(params)
    return out


def texture(path: Path):
    import numpy as np

    from dsfforza.convert.dash_scene import decode_xds

    return np.asarray(decode_xds(path.read_bytes()), float)


def uv_mask(sections, techs, size: tuple[int, int], lod: int):
    """Texels (h, w) the triangles of these techniques at `lod` cover (pixel centres)."""
    import numpy as np

    h, w = size
    mask = np.zeros((h, w), bool)
    for sec in sections:
        for sub in sec.subsections:
            if sub.lod != lod or sub.material.lower() not in techs:
                continue
            pool = sec.vertices_for(sub)
            xo, xs, yo, ys = sub.uv_transform[:4]
            for t in sub.triangles():
                uv = np.array([(pool[i].uv0[0] * xs + xo, pool[i].uv0[1] * ys + yo) for i in t]) * (w, h)
                x0, y0 = np.floor(uv.min(0)).astype(int)
                x1, y1 = np.ceil(uv.max(0)).astype(int)
                x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
                if x1 <= x0 or y1 <= y0:
                    continue
                gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
                (ax, ay), (bx, by), (cx, cy) = uv
                d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
                if abs(d) < 1e-12:
                    continue
                l1 = ((by - cy) * (gx - cx) + (cx - bx) * (gy - cy)) / d
                l2 = ((cy - ay) * (gx - cx) + (ax - cx) * (gy - cy)) / d
                inside = (l1 >= 0) & (l2 >= 0) & (l1 + l2 <= 1)
                mask[y0:y1, x0:x1] |= inside
    return mask


RIM_TECHS = ("rim", "rim_v2", "rim_bump_metal", "rim_bump_metal2", "outer_rim", "rim_simple")
BLUR_TECHS = ("blur_rim", "blur_rim_v2")


def stats(media: str) -> dict:
    """One car's wheel: the rim face Forza draws (rim techniques on wheel.xds), the disc at full
    blur (blur_rim on the last picture), both as linear albedo (PaintScale x TintColor x the
    PWL-decoded texel mean over the technique's texels) and as the emulator draws them in
    forza_lens's day scene, straight on; the PaintScale each settings chain gives."""
    import forza_lens as fl
    import numpy as np

    from dsfforza.convert import forza_materials as fm
    from dsfforza.formats import carbin

    folder = WHEELS / media
    game = (folder / "game.txt").read_text().strip()
    sections = carbin.parse(next(folder.glob("*.carbin")).read_bytes())
    lod = 1 if any(s.lod == 1 for sec in sections for s in sec.subsections) else 0
    present = {s.material.lower() for sec in sections for s in sec.subsections if s.lod == lod}
    rim_tech = next((t for t in RIM_TECHS if t in present), None)
    blur_tech = next((t for t in BLUR_TECHS if t in present), None)
    gs = wheel_settings(media, game)
    car_dir = REPO / "out" / "cache" / game / media
    master = fm.shader_settings(car_dir)
    pwl = np.vectorize(fl.pwl)
    out = {
        "media": media,
        "game": game,
        "lod": lod,
        "rim_tech": rim_tech,
        "blur_tech": blur_tech,
        "wheel_settings_file": (folder / "shadersettings.xml").exists(),
    }

    def ps(tech):
        return gs.get(tech, {}).get("PaintScale", [1.0])[0], gs.get(tech, {}).get("TintColor", [1, 1, 1, 0])[:3]

    fz = fl.Forza.load(game)
    scene = fl.Scene(normal=(0.0, 0.0, 1.0), eye=(0.0, 0.0, 1.0))
    if rim_tech:
        wheel = texture(folder / "wheel.xds")
        m = uv_mask(sections, (rim_tech,), wheel.shape[:2], lod)
        lin = pwl(np.clip(wheel[m][:, :3], 0, 1)).mean(0)
        s_game, tint = ps(rim_tech)
        out["rim"] = {
            "texels": int(m.sum()),
            "texel_srgb_mean": [round(float(x), 1) for x in 255 * wheel[m][:, :3].mean(0)],
            "linear_mean": [round(float(x), 4) for x in lin],
            "paintscale_game": s_game,
            "paintscale_master": fm.wheel_paint_scale(rim_tech.replace("_v2", "_V2"), master),
            "albedo_game": [round(float(x * s_game * t), 4) for x, t in zip(lin, tint)],
        }
        r0 = fz.run(rim_tech, (0, 0, 0, 1), gs.get(rim_tech, {}), scene, consts=constants(0.0, game))[0]
        r1 = fz.run(rim_tech, (1, 1, 1, 1), gs.get(rim_tech, {}), scene, consts=constants(0.0, game))[0]
        out["rim"]["drawn_linear"] = [round(a * a + (b * b - a * a) * x, 4) for a, b, x in zip(r0.rgb, r1.rgb, lin)]
    if blur_tech:
        pics = {
            k: texture(folder / f"{k}.xds")
            for k in ("wheel0", "wheel1", "wheel2", "wheel3")
            if (folder / f"{k}.xds").exists()
        }
        m = uv_mask(sections, (blur_tech,), pics["wheel3"].shape[:2], lod)
        s_game, tint = ps(blur_tech)
        out["blur_paintscale_game"] = s_game
        for k, img in pics.items():
            px = img[m]
            a = px[:, 3]
            lin = pwl(np.clip(px[:, :3], 0, 1))
            wmean = (lin * a[:, None]).sum(0) / max(a.sum(), 1e-6)
            out[k] = {
                "coverage": round(float(a.mean()), 3),
                "alpha_full_blur": round(float(np.clip(a + BLUR_RIM["BlurRimAlphaAdjustment"], 0, 1).mean()), 3),
                "linear_mean_covered": [round(float(x), 4) for x in wmean],
                "albedo_game": [round(float(x * s_game * t), 4) for x, t in zip(wmean, tint)],
            }
        c = constants(1.0, game)
        tex0 = dict.fromkeys(SAMPLERS, (0, 0, 0, 1))
        tex1 = dict.fromkeys(SAMPLERS, (1, 1, 1, 1))
        r0 = fz.run(blur_tech, (1, 1, 1, 1), gs.get(blur_tech, {}), scene, textures=tex0, consts=c)[0]
        r1 = fz.run(blur_tech, (1, 1, 1, 1), gs.get(blur_tech, {}), scene, textures=tex1, consts=c)[0]
        lin3 = out["wheel3"]["linear_mean_covered"]
        out["disc_full_blur_drawn_linear"] = [
            round(a * a + (b * b - a * a) * x, 4) for a, b, x in zip(r0.rgb, r1.rgb, lin3)
        ]
    return out


def survey(out: Path) -> None:
    """stats() for every car of tools/batches/all.txt that has a wheel archive, one JSON line
    each (resumable); the archives are extracted to WHEELS first."""
    import json

    import forza_lens as fl

    fl._idle()
    done = set()
    if out.exists():
        done = {json.loads(ln)["media"] for ln in out.read_text().splitlines() if ln.strip()}
    batch = [ln.strip() for ln in (REPO / "tools" / "batches" / "all.txt").read_text().splitlines()]
    with out.open("a") as f:
        for media in (ln for ln in batch if ln and not ln.startswith("#")):
            if media in done:
                continue
            try:
                if not (WHEELS / media / "game.txt").exists():
                    extract(media)
                row = stats(media)
            except Exception as e:  # no wheel archive, no blur picture...: recorded
                row = {"media": media, "error": f"{type(e).__name__}: {e}"}
            f.write(json.dumps(row) + chr(10))
            f.flush()


def extract(media: str, root: Path = WHEELS) -> Path:
    """Every file of the car's wheel archive (media/wheels/<car>.zip on its disc: wheel.xds,
    wheel0..3.xds, the wheel carbin) into root/<car>; the car's cache stamp names the disc."""
    import json

    from dsfforza.build import forza_roster, port_car
    from dsfforza.formats import stfs
    from dsfforza.formats.forzazip import ForzaZip
    from dsfforza.formats.xiso import XIso

    folder = next(p for g in ("fh1", "fm4") if (p := REPO / "out" / "cache" / g / media / "source.json").exists())
    st = json.loads(folder.read_text())
    src = forza_roster.Source(st["game"], st["where"], st["path"], 0, 0, "car", st["iso"], st.get("package", ""))
    path, prefix = port_car.wheel_source(src, media)
    image = XIso(src.iso)
    dest = root / media
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if src.package:
            archive = ForzaZip(stfs.in_image(image, image.find(src.package)).open(path))
        else:
            archive = ForzaZip(image.open(image.find(path)))
        keep = ("wheel.xds", "wheel0.xds", "wheel1.xds", "wheel2.xds", "wheel3.xds", "shadersettings.xml")
        for info in archive.entries():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.lower().startswith(prefix):
                continue
            leaf = name[len(prefix) :].split("/")[-1].lower()
            if leaf in keep or (leaf.endswith(".carbin") and not leaf.endswith("_s.carbin")):
                (dest / leaf).write_bytes(archive.read(info))
    finally:
        image.close()
    (dest / "game.txt").write_text(st["game"].lower())
    return dest


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stores")
    s.add_argument("game", choices=("fh1", "fm4"))
    s.add_argument("--regs", default="40,56")
    e = sub.add_parser("extract")
    e.add_argument("cars", nargs="+")
    v = sub.add_parser("verify")
    v.add_argument("--game", default="fh1")
    v.add_argument("--trials", type=int, default=200)
    k = sub.add_parser("constants")
    k.add_argument("--rpm", type=float, required=True)
    k.add_argument("--game", default="fh1")
    k.add_argument("--far", action="store_true")
    sv = sub.add_parser("survey")
    sv.add_argument("--out", type=Path, default=REPO / "out" / "re4" / "wheel-survey.jsonl")
    st = sub.add_parser("stats")
    st.add_argument("cars", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "survey":
        survey(args.out)
        return
    if args.cmd == "stats":
        import json

        for media in args.cars:
            if not (WHEELS / media).exists():
                extract(media)
            print(json.dumps(stats(media)))
        return
    if args.cmd == "verify":
        print(args.game, verify(args.game, args.trials))
        return
    if args.cmd == "constants":
        b = blur_amount(args.rpm)
        print(f"b {b:.4f}", constants(b, args.game, args.far))
        return
    if args.cmd == "extract":
        for media in args.cars:
            d = extract(media)
            print(media, sorted(p.name for p in d.iterdir()))
        return
    img = ppc.Image(args.game)
    for site, reg, text in const_sites(img, [int(r) for r in args.regs.split(",")]):
        f = img.function_at(site)
        print(f"c{reg}: {site:#010x} {text}  in {f[0]:#010x}" if f else f"c{reg}: {site:#010x} {text}")


if __name__ == "__main__":
    main()
