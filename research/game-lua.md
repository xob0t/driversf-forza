# The game's Lua: how vehicles are registered and used

Updated: 2026-10-24

Evidence tags as elsewhere: `confirmed-static` (read in data or decompiled
code), `confirmed-runtime`, `hypothesis`.

## Summary

| Question | Answer | Where |
| --- | --- | --- |
| What builds the base traffic? | Named traffic sets ("progressions") in `media/san_franciscoAI.fchunk`, not Lua. Lua only picks a set by name. | AIEX sections 0x118/0x120 |
| Which traffic set is active? | Story chapter N uses "Chapter N" (frequency 0); missions and activities name their own. | `missionprogression.lua`, `challengeprogression.lua`, `activitiesprogression.lua` |
| Per district? | 17 "interesting vehicle" regions (centre, radius, model list), one list per chapter. The engine spools these and mixes them into traffic. | `ScriptsChapterN.fchunk` `traffic\trafficSpoolingData.lua` |
| Why didn't `RequestTrafficVehicles` add 304? | It only loads models. Traffic spawns from the active set's list plus the interesting-vehicle regions. | (hypothesis, consistent with run `20261021-230611`) |
| HUD name and badge | `vehicleStats[id]`, read by the engine through the Lua global `getVehicleStats(id)` | `vehiclestats\vehicleStats.lua`, `VehicleStatFunctions.lua` |
| Garage list | Engine menu code reads `getVehicleStats(id)` fields `Selectable`, `Tier`, `Price`, `Picture1..3`, plus the save's unlocked/owned flags. Felony Rework adds cars to the garage by editing only `vehicleStats`. | strings in `Driver.exe`, Felony Rework diff |
| Unlocks | `vehicleManager.updateVehicleUnlocks` walks `vehicleStats` for `UnlockedInChapter`, `UnlockedInGarage`, `UnlockByActivity` and calls `ProfileSettings.SetVehicleUnlocked`. | `gamelogic\vehiclemanager\data\vehicleData.lua` |
| Shift (zap) | No model list. Any vehicle `zapcontroller.IsVehicleZappable` accepts. | `gamelogic\zap\system\zapModes.lua` |

## Script packages

`media/Scripts*.fchunk` are CHNK files (`tools/chnk.py`):

```text
SCRR  Script Package Root (nested CHNK)
  SCRH  u32 version (1), u32 count, u32 checksum, u32 flags (PC: 2)
  SCRC  count x {u32 offset into SCRS, u32 hash, u32 unused}
  SCRS  compiled chunks, back to back
BINF  UTF-16 build info ("Packaged by NCT-Robot ... Script Packager v1.8.1.0")
```

The hash is a CRC-32 variant of the lowercased `COM:luaScripts\<path>`:
the reflected 0xEDB88320 table indexed by the complemented byte, with the
top byte of each entry flipped and no final complement (`com_hash` in
`tools/lua_scripts.py`; it matches all 2,095 scripts). The path comes from
the main function's debug source name,
`@r:\resources\luascripts\common\\<path>`, where the doubled backslash marks
the package root. Two scripts were packaged under a different name than
their source (`competitivefilelist.lua` and `splitscreenfilelist.lua` both
hash as `filelist.lua`), and one SplitScreen script is named
`..\..\common\gamelogic\onlinescreenmanager\ss_menu.lua`; the extractor
strips the `..`.

### Bytecode (confirmed-static)

Plain Lua 5.1 bytecode, no encryption and no custom opcodes. The only
difference from a stock x86 `luac` is byte 10 of the header,
`sizeof(lua_Number)`:

```text
1b 4c 75 61 51 00 01 04 04 04 04 00   2,088 scripts: lua_Number is a 4-byte float
1b 4c 75 61 51 00 01 04 04 04 08 00   ScriptsTutorials (7 scripts): 8-byte double
```

So the game's Lua numbers are single-precision floats: integers above 2^24
lose precision. unluac reads both widths without a patch. All 2,095 chunks
decompile with no errors.

