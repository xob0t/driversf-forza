"""DSF's straight-line drive model, decoded from Driver.exe 1.04, and what it predicts.

One vehicle physics step (FUN_008C0DF0, 25 Hz: the engine integrates with 0.04 s and the
body takes the summed forces as impulses per step) as research/handling.md, "Drive force,
drag and coasting (decoded)", describes it: the engine and driveline (FUN_008C3CF0), the
driven wheels set from the driveline (FUN_008C0DF0), the longitudinal tyre force from the
slip (FUN_008B8110), the axle force scales of the assists (FUN_008BD9B0), rolling
resistance (FUN_008BA760), drag (FUN_008BE410) and the governor (FUN_008BC4D0). Every
constant is the game's (`out/ghidra/e516-consts.txt`, addresses in the comments).
Not modelled: tyre saturation and the launch assist (so no 0-60 below third gear), the
load-dependent shift test of FUN_008C2F00 (upshift at p183 x p42 of the ground rpm), the
lateral terms.

    uv run dsff handling_sim check <Driver.log> [--set 214:13=0,509=0 ...]
    uv run dsff handling_sim pairs [--csv out/x.csv]

`check` compares the measured full-throttle acceleration of a dyno session (run-series
-Dyno, runs 1 and 3) with the model, gear by gear. `pairs` converts the Forza version of
every vanilla/Forza pair (handling_dsf.PAIRS) with today's rules, with each candidate
Forza-derived parameter on its own and with all of them, and compares each with DSF's
own car: the parameter values and the model's predicted acceleration and top speed.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dsfforza.build import handling as H
from dsfforza.build import handling_dsf as D

DT = 0.04  # s per physics step (0x00DDB90C); 25 steps per second (0x00DD6970)
IMPULSE = 25.0  # N per accumulated force unit: forces are impulses per step (1 / DT)
RPM = math.pi / 30  # 0x00E3A5C0
MPH = 2.23694
TORQUE = 0.25  # 0x00F3F368: engine torque = TORQUE x p18 x curve
DRIVELINE_FRICTION = 0.002  # 0x00DE5850: E[0x19], x driveline speed
GOVERNOR = (0.9, 4.0)  # 0x00F3F318, 0x00DD6878: the throttle fades as ((x - 0.9) / 0.1)^4
MPH_TO_MS = 0.447019  # 0x00DE37A0 (p160 is in mph)
# Forza's drag column read as 1e-4 lbf per mph^2 (hypothesis, not decoded: see pairs()).
LBF_MPH2 = 4.4482216 / 0.44704**2 * 1e-4


Params = Callable[[int], float]


def accessor(params: dict[int, float], over: dict[int, float] | None = None) -> Params:
    """p(i): the value the engine uses (FUN_0088AC20's default where the VO3D has none)."""
    over = over or {}

    def get(pid: int) -> float:
        if pid in over:
            return over[pid]
        val = params.get(pid)
        return H.EXE_DEFAULTS.get(pid, 0.0) if val is None else val

    return get


def torque_curve(p: Params, rpm: float, throttle: float = 1.0) -> float:
    """FUN_008C3CF0: the curve (p522 at idle p40, 1 at p524, p523 at the redline p42) times
    the rev band, which fades from the throttle's target rpm to 0 over p364 of it."""
    x = max(rpm - p(40), 0.0) / (p(42) - p(40))
    p522, p523, p524 = p(522), p(523), p(524)
    c = x / p524 * (1 - p522) + p522 if x < p524 else 1 - (x - p524) / (1 - p524) * (1 - p523)
    low = (1 - p(364)) * p(40)
    target = (p(42) - low) * throttle + low
    band = min(max((target - rpm) / max(100.0, p(364) * target) + 1, 0.0), 1.0)  # 100: 0x00DD6874
    return max(c * band, 0.0)


def governor(p: Params, v: float, throttle: float) -> float:
    """FUN_008BC4D0: controls[0], which scales the engine torque, its friction, the
    driveline friction, drag and rolling resistance."""
    x = v / (p(160) * MPH_TO_MS)
    if x < GOVERNOR[0]:
        return 1.0
    if x <= 1:
        return 1 - ((x - GOVERNOR[0]) / (1 - GOVERNOR[0])) ** GOVERNOR[1] * throttle
    return 1 - throttle


def axle_scale(p: Params, v: float, *, rear: bool) -> float:
    """FUN_008BD9B0 -> FUN_008B8110's local_1c, straight ahead: the front/rear balance
    p345 + p511 (v / 1000)^2 and the level 1 + p510 (v / 1000)^2 (0x00DD6EA8 = 0.001)."""
    q = (0.001 * v) ** 2
    level = 1 + p(510) * q
    balance = min(max(p(345) + p(511) * q, 0.0), 1.0)
    fb = (balance if rear else 1 - balance) * level * 0.5
    lr = 0.5 * level * 0.5
    return ((lr - 0.25) * 4 + 1) * ((fb - 0.25) * 4 + 1) * 0.25


def engine_step(
    w: float, d: float, s: float, te: float, cc: float, g: float, load: float, fd: float, ae: float, ad: float
) -> tuple[float, float]:
    """One implicit step of FUN_008C3CF0's engine (w) and driveline (d):
        w' = w + ae (Te - g C (w' - d'/g)),   d' = d + ad (C (w' - d'/g) - Kt (d' - s) - Fd)
    with C the linearised clutch stiffness (fVar15), Kt = load / (d - s) (fVar12; load is
    p361 x the summed tyre force, param_3), ae = 0.04 / p355, ad = 0.04 / p356. A negative
    Kt flips the sign of both load and Kt, as the code does."""
    kt = load / (d - s) if abs(d - s) > 1e-4 else 0.0
    kt = abs(kt)
    a11, a12 = 1 + ae * g * cc, -ae * cc
    a21, a22 = -ad * cc, 1 + ad * cc / g + ad * kt
    b1 = w + ae * te
    b2 = d + ad * (kt * s - fd)
    det = a11 * a22 - a12 * a21
    return (b1 * a22 - a12 * b2) / det, (a11 * b2 - a21 * b1) / det


@dataclass
class Step:
    t: float
    v: float  # m/s
    a: float  # m/s^2
    gear: int
    rpm: float


def simulate(
    p: Params,
    seconds: float = 30.0,
    v0: float = 0.0,
    throttle: Callable[[float], float] = lambda _t: 1.0,
) -> list[Step]:
    """Straight-line run on a level road at 25 Hz. The engine step (FUN_008C3CF0) solves
        Ie dw/dt = Te - g tc,   Id dd/dt = tc - Kt (d - s) - 0.002 d
    for the engine rpm w and the driveline speed d (rpm x gear), tc the clutch torque
    (p569 x slip, at most p570 x p18 / 4), g the gear, s the ground speed in driveline
    units and Kt = p361 x the previous step's summed tyre force / (d - s): the load is
    linearised on the previous step's slip, which the ground speed has since closed by
    a dt. That lag is what makes the drive force differ from torque / (gear x p361) per
    car (stiffer tyres, smaller slip, larger shortfall)."""
    rels = [p(276 + i) for i in range(int(p(282)))]
    m, p361, radius = p(0), p(361), p(360)
    conv = p361 * radius * RPM  # driveline units -> m/s (FUN_008C2880)
    split = p(371)
    axles = []  # (share of the driveline's speed over the ground, rear?) for each axle's two wheels
    if split > 0:
        axles.append((split, False))
    if split < 1:
        axles.append((1 - split, True))
    v = v0
    gear = next((i + 1 for i, r in enumerate(rels) if v / (r * conv) <= p(183) * p(42)), len(rels))
    d = v / conv
    w = max(p(40), d / rels[gear - 1])
    state = [0.0] * len(axles)  # wheel +0x13C, the filtered slip
    load = 0.0  # +0xD30: the summed tyre force of the previous step
    shift_left, clutch = 0, 1.0
    ie, id_ = p(355), p(356)
    clutch_limit = p(570) * p(18) * TORQUE
    out: list[Step] = []
    t = 0.0
    while t < seconds:
        thr = throttle(t)
        s = v / conv
        k = governor(p, v, thr)
        rel = rels[gear - 1]
        if thr > 0.5 and shift_left <= 0 and gear < len(rels) and s / rel > p(183) * p(42):
            gear += 1
            rel = rels[gear - 1]
            shift_left, clutch = round(p(516) * 25), 0.0
        if shift_left > 0:
            shift_left -= 1
            clutch = 0.0
        elif clutch < 1:
            clutch = min(1.0, clutch + 0.1)  # E[0x1C] = -0.1: re-engages by 0.1 per step
        te = TORQUE * p(18) * torque_curve(p, w, thr) * k - ((p(359) * abs(w) + p(358)) * w + p(357)) * k
        fd = DRIVELINE_FRICTION * d * k
        ae, ad = DT / ie, DT / id_
        slip = w - d / rel
        tc = max(min(p(569) * slip, clutch_limit * clutch), -clutch_limit * clutch)
        cc = tc / slip if abs(slip) > 0.1 else (p(569) if clutch > 0 else 0.0)
        w, d = engine_step(w, d, s, te, cc, rel, p361 * load, fd, ae, ad)
        w = max(w, 0.5 * p(40))
        # FUN_008C0DF0: every wheel at its ground speed, the driven ones plus their share
        # (p371 front, 1 - p371 rear) of the driveline's lead over the ground.
        lead = d * conv - v
        total = 0.0
        for j, (share, rear) in enumerate(axles):
            ws = v + share * lead
            ratio = (ws - v) / max(abs(v), 1.5)  # 1.5: 0x00DD6598
            f = min((abs(ratio) * 50 + 1) * max(abs(v) * DT, 0.1), 1.0)  # 50: 0x00F3F2E4
            state[j] = (1 - f) * state[j] + ratio * f * 0.1
            mu = p(378) * p(376) / (p(376) + abs(ws - v)) + p(377)
            total += 2 * state[j] * 4 * p(369) * 10 * axle_scale(p, v, rear=rear) * mu  # 10: 0x00DD6588
        load = total
        resist = k * (p(509) * v * v + 0.25 * p(13) * v * min(abs(v), 1.0))
        a = IMPULSE * (total - resist) / m
        v += a * DT
        t += DT
        out.append(Step(t, v, a, gear, w))
    return out


# --- predictions -----------------------------------------------------------------------


def metrics(p: Params, only: str | None = None) -> dict[str, float | None]:
    """From a rolling 60 mph start at full throttle: time to 100 and to 150 mph, speed
    after 20 and 60 s (world mph). Launches need the tyre limit, which the model lacks."""
    run = simulate(p, seconds=20.0 if only == "60-100" else 60.0, v0=60 / MPH)

    def time_to(mph: float) -> float | None:
        return next((s.t for s in run if s.v * MPH >= mph), None)

    def speed_at(sec: float) -> float:
        return next((s.v for s in run if s.t >= sec), run[-1].v) * MPH

    return {
        "60-100": time_to(100),
        "60-150": time_to(150),
        "v20": speed_at(20),
        "v60": speed_at(60),
    }


# --- check against dyno logs -----------------------------------------------------------


def _local_slope(t: list[float], v: list[float], i: int, half: float = 0.2) -> float | None:
    js = [j for j in range(max(0, i - 60), min(len(t), i + 60)) if abs(t[j] - t[i]) <= half]
    if len(js) < 10:
        return None
    mt, mv = statistics.mean(t[j] for j in js), statistics.mean(v[j] for j in js)
    den = sum((t[j] - mt) ** 2 for j in js)
    return sum((t[j] - mt) * (v[j] - mv) for j in js) / den if den else None


def check(log: Path, overrides: dict[int, dict[int, float]]) -> list[dict]:
    """Measured / predicted acceleration per car and gear (gears 3 and up, 0.6 s clear of
    a shift, below the governor), full-throttle runs 1 and 3."""
    from dsfforza.harness import dyno

    fleet = D.load_fleet()
    runs, _ = dyno.parse([log])
    rows: dict[tuple[int, int], list[float]] = {}
    sims: dict[int, list[Step]] = {}
    for r in runs:
        if r.run not in (1, 3) or r.car not in fleet:
            continue
        p = accessor(fleet[r.car]["params"], overrides.get(r.car))
        if r.car not in sims:
            sims[r.car] = simulate(p, seconds=40.0)
        period = dyno.analyse(r).get("period")
        if not period:
            continue
        t = [(k - r.n[0]) * period for k in r.n]
        peak = max(range(len(r.v)), key=r.v.__getitem__)
        conv = p(361) * p(360) * RPM
        rels = [p(276 + i) for i in range(int(p(282)))]
        last, since = None, -9.0
        for i in range(0, peak, 5):
            v = r.v[i]
            gear = next((g + 1 for g, rel in enumerate(rels) if v / (rel * conv) <= p(183) * p(42)), len(rels))
            if gear != last:
                last, since = gear, t[i]
            if t[i] - since < 0.6 or gear < 3 or v > 0.88 * p(160) * MPH_TO_MS:
                continue
            a = _local_slope(t, r.v, i)
            pred = [s for s in sims[r.car] if s.gear == gear]
            if a is None or a < 0.3 or not pred:
                continue
            near = min(pred, key=lambda s: abs(s.v - v))
            if near.a > 0.2:
                rows.setdefault((r.car, gear), []).append(a / near.a)
    return [
        {"car": car, "gear": gear, "n": len(x), "ratio": statistics.median(x)}
        for (car, gear), x in sorted(rows.items())
        if len(x) > 5
    ]


# --- the vanilla/Forza pairs -----------------------------------------------------------


def forza_engine_inertia(media: str, dbs: list[Path]) -> float | None:
    """Data_Engine.MomentInertia plus the stock flywheel's (List_UpgradeEngineFlywheel),
    kg m^2."""
    import sqlite3

    for db in dbs:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            car = con.execute("SELECT Id FROM Data_Car WHERE MediaName = ? COLLATE NOCASE", (media,)).fetchone()
            if car is None:
                continue
            eid = con.execute(
                "SELECT EngineID FROM List_UpgradeEngine WHERE Ordinal = ? AND IsStock = 1", (car[0],)
            ).fetchone()
            if eid is None:
                return None
            eng = con.execute("SELECT MomentInertia FROM Data_Engine WHERE EngineID = ?", (eid[0],)).fetchone()
            fly = con.execute(
                "SELECT MomentInertia FROM List_UpgradeEngineFlywheel WHERE EngineID = ? AND IsStock = 1", (eid[0],)
            ).fetchone()
            return (eng[0] if eng else 0.0) + (fly[0] if fly else 0.0)
        finally:
            con.close()
    return None


def derived_torque(car: H.ForzaCar, p: Params) -> dict[int, float]:
    """p18 and the knots p522..p524 from Forza's torque curve, net of DSF's engine
    friction: the wheel force DSF makes in a gear, 25 x (p18 x curve / 4 - friction) /
    (gear x p361) N (steady drive, FUN_008C3CF0 and the impulse unit), equals Forza's
    torque x overall ratio / tyre radius at every rpm when
        p18 x curve(rpm) = 4 x (T(rpm) / (25 r) + friction(rpm)).
    Knots: idle p40, the maximum (p524 its position), the redline p42."""
    r = p(360)

    def target(rpm: float) -> float:
        return 4 * (car.torque_at(rpm) / (IMPULSE * r) + (p(359) * rpm + p(358)) * rpm + p(357))

    lo, hi = p(40), p(42)
    grid = [lo + (hi - lo) * i / 200 for i in range(201)]
    vals = [target(x) for x in grid]
    top = max(range(len(vals)), key=vals.__getitem__)
    peak = vals[top]
    pos = min(max((grid[top] - lo) / (hi - lo), 0.01), 0.99)
    return {18: peak, 522: max(vals[0] / peak, 0.0), 524: pos, 523: max(vals[-1] / peak, 0.0)}


@dataclass
class Pair:
    sid: int
    media: str
    host: int
    truth: dict[int, float]
    base: dict[int, float]  # (a): today's rules on the best host other than the car's own model
    car: H.ForzaCar
    inertia: float | None
    forza: dict[str, float]


CANDIDATES = {
    "torque": "p18, p522..p524 from Forza's torque curve net of friction",
    "inertia": "p355 = (MomentInertia + flywheel) x pi / (750 r)",
    "drag": "p509 = Forza drag x GameDragScale x 0.002226 / 25 (unit a hypothesis)",
    "split": "p371 = 1 - RearToqueSplit (AWD)",
    "cog": "p30 without the 0.052 m offset",
    "box": "p15..p17 = box inertia of BlockDim, no 2.30/2.36/2.70",
}
COMPARED = {
    "torque": (18, 522, 523, 524),
    "inertia": (355,),
    "drag": (509,),
    "split": (371,),
    "cog": (30,),
    "box": (15, 16, 17),
}
LOG_PIDS = {18, 355, 509, 15, 16, 17}


def candidate(name: str, pair: Pair, params: dict[int, float]) -> dict[int, float]:
    p = accessor(params)
    car = pair.car
    if name == "torque":
        return derived_torque(car, p)
    if name == "inertia":
        return {355: pair.inertia * RPM / (IMPULSE * p(360))} if pair.inertia else {}
    if name == "drag":
        return {509: car.drag * LBF_MPH2 / IMPULSE}
    if name == "split":
        return {371: 1 - car.rear_split} if car.drive == "AWD" else {}
    if name == "cog":
        return {30: p(30) - D.COG_HEIGHT_OFFSET, 599: p(30) - D.COG_HEIGHT_OFFSET} if 30 in params else {}
    if name == "box":
        w, h, length = car.block
        m = car.mass
        return {
            15: m * (h * h + length * length) / 12,
            16: m * (w * w + length * length) / 12,
            17: m * (w * w + h * h) / 12,
        }
    raise KeyError(name)


def load_pairs() -> list[Pair]:
    fleet = D.load_fleet()
    dbs = D.default_dbs()
    out = []
    for sid, media in D.PAIRS.items():
        try:
            car = H.load_forza(media, dbs)
        except (SystemExit, ValueError, LookupError) as error:
            print(f"skip {media}: {error}", file=sys.stderr)
            continue
        order, same = D.ranked_hosts(media, fleet, sid)
        order = [i for i in order if i not in same]
        ctx = D.context(order[0], media, fleet, order, exclude=same, car=car)
        built, _ = D.build(ctx, feel="medoid")
        base = dict(ctx.base)
        base.update(built)
        # The CoG follows the hubs; compare it on the vanilla car's hubs (as the LOO does).
        for pid in (30, 599):
            if pid in base:
                base[pid] += fleet[sid]["hub_y"] - ctx.host["hub_y"]
        if 32 in base:
            base[32] += fleet[sid]["axle_mid"] - ctx.host["axle_mid"]
        truth = dict(fleet[sid]["params"])
        out.append(
            Pair(sid, media, order[0], truth, base, car, forza_engine_inertia(media, dbs), forza_times(media, dbs))
        )
    return out


def forza_times(media: str, dbs: list[Path]) -> dict[str, float]:
    """Forza's own simulated figures (Data_Car Sim*): s, m/s."""
    import sqlite3

    for db in dbs:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT SimTimeTo60MPH, SimTimeTo100MPH, SimTopSpeed FROM Data_Car WHERE MediaName = ? COLLATE NOCASE",
                (media,),
            ).fetchone()
            if row:
                return {"0-60": row[0], "0-100": row[1], "top": row[2]}
        finally:
            con.close()
    return {}


