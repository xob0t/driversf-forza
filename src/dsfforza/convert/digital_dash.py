"""Live digital displays in the cockpit view, through DSF's own digital dials.

Driver: San Francisco has a live digital-dashboard path of its own, used by 14
vanilla interiors (Volt, LP670, Alfa 8C, ...; research/cockpit.md, "Digital
displays"): materials `*_Digital_Dials.matxml` whose vertex shader adds
`g_fDigiData[(id - 1) * 4 + comp]` to the vertex's v texture coordinate. The
vertex format is the needles' (DashboardInstrument (u, v, id, comp)), and the
game fills, every frame (FUN_00891670):

    id 1, comp 0: rpm / redline x 0.5
    id 2, comp 0..3: mph digits, units to thousands: (digit + 1) x 23/256,
                     0 for a leading blank
    id 6, comp 0: gear / 7 (0 = reverse)
    id 7, comp 0: 0.5 when the gear isn't reverse
    ids 3..5: never written (0); dsfforza.asi's dash module writes km/h digits
              on id 3 like the mph digits on id 2 (tools/asi/src/mod_dash.cpp)

So a digit is a quad over the blank cell of a strip of 11 cells (blank, 0..9,
23/256 apart), a gear a quad over the first of 7 cells, and an rpm bar segment
a quad whose single v samples a column that is transparent below a threshold
and lit above it (alpha-tested away when transparent). Static artwork uses id
5 (no offset). Everything samples one texture per car, built here.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np

# The vanilla Volt's digital material (entry 148, Chevrolet_Volt_2011_Digital_Dials.matxml):
# alpha test 0.5, g_texDiffuseMap b012ecfc52fb762a. Its submodel sits in the
# interior's needle part with MODL flag 1, in a needle-layout buffer.
VANILLA_MATERIAL = bytes.fromhex("e012cc80f0aa8829")
VANILLA_TEXTURE = bytes.fromhex("b012ecfc52fb762a")
MODL_FLAG = 1

RPM = (1, 0)  # (id, comp): rpm / redline x 0.5
SPEED_DIGITS = [(2, k) for k in range(4)]  # units, tens, hundreds, thousands
GEAR = (6, 0)  # gear / 7, 0 = reverse
KMH_DIGITS = [(3, k) for k in range(4)]  # km/h digits: dsfforza.asi [dash] writes them (mod_dash.cpp)
STATIC = (5, 0)  # never written: no offset
DIGIT_PITCH = 23 / 256  # v between digit cells (blank, 0..9)
GEAR_PITCH = 1 / 7  # v between gear cells (R, 1..6)
BAR_LIT_FROM = 0.505  # bar columns: lit from this v up, transparent below
BAR_MAX = 0.49  # largest threshold (rpm at the redline gives 0.5)


@dataclass
class Quad:
    corners: list  # 4 Forza-space points, in order round the quad
    uvs: list  # 4 (u, v) texture coordinates
    slot: tuple  # (id, comp)


@dataclass
class DigitalPlan:
    """What a car's digital displays add to its interior: one texture and quads."""

    width: int
    height: int
    rgba: bytearray
    quads: list = field(default_factory=list)
    report: dict = field(default_factory=dict)
    needles: list = field(default_factory=list)  # _needles: needles for DSF's gauge needles
    patches: list = field(default_factory=list)  # the DisplayPatches it is drawn on

    def blit(self, x: int, y: int, image) -> None:
        """Paste an RGBA PIL image at texel (x, y) (top left)."""
        w, h = image.size
        data = image.tobytes()
        for row in range(h):
            o = ((y + row) * self.width + x) * 4
            self.rgba[o : o + w * 4] = data[row * w * 4 : (row + 1) * w * 4]

    def fill(self, x0: int, y0: int, x1: int, y1: int, colour) -> None:
        px = bytes(colour)
        for row in range(y0, y1):
            o = (row * self.width + x0) * 4
            self.rgba[o : o + (x1 - x0) * 4] = px * (x1 - x0)


def dxt5_dds(width: int, height: int, rgba: bytes | bytearray, template: bytes | None = None) -> bytes:
    """A single-mip DXT5 DDS (the vanilla digital textures are DXT5/DXT1; a single
    mip is never dropped by the texture-quality setting and keeps the cells and
    bar columns from bleeding into each other)."""
    from dsfforza.convert import dsf_materials as dm

    return dm.dds_file(rgba, width, height, 1, template)


VANILLA_STRIDE = 16  # the Volt's digital buffer: s0 position 4 x i16, s8 4 x f16 (u, v, id, comp)

_VANILLA: dict = {}
_VANILLA_VB: dict = {}


def vanilla_buffer(stream):
    """The VXCK the vanilla Volt draws its digital dials from: its vertex declaration
    (no normal) is the one the digital-dials shaders are drawn with."""
    key = id(stream)
    if key not in _VANILLA_VB:
        from dsfforza.convert import dsf_materials as dm
        from dsfforza.formats import chnk
        from dsfforza.formats.vehicle_mesh import child

        root = chnk.read_chunk(stream, 0)
        vb, found = None, None
        for e in root.entries:
            for _, c in chnk.walk([e]):
                if c.tag == "SBMC" and c.is_chunk:
                    d = dm.current(stream, child(c, "SBBH"))
                    if d[8:16] == VANILLA_MATERIAL:
                        vb = d[16:24]
            if vb is not None:
                for _, c in chnk.walk([e]):
                    if c.tag == "VXCK" and c.is_chunk and dm.current(stream, child(c, "VXHD"))[:8] == vb:
                        found = c
                break
        if found is None:
            raise ValueError("the vanilla Volt's digital-dials buffer is not in the source archive")
        _VANILLA_VB[key] = found
    return _VANILLA_VB[key]


