"""Give a ported car its own Forza engine sound (media swap into a stock Wwise bank).

DSF drives a vehicle's engine with two sets of rpm-layered loops in its
Wwise bank (research/engine-sound.md): set A is played by the event at
VSVH+04 on emitter A, set B by VSVH+0C on emitter B. Each loop is a Sound
with a pitch curve 1200*log2(rpm / recording rpm) on vehicle_rpm and a
layer crossfade on vehicle_rpm. The game maps the car's normalised rpm
(VO3D p40..p42) onto VSVH+94..+98, the bank's rpm range.

The conversion keeps a stock template bank's hierarchy (events, switches,
layers, load and speed RTPCs) and replaces the loops of both sets:

1. Forza: <car>_ET.xml gives idle/redline and the HT file per emission;
   the HT file gives the FSB bank and every loop's rpm (src/dsfforza/formats/forza_audio.py).
   One emission per set (A = engine ambience, since emitter A sits at the
   engine; B = exhaust, emitter B at the exhaust marker); as many loops as
   the set has slots are picked, spread over idle..redline.
2. Each loop is cut to at most --max-seconds with a crossfaded seam, resampled
   (periodic FFT resample) to 32 kHz and a whole number of 64-sample ADPCM
   blocks, scaled to the template set's loudness and encoded to Wwise IMA ADPCM.
3. The pitch curves are recomputed for the Forza recording rpms, crossfade and
   other vehicle_rpm curves are warped from the template's rpm points to the
   Forza ones, and VSVH+94/+98 become the Forza idle/redline, so bank rpm is
   Forza rpm. VSVH+4C (engine type) follows the Forza cylinder count.
4. Bank ID, every HIRC object, layer and media ID get fresh IDs (Wwise
   indexes them globally: a copy with the template's IDs would play the
   template's media whenever that car is loaded first), and the VSVH event
   fields follow. Fresh IDs are (spool ID << 22) | 22 bits of an FNV-1 hash of
   the old ID, rehashed while they hit an ID of the stock fleet's banks, of
   WWiseDefault.pck or of the bank itself: deterministic, and two cars of one
   build (different spool IDs) can't collide (`fresh_ids`).

Templates (`template_order`): stock sample banks with the 370Z layout (two
loop sets of 6 to 8 loops, no granular GRBK), in the order of
src/dsfforza/build/host_select.py's sound ranking for the car (identity, engine, era), the
explicit --sound-host first. port_car tries them, and shorter loops, until the
car's NONR fits its 0x6D000 buffer, and keeps the host's sound when none does.

Commands:

    uv run dsff engine_sound convert --car MAZ_RX7_97 --game fh1 --host 227 --out out/sound/MAZ_RX7_97
    uv run dsff engine_sound apply out/sound/build/dngvehicles.sp out/sound/build/dngvehicles.sound.sp \\
        --spool 1 --sound out/sound/MAZ_RX7_97
    uv run dsff engine_sound sets 227          # a stock bank's loop sets
    uv run dsff engine_sound templates         # stock banks usable as templates
    uv run dsff engine_sound lint out/testdrive8/dngvehicles.sp --id 1 [--stock ...]

convert outputs in --out: SBAK.bin, VSVH.bin, report.json, wav/ (every new
loop as the game will decode it) and sweep_{template,new}.wav (an offline
render of an idle -> redline -> idle sweep through both sets' curves, full load).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import itertools
import json
import math
import mmap
import os
import shutil
import struct
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from dsfforza import REPO
from dsfforza.formats import wwise_bank as wb
from dsfforza.formats.chnk import read_chunk, write_chunk

DEFAULT_DSF = REPO / "out" / "stock" / "Install" / "dngvehicles.sp"
CACHE = REPO / "out" / "cache" / "sound"
RPM = 0x9C4D0121
RATE = 32000
VSVH_SIZE = 0x9C
VSVH_EVENTS = (0x04, 0x0C)  # Play events of set A and set B
# VSVH fields holding event IDs of the vehicle's own bank: on all 150 stock cars
# every non-zero value at these offsets is an Event in the car's bank, and no
# other field ever is (research/engine-sound.md).
EVENT_FIELDS = (
    0x04,
    0x08,
    0x0C,
    0x10,
    0x14,
    0x1C,
    0x24,
    0x28,
    0x2C,
    0x34,
    0x38,
    0x3C,
    0x40,
    0x44,
    0x48,
    0x54,
    0x58,
    0x5C,
    0x68,
    0x6C,
    0x70,
    0x74,
    0x78,
)
ENGINE_TYPE_FIELD = 0x4C
# VSVH+4C -> sw_engine_type state (Driver.exe 0x550300: value - 1 indexes a table, out of range = v8;
# 10 maps to il5, not il6). The states pick one of three rpm-following tone generators in the
# HUD bank of WWiseDefault.pck: 200 Hz at 12000 rpm (v8, h4, il4), 250 Hz (il5, v10), 300 Hz
# (v6, vr6, h6, v12, il6), i.e. 1, 1.25 and 1.5 times the crank frequency.
ENGINE_TYPES = {1: "v6", 2: "v8", 3: "v10", 4: "v12", 5: "vr6", 6: "h4", 7: "h6", 8: "il4", 9: "il5", 10: "il5"}
TEMPLATE_SLOTS = range(6, 9)  # loops per set of a usable template ("370Z layout")
ID_NAMESPACE_BITS = 22
MAX_SECONDS = (1.1, 0.8, 0.6)  # loop lengths tried in turn against the NONR budget
GAME_ISOS = {"fh1": ["Forza.Horizon.XBOX360.iso"], "fm4": ["Forza 4 [NTSCU][DVD1].iso", "Forza 4 [NTSCU][DVD2].iso"]}
EMISSION_GROUP = {"Exhaust": "ExhaustL", "EngineAmbient": "EngineAmbient", "EngineIntake": "EngineIntake"}
# Emission per set, first one the car has (race cars often lack one).
SET_EMISSIONS = {"A": ("EngineAmbient", "EngineIntake", "Exhaust"), "B": ("Exhaust", "EngineAmbient", "EngineIntake")}
STOCK_INDEX_VERSION = 2


def fnv1(text: str) -> int:
    h = 2166136261
    for c in text.lower().encode():
        h = (h * 16777619) & 0xFFFFFFFF
        h ^= c
    return h


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(data)
    try:
        Path(temp).replace(path)
    except OSError:  # another process holds the target; its copy is as good
        temp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- template side


@dataclass
class Slot:
    media: int
    rpm: float
    sounds: list = field(default_factory=list)


def stock_sound(dsf: Path, spool: int) -> tuple[bytes, bytes]:
    """(SBAK, VSVH) of a stock vehicle's kind-0 entry."""
    from dsfforza.build import clone_vehicle as cv
    from dsfforza.formats.vehicles import find

    with dsf.open("rb") as s:
        entries = read_chunk(s, 0).entries
        for sid, index, kind in cv.sslp_records(s, entries):
            if sid == spool and kind == 0:
                vsrt = find(find(entries[index].children, "NONR").children, "VSRT")
                parts = {c.tag: cv.read_leaf(s, c) for c in vsrt.children}
                return parts["SBAK"], parts["VSVH"]
    raise KeyError(f"no stock vehicle {spool}")


def subtree(bank: wb.Bank, root: int) -> list[wb.HircObject]:
    objs, out, todo, seen = bank.by_id(), [], [root], set()
    while todo:
        o = objs.get(todo.pop())
        if o is None or o.id in seen:
            continue
        seen.add(o.id)
        out.append(o)
        todo.extend(o.info.get("children", []))
    return out


def play_target(bank: wb.Bank, event: int) -> int | None:
    objs = bank.by_id()
    ev = objs.get(event)
    if ev is None or ev.type != 4:
        return None
    for a in ev.info["actions"]:
        if a in objs and objs[a].info["action"] == 0x04011:
            return objs[a].info["target"]
    return None


