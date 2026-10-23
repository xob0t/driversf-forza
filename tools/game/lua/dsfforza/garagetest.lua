-- dsfforza garage test (test bootstrap only; the player bootstrap never loads it).
--
-- 1. Always: wraps the Lua global getVehicleStats, which Driver.exe calls for
--    the HUD and the garage (research/gui-assets.md). Logs the first call for
--    each ported ID (with Selectable, ManufacturerName, Picture1) and counts
--    calls for model ID 0: no car uses 0, only the garage maker-list loop
--    (FUN_006c57d0, IDs 0..303) asks for it, so each ID-0 call is one build
--    of the garage list.
-- 2. Only when dsfforza\garagetest_on.lua exists (return { garage = 1, delay = 40, unlock = true }):
--    with `unlock`, first marks ported IDs below 304 unlocked and owned (writes
--    the save: use it only on a test save, e.g. the sandbox's copy);
--    once the player has been in a car for `delay` updates (about 0.5 s
--    each), opens that garage with garage.enterGarage(id) (PauseMenu.triggergarage,
--    as driving into the garage does), then logs the counters every 10 updates.
--
-- Output: Driver.log lines "[dsfforza.garage] ..." (not "[dsfforza] done",
-- so the harness keeps taking screenshots until -WaitSeconds).

local function log(message)
	print("[dsfforza.garage] " .. message)
end

_G.dsfforzaGarage = _G.dsfforzaGarage or { calls = {}, zero = 0, total = 0 }
local state = _G.dsfforzaGarage

local ids = {}
do
	local found, ported = user_open("dsfforza\\ported.lua")
	if found and type(ported) == "table" and type(ported.cars) == "table" then
		for id in pairs(ported.cars) do ids[id] = true end
	end
end
state.ids = ids

if not state.wrapped and type(getVehicleStats) == "function" then
	local original = getVehicleStats
	_G.getVehicleStats = function(id, ...)
		local entry = original(id, ...)
		state.total = state.total + 1
		if id == 0 then
			state.zero = state.zero + 1
			if state.zero == 1 then log("garage list built (getVehicleStats(0) called)") end
		end
		if state.ids[id] then
			local n = (state.calls[id] or 0) + 1
			state.calls[id] = n
			if n == 1 then
				local e = type(entry) == "table" and entry or {}
				log(string.format("getVehicleStats(%d) first call: Selectable %s, ManufacturerName %s, Model %s, Picture1 %s",
					id, tostring(e.Selectable), tostring(e.ManufacturerName), tostring(e.Model), tostring(e.Picture1)))
			end
		end
		return entry
	end
	state.wrapped = true
	log("wrapped getVehicleStats")
end

local options
do
	local exists = type(user_fileExists) == "function" and user_fileExists("dsfforza\\garagetest_on.lua")
	if exists then
		local found, cfg = user_open("dsfforza\\garagetest_on.lua")
		options = (found and type(cfg) == "table") and cfg or {}
	end
end

local function summary()
	local low, high = {}, {}
	for id, n in pairs(state.calls) do
		table.insert(id < 304 and low or high, string.format("%d:%d", id, n))
	end
	table.sort(low)
	table.sort(high)
	return string.format("garage list builds %d, getVehicleStats calls %d; ported IDs < 304 {%s}; >= 304 {%s}",
		state.zero, state.total, table.concat(low, " "), table.concat(high, " "))
end

if options then
	local garageId = tonumber(options.garage) or 1
	local delay = tonumber(options.delay) or 40
	local inCar, ticks, opened = 0, 0, false
	removeUserUpdateFunction("dsfforza_garagetest")
	addUserUpdateFunction("dsfforza_garagetest", function()
		ticks = ticks + 1
		local car = type(localPlayer) == "table" and localPlayer.currentVehicle
		if not opened then
			if type(car) == "table" and car.gameVehicle then inCar = inCar + 1 end
			if inCar >= delay then
				opened = true
				if options.unlock then
					-- Test saves only: mark the garage-range ported cars unlocked and owned,
					-- so the garage has a reason to list them (register.lua's writeProfile path).
					local n = 0
					for id in pairs(state.ids) do
						if id <= 303 and type(ProfileSettings) == "table" then
							pcall(ProfileSettings.SetVehicleUnlocked, id)
							pcall(ProfileSettings.SetVehicleOwned, id)
							n = n + 1
						end
					end
					log(string.format("unlocked and owned %d ported IDs below 304 (test save)", n))
				end
				local loc = type(garage) == "table" and type(garage.locations) == "table" and garage.locations[garageId]
				log(string.format("opening garage %d (%s)", garageId, loc and tostring(loc.name) or "?"))
				local ok, err = pcall(garage.enterGarage, garageId)
				log("garage.enterGarage: " .. tostring(ok) .. " " .. tostring(err))
				log(summary())
			end
		elseif ticks % 10 == 0 then
			log(summary())
		end
	end, 30)
	log(string.format("armed: garage %d after %d updates in a car", garageId, delay))
end
