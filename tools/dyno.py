"""Acceleration and braking figures from the in-game dyno (run-test.ps1 -Dyno).

The probe (tools/game/lua/dsfforza/dyno.lua) logs one line per Lua update while a
car runs on the straight:

    [dsfforza] dy <id> <run> <n> <speed m/s> <x> <y> <z>

The Lua update rate isn't known, so time comes from the samples: while the car
moves, each update lasts |position step| / mean speed; the median of that is the
update period, used where the car is too slow for the ratio to be accurate.

    python tools/dyno.py <Driver.log | run dir>... [--labels out/x.json] [--csv out.csv] [--trace ID]

Figures per run: 0-30, 0-60, 0-100 mph (s), quarter mile time and trap speed,
speed after 10 s and 20 s, top speed reached, 60-0 mph braking distance (m) and
mean deceleration (g), the update period (s) and the lateral drift (m).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

MPH = 2.23694
QUARTER_MILE = 402.336
G = 9.81
LINE = re.compile(r"\[dsfforza\] dy (\d+) (\d+) (\d+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"
                  r"(?: ([-\d.]+))?(?: ([-\d.]+))?(?: ([-\d.]+))?")
END = re.compile(r"\[dsfforza\] dyno (\d+) r(\d+) end (\S+)")
INFO = re.compile(r"\[dsfforza\] dyno (\d+): (topSpeed=.*)")


@dataclass
class Run:
    car: int
    run: int
    n: list[int] = field(default_factory=list)
    v: list[float] = field(default_factory=list)
    pos: list[tuple[float, float, float]] = field(default_factory=list)
    lateral: list[float] = field(default_factory=list)
    yaw: list[float] = field(default_factory=list)
    end: str = ""


def parse(paths: list[Path]) -> tuple[list[Run], dict[int, str]]:
    runs: dict[tuple[int, int, int], Run] = {}
    info: dict[int, str] = {}
    for file_index, path in enumerate(paths):
        if path.is_dir():
            path = path / "Driver.log"
        for line in path.read_text(encoding="latin-1", errors="replace").splitlines():
            m = LINE.search(line)
            if m:
                car, run, n = int(m[1]), int(m[2]), int(m[3])
                r = runs.setdefault((file_index, car, run), Run(car, run))
                if r.n and n <= r.n[-1]:  # the probe restarted the run (placing again)
                    r = runs[(file_index, car, run)] = Run(car, run)
                r.n.append(n)
                r.v.append(float(m[4]))
                r.pos.append((float(m[5]), float(m[6]), float(m[7])))
                r.lateral.append(float(m[8]) if m[8] is not None else 0.0)
                r.yaw.append(float(m[9]) if m[9] is not None else 0.0)
                continue
            m = END.search(line)
            if m and (file_index, int(m[1]), int(m[2])) in runs:
                runs[(file_index, int(m[1]), int(m[2]))].end = m[3]
                continue
            m = INFO.search(line)
            if m:
                info[int(m[1])] = m[2]
    return list(runs.values()), info


def _cross(times: list[float], values: list[float], target: float, start: int = 0, rising: bool = True):
    """Interpolated time where values first cross target (from start on)."""
    for i in range(max(start, 1), len(values)):
        a, b = values[i - 1], values[i]
        if (rising and a < target <= b) or (not rising and a > target >= b):
            return times[i - 1] + (times[i] - times[i - 1]) * (target - a) / (b - a)
    return None


def analyse(r: Run) -> dict:
    """Figures for one run. Samples before "go" are the idle ones (every 60 updates)."""
    # The launch: the first sample above 1 m/s (the throttle; idle creep and the
    # probe's settling stay below), with the samples just before it.
    # A creep from the harness's zoom taps can pass 1 m/s long before the throttle
    # (first car of a session), so take the last sample under 1 m/s before 5 m/s.
    fast = next((i for i, v in enumerate(r.v) if v > 5.0), None)
    go = None
    if fast is not None:
        go = next((i + 1 for i in range(fast - 1, -1, -1) if r.v[i] <= 1.0), 0)
        go = next((i for i in range(go, fast + 1) if r.v[i] > 1.0), fast)
    if go is None or len(r.v) - go < 10:
        return {"car": r.car, "run": r.run, "error": "no motion"}
    first = go
    while first > 0 and r.n[first] - r.n[first - 1] == 1 and r.v[first - 1] > 0.05 and first > go - 100:
        first -= 1
    n, v, pos = r.n[first:], r.v[first:], r.pos[first:]
    # Update period from distance / speed while moving reasonably fast.
    steps = []
    for i in range(1, len(n)):
        dn = n[i] - n[i - 1]
        vm = (v[i] + v[i - 1]) / 2
        if dn == 1 and vm > 5:
            steps.append((math.dist(pos[i], pos[i - 1]), vm))
    if len(steps) < 20:
        return {"car": r.car, "run": r.run, "error": f"only {len(steps)} moving samples"}
    period = sum(s for s, _ in steps) / sum(vm for _, vm in steps)
    t = [(k - n[0]) * period for k in n]
    # t = 0 where a straight line through the first samples between 1 and 5 m/s
    # reaches the speed the car had before the throttle (normally 0).
    rest = min(v[:go - first + 1])
    launch = [(t[i], v[i]) for i in range(len(v)) if 1.0 <= v[i] <= 5.0 and i < len(v) // 2]
    launch = launch[:max(3, len(launch))]
    if len(launch) >= 3:
        mt = statistics.mean(x for x, _ in launch)
        mv = statistics.mean(y for _, y in launch)
        slope = sum((x - mt) * (y - mv) for x, y in launch) / sum((x - mt) ** 2 for x, _ in launch)
        t0 = mt - (mv - rest) / slope if slope > 0 else t[go]
    else:
        t0 = t[go]
    t = [x - t0 for x in t]
    dist = [0.0]
    for i in range(1, len(pos)):
        dist.append(dist[-1] + math.dist(pos[i], pos[i - 1]))
    # Distance from the launch point (t = 0) on.
    i0 = next((i for i in range(len(t)) if t[i] >= 0), 0)
    dist = [d - dist[i0] for d in dist]
    mph = [x * MPH for x in v]
    peak = max(range(len(v)), key=v.__getitem__)
    out = {"car": r.car, "run": r.run, "end": r.end, "period": period}
    for target in (30, 60, 100):
        out[f"0-{target}"] = _cross(t[:peak + 1], mph[:peak + 1], target)
    tq = _cross(t, dist, QUARTER_MILE)
    out["qm_time"] = tq
    out["qm_mph"] = None
    if tq is not None:
        i = next(i for i in range(len(t)) if t[i] >= tq)
        out["qm_mph"] = mph[i - 1] + (mph[i] - mph[i - 1]) * (tq - t[i - 1]) / (t[i] - t[i - 1])
    for at in (10, 20):
        i = next((i for i in range(len(t)) if t[i] >= at), None)
        out[f"mph@{at}s"] = mph[i] if i is not None and i <= peak else None
    out["top_mph"] = mph[peak]
    out["top_at_s"] = t[peak]
    out["dist_at_top"] = dist[peak]
    # Braking: from the 60 mph crossing after the peak to the stop.
    stop = next((i for i in range(peak, len(v)) if v[i] < 0.3), None)
    brake_from = 60 if mph[peak] > 65 else math.floor(mph[peak] / 10) * 10 - 10
    if stop is not None and brake_from >= 20:
        tb = _cross(t, mph, brake_from, start=peak, rising=False)
        if tb is not None:
            i = next(i for i in range(peak, len(t)) if t[i] >= tb)
            db = dist[i - 1] + (dist[i] - dist[i - 1]) * (tb - t[i - 1]) / (t[i] - t[i - 1])
            out["brake_from"] = brake_from
            out["brake_m"] = dist[stop] - db
            out["brake_s"] = t[stop] - tb
            out["brake_g"] = brake_from / MPH / (t[stop] - tb) / G
    # How well the lane keeper held the line, and the height change (should be ~0).
    out["max_lat"] = max(abs(x) for x in r.lateral[first:first + peak + 1]) if r.lateral else None
    out["climb_m"] = pos[peak][1] - pos[0][1]
    return out


COLUMNS = ["car", "run", "0-30", "0-60", "0-100", "qm_time", "qm_mph", "mph@10s", "mph@20s", "top_mph",
           "top_at_s", "brake_from", "brake_m", "brake_g", "max_lat", "period", "end"]


def fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 1 else f"{value:.2f}"
    return str(value)


def summarise(rows: list[dict]) -> dict[int, dict]:
    """Mean and spread per car over its runs."""
    out = {}
    for car in sorted({r["car"] for r in rows}):
        mine = [r for r in rows if r["car"] == car and "error" not in r]
        s = {"car": car, "runs": len(mine)}
        for key in COLUMNS[2:-2]:
            values = [r[key] for r in mine if r.get(key) is not None]
            if values:
                s[key] = statistics.mean(values)
                s[key + "_sd"] = statistics.stdev(values) if len(values) > 1 else 0.0
        out[car] = s
    return out


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def manoeuvre(r: Run, period: float = 0.008, window: float = 3.0) -> dict | None:
    """Response to a scripted steer or handbrake input (runs with the keeper released):
    from the first yaw rate above 0.15 rad/s while the car is fast. Figures: speed at the
    input, peak yaw rate (deg/s), time to it, yaw change and velocity-direction change over
    the window (deg), peak slip angle between body and velocity (deg), peak lateral
    acceleration (m/s^2), speed lost over the window, and the time until the yaw rate is
    back under 10% of its peak."""
    if len(r.yaw) < 50 or not any(r.yaw):
        return None
    n = len(r.v)
    step = 5  # samples per derivative step (0.04 s, one physics step)
    rate = [0.0] * n
    vel_dir = [0.0] * n
    for i in range(step, n):
        dt = (r.n[i] - r.n[i - step]) * period
        rate[i] = _wrap(r.yaw[i] - r.yaw[i - step]) / dt if dt > 0 else 0.0
        dx, dz = r.pos[i][0] - r.pos[i - step][0], r.pos[i][2] - r.pos[i - step][2]
        vel_dir[i] = math.atan2(dx, dz) if math.hypot(dx, dz) > 0.05 else vel_dir[i - 1]
    start = next((i for i in range(step * 2, n) if r.v[i] > 15 and abs(rate[i]) > math.radians(8.6)), None)
    if start is None:
        return None
    end = next((i for i in range(start, n) if (r.n[i] - r.n[start]) * period >= window
                or (r.lateral and abs(r.lateral[i]) > 12)), n - 1)  # 12 m: before the kerbs
    seg = range(start, end + 1)
    peak = max(seg, key=lambda i: abs(rate[i]))
    slip = [abs(_wrap(r.yaw[i] - vel_dir[i])) for i in seg]
    lat = []
    for i in seg:
        if i - step >= start:
            dt = (r.n[i] - r.n[i - step]) * period
            lat.append(abs(_wrap(vel_dir[i] - vel_dir[i - step]) / dt * r.v[i]))
    settle = next(((r.n[i] - r.n[start]) * period for i in range(peak, end + 1)
                   if abs(rate[i]) < 0.1 * abs(rate[peak])), None)
    # Step steer: the yaw rate and lateral acceleration held 0.4 to 0.9 s into the input.
    # Lane change: the largest yaw rate of the opposite sign (the counter-steer and any
    # overshoot), as a share of the first peak.
    sign = 1 if rate[peak] > 0 else -1
    steady = [i for i in seg if 0.4 <= (r.n[i] - r.n[start]) * period <= 0.9]
    lat_steady = []
    for i in steady:
        if i - step >= start:
            dt = (r.n[i] - r.n[i - step]) * period
            lat_steady.append(abs(_wrap(vel_dir[i] - vel_dir[i - step]) / dt * r.v[i]))
    reverse = max((-sign * rate[i] for i in seg), default=0.0)
    # Drift recovery (handbrake flick then counter-steer): over up to 8 s, the time from the
    # input until the body is back in line with its path (slip under 5 deg, yaw rate under
    # 10 deg/s, held 0.25 s) after a slide of at least 15 deg; slip_area integrates the slip
    # (deg s). None when the car never got sideways or went 20 m off its line first (the
    # four lanes and the pavement; recovery runs flick briefly and counter-steer at once).
    recover = slip_area = None
    far = next((i for i in range(start, n) if (r.n[i] - r.n[start]) * period >= 8.0
                or (r.lateral and abs(r.lateral[i]) > 20)), n - 1)
    rslip = [abs(_wrap(r.yaw[i] - vel_dir[i])) for i in range(start, far + 1)]
    if rslip and max(rslip) > math.radians(15):
        top = start + rslip.index(max(rslip))
        calm = None
        for i in range(top, far + 1):
            ok = rslip[i - start] < math.radians(5) and abs(rate[i]) < math.radians(10)
            calm = (calm if calm is not None else i) if ok else None
            if calm is not None and (r.n[i] - r.n[calm]) * period >= 0.25:
                recover = (r.n[calm] - r.n[start]) * period
                break
        slip_area = sum(math.degrees(s) for s in rslip) * (r.n[far] - r.n[start]) * period / len(rslip)
    return {"car": r.car, "run": r.run, "v0_mph": r.v[start] * MPH,
            "peak_yaw_rate": math.degrees(abs(rate[peak])), "t_peak": (r.n[peak] - r.n[start]) * period,
            "yaw_rate_steady": math.degrees(statistics.mean(abs(rate[i]) for i in steady)) if steady else None,
            "lat_acc_steady": statistics.mean(lat_steady) / 9.81 if lat_steady else None,
            "reverse_ratio": max(reverse, 0.0) / abs(rate[peak]) if rate[peak] else None,
            "yaw_change": math.degrees(abs(_wrap(r.yaw[end] - r.yaw[start]))),
            "path_change": math.degrees(abs(_wrap(vel_dir[end] - vel_dir[start]))),
            "peak_slip": math.degrees(max(slip)), "peak_lat_acc": max(lat) if lat else None,
            "speed_loss_mph": (r.v[start] - r.v[end]) * MPH, "settle_s": settle,
            "recover_s": recover, "slip_area": slip_area}


MAN_COLUMNS = ["car", "run", "v0_mph", "peak_yaw_rate", "t_peak", "yaw_change", "path_change", "peak_slip",
               "peak_lat_acc", "yaw_rate_steady", "lat_acc_steady", "reverse_ratio", "speed_loss_mph", "settle_s",
               "recover_s", "slip_area"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", type=Path, nargs="+", help="Driver.log files or run folders")
    parser.add_argument("--labels", type=Path, action="append", default=[],
                        help="port_car manifest (out/x.json) to name the ported IDs")
    parser.add_argument("--csv", type=Path, help="write every run's figures here")
    parser.add_argument("--json", type=Path, help="write per-car means here")
    parser.add_argument("--trace", type=int, help="print the speed trace of this car (t, mph, distance)")
    args = parser.parse_args(argv)

    labels: dict[int, str] = {}
    for manifest in args.labels:
        data = json.loads(manifest.read_text())
        for car in data.get("cars", data if isinstance(data, list) else []):
            if isinstance(car, dict) and "id" in car:
                labels[int(car["id"])] = f"{car.get('name', car.get('car', '?'))} on {car.get('host', '?')}"
    runs, info = parse(args.logs)
    rows = [analyse(r) for r in runs]
    mans = [m for m in (manoeuvre(r) for r in runs) if m]
    if mans:
        print("# manoeuvres (scripted steer / handbrake)")
        print("\t".join(MAN_COLUMNS))
        for m in mans:
            print("\t".join(fmt(m.get(c)) for c in MAN_COLUMNS))
        print()
    print("\t".join(COLUMNS))
    for row in rows:
        if "error" in row:
            print(f"{row['car']}\t{row['run']}\t{row['error']}")
            continue
        print("\t".join(fmt(row.get(c)) for c in COLUMNS))
    print()
    summary = summarise(rows)
    keys = ["0-60", "0-100", "qm_time", "qm_mph", "mph@20s", "top_mph", "brake_m"]
    print("car\truns\t" + "\t".join(keys) + "\tlabel")
    for car, s in summary.items():
        cells = [f"{s[k]:.2f}±{s[k + '_sd']:.2f}" if k in s else "-" for k in keys]
        print(f"{car}\t{s['runs']}\t" + "\t".join(cells) + f"\t{labels.get(car, '')}")
    for car, text in sorted(info.items()):
        print(f"# {car}: {text}")
    if args.csv:
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
            w.writeheader()
            w.writerows(rows)
    if args.json:
        args.json.write_text(json.dumps({"runs": rows, "cars": summary, "info": info}, indent=1))
    if args.trace is not None:
        for r in runs:
            if r.car == args.trace:
                print(f"# trace {r.car} run {r.run}")
                for k, v, p in zip(r.n, r.v, r.pos):
                    print(k, f"{v * MPH:.2f}", *(f"{c:.2f}" for c in p))


if __name__ == "__main__":
    sys.exit(main())
