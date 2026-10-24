"""Read, dump and rebuild the Wwise v48 SoundBanks of Driver: San Francisco.

A vehicle's NONR/VSRT/SBAK is a Wwise bank, bank version 48 (the version
Driver.exe accepts; research/engine-sound.md). Sections: BKHD, DIDX (media
index: id, offset in DATA, size), DATA (the embedded .wem files), HIRC (the
object hierarchy), STID (bank name). The HIRC parser follows the v48 paths of
bnnm's wwiser (parser/wparser.py) and checks that every object parses to
exactly its stored size, which is the structural check of a rebuilt bank.

Media are RIFF/WAVE files with format tag 0x0002, Wwise IMA ADPCM: blocks of
0x24 bytes per channel, a 4-byte header (s16 sample, u8 step index, u8 0) and
63 nibbles, low nibble first, the 64th nibble unused; 64 samples per block.
`decode_ima`/`encode_ima` implement it.

A Sound object stores the media's absolute offset in the bank file and its
size (uFileOffset, uInMemoryMediaSize) next to the media ID, so replacing
media means rewriting DATA, DIDX and every Sound that uses them;
`Bank.replace_media` does that.

Commands:
    uv run dsff wwise_bank info  out/sound/stock/227/SBAK.bin
    uv run dsff wwise_bank tree  out/sound/stock/227/SBAK.bin [--curves]
    uv run dsff wwise_bank wavs  out/sound/stock/227/SBAK.bin out/sound/wav/227
    uv run dsff wwise_bank check out/sound/stock/*/SBAK.bin   # parse + byte-identical rebuild
"""

from __future__ import annotations

import argparse
import itertools
import struct
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

VERSION = 48
HIRC_TYPES = {
    1: "State",
    2: "Sound",
    3: "Action",
    4: "Event",
    5: "RanSeq",
    6: "Switch",
    7: "ActorMixer",
    8: "Bus",
    9: "Layer",
    14: "Attenuation",
}
PARAMS = {0: "Volume", 1: "LFE", 2: "Pitch", 3: "LPF", 8: "Priority"}
ACTIONS = {
    0x04011: "Play",
    0x01011: "Stop_E_O",
    0x01010: "Stop_E",
    0x60001: "SetSwitch",
    0x61001: "SetRTPC",
    0x12020: "SetState",
    0x02011: "Pause_E_O",
    0x03011: "Resume_E_O",
}
# Game parameter / switch IDs Driver.exe sets (research/engine-sound.md; names by FNV-1 match).
KNOWN_IDS = {
    0x9C4D0121: "vehicle_rpm",
    0x623BCC90: "vehicle_engineload",
    0x5979CECB: "vehicle_speed",
    0x2324D4E3: "vehicle_gear",
    0x70D23559: "vehicle_damage",
    0xBF71C30C: "audio_mode",
    0xD2D77B8F: "sw_front_rear",
    0x4B21A032: "front",
    0x99C183C7: "rear",
    0x24B047F7: "sw_engine_type",
    0xD70E7AE8: "rpm_norm",
    0x453DD047: "vehicle_player_rpm",
    0x654A9706: "vehicle_player_engineload",
    0x651A92C1: "vehicle_player_speed",
    0xB1A08B47: "vehicle_player_model",
    0xBE567793: "granular_load",
    0xD4E9EA67: "listener_dist",
    0xC0DFB1CD: "approach",
    0x83449B11: "distance",
}

STEPS = [
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    16,
    17,
    19,
    21,
    23,
    25,
    28,
    31,
    34,
    37,
    41,
    45,
    50,
    55,
    60,
    66,
    73,
    80,
    88,
    97,
    107,
    118,
    130,
    143,
    157,
    173,
    190,
    209,
    230,
    253,
    279,
    307,
    337,
    371,
    408,
    449,
    494,
    544,
    598,
    658,
    724,
    796,
    876,
    963,
    1060,
    1166,
    1282,
    1411,
    1552,
    1707,
    1878,
    2066,
    2272,
    2499,
    2749,
    3024,
    3327,
    3660,
    4026,
    4428,
    4871,
    5358,
    5894,
    6484,
    7132,
    7845,
    8630,
    9493,
    10442,
    11487,
    12635,
    13899,
    15289,
    16818,
    18500,
    20350,
    22385,
    24623,
    27086,
    29794,
    32767,
]
INDEX_ADJ = [-1, -1, -1, -1, 2, 4, 6, 8]
BLOCK = 0x24  # bytes per channel per block
BLOCK_SAMPLES = 64


def name(x: int) -> str:
    return KNOWN_IDS.get(x, f"{x:08x}")


# --------------------------------------------------------------------------- IMA ADPCM


def _expand(nib: int, pred: int, idx: int) -> tuple[int, int]:
    # Multiply form, (2n+1)*step/8: matches vgmstream's Wwise IMA decode sample for sample
    # (the shift-and-add form differs by up to 14).
    step = STEPS[idx]
    diff = ((nib & 7) * 2 + 1) * step >> 3
    pred = pred - diff if nib & 8 else pred + diff
    pred = max(-32768, min(32767, pred))
    idx = max(0, min(88, idx + INDEX_ADJ[nib & 7]))
    return pred, idx