`media/ScriptsLuaScripts.fchunk` on this install is a 2021 repack without
BINF; `ScriptsLuaScripts - Copy.fchunk` is the 2012 original. They differ
only in `unlockables.lua` (the repack also unlocks the Uplay challenges).

### Reproduce

```powershell
python tools\lua_scripts.py extract-all "D:\Games\Driver San Francisco\media" out\lua
# unluac 1.2.3.561 built from the official source mirror (MIT), JDK 21, Idle priority:
$java = "C:\Program Files\Microsoft\jdk-21.0.12.101-hotspot\bin\java.exe"
$p = Start-Process $java -ArgumentList '-cp','_ref\downloads\unluac\unluac.jar','tools\unluac\BatchDecompile.java','out\lua','out\lua-src' -NoNewWindow -PassThru
$p.PriorityClass = 'Idle'; $p.WaitForExit()
```

`_ref/downloads/unluac/src` is a clone of github.com/viruscamp/unluac
(mirror of the SourceForge hg repo, which Cloudflare blocks for curl);
`unluac.jar` was compiled from it with `javac`. A whole run takes about two
minutes. Output: `out/lua/<Package>/<path>.luac` and
`out/lua-src/<Package>/<path>.lua`; `LuaScripts.copy` is the stock core
package. Felony Rework's packages are in `out/lua-felony-src/`.

## Traffic

### Traffic sets live in the AI data (confirmed-static)

`civilianTraffic.setTrafficSettingsName(name, frequency)` (no help text, so
it is missing from `lua-api.tsv`) selects a set. The engine string
"Unable to set progression '%s'. The progression was not recognised in the
city data." names them progressions. The names are not stored as text: the
AIEX chunk in `media/san_franciscoAI.fchunk` keys them by the same CRC
variant as the scripts, case-sensitive and without the `COM:` prefix.

```text
AIEX payload (file offset 0x1080)
  +0x50   section directory, {u32 count, u32 offset from AIEX}
  0x118   59 progressions, 28 bytes each:
            u32 name hash, u16 start[5], u16 count[5], u32 index
  0x120   1,369 u16 model IDs; each set's lists are slices of this array
```

`python tools/traffic_sets.py --models` prints all 59 sets, and
`research/traffic-sets.tsv` has one row per set and frequency. 54 names
were recovered from string constants in the decompiled scripts, Felony
Rework and the snoopii mod menu; five hashes are still unnamed (C071E02A,
9CB17769, 11DF09F4, 40CF70C9, CC9F35B5). Each frequency list has 6 to 27
IDs, usually 16 to 18. Examples:

| Set | Frequencies | Models (frequency 0) |
| --- | --- | --- |
| Exposition | 1 | 128 143 148 167 170 176 179 185 227 231 251 255 258 272 281 286 291 298 |
| Chapter 1 | 1 | 124 128 144 148 167 170 176 179 185 231 251 258 272 281 286 287 291 298 |
| Chapter 2 LFK | 1 | includes 266 (Desanne) |
| Challenge traffic | 4 | `trafficFrequencyOverride` 0..3 picks one |
| Online Traffic * | 1 to 3 | `onlineMissionSync.progressionSets`, ID = set*10 + frequency |

`frequency` picks one of up to five lists. Story free roam passes none, so 0.
Lua changes sets in `spooling.isChapterTrafficSpooled` (disable traffic,
wait for `TrafficSpooler.Busy()` to clear, `setTrafficSettingsName`, wait
for `TrafficSpooler.AllTrafficVehiclesLoaded()`). `RequestTrafficVehicles`
is never called from Lua.

A set's list can't be extended from Lua. A data mod can replace an ID in
place: the arrays are packed back to back with only 7 spare slots after the
last one, so growing a list means moving AIEX sections (not attempted).

```powershell
# Neon (179) -> 304 in every set and frequency: 21 places, 42 bytes
python tools\traffic_sets.py --replace "*:*:179=304" --out out\san_franciscoAI.179to304.fchunk
```

### Interesting vehicles: per district, per chapter (confirmed-static)

