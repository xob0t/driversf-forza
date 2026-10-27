"""Checks for src/dsfforza/convert/cockpit.py that need no game files (a fraction of a second).

uv run python tests/test_cockpit.py     # or every test: uv run python -m tests cockpit
"""

from __future__ import annotations

import math
import struct

from dsfforza.convert import cockpit
from dsfforza.convert import forza_materials as fm
from dsfforza.formats import carbin, upvh


def _display(transform: tuple, width: float = 0.25, height: float = 0.08) -> carbin.Section:
    """A flat digital_gauge panel (width x height m, two triangles) with UV0 0..1
    across it, like the S2000's cluster face."""
    corners = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
    verts = [carbin.Vertex((u * width, (1 - v) * height, 0.0), (u, v), (0.0, 0.0), (0.0, 0.0, 1.0)) for u, v in corners]
    sub = carbin.SubSection("digital_gauge", 0, 4, transform, [0, 1, 2, 2, 1, 3])
    return carbin.Section("interior", (0, 0, 0), (0, 0, 0), (0, 0, 0), lod0_vertices=verts, subsections=[sub])


def test_display_face_uses_the_second_transform():
    # S2000: the second transform maps the panel onto the dial artwork (0.79 x 0.24 of
    # the texture over a 0.25 x 0.08 m panel: about 3 UV units per metre, undistorted).
    labels, face = (0.126, 0.746, 0.301, 0.463), (0.009, 0.784, 0.008, 0.244)
    section = _display(labels + face)
    aniso, density = cockpit.uv_map_stats(section, section.subsections[0], face)
    assert abs(aniso - 1.0) < 0.05 and density > 2.5
    out, faces = cockpit.with_display_faces(section)
    assert faces == 2
    sub = out.subsections[0]
    assert sub.material == cockpit.DISPLAY_FACE and tuple(sub.uv_transform[:4]) == face
    assert section.subsections[0].material == "digital_gauge"  # the source is left alone
    assert fm.classify_cockpit(sub.material, "interior") == (fm.TEXTURED, fm.INTERIOR)
    m = fm.resolve(out, sub, fm.classify_cockpit)
    assert m.kind == "textured" and abs(m.bounds[2] - 0.793) < 1e-3


def test_blank_displays_stay_blank():
    labels = (0.126, 0.746, 0.301, 0.463)
    # A swatch (M5: scale 0) and a stretched strip (Raptor: 489 x 12 texels).
    for face in ((0.5, 0.0, 0.5, 0.0), (0.02, 0.95, 0.4, 0.023)):
        section = _display(labels + face)
        assert cockpit.display_face(section, section.subsections[0]) is None
        assert cockpit.with_display_faces(section) == (section, 0)


def test_badges_follow_their_section():
    # Cabin badges (horn logo, labels) sample interior_LOD0; badges on the parts
    # seen through the windscreen (bonnet decals, mirror badges) the exterior atlas.
    assert fm.classify_cockpit("badge", "steering_wheel") == (fm.TEXTURED, fm.INTERIOR)
    assert fm.classify_cockpit("badge", "interior") == (fm.TEXTURED, fm.INTERIOR)
    for section in ("hooda", "a_pillar", "mirrorL"):
        assert fm.classify_cockpit("badge", section) == (fm.TEXTURED, fm.NODAMAGE)
        assert fm.classify_cockpit("stripe_badge", section) == (fm.TEXTURED, fm.NODAMAGE)
        assert fm.classify_cockpit("gauge_emissive", section) == (fm.TEXTURED, fm.INTERIOR)
    assert fm.classify_cockpit("liveryBadge", "hooda")[0] == fm.SKIP
    assert fm.classify_cockpit("badge") == (fm.TEXTURED, fm.INTERIOR)  # role-only callers


def test_upgrade_bonnets_left_out():
    # Cockpit carbins hold every bonnet the car can wear (11 of the 40 testdrive cars);
    # fit() keeps the stock one, as forza2dsf.stock_sections does on the outside. The
    # Mustang Boss 429's body-colour race scoop (hoodCIrace) hid hooda's black one.
    from dsfforza.convert import forza2dsf

    names = [
        "seatL",
        "steering_wheel",
        "mirrorR",
        "glassLF",
        "cagerace",
        "speed_min",
        "speed_mid",
        "tach_max",
        "interior",
        "headlightL",
        "a_pillar",
        "hoodCIrace",
        "hoodPSrace",
        "hooda",
        "hoodb",
        "hoodg",
        "glassF",
    ]
    sections = [carbin.Section(n, (0, 0, 0), (0, 0, 0), (0, 0, 0)) for n in names]
    kept = [s.name for s in forza2dsf.stock_sections(sections)]
    dropped = {"cagerace", "hoodCIrace", "hoodPSrace", "hoodb", "hoodg"}
    assert kept == [n for n in names if n not in dropped], kept


