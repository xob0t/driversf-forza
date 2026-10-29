"""Licence plates for converted Forza cars (research/plates.md).

How vanilla DSF cars draw their plates (confirmed-static, all 150 vanilla cars):

- Every plate is a flat quad (2 to 66 triangles) drawn with the shared paint
  material (GLOBAL_Chassis in the full entry, GLOBAL_Chassis_LOD1 in the
  exterior; 255 of 272 plates; the rest GLOBAL_LOD_Chrome or
  GLOBAL_Chassis_Simple), whose texture is the shared 1024 x 512 atlas
  00122b73d1fa1227. UV0 covers the "SAN FRANCISCO / DRV SF" plate in that
  atlas: u 0.4929..0.5986, v 0.8867..0.9971 (texels 505..613 x 454..510),
  upright (v grows downwards on the plate), read from outside: on a rear plate
  u grows towards -x, on a front plate towards +x (DSF +x is the car's left).
- The quad is about 0.26 x 0.12 m (median; 0.23 to 0.37 wide), vertical or
  following the panel's tilt, a few millimetres proud of the panel.
- Vertex semantics are the panel's: s9 = the bumper (front: role 35 on 121 of
  129; rear: 36 on 73, boot lid 5 on 34, chassis 38 on 32), s12 crush targets
  and s5 normal deltas like the surrounding bumper, UV1 on the bumper's tile
  of the scratch atlas, sky term about 0.5. The plate is in part 0 and in the
  panel's own MODL part, so it crushes, swings and falls off with its panel.
- 130 vanilla cars have front and rear plates, 11 rear only, 1 front only,
  8 none (trucks, buses, buggies).

Forza cars mostly have no plate geometry: in tools/batches/testdrive.txt only
DOD_Durango_99 and CHE_Express2500Cargo_05 carry plate quads (technique
`numberplate`); `numplate` is the plate lamp (a thin strip above the plate),
not the plate. So the plate is placed on the Forza body:

1. a Forza `numberplate` quad at that end, if there is one (its place and
   size; the Forza quad is dropped);
2. else the plate recess or mounting face: a depth map of the body seen from
   behind / in front (1 cm cells, LOD 1), searched for a flat (plane fit
   under 3 mm RMS), paint/trim-covered window of plate size near the
   centreline, facing out of the end (up by at most 39 degrees, down by at
   most 22: never under a bumper's overhang, #179), preferring windows whose border steps outwards (a recess) and
   heights near the host's plate (the host's plate height mapped by the body
   boxes; a recess up to WIDE_BAND away when there is neither a recess nor a
   flat face nearer); the window is then centred on the recess and the plate
   shrunk to fit it;
3. else near the host's plate height, x = 0, pushed out to the nearest surface
   in front (a plate on a bracket), tilted and bent back at its centre line to
   rest on the face as closely as it can (rest; #213): the height where the
   plate hugs the face, hides no badge, lamp, glass or exhaust and sits on one
   section and material (fallback_height; #159).

An end gets a plate only if the host has one there (vanilla trucks and buggies
have none at the front, a few cars none at the front: the host is the most
similar vanilla car). The plate becomes a synthetic body section
`<surface section>_plate_front|rear` (technique PLATE_TECHNIQUE) on every body
LOD, so the rest of the converter treats it like Forza geometry: paint
material (forza2dsf), damage panels by the surface section's rule
(src/dsfforza/convert/damage.py: bumper, boot lid or nearest body panel), crush targets and
decal UV1 from the host. Per LOD the plate moves out if that LOD's surface
comes forward of it.

    uv run dsff plates plan out/cache/fh1/NIS_370Z_10 --host 227 [--png out/plates/370z.png]
    uv run dsff plates host <archive> --id 227
"""

from __future__ import annotations

import argparse
import copy
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from dsfforza.formats import carbin

