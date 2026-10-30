"""Factory paint palettes for converted Forza cars (the SPSR shader param sets).

A DSF vehicle's full entry holds NONR/SPSR (research/lights.md, "Paint"):

    SPID  u32 vehicle (spool) ID
    SPSS  paint palette: 1 to 14 SPSP, one per paint choice; the game picks
          one per spawned car (garageSpawnVehicle passes the index as `shader`)
          SPSP  9 MARP render parameters (7 on two vehicles):
                g_vec4TintColour1_Alpha  (x, 1, z, 1) metallic, (0, 0, 0, 1) solid;
                                         read by no shader
                g_vec3TintColour2        the albedo, linear RGB (shown where the
                                         fresnel term is 0)
                g_vec3TintColour3        mixed in by the fresnel term
                                         bias + (1 - bias) (1 - N.E)^power
                g_fresnelpower, g_fresnelbias, g_hCustomFresnelPower,
                g_hCustomReflectionAmount, g_hSecondSpecularIntensity,
                g_hSecondSpecularPower
    SPSS  1 SPSP: g_shadowDepthOffset
    SPSS  1 SPSP: g_vec4WheelBlurColour

These override the shared paint material GLOBAL_Chassis. A ported car keeps
its host's palette (the Desanne's ten colours). This module builds one from the
car's factory colours in the Forza game database (gamedb.slt Combo_Colors:
Ordinal = Data_Car.Id, Sequence, Metallic, RGB), by the mapping of
research/paint-re.md ("From a Forza colour to the palette"):

- colours 2 and 3: the albedo whose median sunlit panel shows Forza's swatch on screen
  (albedo_for, through the game's exposure, Reinhard curve and colour grade), one colour at
  every angle as Forza's factory paint is;
- the clear coat: the reflection from Forza's FresnelScalar, the second specular from its
  metallic lobes (Shared.zip's Normal.xml and Metallic.xml layers).

    uv run dsff paint colours out/cache/fh1/gamedb.slt NIS_370Z_10
    uv run dsff paint palette <archive> --id 304
    uv run dsff paint survey [--list tools/batches/all.txt] [--build out/build]
"""

from __future__ import annotations

import argparse
import copy
import functools
import json
import os
import sqlite3
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dsfforza import REPO
from dsfforza.formats.chnk import Entry, entry_size
from dsfforza.formats.vehicle_mesh import child, payload

MAX_COLOURS = 14  # the largest vanilla palette


