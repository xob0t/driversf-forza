"""Cockpit view for converted Forza cars: the interior entry (N+1000), the
steering wheel hinge, the gauges and the head camera.

What DSF needs (research/cockpit.md):

- The cockpit view draws the interior VEHC entry (kind 1, MODL mask 64)
  alone. It is self-contained: cabin, seats, dashboard, steering wheel,
  A-pillars and roof lining, glass, and a low-detail copy of the outer body
  (hood, rear deck) for what shows through the windows. The full entry has
  no mask-64 LODs.
- The steering wheel is not a separate part. Its vertices are skinned to the
  hierarchy: vertex semantic 9 = (record +0x34, record +0x2A, light, 1) of
  the steering-wheel record (the part on the hinge whose pivot is marker
  role 47; Desanne: record 11, hinge 0, s9 = (1, 1, 0, 1)). The hinge axis
  is the steering column.
- The needles are one stride-20 submodel in their own MODL part (the
  "Dials" material, alpha-tested); vertex semantic 8 component 2 is the
  gauge id, and UPVH's "seat" table is really the gauge table: pivot,
  dial-plane quaternion, u32 gauge id, u32 sweep. Each frame the engine
  turns needle `id` about the pivot, right-handed about the quaternion's
  z axis (the dial normal, into the dash: clockwise for the driver), by
  sweep degrees x input (FUN_008A2610 via g_InstrumentTransforms):
  id 1 tachometer, input clamp(rpm, p40, p42) / 5000; id 2 speedometer,
  input clamp(mph, p606, p605) / 80 (FUN_00891670). Ids 3..7 get no input.
  So the u32 is the needle's degrees per 5000 rpm or per 80 mph, the rest
  pose is 0 rpm / 0 mph, and VO3D p605 is the speedometer's last mark
  (vanilla DeLorean 88, Eldorado 120; default 255).
- The head camera (VO3D 430..432, FUN_0088DF80 case 0x14) is
  (p430 - marker47.x, p431, p432) in a frame turned 180 degrees about y,
  so in model space eye = (marker47.x - p430, p431, -p432).

Forza side: <car>_cockpit.carbin (LOD 0 only; the existing carbin parser
reads it), nodamage_LOD0 / lights_LOD0 / interior_LOD0 textures, and
gamedb CameraOverrides.CamCockpitOffset{X,Y,Z}: the eye relative to the
steering wheel centre, with +z forward (so eye = wheel + (x, y, -z) in
Forza space, whose front is -z). Zero rows mean "no override"; then the
median of the non-zero rows is used.

    uv run dsff cockpit fit out/fh1/NIS_370Z_10 NIS_370Z_10 --gamedb out/db/fh1_gamedb.slt
    uv run dsff cockpit render <archive> --id 304 --png out/cockpit.png [--eye-view out/eye.png]

As a library: `convert_interior(...)` (called from forza2dsf.convert with
ConvertOptions.cockpit) rebuilds the interior entry and patches the full
entry's UPVH and VO3D.
"""

from __future__ import annotations

import argparse
import itertools
import math
import re
import sqlite3
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza.convert import forza_materials as fm
from dsfforza.formats import carbin

# Median non-zero CamCockpitOffset over FH1 (112 cars) and FM4 (327 cars).
DEFAULT_EYE_OFFSET = (0.0, 0.21, -0.46)
INTERIOR_MASK = 64
STEERING_MARKER_ROLE = 0x2F
GAUGE_IDS = {"speed": 2, "tach": 1}  # Forza needle family -> DSF gauge id (FUN_00891670)
# Needle angle (degrees) = gauge-table u32 x input; the input is rpm x 0.0002
# for the tachometer and mph x 0.0125 for the speedometer (FUN_00891670), so
# the u32 is degrees per GAUGE_UNITS[family] (rpm or mph).
GAUGE_UNITS = {"tach": 5000.0, "speed": 80.0}
MPH_PER_MS = 2.23704  # DAT_00DE2EB4: the speedometer's m/s -> mph
SPEED_DIAL_PARAM, SPEED_FLOOR_PARAM = 605, 606  # VO3D: speedometer clamp (mph), default 255 / 0
# Without a CarAttribs tick table, the needle's sweep is taken to span these.
FALLBACK_DIAL_MAX = {"speed": 160.0, "tach": 8000.0}
# Needles with no DSF gauge stay static: half-way for level and temperature
# gauges, at rest for the others.
PARK_MID = re.compile(r"^(fuel|oil|water|temp|volt)", re.I)
# Forza digital readouts: a shader picks the digits. UV0 through the
# subsection's first transform lands on the label sheet the digits come from;
# through the second transform (the uv1 slot of uv_transform) it lands on
# the display's own face where there is one: the S2000's whole cluster (bar
# tachometer, scale, temperature and fuel marks), the LFA's central dial, the
# 370Z's LCD panels (research/cockpit.md, "Digital clusters"). Such faces are
# drawn textured (DISPLAY_FACE); the rest as a blank display.
DIGITAL_TECHNIQUES = {"digital_gauge"}
DISPLAY_COLOUR = (12, 14, 16, 255)
DISPLAY_FACE = "digital_gauge_face"  # renamed subsection: classify_cockpit -> textured, interior_LOD0
# A face mapping is a real picture of the display when it is undistorted
# (median ratio of the UV map's singular values) and at least as dense as a
# coarse cabin texture (UV units per metre; cabins run 3 to 9). Elsewhere the
# second transform is a swatch (under 0.15 per metre) or a stretched strip
# (anisotropy 17 to 320).
FACE_MAX_ANISOTROPY = 2.0
FACE_MIN_DENSITY = 1.0
# Cockpit subsections that would fall to the host's shared trim material
# (mirror glass, a render target in Forza; tiling detail textures such as the
# Mustang's wood rim and dash) sample DSF's trim texture with Forza UVs, which
# shows unrelated texture cells. They get one flat cell of the interior atlas.
FLAT_COCKPIT = [
    (re.compile(r"^dsf_dial_mark$"), (226, 226, 222, 255)),  # virtual dials (virtual_gauges)
    (re.compile(r"^dsf_dial_mark_dark$"), (22, 22, 22, 255)),  # ... on a light display face
    (re.compile(r"^dsf_dial_red$"), (214, 32, 18, 255)),
    (re.compile(r"^dsf_dial_display$"), DISPLAY_COLOUR),  # a virtual display's screen (pod_display)
    (re.compile(r"^dsf_dial_bezel$"), (30, 30, 32, 255)),  # ... and its housing
    (re.compile(r"^mirror", re.I), (46, 50, 56, 255)),
    (re.compile(r"wood", re.I), (84, 48, 26, 255)),
    (re.compile(r"chrome|metal", re.I), (150, 150, 155, 255)),
]
FLAT_DEFAULT = (34, 34, 36, 255)
VIRTUAL_ONLY_FLAT = {r"^dsf_dial_mark_dark$", r"^dsf_dial_display$", r"^dsf_dial_bezel$"}
# The engine's interior paint material: used by every vanilla interior that has
# painted parts (227, 147, 229, 193, 251, 266 checked; out/cockpit2/host_shared.py).
INTERIOR_PAINT = bytes.fromhex("10122b9653b14e2a")
# Detail techniques whose UVs run along thin strips or noise tiles of the
# atlas (stitching lines, mottled plastic): at the cockpit atlas's scale they
# smear into neighbouring cells (white blocks on the Raptor's wheel, a
# speckled band under the M5's cluster). Drawn flat.
FORCE_FLAT = re.compile(
    r"^(mottled|bump_stitching|stitching|dsf_dial_mark|dsf_dial_mark_dark|dsf_dial_red|dsf_dial_display|dsf_dial_bezel)$",
    re.I,
)
DASHBOARD_INSTRUMENT = 0x75FAD367  # VDCL name hash of the needle buffers' semantic 8 (u, v, gauge id, 0)


def flat_colour(technique: str) -> tuple[int, int, int, int]:
    return next((c for pattern, c in FLAT_COCKPIT if pattern.search(technique)), FLAT_DEFAULT)


# Cockpit sections that are not drawn in DSF's cockpit view by default.
SKIP_SECTIONS = {"cagerace"}
LIGHT_SECTIONS = re.compile(r"(head|tail)light", re.I)
NEEDLE = re.compile(r"^(?P<family>[a-z]+)_(?P<pose>min|mid|max)$", re.I)
# Main-carbin sections never copied into the interior shell.
SHELL_SKIP = re.compile(r"^(wheel|undercarriage|cagerace|blur|.*_plate_(front|rear)$)", re.I)  # plates.py


# --- small vector helpers -----------------------------------------------------------


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _scale(a, k):
    return tuple(x * k for x in a)


def _norm(a):
    length = math.sqrt(_dot(a, a)) or 1.0
    return _scale(a, 1 / length)


def _mean(points):
    n = len(points)
    return tuple(sum(p[i] for p in points) / n for i in range(3))


def _eigen_sym3(m):
    """Eigenvalues/vectors of a symmetric 3x3 (Jacobi), ascending."""
    a = [row[:] for row in m]
    v = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    for _ in range(60):
        p, q = max(((i, j) for i in range(3) for j in range(i + 1, 3)), key=lambda t: abs(a[t[0]][t[1]]))
        if abs(a[p][q]) < 1e-14:
            break
        th = 0.5 * math.atan2(2 * a[p][q], a[q][q] - a[p][p])
        c, s = math.cos(th), math.sin(th)
        for k in range(3):
            akp, akq = a[k][p], a[k][q]
            a[k][p], a[k][q] = c * akp - s * akq, s * akp + c * akq
        for k in range(3):
            apk, aqk = a[p][k], a[q][k]
            a[p][k], a[q][k] = c * apk - s * aqk, s * apk + c * aqk
        for k in range(3):
            vkp, vkq = v[k][p], v[k][q]
            v[k][p], v[k][q] = c * vkp - s * vkq, s * vkp + c * vkq
    return sorted((a[i][i], tuple(v[k][i] for k in range(3))) for i in range(3))


def _covariance(vectors, centre=(0.0, 0.0, 0.0)):
    n = len(vectors)
    return [[sum((p[i] - centre[i]) * (p[j] - centre[j]) for p in vectors) / n for j in range(3)] for i in range(3)]


def quat_from_to(a, b) -> tuple[float, float, float, float]:
    """Shortest-arc unit quaternion (x, y, z, w) rotating unit vector a onto b."""
    a, b = _norm(a), _norm(b)
    c = _dot(a, b)
    if c < -0.999999:
        axis = _norm(_cross(a, (1, 0, 0)) if abs(a[0]) < 0.9 else _cross(a, (0, 1, 0)))
        return (*axis, 0.0)
    x, y, z = _cross(a, b)
    w = 1 + c
    n = math.sqrt(x * x + y * y + z * z + w * w)
    return (x / n, y / n, z / n, w / n)


def quat_rotate(q, v):
    x, y, z, w = q
    u = (x, y, z)
    t = _scale(_cross(u, v), 2)
    return _add(_add(v, _scale(t, w)), _cross(u, t))


# --- Forza cockpit geometry -----------------------------------------------------------


def load_cockpit(folder: Path, name: str) -> list[carbin.Section] | None:
    from dsfforza.convert import forza2dsf

    path = forza2dsf.find_file(folder, f"{name}_cockpit.carbin")
    if path is None:
        return None
    result = carbin.parse_full(path.read_bytes())
    if not result.complete:
        raise ValueError(f"{path.name}: only {len(result.sections)}/{result.declared} sections parsed")
    return result.sections


def section_points(section: carbin.Section, materials: set[str] | None = None) -> list[tuple]:
    pts = []
    for sub in section.subsections:
        if materials is not None and sub.material not in materials:
            continue
        pool = section.vertices_for(sub)
        for tri in sub.triangles():
            pts.extend(pool[i].position for i in tri)
    return pts


@dataclass
class Wheel:
    centre: tuple  # Forza space
    axis: tuple  # unit, Forza space, pointing from the wheel towards the driver
    radius: float


def fit_wheel(section: carbin.Section) -> Wheel:
    """Steering wheel centre and column axis. The axis is the direction of
    least spread (PCA). The rim is every point at 75% of the largest radius
    from the centroid or more; the centre is the middle of the rim's extent
    in the wheel plane (a vertex mean is biased by uneven tessellation), at
    the rim's mean depth along the axis. The axis is then refitted to the rim
    alone (fit_wheel_points)."""
    return fit_wheel_points(list(set(section_points(section))))


# Some Forza wheels are modelled with their column (the FM4 FG Falcon racers: a
# 1 m shaft to the bulkhead) or a deep quick-release spacer (FM4 TT-R): the
# column pulls the least-spread direction off the wheel's own (the Falcons'
# came out 74 deg up, radius 6 cm) and the vanilla hand grips missed by 9 cm.
COLUMN_DEPTH = 0.30  # m along Forza z: a deeper section carries its column
WHEEL_DEPTH = 0.22  # m kept at the driver's end then (a 0.2 m rim at 30 deg spans 0.13)
RIM_REFITS = 3


def _fit_rim(pts: list[tuple], axis, about) -> tuple[tuple, tuple, float, list]:
    """(centre, axis, radius, rim points) for a given axis: the rim is every
    point at 75% of the largest radius about `about` or more."""
    u = _norm(_cross(axis, (1, 0, 0)) if abs(axis[0]) < 0.9 else _cross(axis, (0, 1, 0)))
    v = _cross(axis, u)

    def radius(p, c):
        d = _sub(p, c)
        along = _dot(d, axis)
        return math.sqrt(max(0.0, _dot(d, d) - along * along))

    r_max = max(radius(p, about) for p in pts)
    rim = [p for p in pts if radius(p, about) >= 0.75 * r_max]
    us, vs = [_dot(p, u) for p in rim], [_dot(p, v) for p in rim]
    cu, cv = (min(us) + max(us)) / 2, (min(vs) + max(vs)) / 2
    depth = sum(_dot(p, axis) for p in rim) / len(rim)
    centre = _add(_add(_scale(u, cu), _scale(v, cv)), _scale(axis, depth))
    return centre, axis, sum(radius(p, centre) for p in rim) / len(rim), rim


def _wheel(centre, axis, r) -> Wheel:
    if axis[2] < 0:  # Forza: the driver sits behind the wheel, at larger z
        axis = _scale(axis, -1)
    return Wheel(centre, axis, r)


def _wheel_candidates(pts: list[tuple]):
    """(name, Wheel, rim points) fits of one wheel: the least-spread axis of
    all points ("pca"); then the rim's own plane (its points' least-spread
    direction), the rim picked again about the new centre, RIM_REFITS times
    ("rim"); and both again on the WHEEL_DEPTH at the driver's end (+z) when
    the points run deeper than COLUMN_DEPTH (a modelled column)."""
    sets = [("", pts)]
    z_max = max(p[2] for p in pts)
    if z_max - min(p[2] for p in pts) > COLUMN_DEPTH:
        end = [p for p in pts if p[2] >= z_max - WHEEL_DEPTH]
        if len(end) >= 12:
            sets.append(("driver end ", end))
    for prefix, ps in sets:
        centroid = _mean(ps)
        axis = _norm(_eigen_sym3(_covariance(ps, centroid))[0][1])
        centre, axis, r, rim = _fit_rim(ps, axis, centroid)
        yield prefix + "pca", _wheel(centre, axis, r), rim
        for _ in range(RIM_REFITS):
            if len(rim) < 3:
                break
            plane = _norm(_eigen_sym3(_covariance(rim, _mean(rim)))[0][1])
            if _dot(plane, axis) < 0:
                plane = _scale(plane, -1)
            centre, axis, r, rim = _fit_rim(ps, plane, centre)
        yield prefix + "rim", _wheel(centre, axis, r), rim


def rim_scatter(w: Wheel, rim: list[tuple]) -> float:
    """How far `rim` is from a circle: RMS of its radial misfit plus RMS of its
    spread along the axis (m)."""
    radial, along = [], []
    for p in rim:
        d = _sub(p, w.centre)
        a = _dot(d, w.axis)
        radial.append(math.sqrt(max(0.0, _dot(d, d) - a * a)) - w.radius)
        along.append(a)
    mean = sum(along) / len(along)
    return math.sqrt(sum(x * x for x in radial) / len(radial)) + math.sqrt(
        sum((x - mean) ** 2 for x in along) / len(along)
    )


def fit_wheel_points(pts: list[tuple]) -> Wheel:
    """fit_wheel on a list of points (Forza space): of _wheel_candidates, the
    one whose rim is the best circle (rim_scatter). A hub spacer or a column
    modelled with the wheel no longer tilts it."""
    return min(((rim_scatter(w, rim), n, w) for n, (_, w, rim) in enumerate(_wheel_candidates(pts))))[2]


MAIN_WHEEL_RADIUS = (0.08, 0.30)  # a plausible main-carbin wheel fit (FH1 Mustang Boss 429's: 0.013)
MAIN_WHEEL_MAX_MOVE = 0.5


def fit_main_wheel(section: carbin.Section, near: Wheel) -> Wheel | None:
    """fit_wheel on a main-carbin steering wheel at its finest LOD. A column or boss
    modelled with it spoils the fit (FH1 Mustang Boss 429: radius 0.013 m); then only
    the points around the cockpit's wheel (`near`) are fitted."""
    promoted = promote_lod(section)
    if promoted is None:
        return None
    pts = list(set(section_points(promoted)))
    main = fit_wheel_points(pts)
    if not MAIN_WHEEL_RADIUS[0] <= main.radius <= MAIN_WHEEL_RADIUS[1]:
        close = [p for p in pts if math.dist(p, near.centre) < 1.3 * near.radius + 0.05]
        if len(close) >= 12:
            main = fit_wheel_points(close)
    return main


def align_main_wheel(section: carbin.Section, target: Wheel) -> tuple[carbin.Section | None, dict]:
    """The main carbin's steering wheel (drawn in the full and exterior entries, so in
    the chase views) moved, turned and scaled onto the cockpit's (`target`), where
    the hand rig grips (research/cockpit.md, "The driver in the chase views"). Forza
    models the two apart: the S5S Raptor's main-carbin wheel sits 15 cm ahead of its
    cockpit wheel, the 1993 Skyline's 17 cm. Returns (None, report) when the main
    wheel can't be fitted."""
    import copy
    import dataclasses

    main = fit_main_wheel(section, target)
    if main is None:
        return None, {"kept": "no LOD"}
    report = {
        "main_centre": [round(c, 4) for c in main.centre],
        "main_radius": round(main.radius, 4),
        "move": round(math.dist(main.centre, target.centre), 4),
        "turn_deg": round(math.degrees(math.acos(max(-1.0, min(1.0, _dot(main.axis, target.axis))))), 2),
        "scale": round(target.radius / main.radius, 4) if main.radius else None,
    }
    if not MAIN_WHEEL_RADIUS[0] <= main.radius <= MAIN_WHEEL_RADIUS[1] or report["move"] > MAIN_WHEEL_MAX_MOVE:
        report["kept"] = "implausible main-carbin wheel fit"
        return None, report
    q = quat_from_to(main.axis, target.axis)
    k = target.radius / main.radius

    def move(v):
        p = quat_rotate(q, _scale(_sub(v.position, main.centre), k))
        return dataclasses.replace(v, position=_add(target.centre, p), normal=quat_rotate(q, v.normal))

    out = copy.copy(section)
    out.lod_vertices = [move(v) for v in section.lod_vertices]
    out.lod0_vertices = [move(v) for v in section.lod0_vertices]
    return out, report


@dataclass
class Gauge:
    family: str  # "speed", "tach", ...
    pivot: tuple  # Forza space
    normal: tuple  # unit, Forza space, pointing into the dash (forward, -z)
    sweep_deg: float  # min -> max pose, positive = clockwise seen by the driver
    rest: carbin.Section  # the pose drawn (min)


def _plane_rotation(xs, ys, normal):
    """(angle, pivot) of the 2D rotation taking points xs to ys in the plane
    perpendicular to `normal` (angle counter-clockwise about +normal)."""
    u = _norm(_cross(normal, (0, 1, 0)) if abs(normal[1]) < 0.9 else _cross(normal, (1, 0, 0)))
    v = _cross(normal, u)
    a = [(_dot(p, u), _dot(p, v)) for p in xs]
    b = [(_dot(p, u), _dot(p, v)) for p in ys]
    ca = (sum(p[0] for p in a) / len(a), sum(p[1] for p in a) / len(a))
    cb = (sum(p[0] for p in b) / len(b), sum(p[1] for p in b) / len(b))
    cross = sum((p[0] - ca[0]) * (q[1] - cb[1]) - (p[1] - ca[1]) * (q[0] - cb[0]) for p, q in zip(a, b))
    dot = sum((p[0] - ca[0]) * (q[0] - cb[0]) + (p[1] - ca[1]) * (q[1] - cb[1]) for p, q in zip(a, b))
    theta = math.atan2(cross, dot)
    if abs(theta) < 1e-3:
        return theta, None
    c, s = math.cos(theta), math.sin(theta)
    # pivot: (I - R) p = cb - R ca
    bx, by = cb[0] - (c * ca[0] - s * ca[1]), cb[1] - (s * ca[0] + c * ca[1])
    m00, m01, m10, m11 = 1 - c, s, -s, 1 - c
    det = m00 * m11 - m01 * m10
    px, py = (bx * m11 - m01 * by) / det, (m00 * by - m10 * bx) / det
    depth = sum(_dot(p, normal) for p in xs) / len(xs)
    return theta, _add(_add(_scale(u, px), _scale(v, py)), _scale(normal, depth))


def fit_gauge(family: str, poses: dict[str, carbin.Section]) -> Gauge | None:
    """Pivot and dial normal from the poses of one needle (min, mid, max: the
    same mesh rotated about the pivot). Displacements are perpendicular to
    the dial normal; in that plane each step is a 2D rotation about the
    pivot. The sweep adds min -> mid -> max, each the short way round."""
    order = [poses[k] for k in ("min", "mid", "max") if k in poses]
    if len(order) < 2 or "min" not in poses:
        return None
    if not order[0].lod0_vertices:
        return None

    def matched(a: carbin.Section, b: carbin.Section):
        """Corresponding points of two poses, or None."""
        if len(a.lod0_vertices) == len(b.lod0_vertices):
            return [v.position for v in a.lod0_vertices], [v.position for v in b.lod0_vertices]
        # Poses welded differently (FM4 CTS-V: 136, 135 and 137 vertices) but with
        # the same triangles: match triangle corners instead of pool entries.
        pa, pb = section_points(a), section_points(b)
        return (pa, pb) if pa and len(pa) == len(pb) else None

    steps = [(a, b, matched(a, b)) for a, b in itertools.pairwise(order)]
    disp = [_sub(y, x) for _, _, m in steps if m for x, y in zip(*m)]
    if not disp:
        return None
    normal = _norm(_eigen_sym3(_covariance(disp))[0][1])
    if normal[2] > 0:
        normal = _scale(normal, -1)  # into the dash: forward in Forza is -z
    thetas, pivots = [], []
    for _, _, m in steps:
        theta, pivot = _plane_rotation(*m, normal) if m else (None, None)
        thetas.append(theta)
        if pivot is not None:
            pivots.append(pivot)
    if not pivots:
        return None
    pivot = _mean(pivots)
    # A pose with extra triangles (FM4 Mini Cooper S 2003, Lancer Evo IX MR, Insignia
    # VXR: the min pose) has no point-to-point match: its turn is the one that lays
    # its points onto the next pose's, about the pivot the matched poses give.
    for k, (a, b, m) in enumerate(steps):
        if m is None:
            thetas[k] = _turn_onto(section_points(a), section_points(b), pivot, normal)
    # A viewer looking along +normal (the driver) sees positive angles clockwise.
    return Gauge(family, pivot, normal, math.degrees(sum(thetas)), poses["min"])


def _turn_onto(a: list[tuple], b: list[tuple], pivot, normal) -> float:
    """Angle (radians, right-handed about `normal` through `pivot`) that best
    lays points `a` onto points `b`: the mean distance from each turned point
    to its nearest in `b`, searched over the whole circle, then refined."""
    import numpy as np

    pa = np.unique(np.round(np.array(a, dtype=np.float64), 6), axis=0) - np.array(pivot)
    pb = np.unique(np.round(np.array(b, dtype=np.float64), 6), axis=0) - np.array(pivot)
    n = np.array(_norm(normal))

    def cost(theta: float) -> float:
        c, s = math.cos(theta), math.sin(theta)
        turned = pa * c + np.cross(n, pa) * s + np.outer(pa @ n, n) * (1 - c)
        d = ((turned[:, None, :] - pb[None, :, :]) ** 2).sum(axis=2)
        return float(np.sqrt(d.min(axis=1)).mean())

    best = min((cost(math.radians(deg)), math.radians(deg)) for deg in range(-180, 180))[1]
    step = math.radians(0.5)
    while step > math.radians(0.01):
        best = min((cost(best + k * step), best + k * step) for k in (-1, 0, 1))[1]
        step /= 2
    return best


def gauge_ticks(folder: Path | None) -> dict[str, list[tuple[float, float]]]:
    """CarAttribs.xml <Gauges>: family (lower case: speed, tach, fuel, ...) ->
    [(param, value)]. Param 0..1 runs from the needle's min to its max pose;
    speed values are m/s, tach values rpm."""
    import xml.etree.ElementTree as ET

    if folder is None or not Path(folder).is_dir():
        return {}
    path = next((p for p in Path(folder).iterdir() if p.name.lower() == "carattribs.xml"), None)
    if path is None:
        return {}
    try:
        root = ET.fromstring(path.read_bytes())
    except ET.ParseError:
        return {}
    out = {}
    for gauge in root.iter("Gauges"):
        for family in gauge:
            ticks = [
                (float(t.get("Param")), float(t.get("Value")))
                for t in family.iter("TickMark")
                if t.get("Param") is not None and t.get("Value") is not None
            ]
            if ticks:
                out[family.tag.lower()] = ticks
    return out


