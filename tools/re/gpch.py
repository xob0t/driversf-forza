"""List and disassemble the vehicle GPU programs of dngvehicles.sp (research/lights-re.md).

The archive's shared chunk (SSIC, root entry 450) holds 198 GPCH programs in five DRSPs;
each GPCH is (GPCI uid + 100, GPVD vertex declaration, GPPM parameters, GPTS, GPUS = a D3DX
fx_2_0 effect whose vs_3_0 / ps_3_0 shaders carry debug info). A material (MACO) has eight
passes (MARC); each pass's MARH names its program by GPCI uid at +16. Program numbers here
are the GPCH's index in SSIC walk order, as in research/lights.md ("GPCH 130").

    uv run python tools/re/gpch.py materials [--entry N]     materials and their 8 passes
    uv run python tools/re/gpch.py dis 130 [--out DIR]         disassemble (d3dcompiler_47)
    uv run python tools/re/gpch.py dump --out out/re/shaders   every program to DIR
"""

from __future__ import annotations

import argparse
import ctypes
import re
import struct
import sys
from pathlib import Path

from dsfforza import REPO
from dsfforza.convert.dsf_materials import current, material_info
from dsfforza.formats.chnk import read_root
from dsfforza.formats.vehicle_mesh import child, payload

ARCHIVE = REPO / "out" / "vanilla" / "Install" / "dngvehicles.sp"
SSIC = 450


def find(entry, tag):
    if entry.tag == tag:
        yield entry
    for c in entry.children or []:
        yield from find(c, tag)


def programs(stream, root) -> list:
    return list(find(root.entries[SSIC], "GPCH"))


def disassemble(code: bytes) -> str:
    d3d = ctypes.WinDLL("d3dcompiler_47.dll")
    blob = ctypes.c_void_p()
    hr = d3d.D3DDisassemble(code, len(code), 0, None, ctypes.byref(blob))
    if hr != 0:
        return f"; D3DDisassemble failed {hr & 0xFFFFFFFF:#x}"
    vt = ctypes.cast(ctypes.cast(blob, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    ptr = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(vt[3])(blob)
    size = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(vt[4])(blob)
    return ctypes.string_at(ptr, size).decode(errors="replace").replace(chr(0), "")


def shader_length(code: bytes, start: int) -> int | None:
    """Bytes of the SM3 token stream at `start` (comments skipped by length, END 0x0000FFFF)."""
    p = start + 4
    while p + 4 <= len(code):
        (tok,) = struct.unpack_from("<I", code, p)
        if tok == 0x0000FFFF:
            return p + 4 - start
        if tok & 0xFFFF == 0xFFFE:
            p += 4 + (tok >> 16) * 4
            continue
        p += 4 + ((tok >> 24) & 0xF) * 4
    return None


def shaders(code: bytes):
    """(kind 'vs'/'ps', offset, bytecode) of every SM3 shader in an fx_2_0 effect."""
    for m in re.finditer(rb"\x00\x03(\xfe|\xff)\xff\xfe\xff", code):
        start = m.start()
        n = shader_length(code, start)
        if n:
            yield ("vs" if code[start + 2] == 0xFE else "ps"), start, code[start : start + n]


def fnv(code: bytes) -> str:
    """DSF Enhanced's shader hash (D5HashBytecode: FNV-1a over the bytes, end token included),
    the vs=/ps= names in its Trace log."""
    h = 2166136261
    for b in code:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return f"{h:08X}"


def cmd_materials(args) -> None:
    with Path(args.archive).open("rb") as s:
        root = read_root(s)
        index = {payload(s, child(g, "GPCI"))[:4]: i for i, g in enumerate(programs(s, root))}
        entries = [SSIC] if args.entry is None else [args.entry]
        for e in entries:
            for m in find(root.entries[e], "MACO"):
                ps = []
                for marc in m.children:
                    if marc.tag == "MARC":
                        h = current(s, child(marc, "MARH"))
                        ps.append(index.get(h[16:20], h[16:20].hex()))
                print(e, material_info(s, m)["path"].rsplit("\\", 1)[-1], ps)


def cmd_dis(args) -> None:
    with Path(args.archive).open("rb") as s:
        root = read_root(s)
        gps = programs(s, root)
        which = range(len(gps)) if args.program is None else [args.program]
        for i in which:
            code = payload(s, child(gps[i], "GPUS"))
            uid = payload(s, child(gps[i], "GPCI"))[:4].hex()
            for kind, at, sh in shaders(code):
                head = f"; GPCH {i} (GPCI {uid}) {kind} at {at:#x}, {len(sh)} bytes, hash {fnv(sh)}\n"
                text = head + disassemble(sh)
                if args.out:
                    Path(args.out).mkdir(parents=True, exist_ok=True)
                    (Path(args.out) / f"gpch{i:03d}_{kind}_{fnv(sh)}.txt").write_text(text)
                else:
                    print(text)


def cmd_scan(args) -> None:
    """Every vs/ps 2.x/3.0 shader in any file (e.g. media/SfxMisc.fchunk, the post chain)."""
    data = Path(args.file).read_bytes()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    seen = set()
    for kind, at, sh in shaders(data):
        h = fnv(sh)
        if h in seen:
            continue
        seen.add(h)
        text = disassemble(sh)
        srcs = sorted(set(re.findall(r'#line \d+ "([^"]+)"', text)))
        name = re.sub(r"[^A-Za-z0-9]+", "_", Path(srcs[-1]).stem if srcs else "nosrc")
        (out / f"{kind}_{h}_{name}.txt").write_text(f"; {args.file} {kind} at {at:#x}, hash {h}\n" + text)
        print(f"{kind} {at:#010x} {h} {len(sh):6d} {' '.join(srcs)}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=str(ARCHIVE))
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("materials")
    m.add_argument("--entry", type=int)
    d = sub.add_parser("dis")
    d.add_argument("program", type=int)
    d.add_argument("--out")
    a = sub.add_parser("dump")
    a.add_argument("--out", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("file")
    sc.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "scan":
        cmd_scan(args)
    elif args.cmd == "materials":
        cmd_materials(args)
    else:
        if args.cmd == "dump":
            args.program = None
        cmd_dis(args)


if __name__ == "__main__":
    sys.exit(main())
