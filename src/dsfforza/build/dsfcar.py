"""The distributed car package, format 1: one zip per car (research/car-packages.md,
"Package format").

    <key>.dsfcar (zip, members at its root, nothing else)
      car.json      the manifest (below)
      car.sp        the installed form dsfforza.asi [archive] packages reads: a CHNK v3, not
                    compressed, of four root entries, the car's three VEHC (kinds 0, 1, 2: full
                    car, interior, clean exterior) and a VSCT of its three 28-byte records
      picture.dds   the garage picture: 512 x 256 DXT5, no mips (as gui.sp's car pictures)
      badge.png     the maker badge as the HUD and the maker list draw it (RGBA)
      plate.png     the model nameplate as the HUD draws it (RGBA)
      emblem.dds    the garage card emblem of the car's source (whatever the source is: the
                    format names no game): 512 x 256 DXT5, no mips, laid out like the card
                    icon atlas (this pipeline writes the Forza logo, gui_assets.emblem_texture)

The manifest says what the car is and nothing the manager allocates: no model ID the game
will use, no glyph code, string ID, picture name or garage group. The model ID inside
car.sp is whatever the builder had (`car.model_id` says which); the manager installs car.sp
as it is and dsfforza.asi writes the listed ID into the data it serves. Images are optional: without one the manager leaves
that part as the host's (picture) or the game's own for the maker or model.

    {
      "format": "dsfforza-car", "format_version": 1,
      "key": "forza.fh1.BMW_M3E30_91",          # <source>.<game>.<media>, stable across versions
      "version": 3,                              # this package's version (higher replaces lower)
      "car": {"file": "car.sp", "sha256": "...", "model_id": 1, "host": 205, "sound_host": 205},
      "names": {"display": "BMW M3E30 (1991)", "make": "BMW", "model": "M3",
                "family": null, "year": 1991, "maker_group": "BMW"},
      "garage": {"speed": 0.48, "strength": 0.3, "drift": 0.65},
      "images": {"picture": "picture.dds", "badge": "badge.png", "plate": "plate.png",
                 "emblem": "emblem.dds"},
      "requires": ["archive.packages", "archive.ids"],
      "source": {"pipeline": "dsfforza", "game": "FH1", "media": "BMW_M3E30_91"}
    }

    uv run dsff car_package pack --build out/build --media BMW_M3E30_91 --out out/packages
    uv run dsff car_package validate out/packages/forza.fh1.BMW_M3E30_91.dsfcar
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import zipfile
from pathlib import Path

from dsfforza.formats.chnk import read_chunk
from dsfforza.formats.vehicles import VSCT_RECORD

FORMAT = "dsfforza-car"
FORMAT_VERSION = 1
MANIFEST = "car.json"
CAR = "car.sp"
PICTURE = "picture.dds"
BADGE = "badge.png"
PLATE = "plate.png"
# The garage card emblem texture, as gui_assets.build puts it in Fonts.fchunk
# (research/gui-assets.md, "Garage card emblem"): each package carries its source's emblem, so
# a package is self-contained. The format is source-neutral; this pipeline writes the discs'
# UI4/Common/forza_logo3 for every car.
EMBLEM = "emblem.dds"
MEMBERS = {MANIFEST, CAR, PICTURE, BADGE, PLATE, EMBLEM}
IMAGE_MEMBERS = {"picture": PICTURE, "badge": BADGE, "plate": PLATE, "emblem": EMBLEM}
SUFFIX = ".dsfcar"

# Capabilities a package can ask of dsfforza.asi; the plugin lists its own in its DLL
# (tools/asi/src/patch.h DSF_CAPABILITIES; car_manager.plugin_capabilities reads them).
# "archive.packages": [archive] packages, the loader of car.sp (research/car-packages.md).
# "archive.ids": the plugin writes the listed model ID into car.sp's data as it serves it, so
# car.sp holds its builder's ID ("Model ID in memory"). Packages from this pipeline need both.
CAPABILITIES = {"archive.packages", "archive.ids"}
PIPELINE_REQUIRES = ["archive.packages", "archive.ids"]

KEY = re.compile(r"^[a-z0-9]+\.[a-z0-9]+\.[A-Za-z0-9_\-]+$")
TOP_KEYS = {"format", "format_version", "key", "version", "car", "names", "garage", "images", "requires", "source"}
CAR_KEYS = {"file", "sha256", "model_id", "host", "sound_host"}
NAME_KEYS = {"display", "make", "model", "family", "year", "maker_group"}
GARAGE_KEYS = {"speed", "strength", "drift"}  # vehicleStats Speed/Strength/Drift: the garage stat bars, 0..1

# Limits from the game's data and the plugin:
VANILLA_IDS = 304  # vehicleStats IDs 0..303 are the game's (dsfforza.asi [garage]: added cars start at 304)
MAX_BASE_ID = 999  # FUN_005A7FC0 requests the interior and exterior as id + 1000 and id + 2000 (short)
# The slot buffer each kind's NONR is copied into: the vanilla dngvehicles.sp SSLP header
# (+0x08 + 4 * kind); dsfforza.asi leaves out a package whose NONR is larger (h_sslp_load).
NONR_LIMITS = (0x6D000, 0x1000, 0x1000)
VSCT_SIZE = 3 * VSCT_RECORD.size  # three 28-byte records


def _gui():
    from dsfforza.game import gui_assets

    return gui_assets


def image_limits() -> dict[str, tuple[int, int]]:
    """Largest width x height of each glyph image: what gui_assets fits them to. The
    vehicles_font cell is CELL wide; a badge is a logo fitted to BADGE_SIZE square or a text
    badge of CELL - 1 x TEXT_BADGE_H (badge_glyph); a plate is nameplate()'s 98 x 34."""
    g = _gui()
    return {BADGE: (g.CELL - 1, max(g.BADGE_SIZE, g.TEXT_BADGE_H)), PLATE: (98, 34)}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- reading the parts -----------------------------------------------------------------