class _Car:
    """handling.ForzaCar's two fields the virtual dials read."""

    redline = 6800.0
    top_speed_mph = 150.0


def test_virtual_dials_on_a_blank_display():
    # CL65-like: no speed or tach needle, a blank 16 x 10 cm display 0.7 m ahead of the eye.
    panel = _display((0.5, 0.0, 0.5, 0.0) * 2, width=0.16, height=0.10)
    eye = (0.08, 0.05, 0.7)
    dials, report = cockpit.virtual_gauges({"interior": panel}, {}, eye, _Car(), 2)
    assert [g.family for _, _, g, _ in dials] == ["tach", "speed"]
    assert set(report["dials"]) == {"tach", "speed"}
    for needle, face, g, cal in dials:
        assert cal.source == "virtual dial" and not cal.reverse and cal.rest_deg == 0
        assert g.normal[2] < -0.99  # into the dash
        assert abs(g.pivot[2]) < 1e-9 and 0 < g.pivot[0] < 0.16 and 0 < g.pivot[1] < 0.10
        # The engine turns the needle sweep x input degrees: the whole scale at the dial's end.
        unit = cockpit.GAUGE_UNITS[g.family]
        assert abs(cal.sweep * cal.dial_max / unit - g.sweep_deg) < 1e-6
        assert needle.name == f"{g.family}_min" and cockpit.NEEDLE.match(needle.name)
        # At rest the needle points at 0, half the span left of straight up (driver's view).
        tip = max(cockpit.section_points(needle), key=lambda p: (p[0] - g.pivot[0]) ** 2 + (p[1] - g.pivot[1]) ** 2)
        rest = math.degrees(math.atan2(tip[0] - g.pivot[0], tip[1] - g.pivot[1]))
        assert abs(rest + g.sweep_deg / 2) < 2.0, (rest, g.sweep_deg)
        assert all(p[2] > 0 for p in cockpit.section_points(face))  # over the display, towards the driver
        for sub in face.subsections:
            assert cockpit.FORCE_FLAT.match(sub.material)
            assert fm.classify_cockpit(sub.material, "interior") == (fm.TRIM, None)
    tach, speed = (cal for _, _, _, cal in dials)
    assert tach.dial_max == 8000 and speed.dial_max == 160  # 250 rpm over the redline at least; 5% over the top speed
    assert dials[0][2].pivot[0] < dials[1][2].pivot[0]  # tachometer on the left


def test_needles_the_host_cannot_turn_are_dropped():
    # Stock 250 (the Volt's host) has no gauge record, so add_gauge_records can't give
    # it one: a needle (here a virtual dial) whose id the host can't turn is left out.
    panel = _display((0.5, 0.0, 0.5, 0.0) * 2, width=0.16, height=0.10)
    dials, report = cockpit.virtual_gauges({"interior": panel}, {}, (0.08, 0.05, 0.7), _Car(), 2)
    cf = cockpit.CockpitFit(None, {}, (0, 0, 0), (0, 0, 0), "", {}, {"interior": panel}, {}, virtual=report)
    for needle, face, g, cal in dials:
        cf.gauges[g.family], cf.calibration[g.family], cf.needles[g.family] = g, cal, {"min": needle}
        cf.static[face.name] = face
    assert cockpit.drop_unturned(cf, {2: 0}) == ["tach"]
    assert set(cf.gauges) == {"speed"} and set(cf.needles) == {"speed"}
    assert "dsf_dial_tach" not in cf.static and "dsf_dial_speed" in cf.static
    assert set(cf.virtual["dials"]) == {"speed"} and cf.virtual["no_host_gauge"] == ["tach"]


