"""Checks for the decoded straight-line drive model (src/dsfforza/build/handling_sim.py,
research/handling.md, "Drive force, drag and coasting"). No game files.

uv run python tests/test_handling_sim.py     # or: uv run python -m tests handling_sim
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

from dsfforza.build import handling_sim as S


def _decompiled_step(w, d, s, te, cc, g, load, fd, ae, ad):
    """FUN_008C3CF0's closing expressions as Ghidra prints them (fVar7 = ae, fVar16 = ad,
    fVar15 = C, fVar12 = Kt, param_3 = load, fVar13 = Fd, fVar9 = 1 / g, fVar10 = w,
    fVar11 = d), the sign flip included."""
    kt = load / (d - s)
    if kt < 0:
        load, kt = -load, -kt
    inv = 1 / g
    f14 = (inv * cc + kt) * ad
    f9 = f14 + 1
    w2 = (((kt * d - (load + fd)) * ad + d) * cc * ae + (te * ae + w) * f9) / ((kt * ad + 1) * cc * ae * g + f14 + 1)
    d2 = (((w2 * cc + kt * d) - load - fd) * ad + d) / f9
    return w2, d2


def test_engine_step_is_the_decompiled_one():
    rng = random.Random(516)
    for _ in range(200):
        g = rng.uniform(0.25, 1.5)
        d = rng.uniform(500, 9000)
        s = d - rng.uniform(-300, 300)
        args = (
            rng.uniform(800, 9000),  # w
            d,
            s,
            rng.uniform(-50, 250),  # Te
            rng.uniform(0.0, 2.0),  # C
            g,
            rng.uniform(-2000, 2000),  # p361 x load
            rng.uniform(0, 20),  # Fd
            0.04 / rng.uniform(0.003, 0.02),
            0.04 / rng.uniform(0.003, 0.02),
        )
        got = S.engine_step(*args)
        want = _decompiled_step(*args)
        assert all(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6) for a, b in zip(got, want)), (args, got, want)


def test_axle_scale_and_governor():
    p = S.accessor({345: 0.5, 510: 0.0, 511: 0.0, 160: 100.0})
    # Balanced axles at zero downforce: FUN_008B8110's local_1c is its neutral 0.25.
    assert math.isclose(S.axle_scale(p, 40.0, rear=True), 0.25)
    assert math.isclose(S.axle_scale(p, 40.0, rear=False), 0.25)
    # p345 moves force rearwards: rear (0.58 - 0.25 x 2) x 2 ... = 0.29 / 0.21 of 0.25.
    q = S.accessor({345: 0.58, 510: 0.0, 511: 0.0})
    assert math.isclose(S.axle_scale(q, 0.0, rear=True), 0.25 * ((0.29 - 0.25) * 4 + 1))
    assert math.isclose(S.axle_scale(q, 0.0, rear=False), 0.25 * ((0.21 - 0.25) * 4 + 1))
    # The governor (FUN_008BC4D0) is 1 below 0.9 x p160 and 0 at p160 at full throttle.
    assert S.governor(p, 0.89 * 100 * S.MPH_TO_MS, 1.0) == 1.0
    assert math.isclose(S.governor(p, 100 * S.MPH_TO_MS, 1.0), 0.0, abs_tol=1e-9)


def test_torque_curve_knots():
    p = S.accessor({40: 1000.0, 42: 7000.0, 522: 0.6, 523: 0.7, 524: 0.5, 364: 0.2})
    assert math.isclose(S.torque_curve(p, 1000.0), 0.6)
    assert math.isclose(S.torque_curve(p, 4000.0), 1.0)
    assert math.isclose(S.torque_curve(p, 7000.0), 0.7)
    # The rev band: 0 at p42 x (1 + p364) at full throttle.
    assert S.torque_curve(p, 7000.0 * 1.2) == 0.0


def test_derived_torque_reproduces_the_wheel_force():
    # A flat 400 Nm Forza curve: DSF's steady wheel force 25 x (p18 c / 4 - friction) / (g p361)
    # equals 400 / (g p361 r) at every rpm once p18 x curve follows derived_torque.
    car = SimpleNamespace(torque_at=lambda _rpm: 400.0)
    params = {40: 1000.0, 42: 7000.0, 357: 0.0, 358: 0.005, 359: 0.0, 360: 0.33}
    vals = S.derived_torque(car, S.accessor(params))
    params.update(vals)
    p = S.accessor(params)
    for rpm in (1500.0, 4000.0, 6500.0):
        game = 0.25 * p(18) * S.torque_curve(p, rpm) - p(358) * rpm
        assert math.isclose(S.IMPULSE * game, 400.0 / 0.33, rel_tol=0.02), rpm


def _drive(stiffness: float) -> tuple[float, float]:
    """Mean acceleration over the last second and the lag-free value
    25 x (Te / g - 0.002 d) / p361 over the mass plus the reflected inertia."""
    params = {
        0: 1500.0, 13: 0.0, 509: 0.0, 18: 800.0, 40: 800.0, 42: 20000.0, 522: 1.0, 523: 1.0, 524: 0.5,
        160: 300.0, 183: 0.95, 282: 1.0, 276: 1.0, 355: 0.01, 356: 0.01, 357: 0.0, 358: 0.0,
        359: 0.0, 360: 0.33, 361: 0.25, 369: stiffness, 371: 0.0, 345: 0.5, 510: 0.0, 511: 0.0,
        376: 10.0, 377: 1.0, 378: 0.0, 516: 0.0,
    }  # fmt: skip
    p = S.accessor(params)
    run = S.simulate(p, seconds=3.0, v0=30.0)
    mean = sum(x.a for x in run[-25:]) / 25
    conv = p(361) * p(360) * S.RPM
    d = run[-1].v / conv
    reflected = S.IMPULSE * (p(355) + p(356)) / (p(361) * conv)
    return mean, S.IMPULSE * (0.25 * p(18) - 0.002 * d) / p(361) / (p(0) + reflected)


def test_drive_force_and_the_lagged_load():
    # Soft tyres slip enough that the one-step lag of the load costs little; stiff ones
    # fall short of the lag-free force by about (a x dt) / slip speed.
    soft, ideal = _drive(1500.0)
    assert 0.9 * ideal < soft <= 1.01 * ideal, (soft, ideal)
    stiff, ideal = _drive(10000.0)
    assert stiff < soft and 0.6 * ideal < stiff < 0.9 * ideal, (stiff, ideal)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
