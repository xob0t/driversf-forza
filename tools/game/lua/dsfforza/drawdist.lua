-- dsfforza drawdist probe (research/draw-distance.md). A drop-in for lightstest.lua:
-- run-series.ps1 -Lights -LuaFiles 'dsfforza\lightstest.lua=tools\game\lua\dsfforza\drawdist.lua;
--   dsfforza\ddconfig.lua=<file returning { mode = "lineup" | "traffic", ... }>'
-- clonetest.lua hands it the player's car (M.start), then calls M.step() every clonetest tick.
-- Driven by out/drawdist/probe/run-dd.ps1 (run-series.ps1 -OnHost -Lights). Its ready line ends in
-- "(no drive)", so run-test.ps1 skips the lamp tests' drive steps and only sets phases.
-- ddconfig fields (all optional): mode, distances, lanes, drop, sweep (false or a list of cutoffs),
-- sweep_from, extra (phases { name, lod, flares, lights }), lod / flares (applied at start, both
-- modes), speed, travel, park, sample, radius, settle.
--
-- mode "lineup": traffic off; the player's car is pinned at the start of the 2 km Marin straight
-- (dyno.lua START); copies of the same model are pinned ahead at fixed distances, rear to the
-- camera, brake lamps on. Then "ready K phases"; the harness sets phase k through
-- dsfforza\lightsync.lua and screenshots it. Phases switch the DRL/emergency flare curves
-- (Sfx.*Flare*) and the vehicle LOD thresholds (performance.SetLODThreshold, category 2).
--
-- mode "traffic": traffic on; the player's car is moved along the straight at SPEED m/s
-- (matrix set every update), then parked. Every SAMPLE updates the vehicles within RADIUS of
-- the camera are listed (GameVehicleResource.vehiclesInArea); each first sighting ("new") and
-- each loss ("gone") is logged with its distance to the camera and to the player and its
-- camera-space direction (x/z, y/z; z > 0 is in front).

local M = {}

local START = { x = 1671.4, y = 30.0, z = -4452.0, heading = -1.0257 }
local FWD_X, FWD_Z = math.sin(START.heading), math.cos(START.heading)
local RIGHT_X, RIGHT_Z = math.cos(START.heading), -math.sin(START.heading)

local OPTIONS = {
	mode = "lineup",
	distances = { 12, 18, 26, 36, 50, 70, 100, 140, 200, 280, 400, 560 },
	drop = 0.2,          -- lineup: height of the pinned cars above START.y
	lanes = { -5.25, 1.75, 5.25, -1.75 },
	speed = 20,          -- traffic mode: m/s along the straight
	travel = 1500,       -- traffic mode: metres to travel before parking
	park = 4000,         -- traffic mode: updates to stay parked (at ~125 updates/s)
	sample = 12,         -- traffic mode: updates between samples
	radius = 1500,       -- traffic mode: vehiclesInArea radius around the camera
	settle = 16,         -- lineup: clonetest ticks (30 frames) of settling before "ready"
}

local HIGH = { [5] = 0.026697, [1] = 0.00643125, [2] = 0.00511777, [3] = 6.8e-05, [4] = 1e-05 }
local BIG = 1000      -- this Lua rejects literals beyond +-8388606

local function log(message)
	print("[dsfforza.dd] " .. message)
end

local function fmt(v)
	if v == nil then return "nil" end
	local ok, s = pcall(function() return string.format("(%.2f %.2f %.2f)", v.x, v.y, v.z) end)
	return ok and s or tostring(v)
end

local function setLod(map)
	local parts = {}
	for i = 1, 5 do
		if map[i] ~= nil then
			local ok, err = pcall(performance.SetLODThreshold, 2, i, map[i])
			table.insert(parts, string.format("%d=%g%s", i, map[i], ok and "" or (" ERR " .. tostring(err))))
		end
	end
	log("lod cat2 " .. table.concat(parts, " "))
end

local function lodOnly(slot, value)
	-- Only records up to `slot` can match; `slot` (Lua index) gets `value`, the rest BIG.
	local map = { [5] = BIG, [1] = BIG, [2] = BIG, [3] = BIG, [4] = BIG }
	map[slot] = value
	setLod(map)
end

local FLARE_DEFAULT = {
	DRLFlareScale = { { 6, 80, 300 }, { 0, 0.5, 1.5 } },
	DRLFlareTransparency = { { 0, 20, 300 }, { 0.5, 0.5, 0.5 } },
	DRLFlareBrightness = { { 0, 20, 300 }, { 1, 2.5, 2.5 } },
}

