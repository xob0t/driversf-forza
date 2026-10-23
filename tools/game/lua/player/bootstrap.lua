-- dsfforza player bootstrap: registers the added Forza cars with the game.
-- Installed to <game>\Common\UserLuaScripts\bootstrap.lua by tools\install.ps1,
-- together with dsfforza\register.lua, dsfforza\ported.lua and dsfforza\hud.lua.
-- No test probes run from here (the test bootstrap is tools\game\lua\bootstrap.lua).
-- Options: dsfforza\config.lua, e.g. return { register = { writeProfile = true } }

local Bootstrapper = {}

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
