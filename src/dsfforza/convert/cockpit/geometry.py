"""The Forza cockpit's geometry: section names and constants, vector helpers, the steering
wheel and gauge fits, their calibration, the eye and the display faces."""

from __future__ import annotations

import itertools
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

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
