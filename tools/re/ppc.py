"""PowerPC helpers for Forza's default.xex image (research/forza-lenses-re.md, section 7).

Works on the loaded image `xex.py` writes (out/re3b/<game>/image.bin, base 0x82000000):
functions from the .pdata table, cross-references made of `lis` + a 16-bit low part (addi,
ori, loads, stores), direct calls (`bl`), and a capstone disassembly annotated with the
strings and floats the constants point at. Capstone doesn't know the Xenon's VMX128
opcodes; those print as `.long`.

    uv run --with capstone python tools/re/ppc.py xref fh1 0x8223e550 [--to 0x8223e560]
    uv run --with capstone python tools/re/ppc.py callers fh1 0x82xxxxxx
    uv run --with capstone python tools/re/ppc.py func fh1 0x82xxxxxx       disassemble the function holding it
    uv run --with capstone python tools/re/ppc.py dis fh1 0x82xxxxxx [--count 64]
    uv run --with capstone python tools/re/ppc.py strings fh1 REGEX
"""

from __future__ import annotations

import argparse
import bisect
import re
import struct
import sys
from functools import cached_property
from pathlib import Path

from dsfforza import REPO

sys.path.insert(0, str(Path(__file__).parent))
import xex

DATA = REPO / "out" / "re3b"
BASE = 0x82000000


def s16(x: int) -> int:
    return x - 0x10000 if x & 0x8000 else x


# X-form (opcode 31) operations whose result goes to rA, not rD: logical ops, shifts, counts, extends
_X_TO_RA = {28, 60, 124, 284, 316, 412, 444, 476, 24, 536, 792, 824, 26, 922, 954, 27, 539, 58, 986}
_X_STORES = {151, 183, 215, 247, 407, 439, 149, 181, 150, 214, 662, 918, 663, 727, 983}


def _written(w: int) -> int | None:
    """The general register an instruction writes, roughly (None for stores, branches and the like)."""
    op, rd, ra = w >> 26, (w >> 21) & 31, (w >> 16) & 31
    if op in (7, 8, 12, 13, 14, 15, 32, 33, 34, 35, 40, 41, 42, 43, 58):
        return rd
    if op in (20, 21, 23, 24, 25, 26, 27, 28, 29, 30):
        return ra
    if op == 31:
        xo = (w >> 1) & 0x3FF
        if xo in _X_STORES or xo == 467:
            return None
        return ra if xo in _X_TO_RA else rd
    return None