def png_size(data: bytes) -> tuple[int, int, int, int]:
    """(width, height, bit depth, colour type) from a PNG's IHDR."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    w, h, depth, colour = struct.unpack(">IIBB", data[16:26])
    return w, h, depth, colour


def dds_info(data: bytes) -> tuple[int, int, bytes, int]:
    """(width, height, FourCC, mip count) from a DDS header."""
    if data[:4] != b"DDS " or len(data) < 128:
        raise ValueError("not a DDS")
    h, w = struct.unpack_from("<II", data, 12)
    mips = struct.unpack_from("<I", data, 28)[0]
    return w, h, data[84:88], mips


def _vsct_problems(data: bytes, vsct, host: int | None) -> list[str]:
    if vsct.size != VSCT_SIZE:
        return [f"{CAR}: VSCT is {vsct.size} bytes, not {VSCT_SIZE} (three records)"]
    if host is None:
        return []
    ids = {struct.unpack_from("<H", data, vsct.offset + k * VSCT_RECORD.size)[0] for k in range(3)}
    return [] if ids == {host} else [f"{CAR}: VSCT records are for {sorted(ids)}, not the host {host}"]


def _vehc_problems(k: int, e) -> list[str]:
    """A VEHC is a CHNK whose entries lie inside it; its NONR fits kind k's slot buffer."""
    if not e.children:
        return [f"{CAR}: VEHC {k} is not a CHNK"]
    out = []
    for kid in e.children:
        if kid.offset + kid.size > e.offset + e.size:  # offsets are from the file's start
            out.append(f"{CAR}: VEHC {k}: {kid.tag} ends past the VEHC")
        if kid.tag == "NONR" and kid.size > NONR_LIMITS[k]:
            out.append(f"{CAR}: VEHC {k}: NONR {kid.size:#x} is larger than the slot buffer {NONR_LIMITS[k]:#x}")
    return out


