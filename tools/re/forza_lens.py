"""Forza's lamp, lens and glass shaders run on chosen texels (research/forza-lenses-re.md).

Runs the race pixel shaders of FH1's or FM4's media/shaders/cars/shaders_v16.fxobj with the
Xenos emulator (`xenos.py`) on one pixel: a texel (Lights atlas RGBA), the technique's
material parameters (the car's ShaderSettings.xml over the shared Normal.xml, over the
shader's own defaults), a day scene (sun, sky ambient, sky reflection) and the lamp's state.
Every pass of the technique is run and blended over what lies behind with the pass's blend
(the effect's render states; techniques that set none are taken as SRCALPHA, INVSRCALPHA,
the game's transparent bucket, which is inferred). Colours are in the shaders' output
encoding, sqrt of linear (every car shader ends with sqrt), which is also the space the
render target blends in.

    uv run python tools/re/forza_lens.py probe TECH --texel 200,30,30,255 [--behind R,G,B] [--lamp 1]
    uv run python tools/re/forza_lens.py derive               formulas checked by perturbation
    uv run python tools/re/forza_lens.py extract              shaders_v16.fxobj, Normal.xml from the discs

`--param Name=v[,v,v,v]` overrides a material parameter; `--car DIR` reads that car's
ShaderSettings.xml (out/cache/fh1|fm4/<car>).
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza import REPO

sys.path.insert(0, str(Path(__file__).parent))
import fxl
import xenos

DATA = REPO / "out" / "re3"
BLEND_DEFAULT = (6, 7)  # SRCALPHA, INVSRCALPHA for techniques whose passes set no blend (inferred)


@dataclass
class Scene:
    """Day lighting. Directions point from the surface: `sun` toward the sun, `eye` toward the camera."""

    normal: tuple[float, float, float] = (0.0, 0.0, 1.0)
    eye: tuple[float, float, float] = (0.0, 0.3, 1.0)
    sun: tuple[float, float, float] = (0.3, 0.8, 0.5)
    sun_colour: tuple[float, float, float] = (1.0, 1.0, 1.0)
    sky: tuple[float, float, float] = (0.25, 0.27, 0.3)  # SH ambient at the normal, linear
    env: tuple[float, float, float] = (0.55, 0.6, 0.7)  # env map texel (encoded: shaders square it)
    shadow: float = 1.0  # shadow mask: 1 lit
    direct: float = 1.0  # VS direct-light term (oT2.w)
    reflection: float = 1.0  # reflectionScaler
    cube_dampner: float = 1.0  # oT4.w


def pwl(g: float) -> float:
    """Xenos gamma texel (0-1) to linear: the piecewise-linear curve of xenia's xenos.cc
    PWLGammaToLinear. The Lights and nodamage atlases' fetch constants set sign_xyz = 3
    (gamma) and sign_w = 0, so RGB is decoded by this and alpha is read as stored."""
    g = min(max(g, 0.0), 1.0)
    if g >= 96 / 255:
        scale, offset = (8 / 1024, -1024.0) if g >= 192 / 255 else (4 / 1024, -256.0)
    else:
        scale, offset = (2 / 1024, -64.0) if g >= 64 / 255 else (1 / 1024, 0.0)
    v = g * (255 * 1024 * scale) + offset
    v += math.trunc(v * scale)
    return v / 1023


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / n for x in v)


def parse_settings(text: str) -> dict[str, dict[str, list[float]]]:
    """A ShaderSettings XML as {technique: {param: [4 floats]}}."""
    out: dict[str, dict[str, list[float]]] = {}
    for m in re.finditer(r"<([A-Za-z0-9_]+)>((?:(?!<[A-Za-z0-9_]+>).)*?)</\1>", text, re.S):
        params = {}
        for p in re.finditer(r"<([A-Za-z0-9_]+)\s+([^/>]*)/>", m.group(2)):
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', p.group(2)))
            try:
                if "value" in attrs:
                    params[p.group(1)] = [float(attrs["value"])] + [0.0] * 3
                elif "r" in attrs:
                    params[p.group(1)] = [float(attrs.get(k, 0)) for k in "rgba"]
            except ValueError:
                continue
        out[m.group(1).lower()] = params
    return out


@dataclass
class PassResult:
    name: str
    blend: tuple[int, int]
    rgb: tuple[float, float, float]  # encoded (sqrt linear)
    alpha: float


@dataclass
class Forza:
    game: str = "fh1"
    data: bytes = b""
    techs: dict = field(default_factory=dict)
    shared: dict = field(default_factory=dict)
    bucket_blend: tuple[int, int] = BLEND_DEFAULT  # for passes that set no blend: the game's

    @classmethod
    def load(cls, game: str = "fh1", root: Path = DATA) -> Forza:
        data = (root / game / "media" / "shaders" / "cars" / "shaders_v16.fxobj").read_bytes()
        fx = fxl.Effect(data)
        shared = parse_settings((root / f"{game}_Normal.xml").read_bytes().decode("latin-1"))
        return cls(game, data, {t.name.lower(): t for t in fx.techniques()}, shared)

    def defaults(self, tech: str) -> dict[str, list[float]]:
        """The technique's colour passes' pixel shader constants with their CTAB defaults."""
        out = {}
        for p in self.techs[tech.lower()].passes:
            if p.name == "Cleanup":
                continue
            for c in p.ps.constants.values():
                if c.register_set == "float4":
                    out.setdefault(c.name, list(c.default[:4]) if c.default else [0.0] * 4)
        return out

    def params(self, tech: str, car: dict | None = None) -> dict[str, list[float]]:
        p = dict(self.shared.get(tech.lower(), {}))
        if car:
            p.update(car.get(tech.lower(), {}))
        return p

    def run(
        self,
        tech: str,
        texel: tuple[float, float, float, float],
        params: dict[str, list[float]] | None = None,
        scene: Scene | None = None,
        lamp: float = 0.0,
        car: dict | None = None,
    ) -> list[PassResult]:
        scene = scene or Scene()
        t = self.techs[tech.lower()]
        mat = self.params(tech, car)
        mat.update(params or {})
        out = []
        for p in t.passes:
            if p.name == "Cleanup":
                continue  # an auxiliary pass the race view doesn't composite (see the note)
            sh = p.ps
            regs = fxl.constant_file(sh, self.data)
            names = {c.name: c for c in sh.constants.values()}
            n = _norm(scene.normal)
            pos = (0.0, 0.0, 0.0)
            eye = tuple(5 * x for x in _norm(scene.eye))
            game = {
                "psLightDirWS": list(_norm(scene.sun)) + [0.0],
                "shDirectionalColour": list(scene.sun_colour) + [0.0],
                "psFogColor": [0.0, 0.0, 0.0, 0.0],
                "c_eyePosWS": list(eye) + [1.0],
                "alphaDirtAmount": [1.0, 0.0, 0.0, 0.0],
                "reflectionScaler": [scene.reflection] * 4,
                "dynamicLightsAmount": [lamp, 0.0, 0.0, 0.0],
                "brakeReverseAmount": [lamp, 0.0, 0.0, 0.0],
                "psLightMapColour": [0.0] * 4,
            }
            for name, value in list(game.items()) + list(mat.items()):
                c = names.get(name)
                if c is not None and c.register_set == "float4":
                    regs[c.index] = list(value)
            bools = {c.index: False for c in sh.constants.values() if c.register_set == "bool"}
            samplers = {c.index: c.name for c in sh.constants.values() if c.register_set == "sampler"}

            def fetch(slot, coords, samplers=samplers):
                name = samplers.get(slot, "")
                if name in ("LightsSampler", "baseSampler"):
                    return [pwl(texel[0]), pwl(texel[1]), pwl(texel[2]), texel[3]]
                if name.startswith("env"):
                    return list(scene.env) + [1.0]
                if name.startswith(("ShadowMask", "trackLightmap")):
                    return [scene.shadow] * 4
                if name.startswith("Damage"):
                    return [1.0, 1.0, 1.0, 1.0]
                return [0.0, 0.0, 0.0, 1.0]

            m = xenos.Machine(sh.code, regs, bools, fetch)
            if self.game == "fm4":  # FM4's VS: r4.xy the track lightmap's UV, r5 the SH term
                inputs = {
                    0: [0.5, 0.5, 0.0, 0.0],
                    1: list(n) + [0.0],
                    2: list(pos) + [scene.direct],
                    3: [0.0] * 4,
                    4: [0.5, 0.5, 0.0, 0.0],
                    5: list(scene.sky) + [scene.cube_dampner],
                }
            else:  # FH1's VS: r4 the SH term, r5 the clip position (the shadow mask's UV)
                inputs = {
                    0: [0.5, 0.5, 0.0, 0.0],
                    1: list(n) + [0.0],
                    2: list(pos) + [scene.direct],
                    3: [0.0] * 4,
                    4: list(scene.sky) + [scene.cube_dampner],
                    5: [0.0, 0.0, 0.5, 1.0],
                }
            dead = vs_zeroed(p.vs)
            for reg, comp in dead:
                if reg in inputs:
                    inputs[reg][comp] = 0.0
            o = m.run(inputs).get(0, [0.0] * 4)
            blend = (p.render_states[0x48], p.render_states[0x4C]) if p.render_states.get(0x3C) else self.bucket_blend
            out.append(PassResult(p.name, blend, tuple(o[:3]), o[3]))
        return out


# --- the spec: closed forms of the passes (research/forza-lenses-re.md) -------------------------

LAMP_COLOUR = {  # FH1 technique -> (colour parameter, state constant, component)
    "tail_light": ("BrakeColor", "dynamicLightsAmount", 0),
    "tail_light2": ("BrakeColor", "dynamicLightsAmount", 0),
    "reverse_light": ("ReverseLightColor", "dynamicLightsAmount", 1),
    "indicator_left": ("IndicatorLightColor", "dynamicLightsAmount", 2),
    "indicator_right": ("IndicatorLightColor", "dynamicLightsAmount", 3),
    "taillightst": ("TailLightColor", "dynamicLights2", 0),
    "hidhead": ("HidHeadlightColor", "dynamicLights2", 0),
    "oldhead": ("OldHeadlightColor", "dynamicLights2", 0),
    "xenonhead": ("XenonHeadlightColor", "dynamicLights2", 0),
    "lights_gls_tail_light": ("BrakeColor", "dynamicLightsAmount", 0),
    "lights_gls_reverse_light": ("ReverseLightColor", "dynamicLightsAmount", 1),
    "lights_gls_indicator_left": ("IndicatorLightColor", "dynamicLightsAmount", 2),
    "lights_gls_indicator_right": ("IndicatorLightColor", "dynamicLightsAmount", 3),
    "lights_gls_hidhead": ("HidHeadlightColor", "dynamicLights2", 0),
    "lights_gls_oldhead": ("OldHeadlightColor", "dynamicLights2", 0),
    "lights_gls_taillightst": ("TailLightColor", "dynamicLights2", 0),
    "tail_light_lod0": ("BrakeColor", "dynamicLightsAmount", 0),
    "tail_light_on": ("BrakeColor", "dynamicLightsAmount", 0),
    "reverse_light_lod0": ("ReverseLightColor", "dynamicLightsAmount", 1),
    "fogred": ("FogRedColor", "dynamicLights2", 3),
    "drlwhite": ("DRLWhiteColor", "dynamicLights2", 2),
    "slorange": ("SideLightsOrangeColor", "dynamicLights3", 2),
}
LAMP_COLOUR_FM4 = {
    "tail_light": ("BrakeColor", "brakeReverseAmount", 0),
    "tail_light2": ("BrakeColor", "brakeReverseAmount", 0),
    "tail_light_on": ("BrakeColor", "brakeReverseAmount", 0),
    "tail_light_lod0": ("BrakeColor", "brakeReverseAmount", 0),
    "reverse_light": ("ReverseLightColor", "brakeReverseAmount", 1),
    "reverse_light_lod0": ("ReverseLightColor", "brakeReverseAmount", 1),
}


def _state(state, name, comp):
    return state.get(name, [0.0] * 4)[comp]


def _tail_brake(g, st, i):
    """taillight2S: the tail colour, the brake colour as the brake comes on."""
    tail, brake = _state(st, "dynamicLights2", 0), _state(st, "dynamicLightsAmount", 0)
    colour = g("TailLightColor", i) + brake * (g("BrakeColor", i) - g("TailLightColor", i))
    return colour * min(max(tail + brake, 0.0), 1.0)


def _brake_reverse(g, st, i):
    brake, rev = _state(st, "dynamicLightsAmount", 0), _state(st, "dynamicLightsAmount", 1)
    colour = g("BrakeColor", i) - rev * (g("BrakeColor", i) - g("ReverseLightColor", i))
    return colour * min(max(brake + rev, 0.0), 1.0)


def _brake_indicator(side):
    def f(g, st, i):
        """brinl/brinr: brake, or the indicator in indicator mode (dynamicLights3.x left, .y right)."""
        brake, ind = _state(st, "dynamicLightsAmount", 0), _state(st, "dynamicLightsAmount", side)
        return g("BrakeColor", i) * (brake + _state(st, "dynamicLights3", side - 2) * (ind - brake))

    return f


def _tail_side(side):
    def f(g, st, i):
        """lindtailside/rindtailside: the brighter of tail and brake, or the red indicator."""
        tail = g("TailLightColor", i) * _state(st, "dynamicLights2", 0)
        brake = g("BrakeColor", i) * _state(st, "dynamicLightsAmount", 0)
        ind = g("IndicatorRedLightColor", i) * _state(st, "dynamicLightsAmount", side)
        base = max(tail, brake)
        return base + _state(st, "dynamicLights3", side - 2) * (ind - base)

    return f


def _drl_switch(g, st, i):
    """drlwhiteswitch: the DRL, off while the head lamps are on."""
    return g("DRLWhiteColor", i) * _state(st, "dynamicLights2", 2) * (1 - _state(st, "dynamicLights2", 0))


COMBINED = {
    "drlwhiteswitch": _drl_switch,
    "taillight2s": _tail_brake,
    "lights_gls_taillight2s": _tail_brake,
    "brkreverse": _brake_reverse,
    "brinl": _brake_indicator(2),
    "brinr": _brake_indicator(3),
    "lindtailside": _tail_side(2),
    "rindtailside": _tail_side(3),
}
# Opaque lamp techniques whose output alpha is GlassMaxOpacity (the rest write 1).
GMAX_ALPHA = ("head_light", "tail_light", "reverse_light")
GLASS_TECHNIQUES = ("glass", "glass_2", "red_glass", "window", "window_2", "cockpit_glass")


def family(tech: str) -> str:
    t = tech.lower()
    if t.startswith("detail_glass"):
        return "lens"
    if t.startswith("lights_gls_"):
        return "lamp_glass"
    if t == "lights_glass":
        return "cover"
    if t in GLASS_TECHNIQUES:
        return "glass"
    if t.startswith("black_glass"):
        return "black_glass"
    if t == "reflector":
        return "reflector"
    return "lamp"


def spec(fz: Forza, tech: str, texel, params: dict, scene: Scene, state: dict | None = None) -> list[PassResult]:
    """The decoded maths of a technique's colour passes for a lit pixel (shadow 1), with no
    specular highlight (SpecularAmount's term) and no fog: what Forza.run gives then."""
    t = tech.lower()
    p = fz.defaults(tech)
    p.update(fz.params(tech))
    p.update(params)
    state = state or {}

    def g(name, i=0):
        return p.get(name, [0.0] * 4)[i]

    n, sun, v = _norm(scene.normal), _norm(scene.sun), _norm(scene.eye)
    ndl = sum(a * b for a, b in zip(n, sun, strict=True))
    ndv = min(max(sum(a * b for a, b in zip(n, v, strict=True)), 0.0), 1.0)
    hl = min(max((0.5 * ndl + 0.5) ** 2, 0.0), 1.0)
    eta = g("FresnelIndex")
    f0 = ((eta - 1) / (eta + 1)) ** 2
    fres = f0 + (1 - f0) * (1 - ndv) ** 5
    refl = scene.reflection
    lin_t = [pwl(x) for x in texel[:3]]
    alpha_t = texel[3]
    e2 = [e * e for e in scene.env]
    fm4 = fz.game == "fm4"
    sun_c = (1.0, 1.0, 1.0) if fm4 else scene.sun_colour
    first = next(q for q in fz.techs[t].passes if q.name != "Cleanup")
    dead = vs_zeroed(first.vs)
    sh_reg = 5 if fm4 else 4  # the SH interpolant (w: the cube map dampner)
    sh = (0.0, 0.0, 0.0) if (sh_reg, 0) in dead else scene.sky
    damp = 0.0 if (sh_reg, 3) in dead else scene.cube_dampner
    fam = family(t)

    def enc(c):
        return tuple(math.sqrt(max(x, 0.0)) for x in c)

    gmax = g("GlassMaxOpacity")
    if fam == "lens":
        if t == "detail_glass_red":
            d, lv = g("DarkValues"), g("LightValues")
            filt = (d + (lv - d) * lin_t[0], lin_t[1], lin_t[2])
        else:
            filt = tuple(lin_t[i] + g("GlassColor", i) for i in range(3))
        k = g("Glancing") + (g("Direct") - g("Glancing")) * (
            g("FresnelScale") * ndv ** g("FresnelPower") + g("FresnelBiasA")
        )
        return [PassResult("p0", (8, 0), enc(filt), 1.0), PassResult("p1", (1, 1), tuple(e * k for e in e2), 1.0)]
    if fam in ("glass", "black_glass"):
        fr = min(max(g("FresnelScalar") * refl * fres, 0.0), 1.0)
        # GlassColor shaped by the sun direction (not its colour): 0.4 + 0.6 hl
        shade = 0.4 + 0.6 * hl
        gc = (0.0, 0.0, 0.0) if fam == "black_glass" else tuple(g("GlassColor", i) * shade for i in range(3))
        c = tuple(gc[i] + (e2[i] - gc[i]) * fr for i in range(3))
        if fam == "black_glass":
            a = 1.0
        else:
            a = min(max(g("GlassColor", 3) + g("FresnelScalar") * refl * fres, 0.0), gmax)
        return [PassResult("p0", fz.bucket_blend, enc(c), a)]
    glow = [0.0, 0.0, 0.0]
    table = LAMP_COLOUR_FM4 if fm4 else LAMP_COLOUR
    weight = lin_t if fm4 else [alpha_t] * 3
    if t in table:
        cname, sname, comp = table[t]
        amount = state.get(sname, [0.0] * 4)[comp]
        glow = [g(cname, i) * amount * weight[i] for i in range(3)]
    elif not fm4 and t in COMBINED:
        glow = [weight[i] * COMBINED[t](g, state, i) for i in range(3)]
    if fam in ("lamp_glass", "cover"):
        # lights_gls_* scale the reflection by the cube map dampner; lights_glass does not
        fr = min(max(g("FresnelScalar") * refl * fres * (damp if fam == "lamp_glass" else 1.0), 0.0), 1.0)
        gc = [g("GlassColor", i) for i in range(3)]
        amb = sh
        c = [
            lin_t[i]
            * (
                gc[i] * g("AmbientColor", i) * amb[i]
                + gc[i] * g("PaintScale") * sun_c[i] * hl
                + (e2[i] - gc[i] * g("PaintScale") * sun_c[i] * hl) * fr
            )
            + glow[i]
            for i in range(3)
        ]
        if fam == "cover":
            a = min(max(max(alpha_t, g("GlassColor", 3)) + g("FresnelScalar") * refl * fres, 0.0), gmax)
        else:
            a = gmax
        return [PassResult("p0", fz.bucket_blend, enc(c), a)]
    # opaque lamps and reflectors
    gc = [g("GlassColor", i) for i in range(3)] if fam == "reflector" else [1.0] * 3
    if fm4:  # FM4 adds its ambient flat, not times the texel
        c = [
            g("AmbientColor", i) * sh[i] + lin_t[i] * (gc[i] * g("PaintScale") * hl + g("CubeScale") * e2[i]) + glow[i]
            for i in range(3)
        ]
    else:
        c = [
            lin_t[i]
            * (gc[i] * (g("PaintScale") * sun_c[i] * hl + g("AmbientColor", i) * sh[i]) + g("CubeScale") * e2[i])
            + glow[i]
            for i in range(3)
        ]
    if fam == "reflector":
        a = min(max(max(alpha_t, g("GlassColor", 3)) + g("FresnelScalar") * refl * fres, 0.0), gmax)
    elif t.startswith(GMAX_ALPHA):
        a = min(1.0 + g("FresnelScalar") * refl * fres, gmax)
    else:
        a = 1.0
    return [PassResult("p0", fz.bucket_blend, enc(c), a)]


GLOW_COLOURS = (
    "BrakeColor",
    "ReverseLightColor",
    "IndicatorLightColor",
    "IndicatorRedLightColor",
    "TailLightColor",
    "HidHeadlightColor",
    "OldHeadlightColor",
    "XenonHeadlightColor",
    "FogRedColor",
    "DRLWhiteColor",
    "SideLightsOrangeColor",
)


def verify(fz: Forza, techs: list[str], trials: int = 300, seed: int = 1) -> dict:
    """Max |spec - emulator| per technique over random texels, material parameters, normals,
    views, sun, sky, reflection and lamp states (lit pixel, SpecularAmount 0, no fog)."""
    import random

    rnd = random.Random(seed)
    out = {}
    for t in techs:
        if t.lower() not in fz.techs:
            continue
        worst = [0.0, 0.0]
        for _ in range(trials):
            texel = tuple(rnd.random() for _ in range(4))
            params = {"SpecularAmount": [0.0] * 4}
            for name in (
                "GlassColor",
                "PaintScale",
                "AmbientColor",
                "CubeScale",
                "FresnelScalar",
                "DarkValues",
                "LightValues",
            ):
                params[name] = [rnd.random() for _ in range(4)]
            params["GlassMaxOpacity"] = [0.5 + 0.5 * rnd.random(), 0.0, 0.0, 0.0]
            for cname in GLOW_COLOURS:
                params[cname] = [2 * rnd.random() for _ in range(4)]
            state = {
                k: [rnd.random() for _ in range(4)]
                for k in ("dynamicLightsAmount", "dynamicLights2", "dynamicLights3", "brakeReverseAmount")
            }
            sc = Scene(
                normal=_norm((rnd.uniform(-0.5, 0.5), rnd.uniform(-0.5, 0.5), 1.0)),
                eye=_norm((rnd.uniform(-1, 1), rnd.uniform(-1, 1), 1.0)),
                sun=_norm((rnd.uniform(-1, 1), rnd.uniform(0, 1), rnd.uniform(-0.2, 1))),
                sun_colour=tuple(rnd.uniform(0.2, 1.5) for _ in range(3)),
                sky=tuple(rnd.uniform(0, 0.6) for _ in range(3)),
                env=tuple(rnd.random() for _ in range(3)),
                reflection=rnd.random(),
            )
            a = fz.run(t, texel, params | state, sc)
            b = spec(fz, t, texel, params, sc, state)
            for pa, pb in zip(a, b, strict=True):
                worst[0] = max(worst[0], *(abs(x - y) for x, y in zip(pa.rgb, pb.rgb, strict=True)))
                worst[1] = max(worst[1], abs(pa.alpha - pb.alpha))
        out[t] = {"rgb": round(worst[0], 6), "alpha": round(worst[1], 6)}
    return out


def vs_zeroed(vs) -> set[tuple[int, int]]:
    """(interpolant, component) pairs of 0..5 the vertex shader leaves 0: not exported, or
    exported only by `sgts dst, -|x|` (FH1's compiler zeroes unused interpolants that way)."""
    if vs is None:
        return set()
    cfs, words = xenos.decode_cf(vs.code)
    live = set()
    for cf in cfs:
        if cf.op not in xenos.EXECS:
            continue
        for k in range(cf.count):
            ins = xenos.decode(words, cf.addr + k, bool((cf.seq >> (2 * k)) & 1))
            if isinstance(ins, xenos.Fetch) or not ins.export or ins.vdst > 15:
                continue
            for j in range(4):
                if ins.vmask >> j & 1 or ins.one_mask >> j & 1:
                    live.add((ins.vdst, j))
                if ins.smask >> j & 1:
                    zero = ins.sop == "sgts" and ins.sargs and ins.sargs[0].negate and ins.sargs[0].absolute
                    if not zero:
                        live.add((ins.vdst, j))
    return {(r, j) for r in range(6) for j in range(4)} - live


def _factor(code: int, src, sa: float, dst):
    one = (1.0, 1.0, 1.0)
    return {
        0: (0.0, 0.0, 0.0),
        1: one,
        4: src,
        5: tuple(1 - x for x in src),
        6: (sa,) * 3,
        7: (1 - sa,) * 3,
        8: dst,
        9: tuple(1 - x for x in dst),
    }[code]


def composite(behind, passes: list[PassResult], clamp: float = 1.0):
    """Blend the passes over `behind` (encoded RGB) in order; the target clamps at `clamp`."""
    dst = tuple(behind)
    for p in passes:
        src = tuple(min(max(x, 0.0), clamp) for x in p.rgb)
        a = min(max(p.alpha, 0.0), 1.0)
        fs, fd = _factor(p.blend[0], src, a, dst), _factor(p.blend[1], src, a, dst)
        dst = tuple(min(src[i] * fs[i] + dst[i] * fd[i], clamp) for i in range(3))
    return dst


def lin(x: float) -> float:
    return x * x


def enc(x: float) -> float:
    return math.sqrt(max(x, 0.0))


def _vec(s: str) -> list[float]:
    v = [float(x) for x in s.split(",")]
    return v + [0.0] * (4 - len(v))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--game", default="fh1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("technique")
    p.add_argument("--texel", default="255,255,255,255", help="0-255 RGBA")
    p.add_argument("--behind", default="0,0,0", help="0-255 encoded RGB behind the surface")
    p.add_argument("--lamp", type=float, default=0.0)
    p.add_argument("--param", action="append", default=[], help="Name=v[,v,v,v]")
    p.add_argument("--car", type=Path)
    sub.add_parser("extract")
    vf = sub.add_parser("verify")
    vf.add_argument("techniques", nargs="*")
    vf.add_argument("--trials", type=int, default=300)
    sv = sub.add_parser("survey")
    sv.add_argument("--out", type=Path, default=DATA / "survey")
    sv.add_argument("--cars", nargs="*")
    args = ap.parse_args(argv)
    if args.cmd == "extract":
        extract()
        return
    if args.cmd == "verify":
        import json

        fz = Forza.load(args.game)
        techs = args.techniques or [t for t in fz.techs if LENS_RE.match(t)]
        print(json.dumps(verify(fz, techs, args.trials), indent=1))
        return
    if args.cmd == "survey":
        survey(args.out, args.cars)
        return
    fz = Forza.load(args.game)
    car = None
    if getattr(args, "car", None):
        car = parse_settings((args.car / "ShaderSettings.xml").read_bytes().decode("latin-1"))
    if args.cmd == "probe":
        texel = tuple(x / 255 for x in _vec(args.texel))
        params = {k: _vec(v) for k, v in (s.split("=", 1) for s in args.param)}
        res = fz.run(args.technique, texel, params, lamp=args.lamp, car=car)
        print("params:", {k: v for k, v in fz.params(args.technique, car).items() if not k.startswith("D3DRS")})
        for r in res:
            print(f"{r.name}: blend {r.blend} rgb {tuple(round(x, 4) for x in r.rgb)} a {r.alpha:.4f}")
        behind = tuple(x / 255 for x in _vec(args.behind)[:3])
        print("over behind ->", tuple(round(255 * x) for x in composite(behind, res)))


LENS_RE = re.compile(
    r"^(detail_glass|lights_glass|lights_gls_|glass$|glass_2$|red_glass$|window|black_glass|lexan"
    r"|tail_light|taillight|reverse_light|indicator_|brin|brkreverse|lindtailside|rindtailside|hidhead|oldhead"
    r"|xenonhead|head_light|textured_reflector|reflector$|slorange|drlwhite|fogred)"
)
PARAMS = ("GlassColor", "GlassMaxOpacity", "PaintScale", "AmbientColor", "CubeScale", "DarkValues", "LightValues")


def _idle() -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x40)


