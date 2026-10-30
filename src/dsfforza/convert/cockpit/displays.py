"""Digital displays and virtual dials: blank displays found and given dials, the steering-wheel
pod, the dash scene drawn live on the displays (digital_displays)."""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass
from pathlib import Path

from dsfforza.convert import forza_materials as fm
from dsfforza.convert.cockpit.geometry import (
    DIGITAL_TECHNIQUES,
    DISPLAY_COLOUR,
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
from dsfforza.convert.cockpit.hierarchy import HAND_GRIP_DEG, GaugeSight, camera_eye
from dsfforza.convert.cockpit.visibility import _raster, _screen_parts
from dsfforza.formats import carbin

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


def _touching_groups(tris: list[list[tuple]], cell: float = 0.01) -> list[list[int]]:
    """Indices of each group of triangles that touch: union-find over triangles with a
    vertex in the same `cell` grid cell."""
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
