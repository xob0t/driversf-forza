"""Port Forza cars (FH1, FM4) into Driver: San Francisco's vehicle archive.

    uv run dsff port_car --dsf "$DSF_GAME_DIR/Install/dngvehicles.sp" \\
        --out out/ported.sp --car NIS_370Z_10 --car BMW_M5_12@fh1 --car NIS_SkylineGTR_93@fm4

For each --car NAME[@fh1|@fm4]:

1. Find it on the user's discs (assets/*.iso, src/dsfforza/build/forza_roster.py). Without
   a suffix, FH1 is used when the car is on both games' discs (the FH1 path
   is the one tested in game); @fm4 picks FM4 (disc 1 zips or disc 2 packs).
2. Extract the main and cockpit carbins, the nodamage/Lights atlases and
   their LOD0 versions, interior_LOD0, MAXData.xml and carattribs.xml into
   out/cache/<game>/<NAME>/ (once; re-extracted when this list grows), and
   the game's gamedb.slt (cockpit eye point).
3. Clone the host under the next free base ID from --first-id (IDs below 1000
   that no vehicle uses). --host auto (default) picks the most similar vanilla car
   per Forza car (src/dsfforza/build/host_select.py: same model, engine for the sound, body,
   size, era; research/hosts.md); --host N forces one (266 = ASYM Desanne, the
   host of every earlier port). --sound-host N|auto takes the engine sound
   (NONR/VSRT) from another vanilla car (untested in game). Give the
   clone's resources and MODL parts fresh UIDs (clone_vehicle.renew_uids) so
   several ported cars, and the host itself, can be loaded together. Shared
   part UIDs made a ported car render as the host once the host's entries
   were loaded too (research/in-game-tests.md).
4. Convert with forza2dsf.convert: hollow full entry (Forza LOD 3/4/5 for DSF
   masks 4/8/16, LOD 5 shadow), clean exterior from LOD 1, the car's atlases
   in the host's chassis texture (replace mode), host materials kept, merged
   submodels. This is the configuration confirmed in game for the 370Z.
   If an entry is over the size limits, coarser LODs are tried in turn.
   The interior entry is rebuilt from the car's cockpit carbin, with the
   steering hinge, gauges and head camera moved (src/dsfforza/convert/cockpit.py; not yet
   tested in game; --no-cockpit keeps the host's interior). If that interior
   can't fit, the host's is kept.
5. Engine sound (src/dsfforza/convert/engine_sound.py, research/engine-sound.md; untested in
   game): the car's own Forza engine loops (its ET/HT files, EngAmb loops for
   set A, Exh for set B) go into a vanilla template bank with the 370Z layout,
   the best engine match first, with retuned pitch and crossfade curves,
   VSVH+4C from the cylinder count and fresh Wwise IDs in the car's spool-ID
   namespace. Templates and shorter loops are tried until the NONR fits.
   When the car's Forza audio can't be resolved or nothing fits, the host's
   (or --sound-host's) sound stays, and the reason is logged and reported.
   Previews: <out>-sound/<media>/ (sweep_new.wav, sweep_template.wav, wav/).
   --no-forza-sound turns the step off.

Then the archive is written and every car is checked: no resource or MODL part
UID it defines is defined by another vehicle (before writing), and
src/dsfforza/formats/upvh.py lint (hierarchy and MODL consistency, UID shape, and no
reference to a UID only another vehicle defines). Two files go next to it:
<out>.json (manifest: car -> spool IDs, host, source disc, LODs, placement,
sizes) and <out>.lua (vehicleStats entries copied from the host, for the Lua
probe; --install-lua also copies it to tools/game/lua/dsfforza/ported.lua).

Size limits (research/spool-limits.md): each entry's NONR must fit the
per-kind NONR buffer in the SSLP header (+0x08 + 4 * kind; the game memcpy's
without a check), and MDXN stays at or under the largest vanilla MDXN of its
kind (a bigger one crashed; the cause is still open).

Wheel placement (research/forza-sources.md): Forza's front is at -z and
DSF's at +z, so the car is turned 180 degrees about y and translated. Wheelbase, front track and the wheelbase midpoint come
from Physics/MAXData.xml, which every FH1 and FM4 car on the discs has
(except FH1's NIS_Leaf_11); otherwise from gamedb.slt Data_CarBody (row
Data_Car.Id * 1000; covers 503 FM4 cars). Tyre radius and width are measured
on the car's wheel model. The hubs in the host's UPVH are moved to match.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dsfforza import REPO
from dsfforza.build import (
    clone_vehicle,
    gentle,
    handling,
)
from dsfforza.build.forza_roster import Source, default_images, roster
from dsfforza.convert import (
    cockpit,
    dsf_materials,
    forza2dsf,
)
from dsfforza.formats import stfs
from dsfforza.formats.chnk import ENTRY_LIMIT, entry_size, layout, read_chunk, write_chunk
from dsfforza.formats.forzazip import ForzaZip
from dsfforza.formats.vehicle_mesh import child
from dsfforza.formats.xiso import XIso
from dsfforza.game import game_paths

GAME_PREFERENCE = ("FH1", "FM4")
FULL_LADDER = ("4:3,8:4,16:5", "4:4,8:4,16:5", "4:4,8:5,16:5", "4:5,8:5,16:5")
EXTERIOR_LADDER = (1, 2, 3, 4)
SHADOW_LOD = 5
STATIC_READ_BUFFER = 0x280000
# Car files the conversion reads; matched case-insensitively against the car's
# folder in the zip. The carbin is required, the rest are used when present.
WANTED = (
    "{name}.carbin",
    "nodamage.xds",
    "lights.xds",
    "physics/maxdata.xml",
    "carattribs.xml",
    "shadersettings.xml",
    # cockpit (src/dsfforza/convert/cockpit.py): interior entry, gauges, head camera
    "{name}_cockpit.carbin",
    "nodamage_lod0.xds",
    "lights_lod0.xds",
    "interior_lod0.xds",
)

MAKES = {
    "ACU": "Acura",
    "ALF": "Alfa Romeo",
    "AMC": "AMC",
    "ASC": "Ascari",
    "AST": "Aston Martin",
    "AUD": "Audi",
    "AUS": "Austin",
    "BEN": "Bentley",
    "BER": "Bertone",
    "BMW": "BMW",
    "BUG": "Bugatti",
    "BUI": "Buick",
    "BUS": "Bus",
    "CAD": "Cadillac",
    "CHE": "Chevrolet",
    "CHR": "Chrysler",
    "CIT": "Citroen",
    "DEL": "DeLorean",
    "DET": "De Tomaso",
    "DEV": "Devon",
    "DOD": "Dodge",
    "EAG": "Eagle",
    "FER": "Ferrari",
    "FIA": "Fiat",
    "FOR": "Ford",
    "GMC": "GMC",
    "GUM": "Gumpert",
    "HEN": "Hennessey",
    "HOL": "Holden",
    "HON": "Honda",
    "HUM": "Hummer",
    "HYU": "Hyundai",
    "INF": "Infiniti",
    "JAG": "Jaguar",
    "JOS": "Joss",
    "KEN": "Kenworth",
    "KIA": "Kia",
    "KOE": "Koenigsegg",
    "LAM": "Lamborghini",
    "LAN": "Lancia",
    "LEX": "Lexus",
    "LOT": "Lotus",
    "LR": "Land Rover",
    "MAS": "Maserati",
    "MAZ": "Mazda",
    "MCL": "McLaren",
    "MER": "Mercedes-Benz",
    "MIN": "MINI",
    "MIT": "Mitsubishi",
    "MOR": "Morgan",
    "MOS": "Mosler",
    "MRC": "Mercury",
    "NIS": "Nissan",
    "NOB": "Noble",
    "OLD": "Oldsmobile",
    "OPE": "Opel",
    "PAG": "Pagani",
    "PAN": "Panoz",
    "PEU": "Peugeot",
    "PLY": "Plymouth",
    "PON": "Pontiac",
    "RAD": "Radical",
    "REN": "Renault",
    "ROS": "Rossion",
    "RUF": "RUF",
    "SAA": "Saab",
    "SAL": "Saleen",
    "SAT": "Saturn",
    "SCI": "Scion",
    "SEA": "SEAT",
    "SHE": "Shelby",
    "SPA": "Spania GTA",
    "SPY": "Spyker",
    "SSC": "SSC",
    "SUB": "Subaru",
    "SUZ": "Suzuki",
    "TOY": "Toyota",
    "TVR": "TVR",
    "ULT": "Ultima",
    "VAU": "Vauxhall",
    "VIP": "SRT",
    "VOL": "Volvo",
    "VW": "Volkswagen",
    "WIE": "Wiesmann",
}


def make_model(media: str) -> tuple[str, str]:
    """ "NIS_SkylineGTR_93" -> ("Nissan", "SkylineGTR"). Make and model names are
    string IDs in the games' LSB2 tables (not decoded), so they are built from
    the media name."""
    parts = media.split("_")
    make = MAKES.get(parts[0].upper(), parts[0])
    model = " ".join(p for p in parts[1:-1]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
    return make, model


def display_name(media: str, year: int | None) -> str:
    """ "NIS_SkylineGTR_93", 1993 -> "Nissan SkylineGTR (1993)"."""
    make, model = make_model(media)
    return f"{make} {model}".strip() + (f" ({year})" if year else "")


def car_entry(
    media: str,
    year: int | None,
    games: str,
    source: Source,
    cache: Path,
    spool_id: int,
    host: int,
    host_name: str,
    report: dict,
) -> dict:
    """One car's record in the manifest's "cars" (port_car and port_batch)."""
    return {
        "media": media,
        "name": display_name(media, year),
        "year": year,
        "game": source.game,
        "games_available": games,
        "source": {"iso": Path(source.iso).name, "where": source.where, "package": source.package, "path": source.path},
        "cache": str(Path(cache) / source.game.lower() / media),
        "spool_id": spool_id,
        "interior_id": spool_id + 1000,
        "exterior_id": spool_id + 2000,
        "host": host,
        "host_name": host_name,
        **report,
    }


# --- finding and extracting cars --------------------------------------------------


@dataclass
class CarRequest:
    media: str
    game: str | None  # "FH1", "FM4" or None (prefer FH1)


def parse_car(text: str) -> CarRequest:
    name, _, game = text.partition("@")
    if game and game.lower() not in ("fh1", "fm4"):
        raise SystemExit(f"--car {text}: suffix must be @fh1 or @fm4")
    return CarRequest(name, game.upper() or None)


def pick_source(cars: dict, request: CarRequest):
    car = cars.get(request.media.lower())
    if car is None or car.kind != "car":
        raise SystemExit(f"{request.media}: not a car on the discs (see research/forza-roster.tsv)")
    sources = [s for s in car.sources if s.kind == "car"]
    for game in [request.game] if request.game else GAME_PREFERENCE:
        for source in sources:
            if source.game == game:
                return car, source
    raise SystemExit(f"{request.media}: not on the {request.game} discs (only {car.games})")


def open_car_zip(source: Source) -> tuple[XIso, ForzaZip, str]:
    """(image, zip, folder prefix inside the zip) for a roster source."""
    image = XIso(source.iso)
    if source.package:
        entry = image.find(source.package)
        package = stfs.in_image(image, entry)
        inner, folder = source.path.split("::", 1)
        return image, ForzaZip(package.open(inner)), folder.lower()
    return image, ForzaZip(image.open(image.find(source.path))), ""


def colour_variants(names: set[str], written: set[str]) -> dict[str, str]:
    """{canonical name: variant file} for a car whose zip has no nodamage.xds, only
    colour variants of it (CHE_CamaroZ28_79: nodamage_blue/_gold/...; PON_TransAm_73:
    nodamagegreen/red/white...). The variant that sorts first is taken, its _lod0
    with it. `names`: the car folder's files, lower case; `written`: those extracted."""
    if "nodamage.xds" in written:
        return {}
    stems = sorted(
        n[: -len(".xds")]
        for n in names
        if n.startswith("nodamage") and n.endswith(".xds") and "/" not in n and not n.endswith("_lod0.xds")
    )
    if not stems:
        return {}
    stem = stems[0]
    out = {"nodamage.xds": f"{stem}.xds"}
    if "nodamage_lod0.xds" not in written and f"{stem}_lod0.xds" in names:
        out["nodamage_lod0.xds"] = f"{stem}_lod0.xds"
    return out


