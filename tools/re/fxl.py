"""Forza's car effects (.fxobj): techniques, passes, render states and Xenos microcode.

research/forza-lenses-re.md. An .fxobj is Forza's header (type 0x101 = FXLite, hash,
size, technique, vertex declaration and stride counts) around an XDK FXLite compiled
effect. The effect starts with a 12-byte header (magic 0xA3D70141, size, size of the
pointer part); every pointer inside it is an offset from the end of that header ("rel").

    +0x200  techniques: rel of an array of technique pointers   +0x20c  their count
    technique: name, pass count, 1, flags, pass pointers
    pass:      name, 0, pass block, render state block, sampler state block
    pass block (0x2240 bytes, mostly runtime scratch): +0x48 vertex shader, +0x4c pixel
               shader (rel of the shader object; its 0x102A110x container follows)
    render state block: 6 dirty-mask dwords, count, (state id, value) pairs
    sampler state block: per-sampler masks, count, (value, sampler << 16 | state) pairs

Render state ids are the Xbox 360 D3DRS values that this file uses (`RENDER_STATES`:
alpha blend enable 0x3c, source and destination blend 0x48 and 0x4c, read from the
glass passes); blend values are the Xenos blend enum (`BLENDS`). A technique whose
render state block is empty takes the blend the game sets for its draw bucket.

Disassembly and emulation are in xenos.py (after xenia's ucode.h); constants are named
from each shader's constant table, literals (the physical part's first bytes) from the top
of the constant file down.

    uv run python tools/re/fxl.py techniques FILE [--match RE]   techniques, passes, states
    uv run python tools/re/fxl.py dis FILE TECHNIQUE [--vs]      a technique's shaders
    uv run python tools/re/fxl.py params FILE TECHNIQUE          its pixel shader constants
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza.formats import fxobj

sys.path.insert(0, str(Path(__file__).parent))
import xenos

MAGIC = b"\xa3\xd7\x01\x41"
# The render states this effect sets (the rest: 0xF0 on the paint passes, 0x134 on geoShadow).
RENDER_STATES = {0x3C: "ALPHABLENDENABLE", 0x48: "SRCBLEND", 0x4C: "DESTBLEND"}
BLENDS = {
    0: "ZERO",
    1: "ONE",
    4: "SRCCOLOR",
    5: "INVSRCCOLOR",
    6: "SRCALPHA",
    7: "INVSRCALPHA",
    8: "DESTCOLOR",
    9: "INVDESTCOLOR",
    10: "DESTALPHA",
    11: "INVDESTALPHA",
}


@dataclass
class Pass:
    name: str
    render_states: dict[int, int]
    samplers: list[tuple[int, int, int]]  # (sampler, state, value)
    vs: fxobj.Shader | None
    ps: fxobj.Shader | None

    def blend(self) -> str:
        rs = self.render_states
        if not rs.get(0x3C):
            return "not set (the game's)"
        src, dst = rs.get(0x48, 1), rs.get(0x4C, 0)
        return f"{BLENDS.get(src, src)}, {BLENDS.get(dst, dst)}"


@dataclass
class Technique:
    name: str
    flags: int
    passes: list[Pass] = field(default_factory=list)


class Effect:
    def __init__(self, data: bytes):
        self.data = data
        self.base = data.index(MAGIC) + 12
        self.containers = {s.offset: s for s in fxobj.shaders(data)}
        self._starts = sorted(self.containers)

    def u32(self, rel: int) -> int:
        return struct.unpack_from(">I", self.data, self.base + rel)[0]

    def string(self, rel: int) -> str:
        a = self.base + rel
        return self.data[a : self.data.index(b"\0", a)].decode("latin-1")

    def shader(self, rel: int) -> fxobj.Shader | None:
        a = self.base + rel
        for s in self._starts:
            if a <= s <= a + 0x20:
                return self.containers[s]
        return None

    def render_states(self, rel: int) -> dict[int, int]:
        n = self.u32(rel + 24)
        return {self.u32(rel + 28 + 8 * k): self.u32(rel + 32 + 8 * k) for k in range(n)}

    def sampler_states(self, rel: int) -> list[tuple[int, int, int]]:
        # the count sits after the mask rows: the first dword < 0x100 followed by (value, s<<16|state)
        for k in range(0, 0x200, 4):
            n = self.u32(rel + k)
            if 0 < n < 0x60 and self.u32(rel + k - 4) == 0:
                pairs = [(self.u32(rel + k + 4 + 8 * j), self.u32(rel + k + 8 + 8 * j)) for j in range(n)]
                if all(p[1] >> 16 < 16 and p[1] & 0xFFFF < 0x60 for p in pairs):
                    return [(p[1] >> 16, p[1] & 0xFFFF, p[0]) for p in pairs]
        return []

    def techniques(self) -> list[Technique]:
        table, count = self.u32(0x200), self.u32(0x20C)
        out = []
        for i in range(count):
            t = self.u32(table + 4 * i)
            tech = Technique(self.string(self.u32(t)), self.u32(t + 12))
            for k in range(self.u32(t + 4)):
                p = self.u32(t + 16 + 4 * k)
                name, _, block, rs, ss = (self.u32(p + 4 * j) for j in range(5))
                tech.passes.append(
                    Pass(
                        self.string(name),
                        self.render_states(rs),
                        self.sampler_states(ss),
                        self.shader(self.u32(block + 0x48)),
                        self.shader(self.u32(block + 0x4C)),
                    )
                )
            out.append(tech)
        return out


def literals(shader: fxobj.Shader, data: bytes) -> dict[int, tuple[float, ...]]:
    """Literal float4 constants: the first bytes of the container's physical part, which its
    definition table places at the top of the constant file (c255 down; ps entry 0x1fc = c252)."""
    h = shader.offset
    vsize, _, _, _, _, record = struct.unpack_from(">6I", data, h + 4)
    poff, _ = struct.unpack_from(">2I", data, h + record)
    n = poff // 16
    return {256 - n + j: struct.unpack_from(">4f", data, h + vsize + 16 * j) for j in range(n)}


def constant_file(shader: fxobj.Shader, data: bytes) -> dict[int, list[float]]:
    """Float registers with their CTAB defaults and literals."""
    regs = {k: list(v) for k, v in literals(shader, data).items()}
    for c in shader.constants.values():
        if c.register_set == "float4":
            for k in range(c.count):
                regs[c.index + k] = list(c.default[4 * k : 4 * k + 4]) if c.default else [0.0] * 4
    return regs


def disassemble(shader: fxobj.Shader, data: bytes) -> list[str]:
    names = fxobj.float_names(shader)
    bools = {c.index: c.name for c in shader.constants.values() if c.register_set == "bool"}
    lits = literals(shader, data)

    def cname(reg: int) -> str:
        if reg in names:
            return f"c{reg}{{{names[reg]}}}"
        if reg in lits:
            return f"c{reg}{{{', '.join(f'{v:g}' for v in lits[reg])}}}"
        return f"c{reg}"

    return xenos.disassemble(shader.code, shader.kind, cname, lambda b: f"b{b}{{{bools.get(b, '?')}}}")


def constants_line(s: fxobj.Shader) -> str:
    return ", ".join(
        f"{c.name}={c.register_set[0]}{c.index}"
        + (f"({', '.join(f'{v:g}' for v in c.default[:4])})" if c.default else "")
        for c in sorted(s.constants.values(), key=lambda c: (c.register_set, c.index))
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("techniques")
    t.add_argument("file", type=Path)
    t.add_argument("--match")
    d = sub.add_parser("dis")
    d.add_argument("file", type=Path)
    d.add_argument("technique")
    d.add_argument("--vs", action="store_true")
    p = sub.add_parser("params")
    p.add_argument("file", type=Path)
    p.add_argument("technique")
    args = ap.parse_args(argv)
    data = args.file.read_bytes()
    fx = Effect(data)
    techs = fx.techniques()
    if args.cmd == "techniques":
        for tech in techs:
            if args.match and not re.search(args.match, tech.name):
                continue
            parts = []
            for p_ in tech.passes:
                ps = f"ps {p_.ps.offset:#x}" if p_.ps else "ps ?"
                parts.append(f"{p_.name} [{p_.blend()}] {ps}")
            print(f"{tech.name}: " + " | ".join(parts))
        return
    tech = next((x for x in techs if x.name == args.technique), None)
    if tech is None:
        sys.exit(f"no technique {args.technique}")
    for p_ in tech.passes:
        print(f"== {tech.name} / {p_.name}: blend {p_.blend()}, render states {p_.render_states}")
        for sh in ([p_.vs] if getattr(args, "vs", False) else []) + [p_.ps]:
            if sh is None:
                continue
            print(f"-- {sh.kind} {sh.offset:#x}: {constants_line(sh)}")
            if args.cmd == "dis":
                print("\n".join(disassemble(sh, data)))


if __name__ == "__main__":
    main()