local function flares(set)
	for name, curve in pairs(set) do
		local ok, err = pcall(Sfx[name], curve[1], curve[2])
		log(string.format("Sfx.%s({%g,%g,%g},{%g,%g,%g}) %s", name, curve[1][1], curve[1][2], curve[1][3],
			curve[2][1], curve[2][2], curve[2][3], ok and "ok" or tostring(err)))
	end
end

local function setLights(car, set)
	for _, name in ipairs({ "All", "Headlights", "DaytimeRunning", "Brakes" }) do
		if set[name] ~= nil then
			pcall(GameVehicleResource.setLights, { gameVehicle = car, LightType = name, State = set[name] })
		end
	end
end

local function camera()
	local cam = rawget(_G, "game_camera")
	if cam == nil then
		local ok, c = pcall(function() return CameraSystem.GetCamera() end)
		cam = ok and c or nil
	end
	if cam == nil then return nil end
	local ok, m = pcall(function() return cam.matrix end)
	if not ok or m == nil then return nil end
	local fov = nil
	pcall(function() fov = cam.fov end)
	return { pos = m[3], right = m[0], up = m[1], fwd = -m[2], fov = fov }
end

local function camSpace(cam, p)
	local rel = p - cam.pos
	local d = rel:length()
	local z = rel:dot(cam.fwd)
	local x = rel:dot(cam.right)
	local y = rel:dot(cam.up)
	if math.abs(z) < 0.001 then z = 0.001 end
	return d, x / z, y / z, z
end

local function startMatrix(s, lateral, drop)
	local m = setYRotation(START.heading)
	m[3] = vec.vector(START.x + FWD_X * s + RIGHT_X * lateral, START.y + (drop or 0.6),
		START.z + FWD_Z * s + RIGHT_Z * lateral, 1)
	return m
end

local function pin(car, m)
	pcall(function()
		car.matrix = m
		car.velocity = vec.vector(0, 0, 0, 0)
	end)
end

local function setTraffic(on)
	local results = {}
	for name, fn in pairs({
		traffic = function() civilianTraffic.setTrafficOnOff(on) end,
		parked = function() ParkedVehicles.SetEnabled(on) end,
		interesting = function() InterestingVehicleManager.Enable(on) end,
	}) do
		local ok, err = pcall(fn)
		table.insert(results, name .. "=" .. (ok and tostring(on) or tostring(err)))
	end
	log("traffic: " .. table.concat(results, " "))
end

-- Imposter systems, shader LOD, camera.
local function environmentReport()
	pcall(function()
		local systems = Imposters.GetISystems()
		if type(systems) == "table" then
			for k, name in pairs(systems) do
				local ok, cfg = pcall(Imposters.GetISystemConfig, name)
				local text = tostring(cfg)
				if ok and type(cfg) == "table" then
					local parts = {}
					for key, value in pairs(cfg) do table.insert(parts, tostring(key) .. "=" .. tostring(value)) end
					text = table.concat(parts, " ")
				end
				log(string.format("imposter system %s %s: %s", tostring(k), tostring(name), text))
			end
		else
			log("Imposters.GetISystems: " .. tostring(systems))
		end
	end)
	local ok, shaderLod = pcall(performance.GetEnableShaderLOD)
	log("GetEnableShaderLOD: " .. tostring(ok) .. " " .. tostring(shaderLod))
	local cam = camera()
	if cam then log(string.format("camera pos %s fwd %s fov %s", fmt(cam.pos), fmt(cam.fwd), tostring(cam.fov))) end
end

local state = {}

-- ---------------------------------------------------------------- lineup

local LIGHTS_BRAKES = { All = false, Brakes = true }
local FLARES_OFF = { DRLFlareScale = { { 6, 80, 300 }, { 0, 0, 0 } } }
local NONE = { [5] = BIG, [1] = BIG, [2] = BIG, [3] = BIG, [4] = BIG }

local function scaled(map, k)
	local m = {}
	for key, v in pairs(map) do m[key] = v * k end
	return m
end

-- Visibility sweep cutoffs: slot 0 = x, the rest BIG, so a car is drawn exactly when its
-- metric >= x. Geometric, 2^(1/3) apart (each step ~12 % in distance).
local function sweepValues()
	if type(OPTIONS.sweep) == "table" then return OPTIONS.sweep end
	local list, x = {}, OPTIONS.sweep_from or 0.6
	while x >= 2.5e-6 do
		table.insert(list, x)
		x = x / 1.259921
	end
	return list
end

