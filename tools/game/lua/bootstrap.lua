-- DriverNGHook user bootstrapper for dsfforza tests.
-- Copied into the run folder's Common\UserLuaScripts by tools\wsb\guest\run-test.ps1 -Probe.

local Bootstrapper = {}

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
