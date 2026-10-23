"""Stock-like damage for converted Forza cars: panels, crush morph, hinges.

How a stock DSF car damages (research/damage.md, confirmed-static from the
shared vehicle shaders in SSIC and the stock geometry):

- Vertex semantic 9 bytes 0 and 1 pick the panel. Byte 0 (UPVH record +0x34)
  indexes g_PanelTransforms (a 3x4 matrix per panel: hinge swing, detached
  debris); byte 1 (record +0x2A) indexes g_v4PerPanelData, the panel's damage
  state. Byte 2 is the lamp, byte 3 is 1.
- Semantic 12 ("vec3DamagePos", half4) is the panel's fully crushed position.
  The vertex shader draws lerp(position, s12, fDamage), with
  fDamage = PerPanelData[byte 1].x - offset * x (a per-panel amount with a
  left-right gradient). Stock cars push the nose back by about 6% of the car
  length and the tail forward by about 2%, buckle the bonnet up and dent the
  doors by a few centimetres.
- Semantic 5 bytes 2 and 3 are the normal's polar/azimuth change at full
  damage (angle = byte / 255 * 2pi - pi), so dents shade.
- MODL part 0 of the full entry holds every panel's triangles in one
  submodel per material (the intact car, skinned by panel); each panel's own
  MODL part holds the same vertices again with its own index lists. The
  exterior entry holds the merged part 0 only.

Converted cars had every vertex on the chassis panel with s12 = position: no
crush, no swinging bonnet/boot/bumpers, nothing to fall off. This module
assigns each Forza triangle to one of the host's panels by role (Forza
section name first, then the nearest host panel), copies the host's crush
field onto the Forza vertices (inverse-distance weighted, in car-normalised
coordinates, scaled to the Forza car), writes the normal deltas, builds the
per-panel MODL parts, and moves the host's panel hinges, rigid bodies and
collision boxes onto the Forza panels.

Use: forza2dsf.ConvertOptions(damage=True) (port_car.py --damage). CLI:

    python tools/damage.py panels <archive> --id 266          # the host's panel records
    python tools/damage.py render <archive> --id 304 --png out/x.png [--kind 0|2] [--by part|panel]
                                  [--damage 1] [--parts 2,4,11,12]
    python tools/damage.py check <archive> --id 304           # structural checks of a converted car
"""

from __future__ import annotations

import argparse
import colorsys
import dataclasses
import math
import re
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import upvh as upvh_mod
import vehicle_mesh as vm
from chnk import read_chunk
from vehicle_mesh import child, payload

# --- UPVH roles (record +0x0A) of part records, from a fleet survey of the 150
# stock vehicles (MODL part box centres; research/damage.md). "+x" is the car's
# left (DSF: +z forward, +y up, right-handed); stock names don't say which.
ROLES = {
    38: "chassis", 37: "front structure", 8: "glass", 1: "steering wheel", 185: "gauges",
    32: "door front -x", 31: "door front +x", 55: "door rear -x", 54: "door rear +x",
    4: "bonnet", 5: "boot", 124: "hatch/spoiler", 35: "front bumper", 36: "rear bumper",
    34: "wing front -x", 33: "wing front +x", 51: "headlight -x", 50: "headlight +x",
    41: "mirror -x", 40: "mirror +x", 188: "front suspension", 190: "rear suspension",
    156: "front hub -x", 155: "front hub +x", 136: "exhaust -x", 15: "exhaust +x",
    53: "sill -x", 52: "sill +x", 0: "misc",
}
DOORS = (31, 32, 54, 55)
BODY = (38, 37, 33, 34, 52, 53) + DOORS
# Records whose hinges/bodies other code owns or that have no Forza geometry.
NOT_MOVED = {1, 185, 188, 190, 155, 156}

# Vertex layout of the converter's damage-capable strides: offsets of s9, s5, s12.
LAYOUT = {32: (16, 20, 24), 28: (12, 16, 20)}
K_NEAREST = 6
VOTES = 7          # host vertices voting on a body triangle's panel
COLLISION_SCALE = (0.67, 1.5)  # clamp for a panel's collision box scale
DEGENERATE_UV = 0.004  # host UV1 triangles smaller than this are hidden faces
FACING_WEIGHT = 0.15   # normalised-distance penalty per unit of normal mismatch (UV1 lookup)
MAX_HINGE_MOVE = 0.35  # m; a panel hinge mapped further than this keeps the host's
PANEL_FACING = 0.15    # normalised-distance penalty per unit of normal mismatch (panel vote)
PAINT_REACH = 0.12     # normalised score within which a host paint triangle decides a paint triangle's panel
PAINT_MATERIALS = {bytes.fromhex("001283358afd1027"), bytes.fromhex("10122b9653b14e2a")}  # GLOBAL_Chassis(_LOD1)
ISLAND_AREA = 0.08     # m2; smaller connected patches of one panel inside another are merged into it
ISLAND_TRIANGLES = 3   # patches of this many triangles or fewer too (below ISLAND_AREA_MAX)
ISLAND_AREA_MAX = 0.3  # m2
SEAM_NEAR = 0.003      # m; positions of two rigid panels this close form a seam too (not only identical ones)
SEAM_BORDER = 0.015    # m; host vertices of two rigid panels this close are a host panel border
SEAM_STILL = 0.03      # m; host borders of a role pair whose median crush is below this are pinned
SEAM_REACH = 0.2       # m; crush fades in over this distance from a pinned seam
DASHBOARD_INSTRUMENT = 0x75FAD367  # VDCL name hash of the needle buffers' semantic 8 (u, v, gauge id, 0)


def role_name(role: int) -> str:
    return ROLES.get(role, f"role {role}")


# --- small vector helpers ---------------------------------------------------------

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    length = math.sqrt(_dot(a, a))
    return (a[0] / length, a[1] / length, a[2] / length) if length > 1e-12 else None


@dataclass
class Box:
    lo: tuple
    hi: tuple

    @staticmethod
    def of(points) -> "Box | None":
        points = list(points)
        if not points:
            return None
        return Box(tuple(min(p[a] for p in points) for a in range(3)),
                   tuple(max(p[a] for p in points) for a in range(3)))

    @property
    def centre(self):
        return tuple((self.lo[a] + self.hi[a]) / 2 for a in range(3))

    @property
    def size(self):
        return tuple(self.hi[a] - self.lo[a] for a in range(3))

    def to(self, other: "Box", p):
        """Map a point by its relative position in this box to `other`; a flat
        axis maps by the centre offset."""
        out = []
        for a in range(3):
            s = self.hi[a] - self.lo[a]
            if s < 1e-3:
                out.append(p[a] - self.centre[a] + other.centre[a])
            else:
                out.append(other.lo[a] + (p[a] - self.lo[a]) / s * (other.hi[a] - other.lo[a]))
        return tuple(out)

    def scale_to(self, other: "Box"):
        return tuple((other.size[a] / self.size[a]) if self.size[a] > 1e-3 else 1.0 for a in range(3))


class Grid:
    """k-nearest neighbours over 3D points (a uniform hash grid)."""

    def __init__(self, points, values, cell: float = 0.08):
        self.cell = cell
        self.cells: dict[tuple, list[int]] = defaultdict(list)
        self.points, self.values = points, values
        for n, p in enumerate(points):
            self.cells[self._key(p)].append(n)

    def _key(self, p):
        return (int(math.floor(p[0] / self.cell)), int(math.floor(p[1] / self.cell)), int(math.floor(p[2] / self.cell)))

    def nearest(self, p, k: int = K_NEAREST) -> list[tuple[float, int]]:
        if not self.points:
            return []
        cx, cy, cz = self._key(p)
        found: list[tuple[float, int]] = []
        r = 0
        while True:
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    for dz in range(-r, r + 1):
                        if max(abs(dx), abs(dy), abs(dz)) != r:
                            continue
                        for n in self.cells.get((cx + dx, cy + dy, cz + dz), ()):
                            found.append((math.dist(p, self.points[n]), n))
            # Everything within r * cell is complete once a ring at that radius is done.
            if len(found) >= min(k, len(self.points)) and sorted(found)[min(k, len(found)) - 1][0] <= r * self.cell:
                break
            if len(found) == len(self.points):
                break
            r += 1
        found.sort()
        return found[:k]


# --- decal / scratch UVs ------------------------------------------------------------
#
# The vehicle pixel shader samples the shared damage-decal texture (SSIC
# 00122b6db3fa1227, 1024x1024 DXT5: dent normals in RGB, scratch mask in
# alpha) and the dirt texture (001251560b05e528) at UV1, semantic 8
# components 2-3 of the 32-byte buffers (paint and lamps). Stock cars unwrap
# each panel onto its own tile of that atlas (front bumper top left, rear
# bumper top right, bonnet, boot, sides in the rows below; lamps on the
# small broken-lamp tile). Converted vertices carried Forza's UV1 (paint) or
# the lamp atlas UV, so damaged cars showed scratch tiles in the wrong places
# as bright white shapes (run 20261022-040545). Uv1Field projects each new
# vertex onto the nearest host triangle of the same panel and material and
# interpolates the host's UV1 there.

def closest_on_triangle(p, a, b, c):
    """Closest point to p on triangle abc: (squared distance, barycentric (u, v, w))."""
    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        bary = (1.0, 0.0, 0.0)
    else:
        bp = _sub(p, b)
        d3, d4 = _dot(ab, bp), _dot(ac, bp)
        cp = _sub(p, c)
        d5, d6 = _dot(ab, cp), _dot(ac, cp)
        vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
        if d3 >= 0 and d4 <= d3:
            bary = (0.0, 1.0, 0.0)
        elif d6 >= 0 and d5 <= d6:
            bary = (0.0, 0.0, 1.0)
        elif vc <= 0 and d1 >= 0 and d3 <= 0:
            t = d1 / (d1 - d3) if d1 != d3 else 0.0
            bary = (1 - t, t, 0.0)
        elif vb <= 0 and d2 >= 0 and d6 <= 0:
            t = d2 / (d2 - d6) if d2 != d6 else 0.0
            bary = (1 - t, 0.0, t)
        elif va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
            t = (d4 - d3) / ((d4 - d3) + (d5 - d6)) if (d4 - d3) + (d5 - d6) else 0.0
            bary = (0.0, 1 - t, t)
        else:
            denom = va + vb + vc
            v, w = (vb / denom, vc / denom) if denom else (0.0, 0.0)
            bary = (1 - v - w, v, w)
    q = tuple(bary[0] * a[k] + bary[1] * b[k] + bary[2] * c[k] for k in range(3))
    d = _sub(p, q)
    return _dot(d, d), bary


