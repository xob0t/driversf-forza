"""Run cockpit.fit over every car on the discs (research/forza-roster.tsv) and
list the failures by cause. Reads only <car>_cockpit.carbin, the main carbin
(for the fallback) and CarAttribs.xml from each car's zip, in memory; the tick
tables go to <work>/<game>/<media>/carattribs.xml so fit() can read them.

    uv run dsff cockpit_survey [--work out/cockpit-survey] [--limit N] [--car NAME ...]

Writes <work>/survey.tsv (one row per car) and prints a summary by cause.
"""

from __future__ import annotations

import argparse
import collections
import math
import os
import traceback
from pathlib import Path

from dsfforza import REPO
from dsfforza.build import port_car
from dsfforza.convert import cockpit
from dsfforza.formats import carbin


def read_car(source, media: str) -> dict[str, bytes]:
    """{lower-case relative name: bytes} for the cockpit carbin, main carbin and CarAttribs."""
    wanted = {f"{media}_cockpit.carbin".lower(), f"{media}.carbin".lower(), "carattribs.xml"}
    image, archive, prefix = port_car.open_car_zip(source)
    out = {}
    try:
        for info in archive.entries():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.lower().startswith(prefix):
                continue
            rel = name[len(prefix):].lower()
            if rel in wanted:
                out[rel] = archive.read(info)
    finally:
        image.close()
    return out


def survey_one(source, media: str, work: Path, dbs: list[Path]) -> dict:
    row = {"media": media, "game": source.game, "status": "ok", "cause": "", "wheel": "", "gauges": "",
           "eye_source": ""}
    files = read_car(source, media)
    folder = work / source.game.lower() / media
    folder.mkdir(parents=True, exist_ok=True)
    if "carattribs.xml" in files:
        (folder / "carattribs.xml").write_bytes(files["carattribs.xml"])
    raw = files.get(f"{media}_cockpit.carbin".lower())
    sections = []
    if raw is not None:  # without one, fit() builds the cabin from the main carbin
        parsed = carbin.parse_full(raw)
        if not parsed.complete:
            row.update(status="fail", cause=f"cockpit carbin parse: {len(parsed.sections)}/{parsed.declared} sections")
            return row
        sections = parsed.sections
    main = None
    if f"{media}.carbin".lower() in files:
        try:
            mp = carbin.parse_full(files[f"{media}.carbin".lower()])
        except carbin.CarbinError as error:
            mp = None  # the port fails on this anyway; still fit the cockpit
            row["main_error"] = f"main carbin parse: {error}"
        main = mp.sections if mp is not None and mp.complete else None
    try:
        cf = cockpit.fit(sections, media, dbs, folder=folder, main_sections=main)
    except Exception as error:
        row.update(status="fail", cause=f"{type(error).__name__}: {error}".splitlines()[0][:160])
        return row
    row["wheel"] = cf.wheel_source
    row["gauges"] = ",".join(sorted(cf.gauges))
    row["eye_source"] = cf.offset_source
    row["radius"] = round(cf.wheel.radius, 3)
    row["column_deg"] = round(math.degrees(math.asin(max(-1.0, min(1.0, cf.wheel.axis[1])))), 1)
    warnings = []
    if not 0.12 <= cf.wheel.radius <= 0.30:
        warnings.append("wheel radius")
    if not 0 <= row["column_deg"] <= 60:
        warnings.append("column tilt")
    if not {"speed", "tach"} <= set(cf.gauges):
        warnings.append("missing " + "/".join(f for f in ("speed", "tach") if f not in cf.gauges) + " needle")
    if main:
        stock = [x for x in main if not cockpit.SHELL_SKIP.match(x.name)
                 and x.name.lower() not in {n.lower() for n in cf.static} | {n.lower() for n in cf.shell_skip}]
        lods = sorted({sub.lod for x in stock for sub in x.subsections if sub.lod > 0})
        lod = min(lods[-1], 5) if lods else 0
        while lod >= 1 and cockpit.shell_blocks_view(stock, lod, cf.eye):
            lod -= 1  # convert_interior steps to finer shell LODs the same way
        row["shell_lod"] = lod
        if lods and lod < 1:
            warnings.append("every shell LOD closes the windscreen")
        elif lods and lod < min(lods[-1], 5):
            row["cause"] = (row["cause"] + "; " if row["cause"] else "") + f"shell LOD {lod}"
    if row.get("main_error"):
        warnings.append(row["main_error"])
    row["warnings"] = "; ".join(warnings)
    if cf.source != "cockpit":
        row["status"] = "fallback"
        row["cause"] = cf.source
    return row


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, default=REPO / "out" / "cockpit-survey")
    parser.add_argument("--cache", type=Path, default=REPO / "out" / "cache")
    parser.add_argument("--car", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # BelowNormal
    from dsfforza.build.forza_roster import roster
    isos = sorted((REPO / "assets").glob("*.iso"))
    cars, _ = roster(isos)
    dbs = {g: port_car.extract_gamedb(isos, g, args.cache) for g in ("FH1", "FM4")}
    names = sorted(m for m, c in cars.items() if c.kind == "car")
    if args.car:
        names = [n.lower() for n in args.car]
    if args.limit:
        names = names[:args.limit]
    args.work.mkdir(parents=True, exist_ok=True)
    rows = []
    for n, key in enumerate(names):
        car = cars[key]
        try:
            _, source = port_car.pick_source(cars, port_car.CarRequest(car.name, None))
            row = survey_one(source, car.name, args.work, [p for p in (dbs.get(source.game),) if p])
        except SystemExit as error:
            row = {"media": car.name, "game": "", "status": "fail", "cause": f"source: {error}"}
        except Exception as error:
            row = {"media": car.name, "game": "", "status": "fail",
                   "cause": f"{type(error).__name__}: {error}".splitlines()[0][:160]}
            traceback.print_exc()
        rows.append(row)
        print(f"{n + 1}/{len(names)} {row['media']} {row['status']} {row.get('cause', '')} {row.get('warnings', '')}",
              flush=True)
    cols = ["media", "game", "status", "cause", "wheel", "gauges", "radius", "column_deg", "shell_lod", "warnings", "eye_source"]
    with (args.work / "survey.tsv").open("w", encoding="utf-8") as fh:
        fh.write("\t".join(cols) + "\n")
        for r in rows:
            fh.write("\t".join(str(r.get(c, "")) for c in cols) + "\n")
    by = collections.Counter((r["status"], r["cause"].split(":")[0] if r["status"] == "fail" else r["cause"])
                             for r in rows)
    print("\nsummary:")
    for (status, cause), count in by.most_common():
        print(f"  {count:4}  {status:8} {cause}")
    warned = collections.Counter(w.split(" (")[0] for r in rows for w in r.get("warnings", "").split("; ") if w)
    print("warnings:")
    for w, count in warned.most_common():
        print(f"  {count:4}  {w}")


if __name__ == "__main__":
    main()
