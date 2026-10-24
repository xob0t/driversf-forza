"""Working lamps for converted Forza cars: vertex light bytes and the UPVH light table.

How stock DSF lamps work (research/lights.md, confirmed-static):

- The UPVH light table holds one 0x40-byte entry per lamp: vec4 position,
  vec4 direction (car space, +z forward, +x the car's left), f32 pi, f32 pi,
  u32 id, u32 flags, 16 constant bytes. Type-17 hierarchy records point at
  the entries (+0x30) and hang under the part the lamp belongs to.
- flags byte 3 is the lamp's function, an index into a table in Driver.exe
  (file 0xA38F98) that gives the Lua light-type mask
  (GameVehicleResource.setLights): 1 Headlights, 2 Brakes, 4 DaytimeRunning,
  8 LeftIndicator, 16 RightIndicator, 32 Reverse, 64 Siren, 128
  DippedHeadlights, 256 FogLights. Byte 2 is the colour (4 amber, 6 red,
  8 white, 9 white/xenon), byte 0 a small variant number (2 on indicators, 1
  on centre brake lamps, phases on sirens).
- Every vertex of a lamp carries the lamp in vertex semantic 9, byte 2:
  light id + 1 (0 = not a lamp). 247 of 247 (light byte, id) pairs in nine
  sampled vehicles sit within 0.25 m of the entry with that id. Lamp
  geometry uses the vehicle's Lights material (alpha = glow mask) in the
  full and exterior entries.

Converted cars used to give every lamp triangle byte 0, so no lamp could
light, and kept the host's lamp positions. `LampPlan` classifies each Forza
lamp triangle by its technique (head_light, tail_light, indicator_left...)
and side, groups them into lamps, gives each lamp one of the host's light
entries (same count, same ids, so the hierarchy is untouched), and rewrites
those entries' position, direction and function. Spare entries are switched
off (function 0) and parked on the nearest lamp of the same end.

    uv run dsff lights plan out/cache/fh1/NIS_370Z_10 [--host-upvh upvh.bin]
    uv run dsff lights table <archive> --id 304
"""

from __future__ import annotations

from dsfforza.game import game_paths
import argparse
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Lamp functions: (type byte, colour byte, variant byte). Type bytes index the
# exe's table: 1 Headlights, 2 Brakes, 3 DaytimeRunning, 4/5 Left/Right
# indicator, 6/7 Brakes+Left/Right, 8 Reverse, 0x0C Fog, 0x14/0x15
# DRL+Left/Right, 0x18 DRL+Brakes (tail and stop), 0x19/0x1A DRL+Brakes+L/R.
WHITE, XENON, AMBER, RED = 8, 9, 4, 6
LIGHT_TYPE_MASK = [0x0, 0x1, 0x2, 0x4, 0x8, 0x10, 0xA, 0x12, 0x20, 0x40, 0x80, 0x41, 0x100, 0x40, 0x40, 0x40,
                   0x800, 0x1000, 0x4, 0x44, 0xC, 0x14, 0xC, 0x14, 0x6, 0xE, 0x16, 0xE, 0x16, 0x5]
MASK_NAMES = {1: "Headlights", 2: "Brakes", 4: "DaytimeRunning", 8: "LeftIndicator", 16: "RightIndicator",
              32: "Reverse", 64: "Siren", 128: "DippedHeadlights", 256: "FogLights"}

HEAD, POSITION, TAIL, BRAKE, REVERSE, INDICATOR, FOG, BRAKE_IND, DRL_IND, LENS = (
    "head", "position", "tail", "brake", "reverse", "indicator", "fog", "brake_ind", "drl_ind", "lens")

# Forza technique -> lamp function. Side-specific names carry the side.
TECHNIQUE_FUNCTION: dict[str, tuple[str, str | None]] = {}


def _add(function: str, names: str, side: str | None = None) -> None:
    for name in names.split():
        TECHNIQUE_FUNCTION[name] = (function, side)


