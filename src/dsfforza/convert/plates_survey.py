"""Roster survey of licence-plate placement (research/plates.md): every car of a
build planned on its host by the working tree's src/dsfforza/convert/plates.py and
by other versions of it, with what lies under each plate.

    uv run dsff plates_survey --build out/build --ref master=origin/master [--car NAME ...] [--jobs 2]
    uv run dsff plates_survey --summary out/plates-survey/rows.jsonl [--runs master,now]

Cars and hosts come from <build>/work/<media>.json (a port_batch build's car
reports; ok cars). Each car is loaded as `plates plan` loads it (cut-outs and trim
artwork applied), pickled under <work>/cache for the next run, and planned with
every version: "now" (the working tree) and each --ref NAME=GITREF (git show
GITREF:src/dsfforza/convert/plates.py). The per-LOD push is skipped (only LOD 1
is measured). One JSON row per car goes to <work>/rows.jsonl.

What lies under a plate, on the LOD-1 depth map of its end (#159, #213): the gap
between plate and face per cell; a cell over OPENING behind the plate, or with no
surface, is an opening. Flags: floating (median gap over 3 cm, or over 25% of the
plate over an opening), corner off (a 3 x 3-cell corner block's median gap over
3 cm), over a badge (narrow badge runs, as fallback_height) or a lamp or glass
(over 2%), across sections (under 90% on its main section and class), faces down
(tilt under -0.4) or up (over 0.5).

Runs at idle priority with --jobs processes (AGENTS.md: heavy offline jobs).
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import pickle
import subprocess
import sys
import traceback
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from dsfforza import REPO

PLATES = "src/dsfforza/convert/plates.py"


def idle() -> None:
    if sys.platform == "win32":
        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), 0x40)  # IDLE_PRIORITY_CLASS


def module(path: Path, name: str):
    """A plates.py version loaded from `path` as module `name` (once per process)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def build_cars(build: Path) -> list[tuple[str, Path, int]]:
    """(media, cache folder, host) of the build's ok cars."""
    out = []
    for f in sorted((build / "work").glob("*.json")):
        if f.name.endswith((".job.json", ".fp.json")):
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        if not d.get("ok") or "entry" not in d:
            continue
        game, media = Path(d["entry"]["cache"]).parts[-2:]
        out.append((d["media"], REPO / "out" / "cache" / game / media, d["host"]))
    return out


_hosts: dict = {}


def host(spool: int):
    """(host wheels, host plates) of a vanilla vehicle."""
    if spool not in _hosts:
        from dsfforza.convert import forza2dsf, plates
        from dsfforza.formats import upvh
        from dsfforza.formats.chnk import read_chunk
        from dsfforza.formats.vehicle_mesh import child, payload
        from dsfforza.game import game_paths

        with game_paths.vanilla(None, quiet=True).open("rb") as stream:
            root = read_chunk(stream, 0)
            full, _, exterior = upvh.entries_for(stream, root.entries, spool, None)[:3]
            hw = forza2dsf.host_wheels(
                payload(stream, child(child(full, "NONR"), "UPVH")),
                payload(stream, child(child(full, "NONR"), "MODL")),
                payload(stream, child(child(exterior, "NONR"), "MODL")),
            )
            _hosts[spool] = (hw, plates.host_plates(stream, full))
    return _hosts[spool]


def load(folder: Path, spool: int, cache: Path):
    """(body sections, Placement, ground) as `plates plan` sees them, cached."""
    path = cache / f"{folder.name}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    import dataclasses

    from dsfforza.convert import cutout, forza2dsf, trim_art
    from dsfforza.convert import forza_materials as fm

    car = forza2dsf.load_car(folder, folder.name)
    dims = forza2dsf.car_dims(car)
    opts = forza2dsf.ConvertOptions()
    lods = set(opts.lods.values()) | {opts.shadow_lod, opts.exterior_lod}
    body, _ = cutout.cut_alpha_tested(car.body, fm.AtlasCache(car.folder), lods=lods)
    body, _ = trim_art.split_trim_art(body, fm.AtlasCache(car.folder), lods=lods)
    car = dataclasses.replace(car, body=body)
    hw, _ = host(spool)
    place = forza2dsf.Placement(dy=hw.hub_y - dims.radius + dims.ride_height, dz=hw.axle_mid + dims.mid_z)
    out = (car.body, place, hw.hub_y - dims.radius)
    cache.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps(out, protocol=5))
    tmp.replace(path)
    return out


