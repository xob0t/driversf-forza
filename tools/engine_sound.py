"""Give a ported car its own Forza engine sound (media swap into a host Wwise bank).

DSF drives a vehicle's engine with two sets of rpm-layered loops in its
Wwise bank (research/engine-sound.md): set A is played by the event at
VSVH+04 on emitter A, set B by VSVH+0C on emitter B. Each loop is a Sound
with a pitch curve 1200*log2(rpm / recording rpm) on vehicle_rpm and a
layer crossfade on vehicle_rpm. The game maps the car's normalised rpm
(VO3D p40..p42) onto VSVH+94..+98, the bank's rpm range.

`convert` keeps a stock host bank's hierarchy (events, switches, layers,
load and speed RTPCs) and replaces the loops of both sets:

1. Forza: <car>_ET.xml gives idle/redline and the HT file per emission;
   the HT file gives the FSB bank and every loop's rpm (tools/forza_audio.py).
   One emission per set (default A = engine ambience, since emitter A sits at
   the engine; B = exhaust, emitter B at the exhaust marker); as many
   loops as the set has slots are picked, spread over idle..redline.
2. Each loop is cut to at most --max-seconds with a crossfaded seam, resampled
   (periodic FFT resample) to 32 kHz and a whole number of 64-sample ADPCM
   blocks, scaled to the host set's loudness and encoded to Wwise IMA ADPCM.
3. The pitch curves are recomputed for the Forza recording rpms, crossfade and
   other vehicle_rpm curves are warped from the host's rpm points to the
   Forza ones, and VSVH+94/+98 become the Forza idle/redline, so bank rpm is
   Forza rpm.
4. Bank ID, every HIRC object, layer and media ID get fresh IDs (Wwise
   indexes them globally: a copy with the host's IDs would play the host's
   media whenever the host car is loaded first), and the VSVH event fields
   follow.

Outputs in --out: SBAK.bin, VSVH.bin, report.json, wav/ (every new loop as
the game will decode it) and sweep_{host,new}.wav (an offline render of an
idle -> redline -> idle sweep through both sets' curves, full load).

`apply` writes them into a built archive for one spool ID (kind 0..2 VEHCs
holding a VSRT), checks the NONR budget and writes a new archive.

    python tools/engine_sound.py convert --car MAZ_RX7_97 --game fh1 --host 227 --out out/sound/MAZ_RX7_97
    python tools/engine_sound.py apply out/sound/build/dngvehicles.sp out/sound/build/dngvehicles.sound.sp \\
        --spool 1 --sound out/sound/MAZ_RX7_97
    python tools/engine_sound.py sets 227         # a stock bank's loop sets
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wwise_bank as wb  # noqa: E402
from chnk import read_chunk, write_chunk  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DSF = REPO / "out" / "stock" / "Install" / "dngvehicles.sp"
RPM = 0x9C4D0121
RATE = 32000
VSVH_EVENTS = (0x04, 0x0C)            # Play events of set A and set B
GAME_ISOS = {"fh1": ["Forza.Horizon.XBOX360.iso"],
             "fm4": ["Forza 4 [NTSCU][DVD1].iso", "Forza 4 [NTSCU][DVD2].iso"]}
EMISSION_GROUP = {"Exhaust": "ExhaustL", "EngineAmbient": "EngineAmbient", "EngineIntake": "EngineIntake"}


def fnv1(text: str) -> int:
    h = 2166136261
    for c in text.lower().encode():
        h = (h * 16777619) & 0xFFFFFFFF
        h ^= c
    return h


# --------------------------------------------------------------------------- host side

@dataclass
class Slot:
    media: int
    rpm: float
    sounds: list = field(default_factory=list)


def stock_sound(dsf: Path, spool: int) -> tuple[bytes, bytes]:
    """(SBAK, VSVH) of a stock vehicle's kind-0 entry."""
    import clone_vehicle as cv
    from vehicles import find
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
        if objs[a].info["action"] == 0x04011:
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


def set_of_objects(bank: wb.Bank, vsvh: bytes) -> dict[int, str]:
    out = {}
    for name, off in zip("AB", VSVH_EVENTS):
        target = play_target(bank, struct.unpack_from("<I", vsvh, off)[0])
        for o in subtree(bank, target) if target else []:
            out.setdefault(o.id, name)
    return out