local PHASES = nil
local function buildPhases()
	PHASES = {
		{ name = "base: High LOD, default flares, brakes", lod = HIGH, flares = FLARE_DEFAULT, lights = LIGHTS_BRAKES },
		{ name = "flares off", flares = FLARES_OFF },
		{ name = "flares boosted", flares = {
			DRLFlareScale = { { 6, 80, 300 }, { 0, 1.5, 4 } },
			DRLFlareTransparency = { { 0, 20, 300 }, { 1, 1, 1 } },
			DRLFlareBrightness = { { 0, 20, 300 }, { 1, 4, 4 } } } },
		{ name = "default flares, DaytimeRunning only", flares = FLARE_DEFAULT, lights = { All = false, DaytimeRunning = true } },
		{ name = "default flares, lamps off", lights = { All = false } },
		{ name = "High, brakes, flares off", flares = FLARES_OFF, lights = LIGHTS_BRAKES },
		{ name = "force LOD mask 4 (T0=0)", lod = { [5] = 0, [1] = BIG, [2] = BIG, [3] = BIG, [4] = BIG } },
		{ name = "force LOD mask 8 (T1=0)", lod = { [5] = BIG, [1] = 0, [2] = BIG, [3] = BIG, [4] = BIG } },
		{ name = "force LOD mask 16 (T2=0)", lod = { [5] = BIG, [1] = BIG, [2] = 0, [3] = BIG, [4] = BIG } },
		{ name = "force LOD mask 1 (T3=0)", lod = { [5] = BIG, [1] = BIG, [2] = BIG, [3] = 0, [4] = BIG } },
		{ name = "nothing drawn (all BIG)", lod = NONE },
	}
	if OPTIONS.sweep ~= false then
		for _, x in ipairs(sweepValues()) do
			table.insert(PHASES, { name = string.format("cutoff %.4g", x),
				lod = { [5] = x, [1] = BIG, [2] = BIG, [3] = BIG, [4] = BIG } })
		end
	end
	table.insert(PHASES, { name = "High x0.25 (dsf_enhanced LODEngineThresholdScale), flares off", lod = scaled(HIGH, 0.25) })
	table.insert(PHASES, { name = "High x0.25, default flares", flares = FLARE_DEFAULT })
	-- ddconfig extra = { { name =, lod = { [5]=,[1]=,[2]=,[3]=,[4]= }, flares = { DRLFlareScale = { d, v }, ... },
	--   lights = { All =, Brakes =, DaytimeRunning = } }, ... }
	for _, phase in ipairs(OPTIONS.extra or {}) do table.insert(PHASES, phase) end
	table.insert(PHASES, { name = "High again, default flares", lod = HIGH, flares = FLARE_DEFAULT, lights = LIGHTS_BRAKES })
end

-- performance.GetLODThreshold readback (Lua index 1..5 = slot 1..4, 0; Driver.exe 0x645530).
local function lodReadback(label)
	local parts = {}
	for i = 1, 5 do
		local ok, v = pcall(performance.GetLODThreshold, 2, i)
		table.insert(parts, string.format("%d=%s", i, ok and tostring(v) or ("ERR " .. tostring(v))))
	end
	log("lod readback " .. label .. " cat2 " .. table.concat(parts, " "))
end