Each chapter package carries `traffic\trafficSpoolingData.lua`:

```lua
module("spooling", package.seeall)
interestingVehicles = {
  { position = vec.vector(-2049.446, 259.2407, -3208.7026, 1), radius = 1300,
    vehicleList = { 182, 189, 143, ... } },
  -- 17 regions
}
```

The 17 regions are the same in every chapter; only the lists grow (9 IDs in
Exposition, 24 in Chapter 1, 97 in Chapter 7, 110 in the Epilogue).
`requestChapterDataToBeLoaded` (`loading\chapterLoadingFlow.lua`) →
`loadingSystem.requestInterestingVehicleSettings` →
`InterestingVehicleManager.SetInterestingVehicleRegions(spooling.interestingVehicles)`.
The manager fills spare spool slots (`ChooseDesiredSlotCount`) with models
from the region the player is in and spawns them among traffic.
`spooling.excludedInterestingVehicles[missionName]` feeds
`ExcludeVehicles` during missions. This is the Lua-reachable way to put a
model into ambient traffic.

| # | centre (x, y, z) | radius |
| --- | --- | --- |
| 1 | -2049, 259, -3209 | 1300 |
| 2 | 1346, 181, -3695 | 1836 |
| 3 | 953, 83, -1415 | 600 |
| 4 | -2968, 56, -1373 | 550 |
| 5 | 3614, 300, -378 | 2000 |
| 6 | 833, 119, 1775 | 970 |
| 7 | -130, 106, 50 | 960 |
| 8 | -1252, 111, 1012 | 512 |
| 9 | -904, 232, 2345 | 810 |
| 10 | -2261, 164, 1849 | 550 |
| 11 | -2591, 122, 339 | 945 |
| 12 | -604, 254, 4006 | 850 |
| 13 | -2092, 643, 5084 | 715 |
| 14 | -4033, 150, 3986 | 1024 |
| 15 | -2323, 308, 3269 | 815 |
| 16 | -3912, 134, 1885 | 1024 |
| 17 | 1354, 220, 3758 | 1024 |

### Mission vehicle pots (confirmed-static)

`LuaScripts\gamelogic\spoolingsystem\data\trafficSpoolingData.lua` defines
`spooling.missionSpecificVehiclePots`. On each chapter load
`spooling.isMissionVehiclesSpooled` requests `"All chapters"` (62, 269, 271,
280, 123) plus the chapter's pot through `TrafficSpooler.RequestMissionVehicle`
and releases everything else (cap 18). Felony Rework adds cop car 267 to
`"All chapters"` to keep it resident. A pot only keeps a model loaded; it
doesn't put it on the road.

### Other ways into traffic (hypothesis, untested)

- `civilianTraffic.AddVehicleToCarrier(gameVehicle, onRemoved)` hands a
  script-spawned car to the traffic system. Exposition part 3 spawns
  "Taxi wander" actors and does exactly this; `onRemoved(gameVehicle,
  reason)` fires when traffic lets go of it.
- `GameVehicleResource.swapWithModel(gameVehicle, modelId)` replaces a live
  vehicle's model; Survival uses it on script cars. On a traffic car it
  might give a new model that keeps driving as traffic.
- `civilianTraffic.SetVehicleModelOverride` / `ResetVehicleModelOverride`,
  `GetLaneTrackAndVehicleIDs`,
  `GetCivilianTrafficVehicleForGivenLaneTrackAndVehicleIndices` exist but
  have no help text and no Lua caller; signatures unknown.
- `GameVehicleResource.SetCivilianPot("Free Drive Pot 0N")` is not about
  cars: the pot hashes are in `character_global.fchunk` (drivers and
  pedestrians).

## vehicleStats, HUD, garage and unlocks

`LuaScripts\vehiclestats\vehicleStats.lua` defines 146 entries (62..303).
Spool IDs without an entry: 123 (trailer), 131, 277, 289. Six entries have
`Selectable = false`: 172, 266, 267, 268, 302, 303. Fields:

```text
Manufacturer   badge index          Model, Year, Cylinders, Power*, Torque*, Weight*, Drivetrain
ManufacturerName, ModelName  "ID:nnnnnn" localisation strings
Picture1/2/3   GUI image names (garage, large and small icons)
Price, Tier    garage shop          Selectable  garage list
UnlockedInChapter | UnlockedInGarage | UnlockByActivity   unlock rule
Speed, Strength, Drift   0..1 bars  OnlineGroup, UPlay, UniqueKey
```

`VehicleStatFunctions.lua` defines the global `getVehicleStats(id)`. The
engine calls it: `getVehicleStats`, `Selectable`, `Picture1` and the stat
field names sit together in `Driver.exe`'s strings next to
`CategorySelect_Vehicles`, `GarageShop_ModelName`, `purchaseVehicle`,
`garageSpawnVehicle`. No Lua code reads `Selectable`, `Tier`, `Price`,
`Picture1` or the stat bars, so the menus read them from C++.

Felony Rework's readable `vehicleStats.lua` is the stock file plus:
`Selectable = true` and `UnlockedInChapter = 10` for 172 and 266-268 (plus
a 5000 price for 266-268); a full entry for 277 (prison van, copied from
118); 302 and 303 selectable with prices; dummy entries 622 and 623 (not
models) with `UnlockByActivity` set to two of its new activities. Its
changelog credits this to "Klancnik's garage mod". So a `vehicleStats`
entry with `Selectable = true` and an unlock is enough for a stock model to
show up in the garage. The engine walks a fixed ID range, not the table:
FUN_006C57D0 loops IDs 0..303 (`gui-assets.md`, `asi.md`), so the dummies
622 and 623 are never listed.

Unlock flow, all in Lua:

- `vehicleManager.updateVehicleUnlocks(chapter, activity, garageId)`
  (`vehicleData.lua`) sets `ProfileSettings.SetVehicleUnlocked(id)` for
  every entry whose `UnlockedInChapter`, `UnlockByActivity` or
  `UnlockedInGarage` matches, then queues the unlock panel. Chapter loads,
  activity completion and garage purchases call it.
- Buying in the garage shop calls the global `purchaseVehicle(id)` →
  `ProfileSettings.SetVehicleOwned(id)`.
- Picking a car calls `_G.garageSpawnVehicle(locationName, modelId, shader)`
  → `Garages.SpoolVehicleModel(id)`, wait for
  `TrafficSpooler.IsPlayerVehicleLoaded()`, `vehicleManager.spawnVehicle`.
  `largeVehicleList` (`garageSystem.lua`) picks the large garage cutscene.
- `garage.locations` (`gamelogic\garages\data\data.lua`) has 10 garages;
  `starBuy` is the featured model per garage.
- `ProfileSettings.Get/SetVehicleUnlocked/Owned` keep flags in the save
  (layout and the 304-slot / 320-bit limits: `asi.md`, "Garage and save").

Other model ID lists, for reference: `unlockables.lua`
(`unlockableVehiclesTable`, Uplay rewards), `onlineProgressionCarData.lua`
(online packs), `felony_patrollingVehicleManager.lua`
(`FelonyVehicleSpawnManager.AddModelType`, cop models), mission
`actorPool` entries (`modelID` per actor), dare goals, and
`impactTrafficSystem_luckyEscape.lua`.

## Load order and where a mod hooks in

`loadfiles.lua` loads `VehicleStats\VehicleStats.lua` and
`VehicleStatFunctions.lua` in the initial load, before `launchScripts()`.
DriverNGHook's `game_autoexec.lua` calls `Bootstrapper.Launch()` from its
`launchScripts` wrapper, before the game's own, so `vehicleStats`,
`InterestingVehicleManager` and `spooling.missionSpecificVehiclePots` all
exist there. Chapter data (`spooling.interestingVehicles`) arrives later,
on every chapter load, which is why the mod wraps the setter instead of
editing the table once.

DriverNGHook's `_open` also loads `Common\LuaScripts\<path>` from disk when
the file exists, which is how Felony Rework replaces whole scripts. A
runtime patch from `UserLuaScripts` is smaller and doesn't fight other
mods.

