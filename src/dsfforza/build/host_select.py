"""Pick the vanilla DSF vehicle a ported Forza car is built on (port_car --host auto).

A ported car is a clone of one vanilla vehicle (the host) with the Forza body
put in. Everything the converter doesn't rebuild stays the host's: the engine
sound (NONR/VSRT, a Wwise bank per vehicle), the hierarchy (UPVH: parts,
doors, collision, damage, lights), the VO3D base that src/dsfforza/build/handling.py
rescales (steering, assists, tyre curves, cameras), the paint sets (SPSR) and
the VSCT record. So the closer the host, the more the port behaves like a
first-party car. See research/hosts.md.

Ranking, per vanilla host (scores add up; reasons are printed):

1. Identity: the same model (make and model name, year within 4) +60, the same
   model family (Skyline R32 -> Skyline R33, Raptor -> F-150) +30, same make +6.
2. Engine, for the sound: cylinder count, layout (V/I/flat/W), aspiration
   (natural, turbo, supercharged, diesel, electric) and peak power, from
   vehicleStats (Lua) on the DSF side and gamedb.slt Data_Car on the Forza side.
3. Body, for hierarchy, collision and cockpit: body class (sedan, coupe,
   convertible, hatch, wagon, muscle, supercar, SUV, pickup, van) and door
   count (UPVH door records).
4. Size: wheelbase and track (UPVH hubs), length and height (MODL part 0
   box), mass (VO3D p0).
5. Drivetrain and era (vehicleStats Year).

Excluded: cop, taxi, emergency, tow, rig, trailer, security-van and Tanner
entries (VSCT category byte) unless --allow-special, and every host the
converter can't use (not 4 wheels, no steering marker/hinge, no _Interior
material, no Chassis/Lights materials in the full or exterior entry, missing
vertex-buffer strides, wheel parts it can't find). Drift and rally variants
of a vanilla car get a penalty so the civilian one wins.

    uv run dsff host_select rank NIS_370Z_10 BMW_M5_12 NIS_SkylineGTR_93@fm4 [--top 8]
    uv run dsff host_select survey --tsv research/hosts.tsv     # the vanilla fleet as hosts
    uv run dsff host_select roster --tsv research/host-picks.tsv  # every Forza car, odd picks flagged

The survey of the vanilla archive is cached in out/cache/host-survey.json.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza import REPO
from dsfforza.game import game_paths

DEFAULT_LUA = REPO / "out" / "lua-src" / "LuaScripts" / "vehiclestats" / "vehiclestats.lua"
DEFAULT_GAMEDBS = {"FH1": REPO / "out" / "db" / "fh1_gamedb.slt", "FM4": REPO / "out" / "db" / "fm4_gamedb.slt"}
SURVEY_CACHE = REPO / "out" / "cache" / "host-survey.json"
SURVEY_VERSION = 7

# VSCT record, last byte: vehicle category (research/hosts.md). Not a bit mask:
# the Desanne's three entries are 0x10 civilian saloon, 0x02 cop, 0x04 taxi.
CATEGORIES = {
    0x00: "security van",
    0x01: "fire truck",
    0x02: "cop",
    0x03: "ambulance",
    0x04: "taxi",
    0x05: "rig",
    0x06: "trailer",
    0x07: "truck",
    0x08: "tow truck",
    0x09: "limousine",
    0x0A: "bus",
    0x0B: "pickup",
    0x0C: "70s muscle",
    0x0D: "modern muscle",
    0x0E: "van",
    0x0F: "SUV",
    0x10: "saloon",
    0x11: "mini car",
    0x12: "super car",
    0x13: "sports car",
    0x14: "drift car",
    0x15: "rally/buggy",
    0x16: "classic",
    0x17: "off-roader",
    0x18: "Tanner",
}
SPECIAL_CATEGORIES = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x08, 0x18}
HEAVY_CATEGORIES = {0x07, 0x0A}

FRONT_DOOR_ROLES = {31, 32}
REAR_DOOR_ROLES = {52, 53, 54, 55}

# Forza make code -> name as it appears in DSF model names / vehicleStats pictures.
MAKE_NAMES = {
    "ACU": "acura",
    "ALF": "alfa_romeo",
    "AMC": "amc",
    "AST": "aston_martin",
    "AUD": "audi",
    "BEN": "bentley",
    "BMW": "bmw",
    "BUG": "bugatti",
    "BUI": "buick",
    "CAD": "cadillac",
    "CHE": "chevrolet",
    "CHR": "chrysler",
    "DOD": "dodge",
    "DEL": "dmc",
    "FER": "ferrari",
    "FIA": "fiat",
    "FOR": "ford",
    "GMC": "gmc",
    "HUM": "hummer",
    "JAG": "jaguar",
    "JEE": "jeep",
    "LAM": "lamborghini",
    "LAN": "lancia",
    "LR": "range_rover",
    "MAS": "maserati",
    "MCL": "mclaren",
    "MER": "mercedes",
    "NIS": "nissan",
    "OLD": "oldsmobile",
    "PAG": "pagani",
    "PLY": "plymouth",
    "PON": "pontiac",
    "RUF": "ruf",
    "SHE": "shelby",
    "VIP": "dodge",
    "VW": "volkswagen",
    "LIN": "lincoln",
    "HUD": "hudson",
}
MAKE_ALIASES = {
    "mercades": "mercedes",
    "land_rover": "range_rover",
    "amc_hudson": "hudson",
    "chrysler": "dodge",
    "plymouth": "dodge",
    "shelby": "ford",
}  # same-group sounds
# Model-name aliases (normalised, lowercase alphanumerics), applied to both sides.
MODEL_ALIASES = {
    "challanger": "challenger",
    "sirocco": "scirocco",
    "svtraptor": "f150raptor",
    "raptor": "f150raptor",
    "f150supercrew": "f150",
    "gtr": "gtr",
    "skylinegtr": "skyline",
    "corvettezr1": "corvette",
    "corvettez06": "corvette",
    "transam": "firebird",
    "firebirdtransam": "firebird",
    "cuda426": "barracuda",
    "cuda": "barracuda",
}

MUSCLE_WORDS = (
    "camaro",
    "mustang",
    "challenger",
    "charger",
    "firebird",
    "transam",
    "gto",
    "chevelle",
    "cuda",
    "barracuda",
    "torino",
    "cutlass",
    "superbee",
    "roadrunner",
    "nova",
    "boss",
    "mach1",
)
# Pickups: model-name prefixes (normalised), plus a few words anywhere. "raptor" alone is not one:
# SAL_S5SRaptor_10 is the Saleen S5S Raptor supercar; only Ford's SVT Raptor is an F-150.
PICKUP_PREFIXES = (
    "ram",
    "f150",
    "f250",
    "f350",
    "svtraptor",
    "syclone",
    "elcamino",
    "silverado",
    "tacoma",
    "tundra",
    "c10",
    "ranchero",
    "s331",
    "hilux",
)
PICKUP_WORDS = ("supercab", "supercrew", "pickup", "superduty")
VAN_PREFIXES = (
    "express",
    "savana",
    "vandura",
    "transit",
    "econoline",
    "caravan",
    "odyssey",
    "sienna",
    "microbus",
    "camper",
    "type2",
    "kombi",
    "multivan",
    "eurovan",
)
VAN_CODES = {"MER": ("sprinter",), "DOD": ("sprinter",)}
VAN_WORDS = ("cargo",)
WAGON_WORDS = (
    "avant",
    "wagon",
    "estate",
    "touring",
    "sportwagon",
    "xc70",
    "allroad",
    "vistacruiser",
    "shootingbrake",
    "variant",
)
HEAVY_MAKES = ("BUS", "KEN", "PET", "MAC", "FRE", "INT")  # Forza's bus and truck "makes" (FH1 BUS_TOUR, KEN_T440)
SUV_WORDS = (
    "rangerover",
    "escalade",
    "touareg",
    "jeep",
    "wrangler",
    "hummer",
    "blazer",
    "cayenne",
    "grandcherokee",
    "rav4",
    "explorer",
    "durango",
    "yukon",
    "tahoe",
    "suburban",
    "4runner",
    "landcruiser",
    "pathfinder",
    "xc70",
    "xc90",
    "h1alpha",
    "h3",
)
SUV_CODES = {
    "BMW": ("x5", "x6"),
    "AUD": ("q5", "q7"),
    "MER": ("ml", "gl"),
    "POR": ("cayenne",),
    "VOL": ("xc",),
    "HON": ("crv",),
}  # make -> model prefixes
CONVERTIBLE_WORDS = ("convertible", "volante", "spyder", "spider", "roadster", "cabrio", "speedster")
HATCH_WORDS = (
    "golf",
    "beetle",
    "mini",
    "cooper",
    "fiesta",
    "civic",
    "scirocco",
    "mito",
    "giulietta",
    "cygnet",
    "volt",
    "pacer",
    "abarth",
    "500",
    "focus",
    "megane",
    "clio",
    "polo",
    "fit",
    "yaris",
)

# Forza Data_Car.FamilyBodyID -> body class (labels are string IDs; classes read off the cars in
# each family, research/hosts.md). 12+ are race cars. FH1's family 1 is a catch-all (Corolla,
# Camry, A4 Avant, RAV4, F-350, Express van, tour bus, Kenworth truck): no class, decided by
# name and size (_forza_body).
FORZA_FAMILY = {
    2: "hatch",
    3: "hatch",
    4: "hatch",
    5: "hatch",
    6: "suv",
    7: "sedan",
    8: "convertible",
    9: "convertible",
    10: "coupe",
    11: "supercar",
    12: "race",
    13: "race",
    14: "race",
    15: "race",
    16: "race",
    17: "race",
    18: "race",
    19: "race",
    20: "race",
    21: "race",
    22: "race",
}

# Body class similarity (symmetric; 1 on the diagonal).
BODY_SIMILAR = {
    ("coupe", "muscle"): 0.6,
    ("coupe", "supercar"): 0.5,
    ("coupe", "convertible"): 0.6,
    ("coupe", "sedan"): 0.35,
    ("coupe", "hatch"): 0.35,
    ("coupe", "race"): 0.5,
    ("supercar", "race"): 0.7,
    ("sedan", "wagon"): 0.7,
    ("sedan", "hatch"): 0.4,
    ("sedan", "muscle"): 0.3,
    ("hatch", "wagon"): 0.4,
    ("muscle", "convertible"): 0.35,
    ("supercar", "convertible"): 0.35,
    ("suv", "pickup"): 0.5,
    ("suv", "van"): 0.4,
    ("suv", "wagon"): 0.3,
    ("pickup", "van"): 0.3,
    ("van", "truck"): 0.4,
    ("van", "bus"): 0.3,
    ("bus", "truck"): 0.4,
}


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def body_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return BODY_SIMILAR.get((a, b), BODY_SIMILAR.get((b, a), 0.0))


# --- engine descriptions ---------------------------------------------------------------


@dataclass
class Engine:
    cylinders: int | None  # 0 = electric
    layout: str | None  # V, I, F (flat), W, R (rotary), E (electric)
    aspiration: str  # na, turbo, sc, diesel, electric
    power_kw: float | None

    def text(self) -> str:
        if self.layout == "E":
            return "electric"
        cyl = f"{self.layout or '?'}{self.cylinders or '?'}"
        return (
            cyl
            + ("" if self.aspiration == "na" else f" {self.aspiration}")
            + (f" {self.power_kw:.0f} kW" if self.power_kw else "")
        )


def dsf_engine(stats: dict) -> Engine:
    """vehicleStats Cylinders ("V8 S/Chgr", "L6 TD", "H6 BiTurbo", "Electric") and PowerMetric (kW)."""
    text = stats.get("Cylinders", "").strip()
    power = _float(stats.get("PowerMetric"))
    if text.lower().startswith("electric"):
        return Engine(0, "E", "electric", power)
    m = re.match(r"([VLHWI])\s*(\d+)\s*(.*)", text)
    if not m:
        return Engine(None, None, "na", power)
    layout = {"L": "I", "H": "F"}.get(m.group(1), m.group(1))
    rest = m.group(3).lower()
    aspiration = "na"
    if rest.strip() == "td" or "diesel" in rest:
        aspiration = "diesel"
    elif "turbo" in rest:
        aspiration = "turbo"
    elif "s/c" in rest:
        aspiration = "sc"
    return Engine(int(m.group(2)), layout, aspiration, power)


def _float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def engine_score(host: Engine, car: Engine) -> tuple[float, list[str]]:
    """Up to 30 points: how alike the two engines (and so the sounds) should be."""
    score, why = 0.0, []
    if car.layout == "E" or host.layout == "E":
        if car.layout == host.layout:
            return 30.0, ["both electric"]
        return 0.0, []
    if car.cylinders and host.cylinders:
        diff = abs(car.cylinders - host.cylinders)
        pts = 12 if diff == 0 else 5 if diff <= 2 else 0
        score += pts
        if pts:
            why.append(f"{host.cylinders} cylinders" + ("" if diff == 0 else f" (car {car.cylinders})"))
    if car.layout and host.layout:
        if car.layout == host.layout:
            score += 5
            why.append(f"{host.layout} layout")
        elif {car.layout, host.layout} == {"V", "W"}:
            score += 3
    if car.aspiration == host.aspiration:
        score += 5
        if car.aspiration != "na":
            why.append(car.aspiration)
    elif {car.aspiration, host.aspiration} == {"turbo", "sc"}:
        score += 2
    if car.power_kw and host.power_kw:
        ratio = abs(math.log(car.power_kw / host.power_kw))
        pts = 8 * max(0.0, 1 - ratio / math.log(2))
        score += pts
        if pts >= 4:
            why.append(f"{host.power_kw:.0f} kW")
    return score, why


# --- the vanilla fleet ----------------------------------------------------------------


def _dsf_body(cls: str, model: str) -> str:
    n = norm(model)
    if any(w in n for w in ("elcamino",)):
        return "pickup"
    if any(w in n for w in ("camper", "caravan", "boxvan", "courier")):
        return "van"
    if any(w in n for w in ("avant", "vistacruiser")):
        return "wagon"
    if any(w in n for w in CONVERTIBLE_WORDS) or "cobra427" in n or "solstice" in n:
        return "convertible"
    body = next(
        (
            b
            for prefix, b in (
                ("Luxury Saloon", "sedan"),
                ("Saloon", "sedan"),
                ("Sports Coupe", "coupe"),
                ("Sports Car", "coupe"),
                ("Super Car", "supercar"),
                ("70's Muscle", "muscle"),
                ("70s Muscle", "muscle"),
                ("Modern Muscle", "muscle"),
                ("Tanner", "muscle"),
                ("Mini Car", "hatch"),
                ("SUV", "suv"),
                ("Pick Up", "pickup"),
                ("Van", "van"),
                ("Prison Van", "van"),
                ("Security Van", "van"),
                ("Truck", "truck"),
                ("Bus", "bus"),
                ("Buggy", "buggy"),
                ("Rally", "coupe"),
                ("DriftCar", "coupe"),
                ("Classic", "coupe"),
                ("Old", "sedan"),
                ("Cop Car", "sedan"),
                ("Taxi", "sedan"),
            )
            if cls.startswith(prefix)
        ),
        "sedan",
    )
    if any(w in n for w in SUV_WORDS):
        return "suv"
    if body in ("coupe", "sedan") and any(w in n for w in ("pacer", "beetle", "volt")):
        return "hatch"
    if any(w in n for w in ("jalpa", "miura", "countach")):
        return "supercar"
    return body


def survey(
    dsf: Path | None = None, lua: Path | None = DEFAULT_LUA, cache: Path | None = SURVEY_CACHE, refresh: bool = False
) -> list[dict]:
    """One record per vanilla base vehicle: identity, category, engine (vehicleStats),
    hierarchy facts (UPVH), size, VO3D mass, sound (VSRT), and what the converter
    needs (problems: why it can't be a host). Cached by archive size and mtime."""
    dsf = game_paths.vanilla(Path(dsf) if dsf else None, quiet=True)  # not an installed dsfforza archive (its ports)
    stat = dsf.stat()
    # Not the path: the same archive is read from the game folder or its dsfforza-backup copy
    # (handling_dsf.load_fleet).
    key = f"{SURVEY_VERSION}:{stat.st_size}:{int(stat.st_mtime)}:" + (
        f"{lua.stat().st_size}" if lua and lua.exists() else "-"
    )
    if cache and cache.exists() and not refresh:
        try:
            data = json.loads(cache.read_text())
        except (OSError, ValueError):  # cut short: survey again
            data = {}
        if data.get("key") == key:
            return data["hosts"]
    hosts = _survey(dsf, lua if lua and lua.exists() else None)
    if cache:
        # Atomic: port_batch workers (and other builds sharing out/cache) read it meanwhile.
        from dsfforza.build.port_car import write_atomic

        cache.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(cache, json.dumps({"key": key, "hosts": hosts}, indent=0).encode())
    return hosts


def _survey(dsf: Path, lua: Path | None) -> list[dict]:
    from dsfforza.build import clone_vehicle as cv
    from dsfforza.build import handling
    from dsfforza.convert import cockpit, forza2dsf
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats import upvh
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload
    from dsfforza.formats.vehicles import expand, parse_binf, parse_vsct

    stats = handling.lua_stats(lua) if lua else {}
    out = []
    with dsf.open("rb") as stream:
        root = read_chunk(stream, 0)
        entries = root.entries
        records = cv.sslp_records(stream, entries)
        nonr = child(next(e for e in entries if e.tag == "SSIC"), "NONR")
        vsct = parse_vsct(payload(stream, child(nonr, "VSCT")))
        binf = parse_binf(payload(stream, next(e for e in entries if e.tag == "BINF")))
        by_id = {sid: index for sid, index, _ in records}
        for sid, index, kind in sorted(records):
            if kind != 0:
                continue
            vehc = [by_id.get(sid + k) for k in cv.KIND_OFFSETS]
            name, fields = vsct.get(sid, ("?", ()))
            model = _pick_model(expand(name, binf), stats.get(sid, {}))
            st = stats.get(sid, {})
            rec = {
                "id": sid,
                "vehc": vehc,
                "class": entries[index].name,
                "model": model,
                "vsct": list(fields),
                "category": fields[-1] if fields else None,
                "stats": {
                    k: st[k]
                    for k in (
                        "Year",
                        "Cylinders",
                        "Drivetrain",
                        "PowerMetric",
                        "WeightMetric",
                        "Picture1",
                        "Manufacturer",
                        "Model",
                    )
                    if k in st
                },
                "problems": [],
            }
            rec["category_name"] = CATEGORIES.get(
                rec["category"], f"{rec['category']:#x}" if rec["category"] is not None else "?"
            )
            rec["body"] = _dsf_body(rec["class"], model + " " + st.get("Picture1", ""))
            if rec["category"] in HEAVY_CATEGORIES:  # GMC C5500 is "Van 07" but a medium truck
                rec["body"] = "bus" if rec["category"] == 0x0A else "truck"
            if any(w in norm(model) for w in ("viper", "corvettezr1", "corvettez06")):
                rec["body"] = "supercar"
            problems = rec["problems"]
            if None in vehc:
                problems.append("missing interior or exterior entry")
                out.append(rec)
                continue
            full, interior, exterior = (entries[i] for i in vehc)
            fnonr = child(full, "NONR")
            tags = {c.tag: c for c in fnonr.children}
            # Hierarchy
            h = upvh.parse(payload(stream, tags["UPVH"]))
            hubs = [w[:3] for w in h.vec4s("wheels")]
            rec["wheels"] = len(hubs)
            if len(hubs) == 4:
                zs = [w[2] for w in hubs]
                rec["wheelbase"] = round(max(zs) - min(zs), 4)
                rec["track"] = round(sum(abs(w[0]) for w in hubs) / 2, 4)
                rec["hub_y"] = round(sum(w[1] for w in hubs) / 4, 4)
                rec["axle_mid"] = round((max(zs) + min(zs)) / 2, 4)
            else:
                problems.append(f"{len(hubs)} wheels")
            roles = {r.role for r in h.records if r.type == 1}
            rec["doors"] = len(roles & FRONT_DOOR_ROLES) + min(2, len(roles & REAR_DOOR_ROLES))
            if rec["body"] == "muscle" and rec["doors"] == 4:  # Charger SRT8 2009: a muscle saloon
                rec["body"] = "sedan"
            rec["records"] = len(h.records)
            rec["lights"] = h.counts["lights"]
            try:
                hc = cockpit.host_cockpit(payload(stream, tags["UPVH"]))
                rec["gauges"] = sorted(hc.gauge_slots)
                rec["steering_skin"] = list(hc.wheel_skin)
            except ValueError as error:
                problems.append(f"cockpit: {error}")
                rec["gauges"] = []
            rec["seat_marker"] = 10 in cockpit.seat_markers(payload(stream, tags["UPVH"]))
            # Wheel parts: what the converter fills with the Forza wheel (forza2dsf.host_wheels).
            ext_modl = payload(stream, child(child(exterior, "NONR"), "MODL"))
            if len(hubs) == 4:
                hw = forza2dsf.host_wheels(payload(stream, tags["UPVH"]), payload(stream, tags["MODL"]), ext_modl)
                rec["wheel_parts"], rec["ext_wheel_parts"] = hw.full_parts, hw.exterior_parts
                if not hw.exterior_parts:
                    problems.append("no exterior wheel part (wheel records have no LOD link)")
            rec["ext_parts"] = struct.unpack_from("<I", ext_modl)[0]
            # Size: MODL part 0 box of the clean exterior (origin + three axis vectors).
            box = struct.unpack_from("<16f", ext_modl, 0x10)
            axes = [math.dist((0, 0, 0), box[i : i + 3]) for i in (4, 8, 12)]
            rec["length"], rec["width"], rec["height"] = (round(a, 3) for a in axes)
            # Handling
            vo3d = handling.VO3D.parse(payload(stream, tags["VO3D"]))
            rec["mass"] = round(vo3d.params.get(0, 0.0), 1)
            rec["front_drive"] = round(vo3d.params.get(371, 0.0), 3)
            # Sound: a Wwise bank (SBAK; BKHD +0x0C is the bank ID), plus a granular bank on 18 cars.
            vsrt = {c.tag: c for c in tags["VSRT"].children}
            sbak = payload(stream, vsrt["SBAK"])
            rec["sound"] = {
                "vsrt_size": tags["VSRT"].size,
                "bank_id": f"{struct.unpack_from('<I', sbak, 12)[0]:08x}",
                "granular": "GRBK" in vsrt,
                "granular_md5": hashlib.md5(payload(stream, vsrt["GRBK"])).hexdigest()[:8] if "GRBK" in vsrt else None,
            }
            rec["nonr_size"] = fnonr.size
            # Materials and vertex layouts the converter writes into.
            interior_drsp = child(child(interior, "MDXN"), "DRSP")
            for label, entry in (("full", full), ("exterior", exterior)):
                drsp = child(child(entry, "MDXN"), "DRSP")
                found = dm.host_materials(stream, drsp)
                if label == "exterior":  # most exteriors keep (some of) their _LOD1 materials elsewhere
                    for other in (interior_drsp, child(child(full, "MDXN"), "DRSP")):
                        for kind, value in dm.host_materials(stream, other, lod1=True).items():
                            found.setdefault(kind, value)
                if set(found) != {dm.CHASSIS, dm.LIGHTS_MAT}:
                    problems.append(f"{label}: Chassis/Lights materials {sorted(found)}")
                strides = sorted(
                    {
                        struct.unpack_from("<I", payload(stream, child(e, "VXHD")), 12)[0]
                        for e in drsp.children
                        if e.tag == "VXCK"
                    }
                )
                rec[f"{label}_strides"] = strides
                missing = {20, 28, 32} - set(strides)
                if missing:
                    problems.append(f"{label}: no vertex buffer of stride {sorted(missing)}")
            try:
                cockpit.host_interior(stream, interior)
            except (ValueError, StopIteration, KeyError) as error:
                problems.append(f"interior: {error}")
            out.append(rec)
    return out


def _pick_model(candidates: str, stats: dict) -> str:
    """BINF candidates "A|B|C" for a truncated VSCT name -> the one matching vehicleStats."""
    options = candidates.split("|")
    if len(options) == 1:
        return options[0]
    picture = norm(stats.get("Picture1", ""))
    year = stats.get("Year", "")

    def fit(option: str) -> tuple:
        n = norm(option)
        role = next((r for r in ("cop", "taxi", "drift", "rally", "tow", "ambulance") if r in picture), "civ")
        return (year and year in option, role in n.lower(), -len(n))

    return max(options, key=fit)


# --- the Forza car ---------------------------------------------------------------------


@dataclass
class ForzaInfo:
    media: str
    game: str
    year: int | None
    make: str
    model: str  # normalised model part of the media name
    engine: Engine
    drive: str | None
    body: str
    doors: int | None
    mass: float | None
    wheelbase: float | None
    track: float | None
    length: float | None
    height: float | None
    notes: list[str] = field(default_factory=list)


def forza_info(
    media: str,
    gamedbs: dict[str, Path] | None = None,
    game: str | None = None,
    folder: Path | None = None,
    doors_memo=None,
) -> ForzaInfo:
    """What host selection needs about a Forza car, from gamedb.slt ({game: path}, the
    game the car is taken from first; None: out/db). `folder`: the extracted car, for a
    door-count hint from the carbin's glass sections. `doors_memo` (door_memo()): the
    door counts of earlier runs, for port_batch's plan (issue #297)."""
    dbs = gamedbs if isinstance(gamedbs, dict) else dict(DEFAULT_GAMEDBS)
    order = sorted(dbs.items(), key=lambda kv: (kv[0] != game,))
    row = body = None
    used = None
    for name, path in order:
        if path is None or not Path(path).exists():
            continue
        con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT c.*, cy.Number AS CylNumber, cy.Rotors AS CylRotors FROM Data_Car c "
                "LEFT JOIN List_Cylinders cy ON cy.CylinderID = c.CylinderID "
                "WHERE lower(c.MediaName) = lower(?)",
                (media,),
            ).fetchone()
            if row is not None:
                body = con.execute("SELECT * FROM Data_CarBody WHERE Id = ?", (row["Id"] * 1000,)).fetchone()
                used = name or Path(path).name
                row, body = dict(row), dict(body) if body else None
                break
        finally:
            con.close()
    parts = media.split("_")
    make_code = parts[0].upper()
    model = norm("".join(parts[1:-1]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else ""))
    make = MAKE_NAMES.get(make_code, make_code.lower())
    if row is None:
        b = _forza_body(None, model, None, make_code=make_code)
        return ForzaInfo(
            media,
            game or "?",
            None,
            make,
            model,
            Engine(None, None, "na", None),
            None,
            b,
            DEFAULT_DOORS.get(b),
            None,
            None,
            None,
            None,
            None,
            ["not in gamedb.slt: identity only"],
        )
    config = {1: "V", 2: "W", 3: "I", 4: "R", 5: "F", 6: "E"}.get(row.get("EngineConfigID"))
    cylinders = row.get("CylNumber")
    if config == "E":
        engine = Engine(0, "E", "electric", None)
    else:
        aspiration = {1: "na", 2: "turbo", 3: "turbo", 4: "turbo", 5: "sc", 6: "sc", 7: "turbo"}.get(
            row.get("AspirationTypeId"), "na"
        )
        if "diesel" in media.lower() or "tdi" in media.lower():
            aspiration = "diesel"
        engine = Engine(
            cylinders, config, aspiration, row["SimPeakPower"] / 10 if row.get("SimPeakPower") else None
        )  # 100 W -> kW
    drive = {1: "FWD", 2: "RWD", 3: "AWD"}.get(row.get("DriveTypeID"))
    mass = row["CurbWeight"] * 100 if row.get("CurbWeight") else None
    year = row.get("Year")
    length = height = wheelbase = track = None
    if body:
        wheelbase = body.get("ModelWheelbase")
        track = (body.get("ModelFrontTrackOuter") or 0) or None
        if body.get("PristineBoundingBoxMaxZ") is not None:
            length = body["PristineBoundingBoxMaxZ"] - body["PristineBoundingBoxMinZ"]
            height = body["PristineBoundingBoxMaxY"] - body["PristineBoundingBoxMinY"]
    fb = row.get("FamilyBodyID")
    b = _forza_body(fb, model, engine, make_code=make_code, year=year, mass=mass, length=length, height=height)
    if b in ("bus", "truck") and engine.aspiration == "turbo":  # Forza has no fuel field; trucks and buses are diesel
        engine.aspiration = "diesel"
    doors = None
    if folder is not None and doors_memo is not None and b in DOOR_RULE_BODIES:
        doors = doors_memo.get(door_key(folder, media, b), lambda: _carbin_doors(folder, media, b))
    elif folder is not None:
        doors = _carbin_doors(folder, media, b)
    notes = []
    if doors is not None:
        notes.append(f"{doors} doors from the carbin's side glass")
    else:
        doors = DEFAULT_DOORS.get(b)
    return ForzaInfo(
        media, str(used), year, make, model, engine, drive, b, doors, mass, wheelbase, track, length, height, notes
    )


