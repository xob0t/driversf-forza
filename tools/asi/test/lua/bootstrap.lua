-- Test bootstrap for dsfcars.asi garage/save runs (tools/asi/test): the
-- repo's test bootstrap plus savetest.lua. run-series -LuaFiles puts it over
-- UserLuaScripts\bootstrap.lua.
local Bootstrapper = {}

function Bootstrapper.Init()
	print("[dsfcars] bootstrap init (asi test)")
end

function Bootstrapper.Launch()
	user_open("dsfcars\\savetest.lua")   -- before register.lua: fill entries count for its group caps
	user_open("dsfcars\\register.lua")
	user_open("dsfcars\\garagetest.lua")
	user_open("dsfcars\\clonetest.lua")
end

function Bootstrapper.Shutdown()
	print("[dsfcars] bootstrap shutdown")
end

return Bootstrapper