def vanilla_material(stream):
    """The vanilla Volt digital MACO (Entry) and its TEXC, from the source archive."""
    key = id(stream)
    if key not in _VANILLA:
        from dsfforza.convert import dsf_materials as dm
        from dsfforza.formats import chnk

        root = chnk.read_chunk(stream, 0)
        found = None
        for e in root.entries:
            for _, c in chnk.walk([e]):
                if c.tag == "MACO" and c.is_chunk and dm.material_info(stream, c)["uid"] == VANILLA_MATERIAL:
                    found = c
                    break
            if found is not None:
                break
        if found is None:
            raise ValueError("vanilla digital material e012cc80f0aa8829 (Volt) not in the source archive")
        _VANILLA[key] = found
    return _VANILLA[key]


def vertices(plan: DigitalPlan, place, stride: int) -> tuple[list[bytes], list[tuple[int, int, int]]]:
    """Needle-layout vertices and double-sided triangles for the plan's quads."""
    from dsfforza.convert import cockpit

    verts, tris = [], []
    for q in plan.quads:
        uvs = q.uvs
        a, b, c = q.corners[0], q.corners[1], q.corners[2]
        n = cockpit._norm(cockpit._cross(cockpit._sub(b, a), cockpit._sub(c, a)))
        base = len(verts)
        for p, uv in zip(q.corners, uvs, strict=True):
            verts.append(cockpit._needle_vertex(place.point(p), uv, q.slot[0], place.normal(n), stride, q.slot[1]))
        tris += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
        tris += [(base, base + 2, base + 1), (base, base + 3, base + 2)]
    return verts, tris


# --- Forza's dash scenes -> DSF digital dials ------------------------------------------
TEXTURE_SIZE = (512, 512)
RT_W, RT_H = 1280, 720  # the scene's presentation (render-target) size
SCALE = 2  # render-target pixels are drawn this many texels wide before fitting
RPM_STEPS = 40  # rpm thresholds tried for the bar pieces (0 .. the scene's tach full scale)
LIT_DIFFERENCE = 0.06  # a pixel is lit when its colour moved this far from the rest state
MAX_BAR_COLUMNS = 16  # bar columns at most (rarer colour sequences fold onto these)
PALETTE = 12  # flat colours of the bar pieces (one texture column each)
COLUMN = 4  # texels per bar colour column (a DXT block)
CELL_MARGIN = 2  # texels kept clear round each digit/gear cell
# m towards the eye over the drawn display: bar pieces, then digits over them. Positions
# are stored in 1/4096 m (0.24 mm) steps, so layers sit 0.8 mm apart or more.
PIECE_LIFT = 0.0008
DIGIT_LIFT = 0.0016
CHANGES = 2  # colour changes per pixel the rpm makes (rest -> lit -> warning)
# The digital dials draw their texel unlit (shader program 163 has no light term) in a
# dim, lit cabin, and the game's tone curve flattens bright texels: the Panoz's face at
# texel luminance 157 shows at 205, at 108 still at 181 (runs vm1-20261028-124014,
# -124528). Vanilla digital textures are dark screens with bright glyphs (backgrounds at
# median luminance 0 to 47); Forza's LCD scenes are often pale (Panoz, Acura ARX, Devon:
# 120 to 190) and the LFA's ring is white. The whole texture is scaled down until the
# displays' mean luminance is at most the brightest vanilla background and their 95th
# percentile at most:
DISPLAY_MEAN_LUMINANCE = 48.0
DISPLAY_PEAK_LUMINANCE = 160.0
SPEED_SLOTS = 3  # digits laid out for the speed readout (units, tens, hundreds)
GEAR_CHARS = "R123456"  # g_fDigiData gear: 0 = reverse, then 1..6 (no neutral in DSF)
BLANK_TEXT = (  # readouts DSF has no value for: drawn empty
    "TEXT_TACH",
    "TEXT_CURRENT_LAP",
    "TEXT_MAX_LAPS",
    "PLACE",
    "PLACE_OF",
    "TEXT_CURRENT_LAP_TIME",
    "TEXT_LAST_LAP_TIME",
    "TEXT_BEST_LAP_TIME",
)
RPM_METERS = ("METER_TACH", "METER_TACH_MEMORY", "METER_SHIFT_LIGHTS")


@dataclass
class DisplayPatch:
    """One digital_gauge subsection: its triangles (Forza space, UV0) and the window of
    the scene it shows (render-target uv = uv0 x scale + offset)."""

    section: str
    subsection: int
    transform: tuple  # (x_off, x_scale, y_off, y_scale)
    points: list  # per triangle: 3 Forza-space points
    uv0: list  # per triangle: 3 UV0
    towards_eye: tuple  # unit normal of the display, towards the driver

    def rt(self, uv):
        xo, xs, yo, ys = self.transform
        return ((uv[0] * xs + xo) * RT_W, (uv[1] * ys + yo) * RT_H)

    def window(self) -> tuple:
        """(x0, x1, y0, y1) render-target pixels the triangles show."""
        pts = [self.rt(uv) for tri in self.uv0 for uv in tri]
        return (min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts), max(p[1] for p in pts))

    def point(self, x: float, y: float, lift: float = 0.0):
        """The Forza-space point showing render-target pixel (x, y): barycentric in the
        triangle holding it (else the nearest, extrapolated), `lift` towards the eye."""
        best, best_d = None, None
        for k, tri in enumerate(self.uv0):
            (ax, ay), (bx, by), (cx, cy) = (self.rt(uv) for uv in tri)
            d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(d) < 1e-12:
                continue
            l1 = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / d
            l2 = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / d
            l3 = 1 - l1 - l2
            miss = -min(l1, l2, l3, 0.0)
            if best_d is None or miss < best_d:
                best, best_d = (k, l1, l2, l3), miss
                if miss == 0:
                    break
        k, l1, l2, l3 = best
        p = self.points[k]
        out = tuple(l1 * p[0][i] + l2 * p[1][i] + l3 * p[2][i] for i in range(3))
        return tuple(out[i] + self.towards_eye[i] * lift for i in range(3))

    def contains(self, x: float, y: float) -> bool:
        x0, x1, y0, y1 = self.window()
        return x0 <= x <= x1 and y0 <= y <= y1

    def covers(self, x: float, y: float, tolerance: float = 0.5) -> bool:
        """Whether render-target pixel (x, y) lies on one of the display's triangles
        (within `tolerance` pixels), not just inside its window."""
        for tri in self.uv0:
            (ax, ay), (bx, by), (cx, cy) = (self.rt(uv) for uv in tri)
            d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(d) < 1e-12:
                continue
            ls = (
                ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / d,
                ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / d,
            )
            ls = (*ls, 1 - ls[0] - ls[1])
            # barycentric slack worth `tolerance` pixels along the triangle's shortest height
            edge = max(math.dist((ax, ay), (bx, by)), math.dist((bx, by), (cx, cy)), math.dist((cx, cy), (ax, ay)))
            slack = tolerance * edge / abs(d)
            if min(ls) >= -slack:
                return True
        return False