def _gauge_hierarchy(gauge_id: int) -> bytes:
    """root > cluster part (role 185) > one gauge record (the stock role for its id),
    then the steering marker: a one-gauge host like 146 (speedometer) or 225 (tachometer)."""
    head = bytearray(upvh.HEADER_SIZE)
    head[:4] = b"AWHF"
    struct.pack_into("<6I", head, 4, 0, 0, 26, 4, 1, 0)
    struct.pack_into("<H", head, 0x28, 1)
    head[0x2C] = 1
    records = bytearray()
    for kind, role, subtree, marker, seat in (
        (4, 0, 4, -1, -1),
        (1, cockpit.CLUSTER_ROLE, 2, -1, -1),
        (18, cockpit.GAUGE_ROLES[gauge_id], 1, -1, 0),
        (3, cockpit.STEERING_MARKER_ROLE, 1, 0, -1),
    ):
        rec = bytearray(b"\xff" * upvh.RECORD_SIZE)
        struct.pack_into("<HHHHHHHH", rec, 0x08, kind, role, 0, 0, 1, 0, subtree, 0)
        struct.pack_into("<b", rec, 0x26, seat)
        struct.pack_into("<h", rec, 0x1A, marker)
        records += rec
    markers = struct.pack("<3f4x4f", 0.3, 0.5, 0.3, 0, 0, 0, 1)
    seats = struct.pack("<3f4x4fII", 0.3, 0.6, 0.5, 0, 0, 0, 1, gauge_id, 120) + bytes(8)
    return bytes(head + records + markers + seats)


def test_one_gauge_host_gets_the_other_record():
    # 19 stock hosts have a speedometer record only, 225 (MP4-12C) a tachometer only:
    # the engine turns a needle only through a gauge record for its id (issue #62).
    for have, add in ((2, 1), (1, 2)):
        data = _gauge_hierarchy(have)
        out, added = cockpit.add_gauge_records(data, {1, 2})
        assert added == [add]
        h = upvh.parse(out)
        assert len(h.records) == 5 and h.counts["seats"] == 2
        assert [r.subtree for r in h.records] == [5, 3, 1, 1, 1]
        gauges = cockpit.gauge_records(h)
        # Stock order: the speedometer's record before the tachometer's, both under the cluster.
        assert gauges == {2: 2, 1: 3}, gauges
        for gid, index in gauges.items():
            r = h.records[index]
            assert r.type == 18 and r.role == cockpit.GAUGE_ROLES[gid]
            assert struct.unpack_from("<I", h.table("seats")[r.seat], 0x20)[0] == gid
        assert h.records[gauges[add]].seat == 1  # appended to the table
        assert h.table("seats")[0] == upvh.parse(data).table("seats")[0]
        # The marker after them moved down a record; its table entry is unchanged.
        assert h.records[4].type == 3 and h.records[4].marker == 0
        assert h.table("markers") == upvh.parse(data).table("markers")
        # Nothing to add: the hierarchy comes back as it was.
        assert cockpit.add_gauge_records(out, {1, 2}) == (out, [])
        assert cockpit.add_gauge_records(data, {have}) == (data, [])


def test_no_virtual_dial_on_the_steering_wheel_or_for_needles():
    panel = _display((0.5, 0.0, 0.5, 0.0) * 2, width=0.16, height=0.10)
    eye = (0.08, 0.05, 0.7)
    # Needles are drawn in the car's frame: they wouldn't turn with the wheel.
    assert cockpit.virtual_gauges({"steering_wheel": panel}, {}, eye, _Car(), 2) == ([], {})
    # A car with both needles keeps its display as it is.
    both = {"speed": object(), "tach": object()}
    assert cockpit.virtual_gauges({"interior": panel}, both, eye, _Car(), 2) == ([], {})
    # Too small for a dial (a 4 x 2 cm gear readout).
    small = _display((0.5, 0.0, 0.5, 0.0) * 2, width=0.04, height=0.02)
    dials, report = cockpit.virtual_gauges({"interior": small}, {}, eye, _Car(), 2)
    assert dials == [] and "too small" in report["rejected"][0]


def _needle_pose(name: str, deg: float, extra: bool = False) -> carbin.Section:
    """A needle (a thin quad from the pivot (0.3, 0.7, -0.7) up 5 cm), turned `deg`
    clockwise for a driver looking along -z; `extra` adds a stray triangle."""
    pivot = (0.3, 0.7, -0.7)
    pts = [(-0.002, -0.005), (0.002, -0.005), (0.001, 0.05), (-0.001, 0.05)]
    a = math.radians(deg)
    verts = [
        carbin.Vertex(
            (pivot[0] + x * math.cos(a) + y * math.sin(a), pivot[1] - x * math.sin(a) + y * math.cos(a), pivot[2]),
            (0.0, 0.0),
            (0.0, 0.0),
            (0.0, 0.0, 1.0),
        )
        for x, y in pts
    ]
    idx = [0, 1, 2, 0, 2, 3]
    if extra:
        verts.append(carbin.Vertex(verts[2].position, (0.0, 0.0), (0.0, 0.0), (0.0, 0.0, 1.0)))
        idx += [1, 2, 4]
    sub = carbin.SubSection("gauge_needle", 0, 4, (0.0, 1.0, 0.0, 1.0), idx)
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod0_vertices=verts, subsections=[sub])


