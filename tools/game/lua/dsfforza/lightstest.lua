-- dsfforza: lamp test for converted cars (research/lights.md), driven by the
-- harness (run-test.ps1 -Lights, run-series.ps1 -Lights).
--
-- clonetest.lua loads this module when dsfforza\config.lua has `lights = {...}`
-- and hands it each car once the player drives it (instead of the chase and
-- cockpit views). For each car:
--   1. a second copy is spawned 12 m ahead, facing the player's car, so the
--      chase camera sees the player's rear lamps and the copy's front lamps;
--   2. it logs "[dsfforza.lights] ready <n> phases" and then applies the phase
--      the harness asks for in dsfforza\lightsync.lua ("return k"; 0 = wait,
--      -1 = finish), re-reading that file every update, to both cars with
--      GameVehicleResource.setLights{gameVehicle, LightType, State};
--      phases 1..#PHASES in daylight, then the same again under a dark mood;
--   3. without a sync file change for SYNC_TIMEOUT updates it advances by
--      itself, so it also works without the harness;
--   4. logs "[dsfforza.lights] done", removes the copy and returns control.
-- Light types (Driver.exe 0xDFD8C0): Headlights, Brakes, DaytimeRunning,
-- LeftIndicator, RightIndicator, Reverse, Siren, DippedHeadlights, FogLights, All.

local M = {}

local PHASES = {
	{ name = "off", set = { All = false } },
	{ name = "headlights", set = { All = false, Headlights = true } },
	{ name = "running", set = { All = false, DaytimeRunning = true } },
	{ name = "brakes", set = { All = false, Brakes = true } },
	{ name = "running+brakes", set = { All = false, DaytimeRunning = true, Brakes = true } },
	{ name = "reverse", set = { All = false, Reverse = true } },
	{ name = "left", set = { All = false, LeftIndicator = true } },
	{ name = "right", set = { All = false, RightIndicator = true } },
	{ name = "all", set = { All = true } },
}
local ORDER = { "All", "Headlights", "DaytimeRunning", "Brakes", "Reverse", "LeftIndicator", "RightIndicator",
	"DippedHeadlights", "FogLights", "Siren" }
local MASKS = { Headlights = 1, Brakes = 2, DaytimeRunning = 4, LeftIndicator = 8, RightIndicator = 16, Reverse = 32,
	Siren = 64, DippedHeadlights = 128, FogLights = 256 }
local TYPES = { "Headlights", "Brakes", "DaytimeRunning", "LeftIndicator", "RightIndicator", "Reverse",
	"Siren", "DippedHeadlights", "FogLights" }

local OPTIONS = {
	dark = true,            -- repeat the phases under a dark mood
	distance = 10,          -- metres ahead to the rear-facing copy (the facing one is 8 m further)
	copy = true,            -- spawn the facing copy
	syncTimeout = 40,       -- updates without a sync change before advancing alone (about 20 s)
	brightness = nil,       -- e.g. 3: setLightTypeBrightness for every type
	phases = true,          -- false: only the day and dark drives, no setLights phases
}

local function log(message)
	print("[dsfforza.lights] " .. message)
end

function M.configure(config)
	if type(config) == "table" then
		for key, value in pairs(config) do OPTIONS[key] = value end
	end
end

-- The phase list run in order: daylight, then dark.
local function schedule()
	local list = {}
	if OPTIONS.phases == false then return list end -- drives only (run-test -LightsDriveOnly)
	for _, p in ipairs(PHASES) do table.insert(list, { name = "day " .. p.name, set = p.set, dark = false }) end
	if OPTIONS.dark then
		for _, p in ipairs(PHASES) do table.insert(list, { name = "dark " .. p.name, set = p.set, dark = true }) end
	end
	return list
end

