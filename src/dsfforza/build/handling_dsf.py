"""DSF-first handling: a ported car's VO3D from its host and Reflections' own fleet rules.

Driver: San Francisco's driving model is its own (arcade grip and slides, assists,
boost-friendly gearing). A ported car should drive like a car Reflections would have
made, so this starts from the host's complete VO3D (a stock car already tuned for
DSF, picked by src/dsfforza/build/host_select.py) and changes only what the car's real specs
say must change, by the rules the stock fleet itself follows:

    H  keep the host's value
    M  host value x (new mass / host mass)
    F  host value moved by the fleet model: a least-squares fit over the stock fleet
       of the parameter (log, when always positive) against the vehicleStats specs
       (log weight, log power, log torque, model year, FWD, AWD); the new value is
       the host's times exp(f(new) - f(host)), or plus f(new) - f(host)
    A  the fleet model's absolute prediction for the new car's specs
    N  the median of the three best-ranked hosts
    Z  Forza-derived (src/dsfforza/build/handling.py, the old mapping)

and a few joint rules: gearing (G*: the host's gear set and its top-gear-to-limit
relation, re-solved for the new tyre, redline and top speed), the drive split (D)
and the CoG (C*). Which rule each parameter uses was chosen by the leave-one-out
test on the 48 stock cars that also exist in Forza (`loo`), see RULES and
research/handling.md.

    uv run dsff handling_dsf loo [--csv out.csv]          # the decisive offline check
    uv run dsff handling_dsf build --car NIS_370Z_10 [--host 227] [--exclude-self]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from dsfforza import REPO
from dsfforza.build import handling as H
from dsfforza.build import host_select as HS
from dsfforza.formats.chnk import read_chunk
from dsfforza.formats.vehicle_mesh import payload

FLEET_CACHE = REPO / "out" / "cache" / "dsf-fleet.json"
FLEET_VERSION = 1
PAIRS = {
    227: "NIS_370Z_10",
    125: "ALF_8C_08",
    128: "ALF_MITO_09",
    134: "AST_RAPIDE_10",
    135: "AST_V12VANTAGE_10",
    139: "AUD_R8V10_10",
    255: "AUD_RS6_09",
    119: "AUD_S5_DLC",
    143: "AUD_TTRS_10",
    138: "AUD_Q7_09",
    279: "BEN_CONTINENTALSS_10",
    147: "CAD_CTSV_09",
    146: "CHE_CAMAROSS_69",
    161: "CHE_CORVETTE_60",
    160: "CHE_CHEVELLE_70",
    250: "CHE_VOLT_11",
    172: "DOD_CHALLENGERSRT8_09",
    175: "DOD_CHARGERRT_69",
    181: "DOD_RAMSRT10_06",
    182: "DOD_VIPERSRT10ACR_08",
    252: "FOR_F350SUPERDUTY_08",
    189: "FOR_GT_05",
    196: "FOR_TAURUSSHO_10",
    205: "JAG_ETYPE_61",
    206: "JAG_XFR_10",
    207: "JAG_33_XKR_10",
    210: "LAM_COUNTACH_88",
    211: "LAM_DIABLOSV_97",
    212: "LAM_GALLARDOLP570_11",
    214: "LAM_LP6704SV_10",
    215: "LAM_MIURAP400_67",
    216: "LAM_MURCIELAGOLP640_07",
    217: "LAN_STRATOS_74",
    221: "MAS_GRANTURISMOS_10",
    224: "MCL_F1_93",
    225: "MCL_MP412C_11",
    226: "MER_SLR_05",
    228: "NIS_GTR_12",
    232: "PAG_ZONDACINQUE_09",
    233: "PON_GTOJUDGE_69",
    278: "PON_SOLSTICEGXP_09",
    241: "RUF_YELLOWBIRD_87",
    243: "RUF_RT12S_11",
    248: "SHE_COBRA427_65",
    257: "VW_BEETLE_68",
    275: "VW_SCIROCCOR_11",
    195: "FOR_RS200EVOLUTION_87",
    142: "AUD_SPORTQUATTRO_83",
}
DBS = [REPO / "out" / "db" / "fh1_gamedb.slt", REPO / "out" / "db" / "fm4_gamedb.slt"]

# Parameters set by other steps or tied to the body geometry and the converter.
NOT_HANDLING = (
    set(range(300, 306))
    | set(range(327, 343))
    | {394, 395, 396, 430, 431, 432, 535, 536, 537, 549, 550, 551, 552, 553, 554, 605, 606}
)
GEAR_PIDS = (275, 276, 277, 278, 279, 280, 281, 282, 361)
# Feel weights for the LOO score (grip, slides, steering, assists, power, gearing, mass).
GROUPS = {
    "tyres": ((367, 368, 369, 376, 377, 378, 497, 517, 519, 520, 521, 525, 526, 529, 530, 531), 1.0),
    "steering": ((298, 379, 380, 381, 382, 383, 384, 385, 386, 387, 388, 389, 390, 391, 470), 1.0),
    "assists": ((24, 166, 239, 344, 345, 351, 352, 353, 354, 365, 366, 373, 374, 375, 393, 411, 496, 510, 511), 1.0),
    "suspension": ((1, 2, 3, 4, 25, 188, 292, 505, 506, 508), 1.0),
    "engine": ((18, 40, 42, 160, 183, 184, 186, 355, 356, 357, 358, 362, 516, 522, 523, 524, 569, 371), 1.0),
    "gearing": ((275, 276, 277, 278, 279, 280, 281, 282, 360, 361), 1.0),
    "body": ((0, 13, 15, 16, 17, 30, 32, 509), 1.0),
    "drivetrain set": ((371, 497, 516, 517, 519, 520, 521, 525, 526, 529, 530, 531), 1.0),
}
WEIGHT = {pid: w for pids, w in GROUPS.values() for pid in pids}
MISC_WEIGHT = 0.5


# --- specs ----------------------------------------------------------------------------


@dataclass
class Specs:
    weight: float  # kg
    power: float  # kW
    torque: float  # Nm
    drive: str  # FWD / RWD / AWD
    year: float

    def x(self) -> list[float]:
        return [
            1.0,
            math.log(self.weight),
            math.log(self.power),
            math.log(self.torque),
            (self.year - 2000) / 10,
            1.0 if self.drive == "FWD" else 0.0,
            1.0 if self.drive == "AWD" else 0.0,
        ]


def specs_from_stats(stats: dict) -> Specs | None:
    try:
        w, p, t = (float(stats[k]) for k in ("WeightMetric", "PowerMetric", "TorqueMetric"))
        year = float(stats.get("Year") or 2000)
    except (KeyError, ValueError, TypeError):
        return None
    if min(w, p, t) <= 0:
        return None
    drive = {"4WD": "AWD"}.get(stats.get("Drivetrain"), stats.get("Drivetrain") or "RWD")
    return Specs(w, p, t, drive, year)


def specs_from_forza(media: str, car: H.ForzaCar, dbs: list[Path] = DBS) -> Specs:
    """Real-world specs as vehicleStats states them: Data_Car SimPeakPower (100 W),
    SimPeakTorque (100 Nm), the stock body mass and year."""
    for db in dbs:
        con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
        row = con.execute(
            "SELECT SimPeakPower, SimPeakTorque, Year FROM Data_Car WHERE MediaName = ? COLLATE NOCASE", (media,)
        ).fetchone()
        con.close()
        if row:
            peak_nm, _ = car.peak_torque
            kw, _ = car.peak_power
            return Specs(
                car.mass, row[0] / 10 if row[0] else kw, row[1] * 100 if row[1] else peak_nm, car.drive, row[2] or 2000
            )
    raise SystemExit(f"{media}: not in {dbs}")


# --- the stock fleet --------------------------------------------------------------------


def load_fleet(dsf: Path | None = None, lua: Path = HS.DEFAULT_LUA, refresh: bool = False) -> dict[int, dict]:
    """Every stock base vehicle: VO3D, vehicleStats, hub geometry and host facts."""
    # The original archive when a dsfforza install replaced it (the installed one carries the
    # ported cars at IDs 1..40, which leaked into the fleet on 2026-10-22).
    dsf = HS.game_paths.stock(dsf, quiet=True)
    hosts = HS.survey(dsf, lua)
    stat = dsf.stat()
    key = f"{FLEET_VERSION}:{dsf.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"
    if FLEET_CACHE.exists() and not refresh:
        try:
            data = json.loads(FLEET_CACHE.read_text())
        except (OSError, ValueError):  # cut short (written in place before): build it again
            data = {}
        if data.get("key") == key:
            return {int(k): _thaw(v) for k, v in data["fleet"].items()}
    stats = H.lua_stats(lua) if lua and Path(lua).exists() else {}
    fleet = {}
    with dsf.open("rb") as stream:
        root = read_chunk(stream, 0).entries
        for h in hosts:
            vo3d_entry, upvh_entry = H.host_entries(stream, root, h["id"])
            params = H.VO3D.parse(payload(stream, vo3d_entry)).params
            try:
                wheelbase, track, hub_y, mid = H.hub_geometry(payload(stream, upvh_entry))
            except (ValueError, statistics.StatisticsError):
                wheelbase, track, hub_y, mid = 0.0, 0.0, 0.0, 0.0
            fleet[h["id"]] = {
                "id": h["id"],
                "params": params,
                "stats": stats.get(h["id"], h["stats"]),
                "wheelbase": wheelbase,
                "track": track,
                "hub_y": hub_y,
                "axle_mid": mid,
                "wheels": h["wheels"],
                "category": h["category"],
                "model": h["model"],
                "problems": h["problems"],
            }
    # Atomic: out/cache is shared by every worktree's builds and port_batch workers read it.
    from dsfforza.build.port_car import write_atomic

    FLEET_CACHE.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(FLEET_CACHE, json.dumps({"key": key, "fleet": fleet}).encode())
    return fleet


def _thaw(v: dict) -> dict:
    v["params"] = {int(k): x for k, x in v["params"].items()}
    return v


def value(params: dict[int, float], pid: int) -> float:
    """The parameter as the engine sees it: the VO3D's value, else the exe default (not 0:
    research/handling.md, "Off road")."""
    if pid in params:
        return params[pid]
    return H.exe_default(pid)


def leave_out(base: dict[int, float], new: dict[int, float], pid: int, val: float) -> bool:
    """True when `val` is the engine's default for a parameter that neither the base VO3D
    nor an earlier rule sets (a set donor that leaves it out too): the VO3D then leaves it
    out as well and the engine uses its default, as on the donor. Writing the donor's
    absence as 0 cut the soft-ground traction (p526) of five ports to nothing."""
    return pid not in base and pid not in new and H.same_f32(val, H.exe_default(pid))


def fit_set(fleet: dict[int, dict]) -> list[dict]:
    """Cars the fleet models learn from: four wheels, specs known."""
    out = []
    for car in fleet.values():
        specs = specs_from_stats(car["stats"])
        if specs and car["wheels"] == 4:
            out.append({"id": car["id"], "x": specs.x(), "params": car["params"]})
    return out


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def ridge_fit(xs: list[list[float]], ys: list[float], ridge: float) -> list[float]:
    """Least squares of ys on xs (x[0] = 1, the intercept), `ridge` x n added to the
    diagonal of every slope."""
    k = len(xs[0])
    a = [
        [sum(x[i] * x[j] for x in xs) + (ridge * len(xs) if i == j and i > 0 else 0) for j in range(k)]
        for i in range(k)
    ]
    b = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(k)]
    return _solve(a, b)


# Largest log change rule F may make (the fleet fit's year term moved a 1969 Charger's
# tyre stiffness 30% below its host in run 20261022-071526).
F_LIMIT = 0.22


class FleetModel:
    """Per-parameter least-squares fit over the fleet (ridge 1e-2 on the slopes)."""

    def __init__(self, rows: list[dict], exclude: set[int] = frozenset()):
        self.rows = [r for r in rows if r["id"] not in exclude]
        self.cache: dict[int, tuple[bool, list[float]]] = {}

    def coefficients(self, pid: int) -> tuple[bool, list[float]]:
        if pid not in self.cache:
            ys = [value(r["params"], pid) for r in self.rows]
            log = all(y > 0 for y in ys)
            if log:
                ys = [math.log(y) for y in ys]
            self.cache[pid] = (log, ridge_fit([r["x"] for r in self.rows], ys, 1e-2))
        return self.cache[pid]

    def predict_target(self, key: str, fn, specs: Specs) -> float:
        """Least-squares prediction of fn(params) (any per-car quantity) from specs."""
        if key not in self.cache:
            ys = [fn(r["params"]) for r in self.rows]
            self.cache[key] = (False, ridge_fit([r["x"] for r in self.rows], ys, 1e-2))
        return sum(b * x for b, x in zip(self.cache[key][1], specs.x()))

    def predict(self, pid: int, specs: Specs) -> float:
        log, beta = self.coefficients(pid)
        f = sum(b * x for b, x in zip(beta, specs.x()))
        return math.exp(f) if log else f

    def shift(self, pid: int, host_value: float, host: Specs, new: Specs) -> float:
        log, beta = self.coefficients(pid)
        d = sum(b * (xn - xh) for b, xn, xh in zip(beta, new.x(), host.x()))
        if log and host_value > 0:
            return host_value * math.exp(max(-F_LIMIT, min(F_LIMIT, d)))
        return host_value + d if not log else host_value


# --- rules ------------------------------------------------------------------------------

# Chosen by `loo` (research/handling.md, "DSF-first mapping"): a rule other than H only
# where it beats the host's own value by at least 0.05 and 10% of the error, among the
# host-relative rules (M, F) and the Forza-derived ones that are themselves fits to the
# DSF fleet or real specs (Z: mass, box inertia x 2.3/2.36/2.7, 14.7 Nm^0.621 torque,
# redline, top speed fit, tyre radius). Springs and dampers follow the mass (M) so the
# host's static sag and ride frequency carry over (LOO neutral). Parameters not listed,
# among them every steering, assist, tyre-falloff and suspension-geometry value, keep
# the host's (H).
RULES: dict[int, str] = {
    0: "Z",
    1: "M",
    2: "M",
    13: "F",
    15: "Z",
    16: "Z",
    17: "Z",
    18: "TP",
    42: "Z",
    160: "Z",
    360: "Z",
    366: "A",
    367: "A",
    368: "M",
    369: "F",
    516: "A",
    509: "M",
    361: "GX",
    32: "CZ",
    30: "CY",
    345: "W",
    505: "W",
}
# CoG height (rule CY): p30 = hub height + Forza CMHeight - drive tyre radius + 0.052 m;
# on the 48 pairs |error| 0.030 m against 0.054 for the fleet median offset. The CoG is
# the strongest predictor of handbrake rotation in game (with p32 and the yaw inertia).
COG_HEIGHT_OFFSET = 0.052
# On a host of the car's own model the stock VO3D is Reflections' version of the car:
# keep it, only follow the hubs (wheelbase, CoG offset scaled with it).
SAME_MODEL_RULES: dict[int, str] = {361: "GH", 32: "CH"}


# p18 from peak torque AND power (Forza curve, Nm and kW): fitted on the 45 pairs (the
# Volt, F-350 and Q7 left out). Leave-one-out |log error|: median 0.060, 75th
# percentile 0.102, against 0.075 / 0.175 for the torque-only rule 14.7 x Nm^0.621.
TORQUE_POWER_FIT = (3.2354, 0.1211, 0.4497)


def torque_power_rule(car: H.ForzaCar) -> float:
    a, b, c = TORQUE_POWER_FIT
    return math.exp(a + b * math.log(car.peak_torque[0]) + c * math.log(car.peak_power[0]))


# vehicleStats stat bars (the HUD's Speed / Strength / Drift, Reflections' summary of a
# car), fitted over the 137 stock cars that have them. Leave-one-out mean absolute
# error 0.042, 0.041 and 0.127 (bar spread 0.24, 0.18, 0.28). P/W in kW per kg.
BAR_FITS = {
    "Speed": (-0.6525, 0.0101, 0.0587),  # + p160 (mph), + ln(P/W)
    "Strength": (-2.1427, 0.3429),  # + ln(p0)
    "Drift": (1.2138, 0.2739, -0.3692, -0.1497),  # + ln(P/W), FWD, AWD
}


def stat_bars(params: dict[int, float], specs: Specs) -> dict[str, float]:
    pw = math.log(specs.power / specs.weight)
    s = BAR_FITS["Speed"]
    t = BAR_FITS["Strength"]
    d = BAR_FITS["Drift"]
    raw = {
        "Speed": s[0] + s[1] * value(params, 160) + s[2] * pw,
        "Strength": t[0] + t[1] * math.log(value(params, 0)),
        "Drift": d[0] + d[1] * pw + d[2] * (specs.drive == "FWD") + d[3] * (specs.drive == "AWD"),
    }
    return {k: round(min(1.0, max(0.05, v)), 2) for k, v in raw.items()}


# Handbrake rotation (yaw change in 3 s after a 0.4 s Space+Right flick, degrees) from
# 46 in-game runs on 9 stock cars and 7 ports (runs 20261022-050855, -055824, -063701):
# 25.3 - 11.56 x p16/p0 + 5.64 x p369/p0 - 41.4 x p368/p0 + 1.68 x mph, R^2 0.63,
# rms 11 deg. Yaw inertia per kg, lateral stiffness per kg and grip per kg are the dials
# Reflections' cars differ by. drift_index() is the speed-free part.
DRIFT_FIT = (-11.564, 5.639, -41.438)


def drift_index(params: dict[int, float]) -> float:
    m = value(params, 0)
    a, b, c = DRIFT_FIT
    return a * value(params, 16) / m + b * value(params, 369) / m + c * value(params, 368) / m


TOP_RATIO = (1.0, 1.07)


def _mph_at_redline(rel: float, p361: float, p360: float, p42: float) -> float:
    return rel * p361 * p360 * p42 * math.pi / 30 * 2.23694


@dataclass
class Context:
    host: dict  # fleet record of the host (its stock VO3D and geometry)
    host_specs: Specs
    car: H.ForzaCar
    specs: Specs
    model: FleetModel
    neighbors: list[dict]  # fleet records of the next-best hosts (incl. the host)
    base: dict[int, float]  # the clone's current VO3D (host values plus earlier edits)
    scales: dict[int, float] | None = None  # fleet spread per parameter (for the feel medoid)
    same_model: bool = False  # the host is a stock version of this car
    family: bool = False  # the host is of the car's model family (host_select identity >= 30)
    drive_donor: dict | None = None  # best-ranked host with the car's drivetrain


TORQUE_SCALED = (355, 358)
# Rule W: parameters Reflections set from the weight distribution (front share fw of the
# axle load, from p32: 0.5 + (p32 - axle midpoint) / wheelbase), fitted over the fleet.
# p345 is the base of the axle balance in FUN_008BD9B0 (r = -0.93 with fw, residual sd
# 0.015 over 136 cars): swapping a GT-R port's 0.58 for the stock 0.51 brought its
# handbrake rotation from 41 to 90 deg (stock 72 to 82; run 20261022-123844).
WEIGHT_BALANCE = {345: (0.9448, -0.8298), 505: (3.035, -3.812)}


def candidates(ctx: Context, pid: int, new: dict[int, float]) -> dict[str, float]:
    """Every applicable rule's value for one parameter. `new` holds values decided
    so far (mass first)."""
    hv = value(ctx.base, pid)
    out = {"H": hv}
    host_mass = value(ctx.base, 0)
    mass = new.get(0, host_mass)
    if hv > 0:
        out["M"] = hv * mass / host_mass
    out["F"] = ctx.model.shift(pid, hv, ctx.host_specs, ctx.specs)
    if pid == 18:
        out["TP"] = torque_power_rule(ctx.car)
    if pid in WEIGHT_BALANCE:
        a, b_ = WEIGHT_BALANCE[pid]
        fw = min(max(ctx.car.front_weight, H.FRONT_WEIGHT_CLAMP[0]), H.FRONT_WEIGHT_CLAMP[1])
        out["W"] = a + b_ * fw
    if pid in TORQUE_SCALED and value(ctx.base, 18) > 0:
        # R: the fleet's median ratio to p18 (log p355 vs log p18: r = 0.78), times the new p18.
        ratios = sorted(
            value(r["params"], pid) / value(r["params"], 18) for r in ctx.model.rows if value(r["params"], 18) > 0
        )
        out["R"] = statistics.median(ratios) * new.get(18, value(ctx.base, 18))
    out["A"] = ctx.model.predict(pid, ctx.specs)
    if ctx.neighbors:
        out["N"] = statistics.median(value(n["params"], pid) for n in ctx.neighbors)
    return out


def forza_values(ctx: Context) -> dict[int, float]:
    """The old mapping (src/dsfforza/build/handling.py) on this host, as rule Z: only the parameters
    it derives."""
    host = H.Host(
        ctx.host["id"],
        H.VO3D(ctx.host["id"], dict(ctx.base)),
        ctx.car.wheelbase,
        ctx.host["track"],
        ctx.host["hub_y"],
        ctx.host["axle_mid"],
    )
    vo3d, changes = H.build_vo3d(host, ctx.car)
    return {c.pid: vo3d.params[c.pid] for c in changes}


def gear_rule(ctx: Context, rule: str, p160: float, p42: float, p360: float, z: dict[int, float]) -> dict[int, float]:
    """GH: the host's gear set (count, ratios, reverse) with p361 re-solved so that top
    gear at redline sits at the same multiple of p160 as on the host. GZ: Forza's."""
    if rule == "GZ":
        return {pid: z[pid] for pid in GEAR_PIDS if pid in z}
    b = ctx.base
    if rule == "GX":
        # The host's gear count, spacing and top gear (at the host's multiple of p160);
        # first gear where Forza's first gear puts it.
        n = int(value(b, 282))
        host_top = _mph_at_redline(value(b, 276 + n - 1), value(b, 361), value(b, 360), value(b, 42))
        # Top gear at redline relative to p160: the host's, kept inside the fleet's
        # interquartile range 1.00..1.07 (a tall overdrive host made the Mito slow).
        top_mph = min(max(host_top / value(b, 160), TOP_RATIO[0]), TOP_RATIO[1]) * p160
        car = ctx.car
        first_mph = (
            car.redline
            * math.pi
            / 30
            * 2.23694
            * car.drive_radius
            / (car.gears[0] * car.final_drive)
            * p42
            / car.redline
        )
        first_mph = min(first_mph, top_mph)
        unit = _mph_at_redline(1.0, 1.0, p360, p42)
        host_rels = [value(b, 276 + i) / value(b, 276 + n - 1) for i in range(n)]
        if n == 1 or not 0 < host_rels[0] < 1:
            rels = [1.0] * n if n == 1 else [(first_mph / top_mph) ** (1 - i / (n - 1)) for i in range(n)]
        else:
            # The host's spacing (log ratios relative to top gear), stretched to the new first gear.
            k = math.log(first_mph / top_mph) / math.log(host_rels[0])
            rels = [r**k for r in host_rels]
        out = {361: top_mph / unit}
        out.update({276 + i: r for i, r in enumerate(rels)})
        return out
    n = int(value(b, 282))
    top = value(b, 276 + n - 1)
    host_top = _mph_at_redline(top, value(b, 361), value(b, 360), value(b, 42))
    ratio = host_top / value(b, 160)
    p361 = ratio * p160 / _mph_at_redline(top, 1.0, p360, p42)
    return {361: p361}


