"""Grab one frame from a VNC server (RFB 3.8, no auth, raw encoding) and save it as PNG.
Usage: uv run dsff vncshot host port out.png"""
import socket
import struct
import sys
import zlib
from pathlib import Path


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def write_png(path, w, h, rgb_rows):
    def chunk(kind, data):
        c = kind + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + row for row in rgb_rows)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    with Path(path).open("wb") as f:
        f.write(png)


def main():
    host, port, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    s = socket.create_connection((host, port), timeout=20)
    recv_exact(s, 12)
    s.sendall(b"RFB 003.008\n")
    n = recv_exact(s, 1)[0]
    types = recv_exact(s, n)
    if 1 not in types:
        raise SystemExit(f"no 'None' security type offered: {list(types)}")
    s.sendall(b"\x01")
    if struct.unpack(">I", recv_exact(s, 4))[0] != 0:
        raise SystemExit("security handshake failed")
    s.sendall(b"\x01")  # shared
    w, h = struct.unpack(">HH", recv_exact(s, 4))
    recv_exact(s, 16)
    name_len = struct.unpack(">I", recv_exact(s, 4))[0]
    recv_exact(s, name_len)
    # 32 bpp, depth 24, little endian, true colour, shifts r16 g8 b0.
    s.sendall(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
    s.sendall(struct.pack(">BxHi", 2, 1, 0))  # raw only
    s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
    fb = bytearray(w * h * 3)
    got = 0
    while got < w * h:
        msg = recv_exact(s, 1)[0]
        if msg != 0:
            raise SystemExit(f"unexpected message {msg}")
        recv_exact(s, 1)
        rects = struct.unpack(">H", recv_exact(s, 2))[0]
        for _ in range(rects):
            x, y, rw, rh, enc = struct.unpack(">HHHHi", recv_exact(s, 12))
            if enc != 0:
                raise SystemExit(f"unexpected encoding {enc}")
            data = recv_exact(s, rw * rh * 4)
            for row in range(rh):
                src = data[row * rw * 4:(row + 1) * rw * 4]
                dst = ((y + row) * w + x) * 3
                line = bytearray(rw * 3)
                line[0::3] = src[2::4]
                line[1::3] = src[1::4]
                line[2::3] = src[0::4]
                fb[dst:dst + rw * 3] = line
            got += rw * rh
    s.close()
    write_png(out, w, h, [bytes(fb[r * w * 3:(r + 1) * w * 3]) for r in range(h)])
    print(f"{out} {w}x{h}")


if __name__ == "__main__":
    main()
