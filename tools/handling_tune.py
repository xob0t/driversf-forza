"""Closed-loop in-game calibration of ported cars (optional; needs the sandbox).

The DSF-first mapping (tools/handling_dsf.py) gets a port close to how Reflections
tuned similar cars, but launch and handbrake rotation depend on many values at once
(research/handling.md, rounds 2 and 3). This tool measures the ported cars on the
sandbox dyno and nudges two values per car toward targets, a few iterations:

    p18   peak torque, a scale within +-25% of the rule's value, toward the 0-60 target
    p345  axle balance (FUN_008BD9B0), within +-0.05 of rule W, toward the handbrake
          rotation target (about -7 deg per +0.01, run 20261022-123844)

Targets: for a car DSF has, the stock car's own measurement (--stock 305=227); else
from the stock fleet: 0-60 from a fit of measured stock cars on power-to-weight and
drivetrain, rotation the median of the measured stock cars among the car's best-ranked
hosts (how Reflections tuned similar cars). Results go to out/cache/handling-tune.json
keyed "MEDIA@HOST"; handling_dsf.apply uses them (DSF_HANDLING_NOTUNE=1 ignores them).

    python tools/handling_tune.py stock <run dir>...          # add stock measurements
    python tools/handling_tune.py targets                     # print the target model
    python tools/handling_tune.py run --archive out/play/play.sp --lua out/play/play.lua \\
        --ids 305,309,308 --stock 305=227,309=228 [--iterations 3] [--dry-run]
    python tools/handling_tune.py show
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

import dyno
import handling as H
import handling_dsf as D
import host_select as HS
from chnk import read_chunk, write_chunk

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "out" / "cache" / "handling-tune.json"
STOCK_DB = REPO / "out" / "cache" / "stock-dyno.json"
RUNS = Path.home() / "AppData" / "Local" / "Temp" / "dsfforza-wsb" / "runs"
# The test: run 1 full throttle 25 s then brake (0-60, quarter mile); runs 2 and 3 a
# handbrake flick at 55 mph with the throttle lifted; run 4 a 1 s step steer at 45 mph.
KEYS = ("Up:25,Down:6;Up:@55,Space+Right:0.4,None:2.5,Down:5;Up:@55,Space+Right:0.4,None:2.5,Down:5;"
        "Up:@45,Right:1.0,None:1.5,Down:5")
KEEP = "impulse:1,impulse:1:20,impulse:1:20,impulse:1:18"
# Drift recovery (driving feedback: the Huayra port "can't exit the drift"): a handbrake
# flick held into a slide, then 1.2 s of counter-steer: on the throttle (run 2), lifted
# (run 3), and after a long handbrake pull (run 4). dyno.manoeuvre's recover_s is the time
# from the input until slip and yaw rate are calm again. Run 20261022-165036 used a 0.3 s
# flick with immediate counter-steer: no car got past 9 deg of slip, so no drift to leave.
# '@62' lands near 50 mph (the step timing comes from the launch run's trace).
RECOVERY_KEYS = ("Up:25,Down:6;Up:@62,Space+Right:0.6,Up+Right:0.6,Up+Left:1.2,Up:3,Down:5;"
                 "Up:@62,Space+Right:0.6,Right:0.4,Left:1.2,None:3,Down:5;"
                 "Up:@62,Space+Right:1.0,Left:1.2,None:3,Down:5")
P18_RANGE, P345_RANGE = 0.25, 0.05
P345_SLOPE = -700.0     # deg of handbrake rotation per unit of p345 (prior)
P18_ELASTICITY = -0.8   # d ln(0-60) / d ln(p18) (prior)
NOISE = {"0-60": 0.03, "hb_yaw": 8.0}  # relative / absolute dead bands


# --- measurements --------------------------------------------------------------------

def measure(run_dirs: list[Path]) -> dict[int, dict]:
    """Per car: 0-60, quarter mile (acceleration runs), handbrake yaw change (lifted
    flicks, mean and sd), step-steer yaw rate and lateral g."""
    runs, _ = dyno.parse(run_dirs)
    out: dict[int, dict] = {}
    for r in runs:
        rec = out.setdefault(r.car, {"acc": [], "hb": [], "steer": []})
        a = dyno.analyse(r)
        if "error" not in a and a.get("top_at_s", 0) > 15 and a.get("0-60"):
            rec["acc"].append(a)
            continue
        m = dyno.manoeuvre(r)
        if not m:
            continue
        # KEYS fixes the manoeuvre by run: 2 and 3 handbrake, 4 step steer (a step steer
        # can turn the car more than 25 deg, so the figures can't tell them apart).
        if r.run in (2, 3):
            rec["hb"].append(m)
        else:
            rec["steer"].append(m)
    summary = {}
    for car, rec in out.items():
        def mean(rows, key):
            vals = [x[key] for x in rows if x.get(key) is not None]
            return statistics.mean(vals) if vals else None

        def sd(rows, key):
            vals = [x[key] for x in rows if x.get(key) is not None]
            return statistics.pstdev(vals) if len(vals) > 1 else None
        summary[car] = {"0-60": mean(rec["acc"], "0-60"), "qm": mean(rec["acc"], "qm_time"),
                        "hb_yaw": mean(rec["hb"], "yaw_change"), "hb_yaw_sd": sd(rec["hb"], "yaw_change"),
                        "hb_mph": mean(rec["hb"], "v0_mph"), "hb_slip": mean(rec["hb"], "peak_slip"),
                        "steer_rate": mean(rec["steer"], "yaw_rate_steady"),
                        "steer_g": mean(rec["steer"], "lat_acc_steady"), "steer_mph": mean(rec["steer"], "v0_mph"),
                        "n": [len(rec["acc"]), len(rec["hb"]), len(rec["steer"])]}
    return summary


def load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1))


# --- targets -------------------------------------------------------------------------

def _fit(xs: list[list[float]], ys: list[float]) -> list[float]:
    k = len(xs[0])
    a = [[sum(x[i] * x[j] for x in xs) + (1e-3 * len(xs) if i == j and i else 0) for j in range(k)] for i in range(k)]
    b = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(k)]
    return D._solve(a, b)


def _x(specs: D.Specs) -> list[float]:
    return [1.0, math.log(specs.power / specs.weight), float(specs.drive == "AWD"), float(specs.drive == "FWD")]


class Targets:
    """0-60 from the measured stock cars' power-to-weight and drivetrain; rotation from
    the measured stock cars among a car's best-ranked hosts."""

    def __init__(self, stock: dict[str, dict], fleet: dict[int, dict]):
        self.stock = {int(k): v for k, v in stock.items()}
        self.fleet = fleet
        rows = [(D.specs_from_stats(fleet[i]["stats"]), v["0-60"]) for i, v in self.stock.items()
                if v.get("0-60") and i in fleet and D.specs_from_stats(fleet[i]["stats"])]
        self.beta = _fit([_x(s) for s, _ in rows], [math.log(t) for _, t in rows]) if len(rows) >= 6 else None
        self.rows = rows

    def zero_sixty(self, specs: D.Specs) -> float | None:
        if self.beta is None:
            return None
        return math.exp(sum(b * x for b, x in zip(self.beta, _x(specs))))

    def rotation(self, media: str, exclude: set[int] = frozenset()) -> tuple[float | None, list[int]]:
        info = HS.forza_info(media)
        same = {h["id"] for h in HS.survey() if HS.identity_score(h, info)[0] >= 60} | set(exclude)
        order = [r.id for r in HS.rank(info, HS.survey(), allow_special=True) if r.id not in same]
        used = [i for i in order if (self.stock.get(i) or {}).get("hb_yaw") is not None][:3]
        if not used:
            return None, []
        return statistics.median(self.stock[i]["hb_yaw"] for i in used), used

    def loo(self) -> None:
        """How well the model predicts the stock cars themselves (leave one out)."""
        errs = []
        for i, (specs, t) in enumerate(self.rows):
            rest = self.rows[:i] + self.rows[i + 1:]
            beta = _fit([_x(s) for s, _ in rest], [math.log(v) for _, v in rest])
            pred = math.exp(sum(b * x for b, x in zip(beta, _x(specs))))
            errs.append(abs(math.log(pred / t)))
        if errs:
            print(f"0-60 model, leave one out over {len(errs)} stock cars: median |log error| "
                  f"{statistics.median(errs):.3f}, mean {statistics.mean(errs):.3f}")


