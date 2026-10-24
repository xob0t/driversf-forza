"""Checks for the VO3D defaults and the DSF-first rule that a parameter a set donor
leaves out stays out (src/dsfforza/build/handling.py, handling_dsf.py). No game files.

uv run python tests/test_handling.py     # or: uv run python -m tests handling
"""

from __future__ import annotations

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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