@dataclass
class GaugeCalibration:
    """What the DSF gauge table needs for one Forza needle."""

    family: str
    gauge_id: int
    sweep: int  # u32: needle degrees per GAUGE_UNITS[family] (rpm or mph)
    reverse: bool  # needle turns counter-clockwise: the dial normal is flipped
    rest_deg: float  # turn of the min pose (about the Forza normal) to put it on 0
    dial_max: float  # last mark (mph or rpm)
    residual_deg: float  # worst tick off the linear fit
    source: str

    @property
    def deg_per_unit(self) -> float:
        return self.sweep / GAUGE_UNITS[self.family]


def calibrate_gauge(g: Gauge, ticks: list[tuple[float, float]] | None) -> GaugeCalibration:
    """Fit angle = a + b x value over the tick table (angle = param x the
    min -> max sweep, as Forza's needle runs linearly between its poses;
    values in mph or rpm). DSF can only draw a + b x value with a = 0, so
    `rest_deg` = a turns the rest pose onto 0 and `sweep` = |b| x unit; a
    negative b (counter-clockwise needle) flips the dial normal instead."""
    unit = GAUGE_UNITS[g.family]
    scale = MPH_PER_MS if g.family == "speed" else 1.0
    pts = [(v * scale, p * g.sweep_deg) for p, v in (ticks or [])]
    xs = {round(x, 6) for x, _ in pts}
    if len(xs) >= 2:
        n = len(pts)
        mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
        b = sum((x - mx) * (y - my) for x, y in pts) / sum((x - mx) ** 2 for x, _ in pts)
        a = my - b * mx
        residual = max(abs(a + b * x - y) for x, y in pts)
        dial_max = max(x for x, _ in pts)
        source = f"CarAttribs {len(pts)} ticks"
    else:
        dial_max = FALLBACK_DIAL_MAX[g.family]
        a, b, residual = 0.0, g.sweep_deg / dial_max, 0.0
        source = f"no tick table: sweep taken as 0..{dial_max:g}"
    return GaugeCalibration(
        g.family,
        GAUGE_IDS[g.family],
        max(1, round(abs(b) * unit)),
        b < 0,
        a,
        round(dial_max, 1),
        round(residual, 2),
        source,
    )


def rotate_about(p, pivot, axis, deg: float):
    """Rotate point p right-handed about the line (pivot, unit axis)."""
    th = math.radians(deg)
    c, s = math.cos(th), math.sin(th)
    v = _sub(p, pivot)
    r = _add(_add(_scale(v, c), _scale(_cross(axis, v), s)), _scale(axis, _dot(axis, v) * (1 - c)))
    return _add(pivot, r)


def cockpit_parts(sections: list[carbin.Section]) -> tuple[dict, dict[str, dict[str, carbin.Section]]]:
    """(name -> section of the static cockpit, needle family -> {pose: section})."""
    static, needles = {}, {}
    for s in sections:
        m = NEEDLE.match(s.name)
        if m:
            needles.setdefault(m.group("family").lower(), {})[m.group("pose").lower()] = s
        else:
            static[s.name] = s
    return static, needles


def eye_offset(name: str, gamedbs: list[Path]) -> tuple[tuple[float, float, float], str]:
    """CamCockpitOffset (x, y, z) from the first gamedb that has the car."""
    for db in gamedbs:
        if not db or not Path(db).exists():
            continue
        con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT o.CamCockpitOffsetX, o.CamCockpitOffsetY, o.CamCockpitOffsetZ "
                "FROM CameraOverrides o JOIN Data_Car c ON c.Id = o.CarId "
                "WHERE lower(c.MediaName) = lower(?)",
                (name,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            con.close()
        if row is not None:
            if any(abs(x) > 1e-6 for x in row):
                return tuple(row), f"{Path(db).name} CameraOverrides"
            return DEFAULT_EYE_OFFSET, f"{Path(db).name} CameraOverrides is zero: fleet median"
    return DEFAULT_EYE_OFFSET, "no CameraOverrides row: fleet median"


def eye_point(wheel: Wheel, offset) -> tuple[float, float, float]:
    """Forza space: wheel centre + (x, y, -z); CamCockpitOffset's +z is forward."""
    return (wheel.centre[0] + offset[0], wheel.centre[1] + offset[1], wheel.centre[2] - offset[2])


@dataclass
class CockpitFit:
    wheel: Wheel
    gauges: dict[str, Gauge]
    eye: tuple
    offset: tuple
    offset_source: str
    mirrors: dict[str, tuple]  # "left"/"right" (driver's view, Forza -x = left) -> glass centre
    static: dict
    needles: dict
    calibration: dict = field(default_factory=dict)  # family -> GaugeCalibration (speed, tach)
    digital: int = 0  # digital_gauge triangles (drawn as a blank display)
    source: str = "cockpit"  # "cockpit", or why the main carbin's cabin stands in
    wheel_source: str = "cockpit"
    lod0_atlases: bool = True  # cockpit carbins sample the _LOD0 atlases, the main carbin doesn't
    shell_skip: set = field(default_factory=set)  # main-carbin sections kept out of the shell too
    camera: tuple | None = None  # final head camera (Forza space), set by forza2dsf after camera_eye
    display_faces: int = 0  # digital_gauge triangles drawn textured (display_face)
    virtual: dict = field(default_factory=dict)  # virtual_gauges report: dials drawn on a blank display
    digital_plan: object = None  # digital_dash.DigitalPlan: live digital displays (DSF's digital dials)
    digital_displays: dict = field(default_factory=dict)  # digital_displays' report


# Main-carbin sections that make a cockpit when the cockpit carbin is a stub.
MAIN_CABIN = ("interior", "seatL", "seatR", "steering_wheel")
STUB_EXTENT = 0.5  # m: a real cockpit spans the cabin; stubs are a 0.2 m box per section
STUB_TRIANGLES = 1000  # real cockpits have tens of thousands (370Z 62,522)


def promote_lod(section: carbin.Section, lod: int | None = None) -> carbin.Section | None:
    """A copy of a main-carbin section whose finest LOD (or `lod`) poses as
    LOD 0, so the cockpit code (LOD-0 only) can use it."""
    import copy

    lods = sorted({sub.lod for sub in section.subsections if sub.lod > 0})
    if not lods:
        return None
    lod = lods[0] if lod is None else lod
    out = copy.copy(section)
    out.lod0_vertices = section.lod_vertices
    subs = []
    for sub in section.subsections:
        if sub.lod == lod:
            sub2 = copy.copy(sub)
            sub2.lod = 0
            if section.name.lower() == "steering_wheel" and sub2.material.lower().startswith("blur"):
                # Some main-carbin wheels use a "blur" technique (FH1 Taurus SHO:
                # blur_lip), which the converter drops as wheel motion blur.
                sub2.material = "black"
            subs.append(sub2)
    out.subsections = subs
    return out


def uv_map_stats(section: carbin.Section, sub: carbin.SubSection, transform) -> tuple[float, float]:
    """(anisotropy, density) of UV0 through `transform` (x_off, x_scale,
    y_off, y_scale) over the subsection's triangles, medians: the ratio of
    the singular values of the surface-to-UV map (1 = undistorted) and the
    square root of its determinant (UV units per metre). (inf, 0) when no
    triangle has area in both."""
    xo, xs, yo, ys = transform
    pool = section.vertices_for(sub)
    aniso, dens = [], []
    for tri in sub.triangles():
        p = [pool[i].position for i in tri]
        uv = [(pool[i].uv0[0] * xs + xo, pool[i].uv0[1] * ys + yo) for i in tri]
        e1, e2 = _sub(p[1], p[0]), _sub(p[2], p[0])
        l1 = math.sqrt(_dot(e1, e1))
        if l1 < 1e-9:
            continue
        x2 = _dot(e2, e1) / l1
        y2 = math.sqrt(max(0.0, _dot(e2, e2) - x2 * x2))
        if y2 < 1e-9:
            continue
        # Surface frame (0, 0), (l1, 0), (x2, y2) -> UV: J = [[a, b], [c, d]].
        du1, dv1, du2, dv2 = uv[1][0] - uv[0][0], uv[1][1] - uv[0][1], uv[2][0] - uv[0][0], uv[2][1] - uv[0][1]
        a, c = du1 / l1, dv1 / l1
        b, d = (du2 - a * x2) / y2, (dv2 - c * x2) / y2
        s, det = a * a + b * b + c * c + d * d, abs(a * d - b * c)
        r = math.sqrt(max(0.0, s * s / 4 - det * det))
        big, small = math.sqrt(s / 2 + r), math.sqrt(max(0.0, s / 2 - r))
        aniso.append(big / small if small > 1e-12 else math.inf)
        dens.append(math.sqrt(det))
    if not aniso:
        return math.inf, 0.0
    aniso.sort()
    dens.sort()
    return aniso[len(aniso) // 2], dens[len(dens) // 2]


def display_face(section: carbin.Section, sub: carbin.SubSection) -> carbin.SubSection | None:
    """A digital_gauge subsection whose second UV transform maps UV0 onto a
    picture of the display (undistorted and dense enough, see
    FACE_MAX_ANISOTROPY), as a textured copy (DISPLAY_FACE, that transform
    first); None for blank displays."""
    import copy

    if sub.material.lower() not in DIGITAL_TECHNIQUES or len(sub.uv_transform) < 8:
        return None
    face = tuple(sub.uv_transform[4:8])
    aniso, density = uv_map_stats(section, sub, face)
    if aniso > FACE_MAX_ANISOTROPY or density < FACE_MIN_DENSITY:
        return None
    out = copy.copy(sub)
    out.material = DISPLAY_FACE
    out.uv_transform = face + tuple(sub.uv_transform[:4]) + tuple(sub.uv_transform[8:])
    return out


def with_display_faces(section: carbin.Section) -> tuple[carbin.Section, int]:
    """The section with its textured display faces (display_face) swapped in,
    and how many triangles they hold."""
    import copy

    subs, faces = [], 0
    for sub in section.subsections:
        face = display_face(section, sub) if sub.lod == 0 else None
        if face is not None:
            faces += len(face.triangles())
        subs.append(face or sub)
    if not faces:
        return section, 0
    out = copy.copy(section)
    out.subsections = subs
    return out, faces


# --- virtual dials on blank digital displays -------------------------------------------
# Where Forza's speedometer or tachometer is a digital readout (a digital_gauge display
# its shader fills at run time: the CL65's centre TFT, race-car LCDs), DSF has nothing
# to draw there and no way to draw changing digits: its gauges are needles turned by
# the engine (research/cockpit.md, "Gauges"). On a blank display in the dashboard the
# missing gauge becomes a dial: tick marks, seven-segment numerals and a red zone as
# flat-colour geometry, and a needle driven by the host's gauge entry like any Forza
# needle (research/cockpit.md, "Virtual dials"). Where no blank display can hold one, a
# display with its own face (a static picture of the LCD, display_face) takes it, drawn
# flat in the face's colour under the dial; where neither can (the readout is on the
# steering wheel, or only small strips), a virtual display on the dashboard does
# (pod_display; research/cockpit.md, "Virtual dials on display faces and virtual displays").
VIRTUAL_MIN_AREA = 0.0025  # m^2: the display's rectangle (25 cm^2; race LCDs are 13-16 x 5-9 cm)
VIRTUAL_MAX_TILT_DEG = 60.0  # display normal against the direction to the eye
VIRTUAL_MIN_SIGHT = 0.5  # share of the display the eye sees past the wheel and the cowl
VIRTUAL_INSET = 0.08  # of each cell side, kept clear around a dial
VIRTUAL_MARK_LIFT = 0.0012  # m towards the driver: dial marks over the display
VIRTUAL_NEEDLE_LIFT = 0.0025  # m: the needle over the marks
VIRTUAL_HUB_LIFT = 0.0035
VIRTUAL_NEEDLE_COLOURS = {
    "dsf_needle": (252, 45, 8, 255),  # Forza's needle swatch (interior_LOD0)
    "dsf_needle_hub": (40, 40, 42, 255),
}
VIRTUAL_SPEED_MIN = 120.0  # mph: smallest virtual speedometer scale
VIRTUAL_TACH_MIN = 6000.0  # rpm
VIRTUAL_SOURCE = "virtual dial"  # GaugeCalibration.source of a virtual dial
VIRTUAL_DARK_MARKS = 100.0  # display luminance (0..255) above which the marks are dark
FACE_FRONT_DEPTH = 0.02  # m: digital patches this near a face's plane may stand in front of it
_SEGMENTS = {  # seven-segment digits: a top, b upper right, c lower right, d bottom, e lower left, f upper left, g middle
    "0": "abcdef",
    "1": "bc",
    "2": "abged",
    "3": "abgcd",
    "4": "fgbc",
    "5": "afgcd",
    "6": "afgedc",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
}


@dataclass
class Display:
    """A blank digital_gauge display: its plane and rectangle, Forza space."""

    section: str
    centre: tuple
    normal: tuple  # unit, into the dash (the driver looks along it)
    up: tuple  # unit, in the plane
    right: tuple  # unit, in the plane: the driver's right
    rect: tuple  # (u0, u1, v0, v1): extent along right and up from the centre
    subsection: int | None = None  # index in its section's subsections
    colour: tuple = DISPLAY_COLOUR  # what the dial is drawn on: a face's median texel
    kind: str = "display"  # "display" (blank), "face" (display_face) or "virtual" (pod_display)

    @property
    def size(self) -> tuple[float, float]:
        return self.rect[1] - self.rect[0], self.rect[3] - self.rect[2]

    def point(self, u: float, v: float, lift: float = 0.0) -> tuple:
        """The display point at (u, v) of its plane, `lift` towards the driver."""
        return _add(_add(_add(self.centre, _scale(self.right, u)), _scale(self.up, v)), _scale(self.normal, -lift))


def _touching(tris: list[list[tuple]], cell: float = 0.01) -> list[list[tuple]]:
    """Points of each group of triangles that touch: union-find over triangles
    with a vertex in the same `cell` grid cell."""
    return [[p for i in group for p in tris[i]] for group in _touching_groups(tris, cell)]


def _touching_groups(tris: list[list[tuple]], cell: float = 0.01) -> list[list[int]]:
    """Indices of each group of triangles that touch (_touching)."""
    parent = list(range(len(tris)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    first: dict[tuple, int] = {}
    for i, t in enumerate(tris):
        for p in t:
            k = tuple(round(c / cell) for c in p)
            if k in first:
                parent[find(i)] = find(first[k])
            else:
                first[k] = i
    groups: dict[int, list] = {}
    for i in range(len(tris)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def display_patches(static: dict, eye, on_wheel: bool = False) -> list:
    """The digital displays a dash scene is drawn on (digital_dash.DisplayPatch): every
    LOD-0 digital_gauge subsection (or its textured face, display_face) in the cabin,
    not on the steering wheel (the digital dials aren't skinned to it). With
    `on_wheel`, the steering wheel's instead (wheel_display_pod moves them)."""
    from dsfforza.convert import digital_dash

    out = []
    for name, s in static.items():
        if fm.COCKPIT_EXTERIOR_SECTIONS.match(name) or (name.lower() == "steering_wheel") != on_wheel:
            continue
        for ui, sub in enumerate(s.subsections):
            m = sub.material.lower()
            if sub.lod or not (m in DIGITAL_TECHNIQUES or sub.material == DISPLAY_FACE):
                continue
            # display_face swapped the transforms: the face first, the scene's window second.
            t = tuple(sub.uv_transform[4:8] if sub.material == DISPLAY_FACE else sub.uv_transform[:4])
            pool = s.vertices_for(sub)
            tris = sub.triangles()
            if not tris:
                continue
            pts = [[pool[i].position for i in t3] for t3 in tris]
            uvs = [[pool[i].uv0 for i in t3] for t3 in tris]
            n = (0.0, 0.0, 0.0)
            for a, b, c in pts:
                n = _add(n, _cross(_sub(b, a), _sub(c, a)))
            if _dot(n, n) < 1e-18:
                continue
            n = _norm(n)
            centre = _mean([p for tri in pts for p in tri])
            if _dot(n, _sub(eye, centre)) < 0:
                n = _scale(n, -1)
            out.append(digital_dash.DisplayPatch(name, ui, t, pts, uvs, n))
    return out


def face_colour(section: carbin.Section, sub: carbin.SubSection, tris: list[int], atlas) -> tuple:
    """Median RGBA of a display face's texels (UV0 through its first transform, the
    face's: display_face) at the corners, edge midpoints and centre of `tris`."""
    width, height, rgba = atlas
    xo, xs, yo, ys = sub.uv_transform[:4]
    pool = section.vertices_for(sub)
    triangles = sub.triangles()
    samples = []
    for k in tris:
        uv = [pool[i].uv0 for i in triangles[k]]
        pts = uv + [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in itertools.combinations(uv, 2)]
        pts.append((sum(u for u, _ in uv) / 3, sum(v for _, v in uv) / 3))
        for u, v in pts:
            x = min(width - 1, max(0, int((u * xs + xo) % 1.0 * width)))
            y = min(height - 1, max(0, int((v * ys + yo) % 1.0 * height)))
            o = (y * width + x) * 4
            samples.append(tuple(rgba[o : o + 3]))
    if not samples:
        return DISPLAY_COLOUR
    return tuple(sorted(c[k] for c in samples)[len(samples) // 2] for k in range(3)) + (255,)


def luminance(colour) -> float:
    return 0.299 * colour[0] + 0.587 * colour[1] + 0.114 * colour[2]


def blank_displays(static: dict, eye, faces: bool = False, atlas=None) -> list[Display]:
    """digital_gauge patches (triangles sharing a vertex position within 1 cm)
    in the cabin sections, not on the steering wheel (needles don't turn with
    it), each with its plane and rectangle. Textured faces (display_face) are
    not digital_gauge any more and don't count; with `faces` they are what is
    listed instead, each with its median colour from `atlas` (interior_LOD0)."""
    import numpy as np

    techniques = {DISPLAY_FACE} if faces else DIGITAL_TECHNIQUES
    out = []
    for name, s in static.items():
        if fm.COCKPIT_EXTERIOR_SECTIONS.match(name) or name.lower() == "steering_wheel":
            continue
        for ui, sub in enumerate(s.subsections):
            if sub.lod or sub.material.lower() not in techniques:
                continue
            pool = s.vertices_for(sub)
            tris = [[pool[i].position for i in t] for t in sub.triangles()]
            for group in _touching_groups(tris):
                pts = [p for k in group for p in tris[k]]
                p = np.array(pts, dtype=np.float64)
                c = p.mean(axis=0)
                _, vecs = np.linalg.eigh(np.cov((p - c).T))
                n = tuple(float(x) for x in vecs[:, 0])
                if _dot(n, _sub(tuple(c), eye)) < 0:
                    n = _scale(n, -1)
                up = _sub((0.0, 1.0, 0.0), _scale(n, n[1]))
                if _dot(up, up) < 1e-6:
                    continue  # a display facing straight up or down
                up = _norm(up)
                right = _cross(n, up)
                us = (p - c) @ np.array(right)
                vs = (p - c) @ np.array(up)
                out.append(
                    Display(
                        name,
                        tuple(float(x) for x in c),
                        n,
                        up,
                        right,
                        tuple(float(x) for x in (us.min(), us.max(), vs.min(), vs.max())),
                        ui,
                        face_colour(s, sub, group, atlas) if faces and atlas is not None else DISPLAY_COLOUR,
                        "face" if faces else "display",
                    )
                )
    return out


def pick_display(displays: list[Display], sight: GaugeSight | None, eye) -> tuple[Display | None, dict]:
    """The largest display the driver can read: at least VIRTUAL_MIN_AREA,
    facing the eye within VIRTUAL_MAX_TILT_DEG, and VIRTUAL_MIN_SIGHT of a
    5 x 3 grid over it in sight. Returns (display or None, why)."""
    best, why = None, {"displays": len(displays)}
    for d in sorted(displays, key=lambda d: -d.size[0] * d.size[1]):
        w, h = d.size
        if w * h < VIRTUAL_MIN_AREA:
            why.setdefault("rejected", []).append(f"{d.section} {w * 100:.0f} x {h * 100:.0f} cm: too small")
            continue
        to_eye = _norm(_sub(eye, d.centre))
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, -_dot(d.normal, to_eye)))))
        if tilt > VIRTUAL_MAX_TILT_DEG:
            why.setdefault("rejected", []).append(f"{d.section}: faces {tilt:.0f} deg away from the eye")
            continue
        if sight is not None:
            u0, u1, v0, v1 = d.rect
            grid = [
                d.point(u0 + (u1 - u0) * (i + 0.5) / 5, v0 + (v1 - v0) * (j + 0.5) / 3, VIRTUAL_MARK_LIFT)
                for i in range(5)
                for j in range(3)
            ]
            seen = sight.fraction(eye, grid)
            if seen < VIRTUAL_MIN_SIGHT:
                why.setdefault("rejected", []).append(f"{d.section}: {seen:.0%} in sight")
                continue
        best = d
        break
    return best, why


def virtual_scale(family: str, forza) -> tuple[float, float, float, float | None]:
    """(dial max, major step, minor step, red zone start) for a virtual dial,
    from the Forza car (handling.ForzaCar: top speed, redline) or None."""
    if family == "speed":
        top = getattr(forza, "top_speed_mph", 0.0) or FALLBACK_DIAL_MAX["speed"]
        step = 20.0 if top * 1.05 <= 160 else 40.0
        dial = max(VIRTUAL_SPEED_MIN, step * math.ceil(top * 1.05 / step))
        return dial, step, step / 2, None
    redline = getattr(forza, "redline", 0.0) or FALLBACK_DIAL_MAX["tach"] - 1000
    dial = max(VIRTUAL_TACH_MIN, 1000.0 * math.ceil((redline + 250) / 1000))
    major = 1000.0 if dial <= 10000 else 2000.0
    return dial, major, major / 2, redline


def _quad(pool, idx, corners, normal):
    """Append a double-sided quad (four Forza points, in order round it)."""
    base = len(pool)
    for p in corners:
        pool.append(carbin.Vertex(p, (0.0, 0.0), (0.0, 0.0), normal, bytes(8)))
    idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    idx += [base, base + 2, base + 1, base, base + 3, base + 2]


def _dial_section(name: str, version: int, parts: dict[str, list]) -> carbin.Section:
    """A Forza-space section with one LOD-0 subsection per technique; `parts`:
    technique -> [quad corners]."""
    pool, subs = [], []
    for technique, quads in parts.items():
        if not quads:
            continue
        idx: list[int] = []
        for corners, normal in quads:
            _quad(pool, idx, corners, normal)
        subs.append(carbin.SubSection(technique, 0, 4, (0.0, 1.0, 0.0, 1.0) * 2, idx))
    zero = (0.0, 0.0, 0.0)
    return carbin.Section(name, zero, zero, zero, pool, pool, subs, version)


def virtual_dial(d: Display, cell: tuple, family: str, forza, version: int):
    """(needle section, face section, Gauge, GaugeCalibration, report) for a
    dial in `cell` ((u0, u1, v0, v1) of the display). A 240-degree dial, or a
    180-degree one where that makes it larger (wide, low cells). The scale is
    drawn at the sweep the gauge table can hold (whole degrees per unit)."""
    u0, u1, v0, v1 = cell
    w, h = (u1 - u0) * (1 - 2 * VIRTUAL_INSET), (v1 - v0) * (1 - 2 * VIRTUAL_INSET)
    r240, r180 = min(w / 2, h / 1.5), min(w / 2, h / 1.12)
    span, r = (180.0, r180) if r180 > r240 * 1.15 else (240.0, r240)
    below = 0.5 * r if span == 240.0 else 0.12 * r  # extent under the pivot (arc ends, hub)
    pu = (u0 + u1) / 2
    pv = (v0 + v1) / 2 - (r + below) / 2 + below
    dial_max, major, minor, red = virtual_scale(family, forza)
    unit = GAUGE_UNITS[family]
    sweep = max(1, round(span * unit / dial_max))
    span = sweep * dial_max / unit
    start = -span / 2  # degrees clockwise from up, where the needle rests (0)
    back = d.normal  # vertex normal: the quads face the driver (-normal); normals point back at him
    nrm = _scale(back, -1)

    def at(rad, deg, lift, along=0.0):
        a = math.radians(deg)
        du, dv = math.sin(a), math.cos(a)
        return d.point(pu + rad * du + along * dv, pv + rad * dv - along * du, lift)

    def radial(r_in, r_out, deg, width, lift):
        return [at(rr, deg, lift, side * width / 2) for rr, side in ((r_in, -1), (r_out, -1), (r_out, 1), (r_in, 1))]

    marks, reds = [], []
    steps = round(dial_max / minor)
    for k in range(steps + 1):
        value = k * minor
        deg = start + span * value / dial_max
        is_major = abs(value / major - round(value / major)) < 1e-6
        length, width = (0.17 * r, 0.035 * r) if is_major else (0.09 * r, 0.018 * r)
        (reds if red is not None and value >= red - 1e-6 else marks).append(
            (radial(r - length, r, deg, width, VIRTUAL_MARK_LIFT), nrm)
        )
    arc_steps = max(8, round(span / 4))
    for k in range(arc_steps):
        a0, a1 = start + span * k / arc_steps, start + span * (k + 1) / arc_steps
        value = dial_max * (k + 0.5) / arc_steps
        in_red = red is not None and value >= red
        r_in = r - (0.07 * r if in_red else 0.015 * r)
        quad = [at(rr, aa, VIRTUAL_MARK_LIFT) for rr, aa in ((r_in, a0), (r, a0), (r, a1), (r_in, a1))]
        (reds if in_red else marks).append((quad, nrm))
    # Numerals (thousands of rpm on the tachometer) on every major mark, or every
    # second, third... where neighbours would touch along the arc.
    digit_h = 0.16 * r
    digit_w, thick, gap = 0.55 * digit_h, 0.16 * digit_h, 0.28 * digit_h
    longest = len(str(round(dial_max / 1000 if family == "tach" else dial_max)))
    label_w = longest * digit_w + (longest - 1) * gap
    every = 1
    while 0.66 * r * math.radians(span * major * every / dial_max) < 1.3 * label_w and every < 8:
        every += 1
    k = 0
    while k * major <= dial_max + 1e-6:
        value = k * major
        if k % every:
            k += 1
            continue
        text = str(round(value / 1000)) if family == "tach" else str(round(value))
        deg = start + span * value / dial_max
        a = math.radians(deg)
        cu, cv = pu + 0.66 * r * math.sin(a), pv + 0.66 * r * math.cos(a)
        width = len(text) * digit_w + (len(text) - 1) * gap
        for n, ch in enumerate(text):
            x0 = cu - width / 2 + n * (digit_w + gap)
            y0 = cv - digit_h / 2
            boxes = {
                "a": (0, digit_w, digit_h - thick, digit_h),
                "d": (0, digit_w, 0, thick),
                "g": (0, digit_w, (digit_h - thick) / 2, (digit_h + thick) / 2),
                "f": (0, thick, digit_h / 2, digit_h),
                "e": (0, thick, 0, digit_h / 2),
                "b": (digit_w - thick, digit_w, digit_h / 2, digit_h),
                "c": (digit_w - thick, digit_w, 0, digit_h / 2),
            }
            for seg in _SEGMENTS[ch]:
                bx0, bx1, by0, by1 = boxes[seg]
                corners = [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)]
                marks.append(([d.point(x0 + x, y0 + y, VIRTUAL_MARK_LIFT) for x, y in corners], nrm))
        k += 1
    mark = "dsf_dial_mark_dark" if luminance(d.colour) > VIRTUAL_DARK_MARKS else "dsf_dial_mark"
    face = _dial_section(f"dsf_dial_{family}", version, {mark: marks, "dsf_dial_red": reds})
    # The needle at rest (0 mph / 0 rpm) and its hub; the engine turns both about the pivot.
    outline = ((-0.16 * r, -0.035 * r), (0.9 * r, -0.008 * r), (0.9 * r, 0.008 * r), (-0.16 * r, 0.035 * r))
    blade = [at(rr, start, VIRTUAL_NEEDLE_LIFT, side) for rr, side in outline]
    hub = []
    for k in range(8):
        a0, a1 = 45.0 * k, 45.0 * (k + 1)
        corners = ((0.0, a0), (0.08 * r, a0), (0.08 * r, a1), (0.0, a1))
        hub.append(([at(rr, aa, VIRTUAL_HUB_LIFT) for rr, aa in corners], nrm))
    needle = _dial_section(f"{family}_min", version, {"dsf_needle": [(blade, nrm)], "dsf_needle_hub": hub})
    pivot = d.point(pu, pv)
    gauge = Gauge(family, pivot, d.normal, span, needle)
    cal = GaugeCalibration(family, GAUGE_IDS[family], sweep, False, 0.0, round(dial_max, 1), 0.0, VIRTUAL_SOURCE)
    report = {
        "display": d.section,
        "on": d.kind,
        "display_cm": [round(100 * x, 1) for x in d.size],
        "radius_cm": round(100 * r, 1),
        "span_deg": round(span, 1),
        "dial_max": dial_max,
        "red_from": red,
    }
    return needle, face, gauge, cal, report


# A virtual display (pod_display): where the readout Forza draws is on the steering
# wheel or too small for a dial, a screen in a housing on the dashboard, facing the eye,
# where the driver sees it past the wheel at any steering angle.
POD_SIZES = {  # dials -> display sizes (m) tried, largest first
    2: ((0.16, 0.08), (0.14, 0.07), (0.12, 0.06), (0.10, 0.05)),
    1: ((0.10, 0.075), (0.09, 0.0675), (0.08, 0.06), (0.07, 0.0525)),
}
POD_BEZEL = 0.006  # m: housing around the screen
POD_LIFT = 0.004  # m: the housing's front in front of the nearest dash point behind it
POD_DEPTH = 0.15  # m: the most the dashboard may fall away behind the housing (a box on the dash top)
POD_RANGE = (0.3, 1.1)  # m from the eye along the line of sight
POD_WHEEL_CLEAR = 0.04  # m beyond the rim: kept clear, the wheel turns and the hands hold it
POD_GAUGE_CLEAR = 1.2  # x a Forza needle's length: its dial is kept clear
POD_MAX_DEG = 40.0  # from the top of the rim (seen from the eye), at most
POD_SIDE_DEG = 35.0  # to either side of the wheel's centre, at most
POD_BELOW_EYE = -0.05  # the line of sight's y: at least 3 degrees down (the dashboard, not the roof)
POD_RESOLUTION = 256  # pixels across the 90-degree view the search runs on
POD_STEP = 3  # pixels between candidate centres
# DSF's HUD covers the cockpit view's lower corners: the progress bar from about 8
# degrees left of straight ahead, the car and speed box from about 24 degrees right,
# both from about 13 degrees under the eye line (1280 x 720, the head camera about 80
# degrees wide; runs vm1-20261029-144633, vm2-20261029-144816). A virtual display there
# would be hidden: pod_display keeps clear of it, with a margin. At 21:9 with DSF Enhanced's
# FOV scaling the HUD keeps its share of the screen while the view widens, so its corners
# lie further out (about 11 left, 35 right, 16 down) and these angles hold there too
# (runs vm1-20261029-201836, -203508; research/cockpit.md, "The leftovers").
HUD_DOWN_DEG = 13.5
HUD_LEFT_DEG = 7.0
HUD_RIGHT_DEG = 23.0
# The hand rig holds the rim HAND_GRIP_DEG either side of its top; a display behind a
# hand and wrist is as hidden as one under the HUD (the GT-R's and Skyline 2003's small
# moved displays in runs vm2-20261029-201910, vm1-20261029-202300). pod_display(hands=True)
# keeps this far from each grip, in the wheel's plane.
POD_HAND_CLEAR = 0.13  # m
POD_COWL_SUPPORT = 0.5  # share of a cowl display (and of its bottom quarter) in front of the cowl
# An open cockpit's bodywork round the wheel (ARX-02a: a_pillar; R10: hood): pod_display(cowl=True).
COWL_SECTIONS = re.compile(r"^(hood|a_pillar)", re.I)


def rim_grips(wheel: Wheel) -> list[tuple]:
    """Where the hands hold the rim (Forza space): HAND_GRIP_DEG either side of its
    top, the top being the rim point highest along y."""
    up = _norm(_sub((0.0, 1.0, 0.0), _scale(wheel.axis, wheel.axis[1])))
    right = _norm(_cross(wheel.axis, up))
    t = math.radians(HAND_GRIP_DEG)
    return [
        _add(wheel.centre, _add(_scale(right, s * wheel.radius * math.sin(t)), _scale(up, wheel.radius * math.cos(t))))
        for s in (1, -1)
    ]


def _disc_mask(rays, eye, centre, normal, radius):
    """Rays (N, 3) from `eye` that cross the plane through `centre` (normal
    `normal`) ahead of the eye within `radius` of the centre."""
    import numpy as np

    normal = np.array(normal, dtype=np.float64)
    rel = np.array(centre, dtype=np.float64) - np.array(eye, dtype=np.float64)
    denom = rays @ normal
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (rel @ normal) / denom
        off = rays * t[:, None] - rel
    return (t > 0) & ((off * off).sum(axis=1) < radius * radius)


def pod_display(
    static: dict,
    eye,
    wheel: Wheel,
    dials: int,
    gauges: dict,
    version: int,
    sight_sections=None,
    avoid=(),
    sizes=None,
    cowl: bool = False,
    name: str = "dsf_dial_panel",
    hud: bool = True,
    hands: bool = False,
):
    """A virtual display for `dials` (1 or 2) dials: the largest of POD_SIZES (or
    `sizes`, (width, height) in m, largest first) that fits, nearest the point just
    above the steering wheel's rim, in front of the dashboard and facing the eye.
    Every line of sight to it must reach the cabin within POD_DEPTH behind it (not
    the windscreen, the bonnet or an A-pillar), pass outside the rim (and
    POD_WHEEL_CLEAR) and clear of Forza's own dials, digital displays and gauge
    faces, and with `hud` out from under DSF's HUD (HUD_DOWN_DEG...). Found on a
    depth image from the eye.

    With `cowl` (open cockpits, issue #147), the bodywork in front of the driver
    (the cockpit carbin's `hood*` and `a_pillar` sections, which close round the
    wheel there) holds it too, and it may rise above that bodywork into the open
    sky: it stands on the cowl when POD_COWL_SUPPORT of it, and of its bottom
    quarter, is in front of the cowl. `name`: the housing section's.
    Returns (Display, housing section, report) or (None, None, report)."""
    import numpy as np

    sections = list(sight_sections if sight_sections is not None else static.values())
    points, flags = [], []
    for s in sections:
        section = s.name.lower()
        pool = None
        for sub in s.subsections:
            role = fm.classify_cockpit(sub.material)[0]
            if sub.lod != 0 or role in (fm.GLASS, fm.SKIP):
                continue
            if pool is None:
                pool = np.array([v.position for v in s.lod0_vertices], dtype=np.float64).reshape(-1, 3)
            t = np.array(sub.triangles(), dtype=np.int64).reshape(-1, 3)
            m = sub.material.lower()
            bad = (
                section == "steering_wheel"
                or section.startswith("dsf_")  # another virtual display's housing
                or (bool(fm.COCKPIT_EXTERIOR_SECTIONS.match(section)) and not (cowl and COWL_SECTIONS.match(section)))
                or m in DIGITAL_TECHNIQUES
                or m == DISPLAY_FACE
                or m.startswith(("gauge", "z_gauge"))
            )
            points.append(pool[t])
            flags.append(np.full(len(t), bad))
    if avoid:  # displays drawn elsewhere (digital_displays): kept clear too
        points.append(np.array(avoid, dtype=np.float64).reshape(-1, 3, 3))
        flags.append(np.full(len(avoid), True))
    if not points:
        return None, None, {"virtual_display": "nothing in view"}
    points, flags = np.concatenate(points), np.concatenate(flags)
    res, half = POD_RESOLUTION, POD_RESOLUTION / 2
    e = np.array(eye, dtype=np.float64)
    depth = np.zeros(res * res)
    ids = np.full(res * res, -1, dtype=np.int64)
    parts, owner = _screen_parts(points - e, 2, -1, half)
    _raster(parts, owner, res, depth, ids)
    # Per pixel: its line of sight and what it meets (Forza looks along -z).
    xc = (np.arange(res) + 0.5 - half) / half
    a, b = np.meshgrid(xc, -xc)
    rays = np.stack((a, b, -np.ones_like(a)), axis=-1).reshape(-1, 3)
    rays /= np.linalg.norm(rays, axis=1)[:, None]
    hit = depth > 0
    z = np.where(hit, 1 / np.where(hit, depth, 1), np.inf)
    hits = np.stack((a.ravel() * z, b.ravel() * z, -z), axis=-1)  # eye-relative
    far = np.linalg.norm(np.where(hit[:, None], hits, 0), axis=1) > POD_RANGE[1] / 0.8
    if cowl:  # the nose beyond the cowl is background, like the sky
        hit &= ~far
    # Nothing in view there (the windscreen, the sky): only an open cockpit's cowl
    # display may stand in front of it.
    bad = (~hit & (not cowl)) | (hit & ((ids < 0) | flags[np.maximum(ids, 0)])) | (far & (not cowl))
    bad |= _disc_mask(rays, eye, wheel.centre, wheel.axis, wheel.radius + POD_WHEEL_CLEAR)
    across = np.degrees(np.arctan2(rays[:, 0], -rays[:, 2]))  # the driver's right is +x
    down = np.degrees(np.arctan2(-rays[:, 1], np.hypot(rays[:, 0], rays[:, 2])))
    if hud:
        bad |= (down > HUD_DOWN_DEG) & ((across > HUD_RIGHT_DEG) | (across < -HUD_LEFT_DEG))
    if hands:
        for grip in rim_grips(wheel):
            bad |= _disc_mask(rays, eye, grip, wheel.axis, POD_HAND_CLEAR)
    for g in gauges.values():
        reach = max((math.dist(p, g.pivot) for p in section_points(g.rest)), default=0.0)
        bad |= _disc_mask(rays, eye, g.pivot, g.normal, reach * POD_GAUGE_CLEAR)
    bad = bad.reshape(res, res)
    hit = hit.reshape(res, res)
    hits = hits.reshape(res, res, 3)
    report = {"virtual_display": "no room"}

    def project(rel):
        zz = -rel[:, 2]
        return half + half * rel[:, 0] / zz, half - half * rel[:, 1] / zz, zz

    def fits(n, s, w, h):
        """(front, back) distances along n of the housing at s, or None."""
        n = np.array(n)
        up = _norm(_sub((0.0, 1.0, 0.0), _scale(tuple(n), n[1])))
        right = np.array(_cross(tuple(n), up))
        up = np.array(up)
        for _ in range(5):
            hw, hh = w / 2 + POD_BEZEL, h / 2 + POD_BEZEL
            corners = np.array([n * s + right * x + up * y for x, y in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))])
            px, py, pz = project(corners)
            if (pz <= 0.05).any():
                return None
            x0, x1 = max(0, int(px.min())), min(res, int(px.max()) + 1)
            y0, y1 = max(0, int(py.min())), min(res, int(py.max()) + 1)
            if px.min() < 0 or py.min() < 0 or px.max() > res or py.max() > res or x0 >= x1 or y0 >= y1:
                return None
            gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
            inside = np.ones(gx.shape, dtype=bool)
            for k in range(4):  # the quad is convex; its corners go round one way
                ax, ay, bx, by = px[k], py[k], px[(k + 1) % 4], py[(k + 1) % 4]
                side = (bx - ax) * (gy - ay) - (by - ay) * (gx - ax)
                inside &= side * ((px[1] - px[0]) * (py[2] - py[0]) - (py[1] - py[0]) * (px[2] - px[0])) >= 0
            if not inside.any() or bad[y0:y1, x0:x1][inside].any():
                return None
            held = hit[y0:y1, x0:x1]
            if cowl:  # standing on the cowl, not floating in front of the sky
                base = inside & (gy >= y1 - 0.25 * (y1 - y0))
                if held[inside].mean() < POD_COWL_SUPPORT or held[base].mean() < POD_COWL_SUPPORT:
                    return None
            d = hits[y0:y1, x0:x1][inside & held] @ n
            if d.min() - POD_LIFT < s - 1e-4:
                s = float(d.min()) - POD_LIFT
                continue
            if d.max() - s > POD_DEPTH or not POD_RANGE[0] <= s <= POD_RANGE[1]:
                return None
            return s, float(d.max())
        return None

    # Candidates by how far the driver looks from the top of the rim to them, no lower
    # than the wheel's centre and at most POD_SIDE_DEG to either side of it (not on the
    # doors or the centre console).
    up_w = _norm(_sub((0.0, 1.0, 0.0), _scale(wheel.axis, wheel.axis[1])))
    top = np.array(_norm(_sub(_add(wheel.centre, _scale(up_w, wheel.radius)), eye)))
    hub = _norm(_sub(wheel.centre, eye))
    score = np.degrees(np.arccos(np.clip(rays @ top, -1.0, 1.0))).reshape(res, res)
    side = np.degrees(np.arctan2(rays[:, 0], -rays[:, 2]) - math.atan2(hub[0], -hub[2])).reshape(res, res)
    # Under the eye line (the dash), no lower than the hub. Beside an open cockpit's wheel,
    # where nothing fits above the hub, down to the rim's bottom (the bulkhead panels).
    floors = [hub[1]]
    if cowl:
        floors.append(_norm(_sub(_sub(wheel.centre, _scale(up_w, wheel.radius)), eye))[1])
    pix = np.arange(POD_STEP // 2, res, POD_STEP)

    def candidates(floor):
        low = ((rays[:, 1] < floor) | (rays[:, 1] > POD_BELOW_EYE)).reshape(res, res)
        return sorted(
            (float(score[y, x]), int(x), int(y))
            for y in pix
            for x in pix
            if hit[y, x]
            and not bad[y, x]
            and not low[y, x]
            and abs(side[y, x]) <= POD_SIDE_DEG
            and score[y, x] <= POD_MAX_DEG
        )

    tries = [(w, h, c) for floor in floors for c in [candidates(floor)] for w, h in sizes or POD_SIZES[dials]]
    for w, h, scored in tries:
        for ang, x, y in scored:
            n = rays[y * res + x]
            got = fits(n, float(hits[y, x] @ n), w, h)
            if got is None:
                continue
            front, back = got
            n = tuple(float(c) for c in n)
            up = _norm(_sub((0.0, 1.0, 0.0), _scale(n, n[1])))
            right = _cross(n, up)
            centre = _add(eye, _scale(n, front))
            d = Display(name, centre, n, up, right, (-w / 2, w / 2, -h / 2, h / 2), kind="virtual")
            housing = _housing(d, back - front, version)
            report = {
                "virtual_display": {
                    "size_cm": [round(100 * w, 1), round(100 * h, 1)],
                    "centre": [round(c, 4) for c in centre],
                    "distance_m": round(front, 3),
                    "depth_cm": round(100 * (back - front), 1),
                    "from_rim_top_deg": round(ang, 1),
                }
            }
            if cowl:
                report["virtual_display"]["cowl"] = True
            if not hud:
                report["virtual_display"]["under_hud"] = True
            return d, housing, report
    return None, None, report


# pod_display's passes, (cowl, hud): the dashboard clear of DSF's HUD, else an open
# cockpit's cowl clear of it (#147), else the dashboard as before the HUD rule (#92's
# placement: the display may sit under the HUD's lower corners).
POD_PASSES = ((False, True), (True, True), (False, False))
POD_HUD_PASSES = POD_PASSES[:2]  # the passes clear of DSF's HUD


def place_pod(
    static: dict,
    eye,
    wheel: Wheel,
    dials: int,
    gauges: dict,
    version: int,
    sight_sections=None,
    passes=POD_PASSES,
    **kw,
):
    """pod_display over POD_PASSES: the first that finds room. Seen from the head
    camera as high as camera_eye's rim rule may raise it (up to 12 cm over the Forza
    eye: GT-R GT1), where the converted car will look from: from the Forza eye, a
    display placed low on the dash went under the cowl or the HUD in game (runs
    vm1-20261029-144633, vm1-20261029-151818)."""
    if "steering_wheel" in static:
        eye = camera_eye(eye, section_points(static["steering_wheel"]))
    d, housing, report = None, None, {}
    for cowl, hud in passes:
        d, housing, report = pod_display(
            static, eye, wheel, dials, gauges, version, sight_sections, cowl=cowl, hud=hud, **kw
        )
        if d is not None:
            break
    return d, housing, report


def _housing(d: Display, depth: float, version: int) -> carbin.Section:
    """The virtual display's geometry: the screen over the display rectangle, a
    bezel POD_BEZEL wider just behind it, and sides from the bezel back `depth`
    (to the dashboard behind it)."""
    u0, u1, v0, v1 = d.rect
    nrm = _scale(d.normal, -1)
    screen = [d.point(u, v) for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))]
    b = POD_BEZEL
    rim = [(u0 - b, v0 - b), (u1 + b, v0 - b), (u1 + b, v1 + b), (u0 - b, v1 + b)]
    bezel = [d.point(u, v, -0.0008) for u, v in rim]
    back = [d.point(u, v, -depth - 0.005) for u, v in rim]
    sides = []
    for k in range(4):
        q = [bezel[k], bezel[(k + 1) % 4], back[(k + 1) % 4], back[k]]
        sides.append((q, _norm(_cross(_sub(q[1], q[0]), _sub(q[3], q[0])))))
    return _dial_section(
        d.section, version, {"dsf_dial_display": [(screen, nrm)], "dsf_dial_bezel": [(bezel, nrm)] + sides}
    )