def loop_sets(bank: wb.Bank, vsvh: bytes) -> dict[str, list[Slot]]:
    """Set name -> slots (one per embedded media with an rpm pitch curve), by rpm."""
    media = {m for m, _ in bank.media}
    sets, taken = {}, set()
    for name, off in zip("AB", VSVH_EVENTS):
        target = play_target(bank, struct.unpack_from("<I", vsvh, off)[0])
        slots: dict[int, Slot] = {}
        for o in subtree(bank, target) if target else []:
            if o.type == 2 and o.info["source"] in media and wb.sound_rpm(o):
                if o.info["source"] in taken:
                    raise ValueError(f"media {o.info['source']:08x} is in both loop sets")
                slot = slots.setdefault(o.info["source"], Slot(o.info["source"], wb.sound_rpm(o)))
                slot.sounds.append(o.id)
        taken |= set(slots)
        sets[name] = sorted(slots.values(), key=lambda s: s.rpm)
    return sets


def set_media(bank: wb.Bank, vsvh: bytes) -> set[int]:
    """Every embedded media a Sound under set A's or set B's play event uses."""
    media = {m for m, _ in bank.media}
    out = set()
    for off in VSVH_EVENTS:
        target = play_target(bank, struct.unpack_from("<I", vsvh, off)[0])
        out |= {
            o.info["source"]
            for o in (subtree(bank, target) if target else [])
            if o.type == 2 and o.info["source"] in media
        }
    return out


def set_of_objects(bank: wb.Bank, vsvh: bytes) -> dict[int, str]:
    out = {}
    for name, off in zip("AB", VSVH_EVENTS):
        target = play_target(bank, struct.unpack_from("<I", vsvh, off)[0])
        for o in subtree(bank, target) if target else []:
            out.setdefault(o.id, name)
    return out


# --------------------------------------------------------------------------- stock index


def game_dir(dsf: Path | None = None) -> Path:
    if os.environ.get("DSF_GAME_DIR"):
        return Path(os.environ["DSF_GAME_DIR"])
    if dsf is not None:
        from dsfforza.game import game_paths

        found = game_paths.game_dir_of(dsf)
        if found is not None and found.resolve() != game_paths.REPO_STOCK.resolve():
            return found.parent if found.name == game_paths.BACKUP else found
    from dsfforza.game import game_paths

    return game_paths.game_dir()


def find_pck(game: Path) -> Path | None:
    for media in ("Media", "media"):
        path = game / media / "WWiseDefault.pck"
        if path.is_file():
            return path
    return None


def pck_ids(path: Path) -> set[int]:
    """Bank, HIRC object, media and streamed file IDs of an AKPK package
    (header: magic, header size, version, then the language map, bank and stream
    lookup tables; LUT entries {u32 id, u32 block, u64 size, u32 start block, u32 language})."""
    out: set[int] = set()
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        magic, _hsize, _ver, lang, banks, streams = struct.unpack_from("<4sIIIII", m, 0)
        if magic != b"AKPK":
            raise ValueError(f"{path}: not an AKPK package")
        for at, size in ((0x18 + lang, banks), (0x18 + lang + banks, streams)):
            count = struct.unpack_from("<I", m, at)[0]
            if not count:
                continue
            step = (size - 4) // count
            for k in range(count):
                fid, block, _size, start, _lang = struct.unpack_from("<IIQII", m, at + 4 + k * step)
                out.add(fid)
                if at != 0x18 + lang:
                    continue
                q = start * block
                if m[q : q + 4] != b"BKHD":
                    continue
                while m[q : q + 4] in (b"BKHD", b"DIDX", b"DATA", b"HIRC", b"STID", b"STMG", b"ENVS", b"FXPR"):
                    tag, n = m[q : q + 4], struct.unpack_from("<I", m, q + 4)[0]
                    body = q + 8
                    if tag == b"DIDX":
                        out.update(struct.unpack_from("<I", m, body + i)[0] for i in range(0, n, 12))
                    elif tag == b"HIRC":
                        p = body + 4
                        for _ in range(struct.unpack_from("<I", m, body)[0]):
                            _t, osize, oid = struct.unpack_from("<III", m, p)
                            out.add(oid)
                            p += 8 + osize
                    q = body + n
    return out


def bank_ids(bank: wb.Bank, references: bool = True) -> set[int]:
    """IDs a bank defines (bank, objects, layers, media) and, with references,
    every ID it refers to (children, parents, buses, RTPCs, switches, states)."""
    out = {bank.bank_id} | {m for m, _ in bank.media}
    for o in bank.objects:
        out.add(o.id)
        i = o.info
        out |= {layer["id"] for layer in i.get("layers", [])}
        if not references:
            continue
        out |= {struct.unpack_from("<I", o.body, at)[0] for at in i["refs"]}
        out |= {rt["rtpc"] for rt in i.get("rtpcs", [])}
        for layer in i.get("layers", []):
            out.add(layer["xfade_rtpc"])
            out |= {rt["rtpc"] for rt in layer["rtpcs"]}
        if o.type == 6:
            out |= {i["group"], i["default"]} | {sw for sw, _ in i["switches"]}
        for state in i.get("states", []):
            out |= {state[0], state[2]}
        if "bus" in i:
            out.add(i["bus"])
    out.discard(0)
    return out


def stock_index(dsf: Path = DEFAULT_DSF, cache: Path = CACHE / "stock-banks.json", game: Path | None = None) -> dict:
    """Facts about the stock vehicle banks, cached by archive (and pck) size and mtime:
    "reserved": every ID the stock vehicle banks and WWiseDefault.pck define or use
    (fresh IDs avoid them); "banks": per base spool ID, the loop sets, sizes, rpm range,
    granular flag and whether it can be a template."""
    pck = find_pck(game or game_dir(dsf))
    stat = dsf.stat()
    key = f"{STOCK_INDEX_VERSION}:{dsf.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"
    if pck:
        key += f":{pck}:{pck.stat().st_size}:{int(pck.stat().st_mtime)}"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text())
            if data.get("key") == key:
                data["reserved"] = set(data["reserved"])
                data["banks"] = {int(k): v for k, v in data["banks"].items()}
                return data
        except (ValueError, KeyError):
            pass
    from dsfforza.build import clone_vehicle as cv
    from dsfforza.formats.vehicles import find

    reserved = set(wb.KNOWN_IDS)
    banks = {}
    with dsf.open("rb") as s:
        entries = read_chunk(s, 0).entries
        for sid, index, kind in cv.sslp_records(s, entries):
            if kind != 0:
                continue
            nonr = find(entries[index].children, "NONR")
            vsrt = next((c for c in nonr.children if c.tag == "VSRT"), None)
            if vsrt is None:
                continue
            parts = {c.tag: cv.read_leaf(s, c) for c in vsrt.children}
            bank = wb.Bank.parse(parts["SBAK"])
            reserved |= bank_ids(bank)
            vsvh = parts["VSVH"]
            row = {
                "name": entries[index].name,
                "sbak": len(parts["SBAK"]),
                "grbk": "GRBK" in parts,
                "vsrt": vsrt.size,
                "bank_id": bank.bank_id,
                "rpm": list(struct.unpack_from("<II", vsvh, 0x94)),
                "engine_type": struct.unpack_from("<I", vsvh, ENGINE_TYPE_FIELD)[0],
                "sets": {},
                "why": "",
            }
            try:
                sets = loop_sets(bank, vsvh)
                row["sets"] = {k: [round(s.rpm) for s in v] for k, v in sets.items()}
                row["other_media"] = len(set_media(bank, vsvh) - {s.media for v in sets.values() for s in v})
            except ValueError as error:
                row["why"] = str(error)
            if not row["why"]:
                counts = [len(row["sets"].get(k, [])) for k in "AB"]
                if row["grbk"]:
                    row["why"] = "granular (GRBK)"
                elif not all(counts):
                    row["why"] = "no sample loop sets"
                elif not all(c in TEMPLATE_SLOTS for c in counts):
                    row["why"] = f"{counts[0]}+{counts[1]} loops (not the 370Z layout)"
                elif row["other_media"]:
                    # e.g. 126/129/184/178/302: a random container of 0.13 s idle grains
                    # pitched on its parent; they would keep playing the template's engine
                    row["why"] = f"{row['other_media']} more embedded media in the loop sets (idle grains)"
            banks[sid] = row
    if pck:
        reserved |= pck_ids(pck)
    data = {"key": key, "pck": str(pck) if pck else None, "reserved": sorted(reserved), "banks": banks}
    write_atomic(cache, json.dumps(data).encode())
    data["reserved"] = reserved
    return data


