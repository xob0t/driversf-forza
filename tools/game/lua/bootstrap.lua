-- DriverNGHook user bootstrapper for dsfcars tests.
-- Copied into the run folder's Common\UserLuaScripts by tools\wsb\guest\run-test.ps1 -Probe.

local Bootstrapper = {}

-- As the player bootstrap: the Lua garbage collector stays stopped while a user file
-- is compiled and run (big generated files crashed the Lua VM otherwise;
-- research/in-game-tests.md, "Lua crashes loading big user files").
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

-- As the player bootstrap: another mod's bootstrap kept as dsfcars\chained-bootstrap.lua
-- (a run with that mod's files laid over the game) runs first, errors logged.
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
	print("[dsfcars] bootstrap init")
	chained = nil
	if type(user_fileExists) == "function" and user_fileExists(CHAINED) then
		local ok, other = user_open(CHAINED)
		chained = ok and type(other) == "table" and other or nil
		print("[dsfcars] chained bootstrap: " .. (chained and "loaded" or ("none returned: " .. tostring(other))))
	end
	callChained("Init")
end

-- As the player bootstrap: the chained Launch without its own user_open of register.lua.
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
	user_open("dsfcars\\docdump.lua")
	user_open("dsfcars\\register.lua")  -- HUD names, garage flag, traffic regions (research/game-lua.md)
	user_open("dsfcars\\garagetest.lua")  -- logs garage getVehicleStats calls; opens a garage with garagetest_on.lua
	user_open("dsfcars\\clonetest.lua")
end

function Bootstrapper.Shutdown()
	callChained("Shutdown")
	print("[dsfcars] bootstrap shutdown")
end

return Bootstrapper