# Feel groups: coherent sets a designer tunes together. With feel="medoid" each set is
# taken whole from one car among the three best hosts, the one closest to the other two
# (so a single odd host doesn't decide the feel, and no set is mixed from several cars).
FEEL_GROUPS = {
    "steering": (298, 379, 380, 381, 382, 383, 384, 385, 386, 387, 388, 389, 390, 391, 470),
    "assists": (344, 345, 351, 352, 353, 354, 373, 374, 375, 411, 496, 510, 511),
    "tyre_falloff": (376, 377, 378),
    "suspension": (3, 4, 25, 188, 292, 505, 506, 507, 508),
    # Engine dynamics: rev-up inertia and friction, clutch, rev limiter. With the shift
    # time (drivetrain set) these decide the launch: the GT-R on the RUF CTR3 host got
    # its 0-30 mph back from 1.38 to 0.73 s with 228's 355, 356, 358 and 516 (run
    # 20261022-073627), while its drivetrain set, CoG and tyre stiffness did nothing.
    "engine": (355, 356, 357, 358, 359, 362, 364, 569),
}


# Drivetrain set: what DSF sets differently for AWD, RWD and FWD cars (fleet survey,
# research/handling.md): the drive split, the handbrake grip (p530 is 0 on every AWD
# and FWD car, 0.15..0.5 on RWD), the per-axle longitudinal scale and combined-slip
# shaping (517, 519..521, 525, 526, used together on AWD cars), 497 (read with the
# drive split in FUN_008C0DF0). Taken whole from the best-ranked host with the car's
# own drivetrain, with the launch/burnout block (24, 166, 239, 365, 393, FUN_008BCBB0):
# taken with the other assists from a RWD donor it cost the FWD Mito 1.5 s to 60 mph
# (run 20261022-083248). (The shift time went with it at first; the GT-R then took the R33
# drift car's 0.25 s and launched slowly, so p516 now follows the fleet model, rule A.)
DRIVE_GROUP = (371, 497, 517, 519, 520, 521, 525, 526, 529, 530, 531, 24, 166, 239, 365, 393)


