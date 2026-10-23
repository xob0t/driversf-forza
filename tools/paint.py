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
parameters of the most similar stock paint of the same finish (all 150 stock
palettes, 948 entries), and its base colour; the second colour keeps the stock
entry's brightness ratio in the Forza colour's hue.

    python tools/paint.py colours out/cache/fh1/gamedb.slt NIS_370Z_10
    python tools/paint.py palette <archive> --id 304 [--gamedb ... --media ...]
"""

from __future__ import annotations

import game_paths
import argparse
import copy
import sqlite3
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from chnk import Entry, entry_size
from vehicle_mesh import child, payload

COLOUR_PARAMS = ("g_vec4TintColour1_Alpha", "g_vec3TintColour2", "g_vec3TintColour3")
MAX_COLOURS = 14   # the largest stock palette

# Finishes Combo_Colors can't express (it only has RGB and a metallic flag):
# media name -> finish. Each finish is a preset built from a stock DSF palette entry.
SPECIAL_FINISHES = {
    # Brushed stainless body panels. Forza paints it solid #999999, which the
    # flat stock greys (colour 2 = colour 3, fresnel power 5 to 15) turn into a
    # light silver that reads white in game; DSF's own DeLorean (spool 171)
    # has one steel entry.
    "DEL_DMC12_82": "stainless",
}
FINISH_PRESETS = {
    # From stock 171 (DMC_DeLorean_DMC_12_1981) palette entry 0: mid grey facing
    # the camera (colour 2 0.267), fresnel power 0.75, little reflection (0.1), a
    # broad second specular (0.3, power 5). Its edge tint (colour 3 0.004) is not
    # kept: the fresnel term is applied literally (power 0.75 mixes far toward
    # colour 3; research/lights.md, "The DMC-12 came out black"), and the Forza
    # body shows the chase camera its stainless mostly at grazing angles (paint
    # n.v 0.25 against the stock model's 0.54, whose rear panel and bumper are
    # paint where Forza's are black trim and grey plastic), so 0.004 drew the port
    # near black. Brushed steel stays light at grazing angles: colour 3 is 0.2
    # (run host-20261023-094153 against -094028, 0.13, and the stock edge tint).
    "stainless": {
        "g_vec4TintColour1_Alpha": (2.3328e-05, 1.0, 0.0738278, 1.0),
        "g_vec3TintColour2": (0.267358, 0.267358, 0.267358, 1.0),
        "g_vec3TintColour3": (0.2, 0.2, 0.2, 1.0),
        "g_fresnelpower": 0.75, "g_fresnelbias": 0.01, "g_hCustomFresnelPower": 1.0,
        "g_hCustomReflectionAmount": 0.1, "g_hSecondSpecularIntensity": 0.3, "g_hSecondSpecularPower": 5.0,
        "_template": 171,
    },
}


@dataclass
class ForzaColour:
    sequence: int
    rgb: tuple[int, int, int]   # sRGB bytes
    metallic: bool
    colour_class: str
    finish: str = ""            # a FINISH_PRESETS key, or "" for paint

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
        rows = db.execute("SELECT c.Sequence, c.RGB, c.Metallic, coalesce(k.ColorClass, '') FROM Combo_Colors c "
                          "LEFT JOIN List_ColorClass k ON k.ColorClassId = c.ColorClassId "
                          "WHERE c.Ordinal = ? ORDER BY c.Sequence", (row[0],)).fetchall()
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
    name, value, kind = parse_marp(data)
    if kind == 1:
        return struct.unpack_from("<f", value)[0]
    if kind == 3:
        return struct.unpack_from("<4f", value)
    return value


def set_marp(data: bytes, value) -> bytes:
    name, raw, kind = parse_marp(data)
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
    """The SPSS whose SPSPs carry the paint colours (the first in stock)."""
    spsr = next((c for c in child(full, "NONR").children if c.tag == "SPSR"), None)
    if spsr is None:
        return None
    for spss in spsr.children:
        if spss.tag == "SPSS" and spss.children and "g_vec3TintColour2" in spsp_params(stream, spss.children[0]):
            return spss
    return None


_STOCK_CACHE: dict = {}


def stock_paints(stream, entries=None) -> list[dict]:
    """Every palette entry with the full parameter set in an archive's
    vehicles below spool ID 304 (the stock ones): [{params, sid}]. Cached per
    stream."""
    key = getattr(stream, "name", id(stream))
    if key in _STOCK_CACHE:
        return _STOCK_CACHE[key]
    import vehicles
    if entries is None:
        from chnk import read_chunk
        position = stream.tell()
        entries = read_chunk(stream, 0).entries
        stream.seek(position)
    ssic = vehicles.find(entries, "SSIC")
    records = vehicles.parse_sslp(payload(stream, vehicles.find(vehicles.find(ssic.children, "NONR").children,
                                                                "SSLP")))
    out = []
    for sid, index, kind in records:
        if kind != 0 or sid >= 304:
            continue
        spss = palette_set(stream, entries[index])
        for spsp in spss.children if spss else []:
            params = spsp_params(stream, spsp)
            if len(params) >= 9:
                out.append({"sid": sid, "params": params})
    _STOCK_CACHE[key] = out
    return out


# --- choosing shading for a Forza colour -------------------------------------------

def _lum(c) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def apparent(params: dict) -> tuple[float, float, float]:
    """The colour a stock entry mostly shows: colour 2 with a low fresnel
    power, colour 3 with a high one (research/lights.md)."""
    c2, c3 = params["g_vec3TintColour2"][:3], params["g_vec3TintColour3"][:3]
    return tuple(c3) if params.get("g_fresnelpower", 0) >= 1 and _lum(c3) > _lum(c2) else tuple(c2)


def is_metallic(params: dict) -> bool:
    return params["g_vec4TintColour1_Alpha"][1] > 0.5


def is_template(params: dict) -> bool:
    """Car paint entries: colour 1 is (0, 0, 0) (solid) or (x, 1, z)
    (metallic). The 40 stock entries with an RGB colour there belong to vans,
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


