"""Forza's blend for the car techniques that set none, reflectionScaler and the colour upload,
read out of default.xex (research/forza-lenses-re.md, section 7).

`evidence` re-derives every static finding from both games' executables (out/re3b/<game>/image.bin,
written by `xex.py extract`):

- the D3D render state table ({getter, setter, default} per state, indexed by state x 3) and the
  RB_BLENDCONTROL field each blend setter writes;
- every direct call of the blend setters with its constant value;
- the code that looks up each technique's "Cleanup" pass;
- the stores to pixel shader constant c52 (reflectionScaler, device + 0x1ac0) and the vectors
  they load;
- the sRGB -> linear routine the game runs on colour parameters, and the `lineargammaout`
  switch that skips it.

`predict` recomputes round 3's predictions (forza_lenses-re.md section 5) from the roster survey
(out/re3b/survey, `forza_lens.py survey`) under the confirmed blend (SRCALPHA, INVSRCALPHA),
reflectionScaler 1 and the game's linearised colour parameters.

    uv run --with capstone python tools/re/forza_blend.py evidence [fh1|fm4]
    uv run python tools/re/forza_blend.py predict [--survey DIR]
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

from dsfforza import REPO

sys.path.insert(0, str(Path(__file__).parent))
import forza_lens as fl
import ppc

BLEND_STATES = {
    0x3C: "ALPHABLENDENABLE",
    0x40: "SEPARATEALPHABLENDENABLE",
    0x48: "SRCBLEND",
    0x4C: "DESTBLEND",
    0x50: "BLENDOP",
    0x54: "SRCBLENDALPHA",
    0x58: "DESTBLENDALPHA",
    0x5C: "BLENDOPALPHA",
    0x134: "HIGHPRECISIONBLENDENABLE",
}
BLENDS = {0: "ZERO", 1: "ONE", 6: "SRCALPHA", 7: "INVSRCALPHA", 8: "DESTCOLOR", 12: "BLENDFACTOR"}
BLENDS |= {13: "INVBLENDFACTOR", 14: "CONSTANTALPHA", 15: "INVCONSTANTALPHA"}
SRC_SETTER = 0x508A06FE  # rlwimi r10, r4, 0, 27, 31: SRCBLEND into RB_BLENDCONTROL bits 0-4
LVX128, STVX128, VX128_MASK = 0x100000C3, 0x100001C3, 0xFC0007F3
C52 = 0x1AC0  # device + 0x1780 + 52 x 16: the shadow of pixel shader constant c52


def _vmx_reg(w: int) -> int:
    return ((w >> 21) & 31) | (((w >> 2) & 3) << 5)


def state_table(img: ppc.Image) -> int:
    """The render state table's address, found from the SRCBLEND setter's instruction."""
    for i, w in enumerate(img.words):
        if w == SRC_SETTER:
            setter = img.text[0] + 4 * i - 8
            hit = img.data.find(struct.pack(">I", setter))
            if hit >= 0:
                return ppc.BASE + hit - 4 - 3 * 0x48
    raise LookupError("no SRCBLEND setter")


def _r4(img: ppc.Image, site: int):
    for j in range(1, 10):
        v = img.u32(site - 4 * j)
        if v >> 26 == 14 and (v >> 21) & 31 == 4 and (v >> 16) & 31 == 0:
            return v & 0xFFFF
        if (v >> 21) & 31 == 4 or v >> 26 == 18:
            return "var"
    return "var"


def _vectors_loaded(img: ppc.Image, func: tuple[int, int]) -> list[tuple[int, tuple]]:
    """(address, 4 floats) of every constant vector the function loads with lvx128 from a
    lis/addi register."""
    regs: dict[int, int] = {}
    out = []
    for a in range(func[0], func[1], 4):
        w = img.u32(a)
        op, rd, ra = w >> 26, (w >> 21) & 31, (w >> 16) & 31
        if op == 15 and ra == 0:
            regs[rd] = (w & 0xFFFF) << 16
        elif op == 14 and ra in regs and ra != 0:
            regs[rd] = (regs[ra] + ppc.s16(w & 0xFFFF)) & 0xFFFFFFFF
        elif w & VX128_MASK == LVX128:
            rb = (w >> 11) & 31
            if rb in regs and ppc.BASE <= regs[rb] < ppc.BASE + len(img.data):
                out.append((regs[rb], struct.unpack_from(">4f", img.data, regs[rb] - ppc.BASE)))
    return out


def evidence(game: str) -> None:
    img = ppc.Image(game)
    t = state_table(img)
    print(f"== {game}: render state table {t:#x} ({{getter, setter, default}} at table + 3 x state)")
    setters = {}
    for s, name in BLEND_STATES.items():
        _get, put, default = (img.u32(t + 3 * s + k) for k in (0, 4, 8))
        setters[put] = name
        field = ""
        for a in range(put, put + 24, 4):
            w = img.u32(a)
            if w >> 26 == 20:  # rlwimi: the bits it writes
                sh, mb, me = (w >> 11) & 31, (w >> 6) & 31, (w >> 1) & 31
                field = f"rlwimi sh {sh} bits {31 - me}..{31 - mb}"
                break
        print(f"  {s:#05x} {name:26} setter {put:#x} default {default:#x}  {field}")
    print("== direct calls of the blend setters (value: count)")
    calls: dict[str, dict] = {}
    for site in range(img.text[0], img.text[1], 4):
        w = img.word(site)
        if w >> 26 == 18 and w & 1:
            d = w & 0x3FFFFFC
            d -= 0x4000000 if d & 0x2000000 else 0
            if site + d in setters:
                v = _r4(img, site)
                key = BLENDS.get(v, v) if setters[site + d] in ("SRCBLEND", "DESTBLEND") else v
                c = calls.setdefault(setters[site + d], {})
                c[key] = c.get(key, 0) + 1
    for name, c in calls.items():
        print(f"  {name:26} {c}")
    print('== "Cleanup" pass lookups')
    for a, _ in (x for x in img.strings("^Cleanup$") if x[1] == "Cleanup"):
        for site, _ in img.xrefs(a):
            f = img.function_at(site)
            print(f"  {site:#x} in {f[0]:#x}" if f else f"  {site:#x}")
    print("== stores to c52.x (device + 0x1ac0) and the constant vectors their functions load")
    for site in range(img.text[0], img.text[1], 4):
        w = img.word(site)
        if w >> 26 == 52 and w & 0xFFFF == C52:
            f = img.function_at(site)
            print(f"  stfs at {site:#x} in {f[0]:#x}")
            for a, v in _vectors_loaded(img, f):
                if len(set(v)) == 1:
                    print(f"    lvx128 {a:#x} = {v}")
    print("== sRGB -> linear routine (constants 0.055, 1/1.055, 2.4, 1/12.92, 0.04045)")
    key = struct.pack(">4f", 0.055, 0.055, 0.055, 0.0)
    hit = img.data.find(key)
    if hit >= 0:
        for site, _ in img.xrefs(ppc.BASE + hit):
            print(f"  0.055 vector {ppc.BASE + hit:#x} loaded at {site:#x}")
    for a, _ in img.strings("^lineargammaout$"):
        for site, _ in img.xrefs(a):
            for k in range(1, 12):
                w = img.u32(site + 4 * k)
                if w >> 26 == 38:  # stb: the flag
                    lis = next(img.u32(site + 4 * j) for j in range(k, 0, -1) if img.u32(site + 4 * j) >> 26 == 15)
                    flag = (((lis & 0xFFFF) << 16) + ppc.s16(w & 0xFFFF)) & 0xFFFFFFFF
                    print(f"  lineargammaout -> byte {flag:#x} = {img.data[flag - ppc.BASE]} in the image (0: convert)")
                    break


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


VECTORS = ("GlassColor", "AmbientColor")  # the survey's float4 parameters (PARAMS); the rest are scalars


def _game(p: dict) -> dict:
    """ShaderSettings values as the game uploads them: float4 RGB through sRGB -> linear."""
    return {k: ([srgb_to_linear(x) for x in v[:3]] + list(v[3:]) if k in VECTORS else v) for k, v in p.items()}


def _pct(v, qs=(10, 50, 90)):
    import numpy as np

    return [round(float(np.percentile(v, q)), 3) for q in qs] if len(v) else []


def predict(survey: Path) -> dict:
    import numpy as np

    pwl = np.array([fl.pwl(c / 255) for c in range(256)])
    fz = {g: fl.Forza.load(g) for g in ("fh1", "fm4")}
    raw_shared = {
        g: fl.parse_settings((fl.DATA / f"{g}_Normal.xml").read_bytes().decode("latin-1"), linear=False) for g in fz
    }
    cover_keep, glass_keep, cars_cover, cars_glass = [], [], set(), set()
    filt = {"authored": {}, "game": {}}
    for f in sorted(survey.glob("*.json")):
        d = json.loads(f.read_text())
        if "error" in d:
            continue
        g = d["game"]
        for s in d["subs"]:
            t = s["technique"].lower()
            fam = fl.family(t)
            tex = np.array([x for x in s["texels"] if x], float)
            if not len(tex) or fam not in ("lens", "cover", "glass") or t.startswith(("window", "black_glass")):
                continue
            base = fz[g].defaults(t) | raw_shared[g].get(t, {}) | d["settings"].get(t, {})
            f0 = ((base["FresnelIndex"][0] - 1) / (base["FresnelIndex"][0] + 1)) ** 2
            fres = base.get("FresnelScalar", [1.0])[0] * 1.0 * f0  # reflectionScaler 1, straight on
            cap = base.get("GlassMaxOpacity", [1.0])[0]
            a = tex[:, 3] / 255
            gw = base.get("GlassColor", [0, 0, 0, 0])[3]  # alpha: not converted
            if fam == "cover":
                op = np.minimum(np.maximum(a, gw) + fres, cap)
                cover_keep += list((1 - op) ** 2)
                cars_cover.add(f.stem)
            elif fam == "glass":
                op = min(max(gw + fres, 0.0), cap)
                glass_keep += [(1 - op) ** 2] * len(tex)
                cars_glass.add(f.stem)
            elif t in ("detail_glass_clear", "detail_glass_amber"):
                lin = pwl[np.clip(tex[:, :3].round().astype(int), 0, 255)]
                for label, p in (("authored", base), ("game", _game(base))):
                    fv = lin + np.array(p.get("GlassColor", [0, 0, 0, 0])[:3])
                    filt[label].setdefault(t, []).append(fv)
    out = {
        "cover glow kept (1 - opacity)^2, p10/50/90": _pct(cover_keep),
        "cover cars / subsection texels": [len(cars_cover), len(cover_keep)],
        "plain glass keeps of what is behind, p10/50/90": _pct(glass_keep),
        "plain glass cars": len(cars_glass),
    }
    for label, per in filt.items():
        for t, fv in per.items():
            fv = np.concatenate(fv)
            out[f"{t} filter ({label} GlassColor), red / green, p10/50/90"] = [_pct(fv[:, 0]), _pct(fv[:, 1])]
            out[f"{t} texels passing more than 1 ({label})"] = round(float((fv.max(1) > 1).mean()), 3)
    lamps = {}
    for g, shared in raw_shared.items():
        for t, p in shared.items():
            for k, v in p.items():
                if k.endswith("Color") and k not in ("SpecularColor", "GlassColor", "PaintColor") and any(v[:3]):
                    lamps[f"{g} {t}.{k}"] = [[round(x, 3) for x in v[:3]], [round(srgb_to_linear(x), 3) for x in v[:3]]]
    out["lamp colours, authored -> uploaded"] = {k: lamps[k] for k in sorted(lamps) if "light" in k.lower()}
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("evidence")
    e.add_argument("game", nargs="*", default=["fh1", "fm4"])
    p = sub.add_parser("predict")
    p.add_argument("--survey", type=Path, default=REPO / "out" / "re3b" / "survey")
    args = ap.parse_args(argv)
    if args.cmd == "evidence":
        for g in args.game:
            evidence(g)
    else:
        print(json.dumps(predict(args.survey), indent=1))


if __name__ == "__main__":
    main()
