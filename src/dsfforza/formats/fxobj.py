"""Shaders in Forza's .fxobj effect files (media/shaders/Cars/shaders_v16.fxobj and the
custom ones): their constant tables and a minimal Xenos (Xbox 360 GPU) microcode
disassembler. research/forza-materials.md, "Forza's trim shaders".

Every compiled shader in the file is an Xbox 360 shader container (big-endian):

    +0   0x102A1100 / 0x102A1101 (flags)
    +4   virtual size (header, constant table, definitions)
    +8   physical size (literal constants, then the microcode)
    +16  offset of the constant table's size dword (the D3D CTAB follows it)
    +24  offset of the shader record: physical offset of the microcode, its size

The constant table is the usual D3DXSHADER_CONSTANTTABLE (big-endian): each
constant's name, register set and index, and its default value, so the
techniques' parameters (`PaintScale`, `SpecularAmount`...) can be read with their
defaults and found in the code by register. The microcode follows xenia's ucode.h:
control-flow instructions (two 48-bit ones per three dwords) whose `exec`s run
ALU (vector + scalar pair) and fetch instructions of three dwords each. The
disassembly covers what reading a material shader needs (ALU operands, swizzles,
write masks, texture fetches, exports); flow control is shown raw.

    uv run dsff fxobj FILE                     # every shader: offset, kind, constants
    uv run dsff fxobj FILE --param PaintScale  # pixel shaders using it, with defaults
    uv run dsff fxobj FILE --shader 0x86914    # one shader's constants and microcode

Not decoded: the effect's own technique/pass tables (which technique uses which
shader); the per-car ShaderSettings.xml says which parameters a technique has.
"""

from __future__ import annotations

import argparse
import re
import struct
from dataclasses import dataclass
from pathlib import Path

MAGIC = re.compile(rb"\x10\x2a\x11[\x00\x01]")
REGISTER_SETS = ("bool", "int4", "float4", "sampler")

VECTOR_OPS = [
    "add",
    "mul",
    "max",
    "min",
    "seq",
    "sgt",
    "sge",
    "sne",
    "frc",
    "trunc",
    "floor",
    "mad",
    "cndeq",
    "cndge",
    "cndgt",
    "dp4",
    "dp3",
    "dp2add",
    "cube",
    "max4",
    "pred_setne_push",
    "pred_setgt_push",
    "pred_setge_push",
    "pred_sete_push",
    "kill_eq",
    "kill_gt",
    "kill_ge",
    "kill_ne",
    "dst",
    "mova",
]
SCALAR_OPS = [
    "adds",
    "adds_prev",
    "muls",
    "muls_prev",
    "muls_prev2",
    "maxs",
    "mins",
    "seqs",
    "sgts",
    "sges",
    "snes",
    "frcs",
    "truncs",
    "floors",
    "exp",
    "log_clamp",
    "log",
    "rcp_clamp",
    "rcp_ff",
    "rcp",
    "rsq_clamp",
    "rsq_ff",
    "rsq",
    "movas",
    "mova_floors",
    "subs",
    "subs_prev",
    "pred_setes",
    "pred_setnes",
    "pred_setgts",
    "pred_setges",
    "pred_set_invs",
    "pred_set_pops",
    "pred_set_clrs",
    "pred_set_restores",
    "kills_eq",
    "kills_gt",
    "kills_ge",
    "kills_ne",
    "kills_one",
    "sqrt",
    "op41",
    "mulsc0",
    "mulsc1",
    "addsc0",
    "addsc1",
    "subsc0",
    "subsc1",
    "sin",
    "cos",
    "retain_prev",
]
CF_OPS = [
    "nop",
    "exec",
    "exec_end",
    "cexec",
    "cexec_end",
    "cpexec",
    "cpexec_end",
    "loop_start",
    "loop_end",
    "ccall",
    "ret",
    "cjmp",
    "alloc",
    "cexec_pc",
    "cexec_pc_end",
    "mark_vs_fetch_done",
]
EXECS = {1, 2, 3, 4, 5, 6, 13, 14}
EXEC_ENDS = {2, 4, 6, 14}