class Uv1Field:
    """The host's UV1 (decal-atlas) triangles of one entry's MODL part 0, in
    coordinates normalised by the host car box, grouped for lookups by
    (panel pair, material), material class and all."""

    def __init__(self, tris, box: Box):
        self.box = box
        self.tris = tris  # (normalised a, b, c), (uv a, b, c), pair, material, local, unit normal
        self.groups: dict[tuple, Grid] = {}
        keys = defaultdict(list)
        for n, t in enumerate(tris):
            uvs = t[1]
            if max(abs(uvs[i][k] - uvs[0][k]) for i in (1, 2) for k in (0, 1)) < DEGENERATE_UV:
                # Hidden faces (wheel arches, undersides) all sit on one texel in a
                # corner of the atlas; a vertex projected there drags its triangles
                # across the whole atlas.
                keys[("degenerate",)].append(n)
                continue
            if any(not -0.01 <= uv[k] <= 1.01 for uv in uvs for k in (0, 1)):
                # Tiled UV1 (stock interiors' GLOBAL_LOD_Chrome runs to -4..5): not the
                # panel atlas layout.
                keys[("tiled",)].append(n)
                continue
            keys[("pm", t[2], t[3])].append(n)
            keys[("pc", t[2], t[4])].append(n)
            keys[("c", t[4])].append(n)
            keys[("all",)].append(n)
        # Charts: triangles joined through vertices with the same position and UV1.
        parent = list(range(len(tris)))

        def find(n):
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n
        owner = {}
        for n, t in enumerate(tris):
            for p, uv in zip(t[0], t[1]):
                key = (tuple(round(c, 4) for c in p), round(uv[0], 4), round(uv[1], 4))
                if key in owner:
                    parent[find(n)] = find(owner[key])
                else:
                    owner[key] = n
        self.chart = [find(n) for n in range(len(tris))]
        for n in keys[("all",)]:
            keys[("chart", self.chart[n])].append(n)
        for key, ns in keys.items():
            self.groups[key] = Grid([tuple(sum(v[a] for v in tris[n][0]) / 3 for a in range(3)) for n in ns], ns,
                                    cell=0.06)

    @staticmethod
    def read(stream, vehc, mask: int, local_materials: set) -> "Uv1Field | None":
        mesh = _load_mesh(stream, vehc)
        raw = []
        for lod in mesh.parts[0].lods:
            if lod.mask != mask:
                continue
            for n in lod.submodels:
                sub = mesh.submodels[n]
                for vb, tri in vm.triangles(mesh, sub):
                    e8, e9 = vb.element(8), vb.element(9)
                    if e8 is None or e8.components != 4:
                        continue
                    pts = [vm.position(vb, i) for i in tri]
                    uvs = [tuple(vb.read(e8, i)[2:4]) for i in tri]
                    normal = _norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])))
                    if normal is None:
                        continue
                    raw.append((pts, uvs, s9_pair(vb, tri[0]), sub.material,
                                sub.material in local_materials, normal))
        if not raw:
            return None
        box = Box.of(p for t in raw for p in t[0])
        c, s = box.centre, box.size
        norm = lambda p: tuple((p[a] - c[a]) / (s[a] / 2) for a in range(3))
        return Uv1Field([(tuple(norm(p) for p in t[0]),) + t[1:] for t in raw], box)

    def lookup(self, q, pair, material: bytes, local: bool, any_panel: bool = False, normal=None,
               chart: int | None = None):
        """(UV1, chart) at normalised point q from the nearest host triangle
        facing the same way (`normal`, car space): same panel and material, else
        same panel and material class, else same class; only `chart` if given."""
        keys = (([("chart", chart)] if chart is not None else [])
                + ([] if any_panel else [("pm", pair, material), ("pc", pair, local)])
                + [("c", local), ("all",), ("degenerate",), ("tiled",)])
        for key in keys:
            grid = self.groups.get(key)
            if grid is None:
                continue
            best = None
            for _, n in grid.nearest(q, 16):
                t = self.tris[grid.values[n]]
                d, bary = closest_on_triangle(q, *t[0])
                score = math.sqrt(d) + (FACING_WEIGHT * (1 - _dot(normal, t[5])) if normal else 0.0)
                if best is None or score < best[0]:
                    best = (score, bary, t[1], self.chart[grid.values[n]])
            if best is not None:
                bary, uvs = best[1], best[2]
                return ((sum(bary[k] * uvs[k][0] for k in range(3)), sum(bary[k] * uvs[k][1] for k in range(3))),
                        best[3])
        return None


def s9_pair(vb, index: int) -> tuple[int, int]:
    """Semantic 9 bytes 0, 1 (panel transform, panel data) of a vertex; (0, 0),
    the chassis, for buffers without semantic 9 (some stock interiors, e.g.
    173 and 184, have 32-byte buffers without it)."""
    e9 = vb.element(9)
    if e9 is None:
        return (0, 0)
    s9 = vb.read(e9, index)
    return (s9[0], s9[1])


def stored_normal(buf, at: int):
    """Unit normal from semantic 5 bytes 0, 1 at `at` (as the shader decodes them)."""
    a0, a1 = _angles_of(buf[at], buf[at + 1])
    return _normal_of(a0, a1)


def local_materials(stream, vehc) -> set:
    """Material UIDs an entry's own DRSP defines (the rest are SSIC shared ones)."""
    return {payload(stream, child(e, "MAHE"))[:8] for e in child(child(vehc, "MDXN"), "DRSP").children
            if e.tag == "MACO"}


# --- the host's panels ------------------------------------------------------------

@dataclass
class Panel:
    pair: tuple[int, int]           # s9 bytes 0, 1
    records: list                   # upvh.Record(s) with this pair (normally one)
    part: int | None                # full-entry MODL part of the record
    role: int

    @property
    def record(self):
        return self.records[0]


@dataclass
class HostField:
    """Stock vertices of one entry's part 0: position, s12 - position, panel pair."""
    points: list
    disp: list
    pairs: list
    box: Box
    by_pair: dict = field(default_factory=dict)  # pair -> Grid (normalised coordinates)
    all_grid: Grid | None = None
    uv1: "Uv1Field | None" = None                # decal-atlas UVs (32-byte vertices)
    local: set = field(default_factory=set)       # material UIDs the entry defines itself
    tris: list = field(default_factory=list)      # (normalised corners, pair, unit face normal, paint), part 0

    def normalise(self, p):
        c, s = self.box.centre, self.box.size
        return tuple((p[a] - c[a]) / (s[a] / 2) for a in range(3))


def read_part0(stream, vehc, mask: int) -> list[tuple]:
    """(position, s12 - position, (s9 byte 0, byte 1)) for every vertex in the
    entry's MODL part 0 at `mask` that has semantic 12."""
    drsp = child(child(vehc, "MDXN"), "DRSP")
    mesh = vm.Mesh()
    for e in drsp.children:
        if e.tag == "VXCK":
            hd = payload(stream, child(e, "VXHD"))
            mesh.vertex_buffers[hd[:8]] = vm.VertexBuffer(hd[:8], *struct.unpack_from("<II", hd, 8),
                                                          vm.parse_vdcl(payload(stream, child(e, "VDCL"))),
                                                          payload(stream, child(e, "VXDT")))
        elif e.tag == "IXCK":
            mesh.index_buffers[payload(stream, child(e, "IXHD"))[:8]] = payload(stream, child(e, "IXDT"))
        elif e.tag == "SBMC":
            d = payload(stream, child(e, "SBBH"))
            mesh.submodels.append(vm.SubModel(d[0:8], d[8:16], d[16:24], struct.unpack_from("<I", d, 0x20)[0],
                                              d[0x30:0x38], *struct.unpack_from("<II", d, 0x38)))
    mesh.parts = vm.parse_modl(payload(stream, child(child(vehc, "NONR"), "MODL")), mesh.submodels)
    seen = {}
    for lod in mesh.parts[0].lods:
        if lod.mask != mask:
            continue
        for n in lod.submodels:
            for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                e12 = vb.element(12)
                if e12 is None:
                    continue
                for i in tri:
                    if (vb.uid, i) not in seen:
                        p = vm.position(vb, i)
                        q = vb.read(e12, i)
                        s9 = s9_pair(vb, i)
                        seen[(vb.uid, i)] = (p, (q[0] - p[0], q[1] - p[1], q[2] - p[2]), (s9[0], s9[1]))
    return list(seen.values())


def host_field(stream, vehc, mask: int) -> HostField:
    rows = read_part0(stream, vehc, mask)
    if not rows:
        raise ValueError(f"{vehc.name}: no semantic-12 vertices in MODL part 0 mask {mask}")
    f = HostField([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], Box.of(r[0] for r in rows))
    groups = defaultdict(list)
    for n, pair in enumerate(f.pairs):
        groups[pair].append(n)
    for pair, ns in groups.items():
        f.by_pair[pair] = Grid([f.normalise(f.points[n]) for n in ns], ns)
    f.all_grid = Grid([f.normalise(p) for p in f.points], list(range(len(f.points))))
    mesh = _load_mesh(stream, vehc)
    for lod in mesh.parts[0].lods:
        if lod.mask != mask:
            continue
        for n in lod.submodels:
            for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                if vb.element(12) is None:
                    continue
                pts = [vm.position(vb, i) for i in tri]
                normal = _norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])))
                if normal is not None:
                    f.tris.append((tuple(f.normalise(p) for p in pts), s9_pair(vb, tri[0]), normal,
                                   mesh.submodels[n].material in PAINT_MATERIALS))
    return f


def host_panels(upvh_data: bytes, modl_data: bytes) -> dict[tuple, Panel]:
    """Panel pair -> Panel, from the records that carry a +0x2A index. The
    chassis is (0, 0) (role 38); records without a +0x34 slot use transform 0."""
    h = upvh_mod.parse(upvh_data)
    parts = upvh_mod.modl_part_uids(modl_data)
    out: dict[tuple, Panel] = {}
    for r in h.records:
        if r.model < 0 or r.type != 1:
            continue
        pair = (max(r.slot, 0) & 0xFF, r.model & 0xFF)
        part = parts.index(r.uid) if r.uid in parts else None
        if pair in out:
            out[pair].records.append(r)
        else:
            out[pair] = Panel(pair, [r], part, r.role)
    return out


# --- section rules ----------------------------------------------------------------

def section_rule(name: str) -> tuple[str, tuple]:
    """How a Forza main-carbin section is put on host panels:
    ("fixed", roles) the first role the host has; ("side", roles) the record of
    those roles on the section's side; ("glass", (front|rear)); ("majority",
    roles) the panel most of its triangles are nearest to; ("nearest", roles)
    per triangle."""
    n = name.lower()
    plate = re.fullmatch(r"(.*)_plate_(front|rear)", n)
    if plate:
        # Licence plates (tools/plates.py): the panel of the section they sit
        # on (bumper, boot lid), whole: one panel per plate, like stock. On a
        # plain body section: at the front the bumper (stock: 121 of 131 front
        # plates), at the rear the nearest of rear bumper, boot lid, chassis
        # (stock: 73, 34, 32 of 141).
        kind, roles = section_rule(plate.group(1))
        if kind == "nearest":
            if plate.group(2) == "front":
                return "fixed", (35, 37, 38)
            return "majority", (36, 5, 38)
        return kind, roles
    if n == "steering_wheel":
        return "fixed", (1,)
    # The bumper frames sit right behind the bumper skins. On the chassis or
    # front structure they crushed less than the skin and poked through it
    # (the M5's rear frame showed as a white chevron on the damaged bumper,
    # run 20261022-062852), so they go with their bumper, as stock bumper
    # parts carry their own trim.
    if n.startswith("bumperframef"):
        return "fixed", (35, 37, 38)
    if n.startswith("bumperframer"):
        return "fixed", (36, 38)
    if n.startswith("bumperf"):
        return "fixed", (35, 37, 38)
    if n.startswith("bumperr"):
        return "fixed", (36, 38)
    if n.startswith("hood"):
        return "fixed", (4, 37, 38)
    if n.startswith("trunk"):
        return "fixed", (5, 124, 38)
    if n.startswith("headlight") or n in ("glasslhl", "glassrhl"):
        return "side", (50, 51)
    if n.startswith("mirror"):
        return "side", (40, 41)
    if n.startswith("exhaust"):
        return "side", (136, 15)
    if n == "glassf":
        return "glass", ("front",)
    if n == "glassr":
        return "glass", ("rear",)
    if n.startswith("taillight") or n in ("glassltl", "glassrtl"):
        return "majority", (5, 38, 36)
    if n.startswith("glass") or n.startswith("window"):
        return "majority", DOORS + (38,)  # side windows: their door, or fixed glass
    if n.startswith("wing") or n.startswith("spoiler"):
        return "majority", (5, 124, 38)
    if n in ("interior", "cagerace") or n.startswith("seat") or n.startswith("undercarriage"):
        return "fixed", (38,)
    return "nearest", BODY


def mirror_key(name: str) -> str:
    """Section name with its side letter blanked: taillightL/R -> taillight?,
    glassLTL/glassRTL -> glass?TL."""
    return re.sub(r"(?<=[a-z])[LR](?=[A-Z]|$)", "?", name)


# --- exposed inner geometry -----------------------------------------------------------
#
# When the bonnet, boot lid or a bumper swings open or falls off, stock cars
# show structure drawn from the shared chassis texture (SSIC 00122b73d1fa1227,
# which every GLOBAL_Chassis* material samples: radiator, bulkhead, inner wings,
# strut towers, subframe, boot tub, crash beam and undercarriage pictures) and
# the panel's underside in body paint over the texture's inner-frame picture
# (GLOBAL_Chassis; its blue channel is the paint mask). Baked sky there follows
# the facing: bay floors 0.4-0.7, walls ~0.34, undersides ~0.03, bonnet
# undersides 0.01-0.24 (hosts 147, 193, 227, 251, 266); boot wells 0.23-0.34 and
# the structure behind the bumpers 0.09-0.41 (out/underside/stockref-ext.txt).
# Forza models have that geometry too (techniques frame, black, bottom,
# undercarriage...), but the converter put every trim on TRIM_UV, one dark
# texel (R 24), and Forza's AO is baked with the panels shut: a flat black void
# once a panel was gone (BMW M5 bay, testdrive3; M5 and Skyline boot wells and
# rear panels, testdrive4). split_exposed puts that geometry into subsections
# of its own:
#   inner: trim triangles of hood*/trunk* sections facing away from the
#          section's paint (the panel's underside): technique INNER_TECHNIQUE,
#          the shared paint material;
#   zones: trim triangles of other sections hidden by a removable panel
#          (EXPOSED_ZONES): under the bonnet ("bay") or the boot lid ("boot"),
#          behind the rear or front bumper skin ("rear", "front").
# With DamagePlan (add_geometry(exposed=True)) their vertices carry a sentinel
# UV0 per tag, which _expose replaces by a planar projection of the vertex
# (in the zone's or the panel's box) onto that tag's picture, and a
# facing-based sky. Forza bay triangles are large (the M5's floor is 44
# triangles over 1.65 m2), so copying the host's UVs per vertex stretched its
# pictures into streaks; a projection keeps them continuous.