def fresh_ids(namespace: int, tag: str, olds, reserved: set[int]) -> dict[int, int]:
    """old -> new ID: (namespace << 22) | 22 bits of FNV-1("tag:old:salt"), the salt
    counting up while the value is 0, reserved, one of the old IDs or already given.
    Deterministic; the namespace (the car's spool ID) keeps cars apart."""
    if not 0 < namespace < 1 << (32 - ID_NAMESPACE_BITS):
        raise ValueError(f"ID namespace {namespace} out of range")
    olds = set(olds)
    mask = (1 << ID_NAMESPACE_BITS) - 1
    out, given = {}, set()
    for old in sorted(olds):
        salt = 0
        while True:
            new = (namespace << ID_NAMESPACE_BITS) | (fnv1(f"{tag}:{old:08x}:{salt}") & mask)
            if new and new not in reserved and new not in olds and new not in given:
                break
            salt += 1
        out[old] = new
        given.add(new)
    return out


def engine_type(cylinders: int | None, layout: str | None) -> int | None:
    """VSVH+4C for a Forza engine (host_select.Engine fields), or None (keep the template's).
    The value picks a tone of 1, 1.25 or 1.5 times the crank frequency (half the
    firing frequency of 4, 5 and 6 cylinders): 6-cylinders get v6, since 10 (il6)
    is mapped to il5 by Driver.exe."""
    if not cylinders or layout == "E":
        return None
    if layout == "R":  # rotors fire once per eccentric shaft turn
        return 8 if cylinders <= 2 else 1
    if layout == "F":
        return 6 if cylinders <= 4 else 7
    if cylinders <= 4:
        return 8
    if cylinders == 5:
        return 9
    if cylinders == 6:
        return 5 if layout == "W" else 1  # Forza files the VR6 as W
    return {8: 2, 10: 3, 12: 4, 16: 2}.get(cylinders, 2)


# --------------------------------------------------------------------------- Forza side


@dataclass
class ForzaLoop:
    name: str
    rpm: int
    rpm_min: int
    rpm_max: int
    volume: float
    rate: int
    pcm: np.ndarray  # float, channel 0, -1..1


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        ch, rate = w.getnchannels(), w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768
    return x.reshape(-1, ch)[:, 0], rate


_GAME_OF_ISO: dict[str, str | None] = {}


def isos_for(game: str, isos: list[Path]) -> list[Path]:
    """The given disc images that belong to game ("fh1"/"fm4"): by file name, else by
    default.xex's title ID."""
    game = game.lower()
    named = [p for p in isos if p.name in GAME_ISOS[game]]
    if named:
        return named
    from dsfforza.build.forza_roster import TITLES, xex_title
    from dsfforza.formats.xiso import XIso

    out = []
    for p in isos:
        key = str(Path(p).resolve())
        if key not in _GAME_OF_ISO:
            image = XIso(p)
            try:
                _GAME_OF_ISO[key] = TITLES.get(xex_title(image)[0])
            except (ValueError, KeyError):
                _GAME_OF_ISO[key] = None
            finally:
                image.close()
        if (_GAME_OF_ISO[key] or "").lower() == game:
            out.append(Path(p))
    return out


def _decoded(fsb_path: Path, fsb, folder: Path, vgm: Path) -> list[Path]:
    """The FSB's subsongs as WAVs in folder (decoded once; workers racing on the
    same bank each decode to their own folder and the first rename wins)."""
    from dsfforza.formats import forza_audio as fa

    wavs = sorted(folder.glob("*.wav")) if folder.is_dir() else []
    if len(wavs) == len(fsb.subsongs):
        return wavs
    temp = folder.with_name(f"{folder.name}.{os.getpid()}.tmp")
    shutil.rmtree(temp, ignore_errors=True)
    fa.decode_bank(fsb_path, fsb, temp, vgm)
    if folder.is_dir() and len(list(folder.glob("*.wav"))) != len(fsb.subsongs):
        shutil.rmtree(folder, ignore_errors=True)  # a broken earlier decode
    try:
        Path(temp).replace(folder)
    except OSError:
        shutil.rmtree(temp, ignore_errors=True)
    wavs = sorted(folder.glob("*.wav"))
    if len(wavs) != len(fsb.subsongs):
        raise RuntimeError(f"decoding {fsb_path.name}: {len(wavs)} of {len(fsb.subsongs)} WAVs")
    return wavs


def forza_engine(
    car: str, game: str, isos: list[Path], cache: Path, emissions: list[str] | None = None, vgmstream: str | None = None
) -> dict:
    """Idle, redline and the decoded L0 loops of a Forza car's emissions (default:
    every emission its ET file names). Raises LookupError when the car's audio
    can't be resolved."""
    from dsfforza.formats import forza_audio as fa

    game = game.lower()
    discs = isos_for(game, [Path(p) for p in isos])
    if not discs:
        raise LookupError(f"no {game.upper()} disc images")
    sources = [fa.Source(str(p)) for p in discs]
    try:
        tuning = fa.Tuning(sources)
        try:
            et = tuning.engine(car)
        except KeyError as error:
            raise LookupError(str(error).strip("'\"")) from None
        out = {
            "idle": float(et.settings.get("audio_rpm_idle", 0)),
            "redline": float(et.settings.get("audio_rpm_redline", 0)),
            "et": et.file,
            "emissions": {},
            "missing": {},
        }
        try:
            vgm = fa.vgmstream_path(vgmstream)
        except SystemExit as error:
            raise LookupError(str(error)) from None
        for emission in emissions or list(EMISSION_GROUP):
            ht_name = et.upgrades.get(emission, {}).get("L0")
            if not ht_name or ht_name.lower() in ("na.xml", "na"):
                out["missing"][emission] = f"no {emission} L0 in {et.file}" + (f" ({ht_name})" if ht_name else "")
                continue
            try:
                ht, _ = tuning.harmonic(ht_name)
            except KeyError as error:
                out["missing"][emission] = str(error).strip("'\"")
                continue
            hits = [f for s in sources for f in s.find_name(ht.bank) if fa.BANK_DIR in f.path.lower()]
            if not hits:
                out["missing"][emission] = f"bank {ht.bank} not on the {game.upper()} discs"
                continue
            fsb_path = cache / game / ht.bank
            if not fsb_path.is_file():
                write_atomic(fsb_path, hits[0].read())
            fsb = fa.parse_fsb(fsb_path.read_bytes())
            wavs = _decoded(fsb_path, fsb, cache / game / Path(ht.bank).stem, vgm)
            by_index = {int(w.name.split("_", 1)[0]): w for w in wavs}
            group = EMISSION_GROUP[emission]
            loops = []
            for lp in ht.loops:
                if lp.group != group or lp.index not in by_index:
                    continue
                pcm, rate = read_wav(by_index[lp.index])
                loops.append(
                    ForzaLoop(fsb.subsongs[lp.index].name, lp.rpm_sample, lp.rpm_min, lp.rpm_max, lp.volume, rate, pcm)
                )
            if not loops:
                out["missing"][emission] = f"{ht_name}: no {group} loops"
                continue
            out["emissions"][emission] = {"ht": ht_name, "bank": ht.bank, "loops": sorted(loops, key=lambda l: l.rpm)}
        return out
    finally:
        for s in sources:
            s.close()


def pick_emissions(forza: dict, wanted: dict[str, str] | None = None) -> dict[str, str]:
    """Set -> emission: the wanted one, else the first of SET_EMISSIONS the car has;
    the two sets get different emissions when the car has two."""
    have = forza["emissions"]
    out = {}
    for name in "AB":
        order = ([wanted[name]] if wanted and wanted.get(name) else []) + list(SET_EMISSIONS[name])
        choice = next((e for e in order if e in have and e not in out.values()), None) or next(
            (e for e in order if e in have), None
        )
        if choice is None:
            raise LookupError("no usable emission: " + "; ".join(forza["missing"].values()))
        out[name] = choice
    return out


