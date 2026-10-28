"""Garage maker groups: port_batch.garage_group's rule (register.lua's is in
tests/test_register_asi.py). With dsfforza.asi (ASI_GROUP_CAP) a maker keeps all its
cars in one group; with the vanilla 20-car cap the rest go to the shared FORZA groups.

    uv run python tests/test_garage_groups.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

from dsfforza.build import port_batch


def test_garage_group_asi_one_group():
    counts = {"ID:1": 45}
    assert port_batch.garage_group("ID:1", "ID:1", counts, port_batch.ASI_GROUP_CAP, 119) == "ID:1"


def test_garage_group_vanilla_cap():
    counts = {"ID:1": 19}
    assert port_batch.garage_group("ID:1", "ID:1", counts, 19, 119) == "FORZA"
    counts["FORZA"] = 19
    assert port_batch.garage_group("ID:1", "ID:1", counts, 19, 119) == "FORZA 2"
    # no group left: not listed
    full = {f"M{i}": 1 for i in range(118)} | {"ID:1": 19, "FORZA": 19}
    assert port_batch.garage_group("ID:1", "ID:1", full, 19, 119) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
