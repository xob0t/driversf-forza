"""Driver: San Francisco handling (VO3D): dump it, and generate it from Forza data.

VO3D is the sparse handling override list in each vehicle's full entry
(VEHC N, NONR/VO3D):

    u16 spool_id, u16 0, "VO3D", u32 version=1, u32 count,
    count x {u16 param_id, u16 0xAAAA, f32 value}

Driver.exe copies a static default for each of its 607 parameters into the
vehicle specification, then writes each listed value over it
(FUN_0088BD60: spec + 0x2C70 + 4 * id). research/handling.md lists what the
parameters mean and how that was established.

    python tools/handling.py dump <archive> --id 266 [--all]
    python tools/handling.py build <archive> --id 304 --car NIS_370Z_10 \\
        --db out/db/fh1_gamedb.slt --db out/db/fm4_gamedb.slt [--lua <vehiclestats.lua>] [--out vo3d.bin]
    python tools/handling.py apply <archive> <out.sp> --id 304 --car NIS_370Z_10 --db ... [--lua ...]

`build` starts from the host's VO3D (the vehicle at --id, normally a clone
made by clone_vehicle.py) and only changes parameters it can derive from the
Forza car. Everything else, including tyre grip, steering assists, cameras and
AI tuning, stays the host's. See MAPPING below and research/handling.md.
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import statistics
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from chnk import Entry, read_chunk, write_chunk
from vehicles import find, parse_sslp
from vehicle_mesh import child, payload

HEADER = struct.Struct("<HH4sII")
RECORD = struct.Struct("<HHf")
FILL = 0xAAAA
PARAM_COUNT = 607
NONR_LIMIT = 0x6D000  # per-slot NONR buffer for kind-0 entries (SSLP +0x08, spool-limits.md)

# --- parameter table ------------------------------------------------------------------
# id: (name, unit, evidence). "default" is the value Driver.exe uses when a VO3D
# leaves the ID out (evaluated from the initializer FUN_0088AC20).
# Evidence: S = confirmed-static (code read), D = data correlation across the
# 150 stock vehicles or the Forza pairs, H = hypothesis.


@dataclass(frozen=True)
class Param:
    name: str
    unit: str
    default: float
    evidence: str


PARAMS: dict[int, Param] = {
    0: Param("mass", "kg", 1700, "S: spec printer 'mass'; D: = Lua WeightMetric (r 0.99)"),
    1: Param("spring_rate", "game", 1000, "S: suspension force = p1 * compression (x p505 on one axle)"),
    2: Param("damper_rate", "game", 770, "S: suspension force += p2 * velocity (x p506 on one axle)"),
    3: Param("suspension_travel", "m", 0.3, "S: compression clamp, force ramps beyond it"),
    4: Param("suspension_min", "m", 0, "S: lower compression clamp / bump stop start"),
    13: Param("rolling_resistance", "N/(m/s) x4", 15, "S: force -= 0.25 * p13 * |v| (spec printer, FUN_008B2DF0)"),
    14: Param("p14", "?", 0.005, "S: read with p26 in FUN_008BE990"),
    15: Param("inertia_x", "kg m^2", 6200, "S: inertia diagonal (FUN_008B14F0); D: scales with mass x size"),
    16: Param("inertia_y", "kg m^2", 12500, "S: inertia diagonal, yaw; D: scales with mass x size"),
    17: Param("inertia_z", "kg m^2", 3200, "S: inertia diagonal, roll; D: scales with mass x size"),
    18: Param("engine_peak_torque", "game Nm", 500, "S: engine torque = p18 x curve x throttle; D: ~1.5 x real Nm"),
    24: Param("launch_speed_limit", "?", 10, "S: launch/burnout assist (FUN_008BCBB0)"),
    25: Param("ride_offset", "m", 0, "S: subtracted from suspension length (FUN_0088D620)"),
    26: Param("inertia_scale", "x", 1, "S: multiplies the inertia vector (FUN_008B14F0)"),
    30: Param("cog_y", "m", 0.2, "S: model-space CoG height; all hierarchy positions are shifted by -p30"),
    32: Param("cog_z", "m", 0, "S: model-space CoG fore/aft; D: implies 53% median front weight"),
    40: Param("idle_rpm", "rpm", 2383, "S: gearbox E[1]: torque curve start, downshift point"),
    42: Param("redline_rpm", "rpm", 13000, "S: gearbox E[2]: torque curve end, upshift base; D: = Forza RedlineRPM"),
    138: Param("p138", "?", 1, "S: read in FUN_008858F0"),
    140: Param("gravity", "m/s^2", -17, "S: static sag = -p140 * p0 / p1; no stock car overrides it"),
    141: Param("gravity_air", "m/s^2", -17, "S: FUN_008BE410, FUN_008BCAF0"),
    160: Param("top_speed_limit", "mph", 160, "S: spec printer 'limit topSpeed' (x 0.447 to m/s)"),
    166: Param("launch_throttle", "0..1", 0.9, "S: launch/burnout assist throttle threshold"),
    183: Param("upshift_fraction", "x redline", 0.9, "S: auto upshift when rpm > p183 * p42 (FUN_008C3CF0)"),
    184: Param("shift_fraction_b", "x redline", 0.7, "S: gearbox E[0xC]"),
    185: Param("shift_fraction_c", "x redline", 0.85, "S: gearbox E[0xE]"),
    186: Param("shift_fraction_d", "x redline", 0.95, "S: gearbox E[0xF]"),
    187: Param("reverse_speed_limit", "mph x 1.5", 50, "S: FUN_008B2DF0: p187 * 0.6705 m/s"),
    188: Param("suspension_droop_max", "m", 0.2, "S: FUN_0088D620 clamp"),
    239: Param("launch_assist", "bool", 1, "S: enables the launch/burnout block"),
    275: Param("gear_reverse", "x ref", -0.35, "S: speed ratio of reverse (forced negative)"),
    276: Param("gear_1", "x ref", 0.38, "S: speed per rpm relative to the reference gear"),
    277: Param("gear_2", "x ref", 0.52, "S"),
    278: Param("gear_3", "x ref", 0.66, "S"),
    279: Param("gear_4", "x ref", 0.81, "S"),
    280: Param("gear_5", "x ref", 0.95, "S"),
    281: Param("gear_6", "x ref", 1, "S"),
    282: Param("gear_count", "count", 6, "S: number of forward gears (read as int)"),
    292: Param("suspension_droop_min", "m", -1, "S: FUN_0088D620 clamp"),
    298: Param("steer_max_angle", "deg", 35, "S: x pi/180 in FUN_008B2DF0 and the steering code"),
    355: Param("engine_inertia", "game", 0.0035, "S: gearbox E[0x13]: engine rpm integrates dt / p355"),
    356: Param("driveline_inertia", "game", 0.003, "S: gearbox E[0x14]: dt / p356"),
    357: Param("engine_friction_0", "game", 0.1, "S: friction torque = (p359|w| + p358) w + p357"),
    358: Param("engine_friction_1", "game", 0.0035, "S"),
    359: Param("engine_friction_2", "game", 0, "S"),
    360: Param("wheel_radius", "m", 0.3, "S: v = gear x p361 x p360 x rpm x pi/30; D: = Forza tyre radius"),
    361: Param("drive_ratio", "1/ratio", 0.25, "S: see p360; D: = 1 / (final drive x reference gear)"),
    362: Param("p362", "?", 0.25, "S: gearbox E[0x1A] threshold"),
    364: Param("rev_limit_band", "x idle", 0.2, "S: gearbox E[0x15]"),
    365: Param("launch_ramp", "?", 100, "S: launch/burnout assist"),
    366: Param("p366", "0..1", 0.2, "S: FUN_008B2DF0 (1 - p366) x 0.8; D: rises with model year"),
    367: Param("tyre_long_stiffness", "game", 9000, "S: tyre model FUN_008B8110"),
    368: Param("tyre_grip", "game", 1080, "S: tyre force limit x load; D: r 0.95 with mass"),
    369: Param("tyre_lat_stiffness", "game", 9000, "S: tyre model, x 4"),
    371: Param("front_drive_share", "0..1", 0, "S: drive split (1 - p371) rear, p371 front; D: 1 on every FWD car"),
    376: Param("tyre_mu_slip_scale", "m/s", 10, "S: mu = p376 p378 / (p376 + slip) + p377"),
    377: Param("tyre_mu_sliding", "x", 0.9, "S: see p376"),
    378: Param("tyre_mu_peak_extra", "x", 0.45, "S: see p376"),
    392: Param("wheelbase", "m", 2, "D: equals the UPVH hub wheelbase on 140/140 four-wheel vehicles"),
    469: Param("p469", "?", 100000, "S: FUN_0088D620"),
    505: Param("spring_axle_scale", "x", 1, "S: multiplies p1 on one axle"),
    506: Param("damper_axle_scale", "x", 1.25, "S: multiplies p2 on one axle"),
    507: Param("spring_progression", "x", 0, "S: p1 x (1 + p507 x compression / p3)"),
    508: Param("damper_rebound_scale", "x", 2, "S: damper force when extending"),
    509: Param("aero_drag", "game", 0, "S: force -= p509 v^2 (spec printer)"),
    516: Param("shift_time", "s", 0.2, "S: gearbox E[0x1D] = p516 x 25 frames"),
    517: Param("tyre_long_scale", "x", 1, "S: tyre model, one axle"),
    522: Param("torque_at_idle", "x peak", 0.6, "S: torque curve value at p40"),
    523: Param("torque_at_redline", "x peak", 0.75, "S: torque curve value at p42"),
    524: Param("torque_peak_position", "0..1", 0.45, "S: (rpm - p40) / (p42 - p40) where torque = p18"),
    529: Param("handbrake_mu_base", "x", 1, "S: tyre model when handbrake > 0.5"),
    530: Param("handbrake_mu_extra", "x", 0.5, "S"),
    531: Param("handbrake_mu_slip", "m/s", 15, "S"),
    569: Param("clutch_stiffness", "game", 1, "S: gearbox E[0x21]"),
    570: Param("clutch_torque", "x p18", 4, "S: clutch limit = p570 x p18 x 0.25"),
    599: Param("cog_y_2", "m", 0, "D: equals p30 on most vehicles"),
}

# Camera mounts (FUN_0088DF80 / FUN_0088E230: vec3 per camera index). Scaled with body size.
CAMERA_VECTORS = [(300, 301, 302), (303, 304, 305), (327, 328, 329), (337, 338, 339), (340, 341, 342),
                  (394, 395, 396), (535, 536, 537), (549, 550, 551), (552, 553, 554)]
CAMERA_DEFAULTS = {301: 1.05, 302: 6.7, 304: 0.2, 305: -2, 328: 0.48, 329: -0.75, 338: 0.6, 339: -2.2, 395: 0.6,
                   396: 0.2, 536: 0.9, 537: 0.8, 550: 4.5, 551: 14, 553: 1, 554: -5}
for _i, _ids in enumerate(CAMERA_VECTORS):
    for _axis, _pid in zip("xyz", _ids):
        PARAMS.setdefault(_pid, Param(f"camera{_i}_{_axis}", "m", CAMERA_DEFAULTS.get(_pid, 0),
                                      "S: camera mount vec3 by camera index (FUN_0088DF80, FUN_0088E230)"))
for _axis, _pid, _default in zip("xyz", (430, 431, 432), (0, 0.3, 0.19)):
    PARAMS.setdefault(_pid, Param(f"camera_head_{_axis}", "m", _default,
                                  "S: camera 0x14 offset from hierarchy marker 0x2F (FUN_0088DF80)"))


def name(pid: int) -> str:
    return PARAMS[pid].name if pid in PARAMS else f"p{pid}"


# --- VO3D ------------------------------------------------------------------------------

@dataclass
class VO3D:
    spool_id: int
    params: dict[int, float]  # insertion order is kept when writing
    version: int = 1

    @classmethod
    def parse(cls, data: bytes) -> VO3D:
        spool_id, zero, tag, version, count = HEADER.unpack_from(data)
        if tag != b"VO3D":
            raise ValueError(f"not a VO3D: {tag!r}")
        params = {}
        for i in range(count):
            pid, fill, value = RECORD.unpack_from(data, HEADER.size + i * RECORD.size)
            params[pid] = value
        return cls(spool_id, params, version)

    def build(self) -> bytes:
        out = bytearray(HEADER.pack(self.spool_id, 0, b"VO3D", self.version, len(self.params)))
        for pid, value in self.params.items():
            if not 0 <= pid < PARAM_COUNT:
                raise ValueError(f"parameter {pid} out of range")
            out += RECORD.pack(pid, FILL, value)
        return bytes(out)

    def get(self, pid: int) -> float:
        if pid in self.params:
            return self.params[pid]
        if pid in PARAMS:
            return PARAMS[pid].default
        raise KeyError(f"p{pid} is not in this VO3D and has no known default")


# --- host vehicle ----------------------------------------------------------------------

@dataclass
class Host:
    spool_id: int
    vo3d: VO3D
    wheelbase: float
    track: float
    hub_y: float
    axle_mid: float
    stats: dict[str, str] = field(default_factory=dict)  # Lua vehicleStats entry, if given


def full_nonr(stream, root: list[Entry], spool_id: int) -> Entry:
    """NONR of the vehicle's full entry."""
    ssic = find(root, "SSIC")
    records = parse_sslp(payload(stream, find(find(ssic.children, "NONR").children, "SSLP")))
    index = {sid: idx for sid, idx, _ in records}
    if spool_id not in index:
        raise SystemExit(f"spool ID {spool_id} is not in the archive")
    return child(root[index[spool_id]], "NONR")


