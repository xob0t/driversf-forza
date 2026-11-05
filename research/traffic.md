# Traffic: what decides which cars the player meets

Updated: 2026-11-05

What spawns on the street in free roam, which of it a mod can change, and
how the ported cars get in. This is the ground for the car manager's
traffic settings (design at the end). Evidence tags as elsewhere:
`confirmed-static` (read in data or code), `confirmed-runtime`,
`hypothesis`. Background: `game-lua.md` ("Traffic"), `draw-distance.md`
(spawn distances, traffic counts).

## Summary

| Question | Answer | Evidence |
| --- | --- | --- |
| How many kinds of traffic? | Two. **Ambient traffic** comes from the chapter's traffic set: 15 to 18 models, all kept loaded, about 85 cars within 250 m. **Interesting vehicles (IVs)** are at most 10 other models at once, up to 3 cars of each, spawned 128 to 256 m ahead of the camera. | static (sections 1, 2); runtime census (`draw-distance.md`) |
| Can Lua change the ambient set? | **No.** The sets are AIEX data in `media/san_franciscoAI.fchunk`. Lua only picks one by name. A data patch can only swap an ID in place (`traffic_sets --replace`). | static (`game-lua.md`) |
| Can Lua change the IVs? | **Yes.** The engine takes the 17 region lists from Lua (`SetInterestingVehicleRegions`) on every chapter load. Adding, removing or reordering IDs there decides which models it picks. | static (section 2) |
| Which cars can be IVs? | Any model the spooler can load that isn't already loaded: not in the active traffic set, not the player's car, not already an IV, not excluded, and not of vehicle type 6. Package cars qualify. | static (0x007EB970) |
| Do the two kinds share models? | No, by design. The vanilla IV lists and the story traffic sets are disjoint in every chapter but the Epilogue, where 4 models (173, 179, 258, 274) are in both. A pick of one of those is rejected while the set is loaded. | static (data) |
| Which region applies? | The **first** of the 17 regions, in list order, whose circle (x, z) holds the camera. Regions overlap, so the order matters. | static (0x007EC170) |
| How is a model picked? | One draw per try from that region's list, weighted to the **front**: `idx = round(N * (1 - cos(pi * r / 2)))` with r uniform in [0, 1). The first half of a list gets 2/3 of the draws. | static (0x007EB970, 0x0040B110, 0x00866320) |
| How fast? | A try every 39 frames while fewer than the desired slots are active. After each model it requests, it waits **3600 frames** (60 s at 60 Hz) before the next. | static (0x007ECDF0, 0x007EB8F0) |
| Slot count? | `min(10, active + free spool slots)`, recomputed every 39 frames, so `SetDesiredSlotCount` from Lua is overwritten at once. No Lua calls it. | static (0x007EB860, 0x007EB3E0) |
| Limits | 17 regions; **128 IDs per region list, not checked** (#562); 128 excluded IDs, not checked. | static (0x0067D040, 0x007EB560) |

## 1. Ambient traffic (the traffic set)

`game-lua.md` has the format: 59 named sets in the AIEX chunk, each with
up to five frequency lists of 6 to 27 model IDs. Story free roam uses set
"Chapter N" (Exposition, Epilogue by name), frequency 0, switched in
`spooling.isChapterTrafficSpooled`:

1. `InterestingVehicleManager.Enable(false)`, traffic off;
2. wait for `TrafficSpooler.Busy()` to clear;
3. `civilianTraffic.setTrafficSettingsName(set, frequency)`;
4. wait for `TrafficSpooler.AllTrafficVehiclesLoaded()`;
5. the chapter's mission pot, then `InterestingVehicleManager.Enable(true)`.

Every model of the set is loaded before traffic starts, and traffic spawns
only loaded models. In the VM census about 85 cars are within 250 m
(median 86, p10 77, p90 98; `draw-distance.md`). Lua has no call that adds
a model to the set. `civilianTraffic.SetVehicleModelOverride` exists, but
it has no help text and no caller, and its signature is unknown
(`game-lua.md`, "Open").

The story sets (frequency 0, `traffic-sets.tsv`):

| Chapter | Set | Models |
| --- | --- | ---: |
| Exposition | Exposition | 18 |
| 1 to 7 | Chapter N | 18 each |
| Epilogue | Epilogue | 15 |

**So a port can't be ambient traffic through Lua.** The only route is a
patched AIEX that swaps a vanilla ID for a ported one in place. All the
set's models are then loaded together in the spool's kind-0 slots
(`spool-limits.md`). That is a separate feature, not part of this
design (issue to file).

## 2. Interesting vehicles (confirmed-static)

### Lua bindings

`InterestingVehicleManager`'s function table is at 0x00E12C40 (name, C
function, 0, help). The manager object is `[Engine + 0x6D8]`.

