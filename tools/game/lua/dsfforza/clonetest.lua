-- dsfforza clone test: spools a vehicle ID, spawns it and puts the player in it.
-- Loaded by bootstrap.lua at launchScripts(). Output goes to Driver.log via
-- DriverNGHook's print.
--
-- Per ID, once per session:
--   1. TrafficSpooler.RequestMissionVehicle(id), poll IsMissionVehicleLoaded(id)
--   2. driving: Spawn.Spawn the vehicle in front of the player
--      in Shift view (inZap, no car): GameVehicleResource.create it under the
--      Shift camera with SnapToTerrain
--   3. zap the player into it and report what currentVehicle becomes

--
-- Then, for every tested ID:
--   4. log VehicleLodSpooler and spool-system state for N, N+1000, N+2000,
--      before and after VehicleLodSpooler.RequestVehicle(N)
--   5. if config.traffic is set, TrafficSpooler.RequestTrafficVehicles(those
--      IDs) and count model_id among nearby traffic for a while

local TEST_IDS = { 304 }      -- the clone; overridden by dsfforza\config.lua (run-test -ProbeIds)
local TRAFFIC_IDS = {}        -- overridden by config.traffic (run-test -TrafficIds)
local AHEAD_IDS = {}          -- spawned in front without zapping, to see the full (traffic) model
local CONFIG_IDS = false      -- true when config.lua set TEST_IDS
local DRIVE_SECONDS = 0       -- config.drive (run-test -DriveSeconds): the harness holds the throttle
                              -- this long in cockpit view; the view lasts longer and speed is logged
local HOLD_MPH = {}           -- config.hold_mph: speeds forced in turn during the cockpit view
local DAMAGE = false          -- config.damage (run-test -Damage): full visual damage in chase view
local DAMAGE_PANELS = false   -- config.damage_panels (run-test -DamagePanels): then open and remove the panels
local DAMAGE_SIDE = false     -- config.damage_side (run-test -DamageSide): localised hits on the -x front corner and door
local GLASS_LADDER = false    -- config.glass_ladder (run-test -GlassLadder): damage in steps during the cockpit
                              -- view, so the cockpit glass shows each crack state (research/cockpit.md)
