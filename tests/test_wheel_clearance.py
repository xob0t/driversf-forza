"""Checks for the wheel clearance survey and the hub settle (research/wheels.md, "Wheel
clearance"); no game files needed.

    uv run python tests/test_wheel_clearance.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import struct

import numpy as np

from dsfforza.build import handling
from dsfforza.convert import forza2dsf
from dsfforza.convert import wheel_clearance as wc
from dsfforza.formats import chnk, upvh

# The #2 Audi A4 DTM on host 162 (out/final: AUD_2_A4_11): suspension set from the Alfa 8C.
A4 = {0: 1050.0, 1: 2100.0, 3: 0.29, 4: 0.05, 25: -0.02, 30: -0.3709, 32: -0.0456, 188: 0.075, 292: -0.02, 505: 1.129}
A4_HUBS = [(-0.793, -0.3963, 1.3409), (0.793, -0.3963, 1.3409), (-0.7926, -0.4043, -1.4322), (0.7926, -0.4043, -1.4322)]


def test_rest_offset_follows_the_engine_terms():
    even = {0: 1000.0, 1: 2000.0, 3: 0.3}
    up = wc.rest_offsets(even, 1.3, -1.3)
    # no dead zone, no ride offset, an even load: only the p1/p469 term (2%) of the sag
    assert abs(up["front"]) < 0.003 and abs(up["rear"]) < 0.003
    assert abs(wc.rest_offsets({**even, 25: -0.02}, 1.3, -1.3)["front"] - up["front"] - 0.02) < 1e-9
    # p4: no force below it, full force from p4 + 0.1, so the spring sits deeper
    deep = wc.rest_offsets({**even, 4: 0.05}, 1.3, -1.3)
    assert deep["front"] > up["front"] + 0.015
    # clamped to the visual travel
    assert wc.rest_offsets({**even, 25: -0.5}, 1.3, -1.3)["front"] == 0.2
    # the A4: 39 mm at the front, 33 at the rear (p505 stiffens the rear springs)
    a4 = wc.rest_offsets(A4, 1.3409, -1.4322)
    assert abs(a4["front"] - 0.0394) < 0.001 and abs(a4["rear"] - 0.0331) < 0.001


def _box(lo, hi):
    """Triangles of an axis-aligned box."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    c = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    return np.array([[c[a], c[b], c[d]] for a, b, _, d in faces] + [[c[b], c[e], c[d]] for _, b, e, d in faces])


def test_gap_is_the_distance_to_the_arch():
    wheel = wc.Wheel("FL", np.array([0.8, 0.0, 1.3]), True, 0.33, 0.25, np.zeros(3))
    # a wing 30 mm over the tyre, as wide as the car's side
    wing = _box((0.5, 0.36, 0.9), (1.0, 0.40, 1.7))
    pts = wc.near_points(wing, wheel.hub, 0.6)
    assert abs(wc.gap(pts, wheel) - 0.03) < 0.004
    assert abs(wc.gap(pts, wheel, up=0.05) + 0.02) < 0.004  # 20 mm through it
    # only the outboard half counts: a liner over the inner half is hidden by the tyre
    liner = _box((0.5, 0.30, 0.9), (0.78, 0.31, 1.7))
    assert wc.gap(wc.near_points(liner, wheel.hub, 0.6), wheel) > 0.1
    # turned 40 degrees the tyre's front swings 0.31 m out from the hub, through a wing
    # skin 75 mm beside the tyre at rest
    skin = _box((1.0, -0.1, 1.35), (1.05, 0.2, 1.6))
    pts = wc.near_points(skin, wheel.hub, 0.6)
    assert abs(wc.gap(pts, wheel) - 0.075) < 0.004
    assert wc.gap(pts, wheel, steer_deg=40) < -0.05 or wc.gap(pts, wheel, steer_deg=-40) < -0.05
    # a suspension arm under the car behind the hub, inboard of the tyre: at full lock
    # the tyre's trailing inner half swings over it, hidden, and it doesn't count
    arm = _box((0.60, -0.15, 1.12), (0.66, -0.10, 1.16))
    pts = wc.near_points(arm, wheel.hub, 0.6)
    assert min(wc.gap(pts, wheel, steer_deg=a) for a in (-42, 42)) > 0