def pace_torque(pair: Pair, params: dict[int, float]) -> dict[int, float]:
    """p18 such that DSF's model runs 60 to 100 mph in Forza's own time
    (SimTimeTo100MPH - SimTimeTo60MPH), the curve and everything else kept: Forza's
    figure through DSF's maths, as p160 takes Forza's top speed."""
    target = pair.forza.get("0-100", 0) - pair.forza.get("0-60", 0)
    if target <= 0:
        return {}
    p18 = accessor(params)(18)
    lo, hi = 0.2, 5.0
    for _ in range(30):
        mid = math.sqrt(lo * hi)
        t = metrics(accessor(params, {18: p18 * mid}), only="60-100")["60-100"]
        if t is None or t > target:
            lo = mid
        else:
            hi = mid
    return {18: p18 * math.sqrt(lo * hi)}


def variants(pair: Pair) -> dict[str, dict[int, float]]:
    """(a) today's rules, each candidate on its own, (c) all of them, and "pace"."""
    out = {"a": dict(pair.base)}
    for name in CANDIDATES:
        one = dict(pair.base)
        one.update(candidate(name, pair, pair.base))
        out[name] = one
    merged = dict(pair.base)
    for name in CANDIDATES:  # torque first: net of the friction the port has
        merged.update(candidate(name, pair, merged))
    out["c"] = merged
    pace = dict(pair.base)
    pace.update(pace_torque(pair, pair.base))
    out["pace"] = pace
    return out


