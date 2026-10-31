"""The player bootstrap (tools/game/lua/player/bootstrap.lua) against stubbed DriverNGHook
globals (lupa, Lua 5.1): it runs another mod's bootstrap that tools/install.ps1 kept as
dsfforza/chained-bootstrap.lua before its own register.lua, and survives that file failing.

    uv run python tests/test_bootstrap.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import sys
from pathlib import Path

from lupa import lua51

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "tools" / "game" / "lua" / "player" / "bootstrap.lua"
TEST_BOOTSTRAP = REPO / "tools" / "game" / "lua" / "bootstrap.lua"  # run-test.ps1 -Probe
BS = chr(92)
CHAINED = f"dsfforza{BS}chained-bootstrap.lua"

# The shape of Felony Rework 2.3's Common/UserLuaScripts/bootstrap.lua (research/felony-rework.md).
FELONY = """
UserBootstrapper = {}
function UserBootstrapper.Init() table.insert(events, "felony Init") end
function UserBootstrapper.Launch()
  table.insert(events, "felony Launch")
  user_open("mo.lua")
  user_open("felonyrework.lua")
end
function UserBootstrapper.Shutdown() table.insert(events, "felony Shutdown") end
return UserBootstrapper
"""


def run(files: dict[str, str], bootstrap: Path = BOOTSTRAP) -> tuple[list[str], list[str]]:
    """Init, Launch and Shutdown of the player bootstrap with these user files; returns
    (events, printed lines). A file's source runs when user_open loads it."""
    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    printed: list[str] = []
    g.print = lambda *a: printed.append(" ".join(str(x) for x in a))
    lua.execute("events = {}")
    loader = lua.execute(
        "return function(src, name) local fn, err = loadstring(src, name) "
        "if not fn then return false, err end local ok, v = pcall(fn) "
        "if not ok then return false, v end return true, v end"
    )

    def user_open(name):
        name = str(name)
        lua.execute(f"table.insert(events, 'open {name.replace(BS, '/')}')")
        return loader(files[name], name) if name in files else (False, "missing")

    g.user_open = user_open
    g.py_exists = lambda name: str(name) in files
    lua.execute("user_fileExists = function(name) return py_exists(name) end")  # a Lua function, as DriverNGHook's
    ok, boot = loader(bootstrap.read_text(encoding="utf-8"), "bootstrap.lua")
    assert ok, boot
    boot.Init()
    boot.Launch()
    boot.Shutdown()
    return list(g.events.values()), printed


def test_a_chained_bootstrap_runs_first():
    events, printed = run(
        {CHAINED: FELONY, "felonyrework.lua": "", f"dsfforza{BS}register.lua": "table.insert(events, 'register')"}
    )
    assert events == [
        "open dsfforza/chained-bootstrap.lua",
        "felony Init",
        "felony Launch",
        "open mo.lua",  # not shipped with Felony Rework; its own bootstrap opens it anyway
        "open felonyrework.lua",
        "open dsfforza/register.lua",
        "register",
        "felony Shutdown",
    ], events
    assert not printed, printed


def test_without_a_chained_bootstrap_only_register_loads():
    events, _ = run({f"dsfforza{BS}register.lua": ""})
    assert events == ["open dsfforza/register.lua"], events


def test_a_failing_chained_bootstrap_does_not_stop_register():
    broken = "local B = {} function B.Launch() error('boom') end return B"
    events, printed = run({CHAINED: broken, f"dsfforza{BS}register.lua": "table.insert(events, 'register')"})
    assert events[-1] == "register", events
    assert any("chained-bootstrap.lua Launch failed" in line and "boom" in line for line in printed), printed
    events, printed = run({CHAINED: "return nil", f"dsfforza{BS}register.lua": ""})
    assert events == ["open dsfforza/chained-bootstrap.lua", "open dsfforza/register.lua"], events
    assert any("returned no bootstrapper" in line for line in printed), printed


def test_the_test_bootstrap_chains_too():
    events, _ = run({CHAINED: FELONY, "felonyrework.lua": ""}, TEST_BOOTSTRAP)
    assert events[:5] == [
        "open dsfforza/chained-bootstrap.lua",
        "felony Init",
        "felony Launch",
        "open mo.lua",
        "open felonyrework.lua",
    ], events
    assert "open dsfforza/register.lua" in events and events[-1] == "felony Shutdown", events


if __name__ == "__main__":
    for test in (
        test_a_chained_bootstrap_runs_first,
        test_without_a_chained_bootstrap_only_register_loads,
        test_a_failing_chained_bootstrap_does_not_stop_register,
        test_the_test_bootstrap_chains_too,
    ):
        test()
    print("ok")
    sys.exit(0)