def pick_loops(loops: list[ForzaLoop], n: int, redline: float) -> list[ForzaLoop]:
    """n loops from the lowest to the highest at or below the redline (+10%),
    chosen to minimise the largest rpm ratio between neighbours (then the sum of
    squared log gaps): each loop is pitched as little as possible before the
    crossfade hands over. With fewer than n loops, the largest rpm gap is split
    in turn by a copy of its lower loop pitched to the gap's geometric middle
    (periodic resample: the loop plays faster, as the game would pitch it)."""
    from itertools import combinations

    usable = [l for l in loops if l.rpm <= redline * 1.1] or loops
    if len(usable) < n:
        out = sorted(usable, key=lambda l: l.rpm)
        while len(out) < n:
            if len(out) == 1:
                low, rpm = out[0], out[0].rpm * 1.25
            else:
                k = max(range(len(out) - 1), key=lambda j: out[j + 1].rpm / out[j].rpm)
                low, rpm = out[k], math.sqrt(out[k].rpm * out[k + 1].rpm)
            pcm = periodic_resample(low.pcm, max(64, round(len(low.pcm) * low.rpm / rpm)))
            out.append(
                ForzaLoop(f"{low.name}@{round(rpm)}", round(rpm), low.rpm_min, low.rpm_max, low.volume, low.rate, pcm)
            )
            out.sort(key=lambda l: l.rpm)
        return out
    if len(usable) == n:
        return usable
    logs = [math.log(l.rpm) for l in usable]
    best, pick = None, None
    for mid in combinations(range(1, len(usable) - 1), n - 2):
        idx = (0, *mid, len(usable) - 1)
        gaps = [logs[b] - logs[a] for a, b in itertools.pairwise(idx)]
        key = (round(max(gaps), 6), sum(g * g for g in gaps))
        if best is None or key < best:
            best, pick = key, idx
    return [usable[i] for i in pick]


# --------------------------------------------------------------------------- audio