| Lua | C | What it does |
| --- | --- | --- |
| `Enable(b)` | 0x0067CDF0 → 0x007EC220 | `+0x29B0 = b`; off also clears the active slots (0x007EBEE0) |
| `IsEnabled()` | 0x0067CE50 → 0x007EB390 | `+0x29B0` |
| `ChooseDesiredSlotCount()` | 0x0067CE90 → 0x007EB860 | `+0x29A8 = min(10, active + free spool slots)` |
| `SetDesiredSlotCount(n)` | 0x0067CEC0 → 0x007EB3E0 | `+0x29A8 = min(n, 10)`; overwritten by the next update |
| `GetDesiredSlotCount()` | 0x0067CF10 → 0x007EB400 | `+0x29A8` |
| `SetMin/MaxSpawnDistance(m)` | 0x0067CF50, 0x0067CFB0 | `+0x29A0` (128 m), `+0x29A4` (256 m) |
| `ClearVehicles()` | 0x0067D010 → 0x007EBEE0 | releases the active slots |
| `SetInterestingVehicleRegions(t)` | 0x0067D040 | below |
| `ExcludeVehicles(t)` | 0x0067D1E0 → 0x007EB560 | list at `+0x274C`, count `+0x294C` |

**SetInterestingVehicleRegions (0x0067D040).**

- It clears the old regions (0x007EBFF0) and reads up to 17 tables (`cmp edi, 0x11`).
- It reads each table's `position`, `radius` and `vehicleList` into a stack `{u32 ids[128]; u32 count}`.
- It calls 0x007EC880, then 0x007EB860 (slot count).
- **The list loop has no length bound.** Its test is `cmp edi, 0x80` at 0x0067D150, but `edi` is the region counter. The 129th ID lands on `count`. What follows is in #562.

0x007EC880 drops IDs whose vehicle info (0x0086C5B0, `+2`) is type 6. It
stores the region as a 0x21C-byte record at `+0x0C + n * 0x21C`:

```text
+0x000  position (4 floats), radius, debug marker
+0x018  u32 ids[128]
+0x218  u32 count
```

The region count is at `+0x23E8`, so 17 records fit exactly. Every chapter
gives 17.

**ExcludeVehicles (0x0067D1E0).** It copies a list into the same 128-entry
buffer, again without a bound. The manager's list holds 128 too. The game
calls it only for missions that have an `excludedInterestingVehicles` entry
(`instancefunctions.lua`), and clears it with an empty list when the mission
ends. A mod that excluded models this way would lose them at the next
mission. Editing the region lists doesn't have that problem: they come back
with every chapter load.

### The update (0x007ECDF0, called from the engine update at 0x00538714)

While enabled, the manager decrements a counter each call and works every
39th call (`+0x2998 = 0x26`):