def _model_id_problems(stream, vehc, model_id: int | None) -> list[str]:
    """The full VEHC holds model_id once in each of clone_vehicle.ID_FIELDS (VSVH, VO3D, SPID,
    UPVH), as a vanilla car's kind 0 entry does: dsfforza.asi writes the listed ID into
    exactly these fields."""
    from dsfforza.build.car_package import id_fields
    from dsfforza.build.clone_vehicle import ID_FIELDS

    if model_id is None or not vehc.children:
        return []
    found = id_fields(stream, vehc)
    tags = sorted(tag for tag, _ in found)
    out = []
    if tags != sorted(ID_FIELDS):
        out.append(f"{CAR}: the full VEHC's model-ID fields are {tags}, not {sorted(ID_FIELDS)} once each")
    out += [f"{CAR}: {tag} holds model ID {v}, the manifest says {model_id}" for tag, v in found if v != model_id]
    return out


def car_sp_problems(data: bytes, model_id: int | None = None, host: int | None = None) -> list[str]:
    """What is wrong with car.sp for dsfforza.asi's loader (pkg_open's checks, plus the
    model ID and the VSCT records' host)."""
    stream = io.BytesIO(data)
    try:
        root = read_chunk(stream, 0)
    except (ValueError, struct.error) as error:
        return [f"{CAR}: not a CHNK ({error})"]
    out = []
    if root.compressed:
        out.append(f"{CAR}: compressed (the loader reads it as stored)")
    if root.version & 0x7FFFFFFF != 3:
        out.append(f"{CAR}: CHNK version {root.version & 0x7FFFFFFF}, not 3")
    tags = [e.tag for e in root.entries]
    if tags != ["VEHC", "VEHC", "VEHC", "VSCT"]:
        return [*out, f"{CAR}: entries {tags}, not VEHC, VEHC, VEHC, VSCT"]
    out += [f"{CAR}: entry {i} ends past the file" for i, e in enumerate(root.entries) if e.offset + e.size > len(data)]
    out += _vsct_problems(data, root.entries[3], host)
    for k, e in enumerate(root.entries[:3]):
        out += _vehc_problems(k, e)
    return out + _model_id_problems(stream, root.entries[0], model_id)


# --- the manifest ------------------------------------------------------------------------


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _unknown(where: str, obj: dict, known: set) -> list[str]:
    extra = set(obj) - known
    return (
        [f"{where}: unknown fields {sorted(extra)} (a package carries nothing the manager allocates)"] if extra else []
    )


def _head_problems(m: dict) -> list[str]:
    out = _unknown("manifest", m, TOP_KEYS)
    if m.get("format") != FORMAT:
        out.append(f"format is {m.get('format')!r}, not {FORMAT!r}")
    if m.get("format_version") != FORMAT_VERSION:
        out.append(f"format_version {m.get('format_version')!r}: this reader knows {FORMAT_VERSION}")
    if not isinstance(m.get("key"), str) or not KEY.match(m["key"]):
        out.append(f"key {m.get('key')!r} is not <source>.<game>.<name> (lower-case source and game)")
    if not _is_int(m.get("version")) or m["version"] < 1:
        out.append("version must be a whole number from 1")
    req = m.get("requires")
    if not isinstance(req, list) or "archive.packages" not in req or set(req) - CAPABILITIES:
        out.append(f"requires must list archive.packages (known: {sorted(CAPABILITIES)})")
    if "source" in m and not isinstance(m["source"], dict):
        out.append("source must be an object")
    return out