def car_folder(cache: Path, game: str, media: str) -> Path:
    """cache/<game>/<media>/: where extract_car puts a car's files."""
    return cache / game.lower() / media


def extract_car(source: Source, media: str, cache: Path, log=print) -> Path:
    """Extract the files the conversion needs into cache/<game>/<media>/."""
    folder = car_folder(cache, source.game, media)
    stamp = folder / "source.json"
    try:
        old = json.loads(stamp.read_text())
    except (OSError, ValueError):  # none yet, or cut short: extract again
        old = {}
    # Re-extract when WANTED grew since the cache was made (e.g. the cockpit files),
    # and once for a cache made before colour variants were taken (no nodamage.xds).
    if (
        old.get("path") == source.path
        and old.get("wanted") == list(WANTED)
        and ("variants" in old or "nodamage.xds" in {f.lower() for f in old.get("files", [])})
    ):
        return folder
    image, archive, prefix = open_car_zip(source)
    try:
        wanted = {w.format(name=media).lower() for w in WANTED}
        written, infos = [], {}
        for info in archive.entries():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.lower().startswith(prefix):
                continue
            relative = name[len(prefix) :]
            infos[relative.lower()] = info
            if relative.lower() not in wanted:
                continue
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(target, archive.read(info))
            written.append(relative)
        variants = colour_variants(set(infos), {w.lower() for w in written})
        for canonical, variant in variants.items():
            write_atomic(folder / canonical, archive.read(infos[variant]))
            written.append(canonical)
    finally:
        image.close()
    if f"{media}.carbin".lower() not in {w.lower() for w in written}:
        raise SystemExit(f"{media}: no main carbin in {source.path}")
    write_atomic(
        stamp,
        json.dumps(
            {
                "game": source.game,
                "where": source.where,
                "iso": source.iso,
                "package": source.package,
                "path": source.path,
                "files": sorted(written),
                "wanted": list(WANTED),
                "variants": variants,
            },
            indent=1,
        ).encode(),
    )
    log(f"{media}: extracted {len(written)} files from {source.where} to {folder}")
    for canonical, variant in variants.items():
        log(f"{media}: no {canonical}; using the colour variant {variant}")
    return folder


# The car's wheel texture (forza_materials.WHEEL): every car of both games has its own
# wheel archive next to the cars, media/wheels/<car>.zip on the discs and
# media/wheels/<car>/ in the disc 2 packs (all 40 testdrive cars). It holds the
# wheel carbin (the main carbin's wheel section plus LOD 0), wheel.xds (what the
# rim techniques sample at LOD 1+), wheel_LOD0.xds and the blur discs wheel0..3.xds.
# The archive's carbin ({name}.carbin) is cached as wheel.carbin (the main carbin has
# the car's name too): forza2dsf.load_car takes its wheel when the main carbin has none.
# wheel2.xds, the middle blur picture, is the wheel's speed-blur disc (fm.WHEEL_BLUR).
WHEEL_FILES = ("wheel.xds", "{name}.carbin", "wheel2.xds")
WHEEL_RENAME = {"{name}.carbin": forza2dsf.WHEEL_CARBIN}
WHEEL_STAMP = "wheel_source.json"