def _face_front(static: dict, d: Display) -> float:
    """How far digital patches over a face's rectangle stand in front of it (the
    LFA's round dial face 4.7 mm in front of the cluster face behind it)."""
    front = 0.0
    u0, u1, v0, v1 = d.rect
    for name, s in static.items():
        if name.lower() == "steering_wheel":
            continue
        for sub in s.subsections:
            if sub.lod or not (sub.material.lower() in DIGITAL_TECHNIQUES or sub.material == DISPLAY_FACE):
                continue
            for p in section_points(s, {sub.material}):
                rel = _sub(p, d.centre)
                depth = _dot(rel, d.normal)
                if (
                    -FACE_FRONT_DEPTH < depth < FACE_FRONT_DEPTH
                    and u0 <= _dot(rel, d.right) <= u1
                    and v0 <= _dot(rel, d.up) <= v1
                ):
                    front = max(front, -depth)
    return front


def virtual_gauges(
    static: dict,
    gauges: dict,
    eye,
    forza,
    version: int,
    sight_sections=None,
    wheel: Wheel | None = None,
    atlas=None,
    readout=(),
    provided=(),
    assume=(),
) -> tuple[list, dict, dict]:
    """Virtual dials for the speedometer and tachometer Forza has no needle
    for, on the largest blank display the driver can read (pick_display); else
    on the largest display face (blank_displays with faces; `atlas` gives its
    colour, fit draws it flat in it); else, where the car has a digital
    readout anywhere (on the steering wheel, small strips), on a virtual
    display on the dashboard (pod_display, needs `wheel`). Two side by side
    along the long side when both are missing (tachometer first: left or
    top). `readout`: the triangles of displays a dash scene is drawn on
    (digital_displays), which count as a digital readout and stay clear;
    `provided`: the readouts that scene drives (no dial for those); `assume`: readouts
    the car has though nothing in its cockpit shows them (a placeholder cockpit
    carbin's cabin): dials on a virtual display. Returns
    ([(needle, face, Gauge, GaugeCalibration)], report, extra static sections (the
    virtual display's housing))."""
    missing = [f for f in ("tach", "speed") if f not in gauges and f not in provided]
    if not missing:
        return [], {}, {}
    digital = any(
        sub.lod == 0 and (sub.material.lower() in DIGITAL_TECHNIQUES or sub.material == DISPLAY_FACE)
        for name, s in static.items()
        if not fm.COCKPIT_EXTERIOR_SECTIONS.match(name)
        for sub in s.subsections
    )
    if not digital and not readout and not set(missing) & set(assume):
        return [], {}, {}
    sight = GaugeSight(list(sight_sections if sight_sections is not None else static.values()), [])
    report: dict = {"missing": missing}
    extra = {}
    displays = blank_displays(static, eye)
    d, why = pick_display(displays, sight, eye) if displays else (None, {})
    report.update(why)
    if d is None:
        faces = blank_displays(static, eye, faces=True, atlas=atlas)
        if faces:
            d, why = pick_display(faces, sight, eye)
            report["faces"] = why
            if d is not None:
                front = _face_front(static, d)
                d.centre = _add(d.centre, _scale(d.normal, -front))
                report["face"] = {
                    "section": d.section,
                    "subsection": d.subsection,
                    "colour": list(d.colour[:3]),
                    "front_mm": round(1000 * front, 1),
                }
    if d is None and wheel is not None:
        d, housing, why = place_pod(
            static, eye, wheel, len(missing), gauges, version, sight_sections, avoid=list(readout)
        )
        report.update(why)
        if d is not None:
            extra[housing.name] = housing
    if d is None:
        return [], report, {}
    u0, u1, v0, v1 = d.rect
    w, h = d.size
    cells = []
    for k in range(len(missing)):
        if w >= h:
            cells.append((u0 + w * k / len(missing), u0 + w * (k + 1) / len(missing), v0, v1))
        else:
            cells.append((u0, u1, v1 - h * (k + 1) / len(missing), v1 - h * k / len(missing)))
    out = []
    report["dials"] = {}
    for family, cell in zip(missing, cells):
        needle, face, gauge, cal, rep = virtual_dial(d, cell, family, forza, version)
        out.append((needle, face, gauge, cal))
        report["dials"][family] = rep
    return out, report, extra


def flatten_face(static: dict, face: dict) -> int:
    """The display face a virtual dial went on (virtual_gauges' report["face"])
    back to a blank display in the face's colour (sub.display_rgba), so its
    static artwork (bar outlines, printed scales, "0 MPH") doesn't show through
    the dial. Returns its triangle count."""
    import copy

    s = copy.copy(static[face["section"]])
    s.subsections = list(s.subsections)
    sub = copy.copy(s.subsections[face["subsection"]])
    sub.material = next(iter(DIGITAL_TECHNIQUES))
    sub.display_rgba = tuple(face["colour"]) + (255,)
    s.subsections[face["subsection"]] = sub
    static[face["section"]] = s
    return len(sub.triangles())