@dataclass
class ForzaColour:
    sequence: int
    rgb: tuple[int, int, int]  # sRGB bytes: the swatch
    metallic: bool
    colour_class: str


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def forza_colours(gamedb: Path, media: str) -> list[ForzaColour]:
    """The car's factory colours, in the game's order (Combo_Colors.Sequence).

    Repeats are dropped: some cars list each colour once per livery texture
    (BaseTexture: stripes), e.g. the FH1 Challenger SRT8 392's 26 rows are 7
    colours, the 500 Abarth's 13 are 5."""
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
    out, seen = [], set()
    for seq, rgb, metallic, cls in rows:
        key = (rgb & 0xFFFFFF, bool(metallic))
        if key in seen:
            continue
        seen.add(key)
        out.append(ForzaColour(seq, ((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255), bool(metallic), cls))
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


def is_metallic(params: dict) -> bool:
    return params["g_vec4TintColour1_Alpha"][1] > 0.5


# --- what an entry shows ----------------------------------------------------------------


def _fresnel(params: dict, ndv):
    power, bias = params["g_fresnelpower"], params["g_fresnelbias"]
    return bias + (1 - bias) * (1 - ndv) ** power


_AREA_STEPS = 32
_AREA_NDV = tuple((k + 0.5) / _AREA_STEPS for k in range(_AREA_STEPS))


def apparent(params: dict) -> tuple[float, float, float]:
    """The albedo an entry shows (linear RGB): colour 2 mixed toward colour 3 by the fresnel
    term taken literally (research/paint-re.md, "Fresnel"), averaged over the projected area
    (N.E weighted). A low-power entry shows mostly colour 3, a flat or high-power one colour 2."""
    w = sum(x * _fresnel(params, x) for x in _AREA_NDV) / sum(_AREA_NDV)
    c2, c3 = params["g_vec3TintColour2"][:3], params["g_vec3TintColour3"][:3]
    return tuple(a + w * (b - a) for a, b in zip(c2, c3))


# --- the screen by day (research/paint-re.md, "Colour space and the screen") -------------

# ClearDay's post chain (research/lights-re.md): exposure from the adapted luminance with
# g_vLuminanceControl (HDRTonemapOffset 2, .w 0.3) and g_fMiddleGrey, Reinhard on Rec. 709
# luminance with white W, the colour grade, sRGB.
REC709 = np.array([0.2125, 0.7154, 0.0721])
TONEMAP_OFFSET, LUMINANCE_W, MIDDLE_GREY, WHITE = 2.0, 0.3, 1.5, 5.0
# The light on sunlit paint over its albedo in the frames dumped in game (33 sunlit dumps):
# median 2.13, facing the sun 3.0, turned from it 0.8; their adapted luminance about 0.2.
# A palette colour "shows" screen(LIGHT_REF x A), the median sunlit panel.
LIGHT_REF, LIGHT_SUN, LIGHT_AWAY = 2.13, 3.0, 0.8
LA_REF = 0.2

# The colour grade: ClearDay's 16^3 A8R8G8B8 volume in the game's media/SfxMisc.fchunk, the
# DDS after the chunk with GRADE_UID; without the game, the fit across channels from dumped
# frame pairs (tools/re/paint_grade.json, within 10 / 255 of it on the grey axis).
GRADE_UID = bytes.fromhex("0012e109a4d88c2d")
GRADE_FIT = REPO / "tools" / "re" / "paint_grade.json"


@functools.cache
def grade_lut() -> np.ndarray:
    """The grade as [r, g, b, 3] display-linear floats: the game's, else the fit."""
    root = os.environ.get("DSF_GAME_DIR")
    path = Path(root, "media", "SfxMisc.fchunk") if root else None
    if path is not None and path.exists():
        data = path.read_bytes()
        at = data.find(GRADE_UID)
        dds = data.find(b"DDS ", at) if at >= 0 else -1
        if 0 <= dds - at <= 64:
            vol = np.frombuffer(data, np.uint8, 16 * 16 * 16 * 4, dds + 128).reshape(16, 16, 16, 4)  # [b, g, r, BGRA]
            return np.transpose(vol[..., [2, 1, 0]], (2, 1, 0, 3)).astype(np.float64) / 255
    return np.array(json.loads(GRADE_FIT.read_text())["lut"], np.float64)


def grade3d(c, lut: np.ndarray | None = None) -> np.ndarray:
    """Trilinear lookup of colours (..., 3) in 0..1 in a 16^3 grade (default grade_lut())."""
    lut = grade_lut() if lut is None else lut
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


def exposure(la: float) -> float:
    return TONEMAP_OFFSET + (MIDDLE_GREY - TONEMAP_OFFSET) * min(1.0, max(0.0, 1.5 - LUMINANCE_W / (2 * la)))


def tonemap(hdr, e: float) -> np.ndarray:
    hdr = np.asarray(hdr, np.float64)
    lum = hdr @ REC709
    return hdr * (e * (1 + e * lum / WHITE**2) / (1 + e * lum))[..., None]


def encode_srgb(x) -> np.ndarray:
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def screen(hdr, la: float = LA_REF) -> np.ndarray:
    """sRGB (0..1) on screen of HDR colours (..., 3) at adapted luminance la."""
    return encode_srgb(grade3d(tonemap(hdr, exposure(la))))


def knee(a) -> float:
    """The largest albedo channel worth having: past it the median sunlit panel gets no
    brighter on screen and the shading across the body flattens (a panel facing the sun shows
    what the median one does). A neutral grey stops gaining near 0.5, a saturated colour (one
    strong channel) near 0.3 (paint-re.md, "Why bright reds look flat")."""
    a = np.asarray(a, np.float64)
    return 0.5 - 0.2 * (1 - a.min() / max(a.max(), 1e-6))


def _solve(t_lin: np.ndarray, light: float, la: float, iters: int) -> np.ndarray:
    a = np.clip(t_lin / max(light, 1e-3), 1e-4, 1)
    for _ in range(iters):
        s = screen(light * a, la)
        s_lin = np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)
        a = np.clip(a * ((t_lin + 1e-4) / (s_lin + 1e-4)) ** 0.7, 1e-4, 1)
    return a


def albedo_for(srgb, light: float = LIGHT_REF, la: float = LA_REF, iters: int = 60) -> np.ndarray:
    """The linear albedo A (0..1 per channel) whose screen colour at `light` x A is nearest
    the sRGB colour `srgb` (0..1): a fixed-point search through the exposure, Reinhard, the
    grade and sRGB. A colour the screen can't show (the solution passes the knee) is dimmed
    in linear light, its hue kept, until the solution fits under the knee."""
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