local function setLights(car, set)
	for _, name in ipairs(ORDER) do
		if set[name] ~= nil then
			local ok, err = pcall(GameVehicleResource.setLights, { gameVehicle = car, LightType = name, State = set[name] })
			if not ok then log("setLights(" .. name .. ") failed: " .. tostring(err)) end
		end
	end
end

-- DSF's free roam has no night; a user mood with ClearDay's keys and the
-- light, sky and fog turned down stands in for it. moods.lua is loaded into a
-- module, so the values are written out here instead of copied.
local function darkLighting()
	local v = vec.vector
	return {
		ambientColourLowerBound = v(0.01, 0.012, 0.02, 1), ambientColourUpperBound = v(0.03, 0.035, 0.05, 1),
		directionalLightColour = v(0.05, 0.055, 0.08, 1), directionalLightDirection = v(-1.25, -3.8, 3, 1),
		emissiveColMod = v(1.5, 1.5, 1.5, 1),
		specularLightDirection = v(-1.5, -0.9, 4, 1), specularLightColour = v(0.2, 0.2, 0.3, 1),
		secondspeculargloss = 1, secondspecularmultiplier = 1, speculargloss = 1,
		difftextureintensity = 1, difftextureoffset = 0, specularmaskcontrast = 1, ambientlowerlimit = 0,
		envmapmultiplier = 0.1, middlegrey = 0.5, maxluminance = 5, tonemappingalgorithm = 2, lumadaptionrate = 0.05,
		cutsceneEffectBlend = 0, cutsceneEffectGreyscale = v(0.2176376, 0.2176376, 0.2176376, 1),
		PostProcessParameters_HDRTonemapOffset = 2, PostProcessParameters_HDRTonemapClampMax = 0.3,
		PostProcessParameters_HDRTonemapClampMin = -1,
		SkyParameters_vBlendingTargetColour = v(0.005, 0.006, 0.012, 1), SkyParameters_fBlendingLevel = 1,
		SkyParameters_fDbgExposureControl = 0.3,
		fogNearDistance = 0, fogFarDistance = 600, fogFarValue = 40, fogPower = 1, fogColour = v(0.005, 0.006, 0.012, 1),
		waterDiffuseTilingMult = 0.01, waterTilingMult1 = 0.1, waterTilingMult2 = 0.006,
		waterWaveSpeedUv1 = v(-0.1, 0.02, 0, 0), waterWaveSpeedUv2 = v(-0.008, 0.008, 0, 0),
		waterYAxisBias = 0.1, waterReflectionAmount = 0.2, waterDynReflectionAmount = 0.2,
		bloomThreshold = 0.15, bloomTintColour = v(0.3, 0.3, 0.3, 1), idColourGrading = 1, saturationDesatScale = 0.4,
		ppeFilmGrain = 0, ppeFilmGrainThreshold = 0.3, ppeEdgeVignette = 0.6, ppeEdgeVignetteBorder = 40,
		vignetteInnerRadius = 1, vignetteOutterRadius = 1, vignetteColour = v(0, 0, 0, 1),
		vignetteCentreX = 0.5, vignetteCentreY = 0.5,
		godRaysLightSourcePos = v(65000, 10000, -140000, 1), godRaysColour = v(0, 0, 0, 1), godRaysIntensity = 0,
		godRaysStretchReduction = 0.9, godRaysBlurWidth = 0.06,
		ProgressionLockedZoneCol = v(0.007751024, 0.007751024, 0.007751024, 1),
		vehSpecularLightDirection = v(-0.75, -1.5, 1, 1), vehSpecularLightColour = v(2, 2, 2.5, 1),
		vehColourBoost = v(1, 1, 1, 1), vehSpeculargloss = 1, vehSecondspeculargloss = 1,
		vehSecondspecularmultiplier = 1, vehEnvmapmultiplier = 0.3,
	}
end

