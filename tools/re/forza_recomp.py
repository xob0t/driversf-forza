"""Drive the native FM4 recompilation (fm4-static-recomp) as a reference renderer and a runtime probe.

research/forza-recomp.md. The recompilation is an external tool (BSD-3), built on this PC by its
own setup from our disc and kept under out/recomp; nothing of it is copied here. This script talks
to it through its documented interfaces only:

- the scripted-input file (`FM4_STATIC_RECOMP_INPUT_SCRIPT`): lines `<start s> <hold s> <controls>`
  counted from the process start, re-read when the file changes; `SET:<cvar>=<value>` tokens
  change an option live;
- `<script>.shot` holding a BMP path: the guest output (the game's own frame, before the
  window's scaling) is written there;
- ShiftGlue's draw log (`fh1_debug_log_draws`, switched on live for half a second): each draw's
  vertex and pixel shader hashes (XXH3-64 of the microcode as guest memory holds it), blend,
  texture fetches and the float constants the shader reads, in `runtime.log`.

The game always runs on a desktop of its own with its window hidden, and a watchdog ends it if a
window of it ever reaches the user's desktop (research/forza-recomp.md, section 3.1: never add a
visible or minimized mode).

    uv run python tools/re/forza_recomp.py discs                     our ISOs against the recomps' lists
    uv run python tools/re/forza_recomp.py extract                   FM4 disc 1 and the disc 2 packages
    uv run python tools/re/forza_recomp.py extract-fh1               the FH1 disc for Pinyon Shift's build
    uv run --with pycryptodome --with xxhash python tools/re/forza_recomp.py image   our code against the supported
    uv run --with xxhash python tools/re/forza_recomp.py hashes      shader hash -> technique, pass
    uv run --with xxhash python tools/re/forza_recomp.py run ROUTE RUN [-- GAME_ARGS]
    uv run --with xxhash python tools/re/forza_recomp.py draws LOG [--technique RE] [--json OUT]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

from dsfforza import REPO

sys.path.insert(0, str(Path(__file__).parent))
import fxl

OUT = REPO / "out" / "recomp"
FM4 = OUT / "fm4"
DISC1 = FM4 / "disc1"
HASHES = FM4 / "shader-hashes.json"
# The save and the shader cache (setup.ps1 -StateRoot fills the cache); each run's log, input
# script, dumps and captures go to its own folder.
STATE = FM4 / "state"

# default.xex SHA-256 each recomp accepts (fm4-static-recomp config/supported-discs.json,
# pinyon-shift config/supported-dumps.json; read 2026-10-31).
SUPPORTED = {
    "3618E85AF83E972DDCC1B0D0DD808E78E29AB4AB3890A15B31C210A6A3E93E09": "fm4-static-recomp: FM4 1.0 USA (media 26FF1101)",
    "6A7E9AE561CCCA134916F27870A73299408F4AD14F1499063C0F6F6414C2A7E1": "fm4-static-recomp: FM4 1.0 PAL (media 61D62D9F)",
    "DB40DF605ADE49A612B35A7A24C38F6004BCB17A88ED6B48288DE16DF9E3987C": "pinyon-shift: FH1 USA MS-2505 (media 2DC7007B)",
}
EXECUTION_INFO = 0x00040006  # XEX2 optional header (xenia xex2_info.h)


def xex_identity(data: bytes) -> str:
    """Title, media id, version and disc of a default.xex, from its execution info."""
    n = struct.unpack_from(">I", data, 20)[0]
    headers = dict(struct.unpack_from(">II", data, 24 + 8 * i) for i in range(n))
    off = headers.get(EXECUTION_INFO)
    if off is None:
        return "no execution info"
    media, version, _, title, _, _, disc, discs = struct.unpack_from(">IIIIBBBB", data, off)
    return f"title {title:08X} media {media:08X} version {version:#x} disc {disc}/{discs}"


def title_id(data: bytes) -> str:
    n = struct.unpack_from(">I", data, 20)[0]
    headers = dict(struct.unpack_from(">II", data, 24 + 8 * i) for i in range(n))
    return f"{struct.unpack_from('>I', data, headers[EXECUTION_INFO] + 12)[0]:08X}"


def discs() -> None:
    from dsfforza.formats.xiso import XIso

    for iso in sorted((REPO / "assets").glob("*.iso")):
        x = XIso(iso)
        try:
            data = x.read(x.find("default.xex"))
        except KeyError:
            print(f"{iso.name}: no default.xex")
            continue
        finally:
            x.close()
        digest = hashlib.sha256(data).hexdigest().upper()
        print(f"{iso.name}: {xex_identity(data)}, default.xex {digest[:16]}: {SUPPORTED.get(digest, 'not supported')}")


# The supported FH1 image (Pinyon Shift MS-2505), as its repo records it: site/findings/
# executable.toml ("image extent and entry point", "section map", "function table and size
# profile", "largest function") and config/rexglue/accepted-codegen-warnings.json (two bdz).
FH1_SUPPORTED_IMAGE = {
    "size_of_image": 0x18A3400,
    "entry": 0x82A6FBC0,
    "sections": {
        ".rdata": 0x82000600,
        ".pdata": 0x82366E00,
        ".text": 0x823E0000,
        ".data": 0x831F0000,
        ".reloc": 0x83540200,
    },
    "section_count": 14,
    "tls_size": 0x3069,
    "functions": 57127,
    "median_function": 120,
    "functions_under_64": 7676,
    "functions_over_4k": 122,
    "largest": [(0x8241A370, 110012), (0x82BE1880, 30196)],
}
FH1_BDZ = [(0x82AD8138, 0x82AD836C), (0x82AD813C, 0x82AD836C)]
# xenia-canary game-patches, patches/4D5309C9 - Forza Horizon.patch.toml and 4D530910 - Forza
# Motorsport 4.patch.toml: the base default.xex module hash (media 2DC7007B, 4000D145 and
# 2B7A1346 for FH1; disc 1 for FM4). Pinyon Shift's fh1-post-processing.toml names the same FH1 hash.
XENIA_HASH = {"4D5309C9": 0xD48ABF1704CE5C4A, "4D530910": 0x06DEF37749E2CEAC}
IMPORT_LIBRARIES = 0x000103FF  # XEX2 optional header
XEX_SECTION_CODE = 1  # xenia xex2_info.h page descriptor info


def xenia_module_hash(data: bytes, image: bytes) -> int:
    """xenia's module hash (kernel/user_module.cc UserModule::CalculateHash): XXH3-64 over the
    loaded image from the first to the last code page descriptor, the descriptor index times the
    0x80000000 heap's 64 KB page; the import thunks rewritten first, as XexModule::
    SetupLibraryImports does for kernel imports (sc 2; blr; nop; nop)."""
    import xxhash

    sec = struct.unpack_from(">I", data, 16)[0]
    count = struct.unpack_from(">I", data, sec + 0x180)[0]
    code = [
        i for i in range(count) if struct.unpack_from(">I", data, sec + 0x184 + 24 * i)[0] & 0xF == XEX_SECTION_CODE
    ]
    start, end = code[0] * 0x10000, (code[-1] + 1) * 0x10000
    img = bytearray(image) + bytes(max(0, end - len(image)))
    n = struct.unpack_from(">I", data, 20)[0]
    headers = dict(struct.unpack_from(">II", data, 24 + 8 * i) for i in range(n))
    off = headers[IMPORT_LIBRARIES]
    _size, str_size, libs = struct.unpack_from(">III", data, off)
    p = off + 12 + str_size
    for _ in range(libs):
        lib_size = struct.unpack_from(">I", data, p)[0]
        for k in range(struct.unpack_from(">H", data, p + 0x26)[0]):
            rec = struct.unpack_from(">I", data, p + 0x28 + 4 * k)[0] - 0x82000000
            if struct.unpack_from(">I", img, rec)[0] >> 24 == 1:  # a thunk record
                struct.pack_into(">4I", img, rec, 0x44000042, 0x4E800020, 0x60000000, 0x60000000)
        p += lib_size
    return xxhash.xxh3_64_intdigest(bytes(img[start:end]))


def image_facts(image: bytes) -> dict:
    """The structural facts FH1_SUPPORTED_IMAGE lists, of a decrypted PE image."""
    import statistics

    import xex

    pe = struct.unpack_from("<I", image, 0x3C)[0]
    opt = pe + 24
    secs = xex.sections(image)
    pdata = next(s for s in secs if s[0] == ".pdata")
    funcs = []
    for k in range(pdata[2] // 8):
        begin, info = struct.unpack_from(">II", image, pdata[1] + 8 * k)
        if begin:
            funcs.append((begin, ((info >> 8) & 0x3FFFFF) * 4))
    sizes = [s for _, s in funcs]
    return {
        "size_of_image": struct.unpack_from("<I", image, opt + 56)[0],
        "entry": struct.unpack_from("<I", image, opt + 16)[0] + struct.unpack_from("<I", image, opt + 28)[0],
        "sections": {n: 0x82000000 + va for n, va, _, _ in secs if n in FH1_SUPPORTED_IMAGE["sections"]},
        "section_count": len(secs),
        "tls_size": next((vs for n, _, vs, _ in secs if n == ".tls"), None),
        "functions": len(funcs),
        "median_function": statistics.median(sizes),
        "functions_under_64": sum(s < 64 for s in sizes),
        "functions_over_4k": sum(s > 4096 for s in sizes),
        "largest": sorted(funcs, key=lambda f: -f[1])[:2],
    }


def image_check() -> None:
    """Each disc's default.xex: xenia's module hash against xenia's patch lists, and for FH1 the
    structural facts of Pinyon Shift's supported image and its two bdz instructions."""
    import xex

    from dsfforza.formats.xiso import XIso

    for iso in sorted((REPO / "assets").glob("*.iso")):
        x = XIso(iso)
        try:
            data = x.read(x.find("default.xex"))
        except KeyError:
            continue
        finally:
            x.close()
        title = title_id(data)
        if title not in XENIA_HASH or "disc 1/" not in xex_identity(data):  # FM4 DVD2's is its installer
            continue
        image = xex.Xex(data).image()
        h = xenia_module_hash(data, image)
        print(
            f"{iso.name}: xenia module hash {h:016X}: {'same as' if h == XENIA_HASH[title] else 'NOT'} xenia's {XENIA_HASH[title]:016X}"
        )
        if title != "4D5309C9":
            continue
        facts = image_facts(image)
        for key, want in FH1_SUPPORTED_IMAGE.items():
            print(f"  {key}: {'same' if facts[key] == want else f'DIFFERS: {facts[key]} against {want}'}")
        for at, target in FH1_BDZ:
            w = struct.unpack_from(">I", image, at - 0x82000000)[0]
            bd = w & 0xFFFC
            to = at + (bd - 0x10000 if bd & 0x8000 else bd)
            ok = w >> 26 == 16 and (w >> 21) & 31 in (18, 19, 26, 27) and to == target
            print(f"  bdz at {at:08X} to {target:08X}: {'same' if ok else f'DIFFERS ({w:08X})'}")