def param_error(pid: int, val: float, truth: float) -> float | None:
    """|log ratio| for the scale parameters, |difference| for shares, positions and heights."""
    if pid in LOG_PIDS:
        return abs(math.log(val / truth)) if val > 0 and truth > 0 else None
    return abs(val - truth)


def behaviour_error(m: dict, ref: dict) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key in ("60-100", "60-150"):
        out[key] = abs(math.log(m[key] / ref[key])) if m[key] and ref[key] else None
    for key in ("v20", "v60"):
        out[key] = abs(m[key] - ref[key])
    return out


def pairs(csv_path: Path | None = None) -> None:
    rows = []
    for pair in load_pairs():
        truth = accessor(pair.truth)
        ref = metrics(truth)
        for name, params in variants(pair).items():
            p = accessor(params)
            m = metrics(p)
            row = {"sid": pair.sid, "media": pair.media, "host": pair.host, "variant": name, **m}
            for key, err in behaviour_error(m, ref).items():
                row[f"err {key}"] = err
            for pids in COMPARED.values():
                for pid in pids:
                    row[f"p{pid}"] = p(pid)
                    row[f"err p{pid}"] = param_error(pid, p(pid), truth(pid))
            rows.append(row)
        print(f"{pair.sid} {pair.media} on {pair.host}: done", file=sys.stderr)
    if csv_path:
        keys = list(dict.fromkeys(k for r in rows for k in r))
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, keys)
            w.writeheader()
            w.writerows(rows)
    summarise(rows)