def coverage(patches: list, view: tuple, width: int, height: int):
    """Boolean (height, width) mask of the render-target pixels of `view` (x0, x1, y0,
    y1; one pixel per array cell) that the displays' triangles show. Pixels in a
    display's window but off its triangles (round dials, the corners of an oval) aren't
    drawn by Forza, and a live quad there would float off the display in its plane."""
    from PIL import Image, ImageDraw

    img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(img)
    for p in patches:
        for tri in p.uv0:
            draw.polygon([(p.rt(uv)[0] - view[0], p.rt(uv)[1] - view[2]) for uv in tri], fill=255, outline=255)
    return np.asarray(img) > 0


def _bbox(alpha, threshold: float = 0.02):
    ys, xs = np.nonzero(alpha > threshold)
    if not len(xs):
        return None
    return int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1


def _palette(colours, k: int):
    """k-means (a few rounds) of RGB rows; returns (centres, labels)."""
    colours = np.asarray(colours, np.float32)
    if len(colours) <= k:
        return colours, np.arange(len(colours))
    order = np.argsort(colours.sum(1))
    centres = colours[order[np.linspace(0, len(order) - 1, k).astype(int)]].copy()
    for _ in range(8):
        labels = ((colours[:, None, :] - centres[None]) ** 2).sum(-1).argmin(1)
        for j in range(k):
            sel = colours[labels == j]
            if len(sel):
                centres[j] = sel.mean(0)
    labels = ((colours[:, None, :] - centres[None]) ** 2).sum(-1).argmin(1)
    return centres, labels


def _runs_to_rects(mask, slack: int = 2, forbid=None):
    """Rectangles (x0, x1, y0, y1), pixel edges, covering a boolean mask: runs per row,
    merged down while a run stays within `slack` pixels of the rectangle above (which
    then grows to hold it), and while the grown rectangle takes in no `forbid` pixel
    (another piece's: two pieces in one plane z-fight where they overlap)."""
    if forbid is not None:  # summed-area table: forbidden pixels in a box in O(1)
        sat = np.zeros((mask.shape[0] + 1, mask.shape[1] + 1), np.int64)
        sat[1:, 1:] = np.cumsum(np.cumsum(forbid & ~mask, 0), 1)

    def clear(x0, x1, y0, y1):
        if forbid is None:
            return True
        return sat[y1, x1] - sat[y0, x1] - sat[y1, x0] + sat[y0, x0] == 0

    rects, open_ = [], []  # open: [x0, x1, y0, last row]
    for y in range(mask.shape[0]):
        row = mask[y]
        runs = []
        if row.any():
            edges = np.flatnonzero(np.diff(np.concatenate([[0], row.astype(np.int8), [0]])))
            runs = [(int(a), int(b)) for a, b in zip(edges[0::2], edges[1::2], strict=True)]
        still = []
        for x0, x1 in runs:
            match = next(
                (
                    r
                    for r in open_
                    if r[3] == y - 1
                    and abs(r[0] - x0) <= slack
                    and abs(r[1] - x1) <= slack
                    and clear(min(r[0], x0), max(r[1], x1), r[2], y + 1)
                ),
                None,
            )
            if match is not None:
                open_.remove(match)
                match[0], match[1], match[3] = min(match[0], x0), max(match[1], x1), y
                still.append(match)
            else:
                still.append([x0, x1, y, y])
        rects += [(r[0], r[1], r[2], r[3] + 1) for r in open_]
        open_ = still
    rects += [(r[0], r[1], r[2], r[3] + 1) for r in open_]
    return rects


def _shown_spans(slots: list) -> list:
    """Per slot rectangle (x0, x1, y0, y1), the x span it is drawn over: where two
    neighbours overlap, each keeps its side of the middle of the overlap."""
    spans = [[r[0], r[1]] for r in slots]
    order = sorted(range(len(slots)), key=lambda k: slots[k][0])
    for left, right in itertools.pairwise(order):
        if spans[left][1] > spans[right][0]:
            cut = (spans[left][1] + spans[right][0]) / 2
            spans[left][1], spans[right][0] = cut, cut
    return [tuple(s) for s in spans]


def _rgba(colour) -> tuple:
    return (*(int(round(float(v) * 255)) for v in colour[:3]), 255)


def _subtree(dash, root: int) -> set:
    out, todo = set(), [root]
    while todo:
        i = todo.pop()
        out.add(i)
        todo += dash.children.get(i, [])
    return out


def speed_units(dash) -> str:
    """ "kmh" when the scene labels its speed in km/h (texture names, element names,
    texts: KMH, KM/H, KPH) and nowhere in mph, else "mph"."""
    import re

    words = []
    for o in dash.fbf["objects"]:
        words.append(o.name)
        words.append(str(o.props.get("path", "")))
        words.append(str(o.props.get("text", "")))
    text = " ".join(words).lower()
    kmh = re.search(r"km\s*/\s*h|kmh|km_h|kph", text) is not None
    mph = "mph" in text
    return "kmh" if kmh and not mph else "mph"