# Door count when the carbin can't tell (None: don't score doors).
DEFAULT_DOORS = {"sedan": 4, "suv": 4, "wagon": 4, "coupe": 2, "convertible": 2, "supercar": 2, "muscle": 2, "race": 2}


def _forza_body(
    family,
    model: str,
    engine: Engine | None,
    make_code: str = "",
    year=None,
    mass: float | None = None,
    length: float | None = None,
    height: float | None = None,
) -> str:
    """Body class from the media name, gamedb FamilyBodyID and the body's size (Data_Car
    CurbWeight, Data_CarBody pristine bounding box)."""
    if make_code in HEAVY_MAKES or (mass and mass >= 4500) or (length and length >= 7.0):
        return "bus" if make_code == "BUS" or "bus" in model else "truck"
    if (
        model.startswith(VAN_PREFIXES)
        or any(w in model for w in VAN_WORDS)
        or model.startswith(VAN_CODES.get(make_code, ("\0",)))
    ):
        return "van"
    if (
        model.startswith(PICKUP_PREFIXES)
        or any(w in model for w in PICKUP_WORDS)
        or (make_code == "GMC" and model.startswith("sierra"))
    ):
        return "pickup"
    if any(w in model for w in SUV_WORDS) or model.startswith(SUV_CODES.get(make_code, ("\0",))):
        return "suv"
    body = FORZA_FAMILY.get(family)
    if body is None:  # FH1 family 1 and unknown: by name. The pristine bounding box is no
        # help: its height is 2.0 m on the Focus ZX4 and 1.8 m on the Corolla.
        if any(w in model for w in WAGON_WORDS):
            return "wagon"
        return "sedan" if family is not None else "coupe"
    if body in ("sedan", "hatch") and any(w in model for w in WAGON_WORDS):
        return "wagon"
    if any(w in model for w in CONVERTIBLE_WORDS):
        return "convertible"
    american = make_code in ("CHE", "DOD", "FOR", "PON", "PLY", "OLD", "BUI", "AMC", "MRC", "SHE", "CHR")
    if american and body in ("coupe", "convertible") and any(w in model for w in MUSCLE_WORDS):
        return "muscle"
    return body