def drop_unturned(cf: CockpitFit, gauge_slots: dict[int, int]) -> list[str]:
    """Take out the speed and tach needles (Forza's or virtual dials) whose
    gauge id the host's gauge table has no entry for, after add_gauge_records:
    only hosts without any gauge record (vanilla 250, the Volt's, and four
    unused ones). The engine would leave such a needle unturned, so it isn't
    drawn at all. Returns the families dropped."""
    dropped = [f for f in cf.gauges if f in GAUGE_IDS and GAUGE_IDS[f] not in gauge_slots]
    for f in dropped:
        cf.gauges.pop(f)
        cf.needles.pop(f, None)
        cf.calibration.pop(f, None)
        cf.static.pop(f"dsf_dial_{f}", None)
        cf.virtual.get("dials", {}).pop(f, None)
    if dropped and not cf.virtual.get("dials"):
        cf.static.pop("dsf_dial_panel", None)  # a virtual display with no dial left
    if dropped:
        cf.virtual["no_host_gauge"] = dropped
    return dropped


def dash_needles(plan, version: int) -> list:
    """The dash scene's needles (digital_dash._needles) as DSF needles: (Gauge,
    GaugeCalibration) with a flat needle quad in the scene's colour, at its fitted rest
    (0 rpm / 0 mph), turned by the engine like a Forza needle."""
    out = []
    for n in plan.needles:
        normal = _scale(n["patch"].towards_eye, -1)  # into the dash
        section = _dial_section(f"{n['family']}_min", version, {"dsf_needle": [(n["corners"], n["patch"].towards_eye)]})
        for sub in section.subsections:
            sub.needle_rgba = tuple(n["colour"])
        unit = GAUGE_UNITS[n["family"]]
        sweep = max(1, round(n["per_unit"] * unit))
        span = (-1 if n["reverse"] else 1) * n["per_unit"] * n["top"]
        g = Gauge(n["family"], n["pivot3d"], normal, span, section)
        cal = GaugeCalibration(
            n["family"], GAUGE_IDS[n["family"]], sweep, n["reverse"], 0.0, round(n["top"], 1), 0.0, "dash scene needle"
        )
        out.append((g, cal))
    return out


# A display only on the steering wheel (LMP, GT racers) moves onto a virtual display of
# its own shape, tried at these sizes (x its own) from the largest down: further away
# than the wheel, it keeps about the size it has there.
WHEEL_DISPLAY_SCALES = (1.5, 1.25, 1.0, 0.8, 0.6)
# Tried when those find room only under DSF's HUD, in the passes clear of it and clear of
# the hands: a smaller display in sight beats a larger one behind the progress bar or the
# speed box (#147: the R15 TDI's, on its cowl at 0.5). Where the only smaller spot is
# behind a hand (the GT-R's, the Skyline 2003's), the larger one stays: at 21:9 it shows
# past the HUD's corner (research/cockpit.md, "The leftovers").
WHEEL_DISPLAY_SMALL_SCALES = (0.5, 0.4)
WHEEL_DISPLAY_LIFT = 0.001  # m: the moved display in front of the housing's screen
DASH_PANEL = "dsf_dash_panel"  # the housing of a moved wheel display (pod_display name)


def wheel_display_pod(static: dict, eye, wheel: Wheel, gauges: dict, version: int, sight_sections=None) -> tuple:
    """The steering wheel's digital displays (display_patches(on_wheel=True)) moved onto a
    virtual display in view (pod_display: on the dashboard, else on an open cockpit's
    cowl), so the dash scene can be drawn on them live. DSF's digital dials can't follow
    the wheel: their shader has no skin input and transforms by g_ObjectToWorld alone,
    and the needles' by g_InstrumentTransforms alone (research/cockpit.md, "Displays on
    the steering wheel", issue #147). The patches keep their UVs, so they show the same
    window of the scene; their points move rigidly (and scale) from the wheel display's
    plane onto the virtual display's. Returns (moved patches, housing section, report)
    or ([], None, report)."""
    from dsfforza.convert import digital_dash

    patches = display_patches(static, eye, on_wheel=True)
    if not patches:
        return [], None, {"virtual_display": "no display on the steering wheel"}
    towards = _norm(_mean([p.towards_eye for p in patches]))
    n = _scale(towards, -1)  # into the wheel, as Display.normal
    up = _norm(_sub((0.0, 1.0, 0.0), _scale(n, n[1])))
    right = _cross(n, up)
    pts = [q for p in patches for tri in p.points for q in tri]
    centre = _mean(pts)
    us = [_dot(_sub(q, centre), right) for q in pts]
    vs = [_dot(_sub(q, centre), up) for q in pts]
    w, h = max(us) - min(us), max(vs) - min(vs)
    centre = _add(_add(centre, _scale(right, (max(us) + min(us)) / 2)), _scale(up, (max(vs) + min(vs)) / 2))
    sizes = tuple((w * k, h * k) for k in WHEEL_DISPLAY_SCALES)
    d, housing, report = place_pod(static, eye, wheel, 1, gauges, version, sight_sections, sizes=sizes, name=DASH_PANEL)
    if d is not None and report["virtual_display"].get("under_hud"):
        small = tuple((w * k, h * k) for k in WHEEL_DISPLAY_SMALL_SCALES)
        found = place_pod(
            static,
            eye,
            wheel,
            1,
            gauges,
            version,
            sight_sections,
            passes=POD_HUD_PASSES,
            sizes=small,
            name=DASH_PANEL,
            hands=True,
        )
        if found[0] is not None:
            d, housing, report = found
    if d is None:
        return [], None, report
    k = d.size[0] / w
    report["virtual_display"]["scale"] = round(k, 2)
    report["virtual_display"]["wheel_display_cm"] = [round(100 * w, 1), round(100 * h, 1)]

    def move(q):
        rel = _sub(q, centre)
        return d.point(k * _dot(rel, right), k * _dot(rel, up), WHEEL_DISPLAY_LIFT)

    moved = [
        digital_dash.DisplayPatch(
            DASH_PANEL,
            p.subsection,
            p.transform,
            [[move(q) for q in tri] for tri in p.points],
            p.uv0,
            _scale(d.normal, -1),
        )
        for p in patches
    ]
    return moved, housing, report


def digital_displays(
    static: dict,
    eye,
    folder: Path,
    forza,
    wheel: Wheel | None = None,
    gauges=None,
    version: int = 3,
    sight_sections=None,
) -> tuple:
    """The car's dash scene (folder/digitalgauge, formats/anark.py; shared textures and
    fonts in <cache>/<game>/dash_media) drawn live on its digital displays
    (digital_dash.build_plan): the displays' subsections leave the cabin (the plan draws
    them). A car whose only display is on the steering wheel and whose needles
    (`gauges`) lack a speedometer or tachometer gets it on a virtual display
    (wheel_display_pod, needs `wheel`; the wheel keeps its own). Returns (plan
    or None, report: "provides" lists the speed/tach readouts it drives, extra static
    sections: the virtual display's housing)."""
    import copy

    from dsfforza.convert import dash_scene, digital_dash

    dash_dir = Path(folder) / "digitalgauge"
    media = Path(folder).parent / "dash_media"
    if not dash_dir.is_dir() or not media.is_dir():
        return None, {"skipped": "no dash scene"}, {}
    stems = [p for p in dash_dir.iterdir() if p.suffix.lower() == ".bsg"]
    own = [p for p in stems if p.stem.lower() == f"dash_{Path(folder).name}".lower()] or stems
    if len(own) != 1:
        return None, {"skipped": f"{len(stems)} dash scenes"}, {}
    patches = display_patches(static, eye)
    extra, moved = {}, {}
    if not patches and wheel is not None and not {"speed", "tach"} <= set(gauges or {}):
        patches, housing, moved = wheel_display_pod(static, eye, wheel, gauges or {}, version, sight_sections)
        if housing is not None:
            extra[housing.name] = housing
    if not patches:
        return None, {"skipped": "no display off the steering wheel", **moved}, {}
    try:
        dash = dash_scene.Dash(own[0].with_suffix(""), media)
        redline = getattr(forza, "redline", 0.0) or 7000.0
        plan = digital_dash.build_plan(dash, patches, redline)
    except Exception as error:  # a scene this decoder can't draw: the display stays as it was
        return None, {"skipped": f"{type(error).__name__}: {error}"[:200]}, {}
    if plan is None:
        return None, {"skipped": "no room in the texture"}, {}
    drawn: dict = {}
    for p in patches:  # the plan draws these now (a moved wheel display: the wheel keeps its own)
        if p.section in static:
            drawn.setdefault(p.section, set()).add(p.subsection)
    for name, subs in drawn.items():
        sec = copy.copy(static[name])
        sec.subsections = [sub for k, sub in enumerate(sec.subsections) if k not in subs]
        static[name] = sec
    rep = dict(plan.report)
    rep["provides"] = sorted(
        {f for f, k in (("speed", "speed"), ("tach", "bar_pieces")) if rep.get(k)} | {n["family"] for n in plan.needles}
    )
    rep["missing"] = sorted(dash.assets.missing)
    if moved:
        rep["moved_from_wheel"] = moved["virtual_display"]
    return plan, rep, extra


def is_stub(sections: list[carbin.Section]) -> bool:
    """FH1 ships placeholder cockpit carbins for some cars (Dodge Durango,
    Chevrolet Express): every section is a 17-vertex box about 0.2 m wide."""
    pts = [p for s in sections for p in section_points(s)]
    interior = sum(len(section_points(s)) // 3 for s in sections if s.name.lower() == "interior")
    # FH1 Taurus SHO: 17 sections of 8 triangles each, spread over the cabin.
    if len(pts) < 3 * STUB_TRIANGLES or interior < 100:
        return True
    return max(max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)) < STUB_EXTENT


def fit(
    sections: list[carbin.Section],
    name: str,
    gamedbs: list[Path],
    folder: Path | None = None,
    main_sections: list[carbin.Section] | None = None,
    virtual: bool = True,
) -> CockpitFit:
    """Measure the cockpit; `folder` (the extracted car) supplies the
    CarAttribs.xml tick tables for the gauge calibration. `main_sections`
    (the main carbin) stands in when the cockpit carbin is a stub (its
    interior, seats and steering wheel at their finest LOD, main atlases) or
    lacks a steering wheel (the main carbin's, drawn flat). With `virtual`, a
    speedometer or tachometer Forza shows as a digital readout gets a virtual
    dial on a blank display (virtual_gauges; the gamedbs give its scale)."""
    source, wheel_source, lod0 = "cockpit", "cockpit", True
    dropped: set = set()
    main = {s.name.lower(): s for s in (main_sections or [])}
    if is_stub(sections):
        cabin = [promote_lod(main[n.lower()]) for n in MAIN_CABIN if n.lower() in main]
        cabin = [c for c in cabin if c is not None]
        if not any(c.name.lower() == "interior" for c in cabin):
            raise ValueError(f"{name}: cockpit carbin is a stub and the main carbin has no interior section")
        # The main carbin's driver seat carries a baked low-poly driver (DSF seats
        # its own), whose head would sit around the eye: leave that seat out.
        wheel_at = next((c for c in cabin if c.name.lower() == "steering_wheel"), None)
        if wheel_at is not None:
            wx = _mean(section_points(wheel_at))[0]
            dropped = {
                c.name for c in cabin if c.name.lower().startswith("seat") and _mean(section_points(c))[0] * wx > 0
            }
            cabin = [c for c in cabin if c.name not in dropped]
        # forza2dsf passes [] when the car has no cockpit carbin at all.
        source = ("stub" if sections else "no") + " cockpit carbin: main carbin cabin"
        sections = cabin
        lod0 = False
    else:
        # Upgrade parts, as on the outside (forza2dsf.vanilla_sections): the cockpit
        # carbin holds every bonnet the car can wear (Mustang Boss 429: hooda,
        # hoodCIrace, hoodPSrace), all drawn at once over the vanilla one. The
        # Boss's body-colour race scoop hid hooda's black one from the cockpit.
        from dsfforza.convert import forza2dsf

        sections = forza2dsf.vanilla_sections(sections)
    static, needles = cockpit_parts(sections)
    if "steering_wheel" not in static:
        borrowed = promote_lod(main["steering_wheel"]) if "steering_wheel" in main else None
        if borrowed is None:
            raise ValueError(f"{name}: no steering_wheel section in the cockpit or main carbin")
        borrowed.flat = True  # main-carbin UVs don't fit the cockpit's _LOD0 atlases
        static["steering_wheel"] = borrowed
        wheel_source = "main carbin"
        if source == "cockpit":
            source = "cockpit; steering wheel from the main carbin"
    else:
        wheel_source = "cockpit" if lod0 else "main carbin"
    # Forza's wheel keeps its size in every view: dsfforza.asi [hands] moves the driver's
    # hands onto its rim (grip_params), the cockpit arms and the chase views' driver alike.
    wheel = fit_wheel(static["steering_wheel"])
    gauges = {}
    for family, poses in needles.items():
        g = fit_gauge(family, poses)
        if g is not None:
            gauges[family] = g
    offset, eye_source = eye_offset(name, gamedbs)
    mirrors = {}
    for s in static.values():
        for tech, side in (("mirrorLeft", "left"), ("mirrorRight", "right")):
            pts = section_points(s, {tech})
            if pts:
                mirrors[side] = _mean(pts)
    ticks = gauge_ticks(folder)
    calibration = {f: calibrate_gauge(g, ticks.get(f)) for f, g in gauges.items() if f in GAUGE_IDS}
    faces = 0
    if lod0:  # the face maps into interior_LOD0; main-carbin cabins have no digital_gauge
        for n, s in static.items():
            static[n], k = with_display_faces(s)
            faces += k
    virtual_report = {}
    digital_plan, digital_report = None, {}
    forza = None
    version = next((s.version for s in static.values()), 3)
    if virtual or lod0:
        from dsfforza.build import handling

        try:
            forza = handling.load_forza(name, [Path(db) for db in gamedbs if db and Path(db).exists()])
        except (SystemExit, Exception):
            forza = None
    if lod0 and folder is not None:
        # Forza's digital displays, live through DSF's digital dials (digital_dash); a
        # display only on the steering wheel on a virtual display (wheel_display_pod).
        sight = [s for n, s in static.items() if n not in SKIP_SECTIONS and not LIGHT_SECTIONS.search(n)]
        digital_plan, digital_report, extra = digital_displays(
            static, eye_point(wheel, offset), folder, forza, wheel, gauges, version, sight
        )
        static.update(extra)
        if digital_plan is not None:
            for g, cal in dash_needles(digital_plan, version):
                if g.family not in gauges:  # Forza's own needle first
                    gauges[g.family], calibration[g.family] = g, cal
                    needles[g.family] = {"min": g.rest}
    provided = set(digital_report.get("provides", ()))
    if virtual:
        drawn = [s for n, s in static.items() if n not in SKIP_SECTIONS and not LIGHT_SECTIONS.search(n)]
        try:
            atlas = fm.AtlasCache(folder, lod0=True).get(fm.INTERIOR) if folder is not None and lod0 else None
        except FileNotFoundError:
            atlas = None
        # Readouts the dash scene drives (digital_displays) need no virtual dial.
        readout = [tri for p in getattr(digital_plan, "patches", []) for tri in p.points]
        dials, virtual_report, extra = virtual_gauges(
            static,
            gauges,
            eye_point(wheel, offset),
            forza,
            version,
            drawn,
            wheel=wheel,
            atlas=atlas,
            readout=readout,
            provided=provided,
            # A placeholder cockpit carbin (the main carbin's cabin, issue #148) has no
            # needle and no display; the car has a speedometer and a tachometer all the same.
            assume=() if lod0 else ("tach", "speed"),
        )
        if dials and "face" in virtual_report:
            faces -= flatten_face(static, virtual_report["face"])
        static.update(extra)
        for needle, face, g, cal in dials:
            needles[g.family] = {"min": needle}
            gauges[g.family] = g
            calibration[g.family] = cal
            static[face.name] = face
    digital = sum(
        len(sub.triangles())
        for s in static.values()
        for sub in s.subsections
        if sub.lod == 0 and sub.material.lower() in DIGITAL_TECHNIQUES
    )
    return CockpitFit(
        wheel,
        gauges,
        eye_point(wheel, offset),
        offset,
        eye_source,
        mirrors,
        static,
        needles,
        calibration,
        digital,
        source,
        wheel_source,
        lod0,
        dropped,
        display_faces=faces,
        virtual=virtual_report,
        digital_plan=digital_plan,
        digital_displays=digital_report,
    )


def gauge_params(cf: CockpitFit) -> dict[int, float]:
    """VO3D: the speedometer stops at its last mark (p605), from 0 (p606). Digital
    speed digits read the same clamped value: without a needle they go to 255 mph."""
    cal = cf.calibration.get("speed")
    if cal:
        return {SPEED_DIAL_PARAM: float(cal.dial_max), SPEED_FLOOR_PARAM: 0.0}
    if "speed" in cf.digital_displays.get("provides", ()):
        return {SPEED_DIAL_PARAM: 255.0, SPEED_FLOOR_PARAM: 0.0}
    return {}


# --- DSF hierarchy and camera --------------------------------------------------------


@dataclass
class HostCockpit:
    """Where the host keeps its cockpit animation (from the full entry's UPVH)."""

    wheel_record: int
    wheel_hinge: int
    wheel_marker: int
    wheel_skin: bytes  # vertex semantic 9 for steering-wheel vertices
    gauge_slots: dict[int, int]  # gauge id -> gauge ("seat") table index
    gauge_parent_hinge: int | None
    mirror_slots: list[int]


def parent_of(h, index: int) -> int | None:
    for j in range(index - 1, -1, -1):
        if j + h.records[j].subtree > index:
            return j
    return None


def host_cockpit(upvh_data: bytes) -> HostCockpit:
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    markers = [r for r in h.records if r.type == 3 and r.role == STEERING_MARKER_ROLE and r.marker >= 0]
    if not markers:
        raise ValueError("no steering marker (record type 3, role 0x2F) in the hierarchy")
    marker = markers[0].marker
    mpos = struct.unpack_from("<3f", h.table("markers")[marker])
    best = None
    for r in h.records:
        if r.hinge < 0 or r.type not in (1, 5):
            continue
        pivot = struct.unpack_from("<3f", h.table("hinges")[r.hinge])
        d = math.dist(pivot, mpos)
        if best is None or d < best[0]:
            best = (d, r)
    if best is None or best[0] > 0.05:
        raise ValueError("no hinge at the steering marker")
    rec = best[1]
    slot = max(0, struct.unpack_from("<h", rec.raw, 0x34)[0])
    model = max(0, struct.unpack_from("<h", rec.raw, 0x2A)[0])
    gauge_slots, parent_hinge = {}, None
    for r in h.records:
        if r.type == 18 and r.seat >= 0:
            gid = struct.unpack_from("<I", h.table("seats")[r.seat], 32)[0]
            gauge_slots[gid] = r.seat
            p = parent_of(h, r.index)
            if p is not None and h.records[p].hinge >= 0:
                parent_hinge = h.records[p].hinge
    return HostCockpit(
        rec.index,
        rec.hinge,
        marker,
        bytes((slot & 0xFF, model & 0xFF, 0, 1)),
        gauge_slots,
        parent_hinge,
        list(range(h.counts["mirrors"])),
    )


# Vanilla gauge records (type 18) are leaves under the cluster part (role 185):
# the speedometer's has role 0, the tachometer's 187, and every record field
# but the role and the gauge-table index is the same (144 speedometers, 126
# tachometers over the 150 vanilla hierarchies). FUN_0088E310 keeps, per gauge
# table index (at most 7), the record of each type-18 record whose role isn't
# 185 and whose entry's gauge id is below 8; the render loop turns a needle
# through every kept record's entry (research/cockpit.md, "Gauges").
GAUGE_RECORD_TYPE = 18
CLUSTER_ROLE = 185
GAUGE_ROLES = {2: 0, 1: 187}  # gauge id -> the vanilla record's role
MAX_GAUGES = 7


def gauge_records(h) -> dict[int, int]:
    """Gauge id -> record index of the records the engine keeps (FUN_0088E310)."""
    out = {}
    seats = h.table("seats")
    for r in h.records:
        if r.type == GAUGE_RECORD_TYPE and r.role != CLUSTER_ROLE and 0 <= r.seat < len(seats):
            gid = struct.unpack_from("<I", seats[r.seat], 0x20)[0]
            if gid < 8:
                out.setdefault(gid, r.index)
    return out


def add_gauge_records(upvh_data: bytes, ids) -> tuple[bytes, list[int]]:
    """Give the hierarchy a gauge record and gauge-table entry for each id in
    `ids` it lacks, so the engine turns that needle too. 19 vanilla hosts carry a
    speedometer only (146 the 1968 Camaro, 230, 249...), 225 (MP4-12C) a
    tachometer only. The new record is a copy of the host's own gauge record
    (same parent, the cluster part; the vanilla role for the id), the speedometer's
    before the tachometer's as in vanilla hierarchies; its entry is a copy of
    that record's entry with the new id, appended to the table (patch_hierarchy
    sets the pivot, dial and sweep). A host without any gauge record (250,
    the Volt's, and 293) gets none: its only dial material is a digital one
    (`*_Digital_Dials`), whose program doesn't read g_InstrumentTransforms, so no
    needle could turn there (research/cockpit.md, "Displays on the steering wheel").
    Returns the new hierarchy and the ids added."""
    from dsfforza.convert import damage
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    have = gauge_records(h)
    missing = sorted(set(ids) - set(have), reverse=True)  # speedometer (2) first
    if not have or not missing:
        return upvh_data, []
    data = upvh_data
    added = []
    for gid in missing:
        h = upvh.parse(data)
        have = gauge_records(h)
        if h.counts["seats"] >= MAX_GAUGES or gid not in GAUGE_ROLES:
            break
        src = h.records[min(have.values())]
        if src.subtree != 1:
            break
        seat = h.counts["seats"]
        rec = bytearray(src.raw)
        struct.pack_into("<H", rec, 0x0A, GAUGE_ROLES[gid])
        struct.pack_into("<b", rec, 0x26, seat)
        at = src.index + 1 if gid == 1 else src.index
        records = bytearray(b"".join(r.raw for r in h.records))
        records[at * upvh.RECORD_SIZE : at * upvh.RECORD_SIZE] = rec
        # Every ancestor's subtree grows by one (they all come before `at`).
        p = parent_of(h, src.index)
        while p is not None:
            o = p * upvh.RECORD_SIZE + 0x14
            struct.pack_into("<H", records, o, struct.unpack_from("<H", records, o)[0] + 1)
            p = parent_of(h, p)
        # The fields that follow the tree's shape (damage._tree_fields: +0x0C bit 0 and
        # the hinge pop counts +0x16/+0x18) are written again for the new tree on every
        # record that followed them before. The record that ended the cluster's subtree
        # popped its hinge there; a leaf before the new one pops nothing (#266).
        raw = [records[k * upvh.RECORD_SIZE : (k + 1) * upvh.RECORD_SIZE] for k in range(len(h.records) + 1)]
        old = [r.raw for r in h.records]
        before = damage._tree_fields(old)
        source = [k if k < at else k - 1 for k in range(len(raw))]
        source[at] = src.index
        for k, (r, fields) in enumerate(zip(raw, damage._tree_fields(raw), strict=True)):
            if damage._read_tree_fields(old[source[k]]) == before[source[k]]:
                struct.pack_into("<H", r, 0x0C, (struct.unpack_from("<H", r, 0x0C)[0] & ~1) | fields[0])
                struct.pack_into("<HH", r, 0x16, *fields[1:])
        records = b"".join(raw)
        entry = bytearray(h.table("seats")[src.seat])
        struct.pack_into("<I", entry, 0x20, gid)
        tables = upvh.HEADER_SIZE + len(h.records) * upvh.RECORD_SIZE
        seats_end = h.offsets["seats"] + seat * 0x30
        out = bytearray(data[: upvh.HEADER_SIZE] + records + data[tables:seats_end] + entry + data[seats_end:])
        struct.pack_into("<I", out, 0x10, len(h.records) + 1)
        out[0x2C] = seat + 1
        data = bytes(out)
        added.append(gid)
    return data, added


# --- driver's hands -------------------------------------------------------------------
# The cockpit hands are one rig for every car (research/cockpit.md, "Driver's hands").
# In game (run 20261024-000936) the hands sit at A + H and the skinned wheel at
# A + (v - hinge), where A = steering marker (role 47) + (0, VO3D p538, p559)
# (FUN_005743E0) and H is fixed. When the car steers, both turn about A, about the
# marker's column (FUN_005756E0 -> FUN_00514150). So the marker and p538/p559
# move the wheel and the hands together. Only the hinge, the point of the wheel
# mesh drawn at A, moves the hands against the wheel. The marker's quaternion
# leaves both alone at rest. The rig fits the vanilla wheel: in every vanilla car the
# hinge is the centre of the same wheel mesh, rim radius 0.176 m (fleet median of
# the fit_wheel measure; quartiles 0.174 to 0.180), column 24 deg up. The hands
# grip it about 45 deg either side of the top.
HAND_PARAMS = (538, 559)
VANILLA_RIM_RADIUS = 0.176
VANILLA_COLUMN = (0.0, 0.410, -0.912)  # DSF model space, towards the driver
HAND_GRIP_DEG = 45.0
MAX_HAND_ALONG = 0.12  # m, along the column (no orbit; the driver model moves with it)
MAX_SEAT_SHIFT = 0.06  # m, in the wheel plane: grip_shift for the seat (the chase view's driver)


def _rim_nearest(p, centre, axis, radius):
    d = _sub(p, centre)
    d = _sub(d, _scale(axis, _dot(d, axis)))
    return _add(centre, _scale(_norm(d), radius))


