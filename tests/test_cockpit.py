"""Checks for src/dsfforza/convert/cockpit/ that need no game files (a fraction of a second).

uv run python tests/test_cockpit.py     # or every test: uv run python -m tests cockpit
"""

from __future__ import annotations

import math
import struct
import types

from dsfforza.convert import cockpit, damage
from dsfforza.convert import forza_materials as fm
from dsfforza.convert.cockpit import displays
from dsfforza.formats import carbin, chnk, upvh


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
    # fit() keeps the vanilla one, as forza2dsf.vanilla_sections does on the outside. The
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
    kept = [s.name for s in forza2dsf.vanilla_sections(sections)]
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
    dials, report, extra = cockpit.virtual_gauges({"interior": panel}, {}, eye, _Car(), 2)
    assert extra == {}
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
    # Vanilla 250 (the Volt's host) has no gauge record, so add_gauge_records can't give
    # it one: a needle (here a virtual dial) whose id the host can't turn is left out.
    panel = _display((0.5, 0.0, 0.5, 0.0) * 2, width=0.16, height=0.10)
    dials, report, _ = cockpit.virtual_gauges({"interior": panel}, {}, (0.08, 0.05, 0.7), _Car(), 2)
    cf = cockpit.CockpitFit(None, {}, (0, 0, 0), (0, 0, 0), "", {}, {"interior": panel}, {}, virtual=report)
    for needle, face, g, cal in dials:
        cf.gauges[g.family], cf.calibration[g.family], cf.needles[g.family] = g, cal, {"min": needle}
        cf.static[face.name] = face
    assert cockpit.drop_unturned(cf, {2: 0}) == ["tach"]
    assert set(cf.gauges) == {"speed"} and set(cf.needles) == {"speed"}
    assert "dsf_dial_tach" not in cf.static and "dsf_dial_speed" in cf.static
    assert set(cf.virtual["dials"]) == {"speed"} and cf.virtual["no_host_gauge"] == ["tach"]


def _gauge_hierarchy(gauge_id: int) -> bytes:
    """root > cluster part (role 185) > one gauge record (the vanilla role for its id),
    then the steering marker: a one-gauge host like 146 (speedometer) or 225 (tachometer).
    The cluster is hinged (the needles' hinge, as on 146) and the records' tree fields
    (+0x0C bit 0, +0x16/+0x18) are vanilla's."""
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
        struct.pack_into("<h", rec, 0x1C, 1 if role == cockpit.CLUSTER_ROLE else -1)
        records += rec
    raw = [records[k : k + upvh.RECORD_SIZE] for k in range(0, len(records), upvh.RECORD_SIZE)]
    for r, (bit, leaf, pops) in zip(raw, damage._tree_fields(raw), strict=True):
        struct.pack_into("<HHH", r, 0x14, struct.unpack_from("<H", r, 0x14)[0], leaf, pops)
        struct.pack_into("<H", r, 0x0C, bit)
    records = b"".join(raw)
    markers = struct.pack("<3f4x4f", 0.3, 0.5, 0.3, 0, 0, 0, 1)
    seats = struct.pack("<3f4x4fII", 0.3, 0.6, 0.5, 0, 0, 0, 1, gauge_id, 120) + bytes(8)
    return bytes(head + records + markers + seats)


def test_one_gauge_host_gets_the_other_record():
    # 19 vanilla hosts have a speedometer record only, 225 (MP4-12C) a tachometer only:
    # the engine turns a needle only through a gauge record for its id (issue #62).
    for have, add in ((2, 1), (1, 2)):
        data = _gauge_hierarchy(have)
        out, added = cockpit.add_gauge_records(data, {1, 2})
        assert added == [add]
        h = upvh.parse(out)
        assert len(h.records) == 5 and h.counts["seats"] == 2
        assert [r.subtree for r in h.records] == [5, 3, 1, 1, 1]
        gauges = cockpit.gauge_records(h)
        # Vanilla order: the speedometer's record before the tachometer's, both under the cluster.
        assert gauges == {2: 2, 1: 3}, gauges
        for gid, index in gauges.items():
            r = h.records[index]
            assert r.type == 18 and r.role == cockpit.GAUGE_ROLES[gid]
            assert struct.unpack_from("<I", h.table("seats")[r.seat], 0x20)[0] == gid
        assert h.records[gauges[add]].seat == 1  # appended to the table
        # The gauge record that now ends the cluster's subtree pops its hinge, the
        # other none (#266: a stale pop count on the speedometer failed damage check).
        fields = damage._tree_fields([r.raw for r in h.records])
        assert [damage._read_tree_fields(r.raw) for r in h.records] == fields
        assert fields[2] == (0, 0, 0) and fields[3] == (0, 1, 1), fields
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
    # Needles are drawn in the car's frame: they wouldn't turn with the wheel (without
    # the wheel's fit there is no virtual display either).
    dials, report, extra = cockpit.virtual_gauges({"steering_wheel": panel}, {}, eye, _Car(), 2)
    assert dials == [] and extra == {} and report == {"missing": ["tach", "speed"]}
    # A car with both needles keeps its display as it is.
    both = {"speed": object(), "tach": object()}
    assert cockpit.virtual_gauges({"interior": panel}, both, eye, _Car(), 2) == ([], {}, {})
    # Too small for a dial (a 4 x 2 cm gear readout).
    small = _display((0.5, 0.0, 0.5, 0.0) * 2, width=0.04, height=0.02)
    dials, report, _ = cockpit.virtual_gauges({"interior": small}, {}, eye, _Car(), 2)
    assert dials == [] and "too small" in report["rejected"][0]