TWO_DOOR_BODIES = ("coupe", "muscle", "supercar", "convertible", "race")
DOOR_RULE_BODIES = ("sedan", "suv", "wagon", "hatch", "pickup") + TWO_DOOR_BODIES  # not vans, buses, trucks
VENT_PANE = (-0.15, 0.45)  # m: a pane ending ahead of this and shorter than this is a vent pane
FRONT_GLASS_2DOOR = 0.4  # m behind the wheelbase midpoint: the front door glass ends past it on 2-doors
FRONT_GLASS_4DOOR = 0.175  # x wheelbase: on 4-doors it ends before it (Rapide 0.167, the 3-doors from 0.185)
REAR_DOOR_START = (-0.2, 0.12)  # m from the front door glass's end: where the rear door's (or quarter's) skin starts
REAR_DOOR_END = 0.08  # m behind the rear axle: a rear door ends ahead of it, a rear quarter past it


DOORS_MEMO = REPO / "out" / "cache" / "carbin-doors.json"


def door_key(folder: Path, media: str, body: str) -> str:
    """The door memo's key for a car's door count."""
    return f"{os.path.normcase(os.path.realpath(folder))}|{media}|{body}"


def door_memo():
    """The door counts port_batch's plan keeps between runs (fingerprint.Memo; a full
    roster's take minutes to measure). Call save() on it when done. Workers don't use it:
    they measure the car anyway, and the memo file would be an input of their fingerprint."""
    from dsfforza.build import fingerprint

    return fingerprint.Memo(DOORS_MEMO)


