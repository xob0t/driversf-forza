"""The host's panels: its part-0 vertices as a HostField, the panels they form, and the rules
that give a Forza section a panel."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from dsfforza.convert.damage.core import BODY, DOORS, PAINT_MATERIALS, Box, Grid, _cross, _norm, _sub, load_mesh
from dsfforza.convert.damage.uv import Uv1Field, s9_pair
from dsfforza.formats import upvh as upvh_mod
from dsfforza.formats import vehicle_mesh as vm

# --- the host's panels ------------------------------------------------------------


@dataclass
class Panel:
    pair: tuple[int, int]  # s9 bytes 0, 1
    records: list  # upvh.Record(s) with this pair (normally one)
    part: int | None  # full-entry MODL part of the record
    role: int

    @property
    def record(self):
        return self.records[0]


@dataclass
class HostField:
    """Vanilla vertices of one entry's part 0: position, s12 - position, panel pair."""

    points: list
    disp: list
    pairs: list
    box: Box
    by_pair: dict = field(default_factory=dict)  # pair -> Grid (normalised coordinates)
    all_grid: Grid | None = None
    uv1: Uv1Field | None = None  # decal-atlas UVs (32-byte vertices)
    local: set = field(default_factory=set)  # material UIDs the entry defines itself
    tris: list = field(default_factory=list)  # (normalised corners, pair, unit face normal, paint), part 0

    def normalise(self, p):
        c, s = self.box.centre, self.box.size
        return tuple((p[a] - c[a]) / (s[a] / 2) for a in range(3))


def read_part0(stream, vehc, mask: int, mesh: vm.Mesh | None = None) -> list[tuple]:
    """(position, s12 - position, (s9 byte 0, byte 1)) for every vertex in the
    entry's MODL part 0 at `mask` that has semantic 12 (`mesh`: the entry's,
    if already loaded)."""
    mesh = load_mesh(stream, vehc) if mesh is None else mesh
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
    mesh = load_mesh(stream, vehc)
    rows = read_part0(stream, vehc, mask, mesh)
    if not rows:
        raise ValueError(f"{vehc.name}: no semantic-12 vertices in MODL part 0 mask {mask}")
    f = HostField([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], Box.of(r[0] for r in rows))
    groups = defaultdict(list)
    for n, pair in enumerate(f.pairs):
        groups[pair].append(n)
    for pair, ns in groups.items():
        f.by_pair[pair] = Grid([f.normalise(f.points[n]) for n in ns], ns)
    f.all_grid = Grid([f.normalise(p) for p in f.points], list(range(len(f.points))))
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
                    f.tris.append(
                        (
                            tuple(f.normalise(p) for p in pts),
                            s9_pair(vb, tri[0]),
                            normal,
                            mesh.submodels[n].material in PAINT_MATERIALS,
                        )
                    )
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


def glass_name(name: str) -> str:
    """A Forza section name, lower case, with `lexan` read as `glass`. Forza's
    race cars name their polycarbonate glazing lexan<suffix> with the suffixes
    of glass<suffix> (master build of 2026-10-31: lexanF on 76 cars, lexanR 44,
    lexanLF/RF 69/70, lexanLR/RR 42/41, lexanLM/RM 13, lexanLHL/RHL 65,
    lexanLTL/RTL 39, lexanT 1), on glass materials."""
    n = name.lower()
    return "glass" + n[5:] if n.startswith("lexan") else n


def section_rule(name: str) -> tuple[str, tuple]:
    """How a Forza main-carbin section is put on host panels:
    ("fixed", roles) the first role the host has; ("side", roles) the record of
    those roles on the section's side; ("glass", (front|rear)); ("majority",
    roles) the panel most of its triangles are nearest to; ("nearest", roles)
    per triangle."""
    # lexanF is a windscreen like glassF. Read as a body section, the JT1's was
    # split between the chassis and the front structure, whose crush tore a
    # triangle out of it (#411); vanilla windscreens are on the glass record,
    # which doesn't crush.
    n = glass_name(name)
    plate = re.fullmatch(r"(.*)_plate_(front|rear)", n)
    if plate:
        # Licence plates (src/dsfforza/convert/plates.py): the panel of the section they sit
        # on (bumper, boot lid), whole: one panel per plate, like vanilla. On a
        # plain body section: at the front the bumper (vanilla: 121 of 131 front
        # plates), at the rear the nearest of rear bumper, boot lid, chassis
        # (vanilla: 73, 34, 32 of 141).
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
    # (the M5's rear frame showed as a white chevron on the damaged bumper),
    # so they go with their bumper, as vanilla bumper
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
    if n.startswith(("glass", "window")):
        return "majority", DOORS + (38,)  # side windows: their door, or fixed glass
    if n.startswith(("wing", "spoiler")):
        return "majority", (5, 124, 38)
    if n in ("interior", "cagerace") or n.startswith(("seat", "undercarriage")):
        return "fixed", (38,)
    return "nearest", BODY


def mirror_key(name: str) -> str:
    """Section name with its side letter blanked: taillightL/R -> taillight?,
    glassLTL/glassRTL -> glass?TL."""
    return re.sub(r"(?<=[a-z])[LR](?=[A-Z]|$)", "?", name)