def _num(x) -> float | None:
    if x is None or x == "":
        return None
    return float(x)


def summarise(rows: list[dict]) -> None:
    by = {}
    for r in rows:
        by.setdefault(r["sid"], {})[r["variant"]] = r
    names = ["a", *CANDIDATES, "c", "pace"]
    print(
        "# error against DSF's own car, median over the pairs (closer = smaller); "
        "'better' = pairs where the variant beats (a) by more than 1%"
    )
    keys = [f"err p{pid}" for pids in COMPARED.values() for pid in pids] + [
        "err 60-100",
        "err 60-150",
        "err v20",
        "err v60",
    ]
    print("variant\t" + "\t".join(keys))
    for name in names:
        cells = []
        for key in keys:
            both = [
                (by[s][name][key], by[s]["a"][key])
                for s in by
                if name in by[s] and _num(by[s][name].get(key)) is not None and _num(by[s]["a"].get(key)) is not None
            ]
            vals = [_num(x) for x, _ in both]
            base = [_num(y) for _, y in both]
            if not vals:
                cells.append("-")
                continue
            better = sum(1 for x, y in zip(vals, base) if x < y * 0.99 - 1e-12)
            worse = sum(1 for x, y in zip(vals, base) if x > y * 1.01 + 1e-12)
            cells.append(f"{statistics.median(vals):.3g} ({better}+/{worse}-)")
        print(name + "\t" + "\t".join(cells))