PLATE_TECHNIQUE = "dsf_plate"
SECTION_SUFFIX = "_plate_"  # <surface>_plate_front / _plate_rear
# The plate in the shared paint atlas 00122b73d1fa1227: u0, v0, u1, v1 (vanilla 227's quads).
PLATE_UV = (0.4929, 0.8867, 0.5986, 0.9971)
SHARED_ATLAS_MATERIALS = {
    bytes.fromhex(h)
    for h in (
        "001283358afd1027",  # GLOBAL_Chassis (paint)
        "10122b9653b14e2a",  # GLOBAL_Chassis_LOD1
        "0012833b12fe1027",  # GLOBAL_Chassis_Simple (trim)
        "10122b9753b14e2a",  # GLOBAL_Chassis_Simple_LOD1
        "00128338d7fd1027",  # GLOBAL_Chassis_Chrome
        "a012f01306d57a2a",  # GLOBAL_LOD_Chrome
    )
}
DEFAULT_SIZE = (0.26, 0.122)  # vanilla median quad
FORZA_PLATE_TECHNIQUES = ("numberplate",)
PX = 0.01  # depth-map cell (m)
FLAT_RMS = 0.003  # plane-fit RMS of a usable mounting face
COPLANAR = 0.004  # recess region: within this of the plane
PROUD = 0.003  # plate in front of the face
RECESS_STEP = 0.004  # border cells this much nearer than the plane: recess wall
MAX_TILT = 0.8  # dd/dy of a plate facing up (about 39 degrees)
# ... and of one facing the road: vanilla plates face down by at most 0.36 (the '68
# Camaro's rear, the Countach's front) but one (the Stratos's front, 0.70), so a window
# under a bumper's overhang is no mounting face (#179: the Golf GTI 16v Mk2's rear
# plate, -0.73, was invisible in game). A host whose own plate faces down further
# allows its own tilt.
FACE_DOWN = 0.4  # about 22 degrees
BRACKET_TILT = 0.25  # |dz/dy| of a fallback plate on a bracket (about 14 degrees)
# A fallback plate on a bumper that curves back towards the sides bends at its centre
# line, each half turned back by up to this dd/ds (about 11 degrees; #213). Vanilla
# bends plates so: 25 of 120 front plates in two halves turn by over 2 degrees, the
# '68 Camaro's by 24, the Eldorado's by 14 (research/plates.md, "Bent plates").
BEND_MAX = 0.2
OPENING = 0.1  # a cell this far behind the plate is a hole (grille, gap), not its face
PRIOR_BAND = 0.3  # recesses searched within this of the host's plate height (m)
WIDE_BAND = 0.5  # ... and within this when there is none nearer
FLAT_BAND = 0.15  # plain flat faces (no recess walls) only within this
MIN_FIT = 0.7  # smallest plate scale when fitting a small recess
# Fallback plates (fallback_height): cost per share of the plate over a badge, lamp or
# glass, and per share over other sections than its main one, against the gap (m)
# between plate and face (#159; the hide cost doubled with the gap's in #213).
HIDE_COST = 1.0
# The gap counts twice: a plate standing off its face by a few centimetres shows more
# than one across a section edge or a little off the host's height (#213: with the gap
# at 1, shrinking or clearing an emblem's edge moved plates 3 to 7 cm off their faces).
GAP_COST = 2.0
# ... plus this once it hides more of a badge than HIDE_NONE (a cell or two of edge):
# even a sliver of an emblem shows (#241: the Delta S4's front plate took its grille
# band over the Lancia shield's lower edge, 1.7% of it, for a cost of 0.008). Lamp and
# exhaust edges keep the share cost alone: the step put plates under bumpers, standing
# off by 3 to 7 cm, to clear the corner of a tail lamp (the Cougar Eliminator's rear).
HIDE_STEP = 0.02
HIDE_NONE = 0.005
# A fallback plate keeps this clear of lamps and their covers (about a plate frame):
# cells this near a lamp count as hidden. The Viper GTS-R's front plate sat between its
# lamp covers, touching both (#259).
LAMP_MARGIN = 0.02
# A fallback plate whose best height still covers more than this share of a lamp has no
# place at that end: the face there is a lamp panel (#251: the ARX-01b's and Mazda B09's
# rear plates covered their rain lights, between diffusers with no other face near the
# host's plate height). The end gets no plate, as the Forza car has none.
LAMP_DROP = 0.05
# A fallback plate may shrink to fit between a badge and an edge: scales tried, the
# cost per unit of shrink, and the narrowest plate (vanilla's: 0.23 m front, 0.226 rear).
FALLBACK_SCALES = (1.0, 0.9, 0.8)
SHRINK_COST = 0.05
MIN_WIDTH = 0.23
BADGE_BAND = 0.4  # a run of badge-technique cells this wide is grille artwork (m)
MIX_COST = 0.1
LOD_PUSH_LIMIT = 0.08  # coarser LODs move the plate out by at most this
# Baked AO bytes for the plate vertices (forza2dsf.vertex_sky): an open,
# vertical face, sky term ~0.48 against vanilla plates' ~0.5.
PLATE_AO = (95, 96, 97, 96)
SURFACE_CLASSES = (1, 2)  # paint, trim (depth-map classes below)


# --- the host's plates ------------------------------------------------------------------


@dataclass
class HostPlate:
    end: str  # "front" / "rear"
    centre: tuple[float, float, float]
    size: tuple[float, float]  # width (x), height (y)
    pair: tuple[int, int]  # s9 bytes 0, 1 (panel)
    triangles: int
    tilt: float = 0.0  # dd/dy seen from the end (+: faces up; area-weighted normal)


@dataclass
class HostPlates:
    plates: dict  # end -> HostPlate
    body_y: tuple[float, float]  # host part-0 vertex y range (body box)


def host_plates(stream, vehc) -> HostPlates:
    """Plates of a vanilla vehicle's full entry (MODL part 0, mask 4): triangles on
    a shared-atlas material whose UV0 centroid is on the plate."""
    from dsfforza.convert.damage import load_mesh
    from dsfforza.formats import vehicle_mesh as vm

    mesh = load_mesh(stream, vehc)
    u0, v0, u1, v1 = PLATE_UV
    ends: dict[str, list] = {}
    ys = []
    for lod in mesh.parts[0].lods if mesh.parts else []:
        if lod.mask != 4:
            continue
        for n in lod.submodels:
            sub = mesh.submodels[n]
            for vb, tri in vm.triangles(mesh, sub):
                pts = [vm.position(vb, i) for i in tri]
                ys += [p[1] for p in pts]
                if sub.material not in SHARED_ATLAS_MATERIALS:
                    continue
                e8 = vb.element(8)
                uvs = [vb.read(e8, i)[:2] for i in tri]
                cu, cv = sum(u for u, _ in uvs) / 3, sum(v for _, v in uvs) / 3
                if not (u0 - 0.005 < cu < u1 + 0.005 and v0 - 0.005 < cv < v1 + 0.003):
                    continue
                e9 = vb.element(9)
                s9 = tuple(vb.read(e9, tri[0])[:2]) if e9 is not None else (0, 0)
                c = [sum(p[a] for p in pts) / 3 for a in range(3)]
                ends.setdefault("front" if c[2] > 0 else "rear", []).append((pts, s9))
    plates = {}
    for end, tris in ends.items():
        pts = [p for t, _ in tris for p in t]
        lo = [min(p[a] for p in pts) for a in range(3)]
        hi = [max(p[a] for p in pts) for a in range(3)]
        pair = Counter(s9 for _, s9 in tris).most_common(1)[0][0]
        # Normals turned out of the end (either winding), summed by area.
        out = 1.0 if end == "front" else -1.0
        normal = np.zeros(3)
        for t, _ in tris:
            a, b, c = (np.array(p) for p in t)
            n = np.cross(b - a, c - a)
            normal += n if n[2] * out >= 0 else -n
        tilt = float(normal[1] / abs(normal[2])) if abs(normal[2]) > 1e-12 else 0.0
        plates[end] = HostPlate(
            end, tuple((lo[a] + hi[a]) / 2 for a in range(3)), (hi[0] - lo[0], hi[1] - lo[1]), pair, len(tris), tilt
        )
    return HostPlates(plates, (min(ys), max(ys)) if ys else (0.0, 1.0))


# --- depth maps of the Forza body ---------------------------------------------------------

CLASS = {"paint": 1, "trim": 2, "chrome": 2, "glass": 3, "textured": 4, "tyre": 5}
# What a plate must not hide (depth map `feat`): badges and emblems, lamps, exhaust
# sections (the Zonda Cinque's centre tailpipes). Grilles are marked too: a plate may
# cross one (on a bracket, as on the real cars) but a grille is no mounting face.
FEAT_GRILLE, FEAT_BADGE, FEAT_LAMP, FEAT_EXHAUST = 1, 2, 3, 4
FLAT_TRIM_ART = ("dsf_trim_gloss", "dsf_trim_grey")  # trim_art's one-colour trims


