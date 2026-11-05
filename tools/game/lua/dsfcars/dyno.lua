-- dsfforza dyno: standing-start acceleration and braking runs on a straight road.
-- Loaded by clonetest.lua when config.dyno is set (run-test -Dyno). Per car:
--   place   teleport the player's car to the start (gameVehicle.matrix, every update,
--           velocity zeroed) until the area is resident
--   settle  let it drop onto the road and come to rest
--   ready   print "dyno <id> r<k> ready"; the harness then holds the throttle for
--           DriveSeconds and the brake for BrakeSeconds (run-test.ps1 -Dyno)
--   run     from the first motion (with the 100 updates before it), log every Lua update:
--           "dy <id> <run> <n> <speed m/s> <x> <y> <z> <lateral m> <yaw> <couple> <offroad>"
--           (n = update count, offroad = wheels on an off-road surface)
--           A lane keeper (keepLane) pushes the car back towards the start line: with no
--           steering input cars drift left about 20 m in 330 m here (run 20261022-041951).
--   end     when the car has stopped again after moving, or on timeout
-- src/dsfforza/harness/dyno.py turns the samples into 0-60, 0-100, quarter mile, 60-0 and so on.
-- Time comes from the samples themselves (distance / speed), so the Lua update
-- rate doesn't need to be known.

local M = {}