def test_virtual_dials_on_a_display_face():
    # Panoz Esperante GTLM, Viper Competition Coupe, FG Falcon racers: the only display
    # has a face of its own (a light grey LCD with a printed bar outline). The dials go
    # on it, the face is drawn flat in its colour, and on a light face the marks are dark.
    labels, face = (0.126, 0.746, 0.301, 0.463), (0.009, 0.784, 0.008, 0.244)
    panel, _ = cockpit.with_display_faces(_display(labels + face, width=0.175, height=0.071))
    atlas = (4, 4, bytearray([110, 116, 104, 255] * 16))
    eye = (0.08, 0.05, 0.7)
    static = {"interior": panel}
    dials, report, extra = cockpit.virtual_gauges(static, {}, eye, _Car(), 2, atlas=atlas)
    assert [g.family for _, _, g, _ in dials] == ["tach", "speed"] and extra == {}
    assert report["face"] == {"section": "interior", "subsection": 0, "colour": [110, 116, 104], "front_mm": 0.0}
    assert all(rep["on"] == "face" for rep in report["dials"].values())
    for _, dial, _, _ in dials:
        assert {sub.material for sub in dial.subsections} <= {"dsf_dial_mark_dark", "dsf_dial_red"}
        assert all(p[2] > 0 for p in cockpit.section_points(dial))
    assert cockpit.flatten_face(static, report["face"]) == 2
    sub = static["interior"].subsections[0]
    assert sub.material == "digital_gauge" and sub.display_rgba == (110, 116, 104, 255)
    assert panel.subsections[0].material == cockpit.DISPLAY_FACE  # the fit's copy, not the source
    # A blank display still comes first.
    blank = _display((0.5, 0.0, 0.5, 0.0) * 2, width=0.16, height=0.10)
    blank.name = "dash"
    _, report, _ = cockpit.virtual_gauges({"interior": panel, "dash": blank}, {}, eye, _Car(), 2, atlas=atlas)
    assert "face" not in report and report["dials"]["tach"]["display"] == "dash"


def _quad_section(name: str, corners, materials=("plastic",)) -> carbin.Section:
    """A section of one quad per technique, all over the same four corners."""
    verts = [carbin.Vertex(p, (0.0, 0.0), (0.0, 0.0), (0.0, 0.0, 1.0)) for p in corners]
    subs = [carbin.SubSection(m, 0, 4, (0.0, 1.0, 0.0, 1.0) * 2, [0, 1, 2, 0, 2, 3]) for m in materials]
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod0_vertices=verts, subsections=subs)


def _wheel_disc(centre, axis, radius: float, steps: int = 24) -> carbin.Section:
    """A steering wheel as a flat disc, with a small digital display at its hub."""
    side = cockpit._norm(cockpit._cross(axis, (0.0, 1.0, 0.0)))
    up = cockpit._cross(side, axis)
    ring = [
        cockpit._add(
            centre,
            cockpit._add(
                cockpit._scale(side, radius * math.cos(2 * math.pi * k / steps)),
                cockpit._scale(up, radius * math.sin(2 * math.pi * k / steps)),
            ),
        )
        for k in range(steps)
    ]
    verts = [carbin.Vertex(p, (0.0, 0.0), (0.0, 0.0), axis) for p in [centre] + ring]
    fan = [i for k in range(steps) for i in (0, 1 + k, 1 + (k + 1) % steps)]
    subs = [
        carbin.SubSection("leather", 0, 4, (0.0, 1.0, 0.0, 1.0) * 2, fan),
        carbin.SubSection("digital_gauge", 0, 4, (0.5, 0.0, 0.5, 0.0) * 2, fan[:6]),
    ]
    return carbin.Section("steering_wheel", (0, 0, 0), (0, 0, 0), (0, 0, 0), lod0_vertices=verts, subsections=subs)


def test_virtual_display_past_the_steering_wheel():
    # ARX-02a, R15 TDI, GT-R GT1: the only readout is on the steering wheel. A virtual
    # display goes on the dashboard above the rim, facing the eye, clear of the wheel.
    eye = (0.0, 0.0, 0.0)
    axis = cockpit._norm((0.0, 0.36, 0.93))
    wheel = cockpit.Wheel((0.0, -0.25, -0.45), axis, 0.17)
    dash = _quad_section(
        "interior", [(-0.6, -0.6, -0.85), (0.6, -0.6, -0.85), (0.6, -0.02, -0.85), (-0.6, -0.02, -0.85)]
    )
    static = {"interior": dash, "steering_wheel": _wheel_disc(wheel.centre, axis, wheel.radius)}
    dials, report, extra = cockpit.virtual_gauges(static, {}, eye, _Car(), 2, wheel=wheel)
    assert [g.family for _, _, g, _ in dials] == ["tach", "speed"], report
    assert set(extra) == {"dsf_dial_panel"}
    pod = report["virtual_display"]
    assert pod["size_cm"] == [16.0, 8.0] and pod["depth_cm"] < 10, pod
    housing = extra["dsf_dial_panel"]
    assert {sub.material for sub in housing.subsections} == {"dsf_dial_display", "dsf_dial_bezel"}
    for sub in housing.subsections:
        assert cockpit.FORCE_FLAT.match(sub.material) and fm.classify_cockpit(sub.material)[0] == fm.TRIM
    # In front of the dash, beside or above the rim as the driver sees it, and outside it at any angle.
    screen = cockpit.section_points(housing, {"dsf_dial_display"})
    assert all(-0.85 < p[2] < -0.85 + 0.1 for p in screen), screen  # the housing's sides reach into the dash
    for _, _, g, _ in dials:
        assert g.normal[2] < -0.9 and g.pivot[1] > wheel.centre[1]
        t = cockpit._dot(cockpit._sub(wheel.centre, eye), axis) / cockpit._dot(cockpit._norm(g.pivot), axis)
        crossing = cockpit._scale(cockpit._norm(g.pivot), t)
        assert math.dist(crossing, wheel.centre) > wheel.radius + cockpit.POD_WHEEL_CLEAR
    # A Forza needle where the display would go keeps it away (the tach's dial is clear).
    needle = _needle_pose("tach_min", 0)
    tach = cockpit.Gauge("tach", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0), 270.0, needle)
    tach.pivot = cockpit._scale(cockpit._norm(dials[0][2].pivot), 0.84)
    for v in needle.lod0_vertices:
        v.position = cockpit._add(tach.pivot, cockpit._sub(v.position, (0.3, 0.7, -0.7)))
    dials2, _, _ = cockpit.virtual_gauges(static, {"tach": tach}, eye, _Car(), 2, wheel=wheel)
    assert [g.family for _, _, g, _ in dials2] == ["speed"]
    reach = max(math.dist(p, tach.pivot) for p in cockpit.section_points(needle)) * cockpit.POD_GAUGE_CLEAR
    for p in cockpit.section_points(dials2[0][1]):
        crossing = cockpit._scale(cockpit._norm(p), tach.pivot[2] / cockpit._norm(p)[2])
        assert math.dist(crossing, tach.pivot) > reach
    # No digital readout anywhere: the car has no such gauge, nothing is added.
    rim = [v.position for v in _wheel_disc(wheel.centre, axis, wheel.radius).lod0_vertices[1:5]]
    plain = {"interior": dash, "steering_wheel": _quad_section("steering_wheel", rim, ("leather",))}
    assert cockpit.virtual_gauges(plain, {}, eye, _Car(), 2, wheel=wheel) == ([], {}, {})


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