def test_needle_pose_welded_differently():
    # FM4 Mini Cooper S 2003, Lancer Evo IX MR, Insignia VXR: the min pose carries a few
    # more triangles than mid and max, so no point-to-point match exists for min -> mid.
    poses = {"min": _needle_pose("speed_min", -120, True), "mid": _needle_pose("speed_mid", 0)}
    poses["max"] = _needle_pose("speed_max", 110)
    g = cockpit.fit_gauge("speed", poses)
    assert g is not None
    assert abs(g.sweep_deg - 230.0) < 0.2, g.sweep_deg
    assert math.dist(g.pivot, (0.3, 0.7, -0.7)) < 1e-3 and g.normal[2] < -0.99


def test_camera_raise_stops_before_the_cowl_hides_the_dials():
    wheel_top = [(0.0, 0.38, 0.35)]  # 0.5 m ahead: the rim top 7.9 deg down wants the eye 4.9 cm higher
    eye = (0.0, 0.4, -0.15)
    free = cockpit.camera_eye(eye, wheel_top)
    rise = free[1] - eye[1]
    assert rise > 0.03
    # The cowl hides a tenth of the needle sweep from 2 cm up.
    capped = cockpit.camera_eye(eye, wheel_top, sight=lambda r: 1.0 if r < 0.02 else 0.9)
    assert eye[1] + 0.015 <= capped[1] < eye[1] + 0.02
    # A raise that hides nothing goes all the way.
    assert cockpit.camera_eye(eye, wheel_top, sight=lambda r: 1.0) == free


def _ring(centre, radius: float, tilt_deg: float, n: int = 48) -> list[tuple]:
    """Points of a rim (Forza space: the driver at +z), its column tilt_deg up."""
    t = math.radians(tilt_deg)
    up = (0.0, math.cos(t), -math.sin(t))  # in the wheel plane
    out = []
    for k in range(n):
        a = 2 * math.pi * k / n
        for r in (radius - 0.012, radius + 0.012):
            out.append(
                (
                    centre[0] + r * math.sin(a),
                    centre[1] + r * math.cos(a) * up[1],
                    centre[2] + r * math.cos(a) * up[2],
                )
            )
    return out


def test_wheel_fit_ignores_a_modelled_column():
    # FM4 FG Falcon racers: the steering_wheel section holds a 1 m column to the
    # bulkhead; the fit took it for the wheel (74 deg column, 6 cm radius).
    centre, tilt = (0.25, 0.75, -0.19), 17.0
    rim = _ring(centre, 0.165, tilt)
    t = math.radians(tilt)
    column = [
        (0.25 + dx, 0.75 - s * math.sin(t), -0.19 - s * math.cos(t))
        for s in [i * 0.01 for i in range(100)]
        for dx in (-0.015, 0.015)
    ]
    for pts in (rim, rim + column):
        w = cockpit.fit_wheel_points(pts)
        assert math.dist(w.centre, centre) < 0.01, w
        assert abs(w.radius - 0.165) < 0.01 and abs(math.degrees(math.asin(w.axis[1])) - tilt) < 2, w


def test_rims_keep_their_size_unless_the_hands_miss():
    # The hinge only moves along the column (an in-plane shift made the hub circle
    # while steering: Regal GNX 3.2 cm), so the grips sit 0.17 m from the column.
    # Forza's wheel keeps its size while they are within HAND_FIT_TOLERANCE of the
    # rim (stock trucks: 0.20 m rims); a race yoke is scaled only that far.
    side = (0.0, 0.0, 0.0)
    axis = cockpit._norm(cockpit.STOCK_COLUMN)
    for radius in (0.165, cockpit.STOCK_RIM_RADIUS, 0.190, 0.200):  # hatchback, stock, ..., Regal GNX
        k = cockpit.hand_wheel_scale(side, axis, radius)
        shift = cockpit.hand_shift(side, axis, radius * k)
        assert cockpit.hand_grip_error(shift, side, axis, radius * k) <= cockpit.HAND_FIT_TOLERANCE + 1e-4, radius
        assert k == 1.0 or abs(k - 1) < 0.05, (radius, k)
    assert cockpit.hand_wheel_scale(side, axis, 0.180) == 1.0
    k = cockpit.hand_wheel_scale(side, axis, 0.114)  # ARX-01b
    assert 1.2 < k <= cockpit.WHEEL_SCALE_RANGE[1]
    shift = cockpit.hand_shift(side, axis, 0.114 * k)
    along = cockpit._dot(shift, axis)
    assert math.sqrt(max(0.0, cockpit._dot(shift, shift) - along * along)) < 1e-9  # no orbit
    assert abs(along) <= cockpit.MAX_HAND_ALONG + 1e-9