@functools.lru_cache(maxsize=32)  # forza_info asks up to three times per car (host, sound, same vehicle)
def _carbin_doors(folder: Path, media: str, body: str | None = None) -> int | None:
    """Door count from the main carbin's left side, or None when it can't tell.

    Forza has no door sections, and the side-glass names don't say which pane is a
    door: glassLM is the rear-door window on the M5 but the door window behind a vent
    pane on the 2-door Charger Daytona, Gallardo and MP4-12C. But the `body` mesh is cut
    along the real shut lines (research/damage.md, "Doors and wings as Forza parts"), so:

    - the front door glass is the first left pane after any vent pane (short, ahead of
      the wheelbase midpoint), with panes that overlap it by more than 0.1 m;
    - a rear door is a side panel of the body that starts where that glass ends, ends
      by the rear axle, and carries a door handle (a 3-door's rear quarter, split at the
      wheel arch on the Civic Mugen and Golf GTI VR6, carries none); a rear quarter
      starts there and runs past the rear axle.

    4 doors: a rear door, and the front glass ends by 0.175 x wheelbase (saloons and SUVs
    0.06 to 0.167). 2 doors: the front glass ends past that (3-doors from 0.185: the 205
    T16's side intake carries something handle-like), or there is no rear door and the
    front glass ends 0.4 m or more behind the midpoint (2-doors 0.40 to 1.11, saloons up
    to 0.38) or a rear quarter starts behind it. Coupe, muscle, supercar, convertible and race bodies never get 4 (side
    intakes ahead of a mid-engine's rear wheel look like rear doors). Issue #208: the
    first rule merged a rear-door pane touching the front one into it and called the
    2004 WRX STi, S204, Carlton, Rapide ... 2-door."""
    if body not in DOOR_RULE_BODIES:
        return None  # vans, buses, trucks: their glass and doors don't follow a car's
    try:
        from dsfforza.convert import forza2dsf

        car = forza2dsf.load_car(Path(folder), media)
        dims = forza2dsf.car_dims(car)
    except (OSError, ValueError):
        return None
    sign = 0.0
    panes = []
    for s in car.sections:
        if not re.fullmatch(r"glassL[FMR]", s.name):
            continue
        subs = [sub for sub in s.subsections if sub.lod > 0]
        if not subs:
            continue
        lod = min(sub.lod for sub in subs)
        pts = [
            s.vertices_for(sub)[i].position for sub in subs if sub.lod == lod for tri in sub.triangles() for i in tri
        ]
        if pts:
            # Forza's front is -z; measure backwards from the wheelbase midpoint.
            panes.append((min(p[2] for p in pts) - dims.mid_z, max(p[2] for p in pts) - dims.mid_z))
            sign += sum(p[0] for p in pts)
    if not panes:
        return None
    panels, handles = _side_parts(car, dims, 1.0 if sign >= 0 else -1.0)
    return door_count(panes, panels, handles, dims.wheelbase, body)