def host_entries(stream, root: list[Entry], spool_id: int) -> tuple[Entry, Entry]:
    """(NONR/VO3D, NONR/UPVH) entries of the vehicle's full entry."""
    nonr = full_nonr(stream, root, spool_id)
    return child(nonr, "VO3D"), child(nonr, "UPVH")


def hub_geometry(upvh_data: bytes) -> tuple[float, float, float, float]:
    """(wheelbase, track, hub_y, axle_mid) from the UPVH wheel table."""
    import upvh

    wheels = [w[:3] for w in upvh.parse(upvh_data).vec4s("wheels")]
    zs = [w[2] for w in wheels]
    return (max(zs) - min(zs), 2 * statistics.mean(abs(w[0]) for w in wheels),
            statistics.mean(w[1] for w in wheels), (max(zs) + min(zs)) / 2)


def lua_stats(path: Path | None) -> dict[int, dict[str, str]]:
    """vehicleStats from the game's vehiclestats.lua: {id: {key: value}}."""
    if path is None:
        return {}
    text = path.read_text(encoding="latin-1")
    out = {}
    for match in re.finditer(r"\[(\d+)\]\s*=\s*\{(.*?)\n\s*\}", text, re.S):
        out[int(match.group(1))] = {k: v.strip('"') for k, v in
                                    re.findall(r"(\w+)\s*=\s*(\"[^\"]*\"|[^,\n]+)", match.group(2))}
    return out