def seamless(x: np.ndarray, rate: int, max_seconds: float, fade: float = 0.06) -> np.ndarray:
    """A loop of at most max_seconds from a whole-sample loop x: keep x if short
    enough, else cut a segment whose tail crossfades into the material just
    before x's start (x is circular), at the cut length that correlates best."""
    n = len(x)
    if n <= max_seconds * rate:
        return x
    xf = int(fade * rate)
    target = int(max_seconds * rate)
    tail_ref = x[n - xf :]
    best, best_len = -2.0, target
    for length in range(max(xf * 2, target - rate // 10), target + 1):
        seg = x[length - xf : length]
        c = float(np.dot(seg, tail_ref) / (np.linalg.norm(seg) * np.linalg.norm(tail_ref) + 1e-12))
        if c > best:
            best, best_len = c, length
    y = x[:best_len].copy()
    w = np.sin(np.linspace(0, math.pi / 2, xf)) ** 2
    y[best_len - xf :] = y[best_len - xf :] * (1 - w) + tail_ref * w
    return y


def periodic_resample(x: np.ndarray, m: int) -> np.ndarray:
    """Resample one period of a periodic signal to exactly m samples (FFT)."""
    n = len(x)
    spec = np.fft.rfft(x)
    out = np.zeros(m // 2 + 1, dtype=complex)
    k = min(len(spec), len(out))
    out[:k] = spec[:k]
    if m % 2 == 0 and k == len(out) and k <= len(spec):
        out[-1] = out[-1].real
    return np.fft.irfft(out, m) * (m / n)


def to_bank_rate(x: np.ndarray, rate: int) -> np.ndarray:
    blocks = max(1, round(len(x) * RATE / rate / wb.BLOCK_SAMPLES))
    return periodic_resample(x, blocks * wb.BLOCK_SAMPLES)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


# --------------------------------------------------------------------------- curves


def warp(points: list[tuple[float, float]], x: float) -> float:
    for (x0, y0), (x1, y1) in itertools.pairwise(points):
        if x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def warp_map(old: list[float], new: list[float], top: float = 12000.0) -> list[tuple[float, float]]:
    pts = [(0.0, 0.0)] + list(zip(old, new)) + [(top, max(top, new[-1] * 1.05))]
    out = []
    for p in pts:  # keep it strictly increasing
        if not out or (p[0] > out[-1][0] and p[1] > out[-1][1]):
            out.append(p)
    return out


def retune(
    bank: wb.Bank,
    vsvh: bytes,
    sets: dict[str, list[Slot]],
    new_rpm: dict[int, float],
    lo_hi_old: tuple[float, float],
    lo_hi_new: tuple[float, float],
) -> dict:
    """Pitch curves from the new recording rpms; every other vehicle_rpm curve
    and layer crossfade warped from the old rpm points to the new ones."""
    maps = {
        name: warp_map([s.rpm for s in slots], [new_rpm[s.media] for s in slots])
        for name, slots in sets.items()
        if slots
    }
    other = warp_map(list(lo_hi_old), list(lo_hi_new))
    owner = set_of_objects(bank, vsvh)
    sound_media = {}
    for slots in sets.values():
        for s in slots:
            for sid in s.sounds:
                sound_media[sid] = s.media
    counts = {"pitch": 0, "warped": 0, "xfade": 0}
    for o in bank.objects:
        m = maps.get(owner.get(o.id), other)
        for rt in o.info.get("rtpcs", []):
            if rt["rtpc"] != RPM:
                continue
            if rt["param"] == 2 and o.id in sound_media:
                r = new_rpm[sound_media[o.id]]
                xs = [p[0] for p in rt["points"]]
                # Wwise interpolates linearly between points: put one on the recording rpm
                inner = range(1, len(xs) - 1)
                if inner:
                    k = min(inner, key=lambda j: abs(math.log(max(xs[j], 1) / r)))
                    if xs[k - 1] < r < xs[k + 1]:
                        xs[k] = r
                pts = [
                    (x, -4800.0 if x <= 0 else max(-4800.0, min(4800.0, 1200 * math.log2(x / r))), p[2])
                    for x, p in zip(xs, rt["points"])
                ]
                counts["pitch"] += 1
            else:
                pts = [(warp(m, x), y, i) for x, y, i in rt["points"]]
                counts["warped"] += 1
            bank.set_points(o, rt["at"], pts)
        for layer in o.info.get("layers", []):
            if layer["xfade_rtpc"] != RPM:
                continue
            for _child, points, at in layer["assoc"]:
                bank.set_points(o, at, [(warp(m, x), y, i) for x, y, i in points])
                counts["xfade"] += 1
            for rt in layer["rtpcs"]:
                if rt["rtpc"] == RPM:
                    bank.set_points(o, rt["at"], [(warp(m, x), y, i) for x, y, i in rt["points"]])
                    counts["warped"] += 1
    bank.reparse()
    return counts


# --------------------------------------------------------------------------- preview


def curve_at(points, x: float) -> float:
    return warp([(p[0], p[1]) for p in points], x)


def slot_curves(bank: wb.Bank, vsvh: bytes) -> dict[str, list[tuple[Slot, list, list | None]]]:
    """Set -> (slot, pitch points, crossfade points or None) per loop, from the Sound
    in the rpm layer with the most loops (the player's; AI layers hold a 4-loop
    subset with wide crossfades), else the first Sound of the slot."""
    objs = bank.by_id()
    xfade, width = {}, {}
    for o in bank.objects:
        for layer in o.info.get("layers", []):
            if layer["xfade_rtpc"] == RPM:
                for child, pts, _ in layer["assoc"]:
                    if len(layer["assoc"]) > width.get(child, 0):
                        xfade[child], width[child] = pts, len(layer["assoc"])
    out = {}
    for name, slots in loop_sets(bank, vsvh).items():
        rows = []
        for s in slots:
            snd = max((objs[i] for i in s.sounds), key=lambda o: width.get(o.id, 0))
            pitch = next(rt["points"] for rt in snd.info["rtpcs"] if rt["rtpc"] == RPM and rt["param"] == 2)
            rows.append((s, pitch, xfade.get(snd.id)))
        out[name] = rows
    return out


def pitch_ranges(bank: wb.Bank, vsvh: bytes) -> dict[str, dict]:
    """Per set, over the bank's rpm range VSVH+94..+98: the pitch ratio each loop is
    played at while its crossfade gain is above -40 dB (min, max), the overall
    range ("pitch") and the range of the loudest loop at each rpm ("pitch_main")."""
    lo, hi = struct.unpack_from("<II", vsvh, 0x94)
    grid = np.linspace(lo, hi, 401)
    out = {}
    for name, rows in slot_curves(bank, vsvh).items():
        loops, all_ratios = [], []
        gains = np.array([[curve_at(xf, g) / 255 for g in grid] if xf else [1.0] * len(grid) for _, _, xf in rows])
        cents = np.array([[curve_at(pitch, g) for g in grid] for _, pitch, _ in rows])
        main = 2 ** (cents[gains.argmax(axis=0), np.arange(len(grid))] / 1200)
        for (s, pitch, _xf), gain in zip(rows, gains):
            live = grid[gain > 0.01]
            if not len(live):
                loops.append({"rpm": round(s.rpm), "pitch": None})
                continue
            ratios = [float(2 ** (curve_at(pitch, g) / 1200)) for g in (live[0], live[-1])]
            all_ratios += ratios
            loops.append(
                {
                    "rpm": round(s.rpm),
                    "pitch": [round(min(ratios), 3), round(max(ratios), 3)],
                    "rpm_range": [int(round(live[0])), int(round(live[-1]))],
                }
            )
        out[name] = {
            "pitch": [round(min(all_ratios), 3), round(max(all_ratios), 3)] if all_ratios else None,
            "pitch_main": [round(float(main.min()), 3), round(float(main.max()), 3)],
            "loops": loops,
        }
    return out


def render_sweep(bank: wb.Bank, vsvh: bytes, seconds=(1.0, 6.0, 1.0, 4.0, 1.0)) -> np.ndarray:
    """Idle, ramp to redline, hold, ramp down, idle over the bank's rpm range; both
    sets at full load, each loop pitched and crossfaded by its own curves."""
    lo, hi = struct.unpack_from("<II", vsvh, 0x94)
    media = {m: np.array(wb.Wem.parse(w).pcm()[0], dtype=np.float64) / 32768 for m, w in bank.media}
    n = int(sum(seconds) * RATE)
    t = np.arange(n) / RATE
    hold0, up, hold1, down, hold2 = seconds
    norm = np.interp(t, np.cumsum([0, hold0, up, hold1, down, hold2]), [0, 0, 1, 1, 0, 0])
    bank_rpm = lo + (hi - lo) * norm
    grid = np.linspace(0, 12000, 1201)
    out = np.zeros(n)
    for rows in slot_curves(bank, vsvh).values():
        for s, pitch, xf in rows:
            cents = np.interp(bank_rpm, grid, [curve_at(pitch, g) for g in grid])
            gain = np.interp(bank_rpm, grid, [curve_at(xf, g) / 255 for g in grid]) if xf else np.ones(n)
            src = media[s.media]
            pos = np.cumsum(2 ** (cents / 1200)) % len(src)
            i0 = pos.astype(int)
            frac = pos - i0
            out += gain * (src[i0] * (1 - frac) + src[(i0 + 1) % len(src)] * frac)
    peak = np.abs(out).max() or 1
    return out / peak * 0.9


def write_wav(path: Path, x: np.ndarray, rate: int = RATE) -> None:
    wb.write_wav(path, [list(np.clip(np.round(x * 32767), -32768, 32767).astype(int))], rate)


def same_structure(host: wb.Bank, new: wb.Bank, mapping: dict[int, int], old_bank: int) -> list[str]:
    """The new bank must be the template's hierarchy under the ID mapping: same objects
    in the same order and sizes, same parsed fields; only curve points, media
    offsets/sizes and IDs may differ."""
    m = dict(mapping)
    m[old_bank] = new.bank_id

    def norm(v, key=""):
        if key in ("points", "refs", "at", "offset", "size", "offset_at"):
            return None
        if isinstance(v, dict):
            return {k: norm(x, k) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        if isinstance(v, int):
            return m.get(v, v)
        return v

    problems = []
    if len(host.objects) != len(new.objects):
        return [f"object count {len(host.objects)} -> {len(new.objects)}"]
    for a, b in zip(host.objects, new.objects):
        if a.type != b.type or m.get(a.id) != b.id or len(a.body) != len(b.body):
            problems.append(f"{a.kind} {a.id:08x}: type/id/size changed")
            continue
        info = {k: v for k, v in a.info.items() if k != "layers"}
        layers = [{**l, "assoc": [(c, None, None) for c, _, _ in l["assoc"]]} for l in a.info.get("layers", [])]
        new_layers = [{**l, "assoc": [(c, None, None) for c, _, _ in l["assoc"]]} for l in b.info.get("layers", [])]
        if norm(info) != norm({k: v for k, v in b.info.items() if k != "layers"}) or norm(layers) != norm(new_layers):
            problems.append(f"{a.kind} {a.id:08x}: fields differ")
    if [len(w) > 0 for _, w in host.media] != [len(w) > 0 for _, w in new.media] or [
        m.get(i, i) for i, _ in host.media
    ] != [i for i, _ in new.media]:
        problems.append("media list differs")
    return problems


# --------------------------------------------------------------------------- build


@dataclass
class Sound:
    """A converted engine sound: the new SBAK and VSVH and what went into them."""

    sbak: bytes
    vsvh: bytes
    report: dict
    template_sbak: bytes
    template_vsvh: bytes
    decoded: list = field(default_factory=list)  # (wav name, float pcm) per new loop

    def write(self, out: Path) -> None:
        """SBAK.bin, VSVH.bin, report.json, wav/ and the two sweeps."""
        (out / "wav").mkdir(parents=True, exist_ok=True)
        for old in (out / "wav").glob("*.wav"):
            old.unlink()
        for name, pcm in self.decoded:
            write_wav(out / "wav" / name, pcm)
        # The template as the game would play it on this car (its own rpm range).
        write_wav(out / "sweep_template.wav", render_sweep(wb.Bank.parse(self.template_sbak), self.template_vsvh))
        write_wav(out / "sweep_new.wav", render_sweep(wb.Bank.parse(self.sbak), self.vsvh))
        (out / "SBAK.bin").write_bytes(self.sbak)
        (out / "VSVH.bin").write_bytes(self.vsvh)
        (out / "report.json").write_text(json.dumps(self.report, indent=1))


def build_sound(
    sbak: bytes,
    vsvh: bytes,
    forza: dict,
    *,
    namespace: int,
    reserved: set[int],
    tag: str,
    emissions: dict[str, str] | None = None,
    max_seconds: float = MAX_SECONDS[0],
    engine: int | None = None,
    bank_name: str | None = None,
) -> Sound:
    """The template bank (sbak, vsvh) with the Forza loops of `forza` (forza_engine)."""
    bank = wb.Bank.parse(sbak)
    template_vsvh = bytes(vsvh)
    vsvh = bytearray(vsvh)
    sets = loop_sets(bank, vsvh)
    if not sets["A"] or not sets["B"]:
        raise ValueError("template has no sample loop sets (granular or silent)")
    if set_media(bank, vsvh) - {s.media for v in sets.values() for s in v}:
        raise ValueError("template's loop sets play more media than their rpm loops (idle grains)")
    lo_old, hi_old = struct.unpack_from("<II", vsvh, 0x94)
    host_wavs = {m: np.array(wb.Wem.parse(w).pcm()[0], dtype=np.float64) / 32768 for m, w in bank.media}
    idle, redline = forza["idle"], forza["redline"]
    if not (0 < idle < 5000 and idle < redline < 15000):
        raise ValueError(f"idle/redline {idle:g}/{redline:g} outside the VSVH+94/+98 rules")
    emissions = pick_emissions(forza, emissions)

    report = {
        "idle": idle,
        "redline": redline,
        "et": forza["et"],
        "template_rpm_range": [lo_old, hi_old],
        "max_seconds": max_seconds,
        "sets": {},
    }
    new_media, new_rpm, decoded = {}, {}, []
    template = wb.Wem.parse(bank.media[0][1])
    for name, slots in sets.items():
        emission = emissions[name]
        loops = forza["emissions"][emission]["loops"]
        chosen = pick_loops(loops, len(slots), redline)
        host_level = max(rms(host_wavs[s.media]) for s in slots)
        cut = [seamless(l.pcm, l.rate, max_seconds) for l in chosen]
        resampled = [to_bank_rate(c, l.rate) for c, l in zip(cut, chosen)]
        levels = [rms(r) * l.volume for r, l in zip(resampled, chosen)]
        gain = host_level / max(levels) if max(levels) else 1.0
        scaled = [r * l.volume * gain for r, l in zip(resampled, chosen)]
        peak = max(np.abs(s).max() for s in scaled)
        if peak > 0.97:
            scaled = [s * 0.97 / peak for s in scaled]
        rows = []
        for k, (slot, loop, x) in enumerate(zip(slots, chosen, scaled)):
            pcm = np.clip(np.round(x * 32767), -32768, 32767).astype(int)
            wem = template.with_data(wb.encode_ima(list(pcm)), RATE).build()
            new_media[slot.media] = wem
            new_rpm[slot.media] = float(loop.rpm)
            decoded.append(
                (
                    f"{name}{k}_{loop.rpm:05d}rpm_{loop.name}.wav",
                    np.array(wb.Wem.parse(wem).pcm()[0], dtype=np.float64) / 32768,
                )
            )
            rows.append(
                {
                    "slot": k,
                    "template_media": f"{slot.media:08x}",
                    "template_rpm": round(slot.rpm),
                    "forza": loop.name,
                    "rpm": loop.rpm,
                    "volume": loop.volume,
                    "seconds": round(len(pcm) / RATE, 3),
                    "bytes": len(wem),
                }
            )
        report["sets"][name] = {
            "emission": emission,
            "bank": forza["emissions"][emission]["bank"],
            "ht": forza["emissions"][emission]["ht"],
            "available": len(loops),
            "gain": round(gain, 3),
            "loops": rows,
        }

    bank.replace_media(new_media)
    report["curves"] = retune(bank, bytes(vsvh), sets, new_rpm, (lo_old, hi_old), (idle, redline))
    struct.pack_into("<II", vsvh, 0x94, int(round(idle)), int(round(redline)))
    old_type = struct.unpack_from("<I", vsvh, ENGINE_TYPE_FIELD)[0]
    if engine is not None:
        struct.pack_into("<I", vsvh, ENGINE_TYPE_FIELD, engine)
    report["engine_type"] = {"template": old_type, "value": struct.unpack_from("<I", vsvh, ENGINE_TYPE_FIELD)[0]}
    report["engine_type"]["state"] = ENGINE_TYPES.get(report["engine_type"]["value"], "v8")

    # fresh IDs for the bank, every object, layer and embedded media
    old_bank = bank.bank_id
    ids = bank_ids(bank, references=False)
    mapping = fresh_ids(namespace, tag, ids, reserved)
    new_bank = mapping.pop(old_bank)
    bank.remap_ids(mapping)
    bank.set_bank_id(new_bank, (bank_name or tag)[:60])
    patched = []
    for off in EVENT_FIELDS:
        v = struct.unpack_from("<I", vsvh, off)[0]
        if v in mapping:
            struct.pack_into("<I", vsvh, off, mapping[v])
            patched.append(f"+{off:02X}")
    raw = bank.build()
    problems = wb.Bank.parse(raw).validate()
    problems += same_structure(wb.Bank.parse(sbak), wb.Bank.parse(raw), mapping, old_bank)
    problems += lint_bank(wb.Bank.parse(raw), bytes(vsvh))
    if problems:
        raise RuntimeError("rebuilt bank invalid: " + "; ".join(problems[:5]))
    new = wb.Bank.parse(raw)
    report.update(
        bank_id=f"{new_bank:08x}",
        template_bank_id=f"{old_bank:08x}",
        id_namespace=namespace,
        ids_remapped=len(mapping) + 1,
        vsvh_fields_remapped=patched,
        sbak_bytes=len(raw),
        template_sbak_bytes=len(sbak),
        vsvh_rpm_range=[int(round(idle)), int(round(redline))],
        pitch=pitch_ranges(new, bytes(vsvh)),
    )
    return Sound(raw, bytes(vsvh), report, sbak, template_vsvh, decoded)


# --------------------------------------------------------------------------- port_car step


def template_order(index: dict, ranking: list[int], first: list[int] | None = None, limit: int = 3) -> list[int]:
    """Usable templates (stock_index "why" empty) in preference order: `first`
    (an explicit sound host), then the sound ranking, at most `limit`, and the
    370Z (227) as the proven fallback."""
    out = []
    for sid in list(first or []) + list(ranking):
        row = index["banks"].get(sid)
        if row and not row["why"] and sid not in out:
            out.append(sid)
        if len(out) >= limit:
            break
    if 227 not in out and 227 in index["banks"] and not index["banks"][227]["why"]:
        out.append(227)
    return out


class Plan:
    """The Forza sound of one car, built lazily per (template, loop length) in
    preference order; port_car.build_car installs candidates until the NONR fits."""

    def __init__(
        self,
        stream,
        entries,
        media: str,
        game: str,
        spool_id: int,
        forza: dict,
        templates: list[int],
        reserved: set[int],
        engine: int | None,
        engine_text: str,
        log=print,
    ):
        self.stream, self.entries = stream, entries
        self.media, self.game, self.spool_id = media, game.lower(), spool_id
        self.forza, self.templates, self.reserved = forza, templates, reserved
        self.engine, self.engine_text, self.log = engine, engine_text, log
        self.built: list[Sound] = []
        self.failures: list[str] = []
        self._order = [(t, s) for t in templates for s in MAX_SECONDS]
        self._next = 0

    def candidates(self):
        k = 0
        while True:
            if k < len(self.built):
                yield self.built[k]
                k += 1
                continue
            if self._next >= len(self._order):
                return
            template, seconds = self._order[self._next]
            self._next += 1
            try:
                sbak, vsvh = self.template(template)
                sound = build_sound(
                    sbak,
                    vsvh,
                    self.forza,
                    namespace=self.spool_id,
                    reserved=self.reserved,
                    tag=f"dsfforza:{template}",
                    max_seconds=seconds,
                    engine=self.engine,
                    bank_name=f"dsfforza_{self.game}_{self.media}",
                )
            except (ValueError, RuntimeError, LookupError, KeyError) as error:
                self.failures.append(f"template {template}: {error}")
                self.log(f"  forza sound: template {template} failed: {error}")
                self._order = [o for o in self._order if o[0] != template]
                continue
            sound.report.update(template=template, media=self.media, game=self.game, engine=self.engine_text)
            self.built.append(sound)

    def template(self, sid: int) -> tuple[bytes, bytes]:
        from dsfforza.build import clone_vehicle as cv
        from dsfforza.formats.vehicles import find

        for s, index, kind in cv.sslp_records(self.stream, self.entries):
            if s == sid and kind == 0:
                vsrt = find(find(self.entries[index].children, "NONR").children, "VSRT")
                parts = {c.tag: cv.read_leaf(self.stream, c) for c in vsrt.children}
                return parts["SBAK"], parts["VSVH"]
        raise KeyError(f"no stock vehicle {sid}")


def install(full, sbak: bytes, vsvh: bytes, stream=None) -> dict:
    """Put a converted sound into a VEHC full entry's NONR/VSRT: new SBAK, the VSVH
    with the entry's own ID at +0, and no GRBK. Returns what was replaced."""
    from dsfforza.build import clone_vehicle as cv
    from dsfforza.formats.vehicles import find

    nonr = find(full.children, "NONR")
    at = next(n for n, c in enumerate(nonr.children) if c.tag == "VSRT")
    vsrt = copy.deepcopy(nonr.children[at])
    old = find(vsrt.children, "VSVH")
    old_vsvh = old.payload if old.payload is not None else cv.read_leaf(stream, old)
    v = bytearray(vsvh)
    v[0:4] = old_vsvh[0:4]
    old.payload = bytes(v)
    find(vsrt.children, "SBAK").payload = sbak
    had_grbk = any(c.tag == "GRBK" for c in vsrt.children)
    vsrt.children = [c for c in vsrt.children if c.tag != "GRBK"]
    nonr.children[at] = vsrt
    return {"dropped_grbk": had_grbk}


def summary(report: dict) -> dict:
    """The batch report's per-car sound line."""
    if not report.get("ok"):
        return {"forza": False, "reason": report.get("reason")}
    return {
        "forza": True,
        "template": report["template"],
        "bank_id": report["bank_id"],
        "sbak_bytes": report["sbak_bytes"],
        "max_seconds": report["max_seconds"],
        "idle_redline": report["vsvh_rpm_range"],
        "engine_type": report["engine_type"]["state"],
        **{
            f"set_{k}": {
                "emission": s["emission"],
                "bank": s["bank"],
                "loops_used": len(s["loops"]),
                "loops_available": s["available"],
                "rpms": [row["rpm"] for row in s["loops"]],
                "pitch": report["pitch"][k]["pitch"],
                "pitch_main": report["pitch"][k].get("pitch_main"),
            }
            for k, s in report["sets"].items()
        },
    }


# --------------------------------------------------------------------------- lint


def lint_bank(bank: wb.Bank, vsvh: bytes) -> list[str]:
    """Problems of a vehicle bank with its VSVH: every event field resolves to an
    Event of the bank whose actions are in the bank and whose Play/Stop targets
    exist (in the bank or not: shared objects live in other banks)."""
    problems = []
    objs = bank.by_id()
    for off in EVENT_FIELDS:
        v = struct.unpack_from("<I", vsvh, off)[0]
        if not v:
            continue
        ev = objs.get(v)
        if ev is None or ev.type != 4:
            problems.append(f"VSVH+{off:02X} {v:08x} is not an Event of bank {bank.bank_id:08x}")
            continue
        missing = [a for a in ev.info["actions"] if a not in objs or objs[a].type != 3]
        if missing:
            problems.append(f"VSVH+{off:02X} event {v:08x}: actions {[f'{a:08x}' for a in missing]} not in the bank")
    for name, off in zip("AB", VSVH_EVENTS):
        v = struct.unpack_from("<I", vsvh, off)[0]
        if v and play_target(bank, v) not in objs:
            problems.append(f"set {name} (VSVH+{off:02X}) plays nothing in the bank")
    return problems


def _content(bank: wb.Bank) -> dict[int, bytes]:
    """ID -> content digest for everything a bank defines. Masked, as they differ
    between stock banks sharing an object: references to the bank's own ID (Sound
    file IDs, Play actions' bank), a Sound's media offset (where the media sits in
    this bank file) and an ActorMixer's children list (each bank lists only its
    own children of a shared mixer)."""
    out = {bank.bank_id: hashlib.sha1(bank.build()).digest()}
    for mid, wem in bank.media:
        out[mid] = hashlib.sha1(wem).digest()
    own = struct.pack("<I", bank.bank_id)
    for o in bank.objects:
        body = bytearray(o.body)
        for at in o.info["refs"]:
            if body[at : at + 4] == own:
                body[at : at + 4] = b"\0\0\0\0"
        if "offset_at" in o.info:
            body[o.info["offset_at"] : o.info["offset_at"] + 4] = b"\0\0\0\0"
        if o.type == 7:
            body = body[: len(body) - 4 - 4 * len(o.info["children"])]
        out[o.id] = hashlib.sha1(bytes([o.type]) + bytes(body)).digest()
        for layer in o.info.get("layers", []):
            out[layer["id"]] = out[o.id]
    return out


def is_forza_bank(bank: wb.Bank) -> bool:
    try:
        return any(name.startswith("dsfforza") for bid, name in bank.stid() if bid == bank.bank_id)
    except StopIteration:
        return False


def _sound_records(stream, root_entries) -> dict:
    """lint's per-archive tables: "records" [(spool ID, VSRT parts or None)] of every
    kind-0 entry, "banks" {spool ID: (parsed SBAK, VSVH)} of those whose bank parses."""
    from dsfforza.build import clone_vehicle as cv
    from dsfforza.formats.vehicles import find

    records, banks = [], {}
    for sid, index, kind in cv.sslp_records(stream, root_entries):
        if kind != 0:
            continue
        nonr = find(root_entries[index].children, "NONR")
        vsrt = next((c for c in nonr.children if c.tag == "VSRT"), None)
        if vsrt is None:
            records.append((sid, None))
            continue
        parts = {c.tag: cv.read_leaf(stream, c) for c in vsrt.children}
        records.append((sid, parts))
        with contextlib.suppress(ValueError, KeyError, struct.error):
            banks[sid] = (wb.Bank.parse(parts["SBAK"]), parts["VSVH"])
    return {"records": records, "banks": banks}


def lint(
    stream, root_entries, spool_id: int, reserved: set[int] | None = None, cache: dict | None = None
) -> list[tuple[bool, str]]:
    """Checks of a vehicle's engine sound in an archive: VSRT parts, the bank's parse and
    byte-identical rebuild, media/offset consistency, VSVH event fields, and ID
    uniqueness across the archive. A dsfforza bank's IDs (bank, objects, layers,
    media) must be its own: no other vehicle's bank and no reserved (stock or
    WWiseDefault.pck) ID may equal them. Any other bank may share IDs only with
    banks that give them the same content (clones and sound hosts share their
    template's bank, as stock cars do). `cache` (a dict kept between calls for the
    same archive): every vehicle's bank parsed once, for linting many cars."""
    if cache is None:
        cache = {}
    if "records" not in cache:
        cache.update(_sound_records(stream, root_entries))
    rows: list[tuple[bool, str]] = []
    own = None
    for sid, parts in cache["records"]:
        if sid == spool_id:
            if parts is None:
                return [(False, "sound: the full entry has no NONR/VSRT")]
            own = parts
            break
    # Every other vehicle's bank that parses, in archive order (one that doesn't is
    # another car's problem; its own lint reports it).
    banks = {sid: parsed for sid, parsed in cache["banks"].items() if sid != spool_id}
    if own is None:
        return [(False, f"sound: no kind-0 entry for spool {spool_id}")]
    vsvh = own.get("VSVH", b"")
    rows.append(
        (
            len(vsvh) == VSVH_SIZE and struct.unpack_from("<I", vsvh, 0)[0] == spool_id,
            f"sound: VSVH is {VSVH_SIZE:#x} bytes with the spool ID at +0",
        )
    )
    try:
        bank = wb.Bank.parse(own["SBAK"])
    except (ValueError, KeyError, struct.error) as error:
        rows.append((False, f"sound: SBAK does not parse or rebuild byte-identically ({error})"))
        return rows
    rows.append(
        (
            True,
            f"sound: bank {bank.bank_id:08x} parses (v{bank.header[0]}, {len(bank.objects)} objects, "
            f"{len(bank.media)} media) and rebuilds byte-identically",
        )
    )
    problems = bank.validate()
    rows.append(
        (
            not problems,
            "sound: every Sound's offset/size points at its media, whole ADPCM blocks"
            + (": " + "; ".join(problems[:3]) if problems else ""),
        )
    )
    problems = lint_bank(bank, vsvh) if len(vsvh) == VSVH_SIZE else ["short VSVH"]
    rows.append(
        (
            not problems,
            "sound: VSVH event fields resolve to Events of the bank"
            + (": " + "; ".join(problems[:3]) if problems else ""),
        )
    )
    forza = is_forza_bank(bank)
    mine = _content(bank)
    clashes, shared = [], set()
    # Only banks that share an ID with this one can clash or share: the others' content
    # once per archive, indexed by ID.
    if "by_id" not in cache:
        cache["content"] = {sid: _content(other) for sid, (other, _) in cache["banks"].items()}
        by_id: dict[int, set[int]] = {}
        for sid, content in cache["content"].items():
            for i in content:
                by_id.setdefault(i, set()).add(sid)
        cache["by_id"] = by_id
    near = set().union(*(cache["by_id"].get(i, ()) for i in mine))
    for sid in (sid for sid in banks if sid in near):
        theirs = cache["content"][sid]
        for i in set(mine) & set(theirs):
            if forza or mine[i] != theirs[i]:
                clashes.append(f"{i:08x} with {sid}")
            else:
                shared.add(sid)
    if forza:
        lo, hi = struct.unpack_from("<II", vsvh, 0x94)
        rows.append((0 < lo < 5000 and lo < hi < 15000, f"sound: rpm range {lo}..{hi} within the VSVH+94/+98 rules"))
        sets = loop_sets(bank, vsvh)
        rows.append(
            (all(sets.values()), f"sound: both loop sets hold embedded loops ({len(sets['A'])}+{len(sets['B'])})")
        )
        extra = set_media(bank, vsvh) - {s.media for v in sets.values() for s in v}
        rows.append(
            (
                not extra,
                "sound: the loop sets play no media but the rpm loops (no template idle grains)"
                + (f" ({len(extra)} more)" if extra else ""),
            )
        )
        rows.append(
            (
                not clashes,
                "sound: Forza bank IDs are unique in the archive"
                + (f" (clashes: {', '.join(clashes[:4])})" if clashes else f" ({len(mine)} IDs)"),
            )
        )
        if reserved is not None:
            hit = sorted(i for i in mine if i in reserved)
            rows.append(
                (
                    not hit,
                    "sound: no Forza bank ID is a stock or WWiseDefault.pck ID"
                    + (f" ({', '.join(f'{i:08x}' for i in hit[:4])})" if hit else ""),
                )
            )
    else:
        rows.append(
            (
                not clashes,
                "sound: IDs shared with other vehicles' banks have the same content"
                + (f" (shared with {sorted(shared)[:6]})" if shared and not clashes else "")
                + (f" (differ: {', '.join(clashes[:4])})" if clashes else ""),
            )
        )
    return rows


# --------------------------------------------------------------------------- apply (CLI)


def apply(archive: Path, target: Path, spool: int, sound: Path) -> dict:
    from dsfforza.build import clone_vehicle as cv
    from dsfforza.build import port_car
    from dsfforza.formats.vehicles import find

    sbak = (sound / "SBAK.bin").read_bytes()
    vsvh_new = (sound / "VSVH.bin").read_bytes()
    with archive.open("rb") as s:
        root = read_chunk(s, 0)
        entries = root.entries
        nonr_limit, mdxn_max = port_car.stock_limits(s, entries)
        done = []
        for sid, index, kind in cv.sslp_records(s, entries):
            if sid - 1000 * kind != spool:
                continue
            vehc = entries[index]
            if not any(c.tag == "VSRT" for c in find(vehc.children, "NONR").children):
                continue
            result = install(vehc, sbak, vsvh_new, s)
            sizes, problems = port_car.check_entry(vehc, kind, nonr_limit, mdxn_max)
            if problems:
                raise SystemExit(f"spool {sid} kind {kind}: " + "; ".join(problems))
            done.append(
                {"spool": sid, "kind": kind, "nonr": hex(sizes["NONR"]), "nonr_limit": hex(nonr_limit[kind]), **result}
            )
        if not done:
            raise SystemExit(f"no VEHC with a VSRT for spool {spool}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            write_chunk(out, s, entries, root.version)
    return {"archive": str(target), "entries": done}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert")
    c.add_argument("--car", required=True, help="Forza media name, e.g. MAZ_RX7_97")
    c.add_argument("--game", choices=("fh1", "fm4"), required=True)
    c.add_argument("--host", type=int, default=227, help="stock sample-bank vehicle whose bank is the template")
    c.add_argument("--spool", type=int, default=1, help="spool ID the sound is for (the fresh-ID namespace)")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--dsf", type=Path, default=DEFAULT_DSF)
    c.add_argument("--iso", type=Path, action="append")
    c.add_argument("--cache", type=Path, default=CACHE)
    c.add_argument(
        "--set-a",
        choices=sorted(EMISSION_GROUP),
        help="emission for set A (VSVH+04, emitter A at the engine marker; default EngineAmbient)",
    )
    c.add_argument(
        "--set-b",
        choices=sorted(EMISSION_GROUP),
        help="emission for set B (VSVH+0C, emitter B at the exhaust marker; default Exhaust)",
    )
    c.add_argument("--max-seconds", type=float, default=MAX_SECONDS[0], help="longest loop kept (NONR budget)")
    c.add_argument("--engine-type", type=int, help="VSVH+4C value (default: the template's)")
    a = sub.add_parser("apply")
    a.add_argument("archive", type=Path)
    a.add_argument("target", type=Path)
    a.add_argument("--spool", type=int, required=True, help="base spool ID of the ported car")
    a.add_argument("--sound", type=Path, required=True, help="a convert --out folder")
    s = sub.add_parser("sets")
    s.add_argument("host", type=int)
    s.add_argument("--dsf", type=Path, default=DEFAULT_DSF)
    t = sub.add_parser("templates")
    t.add_argument("--dsf", type=Path, default=DEFAULT_DSF)
    li = sub.add_parser("lint")
    li.add_argument("archive", type=Path)
    li.add_argument("--id", type=int, required=True, action="append")
    li.add_argument("--stock", type=Path, help="stock archive: also check Forza IDs against the reserved ones")
    args = ap.parse_args(argv)

    if args.cmd == "convert":
        sbak, vsvh = stock_sound(args.dsf, args.host)
        isos = args.iso or [REPO / "assets" / n for n in GAME_ISOS[args.game]]
        forza = forza_engine(args.car, args.game, isos, args.cache)
        reserved = stock_index(args.dsf)["reserved"]
        sound = build_sound(
            sbak,
            vsvh,
            forza,
            namespace=args.spool,
            reserved=reserved,
            tag=f"dsfforza:{args.host}",
            emissions={"A": args.set_a, "B": args.set_b},
            max_seconds=args.max_seconds,
            engine=args.engine_type,
            bank_name=f"dsfforza_{args.game}_{args.car}",
        )
        sound.report.update(car=args.car, game=args.game, template=args.host)
        sound.write(args.out)
        r = sound.report
        for name, st in r["sets"].items():
            print(
                f"set {name}: {st['emission']} from {st['bank']} (gain {st['gain']}, pitch {r['pitch'][name]['pitch']})"
            )
            for row in st["loops"]:
                print(
                    f"  slot {row['slot']} template {row['template_rpm']:>5} rpm -> {row['forza']:>12} "
                    f"{row['rpm']:>5} rpm {row['seconds']:.2f}s {row['bytes']:#x} B"
                )
        print(
            f"SBAK {r['sbak_bytes']:#x} (template {r['template_sbak_bytes']:#x}), bank {r['bank_id']}, "
            f"VSVH rpm {r['vsvh_rpm_range']}, curves {r['curves']}, ids {r['ids_remapped']} -> {args.out}"
        )
    elif args.cmd == "apply":
        print(json.dumps(apply(args.archive, args.target, args.spool, args.sound), indent=1))
    elif args.cmd == "templates":
        index = stock_index(args.dsf)
        print(f"{len(index['reserved'])} reserved IDs (pck: {index['pck']})")
        for sid, row in sorted(index["banks"].items()):
            sets = "+".join(str(len(row["sets"].get(k, []))) for k in "AB")
            print(
                f"{sid:4} {row['name'][:24]:24} {sets:6} SBAK {row['sbak']:#8x} rpm {row['rpm']} "
                f"type {ENGINE_TYPES.get(row['engine_type'], row['engine_type'])}  {row['why'] or 'template'}"
            )
    elif args.cmd == "lint":
        reserved = stock_index(args.stock)["reserved"] if args.stock else None
        bad = 0
        with args.archive.open("rb") as stream:
            entries = read_chunk(stream, 0).entries
            for sid in args.id:
                for ok, message in lint(stream, entries, sid, reserved):
                    bad += not ok
                    print(f"{sid} " + ("ok    " if ok else "FAIL  ") + message)
        sys.exit(1 if bad else 0)
    else:
        sbak, vsvh = stock_sound(args.dsf, args.host)
        bank = wb.Bank.parse(sbak)
        lo, hi = struct.unpack_from("<II", vsvh, 0x94)
        print(f"bank {bank.bank_id:08x} VSVH rpm range {lo}..{hi}")
        for name, slots in loop_sets(bank, vsvh).items():
            print(f"set {name}: " + ", ".join(f"{s.rpm:.0f} ({s.media:08x}, {len(s.sounds)} sounds)" for s in slots))


if __name__ == "__main__":
    main()
