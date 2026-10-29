"""Factory paint palettes for converted Forza cars (the SPSR shader param sets).

A DSF vehicle's full entry holds NONR/SPSR (research/lights.md, "Paint"):

    SPID  u32 vehicle (spool) ID
    SPSS  paint palette: 1 to 14 SPSP, one per paint choice; the game picks
          one per spawned car (garageSpawnVehicle passes the index as `shader`)
          SPSP  9 MARP render parameters (7 on two vehicles):
                g_vec4TintColour1_Alpha  (x, 1, z, 1) metallic, (0, 0, 0, 1) solid
                g_vec3TintColour2        base colour, linear RGB (white ~0.9-1.0)
                g_vec3TintColour3        second colour; with a low g_fresnelpower
                                         (< 1) a dark edge tint, with a high one
                                         (5 to 15) the main colour, colour 2 the
                                         facing tint
                g_fresnelpower, g_fresnelbias, g_hCustomFresnelPower,
                g_hCustomReflectionAmount, g_hSecondSpecularIntensity,
                g_hSecondSpecularPower
    SPSS  1 SPSP: g_shadowDepthOffset
    SPSS  1 SPSP: g_vec4WheelBlurColour

These override the shared paint material GLOBAL_Chassis. A ported car keeps
its host's palette (the Desanne's ten colours). This module builds one from the
car's factory colours in the Forza game database (gamedb.slt Combo_Colors:
Ordinal = Data_Car.Id, Sequence, Metallic, RGB): each colour takes the shading
parameters of the most similar vanilla paint of the same finish (all 150 vanilla
palettes, 948 entries), and its base colour; the second colour keeps the vanilla
entry's brightness ratio in the Forza colour's hue.

    uv run dsff paint colours out/cache/fh1/gamedb.slt NIS_370Z_10
    uv run dsff paint palette <archive> --id 304 [--gamedb ... --media ...]
"""

from __future__ import annotations

import argparse
import copy
import sqlite3
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from dsfforza.formats.chnk import Entry, entry_size
from dsfforza.formats.vehicle_mesh import child, payload
from dsfforza.game import game_paths

COLOUR_PARAMS = ("g_vec4TintColour1_Alpha", "g_vec3TintColour2", "g_vec3TintColour3")
MAX_COLOURS = 14  # the largest vanilla palette

# Finishes Combo_Colors can't express (it only has RGB and a metallic flag):
# media name -> finish. Each finish is a preset built from a vanilla DSF palette entry.
SPECIAL_FINISHES = {
    # Brushed stainless body panels. Forza paints it solid #999999, which the
    # flat vanilla greys (colour 2 = colour 3, fresnel power 5 to 15) turn into a
    # light silver that reads white in game; DSF's own DeLorean (spool 171)
    # has one steel entry.
    "DEL_DMC12_82": "stainless",
}
FINISH_PRESETS = {
    # From vanilla 171 (DMC_DeLorean_DMC_12_1981) palette entry 0: mid grey facing
    # the camera (colour 2 0.267), fresnel power 0.75, little reflection (0.1), a
    # broad second specular (0.3, power 5). Its edge tint (colour 3 0.004) is not
    # kept: the fresnel term is applied literally (power 0.75 mixes far toward
    # colour 3; research/lights.md, "The DMC-12 came out black"), and the Forza
    # body shows the chase camera its stainless mostly at grazing angles (paint
    # n.v 0.25 against the vanilla model's 0.54, whose rear panel and bumper are
    # paint where Forza's are black trim and grey plastic), so 0.004 drew the port
    # near black. Brushed steel stays light at grazing angles: colour 3 is 0.2
    # (run host-20261023-094153 against -094028, 0.13, and the vanilla edge tint).
    "stainless": {
        "g_vec4TintColour1_Alpha": (2.3328e-05, 1.0, 0.0738278, 1.0),
        "g_vec3TintColour2": (0.267358, 0.267358, 0.267358, 1.0),
        "g_vec3TintColour3": (0.2, 0.2, 0.2, 1.0),
        "g_fresnelpower": 0.75,
        "g_fresnelbias": 0.01,
        "g_hCustomFresnelPower": 1.0,
        "g_hCustomReflectionAmount": 0.1,
        "g_hSecondSpecularIntensity": 0.3,
        "g_hSecondSpecularPower": 5.0,
        "_template": 171,
    },
}