class Image:
    def __init__(self, game: str, root: Path = DATA):
        self.game = game
        self.data = (root / game / "image.bin").read_bytes()
        self.sections = {n: (BASE + va, BASE + va + vs) for n, va, vs, _ in xex.sections(self.data)}
        self.text = self.sections[".text"]
        n = (self.text[1] - self.text[0]) // 4
        self.words = struct.unpack_from(f">{n}I", self.data, self.text[0] - BASE)

    def u32(self, a: int) -> int:
        return struct.unpack_from(">I", self.data, a - BASE)[0]

    def f32(self, a: int) -> float:
        return struct.unpack_from(">f", self.data, a - BASE)[0]

    def word(self, a: int) -> int:
        return self.words[(a - self.text[0]) >> 2]

    def cstr(self, a: int, limit: int = 80) -> str | None:
        o = a - BASE
        if not 0 <= o < len(self.data):
            return None
        e = self.data.find(b"\0", o, o + limit)
        t = self.data[o:e] if e > o else b""
        if len(t) >= 3 and all(32 <= c < 127 for c in t):
            return t.decode()
        return None

    @cached_property
    def functions(self) -> list[tuple[int, int]]:
        """(start, end) of every function in .pdata (Xenon: begin, then prolog:8 length:22 in words)."""
        lo, hi = self.sections[".pdata"]
        out = []
        for o in range(lo - BASE, hi - BASE, 8):
            begin, packed = struct.unpack_from(">II", self.data, o)
            if begin == 0:
                break
            out.append((begin, begin + 4 * ((packed >> 8) & 0x3FFFFF)))
        out.sort()
        return out

    def function_at(self, a: int) -> tuple[int, int] | None:
        fs = self.functions
        i = bisect.bisect_right(fs, (a, 1 << 40)) - 1
        if i >= 0 and fs[i][0] <= a < fs[i][1]:
            return fs[i]
        return None

    def xrefs(self, lo: int, hi: int | None = None, window: int = 24) -> list[tuple[int, int]]:
        """Instructions that form an address in [lo, hi] from `lis rX, ha` and a 16-bit low
        part on rX (addi, ori, D-form loads and stores) within `window` instructions: (site, address)."""
        hi = lo if hi is None else hi
        out = []
        words, t0 = self.words, self.text[0]
        lis_ok = {}
        for ha in range((lo + 0x8000) >> 16, ((hi + 0x8000) >> 16) + 1):
            lis_ok[ha] = True
        for i, w in enumerate(words):
            op = w >> 26
            if op not in (14, 24, 32, 34, 36, 38, 40, 44, 48, 50, 52, 54, 58, 62):
                continue
            ra = (w >> 16) & 31
            if ra == 0:
                continue
            low = w & 0xFFFF
            for j in range(1, window + 1):
                if i - j < 0:
                    break
                v = words[i - j]
                if v >> 26 == 15 and (v >> 21) & 31 == ra and (v >> 16) & 31 == 0:
                    ha = v & 0xFFFF
                    if ha in lis_ok:
                        addr = ((ha << 16) + (low if op == 24 else s16(low))) & 0xFFFFFFFF
                        if lo <= addr <= hi:
                            out.append((t0 + 4 * i, addr))
                    break
                # rX overwritten in between: stop
                vop = v >> 26
                if vop in (14, 15, 32, 34, 40, 42) and (v >> 21) & 31 == ra:
                    break
        return out

    def callers(self, target: int) -> list[int]:
        out = []
        t0 = self.text[0]
        for i, w in enumerate(self.words):
            if w >> 26 == 18 and w & 3 == 1:
                d = w & 0x3FFFFFC
                if d & 0x2000000:
                    d -= 0x4000000
                if t0 + 4 * i + d == target:
                    out.append(t0 + 4 * i)
        return out

    def dis(self, start: int, count: int) -> list[str]:
        import capstone

        md = capstone.Cs(capstone.CS_ARCH_PPC, capstone.CS_MODE_32 | capstone.CS_MODE_BIG_ENDIAN)
        out = []
        lis: dict[int, int] = {}
        for k in range(count):
            a = start + 4 * k
            code = self.data[a - BASE : a - BASE + 4]
            ins = next(md.disasm(code, a), None)
            w = struct.unpack(">I", code)[0]
            text = f"{ins.mnemonic} {ins.op_str}" if ins else f".long {w:#010x}"
            note = ""
            op, rd, ra = w >> 26, (w >> 21) & 31, (w >> 16) & 31
            if op == 15 and ra == 0:
                lis[rd] = (w & 0xFFFF) << 16
            elif ra in lis and op in (14, 24, 32, 34, 40, 48, 50, 36, 52):
                addr = (lis[ra] + ((w & 0xFFFF) if op == 24 else s16(w & 0xFFFF))) & 0xFFFFFFFF
                s = self.cstr(addr)
                note = f"  ; {addr:#x}"
                if s:
                    note += f' "{s}"'
                elif op in (48, 50) and BASE <= addr < BASE + len(self.data):
                    note += f" = {self.f32(addr):g}"
                elif op == 32 and BASE <= addr < BASE + len(self.data):
                    note += f" = [{self.u32(addr):#x}]"
            if not (op == 15 and ra == 0):
                lis.pop(_written(w), None)
            out.append(f"{a:#010x}  {w:08x}  {text}{note}")
        return out

    def strings(self, pattern: str) -> list[tuple[int, str]]:
        rx = re.compile(pattern.encode(), re.I)
        return [
            (BASE + m.start(), m.group().decode())
            for m in re.finditer(rb"[\x20-\x7e]{4,}", self.data)
            if rx.search(m.group())
        ]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("xref", "callers", "func", "dis", "strings"):
        a = sub.add_parser(name)
        a.add_argument("game", choices=("fh1", "fm4"))
        a.add_argument("arg")
        if name == "xref":
            a.add_argument("--to")
        if name == "dis":
            a.add_argument("--count", type=int, default=64)
    args = ap.parse_args(argv)
    img = Image(args.game)
    if args.cmd == "strings":
        for a, s in img.strings(args.arg):
            print(f"{a:#010x} {s}")
        return
    addr = int(args.arg, 0)
    if args.cmd == "xref":
        for site, ea in img.xrefs(addr, int(args.to, 0) if args.to else None):
            f = img.function_at(site)
            print(f"{site:#010x} -> {ea:#010x}  in {f[0]:#010x}" if f else f"{site:#010x} -> {ea:#010x}")
    elif args.cmd == "callers":
        for site in img.callers(addr):
            f = img.function_at(site)
            print(f"{site:#010x}  in {f[0]:#010x}" if f else f"{site:#010x}")
    elif args.cmd == "func":
        f = img.function_at(addr)
        if not f:
            sys.exit(f"{addr:#x}: no .pdata function")
        print(f"function {f[0]:#010x}..{f[1]:#010x}")
        print("\n".join(img.dis(f[0], (f[1] - f[0]) // 4)))
    elif args.cmd == "dis":
        print("\n".join(img.dis(addr, args.count)))


if __name__ == "__main__":
    main()