def survey(out: Path, cars: list[str] | None = None, lod: int = 1) -> None:
    """Per car: the lamp, lens and glass techniques at `lod` (triangles, the mean Forza texel of
    each triangle) and the car's ShaderSettings for them; out/<car>.json, then summary.json."""
    import json

    from dsfforza.convert import forza2dsf, lights
    from dsfforza.convert import forza_materials as fm

    _idle()
    out.mkdir(parents=True, exist_ok=True)
    batch = [ln.strip() for ln in (REPO / "tools" / "batches" / "all.txt").read_text().splitlines()]
    names = cars or [ln for ln in batch if ln and not ln.startswith("#")]
    for media in names:
        dest = out / f"{media}.json"
        if dest.exists():
            continue
        folder = next(
            (
                REPO / "out" / "cache" / g / media
                for g in ("fh1", "fm4")
                if (REPO / "out" / "cache" / g / media).is_dir()
            ),
            None,
        )
        if folder is None:
            continue
        try:
            car = forza2dsf.load_car(folder, media)
        except Exception as e:
            dest.write_text(json.dumps({"error": str(e)}))
            continue
        sampler = lights.texel_sampler(fm.AtlasCache(folder))
        settings = {}
        if (folder / "ShaderSettings.xml").exists():
            settings = parse_settings((folder / "ShaderSettings.xml").read_bytes().decode("latin-1"))
        subs = []
        for section in car.body:
            for sub in section.subsections:
                if sub.lod != lod or not LENS_RE.match(sub.material.lower()):
                    continue
                tris = list(sub.triangles())
                texels = []
                for k in range(len(tris)):
                    try:
                        t = sampler(section, sub, tris[k])
                    except Exception:
                        t = None
                    texels.append(None if t is None else [round(float(x), 1) for x in t])
                subs.append({"section": section.name, "technique": sub.material, "tris": len(tris), "texels": texels})
        own = {t: {k: v for k, v in p.items() if k in PARAMS} for t, p in settings.items() if LENS_RE.match(t)}
        dest.write_text(json.dumps({"game": folder.parent.name, "subs": subs, "settings": own}))
        print(media, len(subs), flush=True)


def extract(root: Path = DATA) -> None:
    """Copy shaders_v16.fxobj and the shared ShaderSettings from the FH1 disc and FM4 disc 1."""
    from dsfforza.build import forza_roster
    from dsfforza.formats.forzazip import ForzaZip
    from dsfforza.formats.xiso import XIso

    discs = forza_roster.game_discs(sorted((REPO / "assets").glob("*.iso")))
    for game, iso in (("fh1", discs.get("FH1")), ("fm4", discs.get("FM4"))):
        if iso is None:
            print(f"no {game} disc")
            continue
        x = XIso(iso)
        for e in x.files():
            pl = e.path.lower().replace("\\", "/")
            if pl.endswith(("media/shaders/cars/shaders_v16.fxobj", "media/cars/shared.zip")):
                dest = root / game / pl
                dest.parent.mkdir(parents=True, exist_ok=True)
                x.extract(e, dest)
        z = ForzaZip((root / game / "media" / "cars" / "shared.zip").open("rb"))
        for e in z.entries():
            if e.filename.lower() == "shadersettings/normal.xml":
                (root / f"{game}_Normal.xml").write_bytes(z.read(e))
        print(game, "->", root)


if __name__ == "__main__":
    main()