# --- archive patching ----------------------------------------------------------------

def ported_cars(lua: Path) -> dict[int, dict]:
    out = {}
    for m in re.finditer(r'\[(\d+)\] = \{ host = (\d+), media = "([^"]+)"', lua.read_text()):
        out[int(m[1])] = {"host": int(m[2]), "media": m[3]}
    return out


def patch_archive(src: Path, dst: Path, overrides: dict[int, dict[int, float]]) -> None:
    with src.open("rb") as stream:
        root = read_chunk(stream, 0)
        for sid, values in overrides.items():
            entry, _ = H.host_entries(stream, root.entries, sid)
            v = H.VO3D.parse(H.payload(stream, entry))
            for pid, val in values.items():
                v.params[int(pid)] = float(val)
            entry.payload = v.build()
        with dst.open("wb") as out:
            write_chunk(out, stream, root.entries, root.version)


def run_session(archive: Path, lua: Path, ids: list[int], wait: int = 3600, keys: str = KEYS,
                keep: str = KEEP) -> Path:
    shutil.copy(lua, REPO / "tools" / "game" / "lua" / "dsfforza" / "ported.lua")
    rel = archive.resolve().relative_to(REPO)
    cmd = ["powershell", "-NoProfile", "-File", str(REPO / "tools" / "wsb" / "run-series.ps1"),
           "-Archives", str(rel), "-ProbeIds", ",".join(map(str, ids)), "-WaitSeconds", str(wait),
           "-ShotSeconds", "5", "-Dyno", "-DynoRuns", "4", "-DynoKeep", keep, "-DynoKeys", keys]
    subprocess.run(cmd, cwd=REPO, check=False)
    latest = (RUNS / "latest.txt").read_text().strip().replace("C:\\Output\\runs\\", "")
    return RUNS / latest