def load_host(archive: Path, spool_id: int, lua: Path | None = None) -> Host:
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0).entries
        vo3d_entry, upvh_entry = host_entries(stream, root, spool_id)
        vo3d = VO3D.parse(payload(stream, vo3d_entry))
        geometry = hub_geometry(payload(stream, upvh_entry))
    stats = lua_stats(lua)
    # A clone keeps its source's stats only if the Lua file has an entry for the new ID.
    return Host(spool_id, vo3d, *geometry, stats=stats.get(spool_id, {}))


# --- Forza car -------------------------------------------------------------------------

DRIVE_TYPES = {1: "FWD", 2: "RWD", 3: "AWD"}


@dataclass
class ForzaCar:
    media: str
    source: str  # database path
    mass: float  # kg
    front_weight: float  # 0..1
    cm_height: float  # m, from the ground
    block: tuple[float, float, float]  # inertia box: width, height, length (m)
    wheelbase: float
    track_front: float  # outer, m
    track_rear: float
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    tyre_radius_front: float
    tyre_radius_rear: float
    drive: str  # FWD / RWD / AWD
    rear_split: float  # AWD rear torque share
    final_drive: float
    gears: list[float]  # forward ratios
    reverse: float
    shift_time: float
    redline: float  # rpm
    stall: float
    curve_rpm: list[float]
    curve_nm: list[float]
    top_speed_mph: float
    steer_max_angle: float
    drag: float
    spring_front: float
    spring_rear: float

    @property
    def peak_torque(self) -> tuple[float, float]:
        """(Nm, rpm) at the curve's maximum up to the redline."""
        pairs = [(t, r) for r, t in zip(self.curve_rpm, self.curve_nm) if r <= self.redline]
        return max(pairs)

    @property
    def peak_power(self) -> tuple[float, float]:
        """(kW, rpm)."""
        return max((t * r * math.pi / 30 / 1000, r) for r, t in zip(self.curve_rpm, self.curve_nm)
                   if r <= self.redline)

    def torque_at(self, rpm: float) -> float:
        rpms, nms = self.curve_rpm, self.curve_nm
        if rpm <= rpms[0]:
            return nms[0]
        for (r0, t0), (r1, t1) in zip(zip(rpms, nms), zip(rpms[1:], nms[1:])):
            if r0 <= rpm <= r1:
                return t0 + (t1 - t0) * (rpm - r0) / (r1 - r0)
        return nms[-1]

    @property
    def drive_radius(self) -> float:
        if self.drive == "FWD":
            return self.tyre_radius_front
        if self.drive == "RWD":
            return self.tyre_radius_rear
        return self.rear_split * self.tyre_radius_rear + (1 - self.rear_split) * self.tyre_radius_front