def measure(pm, plate, dm) -> dict:
    """What lies under `plate` on the depth map `dm` (pm: its plates module)."""
    s, y = plate.centre
    w, h = plate.size
    height, width = dm.depth.shape
    r0, r1 = max(0, int(round(dm.row(y + h / 2)))), min(height, int(round(dm.row(y - h / 2))) + 1)
    c0, c1 = max(0, int(round(dm.col(s - w / 2)))), min(width, int(round(dm.col(s + w / 2))) + 1)
    if r1 <= r0 or c1 <= c0:
        return {}
    rows, cols = slice(r0, r1), slice(c0, c1)
    win = dm.depth[rows, cols]
    ii, jj = np.mgrid[rows, cols]
    plane = plate.depth + plate.tilt * (dm.y_of(ii) - y) + getattr(plate, "bend", 0.0) * np.abs(dm.s_of(jj) - s)
    gap = win - plane
    opening = ~np.isfinite(gap) | (gap > 0.10)
    g = gap[~opening]
    badge = dm.feat == pm.FEAT_BADGE
    hidden_badge = (badge & (pm.run_widths(badge) * pm.PX < pm.BADGE_BAND))[rows, cols]
    lamp = ((dm.feat == pm.FEAT_LAMP) | (dm.cls == pm.CLASS["glass"]))[rows, cols]
    ok = np.isfinite(win)
    cells = Counter(zip(dm.sec[rows, cols][ok].tolist(), dm.cls[rows, cols][ok].tolist()))
    main = cells.most_common(1)[0][1] / max(1, int(ok.sum())) if cells else 0.0
    capped = np.where(np.isfinite(gap), np.minimum(gap, 0.2), 0.2)
    k = 3
    corners = [capped[:k, :k], capped[:k, -k:], capped[-k:, :k], capped[-k:, -k:]]
    return {
        "gap_med": float(np.median(g)) if g.size else None,
        "gap_p90": float(np.percentile(g, 90)) if g.size else None,
        "opening": float(opening.mean()),
        "badge": float(hidden_badge.mean()),
        "lamp": float(lamp.mean()),
        "main": float(main),
        "corner_gap": [round(float(np.median(c)), 4) for c in corners],
    }


def plan_with(pm, body, place, ground, hp, lods) -> dict:
    """{end: plate report + measure} of one plates module."""
    pm.lod_depths = lambda *a, **k: None  # the per-LOD push: not measured
    fields = pm.HostPlate.__dataclass_fields__
    hps = pm.HostPlates(
        {e: pm.HostPlate(**{k: v for k, v in vars(q).items() if k in fields}) for e, q in hp.plates.items()}, hp.body_y
    )
    pp = pm.plan(body, place, ground, hps, lods, log=lambda *a: None)
    tris = pm.body_triangles(body, 1 if 1 in lods else min(lods), place)
    pts = [p for t, *_ in tris for p in t]
    zs = [p[2] for p in pts]
    z_mid = (max(zs) + min(zs)) / 2
    hw = min(0.9, max(abs(p[0]) for p in pts) + 0.02)
    out = {}
    for p in pp.plates:
        part = [t for t in tris if (sum(q[2] for q in t[0]) / 3 > z_mid) == (p.end == "front")]
        dm = pm.depth_map(part, p.end, (-hw, hw), (ground + 0.05, ground + 1.6))
        out[p.end] = {**p.report(), **measure(pm, p, dm)}
    for end, r in pp.report["plates"].items():
        out.setdefault(end, {"source": r.get("source", "none"), "note": r.get("note", "")})
    return out


def survey_one(job) -> dict:
    idle()
    media, folder, spool, versions, cache = job
    try:
        body, place, ground = load(folder, spool, cache)
        _, hp = host(spool)
        lods = sorted({sub.lod for s in body for sub in s.subsections if sub.lod > 0})
        row = {"media": media, "host": spool}
        for name, path in versions:
            row[name] = plan_with(module(path, f"plates_{name}"), body, place, ground, hp, lods)
        return row
    except Exception as error:
        return {"media": media, "host": spool, "error": repr(error), "traceback": traceback.format_exc()}


# --- summary ------------------------------------------------------------------------------

SOURCES = ("recess", "flat", "forza-mesh", "host", "none")
FLAGS = (
    "floating",
    "corner off > 3 cm",
    "over a badge",
    "over a lamp",
    "across sections",
    "faces down (tilt < -0.4)",
    "faces up (tilt > 0.5)",
)


def flags(p: dict) -> set:
    f = set()
    if "gap_med" not in p:
        return f
    if (p["gap_med"] or 0) > 0.03 or p["opening"] > 0.25:
        f.add("floating")
    if max(p["corner_gap"]) > 0.03:
        f.add("corner off > 3 cm")
    if p["badge"] > 0.02:
        f.add("over a badge")
    if p["lamp"] > 0.02:
        f.add("over a lamp")
    if p["main"] < 0.9:
        f.add("across sections")
    if p["tilt"] < -0.4:
        f.add("faces down (tilt < -0.4)")
    if p["tilt"] > 0.5:
        f.add("faces up (tilt > 0.5)")
    return f