# --- the loop ------------------------------------------------------------------------

def step(points: list[tuple[float, float]], target: float, prior: float) -> float:
    """Next x for y(x) = target: secant through the last two measured points when they
    differ in x and their slope has the prior's sign, else the prior slope."""
    x1, y1 = points[-1]
    slope = prior
    if len(points) > 1:
        x0, y0 = points[-2]
        if abs(x1 - x0) > 1e-9:
            s = (y1 - y0) / (x1 - x0)
            if s * prior > 0:
                slope = s
    return x1 + (target - y1) / slope


def tune(args) -> None:
    fleet = D.load_fleet()
    cars = ported_cars(args.lua)
    ids = [int(i) for i in args.ids.split(",")]
    stock_map = {int(a): int(b) for a, b in (p.split("=") for p in args.stock.split(","))} if args.stock else {}
    stock_db = load_json(STOCK_DB)
    targets = Targets(stock_db, fleet)
    cache = load_json(CACHE)
    base = {}
    for sid in ids:
        v = H.load_host(args.archive, sid).vo3d
        base[sid] = {18: v.get(18), 345: v.get(345)}
    state = {sid: {"x18": base[sid][18], "x345": base[sid][345], "hist": []} for sid in ids}
    work = args.archive.with_name(args.archive.stem + "-tune.sp")
    # Stock references not yet measured with this test (the stock command fills the cache).
    refs = sorted({stock_map[s] for s in ids if s in stock_map and not (stock_db.get(str(stock_map[s])) or {}).get("hb_yaw")}
                  - set(ids))
    for it in range(args.iterations):
        overrides = {sid: {18: st["x18"], 345: st["x345"]} for sid, st in state.items()}
        print(f"iteration {it + 1}: " + ", ".join(f"{sid}: p18 {o[18]:.0f} p345 {o[345]:.3f}"
                                                for sid, o in overrides.items()))
        if args.dry_run:
            break
        patch_archive(args.archive, work, overrides)
        session_ids = (refs if it == 0 else []) + ids
        # Sessions of at most 10 cars (the mission vehicle spool limit).
        chunks = [session_ids[i:i + 10] for i in range(0, len(session_ids), 10)]
        runs = [run_session(work, args.lua, chunk) for chunk in chunks]
        run = runs[-1]
        meas = measure(runs)
        if it == 0:
            for r in refs:
                if r in meas:
                    stock_db[str(r)] = meas[r]
            save_json(STOCK_DB, stock_db)
            targets = Targets(stock_db, fleet)
        for sid in ids:
            m = meas.get(sid)
            if not m:
                continue
            car = cars[sid]
            specs = D.specs_from_forza(car["media"], H.load_forza(car["media"], D.DBS))
            ref = stock_db.get(str(stock_map[sid])) if sid in stock_map else None
            t60 = ref["0-60"] if ref and ref.get("0-60") else targets.zero_sixty(specs)
            thb = ref["hb_yaw"] if ref and ref.get("hb_yaw") else targets.rotation(car["media"])[0]
            st = state[sid]
            st["hist"].append({"x18": st["x18"], "x345": st["x345"], **m, "t60": t60, "thb": thb, "run": run.name})
            pts18 = [(math.log(h["x18"]), math.log(h["0-60"])) for h in st["hist"] if h.get("0-60")]
            # A model target (no stock counterpart) is only good to about 15% (leave one
            # out over the stock cars), so it only bounds the port: act outside +-18%.
            band = NOISE["0-60"] if ref and ref.get("0-60") else 0.18
            if t60 and pts18 and abs(pts18[-1][1] - math.log(t60)) > band:
                # ln(0-60) is about linear in ln(p18)
                nxt = math.exp(step(pts18, math.log(t60), P18_ELASTICITY))
                st["x18"] = min(max(nxt, base[sid][18] * (1 - P18_RANGE)), base[sid][18] * (1 + P18_RANGE))
            pts345 = [(h["x345"], h["hb_yaw"]) for h in st["hist"] if h.get("hb_yaw") is not None]
            if thb is not None and pts345 and abs(pts345[-1][1] - thb) > NOISE["hb_yaw"]:
                nxt = step(pts345, thb, P345_SLOPE)
                st["x345"] = min(max(nxt, base[sid][345] - P345_RANGE), base[sid][345] + P345_RANGE)
            print(f"  {sid} {car['media']}: 0-60 {m['0-60']} (target {t60}), handbrake {m['hb_yaw']} "
                  f"(target {thb}) -> p18 {st['x18']:.0f}, p345 {st['x345']:.3f}")
        for sid, st in state.items():
            car = cars[sid]
            key = f"{car['media']}@{car['host']}"  # keeps hand-set values from driving feedback
            cache[key] = {**cache.get(key, {}), "18": st["x18"], "345": st["x345"], "history": st["hist"]}
        save_json(CACHE, cache)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("stock", help="add stock-car measurements from run folders")
    p.add_argument("runs", type=Path, nargs="+")
    sub.add_parser("targets", help="print the target model")
    sub.add_parser("show", help="print the tune cache")
    p = sub.add_parser("run", help="tune ported cars in the sandbox")
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--lua", type=Path, required=True)
    p.add_argument("--ids", required=True)
    p.add_argument("--stock", default="", help="port=stock pairs whose stock car sets the targets")
    p.add_argument("--iterations", type=int, default=3)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("recovery", help="drift-recovery test: run a session (--archive/--lua/--ids) or read run folders")
    p.add_argument("runs", type=Path, nargs="*")
    p.add_argument("--archive", type=Path)
    p.add_argument("--lua", type=Path)
    p.add_argument("--ids", default="")
    args = parser.parse_args(argv)
    if args.cmd == "stock":
        db = load_json(STOCK_DB)
        for car, m in measure(args.runs).items():
            if car < 304:
                db[str(car)] = m
                print(car, {k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()})
        save_json(STOCK_DB, db)
    elif args.cmd == "targets":
        t = Targets(load_json(STOCK_DB), D.load_fleet())
        print("0-60 model:", t.beta)
        t.loo()
    elif args.cmd == "show":
        for key, v in load_json(CACHE).items():
            if "18" in v and "345" in v:
                print(key, "p18", round(v["18"]), "p345", round(v["345"], 3), f"({len(v.get('history', []))} iterations)")
            else:
                print(key, {k: x for k, x in v.items() if k != "history"})
    elif args.cmd == "recovery":
        runs = list(args.runs)
        if args.archive:
            runs.append(run_session(args.archive, args.lua, [int(x) for x in args.ids.split(",")],
                                    keys=RECOVERY_KEYS, keep="impulse:1,impulse:1:20,impulse:1:20,impulse:1:20"))
        parsed, _ = dyno.parse(runs)
        print("car run  mph  peak_slip yaw_change recover_s slip_area")
        for r in parsed:
            m = dyno.manoeuvre(r) if r.run > 1 else None
            if m:
                print(f"{m['car']:>3} {m['run']:>3} {m['v0_mph']:4.0f} {m['peak_slip']:9.0f} {m['yaw_change']:10.0f} "
                      f"{m['recover_s'] if m['recover_s'] is None else round(m['recover_s'], 2)!s:>9} "
                      f"{m['slip_area'] if m['slip_area'] is None else round(m['slip_area'])!s:>9}")
    else:
        tune(args)


if __name__ == "__main__":
    sys.exit(main())