def wheel_source(source: Source, media: str) -> tuple[str, str]:
    """(zip path on the disc or in the package, folder prefix inside it) of a car's
    wheel archive: media/cars/X.zip -> media/wheels/X.zip; pack cars
    (<inner zip>::media/cars/x/) -> (<inner zip>, media/wheels/x/)."""
    if source.package:
        inner = source.path.split("::", 1)[0]
        return inner, f"media/wheels/{media}/".lower()
    base = source.path.replace("\\", "/").rsplit("/", 2)[0]  # "media" or "Media"
    return f"{base}/wheels/{media}.zip", ""


def extract_wheel(source: Source, media: str, cache: Path, log=print) -> list[str]:
    """Put the car's wheel texture (WHEEL_FILES) into its cache folder, beside the
    car files; its own stamp, so caches made before stay valid. Returns the files
    written (cached: the stamp's list; none if the car has no wheel archive)."""
    folder = cache / source.game.lower() / media
    stamp = folder / WHEEL_STAMP
    path, prefix = wheel_source(source, media)
    try:
        old = json.loads(stamp.read_text())
    except (OSError, ValueError):
        old = {}
    if old.get("path") == path and old.get("prefix") == prefix and old.get("wanted") == list(WHEEL_FILES):
        return old.get("files", [])
    image = XIso(source.iso)
    written = []
    try:
        if source.package:
            archive = ForzaZip(stfs.in_image(image, image.find(source.package)).open(path))
        else:
            try:
                archive = ForzaZip(image.open(image.find(path)))
            except KeyError:
                archive = None
        wanted = {w.format(name=media).lower(): WHEEL_RENAME.get(w, w).lower() for w in WHEEL_FILES}
        for info in archive.entries() if archive is not None else []:
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.lower().startswith(prefix) or name[len(prefix) :].lower() not in wanted:
                continue
            target = wanted[name[len(prefix) :].lower()]
            folder.mkdir(parents=True, exist_ok=True)
            write_atomic(folder / target, archive.read(info))
            written.append(target)
    finally:
        image.close()
    folder.mkdir(parents=True, exist_ok=True)
    write_atomic(
        stamp,
        json.dumps(
            {"path": path, "prefix": prefix, "files": sorted(written), "wanted": list(WHEEL_FILES)}, indent=1
        ).encode(),
    )
    log(
        f"{media}: wheel texture {', '.join(written) if written else 'not found'} ({path}{'::' + prefix if prefix else ''})"
    )
    return written


# The car's brake rotors (forza2dsf.brake_rotor_section): <car>_rotorLF_LOD0.carbin and
# _rotorLR_ (front and rear axle; the right-hand ones mirror them) in the car's zip, LOD 0
# only. Cached under the names in BRAKE_FILES (some zips name the carbins differently from
# the zip), with their own stamp so caches made before stay valid.
BRAKE_FILES = {"rotorlf": forza2dsf.ROTOR_CARBINS["front"], "rotorlr": forza2dsf.ROTOR_CARBINS["rear"]}
BRAKE_STAMP = "brake_source.json"


def extract_brakes(source: Source, media: str, cache: Path, log=print) -> list[str]:
    """Put the car's front and rear brake rotor carbins (BRAKE_FILES) into its cache folder.
    Returns the files written (cached: the stamp's list; none if the car has no rotors)."""
    folder = cache / source.game.lower() / media
    stamp = folder / BRAKE_STAMP
    try:
        old = json.loads(stamp.read_text())
    except (OSError, ValueError):
        old = {}
    if old.get("path") == source.path and old.get("wanted") == sorted(BRAKE_FILES.values()):
        return old.get("files", [])
    image, archive, prefix = open_car_zip(source)
    written = []
    try:
        for info in archive.entries():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.lower().startswith(prefix):
                continue
            match = re.fullmatch(r".*_(rotor[lr][fr])_lod0\.carbin", name[len(prefix) :].lower())
            if match is None or "/" in name[len(prefix) :] or name[len(prefix) :].lower().startswith("stripped_"):
                continue
            target = BRAKE_FILES.get(match.group(1))
            if target is None or target in written:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            write_atomic(folder / target, archive.read(info))
            written.append(target)
    finally:
        image.close()
    folder.mkdir(parents=True, exist_ok=True)
    write_atomic(
        stamp,
        json.dumps(
            {"path": source.path, "files": sorted(written), "wanted": sorted(BRAKE_FILES.values())}, indent=1
        ).encode(),
    )
    log(f"{media}: brake rotors {', '.join(sorted(written)) if written else 'not found'}")
    return written


DASH_STAMP = "dash_source.json"
DASH_FOLDER = "digitalgauge"  # the car's Anark dash scene (cockpit digital displays)
DASH_MEDIA = "dash_media"  # per game: shared dashboard textures and the vector fonts
DASH_MEDIA_ZIPS = (  # (zip on the disc, member prefix, folder under DASH_MEDIA)
    ("media/cars/shared.zip", "dashboards/", "shared/dashboards"),
    ("media/ui/fonts.zip", "", "fonts"),
)


def extract_dash(source: Source, media: str, cache: Path, log=print) -> list[str]:
    """The car's DigitalGauge/ folder (Dash_<car>.bsg/.fbf/.bgf and its textures:
    formats/anark.py) into <cache folder>/digitalgauge/; its own stamp, so caches made
    before stay valid. Returns the files (none if the car has no dash scene)."""
    folder = cache / source.game.lower() / media
    stamp = folder / DASH_STAMP
    try:
        old = json.loads(stamp.read_text())
    except (OSError, ValueError):
        old = {}
    if old.get("path") == source.path:
        return old.get("files", [])
    image, archive, prefix = open_car_zip(source)
    written = []
    try:
        for info in archive.entries():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.lower().startswith(prefix + "digitalgauge/"):
                continue
            target = folder / DASH_FOLDER / name.rsplit("/", 1)[-1]
            target.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(target, archive.read(info))
            written.append(target.name)
    finally:
        image.close()
    folder.mkdir(parents=True, exist_ok=True)
    write_atomic(stamp, json.dumps({"path": source.path, "files": sorted(written)}, indent=1).encode())
    if written:
        log(f"{media}: dash scene, {len(written)} files")
    return written


def extract_dash_media(isos: list[Path], game: str, cache: Path) -> Path | None:
    """The game's shared dashboard textures (Media/cars/Shared.zip dashboards/) and
    vector fonts (Media/UI/Fonts.zip) under <cache>/<game>/dash_media/, from the first
    disc of that game that has them."""
    target = cache / game.lower() / DASH_MEDIA
    if (target / "done").exists():
        return target
    from dsfforza.build.forza_roster import TITLES, xex_title

    found = set()
    for iso in isos:
        image = XIso(iso)
        try:
            if TITLES.get(xex_title(image)[0]) != game:
                continue
            for path, member, sub in DASH_MEDIA_ZIPS:
                if path in found:
                    continue
                try:
                    archive = ForzaZip(image.open(image.find(path)))
                except KeyError:
                    continue
                for info in archive.entries():
                    name = info.filename.replace("\\", "/")
                    if info.is_dir() or not name.lower().startswith(member):
                        continue
                    out = target / sub / name[len(member) :]
                    out.parent.mkdir(parents=True, exist_ok=True)
                    write_atomic(out, archive.read(info))
                found.add(path)
        finally:
            image.close()
    if not found:
        return None
    write_atomic(target / "done", json.dumps(sorted(found)).encode())
    return target


