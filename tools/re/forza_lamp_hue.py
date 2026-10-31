"""Lamps whose texel colour differs from the colour they glow in (research/forza-lamps-blur-re.md).

Forza's lamp shaders add `colour x state x mask` to the lit texel, with `colour` a ShaderSettings
constant per technique (BrakeColor, TailLightColor...) and the mask the texel alpha (FH1) or the
texel colour (FM4); the texel's RGB is only the unlit look (forza-lenses-re.md section 2). A clear
lamp glass over a red tail glows red with white texels. This survey lists, per car, the triangles
of the red-glowing techniques (RED_GLOW) by the hue the converter gives their mean texel
(dsf_materials.lamp_hue), with each glow colour as the shaders get it (sRGB to linear).

    uv run python tools/re/forza_lamp_hue.py survey [--survey out/re3/survey] [--out out/re4/lamp_hue.json]
    uv run python tools/re/forza_lamp_hue.py car FOR_FocusRS500_10
    uv run python tools/re/forza_lamp_hue.py built <build>/dngvehicles.sp --out out/re4/built.json

Inputs: `forza_lens.py survey` (per-car techniques and mean texels), the cars' ShaderSettings.xml
under out/cache, the shared Normal.xml (out/re3, `forza_lens.py extract`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dsfforza import REPO
from dsfforza.convert.dsf_materials import lamp_hue

sys.path.insert(0, str(Path(__file__).parent))
import forza_lens as fl

# technique -> the colour parameters its glow term reads (forza-lenses-re.md section 2; FM4's
# tail_light: BrakeColor x the texel colour)
RED_GLOW = {
    "tail_light": ("BrakeColor",),
    "tail_light_lod0": ("BrakeColor",),
    "tail_light_on": ("BrakeColor",),
    "tail_light2": ("BrakeColor",),
    "taillightst": ("TailLightColor",),
    "taillight2s": ("TailLightColor", "BrakeColor"),
    "lights_gls_tail_light": ("BrakeColor",),
    "lights_gls_taillightst": ("TailLightColor",),
    "lights_gls_taillight2s": ("TailLightColor", "BrakeColor"),
    "fogred": ("FogRedColor",),
}


def glow_colours(fz: fl.Forza, tech: str, car: dict) -> dict[str, list[float]]:
    p = fz.params(tech, car)
    d = fz.defaults(tech) if tech in fz.techs else {}
    return {k: [round(x, 4) for x in (p.get(k) or d.get(k) or [0.0] * 4)[:3]] for k in RED_GLOW[tech]}


def car_rows(media: str, survey: dict, fzs: dict) -> list[dict]:
    game = survey["game"]
    folder = REPO / "out" / "cache" / game / media
    settings = folder / "ShaderSettings.xml"
    car = fl.parse_settings(settings.read_bytes().decode("latin-1")) if settings.exists() else {}
    rows = []
    for s in survey["subs"]:
        tech = s["technique"].lower()
        if tech not in RED_GLOW:
            continue
        hues: dict[str, int] = {}
        alpha: dict[str, list[float]] = {}
        for t in s["texels"]:
            if t is None:
                continue
            h = lamp_hue(*(int(round(c)) for c in t[:3]))
            hues[h] = hues.get(h, 0) + 1
            alpha.setdefault(h, []).append(t[3])
        rows.append(
            {
                "section": s["section"],
                "technique": tech,
                "tris": s["tris"],
                "hues": hues,
                "alpha_mean": {h: round(sum(v) / len(v), 1) for h, v in alpha.items()},
                "glow": glow_colours(fzs[game], tech, car),
            }
        )
    return rows


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("survey")
    s.add_argument("--survey", type=Path, default=fl.DATA / "survey")
    s.add_argument("--out", type=Path, default=REPO / "out" / "re4" / "lamp_hue.json")
    b = sub.add_parser("built")
    b.add_argument("archive", type=Path)
    b.add_argument("--ids", help="spool ids, comma-separated (default: every car of batch-report.json next to it)")
    b.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("car")
    c.add_argument("media")
    c.add_argument("--survey", type=Path, default=fl.DATA / "survey")
    args = ap.parse_args(argv)
    if args.cmd == "built":
        if args.ids:
            ids = [int(x) for x in args.ids.split(",")]
        else:
            report = json.loads((args.archive.parent / "batch-report.json").read_text())
            ids = [r["spool_id"] for r in report["ok"]]
        built(args.archive, ids, args.out)
        return
    fzs = {g: fl.Forza.load(g) for g in ("fh1", "fm4")}
    if args.cmd == "car":
        for r in car_rows(args.media, json.loads((args.survey / f"{args.media}.json").read_text()), fzs):
            print(json.dumps(r))
        return
    out = {}
    for f in sorted(args.survey.glob("*.json")):
        d = json.loads(f.read_text())
        if "subs" in d:
            out[f.stem] = {"game": d["game"], "rows": car_rows(f.stem, d, fzs)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out))
    summary(out)


def summary(out: dict) -> None:
    by = {}
    non_red_glow = set()
    for media, d in out.items():
        for r in d["rows"]:
            k = (d["game"], r["technique"])
            e = by.setdefault(k, {"cars": set(), "tris": {}, "cars_white": set()})
            e["cars"].add(media)
            for h, n in r["hues"].items():
                e["tris"][h] = e["tris"].get(h, 0) + n
            if r["hues"].get("white", 0) + r["hues"].get("amber", 0) > 0:
                e["cars_white"].add(media)
            for v in r["glow"].values():
                if v[1] > 0.02 or v[2] > 0.02:
                    non_red_glow.add((media, r["technique"], tuple(v)))
    print("game technique: cars, cars with non-red texels, triangles by texel hue")
    for (g, t), e in sorted(by.items()):
        print(f"  {g} {t}: {len(e['cars'])} cars, {len(e['cars_white'])} with non-red texels, {e['tris']}")
    cars = {m for m, d in out.items() for r in d["rows"] if r["hues"].get("white", 0) + r["hues"].get("amber", 0)}
    print(f"cars with a red-glowing lamp triangle on non-red texels: {len(cars)} of {len(out)}")
    print(f"glow colours with green or blue over 0.02: {sorted(non_red_glow)[:20]} ({len(non_red_glow)})")


def built(archive: Path, spool_ids: list[int], out: Path) -> None:
    """Per car of a built archive: the DSF red lamps (colour b2 6) seen from behind, the share of
    their area drawn with non-red texels (dsf_materials.lamp_hue on the texel as stored) and
    those texels' alpha. lamp_model.lamp_texels samples the exterior entry, area-weighted."""
    import lamp_model as lm
    import numpy as np

    fl._idle()
    done = json.loads(out.read_text()) if out.exists() else {}
    for sid in spool_ids:
        if str(sid) in done:
            continue
        try:
            lamps = lm.lamp_texels(archive, sid, facing=(0.0, 0.0, -1.0))
        except Exception as e:  # a car the renderer can't load: recorded, not fatal
            done[str(sid)] = {"error": str(e)}
            continue
        area = {"red": 0.0, "white": 0.0, "amber": 0.0}
        alpha = {"red": 0.0, "white": 0.0, "amber": 0.0}
        for a, lin, w, info in lamps.values():
            if info.colour != 6:
                continue
            srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.maximum(lin, 0) ** (1 / 2.4) - 0.055) * 255
            for k in range(len(a)):
                h = lamp_hue(*(int(round(c)) for c in srgb[k]))
                area[h] += float(w[k])
                alpha[h] += float(w[k] * a[k])
        done[str(sid)] = {
            "area_cm2": {h: round(v * 1e4, 1) for h, v in area.items()},
            "alpha_mean": {h: round(255 * alpha[h] / area[h], 1) if area[h] else None for h in area},
        }
        out.write_text(json.dumps(done))
        print(sid, done[str(sid)], flush=True)


if __name__ == "__main__":
    main()