def _rest_state(dash, tach_max: float) -> None:
    dash.text_override.clear()
    dash.apply_state(0.0, 0.0, "", tach_max_default=tach_max)
    for key in ("TEXT_SPEED", "TEXT_GEAR", *BLANK_TEXT):
        dash.text(key, "")


def _layer(dash, element: int, view, only=None):
    """One element alone, RGBA at the render scale, over the view."""
    from dsfforza.convert import dash_scene as ds

    return np.asarray(ds.render(dash, RT_W * SCALE, keep=lambda i: i == element, only=only, alpha=True, view=view))


NEEDLE_ASPECT = 4.0  # a turning element this much longer than wide is a needle, not a cover
NEEDLE_SAMPLES = 12  # poses the needle fit samples, up to full scale
NEEDLE_LIFT = 0.0025  # m towards the eye: a converted needle over the display
NEEDLE_METERS = {"METER_TACH": "tach", "METER_SPEED": "speed"}


def _turning(dash) -> dict:
    """Elements an rpm or speed meter turns: {element: family} for those with a
    rotation track on the meter's PROGRESS slide."""
    from dsfforza.convert import dash_scene as ds

    out = {}
    for key, family in NEEDLE_METERS.items():
        c = dash.bound(key)
        for s in dash.slides.get(c, []) if c is not None else []:
            if s["name"].upper() != "PROGRESS":
                continue
            for en in s["entries"]:
                for on, ti in en["tracks"]:
                    name = ds.HP.get(dash.bgf["tracks"][ti]["prop"] & 0xFFFFFF, "")
                    if on and name.startswith("rotation"):
                        out.setdefault(en["elem"], family)
    return out


def _pose(dash, element: int):
    """(2 x 2 A, t) of the first quad drawn in an element's subtree: world = local @ A + t."""
    sub = _subtree(dash, element)
    for d in dash.drawables():
        if d[-2] in sub and d[0] in ("quad", "mesh"):
            m = d[1]
            return m[:2, :2].copy(), m[3, :2].copy()
    return None


def _set_meter(dash, family: str, value: float, tach_max: float, speed_max: float) -> None:
    if family == "tach":
        for key in RPM_METERS:
            if dash.bound(key) is not None:
                dash.progress(key, value / tach_max)
    else:
        dash.progress("METER_SPEED", value / speed_max)


def _needles(dash, view, patches, tach_max: float, top_rpm: float) -> tuple[list, set]:
    """Needles the scene turns with the rpm or the speed, as DSF needles: a flat quad
    over the needle's rest pose, turned by the engine (gauge id 1 or 2) about the pivot
    the scene turns it about, at the scene's degrees per rpm or mph. Returns
    ([needle dicts], the elements they are made of)."""
    speed_max = dash.number("SPEED_MAX") or 200.0
    found, elements = [], set()
    for element, family in _turning(dash).items():
        if any(n["family"] == family for n in found):
            continue
        _rest_state(dash, tach_max)
        img = _layer_tree(dash, element, view)
        alpha = img[..., 3] / 255.0
        ys, xs = np.nonzero(alpha > 0.3)
        if len(xs) < 6:
            continue
        pts = np.stack([xs, ys], 1).astype(np.float64) / SCALE + (view[0], view[2])
        centre = pts.mean(0)
        _, vecs = np.linalg.eigh(np.cov((pts - centre).T))
        major, minor = vecs[:, 1], vecs[:, 0]
        a = (pts - centre) @ major
        b = (pts - centre) @ minor
        if (a.max() - a.min()) < NEEDLE_ASPECT * max(1.0, b.max() - b.min()):
            continue  # a cover or mask turning over a bar, not a needle
        top = top_rpm if family == "tach" else speed_max
        pose0 = _pose(dash, element)
        values = [top * k / NEEDLE_SAMPLES for k in range(1, NEEDLE_SAMPLES + 1)]
        poses = []
        for value in values:
            _set_meter(dash, family, value, tach_max, speed_max)
            poses.append(_pose(dash, element))
        _rest_state(dash, tach_max)
        if pose0 is None or any(q is None for q in poses):
            continue

        def turn(p, q):
            r = np.linalg.solve(p[0], q[0])  # local rotation from pose p to pose q
            return math.atan2(r[0, 1], r[0, 0])

        # the world point the rest and the full-scale pose both hold (scene units, y up)
        A = np.linalg.solve(pose0[0], poses[-1][0])
        t = poses[-1][1] - pose0[1] @ A
        try:
            fixed = np.linalg.solve(np.eye(2) - A.T, t)
        except np.linalg.LinAlgError:
            continue
        pivot = (fixed[0] + RT_W / 2, RT_H / 2 - fixed[1])  # render-target pixels
        # Angles from the rest pose, unwrapped step by step; DSF's needle is linear, so
        # fit angle = rest + rate x value over the upper three quarters (the range
        # driven in; non-linear faces such as the LFA's are off below it).
        angles, last = [], 0.0
        for q in poses:
            a_ = turn(pose0, q)
            while a_ - last > math.pi:
                a_ -= 2 * math.pi
            while a_ - last < -math.pi:
                a_ += 2 * math.pi
            angles.append(a_)
            last = a_
        sel = [k for k, v in enumerate(values) if v >= 0.25 * top]
        xs_, ys_ = np.array([values[k] for k in sel]), np.array([angles[k] for k in sel])
        rate, rest = np.polyfit(xs_, ys_, 1)
        full = rate * top
        p = next((q for q in patches if q.contains(*pivot)), None)
        if p is None or abs(full) < 1e-3:
            continue
        # the needle's quad turned to the fitted rest (0 rpm / 0 mph)
        c, sn = math.cos(rest), math.sin(rest)
        ends = [centre + major * a.min() + minor * b.min(), centre + major * a.max() + minor * b.min()]
        ends += [centre + major * a.max() + minor * b.max(), centre + major * a.min() + minor * b.max()]
        # render-target y is down: a scene angle r turns (dx, -dy) by r
        ends = [
            np.array(
                (
                    pivot[0] + (e[0] - pivot[0]) * c - (-(e[1] - pivot[1])) * sn,
                    pivot[1] - ((e[0] - pivot[0]) * sn + (-(e[1] - pivot[1])) * c),
                )
            )
            for e in ends
        ]
        corners = [p.point(float(x), float(y), NEEDLE_LIFT) for x, y in ends]
        colour = tuple(int(c) for c in (img[ys, xs, :3].mean(0)).round()) + (255,)
        # The scene turns counter-clockwise for positive angles (y up); seen on the
        # display the same, so a negative angle is clockwise for the driver.
        found.append(
            {
                "family": family,
                "patch": p,
                "pivot": pivot,  # render-target pixels
                "pivot3d": p.point(*pivot),
                "rt": [tuple(float(c) for c in e) for e in ends],
                "corners": corners,
                "colour": colour,
                "per_unit": float(abs(math.degrees(rate))),  # degrees per rpm or mph
                "rest_deg": float(math.degrees(rest)),
                "reverse": bool(full > 0),  # counter-clockwise as the driver sees it
                "top": float(top),
            }
        )
        elements |= _subtree(dash, element)
    return found, elements


