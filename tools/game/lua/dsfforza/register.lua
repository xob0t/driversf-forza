-- dsfforza: register added vehicles with the game's Lua systems.
--
-- Loaded from bootstrap.lua's Bootstrapper.Launch() (the test bootstrap in
-- tools/game/lua and the player one tools/install.ps1 installs).
-- Launch runs before the game's own launchScripts(), after the initial load
-- has created vehicleStats, so every hook below is in place before the first
-- chapter loads. Background: research/game-lua.md.
--
-- What it does per car in CARS:
--   HUD     vehicleStats[id] = copy of a stock entry plus overrides
--           (badge, name strings, stat bars). The HUD, garage and unlock
--           panels read this table through getVehicleStats(id).
--   traffic adds the ID to the chapter's "interesting vehicle" regions
--           (spooling.interestingVehicles), which the engine spools and
--           spawns into traffic.
--   garage  Selectable/Tier/Price in vehicleStats, for IDs below 304 (below
--           1000 with dsfforza.asi) while the garage's maker list has room
--           (garageGroup); with writeProfile it also marks the car unlocked
--           and owned in the save.
--   save    guardProfile keeps the game's own Lua (cheat "unlock all", unlock
--           panels) from writing save flags for IDs 304 and up, which the
--           save can't hold (research/batch.md); with dsfforza.asi it lets
--           them through (the plugin keeps them in dsfforza-garage.dat).
--
-- Player defaults: no probes, no save writes.
-- The test harness (a config.lua with `ids`) turns the probe on.
--
-- Output goes to Driver.log as "[dsfforza.register] ...".

local M = {}