@dataclass
class Constant:
    name: str
    register_set: str
    index: int
    count: int
    default: tuple[float, ...] | None


@dataclass
class Shader:
    offset: int  # of the container
    kind: str  # "vs" or "ps"
    constants: dict[str, Constant]
    code: bytes  # microcode


def _cstr(data: bytes, offset: int) -> str:
    return data[offset : data.index(b"\0", offset)].decode("latin-1")


def constant_table(data: bytes, ctab: int) -> tuple[str, dict[str, Constant]]:
    """(target, {name: Constant}) of the big-endian D3D constant table at `ctab`."""
    _size, _creator, _version, count, info, _flags, target = struct.unpack_from(">7I", data, ctab)
    out = {}
    for k in range(count):
        name, rset, index, rcount, _, _tinfo, default = struct.unpack_from(">IHHHHII", data, ctab + info + 20 * k)
        value = None
        if default and rset == 2:
            value = struct.unpack_from(f">{4 * rcount}f", data, ctab + default)
        cname = _cstr(data, ctab + name)
        out[cname] = Constant(cname, REGISTER_SETS[rset], index, rcount, value)
    return _cstr(data, ctab + target), out


def shaders(data: bytes):
    """Every shader container in an .fxobj (Shader), in file order."""
    for m in MAGIC.finditer(data):
        h = m.start()
        if h + 28 > len(data):
            continue
        vsize, psize, _, ctab_off, _, record = struct.unpack_from(">6I", data, h + 4)
        if not (0 < vsize < 1 << 20 and 0 < psize < 1 << 20 and record + 8 <= vsize and ctab_off + 32 <= vsize):
            continue
        poff, size = struct.unpack_from(">2I", data, h + record)
        try:
            target, constants = constant_table(data, h + ctab_off + 4)
        except (struct.error, ValueError, IndexError):
            continue
        if target[:2] not in ("vs", "ps"):
            continue
        start = h + vsize + poff
        yield Shader(h, target[:2], constants, data[start : start + size])


def _bits(v: int, lo: int, n: int) -> int:
    return (v >> lo) & ((1 << n) - 1)


def _swizzle(s: int, n: int = 4) -> str:
    return "".join("xyzw"[((s >> (2 * i)) + i) & 3] for i in range(n))


def _mask(m: int) -> str:
    return "".join("xyzw"[j] if m >> j & 1 else "_" for j in range(4))