def feature(technique: str) -> int:
    """FEAT_* of a Forza technique (trim_art's names by their source technique)."""
    from dsfforza.convert import forza_materials as fm

    n = fm.technique_source(technique).lower()
    if "badge" in n or "emblem" in n:
        return FEAT_BADGE
    if n.startswith("grille"):
        return FEAT_GRILLE
    role, family = fm.classify(technique)
    if family == fm.LIGHTS and role in (fm.TEXTURED, fm.GLASS):
        return FEAT_LAMP
    return 0


def body_triangles(sections, lod: int, place) -> list:
    """(DSF points, class, section name, feature) for every body triangle at `lod`."""
    from dsfforza.convert import forza_materials as fm

    out = []
    for s in sections:
        for sub in s.subsections:
            if sub.lod != lod or sub.material in FORZA_PLATE_TECHNIQUES or sub.material == PLATE_TECHNIQUE:
                continue
            role, _ = fm.classify(sub.material)
            if role == fm.SKIP:
                continue
            # trim_art's flat-colour trims (dsf_trim_gloss~misc, dsf_trim_grey~...) are
            # the Forza trim they came from: mounting faces (#159: the Mazda 2's rear
            # bumper recess, gloss trim since #176, was no face, and its plate fell
            # back onto the boot lid). Detail trims are grille bars and the like (the
            # Challenger SRT8's upper grille): no face, but no badge either.
            c, f = CLASS.get(role, 0), feature(sub.material)
            kind = fm.technique_scale(sub.material)[0]
            if kind in FLAT_TRIM_ART:
                c = CLASS.get(fm.classify(fm.technique_source(sub.material))[0], 0)
            elif kind == "dsf_trim_detail":
                f = FEAT_GRILLE
            if s.name.lower().startswith("exhaust"):
                f = FEAT_EXHAUST
            pool = s.vertices_for(sub)
            for t in sub.triangles():
                out.append(([place.point(pool[i].position) for i in t], c, s.name, f))
    return out


@dataclass
class DepthMap:
    """Nearest body surface seen from one end, on a grid. Column j is the
    viewer's right coordinate s = s0 + (j + 0.5) PX (rear: s = -x, front: s = x);
    row i is y = y1 - (i + 0.5) PX; depth d = z at the rear, -z at the front
    (smaller is nearer the viewer)."""

    end: str
    s0: float
    y1: float
    depth: np.ndarray
    cls: np.ndarray
    sec: np.ndarray
    names: list
    feat: np.ndarray  # FEAT_* of the nearest surface (0: none)

    def col(self, s: float) -> float:
        return (s - self.s0) / PX - 0.5

    def row(self, y: float) -> float:
        return (self.y1 - y) / PX - 0.5

    def s_of(self, j) -> np.ndarray:
        return self.s0 + (np.asarray(j) + 0.5) * PX

    def y_of(self, i) -> np.ndarray:
        return self.y1 - (np.asarray(i) + 0.5) * PX


def view_coords(end: str, p) -> tuple[float, float, float]:
    """(s, y, d) of a DSF point seen from `end`."""
    return (-p[0], p[1], p[2]) if end == "rear" else (p[0], p[1], -p[2])


def depth_map(tris, end: str, s_range, y_range) -> DepthMap:
    s0, s1 = s_range
    y0, y1 = y_range
    width, height = max(1, int(round((s1 - s0) / PX))), max(1, int(round((y1 - y0) / PX)))
    depth = np.full((height, width), np.inf)
    cls = np.zeros((height, width), np.int8)
    sec = np.full((height, width), -1, np.int32)
    feat = np.zeros((height, width), np.int8)
    names: list = []
    index: dict = {}
    for pts, c, name, f in tris:
        v = [view_coords(end, p) for p in pts]
        q = [(a[0] - s0) / PX for a in v]
        r = [(y1 - a[1]) / PX for a in v]
        area = (q[1] - q[0]) * (r[2] - r[0]) - (r[1] - r[0]) * (q[2] - q[0])
        if abs(area) < 1e-9:
            continue
        cx0, cx1 = max(int(min(q)), 0), min(int(max(q)) + 1, width)
        cy0, cy1 = max(int(min(r)), 0), min(int(max(r)) + 1, height)
        if cx0 >= cx1 or cy0 >= cy1:
            continue
        xs, ys = np.meshgrid(np.arange(cx0, cx1) + 0.5, np.arange(cy0, cy1) + 0.5)
        w0 = ((q[1] - xs) * (r[2] - ys) - (r[1] - ys) * (q[2] - xs)) / area
        w1 = ((q[2] - xs) * (r[0] - ys) - (r[2] - ys) * (q[0] - xs)) / area
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not inside.any():
            continue
        d = w0 * v[0][2] + w1 * v[1][2] + w2 * v[2][2]
        win = depth[cy0:cy1, cx0:cx1]
        better = inside & (d < win)
        if not better.any():
            continue
        win[better] = d[better]
        cls[cy0:cy1, cx0:cx1][better] = c
        feat[cy0:cy1, cx0:cx1][better] = f
        if name not in index:
            index[name] = len(names)
            names.append(name)
        sec[cy0:cy1, cx0:cx1][better] = index[name]
    return DepthMap(end, s0, y1, depth, cls, sec, names, feat)


def fit_plane(dm: DepthMap, rows: slice, cols: slice, mask=None):
    """d = b * y + c over the finite cells (no slope across: plates stay square
    to the car). Returns (b, c, rms, residuals) or None."""
    win = dm.depth[rows, cols]
    ok = np.isfinite(win) if mask is None else (np.isfinite(win) & mask)
    if ok.sum() < 6:
        return None
    ii, _ = np.nonzero(ok)
    y = dm.y_of(ii + rows.start)
    a = np.c_[y, np.ones(len(y))]
    (b, c), *_ = np.linalg.lstsq(a, win[ok], rcond=None)
    res = win[ok] - (b * y + c)
    return float(b), float(c), float(math.sqrt(float((res**2).mean()))), res


# --- planning ----------------------------------------------------------------------------