INNER_TECHNIQUE = "dsf_inner"   # forza_materials: paint role
BAY_CELL = 0.05                 # m; panel height/depth maps
BAY_CLEARANCE = 0.01            # m below the hood surface
BAY_DEPTH = 0.55                # m below the hood's lowest point that the bay box spans
BOOT_DEPTH = 0.55               # m below the boot lid's lowest point that the boot box spans
BUMPER_CLEARANCE = 0.02         # m inward of a bumper skin's outer surface
BUMPER_DEPTH = 0.6              # m inward of it that counts as behind the bumper
INNER_REACH = 0.2               # m; trim triangle to the nearest paint triangle of its panel
BAY_SKIP = ("hood", "headlight", "taillight", "glass", "window", "mirror", "steering", "interior", "seat",
            "wheel", "trunk")
ZONE_SKIP = BAY_SKIP + ("bumper", "exhaust", "numplate", "numberplate")


def _rear_bumper(n: str) -> bool:
    return n.startswith(("bumperr", "bumperframer"))


def _front_bumper(n: str) -> bool:
    return n.startswith("bumperf") and not n.startswith("bumperframer")


def _hood(n: str) -> bool:
    return n.startswith("hood")


def _trunk(n: str) -> bool:
    return n.startswith("trunk")


def exposed_zones(engine: str | None = None) -> tuple:
    """Zones a removable panel hides, in priority order: (kind, panel sections,
    map axis, inward sign along it, map value per cell ("inner": the panel's
    innermost point, "outer": its outermost), reach inward of it (None: any)).
    Placed (DSF) space: +y up, +z front. The engine bay ("bay") is under the
    bonnet, or under the boot lid (engine cover) of a mid- or rear-engined car
    (Forza Data_Car.EnginePlacementID; engine_placement), whose bonnet then
    covers a luggage well ("boot")."""
    front, rear = (_hood, _trunk) if engine not in ("mid", "rear") else (_trunk, _hood)
    return (("bay", front, 1, -1, "inner", None),
            ("boot", rear, 1, -1, "inner", None),
            ("rear", _rear_bumper, 2, +1, "outer", BUMPER_DEPTH),
            ("front", _front_bumper, 2, -1, "outer", BUMPER_DEPTH))


EXPOSED_ZONES = exposed_zones()


def engine_placement(name: str, gamedbs=(), folder=None) -> str | None:
    """'front', 'mid' or 'rear' from Forza's gamedb.slt (Data_Car.EnginePlacementID
    of the car's MediaName), from the first of `gamedbs` (else the one next to
    the car's folder) that lists it; None if unknown."""
    import sqlite3
    from pathlib import Path
    dbs = [Path(g) for g in gamedbs or () if g]
    if folder is not None:
        dbs.append(Path(folder).parent / "gamedb.slt")
    for db in dbs:
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = con.execute("SELECT p.EnginePlacement FROM Data_Car c JOIN List_EnginePlacement p "
                              "ON p.ID = c.EnginePlacementID WHERE c.MediaName = ? COLLATE NOCASE",
                              (name,)).fetchone()
            con.close()
        except sqlite3.Error:
            continue
        if row and row[0]:
            return str(row[0]).lower()
    return None
# tag -> (picture in the shared chassis texture (u0, u1, v0, v1), axis along u, axis along v);
# an axis "y" runs up the picture. Pictures read off out/dmgfix/trimtex-grid-*.png and
# out/underside/atlas-boot.png.
_TUB = (0.420, 0.495, 0.170, 0.300)         # boot tub panels, the part stock boot wells use (147: R ~48)
_BEAM = (0.310, 0.500, 0.365, 0.530)        # rear panel and crash beam (stock 147/229 behind the bumper)
_SUBFRAME = (0.495, 0.598, 0.560, 0.875)    # subframe from below
_INNER_WING = (0.095, 0.172, 0.100, 0.550)  # inner wing, strut tower
_RADIATOR = (0.207, 0.300, 0.005, 0.135)
EXPOSED_MAP = {
    "bay+y": ((0.216, 0.312, 0.356, 0.534), 2, 0),     # floor: engine and wiring, from above
    "bay-y": (_SUBFRAME, 0, 2),                        # underside: subframe from below
    "bay x": (_INNER_WING, 1, 2),                      # side walls: inner wing, strut tower
    "bay+z": ((0.315, 0.495, 0.030, 0.330), 0, 1),     # facing forward: bulkhead panelling
    "bay-z": (_RADIATOR, 0, 1),                        # facing back: radiator
    "inner y": ((0.354, 0.487, 0.562, 0.746), 0, 2),   # bonnet/boot lid undersides: inner frame (paint)
    "inner z": ((0.354, 0.487, 0.562, 0.746), 0, 1),   # hatch inner faces
    "inner x": ((0.354, 0.487, 0.562, 0.746), 2, 1),
    "boot+y": (_TUB, 0, 2),                            # boot floor
    "boot-y": (_SUBFRAME, 0, 2),                       # under the boot floor
    "boot x": (_TUB, 2, 1),                            # boot side walls
    "boot+z": (_TUB, 0, 1),                            # boot rear wall (faces forward)
    "boot-z": (_TUB, 0, 1),                            # boot front wall (seat back side)
    "rear+y": (_SUBFRAME, 0, 2),
    "rear-y": (_SUBFRAME, 0, 2),
    "rear x": (_INNER_WING, 2, 1),
    "rear+z": (_BEAM, 0, 1),
    "rear-z": (_BEAM, 0, 1),                           # rear panel / crash beam, facing back
    "front+y": (_SUBFRAME, 0, 2),
    "front-y": (_SUBFRAME, 0, 2),
    "front x": (_INNER_WING, 2, 1),
    "front+z": (_RADIATOR, 0, 1),                      # facing forward: radiator
    "front-z": (_BEAM, 0, 1),
}
# Sentinel UV0 per tag (exact in half floats; add_geometry writes them, _expose replaces them).
EXPOSED_SENTINEL = {tag: (-2.0, -1.0 - n) for n, tag in enumerate(EXPOSED_MAP)}
INNER_SKY = 0.2                 # stock bonnet undersides 0.01-0.24
BAY_SKY = (0.04, 0.35, 0.62)    # facing down, sideways, up (stock front structure)
ZONE_SKY = {"bay": BAY_SKY,
            "boot": (0.08, 0.28, 0.40),     # stock boot wells 0.23-0.34 (147, 227, 229, 266, 193)
            "rear": (0.08, 0.28, 0.40),     # stock behind the bumpers 0.09-0.41
            "front": (0.08, 0.28, 0.40)}


def exposed_sky(tag: str, normal) -> float:
    kind = _kind_of(tag)
    if kind == "inner":
        return INNER_SKY
    ny = normal[1] if normal else 0.0
    lo, mid, hi = ZONE_SKY[kind]
    return mid + (hi - mid) * ny if ny >= 0 else mid + (mid - lo) * ny


def _kind_of(tag: str) -> str:
    return re.match(r"[a-z]+", tag).group(0)


def _axis_tag(kind: str, normal) -> str:
    """kind + the dominant axis of a face normal (zones: with its sign for y and z)."""
    a = max(range(3), key=lambda k: abs(normal[k]))
    if kind == "inner":
        return f"inner {'xyz'[a]}"
    if a == 0:
        return f"{kind} x"
    return f"{kind}{'+' if normal[a] > 0 else '-'}{'xyz'[a]}"


def _tri_samples(pts, step: float):
    """Points spread over a triangle about `step` apart (corners included)."""
    longest = max(math.dist(pts[a], pts[b]) for a, b in ((0, 1), (1, 2), (2, 0)))
    n = max(1, min(40, int(math.ceil(longest / step))))
    for i in range(n + 1):
        for j in range(n + 1 - i):
            a, b = i / n, j / n
            yield tuple(pts[0][k] * (1 - a - b) + pts[1][k] * a + pts[2][k] * b for k in range(3))


def zone_maps(body, place, lods, zones=EXPOSED_ZONES) -> dict:
    """Per (LOD, EXPOSED_ZONES kind): the panel sections' map over BAY_CELL
    cells across the zone's axis (inward coordinate of the panel's innermost or
    outermost point per cell)."""
    maps = {}
    for kind, is_panel, axis, sign, which, _ in zones:
        across = [a for a in range(3) if a != axis]
        pick = max if which == "inner" else min
        secs = [s for s in body if is_panel(s.name.lower())]
        for lod in lods:
            cells: dict = {}
            for s in secs:
                for sub in s.subsections:
                    if sub.lod != lod:
                        continue
                    pool = s.vertices_for(sub)
                    for t in sub.triangles():
                        for p in _tri_samples([place.point(pool[i].position) for i in t], BAY_CELL / 2):
                            key = (math.floor(p[across[0]] / BAY_CELL), math.floor(p[across[1]] / BAY_CELL))
                            d = sign * p[axis]
                            old = cells.get(key)
                            cells[key] = d if old is None else pick(old, d)
            if cells:
                maps[(lod, kind)] = cells
    return maps


def zone_of(maps: dict, lod: int, c, zones=EXPOSED_ZONES) -> str | None:
    """The first zone kind whose panel hides point c (placed) at this LOD."""
    for kind, _, axis, sign, which, reach in zones:
        cells = maps.get((lod, kind))
        if not cells:
            continue
        across = [a for a in range(3) if a != axis]
        d0 = cells.get((math.floor(c[across[0]] / BAY_CELL), math.floor(c[across[1]] / BAY_CELL)))
        if d0 is None:
            continue
        d = sign * c[axis] - d0
        if d > (BAY_CLEARANCE if which == "inner" else BUMPER_CLEARANCE) and (reach is None or d < reach):
            return kind
    return None


ZONE_STEP = 0.1                 # m; sample spacing over a triangle for zone_vote
ZONE_SHARE = 0.25               # share of a triangle's samples a zone needs


def zone_vote(maps: dict, lod: int, pts, zones=EXPOSED_ZONES) -> str | None:
    """The zone hiding most of a triangle (corners, centroid and points about
    ZONE_STEP apart), if it hides at least ZONE_SHARE of them. Forza's
    structure behind the panels is a few large triangles (the M5's rear panel,
    the Skyline's boot walls) whose centroids alone often sat just outside the
    panel's map or reach."""
    c = tuple(sum(p[k] for p in pts) / 3 for k in range(3))
    samples = [c] + list(_tri_samples(pts, ZONE_STEP))
    votes = Counter(zone_of(maps, lod, q, zones) for q in samples)
    votes.pop(None, None)
    if not votes:
        return None
    kind, n = votes.most_common(1)[0]
    return kind if n >= ZONE_SHARE * len(samples) else None


