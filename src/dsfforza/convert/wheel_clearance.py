"""Wheel clearance of built cars: the tyres against the body (research/wheels.md,
"Wheel clearance").

For each wheel of a car in a built archive, the tyre (a cylinder with the radius,
width and centre of the drawn wheel part) is placed where the game draws it and the
signed distance from it to the body of the clean exterior entry (kind 2, what the
player's car shows) is measured over the tyre's outboard half: positive is a gap,
negative a tyre through the bodywork by that much. Poses:

- `design`: the UPVH hub as stored;
- `rest`: the hub moved by the engine's static wheel offset (`rest_offsets`), the
  pose the game shows a parked car in;
- `bump`: the hub at the top of its visual travel (VO3D p188);
- `lock`: a front wheel at rest, steered to VO3D p298 either way;
- `forza`: Forza's own tyre (Data_Car's size) at Forza's static position, from the
  build manifest, the reference the others are compared with; `forza lock` that tyre
  steered to Data_Car.SteerMaxAngle.

The engine's visual wheel offset (FUN_0088D620, research/wheels.md): with c the
wheel's suspension compression, the wheel is drawn

    up = clamp(c * (1 - p1 / p469) - sag - p25, p292, p188)

above its hub-table position, where sag = max(p4, 17 m / (100 p1)) is the
static compression FUN_0088D750 computes for an even load. At rest c balances
the spring (FUN_008BA900: k = p1, times p505 on the rear axle, no force below p4
and a ramp to full force over the next 0.1 m, progression p507) against the
axle's share of the weight (the CoG at z = p32 between the hubs). In game the
change in rest pose between VO3D variants of one car follows this within 1 mm
(research/wheels.md).

`--as-fixed` measures a build made before the wheel-clearance fix as the
converter builds it now (`as_fixed`), so a survey of the old full-roster archive
shows the effect without converting 573 cars again. `--forza-lock` does the same for
the steering lock: the `lock` pose at Data_Car.SteerMaxAngle, the p298 the converter
writes since issue #54, instead of the archive's p298.

Usage:
    uv run dsff wheel_clearance survey out/final/dngvehicles.sp --manifest out/final/dngvehicles.json --out out/wc/survey.tsv
    uv run dsff wheel_clearance car out/final/dngvehicles.sp 73 --manifest out/final/dngvehicles.json [--as-fixed]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# FUN_0088D750: sag = -p140 * p0 / (wheels * 25) / p1, p140 = -17 on every vanilla car.
SAG_PER_KG = 17.0 / (4 * 25)
DEAD_ZONE_RAMP = 0.1  # m: FUN_008BA900 ramps the force from 0 at p4 to full at p4 + 0.1
TYRE_CORE = 0.75  # body points nearer the axle than this share of the radius are inside the rim (vanilla calipers)
SAMPLE_STEP = 0.008  # m between sample points on body triangles near a wheel


def _param(params: dict[int, float], pid: int) -> float:
    """A VO3D parameter, or the engine's default when the VO3D leaves it out."""
    from dsfforza.build import handling

    return params[pid] if pid in params else handling.VO3D(0, {}).get(pid)


@dataclass
class Wheel:
    name: str  # FL FR RL RR (L = +x, the car's left)
    hub: np.ndarray  # UPVH hub position
    front: bool
    radius: float
    width: float
    offset: np.ndarray  # tyre centre from the hub, model axes (0: centred on the hub)


@dataclass
class Car:
    spool_id: int
    body: np.ndarray  # (T, 3, 3) exterior-entry triangles without the wheel parts
    wheels: list[Wheel]
    params: dict[int, float] = field(default_factory=dict)
    split: bool = False  # the host has separate front and rear wheel parts

    def p(self, pid: int) -> float:
        return _param(self.params, pid)


# --- loading ------------------------------------------------------------------------


def _triangles(mesh, sub, cache: dict) -> np.ndarray:
    from dsfforza.game import car_render

    pos, _, _, _ = car_render._submodel_triangles(mesh, sub, cache)
    return pos