def summary(rows: list[dict], runs: list[str]) -> None:
    rows = [r for r in rows if all(n in r for n in runs)]
    print(f"{len(rows)} cars")
    ends = ("front", "rear")
    print("| |" + "|".join(f" {e} {n} " for e in ends for n in runs) + "|")
    print("| --- |" + " ---: |" * (len(ends) * len(runs)))

    def line(label, test):
        cells = [sum(1 for r in rows if test(r[n].get(e, {}))) for e in ends for n in runs]
        print(f"| {label} |" + "|".join(f" {c} " for c in cells) + "|")

    for src in SOURCES:
        line(src, lambda p, src=src: p.get("source", "none") == src)
    for flag in FLAGS:
        line(flag, lambda p, flag=flag: flag in flags(p))
    for flag in FLAGS[:2]:
        line(f"host fallbacks {flag}", lambda p, flag=flag: p.get("source") == "host" and flag in flags(p))
    if len(runs) < 2:
        return
    a, b = runs[0], runs[-1]

    def key(p):
        return (p.get("source"), p.get("centre_s_y"), p.get("tilt"), p.get("bend", 0), p.get("size"))

    changed = Counter(e for r in rows for e in ends if key(r[a].get(e, {})) != key(r[b].get(e, {})))
    cars = sum(1 for r in rows if any(key(r[a].get(e, {})) != key(r[b].get(e, {})) for e in ends))
    print(f"{a} -> {b}: {dict(changed)} plates change, on {cars} cars")
    for e in ends:
        for n in (a, b):
            ps = [
                r[n][e] for r in rows if r[n].get(e, {}).get("source") == "host" and r[n][e].get("gap_med") is not None
            ]
            if ps:
                med = np.median([p["gap_med"] for p in ps]) * 1000
                corner = [max(p["corner_gap"]) * 1000 for p in ps]
                print(
                    f"{e} host fallbacks, {n}: {len(ps)}, median gap {med:.1f} mm (median of plates), worst corner "
                    f"median {np.median(corner):.0f} mm, p75 {np.percentile(corner, 75):.0f} mm"
                )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build", type=Path, help="a port_batch build folder (its work/<media>.json)")
    parser.add_argument("--ref", action="append", default=[], help="NAME=GITREF: also plan with that plates.py")
    parser.add_argument("--car", action="append", default=[], help="only these cars (media names)")
    parser.add_argument("--cars", type=Path, help="only the cars listed in this file")
    parser.add_argument("--work", type=Path, default=REPO / "out" / "plates-survey")
    parser.add_argument("--out", type=Path, help="rows (default <work>/rows.jsonl)")
    parser.add_argument("--resume", action="store_true", help="append, skipping cars already in --out")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--summary", type=Path, help="print the tables of a rows file and stop")
    parser.add_argument("--runs", default="", help="with --summary: versions to compare (default: all)")
    args = parser.parse_args(argv)
    if args.summary:
        rows = [json.loads(line) for line in args.summary.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = [r for r in rows if "error" not in r]
        runs = args.runs.split(",") if args.runs else [k for k in rows[0] if k not in ("media", "host")]
        summary(rows, runs)
        return
    if not args.build:
        parser.error("--build or --summary")
    idle()
    versions = []
    for spec in args.ref:
        name, _, ref = spec.partition("=")
        path = args.work / "versions" / f"plates_{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        src = subprocess.run(["git", "show", f"{ref}:{PLATES}"], cwd=REPO, capture_output=True, check=True).stdout
        path.write_bytes(src)
        versions.append((name, path))
    versions.append(("now", REPO / PLATES))
    keep = set(args.car) | (set(args.cars.read_text(encoding="utf-8").split()) if args.cars else set())
    cars = [c for c in build_cars(args.build) if not keep or c[0] in keep]
    out = args.out or args.work / "rows.jsonl"
    done = set()
    if args.resume and out.exists():
        done = {json.loads(line)["media"] for line in out.read_text(encoding="utf-8").splitlines() if line.strip()}
    jobs = [(m, f, h, versions, args.work / "cache") for m, f, h in cars if m not in done]
    print(f"{len(jobs)} cars, versions {', '.join(n for n, _ in versions)}", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a" if args.resume else "w", encoding="utf-8") as f, Pool(args.jobs) as pool:
        for n, row in enumerate(pool.imap_unordered(survey_one, jobs), 1):
            f.write(json.dumps(row) + "\n")
            f.flush()
            if "error" in row or n % 25 == 0:
                print(n, row["media"], row.get("error", ""), flush=True)


if __name__ == "__main__":
    main()