-- Per-car settings, from dsfforza\ported.lua (tools/port_car.py or
-- tools/port_batch.py) or config.lua's register.cars. Empty without either, so
-- nothing is registered for cars that aren't in the installed archive.
-- `source` is the stock model whose vehicleStats entry is copied; every other
-- field overrides that copy. Example:
--	[304] = {
--		source = 266,                 -- ASYM Desanne (the clone's host)
--		-- Name strings: "ID:nnnnnn" is a localisation string ID; a plain string
--		-- is passed through as-is. nil keeps the source's.
--		manufacturerName = "Nissan", modelName = "370Z (Forza)",
--		manufacturer = nil,           -- badge glyph (vehicleStats.Manufacturer); nil keeps source's
--		year = "2009",
--		speed = 0.62, strength = 0.45, drift = 0.7,   -- HUD/garage bars, 0..1
--		tier = 2, price = 5000,       -- garage shop tier and price
--		traffic = true,               -- add to interesting-vehicle regions
--		regions = nil,                -- nil = automatic (regionCars), or a list like {1, 2, 4}
--	},
local CARS = {}

local OPTIONS = {
	interesting = true,   -- (1) traffic via InterestingVehicleManager regions
	missionPot = false,   -- keep the car resident: add it to spooling.missionSpecificVehiclePots["All chapters"]
	                      -- (what Felony Rework does for cop car 267; uses one of 18 mission spool slots)
	regionCars = 24,      -- (1) at most about this many added cars per region; more cars are spread
	                      --     over the 17 regions so stock interesting vehicles keep their share
	garage = true,        -- (2) Selectable = true in vehicleStats (IDs below 304, below 1000 with dsfforza.asi,
	                      --     while the maker list has room)
	writeProfile = false, -- (2) ProfileSettings.SetVehicleUnlocked/Owned for garage cars: WRITES THE SAVE
	                      --     (unlocked + owned flags, a "new vehicle" notice); back the save up first
	guardProfile = true,  -- (2) ignore Lua SetVehicleUnlocked/Owned for added IDs 304 and up (save can't hold them;
	                      --     with dsfforza.asi's save hooks only IDs above 999)
	probe = false,        -- log what the game sees (vehiclesAvailable, profile flags, traffic counts);
	                      -- on by default in test sessions (config.lua with `ids`)
	probeTicks = 360,     -- probe updates (every 30 frames), about 3 minutes
}

-- Garage maker list (confirmed-static, research/batch.md): at most 40 maker
-- groups and 20 cars per group, no capacity check; only IDs 0..303 are listed.
-- The save holds flags for IDs up to 303 only.
--
-- dsfforza.asi's garage module (tools/asi, research/asi.md) lifts both: the
-- garage walks IDs up to 999, holds more maker groups (and drops what doesn't
-- fit instead of overrunning memory), and flags of IDs 304..999 go to
-- <savegames>\dsfforza-garage.dat instead of the stock save block. Lua can't
-- call native code, so the plugin answers ProfileSettings.GetVehicleUnlocked
-- for magic IDs 8000000 + k (the game's Lua numbers are single floats, exact
-- below 2^24; literals must stay below 2^23); an unpatched game looks the number up in
-- the save's flag list and says false.
local ASI_MAGIC = 8000000
local function asiQuery(k)
	if type(ProfileSettings) ~= "table" or type(ProfileSettings.GetVehicleUnlocked) ~= "function" then
		return false
	end
	local ok, value = pcall(ProfileSettings.GetVehicleUnlocked, ASI_MAGIC + k)
	return ok and value == true
end
-- Largest n in [lo, hi] with asiQuery(base + n) true (the answers are monotonic), or nil.
local function asiLimit(base, lo, hi)
	if not asiQuery(base + lo) then return nil end
	while lo < hi do
		local mid = math.floor((lo + hi + 1) / 2)
		if asiQuery(base + mid) then lo = mid else hi = mid - 1 end
	end
	return lo
end
local ASI = { save = asiQuery(0), garage = asiQuery(1) }
if ASI.garage then
	ASI.groups = asiLimit(0x1000, 40, 4000)   -- k = 0x1000 + n: n <= max groups
	ASI.maxId = asiLimit(0x2000, 303, 4000)   -- k = 0x2000 + id: id is listed
end

local GARAGE_MAX_GROUPS = ASI.garage and ASI.groups or 40
local GARAGE_MAX_PER_GROUP = 20
local GARAGE_MAX_ID = ASI.garage and ASI.maxId or 303
local SAVE_MAX_ID = ASI.save and 999 or 303   -- highest ID whose save flags are safe to set
local OVERFLOW_GROUP = "FORZA"   -- makes without a group of their own share FORZA, FORZA 2, ...

-- Cars built by tools/port_car.py (dsfforza\ported.lua) replace the sample
-- list above. The HUD badge and name come from the numeric Manufacturer and
-- Model fields (ManufacturerName/ModelName strings don't reach it), so each
-- car borrows them from a stock entry, matched on Picture1 ("nissan_370z"):
--   same make and model  -> copy that stock entry (badge, name, pictures, stats)
--   same make            -> host entry with that make's badge and the model
--                           index of its closest-year stock car
--   make not in DSF      -> host entry unchanged
local function token(text)
	return string.lower(tostring(text or "")):gsub("[^%w]", "")
end

local function stockMatch(car)
	if type(vehicleStats) ~= "table" then
		return nil
	end
	local make = token(string.match(car.make or "", "^(%S+)"))
	local model = token(car.model)
	local year = tonumber(car.year)
	local exact, near, nearGap
	for id, entry in pairs(vehicleStats) do
		local picture = type(entry) == "table" and entry.Picture1
		if type(picture) == "string" and id < 1000 and not (car.ids and car.ids[id]) then
			local prefix, rest = string.match(string.lower(picture), "^([^_]+)_(.*)$")
			if prefix == make and make ~= "" then
				if token(rest) == model and model ~= "" then
					exact = exact or id
				end
				local gap = math.abs((tonumber(entry.Year) or 0) - (year or 0))
				if near == nil or gap < nearGap then
					near, nearGap = id, gap
				end
			end
		end
	end
	return exact, near
end

-- Per-ID vehicleStats overrides from tools/gui_assets.py build: Picture1,
-- Manufacturer, Model (vehicles_font codes), ManufacturerName, ModelName.
local HUD
do
	local found, hud = user_open("dsfforza\\hud.lua")
	if found and type(hud) == "table" then
		HUD = hud
	end
end

local PORTED
do
	local found, ported = user_open("dsfforza\\ported.lua")
	if found and type(ported) == "table" and type(ported.cars) == "table" then
		PORTED = ported
		CARS = {}
		for id, car in pairs(ported.cars) do
			CARS[id] = {
				source = car.host,
				make = car.make, model = car.model, yearText = car.year,
				year = car.year ~= "" and car.year or nil,
				speed = car.speed, strength = car.strength, drift = car.drift,  -- handling_dsf.stat_bars
				traffic = true,
			}
		end
	end
end


do
	local found, config = user_open("dsfforza\\config.lua")
	if found and type(config) == "table" and type(config.ids) == "table" then
		OPTIONS.probe = true   -- test session (run-test.ps1 writes ids); register.probe still wins
	end
	if found and type(config) == "table" and type(config.register) == "table" then
		for key, value in pairs(config.register) do
			OPTIONS[key] = value
		end
		if type(config.register.cars) == "table" then
			CARS = config.register.cars
		end
	end
end

local function sortedIds()
	local ids = {}
	for id in pairs(CARS) do table.insert(ids, id) end
	table.sort(ids)
	return ids
end

local function log(message)
	print("[dsfforza.register] " .. message)
end

local function try(fn, ...)
	if type(fn) ~= "function" then
		return "n/a"
	end
	local ok, a, b = pcall(fn, ...)
	if not ok then
		return "err(" .. tostring(a) .. ")"
	end
	if b ~= nil then
		return tostring(a) .. "," .. tostring(b)
	end
	return tostring(a)
end

-- Survives DriverNGHook re-running bootstrap.lua when the file changes, so
-- functions are wrapped only once.
_G.dsfforzaRegister = _G.dsfforzaRegister or { wrapped = {} }
local state = _G.dsfforzaRegister

---------------------------------------------------------------------------
-- (3) HUD name, badge and stat bars: vehicleStats[id]
---------------------------------------------------------------------------

-- Fields that make the game's own unlock code (vehicleManager.updateVehicleUnlocks)
-- call ProfileSettings.SetVehicleUnlocked for this ID on chapter load.
local UNLOCK_FIELDS = { "UnlockedInChapter", "UnlockedInGarage", "UnlockByActivity" }

-- Resolve stock matches once vehicleStats exists (called from M.registerStats).
local function resolvePorted()
	if not PORTED then
		return
	end
	local own = {}
	for id in pairs(CARS) do own[id] = true end
	for id, car in pairs(CARS) do
		if car.make and not car.resolved then
			car.resolved = true
			local exact, near = stockMatch({ make = car.make, model = car.model, year = car.yearText, ids = own })
			if exact then
				car.source = exact
				car.year = nil
			elseif near then
				car.manufacturer = vehicleStats[near].Manufacturer
				car.modelIndex = vehicleStats[near].Model
			end
			log(string.format("id %d (%s %s): stock match %s, same make %s", id, tostring(car.make),
				tostring(car.model), tostring(exact), tostring(near)))
		end
	end
end

local function buildStats(id, car)
	local source = vehicleStats[car.source]
	if type(source) ~= "table" then
		log(string.format("id %d: no vehicleStats[%s] to copy", id, tostring(car.source)))
		return nil
	end
	local entry = {}
	for key, value in pairs(source) do
		entry[key] = value
	end
	for _, key in ipairs(UNLOCK_FIELDS) do
		entry[key] = nil
	end
	if car.manufacturerName then entry.ManufacturerName = car.manufacturerName end
	if car.modelName then entry.ModelName = car.modelName end
	if car.manufacturer then entry.Manufacturer = car.manufacturer end
	if car.modelIndex then entry.Model = car.modelIndex end
	if car.year then entry.Year = car.year end
	if car.speed then entry.Speed = car.speed end
	if car.strength then entry.Strength = car.strength end
	if car.drift then entry.Drift = car.drift end
	if car.tier then entry.Tier = car.tier end
	if car.price then entry.Price = car.price end
	-- Garage picture, HUD badge and model glyph made by tools/gui_assets.py
	-- (dsfforza\hud.lua; needs its gui.sp and Fonts.fchunk copies installed).
	local hud = HUD and HUD[id]
	if type(hud) == "table" then
		for key, value in pairs(hud) do entry[key] = value end
	end
	entry.Selectable = false   -- set by registerStats (garageGroup)
	entry.UPlay = false
	entry.UniqueKey = false
	return entry
end

-- The garage groups selectable cars of IDs 0..303 by the first 15 characters
-- of ManufacturerName, upper-cased, into a fixed table of 40 groups of 20
-- (FUN_006c57d0 and its inserts, no capacity check: more overruns memory).
-- With dsfforza.asi: IDs 0..GARAGE_MAX_ID, GARAGE_MAX_GROUPS groups, and a car
-- that doesn't fit is left out by the plugin.
local function groupKey(name)
	return string.sub(string.upper(tostring(name or "")), 1, 15)
end

-- Returns the ManufacturerName to use (the car's own, or a shared FORZA group
-- when its make has no room), or nil when the car can't be listed. Same rule
-- as tools/port_batch.py garage_group; one group slot is kept for FORZA.
local function garageGroup(name, counts)
	local key = groupKey(name)
	local groups = 0
	for _ in pairs(counts) do groups = groups + 1 end
	if counts[key] then
		if counts[key] < GARAGE_MAX_PER_GROUP then return name end
	elseif groups + (counts[OVERFLOW_GROUP] and 0 or 1) < GARAGE_MAX_GROUPS then
		return name
	end
	local n = 1
	while true do
		local overflow = n == 1 and OVERFLOW_GROUP or (OVERFLOW_GROUP .. " " .. n)
		if counts[overflow] and counts[overflow] < GARAGE_MAX_PER_GROUP then return overflow end
		if not counts[overflow] then
			return groups < GARAGE_MAX_GROUPS and overflow or nil
		end
		n = n + 1
	end
end

function M.registerStats()
	if type(vehicleStats) ~= "table" then
		log("vehicleStats missing; HUD entries not registered")
		return false
	end
	resolvePorted()
	-- Selectable stock (or other mods') cars per garage group, before ours.
	local counts = {}
	for id, entry in pairs(vehicleStats) do
		if type(id) == "number" and id <= GARAGE_MAX_ID and CARS[id] == nil and type(entry) == "table"
			and entry.Selectable then
			local key = groupKey(entry.ManufacturerName)
			counts[key] = (counts[key] or 0) + 1
		end
	end
	for _, id in ipairs(sortedIds()) do
		local car = CARS[id]
		local entry = buildStats(id, car)
		if entry then
			if OPTIONS.garage and id <= GARAGE_MAX_ID then
				local group = garageGroup(entry.ManufacturerName, counts)
				if group then
					entry.ManufacturerName = group
					entry.Selectable = true
					counts[groupKey(group)] = (counts[groupKey(group)] or 0) + 1
					if OPTIONS.writeProfile then
						-- Same path Felony Rework uses for cars outside the normal unlock
						-- flow; updateVehicleUnlocks(1) then unlocks it on the next load too.
						entry.UnlockedInChapter = 1
					end
				else
					log(string.format("id %d: no room in the garage maker list; not selectable", id))
				end
			end
			car.selectable = entry.Selectable
			vehicleStats[id] = entry
			log(string.format("vehicleStats[%d] <- [%d] (%s / %s, selectable %s)", id, car.source,
				tostring(entry.ManufacturerName), tostring(entry.ModelName), tostring(entry.Selectable)))
		end
	end
	return true
end

---------------------------------------------------------------------------
-- (2) Garage / unlocks
---------------------------------------------------------------------------

-- The save (research/batch.md, confirmed-static) keeps vehicle flags in a
-- 304-slot {id, flags} list plus 320-bit arrays indexed by ID: an ID of 320 or
-- more set unlocked lands in the next array (other cars' "driven" bits), and a
-- full list crashes. The game's Lua sets these for every vehicleStats key in
-- the cheat "unlock all" (unlockAllVehicles) and from unlock fields, so calls
-- for our IDs 304 and up are dropped; only Lua callers pass through here.
-- With dsfforza.asi's save hooks (ASI.save) the limit is 999 instead.
local function guardProfile()
	if type(ProfileSettings) ~= "table" or state.wrapped.profile then
		return
	end
	for _, name in ipairs({ "SetVehicleUnlocked", "SetVehicleOwned" }) do
		local original = ProfileSettings[name]
		if type(original) == "function" then
			ProfileSettings[name] = function(id, ...)
				local cars = _G.dsfforzaRegister.cars
				if type(id) == "number" and id > _G.dsfforzaRegister.saveMaxId and cars and cars[id] ~= nil then
					return nil
				end
				return original(id, ...)
			end
		end
	end
	state.wrapped.profile = true
	log("guarding ProfileSettings.SetVehicleUnlocked/Owned for added IDs above " .. SAVE_MAX_ID)
end

function M.unlockInProfile()
	if not (OPTIONS.writeProfile or OPTIONS.probe) then
		return
	end
	for _, id in ipairs(sortedIds()) do
		local before = try(ProfileSettings.GetVehicleUnlocked, id) .. "/" .. try(ProfileSettings.GetVehicleOwned, id)
		if OPTIONS.writeProfile and CARS[id].selectable and id <= GARAGE_MAX_ID then
			try(ProfileSettings.SetVehicleUnlocked, id)
			try(ProfileSettings.SetVehicleOwned, id)
		end
		local after = try(ProfileSettings.GetVehicleUnlocked, id) .. "/" .. try(ProfileSettings.GetVehicleOwned, id)
		log(string.format("id %d profile unlocked/owned: %s -> %s (writeProfile %s)", id, before, after,
			tostring(OPTIONS.writeProfile)))
	end
end

---------------------------------------------------------------------------
-- (1) Traffic: interesting-vehicle regions
---------------------------------------------------------------------------

-- Each chapter package (ScriptsChapterN.fchunk, traffic\trafficSpoolingData.lua)
-- sets spooling.interestingVehicles = { {position, radius, vehicleList}, x17 }
-- and loadingSystem.requestInterestingVehicleSettings passes it to
-- InterestingVehicleManager.SetInterestingVehicleRegions. Adding the IDs to
-- the table on the way in covers every chapter load.
local function injectRegions(regions)
	if type(regions) ~= "table" then
		return 0
	end
	local added = 0
	-- With more than regionCars cars, car k (in ID order) goes to every m-th
	-- region, offset by k, so each region gets about regionCars of them.
	local traffic = {}
	for _, id in ipairs(sortedIds()) do
		if CARS[id].traffic ~= false then table.insert(traffic, id) end
	end
	local m = math.max(1, math.ceil(#traffic / math.max(1, OPTIONS.regionCars or #traffic)))
	for k, id in ipairs(traffic) do
		local car = CARS[id]
		do
			local wanted
			if type(car.regions) == "table" then
				wanted = {}
				for _, index in ipairs(car.regions) do wanted[index] = true end
			elseif m > 1 then
				wanted = {}
				for index = 1, #regions do
					if (index + k) % m == 0 then wanted[index] = true end
				end
			end
			for index, region in ipairs(regions) do
				if type(region) == "table" and type(region.vehicleList) == "table"
					and (wanted == nil or wanted[index]) then
					local present = false
					for _, model in ipairs(region.vehicleList) do
						if model == id then present = true break end
					end
					if not present then
						table.insert(region.vehicleList, id)
						added = added + 1
					end
				end
			end
		end
	end
	return added
end

local function wrapInterestingVehicles()
	if type(InterestingVehicleManager) ~= "table"
		or type(InterestingVehicleManager.SetInterestingVehicleRegions) ~= "function" then
		log("InterestingVehicleManager.SetInterestingVehicleRegions missing")
		return
	end
	if not state.wrapped.regions then
		state.originalSetRegions = InterestingVehicleManager.SetInterestingVehicleRegions
		InterestingVehicleManager.SetInterestingVehicleRegions = function(regions, ...)
			local added = injectRegions(regions)
			log(string.format("SetInterestingVehicleRegions: %d regions, %d entries added",
				type(regions) == "table" and #regions or -1, added))
			return state.originalSetRegions(regions, ...)
		end
		state.wrapped.regions = true
		log("wrapped InterestingVehicleManager.SetInterestingVehicleRegions")
	end
	-- Already in a chapter (hot reload): re-send the current regions.
	if type(spooling) == "table" and type(spooling.interestingVehicles) == "table" then
		InterestingVehicleManager.SetInterestingVehicleRegions(spooling.interestingVehicles)
	end
end

-- spooling.isMissionVehiclesSpooled requests "All chapters" plus the
-- chapter's pot on every chapter load and releases anything not listed.
local function addToMissionPot()
	local pots = type(spooling) == "table" and spooling.missionSpecificVehiclePots
	if type(pots) ~= "table" or type(pots["All chapters"]) ~= "table" then
		log("spooling.missionSpecificVehiclePots missing")
		return
	end
	for id in pairs(CARS) do
		local present = false
		for _, model in ipairs(pots["All chapters"]) do
			if model == id then present = true break end
		end
		if not present then
			table.insert(pots["All chapters"], id)
			log(string.format("id %d added to mission pot \"All chapters\"", id))
		end
	end
end

---------------------------------------------------------------------------
-- Nearby traffic (probe)
---------------------------------------------------------------------------

local function playerGameVehicle()
	if type(localPlayer) == "table" and type(localPlayer.currentVehicle) == "table" then
		return localPlayer.currentVehicle.gameVehicle
	end
	return nil
end

local function nearbyTraffic(radius)
	local gv = playerGameVehicle()
	if gv == nil or type(vehicleManager) ~= "table" then
		return {}
	end
	local ok, list = pcall(vehicleManager.findVehiclesInTraffic, gv.position, gv.heading, radius, {
		ignoreSciptOwnedVehicles = true, ignoreCops = true, ignoreThrown = true,
	}, 150)
	return ok and type(list) == "table" and list or {}
end

local function countModel(list, id)
	local n = 0
	for _, gv in ipairs(list) do
		local ok, model = pcall(function() return gv.model_id end)
		if ok and model == id then n = n + 1 end
	end
	return n
end

---------------------------------------------------------------------------
-- Probe
---------------------------------------------------------------------------

local function countKeys(t)
	local n = 0
	if type(t) == "table" then for _ in pairs(t) do n = n + 1 end end
	return n
end

function M.probeOnce()
	log(string.format("vehiclesAvailable: %s (%d keys)", type(vehiclesAvailable), countKeys(vehiclesAvailable)))
	log(string.format("Vehicles: %s (%d keys)", type(Vehicles), countKeys(Vehicles)))
	for id, car in pairs(CARS) do
		local va = type(vehiclesAvailable) == "table" and vehiclesAvailable[id] or nil
		local vv = type(Vehicles) == "table" and Vehicles[id] or nil
		log(string.format("id %d: vehiclesAvailable %s%s, Vehicles %s, getVehicleStats %s, source %d profile %s/%s",
			id, type(va), type(va) == "table" and (" name=" .. tostring(va.name)) or "", type(vv),
			type(getVehicleStats) == "function" and type(getVehicleStats(id)) or "n/a", car.source,
			try(ProfileSettings.GetVehicleUnlocked, car.source), try(ProfileSettings.GetVehicleOwned, car.source)))
	end
	if type(civilianTraffic) == "table" then
		local names = {}
		for name in pairs(civilianTraffic) do table.insert(names, tostring(name)) end
		table.sort(names)
		log("civilianTraffic: " .. table.concat(names, " "))
	end
end

local probe = { ticks = 0 }

local function update()
	probe.ticks = probe.ticks + 1
	local parts = {}
	if OPTIONS.probe and probe.ticks % 20 == 1 then
		local list = nearbyTraffic(250)
		for id in pairs(CARS) do
			table.insert(parts, string.format("id %d x%d", id, countModel(list, id)))
		end
		log(string.format("t%d traffic %d cars, %s; IV enabled %s slots %s; spooler busy %s", probe.ticks, #list,
			table.concat(parts, " "), try(InterestingVehicleManager.IsEnabled),
			try(InterestingVehicleManager.GetDesiredSlotCount), try(TrafficSpooler.Busy)))
	end
	if probe.ticks >= OPTIONS.probeTicks then
		removeUserUpdateFunction("dsfforza_register")
		log("probe done")
	end
end

---------------------------------------------------------------------------

function M.apply()
	state.cars = CARS
	state.saveMaxId = SAVE_MAX_ID
	if ASI.save or ASI.garage then
		log(string.format("dsfforza.asi: save hooks %s, garage IDs 0..%d, %d maker groups of %d",
			tostring(ASI.save), GARAGE_MAX_ID, GARAGE_MAX_GROUPS, GARAGE_MAX_PER_GROUP))
	else
		log("dsfforza.asi garage module not active: garage IDs 0..303, 40 maker groups, save flags up to 303")
	end
	M.registerStats()
	if OPTIONS.guardProfile then
		guardProfile()
	end
	if OPTIONS.interesting then
		wrapInterestingVehicles()
	end
	if OPTIONS.missionPot then
		addToMissionPot()
	end
	if OPTIONS.probe then
		M.probeOnce()
	end
	M.unlockInProfile()
	if OPTIONS.probe then
		probe.ticks = 0
		addUserUpdateFunction("dsfforza_register", update, 30)
	end
end

M.cars = CARS
M.options = OPTIONS
M.asi = ASI
M.injectRegions = injectRegions

M.apply()

return M