def _tyre_radius(width_mm: float, aspect: float, rim_in: float) -> float:
    return rim_in * 0.0254 / 2 + width_mm * aspect / 100 / 1000


def load_forza(media: str, databases: list[Path]) -> ForzaCar:
    """Read a car's stock physics from the first gamedb.slt (FH1 or FM4) that has it."""
    for db in databases:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            car = con.execute("SELECT * FROM Data_Car WHERE MediaName = ? COLLATE NOCASE", (media,)).fetchone()
            if car is not None:
                return _read_car(con, car, db)
        finally:
            con.close()
    raise SystemExit(f"{media}: not in {', '.join(map(str, databases))}")


def _one(con, sql: str, *args) -> sqlite3.Row:
    row = con.execute(sql, args).fetchone()
    if row is None:
        raise ValueError(f"no row: {sql} {args}")
    return row


def _read_car(con, car: sqlite3.Row, db: Path) -> ForzaCar:
    cid = car["Id"]
    body_id = _one(con, "SELECT CarBodyID FROM List_UpgradeCarBody WHERE Ordinal = ? AND IsStock = 1", cid)[0]
    body = _one(con, "SELECT * FROM Data_CarBody WHERE Id = ?", body_id)
    weight = con.execute("SELECT * FROM List_UpgradeCarBodyWeight WHERE CarBodyId = ? AND IsStock = 1",
                         (body_id,)).fetchone()
    drivetrain_id = _one(con, "SELECT DrivetrainID FROM List_UpgradeDrivetrain WHERE Ordinal = ? AND IsStock = 1",
                         cid)[0]
    drive = DRIVE_TYPES[_one(con, "SELECT DrivetypeID FROM Data_Drivetrain WHERE DrivetrainID = ?",
                             drivetrain_id)[0]]
    gearbox = _one(con, "SELECT * FROM List_UpgradeDrivetrainTransmission WHERE DrivetrainID = ? AND IsStock = 1",
                   drivetrain_id)
    ratios = [gearbox[f"GearRatio{i}"] for i in range(gearbox["NumGears"])]
    diff = con.execute("SELECT * FROM List_UpgradeDrivetrainDifferential WHERE DrivetrainID = ? AND IsStock = 1",
                       (drivetrain_id,)).fetchone()
    engine_id = _one(con, "SELECT EngineID FROM List_UpgradeEngine WHERE Ordinal = ? AND IsStock = 1", cid)[0]
    cam = _one(con, "SELECT * FROM List_UpgradeEngineCamshaft WHERE EngineID = ? AND IsStock = 1", engine_id)
    curve = _one(con, "SELECT * FROM List_TorqueCurve WHERE TorqueCurveID = ?", cam["TorqueCurveFullThrottleID"])
    count = curve["NumTorqueValues"]
    step = cam["TorqueCurveMaxRPM"] / (count - 1)
    rpms = [i * step for i in range(count)]
    nms = [curve[f"v{i}"] * curve["TorqueScale"] for i in range(count)]
    # The last sample is often a large negative value past the curve's end; drop
    # everything after the peak from the first non-positive sample on.
    peak_index = max(range(count), key=nms.__getitem__)
    end = next((i for i in range(peak_index, count) if nms[i] <= 0), count)
    rpms, nms = rpms[:end], nms[:end]
    # Stock upgrade parts (turbos, intake, ...) rescale the curve. Rather than model
    # each, scale it so its peak matches the game's own simulated peak torque.
    sim_peak = car["SimPeakTorque"] * 100
    curve_peak = max(t for r, t in zip(rpms, nms) if r <= cam["RedlineRPM"])
    if sim_peak > 0 and curve_peak > 0:
        nms = [t * sim_peak / curve_peak for t in nms]
    springs = con.execute("SELECT FrontSpringDamperPhysicsID, RearSpringDamperPhysicsID FROM List_UpgradeSpringDamper"
                          " WHERE Ordinal = ? AND IsStock = 1", (cid,)).fetchone()
    rates = []
    for spring_id in (springs or (None, None)):
        row = con.execute("SELECT DefSpringRate FROM List_SpringDamperPhysics WHERE SpringDamperPhysicsID = ?",
                          (spring_id,)).fetchone()
        rates.append(row[0] if row else 0.0)
    return ForzaCar(
        media=car["MediaName"], source=str(db),
        mass=weight["Mass"] if weight else car["CurbWeight"] * 100,
        front_weight=weight["CMBackFront"] if weight else car["WeightDistribution"],
        cm_height=weight["CMHeight"] if weight else 0.5,
        block=(weight["BlockDimX"], weight["BlockDimY"], weight["BlockDimZ"]) if weight else (0, 0, 0),
        wheelbase=body["ModelWheelbase"], track_front=body["ModelFrontTrackOuter"],
        track_rear=body["ModelRearTrackOuter"],
        bbox_min=(body["PristineBoundingBoxMinX"], body["PristineBoundingBoxMinY"], body["PristineBoundingBoxMinZ"]),
        bbox_max=(body["PristineBoundingBoxMaxX"], body["PristineBoundingBoxMaxY"], body["PristineBoundingBoxMaxZ"]),
        tyre_radius_front=_tyre_radius(car["FrontTireWidthMM"], car["FrontTireAspect"], car["FrontWheelDiameterIN"]),
        tyre_radius_rear=_tyre_radius(car["RearTireWidthMM"], car["RearTireAspect"], car["RearWheelDiameterIN"]),
        drive=drive, rear_split=diff["RearToqueSplit"] if diff else 0.5,
        final_drive=gearbox["FinalDriveRatio"], gears=[r for r in ratios[1:] if r > 0],
        reverse=ratios[0] if ratios and ratios[0] < 0 else -3.5, shift_time=gearbox["GearShiftTime"],
        redline=cam["RedlineRPM"], stall=cam["StallRPM"], curve_rpm=rpms, curve_nm=nms,
        top_speed_mph=car["TopSpeed-mph"], steer_max_angle=car["SteerMaxAngle"],
        drag=car["BodyAeroLongitudinalDrag"] * (car["GameDragScale"] or 1.0),
        spring_front=rates[0], spring_rear=rates[1])


