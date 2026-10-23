-- DriverNGHook user bootstrapper for dsfforza tests.
-- Installed to <game>\Common\UserLuaScripts\bootstrap.lua by tools\game\dsf-test.ps1.

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