def feel_donor(ctx: Context, pids: tuple[int, ...]) -> dict:
    cands = ctx.neighbors
    if len(cands) < 3 or not ctx.scales:
        return ctx.host

    def dist(a: dict, b: dict) -> float:
        return sum(abs(value(a["params"], p) - value(b["params"], p)) / ctx.scales.get(p, 1.0) for p in pids)

    return min(cands, key=lambda c: (sum(dist(c, o) for o in cands), c["id"] != ctx.host["id"]))


def build(
    ctx: Context,
    rules: dict[int, str] | None = None,
    feel: str = "host",
    drive_set: bool = True,
    drift: str = "none",
    engine_scale: bool = True,
) -> tuple[dict[int, float], list[tuple[int, float, float, str]]]:
    """The new VO3D parameters and the changes (pid, old, new, rule). feel: "host" keeps
    the host's steering, assists, tyre falloff and suspension geometry; "medoid" takes
    each set from the most typical of the three best hosts (FEEL_GROUPS)."""
    if rules is None:
        rules = SAME_MODEL_RULES if ctx.same_model else RULES
    if ctx.same_model or ctx.family:
        feel = "host"  # a sibling's own sets beat any blend (LOO: 214 on 216, 275 on 274)
    b = ctx.base
    z = forza_values(ctx)
    new: dict[int, float] = {}
    changes = []

    def put(pid: int, val: float, rule: str) -> None:
        if leave_out(b, new, pid, val):
            return
        new[pid] = float(val)
        if abs(val - value(b, pid)) > 1e-9 * max(1.0, abs(val)):
            changes.append((pid, b.get(pid), float(val), rule))

    def apply(pid: int) -> None:
        rule = rules.get(pid, "H")
        if rule == "Z":
            if pid in z:
                put(pid, z[pid], "Z")
            return
        c = candidates(ctx, pid, new)
        if rule in c:
            put(pid, c[rule], rule)

    apply(0)
    for pid in sorted(set(b) | set(rules) | set(z)):
        if pid in NOT_HANDLING or pid in GEAR_PIDS or pid in (0, 32, 371, 392, 360, 42, 160):
            continue
        apply(pid)
    for pid in (360, 42, 160):
        apply(pid)
    if feel == "medoid":
        for group, pids in FEEL_GROUPS.items():
            donor = feel_donor(ctx, pids)
            if donor["id"] != ctx.host["id"]:
                for pid in pids:
                    if rules.get(pid, "H") == "H":
                        put(pid, value(donor["params"], pid), f"{group} from {donor['id']}")
    # Engine inertia and friction follow the torque they spin against (fleet: log p355 vs
    # log p18, r = 0.78): the fleet's median ratio to p18 times the new p18 (rule R). The
    # GT-R on the RUF and the Mito launched 1 to 3 s slow on heavy-engined hosts and
    # donors (runs 20261022-073627, -091649, -095156); R puts both within 15% of the
    # stock cars' values (GT-R 0.0103 vs 0.009, Mito 0.0052 vs 0.005).
    if engine_scale and not ctx.same_model:
        # Engine inertia (launch): the fleet's median ratio to p18 x new p18 (rule R).
        c = candidates(ctx, 355, new)
        if "R" in c:
            put(355, c["R"], "R: fleet median ratio to p18 x new p18")
        # Engine friction: the drivetrain donor's (best-ranked host with the car's
        # drivetrain), x new p18 / donor p18. The fleet ratio (R) gave the 370Z and the
        # GT-R 50 to 90% more friction than their stock cars and cost them 1.5 to 2 s to
        # 100 mph (run 20261022-100024); the engine-set medoid gave the Mito a 1971-style
        # Fiat 500's 0.0055 against its own 0.003, and a weak engine can't afford it
        # (Mito with the stock engine set: 0-60 9.5 -> 6.6 s, run 20261022-123030). Over
        # the 48 pairs the drivetrain donor is as close as the medoid (1.199 vs 1.201).
        donor = ctx.drive_donor or ctx.host
        p18_new, p18_donor = new.get(18, value(b, 18)), value(donor["params"], 18)
        if p18_donor > 0:
            put(
                358,
                value(donor["params"], 358) * p18_new / p18_donor,
                f"engine set from {donor['id']} x p18 {p18_new:.0f}/{p18_donor:.0f}",
            )
    drive_group = DRIVE_GROUP if drive_set else ()
    donor = ctx.drive_donor
    if drive_group and donor is not None and donor["id"] != ctx.host["id"]:
        for pid in drive_group:
            put(pid, value(donor["params"], pid), f"drivetrain set from {donor['id']}")
    p160, p42, p360 = new.get(160, value(b, 160)), new.get(42, value(b, 42)), new.get(360, value(b, 360))
    for pid, val in gear_rule(ctx, rules.get(361, "GH"), p160, p42, p360, z).items():
        put(pid, val, rules.get(361, "GH"))
    # Drive split: the car's drivetrain; an AWD car keeps an AWD host's split, else the fleet's.
    host_drive = ctx.host_specs.drive if ctx.host_specs else "RWD"
    if drive_group and donor is not None and (specs_from_stats(donor["stats"]) or ctx.specs).drive == ctx.specs.drive:
        pass  # the drive split came with the drivetrain set
    elif ctx.specs.drive == "FWD":
        put(371, 1.0, "D")
    elif ctx.specs.drive == "RWD":
        put(371, 0.0, "D")
    else:
        put(371, value(b, 371) if host_drive == "AWD" and value(b, 371) > 0 else 0.3, "D")
    put(392, ctx.car.wheelbase, "Forza wheelbase (hub patch)")
    # CoG fore/aft: the host's offset from its axle midpoint, scaled to the new wheelbase
    # (CH), or Forza's weight distribution (CZ).
    if rules.get(32, "CH") == "CZ":
        put(32, z.get(32, value(b, 32)), "CZ")
    else:
        hw = value(b, 392) or ctx.host["wheelbase"]
        put(32, ctx.host["axle_mid"] + (value(b, 32) - ctx.host["axle_mid"]) * ctx.car.wheelbase / hw, "CH")
    if rules.get(30) == "CY" and not ctx.same_model:
        cy = ctx.host["hub_y"] + ctx.car.cm_height - ctx.car.drive_radius + COG_HEIGHT_OFFSET
        put(30, cy, "CY")
        if 599 in b:
            put(599, cy, "CY (p599 = p30)")
    # Drift dial: move p369 (lateral stiffness) so the car's drift index lands where the
    # fleet puts a car of its specs: "rel" the host's index moved by the fleet model's
    # difference between the two cars' specs, "abs" the fleet model's prediction. At most
    # +-30% on p369. Not on a same-model host (that VO3D is the car).
    if drift in ("rel", "abs") and not ctx.same_model:
        merged = dict(b)
        merged.update(new)
        target = ctx.model.predict_target("drift_index", drift_index, ctx.specs)
        if drift == "rel":
            target = (
                drift_index(ctx.host["params"])
                + target
                - ctx.model.predict_target("drift_index", drift_index, ctx.host_specs)
            )
        now = drift_index(merged)
        k = value(merged, 369)
        want = k + (target - now) * value(merged, 0) / DRIFT_FIT[1]
        want = min(max(want, 0.7 * k), 1.3 * k)
        put(369, want, f"drift dial ({drift}): index {now:.1f} -> {target:.1f}")
    return new, changes