_add(HEAD, "head_light head_light2 hidhead xenonhead oldhead fullbeam lights_gls_hidhead lights_gls_oldhead")
_add(POSITION, "drlwhite drlwhiteswitch drlred drlorange slorange")
_add(TAIL, "tail_light tail_light_lod0 tail_light_on tail_light2 taillightst taillight2S drlbrakered "
           "drlbrakeorange lights_gls_tail_light lights_gls_taillightst lights_gls_taillight2S")
_add(BRAKE, "white_brake")
_add(REVERSE, "reverse_light reverse_light_lod0 brkreverse lights_gls_reverse_light")
_add(INDICATOR, "indicator_left slinlorange lights_gls_indicator_left", "L")
_add(INDICATOR, "indicator_right slinrorange lights_gls_indicator_right", "R")
_add(BRAKE_IND, "brinl", "L")
_add(BRAKE_IND, "brinr", "R")
_add(DRL_IND, "drlinlorange drlinlred drlinlwhite lindtailside", "L")
_add(DRL_IND, "drlinrorange drlinrred drlinrwhite rindtailside", "R")
_add(FOG, "fogwhite fogred")
# Lenses take their function from their colour and position: red at the rear
# is a tail lamp, amber an indicator, clear the nearest lamp of its cluster.
_add(LENS, "detail_glass_red detail_glass_amber detail_glass_clear linred rinred")

# Default colour per function (lenses and fog lamps use the technique's own).
FUNCTION_COLOUR = {HEAD: WHITE, POSITION: WHITE, TAIL: RED, BRAKE: RED, REVERSE: WHITE, INDICATOR: AMBER,
                   FOG: WHITE, BRAKE_IND: RED, DRL_IND: AMBER}
TECHNIQUE_COLOUR = {"hidhead": XENON, "xenonhead": XENON, "drlred": RED, "drlorange": AMBER, "slorange": AMBER,
                    "fogred": RED, "drlinlred": RED, "drlinrred": RED, "lindtailside": RED, "rindtailside": RED,
                    "drlbrakeorange": AMBER, "drlinlwhite": WHITE, "drlinrwhite": WHITE}

CENTRE_X = 0.3        # a lamp group whose |x| stays below this is a centre lamp
HIGH_BRAKE_RISE = 0.08  # a centre tail lamp this far above the side tails is the high brake lamp

# Group priority when the host has fewer light entries than the car has lamps;
# lower first. Groups that miss out share an entry of the same function and
# side, else stay unlit.
PRIORITY = {HEAD: 0, TAIL: 1, BRAKE: 2, INDICATOR: 3, BRAKE_IND: 3, DRL_IND: 3, REVERSE: 4, POSITION: 5, FOG: 6}


def type_byte(function: str, side: str) -> int:
    if function == HEAD:
        return 0x01
    if function == POSITION:
        return 0x03
    if function == TAIL:
        return 0x18
    if function == BRAKE:
        return 0x02
    if function == REVERSE:
        return 0x08
    if function == FOG:
        return 0x0C
    if function == INDICATOR:
        return 0x05 if side == "R" else 0x04
    if function == BRAKE_IND:
        return 0x1A if side == "R" else 0x19
    if function == DRL_IND:
        return 0x15 if side == "R" else 0x14
    raise ValueError(function)


def variant_byte(function: str) -> int:
    if function in (INDICATOR, BRAKE_IND, DRL_IND):
        return 2
    if function == BRAKE:
        return 1
    return 0