@dataclass
class Plate:
    end: str
    centre: tuple[float, float]  # (s, y): viewer-right coordinate and height
    size: tuple[float, float]
    tilt: float  # dd/dy
    depth: float  # d at the centre (plate surface)
    source: str  # forza-mesh / recess / flat / host
    surface: str  # Forza section under the plate
    lod_depth: dict = field(default_factory=dict)  # lod -> plate d at the centre
    note: str = ""
    bend: float = 0.0  # dd/d|s - centre s|: each half turned back by this (0: flat)

    def point(self, ds: float, dy: float, lod: int | None = None):
        """DSF point of the plate at (ds, dy) from its centre (viewer's right, up)."""
        d0 = self.lod_depth.get(lod, self.depth) if lod is not None else self.depth
        s, y = self.centre
        d = d0 + self.tilt * dy + self.bend * abs(ds)
        return (-(s + ds), y + dy, d) if self.end == "rear" else (s + ds, y + dy, -d)

    def corners(self, lod: int | None = None):
        """DSF corners (viewer's top-left, top-right, bottom-right, bottom-left)."""
        w, h = self.size
        return [
            self.point(ds, dy, lod) for ds, dy in ((-w / 2, h / 2), (w / 2, h / 2), (w / 2, -h / 2), (-w / 2, -h / 2))
        ]

    def normal(self, side: int = 0) -> tuple[float, float, float]:
        """Outward DSF normal of the viewer's left (side -1) or right (+1) half, or
        along the centre line (0)."""
        a = self.bend * side
        n = math.sqrt(1 + self.tilt * self.tilt + a * a)
        if self.end == "rear":
            return (-a / n, self.tilt / n, -1.0 / n)
        return (a / n, self.tilt / n, 1.0 / n)

    def report(self) -> dict:
        return {
            "source": self.source,
            "surface": self.surface,
            "centre_s_y": [round(c, 4) for c in self.centre],
            "size": [round(c, 4) for c in self.size],
            "tilt": round(self.tilt, 3),
            **({"bend": round(self.bend, 3)} if self.bend else {}),
            "depth": round(self.depth, 4),
            "lod_depth": {str(k): round(v, 4) for k, v in sorted(self.lod_depth.items())},
            **({"note": self.note} if self.note else {}),
        }


def _fit_size(base: tuple[float, float], room: tuple[float | None, float | None], allow_up: bool = False):
    """Host plate size scaled (aspect kept) to fit `room` (None = unbounded)."""
    w, h = base
    k = 1.0 if not allow_up else 1.3
    if room[0]:
        k = min(k, room[0] / w)
    if room[1]:
        k = min(k, room[1] / h)
    return (w * max(MIN_FIT, k), h * max(MIN_FIT, k))


def _forza_mesh(sections, end: str, place, z_mid: float):
    """Triangles of Forza plate quads (technique numberplate) at one end, finest LOD."""
    best = None
    for s in sections:
        for sub in s.subsections:
            if sub.material not in FORZA_PLATE_TECHNIQUES or sub.lod <= 0:
                continue
            pool = s.vertices_for(sub)
            for t in sub.triangles():
                pts = [place.point(pool[i].position) for i in t]
                cz = sum(p[2] for p in pts) / 3
                if (cz > z_mid) != (end == "front"):
                    continue
                if best is None or sub.lod < best[0]:
                    best = (sub.lod, [], s.name)
                if sub.lod == best[0]:
                    best[1].append(pts)
    return best


def run_widths(mask: np.ndarray) -> np.ndarray:
    """Per cell: the length (cells) of the horizontal run of True cells holding it (0 off the mask)."""
    out = np.zeros(mask.shape, np.int32)
    for i, row in enumerate(mask):
        edges = np.flatnonzero(np.diff(np.r_[0, row.astype(np.int8), 0]))
        for a, b in zip(edges[::2], edges[1::2]):
            out[i, a:b] = b - a
    return out


def dilate(mask: np.ndarray, cells: int) -> np.ndarray:
    """`mask` grown by `cells` in every direction (a square)."""
    out = mask.copy()
    height, width = mask.shape
    for di in range(-cells, cells + 1):
        for dj in range(-cells, cells + 1):
            src = mask[max(0, -di) : height - max(0, di), max(0, -dj) : width - max(0, dj)]
            out[max(0, di) : height - max(0, -di), max(0, dj) : width - max(0, -dj)] |= src
    return out


def face_cells(dm: DepthMap) -> np.ndarray:
    """Cells of the car's face, not the underbody or cabin seen through a gap: within
    0.3 m of the nearest surface near the centre line."""
    width = dm.depth.shape[1]
    centre_cols = slice(max(0, int(dm.col(-0.3))), min(width, int(dm.col(0.3)) + 1))
    finite = dm.depth[:, centre_cols][np.isfinite(dm.depth[:, centre_cols])]
    return (dm.depth < float(finite.min()) + 0.3) if finite.size else np.isfinite(dm.depth)


def rest(dm: DepthMap, rows: slice, cols: slice, centre: tuple[float, float], ok: np.ndarray):
    """(tilt, bend, d, gaps) of a plate with no face of its own resting on the cells
    `ok` of a window: the surface d + tilt (y - cy) + bend |s - cs| (flat, or bent
    back at its centre line) that touches the nearest cell and stands off the others
    least on average, each gap counted up to OPENING (#213: a flat plate stood off
    a bumper curving back towards its sides by several centimetres at its corners).
    Tilt within BRACKET_TILT, bend within BEND_MAX."""
    cs, cy = centre
    ii, jj = np.nonzero(ok)
    d = dm.depth[rows, cols][ok]
    y = dm.y_of(ii + rows.start) - cy
    s = np.abs(dm.s_of(jj + cols.start) - cs)
    tt, bb = np.meshgrid(
        np.linspace(-BRACKET_TILT, BRACKET_TILT, int(round(2 * BRACKET_TILT / 0.01)) + 1),
        np.linspace(0.0, BEND_MAX, int(round(BEND_MAX / 0.01)) + 1),
        indexing="ij",
    )
    tt, bb = tt.ravel(), bb.ravel()
    rel = d[None, :] - tt[:, None] * y[None, :] - bb[:, None] * s[None, :]
    lo = rel.min(axis=1)
    gaps = np.minimum(rel - lo[:, None], OPENING)
    # Ties (a flat face) go to the plainest plate: upright and flat.
    k = int(np.argmin(gaps.mean(axis=1) + 1e-4 * (np.abs(tt) + bb)))
    return float(tt[k]), float(bb[k]), float(lo[k]), gaps[k]