def context(
    host_id: int,
    media: str,
    fleet: dict[int, dict],
    ranking_ids: list[int] | None = None,
    exclude: set[int] = frozenset(),
    base: dict[int, float] | None = None,
    car: H.ForzaCar | None = None,
    dbs: list[Path] | None = None,
    same_model: bool = False,
) -> Context:
    dbs = dbs or DBS
    car = car or H.load_forza(media, dbs)
    specs = specs_from_forza(media, car, dbs)
    host = fleet[host_id]
    host_specs = specs_from_stats(host["stats"]) or specs
    model = FleetModel(fit_set(fleet), exclude)

    # Set donors: the host and the next best-ranked hosts of the car's drivetrain and era
    # (within 10 years); the Mito's second-best host, a 1971 Fiat 500, gave it a launch
    # 3 s slower than its host's (run 20261022-095156), and the Huayra (2012) took the 1997
    # McLaren F1's steering (auto counter-steer p380 0.4, no correction rate at big slip,
    # p390 0) and "can't exit a drift" (driving feedback). LOO: 10 years 0.638, 15 years 0.641.
    def fits(i: int) -> bool:
        st = specs_from_stats(fleet[i]["stats"])
        return st is not None and st.drive == specs.drive and abs(st.year - specs.year) <= 10

    ids = ranking_ids or [host_id]
    neighbors = [fleet[host_id]] + [fleet[i] for i in ids if i != host_id and fits(i)][:2]
    rows = fit_set(fleet)
    scales = {pid: _scale(rows, pid) for pid in {p for g in FEEL_GROUPS.values() for p in g}}
    info = HS.forza_info(media)
    hrec = next((h for h in HS.survey() if h["id"] == host_id), None)
    family = bool(hrec) and HS.identity_score(hrec, info)[0] >= 30
    drive_donor = None
    for i in ranking_ids or [host_id]:
        donor_specs = specs_from_stats(fleet[i]["stats"]) if i in fleet else None
        if (i == host_id and host_specs.drive == specs.drive) or (donor_specs and donor_specs.drive == specs.drive):
            drive_donor = fleet[i]
            break
    return Context(
        host,
        host_specs,
        car,
        specs,
        model,
        neighbors,
        dict(base if base is not None else host["params"]),
        scales,
        same_model,
        family,
        drive_donor,
    )