-- marin_4l_centre_01 (route data of the game's own scripts): about 2 km straight and
-- level (y = 30.0 along the whole stretch; the AI points stay within 4 m of the line
-- from here to (-20.6, -3426) at heading -1.0257, towards -x, +z).
local START = { x = 1671.4, y = 30.0, z = -4452.0, heading = -1.0257 }
local RUNS = 2
local DROP = 0.6              -- start this far above the road and let the car settle
local PLACE_MIN = 4           -- step() ticks (clonetest TICK) holding the car at the start
local PLACE_MAX = 60
local SETTLE_TICKS = 6
local READY_TIMEOUT = 120     -- step() ticks waiting for the throttle
local RUN_TIMEOUT = 160       -- step() ticks after the first motion
local MOVING = 0.3            -- m/s: stopped below this after a run
local GO = 1.0                -- m/s: the throttle is on (idle creep stays below 0.5)
-- Forward along the test stretch (START towards the far AI point). A run only starts on
-- forward motion: in run 20261022-171444 the guest's brake key from the previous car's
-- schedule (Driver.log reaches the guest late) reversed the next car and started its run,
-- and every later schedule of that car went to the wrong run.
local FWD_X, FWD_Z = -20.6 - 1671.4, -3426 - (-4452)
do local l = math.sqrt(FWD_X * FWD_X + FWD_Z * FWD_Z); FWD_X, FWD_Z = FWD_X / l, FWD_Z / l end
local IDLE_EVERY = 60         -- updates between samples while waiting for the throttle
local HISTORY = 100           -- samples before "go" printed with it
local KEEP = { "impulse:1" }  -- lane keeper mode per run (config.dyno.keep), see keepLane
local KEEP_MAX = 20           -- largest couple impulse per update (kg m/s at each arm)
local KEEP_LATERAL = 0.6      -- heading correction (rad) = this x offset (m) / speed (m/s), at most 0.05
local UPDATE = 0.008          -- s per Lua update (125 Hz, measured in run 20261022-041951)

local cur = nil
local trafficOff = false
local parked = 0

local function log(message)
	print("[dsfforza] " .. message)
end

-- Surface scan (config.dyno.scan = { x1, y1, z1, x2, y2, z2, ... }, run-test -DynoScan):
-- before its runs the first car is dropped at each point in turn and, once at rest,
-- "dyno scan <i> <x> <y> <z> offroad <n>" is printed: n = gameVehicle.isOffRoad, the
-- number of wheels on an off-road surface. Used to find sand, dirt and grass to test on.
local SCAN = nil
local SCAN_HOLD = 12          -- step() ticks at least at each point (about 3 s: a screenshot or two)

function M.configure(config)
	if type(config) ~= "table" then return end
	if type(config.start) == "table" and #config.start >= 4 then
		START = { x = config.start[1], y = config.start[2], z = config.start[3], heading = config.start[4] }
	end
	if type(config.scan) == "table" and #config.scan >= 3 then
		SCAN = {}
		for i = 1, #config.scan - 2, 3 do
			table.insert(SCAN, { x = config.scan[i], y = config.scan[i + 1], z = config.scan[i + 2] })
		end
	end
	if type(config.runs) == "number" then RUNS = config.runs end
	if type(config.timeout) == "number" then RUN_TIMEOUT = config.timeout end
	if type(config.keep) == "table" then KEEP = config.keep end
end

local function startMatrix()
	local m = setYRotation(START.heading)
	m[3] = vec.vector(START.x, START.y + DROP, START.z, 1)
	return m
end

local function startPosition()
	return vec.vector(START.x, START.y, START.z, 1)
end

local function field(object, key)
	local ok, value = pcall(function() return object[key] end)
	if ok then return tostring(value) end
	return "?"
end

local function disableTraffic()
	if trafficOff then return end
	trafficOff = true
	local results = {}
	for name, fn in pairs({
		traffic = function() civilianTraffic.setTrafficOnOff(false) end,
		parked = function() ParkedVehicles.SetEnabled(false) end,
		interesting = function() InterestingVehicleManager.Enable(false) end,
	}) do
		local ok, err = pcall(fn)
		table.insert(results, name .. "=" .. (ok and "off" or tostring(err)))
	end
	log("dyno: " .. table.concat(results, " "))
end

local function clearArea()
	pcall(GameVehicleResource.ClearAreaOfVehicles, startPosition(), 120)
end

-- Begin the runs for the car the player now drives. previous: the car driven
-- before (moved out of the way so it can't block the strip).
function M.start(id, gameVehicle, previous)
	disableTraffic()
	if previous ~= nil and previous ~= gameVehicle then
		-- Park it behind the start line, one slot further back per car, so it can't
		-- block the strip (destroying it under the vehicle manager's feet is riskier).
		parked = parked + 1
		local back = 20 + 9 * parked
		local ok, err = pcall(function()
			local m = setYRotation(START.heading)
			m[3] = vec.vector(START.x - math.sin(START.heading) * back, START.y + DROP,
				START.z - math.cos(START.heading) * back, 1)
			previous.matrix = m
			previous.velocity = vec.vector(0, 0, 0, 0)
		end)
		log(string.format("dyno %d: previous car parked %d m behind the start: %s", id, back,
			ok and "ok" or tostring(err)))
	end
	local keys = { "topSpeed", "topSpeedLimit", "displayedTopSpeed", "performance", "engineTweak",
		"steeringTweak", "softness", "height", "model_id" }
	local parts = {}
	for _, key in ipairs(keys) do
		table.insert(parts, key .. "=" .. field(gameVehicle, key))
	end
	log(string.format("dyno %d: %s", id, table.concat(parts, " ")))
	cur = { id = id, gv = gameVehicle, run = 1, phase = "place", ticks = 0, n = 0, peak = 0, history = {} }
	if SCAN ~= nil and #SCAN > 0 then
		cur.phase, cur.scan = "scan_place", 1
		log(string.format("dyno %d: scanning %d points", id, #SCAN))
		return
	end
	log(string.format("dyno %d r1: placing at (%.1f, %.1f, %.1f) heading %.3f", id, START.x, START.y, START.z,
		START.heading))
end

local function offRoad(gv)
	local ok, value = pcall(function() return gv.isOffRoad end)
	if ok and type(value) == "number" then return value end
	return -1
end

local function finishRun(reason)
	log(string.format("dyno %d r%d end %s (peak %.2f m/s, %d updates)", cur.id, cur.run, reason, cur.peak, cur.n))
	if cur.run < RUNS then
		cur.run = cur.run + 1
		cur.phase, cur.ticks, cur.n, cur.peak, cur.history = "place", 0, 0, 0, {}
		cur.yaw, cur.rate, cur.pulse, cur.released = nil, 0, 0, false
		log(string.format("dyno %d r%d: placing", cur.id, cur.run))
	else
		cur.phase = "done"
	end
end

-- Lane keeping. Writing gameVehicle.matrix every update stalls the car (run
-- 20261022-042557: 0.6 m/s at full throttle), so the keeper pushes instead:
-- GameVehicleResource.applyImpulseAtPoint (vehicle space), a sideways couple at
-- +-COUPLE_ARM m along the car, which turns it without a net side force.
-- Modes per run (config.dyno.keep = { "none", "pulse:50", "impulse:1", ... }):
--   none        log only
--   pulse:J     open loop, every 2 s a 0.32 s couple of J (calibration)
--   impulse:K[:V] closed loop (K scales the gain); with V, off for good once the car
--               reaches V m/s, so scripted steering inputs act on a free car
--   impulse:K   closed loop (K scales the gain): yaw rate towards -2 x heading error, the
--               heading aimed back at the line (KEEP_LATERAL)
local COUPLE_ARM = 1.5
local impulseError = false

local function couple(gv, j)
	for _, arm in ipairs({ COUPLE_ARM, -COUPLE_ARM }) do
		local impulse = vec.vector(arm > 0 and j or -j, 0, 0, 0)
		local point = vec.vector(0, 0, arm, 0)
		local ok, err = pcall(GameVehicleResource.applyImpulseAtPoint,
			{ gameVehicle = gv, Impulse = impulse, Point = point, impulse = impulse, point = point, gv, impulse, point })
		if not ok and not impulseError then
			impulseError = true
			log("dyno: applyImpulseAtPoint failed: " .. tostring(err))
		end
	end
end

local function keepMode()
	local mode = KEEP[cur.run] or KEEP[#KEEP] or "none"
	local name, value, cutoff = string.match(mode, "^(%a+):?([-%d.]*):?([-%d.]*)$")
	return name or "none", tonumber(value) or 0, tonumber(cutoff)
end

-- Returns the signed distance from the start line (m, + left of the direction of
-- travel), the yaw (rad) and the couple applied this update.
local function keepLane(gv, position, speed)
	local h = START.heading
	local dx, dz = position.x - START.x, position.z - START.z
	local lateral = math.cos(h) * dx - math.sin(h) * dz
	local yaw = 0
	pcall(function()
		local forward = gv.matrix[2]
		yaw = math.atan2(forward.x, forward.z)
	end)
	local rate = 0
	if cur.yaw ~= nil then
		local d = yaw - cur.yaw
		if d > math.pi then d = d - 2 * math.pi elseif d < -math.pi then d = d + 2 * math.pi end
		rate = d / UPDATE
	end
	cur.yaw = yaw
	cur.rate = (cur.rate or 0) * 0.8 + rate * 0.2
	local name, value, cutoff = keepMode()
	-- impulse:K:V lets go for good once the car first reaches V m/s (manoeuvre runs).
	-- (Holding the speed with gameVehicle.speed every update until the input was tried
	-- in run 20261022-054100: it pins the car, steering then does nothing.)
	if cutoff ~= nil and speed > cutoff then cur.released = true end
	if cur.released then name = "none" end
	local j = 0
	if speed >= GO and name == "pulse" and speed > 8 then
		cur.pulse = (cur.pulse or 0) + 1
		if cur.pulse % 250 < 40 then j = value end
	elseif speed >= GO and name == "impulse" then
		-- Heading target: back to the line in about 1.7 s at any speed.
		local target = h - math.max(-0.05, math.min(0.05, KEEP_LATERAL * lateral / math.max(speed, 10)))
		local err = yaw - target
		if err > math.pi then err = err - 2 * math.pi elseif err < -math.pi then err = err + 2 * math.pi end
		local wanted = -2 * err
		-- Couple that removes 5% of the yaw-rate error per update. Calibrated in run
		-- 20261022-043154: a couple of 50 for 40 updates turned the 227 by ~1 rad/s,
		-- as an SI impulse on its yaw inertia (VO3D p16 = 7524) predicts.
		local inertia = 7500
		j = value * inertia * 0.05 * (wanted - cur.rate) / (2 * COUPLE_ARM)
		j = math.max(-KEEP_MAX, math.min(KEEP_MAX, j))
	end
	if j ~= 0 then couple(gv, j) end
	return lateral, yaw, j
end

-- Every Lua update.
local function sample()
	if cur == nil then return end
	local gv = cur.gv
	if cur.phase == "place" or cur.phase == "scan_place" then
		pcall(function()
			gv.matrix = cur.matrix
			gv.velocity = vec.vector(0, 0, 0, 0)
		end)
		return
	end
	if cur.phase ~= "ready" and cur.phase ~= "run" then return end
	cur.n = cur.n + 1
	local ok, speed, position = pcall(function() return gv.speed, gv.position end)
	if not ok or speed == nil or position == nil then return end
	local lateral, yaw, j = keepLane(gv, position, speed)
	-- Last field: wheels on an off-road surface (gameVehicle.isOffRoad).
	local line = string.format("[dsfforza] dy %d %d %d %.3f %.3f %.3f %.3f %.2f %.4f %.1f %d", cur.id, cur.run,
		cur.n, speed, position.x, position.y, position.z, lateral, yaw, j, offRoad(gv))
	if cur.phase == "ready" then
		-- Forward of where the car last stood (a reversing car is left where it stops:
		-- placing it again while the guest still holds a key cost the run in 20261022-173553).
		local along = 0
		if cur.readyPos then
			along = (position.x - cur.readyPos.x) * FWD_X + (position.z - cur.readyPos.z) * FWD_Z
		end
		if speed < MOVING or along < 0 then cur.readyPos = { x = position.x, z = position.z } end
		if speed > GO and along > 0.2 then
			cur.phase, cur.ticks = "run", 0
			log(string.format("dyno %d r%d go", cur.id, cur.run))
			for i = 1, #cur.history do print(cur.history[i]) end
			cur.history = {}
		else
			table.insert(cur.history, line)
			if #cur.history > HISTORY then table.remove(cur.history, 1) end
			if cur.n % IDLE_EVERY ~= 0 then return end
		end
	end
	if speed > cur.peak then cur.peak = speed end
	print(line)
	if cur.phase == "run" and cur.peak > 5 and speed < MOVING then
		cur.phase = "stopped"
	end
end

-- Every clonetest step (TICK updates). Returns true when this car is done.
function M.step()
	if cur == nil then return true end
	cur.ticks = cur.ticks + 1
	if cur.phase == "scan_place" then
		local p = SCAN[cur.scan]
		local at = vec.vector(p.x, p.y, p.z, 1)
		if cur.ticks == 1 then
			local m = setYRotation(START.heading)
			m[3] = vec.vector(p.x, p.y + DROP, p.z, 1)
			cur.matrix = m
		end
		local resident = false
		pcall(function() resident = spoolsystem.IsLocationResident(at) end)
		if (resident and cur.ticks >= PLACE_MIN) or cur.ticks >= PLACE_MAX then
			-- A car let go with zero velocity well above the ground can stay put (asleep):
			-- start it falling.
			pcall(function() cur.gv.velocity = vec.vector(0, -3, 0, 0) end)
			cur.phase, cur.ticks = "scan_settle", 0
		end
	elseif cur.phase == "scan_settle" then
		local speed = 0
		pcall(function() speed = cur.gv.speed end)
		if (cur.ticks >= SCAN_HOLD and speed < MOVING) or cur.ticks >= SETTLE_TICKS * 4 then
			local position = cur.gv.position
			log(string.format("dyno scan %d %.2f %.2f %.2f offroad %d speed %.2f", cur.scan, position.x, position.y,
				position.z, offRoad(cur.gv), speed))
			if cur.scan < #SCAN then
				cur.scan, cur.phase, cur.ticks = cur.scan + 1, "scan_place", 0
			else
				SCAN = nil
				cur.phase, cur.ticks = "place", 0
				log(string.format("dyno %d r1: placing at (%.1f, %.1f, %.1f) heading %.3f", cur.id, START.x, START.y,
					START.z, START.heading))
			end
		end
	elseif cur.phase == "place" then
		if cur.ticks == 1 then
			cur.matrix = startMatrix()
			clearArea()
		end
		local resident = false
		pcall(function() resident = spoolsystem.IsLocationResident(startPosition()) end)
		if (resident and cur.ticks >= PLACE_MIN) or cur.ticks >= PLACE_MAX then
			log(string.format("dyno %d r%d: placed after %d ticks (resident %s)", cur.id, cur.run, cur.ticks,
				tostring(resident)))
			cur.phase, cur.ticks = "settle", 0
		end
	elseif cur.phase == "settle" then
		local speed = 0
		pcall(function() speed = cur.gv.speed end)
		if cur.ticks >= SETTLE_TICKS and speed < MOVING then
			clearArea()
			local position = cur.gv.position
			cur.readyPos = { x = position.x, z = position.z }
			log(string.format("dyno %d r%d ready at (%.2f, %.2f, %.2f)", cur.id, cur.run, position.x, position.y,
				position.z))
			cur.phase, cur.ticks, cur.n, cur.peak, cur.history = "ready", 0, 0, 0, {}
		elseif cur.ticks >= SETTLE_TICKS * 4 then
			log(string.format("dyno %d r%d: still moving at %.2f m/s after settling; placing again", cur.id,
				cur.run, speed))
			cur.phase, cur.ticks = "place", 0
		end
	elseif cur.phase == "ready" then
		if cur.ticks >= READY_TIMEOUT then finishRun("no throttle") end
	elseif cur.phase == "run" then
		if cur.ticks >= RUN_TIMEOUT then finishRun("timeout") end
	elseif cur.phase == "stopped" then
		finishRun("stopped")
	elseif cur.phase == "done" then
		cur = nil
		return true
	end
	return false
end

addUserUpdateFunction("dsfforza_dyno", function()
	local ok, err = pcall(sample)
	if not ok then log("dyno sample error: " .. tostring(err)) end
end, 1)

return M