def fallback_height(dm: DepthMap, prior_y: float, base: tuple[float, float]) -> tuple[float, tuple, str]:
    """(height, size, note) of a plate with no face to sit on, at s = 0 (step 3).

    Within PRIOR_BAND of the host's height: where the plate covers the car's
    face and hugs it best once rested on it (rest: a plate on a mounting pad or
    bracket, near-vertical, bent round a curved bumper), on one panel, and hiding
    no badge, lamp, glass or exhaust; shrunk (FALLBACK_SCALES, at least MIN_WIDTH
    wide) where that fits it between them (#241). A grille may sit behind it, as
    on the real cars (the Mazda 2's lower bumper grille); the emblem slot above it
    may not (#159: the host's height alone put the plate over the Mazda's emblem
    and bonnet edge)."""
    height, width = dm.depth.shape
    face = face_cells(dm)
    # Badge cells in a run wider than BADGE_BAND are grille artwork drawn with a badge
    # technique (the BMW M1's nose band), not an emblem.
    badge = dm.feat == FEAT_BADGE
    emblem = badge & (run_widths(badge) * PX < BADGE_BAND)
    lamp = (dm.feat == FEAT_LAMP) | (dm.cls == CLASS["glass"])
    near_lamp = dilate(lamp, int(round(LAMP_MARGIN / PX)))
    hidden = emblem | near_lamp | (dm.feat == FEAT_EXHAUST)
    best = None
    for k in FALLBACK_SCALES:
        if k < 1.0 and base[0] * k < MIN_WIDTH:
            continue
        size = (base[0] * k, base[1] * k)
        c_ = slice(max(0, int(round(dm.col(-size[0] / 2)))), min(width, int(round(dm.col(size[0] / 2))) + 1))
        for dy in np.arange(-PRIOR_BAND, PRIOR_BAND + 0.001, PX):
            y = prior_y + float(dy)
            r = slice(max(0, int(round(dm.row(y + size[1] / 2)))), min(height, int(round(dm.row(y - size[1] / 2))) + 1))
            if r.stop <= r.start:
                continue
            ok = face[r, c_]
            cover = float(ok.mean())
            gap, mixed = 1.0, 1.0
            if ok.sum() > 6:
                # The mean gap under the plate once tilted and bent onto the face (rest).
                gap = float(rest(dm, r, c_, (0.0, y), ok)[3].mean())
                # Section and material boundaries (bonnet shut line, bumper to body, the
                # edge of a grille): the plate on one surface.
                cells = zip(dm.sec[r, c_][ok].tolist(), dm.cls[r, c_][ok].tolist())
                mixed = 1.0 - Counter(cells).most_common(1)[0][1] / float(ok.sum())
            hide = float(hidden[r, c_].mean())
            cost = GAP_COST * gap + 0.1 * abs(float(dy)) + HIDE_COST * hide + MIX_COST * mixed + SHRINK_COST * (1.0 - k)
            if float(emblem[r, c_].mean()) > HIDE_NONE:
                cost += HIDE_STEP
            key = (round(cover, 1), -cost)
            if best is None or key > best[0]:
                best = (key, y, size, hide, mixed, float(lamp[r, c_].mean()))
    if best is None:  # the host's height maps outside the depth map (plan() skips the end)
        raise ValueError(f"{dm.end} plate: no body surface near y {prior_y:.2f}")
    _, y, size, hide, mixed, on_lamp = best
    if on_lamp > LAMP_DROP:
        raise ValueError(f"{dm.end} plate: no face clear of the lamps near y {prior_y:.2f} ({on_lamp:.0%} on a lamp)")
    note = "no flat face of plate size"
    if size != base:
        note += f", shrunk to {size[0] / base[0]:.1f}"
    if hide > 0:
        note += f", hides {hide:.0%} badge/lamp/glass/exhaust"
    if mixed > 0:
        note += f", {mixed:.0%} on other sections"
    return y, size, note