def apply(
    base: dict[int, float], host_id: int, media: str, dbs: list[Path], feel: str = "medoid", log=print
) -> tuple[dict[int, float], dict]:
    """port_car's entry point: the clone's VO3D (host values plus earlier edits) in, the
    DSF-first VO3D out, with a report."""
    fleet = load_fleet()
    car = H.load_forza(media, dbs)
    order, same = ranked_hosts(media, fleet)
    # DSF_HANDLING_LOO=1 (test builds): treat the car as absent from DSF, as the
    # leave-one-out check does: no stock version of it among the feel donors, fleet
    # fits without it.
    loo = os.environ.get("DSF_HANDLING_LOO") == "1"
    ranking = [host_id] + [i for i in order if i != host_id and not (loo and i in same)]
    ctx = context(
        host_id,
        media,
        fleet,
        ranking,
        base=base,
        car=car,
        dbs=dbs,
        exclude=same if loo else frozenset(),
        same_model=host_id in same and not loo,
    )
    new, changes = build(ctx, feel=feel)
    out = dict(base)
    out.update(new)
    # In-game calibration results (src/dsfforza/build/handling_tune.py), per car and host.
    tuned = {}
    tune_cache = REPO / "out" / "cache" / "handling-tune.json"
    if tune_cache.exists() and os.environ.get("DSF_HANDLING_NOTUNE") != "1":
        entry = json.loads(tune_cache.read_text()).get(f"{media}@{host_id}")
        if entry:
            # Any numeric key: in-game calibration (p18, p345) or a hand-set value from
            # driving feedback ("1": 3000, ...); "scale": {"369": 1.1} multiplies.
            for key, val in entry.items():
                if key.isdigit():
                    out[int(key)] = float(val)
                    tuned[int(key)] = out[int(key)]
            for key, factor in (entry.get("scale") or {}).items():
                out[int(key)] = value(out, int(key)) * float(factor)
                tuned[int(key)] = out[int(key)]
            log(f"  handling (dsf): in-game tuned values {tuned} from {tune_cache.name}")
    donors = sorted({rule.split(" from ")[1] for _, _, _, rule in changes if " from " in rule})
    log(
        f"  handling (dsf): {len(changes)} parameters changed on host {host_id}"
        + (" (same model: stock values kept)" if ctx.same_model else "")
        + (f", feel sets from {', '.join(donors)}" if donors else "")
        + f"; {car.mass:.0f} kg, {car.drive}, {ctx.specs.power:.0f} kW, {ctx.specs.torque:.0f} Nm"
    )
    bars = stat_bars(out, ctx.specs)
    log(f"  handling (dsf): stat bars Speed {bars['Speed']}, Strength {bars['Strength']}, Drift {bars['Drift']}")
    return out, {
        "source": "dsf",
        "host": host_id,
        "same_model": ctx.same_model,
        "changed": len(changes),
        "bars": bars,
        "tuned": tuned,
        "feel_donors": donors,
        "changes": [[pid, old, val, rule] for pid, old, val, rule in changes],
    }