1. **Region** (0x007EC170): it walks the records in order and takes the
   first whose radius² is greater than the squared (x, z) distance to the
   position 0x0059F270 returns (the player's or the camera's; the census
   below computes it from the player's car and agrees). When the region changes, 0x007EC060
   releases the active models that the new region's list doesn't have.
2. **Slots** (0x007EB860): desired = `min(10, active + free spool slots)`.
3. **New model**, if the cooldown `+0x299C` is 0 and active `+0x23F8` <
   desired: 0x007EB970 picks one (below). 0x007EB8F0 asks the spooler for
   it (0x005A7860). On success the cooldown is set to **0xE10 = 3600 calls**,
   and a slot record is created (0x007ECEC0, 0x007ECFB0).
4. **Slots** (0x007ECB40), per slot:
   - state 1, requested: once loaded (0x005A6EA0) it moves to state 2;
   - state 2, live: spawn one more car of the model, if the slot has room
     (3 cars; below) and 0x007EC430 finds a place. A slot moves to state 3
     once a car of it is within 40 m of the view position (0x007EB740,
     0x007A79C0; 1600 = 40²): the player has met it;
   - state 3: the model is released (0x005A78D0); the record is freed once
     its cars are gone.

The cooldown counts calls, so frames. At the VMs' 60 Hz
(`car-packages.md`, "frame time") it is 60 s per new IV model, and a try
every 0.65 s.

**Spawning (0x007EC430).** It takes candidate lane positions from the
traffic system (0x0076F520, buckets of 40 m), keeps those 128 to 256 m from
the camera (`+0x29A0`, `+0x29A4`) and ahead of it (dot product with the view
direction ≥ 0.7, 0x00DE22D0), and picks one at random. The car is made by
0x007EBB20 and handed to a traffic lane carrier (0x00770220). From then on it
drives like ambient traffic.

**Cars per model.** A slot record (0x54 bytes) carries a pool of three
16-byte car nodes (0x007ECEC0: storage from `+0x14` to `+0x44`; 0x007EC240,
0x007EBEE0 walk them as {prev, next, vehicle, marker}). So an IV model has
at most 3 cars at once, and the IVs together at most 30. That is a third of
the ambient count at most, and usually far fewer.

### The pick (0x007EB970)

```c
n   = region->count;
r   = rng();                                   // 0x00866320: lagged Fibonacci, u32 * 2^-32, [0, 1)
f   = sin((r + 1) * pi / 2);                   // 0x0040B110 is sin(x) (cos Taylor series of x - pi/2)
idx = min(n - 1, round(n * (1 - f)));          // = round(n * (1 - cos(pi * r / 2)))
id  = region->ids[idx];
```

`u = 1 - cos(pi * r / 2)` has `P(u < x) = (2 / pi) * acos(1 - x)`. Its
density is infinite at 0 and falls to 2/pi at 1. Index 0 takes
`P(u < 0.5 / n)`, and the last index takes everything from `(n - 1.5) / n`
up. For a list of 20:

| index | 0 | 1 | 2 | 3 | 4 | 9 | 18 | 19 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P | 0.143 | 0.105 | 0.074 | 0.061 | 0.053 | 0.038 | 0.032 | 0.048 |

Then the pick is **rejected** (the call returns -1, nothing is requested,
and there is no cooldown) when the model:

- is already an active IV (`+0x2744` list);
- is the player's car;
- is on the exclude list;
- is already loaded or requested (0x005A6EA0: the traffic set's models
  `+0x330`, or a loaded spool request with a reference);
- has vehicle type 6.

So once the front of the list is active, the accepted picks move down the
list. There is one region per position, so the region's list order alone
decides the mix.

### What this means for added cars

- **Where in the list a car goes changes how often it comes.**
  `register.lua` appends the ported IDs after the vanilla ones, where the
  weights are lowest (about 0.032 against 0.05 per entry on average in a
  list of 20). Share of the draws for k IDs after n vanilla IDs, before
  rejections:

  | n vanilla | k = 1 | 3 | 6 | 12 | 24 |
  | ---: | ---: | ---: | ---: | ---: | ---: |
  | 9 | 0.10 | 0.19 | 0.29 | 0.41 | 0.53 |
  | 22 | 0.04 | 0.09 | 0.15 | 0.24 | 0.36 |
  | 45 | 0.02 | 0.05 | 0.08 | 0.14 | 0.23 |
  | 86 | 0.01 | 0.03 | 0.05 | 0.08 | 0.14 |

  Spread evenly through the list, each added ID gets about the list's
  average weight, `1 / (n + k)`, like a vanilla entry.
- **A list can't pass 128** (#562). Room left per region is `128 - n`, as
  low as 18 in the Epilogue.
- **A model in no region never comes.** With more than 17 × 24 = 408
  traffic cars, `register.lua`'s spread `(index + k) % m == 0` leaves cars
  out. Of 721 cars, 330 are in no region.

### The vanilla lists (confirmed-static, `ScriptsChapterN.fchunk` `traffic\trafficSpoolingData.lua`)

The 17 centres and radii are the same in every chapter (`game-lua.md`).
List lengths per chapter and region:

| Chapter | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Exposition | 9 | 9 | 2 | 9 | 9 | 5 | 5 | 4 | 6 | 4 | 5 | 3 | 1 | 1 | 4 | 1 | 2 |
| Chapter1 | 22 | 21 | 7 | 23 | 23 | 11 | 13 | 11 | 10 | 10 | 12 | 6 | 1 | 6 | 9 | 5 | 7 |
| Chapter2 | 28 | 27 | 8 | 29 | 29 | 13 | 17 | 14 | 11 | 13 | 16 | 9 | 3 | 9 | 12 | 6 | 9 |
| Chapter3 | 44 | 43 | 9 | 44 | 45 | 19 | 26 | 22 | 18 | 24 | 28 | 13 | 5 | 15 | 19 | 12 | 10 |
| Chapter4 | 52 | 52 | 9 | 51 | 53 | 23 | 28 | 27 | 18 | 30 | 30 | 14 | 8 | 23 | 20 | 17 | 11 |
| Chapter5 | 60 | 61 | 8 | 59 | 62 | 26 | 35 | 31 | 24 | 36 | 37 | 19 | 11 | 24 | 26 | 19 | 11 |
| Chapter6 | 74 | 77 | 11 | 72 | 76 | 38 | 43 | 38 | 30 | 39 | 43 | 21 | 12 | 33 | 32 | 31 | 15 |
| Chapter7 | 81 | 86 | 15 | 81 | 80 | 40 | 46 | 40 | 29 | 42 | 46 | 24 | 15 | 38 | 35 | 35 | 19 |
| Epilogue | 110 | 104 | 18 | 100 | 100 | 64 | 49 | 39 | 65 | 68 | 51 | 23 | 14 | 53 | 37 | 47 | 18 |

No list repeats an ID. Distinct IV models: 9 in the Exposition, then 24,
30, 48, 59, 69, 86 and 97 in Chapters 1 to 7, and 110 in the Epilogue. The
designers insert a chapter's new models anywhere in the earlier lists, not
only at the front or the end, so a list's order is not a popularity ranking
that a rule could copy.

## 3. In game

Runs: `run-series.ps1 -Vm`, the vanilla archive, the three-package overlay
of run `vm2-20261105-155016` (IDs 1 Alfa 8C, 2 Audi R8 GT, 3 Aston DB5),
dsfforza.asi with `packages=1`. A research copy of `register.lua`
(`-LuaFiles`) reads a prototype `dsfforza\traffic.lua` and logs a census
every 10 updates: every model ID within 250 m
(`findVehiclesInTraffic`, no script cars or cops), the player's position,
and the region the engine's rule picks. `-ProbeIds` are six Chapter 7 set
models, with `-Traffic`. The probe stands at the dyno start (-819, 1836),
which is in region 9.

Settings, in the prototype `traffic.lua`:

- ID 1 `traffic = false`;
- ID 2 pinned to region 9, the probe's region;
- ID 3 pinned to region 13 (-2092, 5084), 3.3 km away.

The second run also keeps no vanilla entry in region 9. Each run took
38 or 39 census samples over about 100 s of `g_NetworkTime` with the world
up. All 18 set models were seen in both. The player was in region 9 in
every sample. Neither run crashed.

| | `vm1-20261105-210733` ("pinned") | `vm1-20261105-211108` ("replace") |
| --- | --- | --- |
| Region 9 as sent | 29 vanilla + ID 2 at the end | ID 2 alone |
| IV models seen (in no traffic set) | 191, 238, 233: **positions 5, 3 and 0** of region 9's list, first seen at 27, 48 and 75 s | **ID 2 only**: from 15 s, up to 2 cars at once, gone from 34 s, back from 83 s to the end |
| ID 1 (off) | never | never |
| ID 3 (region 13) | never | never |
| ID 2 | never | seen |
| Other non-set models | 265, 269, 271, 280 | 269, 271, 280 |

Confirmed-runtime:

- **The engine uses the lists `register.lua` sends.** An ID taken out of the
  lists never comes, and a region list holding only a port spawns that
  port, with no vanilla IV in its place. So a list can **replace** vanilla
  IVs, not only add to them.
- **The region is the one holding the player.** Run "pinned" saw only IV
  models of region 9's list. A port pinned to a region the player isn't in
  never came.
- **The front of the list wins.** The three vanilla IVs of the run were
  among the first six of 29 entries. Uniform picks would put three in the
  first six with probability (6/29)³ ≈ 0.009. ID 2 at the end never came.
- Car 2 was gone from 34 s, after the probe had moved the player on, and
  back at 83 s. That is consistent with a slot released after the player
  got close (state 3), then a new request after the cooldown. Not measured
  more closely.

265 is in the "Free Drive" and "Chapter 7" mission pots; 269, 271 and 280
are in "All chapters". Pots keep models loaded (`game-lua.md`), and these
models drove among the traffic though no region lists them. Which system
puts them on the road (cop patrols, `FelonyVehicleSpawnManager`, for the
cop models) is not decoded. They are in no region list, so the IV settings
don't touch them.

Command (one series, both runs):

```powershell
.	ools\wsbun-series.ps1 -Vm -Archives outanilla\Install\dngvehicles.sp,outanilla\Install\dngvehicles.sp `
  -Labels pinned,replace -Overlay out	raffic-vm\ovl-pinned,out	raffic-vm\ovl-replace `
  -Asi out	raffic-vmsi\dsfforza.asi -LuaFiles 'dsfforzaegister.lua=out	raffic-vmegister-research.lua' `
  -ProbeIds '170,201,291,286,185,173' -Traffic -ShotSeconds 3 -WaitSeconds 600
```

The plugin is master's (lua.packages, #511) with `packages=1`. The
overlays are run `vm2-20261105-155016`'s three packages plus each run's
`traffic.lua`.

**An older run doesn't show what it seemed to.** The "id 1 x1 / id 2 x1"
counts of `vm2-20261105-155016` are the probe's own cars: the player was
driving IDs 1 to 3 then. So they aren't evidence that IVs spawn ports. The
only earlier evidence is run `20261022-025601` (`in-game-tests.md`, "Fix4":
a 306 counted while the probe had only spawned 266).

## 4. Design: traffic settings in the car manager

Everything below is about the IV lists, the one part Lua can change. The
engine's behaviour is unchanged: the same 10 slots, the same 60 s cooldown,
the same pick. The settings decide only what the 17 lists hold.

### Settings (in `manager.json`, keyed by package key)

```json
"traffic": {
  "cars": {
    "forza.fh1.ALF_8C_08": {"traffic": false},
    "forza.fm4.AUD_TT_07": {"regions": [6, 7, 9]}
  },
  "mix": {"share": 0.25},
  "vanilla": {"off": [226, 233]}
}
```

- `cars.<key>.traffic`: false keeps the car out of every list. The car stays
  in the garage. Missing means true.
- `cars.<key>.regions`: the regions the car always goes in (1 to 17).
  Missing means automatic.
- `mix`: how many automatic entries go into each region. Either `share` (the
  ports' fraction of the list, 0 to 1; 1 means only ports), or `per_region`
  (a count, the old `regionCars`). No `mix` means `per_region` 24, the old
  default, so an install that doesn't touch the setting keeps its share.
- `vanilla.off`: vanilla model IDs taken out of every list. Only IV models
  can be listed; the manager refuses a traffic-set model, because Lua can't
  remove it (section 1).

Settings stay when a car is disabled, upgraded or removed, like its ID, so
a reinstall gets them back. They live in manager.json because install.ps1
and uninstall keep that file (`INSTALL.md`).

### What the game reads: `Common\UserLuaScripts\dsfforza\traffic.lua`

The manager writes `traffic.lua` with every change, from the state and the
enabled cars. It is keyed by model ID, as `packages.lua` is:

```lua
-- Written by dsff car_manager; do not edit.
return {
	cars = { [1] = { traffic = false }, [7] = { regions = { 6, 7, 9 } } },
	mix = { share = 0.25 },
	vanilla = { off = { 226, 233 } },
}
```

It is a file of its own, so a traffic change doesn't regenerate the GUI
copies (gui.sp alone is 80 to 160 MB) or `packages.lua`. It takes effect at
the next game start: register.lua reads it once and applies it on every
chapter load.

### register.lua

- It reads `traffic.lua` only when `ASI.packages` is true, together with
  `packages.lua`. With the plugin off the game stays vanilla, with no car
  added and no vanilla car removed (#511).
- `injectRegions(regions)` rebuilds each region's list. It starts from the
  vanilla list minus `vanilla.off`, keeping the vanilla order, and keeps
  that unchanged list for the next chapter load, since the wrapper edits
  `spooling.interestingVehicles` in place. Then it works out how many ports
  the region takes:
  - `quota = per_region`, or `ceil(share / (1 - share) * n)` for
    `n` vanilla entries; with `share = 1`, the vanilla entries are dropped
    and the quota is `128`;
  - capped at `128 - n` (#562) and at the number of traffic cars.
- Pinned cars go first, into their own regions, and count against the
  quota. The automatic cars fill what is left, dealt round-robin over the
  regions. Each car goes into the regions with free quota, in a stable order,
  wrapping round. So with room enough (17 quotas summed ≥ cars), every car
  is in at least one region. When there is less room, the deal starts at an
  offset that changes each game start (from `os.time` if the game's Lua has
  it; to be checked), so every car comes up in some session.
- Placement: the ports are spread evenly through the vanilla entries
  (entry `i` of `k` at position `round((i + 0.5) * (n + k) / k)`). Each port
  then gets about the list's average weight under the engine's pick
  (section 2). Appending them at the end, as now, gives them the lowest
  weights, which nothing in the data asks for.
- A list is never more than 128 entries. Missing or bad settings fall back
  to the defaults and log a line.

### CLI (`dsff car_manager traffic ...`)

```text
traffic status                             per car: in traffic or not, pinned regions; mix; vanilla off
traffic off|on <selector>...               selector: a key, make:<MAKE>, game:FH1|FM4, dlc, all
traffic pin <selector>... --regions 6,7,9
traffic unpin <selector>...
traffic mix --share 0.25 | --per-region 24 | --default
traffic vanilla [--chapter N]              which vanilla models a chapter uses: its traffic set and
                                           its IV lists, per region
traffic vanilla off|on <id>...             IV models only
traffic regions [--chapter N]              the 17 regions: centre, radius, vanilla count, room (128 - n)
```

`dlc` matches the FM4 cars in `tools/batches/fm4-dlc.txt`, since the
packages don't record their pack. `status` also shows the chapter's lists as
`register.lua` would build them, by running the same rule in Python (tested
against the Lua with lupa), so a player can check a setting without starting
the game.

The vanilla tables (traffic sets and IV lists per chapter) are facts about
the game's data: model IDs only, no assets. They go in the repo as
`research/iv-regions.tsv`, next to `traffic-sets.tsv`, written by a
`dsff traffic_sets --regions` from the decompiled chapter scripts.

There is no interactive manager GUI (`car_gui.py` writes the garage
pictures, badges and plates). So the surface is the CLI, and
`install.cmd`'s menu if it gets one.

### Not in this design

- **Ports in ambient traffic.** That needs a patched AIEX served by
  dsfforza.asi, and a check of the spool budget for 15 to 18 loaded models
  (section 1). It would be an enhancement issue of its own.
- **More IVs or a shorter cooldown.** That would mean patching the engine's
  10 and 3600. Vanilla behaviour is the target, so they stay.
