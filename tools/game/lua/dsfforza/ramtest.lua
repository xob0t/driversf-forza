-- dsfforza ramtest: where a car's contacts register (its collision hull; research/damage.md,
-- "Collision hull", #210). Stands in for dyno.lua, same interface:
--   run-series.ps1 -Vm -Dyno -DynoRuns 1 -DynoKeys 'Up:6,Down:5' -LuaFiles
--     'dsfforza\dyno.lua=tools\game\lua\dsfforza\ramtest.lua;dsfforza\dynoreal.lua=tools\game\lua\dsfforza\dyno.lua'
-- Per car, on dyno.lua's straight: a parked copy of the same model stands side-on GAP m
-- ahead; the car is pushed into it at SPEED (velocity set every update until the copy
-- moves, i.e. the first contact), first nose first, then (placed beyond the copy) tail
-- first. Logged every update from the push on:
--   "ram <id> <run> <n> <speed along> <gap> <lateral>"   gap = centre distance along the car
-- and at the first contact "ram <id> <run> contact gap <g> (previous <g0>) ...". The gap at
-- contact = the car's hull extent ahead (or behind) + the copy's half width. Rest heights of
-- both cars are logged before each run ("ram <id> rest ..."). Then, if dsfforza\dynoreal.lua
-- is there, the car's ordinary dyno runs follow (keys from -DynoKeys).

local M = {}

local START = { x = 1671.4, y = 30.0, z = -4452.0, heading = -1.0257 }
local GAP = 12.0
local SPEED = 4.0
local DROP = 0.6
local PLACE_MIN, PLACE_MAX = 4, 60
local SETTLE_TICKS = 8
local RUN_TICKS = 40          -- step() ticks per run at most
local AFTER = 120             -- updates logged after the contact
local MOVED = 0.01            -- m the copy moves: contact

local cur = nil
local trafficOff = false
local parked = 0

local function log(message)
	print("[dsfforza] " .. message)
end

-- After the two pushes the car's ordinary dyno runs follow (tools\game\lua\dsfforza\dyno.lua,
-- copied in as dsfforza\dynoreal.lua): launch, brake, steer pulse with run-series -DynoKeys.
local REAL = nil

function M.configure(config)
	local loaded, module = user_open("dsfforza\\dynoreal.lua")
	if loaded and type(module) == "table" then
		REAL = module
		REAL.configure(config)
	else
		log("ram: dynoreal.lua did not load: " .. tostring(module))
	end
end

local function fwd()
	return math.sin(START.heading), math.cos(START.heading)
end

local function at(along, lift)
	local fx, fz = fwd()
	return vec.vector(START.x + fx * along, START.y + (lift or 0), START.z + fz * along, 1)
end

local function disableTraffic()
	if trafficOff then return end
	trafficOff = true
	pcall(function() civilianTraffic.setTrafficOnOff(false) end)
	pcall(function() ParkedVehicles.SetEnabled(false) end)
	pcall(function() InterestingVehicleManager.Enable(false) end)
end

local function clear()
	pcall(GameVehicleResource.ClearAreaOfVehicles, at(GAP), 60)
end

local function placeCar(gv, along, heading)
	local m = setYRotation(heading)
	m[3] = at(along, DROP)
	gv.matrix = m
	gv.velocity = vec.vector(0, 0, 0, 0)
end

local function removeCopy()
	if cur and cur.copy then
		pcall(function() cur.copy:delete() end)
		pcall(GameVehicleResource.destroy, cur.copy)
		cur.copy = nil
	end
end

local function spawnCopy(id)
	local hc = START.heading + math.pi / 2
	local ok, c = pcall(function()
		return GameVehicleResource.create({
			SnapToTerrain = true, Model_Id = id, NoOccupants = true, InitialVelocity = 0,
			Position = at(GAP, DROP), Heading = vec.vector(math.sin(hc), 0, math.cos(hc), 0),
		})
	end)
	log(string.format("ram %d: copy %s", id, ok and tostring(c) or tostring(c)))
	return ok and c or nil
end

local function rest(label)
	local ok, err = pcall(function()
		local p, q = cur.gv.matrix, cur.copy and cur.copy.matrix
		local s = string.format("ram %d rest %s car y %.4f up (%.3f %.3f %.3f)", cur.id, label, p[3].y, p[1].x, p[1].y,
			p[1].z)
		if q then
			s = s .. string.format(" copy y %.4f up (%.3f %.3f %.3f)", q[3].y, q[1].x, q[1].y, q[1].z)
		end
		log(s)
	end)
	if not ok then log("ram rest error: " .. tostring(err)) end
end

function M.start(id, gameVehicle, previous)
	disableTraffic()
	if previous ~= nil and previous ~= gameVehicle then
		parked = parked + 1
		pcall(placeCar, previous, -(25 + 9 * parked), START.heading)
	end
	cur = { id = id, gv = gameVehicle, run = 1, phase = "place", ticks = 0 }
	log(string.format("ram %d: start", id))
