-- One maker of 30 locked cars (IDs 800..829, no side-file entries in the test save) sold at
-- the Downtown garage, which the test save has open: the maker menu checks each of them
-- for card i of the group (issue #181: past card 20 it read the screen out of bounds).
return { fill = { first = 800, count = 30, perMaker = 30, own = 0, garage = 2 }, delay = 15 }