local darkOn = false
local function setDark(on)
	if on == darkOn then return end
	darkOn = on
	if type(Mood) ~= "table" then
		log("no Mood table; can't " .. (on and "darken" or "restore"))
		return
	end
	if on then
		local ok, mood = pcall(Mood.new, darkLighting())
		if not ok or mood == nil then log("Mood.new failed: " .. tostring(mood)) return end
		-- OverrideZap: the zap-level moods are not blended over it.
		local added, err = pcall(Mood.addMoodUserDefinedOverrideZap, mood, "dsfforzaDark", 1, 0.5, -1, 0)
		if not added then
			added, err = pcall(Mood.addMoodUserDefined, mood, "dsfforzaDark", 1, 0.5, -1)
		end
		log("dark mood " .. (added and "added" or ("failed: " .. tostring(err))))
	else
		local ok, err = pcall(Mood.removeMood, "dsfforzaDark", 0.5)
		log("dark mood removed " .. (ok and "ok" or tostring(err)))
	end
end

local function brightnessReport()
	-- The renderer scales each lamp by a per-type float indexed by the lamp's table type
	-- (flags b3; Driver.exe 0x8A78E0 reads 0xF3FB68[type]); getLightTypeBrightness(n) with a
	-- number seems to read that table, so list 0..31.
	local parts = {}
	for n = 0, 31 do
		local ok, v = pcall(GameVehicleResource.getLightTypeBrightness, n)
		table.insert(parts, string.format("%d=%s", n, ok and tostring(v) or "err"))
	end
	log("brightness by number: " .. table.concat(parts, " "))
	for _, name in ipairs(TYPES) do
		local okA, a = pcall(GameVehicleResource.getLightTypeBrightness, name)
		local okB, b = pcall(GameVehicleResource.getLightTypeBrightness, { LightType = name })
		local okC, c = pcall(GameVehicleResource.getLightTypeBrightness, MASKS[name])
		log(string.format("brightness %s: (name) %s %s, ({LightType}) %s %s, (mask %d) %s %s", name, tostring(okA),
			tostring(a), tostring(okB), tostring(b), MASKS[name], tostring(okC), tostring(c)))
		if OPTIONS.brightness then
			pcall(GameVehicleResource.setLightTypeBrightness, name, OPTIONS.brightness)
			pcall(GameVehicleResource.setLightTypeBrightness, { LightType = name, Brightness = OPTIONS.brightness })
		end
	end
end