# --------------------------------------------------------------------------- Forza side

@dataclass
class ForzaLoop:
    name: str
    rpm: int
    rpm_min: int
    rpm_max: int
    volume: float
    rate: int
    pcm: np.ndarray       # float, channel 0, -1..1


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        ch, rate = w.getnchannels(), w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768
    return x.reshape(-1, ch)[:, 0], rate


def forza_engine(car: str, game: str, isos: list[Path], cache: Path, emissions: list[str]) -> dict:
    """Idle, redline and the decoded loops of the given emissions (L0) of a Forza car."""
    import forza_audio as fa
    sources = [fa.Source(str(p)) for p in isos]
    try:
        tuning = fa.Tuning(sources)
        et = tuning.engine(car)
        out = {"idle": float(et.settings.get("audio_rpm_idle", 0)),
               "redline": float(et.settings.get("audio_rpm_redline", 0)),
               "et": et.file, "emissions": {}}
        vgm = fa.vgmstream_path(None)
        for emission in emissions:
            ht_name = et.upgrades.get(emission, {}).get("L0")
            if not ht_name:
                raise KeyError(f"{car}: no {emission} L0 in {et.file}")
            ht, _ = tuning.harmonic(ht_name)
            hits = [f for s in sources for f in s.find_name(ht.bank) if fa.BANK_DIR in f.path.lower()]
            if not hits:
                raise KeyError(f"{car}: bank {ht.bank} not on the given discs")
            folder = cache / game / Path(ht.bank).stem
            fsb_path = cache / game / ht.bank
            if not fsb_path.is_file():
                fsb_path.parent.mkdir(parents=True, exist_ok=True)
                fsb_path.write_bytes(hits[0].read())
            fsb = fa.parse_fsb(fsb_path.read_bytes())
            wavs = sorted(folder.glob("*.wav")) if folder.is_dir() else []
            if len(wavs) != len(fsb.subsongs):
                wavs = fa.decode_bank(fsb_path, fsb, folder, vgm)
            by_index = {int(w.name.split("_", 1)[0]): w for w in wavs}
            group = EMISSION_GROUP[emission]
            loops = []
            for lp in ht.loops:
                if lp.group != group or lp.index not in by_index:
                    continue
                pcm, rate = read_wav(by_index[lp.index])
                loops.append(ForzaLoop(fsb.subsongs[lp.index].name, lp.rpm_sample, lp.rpm_min, lp.rpm_max,
                                       lp.volume, rate, pcm))
            out["emissions"][emission] = {"ht": ht_name, "bank": ht.bank,
                                          "loops": sorted(loops, key=lambda l: l.rpm)}
        return out
    finally:
        for s in sources:
            s.close()


