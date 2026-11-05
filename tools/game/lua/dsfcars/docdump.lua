-- Dumps the game's Lua API documentation (every library, function and
-- property the engine registers, with help text) into Driver.log. Uses the same engine hooks as DriverNGHook's
-- driverNGDocsUtil.lua: temporarily replaces the LuaDocumentation_* callbacks,
-- then asks the engine to collect.

local rows = {}

local saved = {
	LuaDocumentation_Initialise = LuaDocumentation_Initialise,
	LuaDocumentation_AddLuaLibrary = LuaDocumentation_AddLuaLibrary,
	LuaDocumentation_AddLuaFunctionDocumentation = LuaDocumentation_AddLuaFunctionDocumentation,
	LuaDocumentation_AddLuaPropertyDocumentation = LuaDocumentation_AddLuaPropertyDocumentation,
}

local function clean(text)
	return (tostring(text or ""):gsub("[\r\n\t]+", " "))
end

LuaDocumentation_Initialise = function() end
LuaDocumentation_AddLuaLibrary = function(library)
	table.insert(rows, { "library", clean(library), "", "" })
end
LuaDocumentation_AddLuaFunctionDocumentation = function(library, name, docs)
	table.insert(rows, { "function", clean(library), clean(name), clean(docs) })
end
LuaDocumentation_AddLuaPropertyDocumentation = function(library, name, docs)
	table.insert(rows, { "property", clean(library), clean(name), clean(docs) })
end

local ok, err = pcall(function() Documentation.collectLuaDocumentation() end)

for name, fn in pairs(saved) do
	_G[name] = fn
end

if not ok then
	print("[dsfforza] docdump failed: " .. tostring(err))
	return
end

-- The game's io can't write files (its file layer is virtualised under COM:),
-- so print the rows; tools/wsb/guest/run-test.ps1 extracts "[dsfapi]" lines
-- from Driver.log into dsfforza-lua-api.tsv.
for _, row in ipairs(rows) do
	print("[dsfapi]\t" .. table.concat(row, "\t"))
end
print(string.format("[dsfforza] docdump printed %d rows", #rows))