def test_grip_params_give_dsfforza_asi_the_drawn_rim():
    # Forza's wheel keeps its size; dsfforza.asi [hands] moves the hands onto the rim
    # it reads from VO3D: the radius, and the centre from the steering marker along
    # the engine's column direction (the marker quaternion's +z, away from the driver).
    t = math.radians(24.0)
    axis = (0.0, math.sin(t), -math.cos(t))  # report axis: towards the driver
    centre = (0.4, 0.6, 0.28)
    for radius in (0.114, cockpit.VANILLA_RIM_RADIUS, 0.220):  # ARX-01b, vanilla, tour bus
        shift = cockpit.hand_shift(centre, axis, radius)
        along = cockpit._dot(shift, axis)
        assert math.sqrt(max(0.0, cockpit._dot(shift, shift) - along * along)) < 1e-9  # no orbit
        marker = cockpit._add(centre, shift)
        steering = {"axis": list(axis), "radius": radius, "hands": {"shift": list(shift)}}
        params = cockpit.grip_params(steering)
        assert params[cockpit.GRIP_RADIUS_PARAM] == radius
        engine_dir = cockpit.quat_rotate(cockpit.quat_from_to((0, 0, -1), axis), (0.0, 0.0, 1.0))
        found = cockpit._add(marker, cockpit._scale(engine_dir, params[cockpit.GRIP_ALONG_PARAM]))
        assert math.dist(found, centre) < 1e-6, radius
    hand = cockpit.hand_params(steering)
    assert hand[538] == hand[559] == 0.0 and set(cockpit.GRIP_PARAMS) <= set(hand)
    assert set(cockpit.hand_params()) == set(cockpit.HAND_PARAMS)  # no steering report: p538/p559 only


def test_chase_driver_hands_keep_their_angle_on_forza_rim():
    # The chase views' driver grips the vanilla circle about the seat's anchor
    # (seat_anchor, off the wheel's column on small or large rims); the chase-view wheel
    # keeps Forza's size, and dsfforza.asi [hands] puts each hand on its rim at the angle
    # the animations give it about that anchor, so the hands turn with the wheel.
    from dsfforza.convert import steering_axle

    side = (0.0, 0.0, 0.0)
    axis = cockpit._norm(cockpit.VANILLA_COLUMN)
    for radius in (0.114, 0.200, 0.220):  # ARX-01b, Regal GNX, tour bus
        anchor = cockpit.seat_anchor(side, axis, radius)
        rim = (side, axis, radius)
        drawn = {"centre": side, "axis": axis, "radius": radius}
        err = steering_axle.grip_error(anchor, drawn, rim)
        assert err["grip_asi"] < 1e-6 and err["asi_rim"] < 1e-9, (radius, err)
        for g in cockpit.hand_grips(anchor):
            t = steering_axle.asi_target(g, rim, anchor)
            u, v = cockpit._norm(cockpit._sub(g, anchor)), cockpit._norm(cockpit._sub(t, side))
            assert cockpit._dot(u, v) > 0.9999, radius  # the same angle, about the hub


def test_seat_anchor_puts_the_grips_on_the_rim():
    # The chase view's driver holds the wheel at the vanilla point of the seat's frame;
    # the seat goes where those grips land on the drawn rim (in-plane shift allowed).
    side = (0.0, 0.0, 0.0)
    axis = cockpit._norm(cockpit.VANILLA_COLUMN)
    for radius in (0.150, 0.200):
        anchor = cockpit.grip_shift(side, axis, radius)
        assert cockpit.hand_grip_error(anchor, side, axis, radius) < 0.003, radius