local VIEWS = {}              -- config.views (run-test -Views): extra views between the chase and cockpit views:
                              -- camera modes ("Bonnet", "Bumper") or fixed cameras on the car ("left", "right",
                              -- "front": the driver's seat seen through the side windows or the windscreen)
local DYNO = nil              -- config.dyno (run-test -Dyno): the dyno.lua module, acceleration and braking runs
local LIGHTS = nil            -- config.lights (run-test -Lights): the lightstest.lua module, lamp phases per car

do
	local found, config = user_open("dsfforza\\config.lua")
	if found and type(config) == "table" then
		if type(config.ids) == "table" then TEST_IDS, CONFIG_IDS = config.ids, true end
		if type(config.traffic) == "table" then TRAFFIC_IDS = config.traffic end
		if type(config.ahead) == "table" then AHEAD_IDS = config.ahead end
		if type(config.drive) == "number" then DRIVE_SECONDS = config.drive end
		if type(config.hold_mph) == "table" then HOLD_MPH = config.hold_mph end
		if config.damage then DAMAGE = true end
		if config.damage_panels then DAMAGE_PANELS = true end
		if config.damage_side then DAMAGE_SIDE = true end
		if config.glass_ladder then GLASS_LADDER = true end
		if type(config.views) == "table" then VIEWS = config.views end
		if config.dyno then
			local loaded, module = user_open("dsfforza\\dyno.lua")
			if loaded and type(module) == "table" then
				DYNO = module
				DYNO.configure(config.dyno)
			else
				print("[dsfforza] dyno.lua did not load: " .. tostring(module))
			end
		end
		if config.lights then
			local loaded, module = user_open("dsfforza\\lightstest.lua")
			if loaded and type(module) == "table" then
				LIGHTS = module
				LIGHTS.configure(config.lights)
			else
				print("[dsfforza] lightstest.lua did not load: " .. tostring(module))
			end
		end
	end
end
local TRAFFIC_TICKS = 160     -- about 80 s of traffic sampling
local SPOOL_PACKAGES = { "dngvehicles", "dngvehicles.sp", "Install\\dngvehicles.sp", "vehicles" }
local SPAWN_DISTANCE = 25
local TICK = 30               -- frames between update calls
local SPOOL_TIMEOUT = 40      -- ticks, about 20 s at 60 fps
local VERIFY_TIMEOUT = 40
local VIEW_TICKS = 26          -- about 13 s per camera view, so a 10 s screenshot catches each
local HOLD_TICKS = 10         -- ticks per forced speed (config.hold_mph)
local MPH = 2.23704           -- m/s -> mph, as the speedometer converts (FUN_00891670)
local LEVEL_TIMEOUT = 120     -- ticks to wait for street-level Shift before trying anyway

local function log(message)
	print("[dsfforza] " .. message)
end

-- The HUD, garage and stats screens look vehicles up in the game's Lua table
-- vehicleStats[modelId] (manufacturer badge, name string IDs, pictures, stat
-- bars). A clone has no entry, so the HUD shows other cars' names and badges.
-- Give each clone a copy of its source's entry.
local STATS_SOURCES = { [304] = 266 }

-- Cars built by tools/port_car.py: dsfforza\ported.lua (written with
-- --install-lua) lists each ported ID with its host. Its IDs become the test
-- IDs unless config.lua sets them.
do
	local found, ported = user_open("dsfforza\\ported.lua")
	if found and type(ported) == "table" and type(ported.cars) == "table" then
		for id, car in pairs(ported.cars) do
			STATS_SOURCES[id] = car.host
		end
		if not CONFIG_IDS and type(ported.ids) == "table" then
			TEST_IDS = ported.ids
		end
		log("ported.lua: " .. #(ported.ids or {}) .. " cars from " .. tostring(ported.archive))
	end
end

local function registerCloneStats()
	if type(vehicleStats) ~= "table" then
		log("vehicleStats not available yet")
		return false
	end
	for clone, source in pairs(STATS_SOURCES) do
		if vehicleStats[clone] == nil and type(vehicleStats[source]) == "table" then
			local entry = {}
			for key, value in pairs(vehicleStats[source]) do
				entry[key] = value
			end
			vehicleStats[clone] = entry
			log(string.format("vehicleStats[%d] copied from [%d]", clone, source))
		end
	end
	return true
end

local state = { index = 1, phase = "request", ticks = 0 }

local function describe(value)
	if type(value) == "userdata" or type(value) == "table" then
		local ok, text = pcall(tostring, value)
		return type(value) .. ":" .. (ok and text or "?")
	end
	return type(value) .. ":" .. tostring(value)
end

-- currentVehicle is false (not nil) while the player has no car, e.g. in the
-- Shift overview after loading a save.
local function playerVehicle()
	if type(localPlayer) ~= "table" then
		return nil
	end
	local current = localPlayer.currentVehicle
	if type(current) == "table" and current.gameVehicle ~= nil then
		return current.gameVehicle
	end
	return nil
end

local function spawnInFront(id, playerGameVehicle)
	local vehicles = Spawn.Spawn({
		type = "InFrontOfVehicle",
		distanceInFront = SPAWN_DISTANCE,
		vehicle = playerGameVehicle,
		direction = "with",
		position = "randomLane",
		heading = playerGameVehicle.heading,
		desiredSpeed = 0,
		vehicles = { { modelID = id } },
	})
	return vehicles and vehicles[1]
end

-- zapcontroller.ZapCameraGetTargetPos(): "Will return the position (Vector)
-- of the Zap target" (research/lua-api.tsv). Which object exposes it isn't
-- documented, so try the likely names and log each attempt.
local function zapTargetPosition()
	local candidates = {
		{ "zapcontroller", function() return zapcontroller end },
		{ "zapController", function() return zapController end },
		{ "localPlayer.zapController", function() return localPlayer.zapController end },
		{ "localPlayer.zapcontroller", function() return localPlayer.zapcontroller end },
		{ "localPlayer.zap", function() return localPlayer.zap end },
	}
	for _, candidate in ipairs(candidates) do
		local okGet, object = pcall(candidate[2])
		if okGet and object ~= nil then
			local okCall, position = pcall(function() return object.ZapCameraGetTargetPos(object) end)
			log(string.format("%s is %s; ZapCameraGetTargetPos -> %s %s", candidate[1], describe(object),
				tostring(okCall), describe(position)))
			if okCall and position ~= nil then
				return position
			end
		end
	end
	return nil
end

local function zapLevel()
	local ok, level = pcall(function() return zapcontroller.getZapLevel(zapcontroller) end)
	return ok and tonumber(level) or nil
end

local function spawnUnderCamera(id)
	local position = zapTargetPosition() or localPlayer.position
	log("spawning under the Shift camera at " .. describe(position))
	return GameVehicleResource.create({
		SnapToTerrain = true,
		Model_Id = id,
		Position = position,
		Heading = 0,
		InitialVelocity = 0,
		DisableHandbrake = false,
		NoOccupants = true,
	})
end

local function try(fn, ...)
	local ok, value = pcall(fn, ...)
	if ok then return tostring(value) end
	return "err(" .. tostring(value) .. ")"
end

-- Traffic model IDs currently on the road near the player, excluding the
-- player's own car.
local function trafficModels()
	local current = playerVehicle()
	local models, seen = {}, {}
	if current == nil then
		return models
	end
	for _, vehicle in pairs(vehicleManager.findVehiclesInTraffic(current.position, current.heading, 250, {}, 150) or {}) do
		local ok, model = pcall(function() return vehicle.model_id end)
		model = ok and tonumber(model)
		if model and vehicle ~= current and not seen[model] then
			seen[model] = true
			table.insert(models, model)
		end
	end
	return models
end

-- The binding (0x0066E560) reads luaL_checkinteger at stack -2 and a table
-- at -1: RequestTrafficVehicles(count, {ids}). It probably replaces the
-- traffic set, so pass the models already in traffic plus the new IDs.
local function requestTraffic(ids)
	log("CanRequestVehicles -> " .. try(TrafficSpooler.CanRequestVehicles))
	local list = trafficModels()
	for _, id in ipairs(ids) do
		table.insert(list, id)
	end
	log("RequestTrafficVehicles(" .. #list .. ", {" .. table.concat(list, ",") .. "}) -> "
		.. try(TrafficSpooler.RequestTrafficVehicles, #list, list))
end

-- What is resident for a base ID: the VehicleLodSpooler view and, per spool
-- ID (kinds 0, 1, 2 = N, N+1000, N+2000), the mission-vehicle and
-- spool-system views. Package names are guesses; "err" means the call failed.
local function lodReport(label)
	for _, id in ipairs(TEST_IDS) do
		log(string.format("%s: id %d lod vehicle=%s interior=%s exterior=%s", label, id,
			try(VehicleLodSpooler.IsVehicleLoaded, id), try(VehicleLodSpooler.IsInteriorLoaded, id),
			try(VehicleLodSpooler.IsExteriorLoaded, id)))
		for kind = 0, 2 do
			local spoolId = id + kind * 1000
			local parts = {}
			for _, package in ipairs(SPOOL_PACKAGES) do
				table.insert(parts, package .. "=" .. try(spoolsystem.IsEntityLoaded, package, spoolId))
			end
			log(string.format("%s:   spool id %d mission=%s %s", label, spoolId,
				try(TrafficSpooler.IsMissionVehicleLoaded, spoolId), table.concat(parts, " ")))
		end
	end
end

local function trafficSample()
	local current = playerVehicle()
	if current == nil then
		return "no player vehicle"
	end
	local vehicles = vehicleManager.findVehiclesInTraffic(current.position, current.heading, 250, {}, 150) or {}
	local counts, total = {}, 0
	for _, vehicle in pairs(vehicles) do
		local ok, model = pcall(function() return vehicle.model_id end)
		model = ok and tonumber(model) or -1
		counts[model] = (counts[model] or 0) + 1
		total = total + 1
	end
	local parts = {}
	for model, count in pairs(counts) do
		table.insert(parts, model .. "x" .. count)
	end
	table.sort(parts)
	return string.format("%d cars: %s", total, table.concat(parts, " "))
end

-- gameVehicle.speed is m/s (game scripts set 17.88 for 40 mph).
local function vehicleSpeed()
	local current = playerVehicle()
	if current == nil then return nil end
	local ok, speed = pcall(function() return current.speed end)
	return ok and tonumber(speed) or nil
end

-- Forced speed for config.hold_mph, applied every frame by its own update function.
local holdTarget = nil
local function holdSpeed()
	if holdTarget == nil then return end
	local current = playerVehicle()
	if current ~= nil then
		pcall(function() current.speed = holdTarget / MPH end)
	end
end

-- config.glass_ladder: cockpit-view ticks -> damage step. Clean first, then
-- GameVehicleResource.applyDamage on the front (health damage; whether it
-- raises the glass crack state is what the ladder finds out), then
-- fullVisualDamage. Each step is logged with its tick so screenshots match.
local GLASS_STEPS = {
	[8] = { "applyDamage", 0.25 },
	[16] = { "applyDamage", 0.5 },
	[24] = { "applyDamage", 1.0 },
	[32] = { "fullVisualDamage" },
}
local GLASS_LADDER_TICKS = 42

local function glassStep(id, car, step)
	local ok, err
	if step[1] == "fullVisualDamage" then
		ok, err = pcall(GameVehicleResource.fullVisualDamage, car)
		if not ok then ok, err = pcall(GameVehicleResource.fullVisualDamage, { gameVehicle = car }) end
	else
		ok, err = pcall(GameVehicleResource.applyDamage, { gameVehicle = car, damage = step[2],
			location = vec.vector(0, 1.0, 2.0, 1), overridePlayerOnlyDamage = true })
	end
	log(string.format("id %d: glass ladder t%d %s %s: %s", id, state.ticks, step[1], tostring(step[2] or ""),
		ok and "ok" or tostring(err)))
end

local function cockpitTicks()
	local ticks = VIEW_TICKS
	if GLASS_LADDER then ticks = math.max(ticks, GLASS_LADDER_TICKS) end
	if DRIVE_SECONDS > 0 then ticks = math.max(ticks, DRIVE_SECONDS * 2 + 10) end
	if #HOLD_MPH > 0 then ticks = math.max(ticks, #HOLD_MPH * HOLD_TICKS + 4) end
	return ticks
end

local function nextId()
	state.index, state.phase, state.ticks = state.index + 1, "request", 0
end

-- config.damage: crush each car fully a few ticks into its chase view (research/damage.md).
local DAMAGE_TICK = 6
local function damageCar(id, car)
	local ok, err = pcall(GameVehicleResource.fullVisualDamage, car)
	if not ok then
		ok, err = pcall(GameVehicleResource.fullVisualDamage, { gameVehicle = car })
	end
	log(string.format("id %d: fullVisualDamage %s", id, ok and "ok" or tostring(err)))
end

-- config.damage_panels: after the crush, swing every panel open, then knock them all off, so the
-- 3 s screenshots show the crushed car, the open bonnet/boot and the bare engine bay (research/damage.md).
local PANEL_STAGES = { [12] = "forceOpenPanels", [18] = "removeAllPanels" }
local function panelStage(id, car, name)
	local ok, err = pcall(GameVehicleResource[name], { gameVehicle = car })
	if not ok then
		ok, err = pcall(GameVehicleResource[name], car)
	end
	log(string.format("id %d: %s %s", id, name, ok and "ok" or tostring(err)))
end

-- config.damage_side: GameVehicleResource.applyDamage at points on the car's -x side (its right; the
-- front wing, then the door), so panels get different damage amounts as in a real side impact
-- (fullVisualDamage crushes every panel alike; research/damage.md, seam gaps). The chase camera
-- only sees the car's back, so a parked copy of the model is placed SIDE_AHEAD m ahead and
-- SIDE_LATERAL m to the side (it lands on the left), turned a quarter turn so one side faces the
-- camera, and the hits go to the copy's camera-facing side (SIDE_COPY). Hitting the player's car
-- as well sent the game to the Shift view for most of the chase shots.
local SIDE_STAGES = {
	[4] = { 1.0, -1.0, 0.4, 1.3 },
	[8] = { 1.0, -1.0, 0.4, 0.3 },
	[12] = { 1.0, -1.0, 0.4, 1.0 },
}
local SIDE_AHEAD, SIDE_LATERAL = 9.0, 3.0
-- Hits on the copy, in car space on its camera-facing (+x) side: front wing, door, rear quarter.
-- applyDamage's location is car space: world points beside the copy (run host-20261024-002829)
-- barely marked it, car-space points crushed the near front corner (host-20261024-002016).
local SIDE_COPY = { [4] = { 1.0, 1.0, 0.4, 1.3 }, [8] = { 1.0, 1.0, 0.4, 0.2 }, [12] = { 1.0, 1.0, 0.4, -0.8 } }
local sideCopy = nil
local function sideHit(id, car, hit, what)
	local ok, err = pcall(GameVehicleResource.applyDamage, { gameVehicle = car, damage = hit[1],
		location = vec.vector(hit[2], hit[3], hit[4], 1), overridePlayerOnlyDamage = true })
	log(string.format("id %d: applyDamage %.2f at (%.1f, %.1f, %.1f) on the %s t%d %s", id, hit[1], hit[2], hit[3],
		hit[4], what, state.ticks, ok and "ok" or tostring(err)))
end

local function spawnSideCopy(id, car)
	local ok, copy = pcall(function()
		local m0 = car.matrix
		local h = math.atan2(m0[2].x, m0[2].z)
		local fx, fz = math.sin(h), math.cos(h)
		local rx, rz = math.cos(h), -math.sin(h)
		local p = m0[3]
		local hc = h + math.pi / 2  -- the copy's right (-x) then points back at the camera
		local pos = vec.vector(p.x + fx * SIDE_AHEAD + rx * SIDE_LATERAL, p.y + 0.5,
			p.z + fz * SIDE_AHEAD + rz * SIDE_LATERAL, 1)
		local c = GameVehicleResource.create({
			SnapToTerrain = true, Model_Id = id, NoOccupants = true, InitialVelocity = 0,
			Position = pos, Heading = vec.vector(math.sin(hc), 0, math.cos(hc), 0),
		})
		pcall(function()
			local m = setYRotation(hc)
			m[3] = pos
			c.matrix = m
			c.velocity = vec.vector(0, 0, 0, 0)
		end)
		return c
	end)
	log(string.format("id %d: side copy %s", id, ok and describe(copy) or tostring(copy)))
	return ok and copy or nil
end

local function removeSideCopy()
	if sideCopy ~= nil then
		pcall(function() sideCopy:delete() end)
		pcall(GameVehicleResource.destroy, sideCopy)
		sideCopy = nil
	end
end

-- config.views: each extra view lasts this many ticks.
local EXTRA_VIEW_TICKS = 12
-- Fixed cameras in the car's frame (+x its left, +y up from the ground, +z forward): from, at.
-- They look at the driver's seat (left-hand drive at +x, right-hand drive at -x).
local FIXED_VIEWS = {
	left = { { 2.4, 1.9, 0.1 }, { 0.3, 0.95, 0.25 } },
	right = { { -2.4, 1.9, 0.1 }, { -0.3, 0.95, 0.25 } },
	front = { { 0.0, 1.6, 3.4 }, { 0.0, 0.95, 0.1 } },
}
local fixedCamera = nil

-- A camera matrix at p looking at t (world vectors). Cameras look down -z (debug.lua's
-- printCamLookFrom: lookAt = matrix[3] - matrix[2] * 5); rows are x, y, z, position.
-- Built on a setYRotation matrix with its rows replaced (a vec.matrix of 16 numbers put
-- the camera at the world origin, run vm-20261024-050134).
local function lookMatrix(p, t)
	local dx, dy, dz = t.x - p.x, t.y - p.y, t.z - p.z
	local n = math.sqrt(dx * dx + dy * dy + dz * dz)
	local zx, zy, zz = -dx / n, -dy / n, -dz / n
	local xx, xz = zz, -zx
	n = math.sqrt(xx * xx + xz * xz)
	xx, xz = xx / n, xz / n
	local m = setYRotation(math.atan2(-dx, -dz))
	m[0] = vec.vector(xx, 0, xz, 0)
	m[1] = vec.vector(zy * xz, zz * xx - zx * xz, -zy * xx, 0)
	m[2] = vec.vector(zx, zy, zz, 0)
	m[3] = p
	return m
end

local function carPoint(m, v)
	return m[3] + m[0] * v[1] + m[1] * v[2] + m[2] * v[3]
end

-- The game camera's position and view direction in the car's model space (rows of the
-- car matrix are its x, y, z axes), logged so camera parameters can be checked against
-- the model (research/cockpit.md, "Cameras").
local function logCameraInCar(id, car, label)
	local ok, err = pcall(function()
		local cm, m = game_camera.matrix, car.matrix
		local function loc(v, w)
			local out = {}
			for i = 0, 2 do
				local r = m[i]
				local n = r.x * r.x + r.y * r.y + r.z * r.z
				out[i + 1] = (v.x * r.x + v.y * r.y + v.z * r.z) / n
			end
			return out
		end
		local d = cm[3] - m[3]
		local p = loc(d)
		local f = loc(cm[2] * -1)
		log(string.format("id %d: %s camera in car (%.3f, %.3f, %.3f) looking (%.3f, %.3f, %.3f)", id, label,
			p[1], p[2], p[3], f[1], f[2], f[3]))
	end)
	if not ok then log(string.format("id %d: %s camera in car: %s", id, label, tostring(err))) end
end

local function endFixedCamera()
	if fixedCamera ~= nil then
		pcall(function()
			CameraSystem.EnableLuaDebugCameras(false)
			game_camera.viewport = 0
			fixedCamera:delete()
		end)
		fixedCamera = nil
	end
end

-- Switch to extra view `name`: a camera mode, or a fixed camera made the way
-- cameraselection.lua's createFreeCam makes the debug free camera.
local function showView(id, car, name)
	endFixedCamera()
	local ok, err
	if FIXED_VIEWS[name] then
		ok, err = pcall(function()
			localPlayer.cameraMode = "Normal"
			localPlayer:resetCameraMode()
			local m = car.matrix
			local view = FIXED_VIEWS[name]
			local from, at = carPoint(m, view[1]), carPoint(m, view[2])
			fixedCamera = CameraSystem.CreateCamera()
			fixedCamera.matrix = lookMatrix(from, at)
			log(string.format("id %d: camera %s at (%.2f, %.2f, %.2f) towards (%.2f, %.2f, %.2f)", id, name,
				from.x, from.y, from.z, at.x, at.y, at.z))
			game_camera.viewport = nil
			fixedCamera.viewport = 0
			CameraSystem.EnableLuaDebugCameras(true)
		end)
	else
		ok, err = pcall(function()
			localPlayer.cameraMode = name
			localPlayer:resetCameraMode()
		end)
	end
	log(string.format("id %d: view %s %s (cameraMode %s)", id, name, ok and "on" or ("failed: " .. tostring(err)),
		tostring(localPlayer.cameraMode)))
end

local function step()
	local id = TEST_IDS[state.index]
	state.ticks = state.ticks + 1
	if DAMAGE_SIDE and id ~= nil and state.phase == "chase_view" then
		-- Hold the player's car still (a car spawned in front of a moving one keeps rolling and
		-- the chase camera leaves the copy behind, run host-20261024-003745).
		pcall(function() state.spawned.velocity = vec.vector(0, 0, 0, 0) end)
		if state.ticks == 2 then
			removeSideCopy()
			sideCopy = spawnSideCopy(id, state.spawned)
		end
		if SIDE_COPY[state.ticks] then
			if sideCopy ~= nil then
				sideHit(id, sideCopy, SIDE_COPY[state.ticks], "copy")
				local ok, p = pcall(function() return sideCopy.matrix[3] end)
				local okc, q = pcall(function() return state.spawned.matrix[3] end)
				if ok and okc then
					log(string.format("id %d: copy at (%.1f, %.1f, %.1f), car at (%.1f, %.1f, %.1f)", id, p.x, p.y, p.z,
						q.x, q.y, q.z))
				end
			else
				-- No copy: hit the player's car (the chase camera then sees only its back).
				sideHit(id, state.spawned, SIDE_STAGES[state.ticks], "car")
			end
		end
	end
	if #VIEWS > 0 and id ~= nil and state.spawned ~= nil then
		if state.phase == "chase_view" and state.ticks == VIEW_TICKS - 1 then
			logCameraInCar(id, state.spawned, "Normal")
		elseif state.phase == "extra_view" and state.ticks == EXTRA_VIEW_TICKS - 1 and not FIXED_VIEWS[VIEWS[state.view]] then
			logCameraInCar(id, state.spawned, VIEWS[state.view])
		elseif state.phase == "cockpit_view" and state.ticks == 6 then
			logCameraInCar(id, state.spawned, "DriverEye")
		end
	end
	if DAMAGE and id ~= nil and state.phase == "chase_view" and state.ticks == DAMAGE_TICK then
		damageCar(id, state.spawned)
	end
	if DAMAGE_PANELS and id ~= nil and state.phase == "chase_view" and PANEL_STAGES[state.ticks] then
		panelStage(id, state.spawned, PANEL_STAGES[state.ticks])
	end
	if id == nil then
		-- All IDs spawned: LOD report, then the optional traffic test.
		if state.phase == "request" then
			for _, ahead in ipairs(AHEAD_IDS) do
				local current = playerVehicle()
				if current ~= nil then
					local ok, spawned = pcall(spawnInFront, ahead, current)
					log(string.format("ahead id %d: spawned %s", ahead, ok and describe(spawned) or tostring(spawned)))
				end
			end
			lodReport("after spawns")
			for _, base in ipairs(TEST_IDS) do
				pcall(VehicleLodSpooler.RequestVehicle, base)
			end
			state.phase, state.ticks = "lod_wait", 0
		elseif state.phase == "lod_wait" and state.ticks >= 20 then
			lodReport("after RequestVehicle")
			if #TRAFFIC_IDS > 0 then
				log("traffic before request: " .. trafficSample())
				requestTraffic(TRAFFIC_IDS)
				state.phase, state.ticks = "traffic", 0
			else
				state.phase = "done"
			end
		elseif state.phase == "traffic" then
			if state.ticks % 20 == 0 then
				log(string.format("traffic t%d (all loaded %s): %s", state.ticks,
					try(TrafficSpooler.AllTrafficVehiclesLoaded), trafficSample()))
			end
			if state.ticks >= TRAFFIC_TICKS then
				state.phase = "done"
			end
		elseif state.phase == "done" then
			log("done")
			removeUserUpdateFunction("dsfforza_clonetest")
		end
		return
	end

	if state.phase == "request" then
		if type(localPlayer) ~= "table" then
			return
		end
		if not state.statsRegistered then
			state.statsRegistered = registerCloneStats()
		end
		log(string.format("id %d: requesting (loaded before: %s, inZap: %s, vehicle: %s)", id,
			tostring(TrafficSpooler.IsMissionVehicleLoaded(id)), tostring(localPlayer.inZap),
			describe(localPlayer.currentVehicle)))
		TrafficSpooler.RequestMissionVehicle(id)
		state.phase, state.ticks = "wait_spool", 0
	elseif state.phase == "wait_spool" then
		if TrafficSpooler.IsMissionVehicleLoaded(id) then
			log(string.format("id %d: spooled after %d ticks", id, state.ticks))
			state.phase, state.ticks = "wait_level", 0
		elseif state.ticks >= SPOOL_TIMEOUT then
			log(string.format("id %d: NOT spooled after %d ticks; skipping", id, state.ticks))
			nextId()
		end
	elseif state.phase == "wait_level" then
		-- A loaded save starts in the Shift overview, where the city under the
		-- cursor isn't simulated; wait until the camera is down at street level
		-- (the harness presses Up) or the player has a car.
		local level = zapLevel()
		if state.ticks % 4 == 1 then
			log(string.format("zap level %s, inZap %s", tostring(level), tostring(localPlayer.inZap)))
		end
		if playerVehicle() ~= nil or (level ~= nil and level <= 1) or state.ticks >= LEVEL_TIMEOUT then
			state.phase, state.ticks = "spawn", 0
		end
	elseif state.phase == "spawn" then
		local current = playerVehicle()
		local spawned
		if current ~= nil then
			spawned = spawnInFront(id, current)
		elseif localPlayer.inZap then
			spawned = spawnUnderCamera(id)
		else
			return -- in a cutscene or between states; try again next tick
		end
		log(string.format("id %d: spawn returned %s", id, describe(spawned)))
		if spawned == nil then
			nextId()
			return
		end
		state.previous, state.spawned = current, spawned
		localPlayer:SetZapLevel(1, nil, false, { forcedOut = true })
		localPlayer:ZapIntoVehicle(spawned, false, false, false, nil, { disableZapFlash = true })
		log(string.format("id %d: zapping into it", id))
		state.phase, state.ticks = "verify", 0
	elseif state.phase == "verify" then
		local current = playerVehicle()
		if current ~= nil and current == state.spawned then
			log(string.format("id %d: PLAYER IS DRIVING IT after %d ticks (inZap: %s)", id, state.ticks,
				tostring(localPlayer.inZap)))
			state.phase, state.ticks = "chase_view", 0
			if DYNO then
				DYNO.start(id, current, state.previous)
				state.phase = "dyno"
			elseif LIGHTS then
				LIGHTS.start(id, current, state.previous)
				state.phase = "lights"
			end
		elseif state.ticks >= VERIFY_TIMEOUT then
			log(string.format("id %d: zap not confirmed after %d ticks (vehicle: %s, inZap: %s)", id,
				state.ticks, describe(localPlayer.currentVehicle), tostring(localPlayer.inZap)))
			nextId()
		end
	elseif state.phase == "dyno" then
		if DYNO.step() then nextId() end
	elseif state.phase == "lights" then
		if LIGHTS.step() then nextId() end
	elseif state.phase == "chase_view" and state.ticks >= VIEW_TICKS and #VIEWS > 0 then
		removeSideCopy()
		state.view = 1
		showView(id, state.spawned, VIEWS[1])
		state.phase, state.ticks = "extra_view", 0
	elseif state.phase == "extra_view" and state.ticks >= EXTRA_VIEW_TICKS and state.view < #VIEWS then
		state.view = state.view + 1
		showView(id, state.spawned, VIEWS[state.view])
		state.ticks = 0
	elseif (state.phase == "chase_view" and state.ticks >= VIEW_TICKS)
		or (state.phase == "extra_view" and state.ticks >= EXTRA_VIEW_TICKS) then
		endFixedCamera()
		-- Cockpit camera, the way challengeracelogic.lua forces it.
		local ok, err = pcall(function()
			localPlayer.cameraMode = "DriverEye"
			localPlayer:resetCameraMode()
		end)
		log(string.format("id %d: cockpit view %s (cameraMode %s)", id, ok and "on" or ("failed: " .. tostring(err)),
			tostring(localPlayer.cameraMode)))
		removeSideCopy()
		state.phase, state.ticks = "cockpit_view", 0
	elseif state.phase == "cockpit_view" and state.ticks < cockpitTicks() then
		if GLASS_LADDER and GLASS_STEPS[state.ticks] then
			glassStep(id, state.spawned, GLASS_STEPS[state.ticks])
		end
		if #HOLD_MPH > 0 then
			local step = math.floor((state.ticks - 1) / HOLD_TICKS) + 1
			local target = HOLD_MPH[step]
			if target ~= holdTarget then
				holdTarget = target
				log(string.format("id %d: holding %s mph", id, tostring(target)))
			end
		end
		if (DRIVE_SECONDS > 0 or #HOLD_MPH > 0) and state.ticks % 2 == 0 then
			local speed = vehicleSpeed()
			log(string.format("id %d: cockpit t%d speed %s", id, state.ticks,
				speed and string.format("%.2f m/s %.1f mph", speed, speed * MPH) or "?"))
		end
	elseif state.phase == "cockpit_view" then
		holdTarget = nil
		pcall(function()
			localPlayer.cameraMode = "Normal"
			localPlayer:resetCameraMode()
		end)
		log(string.format("id %d: chase view restored", id))
		nextId()
	end
end

local function safeStep()
	local ok, err = pcall(step)
	if not ok then
		log("error: " .. tostring(err))
		removeUserUpdateFunction("dsfforza_clonetest")
	end
end

log("clone test loaded; ids: " .. table.concat(TEST_IDS, ", "))
addUserUpdateFunction("dsfforza_clonetest", safeStep, TICK)
if #HOLD_MPH > 0 then
	addUserUpdateFunction("dsfforza_speedhold", function() pcall(holdSpeed) end, 1)
end
