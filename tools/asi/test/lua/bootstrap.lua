-- Test bootstrap for dsfforza.asi garage/save runs (tools/asi/test): the
-- repo's test bootstrap plus savetest.lua. run-series -LuaFiles puts it over
-- UserLuaScripts\bootstrap.lua.
local Bootstrapper = {}

function Bootstrapper.Init()
	print("[dsfforza] bootstrap init (asi test)")
end

function Bootstrapper.Launch()
	user_open("dsfforza\\savetest.lua")   -- before register.lua: fill entries count for its group caps
	user_open("dsfforza\\register.lua")
	user_open("dsfforza\\garagetest.lua")
	user_open("dsfforza\\clonetest.lua")
end

function Bootstrapper.Shutdown()
	print("[dsfforza] bootstrap shutdown")
end

return Bootstrapper