def load_car(archive: Path, spool_id: int, entries=None, stream=None) -> Car:
    """The clean exterior entry's body and wheel parts, the hub table and the VO3D of
    the full entry."""
    from dsfforza.build import handling
    from dsfforza.convert import forza2dsf
    from dsfforza.formats import upvh as uv
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.vehicle_mesh import child, payload
    from dsfforza.game import car_render
    from dsfforza.game.gui_assets import _root_entries

    archive = Path(archive)
    own = stream is None
    stream = stream or archive.open("rb")
    try:
        entries = entries if entries is not None else _root_entries(archive, stream)
        kinds = uv.entries_for(stream, entries, spool_id, None)
        full, ext = kinds[0], kinds[2]
        upvh = payload(stream, child(child(full, "NONR"), "UPVH"))
        hw = forza2dsf.host_wheels(
            upvh,
            payload(stream, child(child(full, "NONR"), "MODL")),
            payload(stream, child(child(ext, "NONR"), "MODL")),
        )
        params = dict(handling.VO3D.parse(payload(stream, child(child(full, "NONR"), "VO3D"))).params)
    finally:
        if own:
            stream.close()
    _, mesh = vm.load(archive, entries.index(ext), entries)
    cache: dict = {}
    body, parts = [], {}
    for n, part in enumerate(mesh.parts):
        if not part.lods:
            continue
        lod = min((lod for lod in part.lods if lod.mask > 1), key=lambda lod: lod.mask, default=part.lods[0])
        subs = [mesh.submodels[i] for i in lod.submodels]
        if not subs:
            continue
        tris = np.concatenate([_triangles(mesh, s, cache) for s in subs])
        wheel = n in hw.exterior_parts or any(
            s.material in car_render.TYRE_UIDS or mesh.vertex_buffers[s.vertex_buffer].stride == 20 for s in subs
        )
        if wheel:
            parts[n] = tris
        else:
            body.append(tris)
    dims = {n: tyre_dims(t) for n, t in parts.items()}
    mid = sum(hb[2] for hb in hw.hubs) / 4
    wheels = []
    for hub in hw.hubs:
        front = hub[2] > mid
        axle = "front" if front else "rear"
        part = next((p for p in sorted(dims) if hw.axles.get((2, p)) == axle), None)
        if part is None:
            part = next((p for p in sorted(dims) if hw.axles.get((2, p), "both") == "both"), min(dims))
        radius, width, centre = dims[part]
        # One model serves both sides with its +x outboard: the -x wheels turn 180 degrees
        # about y (quaternion (0, 1, 0, 0)), so local (x, y, z) is (-x, y, -z) on them.
        sign = 1.0 if hub[0] > 0 else -1.0
        offset = np.array([sign * centre[0], centre[1], sign * centre[2]])
        name = ("F" if front else "R") + ("L" if hub[0] > 0 else "R")
        wheels.append(Wheel(name, np.array(hub, float), front, radius, width, offset))
    split = any(axle == "rear" for axle in hw.axles.values())
    return Car(spool_id, np.concatenate(body).astype(np.float64), wheels, params, split)


def as_fixed(car: Car, entry: dict) -> Car:
    """The car as the converter builds it since the wheel-clearance fix, worked out from
    a build made before it (its manifest entry): wheels at Data_Car's tyre sizes
    (forza2dsf.axle_fits), centred, on Forza's ground at the track outer, hubs lowered by
    the engine's rest offset (forza2dsf.settle_wheels). The body stays where it is; the
    hubs move against it."""
    import dataclasses

    from dsfforza.convert import forza2dsf

    pl = entry.get("placement") or {}
    front_drawn = next(w for w in car.wheels if w.front)
    model_r, model_w = pl["tyre_radius"], pl.get("tyre_width", front_drawn.width)
    fits = forza2dsf.axle_fits(model_r, model_w, pl.get("tyres"))
    rear_fit = fits["rear"] if car.split else fits["front"]
    ground = pl["host_hub_y"] - pl.get("ground_radius", model_r)
    track_outer = pl.get("track_outer", pl["track"] + front_drawn.width)
    rear_track_outer = 2 * pl["rear_half_track"] + model_w * (pl.get("rear_fit") or [1.0, 1.0])[0]
    fronts = [w.hub[2] for w in car.wheels if w.front]
    rears = [w.hub[2] for w in car.wheels if not w.front]
    up = rest_offsets(car.params, sum(fronts) / len(fronts), sum(rears) / len(rears))
    wheels = []
    for w in car.wheels:
        fit, outer, axle = (fits["front"], track_outer, "front") if w.front else (rear_fit, rear_track_outer, "rear")
        radius, width = model_r * fit[1], model_w * fit[0]
        hub = np.array([math.copysign((outer - width) / 2, w.hub[0]), ground + radius - up[axle], w.hub[2]])
        wheels.append(Wheel(w.name, hub, w.front, radius, width, np.zeros(3)))
    return dataclasses.replace(car, wheels=wheels)


