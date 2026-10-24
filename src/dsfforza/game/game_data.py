"""Derived game data the build tools read, made from the user's own files.

port_car.py, host_select.py, handling_dsf.py and gui_assets.py read a few
things that came from earlier research steps on the developer's machine:

- out/lua-src/LuaScripts/vehiclestats/vehiclestats.lua (and the same file under
  LuaScripts.copy): the game's vehicleStats table as Lua text. It was made with
  unluac (Java). Here it is rebuilt without Java: the compiled chunk in
  media/ScriptsLuaScripts.fchunk only builds a table of constants, so a small
  Lua 5.1 bytecode evaluator runs it and prints the table in unluac's layout
  (byte-identical to unluac's output for the stock file).
- out/db/fh1_gamedb.slt, out/db/fm4_gamedb.slt and out/cache/<game>/gamedb.slt:
  each game's Media/db/gamedb.slt from its disc.

    uv run dsff game_data prepare --game "<game folder>" [--iso a.iso ...]
    uv run dsff game_data vehiclestats "<game>/media/ScriptsLuaScripts.fchunk" out/vs.lua

The game folder is only read. Set DSF_GAME_DIR to the same folder so the other
tools find dngvehicles.sp, gui.sp, Fonts.fchunk and global.fchunk there
(src/dsfforza/build/port_batch.py does this).
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from dsfforza import REPO

VEHICLESTATS = "vehiclestats\\vehiclestats.lua"


def game_dir() -> Path:
    """The game folder: $DSF_GAME_DIR."""
    from dsfforza.game import game_paths
    return game_paths.game_dir()


# --- Lua 5.1 bytecode (just enough for constant tables) -------------------------------

class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0
        header = data[:12]
        if header[:5] != b"\x1bLuaQ":
            raise ValueError("not Lua 5.1 bytecode")
        self.int_size, self.size_t, self.instr, self.num_size, self.integral = header[7:12]
        if header[6] != 1 or self.int_size != 4 or self.size_t != 4 or self.instr != 4:
            raise ValueError(f"unsupported bytecode header {header.hex()}")
        self.pos = 12

    def u8(self) -> int:
        self.pos += 1
        return self.data[self.pos - 1]

    def i32(self) -> int:
        self.pos += 4
        return struct.unpack_from("<i", self.data, self.pos - 4)[0]

    def string(self) -> str | None:
        n = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        if not n:
            return None
        self.pos += n
        return self.data[self.pos - n:self.pos - 1].decode("latin-1")

    def number(self):
        if self.integral:
            value = struct.unpack_from("<i" if self.num_size == 4 else "<q", self.data, self.pos)[0]
        else:
            value = struct.unpack_from("<f" if self.num_size == 4 else "<d", self.data, self.pos)[0]
        self.pos += self.num_size
        return Num(value, self.num_size)

    def function(self) -> dict:
        f = {"source": self.string(), "line": self.i32(), "lastline": self.i32()}
        f["nups"], f["params"], f["vararg"], f["stack"] = (self.u8() for _ in range(4))
        count = self.i32()
        f["code"] = [struct.unpack_from("<I", self.data, self.pos + 4 * i)[0] for i in range(count)]
        self.pos += 4 * count
        consts = []
        for _ in range(self.i32()):
            kind = self.u8()
            if kind == 0:
                consts.append(None)
            elif kind == 1:
                consts.append(bool(self.u8()))
            elif kind == 3:
                consts.append(self.number())
            elif kind == 4:
                consts.append(self.string())
            else:
                raise ValueError(f"constant type {kind}")
        f["consts"] = consts
        f["protos"] = [self.function() for _ in range(self.i32())]
        lines = self.i32()                               # line info
        self.pos += 4 * lines
        for _ in range(self.i32()):                      # local names
            self.string()
            self.pos += 8
        for _ in range(self.i32()):                      # upvalue names
            self.string()
        return f


class Num(float):
    """A Lua number that remembers its storage width (the game's are 4-byte floats)."""
    def __new__(cls, value, size: int = 8):
        obj = super().__new__(cls, value)
        obj.size = size
        return obj


def load(data: bytes) -> dict:
    return _Reader(data).function()


def run_constant_chunk(data: bytes, env: dict | None = None) -> dict:
    """Run a main chunk that only builds tables of constants and assigns globals
    (MOVE, LOADK, LOADBOOL, LOADNIL, GETGLOBAL, SETGLOBAL, NEWTABLE, SETTABLE,
    SETLIST, RETURN). Returns the globals it set; tables are dicts in
    construction order. Anything else raises ValueError."""
    f = load(data)
    env = {} if env is None else env
    k, regs, out = f["consts"], [None] * max(f["stack"], 2), {}

    def rk(x):
        return k[x & 0xFF] if x & 0x100 else regs[x]

    def key(value):
        return int(value) if isinstance(value, float) and value.is_integer() else value

    code, pc = f["code"], 0
    while pc < len(code):
        i = code[pc]
        op, a, c, b, bx = i & 0x3F, (i >> 6) & 0xFF, (i >> 14) & 0x1FF, (i >> 23) & 0x1FF, i >> 14
        pc += 1
        if op == 0:
            regs[a] = regs[b]
        elif op == 1:
            regs[a] = k[bx]
        elif op == 2:
            regs[a] = bool(b)
            pc += 1 if c else 0
        elif op == 3:
            for r in range(a, b + 1):
                regs[r] = None
        elif op == 5:
            name = k[bx]
            if name not in out and name not in env:
                raise ValueError(f"reads global {name}")
            regs[a] = out.get(name, env.get(name))
        elif op == 7:
            out[k[bx]] = regs[a]
        elif op == 9:
            regs[a][key(rk(b))] = rk(c)
        elif op == 10:
            regs[a] = {}
        elif op == 34:
            if c == 0:
                c, pc = code[pc], pc + 1
            if b == 0:
                raise ValueError("SETLIST to top of stack")
            for n in range(1, b + 1):
                regs[a][(c - 1) * 50 + n] = regs[a + n]
        elif op == 30:
            break
        else:
            raise ValueError(f"opcode {op} at {pc - 1}: not a constant chunk")
    return out


def lua_literal(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "nil"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        fmt = "<f" if getattr(value, "size", 8) == 4 else "<d"
        for digits in range(1, 18):   # shortest text that reads back to the same float
            text = f"{value:.{digits}g}"
            if struct.pack(fmt, float(text)) == struct.pack(fmt, value):
                return text
        return repr(float(value))
    return str(value)


def format_table(name: str, table: dict) -> str:
    """unluac's layout for a global table of tables: two-space indent, [n] keys,
    identifier keys bare, no trailing commas."""
    def body(t: dict, depth: int) -> list[str]:
        pad = "  " * depth
        lines = []
        items = list(t.items())
        for n, (k, v) in enumerate(items):
            keytext = k if isinstance(k, str) and k.isidentifier() else f"[{lua_literal(k)}]"
            comma = "," if n + 1 < len(items) else ""
            if isinstance(v, dict):
                lines.append(f"{pad}{keytext} = {{")
                lines += body(v, depth + 1)
                lines.append(f"{pad}}}{comma}")
            else:
                lines.append(f"{pad}{keytext} = {lua_literal(v)}{comma}")
        return lines
    return "\n".join([f"{name} = {{"] + body(table, 1) + ["}"]) + "\n"


def read_vehiclestats(path: Path) -> dict[int, dict[str, str]]:
    """vehiclestats.lua as written by vehiclestats_text (unluac's layout):
    {id: {field: value}}, every value as its text, string quotes removed."""
    import re
    out = {}
    text = Path(path).read_text(encoding="utf-8")
    for match in re.finditer(r"\[(\d+)\]\s*=\s*\{(.*?)\n\s*\}", text, re.S):
        out[int(match.group(1))] = {key: value.strip('"') for key, value in
                                    re.findall(r"(\w+)\s*=\s*(\"[^\"]*\"|[^,\n]+)", match.group(2))}
    return out


def vehiclestats_text(scripts_package: Path) -> str:
    """vehiclestats.lua as unluac prints it, from media/ScriptsLuaScripts.fchunk."""
    from dsfforza.formats import lua_scripts
    for script in lua_scripts.read_package(scripts_package):
        if (script["path"] or "").lower() == VEHICLESTATS:
            table = run_constant_chunk(script["data"])
            return format_table("vehicleStats", table["vehicleStats"])
    raise SystemExit(f"{scripts_package}: no {VEHICLESTATS}")


# --- prepare ----------------------------------------------------------------------------

def prepare(game: Path, isos: list[Path], log=print, force: bool = False) -> dict:
    """Write the derived files the build tools expect under out/ (only missing
    ones unless force). Returns {name: path}. Each file is written under a
    temporary name and renamed into place, so a build reading it meanwhile never
    sees half of it. tools/worktree-setup.ps1 runs this in a new worktree (each
    worktree has its own out/lua-src and out/db); port_batch runs it every build."""
    from dsfforza.build import port_car
    done = {}
    package = game / "media" / "ScriptsLuaScripts.fchunk"
    text = None
    for variant in ("LuaScripts", "LuaScripts.copy"):
        target = REPO / "out" / "lua-src" / variant / "vehiclestats" / "vehiclestats.lua"
        if target.exists() and not force:
            done[f"vehiclestats ({variant})"] = target
            continue
        text = text or vehiclestats_text(package)
        target.parent.mkdir(parents=True, exist_ok=True)
        port_car.write_atomic(target, text.replace("\n", "\r\n").encode("utf-8"))   # unluac's line ends
        log(f"wrote {target} from {package.name}")
        done[f"vehiclestats ({variant})"] = target
    for name in ("FH1", "FM4"):
        cached = port_car.extract_gamedb(isos, name, REPO / "out" / "cache")
        if cached is None:
            log(f"no {name} disc among the images; {name} cars can't be ported")
            continue
        db = REPO / "out" / "db" / f"{name.lower()}_gamedb.slt"
        # Another size than the cache's: cut short by a copy from before these writes were atomic.
        if force or not db.exists() or db.stat().st_size != cached.stat().st_size:
            db.parent.mkdir(parents=True, exist_ok=True)
            port_car.write_atomic(db, cached.read_bytes())
            log(f"wrote {db}")
        done[f"gamedb {name}"] = db
    return done


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="write the derived files under out/ (only missing ones)")
    p.add_argument("--game", type=Path, default=None, help="game folder (default: $DSF_GAME_DIR)")
    p.add_argument("--iso", type=Path, action="append", help="Forza disc images (default: assets/*.iso)")
    p.add_argument("--force", action="store_true", help="rewrite files that exist")
    v = sub.add_parser("vehiclestats", help="print vehiclestats.lua from a ScriptsLuaScripts.fchunk")
    v.add_argument("package", type=Path)
    v.add_argument("out", type=Path, nargs="?")
    args = parser.parse_args(argv)
    if args.cmd == "prepare":
        isos = args.iso or sorted((REPO / "assets").glob("*.iso"))
        for name, path in prepare(args.game or game_dir(), isos, force=args.force).items():
            print(f"{name}: {path}")
    else:
        text = vehiclestats_text(args.package)
        if args.out:
            args.out.write_text(text, encoding="utf-8", newline="\r\n")
        else:
            sys.stdout.write(text)


if __name__ == "__main__":
    sys.exit(main())
