"""Xenos (Xbox 360 GPU) shader microcode: decode, disassemble and run (research/forza-lenses-re.md).

Follows xenia's src/xenia/gpu/ucode.h and shader_translator.cc:

- control flow: two 48-bit instructions per three dwords. exec / cexec (bool constant) /
  cpexec (predicate) / cexec_pc (bool constant, predicate clean) run `count` ALU or fetch
  instructions from `addr`; the `_end` forms end the program when they run. cjmp jumps on a
  bool constant (or always).
- ALU: one vector and one scalar op per instruction. Swizzles are relative to the
  component. Scalar ops read src3's w slot (a) and x slot (b); mulsc/addsc/subsc take
  constant src3.a and temp register (op & 1) | (sel << 1) | (swizzle & 0x3c), its x slot.
  On an export both results go to the vector destination; components in both write masks
  get 1.
- texture fetch: dst swizzle 3 bits a component (xyzw, 0, 1, -, masked).

`Machine.run` executes a pixel shader on one pixel: interpolants in r0.., texture fetches
answered by a callback (constant colours here: lens maths needs texel values, not filtering),
constants and bools by register. Derivatives and loops are not emulated (the car shaders
this is used on have none).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

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
    "logc",
    "log",
    "rcpc",
    "rcpf",
    "rcp",
    "rsqc",
    "rsqf",
    "rsq",
    "maxas",
    "maxasf",
    "subs",
    "subs_prev",
    "setp_eq",
    "setp_ne",
    "setp_gt",
    "setp_ge",
    "setp_inv",
    "setp_pop",
    "setp_clr",
    "setp_rstr",
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
TWO_OPERAND = {"adds", "muls", "muls_prev2", "maxs", "mins", "subs", "maxas", "maxasf"}
CONST_TEMP = {"mulsc0", "mulsc1", "addsc0", "addsc1", "subsc0", "subsc1"}
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
ENDS = {2, 4, 6, 14}
BOOL_EXECS = {3, 4, 13, 14}
PRED_EXECS = {5, 6}
FLT_MAX = 3.4028234663852886e38


def _b(v: int, lo: int, n: int) -> int:
    return (v >> lo) & ((1 << n) - 1)


def swizzle(s: int) -> tuple[int, int, int, int]:
    return tuple(((s >> (2 * i)) + i) & 3 for i in range(4))


@dataclass
class Operand:
    temp: bool
    reg: int  # temp index or constant index
    absolute: bool
    negate: bool
    comps: tuple[int, ...]

    def text(self, cname) -> str:
        t = f"r{self.reg}" if self.temp else cname(self.reg)
        if self.absolute:
            t = f"|{t}|"
        sw = "".join("xyzw"[c] for c in self.comps)
        if len(self.comps) == 4:
            sw = sw[0] if len(set(sw)) == 1 else ("" if sw == "xyzw" else sw)
        return ("-" if self.negate else "") + t + (f".{sw}" if sw else "")


@dataclass
class Alu:
    vop: str
    sop: str
    vargs: list[Operand]
    sargs: list[Operand]
    vdst: int
    sdst: int
    vmask: int
    smask: int
    one_mask: int
    vsat: bool
    ssat: bool
    export: bool
    pred: int | None  # None, or the predicate value required


@dataclass
class Fetch:
    op: int
    dst: int
    src: int
    src_comps: tuple[int, int, int]
    dst_swz: tuple[int, int, int, int]  # 0-3 xyzw, 4 zero, 5 one, 7 masked
    slot: int
    pred: int | None


@dataclass
class Cf:
    op: int
    addr: int = 0
    count: int = 0
    seq: int = 0
    bool_index: int = 0
    condition: int = 1
    unconditional: bool = False
    predicated: bool = False


def decode_cf(code: bytes) -> tuple[list[Cf], tuple[int, ...]]:
    words = struct.unpack(f">{len(code) // 4}I", code[: len(code) // 4 * 4])
    raws: list[int] = []
    end = None
    i = 0
    while 3 * i + 3 <= len(words):
        d0, d1, d2 = words[3 * i : 3 * i + 3]
        raws += [d0 | (d1 & 0xFFFF) << 32, (d1 >> 16) | d2 << 16]
        if end is None:
            end = next((_b(r, 0, 12) for r in raws if _b(r, 44, 4) in EXECS), None)
        i += 1
        if end is not None and i >= end:
            break
    cfs = []
    for raw in raws:
        op = _b(raw, 44, 4)
        if op in EXECS:
            cfs.append(Cf(op, _b(raw, 0, 12), _b(raw, 12, 3), _b(raw, 16, 12), _b(raw, 34, 8), _b(raw, 42, 1)))
        elif op == 11:
            cfs.append(
                Cf(
                    op,
                    _b(raw, 0, 13),
                    bool_index=_b(raw, 34, 8),
                    condition=_b(raw, 42, 1),
                    unconditional=bool(_b(raw, 13, 1)),
                    predicated=bool(_b(raw, 14, 1)),
                )
            )
        else:
            cfs.append(Cf(op))
    return cfs, words


def decode(words: tuple[int, ...], index: int, fetch: bool) -> Alu | Fetch:
    a0, a1, a2 = words[3 * index : 3 * index + 3]
    if fetch:
        dst_swz = tuple(_b(a1, 3 * j, 3) for j in range(4))
        pred = (_b(a2, 31, 1)) if _b(a1, 31, 1) else None
        return Fetch(
            _b(a0, 0, 5),
            _b(a0, 12, 6),
            _b(a0, 5, 6),
            tuple(_b(a0, 26 + 2 * j, 2) for j in range(3)),
            dst_swz,
            _b(a0, 20, 5),
            pred,
        )
    abs_c = bool(_b(a0, 7, 1))
    export = bool(_b(a0, 15, 1))
    vmask, smask = _b(a0, 16, 4), _b(a0, 20, 4)
    sop = SCALAR_OPS[_b(a0, 26, 6)] if _b(a0, 26, 6) < len(SCALAR_OPS) else "retain_prev"
    vop = VECTOR_OPS[_b(a2, 24, 5)]
    sels = (_b(a2, 31, 1), _b(a2, 30, 1), _b(a2, 29, 1))
    regs = (_b(a2, 16, 8), _b(a2, 8, 8), _b(a2, 0, 8))
    swzs = (_b(a1, 16, 8), _b(a1, 8, 8), _b(a1, 0, 8))
    negs = (_b(a1, 26, 1), _b(a1, 25, 1), _b(a1, 24, 1))

    def operand(k: int, comps: tuple[int, ...]) -> Operand:
        if sels[k]:
            return Operand(True, regs[k] & 63, bool(regs[k] & 128), bool(negs[k]), comps)
        return Operand(False, regs[k], abs_c, bool(negs[k]), comps)

    nv = (
        3
        if vop in ("mad", "cndeq", "cndge", "cndgt", "dp2add")
        else (1 if vop in ("frc", "trunc", "floor", "max4", "mova") else 2)
    )
    vargs = [operand(k, swizzle(swzs[k])) for k in range(nv)]
    sw3 = swizzle(swzs[2])
    if sop in CONST_TEMP:
        temp = (_b(a0, 26, 6) & 1) | (sels[2] << 1) | (swzs[2] & 0x3C)
        sargs = [
            Operand(False, regs[2], abs_c, bool(negs[2]), (sw3[3],)),
            Operand(True, temp, abs_c, bool(negs[2]), (sw3[0],)),
        ]
    elif sop in ("retain_prev", "setp_clr"):
        sargs = []
    elif sop in TWO_OPERAND:
        sargs = [operand(2, (sw3[3], sw3[0]))]
    else:
        sargs = [operand(2, (sw3[3],))]
    one = vmask & smask if export else 0
    if export:
        vm, sm = vmask & ~smask, smask & ~vmask
    else:
        vm, sm = vmask, smask
    pred = _b(a1, 27, 1) if _b(a1, 28, 1) else None
    vdst = _b(a0, 0, 6)
    return Alu(
        vop,
        sop,
        vargs,
        sargs,
        vdst,
        vdst if export else _b(a0, 8, 6),
        vm,
        sm,
        one,
        bool(_b(a0, 24, 1)),
        bool(_b(a0, 25, 1)),
        export,
        pred,
    )


def _mask(m: int) -> str:
    return "".join("xyzw"[j] if m >> j & 1 else "_" for j in range(4))


def disassemble(code: bytes, kind: str, cname=lambda r: f"c{r}", bname=lambda b: f"b{b}") -> list[str]:
    cfs, words = decode_cf(code)

    def dname(alu: Alu, reg: int) -> str:
        if not alu.export:
            return f"r{reg}"
        if kind == "ps":
            return f"oC{reg}" if reg < 4 else ("oDepth" if reg == 61 else f"o{reg}")
        return f"oT{reg}" if reg < 16 else ("oPos" if reg == 62 else ("oPts" if reg == 63 else f"o{reg}"))

    out = []
    for n, cf in enumerate(cfs):
        name = CF_OPS[cf.op]
        if cf.op == 0:
            continue
        if cf.op == 11:
            cond = "always" if cf.unconditional else f"if {'' if cf.condition else '!'}{bname(cf.bool_index)}"
            out.append(f"  {n:2d} cjmp {cf.addr} {cond}")
            continue
        if cf.op not in EXECS:
            out.append(f"  {n:2d} {name}")
            continue
        cond = ""
        if cf.op in BOOL_EXECS:
            cond = f" if {'' if cf.condition else '!'}{bname(cf.bool_index)}"
        elif cf.op in PRED_EXECS:
            cond = f" if {'' if cf.condition else '!'}p"
        out.append(f"  {n:2d} {name} addr {cf.addr} count {cf.count}{cond}")
        for k in range(cf.count):
            if 3 * (cf.addr + k) + 3 > len(words):
                break
            ins = decode(words, cf.addr + k, bool((cf.seq >> (2 * k)) & 1))
            pred = "" if ins.pred is None else f"({'' if ins.pred else '!'}p) "
            if isinstance(ins, Fetch):
                if ins.op == 1:
                    dst = "".join("xyzw01?_"[c] for c in ins.dst_swz)
                    src = "".join("xyzw"[c] for c in ins.src_comps)
                    out.append(f"      {pred}tfetch r{ins.dst}.{dst}, r{ins.src}.{src}, tf{ins.slot}")
                else:
                    out.append(f"      {pred}fetch op{ins.op} r{ins.dst} <- r{ins.src}, vf{ins.slot}")
                continue
            if ins.vmask or ins.one_mask or not ins.smask:
                vop = ins.vop
                args = [a.text(cname) for a in ins.vargs]
                if vop == "max" and len(args) == 2 and args[0] == args[1]:
                    vop, args = "mov", args[:1]
                ones = f" (1 -> {_mask(ins.one_mask)})" if ins.one_mask else ""
                sat = "_sat" if ins.vsat else ""
                out.append(f"      {pred}{vop}{sat} {dname(ins, ins.vdst)}.{_mask(ins.vmask)}, {', '.join(args)}{ones}")
            if ins.smask or ins.sop != "retain_prev":
                sat = "_sat" if ins.ssat else ""
                args = ", ".join(a.text(cname) for a in ins.sargs)
                out.append(f"      {pred}+{ins.sop}{sat} {dname(ins, ins.sdst)}.{_mask(ins.smask)}, {args}")
    return out


# --- execution -------------------------------------------------------------------------------


class Num:
    """The arithmetic the emulator uses; `forza_lens.py formulas` swaps in a sympy one."""

    @staticmethod
    def ge(a, b):
        return a >= b

    @staticmethod
    def gt(a, b):
        return a > b

    @staticmethod
    def eq(a, b):
        return a == b

    @staticmethod
    def sel(cond, a, b):
        return a if cond else b

    @staticmethod
    def sat(x):
        return min(max(x, 0.0), 1.0)

    @staticmethod
    def floor(x):
        return float(math.floor(x))

    @staticmethod
    def exp2(x):
        return 2.0**x if x < 128 else FLT_MAX

    @staticmethod
    def log2(x):
        if x == 1:
            return 0.0
        return math.log2(x) if x > 0 else -FLT_MAX

    @staticmethod
    def rcp(x):
        return 1.0 / x if x != 0 else FLT_MAX

    @staticmethod
    def rsq(x):
        return 1.0 / math.sqrt(x) if x > 0 else FLT_MAX

    @staticmethod
    def sqrt(x):
        return math.sqrt(max(x, 0.0))

    @staticmethod
    def abs(x):
        return abs(x)


N = Num


def _scalar(op: str, a, b, ps, n=Num):
    """(result, new predicate or None)."""
    if op in ("adds", "addsc0", "addsc1"):
        return a + b, None
    if op == "adds_prev":
        return a + ps, None
    if op in ("muls", "mulsc0", "mulsc1"):
        return a * b, None
    if op == "muls_prev":
        return a * ps, None
    if op == "muls_prev2":
        return n.sel(n.gt(b, 0), a * ps, -FLT_MAX), None
    if op in ("maxs", "maxas", "maxasf"):
        return n.sel(n.ge(a, b), a, b), None
    if op == "mins":
        return n.sel(n.gt(b, a), a, b), None
    if op in ("subs", "subsc0", "subsc1"):
        return a - b, None
    if op == "subs_prev":
        return a - ps, None
    if op == "seqs":
        return n.sel(n.eq(a, 0), 1.0, 0.0), None
    if op == "sgts":
        return n.sel(n.gt(a, 0), 1.0, 0.0), None
    if op == "sges":
        return n.sel(n.ge(a, 0), 1.0, 0.0), None
    if op == "snes":
        return n.sel(n.eq(a, 0), 0.0, 1.0), None
    if op == "frcs":
        return a - n.floor(a), None
    if op == "floors":
        return n.floor(a), None
    if op == "truncs":
        return float(math.trunc(a)), None
    if op == "exp":
        return n.exp2(a), None
    if op in ("log", "logc"):
        return n.log2(a), None
    if op in ("rcp", "rcpc", "rcpf"):
        return n.rcp(a), None
    if op in ("rsq", "rsqc", "rsqf"):
        return n.rsq(a), None
    if op == "sqrt":
        return n.sqrt(a), None
    if op == "sin":
        return math.sin(a), None
    if op == "cos":
        return math.cos(a), None
    if op == "setp_eq":
        return (0.0, 1) if a == 0 else (1.0, 0)
    if op == "setp_ne":
        return (0.0, 1) if a != 0 else (1.0, 0)
    if op == "setp_gt":
        return (0.0, 1) if a > 0 else (1.0, 0)
    if op == "setp_ge":
        return (0.0, 1) if a >= 0 else (1.0, 0)
    if op == "setp_inv":
        return (0.0, 1) if a == 1 else ((1.0 if a == 0 else a), 0)
    if op == "setp_pop":
        return (0.0, 1) if a - 1 <= 0 else (a - 1, 0)
    if op == "setp_clr":
        return FLT_MAX, 0
    if op == "setp_rstr":
        return (0.0, 1) if a == 0 else (a, 0)
    if op.startswith("kills"):
        return 0.0, None
    if op == "retain_prev":
        return ps, None
    raise NotImplementedError(op)


def _vector(op: str, s: list, n=Num) -> list:
    a = s[0]
    b = s[1] if len(s) > 1 else None
    c = s[2] if len(s) > 2 else None
    if op == "add":
        return [a[i] + b[i] for i in range(4)]
    if op == "mul":
        return [a[i] * b[i] for i in range(4)]
    if op == "max":
        return [n.sel(n.ge(a[i], b[i]), a[i], b[i]) for i in range(4)]
    if op == "min":
        return [n.sel(n.gt(b[i], a[i]), a[i], b[i]) for i in range(4)]
    if op == "seq":
        return [n.sel(n.eq(a[i], b[i]), 1.0, 0.0) for i in range(4)]
    if op == "sgt":
        return [n.sel(n.gt(a[i], b[i]), 1.0, 0.0) for i in range(4)]
    if op == "sge":
        return [n.sel(n.ge(a[i], b[i]), 1.0, 0.0) for i in range(4)]
    if op == "sne":
        return [n.sel(n.eq(a[i], b[i]), 0.0, 1.0) for i in range(4)]
    if op == "frc":
        return [x - n.floor(x) for x in a]
    if op == "trunc":
        return [float(math.trunc(x)) for x in a]
    if op == "floor":
        return [n.floor(x) for x in a]
    if op == "mad":
        return [a[i] * b[i] + c[i] for i in range(4)]
    if op == "cndeq":
        return [n.sel(n.eq(a[i], 0), b[i], c[i]) for i in range(4)]
    if op == "cndge":
        return [n.sel(n.ge(a[i], 0), b[i], c[i]) for i in range(4)]
    if op == "cndgt":
        return [n.sel(n.gt(a[i], 0), b[i], c[i]) for i in range(4)]
    if op == "dp4":
        return [a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]] * 4
    if op == "dp3":
        return [a[0] * b[0] + a[1] * b[1] + a[2] * b[2]] * 4
    if op == "dp2add":
        return [a[0] * b[0] + a[1] * b[1] + c[0]] * 4
    if op == "max4":
        m = a[0]
        for x in a[1:]:
            m = n.sel(n.ge(m, x), m, x)
        return [m] * 4
    if op == "cube":
        # cube r, src.zzxy, src.yxz: src0 = (z, z, x, y). Only its use as fetch coordinates
        # matters here (fetches return fixed values), so symbolic inputs give zeros.
        try:
            x, y, z = float(a[2]), float(a[3]), float(a[0])
        except TypeError:
            return [0.0] * 4
        ax, ay, az = abs(x), abs(y), abs(z)
        if az >= ax and az >= ay:
            tc, sc, ma, face = -y, (x if z >= 0 else -x), 2 * z, (4.0 if z >= 0 else 5.0)
        elif ay >= ax:
            tc, sc, ma, face = (z if y >= 0 else -z), x, 2 * y, (2.0 if y >= 0 else 3.0)
        else:
            tc, sc, ma, face = -y, (-z if x >= 0 else z), 2 * x, (0.0 if x >= 0 else 1.0)
        return [tc, sc, ma, face]
    if op.startswith("kill"):
        return [0.0] * 4
    if op == "dst":
        return [1.0, a[1] * b[1], a[2], b[3]]
    if op == "mova":
        return list(a)
    raise NotImplementedError(op)


class Machine:
    """Runs one shader for one pixel (or vertex)."""

    def __init__(self, code: bytes, constants: dict[int, list[float]], bools: dict[int, bool], fetch, num=Num):
        self.cfs, self.words = decode_cf(code)
        self.num = num
        self._decoded: dict[tuple[int, bool], Alu | Fetch] = {}
        self.constants = constants
        self.bools = bools
        self.fetch = fetch  # fetch(slot, coords (x, y, z)) -> 4 floats
        self.trace: list[str] = []

    def run(self, inputs: dict[int, list[float]]) -> dict[int, list[float]]:
        r = [[0.0] * 4 for _ in range(64)]
        for k, v in inputs.items():
            r[k] = list(v)
        out: dict[int, list[float]] = {}
        p = 0
        ps = 0.0
        pc = 0
        steps = 0
        while pc < len(self.cfs):
            steps += 1
            if steps > 10000:
                raise RuntimeError("runaway")
            cf = self.cfs[pc]
            pc += 1
            if cf.op == 11:
                if cf.unconditional or (bool(self.bools.get(cf.bool_index, False)) == bool(cf.condition)):
                    pc = cf.addr
                continue
            if cf.op not in EXECS:
                continue
            if cf.op in BOOL_EXECS and bool(self.bools.get(cf.bool_index, False)) != bool(cf.condition):
                continue
            if cf.op in PRED_EXECS and p != cf.condition:
                continue
            for k in range(cf.count):
                key = (cf.addr + k, bool((cf.seq >> (2 * k)) & 1))
                ins = self._decoded.get(key)
                if ins is None:
                    ins = self._decoded[key] = decode(self.words, *key)
                if ins.pred is not None and p != ins.pred:
                    continue
                if isinstance(ins, Fetch):
                    if ins.op != 1:
                        continue
                    src = r[ins.src]
                    tex = self.fetch(ins.slot, tuple(src[c] for c in ins.src_comps))
                    dst = r[ins.dst]
                    for j, sel in enumerate(ins.dst_swz):
                        if sel < 4:
                            dst[j] = tex[sel]
                        elif sel == 4:
                            dst[j] = 0.0
                        elif sel == 5:
                            dst[j] = 1.0
                    continue

                def read(o: Operand) -> list[float]:
                    base = r[o.reg] if o.temp else self.constants.get(o.reg, [0.0] * 4)
                    v = [base[c] for c in o.comps]
                    if o.absolute:
                        v = [self.num.abs(x) for x in v]
                    if o.negate:
                        v = [-x for x in v]
                    return v

                vres = _vector(ins.vop, [read(o) for o in ins.vargs], self.num)
                sv = [x for o in ins.sargs for x in read(o)]
                a = sv[0] if sv else 0.0
                b = sv[1] if len(sv) > 1 else 0.0
                sres, newp = _scalar(ins.sop, a, b, ps, self.num)
                if ins.vsat:
                    vres = [self.num.sat(x) for x in vres]
                if ins.ssat:
                    sres = self.num.sat(sres)
                target = out.setdefault(ins.vdst, [0.0] * 4) if ins.export else r[ins.vdst]
                for j in range(4):
                    if ins.vmask >> j & 1:
                        target[j] = vres[j]
                starget = out.setdefault(ins.sdst, [0.0] * 4) if ins.export else r[ins.sdst]
                for j in range(4):
                    if ins.smask >> j & 1:
                        starget[j] = sres
                    if ins.one_mask >> j & 1:
                        target[j] = 1.0
                ps = sres
                if newp is not None:
                    p = newp
            if cf.op in ENDS:
                break
        return out