def extract_fh1(dest: Path = OUT / "pinyon-v071" / ".local" / "game" / "base") -> None:
    """The FH1 disc's files (no $SystemUpdate) where Pinyon Shift's build reads them."""
    from dsfforza.build import forza_roster
    from dsfforza.formats.xiso import XIso

    iso = forza_roster.game_discs(sorted((REPO / "assets").glob("*.iso"))).get("FH1")
    x = XIso(iso)
    count = 0
    for e in x.files():
        p = e.path.strip("/")
        if e.is_dir or p.startswith("$SystemUpdate"):
            continue
        target = dest / p
        if not (target.exists() and target.stat().st_size == e.size):
            target.parent.mkdir(parents=True, exist_ok=True)
            x.extract(e, target)
            count += 1
    x.close()
    print(f"{iso.name}: {count} files to {dest}")


def extract() -> None:
    """Disc 1's files (no $SystemUpdate) to out/recomp/fm4/disc1, disc 2's four content packages
    to out/recomp/fm4/disc2pkg (the recomp's --fm4_disc2_packages)."""
    from dsfforza.build import forza_roster
    from dsfforza.formats.xiso import XIso

    isos = sorted((REPO / "assets").glob("*.iso"))
    found = forza_roster.game_discs(isos)
    disc1 = found.get("FM4")
    disc2 = next((i for i in isos if "DVD2" in i.name and "Forza 4" in i.name), None)
    jobs = [(disc1, DISC1, ""), (disc2, FM4 / "disc2pkg", "content/0000000000000000/4d530910/00000002")]
    for iso, dest, prefix in jobs:
        if iso is None:
            print(f"no disc for {dest.name}")
            continue
        x = XIso(iso)
        count = 0
        for e in x.files():
            p = e.path.strip("/")
            if e.is_dir or p.startswith("$SystemUpdate") or not p.lower().startswith(prefix):
                continue
            target = dest / (p.rsplit("/", 1)[-1] if prefix else p)  # the packages sit flat
            if target.exists() and target.stat().st_size == e.size:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            x.extract(e, target)
            count += 1
        x.close()
        print(f"{iso.name}: {count} files to {dest}")


