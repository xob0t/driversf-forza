"""register.lua with and without dsfforza.asi's garage module, against stubbed
game APIs (lupa, Lua 5.1). Needs out/lua-src (src/dsfforza/game/game_data.py prepare, which
tools/worktree-setup.ps1 and port_batch run).

    uv run python tests/test_register_asi.py     # or every test: uv run python -m tests

Fakes 600 added cars (IDs 304..903, 70 makes) and checks: the magic-ID
detection, how many cars become Selectable, the group caps, and which
ProfileSettings setter calls guardProfile lets through.
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
LOADER = ("return function(src, name) local fn, err = loadstring(src, name) "
          "if not fn then return false, err end return pcall(fn) end")


def ported(n: int = 600, makes: int = 70) -> str:
    rows = []
    for i in range(n):
        rows.append(f'[{304 + i}] = {{ host = 266, media = "T{i}", name = "Make{i % makes} M{i}", '
                    f'make = "Make{i % makes}", model = "M{i}", year = "2010" }},')
    return "return { cars = {\n" + "\n".join(rows) + "\n} }"


def run(asi: bool, groups: int = 120, max_id: int = 999):
    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    out: list[str] = []
    g.print = lambda *a: out.append(" ".join(str(x) for x in a))
    loader = lua.execute(LOADER)
    over = {f"dsfforza{BS}ported.lua": ported(), f"dsfforza{BS}hud.lua": None, f"dsfforza{BS}config.lua": None}

    def user_open(name):
        name = str(name)
        if name in over:
            return (False, "missing") if over[name] is None else loader(over[name], name)
        path = LUA_DIR / name.replace(BS, "/")
        return loader(path.read_text(encoding="utf-8"), name) if path.exists() else (False, "missing")

    def unlocked(i):
        i = int(i)
        k = i - MAGIC
        if not asi or not 0 <= k < 0x3000:
            return False
        if k in (0, 1):
            return True
        if 0x1000 <= k < 0x2000:
            return k - 0x1000 <= groups
        if 0x2000 <= k < 0x3000:
            return k - 0x2000 <= max_id
        return False

    g.user_open = user_open
    g.py_unlocked = unlocked
    if not STATS.exists():
        raise FileNotFoundError(f"{STATS}: run uv run dsff game_data prepare")
    lua.execute(STATS.read_text(encoding="utf-8"))
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


def check(asi: bool) -> list[str]:
    lua, out = run(asi)
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
    if max(groups.values()) > 20:
        errors.append(f"a group has {max(groups.values())} cars")
    if asi and selectable_added < 500:
        errors.append(f"only {selectable_added} added cars selectable")
    if not asi and selectable_added:
        errors.append(f"{selectable_added} cars at 304+ selectable without the plugin")
    if (500 in calls) != asi:
        errors.append(f"SetVehicleUnlocked(500) passed={500 in calls} with asi={asi}")
    print(f"asi={asi}: {selectable_added} of 600 added cars selectable, {len(groups)} groups "
          f"(largest {max(groups.values())}), setter calls {calls}")
    print("  " + "\n  ".join(line for line in out if "dsfforza.asi" in line))
    return errors


def test_register_asi():
    problems = check(False) + check(True)
    assert not problems, problems


if __name__ == "__main__":
    problems = check(False) + check(True)
    for p in problems:
        print("FAIL", p)
    sys.exit(1 if problems else 0)