def extract_gamedb(isos: list[Path], game: str, cache: Path) -> Path | None:
    """The game's Media/db/gamedb.slt, from the first disc of that game that has it."""
    target = cache / game.lower() / "gamedb.slt"
    if target.exists():
        return target
    from dsfforza.build.forza_roster import discs_of

    for iso in discs_of(isos, game):  # by default.xex, whatever the files are called
        image = XIso(iso)
        try:
            try:
                entry = image.find("Media/db/gamedb.slt")
            except KeyError:
                continue
            # Under a temporary name: a cut-short copy must not pass for the database later.
            temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            try:
                image.extract(entry, temp)
                Path(temp).replace(target)
            except PermissionError:  # another process has it open, so it is already there
                if not target.exists():
                    raise
            finally:
                temp.unlink(missing_ok=True)
            return target
        finally:
            image.close()
    return None


def write_atomic(path: Path, data: bytes) -> None:
    """Write a cache file under a temporary name, then rename it into place, so a
    reader in another process (parallel builds share out/cache) never sees half of
    it. If the file is open elsewhere (Windows can't replace it then) the existing
    copy, from the same source, stays."""
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(data)
        try:
            Path(temp).replace(path)
        except PermissionError:
            if not path.exists():
                raise
    finally:
        temp.unlink(missing_ok=True)


# --- building ---------------------------------------------------------------------


def uid_prefix(*parts) -> bytes:
    return hashlib.sha1(":".join(str(p) for p in parts).encode()).digest()[:4]


def vanilla_limits(stream, entries, mdxn_limit: int | None = None) -> tuple[dict[int, int], dict[int, int]]:
    """(NONR buffer per kind from the SSLP header, largest vanilla MDXN per kind, or
    mdxn_limit (--mdxn-limit) where that is bigger)."""
    nonr = child(next(e for e in entries if e.tag == "SSIC"), "NONR")
    header = clone_vehicle.read_leaf(stream, child(nonr, "SSLP"))
    nonr_limit = dict(enumerate(struct.unpack_from("<3I", header, 8)))
    mdxn_max: dict[int, int] = {}
    for _, index, kind in clone_vehicle.sslp_records(stream, entries):
        mdxn = next((c for c in entries[index].children if c.tag == "MDXN"), None)
        if mdxn is not None:
            mdxn_max[kind] = max(mdxn_max.get(kind, 0), mdxn.size)
    if mdxn_limit:
        # lim-pad-over (VEHC 0x30A000, past the 0x280000 static read buffer)
        # loads in game, so the vanilla maxima aren't hard limits.
        mdxn_max = {kind: max(size, mdxn_limit) for kind, size in mdxn_max.items()}
    return nonr_limit, mdxn_max


