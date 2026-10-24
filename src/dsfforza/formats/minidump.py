"""Summarise a 32-bit Windows minidump: exception, faulting module, registers,
and probable return addresses found on the faulting thread's stack.

    uv run dsff minidump crash.dmp [--stack 64]
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

THREAD_LIST, MODULE_LIST, EXCEPTION, MEMORY_LIST, MEMORY64_LIST = 3, 4, 6, 5, 9


def read_string(data: bytes, rva: int) -> str:
    (length,) = struct.unpack_from("<I", data, rva)
    return data[rva + 4 : rva + 4 + length].decode("utf-16-le", "replace")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dump", type=Path)
    parser.add_argument("--stack", type=int, default=48, help="stack dwords to scan for return addresses")
    args = parser.parse_args(argv)
    data = args.dump.read_bytes()
    if data[:4] != b"MDMP":
        raise SystemExit("not a minidump")
    streams_count, dir_rva = struct.unpack_from("<II", data, 8)
    streams = {}
    for i in range(streams_count):
        kind, size, rva = struct.unpack_from("<III", data, dir_rva + i * 12)
        streams[kind] = (size, rva)

    modules = []
    if MODULE_LIST in streams:
        _, rva = streams[MODULE_LIST]
        (count,) = struct.unpack_from("<I", data, rva)
        for i in range(count):
            base, size = struct.unpack_from("<QI", data, rva + 4 + i * 108)
            (name_rva,) = struct.unpack_from("<I", data, rva + 4 + i * 108 + 20)
            modules.append((base, size, read_string(data, name_rva).split("\\")[-1]))

    def where(address: int) -> str:
        for base, size, name in modules:
            if base <= address < base + size:
                return f"{name}+{address - base:#x}"
        return "?"

    memory = []
    if MEMORY_LIST in streams:
        _, rva = streams[MEMORY_LIST]
        (count,) = struct.unpack_from("<I", data, rva)
        for i in range(count):
            start, size, mrva = struct.unpack_from("<QII", data, rva + 4 + i * 16)
            memory.append((start, size, mrva))
    if MEMORY64_LIST in streams:
        _, rva = streams[MEMORY64_LIST]
        count, base_rva = struct.unpack_from("<QQ", data, rva)
        offset = base_rva
        for i in range(count):
            start, size = struct.unpack_from("<QQ", data, rva + 16 + i * 16)
            memory.append((start, size, offset))
            offset += size

    def read_memory(address: int, size: int) -> bytes | None:
        for start, length, mrva in memory:
            if start <= address and address + size <= start + length:
                return data[mrva + address - start : mrva + address - start + size]
        return None

    if EXCEPTION not in streams:
        raise SystemExit("no exception stream")
    _, rva = streams[EXCEPTION]
    # MINIDUMP_EXCEPTION_STREAM: u32 ThreadId, u32 align, MINIDUMP_EXCEPTION (u32 code, u32 flags,
    # u64 record, u64 address, u32 NumberParameters, u32 align, u64 ExceptionInformation[15]),
    # then the context's location descriptor.
    (thread_id,) = struct.unpack_from("<I", data, rva)
    code, _flags, _record, address, nparams = struct.unpack_from("<IIQQI", data, rva + 8)
    params = struct.unpack_from("<15Q", data, rva + 8 + 32)[:nparams]
    _ctx_size, ctx_rva = struct.unpack_from("<II", data, rva + 8 + 152)
    print(f"exception {code:#010x} at {address:#010x} ({where(address)}), thread {thread_id}")
    if code == 0xC0000005 and len(params) >= 2:
        print(
            f"  access violation: {'write' if params[0] == 1 else 'read' if params[0] == 0 else 'execute'} "
            f"of {params[1]:#010x}"
        )
    # x86 CONTEXT: registers start at offset 0x9C (Edi, Esi, Ebx, Edx, Ecx, Eax, Ebp, Eip, SegCs, EFlags, Esp)
    names = ["edi", "esi", "ebx", "edx", "ecx", "eax", "ebp", "eip", "cs", "eflags", "esp"]
    regs = dict(zip(names, struct.unpack_from("<11I", data, ctx_rva + 0x9C)))
    print("  " + " ".join(f"{n}={regs[n]:08x}" for n in ("eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp")))
    stack = read_memory(regs["esp"], args.stack * 4)
    if stack:
        print("  stack values inside modules (probable return addresses):")
        for i in range(len(stack) // 4):
            (value,) = struct.unpack_from("<I", stack, i * 4)
            location = where(value)
            if location != "?":
                print(f"    [esp+{i * 4:#05x}] {value:#010x} {location}")
    else:
        print("  stack memory not captured")


if __name__ == "__main__":
    sys.exit(main())