@functools.cache
def swatch_albedo(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """albedo_for of an sRGB byte swatch, rounded to the palette's precision."""
    return tuple(round(float(x), 5) for x in albedo_for(np.array(rgb) / 255))


# --- Forza's clear coat ----------------------------------------------------------------

# The `body` technique's parameters the mapping reads, from media/cars/Shared.zip
# ShaderSettings (FH1 and FM4 alike; paint-re.md, "What a Forza colour draws with"):
# Normal.xml for every colour, Metallic.xml over it when Combo_Colors.Metallic is 1.
FORZA_BODY = {"PaintScale": 1.0, "SpecularAmount2": 0.0, "SpecularPower2": 4.0}
FORZA_BODY |= {"SpecularAmount3": 0.0, "SpecularPower3": 2.0, "FresnelScalar": 0.9}
FORZA_METALLIC_BODY = {"PaintScale": 0.5, "SpecularAmount2": 7.0, "SpecularPower2": 6.0}
FORZA_METALLIC_BODY |= {"SpecularAmount3": 6.0, "SpecularPower3": 4.0}

# g_hCustomReflectionAmount for Forza's FresnelScalar 0.9: DSF's reflection is 0.042 x the
# amount facing the camera and 0.85 x at grazing, Forza's clear coat (Schlick, FresnelIndex
# 1.55, times FresnelScalar and reflectionScaler, 1.0 in both games: RE round 3b, #379) 0.042
# and 0.9: 1.0 matches both ends. Vanilla has 0.5 on 917 of 946 entries.
REFLECTION = 1.0
# The second specular (the sheen, tinted by the paint): vanilla's own scale, relative to
# Forza's lobes. A colour without lobes takes the common vanilla 0.1 at power 5; the
# Metallic.xml lobes (7 x lobe^6 + 6 x lobe^4 over a diffuse halved by PaintScale 0.5) take
# vanilla's p75, 0.3, scaled by the lobes' amount and PaintScale, at their powers weighted
# by amount (5.1).
SOLID_SHEEN = (0.1, 5.0)
LOBE_SHEEN = 0.3
LOBE_AMOUNT = FORZA_METALLIC_BODY["SpecularAmount2"] + FORZA_METALLIC_BODY["SpecularAmount3"]


def body_settings(colour: ForzaColour) -> dict[str, float]:
    """The colour's `body` parameters, layered as Forza layers them."""
    return FORZA_BODY | (FORZA_METALLIC_BODY if colour.metallic else {})


def clear_coat(body: dict[str, float]) -> dict[str, float]:
    """The palette's reflection and second specular for Forza `body` parameters."""
    amount = body["SpecularAmount2"] + body["SpecularAmount3"]
    if amount > 0:
        sheen = LOBE_SHEEN * amount / LOBE_AMOUNT * FORZA_METALLIC_BODY["PaintScale"] / max(body["PaintScale"], 0.1)
        power = body["SpecularAmount2"] * body["SpecularPower2"] + body["SpecularAmount3"] * body["SpecularPower3"]
        sheen_power = power / amount
    else:
        sheen, sheen_power = SOLID_SHEEN
    return {
        "g_hCustomFresnelPower": 1.0,
        "g_hCustomReflectionAmount": REFLECTION * body["FresnelScalar"] / FORZA_BODY["FresnelScalar"],
        "g_hSecondSpecularIntensity": round(sheen, 4),
        "g_hSecondSpecularPower": round(sheen_power, 3),
    }


def shade(colour: ForzaColour) -> dict:
    """Palette parameters for one Forza colour: one albedo at every angle (colour 2 = colour 3 = albedo_for(swatch); the fresnel power and
    bias then change nothing and are the vanilla flat entries' 10 / 0.01) under Forza's
    clear coat (clear_coat)."""
    a = swatch_albedo(tuple(colour.rgb)) + (1.0,)
    return {
        "g_vec4TintColour1_Alpha": (0.0, 1.0 if colour.metallic else 0.0, 0.0, 1.0),
        "g_vec3TintColour2": a,
        "g_vec3TintColour3": a,
        "g_fresnelpower": 10.0,
        "g_fresnelbias": 0.01,
        **clear_coat(body_settings(colour)),
    }


def shows(params: dict, light: float = LIGHT_REF) -> tuple[int, int, int]:
    """sRGB bytes an entry shows on a panel lit `light` x its albedo (apparent)."""
    return tuple(int(round(255 * v)) for v in screen(light * np.array(apparent(params))))


# --- installing -----------------------------------------------------------------------


def install(stream, full: Entry, colours: list[ForzaColour], nonr_limit: int | None = None) -> dict:
    """Replace the full entry's palette with the Forza colours (up to
    MAX_COLOURS, fewer if NONR would outgrow nonr_limit). The host's first
    SPSP is the template for the chunk layout; every MARP gets an explicit
    payload. Returns a report."""
    spss = palette_set(stream, full)
    if spss is None or not colours:
        return {"installed": 0, "reason": "no palette set" if spss is None else "no Forza colours"}
    template = spss.children[0]
    host_count = len(spss.children)
    built = []
    for colour in pick_colours(colours):
        params = shade(colour)
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
                "colour2": [round(x, 3) for x in p["g_vec3TintColour2"][:3]],
                "colour3": [round(x, 3) for x in p["g_vec3TintColour3"][:3]],
                "reflection": p["g_hCustomReflectionAmount"],
                "sheen": [p["g_hSecondSpecularIntensity"], p["g_hSecondSpecularPower"]],
                "shows": "#{:02x}{:02x}{:02x}".format(*shows(p)),
            }
            for c, p, _ in built
        ],
    }


