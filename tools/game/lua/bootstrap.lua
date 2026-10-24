-- DriverNGHook user bootstrapper for dsfforza tests.
-- Copied into the run folder's Common\UserLuaScripts by tools\wsb\guest\run-test.ps1 -Probe.

local Bootstrapper = {}

-- As the player bootstrap: the Lua garbage collector stays stopped while a user file
-- is compiled and run (big generated files crashed the Lua VM otherwise;
-- research/in-game-tests.md, "Lua crashes loading big user files").
if not rawget(_G, "dsfforzaUserOpen") then
	local original = user_open
	rawset(_G, "dsfforzaUserOpen", original)
	rawset(_G, "user_open", function(name, ...)
		collectgarbage("stop")
		local ok, value = original(name, ...)
		collectgarbage("restart")
		return ok, value
	end)
end

function Bootstrapper.Init()
	print("[dsfforza] bootstrap init")
end

-- called when the game runs launchScripts()
function Bootstrapper.Launch()
	user_open("dsfforza\\docdump.lua")
	user_open("dsfforza\\register.lua")  -- HUD names, garage flag, traffic regions (research/game-lua.md)
	user_open("dsfforza\\garagetest.lua")  -- logs garage getVehicleStats calls; opens a garage with garagetest_on.lua
	user_open("dsfforza\\clonetest.lua")
end

function Bootstrapper.Shutdown()
	print("[dsfforza] bootstrap shutdown")
end

return Bootstrapper
