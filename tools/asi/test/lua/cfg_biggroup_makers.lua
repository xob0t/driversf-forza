-- 41 listed vanilla cars in one maker group, first in the maker list: Chevrolet, Ford and Dodge filed under
-- Abarth, first in the list (it sorts by the raw ManufacturerName, the vanilla ones "ID:nnn"), so the
-- menu keys reach it without Down presses a busy host may drop. Plus 20 fake makers "ZDSFT nn",
-- last in the list, of one car each (IDs 63..82, free in the vanilla roster, copies of 266, unlocked and owned;
-- register.lua drops entries above 303 that are not ports): 52 groups, past the 40 the menu script
-- declares, so the plugin grows its maker lists. The keys never open them (no model of those IDs).
return {
	merge = { into = "ID:242743", from = { "ID:242753", "ID:242757", "ID:242755" } },
	fill = { first = 63, count = 20, perMaker = 1, own = 1, prefix = "ZDSFT" },
	delay = 15,
}
