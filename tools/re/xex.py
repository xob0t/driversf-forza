"""Xbox 360 XEX2 executables: headers, decryption and the loaded PE image (research/forza-lenses-re.md).

Reads a retail XEX2 (FH1's and FM4's default.xex): the optional headers, the security info,
the file format info, and writes the image as it is loaded at its base address (0x82000000
for both games), ready for a PowerPC disassembler (`ppc.py`). After xenia's xex_module.cc:
the session key is the security info's AES key decrypted (AES-128-ECB) with the retail XEX2
key, the data after the headers is one AES-128-CBC stream (IV 0), and "basic" compression is
a list of (data size, zero size) blocks. LZX ("normal") compression is not handled: neither
game uses it.

    uv run --with pycryptodome python tools/re/xex.py info out/re3b/fh1/default.xex
    uv run --with pycryptodome python tools/re/xex.py image out/re3b/fh1/default.xex out/re3b/fh1/image.bin
    uv run --with pycryptodome python tools/re/xex.py extract     both games' default.xex to out/re3b
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from dsfforza import REPO

DATA = REPO / "out" / "re3b"
RETAIL_KEY = bytes.fromhex("20B185A59D28FDC340583FBB0896BF91")
DEVKIT_KEY = bytes(16)

# optional header keys (xenia xex2_info.h)
ENTRY_POINT = 0x00010100
IMAGE_BASE = 0x00010201
FILE_FORMAT = 0x000003FF
IMPORT_LIBRARIES = 0x000103FF
EXECUTION_INFO = 0x00040006


@dataclass
class Xex:
    data: bytes

    def __post_init__(self):
        if self.data[:4] != b"XEX2":
            raise ValueError("not an XEX2")
        self.flags, self.pe_offset, _, self.sec_offset, n = struct.unpack(">5I", self.data[4:24])
        self.headers = dict(struct.unpack_from(">II", self.data, 24 + 8 * i) for i in range(n))
        s = self.sec_offset
        self.image_size = struct.unpack_from(">I", self.data, s + 4)[0]
        self.image_flags, self.load_address = struct.unpack_from(">II", self.data, s + 0x10C)
        self.file_key = self.data[s + 0x150 : s + 0x160]

    @property
    def base(self) -> int:
        return self.headers.get(IMAGE_BASE, self.load_address)

    @property
    def entry(self) -> int:
        return self.headers[ENTRY_POINT]

    def file_format(self) -> tuple[int, int, list[tuple[int, int]]]:
        """(encryption, compression, basic blocks as (data size, zero size))."""
        o = self.headers[FILE_FORMAT]
        size, enc, comp = struct.unpack_from(">IHH", self.data, o)
        blocks = [struct.unpack_from(">II", self.data, o + 8 + 8 * i) for i in range((size - 8) // 8)]
        return enc, comp, blocks

    def session_key(self, key: bytes = RETAIL_KEY) -> bytes:
        from Crypto.Cipher import AES

        return AES.new(key, AES.MODE_ECB).decrypt(self.file_key)

    def image(self) -> bytes:
        enc, comp, blocks = self.file_format()
        payload = self.data[self.pe_offset :]
        if enc == 1:
            from Crypto.Cipher import AES

            n = len(payload) // 16 * 16
            for key in (RETAIL_KEY, DEVKIT_KEY):
                plain = AES.new(self.session_key(key), AES.MODE_CBC, bytes(16)).decrypt(payload[:n])
                if plain[:2] == b"MZ":
                    payload = plain
                    break
            else:
                raise ValueError("neither the retail nor the devkit key gives an MZ header")
        if comp != 1:
            raise NotImplementedError(f"compression type {comp} (only basic, 1, is handled)")
        out = bytearray()
        p = 0
        for data_size, zero_size in blocks:
            out += payload[p : p + data_size]
            out += bytes(zero_size)
            p += data_size
        if len(out) < self.image_size:
            out += bytes(self.image_size - len(out))
        return bytes(out[: self.image_size])


def sections(image: bytes) -> list[tuple[str, int, int, int]]:
    """The PE's sections as (name, virtual address offset, virtual size, characteristics)."""
    pe = struct.unpack_from("<I", image, 0x3C)[0]
    if image[pe : pe + 4] != b"PE\0\0":
        raise ValueError("no PE header")
    n = struct.unpack_from("<H", image, pe + 6)[0]
    opt = struct.unpack_from("<H", image, pe + 20)[0]
    out = []
    for i in range(n):
        o = pe + 24 + opt + 40 * i
        name = image[o : o + 8].rstrip(b"\0").decode("latin-1")
        vsize, va = struct.unpack_from("<II", image, o + 8)
        flags = struct.unpack_from("<I", image, o + 36)[0]
        out.append((name, va, vsize, flags))
    return out


def extract(root: Path = DATA) -> None:
    from dsfforza.build import forza_roster
    from dsfforza.formats.xiso import XIso

    discs = forza_roster.game_discs(sorted((REPO / "assets").glob("*.iso")))
    for game, iso in (("fh1", discs.get("FH1")), ("fm4", discs.get("FM4"))):
        if iso is None:
            print(f"no {game} disc")
            continue
        x = XIso(iso)
        for e in x.files():
            if e.path.lower().replace("\\", "/") == "default.xex":
                dest = root / game / "default.xex"
                dest.parent.mkdir(parents=True, exist_ok=True)
                x.extract(e, dest)
                img = Xex(dest.read_bytes()).image()
                (root / game / "image.bin").write_bytes(img)
                print(game, dest, "->", root / game / "image.bin", len(img))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("info")
    a.add_argument("xex", type=Path)
    a = sub.add_parser("image")
    a.add_argument("xex", type=Path)
    a.add_argument("out", type=Path)
    sub.add_parser("extract")
    args = ap.parse_args(argv)
    if args.cmd == "extract":
        extract()
        return
    x = Xex(args.xex.read_bytes())
    if args.cmd == "info":
        enc, comp, blocks = x.file_format()
        print(f"base {x.base:#x} entry {x.entry:#x} image {x.image_size:#x} enc {enc} comp {comp}")
        print("blocks", [(hex(a), hex(b)) for a, b in blocks])
        for k, v in sorted(x.headers.items()):
            print(f"  header {k:#010x} {v:#x}")
        for name, va, vs, fl in sections(x.image()):
            print(f"  {name:8} {x.base + va:#010x} {vs:#9x} {fl:#010x}")
    elif args.cmd == "image":
        args.out.write_bytes(x.image())
        print(args.out)


if __name__ == "__main__":
    sys.exit(main())