def ucode_hash(code: bytes) -> str:
    """The recomp's shader hash: XXH3-64 of the microcode as the guest has it in memory, big-endian
    dwords as stored in the .fxobj (ShiftGlue vulkan/pipeline_cache.cpp, `LoadShader`:
    `XXH3_64bits(host_address, dword_count * 4)` on the guest memory)."""
    import xxhash

    return f"{xxhash.xxh3_64_intdigest(code[: len(code) // 4 * 4]):016X}"


def hashes(root: Path = DISC1 / "Media" / "Shaders") -> dict:
    """{hash: [{file, technique, pass, kind, constants: {register: name}}]} for every effect under `root`."""
    table: dict[str, list] = {}
    for path in sorted(root.rglob("*.fxobj")):
        try:
            fx = fxl.Effect(path.read_bytes())
            techniques = fx.techniques()
        except (ValueError, struct.error, IndexError):
            continue
        for tech in techniques:
            for p in tech.passes:
                for sh in (p.vs, p.ps):
                    if sh is None or not sh.code:
                        continue
                    names = {}
                    for c in sh.constants.values():
                        if c.register_set == "float4":
                            for k in range(c.count):
                                names[c.index + k] = c.name if c.count == 1 else f"{c.name}[{k}]"
                    entry = {
                        "file": path.name,
                        "technique": tech.name,
                        "pass": p.name,
                        "kind": sh.kind,
                        "constants": names,
                    }
                    table.setdefault(ucode_hash(sh.code), []).append(entry)
    return table