def test_forza_lock_replaces_p298():
    car = wc.Car(1, np.zeros((0, 3, 3)), [], {298: 40.0})
    assert wc._with_forza_lock(car, 30.0).p(298) == 30.0
    assert wc._with_forza_lock(wc.Car(1, np.zeros((0, 3, 3)), [], {298: 40.0}), None).p(298) == 40.0


def _hierarchy(hubs) -> bytes:
    """A minimal UPVH: no records, a hinge and a wheel entry at each hub."""
    head = bytearray(upvh.HEADER_SIZE)
    head[:4] = b"AWHF"
    struct.pack_into("<6I", head, 4, 0, 0, 26, 0, 1, 0)
    head[0x2A] = len(hubs)
    struct.pack_into("<H", head, 0x52, len(hubs))
    hinges = b"".join(struct.pack("<4f4f2f8x", *h, 1.0, 1.0, 0, 0, 0, 0, 0) for h in hubs)
    wheels = b"".join(struct.pack("<4f4f", *h, 1.0, 0, 0, 0, 1) for h in hubs)
    return bytes(head) + hinges + wheels


def _leaf(tag: str, data: bytes) -> chnk.Entry:
    e = chnk.Entry(tag, 0, len(data), 1, 2, None)
    e.payload = data
    return e


def test_settle_lowers_hubs_and_cog():
    vo3d = handling.VO3D(1, {**A4, 536: 0.2})  # a bonnet camera height
    nonr = chnk.Entry(
        "NONR", 0, 0, 2, 4, None, children=[_leaf("VO3D", vo3d.build()), _leaf("UPVH", _hierarchy(A4_HUBS))]
    )
    full = chnk.Entry("VEHC", 0, 0, 2, 4, None, children=[nonr])
    report = forza2dsf.settle_wheels(None, full, log=lambda *_: None)
    assert report["front_mm"] == 39.4 and report["rear_mm"] == 33.1 and report["hubs_patched"] == 8
    h = upvh.parse(nonr.children[1].payload)
    hubs = [w[:3] for w in h.vec4s("wheels")]
    assert abs(hubs[0][1] - (-0.3963 - 0.0394)) < 2e-4 and abs(hubs[3][1] - (-0.4043 - 0.0331)) < 2e-4
    assert [abs(w[0]) for w in hubs] == [abs(w[0]) for w in hubs[::-1]][::-1]
    assert all(abs(a[1] - b[1]) < 1e-6 for a, b in zip(h.vec4s("hinges"), hubs))  # the hinges came along
    params = handling.VO3D.parse(nonr.children[0].payload).params
    assert abs(params[30] - (-0.3709 - (0.0394 + 0.0331) / 2)) < 2e-4
    assert abs(params[536] - (0.2 + (0.0394 + 0.0331) / 2)) < 2e-4  # the camera rises with the body
    assert 301 not in params  # mounts the VO3D leaves out stay out
    # the physics sees the hubs from the CoG: unchanged on average
    assert abs((hubs[0][1] + hubs[3][1]) / 2 - params[30] - ((-0.3963 - 0.4043) / 2 + 0.3709)) < 2e-4


def _maxdata_car(folder, name: str, wheel_z: float):
    """A ForzaCar with a MAXData.xml (BottomCenterWheelbasePos z 0.1) and a tyre-only wheel
    model of radius 0.3 and width 0.2 whose centre sits wheel_z along the car."""
    from dsfforza.formats import carbin

    (folder / "Physics").mkdir()
    (folder / "Physics" / "MAXData.xml").write_text(
        '<MAXData><Misc Wheelbase="2.6" FrontTrackOuter="1.8" FrontStockRideHeight="0.12" '
        'RearStockRideHeight="0.12"><BottomCenterWheelbasePos x="0" y="0" z="0.1"/></Misc></MAXData>'
    )
    verts = [
        carbin.Vertex((x, 0.3 * np.sin(a), wheel_z + 0.3 * np.cos(a)), (0.0, 0.0), (0.0, 0.0), (0.0, 1.0, 0.0))
        for a in np.linspace(0, 2 * np.pi, 16, endpoint=False)
        for x in (-0.1, 0.1)
    ]
    tris = [i for k in range(16) for i in (2 * k, 2 * k + 1, (2 * k + 2) % 32)]
    sub = carbin.SubSection("tire", 1, 4, (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0), tris)
    wheel = [carbin.Section("wheel", (0.0, 0.0, wheel_z), (0.0,) * 3, (0.0,) * 3, verts, [], [sub], 3)]
    return forza2dsf.ForzaCar(name, folder, wheel, [], wheel)