# --- mapping ---------------------------------------------------------------------------
# Fits from the 45 stock DSF vehicles that have a Forza counterpart (research/handling.md):
#   p160 ~ 50.83 + 0.477 * Forza TopSpeed-mph  (residual sd 8.3 mph; DSF caps at 160)
#   p18  ~ 14.735 * peak_Nm ** 0.621           (DSF torque is "game" torque, compressed)
#   inertia ~ k * box inertia from Forza's BlockDim (k = 2.30, 2.36, 2.70)
#   torque at idle / redline ~ 0.92 / 0.87 x Forza's value
# and the fleet's 5-95% ranges, used as clamps.
TOP_SPEED_FIT = (50.83, 0.477)
TOP_SPEED_CAP = 160.0
TORQUE_FIT = (14.735, 0.621)
INERTIA_K = (2.30, 2.36, 2.70)
CURVE_SCALE = (0.92, 0.87)
CLAMP = {522: (0.5, 0.87), 523: (0.53, 0.78), 524: (0.38, 0.75), 371: (0.15, 0.3)}
FRONT_WEIGHT_CLAMP = (0.42, 0.58)
# Scaled by the mass ratio (new / host): the stock fleet keeps these roughly
# proportional to mass (p368 = 0.65 x mass, IQR 11%), so scaling keeps the host's
# ride frequency, grip in g, and drivetrain-to-body inertia ratio.
MASS_SCALED = (1, 2, 13, 355, 356, 357, 358, 359, 367, 368, 369)
MAX_GEARS = 6