def tyre_dims(tris: np.ndarray) -> tuple[float, float, np.ndarray]:
    """(radius, width, centre) of a wheel part in its own frame (axle x, hub at the
    origin): its bounding box. The tyre is the part's outermost geometry; some Forza
    wheels aren't centred on their origin (TOY_6_Supra_05: 49 mm out, 80 mm down)."""
    pts = tris.reshape(-1, 3).astype(np.float64)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    centre = (lo + hi) / 2
    radius = float((hi[1] - lo[1] + hi[2] - lo[2]) / 4)
    # across: the tread only (a knock-off spinner sticks out past the tyre: AST_DBR1_58)
    tread = pts[np.hypot(pts[:, 1] - centre[1], pts[:, 2] - centre[2]) > 0.85 * radius, 0]
    centre[0] = (tread.min() + tread.max()) / 2
    return radius, float(tread.max() - tread.min()), centre


# --- the engine's static wheel offset -----------------------------------------------


def spring_force(c: float, k: float, travel: float, dead: float, progression: float) -> float:
    """FUN_008BA900's spring force for compression c (per unit of p1 x axle scale = k)."""
    if c > travel:
        eff = (c - travel) * 5.0 + travel
    elif c <= dead:
        eff = 0.0
    elif c < dead + DEAD_ZONE_RAMP:
        eff = (c - dead) / DEAD_ZONE_RAMP * (DEAD_ZONE_RAMP + dead)
    else:
        eff = c
    return k * (1 + min(max(c / travel, 0.0), 1.0) * progression) * eff


def rest_compression(load: float, k: float, travel: float, dead: float, progression: float) -> float:
    lo, hi = 0.0, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if spring_force(mid, k, travel, dead, progression) < load:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def rest_offsets(params: dict[int, float], front_z: float, rear_z: float) -> dict[str, float]:
    """How far above its hub-table position the engine draws each axle's wheels at rest (m)."""

    def p(pid):
        return _param(params, pid)

    mass, k, travel, dead = p(0), p(1), p(3), p(4)
    sag = max(dead, SAG_PER_KG * mass / k)
    share_front = min(max((p(32) - rear_z) / (front_z - rear_z), 0.05), 0.95) if front_z != rear_z else 0.5
    out = {}
    for axle, share, scale in (("front", share_front, 1.0), ("rear", 1 - share_front, p(505))):
        load = SAG_PER_KG * mass * 2 * share
        c = rest_compression(load, k * scale, travel, dead, p(507))
        up = c * (1 - k / p(469)) - sag - p(25)
        out[axle] = min(max(up, p(292)), p(188))
    return out


# --- clearance ----------------------------------------------------------------------


