-- cfg_biggroup_first.lua (41 vanilla cars under Abarth, first in the maker list) plus 20 fake
-- makers "DSFT nn" of one car each (entries 1..20, free in the vanilla roster, copies of 266, unlocked and
-- owned; register.lua drops entries above 303 that are not ports), so the maker list holds more than
-- the 40 groups the menu script declares and the plugin grows its lists. The keys never highlight them.
return {
	merge = { into = "ID:242743", from = { "ID:242753", "ID:242757", "ID:242755" } },
	fill = { first = 1, count = 20, perMaker = 1, own = 1 },
	delay = 15,
}