def door_count(panes: list, panels: list, handles: list, wheelbase: float, body: str | None) -> int | None:
    """_carbin_doors' rule on measured parts (z in m from the wheelbase midpoint
    backwards): left side panes (z0, z1), body panels (z0, z1, y0, y1), door handle
    centres (z, y)."""
    panes = sorted(panes)
    i = 0
    while i + 1 < len(panes) and panes[i][1] < VENT_PANE[0] and panes[i][1] - panes[i][0] < VENT_PANE[1]:
        i += 1
    start, end = panes[i]
    for z0, z1 in panes[i + 1 :]:
        if z0 < end - 0.1:
            end = max(end, z1)
    if end - start > 1.6:
        return None  # one pane for every side window (Prius)
    rear_axle = wheelbase / 2
    near = [p for p in panels if end + REAR_DOOR_START[0] <= p[0] <= end + REAR_DOOR_START[1] and p[1] - p[0] >= 0.5]
    rear = [
        p
        for p in near
        if p[1] <= rear_axle + REAR_DOOR_END
        and any(p[0] + 0.25 <= hz <= p[1] and p[2] <= hy <= p[3] + 0.15 for hz, hy in handles)
    ]
    quarter = [p for p in near if p[1] > rear_axle + REAR_DOOR_END]
    long_front = end > FRONT_GLASS_4DOOR * wheelbase
    if rear and not long_front:
        return None if body in TWO_DOOR_BODIES else 4
    if long_front or end >= FRONT_GLASS_2DOOR or quarter:
        return 2
    return None