DRAW_RE = re.compile(r"FH1 debug draw (\d+) .*?vs=([0-9A-F]{16}) ps=([0-9A-F]{16})(.*)$")
CONST_RE = re.compile(r"c(\d+):([-\d.e+naif]+),([-\d.e+naif]+),([-\d.e+naif]+),([-\d.e+naif]+)")
FETCH_RE = re.compile(r" (v?)t(\d+)=([0-9A-F]{8}):(\d+)x(\d+):f(\d+)")


def parse_draws(log: Path) -> list[dict]:
    """The `FH1 debug draw` lines of a runtime.log: per draw its shaders, texture fetches and the
    float constants each shader reads (pc: pixel, vc: vertex), in the shader's own registers."""
    draws = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = DRAW_RE.search(line)
        if not m:
            continue
        tail = m.group(4)
        d = {"draw": int(m.group(1)), "vs": m.group(2), "ps": m.group(3), "pc": {}, "vc": {}, "fetch": []}
        for f in FETCH_RE.finditer(tail):
            d["fetch"].append(
                {
                    "vertex": bool(f.group(1)),
                    "slot": int(f.group(2)),
                    "address": f.group(3),
                    "size": [int(f.group(4)), int(f.group(5))],
                    "format": int(f.group(6)),
                }
            )
        for key in ("vc", "pc"):
            block = re.search(rf" {key}=\[([^\]]*)\]", tail)
            if block:
                for c in CONST_RE.finditer(block.group(1)):
                    d[key][int(c.group(1))] = [float(v) for v in c.group(2, 3, 4, 5)]
        draws.append(d)
    return draws