def disassemble(code: bytes, names: dict[int, str] | None = None) -> list[str]:
    """Xenos microcode as text; `names`: float register -> constant name."""
    words = struct.unpack(f">{len(code) // 4}I", code[: len(code) // 4 * 4])
    cfs: list[int] = []
    end = None
    i = 0
    while 3 * i + 3 <= len(words):
        d0, d1, d2 = words[3 * i : 3 * i + 3]
        cfs += [d0 | (d1 & 0xFFFF) << 32, (d1 >> 16) | d2 << 16]
        if end is None:
            end = next((_bits(r, 0, 12) for r in cfs if _bits(r, 44, 4) in EXECS), None)
        i += 1
        if end is not None and i >= end:
            break

    def operand(sel: int, reg: int, neg: int, swz: int) -> str:
        if sel:
            text = f"r{reg & 63}"
            if reg & 128:
                text = f"|{text}|"
        else:
            text = f"c{reg}" + (f"{{{names[reg]}}}" if names and reg in names else "")
        return ("-" if neg else "") + text + "." + _swizzle(swz)

    out = []
    for raw in cfs:
        op = _bits(raw, 44, 4)
        if op == 0:
            continue
        if op not in EXECS:
            out.append(f"  {CF_OPS[op]} {raw:012x}")
            continue
        addr, count, seq = _bits(raw, 0, 12), _bits(raw, 12, 3), _bits(raw, 16, 12)
        out.append(f"  {CF_OPS[op]} addr {addr} count {count}")
        for k in range(count):
            if 3 * (addr + k) + 3 > len(words):
                break
            a0, a1, a2 = words[3 * (addr + k) : 3 * (addr + k) + 3]
            if (seq >> (2 * k)) & 1:
                if _bits(a0, 0, 5) == 1:
                    dst = "".join("xyzw01?_"[_bits(a1, 3 * j, 3)] for j in range(4))
                    src = _swizzle(_bits(a0, 26, 6), 3)
                    out.append(f"    tfetch r{_bits(a0, 12, 6)}.{dst}, r{_bits(a0, 5, 6)}.{src}, tf{_bits(a0, 20, 5)}")
                else:
                    out.append(f"    fetch op{_bits(a0, 0, 5)} {a0:08x} {a1:08x} {a2:08x}")
                continue
            export = _bits(a0, 15, 1)
            vmask, smask, sop = _bits(a0, 16, 4), _bits(a0, 20, 4), _bits(a0, 26, 6)
            pred = f"({'' if _bits(a1, 27, 1) else '!'}p) " if _bits(a1, 28, 1) else ""
            sel3, sel2, sel1 = _bits(a2, 29, 1), _bits(a2, 30, 1), _bits(a2, 31, 1)
            r3, r2, r1 = _bits(a2, 0, 8), _bits(a2, 8, 8), _bits(a2, 16, 8)
            s3, s2, s1 = _bits(a1, 0, 8), _bits(a1, 8, 8), _bits(a1, 16, 8)
            n3, n2, n1 = _bits(a1, 24, 1), _bits(a1, 25, 1), _bits(a1, 26, 1)
            vdst, sdst = _bits(a0, 0, 6), _bits(a0, 8, 6)
            if vmask or not smask:
                vop = VECTOR_OPS[_bits(a2, 24, 5)]
                args = [operand(sel1, r1, n1, s1), operand(sel2, r2, n2, s2)]
                if vop in ("mad", "cndeq", "cndge", "cndgt"):
                    args.append(operand(sel3, r3, n3, s3))
                dst = f"oC{vdst}" if export else f"r{vdst}"
                sat = "_sat" if _bits(a0, 24, 1) else ""
                out.append(f"    {pred}{vop}{sat} {dst}.{_mask(vmask)}, {', '.join(args)}")
            if smask:
                name = SCALAR_OPS[sop] if sop < len(SCALAR_OPS) else f"sop{sop}"
                if 42 <= sop <= 47:  # src3 temp with a constant
                    arg = f"r{r3 & 63}.{_swizzle(s3)[:2]}, c{r2 if not sel2 else r1}"
                else:
                    arg = operand(sel3, r3, n3, s3)
                dst = f"oC{sdst}" if export else f"r{sdst}"
                sat = "_sat" if _bits(a0, 25, 1) else ""
                out.append(f"    {pred}+{name}{sat} {dst}.{_mask(smask)}, {arg}")
        if op in EXEC_ENDS:
            break
    return out


def float_names(shader: Shader) -> dict[int, str]:
    return {
        c.index + k: c.name + (f"[{k}]" if c.count > 1 else "")
        for c in shader.constants.values()
        if c.register_set == "float4"
        for k in range(c.count)
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", type=Path)
    ap.add_argument("--shader", type=lambda s: int(s, 0), help="container offset: constants and microcode")
    ap.add_argument("--param", help="pixel shaders with this constant (and its default)")
    args = ap.parse_args(argv)
    data = args.file.read_bytes()
    for s in shaders(data):
        if args.shader is not None and s.offset != args.shader:
            continue
        if args.param and (s.kind != "ps" or args.param not in s.constants):
            continue
        consts = ", ".join(
            f"{c.name}={c.register_set[0]}{c.index}" + (f"({c.default[0]:g})" if c.default else "")
            for c in sorted(s.constants.values(), key=lambda c: (c.register_set, c.index))
        )
        print(f"{s.offset:#x} {s.kind} {len(s.code)} bytes: {consts}")
        if args.shader is not None:
            print("\n".join(disassemble(s.code, float_names(s))))


if __name__ == "__main__":
    main()