def hand_grips(pivot) -> list[tuple]:
    """Where the vanilla hand rig grips when the steering pivot (hinge = marker,
    p538/p559 zero) is at `pivot` (DSF model space)."""
    a = _norm(VANILLA_COLUMN)
    up = _norm(_sub((0.0, 1.0, 0.0), _scale(a, a[1])))
    t = math.radians(HAND_GRIP_DEG)
    return [
        _add(
            pivot,
            _add(
                _scale((1.0, 0.0, 0.0), s * VANILLA_RIM_RADIUS * math.sin(t)),
                _scale(up, VANILLA_RIM_RADIUS * math.cos(t)),
            ),
        )
        for s in (1, -1)
    ]


def hand_shift(centre, axis, radius) -> tuple[float, float, float]:
    """Offset from the Forza wheel centre for the steering hinge and marker,
    along the column only: the least-squares move along the axis that brings the
    vanilla hand grips to the rim's plane, capped at MAX_HAND_ALONG (it moves the
    driver model with the seat, patch_seats). The engine turns the wheel about
    the marker's direction through the hinge, so any part of the shift in the
    wheel plane makes the hub circle by 2 |shift| sin(angle / 2) while steering
    (research/cockpit.md, "The steering wheel turns about its hub"); the grips
    reach the rim through dsfforza.asi [hands] instead (grip_params)."""
    axis = _norm(axis)
    grips = hand_grips(centre)
    t = 0.0
    for _ in range(8):
        shift = _scale(axis, t)
        moves = [_sub(_rim_nearest(_add(g, shift), centre, axis, radius), _add(g, shift)) for g in grips]
        t += _dot(_mean(moves), axis)
    t = max(-MAX_HAND_ALONG, min(MAX_HAND_ALONG, t))
    return _scale(axis, t)


def grip_shift(centre, axis, radius) -> tuple[float, float, float]:
    """Translation (y and z) from the wheel centre that puts the vanilla grips on
    the rim (least squares), with the in-plane part capped at MAX_SEAT_SHIFT and
    the part along the column at MAX_HAND_ALONG. Not for the hinge (the wheel
    would turn about an off-hub point): it places the driver's seat, whose
    animated driver holds the wheel at the vanilla point of the seat's frame in
    the chase views (seat_anchor, patch_seats). The same translation placed the
    hinge and marker before 2026-10-26."""
    axis = _norm(axis)
    grips = hand_grips(centre)
    shift = (0.0, 0.0, 0.0)
    for _ in range(8):
        moves = [_sub(_rim_nearest(_add(g, shift), centre, axis, radius), _add(g, shift)) for g in grips]
        shift = _add(shift, _mean(moves))
        shift = (0.0, shift[1], shift[2])
    along = _scale(axis, _dot(shift, axis))
    plane = _sub(shift, along)
    n = math.sqrt(_dot(plane, plane))
    if n > MAX_SEAT_SHIFT:
        plane = _scale(plane, MAX_SEAT_SHIFT / n)
    n = math.sqrt(_dot(along, along))
    if n > MAX_HAND_ALONG:
        along = _scale(along, MAX_HAND_ALONG / n)
    shift = _add(plane, along)
    return (0.0, shift[1], shift[2])


SEAT_FIT_TOLERANCE = 0.005  # m
SEAT_SCALE_RANGE = (0.8, 1.3)


def seat_anchor(centre, axis, forza_radius) -> tuple[float, float, float]:
    """Where the driver's seat holds the wheel (patch_seats): the point whose
    vanilla grips land on Forza's rim, as the hinge and marker were placed up to
    2026-10-26 (grip_shift on the rim scaled the way it was then: by 0.8 to 1.3
    where that shift left the grips over 5 mm off). Kept so fixing the hinge
    doesn't move the driver model: the head room stays. The chase views' wheel
    keeps Forza's size; dsfforza.asi [hands] puts the hands on its rim at the
    angle the animations give them about this point, so they turn with the
    wheel about its hub (research/cockpit.md, "Hands in the chase views")."""
    axis = _norm(axis)
    k = 1.0
    for _ in range(6):
        pivot = _add(centre, grip_shift(centre, axis, forza_radius * k))
        if hand_grip_error(pivot, centre, axis, forza_radius * k) <= SEAT_FIT_TOLERANCE:
            break
        reach = []
        for g in hand_grips(pivot):
            d = _sub(g, centre)
            d = _sub(d, _scale(axis, _dot(d, axis)))
            reach.append(math.sqrt(_dot(d, d)))
        new = min(SEAT_SCALE_RANGE[1], max(SEAT_SCALE_RANGE[0], sum(reach) / len(reach) / forza_radius))
        if abs(new - k) < 1e-4:
            break
        k = new
    return _add(centre, grip_shift(centre, axis, forza_radius * k))


def hand_grip_error(pivot, centre, axis, radius) -> float:
    """Largest distance (m) from a vanilla grip to the Forza rim."""
    return max(math.dist(g, _rim_nearest(g, centre, _norm(axis), radius)) for g in hand_grips(pivot))


def hand_params(steering: dict | None = None) -> dict[int, float]:
    """VO3D p538/p559 (the hand rig's offset from the marker) to zero: they move
    the wheel with the hands, away from where Forza puts it (Cuda host:
    -4 cm y, +7.5 cm z). With patch_hierarchy's steering report, also the rim
    dsfforza.asi [hands] puts the driver's hands on (GRIP_PARAMS)."""
    params = dict.fromkeys(HAND_PARAMS, 0.0)
    if steering:
        params.update(grip_params(steering))
    return params


# dsfforza.asi [hands] (tools/asi/src/mod_hands.cpp) moves the driver's hands onto each
# car's rim (research/cockpit.md, "Hands on Forza's rim"). It reads the rim from VO3D
# parameters that nothing in the game uses: type 3 ("unused") in the parameter type table
# at 0x00F3E450, no displacement or immediate reference in Driver.exe at either base
# (spec + 0x2C70, block + 0x2960), set by no vanilla VO3D. Vanilla cars leave them at 0 and
# keep the vanilla grips.
GRIP_RADIUS_PARAM = 95  # the drawn rim's radius (m)
GRIP_ALONG_PARAM = 87  # the rim centre from the steering marker along the engine's column direction (m)
GRIP_PARAMS = (GRIP_RADIUS_PARAM, GRIP_ALONG_PARAM)


def grip_params(steering: dict) -> dict[int, float]:
    """VO3D values for dsfforza.asi [hands] from patch_hierarchy's steering report:
    the rim's radius and its centre's offset from the marker along the column (the
    marker sits on the hub's column, hand_shift). The engine's column direction
    (spec + 0x37F0) is the marker quaternion's +z: away from the driver, the
    opposite of the report's axis. The centre is the marker minus the shift."""
    shift = steering["hands"]["shift"]
    axis = _norm(steering["axis"])
    return {GRIP_RADIUS_PARAM: float(steering["radius"]), GRIP_ALONG_PARAM: _dot(shift, axis)}


def patch_hierarchy(upvh_data: bytes, host: HostCockpit, place, cf: CockpitFit) -> tuple[bytes, dict]:
    """Move the steering hinge and marker, the gauges and the mirrors to the
    Forza cockpit (all in DSF model space through `place`). The hinge and
    marker sit at the wheel centre plus hand_shift (along the column, so the
    wheel turns about its own hub); dsfforza.asi [hands] moves the vanilla hands
    onto the Forza rim (hand_params in VO3D)."""
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    data = bytearray(upvh_data)
    report = {}
    centre = place.point(cf.wheel.centre)
    axis = _norm(place.normal(cf.wheel.axis))
    hinge_at = h.offsets["hinges"] + host.wheel_hinge * 0x30
    old_axis = struct.unpack_from("<3f", data, hinge_at + 0x10)
    if _dot(axis, old_axis) < 0:
        axis = _scale(axis, -1)
    shift = hand_shift(centre, axis, cf.wheel.radius)
    pivot = _add(centre, shift)
    struct.pack_into("<3f", data, hinge_at, *pivot)
    struct.pack_into("<3f", data, hinge_at + 0x10, *axis)
    # The vanilla marker quaternion turns (0, 0, -1) onto the column (Desanne: 24.4 deg about x);
    # the engine turns the wheel and hands about it.
    q = quat_from_to((0, 0, -1), axis)
    marker_at = h.offsets["markers"] + host.wheel_marker * 0x20
    struct.pack_into("<3f", data, marker_at, *pivot)
    struct.pack_into("<4f", data, marker_at + 0x10, *q)
    along = _dot(shift, axis)
    seat = seat_anchor(centre, axis, cf.wheel.radius)
    report["steering"] = {
        "record": host.wheel_record,
        "hinge": host.wheel_hinge,
        "marker": host.wheel_marker,
        "centre": [round(c, 4) for c in centre],
        "axis": [round(c, 4) for c in axis],
        "radius": round(cf.wheel.radius, 4),
        "skin": list(host.wheel_skin),
        "seat_anchor": [round(c, 5) for c in seat],  # patch_seats: where the driver model holds the wheel
        "hands": {
            "shift": [round(c, 4) for c in shift],
            "along_column": round(along, 4),
            "in_plane": round(math.sqrt(max(0.0, _dot(shift, shift) - along * along)), 4),
            "grip_error": [
                round(hand_grip_error(centre, centre, axis, cf.wheel.radius), 4),
                round(hand_grip_error(pivot, centre, axis, cf.wheel.radius), 4),
            ],
        },
    }
    moved = []
    for family, gid in GAUGE_IDS.items():
        g = cf.gauges.get(family)
        if g is None or gid not in host.gauge_slots:
            continue
        at = h.offsets["seats"] + host.gauge_slots[gid] * 0x30
        pivot = place.point(g.pivot)
        normal = _norm(place.normal(g.normal))
        cal = cf.calibration.get(family)
        if cal is not None and cal.reverse:
            normal = _scale(normal, -1)  # vanilla counter-clockwise needles (Pacer, Aston tachs) do this
        # Vanilla gauge quaternions turn (0, 0, 1) onto the dial normal (Desanne: 21.8 deg about x);
        # the engine turns the needle right-handed about it.
        struct.pack_into("<3f", data, at, *pivot)
        struct.pack_into("<4f", data, at + 0x10, *quat_from_to((0, 0, 1), normal))
        old_sweep = struct.unpack_from("<I", data, at + 0x24)[0]
        if cal is not None:
            struct.pack_into("<I", data, at + 0x24, cal.sweep)
        moved.append(pivot)
        entry = {
            "id": gid,
            "pivot": [round(c, 4) for c in pivot],
            "normal": [round(c, 4) for c in normal],
            "forza_sweep_deg": round(g.sweep_deg, 1),
            "host_sweep": old_sweep,
        }
        if cal is not None:
            entry.update(
                {
                    "sweep": cal.sweep,
                    "unit": GAUGE_UNITS[family],
                    "reverse": cal.reverse,
                    "rest_deg": round(cal.rest_deg, 2),
                    "dial_max": cal.dial_max,
                    "residual_deg": cal.residual_deg,
                    "source": cal.source,
                }
            )
        report.setdefault("gauges", {})[family] = entry
    if moved and host.gauge_parent_hinge is not None:
        at = h.offsets["hinges"] + host.gauge_parent_hinge * 0x30
        struct.pack_into("<3f", data, at, *_mean(moved))
    for slot in host.mirror_slots:
        at = h.offsets["mirrors"] + slot * 0x10
        x = struct.unpack_from("<f", data, at)[0]
        # DSF +x is the driver's left (Forza -x after the turn).
        side = "left" if x > 0 else "right"
        if side in cf.mirrors:
            struct.pack_into("<3f", data, at, *place.point(cf.mirrors[side]))
            report.setdefault("mirrors", {})[side] = [round(c, 4) for c in place.point(cf.mirrors[side])]
    return bytes(data), report


DRIVER_SEAT_ROLE, PASSENGER_SEAT_ROLE = 10, 11
# The driver model in the chase views is one animated rig placed on the driver seat
# marker (role 10), hands included: they hold the wheel only where the vanilla wheel is
# in the seat's frame (research/cockpit.md, "The driver's seat follows the wheel").
# 138 of 147 vanilla cars put the steering marker (role 47) at this point of the seat
# marker's frame within 1 cm, all but two within 2 cm (the seat tilt, 0 or -5 deg,
# turns the frame; `dsff driver_survey vanilla`).
VANILLA_MARKER_FROM_SEAT = (0.0, 0.469, 0.428)
# Head-camera eye in the seat marker's frame, vanilla median (y 0.642..0.841, z -0.286..0.129):
# about where the driver model's eyes are.
FLEET_EYE_ABOVE_SEAT = (0.0, 0.738, -0.069)


def seat_markers(upvh_data: bytes) -> dict[int, int]:
    """Role (10 driver, 11 passenger) -> marker table index."""
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    out = {}
    for r in h.records:
        if r.type == 3 and r.marker >= 0 and r.role in (DRIVER_SEAT_ROLE, PASSENGER_SEAT_ROLE):
            out.setdefault(r.role, r.marker)
    return out


def seat_for_marker(marker, seat_quat) -> tuple[float, float, float]:
    """The driver seat position that puts the steering marker where vanilla cars have
    it in the seat's frame (VANILLA_MARKER_FROM_SEAT), for a seat turned by seat_quat."""
    return _sub(marker, quat_rotate(seat_quat, VANILLA_MARKER_FROM_SEAT))


def patch_seats(upvh_data: bytes, eye_dsf=None, anchor=None) -> tuple[bytes, dict]:
    """Move the driver's seat marker (role 10, where the game seats the driver
    model) so `anchor` (default: the steering marker) sits where the steering
    marker does in vanilla cars, in the seat's frame (VANILLA_MARKER_FROM_SEAT), and
    the passenger's (role 11) to the mirrored x at the same height and depth.
    patch_hierarchy reports the anchor (steering.seat_anchor, seat_anchor()). Call after patch_hierarchy with VO3D
    p538/p559 at zero (hand_params). The marker
    quaternions (seat tilt) stay the host's. `eye_dsf` (the head camera) only goes
    into the report, against the driver model's eyes."""
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    slots = seat_markers(upvh_data)
    data = bytearray(upvh_data)
    report = {}
    if DRIVER_SEAT_ROLE not in slots:
        return bytes(data), report
    rec = next(r for r in h.records if r.type == 3 and r.role == STEERING_MARKER_ROLE and r.marker >= 0)
    marker = anchor or struct.unpack_from("<3f", h.table("markers")[rec.marker])
    driver_at = h.offsets["markers"] + slots[DRIVER_SEAT_ROLE] * 0x20
    quat = struct.unpack_from("<4f", data, driver_at + 0x10)
    seat = seat_for_marker(marker, quat)
    for role, pos in ((DRIVER_SEAT_ROLE, seat), (PASSENGER_SEAT_ROLE, (-seat[0], seat[1], seat[2]))):
        if role in slots:
            at = h.offsets["markers"] + slots[role] * 0x20
            report["driver" if role == DRIVER_SEAT_ROLE else "passenger"] = {
                "from": [round(c, 4) for c in struct.unpack_from("<3f", data, at)],
                "to": [round(c, 4) for c in pos],
            }
            struct.pack_into("<3f", data, at, *pos)
    body_eye = _add(seat, quat_rotate(quat, FLEET_EYE_ABOVE_SEAT))
    report["body_eye"] = [round(c, 4) for c in body_eye]
    if eye_dsf is not None:
        report["body_eye_from_camera"] = [round(b - e, 4) for b, e in zip(body_eye, eye_dsf)]
    return bytes(data), report


# Vanilla head cameras see the steering rim's top this far below eye level
# (median of 146 cars, out/eye/wheel_view.py; quartiles 7.0 to 9.0, Desanne
# 13.7). Forza's CamCockpitOffset sits lower (370Z 6.5, M5 3.3, Skyline 1.9),
# so with DSF's field of view the rim fills the lower half of the screen.
RIM_DROP_DEG = 7.9


def camera_eye(eye_dsf, wheel_points_dsf, sight=None) -> tuple[float, float, float]:
    """The head-camera position: the Forza eye, raised (never lowered) until the
    steering wheel's topmost vertex is RIM_DROP_DEG below it, measured the way
    out/eye/wheel_view.py measures vanilla cars. The driver's seat stays under
    the Forza eye, so the driver model keeps its headroom.

    `sight(rise)` (GaugeSight) is the share of the needles' sweep the driver
    sees from the eye raised by `rise`: the raise stops in CAMERA_RISE_STEP
    steps before the cluster's cowl hides more than GAUGE_SIGHT_LOSS of what
    the Forza eye sees (research/cockpit.md, "Gauges under the cowl")."""
    top = max(wheel_points_dsf, key=lambda p: p[1])
    dist = abs(eye_dsf[2] - top[2])
    y = max(eye_dsf[1], top[1] + math.tan(math.radians(RIM_DROP_DEG)) * dist)
    rise = y - eye_dsf[1]
    if sight is not None and rise > 0:
        floor = sight(0.0) - GAUGE_SIGHT_LOSS
        steps = math.ceil(rise / CAMERA_RISE_STEP)
        allowed = 0.0
        for k in range(1, steps + 1):
            r = rise * k / steps
            if sight(r) < floor:
                break
            allowed = r
        y = eye_dsf[1] + allowed
    return (eye_dsf[0], y, eye_dsf[2])


CAMERA_RISE_STEP = 0.005  # m
GAUGE_SIGHT_LOSS = 0.05  # share of the needle sweep samples the raise may hide
GAUGE_SIGHT_STEPS = 12  # samples along each needle's sweep (13 angles)
GAUGE_SIGHT_CLEARANCE = 0.006  # m: occluders this close in front of a sample are the dial itself


def gauge_sight_points(cf: CockpitFit) -> list[tuple]:
    """Forza points the driver has to see to read the speedometer and
    tachometer: the needle's tip and a point 60% of the way out, at
    GAUGE_SIGHT_STEPS + 1 angles over the needle's sweep."""
    out = []
    for family, g in cf.gauges.items():
        if family not in GAUGE_IDS:
            continue
        pts = section_points(g.rest)
        if not pts:
            continue

        def in_plane(p, g=g):
            d = _sub(p, g.pivot)
            return _sub(d, _scale(g.normal, _dot(d, g.normal)))

        tip = max(pts, key=lambda p: _dot(in_plane(p), in_plane(p)))
        inner = _add(g.pivot, _scale(_sub(tip, g.pivot), 0.6))
        for k in range(GAUGE_SIGHT_STEPS + 1):
            deg = g.sweep_deg * k / GAUGE_SIGHT_STEPS
            out += [rotate_about(tip, g.pivot, g.normal, deg), rotate_about(inner, g.pivot, g.normal, deg)]
    return out


class GaugeSight:
    """Share of `points` (Forza space) seen from an eye: a ray from the eye to
    each point that no opaque LOD-0 triangle of `sections` stops short of
    GAUGE_SIGHT_CLEARANCE before the point. Glass doesn't stop it."""

    def __init__(self, sections: list[carbin.Section], points: list[tuple]):
        import numpy as np

        tris = []
        for s in sections:
            pool = None
            for sub in s.subsections:
                if sub.lod != 0 or fm.classify_cockpit(sub.material)[0] in (fm.GLASS, fm.SKIP):
                    continue
                if pool is None:
                    pool = np.array([v.position for v in s.lod0_vertices], dtype=np.float64).reshape(-1, 3)
                tris.append(pool[np.array(sub.triangles(), dtype=np.int64).reshape(-1, 3)])
        self.tris = np.concatenate(tris) if tris else np.zeros((0, 3, 3))
        self.points = np.array(points, dtype=np.float64).reshape(-1, 3)
        self.e1 = self.tris[:, 1] - self.tris[:, 0]
        self.e2 = self.tris[:, 2] - self.tris[:, 0]
        self.lo, self.hi = self.tris.min(axis=1), self.tris.max(axis=1)

    def __call__(self, eye) -> float:
        return self.fraction(eye, self.points)

    def fraction(self, eye, points) -> float:
        """Share of `points` seen from `eye` (1 for no points)."""
        import numpy as np

        points = np.array(points, dtype=np.float64).reshape(-1, 3)
        if not len(points):
            return 1.0
        eye = np.array(eye, dtype=np.float64)
        p0, e1, e2 = self.tris[:, 0], self.e1, self.e2
        seen = 0
        for p in points:
            d = p - eye
            length = float(np.sqrt(d @ d))
            d = d / length
            # Only triangles in the box between the eye and the point can stop the ray.
            lo, hi = np.minimum(eye, p) - 1e-4, np.maximum(eye, p) + 1e-4
            box = ((self.hi >= lo) & (self.lo <= hi)).all(axis=1)
            h = np.cross(d, e2[box])
            a = (e1[box] * h).sum(axis=1)
            ok = np.abs(a) > 1e-12
            f = np.where(ok, 1 / np.where(ok, a, 1), 0)
            sv = eye - p0[box]
            u = f * (sv * h).sum(axis=1)
            q = np.cross(sv, e1[box])
            v = f * (q @ d)
            t = f * (e2[box] * q).sum(axis=1)
            hit = ok & (u >= 0) & (u <= 1) & (v >= 0) & (u + v <= 1) & (t > 1e-4) & (t < length - GAUGE_SIGHT_CLEARANCE)
            seen += not hit.any()
        return seen / len(points)


def camera_params(eye_dsf, marker_x: float) -> dict[int, float]:
    """VO3D head camera (FUN_0088DF80 case 0x14): eye = (marker.x - p430, p431, -p432)."""
    return {430: marker_x - eye_dsf[0], 431: eye_dsf[1], 432: -eye_dsf[2]}


def patch_vo3d(vo3d_data: bytes, params: dict[int, float]) -> bytes:
    from dsfforza.build import handling

    v = handling.VO3D.parse(vo3d_data)
    v.params.update(params)
    return v.build()


def head_camera(upvh_data: bytes, vo3d_data: bytes) -> tuple[tuple, tuple]:
    """(eye, steering marker) in DSF model space, as the engine computes them."""
    from dsfforza.build import handling
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    marker = next(r for r in h.records if r.type == 3 and r.role == STEERING_MARKER_ROLE and r.marker >= 0)
    m = struct.unpack_from("<3f", h.table("markers")[marker.marker])
    v = handling.VO3D.parse(vo3d_data)
    return (m[0] - v.get(430), v.get(431), -v.get(432)), m


# --- the interior entry ---------------------------------------------------------------


@dataclass
class InteriorMaterials:
    textured: bytes  # local "Interior" material (samples the interior TEXC)
    needles: bytes | None  # local "Dials" material, stride 20, alpha-tested
    texture: bytes  # interior TEXC UID
    shared: dict  # role -> material UID (paint, glass, trim)
    layout: dict  # material -> (stride, MODL flag), from the host
    needle_part: int | None
    cabin_part: int


def local_material_kind(info: dict) -> str | None:
    """'dials' or 'interior' for an interior MACO (dsf_materials.material_info), by its
    MATX name or alpha test, else None."""
    base = info["path"].lower().replace("\\", "/").rsplit("/", 1)[-1]
    if "_dials" in base or "g_hAlphaTestRefValue" in info["params"]:
        return "dials"
    return "interior" if "_interior" in base else None