@dataclass
class Lamp:
    """One lamp: the triangles of one function on one side at one end.
    Position and direction come from the finest Forza LOD that has it."""
    function: str
    side: str          # "L" (DSF +x, the car's left), "R" or "C"
    front: bool
    colour: int
    per_lod: dict = field(default_factory=dict)   # lod -> [area, centre sum xyz, normal sum xyz, triangles]
    light_id: int | None = None   # host light entry id; vertices get id + 1
    cluster: int = 0              # 0 = the largest lamp of its function, side and end

    def measure(self, lod: int, area: float, centre, normal) -> None:
        acc = self.per_lod.setdefault(lod, [0.0, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0])
        acc[0] += area
        for i in range(3):
            acc[1][i] += centre[i] * area
            acc[2][i] += normal[i] * area  # area-weighted, so small facets don't tilt it
        acc[3] += 1

    @property
    def _best(self):
        return self.per_lod[min(self.per_lod)] if self.per_lod else [0.0, [0.0] * 3, [0.0] * 3, 0]

    @property
    def area(self) -> float:
        return self._best[0]

    @property
    def triangles(self) -> int:
        return self._best[3]

    @property
    def key(self) -> tuple:
        return (self.function, self.side, self.front, self.cluster)

    @property
    def position(self) -> tuple[float, float, float]:
        area, centre = self._best[0] or 1.0, self._best[1]
        return tuple(c / area for c in centre)

    @property
    def direction(self) -> tuple[float, float, float]:
        if self.function == HEAD:
            return (0.0, 0.0, 1.0)
        n = self._best[2]
        length = math.sqrt(sum(c * c for c in n))
        if length < 1e-6:
            return (0.0, 0.0, 1.0 if self.front else -1.0)
        return tuple(c / length for c in n)

    @property
    def flags(self) -> int:
        return type_byte(self.function, self.side) << 24 | self.colour << 16 | variant_byte(self.function)


def _tri_geometry(pts, normals):
    a = [pts[1][i] - pts[0][i] for i in range(3)]
    b = [pts[2][i] - pts[0][i] for i in range(3)]
    cross = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    area = 0.5 * math.sqrt(sum(c * c for c in cross))
    centre = tuple(sum(p[i] for p in pts) / 3 for i in range(3))
    normal = tuple(sum(n[i] for n in normals) for i in range(3))
    return area, centre, normal


LENS_COLOUR = {"detail_glass_red": RED, "detail_glass_amber": AMBER, "linred": RED, "rinred": RED}
CLUSTER_GAP = 0.2  # metres between triangle centres that still belong to one lamp
CLEAR_LENS_REACH = 0.35  # m: a clear lens lights with the nearest lamp this close


@dataclass
class Tri:
    sub: object
    k: int
    function: str
    side: str
    front: bool
    colour: int
    area: float
    centre: tuple
    normal: tuple
    lod: int