def find_gamedb(car_folder: Path) -> Path | None:
    """out/cache/<game>/gamedb.slt next to an extracted car folder."""
    candidate = Path(car_folder).parent / "gamedb.slt"
    return candidate if candidate.exists() else None


# --- survey -----------------------------------------------------------------------------


def _lab(srgb8) -> np.ndarray:
    """CIELAB (D65) of sRGB bytes (..., 3)."""
    c = np.asarray(srgb8, np.float64) / 255
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ np.array([[0.4124, 0.2126, 0.0193], [0.3576, 0.7152, 0.1192], [0.1805, 0.0722, 0.9505]])
    t = xyz / np.array([0.95047, 1.0, 1.08883])
    f = np.where(t > (6 / 29) ** 3, np.cbrt(t), t / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def colour_group(rgb) -> str:
    """black, grey, white or the hue of a swatch (sRGB bytes)."""
    lab = _lab(rgb)
    chroma = float(np.hypot(lab[1], lab[2]))
    if chroma < 12:
        return "black" if lab[0] < 25 else "white" if lab[0] > 85 else "grey"
    hue = float(np.degrees(np.arctan2(lab[2], lab[1]))) % 360
    for limit, name in ((50, "red"), (80, "orange"), (110, "yellow"), (200, "green"), (315, "blue"), (350, "purple")):
        if hue < limit:
            return name
    return "red"


def survey_entry(colour: ForzaColour, params: dict) -> dict:
    """What an entry shows against its swatch: the median sunlit panel (dE76), and how
    much a panel facing the sun shows over it (L*; 0 = the body's shading flattened)."""
    ref, sun = shows(params), shows(params, LIGHT_SUN)
    lab = _lab([colour.rgb, ref, sun])
    return {
        "rgb": "#{:02x}{:02x}{:02x}".format(*colour.rgb),
        "group": colour_group(colour.rgb),
        "shows": "#{:02x}{:02x}{:02x}".format(*ref),
        "dE": round(float(np.linalg.norm(lab[1] - lab[0])), 2),
        "dL": round(float(lab[1][0] - lab[0][0]), 2),
        "sun_gain": round(float(lab[2][0] - lab[1][0]), 2),
        "reflection": params.get("g_hCustomReflectionAmount"),
    }


def build_palettes(build: Path) -> dict[str, list[dict]]:
    """{media: palette entries} of a build's cars (work/<media>.json spool IDs)."""
    from dsfforza.formats import upvh as uv
    from dsfforza.formats.chnk import read_chunk

    ids = {}
    for p in (Path(build) / "work").glob("*.json"):
        if p.name.endswith((".job.json", ".fp.json")):
            continue
        rep = json.loads(p.read_text(encoding="utf-8"))
        if rep.get("ok") and rep.get("spool_id") is not None:
            ids[rep["media"]] = rep["spool_id"]
    out = {}
    with (Path(build) / "dngvehicles.sp").open("rb") as stream:
        root = read_chunk(stream, 0)
        for media, sid in ids.items():
            found = uv.entries_for(stream, root.entries, sid, None)
            spss = palette_set(stream, found[0]) if found else None
            out[media] = [spsp_params(stream, e) for e in spss.children] if spss else []
    return out


def survey(cars: list[str], cache: Path, build: Path | None = None) -> dict:
    """Per roster colour, what its entry shows against Forza's swatch: from this code
    (shade) or from a build's palettes (--build: the entries it holds, in pick_colours
    order)."""
    gamedbs = [cache / g / "gamedb.slt" for g in ("fh1", "fm4")]
    palettes = build_palettes(build) if build else None
    rows = []
    for media in cars:
        name, _, game = media.partition("@")
        found = [db for db in gamedbs if (db.parent / name).is_dir()]
        dbs = [cache / game / "gamedb.slt"] if game else found[:1] or gamedbs
        colours = pick_colours(next((c for db in dbs if (c := forza_colours(db, name))), []))
        entries = [shade(c) for c in colours] if palettes is None else palettes.get(name, [])
        for colour, params in zip(colours, entries):
            rows.append({"car": name, "metallic": colour.metallic, **survey_entry(colour, params)})

    def stats(sel):
        de = np.array([r["dE"] for r in sel]) if sel else np.zeros(1)
        gain = np.array([r["sun_gain"] for r in sel]) if sel else np.zeros(1)
        return {
            "entries": len(sel),
            "dE_median": round(float(np.median(de)), 1),
            "dE_p90": round(float(np.percentile(de, 90)), 1),
            "dE_over_10": int((de > 10).sum()),
            "dL_median": round(float(np.median([r["dL"] for r in sel])) if sel else 0.0, 1),
            "flat": int((gain < 1).sum()),
        }

    groups = sorted({r["group"] for r in rows})
    return {
        "cars": len({r["car"] for r in rows}),
        "all": stats(rows),
        "groups": {g: stats([r for r in rows if r["group"] == g]) for g in groups},
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("colours", help="a car's factory colours and the palette entry each gets")
    c.add_argument("gamedb", type=Path)
    c.add_argument("media")
    v = sub.add_parser("survey", help="what each roster colour's entry shows against Forza's swatch")
    v.add_argument("--list", type=Path, default=REPO / "tools" / "batches" / "all.txt")
    v.add_argument("--build", type=Path, default=None, help="a build's palettes instead of this code's")
    v.add_argument("--out", type=Path, default=None, help="the per-entry rows as JSON")
    p = sub.add_parser("palette", help="print a vehicle's palette")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)

    if args.cmd == "palette":
        from dsfforza.formats import upvh as uv
        from dsfforza.formats.chnk import read_chunk

        with args.archive.open("rb") as stream:
            root = read_chunk(stream, 0)
            full = uv.entries_for(stream, root.entries, args.id, None)[0]
            spss = palette_set(stream, full)
            for n, spsp in enumerate(spss.children if spss else []):
                q = spsp_params(stream, spsp)
                print(
                    f"{n:2} shows #{'{:02x}{:02x}{:02x}'.format(*shows(q))} metallic {is_metallic(q)!s:5} "
                    f"c2 {tuple(round(x, 3) for x in q['g_vec3TintColour2'][:3])} "
                    f"c3 {tuple(round(x, 3) for x in q['g_vec3TintColour3'][:3])} power {q['g_fresnelpower']:.2f} "
                    f"refl {q.get('g_hCustomReflectionAmount', 0):.2f} sheen {q.get('g_hSecondSpecularIntensity', 0):.2f}"
                )
        return
    if args.cmd == "survey":
        cars = [x.split("#")[0].strip() for x in args.list.read_text(encoding="utf-8").splitlines()]
        result = survey([c for c in cars if c], REPO / "out" / "cache", args.build)
        if args.out:
            args.out.write_text(json.dumps(result, indent=1))
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=1))
        return
    for colour in pick_colours(forza_colours(args.gamedb, args.media)):
        q = shade(colour)
        print(
            f"{colour.sequence:2} #{'{:02x}{:02x}{:02x}'.format(*colour.rgb)} {'metallic' if colour.metallic else 'solid   '} "
            f"{colour.colour_class:8} shows #{'{:02x}{:02x}{:02x}'.format(*shows(q))} "
            f"(sun #{'{:02x}{:02x}{:02x}'.format(*shows(q, LIGHT_SUN))}) "
            f"c2 {tuple(round(x, 3) for x in q['g_vec3TintColour2'][:3])} "
            f"refl {q['g_hCustomReflectionAmount']:.2f} sheen {q['g_hSecondSpecularIntensity']:.2f} "
            f"power {q['g_hSecondSpecularPower']:.1f}"
        )


if __name__ == "__main__":
    sys.exit(main())