def near_points(body: np.ndarray, hub: np.ndarray, reach: float, step: float = SAMPLE_STEP) -> np.ndarray:
    """Points on the body triangles within `reach` of the hub (box test), sampled about
    every `step` metres."""
    lo, hi = hub - reach, hub + reach
    tmin, tmax = body.min(axis=1), body.max(axis=1)
    sel = body[np.all((tmax >= lo) & (tmin <= hi), axis=1)]
    if not len(sel):
        return np.zeros((0, 3))
    edges = np.linalg.norm(sel[:, [1, 2, 0]] - sel, axis=2).max(axis=1)
    out = []
    for n in np.unique(np.clip(np.ceil(edges / step).astype(int), 1, 80)):
        tri = sel[np.ceil(edges / step).clip(1, 80).astype(int) == n]
        i, j = np.meshgrid(np.arange(n + 1), np.arange(n + 1), indexing="ij")
        keep = i + j <= n
        a, b = i[keep] / n, j[keep] / n
        w = np.stack((1 - a - b, a, b), axis=1)  # (S, 3)
        out.append(np.einsum("sk,tkc->tsc", w, tri).reshape(-1, 3))
    pts = np.concatenate(out)
    return pts[np.all((pts >= lo) & (pts <= hi), axis=1)]


def gap(points: np.ndarray, wheel: Wheel, up: float = 0.0, steer_deg: float = 0.0) -> float:
    """Signed distance (m) from the tyre to the nearest body point: the tyre a cylinder
    about the hub's x axis, moved up by `up` and turned by `steer_deg` about the
    vertical through the hub. Only the outboard half of the tyre counts (what shows
    from outside: arch lips, flares, wings; the wheel-well liner over the inner half
    is hidden by the tyre), body points inboard of the unsteered tyre's inner wall
    (under the car) and points within TYRE_CORE of the axle are ignored."""
    if not len(points):
        return math.inf
    d = points - (wheel.hub + [0.0, up, 0.0])
    a = math.radians(steer_deg)
    # into the unsteered wheel's frame (turned about the vertical through the hub), then
    # from the tyre's centre
    x = d[:, 0] * math.cos(a) - d[:, 2] * math.sin(a) - wheel.offset[0]
    z = d[:, 0] * math.sin(a) + d[:, 2] * math.cos(a) - wheel.offset[2]
    r = np.hypot(d[:, 1] - wheel.offset[1], z)
    out = x if wheel.hub[0] > 0 else -x  # along the axle from the tyre's centre plane, + outboard
    q1 = r - wheel.radius
    q2 = np.abs(out) - wheel.width / 2
    # Not inboard of the unsteered tyre's inner wall either: steered past about 35 deg,
    # the trailing inner corner swings over body points under the car (suspension, the
    # liner's back), outboard of the turned tyre's centre plane but hidden behind the
    # tyre (AUD_TTRS_10: -83 mm at 36 deg, 3 at 30; issue #54).
    beside = (d[:, 0] - wheel.offset[0]) * (1.0 if wheel.hub[0] > 0 else -1.0)
    keep = (r > TYRE_CORE * wheel.radius) & (out >= 0) & (beside >= -wheel.width / 2)
    if not keep.any():
        return math.inf
    q1, q2 = q1[keep], q2[keep]
    sdf = np.minimum(np.maximum(q1, q2), 0) + np.hypot(np.maximum(q1, 0), np.maximum(q2, 0))
    return float(sdf.min())


def forza_wheel(wheel: Wheel, entry: dict, front_width: float) -> tuple[np.ndarray, float, float] | None:
    """Forza's own tyre at Forza's static position for this wheel: (hub, radius, width).
    Forza draws its wheel at each axle's Data_Car tyre size, standing on its ground, the
    outer walls at the track outer. From a build-manifest entry's placement; manifests
    before the Data_Car fit (no `track_outer`) drew the wheel model's own size at the front,
    `front_width` (the drawn front wheels' width) tells it."""
    pl = entry.get("placement") or {}
    tyres = pl.get("tyres")
    if not tyres:
        return None
    (fd, fw), (rd, rw) = tyres["front"], tyres["rear"]
    # Forza's ground in the model: the front hubs (the host's height before settle_wheels)
    # less the radius convert() stood them on.
    ground = pl["host_hub_y"] - pl.get("ground_radius", pl["tyre_radius"])
    model_width = pl.get("tyre_width", front_width)
    hub = wheel.hub.copy()
    if wheel.front:
        track_outer = pl.get("track_outer", pl["track"] + front_width)
        hub[0] = math.copysign((track_outer - fw) / 2, wheel.hub[0])
        hub[1] = ground + fd / 2
        return hub, fd / 2, fw
    # convert(): rear_half_track = (rear track outer - model width x rear fit) / 2
    rear_fit = pl.get("rear_fit") or [1.0, 1.0]
    track_outer = 2 * pl["rear_half_track"] + model_width * rear_fit[0]
    hub[0] = math.copysign((track_outer - rw) / 2, wheel.hub[0])
    hub[1] = ground + rd / 2
    return hub, rd / 2, rw