def plan_end(
    tris, end: str, host: HostPlate, prior_y: float, ground: float, half_width: float, forza_mesh=None
) -> Plate:
    base = host.size if host.size[0] > 0.15 else DEFAULT_SIZE
    if base[0] > 0.45:  # a vanilla oddity (Super Car 01's front: 0.99 m, several pieces)
        base = DEFAULT_SIZE
    if forza_mesh is not None:
        _, mesh, name = forza_mesh
        v = np.array([view_coords(end, p) for t in mesh for p in t])
        lo, hi = v.min(axis=0), v.max(axis=0)
        a = np.c_[v[:, 1], np.ones(len(v))]
        (b, c), *_ = np.linalg.lstsq(a, v[:, 2], rcond=None)
        b = float(np.clip(b, -MAX_TILT, MAX_TILT))
        cs, cy = float(lo[0] + hi[0]) / 2, float(lo[1] + hi[1]) / 2
        size = _fit_size(base, (float(hi[0] - lo[0]), float(hi[1] - lo[1])), allow_up=True)
        depth = float(min(v[:, 2] - b * (v[:, 1] - cy))) - 0.002
        return Plate(end, (cs, cy), size, b, depth, "forza-mesh", name)

    dm = depth_map(tris, end, (-half_width, half_width), (ground + 0.05, ground + 1.6))
    wcols, wrows = int(round(base[0] / PX)), int(round(base[1] / PX))
    centre_col = dm.col(0.0)
    height, width = dm.depth.shape
    face_down = max(FACE_DOWN, -host.tilt)
    cands = []
    for top in range(height - wrows):
        cy = float(dm.y_of(top + (wrows - 1) / 2))
        if cy - ground < 0.15 or cy - ground > 1.45 or abs(cy - prior_y) > WIDE_BAND:
            continue
        for off in (-4, -2, 0, 2, 4):
            c0 = int(round(centre_col - (wcols - 1) / 2)) + off
            if c0 < 3 or c0 + wcols + 3 > width or top < 3 or top + wrows + 3 > height:
                continue
            rows, cols = slice(top, top + wrows), slice(c0, c0 + wcols)
            surf = np.isin(dm.cls[rows, cols], SURFACE_CLASSES) & np.isfinite(dm.depth[rows, cols])
            if surf.mean() < 0.97:
                continue
            fit = fit_plane(dm, rows, cols, surf)
            if fit is None or fit[2] > FLAT_RMS or not -face_down <= fit[0] <= MAX_TILT:
                continue
            b, c, rms, _ = fit
            # Border ring 3 cells wide: a recess's walls are nearer the viewer.
            ring_rows, ring_cols = slice(top - 3, top + wrows + 3), slice(c0 - 3, c0 + wcols + 3)
            ring = dm.depth[ring_rows, ring_cols]
            ii, jj = np.mgrid[ring_rows, ring_cols]
            inner = (ii >= top) & (ii < top + wrows) & (jj >= c0) & (jj < c0 + wcols)
            pred = b * dm.y_of(ii) + c
            sel = ~inner & np.isfinite(ring)
            proud = float(np.mean((ring - pred)[sel] < -RECESS_STEP)) if sel.any() else 0.0
            s = float(dm.s_of(c0 + (wcols - 1) / 2))
            cands.append((proud, rms, cy, s, b, c, top, c0))
    pick = None
    if cands:
        recess = [k for k in cands if k[0] >= 0.3 and abs(k[2] - prior_y) <= PRIOR_BAND]
        # A flat face without walls is weak evidence (the Skyline R32's boot face won
        # over its bumper's mounting pad): only near the host's height.
        near = [k for k in cands if abs(k[2] - prior_y) <= FLAT_BAND]
        # Then the car's own recess further from the host's height, before a plate on
        # no face (#159: the Audi RS2's rear bumper recess under its tail lamp band,
        # the host's plate on its boot lid).
        far = [k for k in cands if k[0] >= 0.3]
        if recess or (far and not near):
            source = "recess"
            pick = max(recess or far, key=lambda k: k[0] - 0.5 * abs(k[2] - prior_y) - 2.0 * abs(k[3]))
        elif near:
            source = "flat"
            pick = min(near, key=lambda k: abs(k[2] - prior_y) + 2.0 * abs(k[3]))
    if cands and pick is not None:
        proud, rms, cy, cs, b, c, top, c0 = pick
        # Centre on the face: walk out from the window centre along its row and
        # column while the cells stay on the plane.
        ci, cj = int(round(dm.row(cy))), int(round(dm.col(cs)))

        def on(i, j):
            if not (0 <= i < height and 0 <= j < width):
                return False
            d = dm.depth[i, j]
            return (
                np.isfinite(d) and dm.cls[i, j] in SURFACE_CLASSES and abs(d - (b * float(dm.y_of(i)) + c)) < COPLANAR
            )

        def walk(di, dj, limit):
            n = 0
            while n < limit and on(ci + di * (n + 1), cj + dj * (n + 1)):
                n += 1
            return n, n < limit

        left, lb = walk(0, -1, 45)
        right, rb = walk(0, 1, 45)
        up, ub = walk(-1, 0, 25)
        down, db = walk(1, 0, 25)
        room_w = (left + right + 1) * PX - 0.02 if lb and rb else None
        room_h = (up + down + 1) * PX - 0.015 if ub and db else None
        # A face much smaller than the window that found it is a walk stopped
        # by a seam or a hole, not the recess's walls.
        if room_w is not None and room_w < 0.75 * base[0]:
            room_w = None
        if room_h is not None and room_h < 0.6 * base[1]:
            room_h = None
        if room_w is not None:
            cs = float(dm.s_of(cj + (right - left) / 2))
        if room_h is not None:
            cy = float(dm.y_of(ci + (down - up) / 2))
        size = _fit_size(base, (room_w, room_h))
        note = (
            f"window rms {rms * 1000:.1f} mm, recess border {proud:.2f}, room "
            f"{'-' if room_w is None else f'{room_w:.2f}'} x {'-' if room_h is None else f'{room_h:.2f}'}"
        )
    else:
        source, cs = "host", 0.0
        b = 0.0
        cy, size, note = fallback_height(dm, prior_y, base)
    # Final plane over the plate's own cells: tilt from the fit, then out to the
    # nearest surface so no cell pokes through.
    rows = slice(max(0, int(round(dm.row(cy + size[1] / 2)))), min(height, int(round(dm.row(cy - size[1] / 2))) + 1))
    cols = slice(max(0, int(round(dm.col(cs - size[0] / 2)))), min(width, int(round(dm.col(cs + size[0] / 2))) + 1))
    if fit_plane(dm, rows, cols) is None:
        raise ValueError(f"{end} plate: no body surface at y {cy:.2f}")
    win = dm.depth[rows, cols]
    ok = np.isfinite(win)
    bend = 0.0
    if source == "host":
        on_face = ok & face_cells(dm)[rows, cols]
        b, bend, nearest, _ = rest(dm, rows, cols, (cs, cy), on_face if on_face.sum() >= 6 else ok)
        depth = nearest - PROUD
    else:
        ii, _ = np.mgrid[rows, cols]
        depth = float(np.min(win[ok] - b * (dm.y_of(ii[ok]) - cy))) - PROUD
    surface_idx = Counter(dm.sec[rows, cols][ok].ravel().tolist()).most_common(1)[0][0]
    return Plate(
        end,
        (cs, cy),
        size,
        b,
        depth,
        source,
        dm.names[surface_idx] if surface_idx >= 0 else "body",
        note=note,
        bend=bend,
    )


def lod_depths(plate: Plate, sections, lods, place) -> None:
    """Per LOD: the plate's d, moved out if that LOD's surface is nearer than
    the plate anywhere under it."""
    s, y = plate.centre
    w, h = plate.size
    for lod in lods:
        tris = [
            t
            for t in body_triangles(sections, lod, place)
            if any(abs(view_coords(plate.end, p)[0] - s) < w and abs(p[1] - y) < h for p in t[0])
        ]
        dm = depth_map(tris, plate.end, (s - w / 2, s + w / 2), (y - h / 2, y + h / 2))
        ok = np.isfinite(dm.depth)
        d = plate.depth
        if ok.any():
            ii, jj = np.nonzero(ok)
            plane = plate.tilt * (dm.y_of(ii) - y) + plate.bend * np.abs(dm.s_of(jj) - s)
            nearest = float(np.min(dm.depth[ok] - plane)) - PROUD
            if nearest < d:
                d = max(nearest, plate.depth - LOD_PUSH_LIMIT)
        plate.lod_depth[lod] = d