def named_draws(draws: list[dict], table: dict) -> list[dict]:
    """Each draw with its techniques (by the pixel shader's hash) and its constants by name."""
    out = []
    for d in draws:
        ps = table.get(d["ps"], [])
        vs = table.get(d["vs"], [])
        e = dict(d)
        e["ps_techniques"] = sorted({f"{x['file']}:{x['technique']}/{x['pass']}" for x in ps})
        e["vs_techniques"] = sorted({f"{x['file']}:{x['technique']}/{x['pass']}" for x in vs})
        for key, src in (("pc", ps), ("vc", vs)):
            names = src[0]["constants"] if src else {}
            e[key] = {names.get(int(r), names.get(str(r), f"c{r}")) + f"@c{r}": v for r, v in d[key].items()}
        out.append(e)
    return out


# --- running the game ----------------------------------------------------------------------------

SOURCE = OUT / "fm4-v030"  # the recomp's source at its release tag, built by its tools/setup.ps1
EXE = SOURCE / "out" / "build" / "win-amd64-release" / "fm4_static_recomp.exe"
# The log: frames of draws are megabytes, and ShiftGlue rotates at 5 MB and flushes only when its
# buffer fills (core/logging.cpp), so the tail would lag and lose lines.
BASE_ARGS = [
    "--fm4_skip_intro=true",
    "--audio_mute=true",
    "--fullscreen=false",
    "--fm4_log_strings=true",
    "--log_max_file_size_mb=4000",
    "--log_flush_interval=1",
]
PRESS_LEAD = 0.3  # s ahead of now that a press starts (the game re-reads the script every 0.1 s)
IDLE, BELOW_NORMAL = 0x40, 0x4000