def ranked_hosts(media: str, fleet: dict[int, dict], exclude_model_of: int | None = None) -> tuple[list[int], set[int]]:
    """Host IDs best first (host_select), and the IDs of the car's own stock model."""
    hosts = HS.survey()
    info = HS.forza_info(media)
    same = {h["id"] for h in hosts if HS.identity_score(h, info)[0] >= 60}
    if exclude_model_of is not None:
        same.add(exclude_model_of)
    order = [r.id for r in HS.rank(info, hosts) if not r.excluded]
    return order, same


# --- leave-one-out ----------------------------------------------------------------------

GEAR_METRICS = {
    1001: "gear1_mph_at_redline",
    1002: "gear2_mph_at_redline",
    1003: "top_gear_redline_over_p160",
    1004: "gear_count",
}


def gear_metrics(params: dict[int, float]) -> dict[int, float]:
    """What the gearing does: speed at redline in first and second gear, top gear at
    redline relative to the top speed limit, gear count."""
    n = max(1, int(value(params, 282)))
    unit = value(params, 361) * value(params, 360) * value(params, 42) * math.pi / 30 * 2.23694
    out = {
        1001: value(params, 276) * unit,
        1004: float(n),
        1003: value(params, 276 + n - 1) * unit / max(value(params, 160), 1.0),
    }
    out[1002] = value(params, 277) * unit if n > 1 else out[1001]
    return out


