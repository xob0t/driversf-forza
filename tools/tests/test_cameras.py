"""Checks for vehicle_cameras (the Bonnet camera on the Forza windscreen); no game
files needed.

    python tools/tests/test_cameras.py     # or every test: python -m tools.tests
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import handling  # noqa: E402
import vehicle_cameras as vc  # noqa: E402


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
    assert vc.bonnet_camera(patched, ground) == (0.0, 0.48 + 0.0, 0.87) or \
        max(abs(a - b) for a, b in zip(vc.bonnet_camera(patched, ground), cam)) < 1e-6


def test_host_ground_from_highest_hub():
    import upvh
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