def split_exposed(body, place, log=None, engine: str | None = None) -> tuple[list, dict]:
    """Split the trim subsections of `body` that a missing bonnet, boot lid or
    bumper exposes into their own subsections, one per EXPOSED_MAP tag (inner
    faces of hood*/trunk*, technique INNER_TECHNIQUE; trims hidden by a panel,
    by EXPOSED_ZONES zone and facing). Each new subsection carries `exposed`
    (the tag) and `exposed_uv` (its EXPOSED_SENTINEL, which
    add_geometry(exposed=True) writes as UV0). Returns (new body, report).
    Untouched sections and subsections stay the same objects."""
    import carbin
    import forza_materials as fm
    lods = sorted({sub.lod for s in body for sub in s.subsections if sub.lod > 0})
    zones = exposed_zones(engine)
    maps = zone_maps(body, place, lods, zones)
    report = Counter()
    out = []
    for section in body:
        name = section.name.lower()
        panel = name.startswith(("hood", "trunk"))
        bay_ok = not name.startswith(BAY_SKIP)
        zone_ok = not name.startswith(ZONE_SKIP)
        paint_grid = {}
        new_subs, changed = [], False
        for sub in section.subsections:
            role = fm.classify(sub.material)[0]
            if sub.lod <= 0 or role != fm.TRIM or "plate" in sub.material.lower() or not (panel or bay_ok):
                new_subs.append(sub)
                continue
            pool = section.vertices_for(sub)
            if panel and sub.lod not in paint_grid:
                cents, normals = [], []
                for other in section.subsections:
                    if other.lod != sub.lod or fm.classify(other.material)[0] != fm.PAINT:
                        continue
                    opool = section.vertices_for(other)
                    for t in other.triangles():
                        pts = [place.point(opool[i].position) for i in t]
                        nrm = _norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])))
                        if nrm is not None:
                            cents.append(tuple(sum(p[k] for p in pts) / 3 for k in range(3)))
                            normals.append(nrm)
                paint_grid[sub.lod] = Grid(cents, normals, cell=0.1)
            groups = defaultdict(list)
            for t in sub.triangles():
                pts = [place.point(pool[i].position) for i in t]
                c = tuple(sum(p[k] for p in pts) / 3 for k in range(3))
                nrm = _norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])))
                tag = None
                if panel and nrm is not None:
                    grid = paint_grid[sub.lod]
                    hits = grid.nearest(c, 1)
                    if hits and hits[0][0] < INNER_REACH and _dot(nrm, grid.values[hits[0][1]]) < -0.3:
                        tag = _axis_tag("inner", nrm)
                elif not panel and nrm is not None:
                    kind = zone_vote(maps, sub.lod, pts, zones)
                    # Bumper parts and exhausts (they leave with their panel or are
                    # their own) keep their trim outside the bay, as before.
                    if kind is not None and (kind == "bay" or zone_ok):
                        tag = _axis_tag(kind, nrm)
                groups[tag].extend(t)
            if set(groups) <= {None}:
                new_subs.append(sub)
                continue
            changed = True
            for tag, idx in groups.items():
                if not idx:
                    continue
                material = INNER_TECHNIQUE if tag and tag.startswith("inner") else sub.material
                part = carbin.SubSection(material, sub.lod, 4, sub.uv_transform, idx)
                part.exposed, part.exposed_uv = tag, EXPOSED_SENTINEL.get(tag)
                new_subs.append(part)
                if tag:
                    report[f"{_kind_of(tag)} lod {sub.lod}"] += len(idx) // 3
        if changed:
            section = dataclasses.replace(section, subsections=new_subs)
        out.append(section)
    if log and report:
        log("exposed geometry: " + ", ".join(f"{k} {v}" for k, v in sorted(report.items())))
    return out, dict(report)


def smooth_labels(labels: list, areas: list, corners: list, passes: int = 2) -> tuple[list, int]:
    """Clean up a per-triangle panel labelling over the mesh (triangles joined
    by shared edges, corners given as position keys): `passes` rounds of an
    area-weighted majority filter, then connected patches of one label smaller
    than ISLAND_AREA (or of at most ISLAND_TRIANGLES triangles, below
    ISLAND_AREA_MAX) take the label they share most edges with. Returns
    (labels, patches merged)."""
    n = len(labels)
    owners = defaultdict(list)
    for t, cs in enumerate(corners):
        for a, b in ((0, 1), (1, 2), (2, 0)):
            if cs[a] != cs[b]:
                owners[(min(cs[a], cs[b]), max(cs[a], cs[b]))].append(t)
    nbrs = [[] for _ in range(n)]
    for ts in owners.values():
        if 2 <= len(ts) <= 4:
            for a in ts:
                nbrs[a].extend(b for b in ts if b != a)
    labels = list(labels)
    for _ in range(passes):
        new = list(labels)
        for t in range(n):
            if nbrs[t]:
                w = Counter({labels[t]: areas[t]})
                for u in nbrs[t]:
                    w[labels[u]] += areas[u]
                new[t] = w.most_common(1)[0][0]
        labels = new
    merged = 0
    for _ in range(12):
        comp, patches = [-1] * n, []
        for t in range(n):
            if comp[t] >= 0:
                continue
            comp[t], members, stack = len(patches), [], [t]
            while stack:
                x = stack.pop()
                members.append(x)
                for u in nbrs[x]:
                    if comp[u] < 0 and labels[u] == labels[t]:
                        comp[u] = comp[t]
                        stack.append(u)
            patches.append((sum(areas[x] for x in members), members))
        changed = 0
        for area, members in sorted(patches, key=lambda p: p[0]):
            if area >= ISLAND_AREA_MAX:
                break
            if area >= ISLAND_AREA and len(members) > ISLAND_TRIANGLES:
                continue
            own = labels[members[0]]
            border = Counter(labels[u] for x in members for u in nbrs[x] if labels[u] != own)
            if not border:
                continue  # a separate piece of geometry: keep its vote
            new = border.most_common(1)[0][0]
            for x in members:
                labels[x] = new
            changed += 1
        merged += changed
        if not changed:
            break
    return labels, merged


# --- the plan for one car ---------------------------------------------------------

def _set_idle_priority() -> None:
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), 0x40)  # IDLE_PRIORITY_CLASS


@dataclass
class DamageOptions:
    hull: bool = False      # also scale the root collision hull (PDL) to the Forza body
    k: int = K_NEAREST