def _side_parts(car, dims, sign: float) -> tuple[list, list]:
    """The `body` section's pieces on the carbin's left side (finest LOD past 0), as
    triangles joined through shared vertices (damage.part_components): panels, side-facing
    paint skins of at least 0.12 m2, as (z0, z1, y0, y1); and door handles, small pieces
    (0.08 to 0.4 m long, at most 0.15 m tall, 12 triangles or more) at the side, as their
    centre (z, y). z from the wheelbase midpoint backwards."""
    from dsfforza.convert import forza_materials as fm
    from dsfforza.convert.damage import part_components

    body = next((s for s in car.sections if s.name == "body"), None)
    lods = [sub.lod for sub in body.subsections if sub.lod > 0] if body is not None else []
    if not lods:
        return [], []
    lod = min(lods)
    keys, tris = [], []
    for sub in body.subsections:
        if sub.lod != lod:
            continue
        pool = body.vertices_for(sub)
        paint = fm.classify(sub.material)[0] == fm.PAINT
        for t in sub.triangles():
            keys.append([(id(pool), i) for i in t])
            tris.append(([pool[i].position for i in t], paint))
    groups: dict[int, list] = {}
    for c, tri in zip(part_components(keys), tris):
        groups.setdefault(c, []).append(tri)
    panels, handles = [], []
    for members in groups.values():
        pts = [q for p, _ in members for q in p]
        zs = [q[2] - dims.mid_z for q in pts]
        ys = [q[1] for q in pts]
        at_side = min(q[0] * sign for q in pts) >= 0.5 and len(members) >= 12
        if at_side and 0.08 <= max(zs) - min(zs) <= 0.4 and max(ys) - min(ys) <= 0.15:
            handles.append(((min(zs) + max(zs)) / 2, (min(ys) + max(ys)) / 2))
            continue
        side = total = 0.0
        for p, paint in members:
            if not paint:
                continue
            a = [p[1][k] - p[0][k] for k in range(3)]
            b = [p[2][k] - p[0][k] for k in range(3)]
            n = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
            area = math.sqrt(sum(c * c for c in n)) / 2
            total += area
            if area and n[0] * sign / (2 * area) > 0.5:
                side += area
        if side >= 0.12 and side >= 0.5 * total:
            panels.append((min(zs), max(zs), min(ys), max(ys)))
    return sorted(panels), handles


# --- scoring ------------------------------------------------------------------------


def _model_parts(host: dict) -> tuple[str, str, int | None, str]:
    """(make, normalised model, year, model family) of a vanilla host, from vehicleStats
    Picture1 ("ford_mustang_shelby_gt500_old": ford, mustangshelbygt500, 1967, mustang)."""
    pic = host["stats"].get("Picture1", "").lower()
    pic = re.sub(r"^unlicensed_", "", pic)
    pic = re.sub(r"_(civ|cop|taxi|drift|rally|tow|old|onew|ambulance)$", "", pic)
    make = ""
    for name in sorted(set(MAKE_NAMES.values()) | set(MAKE_ALIASES), key=len, reverse=True):
        if pic.startswith(name + "_"):
            make, pic = MAKE_ALIASES.get(name, name), pic[len(name) + 1 :]
            break
    year = _float(host["stats"].get("Year"))
    if year is None:
        m = re.search(r"(19|20)\d\d", host["model"])
        year = float(m.group(0)) if m else None
    family = _alias(norm(pic.split("_")[0])) if make else ""
    return make, norm(pic), int(year) if year else None, family


def _alias(model: str) -> str:
    return MODEL_ALIASES.get(model, model)


def identity_score(host: dict, car: ForzaInfo) -> tuple[float, str | None]:
    make, model, year, family = _model_parts(host)
    car_make = MAKE_ALIASES.get(car.make, car.make)
    if not make or make != car_make:
        return 0.0, None
    a, b = _alias(model), _alias(car.model)
    close_year = year is None or car.year is None or abs(year - car.year) <= 4
    if a and b and (a in (b, car.model) or model == b) and close_year:
        return 60.0, f"same model ({host['model']})"
    # Families are words of 3+ letters (skyline, mustang) or short names with a digit (a4, q7, r8).
    if (len(family) >= 3 or (len(family) == 2 and any(c.isdigit() for c in family))) and (
        b.startswith(family) or car.model.startswith(family)
    ):
        return 30.0, f"same model family ({host['model']})"
    if a and b and (a in b or b in a) and min(len(a), len(b)) >= 3:
        return 30.0, f"same model family ({host['model']})"
    return 6.0, "same make"


# Corporate groups (make names as in MAKE_NAMES / vehicleStats pictures): their
# badge-engineered twins share one body and cabin (Chevrolet Express / GMC Savana,
# Silverado / Sierra, Tahoe / Yukon, Ford / Mercury).
MAKE_GROUPS = {
    "gm": (
        "chevrolet",
        "gmc",
        "pontiac",
        "buick",
        "cadillac",
        "oldsmobile",
        "hummer",
        "holden",
        "vauxhall",
        "opel",
        "saturn",
    ),
    "chrysler": ("dodge", "chrysler", "plymouth", "jeep"),
    "ford": ("ford", "mercury", "lincoln", "shelby"),
    "vw": ("volkswagen", "audi", "seat", "skoda"),
    "toyota": ("toyota", "lexus", "scion"),
    "nissan": ("nissan", "infiniti"),
    "honda": ("honda", "acura"),
}
# A host is the same vehicle as the Forza car (same model, or a badge twin) only
# with the same body class, model years this close and these dimensions.
SAME_VEHICLE_YEARS = 4
SAME_VEHICLE_WHEELBASE = 0.025  # relative
SAME_VEHICLE_LENGTH = 0.04  # relative
TWIN_WHEELBASE = 0.01  # relative, badge twins (two makes)
TWIN_LENGTH = 0.01


def make_group(make: str) -> str:
    make = MAKE_ALIASES.get(make, make)
    return next((g for g, makes in MAKE_GROUPS.items() if make in makes), make)