class Game:
    """One fm4_static_recomp process on the shared state (one at a time), on a desktop of its own
    with its window never shown, watched so that it ends if a window of it ever reaches the user's
    desktop; held in a job that ends it with this script, at Idle CPU priority."""

    def __init__(self, run: Path, args: list[str], *, desktop: str = "dsff-recomp", priority: int = IDLE):
        import ctypes
        from ctypes import wintypes

        run = run.resolve()
        self.run, self.state = run, STATE
        for d in (STATE / "user", STATE / "update", STATE / "cache", run / "logs", run / "dumps"):
            d.mkdir(parents=True, exist_ok=True)
        self.log_path = run / "logs" / "runtime.log"
        self.log_path.unlink(missing_ok=True)
        self.dumps = run / "dumps"
        self.script = run / "live.input"
        self.script.write_text("# forza_recomp.py\n", encoding="ascii")
        argv = [
            str(EXE),
            f"--game_data_root={DISC1}",
            f"--user_data_root={self.state / 'user'}",
            f"--update_data_root={self.state / 'update'}",
            f"--cache_root={self.state / 'cache'}",
            f"--log_file={self.log_path}",
            # where --fh1_debug_dump_range and --fh1_debug_log_vs write; frame -1 (on a `trigger`
            # file only), as 0 would write every resolve of every frame
            f"--fh1_resolve_dump_dir={self.dumps}",
            "--fh1_resolve_dump_frame=-1",
            *BASE_ARGS,
            *args,
        ]
        env = dict(
            __import__("os").environ,
            FM4_STATIC_RECOMP_INPUT_SCRIPT=str(self.script),
            SDL_WINDOW_ACTIVATE_WHEN_SHOWN="0",
            REX_WINDOW_HIDDEN="1",  # ShiftGlue window_sdl.cpp: the window is created, never shown
        )
        block = "".join(f"{k}={v}\0" for k, v in env.items()) + "\0"
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32, self.u32, self.ct, self.wt = k32, ctypes.WinDLL("user32", use_last_error=True), ctypes, wintypes

        class STARTUPINFO(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD),
                ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD),
                ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class ProcessInformation(ctypes.Structure):
            _fields_ = [
                ("hProcess", wintypes.HANDLE),
                ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD),
            ]

        # A desktop of our own: Windows shows only the input desktop, so nothing the game opens
        # there can appear on the user's screen or take their focus or input. Never switched to.
        self.u32.CreateDesktopW.restype = wintypes.HANDLE
        self.desktop_handle = self.u32.CreateDesktopW(desktop, None, None, 0, 0x10000000, None)  # GENERIC_ALL
        if not self.desktop_handle:
            raise OSError(f"CreateDesktopW: {ctypes.get_last_error()}; not launching")
        si = STARTUPINFO(cb=ctypes.sizeof(STARTUPINFO), lpDesktop=f"WinSta0\\{desktop}")
        pi = ProcessInformation()
        command = " ".join(f'"{a}"' if " " in a else a for a in argv)
        k32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_wchar_p,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        flags = priority | 0x400 | 0x4  # CREATE_UNICODE_ENVIRONMENT, CREATE_SUSPENDED (joined to the job first)
        if not k32.CreateProcessW(
            str(EXE),
            ctypes.create_unicode_buffer(command),
            None,
            None,
            False,
            flags,
            block,
            str(EXE.parent),
            ctypes.byref(si),
            ctypes.byref(pi),
        ):
            raise OSError(f"CreateProcessW: {ctypes.get_last_error()}")
        self.process, self.pid = pi.hProcess, pi.dwProcessId
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.job = k32.CreateJobObjectW(None, None)
        limits = (ctypes.c_byte * 144)()  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION; LimitFlags at +16
        ctypes.c_uint32.from_buffer(limits, 16).value = 0x2000  # KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(wintypes.HANDLE(self.job), 9, limits, 144)
        k32.AssignProcessToJobObject(wintypes.HANDLE(self.job), wintypes.HANDLE(self.process))
        k32.ResumeThread(wintypes.HANDLE(pi.hThread))
        k32.CloseHandle(wintypes.HANDLE(pi.hThread))
        self.started = __import__("time").monotonic()
        self.breached = False
        __import__("threading").Thread(target=self._watchdog, daemon=True).start()
        self.lines: list[str] = []
        self._log_pos = 0
        self.mark = 0

    # process
    def alive(self) -> bool:
        return self.k32.WaitForSingleObject(self.wt.HANDLE(self.process), 0) == 0x102

    def stop(self) -> None:
        self.k32.TerminateProcess(self.wt.HANDLE(self.process), 1)
        self.k32.WaitForSingleObject(self.wt.HANDLE(self.process), 10000)
        self.k32.CloseHandle(self.wt.HANDLE(self.process))
        self.k32.CloseHandle(self.wt.HANDLE(self.job))
        if self.desktop_handle:
            self.u32.CloseDesktop(self.wt.HANDLE(self.desktop_handle))

    def windows_on_user_desktop(self) -> int:
        """The game's top-level windows on this script's (the user's) desktop: EnumWindows lists the
        calling thread's desktop. With the game on its own desktop there are none."""
        ct, wt, u32 = self.ct, self.wt, self.u32
        found = []
        proc = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def each(hwnd, _):
            pid = wt.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ct.byref(pid))
            if pid.value == self.pid:
                found.append(hwnd)
            return True

        u32.EnumWindows(proc(each), 0)
        return len(found)

    def _watchdog(self) -> None:
        """Ends the game the moment any window of it is on the user's desktop."""
        import time

        while self.alive():
            if self.windows_on_user_desktop():
                self.k32.TerminateProcess(self.wt.HANDLE(self.process), 1)
                self.breached = True
                print("WATCHDOG: a game window reached the user's desktop; the game was ended", flush=True)
                return
            time.sleep(0.05)

    # log
    def poll(self) -> list[str]:
        try:
            with self.log_path.open("rb") as f:
                f.seek(self._log_pos)
                data = f.read()
        except FileNotFoundError:
            return []
        if not data and self.log_path.stat().st_size < self._log_pos:  # rotated: start again
            self._log_pos = 0
            return self.poll()
        end = data.rfind(b"\n") + 1
        self._log_pos += end
        new = data[:end].decode("utf-8", "replace").splitlines()
        self.lines.extend(new)
        return new

    def _seen(self, pattern: re.Pattern, since: int) -> bool:
        """Whether a line since `since` matches; the mark moves to just after the first that does,
        so lines the same screen logged after it still count for the next step."""
        self.poll()
        for k in range(since, len(self.lines)):
            if pattern.search(self.lines[k]):
                self.mark = k + 1
                return True
        return False

    # input
    def _write(self, hold: float, controls: str) -> None:
        import time

        start = time.monotonic() - self.started + PRESS_LEAD
        with self.script.open("a", encoding="ascii") as f:
            f.write(f"{start:.2f} {hold:.2f} {controls}\n")

    def press(self, controls: str, hold: float = 0.1) -> None:
        import time

        self.poll()
        self.mark = len(self.lines)
        self._write(hold, controls)
        time.sleep(hold + PRESS_LEAD)

    def until(self, controls: str, pattern: str, timeout: float = 30, every: float = 2.0) -> None:
        """Presses every `every` seconds until a log line since the previous step matches (no
        press if one already does)."""
        import time

        rx = re.compile(pattern)
        since = self.mark  # what the previous step's screen logged counts too
        if self._seen(rx, since):
            return
        deadline = time.monotonic() + timeout
        cycle = controls.split("|")  # "START|A": each press the next of them
        presses = 0
        while True:
            self._write(0.1, cycle[presses % len(cycle)])
            presses += 1
            step_end = min(time.monotonic() + every, deadline)
            while time.monotonic() < step_end:
                if self._seen(rx, since):
                    return
                if not self.alive():
                    raise RuntimeError(f"the game exited waiting for /{pattern}/")
                time.sleep(0.2)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no /{pattern}/ within {timeout:g} s pressing {controls}")

    def wait(self, pattern: str, timeout: float = 60) -> None:
        import time

        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while not self._seen(rx, self.mark):
            if not self.alive():
                raise RuntimeError(f"the game exited waiting for /{pattern}/")
            if time.monotonic() > deadline:
                raise TimeoutError(f"no /{pattern}/ within {timeout:g} s")
            time.sleep(0.25)

    def shot(self, name: str, timeout: float = 15) -> Path:
        """The guest output (the game's frame, not the window) as <run>/shots/<name>.png."""
        import time

        from PIL import Image

        shots = self.run / "shots"
        shots.mkdir(exist_ok=True)
        bmp = shots / f"{name}.bmp"
        bmp.unlink(missing_ok=True)
        request = Path(str(self.script) + ".shot")
        pending = Path(str(request) + ".tmp")
        pending.write_text(str(bmp), encoding="ascii")
        pending.replace(request)
        deadline = time.monotonic() + timeout
        while not bmp.exists():
            if time.monotonic() > deadline or not self.alive():
                raise TimeoutError(f"no capture {name}")
            time.sleep(0.05)
        time.sleep(0.1)
        png = shots / f"{name}.png"
        Image.open(bmp).save(png)
        bmp.unlink()
        return png

    def frame(self, name: str, timeout: float = 30) -> Path:
        """One whole frame's draw log (shaders, fetches, blend, the constants each shader reads) as
        <run>/frames/<name>.log. `fh1_debug_log_draws` is read on every draw
        (command_processor.cpp), so it is switched on for 0.5 s (the showroom runs below 30 fps) with the script's SET: token; of
        the frames logged, the first that starts at draw 0 and ends where the next one starts is
        kept. (The `trigger` file of --fh1_resolve_dump_dir logs a frame only from its first
        resolve on, which in a race is after the cars.)"""
        import time

        self.poll()
        since = len(self.lines)
        start = time.monotonic() - self.started + PRESS_LEAD
        with self.script.open("a", encoding="ascii") as f:
            f.write(f"{start:.2f} 0.05 SET:fh1_debug_log_draws=true\n")
            f.write(f"{start + 0.5:.2f} 0.05 SET:fh1_debug_log_draws=false\n")
        deadline = time.monotonic() + timeout
        frames: list[list[str]] = []
        count, quiet = 0, time.monotonic()
        while time.monotonic() < deadline:  # lines reach the file at the next flush (1 s)
            time.sleep(0.25)
            self.poll()
            draws = [x for x in self.lines[since:] if "FH1 debug draw " in x]
            if len(draws) != count:
                count, quiet = len(draws), time.monotonic()
            elif count and time.monotonic() - quiet > 2.0:
                break
        draws = [x for x in self.lines[since:] if "FH1 debug draw " in x]
        for x in draws:
            if DRAW_RE.search(x) and int(DRAW_RE.search(x).group(1)) == 0:
                frames.append([])
            if frames:
                frames[-1].append(x)
        whole = frames[0] if len(frames) > 1 else []
        out = self.run / "frames" / f"{name}.log"
        out.parent.mkdir(exist_ok=True)
        out.write_text("\n".join(whole) + "\n", encoding="utf-8")
        print(f"  frame {name}: {len(whole)} draws ({len(frames)} frame starts logged)")
        return out