class DamagePlan:
    """Everything the damage conversion needs from the host, read before the
    converter replaces the host's geometry."""

    def __init__(self, stream, full, exterior, car, place, opts: DamageOptions | None = None, log=print,
                 interior=None, engine: str | None = None):
        import forza2dsf
        self.f2d = forza2dsf
        self.opts = opts or DamageOptions()
        self.place = place
        self.car = car
        self.log = log
        nonr = child(full, "NONR")
        self.upvh = payload(stream, child(nonr, "UPVH"))
        self.panels = host_panels(self.upvh, payload(stream, child(nonr, "MODL")))
        self.full_field = host_field(stream, full, 4)
        self.ext_field = host_field(stream, exterior, 2) if exterior is not None else self.full_field
        self.chassis = (0, 0) if (0, 0) in self.panels else min(self.panels)
        for f, vehc, mask in ((self.full_field, full, 4), (self.ext_field, exterior, 2)):
            if vehc is not None:
                f.local = local_materials(stream, vehc)
                f.uv1 = Uv1Field.read(stream, vehc, mask, f.local)
        self.int_uv1 = None
        if interior is not None:
            self.int_local = local_materials(stream, interior)
            self.int_uv1 = Uv1Field.read(stream, interior, 64, self.int_local)
        # The Forza car's box (placed), over its finest body LOD.
        lods = sorted({sub.lod for s in car.body for sub in s.subsections if sub.lod > 0})
        pts = [place.point(v.position) for s in car.body for sub in s.subsections if sub.lod == lods[0]
               for t in sub.triangles() for v in (s.vertices_for(sub)[i] for i in t)]
        self.forza_box = Box.of(pts)
        # Projection boxes for exposed inner geometry (split_exposed, _expose).
        self.exposed_boxes: dict[str, Box] = {}
        for name in ("hood", "trunk"):
            box = Box.of(place.point(v.position) for s in car.body if s.name.lower().startswith(name)
                         for sub in s.subsections if sub.lod == lods[0]
                         for t in sub.triangles() for v in (s.vertices_for(sub)[i] for i in t))
            if box is not None:
                self.exposed_boxes[name] = box
        # Zones (exposed_zones): under a cover, its box deepened by BAY_DEPTH or
        # BOOT_DEPTH; behind a bumper, its box deepened inward by BUMPER_DEPTH.
        for kind, is_panel, axis, sign, _, reach in exposed_zones(engine):
            box = Box.of(place.point(v.position) for s in car.body if is_panel(s.name.lower())
                         for sub in s.subsections if sub.lod == lods[0]
                         for t in sub.triangles() for v in (s.vertices_for(sub)[i] for i in t))
            if box is None:
                continue
            lo, hi = list(box.lo), list(box.hi)
            if kind in ("bay", "boot"):
                lo[1] -= BAY_DEPTH if kind == "bay" else BOOT_DEPTH
            elif sign > 0:
                hi[axis] = lo[axis] + reach
            else:
                lo[axis] = hi[axis] - reach
            self.exposed_boxes[kind] = Box(tuple(lo), tuple(hi))
        self._assign: dict[tuple, dict] = {}
        self._members: dict[tuple, dict] = {}
        self._grids: dict[tuple, Grid] = {}
        self.boxes: dict[tuple, list] = defaultdict(list)  # pair -> placed points (finest LOD built)
        self._boxes_lod: int | None = None
        self._seam: dict = {}           # quantised position -> rigid panels sharing it (current LOD)
        self._seam_written: set = set()  # (field, position) given a shared crushed position
        self._seam_rep: dict = {}        # quantised position -> its near-coincident group's position
        self._pins: dict = {}            # pair -> Grid of its pinned seam positions (current LOD)
        self._motion: dict = {}          # id(HostField) -> {frozenset of roles: median host border crush}
        self.report: dict = {"panels": {}}

    # -- panel lookup --
    def panels_with(self, roles) -> list[Panel]:
        return [p for p in self.panels.values() if p.role in roles]

    def _host_box(self, pair, f: HostField | None = None) -> Box | None:
        f = f or self.full_field
        return Box.of(f.points[n] for n in f.by_pair[pair].values) if pair in f.by_pair else None

    def _forza_norm(self, p):
        c, s = self.forza_box.centre, self.forza_box.size
        return tuple((p[a] - c[a]) / (s[a] / 2) for a in range(3))

    def _nearest_pair(self, p_dsf, allowed: set, f: HostField, normal=None, paint: bool = False) -> tuple | None:
        """The panel of the host surface nearest to a placed point: a vote of
        the 3 host triangles (of the allowed panels) closest to it in
        normalised coordinates, by surface distance plus PANEL_FACING per unit
        of normal mismatch. Host triangles, not vertices: the host's door skins
        are large triangles with vertices only at their edges, so a point
        mid-door was nearer to vertices of the chassis behind it (door jambs,
        cabin), and the vertex vote put chassis islands into the M5's door
        skins, which tore open when crushed (testdrive3). A paint triangle
        votes among the host's paint triangles first: the outer skins are
        shared paint, while the structure a few centimetres under them (the
        front structure under the M5's wings) is trim or the host's own
        chassis texture."""
        if paint:
            pair = self._vote(self._tri_index(f, allowed, paint=True), f, p_dsf, normal, PAINT_REACH)
            if pair is not None:
                return pair
        return self._vote(self._tri_index(f, allowed), f, p_dsf, normal)

    def _vote(self, index, f: HostField, p_dsf, normal, reach: float | None = None) -> tuple | None:
        if index is None:
            return None
        cells, cell = index
        q = self._forza_norm(p_dsf)
        key = tuple(int(math.floor(q[a] / cell)) for a in range(3))
        scored = []
        for r in range(0, 8):
            seen = set()
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    for dz in range(-r, r + 1):
                        if max(abs(dx), abs(dy), abs(dz)) != r:
                            continue
                        seen.update(cells.get((key[0] + dx, key[1] + dy, key[2] + dz), ()))
            for n in seen:
                t = f.tris[n]
                d, _ = closest_on_triangle(q, *t[0])
                scored.append((math.sqrt(d) + (PANEL_FACING * (1 - _dot(normal, t[2])) if normal else 0.0), t[1]))
            # Cells within r of q's cell cover every triangle closer than r * cell.
            if len(scored) >= 3 and sorted(scored)[2][0] <= r * cell:
                break
        if reach is not None:
            scored = [sc for sc in scored if sc[0] <= reach]
        if not scored:
            return None
        votes = Counter()
        for score, pair in sorted(scored)[:3]:
            votes[pair] += 1.0 / (score + 0.005)
        return votes.most_common(1)[0][0]

    def _tri_index(self, f: HostField, allowed: set, paint: bool = False):
        """Host part-0 triangles of the allowed panels (paint: only the shared
        paint ones), binned by every grid cell their (normalised) bounding box
        touches."""
        key = (id(f), frozenset(allowed), "tris", paint)
        if key not in self._grids:
            cell = 0.08
            cells = defaultdict(list)
            for n, (pts, pair, _, is_paint) in enumerate(f.tris):
                if pair not in allowed or (paint and not is_paint):
                    continue
                lo = [int(math.floor(min(p[a] for p in pts) / cell)) for a in range(3)]
                hi = [int(math.floor(max(p[a] for p in pts) / cell)) for a in range(3)]
                for i in range(lo[0], hi[0] + 1):
                    for j in range(lo[1], hi[1] + 1):
                        for k in range(lo[2], hi[2] + 1):
                            cells[(i, j, k)].append(n)
            self._grids[key] = (dict(cells), cell) if cells else None
        return self._grids[key]

    def _side_panel(self, roles, centroid) -> tuple | None:
        cands = [p for p in self.panels_with(roles) if p.pair in self.full_field.by_pair]
        if not cands:
            return None
        if abs(centroid[0]) > 0.1 * self.forza_box.size[0] / 2:
            same = [p for p in cands if (self._host_box(p.pair).centre[0] > 0) == (centroid[0] > 0)]
            cands = same or cands
        q = self._forza_norm(centroid)
        return min(cands, key=lambda p: math.dist(q, self.full_field.normalise(self._host_box(p.pair).centre))).pair

    def _glass_panel(self, which: str) -> tuple | None:
        """The host's centred glass panel (role 8) in the front (z > 0) or rear
        half: windscreen or rear window. None if the host has none there (the
        stock 370Z's rear window is part of its hatch)."""
        cands = [p for p in self.panels_with((8,)) if p.pair in self.full_field.by_pair
                 and abs(self._host_box(p.pair).centre[0]) < 0.3
                 and (self._host_box(p.pair).centre[2] > 0) == (which == "front")]
        if not cands:
            return None
        key = lambda p: self._host_box(p.pair).centre[2]
        return (max if which == "front" else min)(cands, key=key).pair

    def _first(self, roles) -> tuple | None:
        for role in roles:
            for p in self.panels_with((role,)):
                if p.pair in self.full_field.by_pair or role == 1:
                    return p.pair
        return None

    def assignment(self, sections, lod: int) -> dict:
        """{(id(sub), triangle index): panel pair} for every body triangle at `lod`."""
        import forza_materials as fm
        key = (id(sections), lod)
        if key in self._assign:
            return self._assign[key]
        f = self.full_field
        out = {}
        per_section = {}
        majority: dict[str, list] = {}
        for section in sections:
            kind, roles = section_rule(section.name)
            tris, shape = [], []  # (sub id, index, centroid); (unit normal, area, corner keys, paint)
            for sub in section.subsections:
                if sub.lod != lod:
                    continue
                pool = section.vertices_for(sub)
                is_paint = fm.classify(sub.material)[0] == fm.PAINT
                for k, t in enumerate(sub.triangles()):
                    pts = [self.place.point(pool[i].position) for i in t]
                    tris.append((id(sub), k, tuple(sum(p[a] for p in pts) / 3 for a in range(3))))
                    cross = _cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0]))
                    shape.append((_norm(cross), math.sqrt(_dot(cross, cross)) / 2,
                                  [tuple(round(c * 2000) for c in p) for p in pts], is_paint))
            if not tris:
                continue
            centroid = tuple(sum(t[2][a] for t in tris) / len(tris) for a in range(3))
            pair = None
            if kind == "fixed":
                pair = self._first(roles)
            elif kind == "side":
                pair = self._side_panel(roles, centroid)
            elif kind == "glass":
                pair = self._glass_panel(roles[0])
            if kind == "glass" and pair is None:
                kind, roles = "majority", ((4, 37, 38) if roles[0] == "front" else (124, 5, 38))
            if kind in ("fixed", "side", "glass"):
                pair = pair or self.chassis
                for sid, k, _ in tris:
                    out[(sid, k)] = pair
                per_section[section.name] = Counter({pair: len(tris)})
                continue
            allowed = {p.pair for p in self.panels_with(roles)} & set(f.by_pair)
            allowed = allowed or {self.chassis}
            labels = [self._nearest_pair(c, allowed, f, shape[n][0], shape[n][3]) or self.chassis
                      for n, (_, _, c) in enumerate(tris)]
            if kind == "nearest":
                # Panels as connected patches of the Forza surface: a seam
                # between two panels opens when they crush differently, so
                # stray triangles and small islands join the panel around them.
                labels, merged = smooth_labels(labels, [s[1] for s in shape], [s[2] for s in shape])
                self.report.setdefault("islands_merged", {})[str(lod)] = (
                    self.report.get("islands_merged", {}).get(str(lod), 0) + merged)
            votes = Counter(labels)
            picks = [(sid, k, pair) for (sid, k, _), pair in zip(tris, labels)]
            if kind == "majority":
                # Centred panels (boot, chassis, bumper): a left/right pair of
                # sections (taillightL/R) votes together, so both lamps land on
                # the same panel.
                group = mirror_key(section.name) if not set(roles) & set(DOORS) else section.name
                majority.setdefault(group, []).append((section.name, picks, votes))
                continue
            for sid, k, pair in picks:
                out[(sid, k)] = pair
            per_section[section.name] = votes
        for group, members_ in majority.items():
            total = sum((v for _, _, v in members_), Counter())
            pair = total.most_common(1)[0][0]
            for name, picks, _ in members_:
                for sid, k, _ in picks:
                    out[(sid, k)] = pair
                per_section[name] = Counter({pair: len(picks)})
        self._assign[key] = out
        members: dict[tuple, set] = defaultdict(set)
        sub_owner = {id(sub): id(section) for section in sections for sub in section.subsections}
        for (sid, _), pair in out.items():
            members[pair].add(sub_owner[sid])
        self._members[key] = members
        self.report.setdefault("sections", {})[str(lod)] = {
            name: {self.label(p): n for p, n in votes.items()} for name, votes in per_section.items()}
        return out

    def label(self, pair) -> str:
        p = self.panels.get(pair)
        return f"{pair[0]},{pair[1]} {role_name(p.role) if p else '?'}"

    # -- geometry --
    def _geometry(self, builder, sections, lod, f: HostField, record_box: bool = False,
                  **kwargs) -> dict[tuple, list]:
        """Per panel: add_geometry over that panel's triangles, then s9, s12 and
        normal deltas written into the new vertices."""
        assign = self.assignment(sections, lod)
        by_pair = self._members[(id(sections), lod)]
        kwargs.pop("glass_links", None)  # panels replace the glass links
        out = {}
        if record_box and self._boxes_lod is not None and lod > self._boxes_lod:
            record_box = False  # keep the boxes of the finest full-entry LOD
        if record_box and self._boxes_lod != lod:
            self.boxes.clear()
            self._boxes_lod = lod
        self._seam = self._seam_map(sections, lod, assign)
        self._pins = self._pin_map(f)
        kwargs["exposed"] = True  # split_exposed's subsections: UV0/sky from the host (_expose)
        for pair in sorted(by_pair):
            secs = [s for s in sections if id(s) in by_pair[pair]]
            keep = (lambda pr: (lambda section, sub, k, tri: assign.get((id(sub), k)) == pr))(pair)
            subs = self.f2d.add_geometry(builder, secs, lod, self.place, wheel=False, keep=keep, **kwargs)
            pts = self._finish(builder, subs, pair, f)
            if record_box:
                self.boxes[pair] += pts
            if subs:
                out[pair] = subs
        self.report["seam_positions"] = len(self._seam_written)
        return out

    def _rigid(self, pair) -> bool:
        """A panel that only crushes (chassis, front structure, doors, wings, sills:
        hinge limit 0 or none), so its seams with others can be kept shut."""
        p = self.panels.get(pair)
        return p is not None and p.role in BODY

    def _seam_map(self, sections, lod: int, assign: dict) -> dict:
        """Quantised position (int16 x3, as encoded) -> the rigid panels whose
        triangles use it, for positions on more than one."""
        users = defaultdict(set)
        for section in sections:
            for sub in section.subsections:
                if sub.lod != lod:
                    continue
                pool = section.vertices_for(sub)
                for k, t in enumerate(sub.triangles()):
                    pair = assign.get((id(sub), k))
                    if pair is None or not self._rigid(pair):
                        continue
                    for i in t:
                        p = self.place.point(pool[i].position)
                        users[tuple(max(-32768, min(32767, round(c * 4096))) for c in p)].add(pair)
        # Near-coincident positions of different panels (separate Forza shells
        # that touch) are seams too: such positions are grouped, and every
        # member takes the group's panels and its first position's crush.
        near = SEAM_NEAR * 4096
        cell = defaultdict(list)
        for key in users:
            cell[tuple(int(c // near) for c in key)].append(key)
        parent = {}

        def find(k):
            while parent.get(k, k) != k:
                parent[k] = parent.get(parent[k], parent[k])
                k = parent[k]
            return k
        for key, pairs in users.items():
            ck = tuple(int(c // near) for c in key)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for other in cell.get((ck[0] + dx, ck[1] + dy, ck[2] + dz), ()):
                            if other != key and users[other] != pairs and math.dist(key, other) <= near:
                                a, b = find(key), find(other)
                                if a != b:
                                    parent[max(a, b)] = min(a, b)
        groups = defaultdict(set)
        for key in parent:
            groups[find(key)].add(key)
        self._seam_rep = {}
        for root, members in groups.items():
            pairs = set().union(*(users[k] for k in members))
            for k in members:
                users[k] = pairs
                self._seam_rep[k] = root
        return {k: tuple(sorted(v)) for k, v in users.items() if len(v) > 1}

    # Seams that stay shut whatever the damage amounts (testdrive7: the S5S
    # Raptor's door panel border ran through the rear quarter and the front
    # wing as a sawtooth of Forza triangle edges, and opened into jagged black
    # slits after a side hit). A seam vertex is drawn at p + f * d on each
    # side, f being that panel's damage amount; sharing d closes the seam only
    # when both panels get the same f, which fullVisualDamage does and a real
    # impact doesn't. Only d = 0 keeps it shut for any f. Stock cars do that:
    # the vertices where doors, wings, sills and the chassis meet barely move
    # (median 0-3 mm on 147, 163, 227), while their middles dent. So seams
    # between rigid panels whose host borders stay still are pinned (d = 0)
    # and the crush fades in over SEAM_REACH; borders the host moves (wings
    # against the front structure under the bonnet) keep the shared d.

    def _border_motion(self, f: HostField) -> dict:
        """{frozenset of the two roles: median crush (m) of the host's vertices
        where panels of those roles meet} over the host's rigid panels."""
        key = id(f)
        if key in self._motion:
            return self._motion[key]
        cells = defaultdict(list)
        rigid = [n for n, pr in enumerate(f.pairs) if self._rigid(pr)]
        for n in rigid:
            cells[tuple(int(math.floor(c / SEAM_BORDER)) for c in f.points[n])].append(n)
        moved = defaultdict(list)
        for n in rigid:
            ck = tuple(int(math.floor(c / SEAM_BORDER)) for c in f.points[n])
            best = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for m in cells.get((ck[0] + dx, ck[1] + dy, ck[2] + dz), ()):
                            if f.pairs[m] == f.pairs[n]:
                                continue
                            d = math.dist(f.points[m], f.points[n])
                            if d <= SEAM_BORDER and (best is None or d < best[0]):
                                best = (d, m)
            if best is not None:
                roles = frozenset((self.panels[f.pairs[n]].role, self.panels[f.pairs[best[1]]].role))
                moved[roles].append(math.sqrt(_dot(f.disp[n], f.disp[n])))
        out = {k: sorted(v)[len(v) // 2] for k, v in moved.items()}
        self._motion[key] = out
        self.report.setdefault("host_border_motion", {
            " | ".join(sorted(role_name(r) for r in k)): round(v, 3) for k, v in out.items()})
        return out

    def _pinned(self, a, b, motion: dict) -> bool:
        """Is the seam between rigid panels a and b pinned: the host's border
        of these two roles stays still, or the host has no such border."""
        m = motion.get(frozenset((self.panels[a].role, self.panels[b].role)))
        return m is None or m <= SEAM_STILL

    def _pin_map(self, f: HostField) -> dict:
        """Pair -> (cells of SEAM_REACH: placed points) of its pinned seam positions."""
        motion = self._border_motion(f)
        pins = defaultdict(lambda: defaultdict(list))
        count = 0
        for key, pairs in self._seam.items():
            if not any(self._pinned(a, b, motion) for a in pairs for b in pairs if a < b):
                continue
            p = tuple(c / 4096 for c in key)
            ck = tuple(int(math.floor(c / SEAM_REACH)) for c in p)
            for pair in pairs:
                pins[pair][ck].append(p)
            count += 1
        self.report.setdefault("pinned_seam_positions", []).append(count)
        return pins

    def _pin_weight(self, pair, p) -> float:
        """0 on a pinned seam of `pair`, rising smoothly to 1 at SEAM_REACH from it."""
        cells = self._pins.get(pair)
        if not cells:
            return 1.0
        ck = tuple(int(math.floor(c / SEAM_REACH)) for c in p)
        best = SEAM_REACH
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for q in cells.get((ck[0] + dx, ck[1] + dy, ck[2] + dz), ()):
                        d = math.dist(p, q)
                        if d < best:
                            best = d
        t = best / SEAM_REACH
        return t * t * (3 - 2 * t)

    def _disp(self, f: HostField, pair, p) -> tuple:
        """Crush displacement (s12 - position) at placed point p: the host
        panel's, inverse-distance weighted over its nearest vertices in
        normalised coordinates, scaled by the car size ratio."""
        cache = self.__dict__.setdefault("_disp_cache", {})
        key = (id(f), pair, p)
        if key not in cache:
            scale = f.box.scale_to(self.forza_box)
            grid = f.by_pair.get(pair) or f.all_grid
            hits = grid.nearest(self._forza_norm(p), self.opts.k)
            wsum, d = 0.0, [0.0, 0.0, 0.0]
            for dist, n in hits:
                w = 1.0 / (dist + 0.02) ** 2
                disp = f.disp[grid.values[n]]
                for a in range(3):
                    d[a] += w * disp[a]
                wsum += w
            cache[key] = tuple(d[a] / wsum * scale[a] for a in range(3)) if wsum else (0.0, 0.0, 0.0)
        return cache[key]

    def _exposed_box(self, tag: str, pair, p) -> Box | None:
        """Projection box of an exposed vertex: the bay's, or its panel's (bonnet,
        else boot lid, by the panel's role or the nearer one)."""
        kind = _kind_of(tag)
        if kind != "inner":
            return self.exposed_boxes.get(kind)
        role = self.panels[pair].role if pair in self.panels else None
        name = "trunk" if role in (5, 124) else "hood" if role == 4 else None
        boxes = {k: b for k, b in self.exposed_boxes.items() if k in ("hood", "trunk")}
        if name is None and boxes:
            name = min(boxes, key=lambda k: abs(boxes[k].centre[2] - p[2]))
        return boxes.get(name)

    def _expose(self, builder, sub, pair, f: HostField) -> None:
        """UV0 and baked sky for the vertices add_geometry marked with an
        EXPOSED_SENTINEL (split_exposed's inner faces and bay trims): a planar
        projection of the vertex in its box onto the tag's picture of the shared
        chassis texture (EXPOSED_MAP), and exposed_sky."""
        stride, buf = sub.stride, builder.buffers[sub.stride]
        tags = {struct.pack("<2e", *uv): tag for tag, uv in EXPOSED_SENTINEL.items()}
        first, count = sub.first_index, sub.index_count
        stats = self.report.setdefault("exposed", Counter())
        for v in sorted({sub.base_vertex + i for i in builder.indices[first:first + count]}):
            at = v * stride
            tag = tags.get(bytes(buf[at + 8:at + 12]))
            if tag is None:
                continue
            p = tuple(c / 4096 for c in struct.unpack_from("<3h", buf, at))
            box = self._exposed_box(tag, pair, p)
            rect, au, av = EXPOSED_MAP[tag]
            if box is None:
                uv = ((rect[0] + rect[1]) / 2, (rect[2] + rect[3]) / 2)
            else:
                t = [min(1.0, max(0.0, (p[a] - box.lo[a]) / box.size[a])) if box.size[a] > 1e-3 else 0.5
                     for a in (au, av)]
                uv = (rect[0] + t[0] * (rect[1] - rect[0]),
                      rect[3] - t[1] * (rect[3] - rect[2]) if av == 1 else rect[2] + t[1] * (rect[3] - rect[2]))
            struct.pack_into("<2e", buf, at + 8, *uv)
            sky = exposed_sky(tag, stored_normal(buf, at + LAYOUT[stride][1]))
            struct.pack_into("<h", buf, at + 6, round(sky * 4096))
            stats[_kind_of(tag) + " vertices"] += 1

    def _is_local(self, f: HostField, material: bytes) -> bool:
        """A material of the entry itself (Chassis/Lights) rather than a shared one."""
        if material in f.local:
            return True
        shared = set(self.f2d.SHARED.values()) | set(self.f2d.SHARED_EXTERIOR.values())
        if f.uv1 is not None:
            shared |= {t[3] for t in f.uv1.tris if not t[4]}
        return material not in shared

    def _uv1(self, f: HostField, p, pair, material: bytes, any_panel: bool = False, normal=None,
             chart: int | None = None):
        """(UV1, host chart) or None."""
        key = (id(f), pair, material, p, any_panel, normal, chart)
        cache = self.__dict__.setdefault("_uv1_cache", {})
        if key not in cache:
            cache[key] = f.uv1.lookup(self._forza_norm(p), pair, material, self._is_local(f, material), any_panel,
                                      normal, chart)
        return cache[key]

    def _finish(self, builder, subs, pair, f: HostField) -> list:
        """Write s9 bytes 0-1, s12, the s5 normal delta and (32-byte vertices) the
        decal-atlas UV1 of the vertices `subs` use."""
        points = []
        rigid = self._rigid(pair)
        for sub in subs:
            if sub.stride not in LAYOUT:
                continue
            self._expose(builder, sub, pair, f)
            o9, o5, o12 = LAYOUT[sub.stride]
            buf = builder.buffers[sub.stride]
            stride = sub.stride
            idx = builder.indices[sub.first_index:sub.first_index + sub.index_count]
            verts = sorted({sub.base_vertex + i for i in idx})
            pos, dmg, charts = {}, {}, {}
            for v in verts:
                at = v * stride
                raw = struct.unpack_from("<3h", buf, at)
                p = tuple(c / 4096 for c in raw)
                pos[v] = p
                # A position shared with other never-swinging panels (a seam inside
                # one Forza surface) gets the same crushed position on each, so the
                # seam stays shut when the panels crush alike.
                others = self._seam.get(raw) if rigid else None
                if others and pair in others:
                    at_p = tuple(c / 4096 for c in self._seam_rep.get(raw, raw))
                    ds = [self._disp(f, q, at_p) for q in others]
                    disp = tuple(sum(d[a] for d in ds) / len(ds) for a in range(3))
                    self._seam_written.add((id(f), raw))
                else:
                    disp = self._disp(f, pair, p)
                if rigid:
                    w = self._pin_weight(pair, p)
                    disp = tuple(w * c for c in disp)
                q = tuple(p[a] + disp[a] for a in range(3))
                dmg[v] = q
                buf[at + o9] = pair[0]
                buf[at + o9 + 1] = pair[1]
                struct.pack_into("<4e", buf, at + o12, q[0], q[1], q[2], 1.0)
                if stride == 32 and f.uv1 is not None:
                    # Lights-material triangles that aren't lamps (badges, plate,
                    # reflector trim; s9 byte 2 = 0) take the panel's paint layout:
                    # the lamp layout puts them on the broken-lamp tile, whose dark
                    # blobs strip the paint to white when damaged.
                    material = sub.material
                    if buf[at + o9 + 2] == 0 and self._is_local(f, material):
                        material = self.f2d.PAINT
                    hit = self._uv1(f, p, pair, material, normal=stored_normal(buf, at + o5))
                    if hit is not None:
                        struct.pack_into("<2e", buf, at + 12, *hit[0])  # semantic 8 components 2, 3
                        charts[v] = hit[1]
            points += pos.values()
            # Normal deltas: rotate the stored normal by the change of the
            # area-weighted face normal from the intact to the crushed mesh.
            n0 = defaultdict(lambda: [0.0, 0.0, 0.0])
            n1 = defaultdict(lambda: [0.0, 0.0, 0.0])
            for t in range(0, len(idx) - 2, 3):
                tri = [sub.base_vertex + idx[t + j] for j in range(3)]
                a = _cross(_sub(pos[tri[1]], pos[tri[0]]), _sub(pos[tri[2]], pos[tri[0]]))
                b = _cross(_sub(dmg[tri[1]], dmg[tri[0]]), _sub(dmg[tri[2]], dmg[tri[0]]))
                for v in tri:
                    for k in range(3):
                        n0[v][k] += a[k]
                        n1[v][k] += b[k]
            for v in verts:
                at = v * stride
                b0, b1 = buf[at + o5], buf[at + o5 + 1]
                delta = normal_delta(b0, b1, _norm(n0[v]), _norm(n1[v]))
                buf[at + o5 + 2], buf[at + o5 + 3] = delta
            if charts:
                self._split_seams(builder, sub, f, pair, pos, charts)
        return points

    def _split_seams(self, builder, sub, f: HostField, pair, pos, charts) -> None:
        """A triangle whose corners took UV1 from different host charts would
        stretch across the decal atlas (scratch rows between the tiles show as
        long streaks). Put the triangle on the chart most of its corners use:
        the other corners get a copy of their vertex with UV1 from that chart."""
        buf = builder.buffers[sub.stride]
        stride, o5 = sub.stride, LAYOUT[sub.stride][1]
        copies: dict[tuple, int | None] = {}
        for t in range(sub.first_index, sub.first_index + sub.index_count, 3):
            tri = [sub.base_vertex + builder.indices[t + j] for j in range(3)]
            cs = [charts.get(v) for v in tri]
            if None in cs or cs[0] == cs[1] == cs[2]:
                continue
            chart = Counter(cs).most_common(1)[0][0]
            for j, v in enumerate(tri):
                if cs[j] == chart:
                    continue
                key = (v, chart)
                if key not in copies:
                    copies[key] = None
                    material = sub.material
                    if buf[v * stride + LAYOUT[stride][0] + 2] == 0 and self._is_local(f, material):
                        material = self.f2d.PAINT  # as in _finish: non-lamp Lights triangles
                    hit = self._uv1(f, pos[v], pair, material, normal=stored_normal(buf, v * stride + o5),
                                    chart=chart)
                    new = len(buf) // stride
                    why = ("no hit" if hit is None else "other chart" if hit[1] != chart
                           else "16-bit range" if new - sub.base_vertex > 0xFFFF else None)
                    if why:
                        stats = self.report.setdefault("seam_skipped", {})
                        stats[why] = stats.get(why, 0) + 1
                    if why is None:
                        buf += buf[v * stride:(v + 1) * stride]
                        struct.pack_into("<2e", buf, new * stride + 12, *hit[0])
                        copies[key] = new
                        self.report["seam_vertices"] = self.report.get("seam_vertices", 0) + 1
                if copies[key] is not None:
                    builder.indices[t + j] = copies[key] - sub.base_vertex

    def _merge(self, builder, panel_subs: dict[tuple, list]) -> list:
        """Part-0 submodels: every panel's triangles again, one submodel per
        material (split when the vertex span passes 16 bits), on the same vertices."""
        by_material = defaultdict(list)
        for pair in sorted(panel_subs):
            for s in panel_subs[pair]:
                by_material[s.material].append(s)
        merged = []
        for material, subs in by_material.items():
            groups, current = [], []
            lo = hi = None
            for s in sorted(subs, key=lambda s: s.base_vertex):
                idx = builder.indices[s.first_index:s.first_index + s.index_count]
                s_lo, s_hi = s.base_vertex + min(idx), s.base_vertex + max(idx)
                if current and max(hi, s_hi) - min(lo, s_lo) > 0xFFFF:
                    groups.append((lo, current))
                    current, lo, hi = [], None, None
                current.append(s)
                lo = s_lo if lo is None else min(lo, s_lo)
                hi = s_hi if hi is None else max(hi, s_hi)
            if current:
                groups.append((lo, current))
            for base, group in groups:
                first = len(builder.indices)
                for s in group:
                    for i in builder.indices[s.first_index:s.first_index + s.index_count]:
                        builder.indices.append(s.base_vertex + i - base)
                new = self.f2d.NewSub(builder.new_uid(), material, group[0].stride, base, first,
                                      len(builder.indices) - first, group[0].flag)
                builder.subs.append(new)
                merged.append(new)
        return merged

    def full_lod(self, builder, part_lods: dict, mask: int, lod: int, **kwargs) -> None:
        """Full entry, one detail mask: per-panel MODL parts plus the merged part 0."""
        panel_subs = self._geometry(builder, self.car.body, lod, self.full_field, record_box=True, **kwargs)
        for pair, subs in panel_subs.items():
            part = self.panels[pair].part if pair in self.panels else None
            if part is not None and part != 0:
                part_lods.setdefault(part, {}).setdefault(mask, []).extend(subs)
        part_lods.setdefault(0, {})[mask] = self._merge(builder, panel_subs)
        self.report["panels"].setdefault(str(mask), {
            self.label(p): sum(s.index_count // 3 for s in subs) for p, subs in panel_subs.items()})

    def exterior(self, builder, lod: int, **kwargs) -> list:
        """Exterior entry: the merged body only (stock exteriors have no panel parts)."""
        panel_subs = self._geometry(builder, self.car.body, lod, self.ext_field, **kwargs)
        merged = self._merge(builder, panel_subs)
        drop = {id(s) for subs in panel_subs.values() for s in subs}
        compact(builder, [s for s in builder.subs if id(s) not in drop])
        return merged

    def interior_uv1(self, stream, interior) -> int:
        """Decal-atlas UV1 for the rebuilt interior entry's 32-byte vertices (its
        outer shell's paint: the bonnet seen from the cockpit), from the host
        interior's shell by nearest triangle of the same material class, any
        panel. Call after the interior is rebuilt. Returns vertices written."""
        if self.int_uv1 is None:
            return 0
        f = HostField([], [], [], self.int_uv1.box, uv1=self.int_uv1, local=self.int_local)
        drsp = child(child(interior, "MDXN"), "DRSP")
        buffers, index_buffers, users = {}, {}, defaultdict(dict)
        for e in drsp.children:
            if e.tag == "VXCK":
                hd = payload(stream, child(e, "VXHD"))
                stride = struct.unpack_from("<I", hd, 12)[0]
                elements = vm.parse_vdcl(payload(stream, child(e, "VDCL")))
                e8 = next((x for x in elements if x.semantic == 8), None)
                e5 = next((x for x in elements if x.semantic == 5), None)
                # Not the needle buffers: their semantic 8 is DashboardInstrument
                # (u, v, gauge id, 0), and a UV1 there overwrites the gauge id,
                # so the engine never turns the needles.
                if (e8 is not None and e8.components == 4 and e8.type == 8 and elements[0].semantic == 0
                        and e8.name_hash != DASHBOARD_INSTRUMENT):
                    # Offsets from the VDCL, not the converter's layout: the
                    # interior may keep host-shaped buffers (some lack semantic 9).
                    buffers[hd[:8]] = (child(e, "VXDT"), stride, e8.offset + 4, e5.offset if e5 else None)
            elif e.tag == "IXCK":
                index_buffers[payload(stream, child(e, "IXHD"))[:8]] = payload(stream, child(e, "IXDT"))
        for e in drsp.children:
            if e.tag != "SBMC":
                continue
            d = payload(stream, child(e, "SBBH"))
            if d[16:24] not in buffers:
                continue
            base, = struct.unpack_from("<I", d, 0x20)
            first, count = struct.unpack_from("<II", d, 0x38)
            ib = index_buffers[d[0x30:0x38]]
            for t in range(first, first + count):
                users[d[16:24]][base + struct.unpack_from("<H", ib, t * 2)[0]] = d[8:16]
        written = 0
        for uid, (vxdt, stride, o_uv1, o5) in buffers.items():
            buf = bytearray(payload(stream, vxdt))
            for v, material in users[uid].items():
                at = v * stride
                p = tuple(c / 4096 for c in struct.unpack_from("<3h", buf, at))
                normal = stored_normal(buf, at + o5) if o5 is not None else None
                hit = self._uv1(f, p, (0, 0), material, any_panel=True, normal=normal)
                if hit is not None:
                    struct.pack_into("<2e", buf, at + o_uv1, *hit[0])
                    written += 1
            vxdt.payload = bytes(buf)
        self.report["interior_uv1"] = written
        return written

    # -- hierarchy --
    def patch_upvh(self, data: bytes) -> tuple[bytes, dict]:
        """Move each panel's hinge pivot, rigid body (centre, inertia) and
        collision boxes from the host panel's box to the Forza panel's, by
        relative position in the box. With opts.hull, scale the root hull too."""
        h = upvh_mod.parse(data)
        out = bytearray(data)
        report = {}
        pdl = h.pdl_offset
        for pair, pts in sorted(self.boxes.items()):
            panel = self.panels.get(pair)
            hb, fb = self._host_box(pair), robust_box(pts)
            if panel is None or hb is None or fb is None:
                continue
            for r in panel.records:
                if r.role in NOT_MOVED or r.type != 1:
                    continue
                if r.role == 38 and not self.opts.hull:
                    continue  # the chassis body/box is the car's; only with the hull option
                entry = {"role": role_name(r.role)}
                if r.hinge >= 0:
                    at = h.offsets["hinges"] + r.hinge * 0x30
                    old = struct.unpack_from("<3f", out, at)
                    new = hb.to(fb, old)
                    if abs(old[0]) < 0.02:
                        new = (old[0],) + new[1:]  # keep centred hinges centred
                    if math.dist(old, new) > MAX_HINGE_MOVE:
                        entry["hinge_kept"] = round(math.dist(old, new), 3)  # implausible: keep the host's
                        new = old
                    struct.pack_into("<3f", out, at, *new)
                    entry["hinge"] = [round(c, 3) for c in new]
                    entry["hinge_moved"] = round(math.dist(old, new), 3)
                scale = tuple(min(COLLISION_SCALE[1], max(COLLISION_SCALE[0], c)) for c in hb.scale_to(fb))
                if r.body >= 0:
                    at = h.offsets["bodies"] + r.body * 0xB0
                    m = struct.unpack_from("<12f", out, at)
                    rows = (m[0:3], m[4:7], m[8:11])
                    pos = struct.unpack_from("<3f", out, at + 0x30)
                    centre = tuple(-_dot(rows[a], pos) for a in range(3))
                    new_c = hb.to(fb, centre)
                    if abs(centre[0]) < 0.02:
                        new_c = (centre[0],) + new_c[1:]
                    new_pos = tuple(-sum(rows[k][a] * new_c[k] for k in range(3)) for a in range(3))
                    struct.pack_into("<3f", out, at + 0x30, *new_pos)
                    inertia = struct.unpack_from("<3f", out, at + 0x40)
                    new_i = (inertia[0] * (scale[1] ** 2 + scale[2] ** 2) / 2,
                             inertia[1] * (scale[0] ** 2 + scale[2] ** 2) / 2,
                             inertia[2] * (scale[0] ** 2 + scale[1] ** 2) / 2)
                    struct.pack_into("<3f", out, at + 0x40, *new_i)
                    entry["centre"] = [round(c, 3) for c in new_c]
                if r.collision >= 0:
                    _scale_collision(out, pdl + 0x10 + r.collision, scale)
                    entry["collision_scale"] = [round(s, 3) for s in scale]
                report[f"record {r.index}"] = entry
        if self.opts.hull:
            root = h.records[0]
            if root.collision >= 0:
                host_box, forza_box = self.full_field.box, self.forza_box
                _map_collision(out, pdl + 0x10 + root.collision, host_box, forza_box)
                report["hull"] = {"host": [host_box.lo, host_box.hi], "forza": [forza_box.lo, forza_box.hi]}
        self.report["upvh"] = report
        return bytes(out), report


def robust_box(points, trim: float = 0.02) -> Box | None:
    """Box over the 2nd to 98th percentile per axis (a few stray triangles
    assigned to a panel don't stretch it)."""
    points = list(points)
    if not points:
        return None
    lo, hi = [], []
    for a in range(3):
        v = sorted(p[a] for p in points)
        k = int(len(v) * trim)
        lo.append(v[k])
        hi.append(v[len(v) - 1 - k])
    return Box(tuple(lo), tuple(hi))


def _scale_collision(data: bytearray, at: int, scale) -> None:
    """Scale every primitive of one PDL collision model (in its body's frame):
    translation and extents per axis, radius by the largest factor."""
    size, nbytes, radius, n = struct.unpack_from("<IIfI", data, at)
    struct.pack_into("<f", data, at + 8, radius * max(scale))
    for k in range(n):
        p = at + 0x10 + k * 0x60
        t = struct.unpack_from("<3f", data, p + 0x30)
        struct.pack_into("<3f", data, p + 0x30, *(t[a] * scale[a] for a in range(3)))
        e = struct.unpack_from("<3f", data, p + 0x50)
        struct.pack_into("<3f", data, p + 0x50, *(e[a] * scale[a] for a in range(3)))


def _map_collision(data: bytearray, at: int, host: Box, forza: Box) -> None:
    """Root hull: primitive centres mapped host box -> Forza box, extents scaled."""
    scale = host.scale_to(forza)
    size, nbytes, radius, n = struct.unpack_from("<IIfI", data, at)
    struct.pack_into("<f", data, at + 8, radius * max(scale))
    for k in range(n):
        p = at + 0x10 + k * 0x60
        t = struct.unpack_from("<3f", data, p + 0x30)
        struct.pack_into("<3f", data, p + 0x30, *host.to(forza, t))
        e = struct.unpack_from("<3f", data, p + 0x50)
        struct.pack_into("<3f", data, p + 0x50, *(e[a] * scale[a] for a in range(3)))


def compact(builder, keep: list) -> None:
    """Keep only `keep` submodels in a forza2dsf.Builder and rebuild its index list."""
    indices = []
    for s in keep:
        seg = builder.indices[s.first_index:s.first_index + s.index_count]
        s.first_index = len(indices)
        indices.extend(seg)
    builder.indices = indices
    builder.subs = list(keep)


# --- normals ----------------------------------------------------------------------

def _angles_of(b0: int, b1: int) -> tuple[float, float]:
    return b0 / 255 * 2 * math.pi - math.pi, b1 / 255 * 2 * math.pi - math.pi


def _normal_of(a0: float, a1: float):
    return (math.sin(a0) * math.sin(a1), math.cos(a0), math.sin(a0) * math.cos(a1))


def _rotate(v, a, b):
    """Rotate v by the rotation that takes unit a onto unit b (Rodrigues)."""
    axis = _cross(a, b)
    s = math.sqrt(_dot(axis, axis))
    c = _dot(a, b)
    if s < 1e-9:
        return v
    k = (axis[0] / s, axis[1] / s, axis[2] / s)
    kv = _cross(k, v)
    kd = _dot(k, v)
    return tuple(v[i] * c + kv[i] * s + k[i] * kd * (1 - c) for i in range(3))


def normal_delta(b0: int, b1: int, face0, face1) -> tuple[int, int]:
    """s5 bytes 2, 3: the polar/azimuth change that turns the stored normal
    (bytes 0, 1) the way the face normals turn from intact to crushed. The
    shader adds (byte / 255 * 2pi - pi) x fDamage to each angle; 127/128 is no change."""
    if face0 is None or face1 is None:
        return 128, 128
    a0, a1 = _angles_of(b0, b1)
    n = _rotate(_normal_of(a0, a1), face0, face1)
    polar = math.acos(max(-1.0, min(1.0, n[1])))
    azimuth = math.atan2(n[0], n[2])
    out = []
    for d in (polar - a0, azimuth - a1):
        d = (d + math.pi) % (2 * math.pi) - math.pi
        out.append(max(0, min(255, round((d + math.pi) / (2 * math.pi) * 255))))
    return out[0], out[1]


# --- CLI: inspection and checks ---------------------------------------------------

def _entries(archive: Path, spool_id: int):
    stream = archive.open("rb")
    root = read_chunk(stream, 0)
    return stream, root, upvh_mod.entries_for(stream, root.entries, spool_id, None)


def cmd_panels(args) -> None:
    stream, root, kinds = _entries(args.archive, args.id)
    with stream:
        full = kinds[0]
        nonr = child(full, "NONR")
        data = payload(stream, child(nonr, "UPVH"))
        h = upvh_mod.parse(data)
        panels = host_panels(data, payload(stream, child(nonr, "MODL")))
        f = host_field(stream, full, 4)
    counts = Counter(f.pairs)
    print(f"{full.name}: {len(panels)} panels; part 0 mask 4: {len(f.points)} vertices, box {f.box}")
    for pair, p in sorted(panels.items()):
        r = p.record
        hinge = ""
        if r.hinge >= 0:
            piv = struct.unpack_from("<3f", h.table("hinges")[r.hinge])
            axis = struct.unpack_from("<3f", h.table("hinges")[r.hinge], 16)
            limit, param = struct.unpack_from("<2f", h.table("hinges")[r.hinge], 32)
            hinge = (f"hinge {r.hinge} pivot ({piv[0]:.2f}, {piv[1]:.2f}, {piv[2]:.2f}) axis "
                     f"({axis[0]:.0f}, {axis[1]:.0f}, {axis[2]:.0f}) limit {limit:.3f} param {param:g}")
        ns = [n for n, q in enumerate(f.pairs) if q == pair]
        crush = max((math.dist((0, 0, 0), f.disp[n]) for n in ns), default=0)
        print(f"  s9 {pair[0]:2d},{pair[1]:2d}  rec {','.join(str(x.index) for x in p.records):6s} "
              f"{role_name(p.role):18s} part {p.part if p.part is not None else '-':>2}  verts {counts[pair]:5d} "
              f"max crush {crush:.3f}  {hinge}")


def _load_mesh(stream, vehc) -> vm.Mesh:
    mesh = vm.Mesh()
    for e in child(child(vehc, "MDXN"), "DRSP").children:
        if e.tag == "VXCK":
            hd = payload(stream, child(e, "VXHD"))
            mesh.vertex_buffers[hd[:8]] = vm.VertexBuffer(hd[:8], *struct.unpack_from("<II", hd, 8),
                                                          vm.parse_vdcl(payload(stream, child(e, "VDCL"))),
                                                          payload(stream, child(e, "VXDT")))
        elif e.tag == "IXCK":
            mesh.index_buffers[payload(stream, child(e, "IXHD"))[:8]] = payload(stream, child(e, "IXDT"))
        elif e.tag == "SBMC":
            d = payload(stream, child(e, "SBBH"))
            mesh.submodels.append(vm.SubModel(d[0:8], d[8:16], d[16:24], struct.unpack_from("<I", d, 0x20)[0],
                                              d[0x30:0x38], *struct.unpack_from("<II", d, 0x38)))
    mesh.parts = vm.parse_modl(payload(stream, child(child(vehc, "NONR"), "MODL")), mesh.submodels)
    return mesh


def _colour(k: int):
    r, g, b = colorsys.hsv_to_rgb((k * 0.61803) % 1.0, 0.65, 1.0)
    return r, g, b


def render(tris, path: Path, size: int = 300) -> None:
    """Side (both), top, front and rear views; tris: [(3 points, rgb 0..1)] or
    [(3 points, None, 3 uvs, texel function uv -> rgb 0..1)] for per-pixel texturing."""
    xs = [p[0] for t in tris for p in t[0]]
    ys = [p[1] for t in tris for p in t[0]]
    zs = [p[2] for t in tris for p in t[0]]
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) * 1.05
    centre = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2)
    # (horizontal axis, flip, vertical axis, depth axis, depth sign): -x side, +x side, top, front, rear
    views = [(2, 1, 1, 0, 1), (2, -1, 1, 0, -1), (0, 1, 2, 1, -1), (0, -1, 1, 2, -1), (0, 1, 1, 2, 1)]
    width = size * len(views)
    pixels = bytearray([24, 24, 28] * width * size)
    light = (0.4, 0.8, 0.45)
    for view, (h, flip, v, d, sign) in enumerate(views):
        depth = [math.inf] * (size * size)
        for item in tris:
            tri, col = item[0], item[1]
            n = _cross(_sub(tri[1], tri[0]), _sub(tri[2], tri[0]))
            length = math.sqrt(_dot(n, n)) or 1
            shade = 0.3 + 0.7 * abs(_dot(n, light)) / length
            c = bytes(int(255 * shade * k) for k in col) if col is not None else None
            pts = [(flip * (p[h] - centre[h]) / span * size + size / 2,
                    size / 2 - (p[v] - centre[v]) / span * size, sign * (p[d] - centre[d])) for p in tri]
            x0, x1 = max(int(min(p[0] for p in pts)), 0), min(int(max(p[0] for p in pts)) + 1, size)
            y0, y1 = max(int(min(p[1] for p in pts)), 0), min(int(max(p[1] for p in pts)) + 1, size)
            (px, py, pz), (qx, qy, qz), (rx, ry, rz) = pts
            area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
            if abs(area) < 1e-9:
                continue
            for y in range(y0, y1):
                for x in range(x0, x1):
                    w0 = ((qx - x) * (ry - y) - (qy - y) * (rx - x)) / area
                    w1 = ((rx - x) * (py - y) - (ry - y) * (px - x)) / area
                    w2 = 1 - w0 - w1
                    if w0 < 0 or w1 < 0 or w2 < 0:
                        continue
                    z = w0 * pz + w1 * qz + w2 * rz
                    k = y * size + x
                    if z < depth[k]:
                        depth[k] = z
                        o = (y * width + view * size + x) * 3
                        if c is None:
                            uvs, texel = item[2], item[3]
                            u = w0 * uvs[0][0] + w1 * uvs[1][0] + w2 * uvs[2][0]
                            t = w0 * uvs[0][1] + w1 * uvs[1][1] + w2 * uvs[2][1]
                            pixels[o:o + 3] = bytes(min(255, int(255 * (0.4 + 0.6 * shade) * k)) for k in texel(u, t))
                        else:
                            pixels[o:o + 3] = c
    vm.write_png(path, width, size, pixels)


DECAL_TEXTURE = bytes.fromhex("00122b6db3fa1227")  # SSIC: dent normals (RGB) + scratch mask (alpha)


def decal_texel(stream_path: Path):
    """uv -> rgb of a preview of the shared damage decal: grey paint, white where
    the scratch mask (alpha) is dark, tinted by the dent normal map."""
    import dsf_materials
    from chnk import walk
    with stream_path.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = next(e for e in root.entries if e.tag == "SSIC")
        dds = next(payload(stream, child(e, "DXTC")) for _, e in walk(ssic.children)
                   if e.tag == "TEXC" and payload(stream, child(e, "UIDC"))[:8] == DECAL_TEXTURE)
    w, h, rgba = dsf_materials.decode_dds(dds)

    def texel(u, v):
        x = min(w - 1, max(0, int(u * w)))
        y = min(h - 1, max(0, int(v * h)))
        r, g, b, a = rgba[(y * w + x) * 4:(y * w + x) * 4 + 4]
        scratch = 1 - a / 255
        base = (0.45 + 0.25 * (r / 255 - 0.5), 0.45 + 0.25 * (g / 255 - 0.5), 0.5)
        return tuple(base[k] * (1 - scratch) + scratch for k in range(3))
    return texel


TRIM_TEXTURE = bytes.fromhex("00122b73d1fa1227")  # SSIC: GLOBAL_Chassis(_Simple) diffuse
TRIM_MATERIALS = {bytes.fromhex("0012833b12fe1027"), bytes.fromhex("10122b9753b14e2a")}
CLASS_COLOURS = {bytes.fromhex("001283358afd1027"): (0.75, 0.35, 0.1), bytes.fromhex("10122b9653b14e2a"): (0.75, 0.35, 0.1),
                 bytes.fromhex("001283369afd1027"): (0.3, 0.5, 0.7), bytes.fromhex("10122b9953b14e2a"): (0.3, 0.5, 0.7)}


def shared_texel(stream_path: Path, uid: bytes):
    """uv -> grey (its red channel) of a shared SSIC texture, for --by trim."""
    import dsf_materials
    from chnk import walk
    with stream_path.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = next(e for e in root.entries if e.tag == "SSIC")
        dds = next(payload(stream, child(e, "DXTC")) for _, e in walk(ssic.children)
                   if e.tag == "TEXC" and payload(stream, child(e, "UIDC"))[:8] == uid)
    w, h, rgba = dsf_materials.decode_dds(dds)

    def texel(u, v):
        x = min(w - 1, max(0, int(u % 1 * w)))
        y = min(h - 1, max(0, int(v % 1 * h)))
        r = rgba[(y * w + x) * 4] / 255
        return (r, r, r)
    return texel


def cmd_render(args) -> None:
    """Colour by MODL part or by s9 panel; --damage t draws lerp(position, s12, t)
    for every panel, as the shader does with fDamage = t."""
    stream, root, kinds = _entries(args.archive, args.id)
    with stream:
        vehc = kinds[args.kind]
        mesh = _load_mesh(stream, vehc)
    mask = args.mask or {0: 4, 1: 64, 2: 2}[args.kind]
    only = {int(x) for x in args.parts.split(",")} if args.parts else None
    skip = {int(x) for x in args.skip.split(",")} if args.skip else set()
    tris = []
    texel = decal_texel(stream_path=args.archive) if args.by == "decal" else None
    trim_texel = shared_texel(args.archive, TRIM_TEXTURE) if args.by == "trim" else None
    for p, part in enumerate(mesh.parts):
        if (only is not None and p not in only) or p in skip:
            continue
        for lod in part.lods:
            if lod.mask != mask:
                continue
            for n in lod.submodels:
                for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                    e12, e9 = vb.element(12), vb.element(9)
                    pts = []
                    for i in tri:
                        q = vm.position(vb, i)
                        if e12 is not None and args.damage:
                            d = vb.read(e12, i)
                            q = tuple(q[a] + args.damage * (d[a] - q[a]) for a in range(3))
                        pts.append(q)
                    if trim_texel is not None:
                        m = mesh.submodels[n].material
                        if m in TRIM_MATERIALS:
                            e8 = vb.element(8)
                            tris.append((pts, None, [vb.read(e8, i)[:2] for i in tri], trim_texel))
                        else:
                            tris.append((pts, CLASS_COLOURS.get(m, (0.55, 0.5, 0.45))))
                        continue
                    if texel is not None:
                        e8 = vb.element(8)
                        if e8 is not None and e8.components == 4:
                            tris.append((pts, None, [vb.read(e8, i)[2:4] for i in tri], texel))
                        else:
                            tris.append((pts, (0.35, 0.35, 0.4)))
                        continue
                    key = p if args.by == "part" else s9_pair(vb, tri[0])[1] * 7 + s9_pair(vb, tri[0])[0]
                    tris.append((pts, _colour(key)))
    if args.wheels:
        # The wheel model (20-byte buffers, centred on the origin) at every UPVH hub,
        # to see the body against its wheels.
        with args.archive.open("rb") as stream2:
            root2 = read_chunk(stream2, 0)
            kinds2 = upvh_mod.entries_for(stream2, root2.entries, args.id, None)
            h = upvh_mod.parse(payload(stream2, child(child(kinds2[0], "NONR"), "UPVH")))
        hubs = [v[:3] for v in h.vec4s("wheels")]
        wheel = []
        for part in mesh.parts:
            for lod in part.lods:
                if lod.mask != mask:
                    continue
                for n in lod.submodels:
                    for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                        if vb.stride == 20:
                            wheel.append([vm.position(vb, i) for i in tri])
        for hub in hubs:
            for t in wheel:
                tris.append(([tuple(p[a] + hub[a] for a in range(3)) for p in t], (0.25, 0.25, 0.25)))
    if not tris:
        raise SystemExit("nothing to render")
    render(tris, args.png, args.size)
    print(f"wrote {args.png} ({len(tris)} triangles)")


def cmd_check(args) -> None:
    """Structural damage checks for one vehicle's full and exterior entries."""
    stream, root, kinds = _entries(args.archive, args.id)
    problems = 0

    def report(ok: bool, text: str) -> None:
        nonlocal problems
        problems += not ok
        print(("ok    " if ok else "FAIL  ") + text)

    with stream:
        full = kinds[0]
        nonr = child(full, "NONR")
        panels = host_panels(payload(stream, child(nonr, "UPVH")), payload(stream, child(nonr, "MODL")))
        meshes = {k: _load_mesh(stream, kinds[k]) for k in (0, 2) if k in kinds}
    known = set(panels)
    for kind, mesh in meshes.items():
        mask = 4 if kind == 0 else 2
        part0 = set()
        pairs = Counter()
        moved = total = 0
        for lod in mesh.parts[0].lods:
            if lod.mask != mask:
                continue
            for n in lod.submodels:
                for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                    e12, e9 = vb.element(12), vb.element(9)
                    part0.add((vb.uid,) + tuple(sorted(tri)))
                    for i in tri:
                        pairs[s9_pair(vb, i)] += 1
                        if e12 is not None:
                            total += 1
                            moved += math.dist(vm.position(vb, i), vb.read(e12, i)[:3]) > 0.005
        unknown = {p: c for p, c in pairs.items() if p not in known}
        report(not unknown, f"kind {kind}: every part-0 s9 pair is a host panel ({len(pairs)} pairs, unknown {unknown})")
        report(len(pairs) > 3, f"kind {kind}: part 0 uses {len(pairs)} panels")
        report(total and moved / total > 0.2, f"kind {kind}: {moved} of {total} part-0 vertex uses have a crush target")
        if kind != 0:
            continue
        for pair, panel in sorted(panels.items()):
            if panel.part in (None, 0):
                continue
            tris = set()
            bad = 0
            for lod in mesh.parts[panel.part].lods:
                if lod.mask != mask:
                    continue
                for n in lod.submodels:
                    for vb, tri in vm.triangles(mesh, mesh.submodels[n]):
                        tris.add((vb.uid,) + tuple(sorted(tri)))
                        bad += any(s9_pair(vb, i) != pair for i in tri)
            if tris:
                report(not bad and tris <= part0,
                       f"part {panel.part} ({role_name(panel.role)}): {len(tris)} triangles, all on panel {pair} "
                       f"({bad} not) and all in part 0 ({len(tris - part0)} not)")
    print(f"{problems} problem(s)")
    if problems:
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("panels", help="the host's panel records, pairs, parts, hinges and crush")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    p = sub.add_parser("render", help="render coloured by MODL part or s9 panel, optionally crushed")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--png", type=Path, required=True)
    p.add_argument("--kind", type=int, default=0, choices=(0, 1, 2))
    p.add_argument("--mask", type=int)
    p.add_argument("--by", choices=("part", "panel", "decal", "trim"), default="part",
                   help="decal: the shared scratch/dent decal at UV1 (white = scratch where fully damaged); "
                        "trim: shared trim textured from the global chassis texture at UV0, paint orange, glass blue")
    p.add_argument("--damage", type=float, default=0.0, help="fDamage for every panel (0 intact, 1 crushed)")
    p.add_argument("--parts", help="only these MODL parts")
    p.add_argument("--skip", help="leave these MODL parts out")
    p.add_argument("--size", type=int, default=300)
    p.add_argument("--wheels", action="store_true", help="also draw the wheel model at every UPVH hub")
    p = sub.add_parser("check", help="structural damage checks of a (converted) vehicle")
    p.add_argument("archive", type=Path)
    p.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)
    _set_idle_priority()
    {"panels": cmd_panels, "render": cmd_render, "check": cmd_check}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