@dataclass
class Row:
    media: str
    spool_id: int
    wheel: str
    radius: float
    width: float
    up_rest: float
    up_bump: float
    steer: float
    design: float
    rest: float
    bump: float
    lock: float
    forza: float | None
    forza_steer: float | None = None
    forza_lock: float | None = None

    def cells(self) -> list:
        def mm(v):
            return "" if v is None or not math.isfinite(v) else f"{v * 1000:.0f}"

        return [
            self.media,
            self.spool_id,
            self.wheel,
            f"{self.radius:.3f}",
            f"{self.width:.3f}",
            mm(self.up_rest),
            mm(self.up_bump),
            f"{self.steer:.0f}",
            mm(self.forza),
            mm(self.design),
            mm(self.rest),
            mm(self.bump),
            mm(self.lock),
            "" if self.forza_steer is None else f"{self.forza_steer:.0f}",
            mm(self.forza_lock),
        ]


HEADER = ["media", "id", "wheel", "radius", "width", "up_rest_mm", "up_bump_mm", "steer_deg"]
HEADER += ["gap_forza_mm", "gap_design_mm", "gap_rest_mm", "gap_bump_mm", "gap_lock_mm"]
HEADER += ["forza_steer_deg", "gap_forza_lock_mm"]


def measure(car: Car, media: str = "", entry: dict | None = None, forza_steer: float | None = None) -> list[Row]:
    """The car's wheels in every pose; entry: its build-manifest entry (the Forza
    reference), forza_steer: Forza's SteerMaxAngle (degrees) for the reference at lock."""
    fronts = [w.hub[2] for w in car.wheels if w.front]
    rears = [w.hub[2] for w in car.wheels if not w.front]
    rest = rest_offsets(car.params, sum(fronts) / len(fronts), sum(rears) / len(rears))
    steer = car.p(298)
    bump = car.p(188)
    front_width = next(w.width for w in car.wheels if w.front)
    rows = []
    for w in car.wheels:
        reach = math.hypot(w.radius, w.width / 2) + float(np.abs(w.offset).max()) + max(bump, 0.0) + 0.08
        pts = near_points(car.body, w.hub, reach)
        up = rest["front" if w.front else "rear"]
        lock = min(gap(pts, w, up, steer), gap(pts, w, up, -steer)) if w.front else gap(pts, w, up)
        ref = ref_lock = None
        fz = forza_wheel(w, entry, front_width) if entry else None
        if fz is not None:
            hub, radius, width = fz
            fw_ = Wheel(w.name, hub, w.front, radius, width, np.zeros(3))
            ref_pts = near_points(car.body, hub, math.hypot(radius, width / 2) + 0.08)
            ref = gap(ref_pts, fw_)
            if w.front and forza_steer:
                ref_lock = min(gap(ref_pts, fw_, 0.0, forza_steer), gap(ref_pts, fw_, 0.0, -forza_steer))
        rows.append(
            Row(
                media,
                car.spool_id,
                w.name,
                w.radius,
                w.width,
                up,
                bump,
                steer if w.front else 0.0,
                gap(pts, w),
                gap(pts, w, up),
                gap(pts, w, bump),
                lock,
                ref,
                forza_steer if w.front else None,
                ref_lock,
            )
        )
    return rows


# --- CLI ------------------------------------------------------------------------------


def _manifest(path: Path | None) -> dict[int, dict]:
    if path is None:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {c["spool_id"]: c for c in data.get("cars", [])}