def same_vehicle(host: dict, car: ForzaInfo) -> str | None:
    """Why the vanilla host is the same vehicle as the Forza car, or None: the same
    model (identity_score's "same model": make, model name, year within 4) or a
    badge-engineered twin (another make of the same group), either way with the
    same body class, model years within SAME_VEHICLE_YEARS and wheelbase and length
    within SAME_VEHICLE_WHEELBASE / SAME_VEHICLE_LENGTH. A host that only shares a
    model family (A4 Avant 2004 on the A4 2010) or a platform (Yukon XL 2001 on the
    Escalade 2007) is not the same vehicle: its cabin differs."""
    make, _, year, _ = _model_parts(host)
    if not make or year is None or car.year is None or abs(year - car.year) > SAME_VEHICLE_YEARS:
        return None
    if host["body"] != car.body:
        return None
    if not (car.wheelbase and host.get("wheelbase")):
        return None
    wb = abs(car.wheelbase - host["wheelbase"]) / host["wheelbase"]
    if wb > SAME_VEHICLE_WHEELBASE:
        return None
    if car.length and host.get("length") and abs(car.length - host["length"]) / host["length"] > SAME_VEHICLE_LENGTH:
        return None
    size = f"{host['body']}, {year} vs {car.year}, wheelbase {host['wheelbase']:.3f} vs {car.wheelbase:.3f} m"
    ident, text = identity_score(host, car)
    if ident >= 60:
        return f"{text}; {size}"
    # Badge engineering: one vehicle sold under two makes of a group. Platform mates
    # (300C / Charger, Cuda / Challenger, Golf / Scirocco) share a wheelbase but not
    # the cabin; their lengths differ by 1.6% and more, or they carry the same badge.
    raw = make_parts_raw(host)
    if raw == car.make or make_group(raw) != make_group(car.make) or make_group(raw) not in MAKE_GROUPS:
        return None
    if (
        wb > TWIN_WHEELBASE
        or not (car.length and host.get("length"))
        or (abs(car.length - host["length"]) / host["length"] > TWIN_LENGTH)
    ):
        return None
    return (
        f"badge twin ({host['model']}, {make_group(raw).upper()} group); {size}, length "
        f"{host['length']:.3f} vs {car.length:.3f} m"
    )


def same_vehicle_for(
    media: str,
    host_id: int,
    dsf: Path | None = None,
    game: str | None = None,
    folder: Path | None = None,
    gamedbs: dict[str, Path] | None = None,
    lua: Path | None = DEFAULT_LUA,
) -> str | None:
    """same_vehicle for a Forza car on a vanilla host given by spool ID."""
    host = next((h for h in survey(dsf, lua) if h["id"] == host_id), None)
    return same_vehicle(host, forza_info(media, gamedbs, game, folder)) if host else None


def make_parts_raw(host: dict) -> str:
    """The host's make as its vehicleStats picture names it, before MAKE_ALIASES."""
    pic = re.sub(r"^unlicensed_", "", host["stats"].get("Picture1", "").lower())
    for name in sorted(set(MAKE_NAMES.values()) | set(MAKE_ALIASES), key=len, reverse=True):
        if pic.startswith(name + "_"):
            return name
    return ""


def score_host(host: dict, car: ForzaInfo, mode: str = "host") -> tuple[float, list[str]]:
    """(score, reasons). mode "sound" ranks by engine alone (identity, engine, era)."""
    why: list[str] = []
    total = 0.0
    ident, text = identity_score(host, car)
    total += ident
    if text:
        why.append(f"+{ident:.0f} {text}")
    engine = dsf_engine(host["stats"])
    pts, reasons = engine_score(engine, car.engine)
    total += pts
    why.append(f"+{pts:.0f} engine {engine.text()}" + (f" ({', '.join(reasons)})" if reasons else ""))
    year = _float(host["stats"].get("Year"))
    if year and car.year:
        pts = 5 * max(0.0, 1 - abs(year - car.year) / 30)
        total += pts
        if pts >= 2.5:
            why.append(f"+{pts:.0f} era {year:.0f}")
    if mode == "sound":
        return total, why
    sim = body_similarity(host["body"], car.body)
    pts = 15 * sim
    total += pts
    if pts:
        why.append(f"+{pts:.0f} body {host['body']}" + ("" if sim == 1 else f" (car {car.body})"))
    if car.doors and host.get("doors") and car.doors == host["doors"]:
        total += 6
        why.append(f"+6 {host['doors']} doors")
    host_drive = {"4WD": "AWD"}.get(host["stats"].get("Drivetrain"), host["stats"].get("Drivetrain"))
    if car.drive and host_drive == car.drive:
        total += 4
        why.append(f"+4 {car.drive}")
    size = 0.0
    if car.wheelbase and host.get("wheelbase"):
        size += 6 * max(0.0, 1 - abs(car.wheelbase - host["wheelbase"]) / 0.6)
    if car.track and host.get("track"):
        size += 3 * max(0.0, 1 - abs(car.track - host["track"] - 0.22) / 0.35)  # outer track vs hub track
    if car.length and host.get("length"):
        size += 3 * max(0.0, 1 - abs(car.length - host["length"]) / 1.2)
    if car.height and host.get("height"):
        size += 3 * max(0.0, 1 - abs(car.height - host["height"]) / 0.5)
    if car.mass:
        mass = host.get("mass") or 1700.0  # VO3D default when p0 is not set (2 vanilla cars)
        size += 4 * max(0.0, 1 - abs(math.log(car.mass / mass)) / math.log(1.6))
    total += size
    why.append(f"+{size:.0f} size (wheelbase {host.get('wheelbase', 0):.2f}, {host.get('mass', 0):.0f} kg)")
    if not {1, 2} <= set(host.get("gauges", [])):
        total -= 3
        why.append("-3 not both gauges (speed 2, tach 1)")
    if re.search(r"_(drift|rally)$", host["model"], re.I):  # the civilian sibling is a better base
        total -= 8
        why.append("-8 drift/rally variant of a vanilla car")
    if host["category"] in HEAVY_CATEGORIES and car.body not in ("bus", "truck", "van"):
        total -= 20
        why.append("-20 heavy vehicle")
    return total, why


@dataclass
class Ranked:
    id: int
    score: float
    host: dict
    reasons: list[str]
    excluded: str | None = None


def rank(car: ForzaInfo, hosts: list[dict], allow_special: bool = False, mode: str = "host") -> list[Ranked]:
    """Every vanilla host, best first; excluded hosts (special categories, converter
    problems) at the end with the reason."""
    out = []
    for h in hosts:
        score, reasons = score_host(h, car, mode)
        excluded = None
        if h["problems"] and mode == "host":
            excluded = "; ".join(h["problems"])
        elif h["category"] in SPECIAL_CATEGORIES and not allow_special:
            excluded = f"special vehicle ({h['category_name']})"
        out.append(Ranked(h["id"], round(score, 2), h, reasons, excluded))
    out.sort(key=lambda r: (r.excluded is not None, -r.score, r.id))
    return out


def choose(
    media: str,
    dsf: Path | None = None,
    game: str | None = None,
    folder: Path | None = None,
    gamedbs: dict[str, Path] | None = None,
    lua: Path | None = DEFAULT_LUA,
    allow_special: bool = False,
    mode: str = "host",
) -> tuple[int, list[Ranked], ForzaInfo]:
    """The best host for one Forza car: (spool ID, ranking, car info)."""
    hosts = survey(dsf, lua)
    car = forza_info(media, gamedbs, game, folder)
    ranking = rank(car, hosts, allow_special, mode)
    if ranking[0].excluded:
        raise SystemExit(f"{media}: no usable host")
    return ranking[0].id, ranking, car


