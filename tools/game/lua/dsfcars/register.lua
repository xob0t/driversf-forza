-- dsfcars: register added vehicles with the game's Lua systems.
--
-- Loaded from bootstrap.lua's Bootstrapper.Launch() (the test bootstrap in
-- tools/game/lua and the player one tools/install.ps1 installs).
-- Launch runs before the game's own launchScripts(), after the initial load
-- has created vehicleStats, so every hook below is in place before the first
-- chapter loads. Background: research/game-lua.md.
--
-- What it does per car in CARS:
--   HUD     vehicleStats[id] = copy of a vanilla entry plus overrides
--           (badge, name strings, stat bars). The HUD, garage and unlock
--           panels read this table through getVehicleStats(id).
--   traffic adds the ID to the chapter's "interesting vehicle" regions
--           (spooling.interestingVehicles), which the engine spools and
--           spawns into traffic.
--   garage  Selectable/Tier/Price in vehicleStats, for IDs below 304 (below
--           1000 with dsfcars.asi) while the garage's maker list has room
--           (garageGroup); with writeProfile it also marks the car unlocked
--           and owned in the save.
--   save    guardProfile keeps the game's own Lua (cheat "unlock all", unlock
--           panels) from writing save flags for IDs 304 and up, which the
--           save can't hold (research/batch.md); with dsfcars.asi it lets
--           them through (the plugin keeps them in dsfcars-garage.dat).
--
-- The cars of dsfcars\packages.lua load only through dsfcars.asi's [archive] packages, so
-- they are registered only when the plugin says it has loaded them (ASI.packages and the
-- per-ID query below): with the ASI loader missing or blocked, the plugin off, or no patches
-- on this exe, none of them gets a vehicleStats entry, a garage card or a traffic slot (#511).
--
-- Player defaults: no probes, no save writes.
-- The test harness (a config.lua with `ids`) turns the probe on.
--
-- Output goes to Driver.log as "[dsfcars.register] ...".

local M = {}

-- Keep the Lua garbage collector stopped while a user file is compiled and run, as the
-- player bootstrap does (research/in-game-tests.md, "Lua crashes loading big user files"):
-- here too, so another mod's bootstrap that only adds user_open("dsfcars\\register.lua")
-- loads the car lists (packages.lua, packages_hud.lua) the safe way. user_open is read-only
-- in DriverNGHook's _G metatable, hence rawset; the rawget guard keeps it to one wrapper.
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

-- Per-car settings, from dsfcars\packages.lua (the installed car packages: dsff
-- car_manager), dsfcars\ported.lua (in the in-game tests: the merged archive of a build,
-- src/dsfforza/build/port_car.py or port_batch.py) or config.lua's register.cars. Empty
-- without any, so nothing is registered for cars the game can't load.
-- `source` is the vanilla model whose vehicleStats entry is copied; every other
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
	                      --     over the 17 regions so vanilla interesting vehicles keep their share
	garage = true,        -- (2) Selectable = true in vehicleStats (IDs below 304, below 1000 with dsfcars.asi,
	                      --     while the maker list has room)
	writeProfile = false, -- (2) ProfileSettings.SetVehicleUnlocked/Owned for garage cars: WRITES THE SAVE
	                      --     (unlocked + owned flags, a "new vehicle" notice); back the save up first
	guardProfile = true,  -- (2) ignore Lua SetVehicleUnlocked/Owned for added IDs 304 and up (save can't hold them;
	                      --     with dsfcars.asi's save hooks only IDs above 999)
	probe = false,        -- log what the game sees (vehiclesAvailable, profile flags, traffic counts);
	                      -- on by default in test sessions (config.lua with `ids`)
	probeTicks = 360,     -- probe updates (every 30 frames), about 3 minutes
}

-- Garage maker list (confirmed-static, research/batch.md): at most 40 maker
-- groups and 20 cars per group, no capacity check; only IDs 0..303 are listed.
-- The save holds flags for IDs up to 303 only.
--
-- dsfcars.asi's garage module (tools/asi, research/asi.md) lifts both: the
-- garage walks IDs up to 999, holds more maker groups of any number of cars
-- (and drops what doesn't fit instead of overrunning memory), and flags of IDs 304..999 go to
-- <savegames>\dsfcars-garage.dat instead of the vanilla save block. Lua can't
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
-- k = 2: [archive] packages is on and its patches applied (mod_archive archive_packages_active);
-- k = 0x4000 + id: the package of that ID is loaded (archive_package_loaded).
local ASI = { save = asiQuery(0), garage = asiQuery(1), packages = asiQuery(2) }
if ASI.garage then
	ASI.groups = asiLimit(0x1000, 40, 4000)   -- k = 0x1000 + n: n <= max groups
	ASI.maxId = asiLimit(0x2000, 303, 4000)   -- k = 0x2000 + id: id is listed
	ASI.cars = asiLimit(0x3000, 20, 4000)     -- k = 0x3000 + n: n <= cars per maker group (nil: 20)
end

local GARAGE_MAX_GROUPS = ASI.garage and ASI.groups or 40
local GARAGE_MAX_PER_GROUP = ASI.garage and ASI.cars or 20
local VANILLA_MAX_ID = 303   -- the vanilla garage lists IDs 0..303
local GARAGE_MAX_ID = ASI.garage and ASI.maxId or VANILLA_MAX_ID
local SAVE_MAX_ID = ASI.save and 999 or 303   -- highest ID whose save flags are safe to set
local OVERFLOW_GROUP = "ADDED CARS"   -- makes without a group of their own share ADDED CARS, ADDED CARS 2, ...

-- The cars of packages.lua or ported.lua (below) replace the sample list above. The HUD
-- badge and name come from the numeric Manufacturer and Model fields (vehicles_font glyph
-- codes; ManufacturerName/ModelName strings don't reach it). packages_hud.lua (or hud.lua)
-- gives every car its own; for a car it doesn't cover, the vanilla entries are matched on Picture1 ("nissan_370z"):
--   same make and model  -> copy that vanilla entry (badge, name, pictures, stats)
--   same make            -> host entry with that make's badge and no nameplate
--   make not in DSF      -> host entry with no badge and no nameplate
-- Never another model's plate or name: a Ford Ka without a hud.lua entry used
-- to take the closest-year Ford's, and showed "SHELBY GT500" (issue #98).
local BLANK_GLYPH = 32   -- " ": a text of spaces and control characters draws nothing
                         -- (the renderer drops it; research/gui-assets.md), whatever the font
local function token(text)
	return string.lower(tostring(text or "")):gsub("[^%w]", "")
end

local function vanillaMatch(car)
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

-- Per-ID vehicleStats overrides: Picture1, Manufacturer, Model (vehicles_font codes),
-- ManufacturerName, ModelName. The installed car packages' are in dsfcars\packages_hud.lua
-- (dsff car_manager, src/dsfforza/build/car_gui.py; their pictures, glyphs and strings are in
-- dsfcars\generated\, which dsfcars.asi opens in place of the game's files). In the
-- in-game tests a build's merged archive brings its gui\hud.lua as dsfcars\hud.lua (dsff
-- gui_assets build), the same fields. Picture1 is "dsfforza_<media>": an entry made for
-- another car (a hud.lua of another build) is not used (hudEntry).
local HUD
local function addHud(name)
	local found, entries = user_open(name)
	if found and type(entries) == "table" then
		HUD = HUD or {}
		for id, entry in pairs(entries) do
			if HUD[id] == nil then
				HUD[id] = entry
			end
		end
	end
end
if ASI.packages then
	addHud("dsfcars\\packages_hud.lua")   -- the packages' cars only; their pictures and glyphs come through the plugin too
end
addHud("dsfcars\\hud.lua")

-- The cars: dsfcars\packages.lua (the enabled car packages, written by dsff car_manager;
-- dsfcars.asi [archive] packages loads them) and, in the in-game tests, dsfcars\ported.lua
-- (a build's dngvehicles.lua beside its merged archive, tools/wsb/guest/run-test.ps1). The
-- same fields per car; either replaces the sample list above.
local PORTED
local function portedCar(car)
	return {
		source = car.host, media = car.media,
		make = car.make, model = car.model, yearText = car.year,
		year = car.year ~= "" and car.year or nil,
		speed = car.speed, strength = car.strength, drift = car.drift,  -- handling_dsf.stat_bars
		traffic = true,
	}
end
-- loaded(id), when given, says whether the game has that car's model; a car it hasn't is left
-- out. Returns the number of cars the file lists and the IDs left out.
local function addCars(name, loaded)
	local found, list = user_open(name)
	local listed, missing = 0, {}
	if not (found and type(list) == "table" and type(list.cars) == "table") then
		return listed, missing
	end
	for id, car in pairs(list.cars) do
		listed = listed + 1
		if loaded and not loaded(id) then
			table.insert(missing, id)
		else
			if not PORTED then
				PORTED = list
				CARS = {}
			end
			if CARS[id] == nil then
				CARS[id] = portedCar(car)
			end
		end
	end
	table.sort(missing)
	return listed, missing
end
-- packages.lua: only the cars dsfcars.asi has loaded (none without ASI.packages).
local PACKAGES = {}
PACKAGES.listed, PACKAGES.missing = addCars("dsfcars\\packages.lua", function(id)
	return ASI.packages and type(id) == "number" and asiQuery(0x4000 + id)
end)
addCars("dsfcars\\ported.lua")

do
	local found, config = user_open("dsfcars\\config.lua")
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

-- The car manager's interesting-vehicle settings, dsfcars\traffic.lua (src/dsfforza/build/traffic.py),
-- by model ID: cars[id].traffic = false keeps a car out of every region, cars[id].regions pins it
-- to those regions; ports = false gives the other cars no region; vanilla = false takes the game's
-- own interesting vehicles out of every region; per_region is regionCars. Read
-- only with the car packages loaded, as packages.lua is (#511).
local TRAFFIC = {}
if ASI.packages then
	local found, t = user_open("dsfcars\\traffic.lua")
	if found and type(t) == "table" then TRAFFIC = t end
end
local function applyTraffic()
	if type(TRAFFIC.cars) == "table" then
		for id, s in pairs(TRAFFIC.cars) do
			if CARS[id] and type(s) == "table" then
				if s.traffic == false then CARS[id].traffic = false end
				if type(s.regions) == "table" then CARS[id].regions = s.regions end
			end
		end
	end
	if type(TRAFFIC.per_region) == "number" then OPTIONS.regionCars = TRAFFIC.per_region end
end

local function sortedIds()
	local ids = {}
	for id in pairs(CARS) do table.insert(ids, id) end
	table.sort(ids)
	return ids
end

local function log(message)
	print("[dsfcars.register] " .. message)
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
_G.dsfcarsRegister = _G.dsfcarsRegister or { wrapped = {} }
local state = _G.dsfcarsRegister

---------------------------------------------------------------------------
-- (3) HUD name, badge and stat bars: vehicleStats[id]
---------------------------------------------------------------------------

-- Fields that make the game's own unlock code (vehicleManager.updateVehicleUnlocks)
-- call ProfileSettings.SetVehicleUnlocked for this ID on chapter load.
local UNLOCK_FIELDS = { "UnlockedInChapter", "UnlockedInGarage", "UnlockByActivity" }

-- Resolve vanilla matches once vehicleStats exists (called from M.registerStats).
local function resolvePorted()
	if not PORTED then
		return
	end
	local own = {}
	for id in pairs(CARS) do own[id] = true end
	for id, car in pairs(CARS) do
		if car.make and not car.resolved then
			car.resolved = true
			local exact, near = vanillaMatch({ make = car.make, model = car.model, year = car.yearText, ids = own })
			if exact then
				car.source = exact
				car.year = nil
			else
				-- The host's entry under this car's own names: the make's badge if DSF
				-- has the make, and no nameplate (hud.lua's, when it covers the car,
				-- replaces both). Plain-text names don't show in the garage.
				car.manufacturer = near and vehicleStats[near].Manufacturer or BLANK_GLYPH
				car.manufacturerName = near and vehicleStats[near].ManufacturerName or string.upper(car.make)
				car.modelIndex = BLANK_GLYPH
				car.modelName = string.upper(tostring(car.model or ""))
			end
			log(string.format("id %d (%s %s): vanilla match %s, same make %s", id, tostring(car.make),
				tostring(car.model), tostring(exact), tostring(near)))
		end
	end
end

-- hud.lua's entry for this ID, unless it was made for another car: its picture is
-- "dsfforza_<media>" (gui_assets), so a hud.lua left from another build (other cars
-- at these IDs) can't give a car another car's badge, plate and picture.
local warnedHud = false
local function hudEntry(id, car)
	if not HUD then
		if PORTED and not warnedHud then
			warnedHud = true
			log("no dsfcars\\hud.lua: ported cars get their maker's vanilla badge (or none) and no nameplate")
		end
		return nil
	end
	local hud = HUD[id]
	if type(hud) ~= "table" then
		if PORTED then log(string.format("id %d: no hud.lua entry", id)) end
		return nil
	end
	local picture = hud.Picture1
	if car.media and type(picture) == "string" and string.sub(picture, 1, 9) == "dsfforza_"
		and picture ~= "dsfforza_" .. string.lower(car.media) then
		log(string.format("id %d: hud.lua entry is for %s, not %s (another build's hud.lua); not used", id,
			string.sub(picture, 10), tostring(car.media)))
		return nil
	end
	return hud
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
	-- Garage picture, HUD badge and model glyph made by src/dsfforza/game/gui_assets.py
	-- (dsfcars\hud.lua; needs its gui.sp and Fonts.fchunk copies installed).
	local hud = hudEntry(id, car)
	if hud then
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
-- With dsfcars.asi: IDs 0..GARAGE_MAX_ID, GARAGE_MAX_GROUPS groups of
-- GARAGE_MAX_PER_GROUP cars (the plugin's [garage] max_cars, 1000: a maker keeps
-- all its cars in one group), and a car that doesn't fit is left out by the plugin.
local function groupKey(name)
	return string.sub(string.upper(tostring(name or "")), 1, 15)
end

-- Returns the ManufacturerName to use (the car's own, or a shared ADDED CARS group
-- when its make has no room), or nil when the car can't be listed. Same rule
-- as src/dsfforza/build/port_batch.py garage_group; one group slot is kept for ADDED CARS.
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
	-- Only dsfcars.asi's garage walks past ID 303, and every model there is one of ours, so
	-- another mod's Selectable entry above 303 is a placeholder, not a car: Felony Rework's
	-- 622 and 623 (activity rewards, ManufacturerName "", research/felony-rework.md) would
	-- list as an empty maker group of "NOTHING" cards that spool a model the archive
	-- doesn't have. The vanilla garage (0..303) never lists them, so neither does this one.
	for id, entry in pairs(vehicleStats) do
		if type(id) == "number" and id > VANILLA_MAX_ID and id <= GARAGE_MAX_ID and CARS[id] == nil
			and type(entry) == "table" and entry.Selectable then
			entry.Selectable = false
			log(string.format("vehicleStats[%d] (%s / %s) is not a model: not listed in the garage", id,
				tostring(entry.ManufacturerName), tostring(entry.ModelName)))
		end
	end
	-- Selectable vanilla (or other mods') cars per garage group, before ours.
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
-- With dsfcars.asi's save hooks (ASI.save) the limit is 999 instead.
local function guardProfile()
	if type(ProfileSettings) ~= "table" or state.wrapped.profile then
		return
	end
	for _, name in ipairs({ "SetVehicleUnlocked", "SetVehicleOwned" }) do
		local original = ProfileSettings[name]
		if type(original) == "function" then
			ProfileSettings[name] = function(id, ...)
				local cars = _G.dsfcarsRegister.cars
				if type(id) == "number" and id > _G.dsfcarsRegister.saveMaxId and cars and cars[id] ~= nil then
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
-- SetInterestingVehicleRegions (0x0067D040) copies each list into a 128-entry stack buffer and
-- doesn't check the length: the 129th ID lands on the count (#562; research/traffic.md 2). The
-- vanilla lists hold up to 110 (Epilogue, region 1), so added cars stop at 128.
local REGION_MAX = 128

local function injectRegions(regions)
	if type(regions) ~= "table" then
		return 0
	end
	local added, full = 0, 0
	if TRAFFIC.vanilla == false then
		for _, region in ipairs(regions) do
			if type(region) == "table" and type(region.vehicleList) == "table" then
				local kept = {}
				for _, model in ipairs(region.vehicleList) do
					if CARS[model] then table.insert(kept, model) end
				end
				region.vehicleList = kept
			end
		end
	end
	-- A pinned car goes at the front of its regions: the engine's pick favours the front of a list
	-- (idx = round(n * (1 - cos(pi * r / 2))), 0x007EB970; research/traffic.md 2); the others go last.
	local front = {}
	-- With more than regionCars cars, car k (in ID order) goes to every m-th
	-- region, offset by k, so each region gets about regionCars of them.
	local traffic = {}
	for _, id in ipairs(sortedIds()) do
		local car = CARS[id]
		if car.traffic ~= false and (TRAFFIC.ports ~= false or type(car.regions) == "table") then
			table.insert(traffic, id)
		end
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
					if present then
						-- already listed
					elseif #region.vehicleList >= REGION_MAX then
						full = full + 1
					else
						if type(car.regions) == "table" then
							front[index] = (front[index] or 0) + 1
							table.insert(region.vehicleList, front[index], id)
						else
							table.insert(region.vehicleList, id)
						end
						added = added + 1
					end
				end
			end
		end
	end
	if full > 0 then
		log(string.format("SetInterestingVehicleRegions: %d entries left out, their regions hold %d IDs (#562)", full,
			REGION_MAX))
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
		removeUserUpdateFunction("dsfcars_register")
		log("probe done")
	end
end

---------------------------------------------------------------------------

function M.apply()
	applyTraffic()
	state.cars = CARS
	state.saveMaxId = SAVE_MAX_ID
	if ASI.save or ASI.garage then
		log(string.format("dsfcars.asi: save hooks %s, garage IDs 0..%d, %d maker groups of %d",
			tostring(ASI.save), GARAGE_MAX_ID, GARAGE_MAX_GROUPS, GARAGE_MAX_PER_GROUP))
	else
		log("dsfcars.asi garage module not active: garage IDs 0..303, 40 maker groups, save flags up to 303")
	end
	if PACKAGES.listed > 0 and not ASI.packages then
		if ASI.save or ASI.garage then
			log(string.format("dsfcars.asi has not loaded the car packages: the %d installed cars are not "
				.. "registered (no garage, traffic or HUD entry); see plugins\\dsfcars.log", PACKAGES.listed))
		else
			log(string.format("dsfcars.asi is not active: the %d installed cars are not loaded and not registered "
				.. "(no garage, traffic or HUD entry); see plugins\\dsfcars.log (none there: the ASI loader "
				.. "didn't load the plugin)", PACKAGES.listed))
		end
	elseif #PACKAGES.missing > 0 then
		local ids = {}
		for _, id in ipairs(PACKAGES.missing) do table.insert(ids, tostring(id)) end
		log(string.format("dsfcars.asi has not loaded %d of the %d installed cars (IDs %s): not registered; "
			.. "see plugins\\dsfcars.log", #PACKAGES.missing, PACKAGES.listed, table.concat(ids, ", ")))
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
		addUserUpdateFunction("dsfcars_register", update, 30)
	end
end

M.cars = CARS
M.options = OPTIONS
M.asi = ASI
M.packages = PACKAGES
M.injectRegions = injectRegions

M.apply()

return M
