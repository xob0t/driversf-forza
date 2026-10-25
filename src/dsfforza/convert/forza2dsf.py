"""Put a Forza Horizon 1 car's geometry into a DSF vehicle slot (version 1).

Takes an archive that already holds a host vehicle at --id (made with
src/dsfforza/build/clone_vehicle.py; the host's hierarchy, handling and sound stay) and
replaces the geometry of its full (N) and clean exterior (N+2000) entries with
the Forza car's main carbin. The interior (N+1000) is left alone unless
--cockpit: then it is rebuilt from <car>_cockpit.carbin, and the steering
hinge, gauges and head camera move to the Forza cockpit (src/dsfforza/convert/cockpit.py,
research/cockpit.md).

Version 1 limits: no textures unless --textures (then lamps, badges and
interior sample the car's converted atlas through the host's Chassis/Lights
materials, see src/dsfforza/convert/dsf_materials.py; everything else uses one of DSF's
shared materials by role), no separate damage parts (all body geometry goes into
MODL part 0; other parts are emptied), wheel blur dropped. Wheels are moved
by rewriting the hub positions in the host's UPVH.

    uv run dsff forza2dsf out/dngvehicles.clone.sp out/forza.sp --id 304 \\
        --car out/fh1/NIS_370Z_10 --name NIS_370Z_10

FM4 car folders work the same way (lower-case file names are matched
case-insensitively). Wheel placement comes from Physics/MAXData.xml, or with
--gamedb from gamedb.slt Data_CarBody (car_dims).

As a library: load_car(folder, name), car_dims(car), then
convert(stream, full_vehc, exterior_vehc, car, dims, ConvertOptions(...))
edits the two VEHC entries in memory (src/dsfforza/build/port_car.py uses this).

See research/vehicle-model.md (DSF side) and src/dsfforza/formats/carbin.py (Forza side).
"""

from __future__ import annotations

import argparse
import copy
import math
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza.formats import carbin
from dsfforza.formats.chnk import Entry, entry_size, read_chunk, write_chunk
from dsfforza.formats.vehicle_mesh import child, payload
from dsfforza.formats.vehicles import find, parse_sslp

# --- DSF shared materials, identified by rendering the Desanne's submodels per material.
PAINT = bytes.fromhex("001283358afd1027")
GLASS = bytes.fromhex("001283369afd1027")
TRIM = bytes.fromhex("0012833b12fe1027")
TYRE = bytes.fromhex("00128337b2fd1027")
# Their *_LOD1 twins (GLOBAL_Chassis_LOD1, _Glass_LOD1, _Chassis_Simple_LOD1, _Tyre_LOD1):
# what stock clean exterior entries use (paint and trim in 149 of 150, tyres in 150,
# glass in 145; the full-entry materials in at most 8, for a few hundred triangles).
# Converted exteriors used the full-entry ones, and the player's car, which the
# chase camera draws from that entry, came out wrong where the full-entry shaders
# read per-car constants: the DMC-12's stainless entry (copied from stock 171, same
# shader maths) drew near black (research/lights.md, "The DMC-12 came out black").
PAINT_LOD1 = bytes.fromhex("10122b9653b14e2a")
GLASS_LOD1 = bytes.fromhex("10122b9953b14e2a")
TRIM_LOD1 = bytes.fromhex("10122b9753b14e2a")
TYRE_LOD1 = bytes.fromhex("10122b9b53b14e2a")
# Chrome (Forza's chrome techniques: bumpers, window frames, drip rails). Stock
# full entries draw their chrome with GLOBAL_LOD_Chrome (96 of 150 vehicles,
# 374k triangles; GLOBAL_Chassis_Chrome in 9) and clean exteriors with
# GLOBAL_Chassis_Chrome (148 of 150, 322k triangles; GLOBAL_LOD_Chrome in 8),
# both stride 32, MODL flag 5, sampling the shared chassis texture (reflection
# amount 1, specular power 150; the trim material has specular intensity 0.01).
CHROME = bytes.fromhex("a012f01306d57a2a")  # GLOBAL_LOD_Chrome
CHROME_LOD1 = bytes.fromhex("00128338d7fd1027")  # GLOBAL_Chassis_Chrome
# Vertex stride each material was used with in the Desanne, and MODL render flag
# (the _LOD1 twins: the same in stock exteriors, e.g. 147, 171, 227). Read only:
# each conversion adds its host's own materials to a copy (Builder.layout).
MATERIAL_LAYOUT = {
    PAINT: (32, 5),
    GLASS: (28, 7),
    TRIM: (28, 5),
    TYRE: (20, 5),
    CHROME: (32, 5),
    PAINT_LOD1: (32, 5),
    GLASS_LOD1: (28, 7),
    TRIM_LOD1: (28, 5),
    TYRE_LOD1: (20, 5),
    CHROME_LOD1: (32, 5),
}

DEFAULT_LODS = "4:1,8:3,16:5"  # DSF MODL mask -> Forza LOD
SHADOW_MASK, DEFAULT_SHADOW_LOD = 1, 5
EXTERIOR_MASK = 2
POSITION_SCALE = 4096
# Largest MDXN among stock VEHC entries per kind. Spooling a bigger one crashed
# the game (run 20261021-233936, MDXN 0x21D800), so the converter refuses.
MDXN_LIMIT = {0: 0x138800, 1: 0x12D000, 2: 0xDD800}
NONR_LIMIT = {0: 0x6D000, 1: 0x1000, 2: 0x1000}  # SSLP per-kind NONR buffers (spool-limits.md)

# DSF hub height in the Desanne hierarchy (front z > 0). Only src/dsfforza/convert/exp_hollow.py uses it now;
# the converter reads each host's hubs from its wheel table (host_wheels).
HOST_HUB_Y = -0.398
# The paint shader overlays the vehicle's chassis texture through UV0. Until
# Forza textures are converted, point every paint vertex at a plain texel the
# Desanne's large paint panels use.
PAINT_UV = (0.835, 0.703)
# Shared trim (GLOBAL_Chassis_Simple) UV: the uniform dark block of the global
# chassis texture (SSIC 00122b73d1fa1227, texels ~(944-1023, 236-268), RG ~24)
# that stock trims drawn from a single texel use most (46 hosts: 39% of that area).
TRIM_UV = (0.969, 0.496)
# Shared chrome UV0: the middle of the block of that texture stock chrome samples
# most (texels 972-980 x 404-424 on both chrome materials, 150 vehicles; red,
# the grey level, ~98). One texel: Forza's chrome UVs land on its own atlas.
CHROME_UV = (0.9536, 0.8135)
# Licence plates (src/dsfforza/convert/plates.py): stock plates are paint-material quads whose
# UV0 covers the plate in the same shared atlas; the synthetic plate sections
# carry those UVs and keep them (add_geometry).
PLATE_TECHNIQUE = "dsf_plate"
# Exposed inner geometry (src/dsfforza/convert/damage.py split_exposed): bonnet/boot undersides
# (technique INNER_TECHNIQUE, paint) and trims under the bonnet. Their
# subsections carry `exposed_uv`, a sentinel UV0 that add_geometry(exposed=True)
# writes and DamagePlan replaces with a projection onto the shared chassis
# texture and a baked sky.
INNER_TECHNIQUE = "dsf_inner"


def material_for(name: str) -> bytes | None:
    n = name.lower()
    if "blur" in n:  # blur_rim, blur_lip, chrome_blur_lip: Forza's spinning-wheel discs
        return None
    if n.startswith("body") or n in (PLATE_TECHNIQUE, INNER_TECHNIQUE):
        return PAINT
    if "glass" in n or "window" in n:
        return GLASS
    if n in ("tire", "rim", "inner_rim", "tyre"):
        return TYRE
    if n.startswith("chrome") or n == "lights_chrome":
        return CHROME
    return TRIM


def stock_sections(sections: list[carbin.Section]) -> list[carbin.Section]:
    """Drop upgrade variants: for parts named <base><letter>, keep variant 'a';
    drop <base>race and parts that only exist as non-'a' variants."""
    names = {s.name for s in sections}
    keep = []
    for s in sections:
        name = s.name
        if name.endswith("race") and name != "race":
            continue
        m = re.fullmatch(r"(.*?)([a-z])", name)  # FM4 goes past d: Skyline R32 bumperFa..Fg, hooda/hoodg
        if m and m.group(1) and (m.group(1) + "a" in names or m.group(1) + "b" in names) and m.group(2) != "a":
            continue
        keep.append(s)
    return keep


# --- vertex encoding ------------------------------------------------------------


def spherical(n: tuple[float, float, float]) -> tuple[int, int]:
    nx, ny, nz = n
    length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    nx, ny, nz = nx / length, ny / length, nz / length
    polar = math.acos(max(-1.0, min(1.0, ny)))
    azimuth = math.atan2(nx, nz)
    b0 = max(0, min(255, round(128 + polar / math.pi * 127)))
    b1 = round(128 + azimuth / math.pi * 128) & 0xFF
    return b0, b1


RIGID = bytes((0, 0, 0, 1))  # vertex semantic 9: follow the chassis


def encode_vertex(stride: int, pos, uv0, uv1, normal, s9: bytes = RIGID, sky: float = 0.0) -> bytes:
    """One vertex in the host's layouts. s9 (VDCL semantic 9, 4 x u8) binds the
    vertex to a hierarchy node: (record +0x34 slot, record +0x2A model, light, 1);
    stock steering-wheel vertices carry the wheel record's pair, e.g. (1, 1, 0, 1)
    on the Desanne (research/cockpit.md). (0, 0, 0, 1) is the chassis.

    sky (0..1) goes into the fourth position component (x 4096): a baked
    ambient/sky-light term (vertex_sky). 0 renders the vertex nearly black
    except for specular (research/vehicle-model.md)."""
    q = [max(-32768, min(32767, round(c * POSITION_SCALE))) for c in pos]
    p0, p1 = spherical(normal)
    head = struct.pack("<4h", q[0], q[1], q[2], max(0, min(4096, round(sky * 4096))))
    s5 = bytes((p0, p1, 128, 128))
    if stride == 32:
        return head + struct.pack("<4e", uv0[0], uv0[1], uv1[0], uv1[1]) + s9 + s5 + struct.pack("<4e", *pos, 1.0)
    if stride == 28:
        return head + struct.pack("<2e", uv0[0], uv0[1]) + s9 + s5 + struct.pack("<4e", *pos, 1.0)
    if stride == 20:
        return head + struct.pack("<2e", uv0[0], uv0[1]) + s9 + s5
    raise ValueError(stride)


# --- baked lighting and glass UVs -------------------------------------------------
#
# Fourth position component (s0.w, int16 / 4096): every stock vertex carries a
# baked sky-light term. On the Desanne it tracks the normal's up component
# (exterior: mean 0.67 facing up, 0.38 sideways, 0.04 facing down; the
# interior is darker, median 0.21) and drops in occluded places. Converted
# vertices had 0 there, which drew bodies black and cockpits nearly black
# (needles, with 4096, were the only lit parts). Forza bakes per-vertex
# ambient occlusion into the first trailing vertex byte (0..~107, FH1 and
# FM4). We write AO x a hemisphere factor.
FORZA_AO_FULL = 105  # Forza AO byte for a fully open vertex
DEFAULT_AO = 0.8  # sections whose AO byte is garbage (FM4 glass: constant 0 or 255)
SKY_FLOOR, SKY_RANGE = 0.25, 0.55
_ao_valid: dict[int, bool] = {}


def _pool_has_ao(pool) -> bool:
    key = id(pool)
    if key not in _ao_valid:
        values = {v.extra[0] for v in pool if v.extra}
        _ao_valid[key] = bool(values) and max(values) <= FORZA_AO_FULL + 10 and len(values) >= 3
    return _ao_valid[key]


def vertex_sky(vertex, pool, normal_dsf) -> float:
    """s0.w for a converted vertex: Forza AO x (SKY_FLOOR + SKY_RANGE x (1 + ny) / 2)."""
    ao = min(1.0, vertex.extra[0] / FORZA_AO_FULL) if _pool_has_ao(pool) else DEFAULT_AO
    length = math.sqrt(sum(c * c for c in normal_dsf)) or 1.0
    up = (1 + normal_dsf[1] / length) / 2
    return ao * (SKY_FLOOR + SKY_RANGE * up)


# Glass UVs. Every stock glass submodel uses one of the shared glass materials
# GLOBAL_Glass / _LOD1 / _LOD5 (defined in SSIC, the archive's shared chunk),
# which all sample texture 00122b77dffa1227 (SSIC, DXT1 512 x 1024). Its four
# v bands are damage states: 0..0.25 clean, then light cracks, heavy cracks,
# shattered: the damage-aware glass techniques add 0.25 * floor(
# g_v4PerPanelData[s9 byte 1].y) to v (confirmed-static, src/dsfforza/convert/glass.py).
# Within the clean band:
#   u 0.12..1:       exterior glass, flat except the rear-window defroster
#                    lines (u 0.17..0.62, v 0.13..0.23);
#   u 0..0.12:       the strip GLOBAL_Glass_LOD5 (interior glass) samples:
#                    a grey smear texture at v 0..0.06, flat black at
#                    v 0.06..0.125 and at u 0.06..0.12, v 0.125..0.25, a dark
#                    red block at u 0..0.06, v 0.125..0.25.
# Stock interior windows use that strip in three layers (src/dsfforza/convert/glass.py,
# "interior glass"): a smear surface (states 0-2; the windscreen only along
# its top and pillars), crack-line slivers on the red block (states 1-2) and
# rim shards on the black blocks (state 3). The pixel shader discards texels
# with blue < 0.01, so each layer shows only in its states. Converted cockpits
# first spread Forza's UVs over the smear (broad white streaks, M5,
# img/fix3-m5-cockpit.png), then sat in the black block (no glass in states
# 0-2, stretched shard streaks in 3), then in a 10 x 10 texel smear patch
# (uniform in states 0-2, gone in 3: no cracks ever showed from the cockpit).
# With glass_rect=INTERIOR_GLASS_RECT add_geometry now rebuilds each window in
# the stock layout (glass.InteriorGlass). Exterior and full-entry glass no
# longer squash Forza's UVs into GLASS_RECT: with glass_rect=GLASS_RECT
# add_geometry maps each window by its shape onto its panel slot (windscreen,
# rear window, side glass; src/dsfforza/convert/glass.py), so the cracks and the shattered
# hole of each damage state line up with the window.
GLASS_RECT = (0.14, 0.98, 0.004, 0.12)  # exterior/full: selects the slot mapping
INTERIOR_GLASS_RECT = (0.0664, 0.0859, 0.0371, 0.0469)  # interior: selects glass.InteriorGlass (stock layout)