@dataclass
class PlatePlan:
    plates: list
    dropped: list  # Forza numberplate sections/techniques replaced
    report: dict

    def sections(self, template_version: int, lods) -> list:
        return [plate_section(p, template_version, lods) for p in self.plates]


def plan(car_body, place, ground: float, host: HostPlates, lods, log=print) -> PlatePlan:
    """Plates for the converted car: one per end the host has a plate at."""
    tris = body_triangles(car_body, 1 if 1 in lods else min(lods), place)
    if not tris:
        return PlatePlan([], [], {"plates": {}})
    pts = [p for t, *_ in tris for p in t]
    ys = [p[1] for p in pts]
    zs = [p[2] for p in pts]
    xs = [abs(p[0]) for p in pts]
    z_mid = (max(zs) + min(zs)) / 2
    half_width = min(0.9, max(xs) + 0.02)
    by_end = {
        "front": [t for t in tris if sum(p[2] for p in t[0]) / 3 > z_mid],
        "rear": [t for t in tris if sum(p[2] for p in t[0]) / 3 <= z_mid],
    }
    plates, report = [], {}
    hy0, hy1 = host.body_y
    for end in ("rear", "front"):
        hp = host.plates.get(end)
        if hp is None:
            report[end] = {"source": "none", "note": "the host has no plate here"}
            continue
        t = (hp.centre[1] - hy0) / max(1e-6, hy1 - hy0)
        prior_y = min(ys) + t * (max(ys) - min(ys))
        mesh = _forza_mesh(car_body, end, place, z_mid)
        try:
            p = plan_end(by_end[end], end, hp, prior_y, ground, half_width, mesh)
        except ValueError as error:
            report[end] = {"source": "none", "note": str(error)}
            log(f"plates: {error}")
            continue
        lod_depths(p, car_body, sorted(lods), place)
        plates.append(p)
        report[end] = {
            **p.report(),
            "host": {
                "centre": [round(c, 3) for c in hp.centre],
                "size": [round(c, 3) for c in hp.size],
                "pair": list(hp.pair),
                "tilt": round(hp.tilt, 3),
            },
            "prior_y": round(prior_y, 3),
        }
    dropped = sorted({s.name for s in car_body for sub in s.subsections if sub.material in FORZA_PLATE_TECHNIQUES})
    return PlatePlan(plates, dropped, {"plates": report, "dropped_forza_plates": dropped})


# --- the synthetic section ------------------------------------------------------------------


def plate_section(p: Plate, version: int, lods) -> carbin.Section:
    """A Forza-space section holding the plate quad on every LOD. Placement is
    undone here (forza2dsf.add_geometry places it again)."""
    u0, v0, u1, v1 = PLATE_UV
    w, h = p.size
    # Columns across the plate seen from the viewer: (ds, u, side); a bent plate
    # has a third along its centre line.
    columns = [(-w / 2, u0, -1), (w / 2, u1, 1)]
    if p.bend:
        columns.insert(1, (0.0, (u0 + u1) / 2, 0))
    pool, subs = [], []
    extra_len = 4 if version == 3 else 8
    for lod in sorted(lods):
        base = len(pool)
        # Top row left to right, then the bottom row (AO bytes: tl, tr, br, bl).
        for row, (dy, v) in enumerate(((h / 2, v0), (-h / 2, v1))):
            for ds, u, side in columns:
                k = (0, 1)[side > 0] if row == 0 else (3, 2)[side > 0]
                pool.append(
                    carbin.Vertex(
                        p.point(ds, dy, lod),
                        (u, v),
                        (0.0, 0.0),
                        p.normal(side),
                        bytes([PLATE_AO[k]]) + bytes(extra_len - 1),
                    )
                )
        # Outward winding per panel: (tl, bl, br), (tl, br, tr) seen from the viewer.
        idx = []
        n = len(columns)
        for c in range(n - 1):
            tl, tr, bl, br = base + c, base + c + 1, base + n + c, base + n + c + 1
            idx += [tl, bl, br, tl, br, tr]
        subs.append(carbin.SubSection(PLATE_TECHNIQUE, lod, 4, (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0), idx))
    section = carbin.Section(
        f"{p.surface}{SECTION_SUFFIX}{p.end}",
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        pool,
        pool,
        subs,
        version,
    )
    return section


def to_forza(section: carbin.Section, place) -> carbin.Section:
    """Undo `place` on a section built in DSF coordinates (Placement is a
    180-degree turn about y plus a translation)."""
    out = copy.copy(section)
    verts = []
    for v in section.lod_vertices:
        x, y, z = v.position
        nx, ny, nz = v.normal
        verts.append(carbin.Vertex((-x, y - place.dy, -(z - place.dz)), v.uv0, v.uv1, (-nx, ny, -nz), v.extra))
    out.lod_vertices = out.lod0_vertices = verts
    return out


def without_forza_plates(sections) -> list:
    """Sections with their numberplate subsections removed (copies where needed)."""
    out = []
    for s in sections:
        if any(sub.material in FORZA_PLATE_TECHNIQUES for sub in s.subsections):
            s = copy.copy(s)
            s.subsections = [sub for sub in s.subsections if sub.material not in FORZA_PLATE_TECHNIQUES]
        out.append(s)
    return out


def apply(car, place, ground: float, host: HostPlates, log=print):
    """(new body section list, PlatePlan): Forza plate quads replaced by DSF plates."""
    lods = sorted({sub.lod for s in car.body for sub in s.subsections if sub.lod > 0})
    pp = plan(car.body, place, ground, host, lods, log=log)
    version = car.body[0].version if car.body else 3
    new = [to_forza(s, place) for s in pp.sections(version, lods)]
    body = (without_forza_plates(car.body) if pp.plates else list(car.body)) + new
    for p in pp.plates:
        log(
            f"plates: {p.end} {p.source} on {p.surface}, {p.size[0]:.3f} x {p.size[1]:.3f} m at "
            f"s {p.centre[0]:+.3f} y {p.centre[1]:+.3f}, tilt {p.tilt:+.2f}"
            + (f", bend {p.bend:.2f}" if p.bend else "")
            + (f" ({p.note})" if p.note else "")
        )
    return body, pp