def shade(colour: ForzaColour, stock: list[dict]) -> dict:
    """Parameters for one Forza colour: a special finish's preset as is, else
    the most similar stock car-paint entry of the same finish (any finish if
    none), recoloured."""
    if colour.finish in FINISH_PRESETS:
        return dict(FINISH_PRESETS[colour.finish])
    target = colour.linear
    stock = [s for s in stock if is_template(s["params"])] or stock
    same = [s for s in stock if is_metallic(s["params"]) == colour.metallic] or stock
    best = min(same, key=lambda s: _distance(apparent(s["params"]), target))
    params = dict(best["params"])
    c2, c3 = params["g_vec3TintColour2"], params["g_vec3TintColour3"]
    main_is_3 = tuple(apparent(params)) == tuple(c3[:3]) and tuple(c3[:3]) != tuple(c2[:3])
    main, other = (c3, c2) if main_is_3 else (c2, c3)
    ratio = _lum(other) / max(_lum(main), 1e-4)
    new_main = tuple(min(1.0, c) for c in target)
    new_other = tuple(min(1.0, c * ratio) for c in target)
    if _lum(target) < 0.01:      # near black: keep the stock sheen colour
        new_other = tuple(other[:3])
    if main_is_3:
        params["g_vec3TintColour3"], params["g_vec3TintColour2"] = new_main + (c3[3],), new_other + (c2[3],)
    else:
        params["g_vec3TintColour2"], params["g_vec3TintColour3"] = new_main + (c2[3],), new_other + (c3[3],)
    params["_template"] = best["sid"]
    return params


# --- installing -----------------------------------------------------------------------

def install(stream, full: Entry, colours: list[ForzaColour], stock: list[dict],
            nonr_limit: int | None = None) -> dict:
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
        params = shade(colour, stock)
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
    return {"installed": len(built), "host_count": host_count, "nonr": entry_size(nonr),
            "colours": [{"sequence": c.sequence, "rgb": "#%02x%02x%02x" % c.rgb, "metallic": c.metallic,
                         "class": c.colour_class, "finish": c.finish, "template": p["_template"],
                         "colour2": [round(x, 3) for x in p["g_vec3TintColour2"][:3]],
                         "colour3": [round(x, 3) for x in p["g_vec3TintColour3"][:3]],
                         "fresnelpower": round(p["g_fresnelpower"], 3)} for c, p, _ in built]}


def find_gamedb(car_folder: Path) -> Path | None:
    """out/cache/<game>/gamedb.slt next to an extracted car folder."""
    candidate = Path(car_folder).parent / "gamedb.slt"
    return candidate if candidate.exists() else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("colours", help="a car's factory colours and the stock shading each would get")
    c.add_argument("gamedb", type=Path)
    c.add_argument("media")
    c.add_argument("--dsf", type=Path, default=Path("D:/Games/Driver San Francisco/Install/dngvehicles.sp"))
    p = sub.add_parser("palette", help="print a vehicle's palette")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "colours":
        args.dsf = game_paths.stock(args.dsf)  # the original when a dsfforza install replaced it

    from chnk import read_chunk
    import upvh as uv
    if args.cmd == "palette":
        with args.archive.open("rb") as stream:
            root = read_chunk(stream, 0)
            full = uv.entries_for(stream, root.entries, args.id, None)[0]
            spss = palette_set(stream, full)
            for n, spsp in enumerate(spss.children if spss else []):
                q = spsp_params(stream, spsp)
                c2 = q["g_vec3TintColour2"][:3]
                srgb = "#%02x%02x%02x" % tuple(round(linear_to_srgb(x) * 255) for x in c2)
                print(f"{n:2} {srgb} metallic {is_metallic(q)!s:5} c2 {tuple(round(x, 3) for x in c2)} "
                      f"c3 {tuple(round(x, 3) for x in q['g_vec3TintColour3'][:3])} power {q['g_fresnelpower']:.2f}")
        return
    with args.dsf.open("rb") as stream:
        stock = stock_paints(stream, read_chunk(stream, 0).entries)
    for colour in pick_colours(forza_colours(args.gamedb, args.media)):
        q = shade(colour, stock)
        print(f"{colour.sequence:2} #{'%02x%02x%02x' % colour.rgb} {'metallic' if colour.metallic else 'solid   '} "
              f"{colour.colour_class:8} {colour.finish or ''}<- stock {q['_template']} c2 {tuple(round(x, 3) for x in q['g_vec3TintColour2'][:3])} "
              f"c3 {tuple(round(x, 3) for x in q['g_vec3TintColour3'][:3])} power {q['g_fresnelpower']:.2f}")


if __name__ == "__main__":
    sys.exit(main())