def test_wheel_model_z_offset_moves_the_midpoint():
    """Forza draws its wheel model unturned at the hubs, so a tyre 0.3 m behind the model's
    origin (the Commodore VE race cars) stands 0.3 m behind the physics' hubs, where the
    arches are; the converter centres the tyre on the hub, so the hubs go there (#301)."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "a").mkdir()
        (Path(tmp) / "b").mkdir()
        plain = forza2dsf.car_dims(_maxdata_car(Path(tmp) / "a", "X", 0.0))
        moved = forza2dsf.car_dims(_maxdata_car(Path(tmp) / "b", "X", 0.3))
    assert abs(plain.mid_z + 0.1) < 1e-9
    assert abs(moved.tyre_centre[2] - 0.3) < 1e-6
    assert abs(moved.mid_z - (plain.mid_z + 0.3)) < 1e-6
    assert abs(moved.radius - plain.radius) < 1e-9 and moved.wheelbase == plain.wheelbase


def test_mid_z_fix_replaces_maxdata():
    """CIT_C1_11 carries the 107's MAXData.xml; its own midpoint is MID_Z_FIXES' (#301)."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        dims = forza2dsf.car_dims(_maxdata_car(Path(tmp), "CIT_C1_11", 0.0))
    assert abs(dims.mid_z - forza2dsf.MID_Z_FIXES["cit_c1_11"]) < 1e-9
    assert abs(dims.mid_z - (-0.019)) < 1e-9


def test_wheel_collision_takes_the_drawn_tyre():
    """Each wheel record's collision cylinder (PDL extents: half width, radius) takes its axle's
    drawn tyre, as vanilla's do; the engine places the drawn wheel where its cylinder meets the
    road, so the H1 Alpha's 0.468 m tyres on the Q7's 0.386 m cylinders sank 8 cm (#317)."""
    import math
    from unittest import mock

    wheels = struct.pack("<4f4f", 0.8, -0.5, 1.5, 1, 0, 0, 0, 1) + struct.pack("<4f4f", 0.8, -0.5, -1.5, 1, 0, 0, 0, 1)

    def model(ext, n=1):
        prim = bytearray(0x60)
        struct.pack_into("<3f", prim, 0x50, *ext)
        return struct.pack("<IIfI", 0x10 + n * 0x60, n * 0x60, 0.559, n) + bytes(prim) * n

    models = [model((0.117, 0.386, 0.0)), model((0.117, 0.386, 0.0)), model((0.5, 0.2, 0.3))]
    offsets, at = [], 0
    for m in models:
        offsets.append(at)
        at += len(m)
    pdl = struct.pack("<4I", 1, len(models), at, 0x10) + b"".join(models)
    data = wheels + pdl

    def record(index, wheel, collision):
        return upvh.Record(index, None, 2, 27, 1, -1, -1, -1, collision, -1, -1, 0, -1, 0, -1, -1, wheel, b"")

    h = upvh.Hierarchy(b"\0" * 8, 0, 0, 1, len(pdl), {"wheels": 2}, {"wheels": 0}, data=data)
    h.records = [
        record(0, 0, offsets[0]),
        record(1, 1, offsets[1]),
        record(2, 1, offsets[2]),
        record(3, -1, offsets[2]),
    ]
    with mock.patch.object(upvh, "parse", lambda _data: h):
        out, sized = forza2dsf.fit_wheel_collision(data, (0.468, 0.295), (0.47, 0.31))
    assert sized == 2

    def ext(k):
        at = len(wheels) + 0x10 + offsets[k]
        return struct.unpack_from("<f", out, at + 8)[0], struct.unpack_from("<3f", out, at + 0x10 + 0x50)

    radius, e = ext(0)  # the front wheel: the front tyre
    assert abs(e[0] - 0.1475) < 1e-6 and abs(e[1] - 0.468) < 1e-6 and e[2] == 0.0
    assert abs(radius - math.sqrt(0.1475**2 + 2 * 0.468**2)) < 1e-5
    assert abs(ext(1)[1][1] - 0.47) < 1e-6 and abs(ext(1)[1][0] - 0.155) < 1e-6  # the rear wheel: the rear tyre
    assert all(abs(a - b) < 1e-6 for a, b in zip(ext(2)[1], (0.5, 0.2, 0.3)))  # not a cylinder: left as it is