def forza_steer_angles(folder: Path | None = None) -> dict[tuple[str, str], float]:
    """(game, media lower case) -> Data_Car.SteerMaxAngle from out/db/<game>_gamedb.slt
    (``dsff game_data prepare``)."""
    import sqlite3

    from dsfforza import REPO

    folder = Path(folder) if folder else REPO / "out" / "db"
    out = {}
    for game in ("fh1", "fm4"):
        path = folder / f"{game}_gamedb.slt"
        if not path.exists():
            continue
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            for media, angle in db.execute("SELECT MediaName, SteerMaxAngle FROM Data_Car"):
                if media and angle:
                    out[(game.upper(), media.lower())] = float(angle)
        finally:
            db.close()
    return out


def _steer(angles: dict, entry: dict | None) -> float | None:
    return angles.get((entry.get("game", ""), entry["media"].lower())) if entry else None


def _with_forza_lock(car: Car, angle: float | None) -> Car:
    """The car with p298 at Forza's lock (`--forza-lock`)."""
    if angle:
        car.params = {**car.params, 298: angle}
    return car


def cmd_car(args) -> None:
    manifest = _manifest(args.manifest)
    angles = forza_steer_angles()
    for sid in args.ids:
        car = load_car(args.archive, sid)
        entry = manifest.get(sid)
        if args.as_fixed and entry:
            car = as_fixed(car, entry)
        if args.forza_lock:
            car = _with_forza_lock(car, _steer(angles, entry))
        print(
            f"id {sid}: {len(car.body)} body triangles; VO3D "
            + ", ".join(f"p{k} {car.p(k):g}" for k in (0, 1, 3, 4, 25, 32, 188, 292, 298, 505))
        )
        print("\t".join(HEADER))
        for row in measure(car, entry["media"] if entry else "", entry, _steer(angles, entry)):
            print("\t".join(map(str, row.cells())))


def cmd_survey(args) -> None:
    from dsfforza.game.gui_assets import _root_entries

    manifest = _manifest(args.manifest)
    angles = forza_steer_angles()
    ids = args.ids or sorted(manifest)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with Path(args.archive).open("rb") as stream, out.open("w", encoding="utf-8", newline="\n") as f:
        entries = _root_entries(Path(args.archive), stream)
        f.write("\t".join(HEADER) + "\n")
        for n, sid in enumerate(ids):
            entry = manifest.get(sid)
            try:
                car = load_car(args.archive, sid, entries, stream)
                if args.as_fixed and entry:
                    car = as_fixed(car, entry)
                if args.forza_lock:
                    car = _with_forza_lock(car, _steer(angles, entry))
                rows = measure(car, entry["media"] if entry else str(sid), entry, _steer(angles, entry))
            except Exception as error:  # noqa: BLE001 - printed; one broken car must not stop a 573-car survey
                print(f"{sid}: {error}", file=sys.stderr)
                continue
            for row in rows:
                f.write("\t".join(map(str, row.cells())) + "\n")
            f.flush()
            if n % 25 == 0:
                print(f"{n + 1}/{len(ids)} {entry['media'] if entry else sid}", flush=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="dsff wheel_clearance", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("car", help="print one or more cars' wheels")
    a.add_argument("archive", type=Path)
    a.add_argument("ids", type=int, nargs="+")
    a.add_argument("--manifest", type=Path)
    fixed = "measure a build from before the wheel-clearance fix as the converter builds it now (as_fixed)"
    lock = "steer the lock pose to Forza's SteerMaxAngle, as the converter does since issue #54"
    a.add_argument("--as-fixed", action="store_true", help=fixed)
    a.add_argument("--forza-lock", action="store_true", help=lock)
    a.set_defaults(func=cmd_car)
    a = sub.add_parser("survey", help="every car of a build manifest into a TSV")
    a.add_argument("archive", type=Path)
    a.add_argument("--manifest", type=Path, required=True)
    a.add_argument("--ids", type=int, nargs="*")
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--as-fixed", action="store_true", help=fixed)
    a.add_argument("--forza-lock", action="store_true", help=lock)
    a.set_defaults(func=cmd_survey)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
