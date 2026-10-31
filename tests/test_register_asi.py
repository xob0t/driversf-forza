"""register.lua with and without dsfforza.asi's garage module, against stubbed
game APIs (lupa, Lua 5.1). Needs out/lua-src (src/dsfforza/game/game_data.py prepare, which
tools/worktree-setup.ps1 and port_batch run).

    uv run python tests/test_register_asi.py     # or every test: uv run python -m tests

Fakes 600 added cars (IDs 304..903, 70 makes) and checks: the magic-ID
detection, how many cars become Selectable, the group caps, and which
ProfileSettings setter calls guardProfile lets through. A plugin with the car
list window answers the cars-per-group query (k = 0x3000 + n), so a make keeps
all its cars in one group; one without it (cars=None) keeps groups at 20.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from lupa import lua51

REPO = Path(__file__).resolve().parents[1]
LUA_DIR = REPO / "tools" / "game" / "lua"
STATS = REPO / "out" / "lua-src" / "LuaScripts" / "vehiclestats" / "vehiclestats.lua"
BS = chr(92)
MAGIC = 8000000
LOADER = (
    "return function(src, name) local fn, err = loadstring(src, name) "
    "if not fn then return false, err end return pcall(fn) end"
)


def ported(n: int = 600, makes: int = 70) -> str:
    rows = []
    for i in range(n):
        rows.append(
            f'[{304 + i}] = {{ host = 266, media = "T{i}", name = "Make{i % makes} M{i}", '
            f'make = "Make{i % makes}", model = "M{i}", year = "2010" }},'
        )
    return "return { cars = {\n" + "\n".join(rows) + "\n} }"


def run(
    asi: bool,
    groups: int = 120,
    max_id: int = 999,
    cars: str | None = None,
    hud: str | None = None,
    per_group: int | None = 1000,
    stats_extra: str = "",
):
    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    out: list[str] = []
    g.print = lambda *a: out.append(" ".join(str(x) for x in a))
    loader = lua.execute(LOADER)
    over = {f"dsfforza{BS}ported.lua": cars or ported(), f"dsfforza{BS}hud.lua": hud, f"dsfforza{BS}config.lua": None}

    def user_open(name):
        name = str(name)
        if name == "raise.lua":  # a user script that raises
            raise RuntimeError("script error in raise.lua")
        if name in over:
            return (False, "missing") if over[name] is None else loader(over[name], name)
        path = LUA_DIR / name.replace(BS, "/")
        return loader(path.read_text(encoding="utf-8"), name) if path.exists() else (False, "missing")

    def unlocked(i):
        i = int(i)
        k = i - MAGIC
        if not asi or not 0 <= k < 0x4000:
            return False
        if k in (0, 1):
            return True
        if 0x1000 <= k < 0x2000:
            return k - 0x1000 <= groups
        if 0x2000 <= k < 0x3000:
            return k - 0x2000 <= max_id
        if 0x3000 <= k < 0x4000:
            return per_group is not None and k - 0x3000 <= per_group
        return False

    g.user_open = user_open
    g.py_unlocked = unlocked
    if not STATS.exists():
        raise FileNotFoundError(f"{STATS}: run uv run dsff game_data prepare")
    lua.execute(STATS.read_text(encoding="utf-8"))
    lua.execute(stats_extra)
    lua.execute("""
    calls = {}
    ProfileSettings = {
      SetVehicleUnlocked = function(id) table.insert(calls, id) end,
      SetVehicleOwned = function(id) end,
      GetVehicleUnlocked = function(id) return py_unlocked(id) end,
      GetVehicleOwned = function(id) return false end }
    InterestingVehicleManager = { SetInterestingVehicleRegions = function(r) end,
      IsEnabled = function() return true end, GetDesiredSlotCount = function() return 3 end }
    spooling = { interestingVehicles = {}, missionSpecificVehiclePots = { ["All chapters"] = {62} } }
    addUserUpdateFunction = function() end
    removeUserUpdateFunction = function() end
    TrafficSpooler = { Busy = function() return false end }
    """)
    ok, mod = user_open(f"dsfforza{BS}register.lua")
    if not ok:
        raise SystemExit(f"register.lua failed: {mod}")
    return lua, out


def check(asi: bool, per_group: int | None = 1000) -> list[str]:
    lua, out = run(asi, per_group=per_group)
    stats = lua.globals().vehicleStats
    groups = Counter()
    selectable_added = 0
    for sid in range(1000):
        e = stats[sid]
        if e is not None and e.Selectable:
            groups[str(e.ManufacturerName).upper()[:15]] += 1
            selectable_added += sid >= 304
    lua.execute("ProfileSettings.SetVehicleUnlocked(500); ProfileSettings.SetVehicleUnlocked(200)")
    calls = list(lua.globals().calls.values())
    errors = []
    limit = 120 if asi else 40
    if len(groups) > limit:
        errors.append(f"{len(groups)} groups > {limit}")
    cap = per_group if asi and per_group else 20
    if max(groups.values()) > cap:
        errors.append(f"a group has {max(groups.values())} cars (cap {cap})")
    if "FORZA" in groups and asi and per_group and per_group > 20:
        errors.append("a FORZA group although the plugin's groups have no 20-car cap")
    if asi and selectable_added < 500:
        errors.append(f"only {selectable_added} added cars selectable")
    if not asi and selectable_added:
        errors.append(f"{selectable_added} cars at 304+ selectable without the plugin")
    if (500 in calls) != asi:
        errors.append(f"SetVehicleUnlocked(500) passed={500 in calls} with asi={asi}")
    print(
        f"asi={asi} per_group={per_group}: {selectable_added} of 600 added cars selectable, {len(groups)} groups "
        f"(largest {max(groups.values())}), setter calls {calls}"
    )
    print("  " + "\n  ".join(line for line in out if "dsfforza.asi" in line))
    return errors


def test_register_asi():
    problems = check(False) + check(True) + check(True, per_group=None)
    assert not problems, problems


def big_group(per_group: int | None) -> Counter:
    """55 added cars of one make: one group with a plugin whose groups have no 20-car cap,
    else 20 in the make's group and the rest in the shared FORZA groups."""
    n = 55
    cars = "return { cars = {\n" + "\n".join(
        f'[{304 + i}] = {{ host = 266, media = "B{i}", name = "Big B{i}", make = "Big", model = "B{i}", year = "2010" }},'
        for i in range(n)
    )
    cars += "\n} }"
    hud = "return {\n" + "\n".join(f'[{304 + i}] = {{ ManufacturerName = "ID:900001" }},' for i in range(n)) + "\n}"
    lua, _ = run(True, cars=cars, hud=hud, per_group=per_group)
    stats = lua.globals().vehicleStats
    return Counter(str(stats[304 + i].ManufacturerName) for i in range(n) if stats[304 + i].Selectable)


