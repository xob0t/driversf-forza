"""DSF's side of the cockpit: the host's hierarchy and gauge records, the driver's hands on the
rim, the seats and the head camera."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from dsfforza.convert import forza_materials as fm
from dsfforza.convert.cockpit.geometry import (
    GAUGE_IDS,
    GAUGE_UNITS,
    STEERING_MARKER_ROLE,
    CockpitFit,
    _add,
    _dot,
    _mean,
    _norm,
    _scale,
    _sub,
    quat_from_to,
    quat_rotate,
    rotate_about,
    section_points,
)
from dsfforza.formats import carbin

# --- DSF hierarchy and camera --------------------------------------------------------


@dataclass
class HostCockpit:
    """Where the host keeps its cockpit animation (from the full entry's UPVH)."""

    wheel_record: int
    wheel_hinge: int
    wheel_marker: int  # the first steering marker: the head camera's (FUN_0088D3B0)
    wheel_skin: bytes  # vertex semantic 9 for steering-wheel vertices
    gauge_slots: dict[int, int]  # gauge id -> gauge ("seat") table index
    gauge_parent_hinge: int | None
    mirror_slots: list[int]
    # Every steering marker, in record order. FUN_0088E310 keeps the last one for the
    # wheel, the cockpit arms and the chase views' driver; the head camera reads the
    # first. Host 132 has two (research/cockpit.md, "Hosts with two steering markers").
    wheel_markers: tuple[int, ...] = ()

    @property
    def engine_marker(self) -> int:
        """The steering marker the engine turns the wheel and the hands about."""
        return self.wheel_markers[-1] if self.wheel_markers else self.wheel_marker


def parent_of(h, index: int) -> int | None:
    for j in range(index - 1, -1, -1):
        if j + h.records[j].subtree > index:
            return j
    return None


def host_cockpit(upvh_data: bytes) -> HostCockpit:
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    markers = [r for r in h.records if r.type == 3 and r.role == STEERING_MARKER_ROLE and r.marker >= 0]
    if not markers:
        raise ValueError("no steering marker (record type 3, role 0x2F) in the hierarchy")
    marker = markers[0].marker
    mpos = struct.unpack_from("<3f", h.table("markers")[marker])
    best = None
    for r in h.records:
        if r.hinge < 0 or r.type not in (1, 5):
            continue
        pivot = struct.unpack_from("<3f", h.table("hinges")[r.hinge])
        d = math.dist(pivot, mpos)
        if best is None or d < best[0]:
            best = (d, r)
    if best is None or best[0] > 0.05:
        raise ValueError("no hinge at the steering marker")
    rec = best[1]
    slot = max(0, struct.unpack_from("<h", rec.raw, 0x34)[0])
    model = max(0, struct.unpack_from("<h", rec.raw, 0x2A)[0])
    gauge_slots, parent_hinge = {}, None
    for r in h.records:
        if r.type == 18 and r.seat >= 0:
            gid = struct.unpack_from("<I", h.table("seats")[r.seat], 32)[0]
            gauge_slots[gid] = r.seat
            p = parent_of(h, r.index)
            if p is not None and h.records[p].hinge >= 0:
                parent_hinge = h.records[p].hinge
    return HostCockpit(
        rec.index,
        rec.hinge,
        marker,
        bytes((slot & 0xFF, model & 0xFF, 0, 1)),
        gauge_slots,
        parent_hinge,
        list(range(h.counts["mirrors"])),
        tuple(r.marker for r in markers),
    )


# Vanilla gauge records (type 18) are leaves under the cluster part (role 185):
# the speedometer's has role 0, the tachometer's 187, and every record field
# but the role and the gauge-table index is the same (144 speedometers, 126
# tachometers over the 150 vanilla hierarchies). FUN_0088E310 keeps, per gauge
# table index (at most 7), the record of each type-18 record whose role isn't
# 185 and whose entry's gauge id is below 8; the render loop turns a needle
# through every kept record's entry (research/cockpit.md, "Gauges").
GAUGE_RECORD_TYPE = 18
CLUSTER_ROLE = 185
GAUGE_ROLES = {2: 0, 1: 187}  # gauge id -> the vanilla record's role
MAX_GAUGES = 7


def gauge_records(h) -> dict[int, int]:
    """Gauge id -> record index of the records the engine keeps (FUN_0088E310)."""
    out = {}
    seats = h.table("seats")
    for r in h.records:
        if r.type == GAUGE_RECORD_TYPE and r.role != CLUSTER_ROLE and 0 <= r.seat < len(seats):
            gid = struct.unpack_from("<I", seats[r.seat], 0x20)[0]
            if gid < 8:
                out.setdefault(gid, r.index)
    return out


def add_gauge_records(upvh_data: bytes, ids) -> tuple[bytes, list[int]]:
    """Give the hierarchy a gauge record and gauge-table entry for each id in
    `ids` it lacks, so the engine turns that needle too. 19 vanilla hosts carry a
    speedometer only (146 the 1968 Camaro, 230, 249...), 225 (MP4-12C) a
    tachometer only. The new record is a copy of the host's own gauge record
    (same parent, the cluster part; the vanilla role for the id), the speedometer's
    before the tachometer's as in vanilla hierarchies; its entry is a copy of
    that record's entry with the new id, appended to the table (patch_hierarchy
    sets the pivot, dial and sweep). A host without any gauge record (250,
    the Volt's, and 293) gets none: its only dial material is a digital one
    (`*_Digital_Dials`), whose program doesn't read g_InstrumentTransforms, so no
    needle could turn there (research/cockpit.md, "Displays on the steering wheel").
    Returns the new hierarchy and the ids added."""
    from dsfforza.convert import damage
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    have = gauge_records(h)
    missing = sorted(set(ids) - set(have), reverse=True)  # speedometer (2) first
    if not have or not missing:
        return upvh_data, []
    data = upvh_data
    added = []
    for gid in missing:
        h = upvh.parse(data)
        have = gauge_records(h)
        if h.counts["seats"] >= MAX_GAUGES or gid not in GAUGE_ROLES:
            break
        src = h.records[min(have.values())]
        if src.subtree != 1:
            break
        seat = h.counts["seats"]
        rec = bytearray(src.raw)
        struct.pack_into("<H", rec, 0x0A, GAUGE_ROLES[gid])
        struct.pack_into("<b", rec, 0x26, seat)
        at = src.index + 1 if gid == 1 else src.index
        records = bytearray(b"".join(r.raw for r in h.records))
        records[at * upvh.RECORD_SIZE : at * upvh.RECORD_SIZE] = rec
        # Every ancestor's subtree grows by one (they all come before `at`).
        p = parent_of(h, src.index)
        while p is not None:
            o = p * upvh.RECORD_SIZE + 0x14
            struct.pack_into("<H", records, o, struct.unpack_from("<H", records, o)[0] + 1)
            p = parent_of(h, p)
        # The fields that follow the tree's shape (damage._tree_fields: +0x0C bit 0 and
        # the hinge pop counts +0x16/+0x18) are written again for the new tree on every
        # record that followed them before. The record that ended the cluster's subtree
        # popped its hinge there; a leaf before the new one pops nothing (#266).
        raw = [records[k * upvh.RECORD_SIZE : (k + 1) * upvh.RECORD_SIZE] for k in range(len(h.records) + 1)]
        old = [r.raw for r in h.records]
        before = damage._tree_fields(old)
        source = [k if k < at else k - 1 for k in range(len(raw))]
        source[at] = src.index
        for k, (r, fields) in enumerate(zip(raw, damage._tree_fields(raw), strict=True)):
            if damage._read_tree_fields(old[source[k]]) == before[source[k]]:
                struct.pack_into("<H", r, 0x0C, (struct.unpack_from("<H", r, 0x0C)[0] & ~1) | fields[0])
                struct.pack_into("<HH", r, 0x16, *fields[1:])
        records = b"".join(raw)
        entry = bytearray(h.table("seats")[src.seat])
        struct.pack_into("<I", entry, 0x20, gid)
        tables = upvh.HEADER_SIZE + len(h.records) * upvh.RECORD_SIZE
        seats_end = h.offsets["seats"] + seat * 0x30
        out = bytearray(data[: upvh.HEADER_SIZE] + records + data[tables:seats_end] + entry + data[seats_end:])
        struct.pack_into("<I", out, 0x10, len(h.records) + 1)
        out[0x2C] = seat + 1
        data = bytes(out)
        added.append(gid)
    return data, added


# --- driver's hands -------------------------------------------------------------------
# The cockpit hands are one rig for every car (research/cockpit.md, "Driver's hands").
# In game (run 20261024-000936) the hands sit at A + H and the skinned wheel at
# A + (v - hinge), where A = steering marker (role 47) + (0, VO3D p538, p559)
# (FUN_005743E0) and H is fixed. When the car steers, both turn about A, about the
# marker's column (FUN_005756E0 -> FUN_00514150). So the marker and p538/p559
# move the wheel and the hands together. Only the hinge, the point of the wheel
# mesh drawn at A, moves the hands against the wheel. The marker's quaternion
# leaves both alone at rest. The rig fits the vanilla wheel: in every vanilla car the
# hinge is the centre of the same wheel mesh, rim radius 0.176 m (fleet median of
# the fit_wheel measure; quartiles 0.174 to 0.180), column 24 deg up. The hands
# grip it about 45 deg either side of the top.
HAND_PARAMS = (538, 559)
VANILLA_RIM_RADIUS = 0.176
VANILLA_COLUMN = (0.0, 0.410, -0.912)  # DSF model space, towards the driver
HAND_GRIP_DEG = 45.0
MAX_HAND_ALONG = 0.12  # m, along the column (no orbit; the driver model moves with it)
MAX_SEAT_SHIFT = 0.06  # m, in the wheel plane: grip_shift for the seat (the chase view's driver)


def _rim_nearest(p, centre, axis, radius):
    d = _sub(p, centre)
    d = _sub(d, _scale(axis, _dot(d, axis)))
    return _add(centre, _scale(_norm(d), radius))


def hand_grips(pivot) -> list[tuple]:
    """Where the vanilla hand rig grips when the steering pivot (hinge = marker,
    p538/p559 zero) is at `pivot` (DSF model space)."""
    a = _norm(VANILLA_COLUMN)
    up = _norm(_sub((0.0, 1.0, 0.0), _scale(a, a[1])))
    t = math.radians(HAND_GRIP_DEG)
    return [
        _add(
            pivot,
            _add(
                _scale((1.0, 0.0, 0.0), s * VANILLA_RIM_RADIUS * math.sin(t)),
                _scale(up, VANILLA_RIM_RADIUS * math.cos(t)),
            ),
        )
        for s in (1, -1)
    ]


def hand_shift(centre, axis, radius) -> tuple[float, float, float]:
    """Offset from the Forza wheel centre for the steering hinge and marker,
    along the column only: the least-squares move along the axis that brings the
    vanilla hand grips to the rim's plane, capped at MAX_HAND_ALONG (it moves the
    driver model with the seat, patch_seats). The engine turns the wheel about
    the marker's direction through the hinge, so any part of the shift in the
    wheel plane makes the hub circle by 2 |shift| sin(angle / 2) while steering
    (research/cockpit.md, "The steering wheel turns about its hub"); the grips
    reach the rim through dsfforza.asi [hands] instead (grip_params)."""
    axis = _norm(axis)
    grips = hand_grips(centre)
    t = 0.0
    for _ in range(8):
        shift = _scale(axis, t)
        moves = [_sub(_rim_nearest(_add(g, shift), centre, axis, radius), _add(g, shift)) for g in grips]
        t += _dot(_mean(moves), axis)
    t = max(-MAX_HAND_ALONG, min(MAX_HAND_ALONG, t))
    return _scale(axis, t)


def grip_shift(centre, axis, radius) -> tuple[float, float, float]:
    """Translation (y and z) from the wheel centre that puts the vanilla grips on
    the rim (least squares), with the in-plane part capped at MAX_SEAT_SHIFT and
    the part along the column at MAX_HAND_ALONG. Not for the hinge (the wheel
    would turn about an off-hub point): it places the driver's seat, whose
    animated driver holds the wheel at the vanilla point of the seat's frame in
    the chase views (seat_anchor, patch_seats). The same translation placed the
    hinge and marker before 2026-10-26."""
    axis = _norm(axis)
    grips = hand_grips(centre)
    shift = (0.0, 0.0, 0.0)
    for _ in range(8):
        moves = [_sub(_rim_nearest(_add(g, shift), centre, axis, radius), _add(g, shift)) for g in grips]
        shift = _add(shift, _mean(moves))
        shift = (0.0, shift[1], shift[2])
    along = _scale(axis, _dot(shift, axis))
    plane = _sub(shift, along)
    n = math.sqrt(_dot(plane, plane))
    if n > MAX_SEAT_SHIFT:
        plane = _scale(plane, MAX_SEAT_SHIFT / n)
    n = math.sqrt(_dot(along, along))
    if n > MAX_HAND_ALONG:
        along = _scale(along, MAX_HAND_ALONG / n)
    shift = _add(plane, along)
    return (0.0, shift[1], shift[2])


SEAT_FIT_TOLERANCE = 0.005  # m
SEAT_SCALE_RANGE = (0.8, 1.3)


def seat_anchor(centre, axis, forza_radius) -> tuple[float, float, float]:
    """Where the driver's seat holds the wheel (patch_seats): the point whose
    vanilla grips land on Forza's rim, as the hinge and marker were placed up to
    2026-10-26 (grip_shift on the rim scaled the way it was then: by 0.8 to 1.3
    where that shift left the grips over 5 mm off). Kept so fixing the hinge
    doesn't move the driver model: the head room stays. The chase views' wheel
    keeps Forza's size; dsfforza.asi [hands] puts the hands on its rim at the
    angle the animations give them about this point, so they turn with the
    wheel about its hub (research/cockpit.md, "Hands in the chase views")."""
    axis = _norm(axis)
    k = 1.0
    for _ in range(6):
        pivot = _add(centre, grip_shift(centre, axis, forza_radius * k))
        if hand_grip_error(pivot, centre, axis, forza_radius * k) <= SEAT_FIT_TOLERANCE:
            break
        reach = []
        for g in hand_grips(pivot):
            d = _sub(g, centre)
            d = _sub(d, _scale(axis, _dot(d, axis)))
            reach.append(math.sqrt(_dot(d, d)))
        new = min(SEAT_SCALE_RANGE[1], max(SEAT_SCALE_RANGE[0], sum(reach) / len(reach) / forza_radius))
        if abs(new - k) < 1e-4:
            break
        k = new
    return _add(centre, grip_shift(centre, axis, forza_radius * k))


def hand_grip_error(pivot, centre, axis, radius) -> float:
    """Largest distance (m) from a vanilla grip to the Forza rim."""
    return max(math.dist(g, _rim_nearest(g, centre, _norm(axis), radius)) for g in hand_grips(pivot))


def hand_params(steering: dict | None = None) -> dict[int, float]:
    """VO3D p538/p559 (the hand rig's offset from the marker) to zero: they move
    the wheel with the hands, away from where Forza puts it (Cuda host:
    -4 cm y, +7.5 cm z). With patch_hierarchy's steering report, also the rim
    dsfforza.asi [hands] puts the driver's hands on (GRIP_PARAMS)."""
    params = dict.fromkeys(HAND_PARAMS, 0.0)
    if steering:
        params.update(grip_params(steering))
    return params


# dsfforza.asi [hands] (tools/asi/src/mod_hands.cpp) moves the driver's hands onto each
# car's rim (research/cockpit.md, "Hands on Forza's rim"). It reads the rim from VO3D
# parameters that nothing in the game uses: type 3 ("unused") in the parameter type table
# at 0x00F3E450, no displacement or immediate reference in Driver.exe at either base
# (spec + 0x2C70, block + 0x2960), set by no vanilla VO3D. Vanilla cars leave them at 0 and
# keep the vanilla grips.
GRIP_RADIUS_PARAM = 95  # the drawn rim's radius (m)
GRIP_ALONG_PARAM = 87  # the rim centre from the steering marker along the engine's column direction (m)
GRIP_PARAMS = (GRIP_RADIUS_PARAM, GRIP_ALONG_PARAM)


def grip_params(steering: dict) -> dict[int, float]:
    """VO3D values for dsfforza.asi [hands] from patch_hierarchy's steering report:
    the rim's radius and its centre's offset from the marker along the column (the
    marker sits on the hub's column, hand_shift). The engine's column direction
    (spec + 0x37F0) is the marker quaternion's +z: away from the driver, the
    opposite of the report's axis. The centre is the marker minus the shift."""
    shift = steering["hands"]["shift"]
    axis = _norm(steering["axis"])
    return {GRIP_RADIUS_PARAM: float(steering["radius"]), GRIP_ALONG_PARAM: _dot(shift, axis)}


def patch_hierarchy(upvh_data: bytes, host: HostCockpit, place, cf: CockpitFit) -> tuple[bytes, dict]:
    """Move the steering hinge and marker, the gauges and the mirrors to the
    Forza cockpit (all in DSF model space through `place`). The hinge and
    marker sit at the wheel centre plus hand_shift (along the column, so the
    wheel turns about its own hub); dsfforza.asi [hands] moves the vanilla hands
    onto the Forza rim (hand_params in VO3D)."""
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    data = bytearray(upvh_data)
    report = {}
    centre = place.point(cf.wheel.centre)
    axis = _norm(place.normal(cf.wheel.axis))
    hinge_at = h.offsets["hinges"] + host.wheel_hinge * 0x30
    old_axis = struct.unpack_from("<3f", data, hinge_at + 0x10)
    if _dot(axis, old_axis) < 0:
        axis = _scale(axis, -1)
    shift = hand_shift(centre, axis, cf.wheel.radius)
    pivot = _add(centre, shift)
    struct.pack_into("<3f", data, hinge_at, *pivot)
    struct.pack_into("<3f", data, hinge_at + 0x10, *axis)
    # The vanilla marker quaternion turns (0, 0, -1) onto the column (Desanne: 24.4 deg about x);
    # the engine turns the wheel and hands about it.
    q = quat_from_to((0, 0, -1), axis)
    # Every steering marker: the head camera reads the first, the wheel and the hands the
    # last (host 132 has two; moving only the first left them at the host's wheel).
    for marker in host.wheel_markers or (host.wheel_marker,):
        marker_at = h.offsets["markers"] + marker * 0x20
        struct.pack_into("<3f", data, marker_at, *pivot)
        struct.pack_into("<4f", data, marker_at + 0x10, *q)
    along = _dot(shift, axis)
    seat = seat_anchor(centre, axis, cf.wheel.radius)
    report["steering"] = {
        "record": host.wheel_record,
        "hinge": host.wheel_hinge,
        "marker": host.wheel_marker,
        **({"markers": list(host.wheel_markers)} if len(host.wheel_markers) > 1 else {}),
        "centre": [round(c, 4) for c in centre],
        "axis": [round(c, 4) for c in axis],
        "radius": round(cf.wheel.radius, 4),
        "skin": list(host.wheel_skin),
        "seat_anchor": [round(c, 5) for c in seat],  # patch_seats: where the driver model holds the wheel
        "hands": {
            "shift": [round(c, 4) for c in shift],
            "along_column": round(along, 4),
            "in_plane": round(math.sqrt(max(0.0, _dot(shift, shift) - along * along)), 4),
            "grip_error": [
                round(hand_grip_error(centre, centre, axis, cf.wheel.radius), 4),
                round(hand_grip_error(pivot, centre, axis, cf.wheel.radius), 4),
            ],
        },
    }
    moved = []
    for family, gid in GAUGE_IDS.items():
        g = cf.gauges.get(family)
        if g is None or gid not in host.gauge_slots:
            continue
        at = h.offsets["seats"] + host.gauge_slots[gid] * 0x30
        pivot = place.point(g.pivot)
        normal = _norm(place.normal(g.normal))
        cal = cf.calibration.get(family)
        if cal is not None and cal.reverse:
            normal = _scale(normal, -1)  # vanilla counter-clockwise needles (Pacer, Aston tachs) do this
        # Vanilla gauge quaternions turn (0, 0, 1) onto the dial normal (Desanne: 21.8 deg about x);
        # the engine turns the needle right-handed about it.
        struct.pack_into("<3f", data, at, *pivot)
        struct.pack_into("<4f", data, at + 0x10, *quat_from_to((0, 0, 1), normal))
        old_sweep = struct.unpack_from("<I", data, at + 0x24)[0]
        if cal is not None:
            struct.pack_into("<I", data, at + 0x24, cal.sweep)
        moved.append(pivot)
        entry = {
            "id": gid,
            "pivot": [round(c, 4) for c in pivot],
            "normal": [round(c, 4) for c in normal],
            "forza_sweep_deg": round(g.sweep_deg, 1),
            "host_sweep": old_sweep,
        }
        if cal is not None:
            entry.update(
                {
                    "sweep": cal.sweep,
                    "unit": GAUGE_UNITS[family],
                    "reverse": cal.reverse,
                    "rest_deg": round(cal.rest_deg, 2),
                    "dial_max": cal.dial_max,
                    "residual_deg": cal.residual_deg,
                    "source": cal.source,
                }
            )
        report.setdefault("gauges", {})[family] = entry
    if moved and host.gauge_parent_hinge is not None:
        at = h.offsets["hinges"] + host.gauge_parent_hinge * 0x30
        struct.pack_into("<3f", data, at, *_mean(moved))
    for slot in host.mirror_slots:
        at = h.offsets["mirrors"] + slot * 0x10
        x = struct.unpack_from("<f", data, at)[0]
        # DSF +x is the driver's left (Forza -x after the turn).
        side = "left" if x > 0 else "right"
        if side in cf.mirrors:
            struct.pack_into("<3f", data, at, *place.point(cf.mirrors[side]))
            report.setdefault("mirrors", {})[side] = [round(c, 4) for c in place.point(cf.mirrors[side])]
    return bytes(data), report


DRIVER_SEAT_ROLE, PASSENGER_SEAT_ROLE = 10, 11
# The driver model in the chase views is one animated rig placed on the driver seat
# marker (role 10), hands included: they hold the wheel only where the vanilla wheel is
# in the seat's frame (research/cockpit.md, "The driver's seat follows the wheel").
# 138 of 147 vanilla cars put the steering marker (role 47) at this point of the seat
# marker's frame within 1 cm, all but two within 2 cm (the seat tilt, 0 or -5 deg,
# turns the frame; `dsff driver_survey vanilla`).
VANILLA_MARKER_FROM_SEAT = (0.0, 0.469, 0.428)
# Head-camera eye in the seat marker's frame, vanilla median (y 0.642..0.841, z -0.286..0.129):
# about where the driver model's eyes are.
FLEET_EYE_ABOVE_SEAT = (0.0, 0.738, -0.069)


def seat_markers(upvh_data: bytes) -> dict[int, int]:
    """Role (10 driver, 11 passenger) -> marker table index."""
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    out = {}
    for r in h.records:
        if r.type == 3 and r.marker >= 0 and r.role in (DRIVER_SEAT_ROLE, PASSENGER_SEAT_ROLE):
            out.setdefault(r.role, r.marker)
    return out


def seat_for_marker(marker, seat_quat) -> tuple[float, float, float]:
    """The driver seat position that puts the steering marker where vanilla cars have
    it in the seat's frame (VANILLA_MARKER_FROM_SEAT), for a seat turned by seat_quat."""
    return _sub(marker, quat_rotate(seat_quat, VANILLA_MARKER_FROM_SEAT))


def patch_seats(upvh_data: bytes, eye_dsf=None, anchor=None) -> tuple[bytes, dict]:
    """Move the driver's seat marker (role 10, where the game seats the driver
    model) so `anchor` (default: the steering marker) sits where the steering
    marker does in vanilla cars, in the seat's frame (VANILLA_MARKER_FROM_SEAT), and
    the passenger's (role 11) to the mirrored x at the same height and depth.
    patch_hierarchy reports the anchor (steering.seat_anchor, seat_anchor()). Call after patch_hierarchy with VO3D
    p538/p559 at zero (hand_params). The marker
    quaternions (seat tilt) stay the host's. `eye_dsf` (the head camera) only goes
    into the report, against the driver model's eyes."""
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    slots = seat_markers(upvh_data)
    data = bytearray(upvh_data)
    report = {}
    if DRIVER_SEAT_ROLE not in slots:
        return bytes(data), report
    # The last steering marker: the one the engine turns the hands about (FUN_0088E310).
    rec = [r for r in h.records if r.type == 3 and r.role == STEERING_MARKER_ROLE and r.marker >= 0][-1]
    marker = anchor or struct.unpack_from("<3f", h.table("markers")[rec.marker])
    driver_at = h.offsets["markers"] + slots[DRIVER_SEAT_ROLE] * 0x20
    quat = struct.unpack_from("<4f", data, driver_at + 0x10)
    seat = seat_for_marker(marker, quat)
    for role, pos in ((DRIVER_SEAT_ROLE, seat), (PASSENGER_SEAT_ROLE, (-seat[0], seat[1], seat[2]))):
        if role in slots:
            at = h.offsets["markers"] + slots[role] * 0x20
            report["driver" if role == DRIVER_SEAT_ROLE else "passenger"] = {
                "from": [round(c, 4) for c in struct.unpack_from("<3f", data, at)],
                "to": [round(c, 4) for c in pos],
            }
            struct.pack_into("<3f", data, at, *pos)
    body_eye = _add(seat, quat_rotate(quat, FLEET_EYE_ABOVE_SEAT))
    report["body_eye"] = [round(c, 4) for c in body_eye]
    if eye_dsf is not None:
        report["body_eye_from_camera"] = [round(b - e, 4) for b, e in zip(body_eye, eye_dsf)]
    return bytes(data), report


# Vanilla head cameras see the steering rim's top this far below eye level
# (median of 146 cars, out/eye/wheel_view.py; quartiles 7.0 to 9.0, Desanne
# 13.7). Forza's CamCockpitOffset sits lower (370Z 6.5, M5 3.3, Skyline 1.9),
# so with DSF's field of view the rim fills the lower half of the screen.
RIM_DROP_DEG = 7.9


def camera_eye(eye_dsf, wheel_points_dsf, sight=None) -> tuple[float, float, float]:
    """The head-camera position: the Forza eye, raised (never lowered) until the
    steering wheel's topmost vertex is RIM_DROP_DEG below it, measured the way
    out/eye/wheel_view.py measures vanilla cars. The driver's seat stays under
    the Forza eye, so the driver model keeps its headroom.

    `sight(rise)` (GaugeSight) is the share of the needles' sweep the driver
    sees from the eye raised by `rise`: the raise stops in CAMERA_RISE_STEP
    steps before the cluster's cowl hides more than GAUGE_SIGHT_LOSS of what
    the Forza eye sees (research/cockpit.md, "Gauges under the cowl")."""
    top = max(wheel_points_dsf, key=lambda p: p[1])
    dist = abs(eye_dsf[2] - top[2])
    y = max(eye_dsf[1], top[1] + math.tan(math.radians(RIM_DROP_DEG)) * dist)
    rise = y - eye_dsf[1]
    if sight is not None and rise > 0:
        floor = sight(0.0) - GAUGE_SIGHT_LOSS
        steps = math.ceil(rise / CAMERA_RISE_STEP)
        allowed = 0.0
        for k in range(1, steps + 1):
            r = rise * k / steps
            if sight(r) < floor:
                break
            allowed = r
        y = eye_dsf[1] + allowed
    return (eye_dsf[0], y, eye_dsf[2])


CAMERA_RISE_STEP = 0.005  # m
GAUGE_SIGHT_LOSS = 0.05  # share of the needle sweep samples the raise may hide
GAUGE_SIGHT_STEPS = 12  # samples along each needle's sweep (13 angles)
GAUGE_SIGHT_CLEARANCE = 0.006  # m: occluders this close in front of a sample are the dial itself


def gauge_sight_points(cf: CockpitFit) -> list[tuple]:
    """Forza points the driver has to see to read the speedometer and
    tachometer: the needle's tip and a point 60% of the way out, at
    GAUGE_SIGHT_STEPS + 1 angles over the needle's sweep."""
    out = []
    for family, g in cf.gauges.items():
        if family not in GAUGE_IDS:
            continue
        pts = section_points(g.rest)
        if not pts:
            continue

        def in_plane(p, g=g):
            d = _sub(p, g.pivot)
            return _sub(d, _scale(g.normal, _dot(d, g.normal)))

        tip = max(pts, key=lambda p: _dot(in_plane(p), in_plane(p)))
        inner = _add(g.pivot, _scale(_sub(tip, g.pivot), 0.6))
        for k in range(GAUGE_SIGHT_STEPS + 1):
            deg = g.sweep_deg * k / GAUGE_SIGHT_STEPS
            out += [rotate_about(tip, g.pivot, g.normal, deg), rotate_about(inner, g.pivot, g.normal, deg)]
    return out


class GaugeSight:
    """Share of `points` (Forza space) seen from an eye: a ray from the eye to
    each point that no opaque LOD-0 triangle of `sections` stops short of
    GAUGE_SIGHT_CLEARANCE before the point. Glass doesn't stop it."""

    def __init__(self, sections: list[carbin.Section], points: list[tuple]):
        import numpy as np

        tris = []
        for s in sections:
            pool = None
            for sub in s.subsections:
                if sub.lod != 0 or fm.classify_cockpit(sub.material)[0] in (fm.GLASS, fm.SKIP):
                    continue
                if pool is None:
                    pool = np.array([v.position for v in s.lod0_vertices], dtype=np.float64).reshape(-1, 3)
                tris.append(pool[np.array(sub.triangles(), dtype=np.int64).reshape(-1, 3)])
        self.tris = np.concatenate(tris) if tris else np.zeros((0, 3, 3))
        self.points = np.array(points, dtype=np.float64).reshape(-1, 3)
        self.e1 = self.tris[:, 1] - self.tris[:, 0]
        self.e2 = self.tris[:, 2] - self.tris[:, 0]
        self.lo, self.hi = self.tris.min(axis=1), self.tris.max(axis=1)

    def __call__(self, eye) -> float:
        return self.fraction(eye, self.points)

    def fraction(self, eye, points) -> float:
        """Share of `points` seen from `eye` (1 for no points)."""
        import numpy as np

        points = np.array(points, dtype=np.float64).reshape(-1, 3)
        if not len(points):
            return 1.0
        eye = np.array(eye, dtype=np.float64)
        # Only triangles in the box between the eye and a point can stop its ray: those
        # in the box round the eye and every point first (each point's box is inside
        # it), then each point's own, the same test on fewer triangles.
        lo, hi = np.minimum(eye, points.min(axis=0)) - 1e-4, np.maximum(eye, points.max(axis=0)) + 1e-4
        near = np.flatnonzero(((self.hi >= lo) & (self.lo <= hi)).all(axis=1))
        p0, e1, e2 = self.tris[near, 0], self.e1[near], self.e2[near]
        tlo, thi = self.lo[near], self.hi[near]
        seen = 0
        for p in points:
            d = p - eye
            length = float(np.sqrt(d @ d))
            d = d / length
            lo, hi = np.minimum(eye, p) - 1e-4, np.maximum(eye, p) + 1e-4
            box = ((thi >= lo) & (tlo <= hi)).all(axis=1)
            h = np.cross(d, e2[box])
            a = (e1[box] * h).sum(axis=1)
            ok = np.abs(a) > 1e-12
            f = np.where(ok, 1 / np.where(ok, a, 1), 0)
            sv = eye - p0[box]
            u = f * (sv * h).sum(axis=1)
            q = np.cross(sv, e1[box])
            v = f * (q @ d)
            t = f * (e2[box] * q).sum(axis=1)
            hit = ok & (u >= 0) & (u <= 1) & (v >= 0) & (u + v <= 1) & (t > 1e-4) & (t < length - GAUGE_SIGHT_CLEARANCE)
            seen += not hit.any()
        return seen / len(points)


def camera_params(eye_dsf, marker_x: float) -> dict[int, float]:
    """VO3D head camera (FUN_0088DF80 case 0x14): eye = (marker.x - p430, p431, -p432)."""
    return {430: marker_x - eye_dsf[0], 431: eye_dsf[1], 432: -eye_dsf[2]}


def patch_vo3d(vo3d_data: bytes, params: dict[int, float]) -> bytes:
    from dsfforza.build import handling

    v = handling.VO3D.parse(vo3d_data)
    v.params.update(params)
    return v.build()


def head_camera(upvh_data: bytes, vo3d_data: bytes) -> tuple[tuple, tuple]:
    """(eye, steering marker) in DSF model space, as the engine computes them."""
    from dsfforza.build import handling
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    marker = next(r for r in h.records if r.type == 3 and r.role == STEERING_MARKER_ROLE and r.marker >= 0)
    m = struct.unpack_from("<3f", h.table("markers")[marker.marker])
    v = handling.VO3D.parse(vo3d_data)
    return (m[0] - v.get(430), v.get(431), -v.get(432)), m
