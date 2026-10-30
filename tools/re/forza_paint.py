"""Forza's paint settings per car colour (research/paint-re.md, "Forza's paint data").

What a Forza (FH1, FM4) car colour draws with is its `body` technique's parameters, layered:

    Shared.zip ShaderSettings/Normal.xml        every technique's defaults
    Shared.zip ShaderSettings/Metallic.xml      over them when Combo_Colors.Metallic is 1
    <car>.zip ShaderSettings.xml                the car's own
    <car>.zip ColorShaderSettings<Sequence>.xml that colour's own (Combo_Colors.Sequence)

`survey` reads them from the disc images in place (no extraction) and reports which parameters
the cars and colours set and how often.

    uv run python tools/re/forza_paint.py survey [--iso assets/Forza.Horizon.XBOX360.iso ...] [--out FILE]
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from dsfforza import REPO
from dsfforza.formats.forzazip import ForzaZip
from dsfforza.formats.xiso import XIso

ISOS = {
    "fh1": [REPO / "assets" / "Forza.Horizon.XBOX360.iso"],
    "fm4": [REPO / "assets" / "Forza 4 [NTSCU][DVD1].iso", REPO / "assets" / "Forza 4 [NTSCU][DVD2].iso"],
}
BODY = ("body", "body_2", "body2")


def parse_settings(xml: str) -> dict[str, dict[str, object]]:
    """{technique: {parameter: value or (r, g, b, a)}} of a ShaderSettings XML."""
    out: dict[str, dict[str, object]] = {}
    xml = re.sub(r"<!--.*?-->", "", xml, flags=re.S)
    m = re.search(r"<ShaderSettings>(.*)</ShaderSettings>", xml, re.S | re.I)
    xml = m.group(1) if m else xml
    for tech, inner in re.findall(r"<(\w+)\s*>(.*?)</\1\s*>", xml, re.S):
        params = {}
        for name, attrs in re.findall(r"<(\w+)\s+([^/>]*)/>", inner):
            kv = dict(re.findall(r'(\w+)="([^"]*)"', attrs))
            if "value" in kv:
                params[name] = float(kv["value"])
            elif "r" in kv:
                params[name] = tuple(float(kv[c]) for c in "rgba" if c in kv)
        out[tech.lower()] = params
    return out


class _ZipStream(ForzaZip):
    def __init__(self, stream):
        self.zip = zipfile.ZipFile(stream)


def car_zips(game: str):
    """(media name, ForzaZip) for every car zip on the game's discs (each media once)."""
    seen = set()
    for iso in ISOS[game]:
        if not iso.exists():
            continue
        img = XIso(iso)
        for entry in img.files():
            m = re.match(r"media/cars/([^/]+)\.zip$", entry.path, re.I)
            if not m or m.group(1).lower() in seen or m.group(1).lower() == "shared":
                continue
            seen.add(m.group(1).lower())
            try:
                yield m.group(1), _ZipStream(io.BufferedReader(img.open(entry), 1 << 16))
            except zipfile.BadZipFile:
                continue


def shared_settings(game: str) -> dict[str, dict]:
    for iso in ISOS[game]:
        if not iso.exists():
            continue
        img = XIso(iso)
        try:
            entry = img.find("media/cars/Shared.zip")
        except (KeyError, FileNotFoundError):
            continue
        z = _ZipStream(io.BufferedReader(img.open(entry), 1 << 16))
        out = {}
        names = {i.filename.lower(): i for i in z.entries()}
        for name in ("Normal", "Metallic", "Matte"):
            info = names.get(f"shadersettings/{name.lower()}.xml")
            if info is not None:
                out[name] = parse_settings(z.read(info).decode("utf-8", "replace"))
        return out
    return {}


def combo_colours(game: str) -> dict[str, list[tuple[int, int, int]]]:
    """{media (lower case): [(Sequence, Metallic, RGB)]} from the cached gamedb."""
    db = sqlite3.connect(f"file:{(REPO / 'out' / 'cache' / game / 'gamedb.slt').as_posix()}?mode=ro", uri=True)
    out = defaultdict(list)
    for media, seq, met, rgb in db.execute(
        "SELECT lower(d.MediaName), c.Sequence, c.Metallic, c.RGB FROM Combo_Colors c "
        "JOIN Data_Car d ON d.Id = c.Ordinal ORDER BY c.Sequence"
    ):
        out[media].append((seq, met, rgb & 0xFFFFFF))
    return out


def cmd_survey(args) -> None:
    report = {}
    for game in args.games.split(","):
        shared = shared_settings(game)
        colours = combo_colours(game)
        cars = 0
        car_body = Counter()  # parameters car ShaderSettings.xml set on body
        colour_files = 0
        colour_params = Counter()
        colour_values = defaultdict(Counter)
        colours_out = []
        cars_with = 0
        for media, z in car_zips(game):
            cars += 1
            names = [i.filename for i in z.entries()]
            ss = next((n for n in names if n.lower() == "shadersettings.xml"), None)
            if ss:
                body = parse_settings(z.read(ss).decode("utf-8", "replace")).get("body", {})
                car_body.update(body.keys())
            cs = [n for n in names if re.match(r"colorshadersettings\d+\.xml$", n, re.I)]
            if cs:
                cars_with += 1
            for n in cs:
                colour_files += 1
                seq = int(re.search(r"(\d+)", n).group(1))
                body = parse_settings(z.read(n).decode("utf-8", "replace")).get("body", {})
                colour_params.update(body.keys())
                for k, v in body.items():
                    colour_values[k][str(v)] += 1
                row = next((c for c in colours.get(media.lower(), []) if c[0] == seq), None)
                colours_out.append(
                    {
                        "car": media,
                        "sequence": seq,
                        "combo": None if row is None else {"metallic": row[1], "rgb": f"#{row[2]:06x}"},
                        "body": body,
                    }
                )
        report[game] = {
            "cars": cars,
            "cars_with_colour_settings": cars_with,
            "colour_files": colour_files,
            "colour_params": colour_params.most_common(),
            "car_body_params": car_body.most_common(),
            "colour_values": {k: v.most_common(8) for k, v in colour_values.items()},
            "shared_body": {k: v.get("body", {}) for k, v in shared.items()},
            "colours": colours_out,
        }
        print(f"{game}: {cars} cars, {cars_with} with ColorShaderSettings ({colour_files} files)")
        print("  per-colour body parameters:", colour_params.most_common())
        print("  car ShaderSettings body parameters:", car_body.most_common(12))
        for k, v in colour_values.items():
            print(f"    {k}: {v.most_common(6)}")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, default=str))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("survey")
    s.add_argument("--games", default="fh1,fm4")
    s.add_argument("--out")
    args = ap.parse_args(argv)
    {"survey": cmd_survey}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