def test_hand_shift_keeps_the_hinge_on_the_column():
    # A flatter Forza column (S5S Raptor: 10 deg) than the vanilla rig's (24 deg): the
    # grips sit off the rim's plane, and only a move along the column is allowed.
    t = math.radians(10.0)
    axis = (0.0, math.sin(t), -math.cos(t))
    shift = cockpit.hand_shift((0.3, 0.6, 0.2), axis, cockpit.VANILLA_RIM_RADIUS)
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
    # The driver model holds the wheel only where vanilla cars have it in the seat's
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
    # in the seat's frame the marker is the vanilla point; in model space the host's own offset
    frame = cockpit.quat_rotate((-tilt[0], 0, 0, tilt[3]), tuple(m - s for m, s in zip(marker, seat)))
    assert max(abs(a - b) for a, b in zip(frame, cockpit.VANILLA_MARKER_FROM_SEAT)) < 1e-4
    assert abs(marker[1] - seat[1] - 0.505) < 2e-3 and abs(marker[2] - seat[2] - 0.385) < 2e-3
    assert report["driver"]["from"] == [0.3, -0.4, -0.12] and "body_eye_from_camera" in report


def test_every_steering_marker_moves_to_the_wheel():
    # Host 132 (Aston DB5) has two steering markers at its wheel. The head camera hangs from
    # the first, the wheel and the hands from the last (FUN_0088E310): moving only the first
    # left the EAE Speedster's wheel and arms 0.7 m from its right-hand-drive cockpit (#318).
    host_wheel, column_q = (0.326, 0.505, 0.078), (0.211, 0.0, 0.0, 0.977)
    head = bytearray(upvh.HEADER_SIZE)
    head[:4] = b"AWHF"
    struct.pack_into("<6I", head, 4, 0, 0, 26, 3, 1, 0)
    head[0x2A] = 1  # one hinge
    struct.pack_into("<H", head, 0x28, 2)  # two markers
    records = bytearray()
    for kind, role, marker, hinge in ((1, 30, -1, 0), (3, 47, 0, -1), (3, 47, 1, -1)):
        rec = bytearray(upvh.RECORD_SIZE)
        rec[:8] = upvh.NONE_UID
        struct.pack_into("<HH", rec, 0x08, kind, role)
        struct.pack_into("<hhh", rec, 0x1A, marker, hinge, -1)
        struct.pack_into("<hh", rec, 0x2A, 1 if kind == 1 else -1, 0)
        struct.pack_into("<h", rec, 0x34, 1 if kind == 1 else -1)
        records += rec
    hinges = struct.pack("<3f4x3f4x16x", *host_wheel, 0.0, 0.41, -0.91)
    markers = struct.pack("<3f4x4f", *host_wheel, *column_q) * 2
    data = bytes(head + records + hinges + markers)
    host = cockpit.host_cockpit(data)
    assert host.wheel_markers == (0, 1) and host.wheel_marker == 0 and host.engine_marker == 1
    wheel = cockpit.Wheel((0.347, 0.443, 0.215), (0.0, 0.425, 0.905), 0.165)  # Forza space: RHD at +x
    cf = types.SimpleNamespace(wheel=wheel, gauges={}, calibration={}, mirrors={})
    place = types.SimpleNamespace(point=lambda p: (-p[0], p[1], -p[2]), normal=lambda n: (-n[0], n[1], -n[2]))
    out, report = cockpit.patch_hierarchy(data, host, place, cf)
    h = upvh.parse(out)
    first, last = (struct.unpack_from("<3f4x4f", m) for m in h.table("markers"))
    assert first == last and abs(first[0] + 0.347) < 1e-6, (first, last)
    assert report["steering"]["markers"] == [0, 1]
    # The head camera (first marker) and the wheel (last) agree, so the eye sits behind the wheel.
    from dsfforza.build import handling

    vo3d = handling.VO3D(1, {430: 0.0, 431: 0.67, 432: 0.61}).build()
    eye, marker = cockpit.head_camera(out, vo3d)
    assert abs(eye[0] - last[0]) < 1e-6 and marker == first[:3]