local function lineupStart(id, car)
	setTraffic(false)
	pcall(GameVehicleResource.ClearAreaOfVehicles, vec.vector(START.x, START.y, START.z, 1), 150)
	state.cars = {}
	for i, s in ipairs(OPTIONS.distances) do
		local lateral = OPTIONS.lanes[(i - 1) % #OPTIONS.lanes + 1]
		local m = startMatrix(s, lateral, OPTIONS.drop)
		local ok, gv = pcall(GameVehicleResource.create, {
			SnapToTerrain = true, Model_Id = id, NoOccupants = true, InitialVelocity = 0,
			Position = m[3], Heading = vec.vector(FWD_X, 0, FWD_Z, 0),
		})
		if ok and gv ~= nil then
			table.insert(state.cars, { gv = gv, s = s, lateral = lateral, m = m })
		end
		log(string.format("lineup car %d at %g m lateral %g: %s", i, s, lateral, ok and tostring(gv) or ("failed " .. tostring(gv))))
	end
end

-- Every car, the player's included, is pinned every frame (own update function) at road height
-- (settled cars sit 0.18 to 0.25 m above START.y) with START's heading: rear to the camera. A pin
-- every clonetest tick (30 frames) 0.6 m up let the cars drop and bounce, and the camera bobbed.
local function lineupPin()
	pin(state.car, startMatrix(0, -1.75, OPTIONS.drop))
	for _, c in ipairs(state.cars or {}) do pin(c.gv, c.m) end
end

local function cameraDetails()
	local parts = {}
	local ok, cam = pcall(function() return CameraSystem.GetCamera() end)
	for _, name in ipairs({ "fov", "aspect_ratio", "nearz", "farz", "viewport", "viewport_rect" }) do
		local okv, v = pcall(function() return (rawget(_G, "game_camera") or cam)[name] end)
		local text = okv and tostring(v) or "?"
		if okv and type(v) == "table" then
			local sub = {}
			for k, x in pairs(v) do table.insert(sub, tostring(k) .. "=" .. tostring(x)) end
			text = "{" .. table.concat(sub, " ") .. "}"
		end
		table.insert(parts, name .. "=" .. text)
	end
	return table.concat(parts, " ")
end

local function lineupReport()
	local cam = camera()
	if cam == nil then log("no camera") return end
	log(string.format("camera pos %s fwd %s right %s up %s player %s", fmt(cam.pos), fmt(cam.fwd), fmt(cam.right), fmt(cam.up),
		fmt(state.car.position)))
	log("camera " .. cameraDetails())
	for i, c in ipairs(state.cars) do
		local ok, p = pcall(function() return c.gv.position end)
		if ok and p ~= nil then
			local d, xz, yz, z = camSpace(cam, p)
			log(string.format("lineup %d s=%g cam_d=%.2f z=%.2f x/z=%.4f y/z=%.4f pos %s", i, c.s, d, z, xz, yz, fmt(p)))
		end
	end
end

local function applyPhase(k)
	local phase = PHASES[k]
	if phase == nil then return end
	if phase.run then phase.run() end
	if phase.lod then setLod(phase.lod) end
	if phase.flares then flares(phase.flares) end
	if phase.lights then
		setLights(state.car, { All = false })
		for _, c in ipairs(state.cars) do setLights(c.gv, phase.lights) end
	end
	log(string.format("phase %d %s", k, phase.name))
	if k == 1 then lineupReport() lodReadback("phase 1") end
	if k % 10 == 0 then lineupReport() end
end

-- ---------------------------------------------------------------- traffic

local seen = {}
local samples = 0

local function trafficSample(t)
	local cam = camera()
	if cam == nil then return end
	local playerPos = nil
	pcall(function() playerPos = state.car.position end)
	local ok, list = pcall(GameVehicleResource.vehiclesInArea, { center = cam.pos, radius = OPTIONS.radius })
	if not ok or type(list) ~= "table" then
		if samples == 0 then log("vehiclesInArea failed: " .. tostring(list)) end
		return
	end
	samples = samples + 1
	local now = {}
	local count, maxd = 0, 0
	local playerKey = tostring(state.car)
	for _, gv in ipairs(list) do
		local okp, p = pcall(function() return gv.position end)
		local key = tostring(gv)
		if okp and p ~= nil and key ~= playerKey then
			local d, xz, yz, z = camSpace(cam, p)
			local dp = playerPos and (p - playerPos):length() or -1
			local model, owner = -1, "?"
			pcall(function() model = gv.model_id end)
			pcall(function() owner = tostring(gv.owner) end)
			local prev = seen[key]
			if prev ~= nil and (p - prev.p):length() > 60 then prev = nil end   -- reused object
			if prev == nil and samples > 1 then
				log(string.format("new t=%.2f %s m=%s o=%s d=%.1f dp=%.1f x/z=%.3f y/z=%.3f z=%.1f", t, key,
					tostring(model), owner, d, dp, xz, yz, z))
			end
			now[key] = { p = p, d = d, dp = dp, xz = xz, yz = yz, z = z, model = model, owner = owner }
			count = count + 1
			if d > maxd then maxd = d end
		end
	end
	for key, v in pairs(seen) do
		if now[key] == nil then
			log(string.format("gone t=%.2f %s m=%s o=%s d=%.1f dp=%.1f x/z=%.3f y/z=%.3f z=%.1f", t, key,
				tostring(v.model), v.owner, v.d, v.dp, v.xz, v.yz, v.z))
		end
	end
	seen = now
	if samples % 25 == 1 then
		log(string.format("sample %d t=%.2f vehicles=%d max_d=%.1f cam %s player %s", samples, t, count, maxd,
			fmt(cam.pos), fmt(playerPos)))
	end
end

local function trafficUpdate()
	if state.mode ~= "traffic" or state.done then return end
	state.u = state.u + 1
	local t = state.u * 0.008
	local dt = 0.008
	if type(os) == "table" and type(os.clock) == "function" then
		local c = os.clock()
		if state.c0 == nil then state.c0, state.cl = c, c end
		dt = math.max(0, math.min(0.1, c - state.cl))
		state.cl = c
		t = c - state.c0
	end
	if state.s < OPTIONS.travel then
		if state.u > 300 then state.s = state.s + OPTIONS.speed * dt end
		local m = startMatrix(state.s, -1.75, 0.1)
		pcall(function()
			state.car.matrix = m
			state.car.velocity = vec.vector(FWD_X * OPTIONS.speed, 0, FWD_Z * OPTIONS.speed, 0)
		end)
	else
		state.parked = state.parked + 1
		if state.parked == 1 then log(string.format("parked at s=%.1f t=%.2f", state.s, t)) end
		if state.parked < 200 then
			pin(state.car, startMatrix(state.s, -1.75, 0.1))
		end
		if state.parked >= OPTIONS.park then
			state.done = true
			log(string.format("id %d: ready 1 phases", state.id))
			log(string.format("[traffic summary] samples=%d", samples))
		end
	end
	if state.u % OPTIONS.sample == 0 then
		local ok, err = pcall(trafficSample, t)
		if not ok then log("sample error: " .. tostring(err)) end
	end
end

-- ---------------------------------------------------------------- interface

function M.configure(config)
	local found, cfg = user_open("dsfforza\\ddconfig.lua")
	if found and type(cfg) == "table" then
		for key, value in pairs(cfg) do OPTIONS[key] = value end
	end
	-- clonetest.lua switches traffic off for the probe unless the module asks for it.
	M.trafficOn = OPTIONS.mode ~= "lineup"
	log("mode " .. tostring(OPTIONS.mode))
end

function M.start(id, car)
	state = { id = id, car = car, mode = OPTIONS.mode, ticks = 0, lastSync = nil, index = 0, ready = false,
		u = 0, s = 0, parked = 0, done = false }
	environmentReport()
	if type(OPTIONS.lod) == "table" then setLod(OPTIONS.lod) end
	if type(OPTIONS.flares) == "table" then flares(OPTIONS.flares) end
	lodReadback("start")
	if OPTIONS.mode == "lineup" then
		buildPhases()
		lineupStart(id, car)
		addUserUpdateFunction("dsfforza_drawdist_pin", function() pcall(lineupPin) end, 1)
	else
		setTraffic(true)
		addUserUpdateFunction("dsfforza_drawdist", function()
			local ok, err = pcall(trafficUpdate)
			if not ok and not state.errored then state.errored = true log("update error: " .. tostring(err)) end
		end, 1)
	end
	log(string.format("id %d: start mode %s", id, OPTIONS.mode))
end

local SYNC = "dsfforza\\lightsync.lua"
local function syncValue()
	local base = rawget(_G, "UserLuaMediaPath") or ""
	if type(io) == "table" and type(io.open) == "function" then
		local ok, value = pcall(function()
			local f = io.open(base .. SYNC, "r")
			if f == nil then return nil end
			local text = f:read("*a")
			f:close()
			return tonumber(string.match(text or "", "-?%d+"))
		end)
		if ok and value ~= nil then return value end
	end
	local ok, found, value = pcall(user_open, SYNC)
	if ok and found and type(value) == "table" then return tonumber(value.phase) end
	return nil
end

-- Returns true when finished.
function M.step()
	state.ticks = state.ticks + 1
	if state.mode == "lineup" then
		lineupPin()
		if not state.ready then
			if state.ticks >= OPTIONS.settle then
				state.ready = true
				state.lastSync = syncValue() or 0
				lineupReport()
				log(string.format("id %d: ready %d phases", state.id, #PHASES))
				print(string.format("[dsfforza.lights] id %d: ready %d phases (no drive)", state.id, #PHASES))
			end
			return false
		end
	elseif not state.done then
		return false
	elseif not state.readyPrinted then
		state.readyPrinted = true
		state.lastSync = syncValue() or 0
		print(string.format("[dsfforza.lights] id %d: ready 1 phases (no drive)", state.id))
		return false
	end
	local sync = syncValue()
	if sync ~= nil and sync ~= state.lastSync then
		state.lastSync = sync
		if sync < 0 then
			setLod(type(OPTIONS.lod) == "table" and OPTIONS.lod or HIGH)
			flares(FLARE_DEFAULT)
			pcall(removeUserUpdateFunction, "dsfforza_drawdist_pin")
			log(string.format("id %d: done", state.id))
			return true
		end
		if sync > 0 and state.mode == "lineup" then applyPhase(sync) end
		if sync > 0 and state.mode ~= "lineup" then log("traffic final phase " .. sync) end
	end
	return false
end

return M