def test_seat_anchor_puts_the_grips_on_the_rim():
    # The chase view's driver holds the wheel at the stock point of the seat's frame;
    # the seat goes where those grips land on the drawn rim (in-plane shift allowed).
    side = (0.0, 0.0, 0.0)
    axis = cockpit._norm(cockpit.STOCK_COLUMN)
    for radius in (0.150, 0.200):
        anchor = cockpit.grip_shift(side, axis, radius)
        assert cockpit.hand_grip_error(anchor, side, axis, radius) < 0.003, radius


def test_hand_shift_keeps_the_hinge_on_the_column():
    # A flatter Forza column (S5S Raptor: 10 deg) than the stock rig's (24 deg): the
    # grips sit off the rim's plane, and only a move along the column is allowed.
    t = math.radians(10.0)
    axis = (0.0, math.sin(t), -math.cos(t))
    shift = cockpit.hand_shift((0.3, 0.6, 0.2), axis, cockpit.STOCK_RIM_RADIUS)
    assert abs(math.sqrt(cockpit._dot(shift, shift)) - abs(cockpit._dot(shift, axis))) < 1e-9
    assert cockpit._dot(shift, shift) > 0.0001  # it does move


def _hierarchy(markers: list[tuple[int, tuple, tuple]]) -> bytes:
    """A minimal UPVH: one marker record per (role, position, quaternion), the markers
    table and nothing else."""
    head = bytearray(upvh.HEADER_SIZE)
    head[:4] = b"AWHF"
    struct.pack_into("<6I", head, 4, 0, 0, 26, len(markers), 1, 0)
    struct.pack_into("<H", head, 0x28, len(markers))
    records, table = bytearray(), bytearray()
    for n, (role, pos, quat) in enumerate(markers):
        rec = bytearray(upvh.RECORD_SIZE)
        rec[:8] = upvh.NONE_UID
        struct.pack_into("<HH", rec, 0x08, 3, role)
        struct.pack_into("<hhh", rec, 0x1A, n, -1, -1)
        records += rec
        table += struct.pack("<3f4x4f", *pos, *quat)
    return bytes(head + records + table)


def test_seat_follows_the_steering_marker():
    # The driver model holds the wheel only where stock cars have it in the seat's
    # frame: the seat goes where that puts the marker, at the host's seat tilt
    # (Mustang Shelby GT500 1967, 193: -5 deg; its own marker sits at (0.505, 0.385) from the seat).
    tilt = (-0.0436, 0.0, 0.0, 0.999)
    marker = (0.3853, 0.1646, 0.1733)
    data = _hierarchy(
        [(47, marker, (0.2, 0, 0, 0.98)), (10, (0.3, -0.4, -0.12), tilt), (11, (-0.3, -0.4, -0.12), tilt)]
    )
    out, report = cockpit.patch_seats(data, eye_dsf=(0.385, 0.391, -0.296))
    h = upvh.parse(out)
    seat = struct.unpack_from("<3f", h.table("markers")[1])
    passenger = struct.unpack_from("<3f", h.table("markers")[2])
    assert abs(seat[0] - marker[0]) < 1e-6 and passenger == (-seat[0], seat[1], seat[2])
    assert struct.unpack_from("<4f", h.table("markers")[1], 0x10) == struct.unpack_from(
        "<4f", upvh.parse(data).table("markers")[1], 0x10
    )
    # in the seat's frame the marker is the stock point; in model space the host's own offset
    frame = cockpit.quat_rotate((-tilt[0], 0, 0, tilt[3]), tuple(m - s for m, s in zip(marker, seat)))
    assert max(abs(a - b) for a, b in zip(frame, cockpit.STOCK_MARKER_FROM_SEAT)) < 1e-4
    assert abs(marker[1] - seat[1] - 0.505) < 2e-3 and abs(marker[2] - seat[2] - 0.385) < 2e-3
    assert report["driver"]["from"] == [0.3, -0.4, -0.12] and "body_eye_from_camera" in report


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