def write_archive(sids: list[int], variant: str, out: Path) -> None:
    """A copy of the vanilla dngvehicles.sp where each pair car's VO3D takes the variant's
    handling values (cameras and the other non-handling IDs stay the car's own), so a dyno
    run drives the vanilla body on the port's handling."""
    from dsfforza import REPO

    archive = REPO / "out" / "vanilla" / "Install" / "dngvehicles.sp"
    vo3ds = {}
    for pair in load_pairs():
        if pair.sid not in sids:
            continue
        new = dict(pair.truth)
        new.update({pid: val for pid, val in variants(pair)[variant].items() if pid not in D.NOT_HANDLING})
        vo3ds[pair.sid] = H.VO3D(pair.sid, new)
    H.replace_vo3ds(archive, out, vo3ds)


def predict(sids: list[int], names: list[str]) -> None:
    """The model's figures for the vanilla car and each variant (written before a dyno run)."""
    for pair in load_pairs():
        if pair.sid not in sids:
            continue
        rows = {"vanilla": pair.truth, **{n: v for n, v in variants(pair).items() if n in names}}
        for name, params in rows.items():
            p = accessor(params)
            m = metrics(p)
            print(
                f"{pair.sid}\t{pair.media}\t{name}\tp18 {p(18):.0f}\tp355 {p(355):.4f}\tp509 {p(509):.4f}"
                f"\t60-100 {_fmt(m['60-100'])} s\t60-150 {_fmt(m['60-150'])} s"
                f"\tv20 {m['v20']:.1f}\tv60 {m['v60']:.1f} mph"
            )