def _scale(rows: list[dict], pid: int) -> float:
    vals = sorted(value(r["params"], pid) for r in rows)
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals) * 1.4826
    if mad <= 1e-9:
        mad = statistics.pstdev(vals)
    return mad if mad > 1e-9 else max(abs(med), 1.0)


def loo(
    rules_to_try: tuple[str, ...] = ("H", "M", "F", "A", "N", "Z", "R", "W"),
    csv_path: Path | None = None,
    verbose: bool = False,
) -> None:
    fleet = load_fleet()
    rows = fit_set(fleet)
    pids = sorted({pid for car in fleet.values() for pid in car["params"]} - NOT_HANDLING)
    pids = [p for p in pids if sum(1 for c in fleet.values() if p in c["params"]) >= 10 or p in H.PARAMS]
    scales = {pid: _scale(rows, pid) for pid in pids}
    gm = [gear_metrics(r["params"]) for r in rows]
    for k in GEAR_METRICS:
        vals = [g[k] for g in gm]
        med = statistics.median(vals)
        scales[k] = statistics.median(abs(v - med) for v in vals) * 1.4826 or statistics.pstdev(vals) or 1.0
    pids_and_metrics = pids + list(GEAR_METRICS)
    errors: dict[str, dict[int, list[float]]] = {}
    hosts_used = []
    per_car: dict[tuple, dict[str, list[float]]] = {}
    drift_err: dict[str, list[float]] = {}
    for sid, media in PAIRS.items():
        try:
            car = H.load_forza(media, DBS)
        except (SystemExit, ValueError, LookupError) as error:
            print(f"skip {media}: {error}", file=sys.stderr)
            continue
        order, same = ranked_hosts(media, fleet, sid)
        order = [i for i in order if i not in same]
        host_id = order[0]
        hosts_used.append((sid, media, host_id))
        ctx = context(host_id, media, fleet, order, exclude=same, car=car)
        truth = fleet[sid]["params"]
        z = forza_values(ctx)
        variants: dict[str, dict[int, float]] = {}
        # Per-parameter candidates, evaluated with the mass from rule Z (Forza mass) first.
        new0 = {0: z.get(0, value(ctx.base, 0)), 18: torque_power_rule(car)}
        for pid in pids:
            if pid in GEAR_PIDS or pid in (32, 371, 392):
                continue
            c = candidates(ctx, pid, new0)
            if pid in z:
                c["Z"] = z[pid]
            for rule, val in c.items():
                variants.setdefault(rule, {})[pid] = val
        # Joint rules.
        built, _ = build(ctx)
        full_dsf = dict(ctx.base)
        full_dsf.update(built)
        built, _ = build(ctx, feel="medoid")
        full_med = dict(ctx.base)
        full_med.update(built)
        built, _ = build(ctx, feel="medoid", drive_set=False)
        full_nod = dict(ctx.base)
        full_nod.update(built)
        drift_variants = {}
        for mode in ("rel", "abs"):
            built, _ = build(ctx, feel="medoid", drift=mode)
            drift_variants[mode] = dict(ctx.base)
            drift_variants[mode].update(built)
        for name, params in (
            ("medoid", full_med),
            ("drift_rel", drift_variants["rel"]),
            ("drift_abs", drift_variants["abs"]),
        ):
            drift_err.setdefault(name, []).append(abs(drift_index(params) - drift_index(truth)))
        full_forza = dict(ctx.base)
        full_forza.update(z)
        full_host = dict(ctx.base)
        for name, params in (
            ("dsf", full_dsf),
            ("medoid", full_med),
            ("nodrive", full_nod),
            ("forza", full_forza),
            ("host", full_host),
        ):
            variants[name] = {pid: value(params, pid) for pid in pids}
            variants[name][32] = value(params, 32) - ctx.host["axle_mid"] + fleet[sid]["axle_mid"]
        for rule in ("GH", "GZ", "GX"):
            p160 = full_dsf.get(160, value(ctx.base, 160))
            g = gear_rule(
                ctx, rule, p160, full_dsf.get(42, value(ctx.base, 42)), full_dsf.get(360, value(ctx.base, 360)), z
            )
            merged = dict(ctx.base)
            merged.update({k: full_dsf[k] for k in (42, 160, 360) if k in full_dsf})
            merged.update(g)
            variants[rule] = {pid: value(merged, pid) for pid in GEAR_PIDS}
            variants[rule].update(gear_metrics(merged))
        for name, params in (
            ("dsf", full_dsf),
            ("medoid", full_med),
            ("nodrive", full_nod),
            ("forza", full_forza),
            ("host", full_host),
        ):
            variants[name].update(gear_metrics(params))
        truth = dict(truth)
        truth_metrics = gear_metrics(truth)
        variants["CY"] = {30: car.cm_height - car.drive_radius + COG_HEIGHT_OFFSET + fleet[sid]["hub_y"]}
        for name in ("dsf", "medoid", "nodrive"):
            if name in variants and 30 in variants[name]:
                variants[name][30] += fleet[sid]["hub_y"] - ctx.host["hub_y"]
        for rule, _rel in (("CH", None), ("CZ", None)):
            hw = value(ctx.base, 392) or ctx.host["wheelbase"]
            if rule == "CH":
                off = (value(ctx.base, 32) - ctx.host["axle_mid"]) * car.wheelbase / hw
            else:
                off = z.get(32, value(ctx.base, 32)) - ctx.host["axle_mid"]
            variants[rule] = {32: off + fleet[sid]["axle_mid"]}
        for name, params in variants.items():
            for pid, val in params.items():
                if pid not in scales:
                    continue
                actual = truth_metrics[pid] if pid in truth_metrics else value(truth, pid)
                err = abs(val - actual) / scales[pid]
                errors.setdefault(name, {}).setdefault(pid, []).append(min(err, 10.0))
                if name in ("medoid", "forza", "host"):
                    w = WEIGHT.get(pid, 1.0 if pid in GEAR_METRICS else MISC_WEIGHT)
                    acc = per_car.setdefault((sid, media, host_id), {}).setdefault(name, [0.0, 0.0])
                    acc[0] += w * min(err, 10.0)
                    acc[1] += w
        if verbose:
            print(f"{sid} {media}: host {host_id} ({fleet[host_id]['model']})", file=sys.stderr)

    def mean(name, pid):
        e = errors.get(name, {}).get(pid)
        return statistics.mean(e) if e else None

    print(f"# leave-one-out over {len(hosts_used)} pairs; error = |value - Reflections'| / fleet MAD")
    print(
        "pid\tname\tweight\t"
        + "\t".join(rules_to_try + ("GH", "GZ", "GX", "CH", "CZ", "CY"))
        + "\tbest\tdsf\tforza\thost\tmedoid"
    )
    totals = {
        "dsf": [0.0, 0.0],
        "medoid": [0.0, 0.0],
        "nodrive": [0.0, 0.0],
        "forza": [0.0, 0.0],
        "host": [0.0, 0.0],
        "best": [0.0, 0.0],
    }
    lines = []
    for pid in pids_and_metrics:
        w = WEIGHT.get(pid, 1.0 if pid in GEAR_METRICS else MISC_WEIGHT)
        cells = [mean(r, pid) for r in rules_to_try + ("GH", "GZ", "GX", "CH", "CZ", "CY")]
        named = {r: c for r, c in zip(rules_to_try + ("GH", "GZ", "GX", "CH", "CZ", "CY"), cells) if c is not None}
        if not named:
            continue
        best = min(named, key=named.get)
        d, f, h, md = mean("dsf", pid), mean("forza", pid), mean("host", pid), mean("medoid", pid)
        nd = mean("nodrive", pid)
        for name, val in (
            ("dsf", d),
            ("medoid", md),
            ("nodrive", nd),
            ("forza", f),
            ("host", h),
            ("best", named[best]),
        ):
            if val is not None:
                totals[name][0] += w * val
                totals[name][1] += w
        line = (
            f"{pid}\t{GEAR_METRICS.get(pid) or H.name(pid)}\t{w}\t"
            + "\t".join("-" if c is None else f"{c:.2f}" for c in cells)
            + f"\t{best}\t{'-' if d is None else f'{d:.2f}'}\t{'-' if f is None else f'{f:.2f}'}\t{'-' if h is None else f'{h:.2f}'}\t{'-' if md is None else f'{md:.2f}'}"
        )
        lines.append(line)
        print(line)
    print("# weighted mean error: " + ", ".join(f"{k} {v[0] / v[1]:.3f}" for k, v in totals.items() if v[1]))
    for group, (gpids, _) in GROUPS.items():
        parts = []
        for name in ("dsf", "medoid", "nodrive", "forza", "host"):
            vals = [mean(name, p) for p in gpids if mean(name, p) is not None]
            if vals:
                parts.append(f"{name} {statistics.mean(vals):.3f}")
        print(f"#   {group}: " + ", ".join(parts))
    print("# hosts: " + ", ".join(f"{sid}->{h}" for sid, _, h in hosts_used))
    print(
        "# drift index |error| (deg of handbrake rotation): "
        + ", ".join(
            f"{k} mean {statistics.mean(v):.1f} median {statistics.median(v):.1f}" for k, v in drift_err.items()
        )
    )
    print("# per car (weighted mean error): id media host medoid forza host")
    for (sid, media, host_id), acc in per_car.items():
        print(
            "#   "
            + "	".join(
                [str(sid), media, str(host_id)] + [f"{acc[k][0] / acc[k][1]:.3f}" for k in ("medoid", "forza", "host")]
            )
        )
    for k in ("medoid", "forza", "host"):
        vals = sorted(acc[k][0] / acc[k][1] for acc in per_car.values())
        print(
            f"# {k}: median {statistics.median(vals):.3f}, worst {vals[-1]:.3f}, "
            f"better than forza on {sum(1 for a in per_car.values() if a[k][0] / a[k][1] < a['forza'][0] / a['forza'][1] - 1e-9)}"
        )
    if csv_path:
        csv_path.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("loo", help="leave-one-out over the 48 stock/Forza pairs")
    p.add_argument("--csv", type=Path)
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("build", help="print the DSF-first VO3D changes for a Forza car")
    p.add_argument("--car", required=True)
    p.add_argument("--host", type=int, help="default: host_select's choice")
    p.add_argument("--exclude-self", action="store_true", help="skip hosts of the car's own stock model")
    p.add_argument("--feel", choices=("medoid", "host"), default="medoid")
    sub.add_parser("refresh", help="rebuild the fleet cache")
    args = parser.parse_args(argv)
    if args.cmd == "loo":
        loo(csv_path=args.csv, verbose=args.verbose)
    elif args.cmd == "refresh":
        print(len(load_fleet(refresh=True)), "vehicles")
    else:
        fleet = load_fleet()
        order, same = ranked_hosts(args.car, fleet)
        if args.exclude_self:
            order = [i for i in order if i not in same]
        host = args.host or order[0]
        ctx = context(
            host,
            args.car,
            fleet,
            [host] + [i for i in order if i != host],
            exclude=same if args.exclude_self else set(),
            same_model=host in same,
        )
        _, changes = build(ctx, feel=args.feel)
        print(f"# {args.car} on {host} ({fleet[host]['model']}); specs {ctx.specs}")
        for pid, old, new, rule in changes:
            print(f"{pid}\t{H.name(pid)}\t{H.fmt(old)}\t{H.fmt(new)}\t{rule}")


if __name__ == "__main__":
    sys.exit(main())