@dataclass
class Change:
    pid: int
    old: float | None  # host value, None if the host VO3D doesn't list it
    new: float
    rule: str


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(max(value, bounds[0]), bounds[1])


def gear_set(car: ForzaCar, top_speed_mph: float) -> tuple[list[float], float, float, float]:
    """(relative forward ratios, reverse, p361, p160) for DSF's gearbox.

    DSF speed in gear i: rel_i * p361 * p360 * rpm * pi / 30. p361 = 1 / (final
    drive * reference gear), the reference being the Forza gear closest to 1:1,
    as in the stock data. Stock vehicles reach p160 in top gear at redline
    (fleet median ratio 1.01), so the gears above second are compressed in log
    space, first and second kept, until top gear at redline equals the target
    top speed (stock data often keeps the low gears exact, e.g. the 370Z).
    """
    gears = car.gears[:MAX_GEARS]
    reference = min(car.gears, key=lambda g: abs(g - 1.0))
    p361 = 1 / (car.final_drive * reference)
    rel = [reference / g for g in gears]
    unit_mph = p361 * car.drive_radius * car.redline * math.pi / 30 * 2.23694  # mph at rel = 1
    target_rel = top_speed_mph / unit_mph
    anchor = 1 if len(rel) > 2 and rel[1] < target_rel else 0
    if len(rel) > anchor + 1 and rel[-1] > target_rel > rel[anchor]:
        base = rel[anchor]
        alpha = math.log(target_rel / base) / math.log(rel[-1] / base)
        rel = rel[:anchor + 1] + [base * (r / base) ** alpha for r in rel[anchor + 1:]]
    p160 = min(top_speed_mph, rel[-1] * unit_mph)
    return rel, reference / car.reverse, p361, p160


