-- dsfforza player bootstrap: registers the added Forza cars with the game.
-- Installed to <game>\Common\UserLuaScripts\bootstrap.lua by tools\install.ps1,
-- together with dsfforza\register.lua, dsfforza\ported.lua and dsfforza\hud.lua.
-- No test probes run from here (the test bootstrap is tools\game\lua\bootstrap.lua).
-- Options: dsfforza\config.lua, e.g. return { register = { writeProfile = true } }

local Bootstrapper = {}

-- Keep the Lua garbage collector stopped while a user file is compiled and run.
-- The full roster's ported.lua and hud.lua (about 110 KB and 2,000 constants each)
-- crashed the game's Lua VM in about one load in five when a collection step ran
-- during the load (fault in the VM at 0x005F078A, research/in-game-tests.md,
-- "Lua crashes loading big user files"); with the collector held off, none did.
-- user_open is read-only in DriverNGHook's _G metatable, hence rawset.
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
end

-- called when the game runs launchScripts()
function Bootstrapper.Launch()
	local ok, err = user_open("dsfforza\\register.lua")  -- "\\": "\r" would be a carriage return
	if not ok then
		print("[dsfforza] register.lua failed: " .. tostring(err))
	end
end

function Bootstrapper.Shutdown()
end

return Bootstrapper
