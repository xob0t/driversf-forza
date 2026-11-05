"""Checks for the VO3D defaults and the DSF-first rule that a parameter a set donor
leaves out stays out (src/dsfforza/build/handling.py, handling_dsf.py). No game files.

uv run python tests/test_handling.py     # or: uv run python -m tests handling
"""

from __future__ import annotations

import math
from types import SimpleNamespace

from dsfforza.build import handling as H
from dsfforza.build import handling_dsf as D


def test_exe_defaults():
    # FUN_0088AC20 sets 365 of the 370 typed parameters (research/handling.md, appendix).
    assert len(H.EXE_DEFAULTS) == 365
    # Off-road terms in the tyre model FUN_008B8110: p525 scales the surface grip, p526
    # the drive-torque feedback on soft ground; both 1, not 0.
    assert H.EXE_DEFAULTS[525] == 1.0 and H.EXE_DEFAULTS[526] == 1.0
    assert (H.EXE_DEFAULTS[519], H.EXE_DEFAULTS[520], H.EXE_DEFAULTS[521]) == (0.0, 0.5, 8.0)
    # Steering terms (FUN_008AF860) that 19 test-drive ports got as 0.
    assert [H.EXE_DEFAULTS[p] for p in (381, 383, 384, 390)] == [2.0, 10.0, 30.0, 3.0]
    assert H.same_f32(H.EXE_DEFAULTS[382], 0.05)
    # The named table agrees with the exe wherever it states a default.
    for pid, p in H.PARAMS.items():
        if pid in H.EXE_DEFAULTS and pid not in H.BOOL_PIDS:
            assert H.same_f32(p.default, H.EXE_DEFAULTS[pid]), pid
    # Bools hold raw integer bits.
    assert H.RECORD.pack(239, H.FILL, H.EXE_DEFAULTS[239])[4:] == (1).to_bytes(4, "little")


def test_value_reads_the_exe_default():
    params = {0: 1500.0, 526: 1.2}
    assert D.value(params, 526) == 1.2
    assert D.value(params, 525) == 1.0
    assert D.value(params, 390) == 3.0
    assert D.value(params, 223) == 0.0  # typed, no default: stays 0
    assert H.VO3D(1, params).get(521) == 8.0


def test_a_donor_absence_stays_an_absence():
    host = {0: 1500.0, 517: 1.8, 381: 5.0}
    # The donor leaves p526 out: the port leaves it out too (it was written as 0).
    assert D.leave_out(host, {}, 526, D.value({}, 526))
    # The host sets p381, the donor doesn't: the default has to be written over it.
    assert not D.leave_out(host, {}, 381, D.value({}, 381))
    # A real value is always written, a default after an earlier rule's value too.
    assert not D.leave_out(host, {}, 526, 1.2)
    assert not D.leave_out(host, {526: 1.2}, 526, 1.0)


def _forza_car(lock: float) -> H.ForzaCar:
    """A plain RWD coupe as handling.load_forza returns it, with SteerMaxAngle `lock`."""
    return H.ForzaCar(
        media="TEST_Coupe_10", source="", mass=1400.0, front_weight=0.52, cm_height=0.45,
        block=(1.8, 1.2, 4.3), wheelbase=2.6, track_front=1.55, track_rear=1.56,
        bbox_min=(-0.9, 0.0, -2.2), bbox_max=(0.9, 1.3, 2.2), tyre_radius_front=0.32,
        tyre_radius_rear=0.33, drive="RWD", rear_split=1.0, final_drive=3.5,
        gears=[3.5, 2.2, 1.5, 1.2, 1.0, 0.8], reverse=3.4, shift_time=0.2, redline=7000.0,
        stall=800.0, curve_rpm=[1000.0, 4000.0, 7000.0], curve_nm=[250.0, 400.0, 350.0],
        top_speed_mph=170.0, steer_max_angle=lock, drag=0.3, spring_front=80.0, spring_rear=90.0,
    )  # fmt: skip


def test_steering_lock_is_forzas():
    # Issue #54: a donor's larger lock put the tyres through the arches at full lock;
    # p298 is Data_Car.SteerMaxAngle on every port, own-model hosts included.
    host = H.Host(200, H.VO3D(200, {0: 1300.0, 298: 40.0, 379: 1.1, 470: 25.0}), 2.5, 1.5, -0.35, 0.0)
    for lock in (30.0, 36.0, 42.0):
        vo3d, changes = H.build_vo3d(host, _forza_car(lock))
        assert vo3d.params[298] == lock
        assert any(c.pid == 298 and c.new == lock for c in changes)
        # the speed-sensitive steering stays the host's
        assert vo3d.params[379] == 1.1 and vo3d.params[470] == 25.0
    assert D.RULES[298] == "Z" and D.SAME_MODEL_RULES[298] == "Z"
    # so the steering-set donor's p298 is not copied (only rule-H parameters are)
    assert 298 in D.FEEL_GROUPS["steering"]


def test_top_speed_and_shift_time_are_the_cars():
    # Issue #462: p160 is the world speed DSF shows as Forza's top speed (FUN_008994F0,
    # x 1.3 from 160 mph on), with no cap at 160; p516 is Forza's shift time.
    car = _forza_car(42.0)
    car.top_speed_mph = 268.0
    ctx = SimpleNamespace(car=car, base={0: 1300.0}, host={"id": 200, "track": 1.5, "hub_y": -0.35, "axle_mid": 0.0})
    z = D.forza_values(ctx)
    assert abs(z[160] - 268.0 / 1.3) < 1e-6
    assert abs(H.displayed_speed(z[160] / H.MPH) * H.MPH - 268.0) < 1e-6
    assert z[516] == car.shift_time
    # below 22 m/s (49 mph) the shown speed is the world speed
    assert H.world_speed(20.0) == 20.0 and abs(H.displayed_speed(H.world_speed(50.0)) - 50.0) < 1e-9


def test_gearbox_is_forzas():
    # Issue #462: the port's gear count and ratios are Forza's (the host's count gave the
    # Sagaris 6 gears instead of 5, the LFA 5 instead of 6).
    car = _forza_car(42.0)
    rels, reverse, p361 = H.dsf_gearbox(car)
    assert len(rels) == len(car.gears) == 6
    # each gear's speed at the redline is the world speed DSF shows as Forza's
    for rel, gear in zip(rels, car.gears):
        forza = car.redline * math.pi / 30 * car.drive_radius / (gear * car.final_drive)
        dsf = rel * p361 * car.drive_radius * car.redline * math.pi / 30
        assert abs(H.displayed_speed(dsf) - forza) < 1e-6
        if forza <= H.DISPLAY_KNEE:  # below 22 m/s the reduction is Forza's
            assert abs(1 / (rel * p361) - gear * car.final_drive) < 1e-9
    assert reverse < 0 and abs(1 / (reverse * p361) + abs(car.reverse) * car.final_drive) < 1e-9
    # A 7-speed keeps gears 1..5 and its top gear (DSF holds six).
    car.gears = [3.2, 2.2, 1.7, 1.3, 1.06, 0.88, 0.78]
    rels, _, p361 = H.dsf_gearbox(car)
    assert len(rels) == 6
    top = rels[-1] * p361 * car.drive_radius * car.redline * math.pi / 30
    assert abs(H.displayed_speed(top) - car.redline * math.pi / 30 * car.drive_radius / (0.78 * car.final_drive)) < 1e-6


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