def build_vo3d(host: Host, car: ForzaCar, torque_scale: float | None = None,
               scale_cameras: bool = False) -> tuple[VO3D, list[Change]]:
    """Host VO3D with the parameters derivable from the Forza car replaced."""
    base = host.vo3d
    out: dict[int, float] = dict(base.params)
    changes: list[Change] = []

    def put(pid: int, value: float, rule: str) -> None:
        old = base.params.get(pid)
        out[pid] = float(value)
        changes.append(Change(pid, old, float(value), rule))

    host_mass = base.get(0)
    mass_ratio = car.mass / host_mass
    put(0, car.mass, "Forza body mass")
    for pid in MASS_SCALED:
        put(pid, base.get(pid) * mass_ratio, f"host x mass ratio {mass_ratio:.3f}")
    width, height, length = car.block
    if width and height and length:
        boxes = (height ** 2 + length ** 2, width ** 2 + length ** 2, width ** 2 + height ** 2)
        for pid, k, box in zip((15, 16, 17), INERTIA_K, boxes):
            put(pid, k * car.mass * box / 12, f"{k} x box inertia ({width:.2f} x {height:.2f} x {length:.2f} m)")

    # Engine.
    peak_nm, peak_rpm = car.peak_torque
    if torque_scale is None:
        torque = TORQUE_FIT[0] * peak_nm ** TORQUE_FIT[1]
        rule = f"fit 14.7 x {peak_nm:.0f} Nm ^ 0.621"
    else:
        torque = torque_scale * peak_nm
        rule = f"{torque_scale} x {peak_nm:.0f} Nm"
    put(18, torque, rule)
    put(42, car.redline, "Forza RedlineRPM")
    idle, redline = base.get(40), car.redline
    span = redline - idle
    put(524, _clamp((peak_rpm - idle) / span, CLAMP[524]), f"Forza torque peak at {peak_rpm:.0f} rpm")
    put(522, _clamp(car.torque_at(idle) / peak_nm * CURVE_SCALE[0], CLAMP[522]),
        f"{CURVE_SCALE[0]} x Forza torque at {idle:.0f} rpm / peak")
    put(523, _clamp(car.torque_at(redline) / peak_nm * CURVE_SCALE[1], CLAMP[523]),
        f"{CURVE_SCALE[1]} x Forza torque at redline / peak")

    # Gearbox and top speed.
    target = min(TOP_SPEED_FIT[0] + TOP_SPEED_FIT[1] * car.top_speed_mph, TOP_SPEED_CAP)
    rel, reverse, p361, p160 = gear_set(car, target)
    put(360, car.drive_radius, f"Forza {car.drive} tyre radius")
    put(361, p361, f"1 / (final drive {car.final_drive} x reference gear)")
    put(282, len(rel), f"{len(car.gears)} Forza forward gears, DSF holds {MAX_GEARS}")
    put(275, reverse, "reverse / reference gear")
    for i, r in enumerate(rel):
        put(276 + i, r, "Forza gear, upper gears compressed to the top speed")
    put(160, p160, f"fit from Forza top speed {car.top_speed_mph:.0f} mph")

    # Drivetrain.
    if car.drive == "FWD":
        put(371, 1.0, "FWD")
    elif car.drive == "RWD":
        put(371, 0.0, "RWD")
    else:
        put(371, _clamp(1 - car.rear_split, CLAMP[371]), f"AWD, Forza rear split {car.rear_split}")

    # Geometry. forza2dsf keeps the host's axle midpoint and moves the hubs to the
    # Forza wheelbase, so the CoG goes there too.
    put(392, car.wheelbase, "Forza wheelbase (= UPVH hub wheelbase in stock data)")
    front = _clamp(car.front_weight, FRONT_WEIGHT_CLAMP)
    put(32, host.axle_mid + (front - 0.5) * car.wheelbase, f"CoG at {front:.2f} front weight")

    if scale_cameras:
        host_wheelbase = base.params.get(392, host.wheelbase)
        z_scale = car.wheelbase / host_wheelbase
        for vector in CAMERA_VECTORS:
            pid = vector[2]  # only the fore/aft component; heights stay the host's
            if pid in base.params:
                put(pid, base.params[pid] * z_scale, f"camera z x wheelbase ratio {z_scale:.3f}")

    return VO3D(base.spool_id, out, base.version), changes


# --- archive I/O -----------------------------------------------------------------------

def replace_vo3ds(archive: Path, out_path: Path, vo3ds: dict[int, VO3D]) -> None:
    """Write a copy of `archive` with the VO3D of each spool ID in `vo3ds` replaced."""
    if out_path.resolve() == archive.resolve():
        raise SystemExit("refusing to overwrite the input archive")
    from chnk import entry_size

    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        for spool_id, vo3d in vo3ds.items():
            nonr = full_nonr(stream, root.entries, spool_id)
            child(nonr, "VO3D").payload = vo3d.build()
            if entry_size(nonr) > NONR_LIMIT:
                raise SystemExit(f"{spool_id}: NONR would be {entry_size(nonr):#x} bytes, "
                                 f"over the {NONR_LIMIT:#x} spool slot")
        with out_path.open("wb") as out:
            write_chunk(out, stream, root.entries, root.version)