def glass_uv(uv, rect: tuple[float, float, float, float] = GLASS_RECT) -> tuple[float, float]:
    u = min(1.0, max(0.0, uv[0]))
    v = min(1.0, max(0.0, uv[1]))
    return rect[0] + (rect[1] - rect[0]) * u, rect[2] + (rect[3] - rect[2]) * v


def host_glass_links(stream, vehc: Entry, glass: tuple = (GLASS,)) -> dict[str, bytes]:
    """s9 bytes of the host's windscreen and rear-window glass, read from the
    host's own glass vertices (call before the geometry is replaced). Stock
    glass vertices are never bound to the chassis (0, 0): on the Desanne the
    windscreen carries (0, 5) (record 29, role 8), the rear window (0, 20)
    (record 61, role 8), side windows their doors, lamp glass the lamps.
    Returns {"front": s9, "rear": s9}: of the s9 groups of front/rear-facing
    glass ahead of / behind z = 0, the highest one (lamp glass sits lower).
    `glass`: the entry's glass material UIDs (the interior has its own)."""
    from dsfforza.formats import vehicle_mesh as vm

    drsp = child(child(vehc, "MDXN"), "DRSP")
    buffers, index_buffers, subs = {}, {}, []
    for e in drsp.children:
        if e.tag == "VXCK":
            hd = payload(stream, child(e, "VXHD"))
            buffers[hd[:8]] = vm.VertexBuffer(
                hd[:8],
                *struct.unpack_from("<II", hd, 8),
                vm.parse_vdcl(payload(stream, child(e, "VDCL"))),
                payload(stream, child(e, "VXDT")),
            )
        elif e.tag == "IXCK":
            index_buffers[payload(stream, child(e, "IXHD"))[:8]] = payload(stream, child(e, "IXDT"))
        elif e.tag == "SBMC":
            subs.append(payload(stream, child(e, "SBBH")))
    groups: dict[bytes, list] = {}
    for d in subs:
        vb = buffers.get(d[16:24])
        if d[8:16] not in glass or vb is None or vb.stride != 28:
            continue
        ib = index_buffers[d[0x30:0x38]]
        (base,) = struct.unpack_from("<I", d, 0x20)
        first, count = struct.unpack_from("<II", d, 0x38)
        e9, e5 = vb.element(9), vb.element(5)
        for t in range(first, first + count):
            i = base + struct.unpack_from("<H", ib, t * 2)[0]
            s9 = bytes(vb.read(e9, i))
            if s9[:2] == bytes(2):
                continue
            b0, b1 = vb.read(e5, i)[:2]
            polar, azimuth = (b0 - 128) / 127 * math.pi, (b1 - 128) / 128 * math.pi
            nx, nz = math.sin(polar) * math.sin(azimuth), math.sin(polar) * math.cos(azimuth)
            g = groups.setdefault(s9, [0, 0.0, 0.0, 0.0, 0.0])
            g[0] += 1
            g[1] += vm.position(vb, i)[2]
            g[2] += abs(nx)
            g[3] += abs(nz)
            g[4] += vm.position(vb, i)[1]
    facing = [(g[1] / g[0], g[4] / g[0], s9) for s9, g in groups.items() if g[3] > g[2] and g[0] >= 6]
    out = {}
    for name, half in (("front", [f for f in facing if f[0] > 0]), ("rear", [f for f in facing if f[0] < 0])):
        if half:
            out[name] = max(half, key=lambda f: f[1])[2]
    return out


def glass_link(links: dict, section: str, pts, normals, default: bytes) -> bytes:
    """Front-/rear-facing glass triangles in the cabin go to the windscreen /
    rear-window record; side windows and lamp glass keep `default`."""
    if "light" in section.lower():
        return default
    n = [sum(c[a] for c in normals) for a in range(3)]
    if abs(n[2]) < abs(n[0]):
        return default  # side window
    z = sum(p[2] for p in pts) / len(pts)
    return links.get("front" if z > 0 else "rear", default)


def interior_glass_skin(dmg, car, opts) -> dict[str, bytes]:
    """Section name -> s9 bytes for the interior entry's glass sections: the
    panel the damage plan put that window on in the full entry (stock
    interiors bind their glass like the exterior: windscreen (0, 5), door
    glass on its door). Glass on the chassis showed the chassis record's
    crack state, so a knocked car lost its interior windscreen. {} without
    --damage."""
    if dmg is None:
        return {}
    from collections import Counter

    lod = min(opts.lods.values())
    glass_sections = [s for s in car.body if s.name.lower().startswith("glass")]
    assign = dmg.assignment(car.body, lod)
    owner = {id(sub): s.name for s in glass_sections for sub in s.subsections}
    votes: dict[str, Counter] = {}
    for (sid, _), pair in assign.items():
        if sid in owner:
            votes.setdefault(owner[sid], Counter())[pair] += 1
    # dmg.bound: door glass keeps the door's crack state but not its swing,
    # like the exterior door it sits in (damage.DamagePlan.bound).
    return {name: bytes((*dmg.bound(c.most_common(1)[0][0]), 0, 1)) for name, c in votes.items()}


@dataclass
class Placement:
    """Forza body space to the host's: a 180-degree turn about y, then a
    translation. Both use +y up, but Forza's front is at -z (370Z headlights
    and front bumper at z = -2.0) and DSF's at +z (Desanne front hubs at
    z = +1.583). Without the turn the car drives backwards (runs
    20261022-002224 and -004928 show the 370Z's nose facing the chase
    camera). The turn is a proper rotation, so triangle winding is kept."""

    dy: float
    dz: float

    def point(self, p) -> tuple[float, float, float]:
        return (-p[0], p[1] + self.dy, -p[2] + self.dz)

    @staticmethod
    def normal(n) -> tuple[float, float, float]:
        return (-n[0], n[1], -n[2])


# --- building a DSF model package ---------------------------------------------


@dataclass
class NewSub:
    uid: bytes
    material: bytes
    stride: int
    base_vertex: int
    first_index: int
    index_count: int
    flag: int


@dataclass
class Builder:
    uid_prefix: bytes
    counter: int = 0
    buffers: dict[int, bytearray] = field(default_factory=dict)  # stride -> vertex bytes
    indices: list[int] = field(default_factory=list)
    subs: list[NewSub] = field(default_factory=list)
    # material UID -> (vertex stride, MODL flag): MATERIAL_LAYOUT plus this conversion's
    # host materials (convert shares one dict between its builders).
    layout: dict[bytes, tuple[int, int]] = field(default_factory=lambda: dict(MATERIAL_LAYOUT))

    def new_uid(self) -> bytes:
        """A stock-looking resource UID: [counter<<4] 0x12 [counter>>4] prefix[3]
        then byte 7 = 0x2D.

        The engine stores a resolved resource as a pointer in the same 8-byte
        slot and marks it by setting the top bit of byte 7 (research/upvh.md),
        so byte 7 must keep that bit clear. Our first scheme put a counter in
        byte 7 and crashed once it reached 0x80; a scheme with byte 7 = 0x2E
        and arbitrary bytes 0..6 crashed too. This layout, copied from stock
        UIDs (byte 1 = 0x12, byte 7 = 0x2D), loads with 130+ new submodels
        (runs 20261022-004457 and -004637). Unique for 4095 resources per
        builder; different builders must use different prefixes."""
        self.counter += 1
        if self.counter > 0xFFF:
            raise ValueError("too many resources for one UID prefix")
        p = self.uid_prefix
        return bytes([(self.counter << 4) & 0xFF, 0x12, self.counter >> 4, p[0], p[1], p[2], p[3] & 0x7F, 0x2D])

    def add(self, material: bytes, verts: list[bytes], tris: list[tuple[int, int, int]]) -> NewSub:
        stride, flag = self.layout[material]
        buf = self.buffers.setdefault(stride, bytearray())
        base = len(buf) // stride
        for v in verts:
            buf += v
        first = len(self.indices)
        for t in tris:
            self.indices.extend(t)
        sub = NewSub(self.new_uid(), material, stride, base, first, len(tris) * 3, flag)
        self.subs.append(sub)
        return sub


SHARED = {"paint": PAINT, "glass": GLASS, "trim": TRIM, "tyre": TYRE, "chrome": CHROME, "skip": None}
SHARED_EXTERIOR = {
    "paint": PAINT_LOD1,
    "glass": GLASS_LOD1,
    "trim": TRIM_LOD1,
    "tyre": TYRE_LOD1,
    "chrome": CHROME_LOD1,
    "skip": None,
}
ROLE_OF = {PAINT: "paint", GLASS: "glass", TRIM: "trim", TYRE: "tyre", CHROME: "chrome"}


# --- wheels ------------------------------------------------------------------------
#
# The Forza wheel section is one wheel in its own frame: hub at the origin, spin axis x,
# tyre centred on x = 0, and the rim face (spokes, lug nuts, centre cap, blur discs) at -x,
# which is the car's left in Forza space. DSF wheel models face +x: the UPVH wheel table gives
# the +x wheels the identity and the -x wheels a 180-degree turn about y ((0, 1, 0, 0)), so one
# model serves both sides (stock 227 and 266: centre cap and spoke disc at x +0.05..+0.12). The
# converter used to copy the Forza wheel unturned, so all four wheels showed the back of the rim
# outwards. Now it is turned 180 degrees about y like the body (Placement), which puts the face
# at +x. Stock wheels use the shared tyre texture (GLOBAL_Tyre, 512x512 DXT1: red is the grey
# level, green the gloss, blue 255), never Forza's tiling tyre/rim UVs; the tyre is unwrapped onto
# the texture's tread strip and sidewall ring like stock tyres (TyreMap; the "tyre" swatch is only
# the fallback). Forza's rims sample the car's own wheel.xds (media/wheels/<car>, port_car.
# extract_wheel): with textures, rims go through the car's atlas on the Chassis material
# (wheel_material, dsf_materials.Texturing.plan(wheels=...)). Without the texture each rim
# material gets a uniform swatch of the tyre texture: the Desanne's rim grey, a dark inner-rim
# grey, bright chrome (research/wheels.md); every rim came out plain silver that way.
WHEEL_UV = {"tyre": (0.5, 0.0078), "rim": (0.906, 0.0078), "inner": (0.4375, 0.0547), "chrome": (0.117, 0.0078)}


def wheel_role(material: str) -> str | None:
    """Which tyre-texture swatch a Forza wheel subsection gets; None = leave out (blur discs,
    shadows, alpha-cut emblems)."""
    n = material.lower()
    if "blur" in n or "shadow" in n or "emblem" in n or n.startswith("ghost"):
        return None
    if n.startswith(("tire", "tyre", "tread", "sidewall")):
        return "tyre"
    if "inner" in n:
        return "inner"
    if "chrome" in n:
        return "chrome"
    return "rim"


def wheel_material(textures, section, sub, shared: dict) -> bytes | None:
    """DSF material of a Forza wheel subsection; None = leave it out. With the car's
    wheel texture in the atlas (dsf_materials.Texturing.plan(wheels=...)), rims,
    caps and wheel emblems use the entry's Chassis material and their own
    artwork, as stock centre caps do (the wheel parts of 110 of the 140 four-wheeled
    stock vehicles mix GLOBAL_Tyre with 4 to 244 triangles on the vehicle's own
    Chassis material, in both entries); the tyre stays on the shared tyre material
    (stride 20, like stock wheels). Without it, every part is on the shared tyre
    with a uniform swatch per wheel_role (WHEEL_UV)."""
    tyre = shared.get("tyre") or TYRE
    if textures is not None:
        from dsfforza.convert import dsf_materials

        kind = textures.texturing.material(section, sub)
        if kind in (dsf_materials.CHASSIS, dsf_materials.LIGHTS_MAT):
            return textures.material(section, sub, shared)
        if kind == "skip":
            return None
    return None if wheel_role(sub.material) is None else tyre


def wheel_point(p, fit=(1.0, 1.0)) -> tuple[float, float, float]:
    """Forza wheel frame -> DSF wheel frame: from the tyre's centre (fit[2:5], CarDims.
    tyre_centre; the origin when not given), 180 degrees about y, then the axle's size
    (fit[:2] = (axial scale, radial scale), 1 for the model's own size)."""
    cx, cy, cz = fit[2:5] if len(fit) >= 5 else (0.0, 0.0, 0.0)
    return (-(p[0] - cx) * fit[0], (p[1] - cy) * fit[1], -(p[2] - cz) * fit[1])


def wheel_normal(n, fit=(1.0, 1.0)) -> tuple[float, float, float]:
    return (-n[0] / fit[0], n[1] / fit[1], -n[2] / fit[1])  # inverse transpose; encoding normalises