## The mod: `tools/game/lua/dsfforza/register.lua`

Loaded by both bootstraps (the test one in `tools/game/lua/` and the player one `tools/install.ps1` installs). The bootstrap does:

```lua
function Bootstrapper.Launch()
	user_open("dsfforza\\register.lua")
end
```

It reads optional overrides from `dsfforza\config.lua` (`register = { ... }`,
including `cars`). The core of each part:

(3) HUD name and badge: copy a stock entry, override names and bars.

```lua
local entry = {}
for k, v in pairs(vehicleStats[266]) do entry[k] = v end
entry.UnlockedInChapter, entry.UnlockedInGarage, entry.UnlockByActivity = nil, nil, nil
entry.ManufacturerName = "Nissan"        -- or an existing "ID:nnnnnn"
entry.ModelName = "370Z (Forza)"
entry.Speed, entry.Strength, entry.Drift = 0.62, 0.45, 0.7
vehicleStats[304] = entry
```

(2) Garage: make it selectable; unlocking writes the save.

```lua
vehicleStats[304].Selectable, vehicleStats[304].Tier, vehicleStats[304].Price = true, 2, 5000
ProfileSettings.SetVehicleUnlocked(304)   -- only with writeProfile = true
ProfileSettings.SetVehicleOwned(304)
```

(1) Traffic: add the ID to every interesting-vehicle region as the engine
receives them.

```lua
local original = InterestingVehicleManager.SetInterestingVehicleRegions
InterestingVehicleManager.SetInterestingVehicleRegions = function(regions, ...)
	for _, region in ipairs(regions) do
		table.insert(region.vehicleList, 304)
	end
	return original(regions, ...)
end
```

The script guards against double-wrapping when DriverNGHook reloads the
bootstrap, skips IDs already in a list, and re-sends
`spooling.interestingVehicles` if a chapter is already loaded. Options:

| Option | Default | Effect |
| --- | --- | --- |
| `interesting` | true | the wrapper above |
| `missionPot` | false | add the IDs to `missionSpecificVehiclePots["All chapters"]` |
| `swapTraffic` | false | every 5 s, swap one civilian traffic car 70-200 m away to the new model (`swapWithModel`) |
| `carrier` | false | every 5 s, spawn the car 120 m ahead on a lane and `AddVehicleToCarrier` |
| `swapMax` | 1 | stop swapping or spawning while this many are within 200-250 m |
| `garage` | true | `Selectable = true` for IDs below 304 while the garage maker list has room (`research/batch.md`) |
| `writeProfile` | false | set unlocked and owned in the save, `UnlockedInChapter = 1` (garage-listed IDs only) |
| `guardProfile` | true | drop Lua `SetVehicleUnlocked`/`Owned` calls for added IDs above 303 (`research/batch.md`) |
| `regionCars` | 24 | spread more added cars than this over the regions |
| `probe` | false (true with a test `config.lua` that has `ids`) | log `vehiclesAvailable`, `Vehicles`, profile flags, `civilianTraffic` functions and nearby traffic counts every 10 s for about 3 minutes |

It is tested against stubbed APIs in an embedded Lua 5.1 (lupa,
`tools/asi/test/test_register_asi.py`) and runs in game in every harness run
and install (e.g. run `20261022-230004`, `asi.md`).

## Open

1. Whether the interesting-vehicle manager spawns a model that isn't in
   the active traffic set. The stock lists mix set members and non-members,
   so probably yes.
2. Answered: the garage loops IDs 0..303 and the save holds flags for IDs
   below 304 (320 bits); dsfforza.asi lifts both (`asi.md`).
3. Answered: plain strings don't render; the HUD takes glyph codes from the
   numeric `Manufacturer`/`Model` fields and the garage `ID:nnnnnn` strings
   (`gui-assets.md`).
4. Signatures of `civilianTraffic.SetVehicleModelOverride` and friends.
5. Whether the engine checks `san_franciscoAI.fchunk` (size, hash) before
   loading a patched copy.