@dataclass
class ForzaColour:
    sequence: int
    rgb: tuple[int, int, int]  # sRGB bytes
    metallic: bool
    colour_class: str
    finish: str = ""  # a FINISH_PRESETS key, or "" for paint

    @property
    def linear(self) -> tuple[float, float, float]:
        return tuple(srgb_to_linear(c / 255) for c in self.rgb)


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def forza_colours(gamedb: Path, media: str) -> list[ForzaColour]:
    """The car's factory colours, in the game's order (Combo_Colors.Sequence).

    Repeats are dropped: some cars list each colour once per livery texture
    (BaseTexture: stripes), e.g. the FH1 Challenger SRT8 392's 26 rows are 7
    colours, the 500 Abarth's 13 are 5. Cars in SPECIAL_FINISHES get their
    finish on every colour."""
    db = sqlite3.connect(f"file:{Path(gamedb).as_posix()}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT Id FROM Data_Car WHERE lower(MediaName) = lower(?)", (media,)).fetchone()
        if row is None:
            return []
        rows = db.execute(
            "SELECT c.Sequence, c.RGB, c.Metallic, coalesce(k.ColorClass, '') FROM Combo_Colors c "
            "LEFT JOIN List_ColorClass k ON k.ColorClassId = c.ColorClassId "
            "WHERE c.Ordinal = ? ORDER BY c.Sequence",
            (row[0],),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    finish = SPECIAL_FINISHES.get(next((k for k in SPECIAL_FINISHES if k.lower() == media.lower()), ""), "")
    out, seen = [], set()
    for seq, rgb, metallic, cls in rows:
        key = (rgb & 0xFFFFFF, bool(metallic))
        if key in seen:
            continue
        seen.add(key)
        out.append(ForzaColour(seq, ((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255), bool(metallic), cls, finish))
    return out


def pick_colours(colours: list[ForzaColour], limit: int = MAX_COLOURS) -> list[ForzaColour]:
    """At most `limit` colours, in Forza's order. Beyond the limit keep the
    most distinct ones (farthest-point in sRGB from the first colour on), so a
    long list like the Charger Daytona's 20 keeps its white, grey and black
    rather than its first 14."""
    if len(colours) <= limit:
        return list(colours)
    chosen = [0]

    def d(i, j):
        a, b = colours[i], colours[j]
        return sum((x - y) ** 2 for x, y in zip(a.rgb, b.rgb)) + (0 if a.metallic == b.metallic else 400)

    while len(chosen) < limit:
        best = max((i for i in range(len(colours)) if i not in chosen), key=lambda i: min(d(i, j) for j in chosen))
        chosen.append(best)
    return [colours[i] for i in sorted(chosen)]


# --- MARP / SPSP ------------------------------------------------------------------


def parse_marp(data: bytes) -> tuple[str, bytes, int]:
    return data[:32].split(b"\0", 1)[0].decode("latin-1"), data[32:48], struct.unpack_from("<I", data, 48)[0]


def marp_value(data: bytes):
    _name, value, kind = parse_marp(data)
    if kind == 1:
        return struct.unpack_from("<f", value)[0]
    if kind == 3:
        return struct.unpack_from("<4f", value)
    return value


def set_marp(data: bytes, value) -> bytes:
    _name, raw, kind = parse_marp(data)
    if kind == 1:
        raw = struct.pack("<f", value) + raw[4:]
    elif kind == 3:
        raw = struct.pack("<4f", *value)
    return data[:32] + raw + data[48:]


def _current(stream, e: Entry) -> bytes:
    return e.payload if e.payload is not None else payload(stream, e)


def spsp_params(stream, spsp: Entry) -> dict:
    out = {}
    for p in spsp.children or []:
        if p.tag == "MARP":
            data = _current(stream, p)
            out[parse_marp(data)[0]] = marp_value(data)
    return out


def palette_set(stream, full: Entry) -> Entry | None:
    """The SPSS whose SPSPs carry the paint colours (the first in vanilla)."""
    spsr = next((c for c in child(full, "NONR").children if c.tag == "SPSR"), None)
    if spsr is None:
        return None
    for spss in spsr.children:
        if spss.tag == "SPSS" and spss.children and "g_vec3TintColour2" in spsp_params(stream, spss.children[0]):
            return spss
    return None


def set_wheel_blur_colour(stream, full: Entry, rgb) -> bool:
    """Set g_vec4WheelBlurColour (SPSR's third set) to (r, g, b, 1): what the wheel's blur
    disc multiplies its grey by (forza2dsf.wheel_blur_section; vanilla: a grey per vehicle,
    1.0 on 37 of 150). False if the entry has no such parameter."""
    spsr = next((c for c in child(full, "NONR").children if c.tag == "SPSR"), None)
    done = False
    for spss in spsr.children if spsr is not None else []:
        for spsp in spss.children or []:
            for p in spsp.children or []:
                if p.tag == "MARP":
                    data = _current(stream, p)
                    if parse_marp(data)[0] == "g_vec4WheelBlurColour":
                        p.payload = set_marp(data, (*(float(c) for c in rgb[:3]), 1.0))
                        done = True
    return done


_VANILLA_CACHE: dict = {}


def vanilla_paints(stream, entries=None) -> list[dict]:
    """Every palette entry with the full parameter set in an archive's
    vehicles below spool ID 304 (the vanilla ones): [{params, sid}]. Cached per
    stream."""
    key = getattr(stream, "name", id(stream))
    if key in _VANILLA_CACHE:
        return _VANILLA_CACHE[key]
    from dsfforza.formats import vehicles

    if entries is None:
        from dsfforza.formats.chnk import read_chunk

        position = stream.tell()
        entries = read_chunk(stream, 0).entries
        stream.seek(position)
    ssic = vehicles.find(entries, "SSIC")
    records = vehicles.parse_sslp(payload(stream, vehicles.find(vehicles.find(ssic.children, "NONR").children, "SSLP")))
    out = []
    for sid, index, kind in records:
        if kind != 0 or sid >= 304:
            continue
        spss = palette_set(stream, entries[index])
        for spsp in spss.children if spss else []:
            params = spsp_params(stream, spsp)
            if len(params) >= 9:
                out.append({"sid": sid, "params": params})
    _VANILLA_CACHE[key] = out
    return out


# --- choosing shading for a Forza colour -------------------------------------------


def _lum(c) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def is_metallic(params: dict) -> bool:
    return params["g_vec4TintColour1_Alpha"][1] > 0.5


def is_template(params: dict) -> bool:
    """Car paint entries: colour 1 is (0, 0, 0) (solid) or (x, 1, z)
    (metallic). The 40 vanilla entries with an RGB colour there belong to vans,
    trucks, the buggy and striped or two-tone cars (Courier, Vandura, rigs,
    Cobra, Shelby GT500, Eldorado, Monaco) and carry their own reflection and
    specular set-ups (the Courier's flat grey gave the DeLorean port its
    look); they are not used as templates."""
    t = params["g_vec4TintColour1_Alpha"]
    return max(t[:3]) == 0 or abs(t[1] - 1) < 1e-3


def _distance(a, b) -> float:
    """sRGB distance with luminance weighted twice."""
    sa = [linear_to_srgb(c) for c in a]
    sb = [linear_to_srgb(c) for c in b]
    d = sum((x - y) ** 2 for x, y in zip(sa, sb))
    return d + 2 * (_lum(sa) - _lum(sb)) ** 2


# What a palette entry shows: the paint shader mixes colour 2 toward colour 3 by
# f = bias + (1 - bias) x (1 - n.v)^power, taken literally (research/lights.md, "The DMC-12
# came out black"). Measured at n.v 0.9, 0.7 and 0.5 (panels facing the camera, at an angle,
# oblique) and averaged over the projected area (n.v weighted).
BRIGHTNESS_ANGLES = (0.9, 0.7, 0.5)
_AREA_STEPS = 32
_AREA_NDV = tuple((k + 0.5) / _AREA_STEPS for k in range(_AREA_STEPS))

# How bright DSF draws paint against Forza's swatch. On the 50 vanilla cars Forza also has
# (handling_dsf.PAIRS) the vanilla entries show (area-weighted) about 0.3 of the linear
# luminance of the nearest Forza colour of the same car: whites 0.25 to 0.33 against Forza's
# 0.86 to 0.99, the Ruf's yellow 0.33 against 0.58; a fit over all 338 colours is best at
# 0.3 (research/lights.md, "Paint shows darker than Forza"). The Forza swatch taken as is
# drew a mid grey (#999999 flat, 0.32) white and the S5S Raptor's yellow as a glow.
SHOWN_GAIN = 0.3
NEAR_BLACK = 0.01  # Forza colours darker than this (linear luminance) keep the vanilla black


def _fresnel(params: dict, ndv: float) -> float:
    power, bias = params["g_fresnelpower"], params["g_fresnelbias"]
    return bias + (1 - bias) * (1 - ndv) ** power


def _mix(params: dict, ndv: float) -> tuple[float, float, float]:
    c2, c3 = params["g_vec3TintColour2"][:3], params["g_vec3TintColour3"][:3]
    f = _fresnel(params, ndv)
    return tuple(a + f * (b - a) for a, b in zip(c2, c3))


def shown_weight(params: dict) -> float:
    """The share of colour 3 in what an entry shows, averaged over the projected area."""
    return sum(x * _fresnel(params, x) for x in _AREA_NDV) / sum(_AREA_NDV)


def apparent(params: dict) -> tuple[float, float, float]:
    """The colour an entry shows (linear RGB): the fresnel mix averaged over the projected
    area. A low-power entry shows mostly colour 3 (the vanilla whites with a dark colour 3
    and power 0.1 show 0.14 to 0.2), a flat or high-power one colour 2."""
    w = shown_weight(params)
    c2, c3 = params["g_vec3TintColour2"][:3], params["g_vec3TintColour3"][:3]
    return tuple(a + w * (b - a) for a, b in zip(c2, c3))


def brightness(params: dict) -> tuple[float, ...]:
    """Luminance of an entry at BRIGHTNESS_ANGLES, then its area-weighted mean."""
    return tuple(_lum(_mix(params, x)) for x in BRIGHTNESS_ANGLES) + (_lum(apparent(params)),)


def target_colour(colour: ForzaColour) -> tuple[float, float, float]:
    """The colour a Forza swatch should show in DSF (linear RGB)."""
    return tuple(SHOWN_GAIN * c for c in colour.linear)


def recolour(params: dict, target) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Colours 2 and 3 in the target's hue so that the entry shows the target (apparent),
    with colour 3 at the template's brightness ratio to colour 2. Where that ratio would
    need a colour above 1 (the MP4-12C's red: colour 3 at 0.03 of colour 2 and 98% of
    colour 3 showing), the brighter one is the target's hue at full scale and the other
    makes up the rest."""
    w = min(max(shown_weight(params), 1e-6), 1 - 1e-6)
    l2, l3 = _lum(params["g_vec3TintColour2"][:3]), _lum(params["g_vec3TintColour3"][:3])
    if l2 > 1e-4:
        k2 = 1 / ((1 - w) + w * l3 / l2)
        k3 = k2 * l3 / l2
    else:
        k2, k3 = 0.0, 1 / w
    top = 1 / max(max(target), 1e-6)
    if k2 > top and k2 >= k3:
        k2 = top
        k3 = (1 - (1 - w) * top) / w
    elif k3 > top:
        k3 = top
        k2 = (1 - w * top) / (1 - w)
    return tuple(min(1.0, k2 * c) for c in target), tuple(min(1.0, k3 * c) for c in target)


_CAP_CACHE: dict = {}


def brightness_cap(vanilla: list[dict]) -> tuple[float, ...]:
    """The brightest vanilla car paint, per brightness() measure."""
    key = (id(vanilla), len(vanilla))
    if key not in _CAP_CACHE:
        rows = [brightness(s["params"]) for s in vanilla if is_template(s["params"])] or [(1.0,) * 4]
        _CAP_CACHE[key] = tuple(max(r[k] for r in rows) for k in range(len(rows[0])))
    return _CAP_CACHE[key]


def shade(colour: ForzaColour, vanilla: list[dict]) -> dict:
    """Parameters for one Forza colour: a special finish's preset as is, else the vanilla
    car-paint entry of the same finish (any finish if none) whose shown colour (apparent)
    is nearest the colour's DSF target (target_colour), recoloured to show the target.
    Near-black colours keep the vanilla black as it is (its sheen and edge tint)."""
    if colour.finish in FINISH_PRESETS:
        return dict(FINISH_PRESETS[colour.finish])
    target = target_colour(colour)
    vanilla = [s for s in vanilla if is_template(s["params"])] or vanilla
    same = [s for s in vanilla if is_metallic(s["params"]) == colour.metallic] or vanilla
    best = min(same, key=lambda s: _distance(apparent(s["params"]), target))
    params = dict(best["params"])
    if _lum(colour.linear) >= NEAR_BLACK:
        c2, c3 = params["g_vec3TintColour2"], params["g_vec3TintColour3"]
        new2, new3 = recolour(params, target)
        params["g_vec3TintColour2"], params["g_vec3TintColour3"] = new2 + tuple(c2[3:]), new3 + tuple(c3[3:])
    # Never brighter than the brightest vanilla car paint. Forza's swatch RGB taken as linear
    # was far above anything vanilla DSF paints: the S5S Raptor's #ffbf19 on the security
    # van's yellow gave luminance 0.55 facing the camera where no vanilla paint passes 0.39,
    # and the body bloomed into an orange halo in daylight (user report on out/testdrive9).
    # SHOWN_GAIN keeps targets under that; the cap stays as a guard. Both colours are
    # scaled, so the hue and the template's shading stay.
    cap, now = brightness_cap(vanilla), brightness(params)
    k = min([1.0] + [c / v for c, v in zip(cap, now) if v > c])
    if k < 1.0:
        for name in ("g_vec3TintColour2", "g_vec3TintColour3"):
            c = params[name]
            params[name] = tuple(x * k for x in c[:3]) + tuple(c[3:])
    params["_template"] = best["sid"]
    params["_dimmed"] = round(k, 3)
    return params


# --- installing -----------------------------------------------------------------------


def install(
    stream, full: Entry, colours: list[ForzaColour], vanilla: list[dict], nonr_limit: int | None = None
) -> dict:
    """Replace the full entry's palette with the Forza colours (up to
    MAX_COLOURS, fewer if NONR would outgrow nonr_limit). The host's first
    SPSP is the template for the chunk layout; every MARP gets an explicit
    payload. Returns a report."""
    spss = palette_set(stream, full)
    if spss is None or not colours:
        return {"installed": 0, "reason": "no palette set" if spss is None else "no Forza colours"}
    template = spss.children[0]
    host_count = len(spss.children)
    chosen = pick_colours(colours)
    built = []
    for colour in chosen:
        params = shade(colour, vanilla)
        spsp = copy.deepcopy(template)
        for p in spsp.children:
            if p.tag != "MARP":
                continue
            data = _current(stream, p)
            name = parse_marp(data)[0]
            p.payload = set_marp(data, params[name]) if name in params else data
        built.append((colour, params, spsp))
    nonr = child(full, "NONR")
    while True:
        spss.children = [b[2] for b in built]
        if nonr_limit is None or entry_size(nonr) <= nonr_limit or len(built) <= 1:
            break
        built.pop()
    return {
        "installed": len(built),
        "host_count": host_count,
        "nonr": entry_size(nonr),
        "colours": [
            {
                "sequence": c.sequence,
                "rgb": "#{:02x}{:02x}{:02x}".format(*c.rgb),
                "metallic": c.metallic,
                "class": c.colour_class,
                "finish": c.finish,
                "template": p["_template"],
                "dimmed": p.get("_dimmed", 1.0),
                "colour2": [round(x, 3) for x in p["g_vec3TintColour2"][:3]],
                "colour3": [round(x, 3) for x in p["g_vec3TintColour3"][:3]],
                "fresnelpower": round(p["g_fresnelpower"], 3),
            }
            for c, p, _ in built
        ],
    }


def find_gamedb(car_folder: Path) -> Path | None:
    """out/cache/<game>/gamedb.slt next to an extracted car folder."""
    candidate = Path(car_folder).parent / "gamedb.slt"
    return candidate if candidate.exists() else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("colours", help="a car's factory colours and the vanilla shading each would get")
    c.add_argument("gamedb", type=Path)
    c.add_argument("media")
    c.add_argument("--dsf", type=Path, default=None, help="vanilla dngvehicles.sp (default: from $DSF_GAME_DIR)")
    p = sub.add_parser("palette", help="print a vehicle's palette")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "colours":
        args.dsf = game_paths.vanilla(args.dsf)  # the original when a dsfforza install replaced it

    from dsfforza.formats import upvh as uv
    from dsfforza.formats.chnk import read_chunk

    if args.cmd == "palette":
        with args.archive.open("rb") as stream:
            root = read_chunk(stream, 0)
            full = uv.entries_for(stream, root.entries, args.id, None)[0]
            spss = palette_set(stream, full)
            for n, spsp in enumerate(spss.children if spss else []):
                q = spsp_params(stream, spsp)
                c2 = q["g_vec3TintColour2"][:3]
                srgb = "#{:02x}{:02x}{:02x}".format(*(round(linear_to_srgb(x) * 255) for x in c2))
                print(
                    f"{n:2} {srgb} metallic {is_metallic(q)!s:5} c2 {tuple(round(x, 3) for x in c2)} "
                    f"c3 {tuple(round(x, 3) for x in q['g_vec3TintColour3'][:3])} power {q['g_fresnelpower']:.2f}"
                )
        return
    with args.dsf.open("rb") as stream:
        vanilla = vanilla_paints(stream, read_chunk(stream, 0).entries)
    for colour in pick_colours(forza_colours(args.gamedb, args.media)):
        q = shade(colour, vanilla)
        dimmed = q.get("_dimmed", 1.0)
        print(
            f"{colour.sequence:2} #{'{:02x}{:02x}{:02x}'.format(*colour.rgb)} {'metallic' if colour.metallic else 'solid   '} "
            f"{colour.colour_class:8} {colour.finish or ''}<- vanilla {q['_template']} c2 {tuple(round(x, 3) for x in q['g_vec3TintColour2'][:3])} "
            f"c3 {tuple(round(x, 3) for x in q['g_vec3TintColour3'][:3])} power {q['g_fresnelpower']:.2f}"
            + (f" dimmed x{dimmed}" if dimmed < 1 else "")
        )


if __name__ == "__main__":
    sys.exit(main())
