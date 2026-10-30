"""Vertex encoding, baked lighting (sky and Forza AO), glass UVs and links, and Placement
(Forza space to the host's)."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from dsfforza.convert.forza2dsf.materials import GLASS, POSITION_SCALE
from dsfforza.formats.chnk import Entry
from dsfforza.formats.vehicle_mesh import child, payload

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
    vanilla steering-wheel vertices carry the wheel record's pair, e.g. (1, 1, 0, 1)
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
# Fourth position component (s0.w, int16 / 4096): every vanilla vertex carries a
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
_ao_valid: dict[int, tuple[list, bool]] = {}  # id(pool) -> (the pool, so its id stays its own; valid)
AO_VALID_KEEP = 1024  # pools remembered (a car has a few hundred)


def _pool_has_ao(pool) -> bool:
    hit = _ao_valid.get(id(pool))
    if hit is None or hit[0] is not pool:
        if len(_ao_valid) >= AO_VALID_KEEP:
            _ao_valid.clear()
        values = {v.extra[0] for v in pool if v.extra}
        hit = _ao_valid[id(pool)] = (pool, bool(values) and max(values) <= FORZA_AO_FULL + 10 and len(values) >= 3)
    return hit[1]


def vertex_sky(vertex, pool, normal_dsf) -> float:
    """s0.w for a converted vertex: Forza AO x (SKY_FLOOR + SKY_RANGE x (1 + ny) / 2)."""
    ao = min(1.0, vertex.extra[0] / FORZA_AO_FULL) if _pool_has_ao(pool) else DEFAULT_AO
    length = math.sqrt(sum(c * c for c in normal_dsf)) or 1.0
    up = (1 + normal_dsf[1] / length) / 2
    return ao * (SKY_FLOOR + SKY_RANGE * up)


# Glass UVs. Every vanilla glass submodel uses one of the shared glass materials
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
# Vanilla interior windows use that strip in three layers (src/dsfforza/convert/glass.py,
# "interior glass"): a smear surface (states 0-2; the windscreen only along
# its top and pillars), crack-line slivers on the red block (states 1-2) and
# rim shards on the black blocks (state 3). The pixel shader discards texels
# with blue < 0.01, so each layer shows only in its states. Converted cockpits
# first spread Forza's UVs over the smear (broad white streaks, M5,
# img/fix3-m5-cockpit.png), then sat in the black block (no glass in states
# 0-2, stretched shard streaks in 3), then in a 10 x 10 texel smear patch
# (uniform in states 0-2, gone in 3: no cracks ever showed from the cockpit).
# With glass_rect=INTERIOR_GLASS_RECT add_geometry now rebuilds each window in
# the vanilla layout (glass.InteriorGlass). Exterior and full-entry glass no
# longer squash Forza's UVs into GLASS_RECT: with glass_rect=GLASS_RECT
# add_geometry maps each window by its shape onto its panel slot (windscreen,
# rear window, side glass; src/dsfforza/convert/glass.py), so the cracks and the shattered
# hole of each damage state line up with the window.
GLASS_RECT = (0.14, 0.98, 0.004, 0.12)  # exterior/full: selects the slot mapping
INTERIOR_GLASS_RECT = (0.0664, 0.0859, 0.0371, 0.0469)  # interior: selects glass.InteriorGlass (vanilla layout)


def glass_uv(uv, rect: tuple[float, float, float, float] = GLASS_RECT) -> tuple[float, float]:
    u = min(1.0, max(0.0, uv[0]))
    v = min(1.0, max(0.0, uv[1]))
    return rect[0] + (rect[1] - rect[0]) * u, rect[2] + (rect[3] - rect[2]) * v


def host_glass_links(stream, vehc: Entry, glass: tuple = (GLASS,)) -> dict[str, bytes]:
    """s9 bytes of the host's windscreen and rear-window glass, read from the
    host's own glass vertices (call before the geometry is replaced). Vanilla
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
    panel the damage plan put that window on in the full entry (vanilla
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
