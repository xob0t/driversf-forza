"""register.lua's HUD badge and nameplate for ported cars (lupa, Lua 5.1, the stubbed
game of tests/test_register_asi.py): a car never shows another model's nameplate.

A Ford Ka (FOR_Ka_11, 2011, host 249 Aston Martin Cygnet) showed the Ford badge and
the stock "SHELBY GT500" plate: without its hud.lua entry register.lua gave a ported
car of a make DSF has the plate of that make's closest-year stock car. For 2011 three
stock Fords tie (2010 Shelby GT500 194, Taurus SHO 196, F-150 251) and the first in
pairs() order wins: the Shelby (Model 186) in the game, the F-150 (178) here. Now such
a car gets the make's badge and no plate (Model 32, " ", which the renderer drops), and
a hud.lua entry made for another car isn't used.

    uv run python -m tests register_hud
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "register_asi_harness", Path(__file__).resolve().parent / "test_register_asi.py"
)
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)

BLANK = 32
FORD, SHELBY_GT500 = 43, 186  # stock vehicleStats[194]: Ford badge, "SHELBY GT500" plate
CARS = {  # id: (media, host, make, model, year)
    404: ("FOR_Ka_11", 249, "Ford", "Ka", "2011"),
    412: ("FOR_ShelbyGT500_10", 194, "Ford", "Shelby GT500", "2010"),
    500: ("KOE_Agera_11", 225, "Koenigsegg", "Agera", "2011"),  # a make DSF doesn't have
}


def _ported(cars=CARS) -> str:
    rows = [
        f'[{i}] = {{ host = {host}, media = "{media}", name = "{make} {model}", make = "{make}", '
        f'model = "{model}", year = "{year}" }},'
        for i, (media, host, make, model, year) in cars.items()
    ]
    return "return { cars = {\n" + "\n".join(rows) + "\n} }"


def _hud(entries: dict[int, tuple[str, int, int]]) -> str:
    rows = [
        f'[{i}] = {{ Manufacturer = {mf}, Model = {mo}, Picture1 = "dsfforza_{media.lower()}", GarageEmblem = 1 }},'
        for i, (media, mf, mo) in entries.items()
    ]
    return "return {\n" + "\n".join(rows) + "\n}"


def _stats(hud: str | None):
    lua, out = _harness.run(True, cars=_ported(), hud=hud)
    stats = lua.globals().vehicleStats
    return {i: (stats[i].Manufacturer, stats[i].Model, stats[i].Picture1) for i in CARS}, out


def test_no_hud_lua_gives_no_other_cars_plate():
    stats, out = _stats(None)
    assert stats[404][:2] == (FORD, BLANK), stats[404]  # was (43, 186) in game, "SHELBY GT500"
    assert stats[412][:2] == (FORD, BLANK)
    assert stats[500][:2] == (BLANK, BLANK)  # was the host's badge and plate
    assert any("no dsfforza\\hud.lua" in line for line in out)


def test_another_builds_hud_lua_is_not_used():
    # A subset build put the Shelby at the Ka's ID: its entry must not reach the Ka.
    stats, out = _stats(_hud({404: ("FOR_ShelbyGT500_10", FORD, SHELBY_GT500)}))
    assert stats[404][:2] == (FORD, BLANK), stats[404]
    assert not str(stats[404][2]).startswith("dsfforza_")
    assert any("hud.lua entry is for for_shelbygt500_10, not FOR_Ka_11" in line for line in out)


def test_the_builds_own_hud_lua_wins():
    hud = _hud(
        {
            404: ("FOR_Ka_11", FORD, 158),
            412: ("FOR_ShelbyGT500_10", FORD, SHELBY_GT500),
            500: ("KOE_Agera_11", 145, 150),
        }
    )
    stats, _ = _stats(hud)
    assert stats[404] == (FORD, 158, "dsfforza_for_ka_11")
    assert stats[412] == (FORD, SHELBY_GT500, "dsfforza_for_shelbygt500_10")
    assert stats[500] == (145, 150, "dsfforza_koe_agera_11")


if __name__ == "__main__":
    test_no_hud_lua_gives_no_other_cars_plate()
    test_another_builds_hud_lua_is_not_used()
    test_the_builds_own_hud_lua_wins()
    print("ok")