def run_route(route: Path, run: Path, args: list[str]) -> None:
    """Runs a route file, one step per line (# comments):

    press <controls> [hold=<s>]      e.g. `press A`, `press RT=1 LX=0 hold=3`
    until <controls> <regex> [timeout] [every]   press again until a log line matches
    wait <regex> [timeout]           a log line since the last action
    sleep <seconds>
    set <cvar> <value>               a live option (the script's SET: token)
    shot <name>                      the guest output to shots/<name>.png
    frame <name>                     one frame's draw log to frames/<name>.log
    """
    import shlex
    import time

    run.mkdir(parents=True, exist_ok=True)
    game = Game(run, args)
    print(f"started pid {game.pid} on {run}, on its own desktop")
    try:
        for raw in route.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            op, *rest = shlex.split(line)
            print(f"[{time.monotonic() - game.started:7.1f}] {line}".encode("ascii", "replace").decode(), flush=True)
            if op == "press":
                holds = [float(x[5:]) for x in rest if x.startswith("hold=")]
                game.press(" ".join(x for x in rest if not x.startswith("hold=")), holds[0] if holds else 0.1)
            elif op == "hold":  # returns at once: the controls stay held while the next steps run
                game.poll()
                game.mark = len(game.lines)
                game._write(float(rest[-1]), " ".join(rest[:-1]))
            elif op == "until":
                game.until(rest[0], rest[1], *(float(x) for x in rest[2:]))
            elif op == "wait":
                game.wait(rest[0], *(float(x) for x in rest[1:]))
            elif op == "sleep":
                time.sleep(float(rest[0]))
            elif op == "set":
                game.press(f"SET:{rest[0]}={rest[1]}")
            elif op == "shot":
                game.shot(rest[0])
            elif op == "frame":
                game.frame(rest[0])
            else:
                raise ValueError(f"unknown step {line!r}")
            if game.breached:
                raise RuntimeError("the watchdog ended the game: a window of it reached the user's desktop")
    finally:
        game.poll()
        game.stop()
        print(f"stopped; windows seen on the user's desktop: {'yes' if game.breached else 'none'}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discs")
    sub.add_parser("extract")
    sub.add_parser("extract-fh1")
    sub.add_parser("image")
    h = sub.add_parser("hashes")
    h.add_argument("--root", type=Path, default=DISC1 / "Media" / "Shaders")
    d = sub.add_parser("draws")
    d.add_argument("log", type=Path)
    d.add_argument("--technique", help="regex on the pixel shader's technique names")
    d.add_argument("--json", type=Path)
    r = sub.add_parser("run", help=run_route.__doc__.split("\n\n")[0])
    r.add_argument("route", type=Path)
    r.add_argument("run", type=Path, help="run folder (state, shots, frames)")
    r.add_argument("game_args", nargs="*", help="recomp options after --, e.g. -- --resolution_scale=2")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        run_route(args.route, args.run, args.game_args)
        return
    if args.cmd == "discs":
        discs()
    elif args.cmd == "extract":
        extract()
    elif args.cmd == "extract-fh1":
        extract_fh1()
    elif args.cmd == "image":
        image_check()
    elif args.cmd == "hashes":
        table = hashes(args.root)
        HASHES.parent.mkdir(parents=True, exist_ok=True)
        HASHES.write_text(json.dumps(table))
        print(f"{len(table)} shader hashes -> {HASHES}")
    else:
        table = json.loads(HASHES.read_text()) if HASHES.exists() else hashes()
        draws = named_draws(parse_draws(args.log), table)
        if args.technique:
            draws = [x for x in draws if any(re.search(args.technique, t) for t in x["ps_techniques"])]
        if args.json:
            args.json.write_text(json.dumps(draws, indent=1))
        for x in draws:
            print(f"draw {x['draw']}: ps {x['ps']} {', '.join(x['ps_techniques']) or '?'}")
            for name, v in x["pc"].items():
                print(f"    {name} = {', '.join(f'{c:g}' for c in v)}")


if __name__ == "__main__":
    main()