def _fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def parse_sets(items: list[str]) -> dict[int, dict[int, float]]:
    out: dict[int, dict[int, float]] = {}
    for item in items:
        car, _, rest = item.partition(":")
        out[int(car)] = {int(k): float(v) for k, v in (kv.split("=") for kv in rest.split(",") if kv)}
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="measured / predicted acceleration of a dyno session")
    c.add_argument("log", type=Path)
    c.add_argument("--set", nargs="*", default=[], help="CAR:pid=value,... overrides the archive had")
    s = sub.add_parser("pairs", help="Forza-derived parameters against DSF's own cars")
    s.add_argument("--csv", type=Path)
    s.add_argument("--summary", type=Path, help="summarise an earlier --csv instead of converting")
    a = sub.add_parser("archive", help="vanilla archive with pair cars on a variant's handling")
    a.add_argument("out", type=Path)
    a.add_argument("--ids", required=True, help="pair spool IDs, comma-separated")
    a.add_argument("--variant", required=True)
    pr = sub.add_parser("predict", help="the model's figures for pair cars, vanilla and variants")
    pr.add_argument("--ids", required=True)
    pr.add_argument("--variants", default="a,c,pace")
    args = ap.parse_args(argv)
    if args.cmd == "archive":
        write_archive([int(x) for x in args.ids.split(",")], args.variant, args.out)
        return
    if args.cmd == "predict":
        predict([int(x) for x in args.ids.split(",")], args.variants.split(","))
        return
    if args.cmd == "check":
        for row in check(args.log, parse_sets(args.set)):
            print(f"{row['car']}\tgear {row['gear']}\tn {row['n']}\tmeasured / model {row['ratio']:.2f}")
    elif args.summary:
        with args.summary.open(newline="") as f:
            summarise(list(csv.DictReader(f)))
    else:
        pairs(args.csv)


if __name__ == "__main__":
    main()
