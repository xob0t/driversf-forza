"""Garage maker groups past 20 cars: gui_assets.continuation_groups labels and
port_batch.garage_group's rule (register.lua's is in tests/test_register_asi.py).
continuation_groups reads the stock vehicleStats from out/lua-src (worktree-setup).

    uv run python tests/test_garage_groups.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

from dsfforza.build import port_batch
from dsfforza.game import gui_assets


def test_continuation_labels_new_maker():
    hud = {i: {"ManufacturerName": "ID:700001"} for i in range(1, 46)}  # 45 cars of a new maker
    hud.update({i: {"ManufacturerName": "ID:700002"} for i in range(100, 110)})  # 10: fits one group
    labels = gui_assets.continuation_groups(hud, {700001: "Testmake", 700002: "Small"}, 700100)
    assert labels == {"ID:700001": {700100: "Testmake 2", 700101: "Testmake 3"}}


def test_garage_group_continuation():
    counts = {"ID:1": 19}
    assert port_batch.garage_group("ID:1", "ID:1", counts, 19, 119) == "ID:1 #2"
    counts["ID:1 #2"] = 19
    assert port_batch.garage_group("ID:1", "ID:1", counts, 19, 119) == "ID:1 #3"
    # no group left for a continuation: the shared FORZA group, as before
    full = {f"M{i}": 1 for i in range(117)} | {"ID:1": 19}
    assert port_batch.garage_group("ID:1", "ID:1", full, 19, 119) == "FORZA"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