def check_entry(vehc, kind: int, nonr_limit: dict, mdxn_max: dict) -> tuple[dict, list[str]]:
    sizes = {c.tag: entry_size(c) for c in vehc.children}
    total = layout(vehc.children)[0]
    # The spool reads align(align(size + align4(name), 1 << align_log2), 0x800) bytes
    # into a static 0x280000 buffer, or a heap buffer above that (untested in game).
    name = -(-len(vehc.name_raw) // 4) * 4
    read = -(-(-(-(total + name) // (1 << vehc.align_log2)) * (1 << vehc.align_log2)) // 0x800) * 0x800
    problems = []
    if sizes.get("NONR", 0) > nonr_limit[kind]:
        problems.append(f"NONR {sizes['NONR']:#x} > SSLP buffer {nonr_limit[kind]:#x}")
    if sizes.get("MDXN", 0) > mdxn_max[kind]:
        problems.append(f"MDXN {sizes['MDXN']:#x} > vanilla max {mdxn_max[kind]:#x}")
    if read > STATIC_READ_BUFFER:
        problems.append(f"VEHC read size {read:#x} > static buffer {STATIC_READ_BUFFER:#x}")
    for mdxn in (c for c in vehc.children if c.tag == "MDXN"):
        for drsp in (c for c in mdxn.children if c.tag == "DRSP"):
            if len(drsp.children) > ENTRY_LIMIT:
                problems.append(f"DRSP has {len(drsp.children)} children (> {ENTRY_LIMIT})")
    sizes["VEHC"] = total
    return sizes, problems


def clamp_lods(text: str, available: set[int]) -> dict[int, int]:
    """Masks -> Forza LODs, each moved to the nearest LOD the car has (coarser first)."""

    def near(lod: int) -> int:
        if lod in available:
            return lod
        lower = [x for x in available if x < lod]
        return max(lower) if lower else min(available)

    return {mask: near(lod) for mask, lod in forza2dsf.parse_lods(text).items()}


def forza_sound_plan(
    args,
    stream,
    entries,
    media: str,
    game: str,
    folder: Path,
    gamedb: Path | None,
    isos,
    sound: int | None,
    spool_id: int,
    log,
):
    """(engine_sound.Plan or None, report) for the car's own Forza engine sound
    (src/dsfforza/convert/engine_sound.py, research/engine-sound.md): its Forza loops, its engine
    type and the vanilla template banks to try, best engine match first (an explicit
    --sound-host before them). None, with the reason, when it is disabled or the
    car's Forza audio can't be resolved; the car then keeps the host's sound."""
    if getattr(args, "no_forza_sound", False):
        return None, {"ok": False, "reason": "disabled (--no-forza-sound)"}
    from dsfforza.build import host_select
    from dsfforza.convert import engine_sound

    try:
        index = engine_sound.vanilla_index(args.dsf)
        db = gamedb or extract_gamedb(isos, game, args.cache)
        info = host_select.forza_info(media, {game: db} if db else None, game, folder)
        ranking = host_select.rank(info, host_select.survey(args.dsf), allow_special=True, mode="sound")
        templates = engine_sound.template_order(
            index, [r.id for r in ranking], [sound] if isinstance(sound, int) else []
        )
        forza = engine_sound.forza_engine(media, game, isos, args.cache / "sound")
        engine_sound.pick_emissions(forza)
    except (LookupError, ValueError, OSError, RuntimeError, sqlite3.Error, SystemExit) as error:
        reason = (
            f"{type(error).__name__}: {error}"
            if not isinstance(error, (LookupError, SystemExit))
            else str(error).strip("'\"")
        )
        log(f"  forza sound: {media}: {reason}; keeping the host's sound")
        return None, {"ok": False, "reason": reason}
    etype = engine_sound.engine_type(info.engine.cylinders, info.engine.layout)
    log(
        f"  forza sound: idle {forza['idle']:g}, redline {forza['redline']:g}, "
        + ", ".join(f"{e} {v['bank']} ({len(v['loops'])} loops)" for e, v in forza["emissions"].items())
        + f"; {info.engine.text()} -> engine type {engine_sound.ENGINE_TYPES.get(etype, 'template')}"
        + f"; templates {templates}"
        + (f"; {forza['et_substitute']}" if forza.get("et_substitute") else "")
    )
    plan = engine_sound.Plan(
        stream, entries, media, game, spool_id, forza, templates, index["reserved"], etype, info.engine.text(), log
    )
    return plan, {"ok": False, "reason": "not built"}


def fit_forza_sound(stream, full, plan, nonr_limit, mdxn_max, log) -> dict:
    """Install the plan's first candidate (template, loop length) under which the
    full entry's NONR fits its SSLP buffer; keep the clone's own VSRT when none does."""
    from dsfforza.convert import engine_sound

    nonr = child(full, "NONR")
    at = next(n for n, c in enumerate(nonr.children) if c.tag == "VSRT")
    original = nonr.children[at]
    over = []
    for candidate in plan.candidates():
        dropped = engine_sound.install(full, candidate.sbak, candidate.vsvh, stream)
        _, problems = check_entry(full, 0, nonr_limit, mdxn_max)
        nonr_over = [p for p in problems if p.startswith("NONR")]
        if not nonr_over:
            return {"ok": True, **dropped, **candidate.report, "_sound": candidate}
        over.append(f"template {candidate.report['template']} at {candidate.report['max_seconds']} s: {nonr_over[0]}")
        log(f"  forza sound: {over[-1]}")
        nonr.children[at] = original
    nonr.children[at] = original
    reason = "; ".join(plan.failures + over) or "no usable template"
    log(f"  forza sound: none fits ({reason}); keeping the host's sound")
    return {"ok": False, "reason": reason}


def build_car(
    stream,
    entries,
    host: int,
    spool_id: int,
    car: forza2dsf.ForzaCar,
    dims,
    args,
    nonr_limit,
    mdxn_max,
    log,
    gamedb: Path | None = None,
    sound: int | None = None,
    forza_sound=None,
    forza_sound_report: dict | None = None,
    same_vehicle: str | None = None,
) -> tuple[list, dict]:
    """Clone + convert, stepping down LODs until every entry fits. Returns the
    three VEHC entries (not yet registered) and a report. forza_sound (an
    engine_sound.Plan from forza_sound_plan) replaces the clone's engine sound.
    same_vehicle (host_select.same_vehicle): why the host is the Forza car's own
    vehicle; its interior then stays when the Forza cockpit is a placeholder."""
    available = {sub.lod for s in car.body for sub in s.subsections if sub.lod > 0}
    full_ladder = [args.lods] if args.lods else list(FULL_LADDER)
    ext_ladder = [args.exterior_lod] if args.exterior_lod else list(EXTERIOR_LADDER)
    fi = ei = 0
    attempts = []
    use_cockpit = not args.no_cockpit
    use_rotors = not getattr(args, "no_brake_rotors", False)
    while True:
        lods = clamp_lods(full_ladder[fi], available)
        shadow = clamp_lods(f"1:{SHADOW_LOD}", available)[1]
        exterior_lod = clamp_lods(f"2:{ext_ladder[ei]}", available)[2]
        clones = clone_vehicle.make_clones(stream, entries, host, spool_id, args.vehc_name or car.name[:31])
        sound_report = None
        if sound is not None and sound != host:
            sound_report = clone_vehicle.copy_sound(stream, entries, clones[0], sound, spool_id)
        renewed = 0
        if not args.share_host_uids:
            renewed = len(clone_vehicle.renew_uids(stream, clones, uid_prefix("renew", car.name, spool_id)))
        opts = forza2dsf.ConvertOptions(
            lods=lods,
            shadow_lod=shadow,
            exterior_lod=exterior_lod,
            keep_host_materials=True,
            textures=None if args.textures == "none" else args.textures,
            merge_submodels=True,
            uid_seed=uid_prefix("geometry", car.name, spool_id),
            cockpit=use_cockpit,
            gamedbs=[gamedb] if gamedb else [],
            glass_links=args.glass_links,
            same_vehicle_host=same_vehicle,
            plates=not getattr(args, "no_plates", False),
            exhaust=not getattr(args, "no_exhaust", False),
            split_rear_wheel=not getattr(args, "no_rear_wheel_part", False),
            cockpit_opts=cockpit.CockpitOptions(mdxn_limit=mdxn_max[1]) if use_cockpit else None,
            wheel_blur=not getattr(args, "no_wheel_blur", False),
            brake_rotors=use_rotors,
            blur_materials=dsf_materials.shared_macos(stream, entries, dsf_materials.SPOKE_DISC_MATERIALS),
        )
        if args.damage:  # src/dsfforza/convert/damage.py
            from dsfforza.convert import damage

            opts.damage, opts.damage_opts = True, damage.DamageOptions(hull=args.damage == "hull")
        report = forza2dsf.convert(stream, clones[0], clones[2], car, dims, opts, log=log, interior=clones[1])
        if not args.no_handling:
            report["handling"] = apply_handling(
                stream, clones[0], spool_id, car.name, gamedb, log, host=host, mode=args.handling
            )
        if not getattr(args, "no_settle", False):  # on the final VO3D
            report["placement"]["settle"] = forza2dsf.settle_wheels(stream, clones[0], log)
        forza_report = dict(forza_sound_report or {"ok": False, "reason": "disabled (--no-forza-sound)"})
        if forza_sound is not None:
            forza_report = fit_forza_sound(stream, clones[0], forza_sound, nonr_limit, mdxn_max, log)
        checks = {kind: check_entry(clones[kind], kind, nonr_limit, mdxn_max) for kind in (0, 1, 2)}
        attempts.append(
            {
                "lods": {str(m): v for m, v in lods.items()},
                "exterior_lod": exterior_lod,
                "brake_rotors": bool(report.get("brake_rotors")),
                "problems": {k: p for k, (_, p) in checks.items() if p},
            }
        )
        for kind, (sizes, problems) in checks.items():
            log(
                f"  kind {kind}: "
                + ", ".join(f"{t} {v:#x}" for t, v in sizes.items())
                + (f"  OVER: {'; '.join(problems)}" if problems else "")
            )
        over = {kind for kind, (_, problems) in checks.items() if problems}
        if not over:
            break
        if 1 in over:
            if not use_cockpit:
                raise SystemExit(f"{car.name}: the interior entry is over the limits; it is the host's own")
            log(f"{car.name}: the converted interior is over the limits; keeping the host's interior")
            use_cockpit = False
            continue
        if use_rotors and over & {0, 2} and report.get("brake_rotors"):
            # the brake rotors give way before the body's detail does
            log(f"{car.name}: over the limits; retrying without the brake rotors")
            use_rotors = False
            continue
        stepped = False
        if 0 in over and fi + 1 < len(full_ladder):
            fi, stepped = fi + 1, True
        if 2 in over and ei + 1 < len(ext_ladder):
            ei, stepped = ei + 1, True
        if not stepped:
            raise SystemExit(f"{car.name}: over the size limits at the coarsest LODs; see the log above")
        log(f"{car.name}: over the limits, retrying with coarser LODs")
    chosen = forza_report.pop("_sound", None)
    if chosen is not None:
        r = chosen.report
        log(
            f"  forza sound: template {r['template']}, bank {r['bank_id']}, SBAK {r['sbak_bytes']:#x}, "
            + ", ".join(
                f"set {k} {s['emission']} {s['bank']} {len(s['loops'])}/{s['available']} loops "
                f"pitch {r['pitch'][k]['pitch']} (loudest loop {r['pitch'][k]['pitch_main']})"
                for k, s in r["sets"].items()
            )
            + f", engine type {r['engine_type']['state']}"
        )
        out = getattr(args, "forza_sound_dir", None)
        if out:
            chosen.write(Path(out) / car.name)
            forza_report["previews"] = str(Path(out) / car.name)
    report.update(
        {
            "lods": {"full": {str(m): v for m, v in lods.items()}, "shadow": shadow, "exterior": exterior_lod},
            "attempts": attempts,
            "renewed_uid_count": renewed,
            "sound": sound_report or {"sound_host": host},
            "forza_sound": forza_report,
            "sizes": {str(kind): dict(sizes.items()) for kind, (sizes, _) in checks.items()},
        }
    )
    return clones, report


def apply_handling(
    stream, full, spool_id: int, media: str, gamedb: Path | None, log, host: int | None = None, mode: str = "dsf"
) -> dict:
    """Replace the clone's VO3D. mode "dsf" (default, src/dsfforza/build/handling_dsf.py): the host's
    VO3D changed only where Reflections' own fleet rules say the car's specs require
    it, with the steering/assist/tyre/suspension sets of the most typical of the three
    best hosts. mode "forza" (src/dsfforza/build/handling.py): mass, inertia, torque curve, gearbox,
    top speed, drive split, wheelbase and CoG from Forza; springs, dampers and tyres
    scaled from the host by mass. Runs after the hub patch, so the hierarchy already
    has the Forza wheelbase."""
    if gamedb is None:
        log(f"  handling: no gamedb.slt for {media}; keeping the host's")
        return {"source": "host"}
    nonr = child(full, "NONR")
    vo3d_entry, upvh_entry = child(nonr, "VO3D"), child(nonr, "UPVH")
    try:
        forza = handling.load_forza(media, [gamedb])
    except (SystemExit, LookupError, ValueError, sqlite3.Error) as error:
        log(f"  handling: {media} not usable in {gamedb.name} ({error}); keeping the host's")
        return {"source": "host", "error": str(error)}
    if mode == "dsf" and host is not None:
        from dsfforza.build import handling_dsf

        base = handling.VO3D.parse(forza2dsf.payload(stream, vo3d_entry))
        params, report = handling_dsf.apply(dict(base.params), host, media, [gamedb], log=log)
        vo3d_entry.payload = handling.VO3D(base.spool_id, params, base.version).build()
        return report
    host = handling.Host(
        spool_id,
        handling.VO3D.parse(forza2dsf.payload(stream, vo3d_entry)),
        *handling.hub_geometry(forza2dsf.payload(stream, upvh_entry)),
    )
    vo3d, changes = handling.build_vo3d(host, forza)
    vo3d_entry.payload = vo3d.build()
    log(
        f"  handling: {len(changes)} parameters from {gamedb.name} ({forza.mass:.0f} kg, {forza.drive}, "
        f"redline {forza.redline:.0f})"
    )
    return {"source": gamedb.name, "changed": len(changes), "mass": round(forza.mass, 1), "drive": forza.drive}


def uid_clashes(stream, entries, new_indices: set[int]) -> dict[int, list[str]]:
    """For each new VEHC, the resource and MODL part UIDs it defines that another
    VEHC defines too (both are keys of global tables in the game)."""

    def defined(e):
        return clone_vehicle.defined_uids(stream, e) + clone_vehicle.part_uids(stream, e)

    owners: dict[bytes, set[int]] = {}
    for index, e in enumerate(entries):
        if e.tag == "VEHC":
            for uid in defined(e):
                owners.setdefault(uid, set()).add(index)
    out = {}
    for index in new_indices:
        bad = sorted({u.hex() for u in defined(entries[index]) if len(owners[u]) > 1})
        if bad:
            out[index] = bad
    return out


def lua_snippet(manifest: dict) -> str:
    def q(text) -> str:
        return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = [
        f"-- Generated by tools/port_car.py for {Path(manifest['archive']).name}; do not edit.",
        "-- vehicleStats entries for the ported cars: a copy of the host's entry (badge, name",
        "-- strings, pictures, stat bars), plus the Forza names in dsfforzaName/dsfforzaMedia.",
        "-- tools/game/lua/dsfforza/clonetest.lua loads this file as dsfforza\\ported.lua.",
        "local ported = {",
        f"\tarchive = {q(Path(manifest['archive']).name)},",
        "\tids = { " + ", ".join(str(c["spool_id"]) for c in manifest["cars"]) + " },",
        "\tcars = {",
    ]
    for c in manifest["cars"]:
        make, model = make_model(c["media"])
        bars = (c.get("handling") or {}).get("bars") or {}  # handling_dsf.stat_bars: HUD bars
        extra = "".join(f", {k.lower()} = {v}" for k, v in bars.items())
        lines.append(
            f"\t\t[{c['spool_id']}] = {{ host = {c['host']}, media = {q(c['media'])}, "
            f"name = {q(c['name'])}, make = {q(make)}, model = {q(model)}, "
            f"year = {q(c.get('year') or '')}, game = {q(c['game'])}{extra} }},"
        )
    lines += [
        "\t},",
        "}",
        "",
        "-- Copy each host's vehicleStats entry to its ported ID. Returns false until",
        "-- the game's vehicleStats table exists; call again later in that case.",
        "function ported.register(log)",
        '\tif type(vehicleStats) ~= "table" then',
        "\t\treturn false",
        "\tend",
        "\tfor id, car in pairs(ported.cars) do",
        '\t\tif vehicleStats[id] == nil and type(vehicleStats[car.host]) == "table" then',
        "\t\t\tlocal entry = {}",
        "\t\t\tfor key, value in pairs(vehicleStats[car.host]) do",
        "\t\t\t\tentry[key] = value",
        "\t\t\tend",
        "\t\t\tentry.dsfforzaMedia = car.media",
        "\t\t\tentry.dsfforzaName = car.name",
        "\t\t\tif car.speed then entry.Speed = car.speed end",
        "\t\t\tif car.strength then entry.Strength = car.strength end",
        "\t\t\tif car.drift then entry.Drift = car.drift end",
        "\t\t\tvehicleStats[id] = entry",
        "\t\t\tif log then",
        '\t\t\t\tlog(string.format("vehicleStats[%d] (%s) copied from [%d]", id, car.name, car.host))',
        "\t\t\tend",
        "\t\tend",
        "\tend",
        "\treturn true",
        "end",
        "",
        "return ported",
        "",
    ]
    return "\n".join(lines)


def run_lint(out: Path, spool_id: int, dsf: Path, host: int, allow_shared: bool = False) -> tuple[bool, str]:
    """upvh.py lint: hierarchy/MODL consistency, UID shape, and (unless
    allow_shared) that the car neither shares a resource or part UID with
    another vehicle nor references one only another vehicle defines."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dsfforza.formats.upvh",
            "lint",
            str(out),
            "--id",
            str(spool_id),
            "--vanilla",
            str(dsf),
            "--source",
            str(host),
        ]
        + (["--allow-shared"] if allow_shared else []),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def host_arg(text: str) -> int | str:
    return "auto" if text.lower() == "auto" else int(text)


def pick_hosts(args, media: str, game: str, folder: Path, gamedb: Path | None, isos, log) -> tuple:
    """(host, sound host or None, report) for one car: --host/--sound-host as given,
    or src/dsfforza/build/host_select.py's best vanilla car for "auto"."""
    from dsfforza.build import host_select

    choice: dict = {"mode": "forced" if args.host != "auto" else "auto"}
    host, sound = args.host, args.sound_host
    if "auto" in (host, sound):
        db = gamedb or extract_gamedb(isos, game, args.cache)
        dbs = {game: db} if db else None
        for mode in ("host", "sound"):
            if (host if mode == "host" else sound) != "auto":
                continue
            best, ranking, info = host_select.choose(
                media, args.dsf, game, folder, dbs, allow_special=args.allow_special_hosts, mode=mode
            )
            if mode == "host":
                host = best
            else:
                sound = best
            choice[mode] = [
                {"id": r.id, "model": r.host["model"], "score": r.score, "reasons": r.reasons} for r in ranking[:3]
            ]
            log(
                f"{media}: {mode} {best} ({ranking[0].host['model']}, score {ranking[0].score:.0f}); "
                + "; ".join(ranking[0].reasons)
            )
            log(f"  {host_select.describe(info)}")
            log("  next: " + ", ".join(f"{r.id} {r.host['model']} {r.score:.0f}" for r in ranking[1:4]))
    # Is the host this very vehicle (same model or a badge twin)? The cockpit uses it.
    db = gamedb or extract_gamedb(isos, game, args.cache)
    same = host_select.same_vehicle_for(media, host, args.dsf, game, folder, {game: db} if db else None)
    if same:
        choice["same_vehicle"] = same
        log(f"{media}: host {host} is the same vehicle: {same}")
    return host, sound, choice


@dataclass
class PreparedCar:
    """One car ready to convert: extracted, loaded, measured, its hosts picked."""

    media: str
    source: Source
    folder: Path
    car: forza2dsf.ForzaCar
    dims: object
    gamedb: Path | None
    host: int
    sound: int | None
    choice: dict


def prepare_car(args, source: Source, media: str, isos, log=print) -> PreparedCar:
    """Extract the car's files (cached), load and measure it (MAXData.xml, else the
    game's gamedb.slt), get the gamedb for the cockpit eye point and the handling,
    and pick its hosts. Shared by main and every port_batch worker."""
    folder = extract_car(source, media, args.cache, log)
    # rim textures (forza_materials.WHEEL), and the wheel for cars whose main carbin has none
    extract_wheel(source, media, args.cache, log)
    extract_brakes(source, media, args.cache, log)  # brake rotors behind the spokes
    if not args.no_cockpit and extract_dash(source, media, args.cache, log):  # cockpit digital displays
        extract_dash_media(isos, source.game, args.cache)
    car = forza2dsf.load_car(folder, media)
    gamedb = None
    try:
        dims = forza2dsf.car_dims(car)
    except ValueError:  # no MAXData.xml: fall back to the game's gamedb.slt
        gamedb = extract_gamedb(isos, source.game, args.cache)
        dims = forza2dsf.car_dims(car, gamedb)
    if (not args.no_cockpit or not args.no_handling) and gamedb is None:  # eye point, handling
        gamedb = extract_gamedb(isos, source.game, args.cache)
    host, sound, choice = pick_hosts(args, media, source.game, folder, gamedb, isos, log)
    return PreparedCar(media, source, folder, car, dims, gamedb, host, sound, choice)


def convert_car(
    stream,
    entries,
    prep: PreparedCar,
    spool_id: int,
    args,
    nonr_limit,
    mdxn_max,
    names: dict[int, str],
    isos,
    log=print,
) -> tuple[list, dict]:
    """The Forza sound plan and build_car for one prepared car under spool_id.
    Returns the three VEHC entries (not yet registered) and the report. Shared by
    main and every port_batch worker."""
    host, sound = prep.host, prep.sound
    log(
        f"--- {prep.media} ({prep.source.game}, {prep.source.where}) -> spool ID {spool_id}, host {host} "
        f"({names[host]})" + (f", sound from {sound} ({names[sound]})" if sound not in (None, host) else "")
    )
    plan, plan_report = forza_sound_plan(
        args,
        stream,
        entries,
        prep.media,
        prep.source.game,
        prep.folder,
        prep.gamedb,
        isos,
        args.sound_host if isinstance(args.sound_host, int) else None,
        spool_id,
        log,
    )
    clones, report = build_car(
        stream,
        entries,
        host,
        spool_id,
        prep.car,
        prep.dims,
        args,
        nonr_limit,
        mdxn_max,
        log,
        prep.gamedb,
        sound,
        plan,
        plan_report,
        same_vehicle=prep.choice.get("same_vehicle"),
    )
    report["host_choice"] = prep.choice
    return clones, report


def build_parser() -> argparse.ArgumentParser:
    """port_car's options (port_batch parses its --port options with it too)."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Details: this file's docstring and docs/DEVELOPMENT.md.",
    )
    parser.add_argument("--dsf", type=Path, required=True, help="vanilla Install/dngvehicles.sp (only read)")
    parser.add_argument("--out", type=Path, required=True, help="archive to write")
    parser.add_argument(
        "--car",
        action="append",
        required=True,
        metavar="NAME[@fh1|@fm4]",
        help="Forza media name, e.g. NIS_370Z_10; repeat for several cars",
    )
    parser.add_argument(
        "--host",
        type=host_arg,
        default="auto",
        help="vanilla vehicle to build on: auto (default: the most similar vanilla car per Forza "
        "car, src/dsfforza/build/host_select.py) or a base spool ID (266 = ASYM Desanne)",
    )
    parser.add_argument(
        "--sound-host",
        type=host_arg,
        help="take the engine sound (NONR/VSRT) from this vanilla car instead of the host: "
        "a base spool ID, or auto (best engine match); default: the host's (untested in game)",
    )
    parser.add_argument(
        "--no-forza-sound",
        action="store_true",
        help="keep the host's (or --sound-host's) engine sound; default: the car's own Forza "
        "engine loops in a vanilla template bank (src/dsfforza/convert/engine_sound.py; untested in game)",
    )
    parser.add_argument(
        "--forza-sound-dir",
        type=Path,
        help="where each car's sound previews go (<dir>/<media>/sweep_new.wav ...; default "
        "<out>-sound next to the archive)",
    )
    parser.add_argument(
        "--allow-special-hosts",
        action="store_true",
        help="with --host auto: also consider cop, taxi, emergency and other special entries",
    )
    parser.add_argument("--first-id", type=int, default=304, help="first base spool ID to try (default 304)")
    parser.add_argument(
        "--iso", type=Path, action="append", help="Forza disc images, any file names (default: assets/*.iso)"
    )
    parser.add_argument("--cache", type=Path, default=REPO / "out" / "cache", help="extraction cache")
    parser.add_argument(
        "--root-limit",
        type=int,
        default=ENTRY_LIMIT,
        help=f"root entries allowed in the output archive (default {ENTRY_LIMIT}, the vanilla "
        "reader); more needs dsfforza.asi with [archive] enabled",
    )
    parser.add_argument(
        "--mdxn-limit",
        type=lambda text: int(text, 0),
        help="allow MDXN up to this size in every entry instead of the vanilla maximum "
        "of its kind, e.g. 0x2C0000 (bigger VEHCs load in game: spool-limits.md)",
    )
    parser.add_argument(
        "--lods",
        help="full entry DSF mask:Forza LOD pairs; disables the LOD fallback "
        f"(default: try {' then '.join(FULL_LADDER)})",
    )
    parser.add_argument(
        "--exterior-lod",
        type=int,
        help="Forza LOD for the clean exterior; disables the fallback (default: 1, then 2, 3, 4)",
    )
    parser.add_argument(
        "--textures",
        choices=("replace", "none"),
        default="replace",
        help="replace: the car's atlases in the host's chassis texture (default)",
    )
    parser.add_argument(
        "--share-host-uids",
        action="store_true",
        help="keep the host's resource UIDs (the older single-car layout; textured "
        "cars then clash with the host and each other when loaded together)",
    )
    parser.add_argument("--vehc-name", help="VEHC display class for the clones (default: the media name)")
    parser.add_argument("--manifest", type=Path, help="default: <out>.json")
    parser.add_argument("--lua", type=Path, help="default: <out>.lua")
    parser.add_argument(
        "--install-lua",
        action="store_true",
        help="also write the Lua to tools/game/lua/dsfforza/ported.lua for the test harness",
    )
    parser.add_argument("--no-lint", action="store_true")
    parser.add_argument(
        "--damage",
        nargs="?",
        const="hull",
        choices=("panels", "hull"),
        help="vanilla-like damage (src/dsfforza/convert/damage.py): body on the host's panels, crush "
        "targets, panel parts, hinges moved, and (default, 'hull') the root collision hull mapped "
        "onto the Forza body; 'panels' keeps the host's hull",
    )
    parser.add_argument(
        "--glass-links",
        action="store_true",
        help="bind windscreen/rear-window glass to the host's glass-panel records, like vanilla "
        "glass (default: the chassis; untested in game)",
    )
    parser.add_argument(
        "--no-plates",
        action="store_true",
        help="no licence plates (default: the vanilla DSF plate on the Forza car's plate "
        "recess at each end the host has one, src/dsfforza/convert/plates.py; untested in game)",
    )
    parser.add_argument(
        "--no-exhaust",
        action="store_true",
        help="keep the host's exhaust markers (default: moved onto the Forza tailpipes, "
        "src/dsfforza/convert/exhaust.py)",
    )
    parser.add_argument(
        "--handling",
        choices=("dsf", "forza"),
        default="dsf",
        help="dsf (default): the host's VO3D moved by Reflections' fleet rules "
        "(src/dsfforza/build/handling_dsf.py); forza: Forza-derived values (src/dsfforza/build/handling.py)",
    )
    parser.add_argument(
        "--no-handling",
        action="store_true",
        help="keep the host's VO3D handling (default: build it from the Forza "
        "gamedb.slt, src/dsfforza/build/handling.py; untested in game)",
    )
    parser.add_argument(
        "--no-cockpit",
        action="store_true",
        help="keep the host's interior entry, steering hinge and head camera "
        "(default: rebuild them from <car>_cockpit.carbin, src/dsfforza/convert/cockpit.py)",
    )
    parser.add_argument(
        "--no-settle",
        action="store_true",
        help="leave the hubs on Forza's static pose (default: lower them by the wheel offset the "
        "engine draws at rest, forza2dsf.settle_wheels, research/wheels.md 'Wheel clearance')",
    )
    parser.add_argument(
        "--no-rear-wheel-part",
        action="store_true",
        help="on a host with one wheel part for both axles, draw the rear wheels at the front "
        "tyre's size (default: a rear copy of the part when the tyres differ, "
        "forza2dsf.split_rear_wheel, research/wheels.md 'Rear wheel parts')",
    )
    parser.add_argument(
        "--no-wheel-blur",
        action="store_true",
        help="no speed-blur disc on the wheels (default: one that fades in as they spin, "
        "forza2dsf.wheel_blur_section, research/wheels.md 'Speed blur')",
    )
    parser.add_argument(
        "--no-brake-rotors",
        action="store_true",
        help="no brake rotors behind the spokes (default: Forza's front and rear rotor in the "
        "wheel parts, forza2dsf.brake_rotor_section, research/wheels.md 'Brake rotors')",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    gentle.apply()  # out/gentle: Idle priority (src/dsfforza/build/gentle.py)
    args = build_parser().parse_args(argv)
    args.dsf = game_paths.vanilla(args.dsf)  # the original when a dsfforza install replaced it

    if args.out.resolve() == args.dsf.resolve():
        raise SystemExit("refusing to overwrite the vanilla archive")
    if args.forza_sound_dir is None:
        args.forza_sound_dir = args.out.with_name(args.out.stem + "-sound")
    if args.share_host_uids and len(args.car) > 1 and args.textures != "none":
        print("warning: --share-host-uids with several textured cars: they share one texture UID", file=sys.stderr)
    requests = [parse_car(c) for c in args.car]
    if len({r.media.lower() for r in requests}) != len(requests):
        raise SystemExit("the same car is listed twice")
    isos = args.iso or default_images()
    log = print

    cars, _ = roster(isos)
    picked = []
    for request in requests:
        rcar, source = pick_source(cars, request)
        picked.append((rcar, prepare_car(args, source, rcar.name, isos, log)))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_name(args.out.name + ".tmp")
    manifest = {
        "archive": str(args.out),
        "vanilla": str(args.dsf),
        "host": args.host,
        "sound_host": args.sound_host,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "textures": args.textures,
        "renewed_uids": not args.share_host_uids,
        "cockpit": not args.no_cockpit,
        "cars": [],
    }
    with args.dsf.open("rb") as stream:
        root = read_chunk(stream, 0)
        entries = root.entries
        nonr_limit, mdxn_max = vanilla_limits(stream, entries, args.mdxn_limit)
        manifest["limits"] = {
            "nonr": {str(k): v for k, v in nonr_limit.items()},
            "mdxn": {str(k): v for k, v in mdxn_max.items()},
        }
        names = {s: entries[i].name for s, i, k in clone_vehicle.sslp_records(stream, entries) if k == 0}
        taken = clone_vehicle.taken_ids(stream, entries)
        next_id = args.first_id
        built = []
        for rcar, prep in picked:
            while next_id in taken:
                next_id += 1
            if next_id >= 1000:
                raise SystemExit("no free base spool ID below 1000")
            spool_id = next_id
            taken.add(spool_id)
            clones, report = convert_car(stream, entries, prep, spool_id, args, nonr_limit, mdxn_max, names, isos, log)
            built.append((rcar, prep, spool_id, clones, report))
        for rcar, prep, spool_id, clones, report in built:
            clone_vehicle.register_clones(stream, entries, clones, prep.host, spool_id)
            manifest["cars"].append(
                car_entry(
                    rcar.name,
                    rcar.year,
                    rcar.games,
                    prep.source,
                    args.cache,
                    spool_id,
                    prep.host,
                    names[prep.host],
                    report,
                )
            )
        # Root indices shift as clones are inserted; look them up once all are in.
        records = {s: i for s, i, _ in clone_vehicle.sslp_records(stream, entries)}
        for c in manifest["cars"]:
            c["vehc"] = [records[c["spool_id"] + k] for k in clone_vehicle.KIND_OFFSETS]
        root_limit = getattr(args, "root_limit", None) or ENTRY_LIMIT
        if len(entries) > root_limit:
            raise SystemExit(
                f"{len(entries)} root entries (> {root_limit}; --root-limit, needs dsfforza.asi "
                f"[archive] above {ENTRY_LIMIT})"
            )
        clashes = uid_clashes(stream, entries, {i for c in manifest["cars"] for i in c["vehc"]})
        if clashes and not args.share_host_uids:
            raise SystemExit(f"resource UID clashes: {clashes}")
        try:
            with temp.open("wb") as out:
                write_chunk(out, stream, entries, root.version)
        except BaseException:
            temp.unlink(missing_ok=True)  # no partial archive copy left behind
            raise
    Path(temp).replace(args.out)
    log(f"wrote {args.out} ({args.out.stat().st_size:#x} bytes), {len(manifest['cars'])} cars")

    failed = 0
    for c in manifest["cars"]:
        if args.no_lint:
            break
        ok, text = run_lint(args.out, c["spool_id"], args.dsf, c["host"], args.share_host_uids)
        c["lint"] = "ok" if ok else text
        failed += not ok
        log(f"lint {c['spool_id']} {c['media']}: {'ok' if ok else 'FAILED'}")
        if not ok:
            log(text)
    manifest["probe_ids"] = [c["spool_id"] for c in manifest["cars"]]

    manifest_path = args.manifest or args.out.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    lua = lua_snippet(manifest)
    lua_path = args.lua or args.out.with_suffix(".lua")
    lua_path.write_text(lua, encoding="utf-8", newline="\n")
    if args.install_lua:
        (REPO / "tools" / "game" / "lua" / "dsfforza" / "ported.lua").write_text(lua, encoding="utf-8", newline="\n")
    log(f"wrote {manifest_path} and {lua_path}")
    for c in manifest["cars"]:
        log(
            f"  {c['spool_id']}  {c['media']:28} {c['game']}  host {c['host']}  full LOD {c['lods']['full']} "
            f"exterior LOD {c['lods']['exterior']}  placement {c['placement']['source']}"
            + (f"  cockpit eye {c['cockpit']['eye']}" if "cockpit" in c else "  host interior")
            + ("  forza sound" if c.get("forza_sound", {}).get("ok") else "  host sound")
        )
    if failed:
        raise SystemExit(f"{failed} car(s) failed lint")


if __name__ == "__main__":
    sys.exit(main())