def _shell(quads: dict[int, list]) -> carbin.Section:
    """A main-carbin body section: per LOD, quads given by their four corners."""
    verts, subs = [], []
    for lod, corners in quads.items():
        idx = []
        for quad in corners:
            n = len(verts)
            verts += [carbin.Vertex(p, (0.0, 0.0), (0.0, 0.0), (0.0, 1.0, 0.0)) for p in quad]
            idx += [n, n + 1, n + 2, n, n + 2, n + 3]
        subs.append(carbin.SubSection("body", lod, 4, (0.0, 1.0, 0.0, 1.0) * 2, idx))
    return carbin.Section("body", (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=subs)


def test_shell_lid_over_an_open_cockpit_steps_to_a_finer_lod():
    # Audi R10 (#157): the main carbin's LOD 5 closes the open cockpit with a flat
    # panel at the height of the wheel's top; LOD 4 has the opening. The windscreen
    # rays pass over the panel, the rays from the eye to the wheel don't.
    eye = (-0.2, 0.75, -0.08)
    wheel = cockpit.Wheel((-0.2, 0.55, -0.55), cockpit._norm((0.0, 0.3, 1.0)), 0.15)
    cf = types.SimpleNamespace(eye=eye, wheel=wheel)

    def flat(x0, x1, y, z0, z1):
        return [(x0, y, z0), (x1, y, z0), (x1, y, z1), (x0, y, z1)]

    sides = [flat(-1.0, -0.45, 0.6, -1.5, 0.6), flat(0.45, 1.0, 0.6, -1.5, 0.6)]
    shell = [_shell({4: sides, 5: [flat(-1.0, 1.0, 0.62, -1.5, 0.6)]})]
    assert not cockpit.shell_blocks_view(shell, 5, eye)  # the old rule kept LOD 5
    assert cockpit.shell_lids_cockpit(shell, 5, eye, wheel) >= cockpit.LID_RAYS
    assert cockpit.shell_closes(shell, 5, cf) == "cockpit"
    assert cockpit.shell_lids_cockpit(shell, 4, eye, wheel) == 0 and cockpit.shell_closes(shell, 4, cf) is None
    # A closed car's roof above the eye and its floor under the wheel are no lid.
    closed = [_shell({5: [flat(-1.0, 1.0, 1.2, -1.0, 1.0), flat(-1.0, 1.0, 0.1, -2.0, 1.0)]})]
    assert cockpit.shell_closes(closed, 5, cf) is None


def _leaf(tag: str, data: bytes) -> chnk.Entry:
    return chnk.Entry(tag, 0, len(data), 0, 0, None, payload=data)


def _node(tag: str, *children: chnk.Entry) -> chnk.Entry:
    return chnk.Entry(tag, 0, 0, 0, 0, None, children=list(children))


def _needle_interior(dials_ids: list[int], digital_ids: list[int], blind: bool = False) -> chnk.Entry:
    """An interior entry like a port on host 250 or 293: the host's needle material
    and the digital dials' material (a copy of digital_dash.VANILLA_MATERIAL) draw
    from one stride-16 DashboardInstrument buffer (position 4 x i16, then u, v, gauge
    id, component as f16), a quad per gauge id. With `blind`, the needle material also
    draws a quad from a buffer without the declaration."""
    from dsfforza.convert import digital_dash

    dials, digital = b"D" * 8, b"G" * 8

    def maco(uid: bytes, path: str) -> chnk.Entry:
        return _node("MACO", _leaf("MAHE", uid + bytes(8)), _leaf("MATX", path.encode() + b"\0"))

    def vxck(uid: bytes, ids: list[int], name_hash: int) -> chnk.Entry:
        vdcl = struct.pack("<I4I4I", 2, 0, 4, 1, 0, 8, 4, 8, name_hash)
        data = b"".join(struct.pack("<4h4e", 0, 0, 0, 0, 0.5, 0.5, gid, 0) for gid in ids for _ in range(4))
        head = uid + struct.pack("<II", len(ids) * 4, 16)
        return _node("VXCK", _leaf("VXHD", head), _leaf("VDCL", vdcl), _leaf("VXDT", data))

    quads = [(dials, q) for q in range(len(dials_ids))] + [
        (digital, len(dials_ids) + q) for q in range(len(digital_ids))
    ]
    indices = b"".join(struct.pack("<6H", 0, 1, 2, 2, 1, 3) for _ in quads)
    subs = [
        _node(
            "SBMC",
            _leaf(
                "SBBH",
                b"S%07d" % n
                + mat
                + b"V" * 8
                + bytes(8)
                + struct.pack("<I12x", q * 4)
                + b"I" * 8
                + struct.pack("<II", n * 6, 6),
            ),
        )
        for n, (mat, q) in enumerate(quads)
    ]
    drsp = [
        maco(dials, r"R:\Vehicles\Host\Textures\Host_Dials.matxml"),
        maco(b"T" * 8, r"R:\Vehicles\Host\Textures\Host_Interior.matxml"),
        maco(digital, r"R:\Vehicles\Chevrolet_Volt_2011\Textures\Chevrolet_Volt_2011_Digital_Dials.matxml"),
        vxck(b"V" * 8, dials_ids + digital_ids, cockpit.DASHBOARD_INSTRUMENT),
        _node("IXCK", _leaf("IXHD", b"I" * 8 + struct.pack("<II", len(indices) // 2, 2)), _leaf("IXDT", indices)),
        *subs,
    ]
    if blind:
        drsp.append(vxck(b"W" * 8, [2], 0x12345678))
        drsp.append(
            _node("SBMC", _leaf("SBBH", b"B" * 8 + dials + b"W" * 8 + bytes(24) + b"I" * 8 + struct.pack("<II", 0, 6)))
        )
    assert digital_dash.RPM[0] == 1 and digital_dash.SPEED_DIGITS[0][0] == 2
    return _node("VEHC", _node("MDXN", _node("DRSP", *drsp)))


def test_digital_dials_are_not_needles():
    # Issue #234: the digital dials draw g_fDigiData vertices with gauge ids 1 (rpm bar,
    # the Spark) and 2 (mph digits, the Volt) through the needles' declaration. The engine
    # reads those through no gauge record, so lint mustn't ask for one.
    h = upvh.parse(_gauge_hierarchy(2))  # a speedometer record only (host 249, the Spark's)
    spark = _needle_interior([2], [1, 2, 5, 6])
    assert cockpit.dials_material(None, spark) == b"D" * 8
    assert cockpit.needle_check(None, spark, h) == ([], 0)
    assert cockpit.needle_gauge_ids(None, spark, dials_only=True) == {2.0: 4}
    # The build's own check still sees every DashboardInstrument vertex.
    assert cockpit.needle_gauge_ids(None, spark) == {2.0: 8, 1.0: 4, 5.0: 4, 6.0: 4}
    # A real tachometer needle on a host without its record is still caught,
    assert cockpit.needle_check(None, _needle_interior([2, 1], [1, 5]), h) == ([1], 0)
    # and so is a needle drawn from a buffer without gauge ids.
    assert cockpit.needle_check(None, _needle_interior([2], [1], blind=True), h) == ([], 2)


def _open_cockpit(cowl_name: str = "hooda"):
    """Eye at the origin, a wheel 0.45 m ahead, and in front only the bodywork round the
    wheel (`cowl_name`): an open cockpit like the ARX-02a's, no dash."""
    axis = cockpit._norm((0.0, 0.03, 1.0))
    wheel = cockpit.Wheel((0.0, -0.25, -0.45), axis, 0.15)
    cowl = _quad_section(cowl_name, [(-0.8, -0.7, -0.8), (0.8, -0.7, -0.8), (0.8, -0.12, -0.8), (-0.8, -0.12, -0.8)])
    return {cowl_name: cowl, "steering_wheel": _wheel_disc(wheel.centre, axis, wheel.radius)}, wheel


def test_cowl_holds_a_virtual_display_in_an_open_cockpit():
    # Issue #147: the bodywork round an open cockpit's wheel is the car's outside
    # (hood*, a_pillar), so the dash search finds no room; the cowl search may use it.
    eye = (0.0, 0.0, 0.0)
    for name in ("hooda", "a_pillar"):
        static, wheel = _open_cockpit(name)
        d, _, report = cockpit.pod_display(static, eye, wheel, 2, {}, 2)
        assert d is None and report == {"virtual_display": "no room"}
        d, housing, report = cockpit.pod_display(static, eye, wheel, 2, {}, 2, cowl=True)
        assert d is not None and report["virtual_display"]["cowl"] is True, report
        assert -0.8 < d.centre[2] < -0.7  # in front of the cowl,
        assert cockpit._dot(d.normal, cockpit._norm(d.centre)) > 0.99  # facing the eye
        # and out from under the HUD's lower corners.
        down = math.degrees(math.atan2(-d.centre[1], math.hypot(d.centre[0], d.centre[2])))
        across = math.degrees(math.atan2(d.centre[0], -d.centre[2]))
        assert down <= cockpit.HUD_DOWN_DEG or -cockpit.HUD_LEFT_DEG <= across <= cockpit.HUD_RIGHT_DEG
    # The housing of one virtual display keeps a second one off it.
    static[housing.name] = housing
    d2, _, _ = cockpit.pod_display(static, eye, wheel, 2, {}, 2, cowl=True, name="dsf_second")
    assert d2 is not None
    corners = [d2.point(u, v) for u in d2.rect[:2] for v in d2.rect[2:]]
    box = cockpit.section_points(housing)
    lo = [min(p[i] for p in box) for i in range(2)]
    hi = [max(p[i] for p in box) for i in range(2)]
    assert not any(lo[0] < p[0] < hi[0] and lo[1] < p[1] < hi[1] for p in corners)
    # #92's dials on an open cockpit go there too.
    dials, report, _ = cockpit.virtual_gauges(_open_cockpit()[0], {}, eye, _Car(), 2, wheel=wheel)
    assert [g.family for _, _, g, _ in dials] == ["tach", "speed"] and report["virtual_display"]["cowl"]


def test_wheel_display_moves_onto_a_virtual_display():
    # Issue #147: DSF can't draw on the turning wheel, so the wheel's display patches go,
    # rigidly and with their UVs, onto a virtual display in view.
    eye = (0.0, 0.0, 0.0)
    static, wheel = _open_cockpit()
    on_wheel = cockpit.display_patches(static, eye, on_wheel=True)
    assert len(on_wheel) == 1 and cockpit.display_patches(static, eye) == []
    moved, housing, report = cockpit.wheel_display_pod(static, eye, wheel, {}, 2)
    assert housing.name == cockpit.DASH_PANEL and len(moved) == 1
    m, w = moved[0], on_wheel[0]
    assert m.section == cockpit.DASH_PANEL and m.uv0 == w.uv0 and m.transform == w.transform
    k = report["virtual_display"]["scale"]
    assert k in cockpit.WHEEL_DISPLAY_SCALES
    for ta, tb in zip(m.points, w.points):  # the same triangles, scaled by k
        for i in range(3):
            assert abs(math.dist(ta[i], ta[(i + 1) % 3]) - k * math.dist(tb[i], tb[(i + 1) % 3])) < 1e-6
    screen = cockpit.section_points(housing, {"dsf_dial_display"})
    n = cockpit._scale(m.towards_eye, -1)
    plane = cockpit._dot(screen[0], n)
    for tri in m.points:
        for p in tri:  # just in front of the screen
            assert abs(plane - cockpit._dot(p, n) - cockpit.WHEEL_DISPLAY_LIFT) < 1e-6
    centre = cockpit._mean([p for tri in m.points for p in tri])
    assert cockpit._dot(m.towards_eye, cockpit._norm(centre)) < -0.99  # facing the eye
    # Without a display on the wheel there's nothing to move.
    static["steering_wheel"].subsections = static["steering_wheel"].subsections[:1]
    assert cockpit.wheel_display_pod(static, eye, wheel, {}, 2)[:2] == ([], None)


def test_wheel_display_found_only_under_the_hud_tries_smaller_sizes():
    # Issue #147: the R15 TDI's, GT-R's and Skyline 2003's moved displays found room only
    # under DSF's HUD; a smaller one clear of it is drawn instead, and the larger one stays
    # when no smaller one fits either.
    eye = (0.0, 0.0, 0.0)
    static, wheel = _open_cockpit()
    real = displays.place_pod  # patched where wheel_display_pod looks it up
    for small_fits in (True, False):
        calls = []

        def fake(*args, passes=cockpit.POD_PASSES, sizes=None, calls=calls, small_fits=small_fits, **kw):
            calls.append((passes, sizes))
            d, housing, report = real(*args, passes=passes, sizes=sizes, **kw)
            if passes == cockpit.POD_PASSES:
                report["virtual_display"]["under_hud"] = True  # as if only the last pass found room
            elif not small_fits:
                return None, None, {"virtual_display": "no room"}
            return d, housing, report

        displays.place_pod = fake
        try:
            moved, housing, report = cockpit.wheel_display_pod(static, eye, wheel, {}, 2)
        finally:
            displays.place_pod = real
        assert [c[0] for c in calls] == [cockpit.POD_PASSES, cockpit.POD_HUD_PASSES]
        w = calls[0][1][0][0] / cockpit.WHEEL_DISPLAY_SCALES[0]
        assert [round(s[0] / w, 3) for s in calls[1][1]] == list(cockpit.WHEEL_DISPLAY_SMALL_SCALES)
        assert moved and housing is not None
        k = report["virtual_display"]["scale"]
        if small_fits:
            assert k in cockpit.WHEEL_DISPLAY_SMALL_SCALES and not report["virtual_display"].get("under_hud")
        else:
            assert k in cockpit.WHEEL_DISPLAY_SCALES and report["virtual_display"]["under_hud"]


def test_virtual_display_keeps_clear_of_the_hands():
    # The GT-R's small moved display sat behind the left hand in game (#147); with
    # hands=True no line of sight to the display passes within POD_HAND_CLEAR of a grip.
    eye = (0.0, 0.0, 0.0)
    static, wheel = _open_cockpit()
    grips = cockpit.rim_grips(wheel)
    for g in grips:  # on the rim, HAND_GRIP_DEG either side of its top
        assert abs(math.dist(g, wheel.centre) - wheel.radius) < 1e-9 and g[1] > wheel.centre[1]
    assert grips[0][0] * grips[1][0] < 0
    d, _, _ = cockpit.pod_display(static, eye, wheel, 1, {}, 2, cowl=True, hands=True, sizes=[(0.05, 0.03)])
    assert d is not None
    for u in d.rect[:2]:
        for v in d.rect[2:]:
            ray = cockpit._norm(d.point(u, v))
            for g in grips:
                hit = cockpit._scale(ray, cockpit._dot(g, wheel.axis) / cockpit._dot(ray, wheel.axis))
                assert math.dist(hit, g) > cockpit.POD_HAND_CLEAR


def test_placeholder_cockpit_gets_virtual_dials():
    # Issue #148: a placeholder cockpit carbin's cabin (the main carbin's) has no needle
    # and no display; the car still has a speedometer and a tachometer.
    eye = (0.0, 0.0, 0.0)
    axis = cockpit._norm((0.0, 0.36, 0.93))
    wheel = cockpit.Wheel((0.0, -0.25, -0.45), axis, 0.17)
    dash = _quad_section(
        "interior", [(-0.6, -0.6, -0.85), (0.6, -0.6, -0.85), (0.6, -0.02, -0.85), (-0.6, -0.02, -0.85)]
    )
    rim = [v.position for v in _wheel_disc(wheel.centre, axis, wheel.radius).lod0_vertices[1:5]]
    static = {"interior": dash, "steering_wheel": _quad_section("steering_wheel", rim, ("leather",))}
    assert cockpit.virtual_gauges(static, {}, eye, _Car(), 2, wheel=wheel) == ([], {}, {})
    dials, _, extra = cockpit.virtual_gauges(static, {}, eye, _Car(), 2, wheel=wheel, assume=("tach", "speed"))
    assert [g.family for _, _, g, _ in dials] == ["tach", "speed"] and set(extra) == {"dsf_dial_panel"}


def _coarse_wheel(centre, radius: float, tilt_deg: float):
    """A main-carbin LOD wheel (Forza space, the driver at +z): a 16-gon rim 3 cm
    either side of its plane with one vertex 3 cm out, three flat spokes and a hub."""
    t = math.radians(tilt_deg)
    axis = (0.0, math.sin(t), math.cos(t))
    u, v = (1.0, 0.0, 0.0), (0.0, math.cos(t), -math.sin(t))

    def at(r, a, d=0.0, w=0.0):
        side = [-math.sin(a) * u[i] + math.cos(a) * v[i] for i in range(3)]
        return tuple(
            centre[i] + r * (math.cos(a) * u[i] + math.sin(a) * v[i]) + d * axis[i] + w * side[i] for i in range(3)
        )

    pts = []
    for k in range(16):
        a = 2 * math.pi * k / 16
        out = radius + 0.015 + (0.03 if k == 8 else 0.0)
        pts += [at(out, a), at(radius - 0.015, a), at(radius, a, 0.03), at(radius, a, -0.03)]
    for spoke in (0, 180, 270):
        a = math.radians(spoke)
        pts += [at(f * radius, a, 0.0, w) for f in (0.25, 0.45, 0.65) for w in (-0.025, 0.025)]
    pts += [at(0.04, 2 * math.pi * k / 8, 0.02) for k in range(8)]
    return pts, axis


def _point_section(name, pts, technique="interior", lod=1):
    """A section of degenerate triangles, one per point (section_points sees each)."""
    verts = [carbin.Vertex(p, (0.0, 0.0), (0.0, 0.0), (0.0, 0.0, 1.0)) for p in pts]
    sub = carbin.SubSection(technique, lod, 4, (0, 1, 0, 1, 0, 1, 0, 1), [i for i in range(len(pts)) for _ in range(3)])
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])


def test_main_wheel_rim_is_a_circle_through_the_rim():
    # Issue #86: a main-carbin LOD wheel is a coarse polygon; the middle of its rim's
    # extent follows the vertex that sticks out furthest (E36 M3: the fit walked 6 cm
    # off the ring). The least-squares circle through the rim stays on the hub.
    centre = (-0.35, 0.62, -0.48)
    pts, axis = _coarse_wheel(centre, 0.19, 18.0)
    near = cockpit.Wheel((-0.35, 0.65, -0.40), cockpit._norm((0.0, 0.38, 0.925)), 0.162)
    w = cockpit.fit_main_wheel(_point_section("steering_wheel", pts), near)
    assert math.dist(w.centre, centre) < 0.005, w
    assert abs(w.radius - 0.19) < 0.005 and cockpit._dot(w.axis, axis) > 0.999, w


def test_main_wheel_fit_leaves_out_the_hub_badge():
    # Issue #86: cutout.cut_alpha_tested makes the hub badge's letters (the XKR-S's 4
    # triangles became 160); their points pulled the fit in to a 2 cm "rim".
    centre = (-0.38, 0.707, -0.205)
    pts, _ = _coarse_wheel(centre, 0.16, 21.0)
    wheel = _point_section("steering_wheel", pts)
    badge = [(centre[0] + 0.03 * math.cos(k), centre[1] + 0.03 * math.sin(k), centre[2] + 0.025) for k in range(400)]
    wheel.subsections.append(_point_section("x", badge, "badge").subsections[0])
    wheel.subsections[1].indices = [len(pts) + i for i in wheel.subsections[1].indices]
    wheel.lod_vertices += _point_section("x", badge).lod_vertices
    near = cockpit.Wheel(centre, cockpit._norm((0.0, 0.37, 0.93)), 0.161)
    w, bare = cockpit.fit_main_wheel(wheel, near), cockpit.fit_main_wheel(_point_section("steering_wheel", pts), near)
    assert math.dist(w.centre, bare.centre) < 1e-9 and w.radius == bare.radius, (w, bare)
    assert math.dist(w.centre, centre) < 0.005, w


def test_placement_forza_is_the_inverse_of_point():
    from dsfforza.convert.forza2dsf.vertices import Placement

    place = Placement(dy=-0.6324, dz=-0.0218)
    p = (0.4086, 0.8501, 0.2739)
    assert math.dist(place.forza(place.point(p)), p) < 1e-12


def test_host_wheel_reads_the_skinned_rim():
    # Issue #86: a placeholder cockpit keeps the host's interior; the chase views' wheel
    # goes onto the wheel that interior draws (its vertices bound to the steering record).
    from dsfforza.convert.forza2dsf.vertices import Placement
    from dsfforza.formats import vehicle_mesh as vm

    place = Placement(dy=-0.6, dz=0.05)
    centre = (0.37, 0.95, -0.30)  # Forza space
    rim = _ring(centre, 0.18, 20.0)
    other = [(x + 0.3, y, z) for x, y, z in rim]  # a door card: another binding
    skin, chassis = bytes((1, 1, 0, 1)), bytes((0, 0, 0, 1))
    data = b""
    for p, s in [*((p, skin) for p in rim), *((p, chassis) for p in other)]:
        dsf = place.point(p)
        data += struct.pack("<4h", *(round(c / vm.POSITION_SCALE) for c in dsf), 0) + s
    elements = [vm.VertexElement(0, 4, 1, 0, 0), vm.VertexElement(9, 4, 2, 0, 8)]
    n = len(rim) + len(other)
    mesh = vm.Mesh()
    mesh.vertex_buffers[b"v"] = vm.VertexBuffer(b"v", n, 12, elements, data)
    mesh.index_buffers[b"i"] = struct.pack(f"<{3 * n}H", *(i for i in range(n) for _ in range(3)))
    mesh.submodels.append(vm.SubModel(b"s", b"m", b"v", 0, b"i", 0, 3 * n))
    w = cockpit.host_wheel(mesh, skin, place.forza)
    assert math.dist(w.centre, centre) < 0.002 and abs(w.radius - 0.18) < 0.002, w
    assert cockpit.host_wheel(mesh, bytes((2, 2, 0, 1)), place.forza) is None


def test_chase_cabin_is_the_cockpit_cabin_without_outside_parts_or_glass():
    # Issue #429: the exterior entry draws the cockpit carbin's cabin; the bonnet, the
    # A-pillars, the mirrors and the glass are the exterior's own (the main carbin's).
    from dsfforza.convert.cockpit import chase

    def section(name, *techniques):
        s = _point_section(name, [(0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (0.0, 0.1, 0.0)], techniques[0], lod=0)
        for t in techniques[1:]:
            s.subsections.append(carbin.SubSection(t, 0, 4, (0, 1, 0, 1, 0, 1, 0, 1), [0, 1, 2]))
        return s

    static = {
        "interior": section("interior", "plastic", "window"),
        "steering_wheel": section("steering_wheel", "leather"),
        "seatL": section("seatL", "leather"),
        "a_pillar": section("a_pillar", "black"),
        "hooda": section("hooda", "body"),
        "mirrorL": section("mirrorL", "chrome"),
        "headlightL": section("headlightL", "head_light"),
        "cagerace": section("cagerace", "metal"),
        "lexanF": section("lexanF", "window", "black"),  # FM4's windscreen with its frit (#441: lexan is glass)
    }
    cabin = {s.name: [u.material for u in s.subsections] for s in chase.cabin_sections(static)}
    assert cabin == {"interior": ["plastic"], "steering_wheel": ["leather"], "seatL": ["leather"]}


def test_chase_eyes_ring_the_car_at_the_chase_camera():
    # The probe logs DSF's chase camera 5.5 to 5.8 m behind the car's origin, 1.65 to 1.77 m
    # up (cameraMode Normal); the ring is at that distance and height all round, in Forza space.
    from dsfforza.convert.cockpit import chase
    from dsfforza.convert.forza2dsf.vertices import Placement

    place = Placement(dy=-0.6, dz=0.05)
    eyes = chase.chase_eyes(place)
    assert len(eyes) == chase.CHASE_AZIMUTHS
    for e in eyes:
        d = place.point(e)
        assert abs(math.hypot(d[0], d[2]) - chase.CHASE_DISTANCE) < 1e-9 and abs(d[1] - chase.CHASE_HEIGHT) < 1e-9
    assert any(place.point(e)[2] < -5.7 for e in eyes)  # one behind the car (DSF -z), the chase view's
    rear = chase.chase_eyes(place, rear=True)  # an exterior over its cap: beside and behind the car only
    assert len(rear) == 7 and all(place.point(e)[2] <= 1e-9 for e in rear)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