def test_big_group():
    assert big_group(1000) == Counter({"ID:900001": 55})
    old = big_group(None)
    assert old == Counter({"ID:900001": 20, "FORZA": 20, "FORZA 2": 15}), old


# Felony Rework's vehicleStats dummies (its whole-file vehiclestats.lua, research/felony-rework.md):
# Selectable placeholders above 303 with no model behind them.
FELONY_DUMMIES = """
for _, id in ipairs({622, 623}) do
  vehicleStats[id] = { ManufacturerName = "", ModelName = "NOTHING", Picture1 = "default_ability",
    Price = 1, Tier = 1, UnlockByActivity = "ClassicSurvival", Selectable = true }
end
"""


def test_other_mods_placeholders_past_303_stay_out_of_the_plugins_garage():
    """The plugin's garage walks IDs up to 999; only our cars are models there, so another
    mod's Selectable entry above 303 is hidden (the vanilla garage never reaches it) and takes
    no maker group. Without the plugin it is left alone."""
    cars = ported(100)  # IDs 304..403: port_batch never gives out 622 and 623 (RESERVED_IDS)
    lua, out = run(True, cars=cars, stats_extra=FELONY_DUMMIES)
    stats = lua.globals().vehicleStats
    assert not stats[622].Selectable and not stats[623].Selectable
    assert stats[622].UnlockByActivity == "ClassicSurvival"  # its unlock reward is untouched
    assert sum(1 for line in out if "is not a model" in line) == 2
    assert stats[304].Selectable  # our cars past 303 still are
    lua, _ = run(False, cars=cars, stats_extra=FELONY_DUMMIES)
    assert lua.globals().vehicleStats[622].Selectable


def test_registers_gc_wrapper_restarts_the_collector_when_a_file_raises():
    """register.lua wraps user_open itself (without the player bootstrap): the collector is
    restarted when a file raises, and the caller still gets the error."""
    lua, _ = run(True, cars=ported(10))
    lua.execute("""
    gc_calls = {}
    local gc = collectgarbage
    collectgarbage = function(what, ...) table.insert(gc_calls, what) return gc(what, ...) end
    raised_ok, raised = pcall(user_open, "raise.lua")
    """)
    g = lua.globals()
    assert g.raised_ok is False and "script error in raise.lua" in str(g.raised), g.raised
    assert list(g.gc_calls.values()) == ["stop", "restart"], list(g.gc_calls.values())


if __name__ == "__main__":
    print("one make, 55 cars:", dict(big_group(1000)), "without the window:", dict(big_group(None)))
    failed = []
    for name, test in sorted(globals().items()):  # every test_* function, as `python -m tests` runs them
        if name.startswith("test_") and callable(test):
            try:
                test()
            except AssertionError as error:
                failed.append(name)
                print("FAIL", name, error)
    sys.exit(1 if failed else 0)