# _expand (one sample) as tables: the prediction step per step index and nibble magnitude,
# and the next step index; decode_ima and _encode_block inline them (the same integers).
_DIFF = [[((m * 2 + 1) * step) >> 3 for m in range(8)] for step in STEPS]
_NEXT = [[max(0, min(88, i + INDEX_ADJ[m])) for m in range(8)] for i in range(len(STEPS))]


def decode_ima(data: bytes, channels: int = 1) -> list[list[int]]:
    """Decode Wwise IMA ADPCM; returns one sample list per channel."""
    return [c.tolist() for c in decode_ima_arrays(data, channels)]


def decode_ima_arrays(data: bytes, channels: int = 1) -> list:
    """decode_ima as one int64 array per channel. Every block starts from its own
    header, so all blocks are decoded at once, nibble by nibble."""
    import numpy as np

    span = BLOCK * channels
    count = len(data) // span
    if not count:
        return [np.zeros(0, dtype=np.int64) for _ in range(channels)]
    raw = np.frombuffer(data, dtype=np.uint8, count=count * span).reshape(count * channels, BLOCK)
    pred = raw[:, :2].copy().view("<i2")[:, 0].astype(np.int64)
    idx = np.minimum(raw[:, 2], 88).astype(np.int64)
    body = raw[:, 4:].astype(np.int64)
    nibs = np.empty((len(raw), BLOCK_SAMPLES), dtype=np.int64)
    nibs[:, 0::2], nibs[:, 1::2] = body & 0xF, body >> 4
    diff, step_next = np.array(_DIFF, dtype=np.int64), np.array(_NEXT, dtype=np.int64)
    out = np.empty((len(raw), BLOCK_SAMPLES), dtype=np.int64)
    out[:, 0] = pred
    for i in range(BLOCK_SAMPLES - 1):  # the 64th nibble is unused
        nib = nibs[:, i]
        m = nib & 7
        d = diff[idx, m]
        pred = np.where(nib & 8, pred - d, pred + d)
        np.clip(pred, -32768, 32767, out=pred)
        idx = step_next[idx, m]
        out[:, i + 1] = pred
    out = out.reshape(count, channels, BLOCK_SAMPLES)
    return [out[:, ch].ravel() for ch in range(channels)]


