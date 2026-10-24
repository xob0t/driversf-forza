"""Checks for vehicle_cameras (the Bonnet camera on the Forza windscreen); no game
files needed.

    uv run python tests/test_cameras.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import math
import struct

from dsfforza.build import handling
from dsfforza.convert import vehicle_cameras as vc


def quad(a, b, c, d):
    return [(a, b, c), (a, c, d)]


# A windscreen sloping back from (y 0.3, z 0.8) at its base to (y 0.7, z 0.2) at its top, 1.2 m wide.
WINDSCREEN = quad((-0.6, 0.3, 0.8), (0.6, 0.3, 0.8), (0.6, 0.7, 0.2), (-0.6, 0.7, 0.2))
BONNET = quad((-0.8, 0.25, 0.8), (0.8, 0.25, 0.8), (0.8, 0.2, 2.2), (-0.8, 0.2, 2.2))


def test_windscreen_centre_is_on_the_glass():
    w = vc.windscreen_centre(WINDSCREEN)
    assert w is not None and w[0] == 0.0
    assert abs(w[1] - (0.3 + vc.WINDSCREEN_HEIGHT * 0.4)) < 0.01  # the extent is sampled every cm
    # on the plane through base and top: z = 0.8 - (y - 0.3) * 1.5
    assert abs(w[2] - (0.8 - (w[1] - 0.3) * 1.5)) < 1e-9
    assert vc.windscreen_centre(quad((0.3, 0, 0), (0.6, 0, 0), (0.6, 1, 0), (0.3, 1, 0))) is None  # off centre


def test_bonnet_position_clears_the_glass():
    w = vc.windscreen_centre(WINDSCREEN)
    cam, rays = vc.bonnet_position(w, WINDSCREEN + BONNET)
    assert rays["over"] >= vc.MIN_CLEARANCE - 1e-6
    assert rays["above"] is None and rays["ahead"] is None
    assert cam[2] == w[2] + vc.BONNET_OFFSET[2]
    # Glass close under the offset point: the camera is raised to MIN_CLEARANCE over it.
    flat = quad((-0.6, 0.45, 0.0), (0.6, 0.45, 0.0), (0.6, 0.45, 2.0), (-0.6, 0.45, 2.0))
    cam, rays = vc.bonnet_position((0.0, 0.42, 1.0), flat)
    assert abs(cam[1] - (0.45 + vc.MIN_CLEARANCE)) < 1e-9 and rays["raised"] > 0


def test_bonnet_params_round_trip():
    ground = -0.73
    cam = (0.0, 0.48, 0.87)
    params = vc.bonnet_params(cam, ground)
    # Stock 147: p536 0.91, p537 0.87, ground 0.73 under the origin -> (0, 0.48, 0.87) in model space.
    assert abs(params[536] - 0.91) < 1e-9 and params[537] == 0.87
    vo3d = handling.VO3D(304, {0: 1500.0}).build()
    patched = vc.patch_vo3d(vo3d, params)
    assert (
        vc.bonnet_camera(patched, ground) == (0.0, 0.48 + 0.0, 0.87)
        or max(abs(a - b) for a, b in zip(vc.bonnet_camera(patched, ground), cam)) < 1e-6
    )


def test_host_ground_from_highest_hub():
    from dsfforza.formats import upvh

    hubs = [(0.8, -0.402, 1.3), (-0.8, -0.402, 1.3), (0.8, -0.391, -1.3), (-0.8, -0.391, -1.3)]

    class Fake:
        def table(self, name):
            assert name == "wheels"
            return [struct.pack("<4f", *h, 1.0) + bytes(16) for h in hubs]

    real = upvh.parse
    upvh.parse = lambda data: Fake()
    try:
        ground = vc.host_ground(b"", handling.VO3D(163, {360: 0.34}).build())
    finally:
        upvh.parse = real
    assert abs(ground - (-0.391 - 0.34)) < 1e-6


def ring_section(centre, axis_tilt_deg: float, radius: float, lod: int = 1):
    """A steering-wheel rim (main-carbin style: LOD >= 1) about `centre`, its axis
    (towards the driver, Forza +z) tilted up by axis_tilt_deg."""
    from dsfforza.formats import carbin

    t = math.radians(axis_tilt_deg)
    axis = (0.0, math.sin(t), math.cos(t))
    u = (1.0, 0.0, 0.0)
    v = (0.0, math.cos(t), -math.sin(t))
    verts, idx = [], []
    n = 48
    for k in range(n):
        a = 2 * math.pi * k / n
        for depth, r in ((0.0, radius), (0.0, radius * 0.85), (0.02, radius)):
            p = tuple(centre[i] + r * (math.cos(a) * u[i] + math.sin(a) * v[i]) + depth * axis[i] for i in range(3))
            verts.append(carbin.Vertex(p, (0.0, 0.0), (0.0, 0.0), axis))
    for k in range(n):
        a, b = 3 * k, 3 * ((k + 1) % n)
        idx += [a, b, a + 1, b, b + 1, a + 1, a, a + 2, b, b, a + 2, b + 2]
    sub = carbin.SubSection("leather", lod, 4, (0, 1, 0, 1, 0, 1, 0, 1), idx)
    return carbin.Section("steering_wheel", (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])


def test_main_wheel_aligned_onto_the_cockpit_wheel():
    from dsfforza.convert import cockpit

    target = cockpit.Wheel((-0.38, 0.63, -0.37), cockpit._norm((0.0, 0.41, 0.91)), 0.163)
    main = ring_section((-0.37, 0.65, -0.52), 10.0, 0.158)  # 15 cm ahead, flatter column (S5S Raptor-like)
    aligned, report = cockpit.align_main_wheel(main, target)
    assert aligned is not None and abs(report["move"] - 0.15) < 0.01
    w = cockpit.fit_wheel(cockpit.promote_lod(aligned))
    assert math.dist(w.centre, target.centre) < 0.002
    assert abs(w.radius - target.radius) < 0.002
    assert cockpit._dot(w.axis, target.axis) > 0.9999
    # An implausible fit (a wheel 1 m away) is left alone.
    far = ring_section((0.6, 0.65, -1.4), 10.0, 0.158)
    assert cockpit.align_main_wheel(far, target)[0] is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
