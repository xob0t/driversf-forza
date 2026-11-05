-- dsfcars player bootstrap: registers the added cars with the game.
-- Installed to <game>\Common\UserLuaScripts\bootstrap.lua by tools\install.ps1,
-- together with dsfcars\register.lua; the car manager writes the cars' dsfcars\packages.lua
-- and dsfcars\packages_hud.lua.
-- No test probes run from here (the test bootstrap is tools\game\lua\bootstrap.lua).
-- Options: dsfcars\config.lua, e.g. return { register = { writeProfile = true } }

local Bootstrapper = {}

-- Keep the Lua garbage collector stopped while a user file is compiled and run.
-- The full roster's car lists (packages.lua and packages_hud.lua; a build's ported.lua and
-- hud.lua in the in-game tests), about 110 KB and 2,000 constants each,
-- crashed the game's Lua VM in about one load in five when a collection step ran
-- during the load (fault in the VM at 0x005F078A, research/in-game-tests.md,
-- "Lua crashes loading big user files"); with the collector held off, none did.
-- user_open is read-only in DriverNGHook's _G metatable, hence rawset.
if not rawget(_G, "dsfcarsUserOpen") then
	local original = user_open
	rawset(_G, "dsfcarsUserOpen", original)
	rawset(_G, "user_open", function(name, ...)
		collectgarbage("stop")
		local called, ok, value = pcall(original, name, ...)
		collectgarbage("restart")   -- also when the file raised: a stopped collector fills the Lua heap
		if not called then
			error(ok, 0)   -- the caller sees the same error as without the wrapper
		end
		return ok, value
	end)
end

-- Another mod's bootstrap.lua that tools\install.ps1 replaced (Felony Rework's, for one) is
-- kept as dsfcars\chained-bootstrap.lua. Its Init, Launch and Shutdown run before ours, so
-- that mod still loads (research/felony-rework.md). An error in it is logged and doesn't stop
-- ours, nor the game's launchScripts() that DriverNGHook calls after Launch.
local CHAINED = "dsfcars\\chained-bootstrap.lua"
local chained = nil

local function callChained(name)
	local fn = chained and chained[name]
	if type(fn) == "function" then
		local ok, err = pcall(fn)
		if not ok then
			print("[dsfcars] " .. CHAINED .. " " .. name .. " failed: " .. tostring(err))
		end
	end
end

function Bootstrapper.Init()
	chained = nil
	if type(user_fileExists) == "function" and user_fileExists(CHAINED) then
		local ok, other = user_open(CHAINED)
		if ok and type(other) == "table" then
			chained = other
		else
			print("[dsfcars] " .. CHAINED .. " returned no bootstrapper: " .. tostring(other))
		end
	end
	callChained("Init")
end

-- The chained bootstrap's Launch, without its own user_open of register.lua: a player who
-- added that line to the other mod's bootstrap (INSTALL.md) would otherwise register twice.
-- The name before the rename to dsfcars too: a line added to another mod's bootstrap for an
-- older install opens dsfforza\register.lua, which is gone (install.ps1 moved the folder).
local REGISTER = { ["dsfcars\\register.lua"] = true, ["dsfforza\\register.lua"] = true }
local function launchChained()
	local open = rawget(_G, "user_open")
	rawset(_G, "user_open", function(name, ...)
		if type(name) == "string" and REGISTER[string.lower((string.gsub(name, "/", "\\")))] then
			print("[dsfcars] " .. CHAINED .. " opens register.lua; skipped, this bootstrap loads it next")
			return true, nil
		end
		return open(name, ...)
	end)
	callChained("Launch")
	rawset(_G, "user_open", open)
end

-- called when the game runs launchScripts()
function Bootstrapper.Launch()
	launchChained()
	local ok, err = user_open("dsfcars\\register.lua")  -- "\\": "\r" would be a carriage return
	if not ok then
		print("[dsfcars] register.lua failed: " .. tostring(err))
	end
end

function Bootstrapper.Shutdown()
	callChained("Shutdown")
end

return Bootstrapper