def _car_problems(car) -> list[str]:
    if not isinstance(car, dict):
        return ["car is missing"]
    out = _unknown("car", car, CAR_KEYS)
    if car.get("file") != CAR:
        out.append(f"car.file must be {CAR!r}")
    if not isinstance(car.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", car["sha256"]):
        out.append("car.sha256 must be the file's SHA-256 (64 lower-case hex digits)")
    if not _is_int(car.get("model_id")) or not 1 <= car["model_id"] <= MAX_BASE_ID:
        out.append(f"car.model_id must be 1..{MAX_BASE_ID} (the ID car.sp holds)")
    if "host" not in car:
        out.append("car.host is missing (the vanilla car whose VSCT records and vehicleStats it copies)")
    for field in ("host", "sound_host"):
        if field in car and (not _is_int(car[field]) or not 0 <= car[field] < VANILLA_IDS):
            out.append(f"car.{field} must be a vanilla model ID (0..{VANILLA_IDS - 1})")
    return out


def _names_problems(names) -> list[str]:
    if not isinstance(names, dict):
        return ["names is missing"]
    out = _unknown("names", names, NAME_KEYS)
    for field in ("display", "make", "model", "maker_group"):
        if not isinstance(names.get(field), str) or not names[field].strip():
            out.append(f"names.{field} must be a non-empty string")
    if names.get("family") is not None and not isinstance(names["family"], str):
        out.append("names.family must be a string or null")
    if names.get("year") is not None and not _is_int(names["year"]):
        out.append("names.year must be a whole number or null")
    return out


def _garage_images_problems(m: dict) -> list[str]:
    out = []
    garage = m.get("garage", {})
    if not isinstance(garage, dict) or set(garage) - GARAGE_KEYS:
        out.append(f"garage holds only {sorted(GARAGE_KEYS)}")
    else:
        bad = [
            k for k, v in garage.items() if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1
        ]
        out += [f"garage.{k} must be a number from 0 to 1" for k in bad]
    images = m.get("images", {})
    want = IMAGE_MEMBERS
    if not isinstance(images, dict) or any(images.get(k) != want.get(k) for k in images):
        out.append(f"images maps picture/badge/plate/emblem to {sorted(want.values())}")
    return out


def manifest_problems(m) -> list[str]:
    """What is wrong with a manifest (its own fields; the files are checked in validate)."""
    if not isinstance(m, dict):
        return ["the manifest is not a JSON object"]
    return (
        _head_problems(m) + _car_problems(m.get("car")) + _names_problems(m.get("names")) + _garage_images_problems(m)
    )


# --- writing and checking a package --------------------------------------------------------


def write(out: Path, manifest: dict, car_sp: bytes, images: dict[str, bytes]) -> Path:
    """Write a package: the manifest (car.sha256 set from car_sp, images from what is given),
    car.sp and the images ({member name: bytes}). Stored, not deflated: car.sp is mostly
    compressed textures and the picture is DXT5. Returns the path."""
    manifest = json.loads(json.dumps(manifest))
    manifest["car"]["file"] = CAR
    manifest["car"]["sha256"] = sha256(car_sp)
    manifest["images"] = {k: v for k, v in IMAGE_MEMBERS.items() if v in images}
    problems = manifest_problems(manifest)
    if problems:
        raise ValueError("; ".join(problems))
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_STORED) as z:
        z.writestr(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        z.writestr(CAR, car_sp)
        for name in sorted(images):
            z.writestr(name, images[name])
    temp.replace(out)
    return out


def _glyph_problems(member: str, data: bytes) -> list[str]:
    try:
        w, h, depth, colour = png_size(data)
    except (ValueError, struct.error) as error:
        return [f"{member}: {error}"]
    out = []
    if depth != 8 or colour != 6:
        out.append(f"{member}: 8-bit RGBA expected (depth {depth}, colour type {colour})")
    mw, mh = image_limits()[member]
    if not (0 < w <= mw and 0 < h <= mh):
        out.append(f"{member}: {w} x {h}, larger than the glyph's {mw} x {mh}")
    return out


def _picture_problems(data: bytes) -> list[str]:
    try:
        w, h, fourcc, _ = dds_info(data)
    except (ValueError, struct.error) as error:
        return [f"{PICTURE}: {error}"]
    if (w, h) != _gui().PICTURE_SIZE or fourcc != b"DXT5":
        return [f"{PICTURE}: {w} x {h} {fourcc!r}, not 512 x 256 DXT5 (gui.sp's car pictures)"]
    return []


def emblem_problems(data: bytes) -> list[str]:
    """A package's emblem texture: what dsfforza.asi [hud] card_emblem binds in place of the card
    icon atlas, so its size (gui_assets.CARD_ATLAS_SIZE): 512 x 256 DXT5, one level."""
    try:
        w, h, fourcc, mips = dds_info(data)
    except (ValueError, struct.error) as error:
        return [f"{EMBLEM}: {error}"]
    size = _gui().CARD_ATLAS_SIZE
    if (w, h) != size or fourcc != b"DXT5" or mips > 1 or len(data) != 128 + w * h:
        return [f"{EMBLEM}: {w} x {h} {fourcc!r}, {len(data)} bytes, not one level of 512 x 256 DXT5 (the card atlas)"]
    return []


def _member_problems(z: zipfile.ZipFile, names: list[str], manifest: dict) -> list[str]:
    """car.sp against the manifest, and each image the manifest names."""
    out = []
    car = manifest.get("car")
    data = z.read(CAR)
    if isinstance(car, dict):
        if car.get("sha256") != sha256(data):
            out.append(f"{CAR}: SHA-256 differs from the manifest's")
        mid = car.get("model_id") if _is_int(car.get("model_id")) else None
        host = car.get("host") if _is_int(car.get("host")) else None
        out += car_sp_problems(data, mid, host)
    images = manifest.get("images", {})
    listed = set(images.values()) if isinstance(images, dict) else set()
    out += [f"images names {m}, which the zip doesn't hold" for m in sorted(listed) if m not in names]
    out += [f"{m} is in the zip but not in images" for m in IMAGE_MEMBERS.values() if m in names and m not in listed]
    for member in (BADGE, PLATE):
        if member in names:
            out += _glyph_problems(member, z.read(member))
    if PICTURE in names:
        out += _picture_problems(z.read(PICTURE))
    if EMBLEM in names:
        out += emblem_problems(z.read(EMBLEM))
    return out


def validate(path: Path) -> list[str]:
    """Everything wrong with a package; empty when the manager may install it."""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        return [f"not a zip ({error})"]
    with z:
        names = z.namelist()
        out = [f"unexpected member {n!r}" for n in names if n not in MEMBERS]
        if len(set(names)) != len(names):
            out.append("a member is stored twice")
        if MANIFEST not in names or CAR not in names:
            return [*out, f"{MANIFEST} and {CAR} are required"]
        try:
            manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return [*out, f"{MANIFEST}: {error}"]
        out += manifest_problems(manifest)
        if isinstance(manifest, dict):
            out += _member_problems(z, names, manifest)
    return out


def requires_of(path: Path) -> set[str]:
    """The capabilities a package's manifest asks for, read without checking the rest (a
    first look before anything is installed; read() checks the whole package). Empty when
    the manifest can't be read."""
    try:
        with zipfile.ZipFile(path) as z:
            req = json.loads(z.read(MANIFEST).decode("utf-8")).get("requires")
    except (OSError, KeyError, ValueError, AttributeError, zipfile.BadZipFile):
        return set()
    return {r for r in req if isinstance(r, str)} if isinstance(req, list) else set()


def read(path: Path) -> tuple[dict, bytes, dict[str, bytes]]:
    """(manifest, car.sp, {image member: bytes}) of a valid package; ValueError otherwise."""
    problems = validate(path)
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
        images = {m: z.read(m) for m in manifest.get("images", {}).values()}
        return manifest, z.read(CAR), images


# --- from a port_batch build ------------------------------------------------------------------


def key_of(game: str, media: str) -> str:
    return f"forza.{game.lower()}.{media}"


def _png_bytes(image) -> bytes:
    from dsfforza.formats import xds

    return xds.png_bytes(image.w, image.h, bytearray(image.px))


def _build_images(build: Path) -> dict:
    """The logos and plates the build's pack step kept (pack/pack.json "images")."""
    from dsfforza.build import pack_reuse

    gui = _gui()
    record = build / pack_reuse.FOLDER / pack_reuse.RECORD
    if not record.exists():
        return {}
    out = {}
    for key, name in json.loads(record.read_text()).get("images", {}).items():
        path = build / pack_reuse.FOLDER / pack_reuse.IMAGES / name
        try:
            out[key] = pack_reuse.unpack_image(gui.Image, path.read_bytes())
        except (OSError, ValueError):
            continue
    return out


def pack(build: Path, media: list[str], out_dir: Path, version: int = 1, isos=None, log=print) -> list[Path]:
    """One package per car of a port_batch build (its manifest dngvehicles.json, work/<media>.chnk,
    pack/pictures and pack/images): car.sp with the build's model ID, the garage picture,
    the badge (the Forza logo, else a text badge, as gui_assets draws them), the plate and the
    garage card emblem (from the discs, the same for every car of this pipeline)."""
    from dsfforza.build import car_package

    gui = _gui()
    data = json.loads((build / "dngvehicles.json").read_text())
    cars = {c["media"]: c for c in data["cars"]}
    missing = [m for m in media if m not in cars]
    if missing:
        raise ValueError(f"not in {build / 'dngvehicles.json'}: {', '.join(missing)}")
    chosen = [cars[m] for m in media]
    texts = gui.car_texts(chosen, isos)
    cached = _build_images(build)
    for key, image in cached.items():  # as gui_assets.build seeds them from pack_reuse
        kind, *rest = json.loads(key)
        if kind == "plate":
            gui._TEXT_PLATES[tuple(rest)] = image
    ui = None

    def logo(make: str):
        nonlocal ui
        k = json.dumps(["logo", make])
        if k in cached:
            return cached[k]
        if ui is None:
            ui = gui.ForzaUI.of(isos)
        return gui.logo_image(ui, make.title()) or gui.logo_image(ui, make)

    vanilla = Path(data.get("vanilla") or "out/vanilla/Install/dngvehicles.sp")
    emblem = gui.emblem_texture(isos)
    problems = emblem_problems(emblem)
    if problems:
        raise ValueError("; ".join(problems))
    written = []
    for car in chosen:
        t = texts[car["media"]]
        group = gui.dsf_make(t["make"], t["model"])
        art = logo(group) or logo(t["make"]) or gui.maker_badge(group)
        images = {BADGE: _png_bytes(gui.badge_glyph(art)), PLATE: _png_bytes(gui.nameplate(t["model"]))}
        picture = build / "pack" / "pictures" / f"dsfforza_{car['media'].lower()}.dds"
        if picture.exists():
            images[PICTURE] = picture.read_bytes()
        images[EMBLEM] = emblem
        temp = out_dir / f".{car['media']}.sp"
        model_id = car["spool_id"]
        car_package.make(build / "work" / f"{car['media']}.chnk", None, car["host"], vanilla, temp)
        car_sp = temp.read_bytes()
        temp.unlink()
        bars = (car.get("handling") or {}).get("bars") or {}
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "key": key_of(car["game"], car["media"]),
            "version": version,
            "car": {"file": CAR, "sha256": "", "model_id": model_id, "host": car["host"]},
            "names": {
                "display": car["name"],
                "make": t["make"],
                "model": t["model"],
                "family": t["family"],
                "year": car.get("year") if _is_int(car.get("year")) else t.get("year"),
                "maker_group": group,
            },
            "garage": {k.lower(): v for k, v in bars.items() if k.lower() in GARAGE_KEYS},
            "requires": list(PIPELINE_REQUIRES),
            "source": {"pipeline": "dsfforza", "game": car["game"], "media": car["media"]},
        }
        sound_host = (car.get("sound") or {}).get("sound_host")
        if _is_int(sound_host):
            manifest["car"]["sound_host"] = sound_host
        path = write(out_dir / f"{manifest['key']}{SUFFIX}", manifest, car_sp, images)
        log(f"{path}: {len(car_sp) >> 10} KiB car.sp, {', '.join(sorted(images))}")
        written.append(path)
    return written