end

-- Run 1: car at 0 facing forward, nose into the copy at GAP. Run 2: car at 2 GAP facing
-- forward, pushed backwards, tail into the copy.
local function runSetup()
	return (cur.run == 1) and 0 or 2 * GAP, (cur.run == 1) and 1 or -1
end

local function sample()
	if cur == nil then return end
	local gv = cur.gv
	if cur.phase == "place" or cur.phase == "settle0" then
		local along = runSetup()
		pcall(placeCar, gv, along, START.heading)
		if cur.copy and cur.phase == "place" then
			pcall(function()
				local m = setYRotation(START.heading + math.pi / 2)
				m[3] = at(GAP, DROP)
				cur.copy.matrix = m
				cur.copy.velocity = vec.vector(0, 0, 0, 0)
			end)
		end
		return
	end
	if cur.phase ~= "push" and cur.phase ~= "after" then return end
	cur.n = cur.n + 1
	local ok, err = pcall(function()
		local fx, fz = fwd()
		local p, q = gv.matrix[3], cur.copy.matrix[3]
		local v = gv.velocity
		local along = v.x * fx + v.z * fz
		local gap = math.abs((q.x - p.x) * fx + (q.z - p.z) * fz)
		local lateral = (q.x - p.x) * fz - (q.z - p.z) * fx
		local _, dir = runSetup()
		print(string.format("[dsfforza] ram %d %d %d %.3f %.4f %.3f", cur.id, cur.run, cur.n, along, gap, lateral))
		if cur.phase == "push" then
			-- Contact: the parked copy moves (MOVED m from where it stood when the push began).
			-- The car's speed is a poor sign: gameVehicle.velocity reads change only at physics
			-- steps (every ~5 updates), and some cars lose 0.6 m/s a step reversing
			-- (runs vm2-20261029-185122, -190839).
			if cur.q0 == nil then cur.q0 = { x = q.x, z = q.z } end
			local moved = math.sqrt((q.x - cur.q0.x) ^ 2 + (q.z - cur.q0.z) ^ 2)
			if moved > MOVED then
				log(string.format("ram %d %d contact gap %.4f (previous %.4f) copy moved %.3f speed %.3f lateral %.3f",
					cur.id, cur.run, gap, cur.lastGap or -1, moved, along, lateral))
				cur.phase, cur.n = "after", 0
				return
			end
			cur.lastGap = gap
			gv.velocity = vec.vector(fx * SPEED * dir, v.y, fz * SPEED * dir, 0)
		elseif cur.n >= AFTER then
			cur.phase = "next"
		end
	end)
	if not ok then log("ram sample error: " .. tostring(err)) end
end

function M.step()
	if cur == nil then return true end
	cur.ticks = cur.ticks + 1
	if cur.phase == "place" then
		if cur.ticks == 1 and not cur.copy then
			clear()  -- not in run 2: it would take the copy too
			cur.copy = spawnCopy(cur.id)
		end
		local resident = false
		pcall(function() resident = spoolsystem.IsLocationResident(at(GAP)) end)
		if (resident and cur.ticks >= PLACE_MIN) or cur.ticks >= PLACE_MAX then
			cur.phase, cur.ticks = "settle", 0
			pcall(function() cur.gv.velocity = vec.vector(0, -2, 0, 0) end)
			pcall(function() cur.copy.velocity = vec.vector(0, -2, 0, 0) end)
		end
	elseif cur.phase == "settle" then
		if cur.ticks >= SETTLE_TICKS then
			rest("r" .. cur.run)
			if cur.copy == nil then
				log(string.format("ram %d: no copy, skipping", cur.id))
				cur.phase = "done"
			else
				cur.phase, cur.ticks, cur.n, cur.lastGap, cur.q0 = "push", 0, 0, nil, nil
				log(string.format("ram %d %d push", cur.id, cur.run))
			end
		end
	elseif cur.phase == "push" or cur.phase == "after" then
		if cur.ticks >= RUN_TICKS then
			log(string.format("ram %d %d: timeout in %s", cur.id, cur.run, cur.phase))
			cur.phase = "next"
		end
	elseif cur.phase == "next" then
		if cur.run == 1 then
			cur.run, cur.phase, cur.ticks = 2, "place", 0
		else
			cur.phase = "done"
		end
	elseif cur.phase == "done" then
		removeCopy()
		if REAL then
			REAL.start(cur.id, cur.gv, nil)
			cur.phase = "real"
			return false
		end
		cur = nil
		return true
	elseif cur.phase == "real" then
		if REAL.step() then
			cur = nil
			return true
		end
	end
	return false
end

addUserUpdateFunction("dsfforza_ram", function()
	local ok, err = pcall(sample)
	if not ok then log("ram sample error: " .. tostring(err)) end
end, 1)

return M