def _single_link(points, gap: float) -> list[list[int]]:
    """Indices of `points` grouped so that points closer than `gap` share a group."""
    cells: dict[tuple, list[int]] = {}
    for i, p in enumerate(points):
        cells.setdefault(tuple(int(math.floor(c / gap)) for c in p), []).append(i)
    parent = list(range(len(points)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for (cx, cy, cz), members in cells.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in cells.get((cx + dx, cy + dy, cz + dz), ()):
                        for i in members:
                            if i < j and math.dist(points[i], points[j]) < gap:
                                parent[root(i)] = root(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(points)):
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())


def _high_brake(records) -> None:
    """A centre tail lamp above the side tail lamps (or the only tail lamp in
    the middle) is the high-mounted stop lamp: brakes only."""
    def mean_y(rs):
        finest = min(r.lod for r in rs)
        rs = [r for r in rs if r.lod == finest]
        return sum(r.centre[1] * r.area for r in rs) / (sum(r.area for r in rs) or 1.0)
    centre = [r for r in records if r.function == TAIL and r.side == "C" and not r.front]
    sides = [r for r in records if r.function == TAIL and r.side != "C" and not r.front]
    if centre and sides and mean_y(centre) > mean_y(sides) + HIGH_BRAKE_RISE:
        for r in centre:
            r.function = BRAKE


@dataclass
class LampPlan:
    """Lamp per Forza triangle, and the host light entries the lamps use.

    Build with `LampPlan.build(sections, place, mid_z)`, then `assign(upvh)`.
    `s9(sub, k, s9)` gives a triangle's vertex semantic-9 bytes with the light
    byte set; `patch_upvh(upvh)` rewrites the light table."""
    lamps: dict = field(default_factory=dict)       # key -> Lamp
    tri_lamp: dict = field(default_factory=dict)    # (id(sub), triangle index) -> lamp key
    unlit: list = field(default_factory=list)       # lamp keys that got no entry
    slots: list = field(default_factory=list)       # per host light entry: (offset, id, Lamp | None)

    @classmethod
    def build(cls, sections, place, mid_z: float, lenses=frozenset()) -> "LampPlan":
        """sections: Forza body sections (all LODs). place: forza2dsf.Placement.
        mid_z: DSF z of the wheelbase midpoint (front lamps are ahead of it).
        lenses: id(sub) of lamp-cover glass subsections drawn as lamp texels
        (dsf_materials.Texturing.lens_subs); they light like clear lenses,
        with the lamp behind each triangle (the DMC-12's grid lens: tail,
        indicator and reverse cells over one reflector)."""
        plan = cls()
        records, clear = [], []  # clear lenses are placed after the lamps
        for section in sections:
            for sub in section.subsections:
                entry = TECHNIQUE_FUNCTION.get(sub.material)
                if entry is None and id(sub) in lenses:
                    entry = (LENS, None)
                if entry is None:
                    continue
                function, named_side = entry
                pool = section.vertices_for(sub)
                tris = list(sub.triangles())
                if not tris:
                    continue
                pts_all = [[place.point(pool[i].position) for i in t] for t in tris]
                max_x = max(abs(p[0]) for pts in pts_all for p in pts)
                colour = TECHNIQUE_COLOUR.get(sub.material, FUNCTION_COLOUR.get(function, WHITE))
                if function == LENS:
                    colour = LENS_COLOUR.get(sub.material, WHITE)
                for k, (t, pts) in enumerate(zip(tris, pts_all)):
                    normals = [place.normal(pool[i].normal) for i in t]
                    area, centre, normal = _tri_geometry(pts, normals)
                    front = centre[2] > mid_z
                    if named_side:
                        side = named_side
                    elif max_x < CENTRE_X:
                        side = "C"
                    else:
                        side = "L" if centre[0] > 0 else "R"
                    f, c = function, colour
                    if f == POSITION:
                        c = WHITE if front else RED  # front parking / DRL, rear tail marker
                    if f == LENS:
                        # Amber lenses are indicators, red ones at the rear tail lamps;
                        # clear ones light with the lamp behind them.
                        if c == AMBER:
                            f = INDICATOR
                            side = side if side != "C" else ("L" if centre[0] > 0 else "R")
                        elif c == RED and not front:
                            f = TAIL
                        else:
                            clear.append(Tri(sub, k, LENS, side, front, c, area, centre, normal, sub.lod))
                            continue
                    records.append(Tri(sub, k, f, side, front, c, area, centre, normal, sub.lod))
        _high_brake(records)
        plan._cluster(records)
        plan._place_clear(clear)
        return plan

    def _cluster(self, records) -> None:
        """Group triangles by (function, side, end) and split each group into
        lamps CLUSTER_GAP apart on its finest LOD: a side repeater is not the
        headlamp's indicator, a bumper driving lamp not the headlamp. Other
        LODs' triangles join the nearest lamp of their group."""
        groups: dict[tuple, list] = {}
        for r in records:
            groups.setdefault((r.function, r.side, r.front), []).append(r)
        for (function, side, front), rs in groups.items():
            finest = min(r.lod for r in rs)
            base = [r for r in rs if r.lod == finest]
            lamps = []
            for members in _single_link([r.centre for r in base], CLUSTER_GAP):
                lamp = Lamp(function, side, front, base[members[0]].colour)
                for m in members:
                    lamp.measure(finest, base[m].area, base[m].centre, base[m].normal)
                lamps.append(lamp)
            # The main lamp first: the largest, but for indicators the one at the
            # car's end (the headlamp or tail-lamp indicator, not the side repeater).
            lamps.sort(key=lambda l: -abs(l.position[2]) if function == INDICATOR else -l.area)
            for n, lamp in enumerate(lamps):
                lamp.cluster = n
                self.lamps[lamp.key] = lamp
            anchors = [(lamp, lamp.position) for lamp in lamps]
            for r in rs:
                lamp = min(anchors, key=lambda a: math.dist(a[1], r.centre))[0]
                if r.lod != finest:
                    lamp.measure(r.lod, r.area, r.centre, r.normal)
                self.tri_lamp[(id(r.sub), r.k)] = lamp.key

    def _place_clear(self, clear) -> None:
        """Clear lenses light with the nearest lamp at the same end (within
        CLEAR_LENS_REACH), else stay unlit."""
        for r in clear:
            best, best_d = None, CLEAR_LENS_REACH
            for lamp in self.lamps.values():
                if lamp.front != r.front or not lamp.area:
                    continue
                d = math.dist(lamp.position, r.centre)
                if d < best_d:
                    best, best_d = lamp, d
            if best is not None:
                self.tri_lamp[(id(r.sub), r.k)] = best.key

    # -- host light entries --
    def assign(self, upvh: bytes) -> "LampPlan":
        """Give lamps the host's light entries, most important first. Lamps
        left over share an entry of the same function and side if there is
        one (their geometry lights with it), else stay unlit."""
        from dsfforza.formats import upvh as uv
        h = uv.parse(upvh)
        base = h.offsets["lights"]
        entries = []
        for i in range(h.counts["lights"]):
            o = base + i * 0x40
            entries.append((o, struct.unpack_from("<I", h.data, o + 40)[0]))
        order = sorted((l for l in self.lamps.values() if l.area),
                       key=lambda l: (PRIORITY.get(l.function, 9) + 10 * min(l.cluster, 1), not l.front, l.side))
        free = list(entries)
        taken = {}
        for lamp in order:
            if free:
                o, lid = free.pop(0)
                lamp.light_id = lid
                taken[(o, lid)] = lamp
        for lamp in order:
            if lamp.light_id is None:
                twin = next((l for l in order if l.light_id is not None and l.function == lamp.function
                             and l.side == lamp.side), None)
                if twin is not None:
                    lamp.light_id = twin.light_id
                else:
                    self.unlit.append(lamp.key)
        self.slots = [(o, lid, taken.get((o, lid))) for o, lid in entries]
        return self

    def light_byte(self, sub, k) -> int:
        key = self.tri_lamp.get((id(sub), k))
        if key is None:
            return 0
        lamp = self.lamps.get(key)
        return 0 if lamp is None or lamp.light_id is None else lamp.light_id + 1

    def s9(self, sub, k, s9: bytes) -> bytes:
        b = self.light_byte(sub, k)
        if not b:
            return s9
        return s9[:2] + bytes([b]) + s9[3:]

    def patch_upvh(self, upvh: bytes) -> bytes:
        """Rewrite position, direction and flags of every host light entry:
        used ones from their lamp, spare ones off (flags 0) at the nearest lamp
        of the same end (or where they were)."""
        data = bytearray(upvh)
        used = [lamp for _, _, lamp in self.slots if lamp is not None]
        for o, lid, lamp in self.slots:
            if lamp is not None:
                struct.pack_into("<3f", data, o, *lamp.position)
                struct.pack_into("<3f", data, o + 16, *lamp.direction)
                struct.pack_into("<I", data, o + 44, lamp.flags)
                continue
            old = struct.unpack_from("<3f", data, o)
            front = old[2] > 0
            near = [l for l in used if l.front == front] or used
            if near:
                park = min(near, key=lambda l: math.dist(l.position, old))
                struct.pack_into("<3f", data, o, *park.position)
                struct.pack_into("<3f", data, o + 16, *park.direction)
            struct.pack_into("<I", data, o + 44, 0)
        return bytes(data)

    def report(self) -> dict:
        lamps = []
        for lamp in sorted(self.lamps.values(), key=lambda l: (not l.front, l.function, l.side)):
            mask = LIGHT_TYPE_MASK[type_byte(lamp.function, lamp.side)]
            lamps.append({"function": lamp.function, "side": lamp.side, "front": lamp.front,
                          "light_id": lamp.light_id, "flags": f"{lamp.flags:#010x}",
                          "lua": "+".join(n for b, n in MASK_NAMES.items() if mask & b),
                          "position": [round(c, 3) for c in lamp.position], "triangles": lamp.triangles})
        return {"lamps": lamps, "unlit": [list(k) for k in self.unlit],
                "spare_entries": sum(1 for _, _, l in self.slots if l is None)}


def read_table(upvh: bytes) -> list[dict]:
    from dsfforza.formats import upvh as uv
    h = uv.parse(upvh)
    out = []
    for i in range(h.counts["lights"]):
        o = h.offsets["lights"] + i * 0x40
        pos = struct.unpack_from("<3f", h.data, o)
        direction = struct.unpack_from("<3f", h.data, o + 16)
        lid, flags = struct.unpack_from("<2I", h.data, o + 40)
        t = flags >> 24
        mask = LIGHT_TYPE_MASK[t] if t < len(LIGHT_TYPE_MASK) else None
        out.append({"index": i, "id": lid, "flags": flags, "position": pos, "direction": direction,
                    "lua": "+".join(n for b, n in MASK_NAMES.items() if mask and mask & b) or "-"})
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="classify a Forza car's lamps on a stock host (default the Desanne)")
    p.add_argument("car", type=Path)
    p.add_argument("--dsf", type=Path, default=None, help="stock dngvehicles.sp (default: from $DSF_GAME_DIR)")
    p.add_argument("--host", type=int, default=266)
    t = sub.add_parser("table", help="print a vehicle's light table")
    t.add_argument("archive", type=Path)
    t.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)
    if getattr(args, 'dsf', None):
        args.dsf = game_paths.stock(args.dsf)  # the original when a dsfforza install replaced it

    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats import upvh as uv
    archive = args.dsf if args.cmd == "plan" else args.archive
    spool = args.host if args.cmd == "plan" else args.id
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        full = uv.entries_for(stream, root.entries, spool, None)[0]
        upvh = vm.payload(stream, vm.child(vm.child(full, "NONR"), "UPVH"))
        modl = vm.payload(stream, vm.child(vm.child(full, "NONR"), "MODL"))
    if args.cmd == "table":
        for e in read_table(upvh):
            print(f"{e['index']:2} id {e['id']:2} flags {e['flags']:#010x} {e['lua']:28} "
                  f"pos ({', '.join(f'{c:+.2f}' for c in e['position'])}) "
                  f"dir ({', '.join(f'{c:+.2f}' for c in e['direction'])})")
        return
    from dsfforza.convert import forza2dsf
    car = forza2dsf.load_car(args.car, args.car.name)
    dims = forza2dsf.car_dims(car)
    # Placed on the host's own wheels, as forza2dsf.convert does (this used the
    # Desanne's hub height and axle midpoint whatever --host was).
    hw = forza2dsf.host_wheels(upvh, modl)
    place = forza2dsf.Placement(dy=hw.hub_y - dims.radius + dims.ride_height, dz=hw.axle_mid + dims.mid_z)
    plan = LampPlan.build(car.body, place, hw.axle_mid).assign(upvh)
    import json
    print(json.dumps(plan.report(), indent=1))


if __name__ == "__main__":
    sys.exit(main())
