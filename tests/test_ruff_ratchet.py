"""The ruff ratchet's comparison (tests/ruff_ratchet.py): a count above the baseline is new,
one below it asks for --update, a file missing from the baseline starts at zero."""

from tests import ruff_ratchet


def test_compare():
    baseline = {"a.py": {"C901": 2, "ARG001": 1}, "gone.py": {"BLE001": 1}}
    current = {"a.py": {"C901": 3}, "new.py": {"FBT001": 1}}
    over, under = ruff_ratchet.compare(current, baseline)
    assert over == [("a.py", "C901", 3, 2), ("new.py", "FBT001", 1, 0)]
    assert under == [("a.py", "ARG001", 0, 1), ("gone.py", "BLE001", 0, 1)]


def test_counts():
    found = [
        {"path": "b.py", "code": "ARG001"},
        {"path": "a.py", "code": "C901"},
        {"path": "b.py", "code": "ARG001"},
    ]
    assert ruff_ratchet.counts(found) == {"a.py": {"C901": 1}, "b.py": {"ARG001": 2}}