def describe(car: ForzaInfo) -> str:
    return (
        f"{car.media} ({car.game}): {car.year} {car.make} {car.model}, {car.engine.text()}, {car.drive}, "
        f"{car.body}, {car.doors or '?'} doors, {car.mass or 0:.0f} kg, wheelbase {car.wheelbase or 0:.2f}"
    )


def host_line(r: Ranked) -> str:
    h = r.host
    return f"{r.id:4} {r.score:6.1f}  {h['class'][:17]:17} {h['model'][:30]:30} " + (
        "EXCLUDED: " + r.excluded if r.excluded else "; ".join(r.reasons)
    )


# --- whole roster -----------------------------------------------------------------------

ROSTER_COLUMNS = [
    "media",
    "game",
    "year",
    "body",
    "doors",
    "engine",
    "drive",
    "mass",
    "host",
    "host_model",
    "host_class",
    "host_body",
    "score",
    "next",
    "margin",
    "flags",
    "reasons",
]


def roster_flags(car: ForzaInfo, best: Ranked, runner: Ranked | None) -> list[str]:
    """Reasons to look at a pick by hand."""
    flags = []
    host = best.host
    sim = body_similarity(host["body"], car.body)
    if sim < 0.5:
        flags.append(f"body {car.body} on {host['body']}")
    if car.doors and host.get("doors") and car.doors != host["doors"] and sim < 1:
        flags.append(f"{car.doors} doors on {host['doors']}")
    if best.score < 45:
        flags.append("low score")
    if (
        car.engine.cylinders
        and dsf_engine(host["stats"]).cylinders
        and abs(car.engine.cylinders - dsf_engine(host["stats"]).cylinders) > 2
    ):
        flags.append(f"{car.engine.cylinders} cyl on {dsf_engine(host['stats']).cylinders}")
    if car.wheelbase and host.get("wheelbase") and abs(car.wheelbase - host["wheelbase"]) > 0.6:
        flags.append(f"wheelbase {car.wheelbase:.2f} on {host['wheelbase']:.2f}")
    if runner is not None and best.score - runner.score < 1 and (body_similarity(runner.host["body"], car.body) > sim):
        flags.append(f"close call with {runner.id} ({runner.host['body']})")
    if car.notes and car.notes[0].startswith("not in gamedb"):
        flags.append("no gamedb row")
    return flags


def roster_table(hosts: list[dict], roster: Path, cache: Path, out: Path | None) -> list[dict]:
    """host_select over every car in the Forza roster (FH1 first, as port_car picks), one row
    per car with the pick, the runner-up, and flags for picks that look wrong."""
    import csv

    rows = []
    with roster.open(encoding="utf-8") as f:
        cars = [r for r in csv.DictReader(f, delimiter="\t") if r["kind"] == "car"]
    for r in cars:
        games = r["games"].split(",")
        game = "FH1" if "FH1" in games else games[0]
        folder = cache / game.lower() / r["media_name"]
        car = forza_info(r["media_name"], None, game, folder if folder.exists() else None)
        ranking = [x for x in rank(car, hosts) if not x.excluded]
        best, runner = ranking[0], (ranking[1] if len(ranking) > 1 else None)
        flags = roster_flags(car, best, runner)
        rows.append(
            {
                "media": car.media,
                "game": game,
                "year": car.year or "",
                "body": car.body,
                "doors": car.doors or "",
                "engine": car.engine.text(),
                "drive": car.drive or "",
                "mass": round(car.mass or 0),
                "host": best.id,
                "host_model": best.host["model"],
                "host_class": best.host["class"],
                "host_body": best.host["body"],
                "score": best.score,
                "next": f"{runner.id} {runner.host['model']}" if runner else "",
                "margin": round(best.score - runner.score, 1) if runner else "",
                "flags": "; ".join(flags),
                "reasons": "; ".join(best.reasons),
            }
        )
    lines = ["\t".join(ROSTER_COLUMNS)] + ["\t".join(str(row[c]) for c in ROSTER_COLUMNS) for row in rows]
    text = "\n".join(lines) + "\n"
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}: {len(rows)} cars, {sum(bool(r['flags']) for r in rows)} flagged")
    else:
        sys.stdout.write(text)
    return rows


# --- CLI ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dsf", type=Path, default=None, help="vanilla dngvehicles.sp (default: from $DSF_GAME_DIR)")
    parser.add_argument("--lua", type=Path, default=DEFAULT_LUA, help="decompiled vehiclestats.lua")
    parser.add_argument("--refresh", action="store_true", help="re-read the archive instead of the cache")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("rank", help="rank vanilla hosts for Forza cars")
    p.add_argument("cars", nargs="+", metavar="NAME[@fh1|@fm4]")
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--allow-special", action="store_true", help="consider cop/taxi/emergency/... entries")
    p.add_argument("--sound", action="store_true", help="rank by engine only (for --sound-host)")
    p.add_argument("--cache", type=Path, default=REPO / "out" / "cache", help="port_car extraction cache")
    p = sub.add_parser("roster", help="pick a host for every car in research/forza-roster.tsv and flag odd picks")
    p.add_argument("--roster", type=Path, default=REPO / "research" / "forza-roster.tsv")
    p.add_argument("--tsv", type=Path, help="write the table here (default: stdout)")
    p.add_argument("--cache", type=Path, default=REPO / "out" / "cache", help="port_car extraction cache")
    p = sub.add_parser("survey", help="print (or write) the vanilla fleet as hosts")
    p.add_argument("--tsv", type=Path)
    args = parser.parse_args(argv)
    args.dsf = game_paths.vanilla(args.dsf)  # the original when a dsfforza install replaced it

    hosts = survey(args.dsf, args.lua, refresh=args.refresh)
    if args.cmd == "survey":
        cols = [
            "id",
            "class",
            "model",
            "category_name",
            "body",
            "doors",
            "wheels",
            "wheelbase",
            "track",
            "length",
            "height",
            "mass",
            "gauges",
            "engine",
            "drive",
            "year",
            "granular",
            "usable",
        ]
        lines = ["\t".join(cols)]
        for h in hosts:
            row = dict(
                h,
                engine=dsf_engine(h["stats"]).text(),
                drive=h["stats"].get("Drivetrain", ""),
                year=h["stats"].get("Year", ""),
                granular=int(h.get("sound", {}).get("granular", False)),
                usable="yes" if not h["problems"] else "; ".join(h["problems"]),
                gauges=",".join(map(str, h.get("gauges", []))),
            )
            lines.append("\t".join(str(row.get(c, "")) for c in cols))
        text = "\n".join(lines) + "\n"
        if args.tsv:
            args.tsv.write_text(text, encoding="utf-8")
            print(f"wrote {args.tsv} ({len(hosts)} vehicles, {sum(not h['problems'] for h in hosts)} usable)")
        else:
            sys.stdout.write(text)
        return
    if args.cmd == "roster":
        roster_table(hosts, args.roster, args.cache, args.tsv)
        return
    for text in args.cars:
        media, _, game = text.partition("@")
        game = game.upper() or None
        folder = None
        for g in [game] if game else ["FH1", "FM4"]:
            candidate = args.cache / g.lower() / media
            if candidate.exists():
                folder, game = candidate, g
                break
        car = forza_info(media, None, game, folder)
        print(describe(car))
        ranking = rank(car, hosts, args.allow_special, "sound" if args.sound else "host")
        for r in ranking[: args.top]:
            print("  " + host_line(r))
        print()


if __name__ == "__main__":
    sys.exit(main())