# --- CLI ------------------------------------------------------------------------------------


def _debug_png(car_body, place, ground, pp: PlatePlan, path: Path) -> None:
    from dsfforza.formats import vehicle_mesh as vm

    tris = body_triangles(car_body, 1, place)
    xs = [abs(p[0]) for t, *_ in tris for p in t]
    hw = min(0.9, max(xs) + 0.02)
    zs = [p[2] for t, *_ in tris for p in t]
    z_mid = (max(zs) + min(zs)) / 2
    tiles = []
    colours = {1: (0.85, 0.3, 0.3), 2: (0.35, 0.35, 0.4), 3: (0.4, 0.6, 0.85), 4: (0.95, 0.8, 0.2), 5: (0.2, 0.2, 0.2)}
    for end in ("rear", "front"):
        part = [t for t in tris if (sum(p[2] for p in t[0]) / 3 > z_mid) == (end == "front")]
        dm = depth_map(part, end, (-hw, hw), (ground + 0.05, ground + 1.6))
        d = dm.depth.copy()
        fin = np.isfinite(d)
        lo, hi = (np.percentile(d[fin], 2), np.percentile(d[fin], 98)) if fin.any() else (0, 1)
        g = np.where(fin, 1 - (np.where(fin, d, hi) - lo) / (hi - lo + 1e-9), 0)
        gy, gx = np.gradient(np.where(fin, d, hi))
        shade = np.clip(0.6 + 30 * (gx - gy), 0, 1)
        rgb = np.zeros(d.shape + (3,))
        for k, c in colours.items():
            rgb[dm.cls == k] = c
        rgb *= (shade * (0.5 + 0.5 * np.clip(g, 0, 1)))[..., None]
        for p in pp.plates:
            if p.end != end:
                continue
            r0, r1 = int(dm.row(p.centre[1] + p.size[1] / 2)), int(dm.row(p.centre[1] - p.size[1] / 2))
            c0, c1 = int(dm.col(p.centre[0] - p.size[0] / 2)), int(dm.col(p.centre[0] + p.size[0] / 2))
            r0, r1 = max(r0, 0), min(r1, d.shape[0] - 1)
            c0, c1 = max(c0, 0), min(c1, d.shape[1] - 1)
            if r0 > r1 or c0 > c1:
                continue
            rgb[r0 : r1 + 1, c0 : c1 + 1] = (0.95, 0.6, 0.15)
            rgb[r0, c0 : c1 + 1] = rgb[r1, c0 : c1 + 1] = (0, 1, 0)
        tiles.append((np.clip(rgb, 0, 1) * 255).astype(np.uint8))
    img = np.kron(np.concatenate(tiles, axis=1), np.ones((2, 2, 1), np.uint8))
    vm.write_png(path, img.shape[1], img.shape[0], bytearray(img.tobytes()))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("host", help="a vanilla vehicle's plates")
    h.add_argument("archive", type=Path)
    h.add_argument("--id", type=int, action="append", required=True)
    p = sub.add_parser("plan", help="where a Forza car's plates go on a host")
    p.add_argument("car", type=Path, help="extracted car folder (out/cache/<game>/<media>)")
    p.add_argument("--host", type=int, required=True)
    p.add_argument("--dsf", type=Path, default=None, help="vanilla dngvehicles.sp (default: from $DSF_GAME_DIR)")
    p.add_argument("--png", type=Path, help="depth maps of both ends with the plates")
    p.add_argument("--raw", action="store_true", help="the Forza sections as loaded (no cut-outs, no trim artwork)")
    args = parser.parse_args(argv)
    from dsfforza.formats import upvh as upvh_mod
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.game import game_paths

    if args.cmd == "host":
        path = game_paths.vanilla(args.archive, quiet=True)
        with path.open("rb") as stream:
            root = read_chunk(stream, 0)
            for spool_id in args.id:
                hp = host_plates(stream, upvh_mod.entries_for(stream, root.entries, spool_id, None)[0])
                print(
                    spool_id,
                    {
                        e: (
                            tuple(round(c, 3) for c in q.centre),
                            tuple(round(c, 3) for c in q.size),
                            q.pair,
                            q.triangles,
                            round(q.tilt, 2),
                        )
                        for e, q in hp.plates.items()
                    },
                )
        return
    from dsfforza.convert import forza2dsf
    from dsfforza.formats.vehicle_mesh import child, payload

    car = forza2dsf.load_car(args.car, args.car.name)
    dims = forza2dsf.car_dims(car)
    if not args.raw:
        # As forza2dsf.convert: alpha cut-outs and trim artwork are split off before
        # the plates are placed, and the plates see their subsections.
        import dataclasses

        from dsfforza.convert import cutout, trim_art
        from dsfforza.convert import forza_materials as fm

        opts = forza2dsf.ConvertOptions()
        lods = set(opts.lods.values()) | {opts.shadow_lod, opts.exterior_lod}
        body, _ = cutout.cut_alpha_tested(car.body, fm.AtlasCache(car.folder), lods=lods)
        body, _ = trim_art.split_trim_art(body, fm.AtlasCache(car.folder), lods=lods)
        car = dataclasses.replace(car, body=body)
    path = game_paths.vanilla(args.dsf, quiet=True)
    with path.open("rb") as stream:
        root = read_chunk(stream, 0)
        kinds = upvh_mod.entries_for(stream, root.entries, args.host, None)
        full, exterior = kinds[0], kinds[2]
        hw = forza2dsf.host_wheels(
            payload(stream, child(child(full, "NONR"), "UPVH")),
            payload(stream, child(child(full, "NONR"), "MODL")),
            payload(stream, child(child(exterior, "NONR"), "MODL")),
        )
        hp = host_plates(stream, full)
    place = forza2dsf.Placement(dy=hw.hub_y - dims.radius + dims.ride_height, dz=hw.axle_mid + dims.mid_z)
    ground = hw.hub_y - dims.radius
    _body, pp = apply(car, place, ground, hp)
    import json

    print(json.dumps(pp.report, indent=1))
    if args.png:
        _debug_png(car.body, place, ground, pp, args.png)
        print(f"wrote {args.png}")


if __name__ == "__main__":
    sys.exit(main())
