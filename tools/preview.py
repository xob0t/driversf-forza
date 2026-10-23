"""Tiny software renderer for checking meshes without Blender.

Flat-shaded, z-buffered orthographic side, top and front views written as a
PNG. Pure Python; tens of thousands of triangles take a few seconds.

    python tools/preview.py model.obj out.png [--objects substr,...]
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

Triangle = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]


def write_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    rows = b"".join(b"\0" + bytes(pixels[y * width * 3:(y + 1) * width * 3]) for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def render(tris: list[Triangle], path: Path, size: int = 360,
           views: tuple[tuple[int, int, int, int], ...] = ((2, 1, 0, 1), (0, 2, 1, -1), (0, 1, 2, 1))) -> None:
    """views: (horizontal axis, vertical axis, depth axis, depth sign) per panel."""
    if not tris:
        raise ValueError("nothing to render")
    lo = [min(p[a] for t in tris for p in t) for a in range(3)]
    hi = [max(p[a] for t in tris for p in t) for a in range(3)]
    span = max(hi[a] - lo[a] for a in range(3)) * 1.05 or 1.0
    centre = [(hi[a] + lo[a]) / 2 for a in range(3)]
    width, height = size * len(views), size
    pixels = bytearray([24, 24, 28] * width * height)
    light = (0.4, 0.8, 0.45)
    for view, (h, v, d, sign) in enumerate(views):
        depth = [math.inf] * (size * size)
        for tri in tris:
            ax, ay, az = (tri[1][i] - tri[0][i] for i in range(3))
            bx, by, bz = (tri[2][i] - tri[0][i] for i in range(3))
            nx, ny, nz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
            shade = 60 + int(180 * abs(nx * light[0] + ny * light[1] + nz * light[2]) / length)
            pts = [((p[h] - centre[h]) / span * size + size / 2,
                    size / 2 - (p[v] - centre[v]) / span * size,
                    sign * (p[d] - centre[d])) for p in tri]
            x0, x1 = max(int(min(p[0] for p in pts)), 0), min(int(max(p[0] for p in pts)) + 1, size)
            y0, y1 = max(int(min(p[1] for p in pts)), 0), min(int(max(p[1] for p in pts)) + 1, size)
            (px, py, pz), (qx, qy, qz), (rx, ry, rz) = pts
            area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
            if abs(area) < 1e-9:
                continue
            for y in range(y0, y1):
                for x in range(x0, x1):
                    w0 = ((qx - x) * (ry - y) - (qy - y) * (rx - x)) / area
                    w1 = ((rx - x) * (py - y) - (ry - y) * (px - x)) / area
                    w2 = 1 - w0 - w1
                    if w0 < 0 or w1 < 0 or w2 < 0:
                        continue
                    z = w0 * pz + w1 * qz + w2 * rz
                    k = y * size + x
                    if z < depth[k]:
                        depth[k] = z
                        o = (y * width + view * size + x) * 3
                        pixels[o:o + 3] = bytes((shade, shade, min(255, shade + 12)))
    write_png(path, width, height, pixels)


def load_obj(path: Path, objects: list[str] | None = None) -> list[Triangle]:
    positions, tris, keep = [], [], True
    for line in path.read_text().splitlines():
        if line.startswith("o "):
            keep = not objects or any(s in line[2:] for s in objects)
        elif line.startswith("v "):
            positions.append(tuple(float(x) for x in line.split()[1:4]))
        elif line.startswith("f ") and keep:
            ids = [int(part.split("/")[0]) - 1 for part in line.split()[1:]]
            for k in range(1, len(ids) - 1):
                tris.append((positions[ids[0]], positions[ids[k]], positions[ids[k + 1]]))
    return tris


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("obj", type=Path)
    parser.add_argument("png", type=Path)
    parser.add_argument("--objects", help="comma-separated substrings of object names to include")
    args = parser.parse_args(argv)
    tris = load_obj(args.obj, args.objects.split(",") if args.objects else None)
    render(tris, args.png)
    print(f"rendered {len(tris)} triangles to {args.png}")


if __name__ == "__main__":
    sys.exit(main())