def _encode_block(x: list[int], idx: int) -> tuple[bytes, int, float]:
    pred = x[0]
    nibs = []
    err = 0.0
    for i in range(1, BLOCK_SAMPLES):
        step = STEPS[idx]
        xi = x[i]
        d = xi - pred
        mag = min(7, (abs(d) * 4) // step) if step else 0  # nearest of (2m+1)*step/8 ~ m*step/4
        if d < 0:
            pred -= _DIFF[idx][mag]
            nibs.append(8 | mag)
        else:
            pred += _DIFF[idx][mag]
            nibs.append(mag)
        pred = -32768 if pred < -32768 else (32767 if pred > 32767 else pred)
        idx = _NEXT[idx][mag]
        err += (xi - pred) ** 2
    nibs.append(0)
    body = bytes(nibs[k] | (nibs[k + 1] << 4) for k in range(0, BLOCK_SAMPLES, 2))
    return body, idx, err


def _encode_blocks(x, start):
    """_encode_block for many (block, start index) lanes at once, sample by sample
    with the same integers: x (L, 64) int64 samples, start (L,) step indices ->
    (nibbles (L, 64) with the unused 64th 0, end step index (L,), error (L,))."""
    import numpy as np

    # STEPS has no 0, so _encode_block's `if step else 0` never applies. The
    # magnitude (abs(d) * 4) // step is taken from a float division: for these
    # integers (below 2**18 over 7..32767) the quotient never rounds up to the
    # next integer, so its truncation is the floor.
    steps = np.array(STEPS, dtype=np.float64)
    diff, step_next = np.array(_DIFF, dtype=np.int64).ravel(), np.array(_NEXT, dtype=np.int64).ravel()
    xt = np.ascontiguousarray(x.T)  # (64, L): one sample of every lane per row
    pred = xt[0].copy()
    idx = np.array(start, dtype=np.int64)
    err = np.zeros(len(pred))
    nibs = np.zeros((BLOCK_SAMPLES, len(pred)), dtype=np.int64)
    for i in range(1, BLOCK_SAMPLES):
        xi = xt[i]
        d = xi - pred
        mag = (np.abs(d) * 4 / steps[idx]).astype(np.int64)
        np.minimum(mag, 7, out=mag)
        neg = d < 0
        cell = idx * 8 + mag
        step = diff[cell]
        np.negative(step, out=step, where=neg)
        pred += step
        np.clip(pred, -32768, 32767, out=pred)
        nibs[i - 1] = mag
        nibs[i - 1][neg] |= 8
        idx = step_next[cell]
        e = xi - pred
        err += e * e  # int64 squares added as floats, as `err += (xi - pred) ** 2` does
    return nibs.T, idx, err


# The start step indices encode_ima tries after a block that ended at index i.
_NEAR = [sorted({max(0, min(88, i + d)) for d in (-8, -4, -2, 0, 2, 4, 8)}) for i in range(len(STEPS))]
ENCODE_ROUNDS = 24  # most guess rounds of encode_ima_many (then block by block)


def _clip16(samples):
    """[max(-32768, min(32767, int(v))) for v in samples] as an int64 array: at
    once for integer arrays and lists of integers, else one by one."""
    import numpy as np

    try:
        a = np.asarray(samples)
    except (OverflowError, ValueError, TypeError):
        a = None
    if a is None or a.ndim != 1 or not (a.dtype.kind == "i" or (a.dtype.kind == "u" and a.dtype.itemsize <= 4)):
        return np.array([max(-32768, min(32767, int(v))) for v in samples], dtype=np.int64)
    return np.clip(a.astype(np.int64), -32768, 32767)


def _pcm16(samples, n: int):
    """encode_ima's samples clipped to 16 bits and zero-padded to n, as int64."""
    import numpy as np

    out = np.zeros(n, dtype=np.int64)
    out[: len(samples)] = _clip16(samples)
    return out


def encode_ima(samples: list[int]) -> bytes:
    """Mono Wwise IMA ADPCM. Pads with zeros to whole 64-sample blocks. Each
    block's start step index is the best of a few around the running one; the
    first block tries every index (from index 0 a loud sound spent its first
    block climbing from the smallest step: 21 dB SNR there on average over 61
    converted Forza loops, 37 dB with the search)."""
    return encode_ima_many([samples])[0]


def encode_ima_many(sounds: list) -> list[bytes]:
    """encode_ima for each of `sounds`, with the same bytes.

    The loop is sequential: a block's candidates are _NEAR[the index the best
    candidate of the block before ended at]. But a block's best end index
    depends little on which index it starts from, so it is guessed: every
    block's candidates under a guessed running index are encoded at once
    (_encode_blocks, all blocks of all sounds), each block's best end becomes the
    next block's guess, and that repeats until the guesses stop changing (the
    first blocks' guesses are right from the first round on, the next ones from
    the next round, and so on). The results go in a table per block and start
    index. Then the loop runs block by block on the table, encoding any
    candidate the guesses missed with _encode_block, and the chosen blocks are
    encoded once more for their nibbles."""
    import numpy as np

    lengths = [-(-len(s) // BLOCK_SAMPLES) for s in sounds]
    if not sum(lengths):
        return [b"" for _ in sounds]
    x = np.concatenate([_pcm16(s, k * BLOCK_SAMPLES) for s, k in zip(sounds, lengths)]).reshape(-1, BLOCK_SAMPLES)
    count, states = len(x), len(STEPS)
    first = np.zeros(count, dtype=bool)
    first[np.cumsum([0, *lengths[:-1]])[np.array(lengths) > 0]] = True
    ends = np.full((count, states), -1, dtype=np.int64)
    errs = np.zeros((count, states))

    def fill(blocks, starts):
        key = np.unique(blocks * states + starts)
        blocks, starts = key // states, key % states
        _, end, err = _encode_blocks(x[blocks], starts)
        ends[blocks, starts], errs[blocks, starts] = end, err

    heads = np.flatnonzero(first)
    fill(np.repeat(heads, states), np.tile(np.arange(states), len(heads)))
    starts = np.zeros(count, dtype=np.int64)
    starts[heads] = np.argmin(errs[heads], axis=1)  # the first of the smallest, as `<` keeps it
    rest = np.flatnonzero(~first)
    settled = True
    if len(rest):
        near = np.array([c + c[-1:] * (7 - len(c)) for c in _NEAR], dtype=np.int64)  # padded with its last
        best_end = np.zeros(count, dtype=np.int64)  # per block: its best candidate's end index
        best_end[heads] = ends[heads, starts[heads]]
        # First guess: every block starts where its sound's first block ended.
        head_of = np.maximum.accumulate(np.where(first, np.arange(count), 0))
        guess = best_end[head_of[rest]]
        settled = False
        for _ in range(ENCODE_ROUNDS):
            cand = near[guess]
            rows = np.broadcast_to(rest[:, None], cand.shape)
            todo = ends[rows, cand] < 0
            if todo.any():
                fill(rows[todo], cand[todo])
            starts[rest] = cand[np.arange(len(rest)), np.argmin(errs[rows, cand], axis=1)]
            best_end[rest] = ends[rest, starts[rest]]
            new = best_end[rest - 1]
            if np.array_equal(new, guess):
                # Every guess is the end of the block before it under its own
                # guess, and the first blocks are exact: so all are the loop's.
                settled = True
                break
            guess = new
    if not settled:  # the loop itself, on the table
        end_rows, err_rows, x_rows = ends.tolist(), errs.tolist(), x.tolist()
        idx = None
        for b in range(count):
            if first[b]:
                idx = None
            best = None
            for c in range(states) if idx is None else _NEAR[idx]:
                end, err = end_rows[b][c], err_rows[b][c]
                if end < 0:  # a candidate the guesses missed
                    _, end, err = _encode_block(x_rows[b], c)
                if best is None or err < best[1]:
                    best = (end, err, c)
            idx = best[0]
            starts[b] = best[2]
    nibs, _, _ = _encode_blocks(x, starts)
    out = np.zeros((count, BLOCK), dtype=np.uint8)
    out[:, :2] = x[:, :1].astype("<i2").view(np.uint8)
    out[:, 2] = starts
    out[:, 4:] = nibs[:, 0::2] | (nibs[:, 1::2] << 4)
    data = out.tobytes()
    bounds = np.cumsum([0, *lengths]) * BLOCK
    return [data[bounds[i] : bounds[i + 1]] for i in range(len(sounds))]


# --------------------------------------------------------------------------- WEM (RIFF)


@dataclass
class Wem:
    fmt: bytes
    chunks: list[tuple[bytes, bytes]]  # every chunk, in order

    @property
    def tag(self) -> int:
        return struct.unpack_from("<H", self.fmt)[0]

    @property
    def channels(self) -> int:
        return struct.unpack_from("<H", self.fmt, 2)[0]

    @property
    def rate(self) -> int:
        return struct.unpack_from("<I", self.fmt, 4)[0]

    @property
    def data(self) -> bytes:
        return next(d for t, d in self.chunks if t == b"data")

    @classmethod
    def parse(cls, raw: bytes) -> Wem:
        if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            raise ValueError("not a RIFF/WAVE wem")
        chunks, p = [], 12
        while p + 8 <= len(raw):
            tag, size = raw[p : p + 4], struct.unpack_from("<I", raw, p + 4)[0]
            if p + 8 + size > len(raw):
                raise ValueError(f"wem chunk {tag!r} at {p:#x}: {size:#x} bytes, past the end")
            chunks.append((tag, raw[p + 8 : p + 8 + size]))
            p += 8 + size + (size & 1)
        fmt = next(d for t, d in chunks if t == b"fmt ")
        return cls(fmt, chunks)

    def build(self) -> bytes:
        body = b"".join(t + struct.pack("<I", len(d)) + d + (b"\0" if len(d) & 1 else b"") for t, d in self.chunks)
        return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body

    def with_data(self, data: bytes, rate: int | None = None) -> Wem:
        fmt = bytearray(self.fmt)
        if rate is not None:
            struct.pack_into("<I", fmt, 4, rate)
            struct.pack_into("<I", fmt, 8, rate * BLOCK * self.channels // BLOCK_SAMPLES)
        chunks = [(t, data if t == b"data" else (bytes(fmt) if t == b"fmt " else d)) for t, d in self.chunks]
        return Wem(bytes(fmt), chunks)

    def pcm(self) -> list[list[int]]:
        if self.tag != 2:
            raise ValueError(f"codec {self.tag:#x} not supported")
        return decode_ima(self.data, self.channels)

    def pcm_arrays(self) -> list:
        """pcm() as one int64 array per channel."""
        if self.tag != 2:
            raise ValueError(f"codec {self.tag:#x} not supported")
        return decode_ima_arrays(self.data, self.channels)


def write_wav(path: Path, channels: list[list[int]], rate: int) -> None:
    import numpy as np

    n = min(len(c) for c in channels)
    frames = np.zeros((n, len(channels)), dtype="<i2")
    for k, c in enumerate(channels):  # int() then the clamp, as the samples were packed one by one
        frames[:, k] = _clip16(c[:n])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(len(channels))
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())


# --------------------------------------------------------------------------- HIRC v48


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos
        self.refs: list[int] = []  # body offsets of u32 object/media/bank references

    def ref(self) -> int:
        self.refs.append(self.p)
        return self.take("I")

    def take(self, fmt: str):
        v = struct.unpack_from("<" + fmt, self.d, self.p)
        self.p += struct.calcsize("<" + fmt)
        return v if len(v) > 1 else v[0]

    def skip(self, n: int) -> None:
        if self.p + n > len(self.d):
            raise ValueError("skip past end")
        self.p += n

    def curve(self, n: int) -> list[tuple[float, float, int]]:
        return [self.take("ffI") for _ in range(n)]


def _fx(r: Reader) -> int:
    r.take("B")
    n = r.take("B")
    if n:
        r.take("B")
        for _ in range(n):
            r.take("BIB")
            r.skip(r.take("I"))
            r.skip(8 * r.take("I"))
    return n


def _rtpcs(r: Reader) -> list[dict]:
    out = []
    for _ in range(r.take("H")):
        fx, _rendered, rid, param, curve_id, scaling, n = r.take("IBIIIBH")
        out.append(
            {
                "fx": fx,
                "rtpc": rid,
                "param": param,
                "curve_id": curve_id,
                "scaling": scaling,
                "at": r.p,
                "points": r.curve(n),
            }
        )
    return out


def _positioning(r: Reader) -> dict:
    bits = r.take("B")
    pos = {"override": bits & 1}
    if bits & 1:
        r.take("iff")
        has3d = r.take("B")
        if not has3d:
            r.take("B")
        else:
            etype = r.take("I")
            att = r.ref()
            r.take("B")
            pos["attenuation"] = att
            if etype == 2:
                r.take("IBiB")
                r.skip(16 * r.take("I"))
                items = r.take("I")
                r.skip(8 * items + 8 * items)
            elif etype == 3:
                r.take("B")
    return pos


def _node_base(r: Reader, feedback: bool) -> dict:
    nfx = _fx(r)
    bus = r.take("I")
    parent = r.ref()
    r.take("BBBb")
    init = r.take("12f")
    r.take("I")
    pos = _positioning(r)
    r.skip(7)  # AdvSettingsParams
    r.take("B")
    states = [r.take("IBI") for _ in range(r.take("H"))]
    rtpcs = _rtpcs(r)
    if feedback and r.take("I"):
        r.skip(24)
    return {
        "fx": nfx,
        "bus": bus,
        "parent": parent,
        "volume": init[0],
        "pitch": init[6],
        "lpf": init[9],
        "positioning": pos,
        "states": states,
        "rtpcs": rtpcs,
    }


def _children(r: Reader) -> list[int]:
    return [r.ref() for _ in range(r.take("I"))]


@dataclass
class HircObject:
    type: int
    id: int
    body: bytes  # the bytes after the ID
    info: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return HIRC_TYPES.get(self.type, f"type{self.type}")

    def build(self) -> bytes:
        return struct.pack("<III", self.type, 4 + len(self.body), self.id) + self.body


def parse_object(obj: HircObject, feedback: bool) -> dict:
    """Parse a v48 HIRC object body; raises unless it consumes exactly the body."""
    r = Reader(obj.body)
    t = obj.type
    info: dict = {}
    if t == 2:
        plugin, stream = r.take("II")
        info.update(plugin=plugin, stream=stream)
        info["source"] = r.ref()
        info["file"] = r.ref()
        if stream != 1:
            info["offset_at"] = r.p
            info["offset"], info["size"] = r.take("II")
        r.take("B")
        if (plugin & 0xF) in (2, 5):
            r.skip(r.take("I"))
        info.update(_node_base(r, feedback))
        info["loop"] = r.take("hhh")[0]
    elif t == 3:
        info["action"] = r.take("I")
        info["target"] = r.ref()
        r.take("iii")
        r.skip(r.take("I"))
        if info["action"] >> 12 in (4, 5):  # Play: the bank ID follows the sub-section
            info["bank"] = r.ref()
    elif t == 4:
        info["actions"] = [r.ref() for _ in range(r.take("I"))]
    elif t == 5:
        info.update(_node_base(r, feedback))
        info["loop"] = r.take("H")
        r.take("fffH")
        info["transition_mode"], info["random_mode"], info["mode"] = r.take("BBB")
        r.skip(5)
        info["children"] = _children(r)
        info["playlist"] = [(r.ref(), r.take("B")) for _ in range(r.take("H"))]
    elif t == 6:
        info.update(_node_base(r, feedback))
        info["group_type"], info["group"], info["default"] = r.take("III")
        r.take("B")
        info["children"] = _children(r)
        info["switches"] = []
        for _ in range(r.take("I")):
            sw = r.take("I")
            info["switches"].append((sw, [r.ref() for _ in range(r.take("I"))]))
        info["switch_params"] = [(r.ref(), *r.take("BBIii")) for _ in range(r.take("I"))]
    elif t == 7:
        info.update(_node_base(r, feedback))
        info["children"] = _children(r)
    elif t == 9:
        info.update(_node_base(r, feedback))
        info["children"] = _children(r)
        info["layers"] = []
        for _ in range(r.take("I")):
            layer = {"id": r.ref(), "rtpcs": _rtpcs(r)}
            layer["xfade_rtpc"], layer["xfade_default"] = r.take("If")
            layer["assoc"] = []
            for _ in range(r.take("I")):
                child = r.ref()
                n = r.take("I")
                layer["assoc"].append((child, r.curve(n), r.p - 12 * n))
            info["layers"].append(layer)
    elif t == 14:
        if r.take("B") & 1:
            r.skip(16)
        info["curve_use"] = r.take("5b")
        info["curves"] = []
        for _ in range(r.take("B")):
            scaling, n = r.take("BH")
            info["curves"].append((scaling, r.curve(n)))
        info["rtpcs"] = _rtpcs(r)
    else:
        raise ValueError(f"HIRC type {t} not handled")
    if r.p != len(obj.body):
        raise ValueError(f"{obj.kind} {obj.id:08x}: parsed {r.p:#x} of {len(obj.body):#x} bytes")
    info["refs"] = r.refs
    return info


# --------------------------------------------------------------------------- bank


@dataclass
class Bank:
    sections: list[list]  # [tag, payload bytes], in file order; DATA/HIRC are rebuilt from the fields below
    media: list[list]  # [id, wem bytes], in DIDX order
    objects: list[HircObject]
    data_align: int = 16

    @classmethod
    def parse(cls, raw: bytes) -> Bank:
        sections, p = [], 0
        while p + 8 <= len(raw):
            tag, size = raw[p : p + 4].decode("latin-1"), struct.unpack_from("<I", raw, p + 4)[0]
            if p + 8 + size > len(raw):
                raise ValueError(f"section {tag} at {p:#x}: {size:#x} bytes, past the end")
            sections.append([tag, raw[p + 8 : p + 8 + size]])
            p += 8 + size
        if p != len(raw):
            raise ValueError(f"trailing bytes at {p:#x}")
        sec = dict(sections)
        version, _bank_id, _lang, feedback = struct.unpack_from("<IIII", sec["BKHD"])
        if version != VERSION:
            raise ValueError(f"bank version {version}, expected {VERSION}")
        media = []
        didx, data = sec.get("DIDX", b""), sec.get("DATA", b"")
        for i in range(0, len(didx), 12):
            mid, off, size = struct.unpack_from("<III", didx, i)
            if off + size > len(data):
                raise ValueError(f"media {mid:08x}: {off:#x}+{size:#x} outside DATA ({len(data):#x} bytes)")
            media.append([mid, data[off : off + size]])
        objects = []
        if "HIRC" in sec:
            h = sec["HIRC"]
            q = 4
            for _ in range(struct.unpack_from("<I", h)[0]):
                t, size, oid = struct.unpack_from("<III", h, q)
                obj = HircObject(t, oid, h[q + 12 : q + 8 + size])
                obj.info = parse_object(obj, bool(feedback))
                objects.append(obj)
                q += 8 + size
            if q != len(h):
                raise ValueError("HIRC size mismatch")
        bank = cls(sections, media, objects)
        if bank.build() != raw:
            raise ValueError("rebuild is not byte-identical")
        return bank

    @property
    def header(self) -> tuple[int, int, int, int]:
        return struct.unpack_from("<IIII", self.section("BKHD"))

    @property
    def bank_id(self) -> int:
        return self.header[1]

    def section(self, tag: str) -> bytes:
        return next(d for t, d in self.sections if t == tag)

    def by_id(self) -> dict[int, HircObject]:
        return {o.id: o for o in self.objects}

    def _layout(self) -> tuple[bytes, bytes, dict[int, tuple[int, int]]]:
        data, didx, where = bytearray(), bytearray(), {}
        for mid, wem in self.media:
            data += b"\0" * (-len(data) % self.data_align)
            where[mid] = (len(data), len(wem))
            didx += struct.pack("<III", mid, len(data), len(wem))
            data += wem
        return bytes(didx), bytes(data), where

    def build(self) -> bytes:
        didx, data, where = self._layout()
        # absolute file offset of the DATA payload
        data_start = 0
        for tag, payload in self.sections:
            if tag == "DATA":
                data_start += 8
                break
            data_start += 8 + (len(didx) if tag == "DIDX" else len(payload))
        bank_id = self.bank_id
        for o in self.objects:
            i = o.info
            if o.type == 2 and i.get("file") == bank_id and "offset_at" in i and i["source"] in where:
                off, size = where[i["source"]]
                body = bytearray(o.body)
                struct.pack_into("<II", body, i["offset_at"], data_start + off, size)
                o.body = bytes(body)
                i["offset"], i["size"] = data_start + off, size
        hirc = struct.pack("<I", len(self.objects)) + b"".join(o.build() for o in self.objects)
        out = bytearray()
        for tag, payload in self.sections:
            payload = {"DIDX": didx, "DATA": data, "HIRC": hirc}.get(tag, payload)
            out += tag.encode("latin-1") + struct.pack("<I", len(payload)) + payload
        return bytes(out)

    def replace_media(self, new: dict[int, bytes]) -> None:
        """Swap the wem bytes of the given media IDs (offsets fixed on build)."""
        ids = {m[0] for m in self.media}
        missing = set(new) - ids
        if missing:
            raise KeyError(f"media not in bank: {sorted(missing)}")
        for m in self.media:
            if m[0] in new:
                m[1] = new[m[0]]

    def stid(self) -> list[tuple[int, str]]:
        d = self.section("STID")
        _type, n = struct.unpack_from("<II", d)
        out, p = [], 8
        for _ in range(n):
            bid, ln = struct.unpack_from("<IB", d, p)
            out.append((bid, d[p + 5 : p + 5 + ln].decode("latin-1")))
            p += 5 + ln
        return out

    def set_bank_id(self, new_id: int, new_name: str | None = None) -> None:
        """New bank ID in BKHD, STID and every in-bank reference (Sound file IDs, Play actions)."""
        old = self.bank_id
        self.remap_ids({old: new_id}, objects=False)
        for sec in self.sections:
            if sec[0] == "BKHD":
                d = bytearray(sec[1])
                struct.pack_into("<I", d, 4, new_id)
                sec[1] = bytes(d)
            if sec[0] == "STID":
                entries = [(new_id if b == old else b, (new_name or n) if b == old else n) for b, n in self.stid()]
                sec[1] = struct.pack("<II", 1, len(entries)) + b"".join(
                    struct.pack("<IB", b, len(n)) + n.encode("latin-1") for b, n in entries
                )

    def remap_ids(self, mapping: dict[int, int], objects: bool = True) -> int:
        """Rewrite object IDs, media IDs and every recorded reference found in ``mapping``.
        Returns the number of values changed."""
        changed = 0
        for m in self.media:
            if m[0] in mapping:
                m[0] = mapping[m[0]]
                changed += 1
        for o in self.objects:
            if objects and o.id in mapping:
                o.id = mapping[o.id]
                changed += 1
            body = bytearray(o.body)
            for at in o.info["refs"]:
                v = struct.unpack_from("<I", body, at)[0]
                if v in mapping:
                    struct.pack_into("<I", body, at, mapping[v])
                    changed += 1
            o.body = bytes(body)
        self.reparse()
        return changed

    def reparse(self) -> None:
        feedback = bool(self.header[3])
        for o in self.objects:
            o.info = parse_object(o, feedback)

    def set_points(self, obj: HircObject, at: int, points: list[tuple[float, float, int]]) -> None:
        """Overwrite a curve's points in place (same count)."""
        body = bytearray(obj.body)
        for k, (x, y, interp) in enumerate(points):
            struct.pack_into("<ffI", body, at + 12 * k, x, y, interp)
        obj.body = bytes(body)

    def validate(self) -> list[str]:
        """Consistency checks a rebuilt bank must pass; returns problems."""
        problems = []
        raw = self.build()
        again = Bank.parse(raw)
        didx = dict(again.media)
        for o in again.objects:
            i = o.info
            if o.type != 2 or i["file"] != again.bank_id:
                continue
            if i["source"] not in didx:
                problems.append(f"Sound {o.id:08x}: media {i['source']:08x} not in DIDX")
                continue
            wem = didx[i["source"]]
            if raw[i["offset"] : i["offset"] + i["size"]] != wem:
                problems.append(f"Sound {o.id:08x}: offset/size do not point at its media")
        for mid, wem in again.media:
            try:
                w = Wem.parse(wem)
                if w.tag == 2 and len(w.data) % (BLOCK * w.channels):
                    problems.append(f"media {mid:08x}: data not whole ADPCM blocks")
            except ValueError as e:
                problems.append(f"media {mid:08x}: {e}")
        return problems


# --------------------------------------------------------------------------- reports


def sound_rpm(o: HircObject) -> float | None:
    """Recording rpm implied by a Sound's pitch-vs-vehicle_rpm curve (0 cents point)."""
    for rt in o.info.get("rtpcs", []):
        if rt["rtpc"] == 0x9C4D0121 and rt["param"] == 2:
            pts = rt["points"]
            for (x0, y0, _), (x1, y1, _) in itertools.pairwise(pts):
                if y0 <= 0 <= y1 and y1 != y0:
                    return x0 + (x1 - x0) * (0 - y0) / (y1 - y0)
    return None


def describe_rtpcs(rtpcs: list[dict], curves: bool) -> str:
    parts = []
    for rt in rtpcs:
        pts = rt["points"]
        s = f"{PARAMS.get(rt['param'], rt['param'])}({name(rt['rtpc'])})"
        if curves:
            s += "[" + " ".join(f"{x:g}:{y:g}" for x, y, _ in pts) + "]"
        else:
            s += f"[{pts[0][0]:g}..{pts[-1][0]:g} -> {min(p[1] for p in pts):g}..{max(p[1] for p in pts):g}]"
        parts.append(s)
    return " ".join(parts)


def tree(bank: Bank, curves: bool = False, out=sys.stdout) -> None:
    objs = bank.by_id()
    media = dict(bank.media)
    kids: dict[int, list[int]] = {}
    for o in bank.objects:
        if "parent" in o.info and o.info["parent"] in objs:
            kids.setdefault(o.info["parent"], []).append(o.id)

    def line(o: HircObject) -> str:
        i = o.info
        s = f"{o.kind} {o.id:08x}"
        if o.type == 2:
            if i["plugin"] == 0x00020001 and i["source"] in media:
                w = Wem.parse(media[i["source"]])
                n = len(w.data) // (BLOCK * w.channels) * BLOCK_SAMPLES
                s += f" media {i['source']:08x} {w.rate}Hz {n / w.rate:.2f}s"
            else:
                s += f" plugin {i['plugin']:08x}"
            rpm = sound_rpm(o)
            if rpm:
                s += f" rec_rpm {rpm:.0f}"
            s += f" loop {i['loop']}"
        if o.type == 6:
            s += f" group {name(i['group'])} default {name(i['default'])}"
        if o.type == 5:
            s += f" mode {i['mode']} random {i['random_mode']} loop {i['loop']}"
        if "volume" in i and (i["volume"] or i["pitch"]):
            s += f" vol {i['volume']:g} pitch {i['pitch']:g}"
        if i.get("rtpcs"):
            s += " | " + describe_rtpcs(i["rtpcs"], curves)
        return s

    def walk(oid: int, depth: int, assoc: str = "") -> None:
        o = objs[oid]
        print("  " * depth + assoc + line(o), file=out)
        i = o.info
        if o.type == 6:
            for sw, nodes in i["switches"]:
                print("  " * (depth + 1) + f"case {name(sw)}: " + " ".join(f"{n:08x}" for n in nodes), file=out)
        xf = {}
        for layer in i.get("layers", []):
            for child, pts, _ in layer["assoc"]:
                xf[child] = (
                    f"layer {layer['id']:08x} xfade {name(layer['xfade_rtpc'])}["
                    + " ".join(f"{x:g}:{y:g}" for x, y, _ in pts)
                    + "] "
                )
            if layer["rtpcs"]:
                print(
                    "  " * (depth + 1)
                    + f"layer {layer['id']:08x} xfade {name(layer['xfade_rtpc'])} | "
                    + describe_rtpcs(layer["rtpcs"], curves),
                    file=out,
                )
        for c in i.get("children", []):
            if c in objs:
                walk(c, depth + 1, xf.get(c, ""))
            else:
                print("  " * (depth + 1) + f"(external {c:08x})", file=out)

    print(f"bank {bank.bank_id:08x} v{bank.header[0]}  media {len(bank.media)}  objects {len(bank.objects)}", file=out)
    print("events:", file=out)
    for o in bank.objects:
        if o.type == 4:
            acts = []
            for a in o.info["actions"]:
                act = objs.get(a)
                if act:
                    acts.append(
                        f"{ACTIONS.get(act.info['action'], hex(act.info['action']))}"
                        f"({name(act.info['target'])}{'' if act.info['target'] in objs else ' ext'})"
                    )
            print(f"  {o.id:08x}: " + ", ".join(acts), file=out)
    print("hierarchy:", file=out)
    roots = [o.id for o in bank.objects if o.type in (2, 5, 6, 7, 9) and o.info["parent"] not in objs]
    for r in roots:
        walk(r, 1)
    att = [o for o in bank.objects if o.type == 14]
    if att:
        print(f"attenuations: {len(att)}", file=out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info")
    p.add_argument("bank", type=Path)
    p = sub.add_parser("tree")
    p.add_argument("bank", type=Path)
    p.add_argument("--curves", action="store_true")
    p = sub.add_parser("wavs")
    p.add_argument("bank", type=Path)
    p.add_argument("out", type=Path)
    p = sub.add_parser("check")
    p.add_argument("banks", type=Path, nargs="+")
    a = ap.parse_args(argv)

    if a.cmd == "check":
        bad = 0
        for path in a.banks:
            try:
                b = Bank.parse(path.read_bytes())
                problems = b.validate()
            except Exception as e:
                problems = [str(e)]
            if problems:
                bad += 1
                print(f"{path}: " + "; ".join(problems))
        print(f"{len(a.banks) - bad}/{len(a.banks)} banks parse, rebuild byte-identically and validate")
        sys.exit(1 if bad else 0)

    bank = Bank.parse(a.bank.read_bytes())
    if a.cmd == "info":
        v, bid, lang, fb = bank.header
        print(f"version {v} bank {bid:08x} language {lang} feedback {fb}")
        for tag, payload in bank.sections:
            print(f"  {tag} {len(payload):#x}")
        counts: dict[str, int] = {}
        for o in bank.objects:
            counts[o.kind] = counts.get(o.kind, 0) + 1
        print("  objects", counts)
        for mid, wem in bank.media:
            w = Wem.parse(wem)
            n = len(w.data) // (BLOCK * w.channels) * BLOCK_SAMPLES if w.tag == 2 else 0
            print(f"  media {mid:08x} {len(wem):#7x} codec {w.tag:#06x} {w.channels}ch {w.rate}Hz {n / w.rate:.3f}s")
    elif a.cmd == "tree":
        tree(bank, a.curves)
    elif a.cmd == "wavs":
        a.out.mkdir(parents=True, exist_ok=True)
        rpm = {}
        for o in bank.objects:
            if o.type == 2 and sound_rpm(o):
                rpm.setdefault(o.info["source"], sound_rpm(o))
        for k, (mid, wem) in enumerate(bank.media):
            w = Wem.parse(wem)
            tag = f"_{rpm[mid]:.0f}rpm" if mid in rpm else ""
            write_wav(a.out / f"{k:02d}_{mid:08x}{tag}.wav", w.pcm(), w.rate)
        print(f"{len(bank.media)} wavs -> {a.out}")


if __name__ == "__main__":
    main()
