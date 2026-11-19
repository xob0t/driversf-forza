"""Digital displays: the dash scene drawn live on the cockpit's displays (digital_displays),
a steering-wheel display moved onto a virtual display on the dash (wheel_display_pod), and
the scene's needles as DSF needles (dash_needles). Where the converter can't put a live
needle on the car's own gauges it adds none: no generic dials (research/cockpit.md, "No
virtual dials")."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from dsfforza.convert import forza_materials as fm
from dsfforza.convert.cockpit.geometry import (
    DIGITAL_TECHNIQUES,
    DISPLAY_FACE,
    FALLBACK_DIAL_MAX,
    GAUGE_IDS,
    GAUGE_UNITS,
    CockpitFit,
    Gauge,
    GaugeCalibration,
    Wheel,
    _add,
    _cross,
    _dot,
    _mean,
    _norm,
    _scale,
    _sub,
    section_points,
)
from dsfforza.convert.cockpit.hierarchy import ARMS_GRIPS, camera_eye
from dsfforza.convert.cockpit.visibility import _raster, _screen_parts
from dsfforza.formats import carbin


@dataclass
class Display:
    """A virtual display's screen (pod_display): its plane and rectangle, Forza space."""

    section: str
    centre: tuple
    normal: tuple  # unit, into the dash (the driver looks along it)
    up: tuple  # unit, in the plane
    right: tuple  # unit, in the plane: the driver's right
    rect: tuple  # (u0, u1, v0, v1): extent along right and up from the centre

    @property
    def size(self) -> tuple[float, float]:
        return self.rect[1] - self.rect[0], self.rect[3] - self.rect[2]

    def point(self, u: float, v: float, lift: float = 0.0) -> tuple:
        """The display point at (u, v) of its plane, `lift` towards the driver."""
        return _add(_add(_add(self.centre, _scale(self.right, u)), _scale(self.up, v)), _scale(self.normal, -lift))


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


# A virtual display (pod_display): where Forza's only display is on the steering wheel
# (wheel_display_pod), a screen in a housing on the dashboard, facing the eye, where the
# driver sees it past the wheel at any steering angle.
POD_SIZES = {  # dials -> display sizes (m) tried, largest first
    2: ((0.16, 0.08), (0.14, 0.07), (0.12, 0.06), (0.10, 0.05)),
    1: ((0.10, 0.075), (0.09, 0.0675), (0.08, 0.06), (0.07, 0.0525)),
}
POD_BEZEL = 0.006  # m: housing around the screen
POD_LIFT = 0.004  # m: the housing's front in front of the nearest dash point behind it
POD_DEPTH = 0.15  # m: the most the dashboard may fall away behind the housing (a box on the dash top)
POD_RANGE = (0.3, 1.1)  # m from the eye along the line of sight
POD_WHEEL_CLEAR = 0.04  # m beyond the rim: kept clear, the wheel turns and the hands hold it
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
# The hands hold the rim at rim_grips; a display behind a
# hand and wrist is as hidden as one under the HUD (the GT-R's and Skyline 2003's small
# moved displays in runs vm2-20261029-201910, vm1-20261029-202300). pod_display(hands=True)
# keeps this far from each grip, in the wheel's plane.
POD_HAND_CLEAR = 0.13  # m
POD_COWL_SUPPORT = 0.5  # share of a cowl display (and of its bottom quarter) in front of the cowl
# An open cockpit's bodywork round the wheel (ARX-02a: a_pillar; R10: hood): pod_display(cowl=True).
COWL_SECTIONS = re.compile(r"^(hood|a_pillar)", re.I)


def rim_grips(wheel: Wheel) -> list[tuple]:
    """Where the hands hold the rim (Forza space): dsfcars.asi [hands] moves each of
    the arms' fists (hierarchy.ARMS_GRIPS, about the hub's column) onto the rim at its
    angle about the column."""
    out = []
    for g in ARMS_GRIPS:
        d = (-g[0], g[1], -g[2])  # DSF model axes -> Forza's
        d = _sub(d, _scale(wheel.axis, _dot(d, wheel.axis)))
        out.append(_add(wheel.centre, _scale(_norm(d), wheel.radius)))
    return out


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
    POD_WHEEL_CLEAR) and clear of Forza's own needles' sweeps, digital displays and gauge
    faces, and with `hud` out from under DSF's HUD (HUD_DOWN_DEG...). Found on a
    depth image from the eye.

    With `cowl` (open cockpits), the bodywork in front of the driver
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
        bad |= _disc_mask(rays, eye, g.pivot, g.normal, reach)  # the needle's sweep (its face is a gauge material)
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
            d = Display(name, centre, n, up, right, (-w / 2, w / 2, -h / 2, h / 2))
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
# cockpit's cowl clear of it, else the dashboard with the display allowed under the HUD's
# lower corners.
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


def drop_unturned(cf: CockpitFit, gauge_slots: dict[int, int]) -> list[str]:
    """Take out the speed and tach needles (Forza's or the dash scene's) whose
    gauge id the host's gauge table has no entry for, after add_gauge_records:
    only hosts without any gauge record (vanilla 250, the Volt's, and four
    unused ones). The engine would leave such a needle unturned, so it isn't
    drawn at all. Returns the families dropped (cf.unturned, the report's
    `no_host_gauge`)."""
    dropped = [f for f in cf.gauges if f in GAUGE_IDS and GAUGE_IDS[f] not in gauge_slots]
    for f in dropped:
        cf.gauges.pop(f)
        cf.needles.pop(f, None)
        cf.calibration.pop(f, None)
    cf.unturned = dropped
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
# speed box (the R15 TDI's, on its cowl at 0.5). Where the only smaller spot is
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
    the steering wheel"). The patches keep their UVs, so they show the same
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
        redline = getattr(forza, "redline", 0.0) or FALLBACK_DIAL_MAX["tach"]
        plan = digital_dash.build_plan(dash, patches, redline)
    except Exception as error:  # noqa: BLE001 - a scene this decoder can't draw: logged (convert_interior), the display stays
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