def host_interior(stream, interior, known: dict | None = None) -> InteriorMaterials:
    """Read the host interior's materials, strides and MODL flags. known: the
    conversion's material layouts (forza2dsf.Builder.layout; default
    forza2dsf.MATERIAL_LAYOUT), for shared materials the interior doesn't use."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.convert import forza2dsf
    from dsfforza.formats.vehicle_mesh import child

    drsp = child(child(interior, "MDXN"), "DRSP")
    local, texc = {}, {}
    for e in drsp.children:
        if e.tag == "MACO":
            info = dm.material_info(stream, e)
            kind = local_material_kind(info)
            if kind and kind not in local:
                local[kind] = info
        elif e.tag == "TEXC":
            texc[dm.current(stream, child(e, "UIDC"))[:8]] = e
    if "interior" not in local:
        raise ValueError("host interior has no _Interior material")
    # Strides and flags per material, and which part holds the needles.
    subs, strides = {}, {}
    for e in drsp.children:
        if e.tag == "SBMC":
            d = dm.current(stream, child(e, "SBBH"))
            subs[d[:8]] = (d[8:16], d[16:24], struct.unpack_from("<I", d, 0x3C)[0])
        elif e.tag == "VXCK":
            d = dm.current(stream, child(e, "VXHD"))
            strides[d[:8]] = struct.unpack_from("<I", d, 12)[0]
    modl = dm.current(stream, child(child(interior, "NONR"), "MODL"))
    parts = struct.unpack_from("<I", modl)[0]
    layout, sizes, needle_part, cabin_part = {}, {}, None, 0
    for p in range(parts):
        count, offset = struct.unpack_from("<II", modl, 0x10 + p * 0x60 + 0x48)
        for k in range(count):
            _mask, n, uid_off, extra_off = struct.unpack_from("<4I", modl, offset + k * 16)
            for i in range(n):
                uid = modl[uid_off + i * 8 : uid_off + i * 8 + 8]
                flag = struct.unpack_from("<I", modl, extra_off + i * 4)[0]
                mat, vb, indices = subs[uid]
                layout.setdefault(mat, (strides[vb], flag))
                sizes[mat] = sizes.get(mat, 0) + indices
                if local.get("dials") and mat == local["dials"]["uid"]:
                    needle_part = p
    shared_used = [m for m in layout if m not in {i["uid"] for i in local.values()}]

    def pick(stride: int, flag: int | None, fallback: bytes) -> bytes:
        found = [m for m in shared_used if layout[m][0] == stride and (flag is None or layout[m][1] == flag)]
        return max(found, key=lambda m: sizes[m]) if found else fallback

    glass = pick(28, 7, forza2dsf.GLASS)
    # Every vanilla interior paints with INTERIOR_PAINT; picking the largest
    # stride-32 material instead took GLOBAL_LOD_Chrome (a012f01306d57a2a) on
    # hosts that use more chrome than paint (193, the Mustang's host).
    paint = INTERIOR_PAINT if INTERIOR_PAINT in shared_used else pick(32, 5, forza2dsf.PAINT)
    # Without the engine trim the largest stride-28 material can be a chassis
    # one (193: Chassis_LOD1); keep the engine trim, MATERIAL_LAYOUT has its layout.
    trim = forza2dsf.TRIM
    # Outside chrome seen from the seat (A-pillar trim, window frames, bonnet strips): the
    # shared chrome vanilla interiors use (host 193: 3,700 triangles), issue #232.
    chrome = forza2dsf.CHROME
    for uid in (glass, paint, trim, chrome):
        layout.setdefault(uid, (forza2dsf.MATERIAL_LAYOUT if known is None else known).get(uid, (28, 5)))
    tex = next(iter(local["interior"]["textures"]))
    if tex not in texc:
        raise ValueError(f"interior texture {tex.hex()} is not in the interior entry")
    return InteriorMaterials(
        local["interior"]["uid"],
        local["dials"]["uid"] if "dials" in local else None,
        tex,
        {"paint": paint, "glass": glass, "trim": trim, "chrome": chrome, "tyre": None, "skip": None},
        layout,
        needle_part,
        cabin_part,
    )


def _needle_vertex(pos, uv, gauge_id: int, normal, stride: int = 20, comp: int = 0) -> bytes:
    """Host needle layout: s0 4 x i16, s8 4 x f16 (u, v, gauge id, comp), then at
    stride 20 s5 4 x u8 (the normal). Hosts 250 and 293 draw their Dials
    material from a stride-16 buffer without the normal (145 vanilla interiors
    use 20)."""
    from dsfforza.convert import forza2dsf

    q = [max(-32768, min(32767, round(c * forza2dsf.POSITION_SCALE))) for c in pos]
    head = struct.pack("<4h4e", q[0], q[1], q[2], 4096, uv[0], uv[1], float(gauge_id), float(comp))
    if stride == 16:
        return head
    p0, p1 = forza2dsf.spherical(normal)
    return head + struct.pack("<4B", p0, p1, 128, 128)


# Gloss materials (copies of the car's Lights material, about 11 KB each) the interior
# entry takes at most for the outside parts it holds (issue #232).
COCKPIT_GLOSS_MATERIALS = 6
# Vertex-cluster cells (m) tried in turn until the interior MDXN fits.
DECIMATE_LADDER = (0.0, 0.004, 0.007, 0.01, 0.015, 0.02, 0.03)
# Largest vanilla interior MDXN (Sports Coupe 10); port_car enforces the same.
INTERIOR_MDXN_LIMIT = 0x12D000


@dataclass
class CockpitOptions:
    shell_lod: int = 5  # main-carbin LOD for the outer body seen through the windows
    shell: bool = True
    headlights: bool = False  # the cockpit carbin's headlight sections (lights_LOD0)
    atlas_size: tuple[int, int] | None = None
    # DXT1 halves the atlas (vanilla interiors ship DXT1 textures too); the
    # Interior material then loses its alpha specular mask, and needle cells
    # stay opaque for the alpha-tested Dials material.
    atlas_format: str = "DXT1"
    # interior_LOD0 holds the whole cabin (trims, dials, labels); nodamage_LOD0
    # only the outside parts seen through the windows (A-pillars, mirrors,
    # bonnet), so the cabin gets four times the texel density.
    atlas_weights: dict = field(default_factory=lambda: {fm.NODAMAGE: 1.0, fm.LIGHTS: 1.0, fm.INTERIOR: 4.0})
    # Lift, tone and filler apply to nodamage_LOD0 only, which in the cockpit
    # atlas holds just the outside parts seen through the windows (A-pillars,
    # mirrors, bonnet; fm.COCKPIT_EXTERIOR_SECTIONS). Cabin trims come from
    # interior_LOD0 in their own colours, unchanged: dark cabins come out at
    # luminance 25 to 60, the Desanne's interior texels have median 44. The
    # three were tuned when cabin trims were wrongly read from nodamage_LOD0
    # (research/cockpit.md, "Cabin trims sample interior_LOD0").
    # Floor for nodamage_LOD0 trim texels (near-black swatches).
    trim_lift: int = 40
    # nodamage_LOD0 (brightness, saturation) after the lift.
    trim_tone: tuple = (0.85, 0.55)
    # Source RGB for nodamage_LOD0 texels with alpha < 16, before lift and
    # tone: paint-through filler (white on most cars, a badge plate's grey on
    # the Jeep GC SRT8), not trim colour; near black like Forza's swatches.
    filler: tuple | None = (10, 10, 12)
    decimate: float = 0.0  # fixed vertex-cluster cell (m); 0 = step through DECIMATE_LADDER
    cull: bool = True  # drop triangles never seen from the head
    cull_resolution: int = 256  # cube-map face size for the visibility pass
    mdxn_limit: int = INTERIOR_MDXN_LIMIT


class CockpitBinding:
    """dsf_materials.Binding-like object for add_geometry: textured cockpit
    subsections use the interior's own material through the cockpit atlas,
    the rest the host interior's paint/glass/trim. Paint samples the shared
    paint atlas at paint_uv (the converted exterior's paint texel). On the
    outside parts (`outside(section)`: the cockpit carbin's bonnet, A-pillars and
    mirrors, and the shell), chrome takes the shared chrome and the atlas trims
    Texturing gave a gloss material take its copy of the Lights material (`gloss`:
    Texturing kind -> UID), as on the exterior (issue #232)."""

    def __init__(self, texturing, materials: InteriorMaterials, paint_uv, gloss: dict | None = None, outside=None):
        self.texturing, self.materials, self._paint_uv = texturing, materials, paint_uv
        self.gloss = gloss or {}
        self.outside = outside or (lambda section: False)

    def _flat(self, section, sub) -> bool:
        """Borrowed main-carbin sections (`section.flat`, a steering wheel
        added to a cockpit that has none) whose UVs don't fit the atlases:
        everything but paint and glass is drawn flat."""
        return getattr(section, "flat", False) and self.texturing.material(section, sub) not in (fm.PAINT, fm.GLASS)

    def material(self, section, sub, shared):
        from dsfforza.convert import dsf_materials as dm

        if sub.material.lower() in DIGITAL_TECHNIQUES:
            return self.materials.textured
        if FORCE_FLAT.match(sub.material) or self._flat(section, sub):
            return self.materials.textured
        kind = self.texturing.material(section, sub)
        if (
            kind != fm.SKIP
            and shared.get("chrome")
            and self.outside(section)
            and fm.classify(sub.material)[0] == fm.CHROME
        ):
            return shared["chrome"]  # add_geometry puts UV0 on its grey texel (forza2dsf.CHROME_UV)
        if kind in self.gloss:
            return self.gloss[kind]
        if dm.is_atlas_kind(kind) or kind == fm.TRIM:
            return self.materials.textured
        return shared.get(kind)

    def triangle_uvs(self, section, sub, triangle_index, raw_uvs):
        if sub.material.lower() in DIGITAL_TECHNIQUES:
            key = ("display",) + getattr(sub, "display_rgba", DISPLAY_COLOUR)
            return [self.texturing.solid_uv(key)] * len(raw_uvs), key
        if (
            FORCE_FLAT.match(sub.material)
            or self._flat(section, sub)
            or (self.texturing.material(section, sub) == fm.TRIM and not self._chrome(section, sub))
        ):
            key = ("flat",) + flat_colour(sub.material)
            return [self.texturing.solid_uv(key)] * len(raw_uvs), key
        return self.texturing.triangle_uvs(section, sub, triangle_index, raw_uvs)

    def _chrome(self, section, sub) -> bool:
        return self.outside(section) and fm.classify(sub.material)[0] == fm.CHROME

    @property
    def paint_uv(self):
        return self._paint_uv


def decimate_section(section: carbin.Section, cell: float) -> carbin.Section:
    """Vertex clustering: snap LOD0 vertices to a `cell` grid per (subsection
    material, rounded normal), keep one representative each, drop collapsed
    triangles. Crude, but keeps UVs per material."""
    import copy

    if cell <= 0:
        return section
    out = copy.copy(section)
    pool = section.lod0_vertices
    new_pool, subs = [], []
    for sub in section.subsections:
        if sub.lod != 0:
            subs.append(sub)
            continue
        reps, tris = {}, []
        for t in sub.triangles():
            ids = []
            for i in t:
                v = pool[i]
                key = (tuple(round(c / cell) for c in v.position), tuple(round(c * 2) for c in v.normal))
                if key not in reps:
                    reps[key] = len(new_pool)
                    new_pool.append(v)
                ids.append(reps[key])
            if len(set(ids)) == 3:
                tris.append(tuple(ids))
        s2 = copy.copy(sub)
        s2.index_type = 4
        s2.indices = [i for t in tris for i in t]
        subs.append(s2)
    out.lod0_vertices = new_pool
    out.subsections = subs
    return out


CUBE_FACES = [  # (axis, sign): view direction of each cube-map face
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (2, 1),
    (2, -1),
]


def _ray_hit(origin, direction, tri) -> float | None:
    """Distance along a unit ray to a triangle (Moller-Trumbore), or None."""
    p0, p1, p2 = tri
    e1, e2 = _sub(p1, p0), _sub(p2, p0)
    h = _cross(direction, e2)
    a = _dot(e1, h)
    if abs(a) < 1e-12:
        return None
    f = 1 / a
    sv = _sub(origin, p0)
    u = f * _dot(sv, h)
    if u < 0 or u > 1:
        return None
    q = _cross(sv, e1)
    v = f * _dot(direction, q)
    if v < 0 or u + v > 1:
        return None
    t = f * _dot(e2, q)
    return t if t > 0 else None


# Rays from the eye (Forza space, front -z) that must reach the windscreen.
# A 3 x 3 fan, 0 to 11 deg up and 17 deg either side: single rays catch the
# cowl, the frit band or a sun strip (black_glass) on cars whose windscreen is open.
WINDSCREEN_RAYS = [_norm((x, y, -1.0)) for x in (-0.3, 0.0, 0.3) for y in (0.0, 0.1, 0.2)]


def shell_blocks_view(shell: list[carbin.Section], lod: int, eye, reach: float = 2.5) -> bool:
    """Does the shell at `lod` close the windscreen (two thirds of the rays
    from the eye stopped within `reach`)? Some main carbins close the window
    openings at their coarsest LOD (FH1 Dodge Durango, Chevrolet Express:
    LOD 5 body)."""
    tris = [
        [s.vertices_for(sub)[i].position for i in t]
        for s in shell
        for sub in s.subsections
        if sub.lod == lod and fm.classify(sub.material)[0] != fm.GLASS
        for t in sub.triangles()
    ]
    stopped = sum(
        1 for ray in WINDSCREEN_RAYS if any((d := _ray_hit(eye, ray, tri)) is not None and d < reach for tri in tris)
    )
    return stopped * 3 >= len(WINDSCREEN_RAYS) * 2


def wheel_targets(wheel: Wheel) -> list[tuple]:
    """The steering wheel's hub and eight points around its rim (Forza space)."""
    a = wheel.axis
    t = _norm(_cross(a, (1.0, 0.0, 0.0)) if abs(a[0]) < 0.9 else _cross(a, (0.0, 1.0, 0.0)))
    b = _cross(a, t)
    rim = [
        tuple(wheel.centre[k] + wheel.radius * (math.cos(th) * t[k] + math.sin(th) * b[k]) for k in range(3))
        for th in (i * math.pi / 4 for i in range(8))
    ]
    return [wheel.centre] + rim


def shell_lids_cockpit(shell: list[carbin.Section], lod: int, eye, wheel: Wheel, margin: float = 0.02) -> int:
    """How many rays from the eye to the steering wheel (wheel_targets) the
    shell at `lod` stops before they reach it, glass included. Open cars' main
    carbins close the cockpit with a lid at their coarsest LOD (Audi R10, R15:
    a flat body panel at the height of the wheel's top, issue #157), which the
    windscreen rays pass over."""
    targets = wheel_targets(wheel)
    lo = [min(p[k] for p in [eye, *targets]) for k in range(3)]
    hi = [max(p[k] for p in [eye, *targets]) for k in range(3)]
    tris = [
        tri
        for s in shell
        for sub in s.subsections
        if sub.lod == lod
        for t in sub.triangles()
        for tri in [[s.vertices_for(sub)[i].position for i in t]]
        if all(min(p[k] for p in tri) <= hi[k] and max(p[k] for p in tri) >= lo[k] for k in range(3))
    ]
    stopped = 0
    for p in targets:
        d = _sub(p, eye)
        length = math.sqrt(_dot(d, d))
        ray = _scale(d, 1 / length)
        if any((h := _ray_hit(eye, ray, tri)) is not None and h < length - margin for tri in tris):
            stopped += 1
    return stopped


LID_RAYS = 3  # eye-to-wheel rays (of 9) a shell may stop before it counts as a lid


def shell_closes(shell: list[carbin.Section], lod: int, cf: CockpitFit) -> str | None:
    """What the shell at `lod` closes from the eye: "windscreen"
    (shell_blocks_view), "cockpit" (shell_lids_cockpit) or None."""
    if shell_blocks_view(shell, lod, cf.eye):
        return "windscreen"
    if shell_lids_cockpit(shell, lod, cf.eye, cf.wheel) >= LID_RAYS:
        return "cockpit"
    return None


NEAR_PLANE = 0.01  # m, cube-face near plane for the visibility pass
# Head-movement volume for the visibility pass (Forza space, m): DSF's cockpit
# camera bobs, leans and looks around (holes in the IS F and M3 when only
# the Forza eye and 8 cm either side were used, below the raised camera).
CULL_OFFSETS = [
    (0.0, 0.0, 0.0),
    (0.12, 0.0, 0.0),
    (-0.12, 0.0, 0.0),
    (0.0, 0.08, 0.0),
    (0.0, -0.06, 0.0),
    (0.0, 0.0, 0.08),
    (0.0, 0.0, -0.08),
]


def _clip_near(poly: list[tuple], near: float = NEAR_PLANE) -> list[tuple]:
    """Clip a polygon of (u, v, z) points to z >= near (Sutherland-Hodgman, one plane)."""
    out = []
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        ina, inb = a[2] >= near, b[2] >= near
        if ina:
            out.append(a)
        if ina != inb:
            t = (near - a[2]) / (b[2] - a[2])
            out.append(tuple(a[k] + (b[k] - a[k]) * t for k in range(3)))
    return out


def _face_parts(r, axis: int, sign: int, half: float) -> list[list[tuple]]:
    """A triangle relative to the eye, projected on the cube-map face looking
    along `sign` x `axis`: screen triangles of (x, y, 1 / z), clipped at
    NEAR_PLANE where it crosses it; none if it lies behind."""
    u_axis, v_axis = [a for a in range(3) if a != axis]
    face = [(p[u_axis], p[v_axis], sign * p[axis]) for p in r]
    if max(z for _, _, z in face) < NEAR_PLANE:
        return []
    if min(z for _, _, z in face) < NEAR_PLANE:
        poly = _clip_near(face)
        parts = [(poly[0], poly[i], poly[i + 1]) for i in range(1, len(poly) - 1)]
    else:
        parts = [face]
    return [[(half + half * u / z, half - half * v / z, 1 / z) for u, v, z in part] for part in parts]


def _screen_parts(rel, axis: int, sign: int, half: float, skip=None):
    """_face_parts for many triangles at once: rel (T, 3, 3) eye-relative points.
    Returns (parts (P, 3, 3) of screen (x, y, 1 / z), owner (P,) triangle index),
    ordered by triangle, then part. The same float operations as _face_parts; the
    triangles crossing the near plane are clipped as _clip_near does (_clip_near_many)."""
    import numpy as np

    u_axis, v_axis = [a for a in range(3) if a != axis]
    z = rel[:, :, axis] * sign
    z0, z1, z2 = z[:, 0], z[:, 1], z[:, 2]
    live = np.maximum(np.maximum(z0, z1), z2) >= NEAR_PLANE
    if skip is not None:
        live &= ~skip
    crossing = live & (np.minimum(np.minimum(z0, z1), z2) < NEAR_PLANE)
    whole = np.flatnonzero(live & ~crossing)
    uvz = [np.stack((rel[whole, :, u_axis], rel[whole, :, v_axis], z[whole]), axis=-1)]
    owner = [whole]
    cross = np.flatnonzero(crossing)
    if len(cross):
        face = np.stack((rel[cross, :, u_axis], rel[cross, :, v_axis], z[cross]), axis=-1)
        clipped, which = _clip_near_many(face)
        uvz.append(clipped)
        owner.append(cross[which])
    uvz, owner = np.concatenate(uvz), np.concatenate(owner)
    # By triangle, then part: an owner's parts are consecutive and in order.
    order = np.argsort(owner, kind="stable")
    uvz, owner = uvz[order], owner[order]
    u, v, zz = uvz[..., 0], uvz[..., 1], uvz[..., 2]
    return np.stack((half + half * u / zz, half - half * v / zz, 1 / zz), axis=-1), owner


def _clip_near_many(face):
    """_clip_near and the fan of _face_parts for triangles (C, 3, 3) of (u, v, z)
    that cross the near plane (at least one vertex either side): (parts (P, 3, 3),
    index into `face` (P,)), ordered by triangle, then part. The same float
    operations: per edge the start vertex if inside, then the crossing point
    a + (b - a) * t if the edge crosses; the polygon of 3 or 4 points is fanned
    from its first point."""
    import numpy as np

    count = len(face)
    inside = face[:, :, 2] >= NEAR_PLANE
    slots = np.zeros((count, 6, 3))
    valid = np.zeros((count, 6), dtype=bool)
    for i in range(3):
        a, b = face[:, i], face[:, (i + 1) % 3]
        slots[:, 2 * i] = a
        valid[:, 2 * i] = inside[:, i]
        crosses = np.flatnonzero(inside[:, i] != inside[:, (i + 1) % 3])
        a, b = a[crosses], b[crosses]
        t = (NEAR_PLANE - a[:, 2]) / (b[:, 2] - a[:, 2])
        slots[crosses, 2 * i + 1] = a + (b - a) * t[:, None]
        valid[crosses, 2 * i + 1] = True
    # The valid slots in order: the polygon (3 or 4 points; pad the 3s with their last).
    order = np.argsort(~valid, axis=1, kind="stable")[:, :4]
    poly = np.take_along_axis(slots, order[:, :, None], axis=1)
    parts = np.stack((poly[:, [0, 1, 2]], poly[:, [0, 2, 3]]), axis=1)  # (C, 2, 3, 3)
    has = np.stack((np.ones(count, dtype=bool), valid.sum(axis=1) == 4), axis=1)  # (C, 2)
    which = np.repeat(np.arange(count), 2)[has.ravel()]
    return parts.reshape(-1, 3, 3)[has.ravel()], which


RASTER_TILE = 8  # largest tile side of _raster, in pixels


def _raster(parts, owner, resolution: int, depth, ids=None, chunk: int = 1 << 16):
    """Rasterise screen parts (from _screen_parts) into `depth` (1 / z, larger is
    nearer; a flat float array of resolution**2) and, if given, `ids` (the owner
    of the nearest part), with the per-pixel loop's float operations: a pixel
    centre inside a part (barycentric weights all >= 0) whose w is above the
    pixel's depth takes it, so among equal depths the earlier part keeps it.
    Returns (covered: the part covered a pixel centre, bbox: its pixel box isn't
    empty), one bool per part.

    The loop's result doesn't depend on the order the pixels are visited in: per
    pixel it is the largest w (if above the depth it had), from the earliest part
    with that w. So the parts' pixel boxes are cut into tiles of 1 to RASTER_TILE
    pixels a side (powers of two, the smallest that fit the box), and the tiles of
    one size are evaluated together as (tiles, rows, columns) arrays; the edge
    terms (qx - xc), (ry - yc) ... are computed per column and per row and
    broadcast, the same subtractions the loop does per pixel. Tile pixels outside
    the box are masked off. The largest w per pixel and the earliest part among
    equals are then found with ufunc.at (np.maximum.at, np.minimum.at)."""
    import numpy as np

    px, py, pw = parts[:, 0, 0], parts[:, 0, 1], parts[:, 0, 2]
    qx, qy, qw = parts[:, 1, 0], parts[:, 1, 1], parts[:, 1, 2]
    rx, ry, rw = parts[:, 2, 0], parts[:, 2, 1], parts[:, 2, 2]
    # int() truncates toward zero, as np.trunc does.
    x0 = np.maximum(np.trunc(np.minimum(np.minimum(px, qx), rx)), 0).astype(np.int64)
    x1 = np.minimum(np.trunc(np.maximum(np.maximum(px, qx), rx)) + 1, resolution).astype(np.int64)
    y0 = np.maximum(np.trunc(np.minimum(np.minimum(py, qy), ry)), 0).astype(np.int64)
    y1 = np.minimum(np.trunc(np.maximum(np.maximum(py, qy), ry)) + 1, resolution).astype(np.int64)
    bbox = (x0 < x1) & (y0 < y1)
    area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
    drawn = np.flatnonzero(bbox & ~(np.abs(area) < 1e-12))
    covered = np.zeros(len(parts), dtype=bool)
    if not len(drawn):
        return covered, bbox
    # Tile sides per part: the smallest power of two >= the box side, at most RASTER_TILE.
    sides = [1 << b for b in range(RASTER_TILE.bit_length())]
    width, height = (x1 - x0)[drawn], (y1 - y0)[drawn]
    tw, th = np.ones_like(width), np.ones_like(height)
    for side in sides[1:]:
        tw[width > side // 2] = side
        th[height > side // 2] = side
    nx, ny = -(-width // tw), -(-height // th)
    frags_k, frags_w, frags_n = [], [], []
    for side_w in sides:
        for side_h in sides:
            sel = np.flatnonzero((tw == side_w) & (th == side_h))
            if not len(sel):
                continue
            # One work item per tile: (part, tile column, tile row).
            count = nx[sel] * ny[sel]
            item_part = np.repeat(drawn[sel], count)
            local = np.arange(len(item_part), dtype=np.int64) - np.repeat(np.cumsum(count) - count, count)
            item_nx = np.repeat(nx[sel], count)
            item_x = x0[item_part] + (local % item_nx) * side_w
            item_y = y0[item_part] + (local // item_nx) * side_h
            step = max(1, chunk // (side_w * side_h))
            cols, rows = np.arange(side_w, dtype=np.int64), np.arange(side_h, dtype=np.int64)
            shift_w = side_w.bit_length() - 1
            for start in range(0, len(item_part), step):
                # Arrays are (rows, columns, tiles): the tile axis innermost and contiguous.
                p = item_part[start : start + step]
                x = cols[:, None] + item_x[start : start + step]  # (columns, tiles)
                y = rows[:, None] + item_y[start : start + step]  # (rows, tiles)
                xc, yc = x + 0.5, y + 0.5
                # Tile pixels outside the box: NaN centres, never inside (NaN >= 0 is False).
                xc[x >= x1[p]] = np.nan
                yc[y >= y1[p]] = np.nan
                qx_xc, rx_xc, px_xc = (qx[p] - xc)[None], (rx[p] - xc)[None], (px[p] - xc)[None]
                ry_yc, qy_yc, py_yc = (ry[p] - yc)[:, None], (qy[p] - yc)[:, None], (py[p] - yc)[:, None]
                a = area[p]
                w0 = qx_xc * ry_yc
                w0 -= qy_yc * rx_xc
                w0 /= a
                w1 = rx_xc * py_yc
                w1 -= ry_yc * px_xc
                w1 /= a
                w2 = np.subtract(1, w0)
                w2 -= w1
                inside = w0 >= 0
                inside &= w1 >= 0
                inside &= w2 >= 0
                f = np.flatnonzero(inside)
                if not len(f):
                    continue
                rc, t = np.divmod(f, len(p))  # f = (row * side_w + column) * tiles + tile
                n = p[t]
                covered[n] = True
                w = w0.ravel()[f] * pw[n] + w1.ravel()[f] * qw[n] + w2.ravel()[f] * rw[n]
                keep = w > 0
                rc, t, n = rc[keep], t[keep], n[keep]
                frags_k.append(y[rc >> shift_w, t] * resolution + x[rc & (side_w - 1), t])
                frags_w.append(w[keep])
                frags_n.append(n)
    if not frags_k:
        return covered, bbox
    k, w, n = np.concatenate(frags_k), np.concatenate(frags_w), np.concatenate(frags_n)
    # Per pixel the nearest fragment, the earliest part among equals; it takes the
    # pixel if it is nearer than the depth there.
    nearest = depth.copy()
    np.maximum.at(nearest, k, w)
    won = nearest > depth
    tie = (w == nearest[k]) & won[k]
    first = np.full(len(depth), len(parts), dtype=np.int64)
    np.minimum.at(first, k[tie], n[tie])
    depth[won] = nearest[won]
    if ids is not None:
        ids[won] = owner[first[won]]
    return covered, bbox


def visible_triangles(
    sections: list[carbin.Section],
    eyes: list[tuple],
    resolution: int = 256,
    near_keep: float = 0.25,
    tiny_slack: float = 0.03,
) -> dict[tuple[int, int], set[int]]:
    """Triangles of the LOD-0 subsections that are seen from any of `eyes`:
    an ID buffer per cube-map face (no back-face culling), plus every
    triangle with a vertex within `near_keep` of an eye, plus one ring of
    neighbours (triangles sharing a vertex with a visible one) so slivers
    along visible surfaces stay. Glass (fm role glass) neither occludes nor
    is culled. Triangles crossing a face's near plane are clipped, not
    skipped (large dash and floor triangles next to the head used to be
    dropped: holes in the IS F and M3); triangles too small to cover a pixel
    centre count as seen when they are within `tiny_slack` (relative depth)
    of what the pixel shows. Returns {(section index, subsection index):
    kept triangle indices}. Forza space."""
    import numpy as np

    # Per LOD-0 subsection (in order): its key, its triangles' vertex indices into
    # the section's pool and whether it is glass; points and vertex ids per triangle.
    blocks, points, vertex_ids, glass = [], [], [], []
    vertex_base = 0
    for si, s in enumerate(sections):
        pool = None
        for ui, sub in enumerate(s.subsections):
            if sub.lod != 0:
                continue
            if pool is None:
                pool = np.array([v.position for v in s.lod0_vertices], dtype=np.float64).reshape(-1, 3)
            t = np.array(sub.triangles(), dtype=np.int64).reshape(-1, 3)
            blocks.append(((si, ui), len(t)))
            points.append(pool[t])
            vertex_ids.append(t + vertex_base)
            glass.append(np.full(len(t), fm.classify_cockpit(sub.material)[0] == fm.GLASS))
        vertex_base += len(s.lod0_vertices)
    if not blocks:
        return {}
    points, vertex_ids, glass = np.concatenate(points), np.concatenate(vertex_ids), np.concatenate(glass)
    seen = glass.copy()
    half = resolution / 2
    for eye in eyes:
        rel = points - np.array(eye, dtype=np.float64)
        x, y, z = rel[..., 0], rel[..., 1], rel[..., 2]
        seen[(x * x + y * y + z * z).min(axis=1) < near_keep * near_keep] = True
        for axis, sign in CUBE_FACES:
            ids = np.full(resolution * resolution, -1, dtype=np.int64)
            depth = np.zeros(resolution * resolution)  # 1 / z, larger is nearer
            parts, owner = _screen_parts(rel, axis, sign, half, glass)
            covered, bbox = _raster(parts, owner, resolution, depth, ids)
            seen[ids[ids >= 0]] = True
            # A triangle that covered no pixel centre: the pixel under the centroid of
            # its last part whose pixel box isn't empty, at that part's nearest 1 / z.
            cx = np.trunc((parts[:, 0, 0] + parts[:, 1, 0] + parts[:, 2, 0]) / 3)
            cy = np.trunc((parts[:, 0, 1] + parts[:, 1, 1] + parts[:, 2, 1]) / 3)
            best = bbox & (cx >= 0) & (cx < resolution) & (cy >= 0) & (cy < resolution)
            hit = np.zeros(len(points), dtype=bool)
            hit[owner[covered]] = True
            n = owner[best]  # ascending: parts are ordered by triangle
            k = (cy[best] * resolution + cx[best]).astype(np.int64)
            w = parts[best, :, 2].max(axis=1)
            last = np.ones(len(n), dtype=bool)  # each triangle's last such part
            last[:-1] = n[1:] != n[:-1]
            n, k, w = n[last], k[last], w[last]
            seen[n[~hit[n] & (w >= depth[k] * (1 - tiny_slack))]] = True
    near = np.zeros(vertex_base, dtype=bool)
    near[vertex_ids[seen & ~glass].ravel()] = True
    keep = seen | near[vertex_ids].any(axis=1)
    kept: dict[tuple[int, int], set[int]] = {}
    start = 0
    for key, count in blocks:
        local = np.flatnonzero(keep[start : start + count])
        if len(local):
            kept[key] = set(local.tolist())
        start += count
    return kept


class GlassSight:
    """Which points the driver sees: a depth cube (`resolution` per face) of the
    opaque LOD-0 cockpit triangles from `eye` (Forza space). Called with a DSF
    point (`place` maps Forza to DSF), True if nothing opaque is nearer the
    eye. glass.InteriorGlass starts crack lines and places rim shards where
    the driver can see them; a Forza windscreen runs well below the dash."""

    def __init__(self, sections: list[carbin.Section], eye, place, resolution: int = 128, triangles=None):
        """`triangles` (Forza-space point triples) instead of `sections`: any opaque mesh."""
        self.eye, self.place, self.res = eye, place, resolution
        self.faces = {}
        half = resolution / 2
        import numpy as np

        tris = [np.array([[p[:3] for p in t] for t in triangles or []], dtype=np.float64).reshape(-1, 3, 3)]
        for s in sections:
            pool = None
            for sub in s.subsections:
                if sub.lod != 0 or fm.classify_cockpit(sub.material)[0] == fm.GLASS:
                    continue
                if pool is None:
                    pool = np.array([v.position for v in s.lod0_vertices], dtype=np.float64).reshape(-1, 3)
                tris.append(pool[np.array(sub.triangles(), dtype=np.int64).reshape(-1, 3)])
        rel = np.concatenate(tris) - np.array(eye[:3], dtype=np.float64)
        for axis, sign in CUBE_FACES:
            depth = np.zeros(resolution * resolution)  # 1 / z, larger is nearer
            parts, owner = _screen_parts(rel, axis, sign, half)
            _raster(parts, owner, resolution, depth)
            self.faces[(axis, sign)] = depth.tolist()

    def __call__(self, point_dsf, slack: float = 0.03) -> bool:
        # DSF -> Forza: Placement.point is (-x, y + dy, -z + dz), its own inverse up to the offsets
        p = (-point_dsf[0], point_dsf[1] - self.place.dy, self.place.dz - point_dsf[2])
        r = [p[a] - self.eye[a] for a in range(3)]
        axis = max(range(3), key=lambda a: abs(r[a]))
        sign = 1 if r[axis] > 0 else -1
        u_axis, v_axis = [a for a in range(3) if a != axis]
        z = sign * r[axis]
        if z < NEAR_PLANE:
            return True
        half = self.res / 2
        x, y = int(half + half * r[u_axis] / z), int(half - half * r[v_axis] / z)
        if not (0 <= x < self.res and 0 <= y < self.res):
            return True
        return 1 / z >= self.faces[(axis, sign)][y * self.res + x] * (1 - slack)


def cull_sections(sections: list[carbin.Section], kept: dict[tuple[int, int], set[int]]) -> list[carbin.Section]:
    """Copies of `sections` whose LOD-0 subsections hold only the kept triangles."""
    import copy

    out = []
    for si, s in enumerate(sections):
        s2 = copy.copy(s)
        subs = []
        for ui, sub in enumerate(s.subsections):
            if sub.lod != 0:
                subs.append(sub)
                continue
            keep = kept.get((si, ui), set())
            tris = [t for k, t in enumerate(sub.triangles()) if k in keep]
            if not tris:
                continue
            sub2 = copy.copy(sub)
            sub2.index_type = 4
            sub2.indices = [i for t in tris for i in t]
            subs.append(sub2)
        s2.subsections = subs
        out.append(s2)
    return out


def _needle_colour(atlases, section, sub) -> tuple[int, int, int, int]:
    if getattr(sub, "needle_rgba", None) is not None:  # a dash scene's needle (dash_needles)
        return sub.needle_rgba
    if sub.material in VIRTUAL_NEEDLE_COLOURS:
        return VIRTUAL_NEEDLE_COLOURS[sub.material]
    m = fm.resolve(section, sub, fm.classify_cockpit)
    if m.family is not None:
        try:
            r, g, b, _ = fm.footprint_colour(atlases.get(m.family), m.bounds)
            return (r, g, b, 255)
        except FileNotFoundError:
            pass
    return (200, 200, 200, 255)


def convert_interior(
    stream,
    interior,
    car,
    cf: CockpitFit,
    place,
    seed: bytes,
    paint_uv,
    opts: CockpitOptions,
    wheel_skin: bytes,
    log=print,
    glass_links: dict | None = None,
    glass_skin: dict | None = None,
    layout: dict | None = None,
    lights_template=None,
) -> dict:
    """Rebuild the interior entry (kind 1) from the Forza cockpit, in place.

    Cockpit sections (LOD 0) minus the race cage and, by default, the
    headlights; triangles never seen from the head (cube-map visibility from
    the eye and 8 cm either side, glass transparent) are dropped. The outer
    body from the main carbin at `shell_lod` fills the window views. If the
    MDXN is over `mdxn_limit`, the cockpit is vertex-clustered with growing
    cells (DECIMATE_LADDER), then the shell drops to its coarsest LOD.
    lights_template: (Lights MACO, its texture UID) of the car (forza2dsf.lights_template):
    the outside parts the cockpit holds then take Forza's specular and reflection from
    copies of it, as on the exterior (issue #232); None: no gloss."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.convert import forza2dsf
    from dsfforza.formats.chnk import entry_size
    from dsfforza.formats.vehicle_mesh import child

    # layout: the conversion's material layouts (forza2dsf.convert's), plus the interior's.
    mats = host_interior(stream, interior, layout)
    layout = {**(forza2dsf.MATERIAL_LAYOUT if layout is None else layout), **mats.layout}
    mdxn = child(interior, "MDXN")
    drsp = child(mdxn, "DRSP")
    original_children = list(drsp.children)
    texc = next(
        e for e in drsp.children if e.tag == "TEXC" and dm.current(stream, child(e, "UIDC"))[:8] == mats.texture
    )
    host_dds = dm.current(stream, child(texc, "DXTC"))
    height, width = struct.unpack_from("<II", host_dds, 12)
    mips = struct.unpack_from("<I", host_dds, 28)[0] or dm.DEFAULT_MIPS
    modl = child(child(interior, "NONR"), "MODL")
    host_modl = dm.current(stream, modl)

    static = {
        n: s
        for n, s in cf.static.items()
        if n not in SKIP_SECTIONS and (opts.headlights or not LIGHT_SECTIONS.search(n))
    }
    animated = {f: g for f, g in cf.gauges.items() if f in GAUGE_IDS and mats.needles is not None}
    # Needles without a DSF gauge stay as static geometry (level and temperature
    # gauges at half, others at rest).
    for family, poses in cf.needles.items():
        if family in animated:
            continue
        pose = poses.get("mid" if PARK_MID.match(family) else "min") or next(iter(poses.values()))
        static[pose.name] = pose
    names = list(static)
    sections = [static[n] for n in names]
    culled_report = None
    if opts.cull:
        # From the final head camera (camera_eye raises it above the Forza eye)
        # and a head-movement volume around it, plus the Forza eye itself.
        e = cf.camera or cf.eye
        eyes = [_add(e, o) for o in CULL_OFFSETS] + ([cf.eye] if cf.camera else [])
        before = sum(len(sub.triangles()) for s in sections for sub in s.subsections if sub.lod == 0)
        sections = cull_sections(sections, visible_triangles(sections, eyes, opts.cull_resolution))
        after = sum(len(sub.triangles()) for s in sections for sub in s.subsections if sub.lod == 0)
        culled_report = {"triangles_before": before, "triangles_after": after}
        log(f"cockpit visibility: {after} of {before} triangles seen from the head")

    atlases = fm.AtlasCache(car.folder, lod0=cf.lod0_atlases)
    needle_colours = {
        ("needle",) + c: c
        for g in animated.values()
        for sub in g.rest.subsections
        for c in [_needle_colour(atlases, g.rest, sub)]
    }
    needle_colours[("display",) + DISPLAY_COLOUR] = DISPLAY_COLOUR
    for s in sections:  # display faces under a virtual dial (flatten_face)
        for sub in s.subsections:
            colour = getattr(sub, "display_rgba", None)
            if colour is not None:
                needle_colours[("display",) + colour] = colour
    # The flat cells every interior's atlas has; a virtual dial's dark marks and a
    # virtual display add theirs only where they are drawn (other cars' atlases stay
    # as they were).
    used = {sub.material for s in sections for sub in s.subsections}
    flat = {
        c
        for pattern, c in FLAT_COCKPIT
        if pattern.pattern not in VIRTUAL_ONLY_FLAT or any(pattern.search(m) for m in used)
    }
    for colour in flat | {FLAT_DEFAULT}:
        needle_colours[("flat",) + colour] = colour
    families = (fm.NODAMAGE, fm.INTERIOR) + ((fm.LIGHTS,) if opts.headlights else ())
    classifier = fm.classify_cockpit
    if not cf.lod0_atlases:
        # The main carbin's cabin (stub cockpit carbin): main atlases, whose
        # dial faces are in nodamage, not interior_LOD0.
        families = (fm.NODAMAGE,) + ((fm.LIGHTS,) if opts.headlights else ())

        # Main-carbin techniques, not the cockpit_ variants: no section rule.
        def classifier(name):
            role, family = fm.classify_cockpit(name)
            return role, fm.NODAMAGE if family == fm.INTERIOR else family

    shell = []
    if opts.shell:
        skip = (
            {n.lower() for n in cf.static}
            | {n.lower() for n in cf.shell_skip}
            | {p.name.lower() for poses in cf.needles.values() for p in poses.values()}
        )
        shell = [s for s in car.body if s.name.lower() not in skip and not SHELL_SKIP.match(s.name)]
    shell_lods = sorted({sub.lod for s in shell for sub in s.subsections if sub.lod > 0})
    shell_ids = {id(s) for s in shell}

    def outside(section) -> bool:
        """The parts of the car's outside the interior entry draws: the cockpit carbin's
        bonnet, A-pillars and mirrors (fm.COCKPIT_EXTERIOR_SECTIONS), and the shell."""
        return id(section) in shell_ids or bool(fm.COCKPIT_EXTERIOR_SECTIONS.match(section.name))

    def gloss_filter(section, sub) -> bool:  # chrome takes the shared chrome (CockpitBinding)
        return outside(section) and fm.classify(sub.material)[0] != fm.CHROME

    # What the driver sees of the glass: crack lines start and rim shards sit there.
    sight = GlassSight(sections, cf.camera or cf.eye, place)
    shell_lod = opts.shell_lod
    # Step to finer shell LODs while the shell closes the windscreen or the cockpit.
    while shell and shell_lod > 1:
        closed = shell_closes(shell, max((l for l in shell_lods if l <= shell_lod), default=shell_lod), cf)
        if not closed:
            break
        log(f"cockpit shell: LOD {shell_lod} closes the {closed}; trying LOD {shell_lod - 1}")
        shell_lod -= 1
    ladder = [(cell, shell_lod) for cell in ([opts.decimate] if opts.decimate else DECIMATE_LADDER)]
    if shell_lods and max(shell_lods) > shell_lod and not shell_closes(shell, max(shell_lods), cf):
        ladder.append((ladder[-1][0], max(shell_lods)))
    attempts = []
    for cell, shell_lod in ladder:
        drsp.children = list(original_children)
        current = [decimate_section(s, cell) for s in sections] if cell > 0 else sections
        t = dm.Texturing(
            car.folder,
            size=opts.atlas_size or (width, height),
            mips=mips,
            plain=(40, 40, 42, 0),
            lod0=cf.lod0_atlases,
            classifier=classifier,
            families=families,
            weights=dict(opts.atlas_weights),
            fourcc=opts.atlas_format.encode(),
            lift={fm.NODAMAGE: opts.trim_lift} if opts.trim_lift else {},
            tone={fm.NODAMAGE: opts.trim_tone} if opts.trim_tone else {},
            filler={fm.NODAMAGE: opts.filler} if opts.filler else {},
            cutout_colour=(34, 34, 36, 0),
            gloss_enabled=lights_template is not None,
            gloss_filter=gloss_filter,
            gloss_max=COCKPIT_GLOSS_MATERIALS,
        )
        t.plan(current, {0}, extra_solids=needle_colours)
        child(texc, "DXTC").payload = t.dds(template=host_dds)
        gloss = {}
        if t.gloss_materials:
            template, template_tex = lights_template
            prefix = seed[:3] + bytes([seed[3] ^ 0x2B])
            gloss = dm.add_gloss(stream, drsp, template, t.gloss_materials, prefix, {template_tex: mats.texture})
            layout.update(dict.fromkeys(gloss.values(), dm.STRIDES[dm.LIGHTS_MAT]))
        binding = CockpitBinding(t, mats, paint_uv, gloss, outside)
        builder = forza2dsf.Builder(seed[:3] + bytes([seed[3] ^ 0x55]), layout=layout)
        cabin = forza2dsf.add_geometry(
            builder,
            current,
            0,
            place,
            wheel=False,
            textures=binding,
            merge=True,
            shared=mats.shared,
            skin={**(glass_skin or {}), "steering_wheel": wheel_skin},
            glass_links=glass_links,
            glass_rect=forza2dsf.INTERIOR_GLASS_RECT,
            glass_sight=sight,
        )
        lod = max((l for l in shell_lods if l <= shell_lod), default=shell_lods[0] if shell_lods else 0)
        if shell:
            cabin += forza2dsf.add_geometry(
                builder,
                shell,
                lod,
                place,
                wheel=False,
                textures=binding,
                merge=True,
                shared=mats.shared,
                glass_links=glass_links,
                glass_rect=forza2dsf.INTERIOR_GLASS_RECT,
                skin=glass_skin,
                glass_sight=sight,
            )
        needle_subs = []
        for family, g in animated.items():
            verts, tris, remap = [], [], {}
            pool = g.rest.lod0_vertices
            cal = cf.calibration.get(family)
            # The engine draws the rest pose at 0 rpm / 0 mph: turn the min pose there.
            turn = cal.rest_deg if cal is not None and abs(cal.rest_deg) >= 0.25 else 0.0
            for sub in g.rest.subsections:
                uv = t.solid_uv(("needle",) + _needle_colour(atlases, g.rest, sub))
                for tri in sub.triangles():
                    out = []
                    for i in tri:
                        if (i, uv) not in remap:
                            remap[(i, uv)] = len(verts)
                            v = pool[i]
                            pos, nrm = v.position, v.normal
                            if turn:
                                pos = rotate_about(pos, g.pivot, g.normal, turn)
                                nrm = rotate_about(nrm, (0.0, 0.0, 0.0), g.normal, turn)
                            stride = layout[mats.needles][0]
                            verts.append(
                                _needle_vertex(place.point(pos), uv, GAUGE_IDS[family], place.normal(nrm), stride)
                            )
                        out.append(remap[(i, uv)])
                    tris.append(tuple(out))
            if tris:
                needle_subs.append(builder.add(mats.needles, verts, tris))
        digital_report, digital_templates = None, None
        if cf.digital_plan is not None and mats.needles is not None:
            from dsfforza.convert import digital_dash as dd

            plan = cf.digital_plan
            tex_uid, mat_uid = builder.new_uid(), builder.new_uid()
            drsp.children.append(
                dm.texture_entry(stream, texc, dd.dxt5_dds(plan.width, plan.height, plan.rgba), tex_uid)
            )
            drsp.children.append(
                dm.retarget_material(stream, dd.vanilla_material(stream), {dd.VANILLA_TEXTURE: tex_uid}, mat_uid)
            )
            # The digital-dials shaders are drawn from the Volt's own buffer layout (no
            # normal): a buffer of their own, stride 16 (hosts 250 and 293 draw their
            # needles from that layout too, so they share it).
            stride = dd.VANILLA_STRIDE
            builder.layout[mat_uid] = (stride, dd.MODL_FLAG)
            digital_templates = {stride: dd.vanilla_buffer(stream)}
            verts, tris = dd.vertices(plan, place, stride)
            if tris:
                needle_subs.append(builder.add(mat_uid, verts, tris))
            digital_report = {
                **cf.digital_displays,
                "quads": len(plan.quads),
                "texture": [plan.width, plan.height],
                "material": mat_uid.hex(),
            }
        forza2dsf.rebuild_drsp(stream, drsp, builder, keep_host_materials=True, templates=digital_templates)
        part_lods = {mats.cabin_part: {INTERIOR_MASK: cabin}}
        if mats.needle_part is not None:
            part_lods[mats.needle_part] = {INTERIOR_MASK: needle_subs}
        else:
            part_lods[mats.cabin_part][INTERIOR_MASK] = cabin + needle_subs
        modl.payload = forza2dsf.rebuild_modl(host_modl, part_lods)
        size = entry_size(mdxn)
        attempts.append({"decimate": cell, "shell_lod": lod if shell else None, "mdxn": size})
        vertex_bytes = sum(len(b) for b in builder.buffers.values())
        log(
            f"interior entry: {len(builder.subs)} submodels ({len(current)} cockpit sections, {len(shell)} shell "
            f"sections at LOD {lod}, {len(needle_subs)} needles), {vertex_bytes // 1024} KiB vertices, "
            f"{len(builder.indices)} indices, cluster cell {cell * 1000:.0f} mm: MDXN {size:#x}"
            + ("" if size <= opts.mdxn_limit else f" > {opts.mdxn_limit:#x}, retrying")
        )
        if size <= opts.mdxn_limit:
            break
    log(f"interior atlas: {t.summary()}")
    if t.gloss_materials:
        log(
            "interior gloss materials (intensity/power/reflection/fresnel power/bias): "
            + "; ".join(
                f"{n}: {m.intensity:g}/{m.power:g}/{m.reflection:g}/{m.fresnel_power:g}/{m.bias:g}"
                for n, m in enumerate(t.gloss_materials)
            )
            + " for "
            + ", ".join(f"{k} {sum(v.values())}" for k, v in sorted(t.gloss_report.items()))
        )
    glass_windows = getattr(builder, "interior_glass", {})
    log(
        "interior glass: "
        + ", ".join(
            f"{g} s9 {w['s9']} {w['surface_m2']} m2"
            + (f", cracks {w['crack_m']} m, {w['shard_triangles']} shards" if "crack_m" in w else "")
            for g, w in sorted(glass_windows.items())
        )
    )
    return {
        "glass_windows": glass_windows,
        "submodels": len(builder.subs),
        "vertex_bytes": vertex_bytes,
        "indices": len(builder.indices),
        "cockpit_sections": names,
        "shell_sections": len(shell),
        "needles": sorted(animated),
        "parked_needles": sorted(f for f in cf.needles if f not in animated),
        "digital_triangles": cf.digital,
        "display_face_triangles": cf.display_faces,
        **({"virtual_gauges": cf.virtual} if cf.virtual else {}),
        **({"digital_displays": digital_report} if digital_report else {}),
        "atlas": t.summary(),
        **({"gloss": {k: sum(v.values()) for k, v in t.gloss_report.items()}} if t.gloss_materials else {}),
        "chrome_triangles": sum(b.index_count // 3 for b in builder.subs if b.material == mats.shared["chrome"]),
        "visibility": culled_report,
        "attempts": attempts,
        "decimate": attempts[-1]["decimate"],
        "shell_lod": attempts[-1]["shell_lod"],
    }


# --- previews -------------------------------------------------------------------------

GLASS_FLAG = 7  # MODL per-submodel value of glass submodels (vanilla interiors and exteriors)


def _entry_triangles(
    archive: Path,
    vehc_index: int,
    mask: int | None,
    textured: bool,
    flat=None,
    skip_glass: bool = False,
    instruments: dict | None = None,
):
    """[(pts, uvs, tex, colour)] for one VEHC entry, as dsf_materials._render wants.
    skip_glass leaves out submodels whose MODL value is GLASS_FLAG (the
    previews have no transparency). instruments: gauge id -> (pivot, axis,
    degrees) turns needle vertices (stride 20, semantic 8 = (u, v, id, 0))
    the way the engine does (gauge_pose)."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    _, mesh = vm.load(archive, vehc_index)
    textures, materials = {}, {}
    if textured:
        with archive.open("rb") as stream:
            root = read_chunk(stream, 0)
            for i in range(max(0, vehc_index - 2), min(len(root.entries), vehc_index + 3)):
                if root.entries[i].tag != "VEHC":
                    continue
                for e in child(child(root.entries[i], "MDXN"), "DRSP").children:
                    if e.tag == "TEXC":
                        textures[payload(stream, child(e, "UIDC"))[:8]] = dm.decode_dds(
                            payload(stream, child(e, "DXTC"))
                        )
                    elif e.tag == "MACO":
                        info = dm.material_info(stream, e)
                        materials[info["uid"]] = info
    subs = vm.lod_submodels(mesh, mask) if mask is not None else mesh.submodels
    flags = {n: x for part in mesh.parts for lod in part.lods for n, x in zip(lod.submodels, lod.extra)}
    out = []
    for sub in subs:
        if skip_glass and flags.get(mesh.submodels.index(sub)) == GLASS_FLAG:
            continue
        info = materials.get(sub.material)
        tex = textures.get(next(iter(info["textures"]))) if info and info["textures"] else None
        colour = flat or dm.FLAT.get(sub.material.hex(), (150, 150, 160))
        needle = instruments is not None and vb_needle(mesh, sub)
        for vb, tri in vm.triangles(mesh, sub):
            pts = [vm.position(vb, i) for i in tri]
            if needle:
                for k, i in enumerate(tri):
                    turn = instruments.get(int(round(vb.read(vb.element(8), i)[2])))
                    if turn:
                        pts[k] = rotate_about(pts[k], turn[0], turn[1], turn[2])
            uvs = [vb.read(vb.element(8), i)[:2] for i in tri] if tex else None
            out.append((pts, uvs, tex, colour))
    return out


def vb_needle(mesh, sub) -> bool:
    vb = mesh.vertex_buffers.get(sub.vertex_buffer)
    return bool(vb and vb.stride in (16, 20) and vb.element(8))


def dials_material(stream, interior) -> bytes | None:
    """UID of the interior's needle material, as host_interior picks it: its first
    local MACO named _Dials or alpha-tested. The digital dials' material (a copy of
    digital_dash.VANILLA_MATERIAL, also named _Dials) is only added after the host's
    materials, and only when the host has a needle material."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats.vehicle_mesh import child

    for e in child(child(interior, "MDXN"), "DRSP").children:
        if e.tag == "MACO":
            info = dm.material_info(stream, e)
            if local_material_kind(info) == "dials":
                return info["uid"]
    return None


def needle_gauge_ids(stream, interior, dials_only: bool = False) -> dict[float, int]:
    """Semantic-8 gauge id (component 2) -> vertex count over the interior's
    needle buffers (DashboardInstrument). Anything but the integer ids of the
    gauge table leaves a needle unturned: a later pass that writes UV1 into
    these buffers (damage.interior_uv1 did) silently breaks the gauges.

    With `dials_only`, only the vertices drawn with the needle material
    (dials_material) count. The digital dials (digital_dash) draw g_fDigiData
    vertices with ids 1..7 through the same declaration, sometimes from the same
    buffer as the needles (hosts 250, 293), but the engine reads those through no
    gauge record (issue #234)."""
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.vehicle_mesh import child, payload

    drsp = child(child(interior, "MDXN"), "DRSP").children
    drawn = None  # buffer UID -> indices of the vertices the needle material draws
    if dials_only:
        dials = dials_material(stream, interior)
        indices = {}
        for e in drsp:
            if e.tag == "IXCK":
                uid, count, size = struct.unpack_from("<8sII", payload(stream, child(e, "IXHD")))
                if size == 2:
                    indices[uid] = payload(stream, child(e, "IXDT"))[: count * 2]
        drawn = {}
        for e in drsp:
            if e.tag != "SBMC" or dials is None:
                continue
            d = payload(stream, child(e, "SBBH"))
            if d[8:16] != dials or d[0x30:0x38] not in indices:
                continue
            (base,) = struct.unpack_from("<I", d, 0x20)
            first, count = struct.unpack_from("<II", d, 0x38)
            ib = indices[d[0x30:0x38]]
            drawn.setdefault(d[16:24], set()).update(base + i for i in struct.unpack_from(f"<{count}H", ib, first * 2))
    counts: dict[float, int] = {}
    for e in drsp:
        if e.tag != "VXCK":
            continue
        elements = vm.parse_vdcl(payload(stream, child(e, "VDCL")))
        e8 = next((x for x in elements if x.semantic == 8), None)
        if e8 is None or e8.name_hash != DASHBOARD_INSTRUMENT or e8.components < 3 or e8.type != 8:
            continue
        header = payload(stream, child(e, "VXHD"))
        count, stride = struct.unpack_from("<II", header, 8)
        data = payload(stream, child(e, "VXDT"))
        verts = range(count) if drawn is None else sorted(v for v in drawn.get(header[:8], ()) if v < count)
        for v in verts:
            gid = struct.unpack_from("<e", data, v * stride + e8.offset + 4)[0]
            counts[gid] = counts.get(gid, 0) + 1
    return counts


def needle_check(stream, interior, h) -> tuple[list[int], int]:
    """What would leave a needle of a built car unturned: the gauge ids the
    needles drawn with the interior's Dials material carry that hierarchy `h`
    has no gauge record for, and the vertices drawn with that material from a
    buffer whose declaration has no DashboardInstrument (the engine reads no
    gauge id). The digital dials' vertices need no record (needle_gauge_ids)."""
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.vehicle_mesh import child, payload

    ids = needle_gauge_ids(stream, interior, dials_only=True)
    records = gauge_records(h)
    unturned = sorted(int(k) for k, n in ids.items() if n and k in GAUGE_IDS.values() and k not in records)
    drsp = child(child(interior, "MDXN"), "DRSP").children
    dials = dials_material(stream, interior)
    declared = {}
    for e in drsp:
        if e.tag == "VXCK":
            e8 = next((x for x in vm.parse_vdcl(payload(stream, child(e, "VDCL"))) if x.semantic == 8), None)
            declared[payload(stream, child(e, "VXHD"))[:8]] = e8 is not None and e8.name_hash == DASHBOARD_INSTRUMENT
    blind = 0
    for e in drsp:
        if e.tag == "SBMC" and dials is not None:
            d = payload(stream, child(e, "SBBH"))
            if d[8:16] == dials and not declared.get(d[16:24], True):
                blind += struct.unpack_from("<I", d, 0x3C)[0] // 3  # index count -> triangles
    return unturned, blind


def needle_survey(archive: Path, manifest: Path | None = None, cars: list[int] | None = None) -> list[dict]:
    """Per ported car of a built archive: the gauge ids its interior needles
    carry (semantic 8) and the ids its hierarchy has a gauge record for
    (gauge_records, as FUN_0088E310 keeps them). A needle id without a record
    is drawn and never turns (issue #62). The manifest (default: the archive's
    .json) names the cars and gives the parked speed and tach needles (drawn
    static because the host interior has no needle material)."""
    import json

    from dsfforza.formats import upvh
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    m = json.loads((manifest or archive.with_suffix(".json")).read_text(encoding="utf-8"))
    rows = []
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        for car in m["cars"]:
            sid = car["spool_id"]
            if cars and sid not in cars:
                continue
            kinds = upvh.entries_for(stream, root.entries, sid, None)
            h = upvh.parse(payload(stream, child(child(kinds[0], "NONR"), "UPVH")))
            ids = needle_gauge_ids(stream, kinds[1], dials_only=True) if 1 in kinds else {}
            unturned, blind = needle_check(stream, kinds[1], h) if 1 in kinds else ([], 0)
            interior = car.get("cockpit", {}).get("interior", {})
            rows.append(
                {
                    "spool_id": sid,
                    "media": car["media"],
                    "host": car["host"],
                    "needles": sorted(int(k) for k, n in ids.items() if n and k == int(k)),
                    "gauge_records": sorted(gauge_records(h)),
                    "unturned": unturned,
                    "no_gauge_id_triangles": blind,
                    "parked": [f for f in interior.get("parked_needles", []) if f in GAUGE_IDS],
                    "added": car.get("cockpit", {}).get("hierarchy", {}).get("gauges_added", []),
                }
            )
    return rows


def gauge_pose(upvh_data: bytes, vo3d_data: bytes, mph: float, rpm: float) -> dict[int, tuple]:
    """gauge id -> (pivot, axis, degrees) as the engine draws the needles at
    this speed and engine speed (FUN_00891670 inputs, FUN_008A2610 turn)."""
    from dsfforza.build import handling
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    v = handling.VO3D.parse(vo3d_data)
    lo, hi = v.params.get(SPEED_FLOOR_PARAM, 0.0), v.params.get(SPEED_DIAL_PARAM, 255.0)
    inputs = {1: min(max(rpm, v.get(40)), v.get(42)) * 0.0002, 2: min(max(abs(mph), lo), hi) * 0.0125}
    out = {}
    for e in h.table("seats"):
        pivot = struct.unpack_from("<3f", e, 0)
        q = struct.unpack_from("<4f", e, 0x10)
        gid, sweep = struct.unpack_from("<II", e, 0x20)
        out[gid] = (pivot, _norm(quat_rotate(q, (0, 0, 1))), sweep * inputs.get(gid, 0.0))
    return out


def render_gauges(
    archive: Path,
    spool_id: int,
    png: Path,
    readings: list[tuple[float, float]],
    fov_deg: float = 28,
    size: tuple[int, int] = (480, 300),
) -> dict:
    """The gauge cluster from the head camera, one tile per (mph, rpm), with
    the needles turned as the engine turns them. Tiles go left to right."""
    from dsfforza.formats import upvh
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        kinds = upvh.entries_for(stream, root.entries, spool_id, None)
        index = {k: root.entries.index(e) for k, e in kinds.items()}
        nonr = child(kinds[0], "NONR")
        upvh_data, vo3d_data = payload(stream, child(nonr, "UPVH")), payload(stream, child(nonr, "VO3D"))
    eye, _ = head_camera(upvh_data, vo3d_data)
    pivots = [struct.unpack_from("<3f", e) for e in upvh.parse(upvh_data).table("seats")]
    look = _norm(_sub(_mean(pivots), eye))
    tiles, info = [], {"eye": [round(c, 4) for c in eye], "readings": []}
    for n, (mph, rpm) in enumerate(readings):
        pose = gauge_pose(upvh_data, vo3d_data, mph, rpm)
        tris = _entry_triangles(archive, index[1], INTERIOR_MASK, textured=True, skip_glass=True, instruments=pose)
        tile = png.with_name(f"{png.stem}.tile{n}.png")
        render_eye_view(tris, eye, tile, size[0], size[1], fov_deg, pitch_deg=0, look=look)
        tiles.append(tile)
        info["readings"].append({"mph": mph, "rpm": rpm, "degrees": {g: round(p[2], 1) for g, p in pose.items()}})
    _compose([tiles], png)
    for tile in tiles:
        tile.unlink()
    info["png"] = str(png)
    return info


def _marker_tris(centre, size: float, colour) -> list:
    """An octahedron, for marking points in renders."""
    c = centre
    axes = [(size, 0, 0), (-size, 0, 0), (0, size, 0), (0, -size, 0), (0, 0, size), (0, 0, -size)]
    tris = []
    for a in axes[0:2]:
        for b in axes[2:4]:
            for d in axes[4:6]:
                tris.append(([_add(c, a), _add(c, b), _add(c, d)], None, None, colour))
    return tris


def render_eye_view(
    tris,
    eye,
    path: Path | None,
    width: int = 640,
    height: int = 400,
    fov_deg: float = 75,
    pitch_deg: float = -8,
    look=(0.0, 0.0, 1.0),
    mark_empty=None,
    unshaded: bool = False,
    alpha_test: bool = False,
):
    """Perspective view from `eye` looking along +z (DSF forward), z-buffered,
    textured where a texture is given; no back-face culling. With
    `mark_empty` (an RGB), pixels nothing covers get that colour; returns
    (pixels, number of empty pixels). `unshaded` writes texels/colours as
    they are (no fake light), for compositing glass offline; `alpha_test`
    skips texels with blue < 3 (no depth write), like the glass pixel
    shaders' texkill."""
    from dsfforza.formats import vehicle_mesh as vm

    f = 0.5 * width / math.tan(math.radians(fov_deg) / 2)
    cp, sp = math.cos(math.radians(pitch_deg)), math.sin(math.radians(pitch_deg))
    fwd = _norm((look[0], look[1] * cp + sp, look[2] * cp))
    right = _norm(_cross((0, 1, 0), fwd))  # DSF +x is left, so right = up x forward
    right = _scale(right, -1) if _dot(right, (1, 0, 0)) > 0 else right
    up = _cross(fwd, right) if _dot(_cross(fwd, right), (0, 1, 0)) > 0 else _cross(right, fwd)
    pixels = bytearray([24, 24, 28] * width * height)
    depth = [math.inf] * (width * height)
    light = _norm((0.3, 0.8, -0.4))
    for pts3, uvs, tex, colour in tris:
        cam = []
        for p in pts3:
            d = _sub(p, eye)
            cam.append((_dot(d, right), _dot(d, up), _dot(d, fwd)))
        if min(c[2] for c in cam) < 0.03:
            continue
        pts = [(width / 2 + f * c[0] / c[2], height / 2 - f * c[1] / c[2], c[2]) for c in cam]
        (px, py, pz), (qx, qy, qz), (rx, ry, rz) = pts
        area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
        if abs(area) < 1e-9:
            continue
        n = _norm(_cross(_sub(pts3[1], pts3[0]), _sub(pts3[2], pts3[0])))
        shade = 1.0 if unshaded else 0.45 + 0.55 * abs(_dot(n, light))
        x0, x1 = max(int(min(p[0] for p in pts)), 0), min(int(max(p[0] for p in pts)) + 1, width)
        y0, y1 = max(int(min(p[1] for p in pts)), 0), min(int(max(p[1] for p in pts)) + 1, height)
        if x1 - x0 > 4 * width or y1 - y0 > 4 * height:
            continue
        iz = [1 / pz, 1 / qz, 1 / rz]
        for y in range(y0, y1):
            for x in range(x0, x1):
                w0 = ((qx - x) * (ry - y) - (qy - y) * (rx - x)) / area
                w1 = ((rx - x) * (py - y) - (ry - y) * (px - x)) / area
                w2 = 1 - w0 - w1
                if w0 < 0 or w1 < 0 or w2 < 0:
                    continue
                inv = w0 * iz[0] + w1 * iz[1] + w2 * iz[2]
                z = 1 / inv
                k = y * width + x
                if z >= depth[k]:
                    continue
                if tex:
                    tw, th, trgba = tex
                    a, b, c = w0 * iz[0] * z, w1 * iz[1] * z, w2 * iz[2] * z
                    u = a * uvs[0][0] + b * uvs[1][0] + c * uvs[2][0]
                    v = a * uvs[0][1] + b * uvs[1][1] + c * uvs[2][1]
                    o = (min(th - 1, max(0, int(v * th))) * tw + min(tw - 1, max(0, int(u * tw)))) * 4
                    col = trgba[o : o + 3]
                    if alpha_test and col[2] < 3:
                        continue
                else:
                    col = colour
                depth[k] = z
                o = k * 3
                pixels[o : o + 3] = bytes(min(255, int(ch * shade)) for ch in col)
    empty = sum(1 for d in depth if d == math.inf)
    if mark_empty is not None:
        for k, d in enumerate(depth):
            if d == math.inf:
                pixels[k * 3 : k * 3 + 3] = bytes(mark_empty)
    if path is not None:
        vm.write_png(path, width, height, pixels)
    return pixels, empty


def render(archive: Path, spool_id: int, png: Path | None, eye_png: Path | None, size: int = 360) -> dict:
    """Cutaway orthographic views and a perspective view from the head camera.

    Row 1: the interior entry (mask 64, textured, glass left out); row 2: the
    clean exterior (blue-grey) with the interior. Columns: side view from the
    passenger side with everything on the passenger side of the eye cut
    away; top view with everything above the eye cut away; rear view with
    everything behind the eye cut away. Yellow: eye (head camera), cyan:
    steering marker, magenta: gauge pivots."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats import upvh
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        kinds = upvh.entries_for(stream, root.entries, spool_id, None)
        index = {k: root.entries.index(e) for k, e in kinds.items()}
        nonr = child(kinds[0], "NONR")
        upvh_data = payload(stream, child(nonr, "UPVH"))
        eye, marker = head_camera(upvh_data, payload(stream, child(nonr, "VO3D")))
    h = upvh.parse(upvh_data)
    gauges = [struct.unpack_from("<3f", e) for e in h.table("seats")]
    info = {
        "eye": [round(c, 4) for c in eye],
        "steering_marker": [round(c, 4) for c in marker],
        "gauges": [[round(c, 4) for c in g] for g in gauges],
    }
    interior = _entry_triangles(archive, index[1], INTERIOR_MASK, textured=True, skip_glass=True)
    marks = (
        _marker_tris(eye, 0.05, (255, 230, 0))
        + _marker_tris(marker, 0.035, (0, 220, 255))
        + [t for g in gauges for t in _marker_tris(g, 0.02, (255, 0, 255))]
    )
    if png:
        exterior = _entry_triangles(archive, index[2], 2, textured=False, flat=(70, 80, 110), skip_glass=True)
        # (view, keep(centroid)) per column; DSF +x is the driver's left, +z forward.
        cuts = [
            (dm.VIEWS["side"], lambda c: c[0] >= eye[0] - 0.02),
            (dm.VIEWS["top"], lambda c: c[1] <= eye[1] - 0.02),
            (dm.VIEWS["front"], lambda c: c[2] >= eye[2] + 0.02),
        ]
        tiles = []
        for row in (interior, exterior + interior):
            line = []
            for n, (view, keep) in enumerate(cuts):
                tris = [t for t in row if keep(_mean(t[0]))] + marks
                tile = png.with_name(f"{png.stem}.tile{len(tiles)}{n}.png")
                dm._render(tris, tile, size, [view])
                line.append(tile)
            tiles.append(line)
        _compose(tiles, png)
        for line in tiles:
            for tile in line:
                tile.unlink()
        info["png"] = str(png)
    if eye_png:
        render_eye_view(interior + _marker_tris(marker, 0.01, (0, 220, 255)), eye, eye_png)
        info["eye_png"] = str(eye_png)
    return info


# Head positions (DSF model space offsets from the head camera; +x is the
# driver's left, +z forward) and look directions for the hole check. DSF's
# cockpit camera bobs and looks left and right.
HEAD_OFFSETS = [(0.0, 0.0, 0.0), (0.10, 0.0, 0.0), (-0.10, 0.0, 0.0), (0.0, 0.06, -0.06)]
HEAD_LOOKS = [
    ("ahead", (0.0, 0.0, 1.0), -8),
    ("left", (0.7, 0.0, 0.7), -10),
    ("right", (-0.7, 0.0, 0.7), -10),
    ("down", (0.0, 0.0, 1.0), -40),
    ("door-l", (1.0, 0.0, 0.25), -15),
    ("door-r", (-1.0, 0.0, 0.25), -15),
]
HOLE_COLOUR = (255, 0, 255)


def check_holes(
    archive: Path, spool_id: int, png: Path | None, size: tuple[int, int] = (240, 150), fov_deg: float = 90
) -> dict:
    """Render the interior (glass included, flat) from HEAD_OFFSETS x
    HEAD_LOOKS around the head camera; pixels nothing covers are holes (or
    open windows on open cars) and turn magenta. Returns the empty-pixel
    share per view and the worst."""
    from dsfforza.formats import upvh
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        kinds = upvh.entries_for(stream, root.entries, spool_id, None)
        index = {k: root.entries.index(e) for k, e in kinds.items()}
        nonr = child(kinds[0], "NONR")
        eye, _ = head_camera(payload(stream, child(nonr, "UPVH")), payload(stream, child(nonr, "VO3D")))
    tris = _entry_triangles(archive, index[1], INTERIOR_MASK, textured=True, skip_glass=False)
    w, h = size
    rows, views = [], {}
    for offset in HEAD_OFFSETS:
        line = []
        for name, look, pitch in HEAD_LOOKS:
            pixels, empty = render_eye_view(
                tris, _add(eye, offset), None, w, h, fov_deg, pitch, look, mark_empty=HOLE_COLOUR
            )
            views[f"{name}@{','.join(f'{c:+.2f}' for c in offset)}"] = round(empty / (w * h), 4)
            line.append(pixels)
        rows.append(line)
    if png is not None:
        out = bytearray()
        for line in rows:
            for y in range(h):
                for pixels in line:
                    out += pixels[y * w * 3 : (y + 1) * w * 3]
        vm.write_png(png, w * len(HEAD_LOOKS), h * len(HEAD_OFFSETS), out)
    worst = max(views, key=views.get)
    return {"eye": [round(c, 4) for c in eye], "views": views, "worst": worst, "worst_share": views[worst]}


def _read_png_rows(path: Path) -> tuple[int, list[bytes]]:
    """(width, RGB rows) of a PNG written by vehicle_mesh.write_png."""
    import zlib

    data = path.read_bytes()
    w, h = struct.unpack(">II", data[16:24])
    pos, idat = 8, b""
    while pos < len(data):
        n = struct.unpack(">I", data[pos : pos + 4])[0]
        if data[pos + 4 : pos + 8] == b"IDAT":
            idat += data[pos + 8 : pos + 8 + n]
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * 3 + 1
    return w, [raw[y * stride + 1 : (y + 1) * stride] for y in range(h)]


def _compose(grid: list[list[Path]], out: Path) -> None:
    """Join PNG tiles (same size) into one image, row by row."""
    from dsfforza.formats import vehicle_mesh as vm

    rows, width = [], 0
    for line in grid:
        tiles = [_read_png_rows(p) for p in line]
        width = sum(w for w, _ in tiles)
        for y in range(len(tiles[0][1])):
            rows.append(b"".join(r[y] for _, r in tiles))
    vm.write_png(out, width, len(rows), bytearray(b"".join(rows)))


# --- CLI -----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fit", help="steering wheel, gauges, mirrors and eye point of a Forza cockpit")
    f.add_argument("car", type=Path, help="extracted car folder")
    f.add_argument("name")
    f.add_argument("--gamedb", type=Path, action="append", default=[])
    r = sub.add_parser("render", help="combined render and head-camera view of a converted car")
    r.add_argument("archive", type=Path)
    r.add_argument("--id", type=int, required=True)
    r.add_argument("--png", type=Path)
    r.add_argument("--eye-view", type=Path)
    r.add_argument("--size", type=int, default=360)
    gp = sub.add_parser("gauges", help="the gauge cluster from the head camera, needles at given readings")
    gp.add_argument("archive", type=Path)
    gp.add_argument("--id", type=int, required=True)
    gp.add_argument("--png", type=Path, required=True)
    gp.add_argument(
        "--at",
        action="append",
        default=[],
        metavar="MPH,RPM",
        help="one tile per reading (default 0,0 30,2000 60,4000 100,6000)",
    )
    gp.add_argument("--fov", type=float, default=28)
    hp = sub.add_parser("holes", help="interior from several head positions and directions; empty pixels magenta")
    hp.add_argument("archive", type=Path)
    hp.add_argument("--id", type=int, action="append", required=True)
    hp.add_argument("--png", type=Path, help="per car: <png stem>-<id>.png")
    ns = sub.add_parser("needles", help="per ported car: needle gauge ids against the hierarchy's gauge records")
    ns.add_argument("archive", type=Path)
    ns.add_argument("--manifest", type=Path, help="default: the archive's .json")
    ns.add_argument("--id", type=int, action="append", default=[])
    ns.add_argument("--tsv", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "needles":
        rows = needle_survey(args.archive, args.manifest, args.id)
        if args.tsv:
            cols = list(rows[0]) if rows else []
            lines = ["\t".join(cols)] + [
                "\t".join(",".join(map(str, v)) if isinstance(v, list) else str(v) for v in r.values()) for r in rows
            ]
            args.tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")
        bad = [r for r in rows if r["unturned"] or r["no_gauge_id_triangles"] or r["parked"]]
        both = sum(1 for r in rows if set(r["needles"]) >= {1, 2} and r not in bad)
        print(
            f"{len(rows)} cars: {both} with both needles turning, "
            f"{sum(1 for r in rows if not r['needles'] and not r['no_gauge_id_triangles'])} without needles"
        )
        for r in bad:
            print(
                f"  {r['spool_id']:4d} {r['media']:30s} host {r['host']:3d}: needles {r['needles']}, "
                f"gauge records {r['gauge_records']}, unturned {r['unturned']}, "
                f"needle triangles without a gauge id {r['no_gauge_id_triangles']}, parked {r['parked']}"
            )
        print(f"{len(bad)} cars with a speed or tach needle that never turns")
        added = [r for r in rows if r["added"]]
        if added:
            print(f"{len(added)} cars got gauge records: " + ", ".join(f"{r['media']} {r['added']}" for r in added))
        return

    if args.cmd == "holes":
        for spool_id in args.id:
            png = args.png.with_name(f"{args.png.stem}-{spool_id}.png") if args.png else None
            r = check_holes(args.archive, spool_id, png)
            print(
                spool_id,
                f"worst {r['worst']} {r['worst_share']:.2%}",
                " ".join(f"{k}={v:.1%}" for k, v in r["views"].items() if v > 0.002),
            )
        return
    if args.cmd == "gauges":
        readings = [tuple(float(x) for x in a.split(",")) for a in args.at] or [
            (0, 0),
            (30, 2000),
            (60, 4000),
            (100, 6000),
        ]
        print(render_gauges(args.archive, args.id, args.png, readings, args.fov))
        return
    if args.cmd == "fit":
        sections = load_cockpit(args.car, args.name)
        if sections is None:
            raise SystemExit(f"no {args.name}_cockpit.carbin in {args.car}")
        cf = fit(sections, args.name, args.gamedb, folder=args.car)

        def r3(v):
            return "(" + ", ".join(f"{x:.3f}" for x in v) + ")"

        print(f"steering wheel: centre {r3(cf.wheel.centre)} axis {r3(cf.wheel.axis)} radius {cf.wheel.radius:.3f}")
        for g in cf.gauges.values():
            print(f"gauge {g.family}: pivot {r3(g.pivot)} normal {r3(g.normal)} sweep {g.sweep_deg:.1f} deg")
            cal = cf.calibration.get(g.family)
            if cal:
                print(
                    f"  DSF id {cal.gauge_id}: {cal.sweep} deg per {GAUGE_UNITS[g.family]:g} "
                    f"{'mph' if g.family == 'speed' else 'rpm'}{' (reversed)' if cal.reverse else ''}, "
                    f"rest turned {cal.rest_deg:+.1f} deg, dial to {cal.dial_max:g}, "
                    f"worst tick {cal.residual_deg:.1f} deg off ({cal.source})"
                )
        if cf.display_faces:
            print(f"digital displays: {cf.display_faces} triangles drawn with their face texture")
        if cf.digital:
            print(f"digital readouts: {cf.digital} triangles, drawn blank")
        for side, p in cf.mirrors.items():
            print(f"mirror {side}: {r3(p)}")
        print(f"eye {r3(cf.eye)} = wheel + offset {r3(cf.offset)} ({cf.offset_source}); Forza space, front -z")
    else:
        print(render(args.archive, args.id, args.png, args.eye_view, args.size))


if __name__ == "__main__":
    sys.exit(main())