def pick_loops(loops: list[ForzaLoop], n: int, redline: float) -> list[ForzaLoop]:
    """n loops from the lowest to the highest at or below the redline (+10%),
    chosen to minimise the largest rpm ratio between neighbours (then the sum of
    squared log gaps): each loop is pitched as little as possible before the
    crossfade hands over. Repeats loops when there are fewer than n."""
    from itertools import combinations
    usable = [l for l in loops if l.rpm <= redline * 1.1] or loops
    if len(usable) <= n:
        return sorted((usable * n)[:n], key=lambda l: l.rpm) if len(usable) < n else usable
    logs = [math.log(l.rpm) for l in usable]
    best, pick = None, None
    for mid in combinations(range(1, len(usable) - 1), n - 2):
        idx = (0, *mid, len(usable) - 1)
        gaps = [logs[b] - logs[a] for a, b in zip(idx, idx[1:])]
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
    tail_ref = x[n - xf:]
    best, best_len = -2.0, target
    for length in range(max(xf * 2, target - rate // 10), target + 1):
        seg = x[length - xf:length]
        c = float(np.dot(seg, tail_ref) / (np.linalg.norm(seg) * np.linalg.norm(tail_ref) + 1e-12))
        if c > best:
            best, best_len = c, length
    y = x[:best_len].copy()
    w = np.sin(np.linspace(0, math.pi / 2, xf)) ** 2
    y[best_len - xf:] = y[best_len - xf:] * (1 - w) + tail_ref * w
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
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def warp_map(old: list[float], new: list[float], top: float = 12000.0) -> list[tuple[float, float]]:
    pts = [(0.0, 0.0)] + list(zip(old, new)) + [(top, max(top, new[-1] * 1.05))]
    out = []
    for p in pts:   # keep it strictly increasing
        if not out or (p[0] > out[-1][0] and p[1] > out[-1][1]):
            out.append(p)
    return out


def retune(bank: wb.Bank, vsvh: bytes, sets: dict[str, list[Slot]], new_rpm: dict[int, float],
           lo_hi_old: tuple[float, float], lo_hi_new: tuple[float, float]) -> dict:
    """Pitch curves from the new recording rpms; every other vehicle_rpm curve
    and layer crossfade warped from the old rpm points to the new ones."""
    maps = {name: warp_map([s.rpm for s in slots], [new_rpm[s.media] for s in slots])
            for name, slots in sets.items() if slots}
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
                pts = [(x, -4800.0 if x <= 0 else max(-4800.0, min(4800.0, 1200 * math.log2(x / r))), p[2])
                       for x, p in zip(xs, rt["points"])]
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


def render_sweep(bank: wb.Bank, vsvh: bytes, idle: float, redline: float, seconds=(1.0, 6.0, 1.0, 4.0, 1.0)) -> np.ndarray:
    """Idle, ramp to redline, hold, ramp down, idle; both sets at full load,
    each loop pitched and crossfaded by its own curves."""
    sets = loop_sets(bank, vsvh)
    lo, hi = struct.unpack_from("<II", vsvh, 0x94)
    media = {m: np.array(wb.Wem.parse(w).pcm()[0], dtype=np.float64) / 32768 for m, w in bank.media}
    objs = bank.by_id()
    xfade = {}
    for o in bank.objects:
        for layer in o.info.get("layers", []):
            if layer["xfade_rtpc"] == RPM:
                for child, pts, _ in layer["assoc"]:
                    xfade.setdefault(child, pts)
    hold0, up, hold1, down, hold2 = seconds
    n = int(sum(seconds) * RATE)
    t = np.arange(n) / RATE
    norm = np.interp(t, np.cumsum([0, hold0, up, hold1, down, hold2]), [0, 0, 1, 1, 0, 0])
    bank_rpm = lo + (hi - lo) * norm
    out = np.zeros(n)
    for slots in sets.values():
        for s in slots:
            snd = next((objs[i] for i in s.sounds if i in xfade), objs[s.sounds[0]])
            pitch = next(rt["points"] for rt in snd.info["rtpcs"] if rt["rtpc"] == RPM and rt["param"] == 2)
            grid = np.linspace(0, 12000, 1201)
            cents = np.interp(bank_rpm, grid, [curve_at(pitch, g) for g in grid])
            gain = np.interp(bank_rpm, grid, [curve_at(xfade[snd.id], g) / 255 for g in grid]) \
                if snd.id in xfade else np.ones(n)
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
    """The new bank must be the host's hierarchy under the ID mapping: same objects
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
    if [len(w) > 0 for _, w in host.media] != [len(w) > 0 for _, w in new.media] or             [m.get(i, i) for i, _ in host.media] != [i for i, _ in new.media]:
        problems.append("media list differs")
    return problems


# --------------------------------------------------------------------------- convert

def convert(args) -> dict:
    out = args.out
    (out / "wav").mkdir(parents=True, exist_ok=True)
    for old in (out / "wav").glob("*.wav"):
        old.unlink()
    sbak, vsvh = stock_sound(args.dsf, args.host)
    bank = wb.Bank.parse(sbak)
    vsvh = bytearray(vsvh)
    sets = loop_sets(bank, vsvh)
    if not sets["A"] or not sets["B"]:
        raise SystemExit(f"host {args.host} has no sample loop sets (granular or silent); pick a sample-bank host")
    lo_old, hi_old = struct.unpack_from("<II", vsvh, 0x94)
    host_wavs = {m: np.array(wb.Wem.parse(w).pcm()[0], dtype=np.float64) / 32768 for m, w in bank.media}

    isos = args.iso or [REPO / "assets" / n for n in GAME_ISOS[args.game]]
    emissions = {"A": args.set_a, "B": args.set_b}
    forza = forza_engine(args.car, args.game, isos, args.cache, sorted(set(emissions.values())))
    idle, redline = forza["idle"], forza["redline"]
    if not (0 < idle < 5000 and idle < redline < 15000):
        raise SystemExit(f"{args.car}: idle/redline {idle}/{redline} outside the VSVH+94/+98 rules")

    report = {"car": args.car, "game": args.game, "host": args.host, "idle": idle, "redline": redline,
              "et": forza["et"], "host_rpm_range": [lo_old, hi_old], "sets": {}}
    new_media, new_rpm = {}, {}
    template = wb.Wem.parse(bank.media[0][1])
    for name, slots in sets.items():
        emission = emissions[name]
        loops = forza["emissions"][emission]["loops"]
        chosen = pick_loops(loops, len(slots), redline)
        host_level = max(rms(host_wavs[s.media]) for s in slots)
        cut = [seamless(l.pcm, l.rate, args.max_seconds) for l in chosen]
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
            decoded = np.array(wb.Wem.parse(wem).pcm()[0], dtype=np.float64) / 32768
            write_wav(out / "wav" / f"{name}{k}_{loop.rpm:05d}rpm_{loop.name}.wav", decoded)
            rows.append({"slot": k, "host_media": f"{slot.media:08x}", "host_rpm": round(slot.rpm),
                         "forza": loop.name, "rpm": loop.rpm, "volume": loop.volume,
                         "seconds": round(len(pcm) / RATE, 3), "bytes": len(wem)})
        report["sets"][name] = {"emission": emission, "bank": forza["emissions"][emission]["bank"],
                                "ht": forza["emissions"][emission]["ht"], "gain": round(gain, 3), "loops": rows}

    # host preview before any change
    write_wav(out / "sweep_host.wav", render_sweep(bank, bytes(vsvh), idle, redline))
    bank.replace_media(new_media)
    report["curves"] = retune(bank, bytes(vsvh), sets, new_rpm, (lo_old, hi_old), (idle, redline))
    struct.pack_into("<II", vsvh, 0x94, int(round(idle)), int(round(redline)))

    # fresh IDs for the bank, every object, layer and embedded media
    tag = f"dsfforza:{args.game}:{args.car}:{args.host}"
    ids = {o.id for o in bank.objects} | {m for m, _ in bank.media}
    for o in bank.objects:
        ids |= {layer["id"] for layer in o.info.get("layers", [])}
    mapping = {old: fnv1(f"{tag}:{old:08x}") for old in ids}
    if len(set(mapping.values())) != len(mapping) or set(mapping.values()) & ids:
        raise RuntimeError("new ID collision")
    old_bank = bank.bank_id
    bank.remap_ids(mapping)
    bank_name = f"dsfforza_{args.game}_{args.car}_{args.host}"
    new_bank = fnv1(bank_name)
    if new_bank in ids:
        raise RuntimeError("new bank ID collides")
    bank.set_bank_id(new_bank, bank_name[:60])
    patched = []
    for off in range(4, len(vsvh) - 3, 4):
        v = struct.unpack_from("<I", vsvh, off)[0]
        if v in mapping:
            struct.pack_into("<I", vsvh, off, mapping[v])
            patched.append(f"+{off:02X}")
    raw = bank.build()
    problems = wb.Bank.parse(raw).validate()
    problems += same_structure(wb.Bank.parse(sbak), wb.Bank.parse(raw), mapping, old_bank)
    if problems:
        raise RuntimeError("rebuilt bank invalid: " + "; ".join(problems))
    (out / "SBAK.bin").write_bytes(raw)
    (out / "VSVH.bin").write_bytes(bytes(vsvh))
    write_wav(out / "sweep_new.wav", render_sweep(wb.Bank.parse(raw), bytes(vsvh), idle, redline))
    report.update(bank_id=f"{new_bank:08x}", host_bank_id=f"{old_bank:08x}", ids_remapped=len(mapping),
                  vsvh_fields_remapped=patched, sbak_bytes=len(raw), host_sbak_bytes=len(sbak),
                  vsvh_rpm_range=[int(round(idle)), int(round(redline))])
    (out / "report.json").write_text(json.dumps(report, indent=1))
    return report


# --------------------------------------------------------------------------- apply

def apply(archive: Path, target: Path, spool: int, sound: Path) -> dict:
    import clone_vehicle as cv
    import port_car
    from vehicles import find
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
            nonr = find(vehc.children, "NONR")
            vsrt = next((c for c in nonr.children if c.tag == "VSRT"), None)
            if vsrt is None:
                continue
            old_vsvh = cv.read_leaf(s, find(vsrt.children, "VSVH"))
            v = bytearray(vsvh_new)
            v[0:4] = old_vsvh[0:4]                 # keep this entry's own ID
            find(vsrt.children, "VSVH").payload = bytes(v)
            find(vsrt.children, "SBAK").payload = sbak
            had_grbk = any(c.tag == "GRBK" for c in vsrt.children)
            vsrt.children = [c for c in vsrt.children if c.tag != "GRBK"]
            sizes, problems = port_car.check_entry(vehc, kind, nonr_limit, mdxn_max)
            if problems:
                raise SystemExit(f"spool {sid} kind {kind}: " + "; ".join(problems))
            done.append({"spool": sid, "kind": kind, "nonr": hex(sizes["NONR"]),
                         "nonr_limit": hex(nonr_limit[kind]), "dropped_grbk": had_grbk})
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
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--dsf", type=Path, default=DEFAULT_DSF)
    c.add_argument("--iso", type=Path, action="append")
    c.add_argument("--cache", type=Path, default=REPO / "out" / "cache" / "sound")
    c.add_argument("--set-a", default="EngineAmbient", choices=sorted(EMISSION_GROUP),
                   help="emission for set A (VSVH+04, emitter A at the engine marker)")
    c.add_argument("--set-b", default="Exhaust", choices=sorted(EMISSION_GROUP),
                   help="emission for set B (VSVH+0C, emitter B at the exhaust marker)")
    c.add_argument("--max-seconds", type=float, default=1.1, help="longest loop kept (NONR budget)")
    a = sub.add_parser("apply")
    a.add_argument("archive", type=Path)
    a.add_argument("target", type=Path)
    a.add_argument("--spool", type=int, required=True, help="base spool ID of the ported car")
    a.add_argument("--sound", type=Path, required=True, help="a convert --out folder")
    s = sub.add_parser("sets")
    s.add_argument("host", type=int)
    s.add_argument("--dsf", type=Path, default=DEFAULT_DSF)
    args = ap.parse_args(argv)

    if args.cmd == "convert":
        r = convert(args)
        for name, st in r["sets"].items():
            print(f"set {name}: {st['emission']} from {st['bank']} (gain {st['gain']})")
            for row in st["loops"]:
                print(f"  slot {row['slot']} host {row['host_rpm']:>5} rpm -> {row['forza']:>12} {row['rpm']:>5} rpm "
                      f"{row['seconds']:.2f}s {row['bytes']:#x} B")
        print(f"SBAK {r['sbak_bytes']:#x} (host {r['host_sbak_bytes']:#x}), bank {r['bank_id']}, "
              f"VSVH rpm {r['vsvh_rpm_range']}, curves {r['curves']}, ids {r['ids_remapped']} -> {args.out}")
    elif args.cmd == "apply":
        print(json.dumps(apply(args.archive, args.target, args.spool, args.sound), indent=1))
    else:
        sbak, vsvh = stock_sound(args.dsf, args.host)
        bank = wb.Bank.parse(sbak)
        lo, hi = struct.unpack_from("<II", vsvh, 0x94)
        print(f"bank {bank.bank_id:08x} VSVH rpm range {lo}..{hi}")
        for name, slots in loop_sets(bank, vsvh).items():
            print(f"set {name}: " + ", ".join(f"{s.rpm:.0f} ({s.media:08x}, {len(s.sounds)} sounds)" for s in slots))


if __name__ == "__main__":
    main()
