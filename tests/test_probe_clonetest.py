"""clonetest.lua against stubbed game APIs (lupa, Lua 5.1): the probe hides the game's UI from
the first spawn on (config.hide_ui) and logs what each shot shows ("shot <n>: ..." lines for the
harness's captions). The game's Lua is 5.1 too: a function with more than 60 upvalues doesn't
load. Offline, under a second.

    uv run python -m tests probe_clonetest
"""

from __future__ import annotations

from pathlib import Path

from lupa import lua51

REPO = Path(__file__).resolve().parents[1]
LUA_DIR = REPO / "tools" / "game" / "lua"
BS = chr(92)
LOADER = (
    "return function(src, name) local fn, err = loadstring(src, name) "
    "if not fn then return false, err end return pcall(fn) end"
)
STUBS = """
updates = {}
addUserUpdateFunction = function(name, fn, ticks) updates[name] = fn end
removeUserUpdateFunction = function(name) updates[name] = nil end
vehicleStats = { [266] = { Manufacturer = 1, Model = 2 } }
Menu = { HideUI = 0, ShowHUD = 1 }
zapLevelNow = 7
zapcontroller = { getZapLevel = function(self) return zapLevelNow end }
TrafficSpooler = { RequestMissionVehicle = function(id) end, IsMissionVehicleLoaded = function(id) return true end,
  Busy = function() return false end }
VehicleLodSpooler = { RequestVehicle = function(id) end }
spoolsystem = {}
vehicleManager = { findVehiclesInTraffic = function() return {} end }
GameVehicleResource = {
  create = function(p) return { model_id = p.Model_Id, position = p.Position, heading = 0 } end,
  ClearAreaOfVehicles = function() end }
localPlayer = { inZap = true, currentVehicle = false, position = { x = 0, y = 0, z = 0 }, cameraMode = "Normal" }
function localPlayer:SetZapLevel() end
function localPlayer:resetCameraMode() end
function localPlayer:ZapIntoVehicle(car) self.currentVehicle = { gameVehicle = car }; self.inZap = false end
"""


def _probe(config: str):
    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    out: list[str] = []
    g.print = lambda *a: out.append(" ".join(str(x) for x in a))
    loader = lua.execute(LOADER)

    def user_open(name):
        name = str(name)
        if name == f"dsfforza{BS}config.lua":
            return loader(config, name)
        path = LUA_DIR / name.replace(BS, "/")
        return loader(path.read_text(encoding="utf-8"), name) if path.exists() else (False, "missing")

    g.user_open = user_open
    lua.execute(STUBS)
    loaded = user_open(f"dsfforza{BS}clonetest.lua")  # pcall's true, or false and the error
    assert loaded is True, loaded
    return lua, out


def _ticks(lua, n: int) -> None:
    for _ in range(n):
        step = lua.globals().updates["dsfforza_clonetest"]
        if step is None:
            return
        step()


def test_ui_hidden_from_the_first_spawn_and_shot_lines():
    lua, out = _probe("return { ids = { 266 }, hide_ui = true }")
    g = lua.globals()
    _ticks(lua, 30)
    # Still in the Shift overview (zap level 7): no car, and the Shift view keeps its UI.
    assert not any("spawn returned" in line for line in out), out
    assert g.Menu.HideUI == 0 and g.Menu.ShowHUD == 1
    g.zapLevelNow = 1
    _ticks(lua, 4)
    assert any("id 266: PLAYER IS DRIVING IT" in line for line in out), out
    assert g.Menu.HideUI == 1 and g.Menu.ShowHUD == 0
    assert any(line.startswith("[dsfforza] ui hidden (was HideUI=0 ShowHUD=1)") for line in out), out
    # The game shows its HUD again (leaving the Shift view): the per-frame hold puts it back.
    g.Menu.ShowHUD = 1
    g.updates["dsfforza_noui"]()
    assert g.Menu.ShowHUD == 0
    _ticks(lua, 200)
    shots = [line.split(": ", 1)[1] for line in out if line.startswith("[dsfforza] shot ")]
    chase, cockpit = "id=266 probe=266 view=chase phase=chase_view", "id=266 probe=266 view=cockpit phase=cockpit_view"
    assert chase in shots and cockpit in shots and shots.index(chase) < shots.index(cockpit), shots
    assert any("chase view restored (ui flags set back 1 times so far)" in line for line in out), out
    assert any(line == "[dsfforza] done" for line in out), out


def test_ui_kept_without_hide_ui():
    lua, out = _probe("return { ids = { 266 } }")
    g = lua.globals()
    g.zapLevelNow = 1
    _ticks(lua, 8)
    assert any("id 266: PLAYER IS DRIVING IT" in line for line in out), out
    assert g.Menu.HideUI == 0 and g.Menu.ShowHUD == 1
    assert g.updates["dsfforza_noui"] is None
