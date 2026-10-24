-- dsfforza.asi garage/save test (tools/asi/test; not installed for players).
-- Loaded by tools/asi/test/lua/bootstrap.lua, which run-series -LuaFiles puts
-- over the test bootstrap. Settings: dsfforza\\savetest_cfg.lua, e.g.
--   return { fill = { first = 400, count = 600, perMaker = 10, own = 2 },
--            set = { 305, 320, 500, 650, 999 }, check = { 305, 320, 500, 650, 999 }, delay = 20 }
--   fill   at load: vehicleStats entries for IDs first..first+count-1, copies of
--          the source entry (266) with ManufacturerName "DSFT nn" (perMaker cars
--          per maker), Selectable; once in a car, every own-th of them is marked
--          unlocked and owned.
--   set    once in a car: SetVehicleUnlocked/Owned/Driven/DrivenInTakeARide for
--          each ID, then TriggerAutoSave.
--   check  once in a car (before set): log every flag of each ID.
-- Output: Driver.log "[dsfforza.savetest] ...".

local function log(message)
	print("[dsfforza.savetest] " .. message)
end

local cfg = {}
do
	local found, c = user_open("dsfforza\\savetest_cfg.lua")
	if found and type(c) == "table" then cfg = c end
end

local P = ProfileSettings
local MAGIC = 8000000

local function call(fn, ...)
	if type(fn) ~= "function" then return "n/a" end
	local ok, a = pcall(fn, ...)
	return ok and tostring(a) or ("err(" .. tostring(a) .. ")")
end

local function q(k)
	local ok, v = pcall(P.GetVehicleUnlocked, MAGIC + k)
	return ok and v == true
end

local function limit(base, lo, hi)
	if not q(base + lo) then return "none" end
	while lo < hi do
		local mid = math.floor((lo + hi + 1) / 2)
		if q(base + mid) then lo = mid else hi = mid - 1 end
	end
	return tostring(lo)
end

log(string.format("asi: save %s, garage %s, groups %s, max listed id %s", tostring(q(0)), tostring(q(1)),
	limit(0x1000, 40, 4000), limit(0x2000, 0, 4000)))

local function flags(id)
	return string.format("id %d: unlocked %s owned %s new %s driven %s", id, call(P.GetVehicleUnlocked, id),
		call(P.GetVehicleOwned, id), call(P.GetVehicleNew, id), call(P.GetVehicleDriven, id))
end

if type(cfg.fill) == "table" and type(vehicleStats) == "table" then
	local f = cfg.fill
	local source = vehicleStats[f.source or 266]
	local per = f.perMaker or 20
	for i = 0, (f.count or 0) - 1 do
		local id = (f.first or 400) + i
		local e = {}
		for k, v in pairs(source) do e[k] = v end
		e.UnlockedInChapter, e.UnlockedInGarage, e.UnlockByActivity = nil, nil, nil
		e.ManufacturerName = string.format("DSFT %02d", math.floor(i / per))
		e.Selectable = true
		e.Price = 1000 + id
		vehicleStats[id] = e
	end
	log(string.format("fill: vehicleStats %d..%d, %d per maker", f.first or 400, (f.first or 400) + (f.count or 0) - 1, per))
end

-- merge = { into = "ID:242753", from = { "ID:242757", "ID:242755" } }: the
-- garage files every car of the `from` makers under `into` (one big group of
-- real, drivable stock cars: Chevrolet 16 + Ford 13 + Dodge 10 = 39).
if type(cfg.merge) == "table" and type(vehicleStats) == "table" then
	local from, n = {}, 0
	for _, name in ipairs(cfg.merge.from or {}) do from[name] = true end
	for id, e in pairs(vehicleStats) do
		if type(e) == "table" and from[e.ManufacturerName] then
			e.ManufacturerName = cfg.merge.into
			n = n + 1
		end
	end
	log(string.format("merge: %d entries filed under %s", n, tostring(cfg.merge.into)))
end

local inCar, done = 0, false
removeUserUpdateFunction("dsfforza_savetest")
addUserUpdateFunction("dsfforza_savetest", function()
	if done then return end
	local car = type(localPlayer) == "table" and localPlayer.currentVehicle
	if type(car) == "table" and car.gameVehicle then inCar = inCar + 1 end
	if inCar < (cfg.delay or 20) then return end
	done = true
	log("in a car; profile checks start")
	log("take-a-ride count " .. call(P.GetNumVehiclesDrivenTakeARide))
	for _, id in ipairs(cfg.check or {}) do log("check " .. flags(id)) end
	if type(cfg.fill) == "table" and (cfg.fill.own or 0) > 0 then
		local n = 0
		for i = 0, (cfg.fill.count or 0) - 1, cfg.fill.own do
			local id = (cfg.fill.first or 400) + i
			P.SetVehicleUnlocked(id)
			P.SetVehicleOwned(id)
			n = n + 1
		end
		log(string.format("fill: %d IDs marked unlocked and owned", n))
	end
	if type(cfg.set) == "table" then
		for _, id in ipairs(cfg.set) do
			P.SetVehicleUnlocked(id)
			P.SetVehicleOwned(id)
			P.SetVehicleDriven(id)
			P.SetVehicleDrivenInTakeARide(id)
			log("set " .. flags(id))
		end
		log("take-a-ride count " .. call(P.GetNumVehiclesDrivenTakeARide))
	end
	if cfg.set or (type(cfg.fill) == "table" and (cfg.fill.own or 0) > 0) then
		log("TriggerAutoSave: " .. call(P.TriggerAutoSave))
	end
	log("profile checks done")
end, 30)