-- The harness writes dsfforza\lightsync.lua ("return { phase = k }"). Read it
-- with io.open (DriverNGHook's own user_fileExists does), else, when its
-- modify time changes, with user_open (which only returns tables and logs
-- every load).
local SYNC = "dsfforza\\lightsync.lua"
local syncMethod, syncStamp, syncCached
local function syncValue()
	local base = rawget(_G, "UserLuaMediaPath") or ""
	if syncMethod ~= "user_open" and type(io) == "table" and type(io.open) == "function" then
		local ok, value = pcall(function()
			local f = io.open(base .. SYNC, "r")
			if f == nil then return nil end
			local text = f:read("*a")
			f:close()
			return tonumber(string.match(text or "", "-?%d+"))
		end)
		if ok and value ~= nil then
			if syncMethod == nil then syncMethod = "io" log("sync via io.open") end
			return value
		end
	end
	local stamp = nil
	if type(file) == "table" and type(file.getmodifytime) == "function" then
		local ok, t = pcall(file.getmodifytime, base .. SYNC)
		stamp = ok and t or nil
	end
	if stamp ~= nil and stamp == syncStamp then return syncCached end
	local ok, found, value = pcall(user_open, SYNC)
	if ok and found and type(value) == "table" then
		if syncMethod == nil then syncMethod = "user_open" log("sync via user_open") end
		syncStamp, syncCached = stamp, tonumber(value.phase)
		return syncCached
	end
	return nil
end

local state = {}

-- The test stretch: dyno.lua's start on marin_4l_centre_01 (straight, level, four lanes).
-- Every car is placed here so the chase camera frames each car the same way, with
-- traffic off. forward = (sin h, 0, cos h); right = (cos h, 0, -sin h).
local SPOT = { x = 1671.4, y = 30.0, z = -4452.0, heading = -1.0257 }
local LANE = 3.4               -- lateral offset of the two copies (adjacent lanes)
local PLACE_TICKS = 6          -- updates holding the car on the spot after a (re)place

local function spotMatrix(along, lateral, flip)
	local h = SPOT.heading
	local fx, fz = math.sin(h), math.cos(h)
	local rx, rz = math.cos(h), -math.sin(h)
	local m = setYRotation(h)
	if flip then
		-- Half a turn about y: negate the right (m[0]) and forward (m[2]) rows
		-- (setYRotation(h + pi) left the copy facing away, run 20261023-113752).
		m[0] = vec.vector(-m[0].x, -m[0].y, -m[0].z, 0)
		m[2] = vec.vector(-m[2].x, -m[2].y, -m[2].z, 0)
	end
	m[3] = vec.vector(SPOT.x + fx * along + rx * lateral, SPOT.y + 0.6, SPOT.z + fz * along + rz * lateral, 1)
	return m
end

-- Traffic stays on (the traffic phase needs cars on the road: with traffic off
-- the probe counted 0 cars and no car to swap, run 20261023-121707); the spot is
-- cleared of vehicles when the car is first placed there instead.
local function clearSpot()
	pcall(function() ParkedVehicles.SetEnabled(false) end)
	pcall(GameVehicleResource.ClearAreaOfVehicles, vec.vector(SPOT.x, SPOT.y, SPOT.z, 1), 80)
end

local function spawnCopy(id, along, lateral, flip)
	local ok, copy = pcall(function()
		local m = spotMatrix(along, lateral, flip)
		local h = SPOT.heading + (flip and math.pi or 0)
		return GameVehicleResource.create({
			SnapToTerrain = true, Model_Id = id, NoOccupants = true, InitialVelocity = 0,
			Position = m[3], Heading = vec.vector(math.sin(h), 0, math.cos(h), 0),
		})
	end)
	if ok and copy ~= nil then
		pcall(function() copy.matrix = spotMatrix(along, lateral, flip) end)
		return copy
	end
	log(string.format("id %d: copy at %.0f/%.1f failed: %s", id, along, lateral, tostring(copy)))
	return nil
end

-- Teleports need the matrix written every frame until the area is resident (dyno.lua's
-- way); one write per clonetest tick left the car where it was (run 20261023-110543).
local PLACE_FRAMES = 90       -- frames at least, then until resident (at most PLACE_MAX)
local PLACE_MAX = 900
local function placeAll()
	state.placing, state.placeFrames = PLACE_MAX, 0
end

local function placeFrame()
	if state.car == nil or (state.placing or 0) <= 0 then return end
	state.placing = state.placing - 1
	state.placeFrames = (state.placeFrames or 0) + 1
	local ok, err = pcall(function()
		state.car.matrix = spotMatrix(0, 0, false)
		state.car.velocity = vec.vector(0, 0, 0, 0)
		if state.rear then state.rear.matrix = spotMatrix(OPTIONS.distance, -LANE, false) end
		if state.front then state.front.matrix = spotMatrix(OPTIONS.distance + 8, LANE, true) end
	end)
	if not ok and not state.placeError then
		state.placeError = true
		log("place error: " .. tostring(err))
	end
	if state.placeFrames >= PLACE_FRAMES then
		local resident = false
		pcall(function() resident = spoolsystem.IsLocationResident(vec.vector(SPOT.x, SPOT.y, SPOT.z, 1)) end)
		if resident or state.placing <= 0 then
			state.placing = 0
			local okP, p = pcall(function() return state.car.position end)
			log(string.format("id %d: placed after %d frames (resident %s) at %s", state.id, state.placeFrames,
				tostring(resident), okP and string.format("(%.1f, %.1f, %.1f)", p.x, p.y, p.z) or "?"))
			if not state.ready then
				-- First placement done: clear the road, the copies, then tell the harness.
				state.ready = true
				clearSpot()
				if OPTIONS.copy then
					-- Two script copies with no driver, so only setLights drives their lamps:
					-- one ahead in the left lane showing its rear, one further ahead in the
					-- right lane facing the camera.
					state.rear = spawnCopy(state.id, OPTIONS.distance, -LANE, false)
					state.front = spawnCopy(state.id, OPTIONS.distance + 8, LANE, true)
					log(string.format("id %d: copies rear %s front %s", state.id, tostring(state.rear), tostring(state.front)))
				end
				brightnessReport()
				log(string.format("id %d: ready %d phases", state.id, #state.list))
			end
		end
	end
end
addUserUpdateFunction("dsfforza_lights_place", function() pcall(placeFrame) end, 1)

-- Called by clonetest with the player's game vehicle once the player drives it
-- (previous: the car driven before, parked out of the way).
function M.start(id, car, previous)
	state = { id = id, car = car, list = schedule(), index = 0, idle = 0, lastSync = syncValue() or 0, ticks = 0 }
	if previous ~= nil and previous ~= car then
		pcall(function()
			previous.matrix = spotMatrix(-40, 2 * LANE, false)
			previous.velocity = vec.vector(0, 0, 0, 0)
		end)
	end
	placeAll()
end

local function apply(k)
	-- 90 / 91: back to the spot, daylight / dark, no setLights (the game's own lamp
	-- logic; the harness then drives: run-test.ps1 Invoke-LightKeys).
	if k == 90 or k == 91 then
		setDark(k == 91)
		placeAll()
		log(string.format("id %d: drive %s", state.id, k == 91 and "dark" or "day"))
		return
	end
	-- Not sent by the harness any more: swapWithModel on a live traffic car crashed the
	-- game (run host-20261023-123840).
	-- 92: the car as traffic: spawned 25 m ahead in the player's direction and handed to
	-- the traffic carriers (civilianTraffic.AddVehicleToCarrier, as Exposition's "Taxi
	-- wander" actors), traffic back on; the traffic AI drives it and sets its lamps
	-- (Driver.exe 0x779960 -> 0x8D0470(3, Brakes...)). 93: lockEmergencyBrakes on it
	-- (full braking), 94: release. Daylight.
	if k == 92 then
		setDark(false)
		setLights(state.car, { All = false })
		for _, copy in ipairs({ state.rear, state.front }) do
			pcall(function() copy:delete() end)
			pcall(GameVehicleResource.destroy, copy)
		end
		state.rear, state.front = nil, nil
		-- AddVehicleToCarrier on a Spawn.Spawn car returned false and the car stayed
		-- parked (run 20261023-115652). Instead turn the nearest civilian traffic car
		-- ahead into this model (swapWithModel keeps it in traffic, register.lua's
		-- swapTraffic), so the traffic AI drives it and sets its lamps.
		local best, bestD = nil, 1000
		pcall(function()
			local p, f = state.car.position, state.car.matrix[2]
			for _, gv in pairs(vehicleManager.findVehiclesInTraffic(p, state.car.heading, 120, {}, 150) or {}) do
				local q = gv.position
				local dx, dz = q.x - p.x, q.z - p.z
				local along = dx * f.x + dz * f.z
				local d = math.sqrt(dx * dx + dz * dz)
				local okT, isT = pcall(GameVehicleResource.isTrafficVehicle, gv)
				local okE, em = pcall(GameVehicleResource.getEmergencyType, gv)
				if gv ~= state.car and okT and isT and okE and em == 0 and along > 8 and d < bestD then
					best, bestD = gv, d
				end
			end
		end)
		local swapped = "no traffic car ahead"
		if best then
			local okS, err = pcall(GameVehicleResource.swapWithModel, best, state.id)
			state.ai = (okS and type(err) == "userdata") and err or best
			swapped = string.format("swapped a traffic car %.0f m ahead: %s %s, model now %s, traffic %s", bestD,
				tostring(okS), tostring(err), tostring(select(2, pcall(function() return best.model_id end))),
				tostring(select(2, pcall(GameVehicleResource.isTrafficVehicle, best))))
		end
		log(string.format("id %d: traffic copy: %s", state.id, swapped))
		return
	end
	if k == 93 or k == 94 then
		local fn = k == 93 and GameVehicleResource.lockEmergencyBrakes or GameVehicleResource.unlockEmergencyBrakes
		local ok, err = pcall(fn, state.ai)
		local okS, speed = pcall(function() return state.ai.speed end)
		log(string.format("id %d: traffic copy %s: %s %s (speed %s)", state.id,
			k == 93 and "lockEmergencyBrakes" or "unlockEmergencyBrakes", tostring(ok), tostring(err), tostring(speed)))
		return
	end
	local phase = state.list[k]
	if phase == nil then return end
	if state.index < 1 or k == 1 then placeAll() end
	state.index = k
	setDark(phase.dark)
	setLights(state.car, phase.set)
	if state.rear then setLights(state.rear, phase.set) end
	if state.front then setLights(state.front, phase.set) end
	log(string.format("id %d: phase %d %s", state.id, k, phase.name))
	if k == 1 then
		-- Where the copies are (they never showed in the shots of run 20261023-112147).
		for name, v in pairs({ player = state.car, rear = state.rear, front = state.front }) do
			local ok, text = pcall(function()
				local q = v.position
				return string.format("(%.1f, %.1f, %.1f)", q.x, q.y, q.z)
			end)
			log(string.format("id %d: %s at %s", state.id, name, ok and text or ("? " .. tostring(text))))
		end
	end
end

local function finish()
	setLights(state.car, { All = false })
	setDark(false)
	for _, copy in ipairs({ state.rear, state.front }) do
		pcall(function() copy:delete() end)
		pcall(GameVehicleResource.destroy, copy)
	end
	-- The swapped traffic car stays with traffic (destroying it under the vehicle
	-- manager's feet is riskier); just let go of its brakes.
	if state.ai then pcall(GameVehicleResource.unlockEmergencyBrakes, state.ai) end
	log(string.format("id %d: done", state.id))
end

local function driveLine()
	local ok, line = pcall(function()
		local p = state.car.position
		local moved = state.lastPos and math.sqrt((p.x - state.lastPos.x) ^ 2 + (p.z - state.lastPos.z) ^ 2) or 0
		state.lastPos = { x = p.x, z = p.z }
		return string.format("moved %.2f m reversing %s", moved, tostring(GameVehicleResource.isReversing(state.car)))
	end)
	return ok and line or ("? " .. tostring(line))
end

-- Returns true when the test of this car is over.
function M.step()
	state.ticks = state.ticks + 1
	local sync = syncValue()
	if sync ~= nil and sync ~= state.lastSync then
		state.lastSync, state.idle = sync, 0
		if sync < 0 then finish() return true end
		if sync > 0 then apply(sync) end
		return false
	end
	state.idle = state.idle + 1
	if state.index == 0 then
		log(string.format("id %d: drive t%d %s", state.id, state.ticks, driveLine()))
	end
	-- The harness reads Driver.log every ShotSeconds and then drives for ~25 s before
	-- phase 1, so wait longer before the first phase.
	local timeout = state.index == 0 and OPTIONS.syncTimeout * 3 or OPTIONS.syncTimeout
	if state.idle >= timeout then
		state.idle = 0
		if state.index >= #state.list then finish() return true end
		apply(state.index + 1)
	end
	return false
end

return M