def wheel_lod(sections, lod: int) -> int:
    """The Forza wheel LOD to use for body LOD `lod`: `lod` itself if it has a tyre
    subsection, else the nearest LOD that has one (the finer on a tie). Some Forza wheels
    have a LOD made of other materials: RX-7 '97 and Civic Type R '04 LOD 1 and M3 E30 LOD 2
    are the whole wheel on 'body' plus an 'interior' lip ring, which came out as one
    rim-grey blob; the Golf R and DMC-12 have a wheel at LOD 1 only (no wheels in the full
    entry), the Durango and Express LOD 5 is one 'wheel_LOD5' mesh."""
    have = {sub.lod for s in sections for sub in s.subsections if sub.material.lower().startswith(("tire", "tyre"))}
    if not have or lod in have:
        return lod
    return min(have, key=lambda l: (abs(l - lod), l))


# Stock tyres (research/wheels.md, 30 hosts surveyed): GLOBAL_Tyre has no normal map, the
# tread and sidewall detail is all in the diffuse atlas. Tread: the fine tread strip, u
# across the tyre (0.505..0.75 with the shoulders), v one tile along the circumference
# (0.897..0.989), repeated round the tyre. Sidewalls (both): the lower half-ring centred on
# (0.5, 1.0), each 180 degrees of the tyre on the whole half-ring, the rim edge at ring
# radius ~0.33 (lettering band 0.33..0.39) and the tread edge at ~0.49.
TYRE_TREAD_UV = (0.507, 0.749, 0.897, 0.989)
TYRE_RING_UV = (0.5, 1.0, 0.33, 0.492)  # centre u, v; ring radius at the rim edge, at the tread edge


@dataclass
class TyreMap:
    """UVs for the tyre triangles of one wheel model (DSF wheel frame, after wheel_point):
    tread triangles onto the tread strip, tiled round the circumference, sidewall
    triangles (face normal within ~55 degrees of the axle) onto the sidewall ring, 180
    degrees of tyre per ring. Tile edges are snapped to the model's own vertex angles, so a
    triangle never spans two tiles (a seam inside it would smear the whole strip)."""

    r_in: float
    r_out: float
    x_min: float
    x_max: float
    tread_edges: list  # degrees, ascending, last = first + 360
    side_edges: list

    @staticmethod
    def build(tris: list) -> TyreMap | None:
        pts = [p for t in tris for p in t]
        if len(tris) < 8:
            return None
        rs = [math.hypot(p[1], p[2]) for p in pts]
        xs = [p[0] for p in pts]
        r_in, r_out = min(rs), max(rs)
        if r_out - r_in < 1e-3 or max(xs) - min(xs) < 1e-3:
            return None
        angles = sorted(
            {
                round(math.degrees(math.atan2(p[1], p[2])) % 360.0, 2)
                for p, r in zip(pts, rs)
                if r > r_out - 0.25 * (r_out - r_in)
            }
        )
        if len(angles) < 3:
            return None
        u0, u1, v0, v1 = TYRE_TREAD_UV
        target = math.degrees((v1 - v0) * (max(xs) - min(xs)) / (u1 - u0) / r_out)  # tile at texture scale

        def edges(count: int) -> list:
            ring = angles + [a + 360.0 for a in angles]
            out = [angles[0]]
            for k in range(1, max(1, count)):
                want = angles[0] + k * 360.0 / count
                snap = min(ring, key=lambda a: abs(a - want))
                if snap > out[-1] + 1e-6 and snap < angles[0] + 360.0 - 1e-6:
                    out.append(snap)
            return out + [angles[0] + 360.0]

        return TyreMap(r_in, r_out, min(xs), max(xs), edges(round(360.0 / target)), edges(2))

    def uvs(self, tri) -> list[tuple[float, float]]:
        a, b, c = tri
        e1 = [b[k] - a[k] for k in range(3)]
        e2 = [c[k] - a[k] for k in range(3)]
        n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        length = math.sqrt(sum(q * q for q in n)) or 1.0
        side = abs(n[0]) / length > 0.55
        edges = self.side_edges if side else self.tread_edges
        cy, cz = sum(p[1] for p in tri), sum(p[2] for p in tri)
        centre = math.degrees(math.atan2(cy, cz)) if abs(cy) + abs(cz) > 1e-9 else 0.0
        centre = edges[0] + (centre - edges[0]) % 360.0
        j = max(i for i in range(len(edges) - 1) if edges[i] <= centre + 1e-6)
        lo, hi = edges[j], edges[j + 1]
        ts = []
        for p in tri:  # unwrapped next to the triangle's centre angle
            ang = math.degrees(math.atan2(p[1], p[2]))
            ang += 360.0 * round((centre - ang) / 360.0)
            ts.append(min(1.0, max(0.0, (ang - lo) / (hi - lo))))
        out = []
        if side:
            cu, cv, ring_in, ring_out = TYRE_RING_UV
            for p, t in zip(tri, ts):
                f = min(1.0, max(0.0, (math.hypot(p[1], p[2]) - self.r_in) / (self.r_out - self.r_in)))
                radius = ring_in + f * (ring_out - ring_in)
                phi = math.pi * (1.0 - t)
                out.append((cu + radius * math.cos(phi), cv - radius * math.sin(phi)))
        else:
            u0, u1, v0, v1 = TYRE_TREAD_UV
            for p, t in zip(tri, ts):
                f = (p[0] - self.x_min) / (self.x_max - self.x_min)
                out.append((u0 + f * (u1 - u0), v0 + t * (v1 - v0)))
        return out


# Baked sky term (s0.w) of the wheel model. Forza's wheel vertices carry no AO byte
# (all 0 on the 40 testdrive cars), so vertex_sky gave every wheel vertex
# DEFAULT_AO x the hemisphere factor of its wheel-local normal: about 0.42 on the rim
# face, the back of the spokes and the inside of the barrel alike. Stock wheels bake
# their own occlusion (wheel parts of the 140 four-wheeled stock vehicles, exterior
# entry, median of per-vehicle medians): outward faces 0.64, inward faces 0.02, the
# barrel's inside 0.15, the tread 0.29, with no up/down term (the wheel turns). With
# the flat 0.42 the inside of a wheel lit like its face: the 205 T16's white rim read
# as a plain disc, its holes showing the white barrel behind (research/wheels.md,
# "Wheel shading"). wheel_sky gives each vertex WHEEL_SKY_OPEN x the share of a
# cosine-weighted hemisphere that escapes: rays into the wheel's own triangles are
# blocked (marched through a WHEEL_AO_VOXEL grid of them; hits closer than
# WHEEL_AO_SKIP voxels are the vertex's own surface), rays heading inboard (-x
# in the DSF wheel frame) end on the car, and rays leaving past the tyre's radius
# count WHEEL_SKY_SIDE (the arch above, the road below): the tread comes out at
# about 0.3 like stock instead of 0.45.
WHEEL_SKY_OPEN = 0.64
WHEEL_SKY_SIDE = 0.5
WHEEL_AO_RAYS = 24
WHEEL_AO_VOXEL = 0.003  # metres
WHEEL_AO_SKIP = 3  # voxels


def _hemisphere(count: int):
    """Cosine-weighted unit vectors about +z (Hammersley points)."""
    import numpy as np

    k = np.arange(count)
    u = (k + 0.5) / count
    v = np.array([int(f"{n:016b}"[::-1], 2) / 65536.0 for n in k])
    r, phi = np.sqrt(u), 2 * np.pi * v
    return np.stack([r * np.cos(phi), r * np.sin(phi), np.sqrt(1 - u)], 1)


def wheel_sky(points, normals, triangles) -> list[float]:
    """s0.w for wheel vertices (DSF wheel frame: outboard +x): WHEEL_SKY_OPEN x the
    unoccluded share of each vertex's hemisphere, the wheel's own `triangles`
    ((n, 3, 3) positions) as occluders and the inboard half-space closed."""
    import numpy as np

    pts = np.asarray(points, float).reshape(-1, 3)
    if not len(pts):
        return []
    nrm = np.asarray(normals, float).reshape(-1, 3)
    nrm = nrm / np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
    tri = np.asarray(triangles, float).reshape(-1, 3, 3)
    h = WHEEL_AO_VOXEL
    corners = np.concatenate([tri.reshape(-1, 3), pts])
    lo = corners.min(0) - 2 * h
    dims = np.ceil((corners.max(0) + 2 * h - lo) / h).astype(int) + 1
    grid = np.zeros(dims, bool)
    if len(tri):  # each triangle sampled at half-voxel spacing
        a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
        longest = np.max(
            [np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1)], 0
        )
        steps = np.maximum(1, np.ceil(longest / (0.5 * h)).astype(int))
        for n in np.unique(steps):
            sel = steps == n
            i, j = np.mgrid[0 : n + 1, 0 : n + 1]
            keep = i + j <= n
            fu, fv = (i[keep] / n)[None, :, None], (j[keep] / n)[None, :, None]
            s = a[sel][:, None] + fu * (b[sel] - a[sel])[:, None] + fv * (c[sel] - a[sel])[:, None]
            q = np.floor((s.reshape(-1, 3) - lo) / h).astype(int)
            grid[q[:, 0], q[:, 1], q[:, 2]] = True
    hemi = _hemisphere(WHEEL_AO_RAYS)
    helper = np.where(np.abs(nrm[:, :1]) < 0.9, [[1.0, 0.0, 0.0]], [[0.0, 1.0, 0.0]])
    t = np.cross(nrm, helper)
    t /= np.linalg.norm(t, axis=1, keepdims=True)
    bt = np.cross(nrm, t)
    dirs = (
        hemi[None, :, :1] * t[:, None] + hemi[None, :, 1:2] * bt[:, None] + hemi[None, :, 2:] * nrm[:, None]
    ).reshape(-1, 3)
    pos = np.repeat(pts + nrm * h, WHEEL_AO_RAYS, 0)
    light = np.zeros(len(dirs))  # per ray: 1 out of the wheel's face, WHEEL_SKY_SIDE out of its side
    alive = dirs[:, 0] >= 0  # inboard: the car
    travelled, length = 0.0, float(np.linalg.norm(dims * h))
    while travelled < length and alive.any():
        idx = np.nonzero(alive)[0]
        q = np.floor((pos[idx] - lo) / h).astype(int)
        inside = np.all((q >= 0) & (q < dims), 1)
        gone = idx[~inside]
        light[gone] = np.where(q[~inside, 0] >= dims[0], 1.0, WHEEL_SKY_SIDE)
        alive[gone] = False
        idx, q = idx[inside], q[inside]
        if travelled >= WHEEL_AO_SKIP * h:
            alive[idx[grid[q[:, 0], q[:, 1], q[:, 2]]]] = False  # hit the wheel
        pos[idx] += dirs[idx] * h
        travelled += h
    vis = light.reshape(-1, WHEEL_AO_RAYS).mean(1)
    return [round(float(WHEEL_SKY_OPEN * v), 4) for v in vis]


_wheel_sky_cache: dict = {}


def wheel_skies(sections, lod: int, fit=(1.0, 1.0)) -> dict:
    """{(id(pool), index): s0.w} for the vertices of the wheel model's drawn
    subsections at `lod` (wheel_sky over those triangles, in the fitted frame).
    Cached per (sections, lod, fit): the full and exterior entries draw the same LODs."""
    from dsfforza.convert import forza_materials as fm

    key = (tuple(id(s) for s in sections), lod, tuple(fit))
    hit = _wheel_sky_cache.get(key)
    if hit is not None and all(a is b for a, b in zip(hit[0], sections)):
        return hit[1]
    tris, verts = [], {}
    for section in sections:
        for sub in section.subsections:
            if sub.lod != lod or fm.classify_wheel(sub.material)[0] == fm.SKIP:
                continue
            pool = section.vertices_for(sub)
            for t in sub.triangles():
                tris.append([wheel_point(pool[i].position, fit) for i in t])
                for i in t:
                    verts.setdefault((id(pool), i), pool[i])
    keys = list(verts)
    skies = wheel_sky(
        [wheel_point(verts[k].position, fit) for k in keys], [wheel_normal(verts[k].normal, fit) for k in keys], tris
    )
    out = dict(zip(keys, skies))
    if len(_wheel_sky_cache) > 32:
        _wheel_sky_cache.clear()
    _wheel_sky_cache[key] = (list(sections), out)
    return out