def _layer_tree(dash, element: int, view):
    """An element and its subtree alone, RGBA at the render scale, over the view."""
    from dsfforza.convert import dash_scene as ds

    keep = _subtree(dash, element)
    return np.asarray(ds.render(dash, RT_W * SCALE, keep=lambda i: i in keep, alpha=True, view=view))


def build_plan(dash, patches: list, redline: float) -> DigitalPlan | None:
    """The DSF digital dials for a car's dash scene on its displays: the scene at rest
    as the displays' artwork (id 3), the speed digits (id 2) and gear (id 6) as cells of
    Forza's own glyphs, and everything the rpm lights up (bars, shift lights) as flat
    pieces lit from their rpm (id 1). Needles that turn with the rpm and readouts DSF
    has no value for stay out (research/cockpit.md, "Digital displays")."""
    from PIL import Image

    from dsfforza.convert import dash_scene as ds

    if not patches:
        return None
    tach_max = dash.number("TACH_MAX") or redline
    units = speed_units(dash)
    report: dict = {"tach_max": tach_max, "patches": len(patches), "units": units}
    # The part of the scene the displays show (render-target pixels, whole).
    wins = [p.window() for p in patches]
    ux0, ux1 = max(0, math.floor(min(w[0] for w in wins))), min(RT_W, math.ceil(max(w[1] for w in wins)))
    uy0, uy1 = max(0, math.floor(min(w[2] for w in wins))), min(RT_H, math.ceil(max(w[3] for w in wins)))
    if ux1 - ux0 < 4 or uy1 - uy0 < 4:
        return None
    view = (ux0, ux1, uy0, uy1)
    # Needles become DSF needles and leave the artwork.
    found, needles = _needles(dash, view, patches, tach_max, min(tach_max, redline))
    report["needles"] = [n["family"] for n in found]

    def keep_static(i):
        return i not in needles

    # 1. The scene at rest.
    _rest_state(dash, tach_max)
    base = np.asarray(ds.render(dash, RT_W * SCALE, keep=keep_static, view=view), np.float32) / 255.0
    W, H = TEXTURE_SIZE
    plan = DigitalPlan(W, H, bytearray(W * H * 4))

    # 2. Speed digits and gear: cells of the text element's glyphs.
    columns = []  # (kind, cell images (None = blank), slot rects in render-scale px)
    speed = dash.bound("TEXT_SPEED")
    if speed is not None:
        tmpl = "8" * SPEED_SLOTS
        cells = [None]
        for d in range(10):
            dash.text_override[speed] = tmpl[:-1] + str(d)
            cells.append(_layer(dash, speed, view, {speed: {SPEED_SLOTS - 1}}))
        dash.text_override[speed] = tmpl
        rects = [
            _bbox(_layer(dash, speed, view, {speed: {SPEED_SLOTS - 1 - j}})[..., 3] / 255.0) for j in range(SPEED_SLOTS)
        ]
        boxes = [b for b in (_bbox(c[..., 3] / 255.0) for c in cells[1:]) if b]
        _rest_state(dash, tach_max)
        if all(rects) and boxes:
            cell = (
                min(b[0] for b in boxes),
                max(b[1] for b in boxes),
                min(b[2] for b in boxes),
                max(b[3] for b in boxes),
            )
            # each slot: the '8' rect's offset applied to the cell box of slot 0
            slots = [(cell[0] + r[0] - rects[0][0], cell[1] + r[0] - rects[0][0], cell[2], cell[3]) for r in rects]
            columns.append(("speed", cells, cell, slots))
    gear = dash.bound("TEXT_GEAR")
    if gear is not None:
        cells = []
        for ch in GEAR_CHARS:
            dash.text_override[gear] = ch
            cells.append(_layer(dash, gear, view))
        _rest_state(dash, tach_max)
        boxes = [b for b in (_bbox(c[..., 3] / 255.0) for c in cells) if b]
        if boxes:
            cell = (
                min(b[0] for b in boxes),
                max(b[1] for b in boxes),
                min(b[2] for b in boxes),
                max(b[3] for b in boxes),
            )
            columns.append(("gear", cells, cell, [cell]))

    # 3. What the rpm changes: per pixel up to two changes (white -> green -> orange on
    # the LFA's ring), the step each happens at and the colour it brings. A pixel that
    # goes back to its rest colour (a needle sweeping past) stays at rest.
    # Texts don't change with the rpm (the rpm readout stays blank): left out, for speed.
    def keep_rpm(i):
        return keep_static(i) and dash.E[i].type != 4

    base1 = np.asarray(ds.render(dash, RT_W, keep=keep_rpm, view=view, ss=1), np.float32) / 255.0
    hgt, wid = base1.shape[:2]
    ev_step = np.full((CHANGES, hgt, wid), -1, np.int32)
    ev_col = np.zeros((CHANGES, hgt, wid, 3), np.float32)
    count = np.zeros((hgt, wid), np.int32)
    flicker = np.zeros((hgt, wid), bool)
    # Up to the Forza redline, which the bar reaches at DSF's (the engine feeds
    # rpm / p42 x 0.5: the bar shows the rpm as a share of the redline).
    top = min(tach_max, redline)
    steps = [top * (k + 1) / RPM_STEPS for k in range(RPM_STEPS)]
    meters = [k for k in RPM_METERS if dash.bound(k) is not None]
    if meters:
        cur = base1.copy()
        for k, rpm in enumerate(steps):
            _rest_state(dash, tach_max)
            for key in meters:
                dash.progress(key, rpm / tach_max)
            img = np.asarray(ds.render(dash, RT_W, keep=keep_rpm, view=view, ss=1), np.float32) / 255.0
            on = np.abs(img - base1).max(-1) > LIT_DIFFERENCE
            flicker |= (count > 0) & ~on
            changed = (np.abs(img - cur).max(-1) > LIT_DIFFERENCE) & on
            for level in range(CHANGES):
                sel = changed & (count == level)
                ev_step[level][sel] = k
                ev_col[level][sel] = img[sel]
            # while a pixel settles (fades in) its latest colour refines the change
            settle = ~changed & on & (count > 0)
            for level in range(CHANGES):
                sel = settle & (count == level + 1)
                ev_col[level][sel] = img[sel]
            count[changed] = np.minimum(count[changed] + 1, CHANGES)
            cur[changed] = img[changed]
        _rest_state(dash, tach_max)
    ev_step[:, flicker] = -1
    # Only what the displays' triangles show: Forza draws nothing of the scene off them.
    shown = coverage(patches, view, wid, hgt)
    report["rpm_pixels_off_display"] = int(((ev_step[0] >= 0) & ~shown).sum())
    ev_step[:, ~shown] = -1
    lit = ev_step[0] >= 0
    report["rpm_pixels"] = int(lit.sum())
    report["flicker_pixels"] = int(flicker.sum())

    # Texture: columns on the right, the artwork on the left. The digital dials draw
    # opaque (no alpha test), so every cell carries the artwork under it.
    x = W
    cols = []  # (kind, slot index, x, width)
    for kind, _, cell, slots in columns:
        pitch = int(DIGIT_PITCH * H) if kind == "speed" else int(GEAR_PITCH * H)
        aspect = (cell[1] - cell[0]) / max(1, cell[3] - cell[2])
        w = max(4, int(math.ceil((pitch - 2 * CELL_MARGIN) * aspect / 4.0)) * 4) + 2 * CELL_MARGIN
        for j in range(len(slots)):
            x -= w
            cols.append((kind, j, x, w))
    # Bar columns: one per (rest, first, second colour, steps between the changes).
    signatures, sig_img = {}, None
    if lit.any():
        states = [base1[lit]] + [ev_col[level][lit] for level in range(CHANGES)]
        palette, labels = _palette(np.concatenate(states), PALETTE)
        n = int(lit.sum())
        lab = [labels[i * n : (i + 1) * n] for i in range(CHANGES + 1)]
        later = [np.where(ev_step[level][lit] >= 0, lab[level + 1], -1) for level in range(1, CHANGES)]
        gaps = [
            np.where(ev_step[level][lit] >= 0, ev_step[level][lit] - ev_step[0][lit], -1) for level in range(1, CHANGES)
        ]
        keys = np.stack([lab[0], lab[1], *later, *gaps], 1)
        uniq, inverse, freq = np.unique(keys, axis=0, return_counts=True, return_inverse=True)
        room = max(0, min(MAX_BAR_COLUMNS, (x - 256) // COLUMN))
        order = np.argsort(-freq)
        keep = set(order[:room].tolist())
        # signatures that don't fit fold onto their first change only
        for s_idx in range(len(uniq)):
            if s_idx in keep:
                signatures[s_idx] = tuple(int(v) for v in uniq[s_idx])
        fold = {}
        for s_idx in range(len(uniq)):
            if s_idx not in keep:  # the most common kept column with the same first change
                same = [k for k in order[:room] if tuple(uniq[k][:2]) == tuple(uniq[s_idx][:2])]
                fold[s_idx] = int(same[0]) if same else -1
        inverse = np.array([fold.get(int(k), int(k)) for k in inverse.ravel()])
        sig_img = np.full((hgt, wid), -1, np.int32)
        sig_img[lit] = inverse
        x -= COLUMN * len(signatures)
        bar_x = x
        report["bar_columns"] = len(signatures)
        report["bar_signatures"] = int(len(uniq))
    bg_w = (x // 4) * 4
    if bg_w < 64:
        return None
    # The artwork: the union of the displays' windows.
    k_bg = min(bg_w / max(1e-6, ux1 - ux0), H / max(1e-6, uy1 - uy0))
    bw, bh = max(1, int((ux1 - ux0) * k_bg)), max(1, int((uy1 - uy0) * k_bg))
    full = Image.fromarray((np.clip(base, 0, 1) * 255 + 0.5).astype(np.uint8))
    plan.blit(0, 0, full.convert("RGBA").resize((bw, bh), Image.LANCZOS))
    report["artwork"] = [bw, bh]

    def art_uv(px, py):
        return ((px - ux0) * k_bg / W, (py - uy0) * k_bg / H)

    for p in patches:
        for tri in p.uv0:
            pts = [p.rt(uv) for uv in tri]
            corners = [p.point(*q) for q in pts]
            uv = [art_uv(*q) for q in pts]
            plan.quads.append(Quad([*corners, corners[2]], [*uv, uv[2]], STATIC))
            plan.quads[-1].rt = [*pts, pts[2]]

    def patch_for(px, py):
        return next((p for p in patches if p.covers(px, py)), None)

    def rect_quad(p, rect, uvbox, slot, lift):
        x0, x1, y0, y1 = rect
        corners = [p.point(cx, cy, lift) for cx, cy in ((x0, y1), (x1, y1), (x1, y0), (x0, y0))]
        u0, v0, u1, v1 = uvbox  # texture box: (u0, v0) top left
        plan.quads.append(Quad(corners, [(u0, v1), (u1, v1), (u1, v0), (u0, v0)], slot))
        plan.quads[-1].rt = [(x0, y1), (x1, y1), (x1, y0), (x0, y0)]
        return plan.quads[-1]

    # Digit and gear cells: cell 0 at the top (blank: the artwork), the engine adds the
    # offset. Each slot has its own column: the artwork under it differs.
    base_img = (np.clip(base, 0, 1) * 255 + 0.5).astype(np.uint8)
    placed: dict = {}
    shown_x = {kind: _shown_spans(slots) for kind, _, _, slots in columns}
    for kind, j, cx, cw in cols:
        _, cells, cell, slots = next(c for c in columns if c[0] == kind)
        r = slots[j]
        pitch = DIGIT_PITCH * H if kind == "speed" else GEAR_PITCH * H
        ch = int(pitch) - 2 * CELL_MARGIN
        under = base_img[max(0, r[2]) : r[3], max(0, r[0]) : r[1], :3].astype(np.float32)
        for n, img in enumerate(cells):
            out = under.copy()
            if img is not None:
                g = img[cell[2] : cell[3], cell[0] : cell[1]].astype(np.float32)
                g = g[: out.shape[0], : out.shape[1]]
                a = g[..., 3:4] / 255.0
                out[: g.shape[0], : g.shape[1]] = out[: g.shape[0], : g.shape[1]] * (1 - a) + g[..., :3] * a
            tile = Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8)).convert("RGBA")
            tile = tile.resize((cw - 2 * CELL_MARGIN, ch), Image.LANCZOS)
            y = int(round(n * pitch)) + CELL_MARGIN
            plan.blit(cx + CELL_MARGIN, y, tile)
            # the margins repeat the cell's edge texels: filtering at its border stays inside it
            t = np.asarray(tile)
            for m in range(CELL_MARGIN):
                plan.blit(cx + CELL_MARGIN, y - 1 - m, Image.fromarray(t[:1]))
                plan.blit(cx + CELL_MARGIN, y + ch + m, Image.fromarray(t[-1:]))
        # Neighbouring slots whose cells overlap (wide glyphs, tight tracking) meet half way:
        # two quads in one plane z-fight where they overlap.
        a, b = shown_x[kind][j]
        u0, u1 = (cx + CELL_MARGIN) / W, (cx + cw - CELL_MARGIN) / W
        uvbox = (
            u0 + (u1 - u0) * (a - r[0]) / (r[1] - r[0]),
            CELL_MARGIN / H,
            u0 + (u1 - u0) * (b - r[0]) / (r[1] - r[0]),
            (CELL_MARGIN + ch) / H,
        )
        digits = KMH_DIGITS if units == "kmh" else SPEED_DIGITS
        slot = digits[j] if kind == "speed" else GEAR
        rr = (ux0 + a / SCALE, ux0 + b / SCALE, uy0 + r[2] / SCALE, uy0 + r[3] / SCALE)
        p = patch_for((rr[0] + rr[1]) / 2, (rr[2] + rr[3]) / 2)
        if p is not None:
            rect_quad(p, rr, uvbox, slot, DIGIT_LIFT)
            placed[kind] = placed.get(kind, 0) + 1
    report.update(placed)

    # Bar pieces: pixels with the same column and first change step, as rectangles; a
    # piece's v puts its first change at its rpm (share of the redline), the column the
    # later one the same number of steps on.
    if signatures:
        step_v = 0.5 * top / RPM_STEPS / max(1.0, redline)
        for col, sig in enumerate(signatures.values()):
            cx0 = bar_x + col * COLUMN
            c0, c1 = palette[sig[0]], palette[sig[1]]
            rows = [(0, BAR_LIT_FROM, c0), (BAR_LIT_FROM, 1.0, c1)]
            later, gaps = sig[2 : 2 + CHANGES - 1], sig[2 + CHANGES - 1 :]
            for lab_, gap in zip(later, gaps, strict=True):
                if lab_ >= 0 and gap > 0:
                    rows.append((BAR_LIT_FROM + gap * step_v, 1.0, palette[lab_]))
            for v0, v1, c in rows:
                plan.fill(cx0, int(math.ceil(v0 * H)), cx0 + COLUMN, int(math.ceil(v1 * H)), _rgba(c))
        col_of = {s_idx: k for k, s_idx in enumerate(signatures)}
        first = ev_step[0]
        pieces = 0
        taken = lit.copy()  # pixels a piece may not grow into: other steps' and earlier pieces'
        for k in range(RPM_STEPS):
            at_k = first == k
            if not at_k.any():
                continue
            v = BAR_LIT_FROM - min(BAR_MAX, 0.5 * steps[k] / max(1.0, redline))
            # Pieces lie in one plane: one reaching into another's pixels z-fights with it.
            # A piece may grow over unlit pixels (fewer pieces), not over another step's.
            rects = _runs_to_rects(at_k, forbid=taken)
            for x0, x1, y0, y1 in rects:
                taken[y0:y1, x0:x1] = True
            for x0, x1, y0, y1 in rects:
                sigs = sig_img[y0:y1, x0:x1][at_k[y0:y1, x0:x1]]
                sigs = sigs[sigs >= 0]
                if not len(sigs):
                    continue
                s_idx = int(np.bincount(sigs).argmax())  # the rectangle's most common column
                if s_idx not in col_of:
                    continue
                u = (bar_x + col_of[s_idx] * COLUMN + COLUMN / 2) / W
                rect = (float(ux0 + x0), float(ux0 + x1), float(uy0 + y0), float(uy0 + y1))
                p = patch_for((rect[0] + rect[1]) / 2, (rect[2] + rect[3]) / 2)
                if p is None:
                    continue
                rect_quad(p, rect, (u, v, u, v), RPM, PIECE_LIFT)
                pieces += 1
        report["bar_pieces"] = pieces
    # Dim pale and glaring displays (DISPLAY_MEAN_LUMINANCE); dark ones stay as they are.
    big = np.repeat(np.repeat(shown, SCALE, 0), SCALE, 1)[: base.shape[0], : base.shape[1]]
    lum = base[: big.shape[0], : big.shape[1], :3] @ np.array([0.299, 0.587, 0.114], np.float32) * 255
    if big.any():
        mean, peak = float(lum[big].mean()), float(np.percentile(lum[big], 95))
        gain = min(1.0, DISPLAY_MEAN_LUMINANCE / max(1.0, mean), DISPLAY_PEAK_LUMINANCE / max(1.0, peak))
        report["luminance"] = [round(mean, 1), round(peak, 1)]
        if gain < 1.0:
            texels = np.frombuffer(bytes(plan.rgba), np.uint8).reshape(-1, 4).copy()
            texels[:, :3] = np.clip(texels[:, :3] * gain + 0.5, 0, 255).astype(np.uint8)
            plan.rgba = bytearray(texels.tobytes())
            report["gain"] = round(gain, 3)
    plan.report = report
    plan.needles = found
    plan.patches = patches
    return plan


def digi_data(mph: float, rpm: float, gear: int, redline: float) -> dict:
    """g_fDigiData as FUN_00891670 fills it: (id, comp) -> v offset."""
    out = {RPM: 0.5 * min(rpm, redline) / max(1.0, redline), GEAR: gear / 7.0, (7, 0): 0.5 if gear else 0.0}
    for slots, value in ((SPEED_DIGITS, mph), (KMH_DIGITS, mph * 1.609344)):  # km/h: dsfforza.asi
        digits = str(int(round(abs(value))))
        for k, slot in enumerate(slots):
            out[slot] = (int(digits[-1 - k]) + 1) * DIGIT_PITCH if k < len(digits) else 0.0
    return out


def preview(
    plan: DigitalPlan,
    mph: float,
    rpm: float,
    gear: int,
    redline: float,
    window=None,
    scale: float = 1.0,
    label: bool = True,
):
    """The displays as the engine would draw the plan (render-target space, alpha test
    0.5), for offline checks."""
    from PIL import Image, ImageDraw

    tex = np.frombuffer(bytes(plan.rgba), np.uint8).reshape(plan.height, plan.width, 4)
    digi = digi_data(mph, rpm, gear, redline)
    pts = [p for q in plan.quads for p in q.rt]
    x0, x1, y0, y1 = window or (
        min(p[0] for p in pts),
        max(p[0] for p in pts),
        min(p[1] for p in pts),
        max(p[1] for p in pts),
    )
    w, h = int((x1 - x0) * scale) + 1, int((y1 - y0) * scale) + 1
    out = np.zeros((h, w, 3), np.uint8)
    gx, gy = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    for q in plan.quads:
        uvs = q.uvs
        off = digi.get(q.slot, 0.0)
        for tri in ((0, 1, 2), (0, 2, 3)):
            P = [((q.rt[i][0] - x0) * scale, (q.rt[i][1] - y0) * scale) for i in tri]
            (ax, ay), (bx, by), (cx, cy) = P
            d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(d) < 1e-9:
                continue
            bx0, bx1 = int(max(0, min(ax, bx, cx))), int(min(w, max(ax, bx, cx) + 1))
            by0, by1 = int(max(0, min(ay, by, cy))), int(min(h, max(ay, by, cy) + 1))
            if bx1 <= bx0 or by1 <= by0:
                continue
            X, Y = gx[by0:by1, bx0:bx1], gy[by0:by1, bx0:bx1]
            l1 = ((by - cy) * (X - cx) + (cx - bx) * (Y - cy)) / d
            l2 = ((cy - ay) * (X - cx) + (ax - cx) * (Y - cy)) / d
            l3 = 1 - l1 - l2
            m = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
            U = l1 * uvs[tri[0]][0] + l2 * uvs[tri[1]][0] + l3 * uvs[tri[2]][0]
            V = l1 * uvs[tri[0]][1] + l2 * uvs[tri[1]][1] + l3 * uvs[tri[2]][1] + off
            tx = np.clip((U % 1.0) * plan.width, 0, plan.width - 1).astype(int)
            ty = np.clip((V % 1.0) * plan.height, 0, plan.height - 1).astype(int)
            texel = tex[ty, tx]
            m &= texel[..., 3] >= 128
            region = out[by0:by1, bx0:bx1]
            region[m] = texel[..., :3][m]
    for n in plan.needles:  # turned as the engine turns them (clockwise unless reverse)
        value = min(rpm, redline) if n["family"] == "tach" else mph
        deg = n["per_unit"] * value * (1 if n["reverse"] else -1)  # y down: negative is clockwise
        c, sn = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        px, py = n["pivot"]
        poly = []
        for x, y in n["rt"]:
            dx, dy = x - px, -(y - py)
            poly.append(((px + dx * c - dy * sn - x0) * scale, (py - (dx * sn + dy * c) - y0) * scale))
        pil = Image.fromarray(out)
        ImageDraw.Draw(pil).polygon(poly, fill=n["colour"][:3])
        out = np.asarray(pil).copy()
    img = Image.fromarray(out)
    if label:
        ImageDraw.Draw(img).text((4, 4), f"{mph:.0f} mph {rpm:.0f} rpm gear {gear}", fill=(255, 255, 0))
    return img