# --- CLI -------------------------------------------------------------------------------

def fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.6g}"


def cmd_dump(args: argparse.Namespace) -> None:
    host = load_host(args.archive, args.id)
    v = host.vo3d
    print(f"# spool {v.spool_id}, VO3D v{v.version}, {len(v.params)} parameters; "
          f"hubs: wheelbase {host.wheelbase:.3f}, track {host.track:.3f}, axle mid z {host.axle_mid:.3f}")
    ids = sorted(set(v.params) | (set(PARAMS) if args.all else set()))
    print("id\tname\tvalue\tdefault\tunit")
    for pid in ids:
        p = PARAMS.get(pid)
        value = v.params.get(pid)
        print(f"{pid}\t{name(pid)}\t{fmt(value) if value is not None else '(default)'}\t"
              f"{fmt(p.default) if p else '?'}\t{p.unit if p else ''}")


def _build(args: argparse.Namespace) -> tuple[Host, ForzaCar, VO3D, list[Change]]:
    host = load_host(args.archive, args.id, args.lua)
    car = load_forza(args.car, args.db)
    vo3d, changes = build_vo3d(host, car, args.torque_scale, args.scale_cameras)
    peak_nm, peak_rpm = car.peak_torque
    kw, kw_rpm = car.peak_power
    print(f"# {car.media} ({Path(car.source).name}): {car.mass:.0f} kg, {car.drive}, {peak_nm:.0f} Nm @ "
          f"{peak_rpm:.0f}, {kw:.0f} kW @ {kw_rpm:.0f}, redline {car.redline:.0f}, {len(car.gears)} gears, "
          f"top {car.top_speed_mph:.0f} mph, wheelbase {car.wheelbase:.3f}", file=sys.stderr)
    if host.stats:
        s = host.stats
        print(f"# host {host.spool_id}: Lua {s.get('WeightMetric')} kg, {s.get('TorqueMetric')} Nm, "
              f"{s.get('PowerMetric')} kW, {s.get('Drivetrain')}", file=sys.stderr)
    if abs(host.wheelbase - car.wheelbase) > 0.02:
        print(f"# note: the archive's hub wheelbase is {host.wheelbase:.3f} m; run forza2dsf (hub patch) "
              "first so geometry and handling agree", file=sys.stderr)
    print("id\tname\thost\tnew\trule", file=sys.stderr)
    for c in changes:
        print(f"{c.pid}\t{name(c.pid)}\t{fmt(c.old)}\t{fmt(c.new)}\t{c.rule}", file=sys.stderr)
    return host, car, vo3d, changes


def cmd_build(args: argparse.Namespace) -> None:
    _, _, vo3d, _ = _build(args)
    if args.out:
        args.out.write_bytes(vo3d.build())
        print(f"wrote {args.out} ({len(vo3d.params)} parameters)", file=sys.stderr)


def cmd_apply(args: argparse.Namespace) -> None:
    _, _, vo3d, _ = _build(args)
    replace_vo3ds(args.archive, args.out, {args.id: vo3d})
    print(f"wrote {args.out}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(required=True)

    dump = sub.add_parser("dump", help="print a vehicle's VO3D with parameter names")
    dump.add_argument("archive", type=Path)
    dump.add_argument("--id", type=int, required=True, help="base spool ID")
    dump.add_argument("--all", action="store_true", help="also list named parameters the VO3D leaves at default")
    dump.set_defaults(func=cmd_dump)

    for command, func, help_text in (("build", cmd_build, "derive a VO3D for a Forza car (report, optional .bin)"),
                                     ("apply", cmd_apply, "derive a VO3D and write it into a copy of the archive")):
        p = sub.add_parser(command, help=help_text)
        p.add_argument("archive", type=Path, help="archive holding the host vehicle at --id")
        if command == "apply":
            p.add_argument("out", type=Path)
        p.add_argument("--id", type=int, required=True, help="base spool ID of the host vehicle")
        p.add_argument("--car", required=True, help="Forza media name, e.g. NIS_370Z_10")
        p.add_argument("--db", type=Path, action="append", required=True,
                       help="gamedb.slt (FH1 and/or FM4); the first that has the car is used")
        p.add_argument("--lua", type=Path, help="vehiclestats.lua (optional, only for reporting the host)")
        p.add_argument("--torque-scale", type=float,
                       help="p18 = scale x Forza peak Nm instead of the fleet fit (stock median 1.38)")
        p.add_argument("--scale-cameras", action="store_true",
                       help="scale the host's camera mounts fore/aft by the wheelbase ratio")
        if command == "build":
            p.add_argument("--out", type=Path, help="write the VO3D payload here")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
