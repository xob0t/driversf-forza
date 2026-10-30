"""Lamp test report: which parts of the screen brighten per lamp phase.

Reads a run folder of `run-test.ps1 -Lights` (screenshots
lights-<id>-<k>a.png / b.png, one pair per phase, phases as logged by
dsfforza\\lightstest.lua in Driver.log) and writes, per car, a sheet with each
phase's screenshot (the brighter of the a/b pair, so a blinking indicator
counts) next to its brightening against the "off" phase of the same lighting
(day or dark), amplified. A lamp that works shows as a bright patch where it
sits; the per-phase score (mean brightening in the lower two thirds of the
screen, where the cars are) is printed and saved in lights-report.json.

    uv run dsff lights_diff %LOCALAPPDATA%/Temp/dsfforza-wsb/runs/<stamp> [--scale 4]
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import zlib
from pathlib import Path


def read_png(path: Path) -> tuple[int, int, bytearray]:
    """8-bit RGB/RGBA, non-interlaced PNG as RGB bytes (enough for screenshots)."""
    data = path.read_bytes()
    pos, idat, width = 8, b"", 0
    while pos < len(data):
        length, kind = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or colour not in (2, 6) or interlace:
                raise ValueError(f"{path}: unsupported PNG (depth {depth}, colour {colour})")
            channels = 3 if colour == 2 else 4
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * channels
    out = bytearray(width * height * 3)
    prev = bytearray(stride)
    o = 0
    for y in range(height):
        f = raw[o]
        line = bytearray(raw[o + 1 : o + 1 + stride])
        o += 1 + stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        for x in range(width):
            out[(y * width + x) * 3 : (y * width + x) * 3 + 3] = line[x * channels : x * channels + 3]
    return width, height, out


def shrink(w: int, h: int, px: bytearray, scale: int) -> tuple[int, int, bytearray]:
    W, H = w // scale, h // scale
    out = bytearray(W * H * 3)
    for y in range(H):
        for x in range(W):
            acc = [0, 0, 0]
            for yy in range(y * scale, y * scale + scale):
                o = (yy * w + x * scale) * 3
                for c in range(3):
                    acc[c] += sum(px[o + c : o + scale * 3 : 3])
            n = scale * scale
            out[(y * W + x) * 3 : (y * W + x) * 3 + 3] = bytes(v // n for v in acc)
    return W, H, out


def caption_rows(run: Path) -> int:
    """Rows of the caption strip run-test.ps1 puts below each probe shot (result.json "shotCaptionPx")."""
    try:
        return int(json.loads((run / "result.json").read_text(encoding="utf-8-sig")).get("shotCaptionPx") or 0)
    except (OSError, ValueError, AttributeError):
        return 0


def read_frame(path: Path, caption: int) -> tuple[int, int, bytearray]:
    """A shot without its caption strip: the game's frame only."""
    w, h, px = read_png(path)
    h -= caption
    return w, h, px[: w * h * 3]


def phase_names(run: Path) -> dict[tuple[int, int], str]:
    names = {}
    log = run / "Driver.log"
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"\[dsfforza\.lights\] id (\d+): phase (\d+) (.+)$", line)
            if m:
                names[(int(m.group(1)), int(m.group(2)))] = m.group(3).strip()
    return names


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--scale", type=int, default=4, help="shrink factor for the sheets")
    parser.add_argument("--gain", type=float, default=4.0, help="amplification of the brightening")
    args = parser.parse_args(argv)
    from dsfforza.formats import vehicle_mesh as vm

    names = phase_names(args.run)
    caption = caption_rows(args.run)
    shots: dict[int, dict[int, list[Path]]] = {}
    for p in sorted(args.run.glob("lights-*-*.png")):
        m = re.match(r"lights-(\d+)-(\d+)([ab])\.png", p.name)
        if m:
            shots.setdefault(int(m.group(1)), {}).setdefault(int(m.group(2)), []).append(p)
    report = {}
    for car, phases in sorted(shots.items()):
        frames = {}
        for k, files in sorted(phases.items()):
            imgs = [shrink(*read_frame(f, caption), args.scale) for f in files]
            # The brighter of the pair, per pixel (an indicator may be off in one).
            w, h, px = imgs[0]
            for _, _, other in imgs[1:]:
                px = bytearray(max(a, b) for a, b in zip(px, other))
            frames[k] = (w, h, px)
        total = len(frames)
        half = total // 2 if total % 2 == 0 and total > 2 else total
        rows, scores = [], {}
        for k, (w, h, px) in sorted(frames.items()):
            base_k = 1 if k <= half else half + 1  # the "off" phase of the same lighting
            _bw, _bh, base = frames.get(base_k, frames[min(frames)])
            diff = bytearray(len(px))
            score = 0
            for i in range(0, len(px), 3):
                d = max(0, sum(px[i : i + 3]) - sum(base[i : i + 3])) / 3
                v = min(255, int(d * args.gain))
                diff[i : i + 3] = bytes((v, v, int(v * 0.6)))
                if i // 3 >= w * h // 3:
                    score += d
            scores[k] = round(score / max(1, w * h * 2 // 3), 2)
            rows.append((px, diff))
            print(f"id {car} phase {k:2} {names.get((car, k), '?'):24} brightening {scores[k]}")
        w, h = frames[min(frames)][:2]
        sheet = bytearray()
        for px, diff in rows:
            for y in range(h):
                sheet += px[y * w * 3 : (y + 1) * w * 3] + diff[y * w * 3 : (y + 1) * w * 3]
        out = args.run / f"lights-sheet-{car}.png"
        vm.write_png(out, w * 2, h * len(rows), sheet)
        report[car] = {
            "phases": {str(k): {"name": names.get((car, k)), "brightening": s} for k, s in scores.items()},
            "sheet": out.name,
        }
        print(f"wrote {out}")
    (args.run / "lights-report.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    sys.exit(main())
