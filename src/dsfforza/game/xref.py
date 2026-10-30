"""Quick static cross-references for patch design, without Ghidra.

Linear-sweep disassembly (capstone) of Driver.exe .text, cached in
out/asi/xref.pickle. Queries:

  uv run dsff xref disp 0x3264        instructions with memory displacement 0x3264
  uv run dsff xref imm 0x130          instructions with immediate 0x130
  uv run dsff xref any 0x130          either
  uv run dsff xref call 0x6c57d0      calls/jumps to an address
  uv run dsff xref dis 0x6c57d0 [n]   disassemble n instructions (default 40)

Linear sweep can mis-decode the odd jump table, so treat a hit as a lead and
confirm it with `dis` (or Ghidra). Runs at Idle priority (src/dsfforza/build/gentle.py).
"""

from __future__ import annotations

import os
import pickle
import struct
import sys
from collections import defaultdict
from pathlib import Path

import capstone
from capstone import x86

from dsfforza import REPO

CACHE = REPO / "out" / "asi" / "xref.pickle"
EXE_CANDIDATES = [REPO / "out" / "vanilla" / "Driver.exe"] + (
    [Path(os.environ["DSF_GAME_DIR"]) / "Driver.exe"] if os.environ.get("DSF_GAME_DIR") else []
)


def load_exe() -> tuple[bytes, int, int, int]:
    for path in EXE_CANDIDATES:
        if path.is_file():
            data = path.read_bytes()
            break
    else:
        raise SystemExit("Driver.exe not found")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    opt = struct.unpack_from("<H", data, pe + 20)[0]
    sec = pe + 24 + opt
    name, vsize, va, _rsize, raw = struct.unpack_from("<8sIIII", data, sec)
    assert name.rstrip(b"\0") == b".text"
    return data, 0x400000 + va, raw, vsize


def idle() -> None:
    from dsfforza.build import gentle

    gentle.apply()
    gentle.set_priority(gentle.IDLE)


def build() -> dict:
    idle()
    data, base, raw, size = load_exe()
    code = data[raw : raw + size]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    disp = defaultdict(list)
    imm = defaultdict(list)
    flow = defaultdict(list)
    offset = 0
    while offset < size:
        progressed = False
        for insn in md.disasm(code[offset : offset + 0x10000], base + offset):
            progressed = True
            for op in insn.operands:
                if op.type == x86.X86_OP_MEM and op.mem.disp:
                    disp[op.mem.disp & 0xFFFFFFFF].append(insn.address)
                elif op.type == x86.X86_OP_IMM:
                    value = op.imm & 0xFFFFFFFF
                    if insn.group(capstone.CS_GRP_CALL) or insn.group(capstone.CS_GRP_JUMP):
                        flow[value].append(insn.address)
                    else:
                        imm[value].append(insn.address)
            offset = insn.address + insn.size - base
            if offset >= size:
                break
        if not progressed:
            offset += 1  # undecodable byte
    result = {"disp": dict(disp), "imm": dict(imm), "flow": dict(flow)}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
    return result


def index() -> dict:
    if CACHE.is_file():
        with CACHE.open("rb") as f:
            return pickle.load(f)
    return build()


def disasm(va: int, count: int = 40) -> list[str]:
    data, base, raw, _size = load_exe()
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    start = va - base
    lines = []
    for insn in md.disasm(data[raw + start : raw + start + count * 16], va):
        lines.append(f"{insn.address:08x}  {insn.bytes.hex(' '):<30} {insn.mnemonic} {insn.op_str}")
        if len(lines) >= count:
            break
    return lines


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv if argv is None else argv
    if len(argv) < 3:
        raise SystemExit(__doc__)
    mode, value = argv[1], int(argv[2], 16)
    if mode == "dis":
        print("\n".join(disasm(value, int(argv[3]) if len(argv) > 3 else 40)))
        return
    if mode == "rebuild":
        build()
        return
    idx = index()
    hits: list[int] = []
    if mode in ("disp", "any"):
        hits += idx["disp"].get(value, [])
    if mode in ("imm", "any"):
        hits += idx["imm"].get(value, [])
    if mode == "call":
        hits += idx["flow"].get(value, [])
    for address in sorted(set(hits)):
        print(disasm(address, 1)[0])


if __name__ == "__main__":
    main()
