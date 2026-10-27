"""Live digital displays in the cockpit view, through DSF's own digital dials.

Driver: San Francisco has a live digital-dashboard path of its own, used by 14
stock interiors (Volt, LP670, Alfa 8C, ...; research/cockpit.md, "Digital
displays"): materials `*_Digital_Dials.matxml` whose vertex shader adds
`g_fDigiData[(id - 1) * 4 + comp]` to the vertex's v texture coordinate. The
vertex format is the needles' (DashboardInstrument (u, v, id, comp)), and the
game fills, every frame (FUN_00891670):

    id 1, comp 0: rpm / redline x 0.5
    id 2, comp 0..3: mph digits, units to thousands: (digit + 1) x 23/256,
                     0 for a leading blank
    id 6, comp 0: gear / 7 (0 = reverse)
    id 7, comp 0: 0.5 when the gear isn't reverse
    ids 3..5: never written (0)

So a digit is a quad over the blank cell of a strip of 11 cells (blank, 0..9,
23/256 apart), a gear a quad over the first of 7 cells, and an rpm bar segment
a quad whose single v samples a column that is transparent below a threshold
and lit above it (alpha-tested away when transparent). Static artwork uses id
3 (no offset). Everything samples one texture per car, built here.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

import numpy as np

# The stock Volt's digital material (entry 148, Chevrolet_Volt_2011_Digital_Dials.matxml):
# alpha test 0.5, g_texDiffuseMap b012ecfc52fb762a. Its submodel sits in the
# interior's needle part with MODL flag 1, in a needle-layout buffer.
STOCK_MATERIAL = bytes.fromhex("e012cc80f0aa8829")
STOCK_TEXTURE = bytes.fromhex("b012ecfc52fb762a")
MODL_FLAG = 1

RPM = (1, 0)  # (id, comp): rpm / redline x 0.5
SPEED_DIGITS = [(2, k) for k in range(4)]  # units, tens, hundreds, thousands
GEAR = (6, 0)  # gear / 7, 0 = reverse
STATIC = (3, 0)  # never written: no offset
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


def rgba_dds(width: int, height: int, rgba: bytes | bytearray) -> bytes:
    """An uncompressed single-mip DDS (A8R8G8B8: BGRA bytes) of RGBA texels.
    DSF's DXTC loader maps 32-bit RGB DDS to D3DFMT_A8R8G8B8 and never drops
    the top mip of a single-mip texture."""
    head = struct.pack(
        "<4s7I44s2I4s5I5I",
        b"DDS ",
        124,
        0x100F,  # caps, height, width, pitch, pixel format
        height,
        width,
        width * 4,
        0,
        1,
        bytes(44),
        32,
        0x41,  # RGB | alpha
        b"\0\0\0\0",
        32,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        0xFF000000,
        0x1000,  # texture
        0,
        0,
        0,
        0,
    )
    bgra = bytearray(rgba)
    bgra[0::4], bgra[2::4] = rgba[2::4], rgba[0::4]
    return head + bytes(bgra)


def dxt5_dds(width: int, height: int, rgba: bytes | bytearray, template: bytes | None = None) -> bytes:
    """A single-mip DXT5 DDS (the stock digital textures are DXT5/DXT1; a single
    mip is never dropped by the texture-quality setting and keeps the cells and
    bar columns from bleeding into each other)."""
    from dsfforza.convert import dsf_materials as dm

    return dm.dds_file(rgba, width, height, 1, template)


_STOCK: dict = {}


def stock_material(stream):
    """The stock Volt digital MACO (Entry) and its TEXC, from the source archive."""
    key = id(stream)
    if key not in _STOCK:
        from dsfforza.convert import dsf_materials as dm
        from dsfforza.formats import chnk

        root = chnk.read_chunk(stream, 0)
        found = None
        for e in root.entries:
            for _, c in chnk.walk([e]):
                if c.tag == "MACO" and c.is_chunk and dm.material_info(stream, c)["uid"] == STOCK_MATERIAL:
                    found = c
                    break
            if found is not None:
                break
        if found is None:
            raise ValueError("stock digital material e012cc80f0aa8829 (Volt) not in the source archive")
        _STOCK[key] = found
    return _STOCK[key]


def bar_v(rpm: float, redline: float) -> float:
    """The v of a bar piece lit from `rpm` on: the engine adds rpm / redline x 0.5."""
    return BAR_LIT_FROM - min(BAR_MAX, 0.5 * rpm / max(1.0, redline))


def vertices(plan: DigitalPlan, place, stride: int, redline: float) -> tuple[list[bytes], list[tuple[int, int, int]]]:
    """Needle-layout vertices and double-sided triangles for the plan's quads (bar
    pieces lit from their rpm with this redline, VO3D p42)."""
    from dsfforza.convert import cockpit

    verts, tris = [], []
    for q in plan.quads:
        uvs = q.uvs
        if getattr(q, "rpm", None) is not None:  # a bar piece: lit from its rpm on
            v = bar_v(q.rpm, redline)
            uvs = [(u, v) for u, _ in uvs]
        a, b, c = q.corners[0], q.corners[1], q.corners[2]
        n = cockpit._norm(cockpit._cross(cockpit._sub(b, a), cockpit._sub(c, a)))
        base = len(verts)
        for p, uv in zip(q.corners, uvs, strict=True):
            verts.append(cockpit._needle_vertex(place.point(p), uv, q.slot[0], place.normal(n), stride, q.slot[1]))
        tris += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
        tris += [(base, base + 2, base + 1), (base, base + 3, base + 2)]
    return verts, tris


def prototype_plan(display, version: int = 3) -> DigitalPlan:
    """In-game test of the mechanism on one display (cockpit.Display): a static
    background (id 3), three speed digits, a gear and a ten-segment rpm bar,
    drawn with a stock font. Not Forza's artwork."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 256, 512
    plan = DigitalPlan(W, H, bytearray(W * H * 4))
    import os

    if os.environ.get("DSF_DD_VARIANT") == "opaque":  # in-game test: every texel opaque
        for y in range(H):
            plan.fill(0, y, W, y + 1, (255 * y // H, 80, 255 - 255 * y // H, 255))
    font = ImageFont.load_default(size=36)

    def cell(text, w, h, colour=(255, 200, 40, 255)):
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        if text:
            box = d.textbbox((0, 0), text, font=font)
            d.text(
                ((w - (box[2] - box[0])) / 2 - box[0], (h - (box[3] - box[1])) / 2 - box[1]),
                text,
                font=font,
                fill=colour,
            )
        return im

    # Digits: column x 0..40, cells of DIGIT_PITCH * H texels (blank, 0..9).
    pitch = DIGIT_PITCH * H
    for k, t in enumerate([""] + [str(i) for i in range(10)]):
        plan.blit(0, round(k * pitch), cell(t, 40, int(pitch) - 2))
    # Gear: column x 48..88, cells R, 1..6.
    gp = GEAR_PITCH * H
    for k, t in enumerate("R123456"):
        plan.blit(48, round(k * gp), cell(t, 40, int(gp) - 2, (120, 230, 255, 255)))
    # Bar: column x 96..104 lit from BAR_LIT_FROM.
    plan.fill(96, round(BAR_LIT_FROM * H), 104, H, (60, 255, 60, 255))
    # Background: x 128..256, a flat dark blue.
    plan.fill(128, 0, 256, 64, (20, 40, 70, 255))

    u0, u1, v0, v1 = display.rect
    lift = 0.0015

    def rect(x0, x1, y0, y1, uvs, slot, dz):
        corners = [display.point(x, y, dz) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        plan.quads.append(Quad(corners, uvs, slot))

    def tex_rect(x0, y0, x1, y1):  # texel box -> uv corners (bottom-left, bottom-right, top-right, top-left)
        return [(x0 / W, y1 / H), (x1 / W, y1 / H), (x1 / W, y0 / H), (x0 / W, y0 / H)]

    rect(u0, u1, v0, v1, tex_rect(130, 2, 254, 62), STATIC, lift)
    w, h = u1 - u0, v1 - v0
    dh = 0.45 * h
    dw = dh * 40 / (pitch - 2)
    for j in range(3):  # units on the right
        x1 = u1 - 0.06 * w - j * dw
        rect(x1 - dw, x1, v0 + 0.1 * h, v0 + 0.1 * h + dh, tex_rect(0, 0, 40, pitch - 2), SPEED_DIGITS[j], 2 * lift)
    gx = u0 + 0.06 * w
    rect(gx, gx + dw, v0 + 0.1 * h, v0 + 0.1 * h + dh, tex_rect(48, 0, 88, gp - 2), GEAR, 2 * lift)
    seg = 0.8 * w / 10
    for k in range(10):
        t = min(BAR_MAX, 0.5 * (k + 1) / 10)
        vb = (BAR_LIT_FROM - t) * H
        x0 = u0 + 0.1 * w + k * seg
        rect(x0, x0 + 0.8 * seg, v1 - 0.25 * h, v1 - 0.08 * h, [(100 / W, vb / H)] * 4, RPM, 2 * lift)
    plan.report = {"prototype": True, "display": display.section}
    return plan


# --- Forza's dash scenes -> DSF digital dials ------------------------------------------
TEXTURE_SIZE = (512, 512)
RT_W, RT_H = 1280, 720  # the scene's presentation (render-target) size
SCALE = 2  # render-target pixels are drawn this many texels wide before fitting
RPM_STEPS = 40  # rpm thresholds tried for the bar pieces (0 .. the scene's tach full scale)
LIT_DIFFERENCE = 0.06  # a pixel is lit when its colour moved this far from the rest state
PALETTE = 12  # flat colours of the bar pieces (one texture column each)
COLUMN = 4  # texels per bar colour column (a DXT block)
CELL_MARGIN = 2  # texels kept clear round each digit/gear cell
DIGIT_LIFT = 0.0008  # m towards the eye: digits and bars over the drawn display
LAYERS = 3  # colour changes per pixel drawn for the rpm (rest -> lit -> warning -> redline)
LAYER_LIFT = 0.0003  # m: each later change over the one before
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


def _runs_to_rects(mask, slack: int = 2):
    """Rectangles (x0, x1, y0, y1), pixel edges, covering a boolean mask: runs per row,
    merged down while a run stays within `slack` pixels of the rectangle above (which
    then grows to hold it)."""
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
                (r for r in open_ if r[3] == y - 1 and abs(r[0] - x0) <= slack and abs(r[1] - x1) <= slack), None
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


def _subtree(dash, root: int) -> set:
    out, todo = set(), [root]
    while todo:
        i = todo.pop()
        out.add(i)
        todo += dash.children.get(i, [])
    return out


def _rest_state(dash, tach_max: float) -> None:
    dash.text_override.clear()
    dash.apply_state(0.0, 0.0, "", tach_max_default=tach_max)
    for key in ("TEXT_SPEED", "TEXT_GEAR", *BLANK_TEXT):
        dash.text(key, "")


def _layer(dash, element: int, view, only=None):
    """One element alone, RGBA at the render scale, over the view."""
    from dsfforza.convert import dash_scene as ds

    return np.asarray(ds.render(dash, RT_W * SCALE, keep=lambda i: i == element, only=only, alpha=True, view=view))


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
    report: dict = {"tach_max": tach_max, "patches": len(patches)}
    needles: set = set()  # elements left out of the artwork (none yet)

    def keep_static(i):
        return i not in needles

    # The part of the scene the displays show (render-target pixels, whole).
    wins = [p.window() for p in patches]
    ux0, ux1 = max(0, math.floor(min(w[0] for w in wins))), min(RT_W, math.ceil(max(w[1] for w in wins)))
    uy0, uy1 = max(0, math.floor(min(w[2] for w in wins))), min(RT_H, math.ceil(max(w[3] for w in wins)))
    if ux1 - ux0 < 4 or uy1 - uy0 < 4:
        return None
    view = (ux0, ux1, uy0, uy1)

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

    # 3. What the rpm lights up: per pixel the first step it changes at.
    # Texts don't change with the rpm (the rpm readout stays blank): left out, for speed.
    def keep_rpm(i):
        return keep_static(i) and dash.E[i].type != 4

    base1 = np.asarray(ds.render(dash, RT_W, keep=keep_rpm, view=view, ss=1), np.float32) / 255.0
    # Per pixel up to LAYERS changes (white -> green -> orange on the LFA's ring): the
    # step each happens at and the colour it brings. A pixel that goes back to its rest
    # colour (a needle sweeping past) can't be drawn this way and is left at rest.
    ev_step = np.full((LAYERS, *base1.shape[:2]), -1, np.int32)
    ev_col = np.zeros((LAYERS, *base1.shape), np.float32)
    count = np.zeros(base1.shape[:2], np.int32)
    flicker = np.zeros(base1.shape[:2], bool)
    # Up to the redline: the engine's rpm input stops there (FUN_00891670 clamps to p42),
    # and some scenes flash past it.
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
            for level in range(LAYERS):
                sel = changed & (count == level)
                ev_step[level][sel] = k
                ev_col[level][sel] = img[sel]
            # the latest colour of a change refines it while the pixel settles (fades)
            settle = ~changed & on & (count > 0)
            for level in range(LAYERS):
                sel = settle & (count == level + 1)
                ev_col[level][sel] = img[sel]
            count[changed] = np.minimum(count[changed] + 1, LAYERS)
            cur[changed] = img[changed]
        _rest_state(dash, tach_max)
    ev_step[:, flicker] = -1
    lit_any = ev_step >= 0
    report["rpm_pixels"] = int(lit_any[0].sum())
    report["flicker_pixels"] = int(flicker.sum())

    # Texture: columns on the right, the artwork on the left.
    x = W
    col_x = {}
    for kind, _, cell, _ in columns:
        pitch = int(DIGIT_PITCH * H) if kind == "speed" else int(GEAR_PITCH * H)
        aspect = (cell[1] - cell[0]) / max(1, cell[3] - cell[2])
        w = max(4, int(math.ceil((pitch - 2 * CELL_MARGIN) * aspect / 4.0)) * 4) + 2 * CELL_MARGIN
        x -= w
        col_x[kind] = (x, w)
    palette, labels, bar_x = None, None, x
    if lit_any.any():
        palette, labels = _palette(ev_col[lit_any], PALETTE)
        x -= COLUMN * len(palette)
        bar_x = x
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
        return next((p for p in patches if p.contains(px, py)), None)

    def rect_quad(p, rect, uvbox, slot, lift):
        x0, x1, y0, y1 = rect
        corners = [p.point(cx, cy, lift) for cx, cy in ((x0, y1), (x1, y1), (x1, y0), (x0, y0))]
        u0, v0, u1, v1 = uvbox  # texture box: (u0, v0) top left
        plan.quads.append(Quad(corners, [(u0, v1), (u1, v1), (u1, v0), (u0, v0)], slot))
        plan.quads[-1].rt = [(x0, y1), (x1, y1), (x1, y0), (x0, y0)]
        return plan.quads[-1]

    # Digit and gear cells: cell 0 at the top, the engine adds the offset.
    for kind, cells, cell, slots_px in columns:
        cx, cw = col_x[kind]
        pitch = DIGIT_PITCH * H if kind == "speed" else GEAR_PITCH * H
        ch = int(pitch) - 2 * CELL_MARGIN
        for n, img in enumerate(cells):
            if img is None:
                continue
            sub = Image.fromarray(img).crop((cell[0], cell[2], cell[1], cell[3]))
            plan.blit(
                cx + CELL_MARGIN,
                int(round(n * pitch)) + CELL_MARGIN,
                sub.resize((cw - 2 * CELL_MARGIN, ch), Image.LANCZOS),
            )
        uvbox = ((cx + CELL_MARGIN) / W, CELL_MARGIN / H, (cx + cw - CELL_MARGIN) / W, (CELL_MARGIN + ch) / H)
        ids = SPEED_DIGITS[:SPEED_SLOTS] if kind == "speed" else [GEAR]
        placed = 0
        for slot, r in zip(ids, slots_px, strict=True):
            rr = (ux0 + r[0] / SCALE, ux0 + r[1] / SCALE, uy0 + r[2] / SCALE, uy0 + r[3] / SCALE)
            p = patch_for((rr[0] + rr[1]) / 2, (rr[2] + rr[3]) / 2)
            if p is not None:
                rect_quad(p, rr, uvbox, slot, DIGIT_LIFT)
                placed += 1
        report[kind] = placed

    # Bar pieces: per step and colour, rectangles over the newly lit pixels.
    if palette is not None:
        for j, c in enumerate(palette):
            col = (*(int(round(float(v) * 255)) for v in c), 255)
            plan.fill(bar_x + j * COLUMN, int(math.ceil(BAR_LIT_FROM * H)), bar_x + (j + 1) * COLUMN, H, col)
        label_img = np.full(ev_step.shape, -1, np.int32)
        label_img[lit_any] = labels
        pieces = 0
        for level in range(LAYERS):
            for k in range(RPM_STEPS):
                at = ev_step[level] == k
                if not at.any():
                    continue
                for x0, x1, y0, y1 in _runs_to_rects(at):
                    labs = label_img[level, y0:y1, x0:x1][at[y0:y1, x0:x1]]
                    if not len(labs):
                        continue
                    j = int(np.bincount(labs).argmax())
                    u = (bar_x + j * COLUMN + COLUMN / 2) / W
                    rect = (float(ux0 + x0), float(ux0 + x1), float(uy0 + y0), float(uy0 + y1))
                    p = patch_for((rect[0] + rect[1]) / 2, (rect[2] + rect[3]) / 2)
                    if p is None:
                        continue
                    q = rect_quad(p, rect, (u, 0.0, u, 0.0), RPM, DIGIT_LIFT + level * LAYER_LIFT)
                    q.rpm = steps[k]
                    pieces += 1
        report["bar_pieces"] = pieces
        report["bar_colours"] = len(palette)
    plan.report = report
    return plan


def digi_data(mph: float, rpm: float, gear: int, redline: float) -> dict:
    """g_fDigiData as FUN_00891670 fills it: (id, comp) -> v offset."""
    out = {RPM: 0.5 * min(rpm, redline) / max(1.0, redline), GEAR: gear / 7.0, (7, 0): 0.5 if gear else 0.0}
    digits = str(int(round(abs(mph))))
    for k, slot in enumerate(SPEED_DIGITS):
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
        if getattr(q, "rpm", None) is not None:
            v = bar_v(q.rpm, redline)
            uvs = [(u, v) for u, _ in uvs]
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
    img = Image.fromarray(out)
    if label:
        ImageDraw.Draw(img).text((4, 4), f"{mph:.0f} mph {rpm:.0f} rpm gear {gear}", fill=(255, 255, 0))
    return img


def retune(stream, interior, report: dict, redline: float) -> int:
    """Re-light the bar pieces of a built interior for the redline the handling pass
    set (VO3D p42; convert_interior used `report["redline"]`): each piece's v holds
    BAR_LIT_FROM - rpm / redline x 0.5. Returns the vertices changed."""
    from dsfforza.formats.vehicle_mesh import child, payload

    old = float(report.get("redline") or 0.0)
    if not old or abs(old - redline) < 0.5:
        return 0
    material = bytes.fromhex(report["material"])
    drsp = child(child(interior, "MDXN"), "DRSP")
    subs = []
    for e in drsp.children:
        if e.tag == "SBMC":
            d = payload(stream, child(e, "SBBH"))
            if d[8:16] == material:
                subs.append((d[16:24], struct.unpack_from("<I", d, 32)[0], *struct.unpack_from("<II", d, 56)))
    if not subs:
        return 0
    ixck = next(e for e in drsp.children if e.tag == "IXCK")
    indices = payload(stream, child(ixck, "IXDT"))
    changed = 0
    for e in drsp.children:
        if e.tag != "VXCK":
            continue
        hd = payload(stream, child(e, "VXHD"))
        stride = struct.unpack_from("<I", hd, 12)[0]
        mine = [s for s in subs if s[0] == hd[:8]]
        if not mine:
            continue
        data = bytearray(payload(stream, child(e, "VXDT")))
        seen = set()
        for _, base, first, count in mine:
            for k in struct.unpack_from(f"<{count}H", indices, first * 2):
                v = base + k
                if v in seen:
                    continue
                seen.add(v)
                _, vv, gid, comp = struct.unpack_from("<4e", data, v * stride + 8)
                if (int(gid), int(comp)) != RPM:
                    continue
                rpm = (BAR_LIT_FROM - vv) / 0.5 * old
                struct.pack_into("<e", data, v * stride + 10, bar_v(rpm, redline))
                changed += 1
        child(e, "VXDT").payload = bytes(data)
    report["redline"] = round(redline, 1)
    return changed
