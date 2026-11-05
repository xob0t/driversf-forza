"""Engine sound pitch (issue #461): the recording rpm a vanilla pitch curve was sampled for,
crossfade supports, the vehicle_rpm range the game sends, and the pitch curves of loops that
are crossfaded together, which share their x points so they stay in tune with each other.
No game data is read.

uv run python tests/test_engine_sound_pitch.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import itertools
import math

import numpy as np

from dsfforza.convert import engine_sound as es

# The vanilla 370Z bank 227's x grid (every vanilla pitch curve is sampled on such a grid).
GRID = [0, 94, 188, 375, 750, 1125, 1500, 2250, 3000, 4500, 6000, 9000, 12000]


def _vanilla_curve(r: float) -> list[tuple[float, float, int]]:
    return [(x, es.cents(x, r), 4) for x in GRID]


def test_recording_rpm_is_the_rpm_the_curve_was_sampled_for():
    # 227's first loop: sampled for 902 rpm; its straight segments cross 0 cents at about 921
    pts = _vanilla_curve(902.0)
    assert abs(es.recording_rpm(pts) - 902.0) < 0.01
    crossing = next(
        x0 + (x1 - x0) * -y0 / (y1 - y0) for (x0, y0, _), (x1, y1, _) in itertools.pairwise(pts) if y0 <= 0 <= y1
    )
    assert 915 < crossing < 925
    # clamped points (-4800 near 0 rpm) don't count
    assert abs(es.recording_rpm(_vanilla_curve(6042.0)) - 6042.0) < 0.01


def test_support_of_vanilla_crossfades():
    assert es.support([(0, 255, 9), (902, 255, 4), (1696, 0, 9)]) == (-math.inf, 1696)
    assert es.support([(902, 0, 1), (1696, 255, 7), (2058, 0, 9)]) == (902, 2058)
    assert es.support([(5220, 0, 1), (6042, 255, 9), (12000, 255, 4)]) == (5220, math.inf)


def test_vehicle_rpm_span():
    lo, hi = es.vehicle_rpm_span(800, 9163)
    assert lo == 800 * (1 - es.RPM_JITTER) and hi == 9163 * (1 + es.RPM_JITTER)
    # the LFA: p40 650, p42 9000, p364 0.2: the engine drives itself to 10800 rpm, x 1.1 noise
    lo, hi = es.vehicle_rpm_span(800, 9163, (650.0, 9000.0, 0.2))
    n = (9000 * 1.2 * 1.1 - 650) / (9000 - 650)
    assert abs(hi - (800 + 8363 * n) * (1 + es.RPM_JITTER)) < 1e-6


def _layer(rpms: list[float], n: int = 11) -> dict:
    """A vanilla-like rpm layer: triangles between neighbours, the first full below its peak,
    the last full above its peak."""
    out = {}
    for k, _r in enumerate(rpms):
        a = -math.inf if k == 0 else rpms[k - 1]
        b = math.inf if k == len(rpms) - 1 else rpms[k + 1]
        out[k] = ((a, b), n)
    return out


def test_shared_grid_keeps_crossfaded_loops_in_tune():
    rpms = [1067.0, 2180.0, 3437.0, 4459.0, 5482.0, 6474.0, 8083.0, 9868.0]  # the LFA's loops
    span = (792.0, 12168.0)
    layer = _layer(rpms)
    grids = es.shared_grid(layer, span)
    assert grids is not None
    xs = np.arange(span[0], span[1], 5.0)
    errors = {}
    for k, r in enumerate(rpms):
        g = grids[k]
        assert len(g) == 11 and g[0] == 0 and all(b > a for a, b in itertools.pairwise(g))
        pts = [(x, es.cents(x, r)) for x in g]
        errors[k] = es.curve_at_many(pts, xs) - np.array([es.cents(x, r) for x in xs])
        # exact at its own recording rpm
        assert abs(es.curve_at(pts, r) - 0.0) < 1e-6
    for k in range(len(rpms) - 1):
        (a, _), _n = layer[k + 1]
        b = rpms[k + 1]
        both = (xs > a) & (xs < b)
        # two neighbours heard together are off by the same amount: no detune between them
        assert np.abs(errors[k][both] - errors[k + 1][both]).max() < 1e-6
        assert np.abs(errors[k][both]).max() < 15  # cents
    # the top loop's curve reaches past 12000 when the game sends more
    assert grids[len(rpms) - 1][-1] == 12168.0


def test_shared_grid_puts_leftover_points_below_the_loop():
    rpms = [1000.0, 2000.0, 4000.0]
    layer = {0: ((-math.inf, 2000.0), 13), 1: ((1000.0, 4000.0), 11), 2: ((2000.0, math.inf), 12)}
    grids = es.shared_grid(layer, (1000.0, 6000.0))
    for k, (_ab, n) in layer.items():
        assert len(grids[k]) == n
    # loop 0 is heard only with loop 1 (the span starts at its recording rpm) and has two points
    # more: they go below 1000, where the game sends nothing
    both = [[x for x in grids[k] if 1000.0 <= x <= 2000.0] for k in (0, 1)]
    assert both[0] == both[1] and len(both[0]) > 2
    assert len([x for x in grids[0] if 0 < x < 1000]) >= 2
    assert all(abs(es.curve_at([(x, es.cents(x, r)) for x in grids[k]], r)) < 1e-6 for k, r in enumerate(rpms))


def test_shared_grid_refuses_too_few_points():
    layer = {0: ((-math.inf, math.inf), 3)}  # knots 950, 6000 and the 12000 end need 4 with 0
    assert es.shared_grid(layer, (950.0, 6000.0)) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
