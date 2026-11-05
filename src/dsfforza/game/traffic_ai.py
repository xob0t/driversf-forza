"""The ambient traffic tables of media/san_franciscoAI.fchunk, and the edits the car manager
makes to a copy of it (research/traffic.md, sections 1b and 3b).

A set ("progression", 59 of them) has per frequency (0..4) a spool list: the models the game
loads when the set is selected (0x0076FDF0 -> the traffic spooler, 0x005A79F0). Each car's
model is drawn per road (0x00916870): the road's record for the active set (section 0x128,
(set.index & 0xFF) + road * 59, 0x14 bytes; byte 0x0D + frequency) names a weight range
(section 0x158, {start, count, f32 total}) over the {u32 model, f32 weight} pairs of section
0x160. Every range spans the same model columns. The models with weight on a set's roads are
exactly its spool list in the vanilla file; check() keeps that true for every set after an edit.

AIEX payload sections (directory at +0x50, {u32 count, u32 offset}):

    0x118  progressions, 28 bytes: u32 hash, u16 start[5], u16 count[5], u32 index
    0x120  u16 spool IDs
    0x128  (road, set) records, 0x14 bytes
    0x158  ranges, 12 bytes
    0x160  pairs, 8 bytes

Writing: the grown tables (spool IDs, pairs) go past the old end of AIEX and only their
directory entries change, so no other byte of AIEX moves; AIEX grows by a multiple of 0x800 and
the sizes and offsets after it shift by that much (the container: file CHNK -> AIRT -> NONR ->
AIEX, AIRF). The repo's generic CHNK writer is not byte-exact on this file, so the fields are
edited in place.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

PROGRESSIONS, SPOOL_IDS, ROADS, RANGES, PAIRS = 0x118, 0x120, 0x128, 0x158, 0x160
ROAD_RECORD = 0x14
SETS = 59  # (road, set) records come in rows of one per set
FREQUENCIES = 5
# 0x0076FDF0 copies a set's list into a stack buffer of 40 IDs with no bound
MAX_SET_MODELS = 40
# sha256 of the vanilla Driver: San Francisco 1.04 media/san_franciscoAI.fchunk (the game as
# installed from the retail release, read on the test PC and in the VMs)
VANILLA_SHA256 = "da5b1ca3e4e51b6c8d937ed0eb2cbeb2a54048c9aca5a3e5a26f8704b1f0b936"
# the container (offsets of the fields edited when AIEX grows)
FILE_TOTAL, AIRT_SIZE, AIRT_TOTAL, NONR_SIZE, NONR_TOTAL = 0x04, 0x1C, 0x804, 0x81C, 0x1004
AIEX_OFFSET, AIEX_SIZE, AIRF_OFFSET = 0x1014, 0x101C, 0x1024
NONR = 0x1000
GROW_ALIGN = 0x800


@dataclass
class TrafficSet:
    index: int
    hash: int
    name: str | None
    lists: dict[int, list[int]]  # frequency -> spool IDs
    record_byte: int  # index & 0xFF: the set's column in the (road, set) records
    raw_index: int


@dataclass
class Edits:
    """What changed, for the report."""

    lists: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)  # (set, f) -> (before, after)
    ranges: set[int] = field(default_factory=set)


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class AiTraffic:
    def __init__(self, data: bytes, names: dict[int, str] | None = None):
        self.file = bytes(data)
        f = self.file
        if f[0:4] != b"CHNK" or f[0x800:0x804] != b"CHNK" or f[NONR : NONR + 4] != b"CHNK":
            raise ValueError("not san_franciscoAI.fchunk: no CHNK -> AIRT -> NONR")
        if f[0x810:0x814] != b"NONR" or f[0x1010:0x1014] != b"AIEX" or f[0x1020:0x1024] != b"AIRF":
            raise ValueError("not san_franciscoAI.fchunk: no NONR with AIEX and AIRF")
        self.aiex_offset = NONR + _u32(f, AIEX_OFFSET)
        self.aiex_size = _u32(f, AIEX_SIZE)
        d = f[self.aiex_offset : self.aiex_offset + self.aiex_size]
        self.aiex = d
        names = names or {}
        pc, po = self._section(PROGRESSIONS)
        vc, vo = self._section(SPOOL_IDS)
        ids = struct.unpack_from(f"<{vc}H", d, vo)
        self.sets: list[TrafficSet] = []
        for p in range(pc):
            h, *rest = struct.unpack_from("<I5H5HI", d, po + 28 * p)
            starts, counts, idx = rest[:5], rest[5:10], rest[10]
            lists = {fr: list(ids[starts[fr] : starts[fr] + counts[fr]]) for fr in range(FREQUENCIES) if counts[fr]}
            self.sets.append(TrafficSet(p, h, names.get(h), lists, idx & 0xFF, idx))
        self.road_count, self.road_offset = self._section(ROADS)
        rc, ro = self._section(RANGES)
        wc, wo = self._section(PAIRS)
        self.ranges = [struct.unpack_from("<IIf", d, ro + 12 * i) for i in range(rc)]
        pairs = [struct.unpack_from("<If", d, wo + 8 * i) for i in range(wc)]
        ncol = self.ranges[0][1]
        self.columns: list[int] = [pairs[self.ranges[0][0] + j][0] for j in range(ncol)]
        self.weights: list[list[float]] = []
        for start, count, _total in self.ranges:
            if count != ncol or [pairs[start + j][0] for j in range(count)] != self.columns:
                raise ValueError("the weight ranges don't share one column layout")
            self.weights.append([pairs[start + j][1] for j in range(count)])
        self._road_ranges: dict[tuple[int, int], set[int]] = {}
        self.edits = Edits()

    @classmethod
    def read(cls, path: Path, names: dict[int, str] | None = None) -> AiTraffic:
        return cls(Path(path).read_bytes(), names)

    def _section(self, slot: int) -> tuple[int, int]:
        return struct.unpack_from("<II", self.aiex, slot)

    # --- reading ---

    def by_name(self, name: str) -> TrafficSet:
        for s in self.sets:
            if name in (s.name, f"{s.hash:08X}"):
                return s
        raise KeyError(name)

    def road_ranges(self, s: TrafficSet, fr: int) -> set[int]:
        """The weight ranges the set's roads use at that frequency."""
        key = (s.index, fr)
        if key not in self._road_ranges:
            base = self.road_offset
            self._road_ranges[key] = {
                self.aiex[base + ROAD_RECORD * i + 0x0D + fr] for i in range(s.record_byte, self.road_count, SETS)
            }
        return self._road_ranges[key]

    def users(self, ranges: set[int]) -> list[tuple[TrafficSet, int]]:
        """Every (set, frequency) whose roads use one of these ranges."""
        return [(s, fr) for s in self.sets for fr in s.lists if self.road_ranges(s, fr) & ranges]

    def weighted(self, r: int) -> dict[int, float]:
        """{model: weight} of range r, the models with weight only (a column may repeat a model)."""
        out: dict[int, float] = {}
        for m, w in zip(self.columns, self.weights[r]):
            if w > 0:
                out[m] = out.get(m, 0.0) + w
        return out

    def share(self, s: TrafficSet, fr: int = 0) -> dict[int, float]:
        """Each model's share of the set's roads, every road counted once (the mean over its
        ranges of weight / total, each range weighted by its number of roads)."""
        base = self.road_offset
        counts: dict[int, int] = {}
        for i in range(s.record_byte, self.road_count, SETS):
            r = self.aiex[base + ROAD_RECORD * i + 0x0D + fr]
            counts[r] = counts.get(r, 0) + 1
        roads = sum(n for r, n in counts.items() if sum(self.weights[r]) > 0)
        out: dict[int, float] = {}
        for r, n in counts.items():
            total = sum(self.weights[r])
            if total > 0:
                for m, w in self.weighted(r).items():
                    out[m] = out.get(m, 0.0) + n * w / total / roads
        return out

    # --- edits ---

    def _column(self, model: int) -> int:
        """The column of `model`, a new one (zero weight everywhere) when it has none with weight."""
        for j, m in enumerate(self.columns):
            if m == model:
                return j
        self.columns.append(model)
        for w in self.weights:
            w.append(0.0)
        return len(self.columns) - 1

    def set_list(self, s: TrafficSet, fr: int, models: list[int]) -> None:
        before = len(s.lists.get(fr, []))
        s.lists[fr] = list(dict.fromkeys(models))
        self.edits.lists[(s.index, fr)] = (self.edits.lists.get((s.index, fr), (before,))[0], len(s.lists[fr]))

    def set_range(self, r: int, weights: dict[int, float]) -> None:
        """Range r's weights become `weights` ({model: weight}); every other column 0."""
        cols = {m: self._column(m) for m in weights}
        row = [0.0] * len(self.columns)
        for m, w in weights.items():
            row[cols[m]] = float(w)
        self.weights[r] = row
        self.edits.ranges.add(r)

    # --- writing ---

    def check(self) -> list[str]:
        """Problems that would make the game weight a model it doesn't load, or overrun the
        set selector's buffer."""
        out = []
        for s in self.sets:
            for fr, lst in s.lists.items():
                if len(lst) > MAX_SET_MODELS:
                    out.append(f"{s.name or f'{s.hash:08X}'} f{fr}: {len(lst)} models, the game takes {MAX_SET_MODELS}")
                weighted = {m for r in self.road_ranges(s, fr) for m in self.weighted(r)}
                if not weighted <= set(lst):
                    out.append(
                        f"{s.name or f'{s.hash:08X}'} f{fr}: weighted but not loaded: {sorted(weighted - set(lst))}"
                    )
        return out

    def to_bytes(self) -> bytes:
        problems = self.check()
        if problems:
            raise ValueError("; ".join(problems))
        d = bytearray(self.aiex)
        _count, po = self._section(PROGRESSIONS)
        ids: list[int] = []
        for s in self.sets:
            h = struct.unpack_from("<I", d, po + 28 * s.index)[0]
            starts, counts = [0] * FREQUENCIES, [0] * FREQUENCIES
            for fr in range(FREQUENCIES):
                lst = s.lists.get(fr, [])
                if lst:
                    starts[fr], counts[fr] = len(ids), len(lst)
                    ids += lst
            struct.pack_into("<I5H5HI", d, po + 28 * s.index, h, *starts, *counts, s.raw_index)
        if len(ids) > 0xFFFF:
            raise ValueError(f"{len(ids)} spool IDs: a set's start is a u16")
        base = (len(d) + 15) & ~15
        tail = bytearray(b"\0" * (base - len(d)))
        ids_at = base
        tail += struct.pack(f"<{len(ids)}H", *ids)
        tail += b"\0" * (-len(tail) % 16)
        pairs_at = len(d) + len(tail)
        n = len(self.columns)
        _count, ro = self._section(RANGES)
        for r, w in enumerate(self.weights):
            struct.pack_into("<IIf", d, ro + 12 * r, r * n, n, sum(w))
            tail += b"".join(struct.pack("<If", c, x) for c, x in zip(self.columns, w))
        struct.pack_into("<II", d, SPOOL_IDS, len(ids), ids_at)
        struct.pack_into("<II", d, PAIRS, n * len(self.weights), pairs_at)
        delta = -(-len(tail) // GROW_ALIGN) * GROW_ALIGN
        tail += b"\0" * (delta - len(tail))
        f = self.file
        out = bytearray(f[: self.aiex_offset]) + d + tail + f[self.aiex_offset + self.aiex_size :]
        for at in (AIEX_SIZE, AIRF_OFFSET, NONR_TOTAL, NONR_SIZE, AIRT_TOTAL, AIRT_SIZE, FILE_TOTAL):
            struct.pack_into("<I", out, at, _u32(f, at) + delta)
        return bytes(out)


def _u32(b: bytes, at: int) -> int:
    return struct.unpack_from("<I", b, at)[0]


def known_names() -> dict[int, str]:
    """{hash: set name} for the names the game's Lua uses (traffic_sets.KNOWN)."""
    from dsfforza.game import traffic_sets

    return {traffic_sets.name_hash(n): n for n in traffic_sets.KNOWN}