def axle_tyres(name: str, gamedbs) -> dict | None:
    """{"front"|"rear": (diameter m, width m)} from gamedb.slt Data_Car tyre sizes."""
    import sqlite3

    for db in gamedbs or []:
        if not db or not Path(db).exists():
            continue
        con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT FrontTireWidthMM, FrontTireAspect, FrontWheelDiameterIN, RearTireWidthMM, "
                "RearTireAspect, RearWheelDiameterIN FROM Data_Car WHERE lower(MediaName) = lower(?)",
                (name,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            con.close()
        if row and all(row):
            fw, fa, fd, rw, ra, rd = row
            return {
                "front": ((fd * 25.4 + 2 * fw * fa / 100) / 1000, fw / 1000),
                "rear": ((rd * 25.4 + 2 * rw * ra / 100) / 1000, rw / 1000),
            }
    return None


def axle_fits(radius: float, width: float, tyres: dict | None) -> dict[str, tuple[float, float]]:
    """{"front"|"rear": (axial scale, radial scale)} that bring the wheel model (radius,
    width) to each axle's Data_Car tyre (axle_tyres), as Forza draws it; (1, 1) without
    tyre sizes. Clamped to 0.6..1.6 across and 0.77..1.3 radially."""
    fits = {"front": (1.0, 1.0), "rear": (1.0, 1.0)}
    for axle in ("front", "rear") if tyres else ():
        diameter, tyre_width = tyres[axle]
        fits[axle] = (min(1.6, max(0.6, tyre_width / width)), min(1.3, max(0.77, diameter / 2 / radius)))
    return fits


def add_geometry(
    builder: Builder,
    sections,
    lod: int,
    place: Placement,
    wheel: bool,
    textures=None,
    merge: bool = False,
    shared: dict | None = None,
    skin: dict[str, bytes] | None = None,
    glass_links: dict | None = None,
    glass_rect: tuple[float, float, float, float] = GLASS_RECT,
    lamps=None,
    keep=None,
    wheel_fit: tuple[float, float] = (1.0, 1.0),
    exposed: bool = False,
    glass_sight=None,
) -> list[NewSub]:
    """Add every subsection of `sections` at Forza `lod`, one DSF submodel per
    (section, material), and return the submodels.

    wheel: the sections are the Forza wheel model: turned into DSF's wheel frame
    (wheel_point, scaled by wheel_fit), blur discs left out. `lod` goes through
    wheel_lod; the tyre is on the shared tyre material, unwrapped onto the stock
    tread strip and sidewall ring (TyreMap). Rims sample the car's wheel texture
    through `textures` (planned with the wheel, wheel_material), else they are on
    the shared tyre material with a swatch per wheel_role.

    textures: a dsf_materials.Binding (see dsf_materials.prepare). Lamps,
    badges and interior then use the entry's Chassis/Lights materials with UVs
    in the converted atlas, other parts DSF's shared materials by
    forza_materials role, and paint points at the atlas's plain texel.
    merge: one submodel per material across all sections (split at 64K
    vertices), for fewer render objects when the sections share a MODL part.
    shared: role -> material UID map instead of SHARED (the interior entry
    uses its host's own paint and glass). skin: section name -> vertex
    semantic 9 bytes (encode_vertex), e.g. the steering wheel's hierarchy node.
    glass_links: {"front": s9, "rear": s9} (host_glass_links): windscreen and
    rear-window triangles are bound to the host's glass-panel records like
    stock glass, instead of the chassis. glass_rect: GLASS_RECT (default) maps
    each window onto its slot of the glass texture (glass.SlotMapper); another
    rectangle squashes Forza's glass UVs into it, except INTERIOR_GLASS_RECT
    (the interior entry), which lays each window out like stock interior glass
    (glass.InteriorGlass; per-window report in builder.interior_glass);
    glass_sight (cockpit.GlassSight) tells it which glass the driver sees."""
    textured = textures is not None
    tyre_map = skies = None
    if wheel:
        lod = wheel_lod(sections, lod)
        skies = wheel_skies(sections, lod, wheel_fit)
        tyre_map = TyreMap.build(
            [
                [wheel_point(s.vertices_for(sub)[i].position, wheel_fit) for i in t]
                for s in sections
                for sub in s.subsections
                if sub.lod == lod and wheel_role(sub.material) == "tyre"
                for t in sub.triangles()
            ]
        )
    shared = SHARED if shared is None else shared
    glass_materials = {GLASS} | ({shared["glass"]} if shared.get("glass") else set())
    skin = skin or {}
    batches: list[dict[bytes, list]] = []  # per batch: material -> [(section, sub)]
    for section in sections:
        if not merge or not batches:
            batches.append({})
        for sub in section.subsections:
            if sub.lod != lod:
                continue
            if wheel:
                material = wheel_material(textures, section, sub, shared)
            elif textured:
                material = textures.material(section, sub, shared)
            else:
                material = material_for(sub.material)
                material = shared.get(ROLE_OF[material], material) if material in ROLE_OF else material
            if material is None:
                continue
            batches[-1].setdefault(material, []).append((section, sub))
    # Exterior/full glass: each window mapped by its own shape onto its panel
    # slot of the glass texture (src/dsfforza/convert/glass.py), from all of the section's
    # glass triangles, so per-panel calls (keep) agree on the mapping.
    slots = None
    if not wheel and glass_rect == GLASS_RECT:
        from dsfforza.convert import glass

        slots = glass.SlotMapper()
        for groups in batches:
            for material, items in groups.items():
                if material in glass_materials:
                    for section, sub in items:
                        pool = section.vertices_for(sub)
                        for tri in sub.triangles():
                            slots.add(section.name, [place.point(pool[i].position) for i in tri], sub.material)
    # Interior glass (glass_rect=INTERIOR_GLASS_RECT): collected per window and
    # rebuilt in the stock interior layout (glass.InteriorGlass: smear surface,
    # crack lines for states 1-2, rim shards for state 3), each window on its
    # own s9 so the panel's crack state shows from the cockpit.
    interior_glass = not wheel and glass_rect == INTERIOR_GLASS_RECT
    added = []
    for groups in batches:
        for material, items in groups.items():
            stride = builder.layout[material][0]
            verts_out, remap, tris = [], {}, []
            window = None
            if interior_glass and material in glass_materials:
                from dsfforza.convert import glass

                window = glass.InteriorGlass(seed=builder.uid_prefix.hex(), sight=glass_sight)

            def flush():  # called within this iteration only
                if tris:  # noqa: B023
                    added.append(builder.add(material, list(verts_out), list(tris)))  # noqa: B023
                verts_out.clear(), remap.clear(), tris.clear()  # noqa: B023

            for section, sub in items:
                pool = section.vertices_for(sub)
                x_off, x_scale, y_off, y_scale = sub.uv_transform[:4]
                role = wheel_role(sub.material) if wheel else None
                for k, tri in enumerate(sub.triangles()):
                    if keep is not None and not keep(section, sub, k, tri):
                        continue  # damage.DamagePlan: another panel's triangle
                    if merge and len(verts_out) > 0xFFFF - 3:
                        flush()
                    uvs, tag = (
                        textures.triangle_uvs(section, sub, k, [pool[i].uv0 for i in tri]) if textured else (None, None)
                    )
                    if tag == "drop":
                        continue  # alpha-tested away in Forza (dsf_materials.DROP)
                    s9 = skin.get(section.name, RIGID)
                    if glass_links and material in glass_materials and not wheel:
                        s9 = glass_link(
                            glass_links,
                            section.name,
                            [place.point(pool[i].position) for i in tri],
                            [place.normal(pool[i].normal) for i in tri],
                            s9,
                        )
                    if lamps is not None and not wheel:
                        s9 = lamps.s9(sub, k, s9)  # lights.LampPlan: light id + 1 in byte 2
                    if window is not None:
                        normals = [place.normal(pool[i].normal) for i in tri]
                        window.add(
                            section.name,
                            [place.point(pool[i].position) for i in tri],
                            normals,
                            [vertex_sky(pool[i], pool, n) for i, n in zip(tri, normals)],
                            s9,
                            sub.material,
                        )
                        continue
                    pane = None  # glass: the window (slot group) this triangle belongs to
                    if slots is not None and material in glass_materials:
                        pane = slots.classify(section.name, [place.point(pool[i].position) for i in tri], sub.material)[
                            1
                        ]
                    # damage.split_exposed subsections: sentinel UV0 for DamagePlan (exposed=True)
                    shown = getattr(sub, "exposed_uv", None) if exposed and not wheel else None
                    tyre_uv = None  # tyre: tread strip / sidewall ring UVs for this triangle (TyreMap)
                    if tyre_map is not None and role == "tyre":
                        tyre_uv = [
                            (round(u, 5), round(w, 5))
                            for u, w in tyre_map.uvs([wheel_point(pool[i].position, wheel_fit) for i in tri])
                        ]
                    out = []
                    for n, i in enumerate(tri):
                        key = (
                            id(pool),
                            i,
                            x_off,
                            x_scale,
                            y_off,
                            y_scale,
                            tag,
                            s9,
                            role,
                            pane,
                            tyre_uv[n] if tyre_uv else None,
                            shown,
                        )
                        if key not in remap:
                            v = pool[i]
                            pos = wheel_point(v.position, wheel_fit) if wheel else place.point(v.position)
                            uv0 = uvs[n] if uvs else (v.uv0[0] * x_scale + x_off, v.uv0[1] * y_scale + y_off)
                            uv1 = v.uv1
                            if wheel and tag is None:
                                uv0 = tyre_uv[n] if tyre_uv else WHEEL_UV[role]
                            elif sub.material == PLATE_TECHNIQUE:
                                uv0 = v.uv0  # the plate in the shared paint atlas (src/dsfforza/convert/plates.py)
                            elif material == PAINT or material == shared.get("paint"):
                                # The paint shader overlays the chassis texture: a plain texel.
                                uv0 = textures.paint_uv if textured else PAINT_UV
                            elif material in glass_materials and pane is not None:
                                uv0 = slots.uv(pane, pos)  # the window's own slot (src/dsfforza/convert/glass.py)
                            elif material in glass_materials:
                                uv0 = glass_uv(uv0, glass_rect)  # another rectangle (not used by the converter)
                            elif material == TRIM or material == shared.get("trim"):
                                # Shared trim samples DSF's global chassis texture (engine bay,
                                # undercarriage, grilles, plate...); Forza's swatch/tiling UVs
                                # land anywhere in it (the Veyron's carbon, x246 tiling, drew
                                # noise over its whole upper body). Stock flat trims use its
                                # dark block.
                                uv0 = TRIM_UV
                            elif material == CHROME or material == shared.get("chrome"):
                                # Shared chrome: a grey texel of the same texture; the
                                # reflection does the rest. UV1 as for paint (the
                                # damage decal; damage.DamagePlan takes the host's).
                                uv0 = CHROME_UV
                            elif tag is not None:
                                uv1 = uv0  # atlas-textured; the Lights material's UV1 feeds its decal map
                            if shown:
                                uv0 = shown
                            remap[key] = len(verts_out)
                            normal = wheel_normal(v.normal, wheel_fit) if wheel else place.normal(v.normal)
                            sky = skies.get((id(pool), i)) if skies is not None else None
                            if sky is None:
                                sky = vertex_sky(v, pool, normal)
                            verts_out.append(encode_vertex(stride, pos, uv0, uv1, normal, s9, sky))
                        out.append(remap[key])
                    tris.append(tuple(out))
            if window is not None:
                for corners, _region in window.triangles():
                    if merge and len(verts_out) > 0xFFFF - 3:
                        flush()
                    out = []
                    for pos, uv0, normal, sky, s9 in corners:
                        key = ("glass", tuple(round(c, 5) for c in pos), uv0, s9)
                        if key not in remap:
                            remap[key] = len(verts_out)
                            verts_out.append(encode_vertex(stride, pos, uv0, uv0, normal, s9, sky))
                        out.append(remap[key])
                    tris.append(tuple(out))
                report = getattr(builder, "interior_glass", {})
                report.update(window.report)
                builder.interior_glass = report
            if len(verts_out) > 0xFFFF:
                raise ValueError(f"{items[0][0].name}: {len(verts_out)} vertices in one submodel")
            flush()
    return added


def entry_like(template: Entry, payload_bytes: bytes | None = None, children: list[Entry] | None = None) -> Entry:
    e = copy.deepcopy(template)
    e.payload = payload_bytes
    if children is not None:
        e.children = children
    return e


def rebuild_drsp(
    stream, drsp: Entry, builder: Builder, keep_host_materials: bool, keep_host_geometry: bool = False
) -> None:
    """Replace SBMC/VXCK/IXCK/MDID in a DRSP with the builder's contents. The
    host's own textures and materials are dropped unless keep_host_materials;
    with keep_host_geometry the host's submodels and buffers stay as well and
    the builder's are added alongside."""
    old = drsp.children
    keep_tags = {"TEXC", "MACO"} if keep_host_materials or keep_host_geometry else set()
    if keep_host_geometry:
        keep_tags |= {"SBMC", "VXCK", "IXCK"}
    tex_mat = [e for e in old if e.tag in keep_tags]
    old_mdid = payload(stream, next(e for e in old if e.tag == "MDID")) if keep_host_geometry else b""
    sbmc_t = next(e for e in old if e.tag == "SBMC")
    vxck_t = {}
    for e in old:
        if e.tag == "VXCK":
            stride = struct.unpack_from("<I", payload(stream, child(e, "VXHD")), 12)[0]
            vxck_t[stride] = e
    ixck_t = next(e for e in old if e.tag == "IXCK")
    mdid_t = next(e for e in old if e.tag == "MDID")

    vb_uid = {stride: builder.new_uid() for stride in builder.buffers}
    ib_uid = builder.new_uid()

    new_children = list(tex_mat)
    for sub in builder.subs:
        sbbh = struct.pack(
            "<8s8s8s8sI12s8sII8s",
            sub.uid,
            sub.material,
            vb_uid[sub.stride],
            bytes(8),
            sub.base_vertex,
            bytes(12),
            ib_uid,
            sub.first_index,
            sub.index_count,
            bytes(8),
        )
        new_children.append(entry_like(sbmc_t, children=[entry_like(sbmc_t.children[0], sbbh)]))
    for stride, data in builder.buffers.items():
        t = vxck_t[stride]
        users = [s.uid for s in builder.subs if s.stride == stride]
        hd = vb_uid[stride] + struct.pack("<II", len(data) // stride, stride)
        dp = b"".join(users) + bytes(len(users))
        kids = []
        for c in t.children:
            body = {"VXHD": hd, "VXDP": dp, "VXDT": bytes(data)}.get(c.tag)
            kids.append(entry_like(c, body if body is not None else payload(stream, c)))
        new_children.append(entry_like(t, children=kids))
    template_ixhd = payload(stream, child(ixck_t, "IXHD"))
    ixhd = ib_uid + struct.pack("<II", len(builder.indices), 2) + template_ixhd[16:]
    ixdp = b"".join(s.uid for s in builder.subs)
    ixdt = struct.pack(f"<{len(builder.indices)}H", *builder.indices)
    kids = [entry_like(c, {"IXHD": ixhd, "IXDP": ixdp, "IXDT": ixdt}[c.tag]) for c in ixck_t.children]
    new_children.append(entry_like(ixck_t, children=kids))

    def current(e: Entry) -> bytes:  # an edited payload, else the source bytes
        return e.payload if e.payload is not None else payload(stream, e)

    texture_uids = [current(child(e, "UIDC"))[:8] for e in tex_mat if e.tag == "TEXC"]
    material_uids = [current(child(e, "MAHE"))[:8] for e in tex_mat if e.tag == "MACO"]
    if keep_host_geometry:
        # Host order is textures, materials, submodels, vertex buffers, index buffer;
        # slot the new resources in after each group.
        old_uids = [old_mdid[i : i + 8] for i in range(0, len(old_mdid), 8)]
        host_subs = {payload(stream, child(e, "SBBH"))[:8] for e in old if e.tag == "SBMC"}
        host_vbs = {payload(stream, child(e, "VXHD"))[:8] for e in old if e.tag == "VXCK"}
        head = [
            u
            for u in old_uids
            if u not in host_subs
            and u not in host_vbs
            and u not in {payload(stream, child(e, "IXHD"))[:8] for e in old if e.tag == "IXCK"}
        ]
        # Textures/materials added to the DRSP (dsf_materials clone mode) go after their kind.
        after_tex = max((n + 1 for n, u in enumerate(head) if u in texture_uids), default=0)
        head = (
            head[:after_tex]
            + [u for u in texture_uids if u not in old_uids]
            + head[after_tex:]
            + [u for u in material_uids if u not in old_uids]
        )
        mdid_uids = (
            head
            + [u for u in old_uids if u in host_subs]
            + [s.uid for s in builder.subs]
            + [u for u in old_uids if u in host_vbs]
            + [vb_uid[s] for s in builder.buffers]
            + [payload(stream, child(e, "IXHD"))[:8] for e in old if e.tag == "IXCK"]
            + [ib_uid]
        )
    else:
        mdid_uids = (
            texture_uids
            + material_uids
            + [s.uid for s in builder.subs]
            + [vb_uid[s] for s in builder.buffers]
            + [ib_uid]
        )
    new_children.append(entry_like(mdid_t, b"".join(mdid_uids)))
    drsp.children = new_children


def rebuild_modl(
    template: bytes, part_lods: dict[int, dict[int, list[NewSub]]], keep_other_parts: bool = False
) -> bytes:
    """Keep the part records; rewrite the LOD tables. part_lods maps part index
    -> {mask: submodels}. Other LOD entries are emptied, or keep the host's
    submodel lists when keep_other_parts."""
    part_count, _lod_count, lod_offset, _ = struct.unpack_from("<4I", template)
    out = bytearray(template[:lod_offset])
    lods = []  # (mask, [(uid, flag)])
    for p in range(part_count):
        r = 0x10 + p * 0x60
        count, offset = struct.unpack_from("<II", template, r + 0x48)
        host = {}
        for k in range(count):
            mask, subs, uid_offset, extra_offset = struct.unpack_from("<4I", template, offset + k * 16)
            host[mask] = [
                (
                    template[uid_offset + i * 8 : uid_offset + i * 8 + 8],
                    struct.unpack_from("<I", template, extra_offset + i * 4)[0],
                )
                for i in range(subs)
            ]
        wanted = {mask: [(s.uid, s.flag) for s in subs] for mask, subs in part_lods.get(p, {}).items()}
        masks = list(host) + [m for m in wanted if m not in host]
        struct.pack_into("<II", out, r + 0x48, len(masks), lod_offset + len(lods) * 16)
        for mask in masks:
            if mask in wanted:
                lods.append((mask, wanted[mask]))
            else:
                lods.append((mask, host[mask] if keep_other_parts else []))
    struct.pack_into("<I", out, 4, len(lods))
    uid_list = lod_offset + len(lods) * 16
    extra_list = uid_list + sum(len(s) for _, s in lods) * 8
    uid_bytes, extra_bytes, entries = bytearray(), bytearray(), bytearray()
    for mask, subs in lods:
        entries += struct.pack("<4I", mask, len(subs), uid_list + len(uid_bytes), extra_list + len(extra_bytes))
        for uid, flag in subs:
            uid_bytes += uid
            extra_bytes += struct.pack("<I", flag)
    return bytes(out) + bytes(entries) + bytes(uid_bytes) + bytes(extra_bytes)


def template_part_masks(modl: bytes) -> dict[int, list[int]]:
    """Part index -> masks of the LODs that had submodels in the host's MODL."""
    part_count = struct.unpack_from("<I", modl)[0]
    result = {}
    for p in range(part_count):
        count, offset = struct.unpack_from("<II", modl, 0x10 + p * 0x60 + 0x48)
        masks = []
        for k in range(count):
            mask, subs = struct.unpack_from("<II", modl, offset + k * 16)
            if subs:
                masks.append(mask)
        result[p] = masks
    return result


def fill_empty_parts(builder: Builder, modl: bytes, part_lods: dict[int, dict[int, list[NewSub]]]) -> int:
    """Give every (part, mask) that had geometry in the host one degenerate
    triangle, so nothing the hierarchy expects is empty."""
    filled = 0
    degenerate = encode_vertex(MATERIAL_LAYOUT[TRIM][0], (0.0, 0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 1.0, 0.0))
    for part, masks in template_part_masks(modl).items():
        for mask in masks:
            if not part_lods.get(part, {}).get(mask):
                sub = builder.add(TRIM, [degenerate] * 3, [(0, 1, 2)])
                part_lods.setdefault(part, {})[mask] = [sub]
                filled += 1
    return filled


@dataclass
class HostWheels:
    """The host's wheels, read from its own hierarchy (research/hosts.md). Hub
    height and axle midpoint differ per host (Desanne y -0.398, z 0.1155; 370Z
    -0.326; Skyline R33 and F-150 y 0), and 33 four-wheeled stock cars keep the front and
    rear wheels in two MODL parts."""

    hubs: list[tuple[float, float, float]]  # UPVH wheel table, in table order
    hub_y: float
    axle_mid: float
    full_parts: list[int]  # full-entry MODL parts the wheel records point at
    exterior_parts: list[int]  # exterior MODL parts the wheel records' LOD links point at
    axles: dict = field(default_factory=dict)  # (kind, part) -> "front", "rear" or "both"


def host_wheels(upvh_data: bytes, full_modl: bytes, exterior_modl: bytes | None = None) -> HostWheels:
    from dsfforza.formats import upvh as upvh_mod

    h = upvh_mod.parse(upvh_data)
    hubs = [w[:3] for w in h.vec4s("wheels")]
    if len(hubs) != 4:
        raise ValueError(f"host has {len(hubs)} wheels; the converter needs 4")
    zs = [w[2] for w in hubs]
    full_ids = upvh_mod.modl_part_uids(full_modl)
    wheels = [r for r in h.records if r.type == 2]
    full_parts = sorted({full_ids.index(r.uid) for r in wheels if r.uid in full_ids})
    ext_parts: list[int] = []
    if exterior_modl is not None:
        ext_ids = upvh_mod.modl_part_uids(exterior_modl)
        links = h.lod_links()
        ext_parts = sorted(
            {
                ext_ids.index(links[r.lod_link][1])
                for r in wheels
                if 0 <= r.lod_link < len(links) and links[r.lod_link][1] in ext_ids
            }
        )
    if not full_parts:
        raise ValueError("no MODL part for the host's wheel records")
    mid = sum(zs) / 4
    seen: dict = {}
    for r in wheels:
        if not 0 <= r.wheel < len(hubs):
            continue
        axle = "front" if hubs[r.wheel][2] > mid else "rear"
        if r.uid in full_ids:
            seen.setdefault((0, full_ids.index(r.uid)), set()).add(axle)
        if exterior_modl is not None and 0 <= r.lod_link < len(links) and links[r.lod_link][1] in ext_ids:
            seen.setdefault((2, ext_ids.index(links[r.lod_link][1])), set()).add(axle)
    axles = {k: (v.pop() if len(v) == 1 else "both") for k, v in seen.items()}
    return HostWheels(hubs, sum(w[1] for w in hubs) / 4, (max(zs) + min(zs)) / 2, full_parts, ext_parts, axles)


def patch_hubs(
    upvh: bytes,
    front_z: float,
    rear_z: float,
    half_track: float,
    hub_y: float,
    rear_half_track: float | None = None,
    rear_hub_y: float | None = None,
) -> tuple[bytes, int]:
    """Move the four wheel hubs to (+-half_track, hub_y, front_z/rear_z) in the
    host's hierarchy: the wheel table, every hinge pivot within 10 cm of a hub
    (the wheel hinges and the steering knuckles next to the front hubs, Desanne
    hinges 3 to 6, 14, 15), which move with their hub, and the wheel records'
    bodies, whose position is the negated hub (they move by minus the hub's
    offset). Hubs come from the host's own wheel table, so this works for any
    host (it used to match the Desanne's hub height, -0.398, by value).
    rear_half_track / rear_hub_y: the rear axle's, when it differs (Veyron: rear
    track outer 2.003 with 365 mm tyres against 1.999 with 265 mm at the front)."""
    from dsfforza.formats import upvh as upvh_mod

    h = upvh_mod.parse(upvh)
    data = bytearray(upvh)
    hubs = [w[:3] for w in h.vec4s("wheels")]
    moves = []  # (old hub, offset)
    for n, (x, y, z) in enumerate(hubs):
        front = z > sum(w[2] for w in hubs) / len(hubs)
        new = (
            math.copysign(half_track if front or rear_half_track is None else rear_half_track, x),
            hub_y if front or rear_hub_y is None else rear_hub_y,
            front_z if front else rear_z,
        )
        moves.append(((x, y, z), tuple(b - a for a, b in zip((x, y, z), new))))
        struct.pack_into("<3f", data, h.offsets["wheels"] + n * 0x20, *new)
    patched = len(hubs)

    def offset_near(p) -> tuple | None:
        best = min(moves, key=lambda m: math.dist(p, m[0]))
        return best[1] if math.dist(p, best[0]) < 0.1 else None

    for n in range(h.counts["hinges"]):
        at = h.offsets["hinges"] + n * 0x30
        pivot = struct.unpack_from("<3f", data, at)
        d = offset_near(pivot)
        if d is not None:
            struct.pack_into("<3f", data, at, *(p + o for p, o in zip(pivot, d)))
            patched += 1
    for r in h.records:
        if r.type == 2 and r.body >= 0 and r.hinge >= 0:
            pivot = struct.unpack_from("<3f", upvh, h.offsets["hinges"] + r.hinge * 0x30)
            d = offset_near(pivot)
            at = h.offsets["bodies"] + r.body * 0xB0 + 0x30
            pos = struct.unpack_from("<3f", data, at)
            if d is not None and math.dist(pos, tuple(-c for c in pivot)) < 0.1:
                struct.pack_into("<3f", data, at, *(p - o for p, o in zip(pos, d)))
                patched += 1
    return bytes(data), patched


# y of the VO3D camera mounts (a vec3 per camera index, research/handling.md "Driver aids,
# steering, cameras"): chase 300..302, bumper 327..329, bonnet 535..537 and the unmeasured ones.
CAMERA_MOUNT_Y = (301, 304, 328, 338, 341, 395, 536, 550, 553)


def settle_wheels(stream, full: Entry, log=print) -> dict:
    """Lower each axle's hubs by the wheel offset the engine draws at rest, so that at rest
    the wheels sit where Forza has them (research/wheels.md, "Wheel clearance").

    The engine draws a wheel `up` above its hub-table position at rest
    (wheel_clearance.rest_offsets: the static compression against VO3D p25, the dead zone
    below p4, the axle's load and spring scale), and the body settles by the same amount.
    convert() puts the hubs on Forza's static pose, so a port sat that much low on its
    wheels: 39 mm at the front of the #2 Audi A4 DTM, whose race-car arches leave less room
    than that.

    Runs on the final VO3D (after the handling). The CoG height p30 (and p599) comes down
    with the hubs by the axles' mean, so the physics, which sees the hierarchy relative to
    the CoG, is unchanged and the body rises; the VO3D camera mounts rise with it."""
    from dsfforza.build import handling
    from dsfforza.convert import wheel_clearance
    from dsfforza.formats import upvh as upvh_mod

    nonr = child(full, "NONR")
    upvh_entry, vo3d_entry = child(nonr, "UPVH"), child(nonr, "VO3D")
    data = payload(stream, upvh_entry)
    vo3d = handling.VO3D.parse(payload(stream, vo3d_entry))
    hubs = [w[:3] for w in upvh_mod.parse(data).vec4s("wheels")]
    mid = sum(h[2] for h in hubs) / len(hubs)
    front = [h for h in hubs if h[2] > mid]
    rear = [h for h in hubs if h[2] <= mid]

    def mean(values):
        values = list(values)
        return sum(values) / len(values)

    front_z, rear_z = mean(h[2] for h in front), mean(h[2] for h in rear)
    up = wheel_clearance.rest_offsets(vo3d.params, front_z, rear_z)
    report = {"front_mm": round(up["front"] * 1000, 1), "rear_mm": round(up["rear"] * 1000, 1)}
    if max(abs(up["front"]), abs(up["rear"])) < 0.001:
        return report
    upvh_entry.payload, patched = patch_hubs(
        data,
        front_z,
        rear_z,
        mean(abs(h[0]) for h in front),
        mean(h[1] for h in front) - up["front"],
        mean(abs(h[0]) for h in rear),
        mean(h[1] for h in rear) - up["rear"],
    )
    drop = (up["front"] + up["rear"]) / 2
    vo3d.params[30] = vo3d.get(30) - drop  # the engine's default when the VO3D has none
    if 599 in vo3d.params:
        vo3d.params[599] -= drop
    # The car's frame (where the VO3D camera mounts are measured from) follows the physics:
    # it sinks against the body with the CoG, so the mounts the VO3D sets go up with the body
    # (runs vm-20261025-074146 / -074620: the head camera, a hierarchy marker, logged 40 mm
    # higher in that frame after a 36 mm drop).
    for pid in CAMERA_MOUNT_Y:
        if pid in vo3d.params:
            vo3d.params[pid] += drop
    vo3d_entry.payload = vo3d.build()
    report.update({"hubs_patched": patched, "cog_drop_mm": round(drop * 1000, 1)})
    log(
        f"  wheels settled: hubs lowered {report['front_mm']:g} mm front, {report['rear_mm']:g} mm rear "
        f"(the engine's rest offset), CoG p30 by {report['cog_drop_mm']:g} mm"
    )
    return report


# --- the Forza car --------------------------------------------------------------


def find_file(folder: Path, relative: str) -> Path | None:
    """Case-insensitive lookup of a relative path in an extracted car folder
    (FH1 has Physics/MAXData.xml and NIS_370Z_10.carbin, FM4 physics/maxdata.xml
    and nis_370z_10.carbin)."""
    here = Path(folder)
    for part in relative.replace("\\", "/").split("/"):
        if not here.is_dir():
            return None
        here = next((p for p in here.iterdir() if p.name.lower() == part.lower()), None)
        if here is None:
            return None
    return here


@dataclass
class ForzaCar:
    name: str
    folder: Path
    sections: list
    body: list  # stock body sections (upgrade variants dropped), wheel excluded
    wheel: list

    @property
    def game(self) -> str:
        return carbin.game_of(self.sections)


# The wheel archive's carbin (media/wheels/<car>), cached by port_car.extract_wheel
# under this name: one section `wheel`, the same model as the main carbin's.
WHEEL_CARBIN = "wheel.carbin"


def load_car(folder: Path, name: str) -> ForzaCar:
    """The car's main carbin. The wheel is its `wheel` section; without one, the
    stock variant of a `wheela`/`wheelb` pair (the Minis), else the `wheel` section
    of the wheel archive's carbin (Ferrari 360 and Dino, Gallardo 05, Esperante,
    Corvette Grand Sport: their main carbin has no wheel)."""
    path = find_file(folder, f"{name}.carbin")
    if path is None:
        raise FileNotFoundError(f"{name}.carbin not in {folder}")
    sections = carbin.parse(path.read_bytes())
    stock = stock_sections(sections)
    wheel = [s for s in sections if s.name == "wheel"]
    if not wheel:
        wheel = [s for s in stock if re.fullmatch(r"wheel[a-z]", s.name)]
    if not wheel:
        archive = find_file(folder, WHEEL_CARBIN)
        if archive is not None:
            wheel = [s for s in carbin.parse(archive.read_bytes()) if s.name == "wheel"]
    if not wheel:
        raise ValueError(f"{name}: no wheel section (main carbin or {WHEEL_CARBIN})")
    taken = {id(s) for s in wheel}
    return ForzaCar(name, Path(folder), sections, [s for s in stock if s.name != "wheel" and id(s) not in taken], wheel)


@dataclass
class CarDims:
    """Where the Forza car's wheels are, in Forza model space (metres)."""

    wheelbase: float
    # Forza model z of the wheelbase midpoint: MINUS BottomCenterWheelbasePos.z.
    # That value is measured towards the front (+z forward) while the model's
    # front is at -z: the wheel arches of the Cuda, Mustang Boss 429 and Veyron
    # centre on -z (-0.24 / -0.20 / -0.12 against z = +0.203 / +0.213 / +0.122).
    # Taking +z put those cars' wheels 2 x z (0.25 to 0.43 m) off their arches.
    mid_z: float
    track_outer: float  # front track, outer tyre wall to outer tyre wall
    radius: float  # tyre radius, measured on the wheel model
    tyre_width: float  # measured on the wheel model
    source: str  # where wheelbase/track/mid_z came from
    # Forza model y = 0 is the bottom of the body at the wheelbase centre
    # (BottomCenterWheelbasePos), not the ground: the ground is StockRideHeight
    # below it (370Z: 0.144; MAXData's collision box reaches y = -0.14, the
    # body itself -0.055). Without it the body sat 0.14 to 0.23 m too low on
    # its wheels, tyres through the arches.
    ride_height: float = 0.0
    rear_track_outer: float | None = None  # MAXData RearTrackOuter (None: as the front)
    # The tyre's centre in the wheel model's frame. (0, 0, 0) on nearly every car; the
    # wheel is turned about it, not about the model's origin (tyre_centre).
    tyre_centre: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def track(self) -> float:
        """Hub to hub, at the model's tyre width (convert scales the wheel to Data_Car's)."""
        return self.track_outer - self.tyre_width


def _tyre_points(wheel_sections) -> list:
    subs = [(s, sub) for s in wheel_sections for sub in s.subsections if sub.lod > 0]
    tyres = [(s, sub) for s, sub in subs if sub.material in ("tire", "tyre")] or subs
    lod = min(sub.lod for _, sub in tyres)
    return [s.vertices_for(sub)[i].position for s, sub in tyres if sub.lod == lod for t in sub.triangles() for i in t]


def tyre_size(wheel_sections) -> tuple[float, float]:
    """(radius, width) of the tyre in the wheel model's lowest non-zero LOD."""
    pts = _tyre_points(wheel_sections)
    return ((max(p[1] for p in pts) - min(p[1] for p in pts)) / 2, max(p[0] for p in pts) - min(p[0] for p in pts))


def tyre_centre(wheel_sections) -> tuple[float, float, float]:
    """The centre of the tyre's bounding box in the wheel model's frame: the axle the
    wheel turns about. Within a millimetre of the origin on nearly every Forza wheel;
    TOY_6_Supra_05's sits 49 mm out and 80 mm down (its section offset), so drawn about
    the origin it hung 80 mm into the road and wobbled as it turned. Coordinates under
    1 mm count as 0."""
    pts = _tyre_points(wheel_sections)
    centre = [(max(p[k] for p in pts) + min(p[k] for p in pts)) / 2 for k in range(3)]
    return tuple(0.0 if abs(c) < 0.001 else c for c in centre)


def gamedb_body(gamedb: Path, name: str) -> tuple[float, float, float, float] | None:
    """(wheelbase, front track outer, wheelbase midpoint z, ride height) from a game's
    gamedb.slt: Data_CarBody row Data_Car.Id * 1000. Holds 503 of FM4's cars."""
    import sqlite3

    db = sqlite3.connect(f"file:{Path(gamedb).as_posix()}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT b.ModelWheelbase, b.ModelFrontTrackOuter, b.BottomCenterWheelbasePosZ, "
            "(b.ModelFrontStockRideHeight + b.ModelRearStockRideHeight) / 2 "
            "FROM Data_Car c JOIN Data_CarBody b ON b.Id = c.Id * 1000 "
            "WHERE lower(c.MediaName) = lower(?)",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return tuple(row) if row else None


def car_dims(car: ForzaCar, gamedb: Path | None = None) -> CarDims:
    """Wheel placement data, from the first source that has it (ride height:
    mean of the front and rear StockRideHeight):

    1. Physics/MAXData.xml in the car's zip (<Misc Wheelbase FrontTrackOuter>,
       <BottomCenterWheelbasePos z>): every FH1 and FM4 car on the discs but
       FH1's NIS_Leaf_11. FM4 cars carry the same file as FH1 (lower-case path).
    2. gamedb.slt Data_CarBody (ModelWheelbase, ModelFrontTrackOuter,
       BottomCenterWheelbasePosZ), when a gamedb is given.
    Tyre radius, width and centre always come from the car's own wheel model."""
    radius, width = tyre_size(car.wheel)
    centre = tyre_centre(car.wheel)
    maxdata = find_file(car.folder, "Physics/MAXData.xml")
    if maxdata is not None:
        misc = ET.parse(maxdata).getroot().find("Misc")
        ride = [float(misc.get(k)) for k in ("FrontStockRideHeight", "RearStockRideHeight") if misc.get(k)]
        return CarDims(
            float(misc.get("Wheelbase")),
            -float(misc.find("BottomCenterWheelbasePos").get("z")),
            float(misc.get("FrontTrackOuter")),
            radius,
            width,
            "MAXData.xml",
            sum(ride) / len(ride) if ride else 0.0,
            float(misc.get("RearTrackOuter")) if misc.get("RearTrackOuter") else None,
            centre,
        )
    if gamedb is not None and Path(gamedb).exists():
        row = gamedb_body(gamedb, car.name)
        if row:
            source = f"{Path(gamedb).name} Data_CarBody"
            return CarDims(row[0], -row[2], row[1], radius, width, source, row[3] or 0.0, tyre_centre=centre)
    raise ValueError(f"{car.name}: no Physics/MAXData.xml and no gamedb Data_CarBody row")


# --- one conversion -----------------------------------------------------------------


@dataclass
class ConvertOptions:
    lods: dict[int, int] = field(default_factory=lambda: parse_lods(DEFAULT_LODS))
    shadow_lod: int = DEFAULT_SHADOW_LOD
    exterior_lod: int = 1
    host_axle_mid: float | None = None  # None: from the host's wheel table (Desanne 0.1155)
    skip_full: bool = False
    skip_exterior: bool = False
    no_hubs: bool = False
    keep_host_materials: bool = False
    fill_empty_parts: bool = False
    keep_host_geometry: bool = False
    no_wheel: bool = False
    empty_other_parts: bool = False
    textures: str | None = None  # None, "replace" or "clone"
    atlas_size: tuple[int, int] | None = None
    swatch_trims: bool = False
    merge_submodels: bool = False
    uid_seed: bytes | None = None  # 4 bytes; random when None
    # Interior entry (N+1000) from <car>_cockpit.carbin, steering wheel hinge,
    # gauges, mirrors and head camera moved to the Forza cockpit (src/dsfforza/convert/cockpit.py).
    cockpit: bool = False
    cockpit_opts: object = None  # cockpit.CockpitOptions; defaults when None
    # Why the host is the same vehicle as the Forza car (host_select.same_vehicle),
    # or None. When Forza's cockpit carbin is a placeholder (cockpit.is_stub), such a
    # host keeps its own stock interior (cockpit, gauges, wheel, hands, head camera)
    # instead of the main carbin's low-detail cabin (research/cockpit.md).
    same_vehicle_host: str | None = None
    gamedbs: list = field(default_factory=list)  # for CameraOverrides (eye point)
    # Bind windscreen/rear-window glass to the host's glass-panel records like
    # stock glass (host_glass_links) instead of the chassis. Off by default
    # until tested in game.
    glass_links: bool = False
    # Working lamps (src/dsfforza/convert/lights.py): lamp triangles carry their light in vertex
    # semantic 9 byte 2, and the host's UPVH light table is moved onto them.
    lights: bool = True
    # Factory paint palette (src/dsfforza/convert/paint.py): the host's SPSR colours replaced by
    # the car's gamedb.slt Combo_Colors (gamedb from gamedbs, else next to the car).
    paint: bool = True
    # Stock-like damage (src/dsfforza/convert/damage.py): body triangles on the host's panels
    # (semantic 9), crush targets (semantic 12), per-panel MODL parts, panel
    # hinges/bodies/collision moved onto the Forza panels. Off until tested in game.
    damage: bool = False
    damage_opts: object = None  # damage.DamageOptions; defaults when None
    # Licence plates (src/dsfforza/convert/plates.py): the stock DSF plate (shared paint atlas)
    # on the Forza car's plate recess / mounting face, at each end the host has
    # a plate, on the panel under it. Forza `numberplate` quads are replaced.
    plates: bool = True


def parse_lods(text: str) -> dict[int, int]:
    return {int(m): int(lod) for m, lod in (pair.split(":") for pair in text.split(","))}


def convert(
    stream,
    full: Entry,
    exterior: Entry,
    car: ForzaCar,
    dims: CarDims,
    opts: ConvertOptions,
    log=print,
    interior: Entry | None = None,
) -> dict:
    """Replace the geometry of a host vehicle's full (kind 0) and clean exterior
    (kind 2) VEHC entries, in memory, with the Forza car. The entries are edited
    in place; leaves read from `stream` unless already edited. With
    opts.cockpit and the interior (kind 1) entry, the interior is rebuilt from
    the car's cockpit carbin too. Returns a report."""
    # The turn maps Forza z to -z, so the wheelbase midpoint (Forza z = mid_z, see
    # CarDims) lands at -mid_z + dz = axle_mid.
    # Forza ground (y = -ride_height) goes to the host's ground (hub y - tyre radius).
    # Hub height, axle midpoint and wheel parts are the host's own (host_wheels).
    hw = host_wheels(
        payload(stream, child(child(full, "NONR"), "UPVH")),
        payload(stream, child(child(full, "NONR"), "MODL")),
        payload(stream, child(child(exterior, "NONR"), "MODL")),
    )
    # The ground the engine counts camera heights from stays the host's as shipped.
    from dsfforza.convert import vehicle_cameras

    camera_ground = vehicle_cameras.host_ground(
        payload(stream, child(child(full, "NONR"), "UPVH")), payload(stream, child(child(full, "NONR"), "VO3D"))
    )
    axle_mid = hw.axle_mid if opts.host_axle_mid is None else opts.host_axle_mid
    # Per-axle wheel size. Forza draws its one wheel model at each axle's Data_Car tyre size
    # (research/wheels.md, "Wheel clearance"): the model matches the front tyre on most cars
    # (370Z, Veyron SS, M3) but not on 160 of 573 (FER_F151_11's is 0.393 m against a 0.340 m
    # tyre, SUZ_Liana_02's 0.248 against 0.298; Forza's thumbnails show the tyre size). So the
    # front wheels are scaled to the front tyre, and a host with separate rear wheel parts gets
    # a rear copy scaled to the rear tyre; with one shared part the rear wheels are front-sized.
    # The hubs stand the front tyres on Forza's ground; the rear hubs go to the rear track and
    # up or down by the radius difference, so all four tyres stand on the same ground.
    tyres = axle_tyres(car.name, opts.gamedbs)
    fits = axle_fits(dims.radius, dims.tyre_width, tyres)
    split = any(axle == "rear" for axle in hw.axles.values())
    rear_fit = fits["rear"] if split else fits["front"]
    fits["both"] = fits["front"]
    front_radius, front_width = dims.radius * fits["front"][1], dims.tyre_width * fits["front"][0]
    place = Placement(dy=hw.hub_y - front_radius + dims.ride_height, dz=axle_mid + dims.mid_z)

    def part_fit(kind: int, part: int) -> tuple[float, ...]:
        # with the tyre's centre: some wheel models sit off their origin (TOY_6_Supra_05)
        return (*fits[hw.axles.get((kind, part), "both")], *dims.tyre_centre)

    half_track = (dims.track_outer - front_width) / 2
    rear_half_track = ((dims.rear_track_outer or dims.track_outer) - dims.tyre_width * rear_fit[0]) / 2
    rear_hub_y = hw.hub_y + dims.radius * rear_fit[1] - front_radius
    log(
        f"{car.name}: {len(car.body)} stock body sections, wheelbase {dims.wheelbase:.3f}, "
        f"tyre radius {front_radius:.3f} (model {dims.radius:.3f}), track {2 * half_track:.3f} ({dims.source})"
    )
    swapped = {lod: wheel_lod(car.wheel, lod) for lod in [*opts.lods.values(), opts.exterior_lod]}
    if any(a != b for a, b in swapped.items()):
        log(
            "wheel LODs without a tyre, using the nearest that has one: "
            + ", ".join(f"{a} -> {b}" for a, b in sorted(swapped.items()) if a != b)
        )
    seed = opts.uid_seed or os.urandom(4)
    report: dict = {
        "placement": {
            "source": dims.source,
            "wheelbase": round(dims.wheelbase, 4),
            "track": round(2 * half_track, 4),
            "track_outer": round(dims.track_outer, 4),
            "tyre_radius": round(dims.radius, 4),  # the wheel model's
            "tyre_width": round(dims.tyre_width, 4),
            "tyre_centre": [round(c, 4) for c in dims.tyre_centre],
            "front_fit": [round(c, 4) for c in fits["front"]],
            "ground_radius": round(front_radius, 4),  # the hubs stand this far over Forza's ground
            "ride_height": round(dims.ride_height, 4),
            "mid_z": round(dims.mid_z, 4),
            "dy": round(place.dy, 4),
            "dz": round(place.dz, 4),
            "host_hub_y": round(hw.hub_y, 4),
            "host_axle_mid": round(axle_mid, 4),
            "host_wheel_parts": {"full": hw.full_parts, "exterior": hw.exterior_parts},
            "rear_fit": [round(c, 4) for c in rear_fit],
            "rear_half_track": round(rear_half_track, 4),
            "rear_hub_y": round(rear_hub_y, 4),
            "tyres": tyres,
        }
    }

    tex_full = tex_ext = None
    layout = dict(MATERIAL_LAYOUT)  # this conversion's materials (no state shared between cars)
    if opts.textures:
        import dataclasses

        from dsfforza.convert import cutout, dsf_materials, trim_art
        from dsfforza.convert import forza_materials as fm

        lods = set(opts.lods.values()) | {opts.shadow_lod, opts.exterior_lod}
        # Alpha-tested badges as geometry: DSF's Chassis material has no alpha test,
        # so a badge quad drew its whole rectangle (src/dsfforza/convert/cutout.py).
        body, cut = cutout.cut_alpha_tested(car.body, fm.AtlasCache(car.folder), lods=lods)
        if cut:
            car = dataclasses.replace(car, body=body)
            report["cutout"] = {k: {**v, "area": round(v["area"], 5)} for k, v in cut.items()}
            log(
                "alpha-tested decals cut to their mask: "
                + ", ".join(f"{k} {v['cut']} -> {v['made']}" for k, v in sorted(cut.items()))
            )
        # Trim triangles on light artwork (the Alfa GTA's quadrifoglio badges) to the
        # atlas, bare metal (the Veyron's scoops) to the shared chrome: the shared trim
        # draws them black (src/dsfforza/convert/trim_art.py).
        body, art = trim_art.split_trim_art(car.body, fm.AtlasCache(car.folder), lods=lods)
        if art:
            car = dataclasses.replace(car, body=body)
            report["trim_art"] = {k: {**v, "area": round(v["area"], 5)} for k, v in art.items()}
            log(
                "trim artwork to the atlas (to chrome): "
                + ", ".join(f"{k} {v['triangles']} ({v['metal']}, {v['area']:.3f} m2)" for k, v in sorted(art.items()))
            )
        wheels = None
        if not opts.no_wheel:  # the wheel LODs add_geometry will draw (wheel_lod)
            wheels = (car.wheel, {wheel_lod(car.wheel, lod) for lod in [*opts.lods.values(), opts.exterior_lod]})
        tex_full, tex_ext = dsf_materials.prepare(
            stream,
            full,
            exterior,
            car.folder,
            car.body,
            lods,
            mode=opts.textures,
            size=opts.atlas_size,
            swatch_trims=opts.swatch_trims,
            log=log,
            interior_vehc=interior,
            wheels=wheels,
        )
        layout.update(tex_full.layout)
        layout.update(tex_ext.layout)

    # Cockpit: fit the Forza cockpit and find the host's steering-wheel node first,
    # so the body's steering wheel can be skinned to it as well.
    cf = host_cp = None
    skin: dict[str, bytes] = {}
    if opts.cockpit:
        from dsfforza.convert import cockpit

        # Without a cockpit carbin, fit() builds the cabin from the main carbin.
        sections = cockpit.load_cockpit(car.folder, car.name) or []
        if interior is None or opts.skip_full:
            log("cockpit: needs the interior entry and the full entry; skipped")
        else:
            # The main carbin stands in for stub cockpit carbins and missing steering wheels.
            try:
                cf = cockpit.fit(sections, car.name, opts.gamedbs, folder=car.folder, main_sections=car.sections)
            except ValueError as error:
                log(f"cockpit: {error}; the interior stays the host's")
            else:
                if cf.source != "cockpit":
                    log(f"cockpit: {cf.source}")
                if not cf.lod0_atlases and opts.same_vehicle_host:
                    # Forza ships a placeholder cockpit; the host's stock interior is
                    # this vehicle's (same model or badge twin) and complete.
                    log(
                        f"cockpit: placeholder Forza cockpit and the host is the same vehicle "
                        f"({opts.same_vehicle_host}): the interior stays the host's"
                    )
                    report["cockpit_host"] = {"forza": cf.source, "same_vehicle": opts.same_vehicle_host}
                    cf = None
                else:
                    host_cp = cockpit.host_cockpit(payload(stream, child(child(full, "NONR"), "UPVH")))
                    skin = {"steering_wheel": host_cp.wheel_skin}
                    # The hands grip the cockpit's wheel in every view; the chase views draw
                    # the main carbin's, which Forza models elsewhere (up to 17 cm away).
                    import dataclasses

                    body = []
                    for section in car.body:
                        if section.name == "steering_wheel":
                            aligned, wheel_report = cockpit.align_main_wheel(section, cf.wheel)
                            report["main_steering_wheel"] = wheel_report
                            log(
                                f"main-carbin steering wheel: moved {wheel_report.get('move')} m, turned "
                                f"{wheel_report.get('turn_deg')} deg, scaled {wheel_report.get('scale')} onto the "
                                f"cockpit's" + (f" ({wheel_report['kept']}: kept)" if aligned is None else "")
                            )
                            section = aligned or section
                        body.append(section)
                    car = dataclasses.replace(car, body=body)

    links = host_glass_links(stream, full) if opts.glass_links else None
    if links is not None:
        log(f"glass links: { ({k: tuple(v) for k, v in links.items()}) }")
        report["glass_links"] = {k: list(v) for k, v in links.items()}

    if opts.plates and car.body:
        # Read the host's plates before its geometry is replaced; the plates
        # join the body as synthetic sections, so damage, LODs and the shadow
        # treat them like Forza geometry.
        import dataclasses

        from dsfforza.convert import plates

        host = plates.host_plates(stream, full)
        body, plate_plan = plates.apply(car, place, hw.hub_y - front_radius, host, log=log)
        car = dataclasses.replace(car, body=body)
        report["plates"] = plate_plan.report

    if opts.damage and car.body:
        # Bonnet/boot undersides and the trims under the bonnet into their own
        # subsections (before the lamp plan, which keys on subsections), so
        # DamagePlan can texture what a missing bonnet exposes like stock.
        import dataclasses

        from dsfforza.convert import damage

        engine = damage.engine_placement(car.name, opts.gamedbs, car.folder)
        body, exposed = damage.split_exposed(car.body, place, log=log, engine=engine)
        car = dataclasses.replace(car, body=body)
        report["exposed"] = exposed

    lamps = None
    if opts.lights:
        from dsfforza.convert import lights

        lenses = tex_full.texturing.lens_subs if tex_full is not None else frozenset()
        lamps = lights.LampPlan.build(car.body, place, axle_mid, lenses).assign(
            payload(stream, child(child(full, "NONR"), "UPVH"))
        )
        report["lights"] = lamps.report()
        log(f"lights: {len(lamps.lamps)} lamps on {len(lamps.slots)} host light entries, {len(lamps.unlit)} unlit")

    dmg = None
    if opts.damage:
        from dsfforza.convert import damage

        dmg = damage.DamagePlan(
            stream,
            full,
            exterior,
            car,
            place,
            opts.damage_opts,
            log=log,
            interior=interior,
            engine=damage.engine_placement(car.name, opts.gamedbs, car.folder),
        )
        report["damage"] = dmg.report

    if not opts.skip_full:
        # Full entry: part 0 gets the body per LOD, the wheel part gets the wheel.
        builder = Builder(seed, layout=layout)
        part_lods: dict[int, dict[int, list[NewSub]]] = {0: {}}
        for mask, lod in opts.lods.items():
            if dmg is not None:  # per-panel parts plus the merged part 0
                dmg.full_lod(
                    builder, part_lods, mask, lod, textures=tex_full, merge=opts.merge_submodels, skin=skin, lamps=lamps
                )
            else:
                part_lods[0][mask] = add_geometry(
                    builder,
                    car.body,
                    lod,
                    place,
                    wheel=False,
                    textures=tex_full,
                    merge=opts.merge_submodels,
                    skin=skin,
                    glass_links=links,
                    lamps=lamps,
                )
            if not opts.no_wheel:
                for wheel_part in hw.full_parts:  # Desanne: part 31; 33 hosts split front/rear
                    part_lods.setdefault(wheel_part, {})[mask] = add_geometry(
                        builder, car.wheel, lod, place, wheel=True, wheel_fit=part_fit(0, wheel_part), textures=tex_full
                    )
        part_lods[0][SHADOW_MASK] = add_geometry(
            builder,
            car.body,
            opts.shadow_lod,
            place,
            wheel=False,
            textures=tex_full,
            merge=opts.merge_submodels,
            skin=skin,
            glass_links=links,
            lamps=lamps,
        )
        nonr = child(full, "NONR")
        modl = child(nonr, "MODL")
        host_modl = payload(stream, modl)
        if opts.fill_empty_parts:
            log(f"filled {fill_empty_parts(builder, host_modl, part_lods)} emptied part LODs")
        rebuild_drsp(
            stream,
            child(child(full, "MDXN"), "DRSP"),
            builder,
            keep_host_materials=opts.keep_host_materials or bool(opts.textures),
            keep_host_geometry=opts.keep_host_geometry,
        )
        modl.payload = rebuild_modl(
            host_modl, part_lods, keep_other_parts=opts.keep_host_geometry and not opts.empty_other_parts
        )
        hubs = 0
        if not opts.no_hubs:
            upvh = child(nonr, "UPVH")
            upvh.payload, hubs = patch_hubs(
                payload(stream, upvh),
                axle_mid + dims.wheelbase / 2,
                axle_mid - dims.wheelbase / 2,
                half_track,
                hw.hub_y,
                rear_half_track,
                rear_hub_y,
            )
        if lamps is not None:
            upvh = child(nonr, "UPVH")
            upvh.payload = lamps.patch_upvh(payload(stream, upvh))
        if dmg is not None:
            upvh = child(nonr, "UPVH")
            upvh.payload, _ = dmg.patch_upvh(payload(stream, upvh))
        vertex_bytes = sum(len(b) for b in builder.buffers.values())
        log(
            f"full entry: {len(builder.subs)} submodels, {vertex_bytes // 1024} KiB vertices, "
            f"{len(builder.indices)} indices; {hubs} hub positions patched"
        )
        report["full"] = {
            "submodels": len(builder.subs),
            "vertex_bytes": vertex_bytes,
            "indices": len(builder.indices),
            "hubs_patched": hubs,
        }

    if not opts.skip_exterior:
        # Clean exterior entry: body in part 0, the wheel in the part(s) the wheel records'
        # LOD links name (the last part on the Desanne).
        ext_builder = Builder(seed[:3] + bytes([seed[3] ^ 0xFF]), layout=layout)
        ext_modl_entry = child(child(exterior, "NONR"), "MODL")
        ext_template = payload(stream, ext_modl_entry)
        ext_parts = struct.unpack_from("<I", ext_template)[0]
        if dmg is not None:
            ext_body = dmg.exterior(
                ext_builder,
                opts.exterior_lod,
                textures=tex_ext,
                merge=opts.merge_submodels,
                skin=skin,
                lamps=lamps,
                shared=SHARED_EXTERIOR,
            )
        else:
            ext_body = add_geometry(
                ext_builder,
                car.body,
                opts.exterior_lod,
                place,
                wheel=False,
                textures=tex_ext,
                merge=opts.merge_submodels,
                skin=skin,
                glass_links=links,
                lamps=lamps,
                shared=SHARED_EXTERIOR,
            )
        ext_lods = {0: {EXTERIOR_MASK: ext_body}}
        for wheel_part in hw.exterior_parts or [ext_parts - 1]:
            ext_lods[wheel_part] = {
                EXTERIOR_MASK: add_geometry(
                    ext_builder,
                    car.wheel,
                    opts.exterior_lod,
                    place,
                    wheel=True,
                    wheel_fit=part_fit(2, wheel_part),
                    shared=SHARED_EXTERIOR,
                    textures=tex_ext,
                )
            }
        rebuild_drsp(
            stream, child(child(exterior, "MDXN"), "DRSP"), ext_builder, keep_host_materials=bool(opts.textures)
        )
        ext_modl_entry.payload = rebuild_modl(ext_template, ext_lods)
        log(f"exterior entry: {len(ext_builder.subs)} submodels")
        report["exterior"] = {"submodels": len(ext_builder.subs)}

    if cf is not None:
        from dsfforza.convert import cockpit

        nonr = child(full, "NONR")
        upvh = child(nonr, "UPVH")
        host_upvh = payload(stream, upvh)
        upvh.payload, hier = cockpit.patch_hierarchy(host_upvh, host_cp, place, cf)
        eye = place.point(cf.eye)
        # The driver model sits on the seat marker (role 10) and its hands hold the wheel
        # only where stock cars have it from the seat: the seat follows the moved marker.
        upvh.payload, hier["seats"] = cockpit.patch_seats(upvh.payload, eye)
        camera_eye = cockpit.camera_eye(
            eye, [place.point(q) for q in cockpit.section_points(cf.static["steering_wheel"])]
        )
        camera = cockpit.camera_params(camera_eye, hier["steering"]["centre"][0])
        # The interior is culled from the final camera (Placement is its own inverse up to dy, dz).
        cf.camera = (-camera_eye[0], camera_eye[1] - place.dy, -(camera_eye[2] - place.dz))
        vo3d = child(nonr, "VO3D")
        # The speedometer stops at the Forza dial's last mark (p605).
        gauge_vo3d = cockpit.gauge_params(cf)
        # The hand rig's offset from the steering marker would move the wheel off its Forza place.
        vo3d.payload = cockpit.patch_vo3d(payload(stream, vo3d), {**camera, **gauge_vo3d, **cockpit.hand_params()})
        paint_uv = tex_ext.paint_uv if tex_ext is not None else PAINT_UV
        interior_report = cockpit.convert_interior(
            stream,
            interior,
            car,
            cf,
            place,
            seed,
            paint_uv,
            opts.cockpit_opts or cockpit.CockpitOptions(),
            host_cp.wheel_skin,
            log=log,
            glass_links=links,
            glass_skin=interior_glass_skin(dmg, car, opts),
            layout=layout,
        )
        if dmg is not None:  # the shell's paint samples the decal atlas at UV1 too
            dmg.interior_uv1(stream, interior)
        # The engine picks each needle's transform by semantic 8's gauge id; any
        # pass that rewrites it leaves the needles static.
        needle_ids = cockpit.needle_gauge_ids(stream, interior)
        stray = sorted(k for k in needle_ids if k not in set(cockpit.GAUGE_IDS.values()))
        if stray:
            raise ValueError(
                f"interior needle vertices with gauge ids {stray[:6]} (not in "
                f"{sorted(cockpit.GAUGE_IDS.values())}): the needles would not turn"
            )
        interior_report["needle_ids"] = {str(int(k)): n for k, n in sorted(needle_ids.items())}
        log(
            f"cockpit: eye {tuple(round(c, 3) for c in eye)} (offset {cf.offset}, {cf.offset_source}); "
            f"wheel {hier['steering']['centre']}; VO3D 430..432 = "
            f"{', '.join(f'{v:.3f}' for v in camera.values())}"
        )
        report["cockpit"] = {
            "eye": [round(c, 4) for c in eye],
            "eye_forza": [round(c, 4) for c in cf.eye],
            "eye_offset": list(cf.offset),
            "eye_source": cf.offset_source,
            "vo3d": {str(k): round(v, 4) for k, v in {**camera, **gauge_vo3d}.items()},
            "hierarchy": hier,
            "interior": interior_report,
        }
    if not opts.skip_full:
        # The Bonnet camera: in front of the Forza windscreen, where stock cars have it
        # relative to theirs (the host's sat inside lower cars' cabins).
        vo3d = child(child(full, "NONR"), "VO3D")
        host_cam = vehicle_cameras.bonnet_camera(payload(stream, vo3d), camera_ground)
        windscreen = vehicle_cameras.windscreen_centre(vehicle_cameras.forza_windscreen(car.body, place))
        cam_report = {"ground": round(camera_ground, 4), "host": [round(c, 4) for c in host_cam]}
        if windscreen is None:
            cam_report["kept"] = f"no {vehicle_cameras.WINDSCREEN_SECTION} section"
            log(f"bonnet camera: no {vehicle_cameras.WINDSCREEN_SECTION}; the host's stays")
        else:
            body = vehicle_cameras.section_triangles(car.body, place, opts.exterior_lod)
            cam, rays = vehicle_cameras.bonnet_position(windscreen, body)
            params = vehicle_cameras.bonnet_params(cam, camera_ground)
            vo3d.payload = vehicle_cameras.patch_vo3d(payload(stream, vo3d), params)
            cam_report.update(
                {
                    "windscreen": [round(c, 4) for c in windscreen],
                    "camera": [round(c, 4) for c in cam],
                    "rays": rays,
                    "vo3d": {str(k): round(v, 4) for k, v in params.items()},
                }
            )
            log(f"bonnet camera: {tuple(round(c, 3) for c in cam)} (host's {tuple(round(c, 3) for c in host_cam)})")
        report["bonnet_camera"] = cam_report
    # Hosts with more than one chassis record (172 and 38 other stock cars) draw a
    # variant part instead of part 0: give those parts the body too (src/dsfforza/convert/chassis_variants.py).
    from dsfforza.convert import chassis_variants

    report["chassis_variants"] = chassis_variants.fill(
        stream,
        full,
        interior if cf is not None else None,
        None if opts.skip_exterior else exterior,
        seed,
        log=log,
        full_rebuilt=not opts.skip_full,
    )
    if opts.paint and not opts.skip_full:
        from dsfforza.convert import paint

        gamedb = next((Path(g) for g in opts.gamedbs if g and Path(g).exists()), None) or paint.find_gamedb(car.folder)
        colours = paint.forza_colours(gamedb, car.name) if gamedb else []
        # Leave room under the NONR buffer for handling edits made after this.
        report["paint"] = paint.install(
            stream, full, colours, paint.stock_paints(stream), nonr_limit=NONR_LIMIT[0] - 0x400
        )
        log(f"paint: {report['paint'].get('installed')} factory colours (host had {report['paint'].get('host_count')})")
    return report


def entry_sizes(vehc: Entry) -> dict[str, int]:
    """Serialized size of a VEHC's children (MDXN, NONR), edits included."""
    return {c.tag: entry_size(c) for c in vehc.children}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path, help="archive with the host vehicle (e.g. the clone)")
    parser.add_argument("out", type=Path)
    parser.add_argument("--id", type=int, required=True, help="base spool ID of the host vehicle")
    parser.add_argument("--car", type=Path, required=True, help="extracted Forza car folder")
    parser.add_argument("--name", required=True, help="car media name, e.g. NIS_370Z_10")
    parser.add_argument(
        "--host-axle-mid",
        type=float,
        help="DSF z of the host's axle midpoint (default: from its wheel table; Desanne: (1.583 - 1.352) / 2)",
    )
    parser.add_argument("--lods", default=DEFAULT_LODS, help="DSF mask:Forza LOD pairs for the full entry")
    parser.add_argument("--shadow-lod", type=int, default=DEFAULT_SHADOW_LOD)
    parser.add_argument("--exterior-lod", type=int, default=1, help="Forza LOD for the clean exterior entry")
    parser.add_argument("--gamedb", type=Path, help="gamedb.slt, for wheel placement when the car has no MAXData.xml")
    parser.add_argument("--skip-full", action="store_true", help="leave the full entry (N) as it is")
    parser.add_argument("--skip-exterior", action="store_true", help="leave the clean exterior (N+2000) as it is")
    parser.add_argument("--no-hubs", action="store_true", help="don't move the hierarchy's wheel hubs")
    parser.add_argument(
        "--keep-host-materials",
        action="store_true",
        help="keep the host's own textures and materials in the full entry",
    )
    parser.add_argument(
        "--fill-empty-parts", action="store_true", help="give host parts that lose their geometry a degenerate triangle"
    )
    parser.add_argument(
        "--keep-host-geometry",
        action="store_true",
        help="keep the host's submodels and every part except the body (part 0) and wheel",
    )
    parser.add_argument("--no-wheel", action="store_true", help="leave the host's wheel part alone")
    parser.add_argument(
        "--ignore-size-limit",
        action="store_true",
        help="write entries larger than the largest stock ones (to test the limit)",
    )
    parser.add_argument(
        "--empty-other-parts",
        action="store_true",
        help="with --keep-host-geometry: keep the host submodels in the package but take "
        "them out of the other parts' LOD lists",
    )
    parser.add_argument(
        "--textures",
        choices=("replace", "clone"),
        help="texture lamps, badges and interior from the car's atlases (dsf_materials): "
        "replace = new DDS in the host's chassis TEXC, clone = new TEXC and materials",
    )
    parser.add_argument("--atlas-size", help="WxH of the chassis atlas (default: the host texture's)")
    parser.add_argument(
        "--swatch-trims",
        action="store_true",
        help="with --textures: colour swatch trims (black, frame...) from the atlas",
    )
    parser.add_argument(
        "--merge-submodels",
        action="store_true",
        help="one body submodel per material instead of per (section, material)",
    )
    parser.add_argument(
        "--cockpit",
        action="store_true",
        help="rebuild the interior (N+1000) from <car>_cockpit.carbin and move the steering "
        "hinge, gauges and head camera (src/dsfforza/convert/cockpit.py); use with --keep-host-materials",
    )
    parser.add_argument(
        "--cockpit-shell-lod", type=int, default=5, help="main-carbin LOD for the outer body in the interior entry"
    )
    parser.add_argument(
        "--cockpit-decimate",
        type=float,
        default=0.0,
        help="vertex-cluster cell size (m) for the cockpit sections; 0 = full detail",
    )
    args = parser.parse_args(argv)
    if args.out.resolve() == args.archive.resolve():
        raise SystemExit("refusing to overwrite the input archive")
    opts = ConvertOptions(
        lods=parse_lods(args.lods),
        shadow_lod=args.shadow_lod,
        exterior_lod=args.exterior_lod,
        host_axle_mid=args.host_axle_mid,
        skip_full=args.skip_full,
        skip_exterior=args.skip_exterior,
        no_hubs=args.no_hubs,
        keep_host_materials=args.keep_host_materials,
        fill_empty_parts=args.fill_empty_parts,
        keep_host_geometry=args.keep_host_geometry,
        no_wheel=args.no_wheel,
        empty_other_parts=args.empty_other_parts,
        textures=args.textures,
        atlas_size=tuple(int(x) for x in args.atlas_size.split("x")) if args.atlas_size else None,
        swatch_trims=args.swatch_trims,
        merge_submodels=args.merge_submodels,
        cockpit=args.cockpit,
        gamedbs=[args.gamedb] if args.gamedb else [],
    )
    if args.cockpit:
        from dsfforza.convert import cockpit

        opts.cockpit_opts = cockpit.CockpitOptions(shell_lod=args.cockpit_shell_lod, decimate=args.cockpit_decimate)

    car = load_car(args.car, args.name)
    dims = car_dims(car, args.gamedb)
    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = find(root.entries, "SSIC")
        records = parse_sslp(payload(stream, find(find(ssic.children, "NONR").children, "SSLP")))
        index_by_id = {spool_id: index for spool_id, index, _ in records}
        full = root.entries[index_by_id[args.id]]
        exterior = root.entries[index_by_id[args.id + 2000]]
        interior = root.entries[index_by_id[args.id + 1000]]
        convert(stream, full, exterior, car, dims, opts, interior=interior)

        # Check sizes against the largest stock entries; oversized entries crash the game.
        too_big = []
        for kind, vehc in ((0, full), (1, interior), (2, exterior)):
            sizes = entry_sizes(vehc)
            print(
                f"kind {kind}: MDXN {sizes['MDXN']:#x} (limit {MDXN_LIMIT[kind]:#x}), "
                f"NONR {sizes['NONR']:#x} (limit {NONR_LIMIT[kind]:#x})"
            )
            if sizes["MDXN"] > MDXN_LIMIT[kind] or sizes["NONR"] > NONR_LIMIT[kind]:
                too_big.append(kind)
        if too_big and not args.ignore_size_limit:
            raise SystemExit(f"entries {too_big} exceed the stock size limits; use lower --lods / --exterior-lod")
        with args.out.open("wb") as out:
            write_chunk(out, stream, root.entries, root.version)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
