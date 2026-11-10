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
| Can Lua change the ambient set? | **No.** The sets are AIEX data in `media/san_franciscoAI.fchunk`. Lua picks one by name; `RequestTrafficVehicles` replaces what is loaded but not what spawns. | static (section 1b) |
| Can a port be ambient traffic? | **Yes**, through a patched copy of the AI file that dsfforza.asi's redirect list serves. Weights per road (208 weight tables over 191 model columns) decide each car's model. A port in a vanilla model's column takes its place; a new column takes the share its weights give it. | static (1b); runtime (3b): 9.8 % and 20.9 % as set |
| What limits ambient ports? | The 35 kind-0 spool slots. The set loads unchecked; past the budget the chapter's mission pot is refused and **the load hangs** (no crash). Added ports per story chapter: 1 to 4 (Exposition 11, Epilogue 14); replacing costs nothing. | static; runtime (3b) |
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

**So Lua alone can't make a port ambient traffic.** The weights that pick each car's model are in the AI file too. Section 1b decodes them and the way in (a patched copy served by dsfforza.asi's redirect list); section 3b shows it in game.

## 1b. Ambient traffic in Driver.exe (confirmed-static)

Section 1 has the summary; this is the decode.

**Selecting a set.** `civilianTraffic.setTrafficSettingsName` (0x0064D390,
registered by 0x0064DC40) hashes the name (0x00BA6DE0) and calls 0x0076FDF0
on the traffic manager `[Engine + 0x758]`:

1. It finds the progression record by hash (0x0040A780: a 61-bucket hash
   table that the AI loader builds from section 0x118).
2. It stores the hash and frequency at `+0x5CA840` and `+0x5CA844`.
3. It copies the frequency's u16 IDs from section 0x120 (`[AI + 0x124]`) into
   a **40-entry stack buffer, with no bound**.
4. It passes the list to **0x005A79F0, the spooler's traffic list**.

0x005A79F0 copies the list to spooler `+0x000`. Room is 50 u32 with the
count at `+0xC8`, and there is no bound here either. It then runs 0x005A7490:

- every listed model not yet loaded is requested (0x0059DB40, 0x0059DB60);
- every model of the old list that isn't in the new one is released;
- the resident traffic models are kept in the list at `+0x330`.

`TrafficSpooler.RequestTrafficVehicles(t)` (0x0066E560) calls the same
0x005A79F0, so from Lua it **replaces** the traffic spool list. It changes
what is loaded, not what spawns (below).

**Picking a car's model.** The traffic system spawns a car (0x0077ADA0) with
the model from 0x00916870:

```c
if (manager->model_override != -1) return manager->model_override;   // +0x5CA9F0
rng.seed(vehicle_seed);                    // per carrier: the same car in the same place
x = rng() * range->total;                  // range = {u32 start, u32 count, f32 total}
for (i = range->start; i < range->start + range->count; i++)
    if ((x -= pairs[i].weight) < 0) return pairs[i].model;   // pairs: {u32 model, f32 weight}
return 0;
```

`SetVehicleModelOverride(id)` (0x0064D920) sets that override, so it turns
**every** traffic car into one model, and `ResetVehicleModelOverride` puts it
back. It is no use for a mix.

**Which weights a road uses** (0x0077DA40, 0x00916510): a road segment's
record for the active set is section 0x128 entry `(set.index & 0xFF) +
road.u16[+8]`, 0x14 bytes. Byte `+0x0D + frequency` of it is the range index
(section 0x158). The ranges point into section 0x160's `{model, weight}` pairs
(`[0x016DA940]`, set by the AI loader 0x0040A7C0 from `AI + 0x164`).

**The data** (`media/san_franciscoAI.fchunk`, `AIRT` → `NONR` → `AIEX`,
`AIRF`; AIRF is route-finding data, floats; scratch script `aipatch.py`):

| Section | Count | Record | Meaning |
| --- | ---: | --- | --- |
| 0x118 | 59 | 28 B: hash, start[5], count[5], index | progressions (sets) |
| 0x120 | 1369 | u16 | each set's spool list per frequency |
| 0x128 | 18821 = 59 × 319 | 0x14 B | per (road, set): byte `0x0D + f` = range |
| 0x158 | 208 | {start, count, f32 total} | weight ranges |
| 0x160 | 39728 = 208 × 191 | {u32 model, f32 weight} | 191 model columns, the same in every range |

- **The non-zero models of a set's roads are exactly its spool list.** This
  holds for every set and frequency checked (Exposition, Chapters 1 and 7,
  Epilogue, Challenge traffic f0 to f3; `aipatch.py` checks all of them).
  So the spool list says what is loaded, and the weights say what spawns
  on which road, by district.
- Chapter 7's roads use 16 different ranges, the Epilogue's one. 62 of the
  191 columns have zero weight everywhere: IDs 1, 5, 9, 12, 21, ... 303, a
  model list from development.
- **Ranges are shared.** 82 of the 149 ranges in use belong to more than one
  set. A story set mostly shares with its own mission and chase variants:
  Chapter 7 with 9CB17769, Chapter 3 with Challenge traffic heavy, Chapter 4
  f1, Chase traffic 3 and two unnamed sets, Chapters 6 and Epilogue with none.
  A weight change in a range affects every set that uses it, so a model given
  weight there must join each of those sets' spool lists.
- Range indices are bytes: 208 of 256 are used, so at most 48 ranges can be
  cloned to make a change apply to one set only.

**The budget: kind-0 spool slots.**

- `TrafficSpooler.GetMaxSpooledVehicles()` (0x0066E480 → 0x0059C5C0 →
  0x005A0AD0) is the SSLP header byte `+0x18 + kind`: 35 for kind 0 in the
  vanilla archive.
- A mission or IV request (0x005A6FD0) is refused when `traffic + mission +
  pending + 1 ≥ max`.
- **The traffic list is not checked.** 0x005A7490 requests every model of
  the set. 0x0059D740 takes a free request handler from the pool at
  `+0x6FDA0` and calls through it without a NULL check (`(*piVar13 + 0x44)`
  with `piVar13 = 0`), so a request with no free handler would read address
  0 (hypothesis until a run shows it).
- Every resident model is one kind-0 slot: the traffic set's 15 to 18, the
  IVs (`min(10, active + free)`), the mission pots and the player's car. A
  port that **replaces** a set model keeps the count. A port **added** to a
  set takes one slot, first from the IVs, whose desired count follows the
  free slots.
- Per model, ports fit the slot. A kind-0 entry's NONR has to fit 0x6D000
  (ports: 0x58D10). Ports' kind-0 VEHC are 1.59 MB median and 2.11 MB at the
  most, against 1.31 to 1.57 MB for the Chapter 7 set's models (25.6 MB for
  all 18). All are below the 2.5 MB static spool buffer. Graphics pool slots
  hold about 1800 render objects (`spool-limits.md`), and the ports are
  within vanilla's range.

**Ways in, compared.**

- **Patched copy, served by dsfforza.asi's redirect list (chosen).** The car
  manager writes `dsfforza\generated\san_franciscoAI.fchunk` from the game's
  current file, as it writes gui.sp, and lists it in `redirect.txt`. The
  plugin's redirect is generic for any file opened through the file layer
  (FUN_00857370 → CreateFileA at 0x0085AC1E; `mod_archive.cpp`), so no
  plugin code changes. With the plugin off the game reads its own file,
  which matches #511 (no packages, no ports). The patch is plain data, with
  everything checked offline and a self-check that no set weights a model it
  doesn't load. It takes effect at the next game start.
- **Runtime patch of the loaded tables.** The plugin would hook 0x0040A7C0
  or 0x0076FDF0, rebuild the pairs and spool lists in memory, and repoint
  `[0x016DA940]` and `[AI + 0x124]`. Same data work, plus hooks, plus the
  plugin parsing the settings. Its one gain is rotating ports per session
  without rewriting the file, and that can come later.
- **Lua.** `RequestTrafficVehicles` replaces the spool list, but the
  weights stay as they are, so the added models would load and never spawn.
  `SetVehicleModelOverride` makes all traffic one model. Neither does it.

**Writing the copy.** The grown tables (spool IDs, pairs) go past the old end
of AIEX, and only their directory entries change, so no other byte inside
AIEX moves. AIEX grows by a multiple of 0x800. The fields after it shift by
that much:

- the AIEX size;
- the AIRF offset;
- the NONR CHNK total and its entry size in AIRT;
- the AIRT total and its entry size in the file;
- the file total.

The repo's generic CHNK writer is not byte-exact on this file: it aligns the
NONR total to 0x431300 and moves NONR's name. So the copy edits the fields in
place instead.

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
.\tools\wsb\run-series.ps1 -Vm -Archives out\vanilla\Install\dngvehicles.sp,out\vanilla\Install\dngvehicles.sp `
  -Labels pinned,replace -Overlay out\traffic-vm\ovl-pinned,out\traffic-vm\ovl-replace `
  -Asi out\traffic-vm\asi\dsfforza.asi -LuaFiles 'dsfforza\register.lua=out\traffic-vm\register-research.lua' `
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

## 3b. Ambient traffic in game (confirmed-runtime)

One series, three runs (`vm1-20261105-214006`, `-214317`, `-214635`).
Setup:

- the vanilla archive and the 24-package overlay of run
  `vm1-20261104-184809` (ports at IDs 1 to 24);
- dsfforza.asi with `packages=1`;
- a patched `san_franciscoAI.fchunk` written by `aipatch.py` and listed in
  the overlay's `redirect.txt` as
  `media\san_franciscoAI.fchunk|dsfforza\generated\san_franciscoAI.fchunk`.

The prototype `traffic.lua` keeps every port out of the IV regions, so any
port seen comes from ambient traffic. The probe is the same as in section 3
(Chapter 7, region 9). Each run took 40 census samples.

| Run | Patch | Result |
| --- | --- | --- |
| replace136 | model 136 → port 10 in every spool list and weight column (in place) | Port 10 was **9.8 %** of all cars seen within 250 m, up to 12 at once. 136 never appeared. In the vanilla-set run `vm1-20261105-210733`, 136 was 9.9 % |
| add2 | ports 3 and 18 as new columns, 20 % of the weight of Chapter 7's 16 ranges; added to the spool lists of Chapter 7 and 9CB17769 (20 models each) | Port 3 **11.6 %**, port 18 **9.3 %**: 20.9 % together, against the 20 % set. Desired IV slots 10 → 9 |
| exhaust14 | 14 ports added the same way (32 models) | **The chapter load never finished**, with no crash: `waitingForTrafficSettings`, `g_NetworkTime` 0, no traffic, IVs never enabled, and the probe couldn't take a car |

Also seen in every run:

- the plugin logs `'media\san_franciscoai.fchunk' opened as
  ...\dsfforza\generated\san_franciscoAI.fchunk`: the AI file goes through the
  redirected file layer;
- `GetMaxSpooledVehicles()` returns 35;
- the game's Lua has **no `os` table** (`os: nil`), so register.lua can't
  rotate anything per session by the clock;
- about 80 cars within 250 m (median 79 to 80, max 99 to 100), as in vanilla;
- the Lua log's two "Script file not found" lines, which every vanilla run has
  too.

What this confirms:

- A port can be ambient traffic. A port in a vanilla model's column takes
  exactly that model's place.
- A new column gets the share its weights give it, as the pick in section
  1b predicts.
- **Running out is a hang, not a crash.** 0x005A7490 loads the whole set
  unchecked. Then the chapter's mission pot is requested through
  `RequestMissionVehicle`, which refuses at `traffic + mission + pending + 1
  ≥ 35` (0x005A6FD0). `spooling.isMissionVehiclesSpooled` waits for those
  models forever (the hang is confirmed; this cause is read from the code).

**Budget per story chapter** (hypothesis from the code; bracketed by +2
working and +14 hanging in Chapter 7). The traffic list may hold at most
`35 - 1 - pot` models, where `pot` is "All chapters" (5) plus the chapter's
own pot (`spooling.missionSpecificVehiclePots`) and the 1 is the player's
car. Models that can be added on top of the vanilla set:

| Exposition | Ch. 1 | Ch. 2 | Ch. 3 | Ch. 4 | Ch. 5 | Ch. 6 | Ch. 7 | Epilogue |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 11 | 4 | 1 | 2 | 2 | 4 | 1 | 3 | 14 |

Removing a vanilla model from a set frees one. Phase 2 checks the boundary:
Chapter 7 with +3 should load, with +4 it should hang. Every IV model the
IVs lose is a slot the added models take, which is the 10 → 9 above.

## 4. The spool slot limit (confirmed-static; runtime in 5)

What the 35 is, what is sized by it, and how dsfcars.asi raises it
(`[archive] vehicle_slots`, `mod_archive.cpp`, "vehicle spool slots"). Ini comments and code point here as "The spool slot limit".

**The 35 is data.** It is the vehicle archive's SSLP header, bytes
`+0x18 + kind`: 35, 2, 2, 0 for kinds 0 to 3 (`dsff vehicle_limits`).

- At package init, FUN_005A1420 makes one slot handler per slot. Each is a
  SceneModelType2 from FUN_0059F9C0, with its NONR buffer
  (`_aligned_malloc(SSLP +0x08, 0x80)`, 0x6D000 = 436 KiB, CRT heap;
  `spool-limits.md`).
- `TrafficSpooler.GetMaxSpooledVehicles()` (0x0066E480 → 0x0059C5C0 →
  0x005A0AD0) returns the byte. Every resident model takes one kind-0 slot,
  whatever loaded it.
- A mission or IV request is refused at `traffic + mission + pending + 1 ≥
  max` (0x005A6FD0).
- The traffic list is loaded without that check (0x005A7490).
- `spooling.requestToSpoolVehicle` ignores the refusal: it adds the model to
  `vehiclesRequestedForSpool` anyway, and `isMissionVehiclesSpooled`'s
  stepper waits until every listed model is loaded. Its warning ("ran out of
  spool slots") is only for its own cap of 18 (`numberOfVehicleAllowed`).
  That is the exhaust14 hang of 3b: no warning in the log, a load that never
  ends.

**What is sized by the slot count:**

| What | Size | Bound checked? | Effect of more slots |
| --- | --- | --- | --- |
| Slot handler arrays in the spool object: `+0x5630` (all kinds, count `+0x5730`) and `+0x5734 + 0x104 k` (per kind, count at `+0x100`) | 64 pointers each | **no** (FUN_005A1420, FUN_005A2230) | Kind 0 can have at most 64 − (2 + 2) = **60**. That is the ceiling short of code patches |
| ID-list node pools in the spool object, `{next free, free count, used}` at `+0x5B44`, `+0x5F5C` (64 nodes), `+0x6374`, `+0x668C`, `+0x69A4`, `+0x6CBC`, `+0x6FD4` (48 nodes), 16-byte nodes, storage from `+0x0C` (constructor 0x005A1B40) | 64 / 48 | **no**: 0x007D7270 bumps the next-free pointer by 0x10 and never checks the end | They hold requested and resident IDs of every kind. 60 + 4 slots would pass 48, so the plugin points them at buffers of 1024 nodes (16 KiB each) while they are still unused |
| Graphics pool 9 "Vehicle Spooled" (rows 0x00FD2618, 0x00FD272C; GraphicsManager_Construct 0x00BAD960 reads them once) | 35 slots: 8192 KiB SYS, 65536 nodes, 35 KiB GPU, 1024 GPU nodes, split evenly | FUN_00BB5690 clamps a slot index to `count − 1` | More spool slots than graphics slots would share the last one (render-object exhaustion, the #540 crash path). The plugin scales the row so each slot keeps its vanilla size: 239,664 B and 1872 nodes |
| The vehicles' resource providers, the provider table's entries `0x17..0x1D` (FUN_0059F9C0, package type 0): each slot handler's scene model takes one wrapper from each (SceneModelType2_InitializeResourceProviders 0x00858CD0) | 7 providers: | | |
| ↳ provider 1 (acquire 0x0086BEE0, vtable 0x00E395D0) and 3, 4 (acquire 0x0086B710, vtable 0x00E3966C) | 45-node pools of 12 B (`{next, free count, used}`, list head after them), appended by 0x0086B6B0 | **no** | Node 46 overwrites the list head and the class ID. The plugin moves a full pool's bump pointer to a heap chunk |
| ↳ provider 2 (acquire 0x0048F7D0, vtable 0x00DDA058) | the same pool with 46 nodes at `+0x04` (head `+0x238`), appended inline | **no** | One node is taken before the vehicle slots, so the 46th vehicle wrapper overwrites the head: the #566 crash. The plugin replaces the acquire with one through the same chunk append |
| ↳ provider 2's wrappers: class 0x7A (creator 0x0048FB40, 0x30 B, a sound bank wrapper of vtable 0x00E36D38 at `+0x0C`) from the class factory's object pool 0x16 | 46 objects (`push 0x2E` at 0x0048F85D and 0x0048FBB8) | **no**: the object pools (`[0x01448BF8]`, 0x18 B each: base, next, free count, size, capacity, used; 0x00869D50) bump with no end check, and ClassOf (0x00869E30) and the free (0x00869DA0) work from the base | The 47th object is made over the next heap block (provider 3). The plugin makes the pool with `46 + (slots − 35)` |
| ↳ provider 5 (vtable 0x00E18184) | 45 inline wrapper pointers (`+0x04`, count `+0xB8`) | **no** | The plugin keeps the list (any length) and mirrors the first 45 |
| ↳ provider 6 (MODL, vtable 0x00DDA2C8, acquire 0x00493FD0) | 157 records made in place (fill 0x00494150: `push 0x9D`), a stack of pointers to them (`+0x4FC`, count `+0x770`); shared with the other spool objects that use the provider | **no**: the pop has no empty check, the push no full check | At 60 slots the 63rd vehicle handler finds the stack empty and pops the record count as a record. The plugin hands out records made as the fill makes them once the stack is empty |
| ↳ provider 0 (acquire 0x00BACD70 → 0x00BABEB0, vtable 0x00E982B8) | a new 0x98 B wrapper each (game operator new), noted in the first empty entry of 128 at `+0x210` (limit at `+0x410`) | **yes**: a full array just doesn't note it | About 40 entries are taken before the vehicle slots (`slots_trace`: the first vehicle slot's lands at `+0x2B0`), 64 handlers bring it to about 104 |
| NONR buffer per slot | 436 KiB, at package init | n/a | 25 more slots: 10.9 MB of heap at start |
| Model data (VB, IB, textures; D3D9 managed pool) | per resident model | n/a | Only while a slot is in use: ports 1.59 MB median kind-0 entry, vanilla traffic 1.41 MB |
| Wwise bank | in place in the slot's NONR buffer (`engine-sound.md`); its structures in Wwise's default pool (`AkInitSettings.uDefaultPoolSize` 0x599999 at 0x0084A64D, created by AK init; the pool table at `[0x01D72390]`, 0x4C B each, used bytes at `+0x44`) | AK's allocator fails when full | 41 slots, 27-model set: peak 4971 of 5731 KiB. 60 slots, 40-model set: **full** (5990 to 6048 KiB counted, block headers included, from 30 s on). The plugin gives the pool each slot's vanilla share, `ceil(0x599999 × slots / 35)` |
| The sound manager's bank list (FUN_00849870, `+0x92DC`) | 50 nodes (the 45-node pools' layout) | **no** | Banks join it when they load, not at package init. Under 20 seen at 60 slots; the plugin moves it to heap chunks when full |
| Traffic spooler lists (0x005A6D20): traffic request list (`+0x000`, 50, count `+0xC8`), resident traffic (`+0x330`, 50 nodes of 12 B), mission (`+0x998`, 50 nodes of 20 B) | 50 each | **no** | Not reached: a set has at most 40 models (next row), the mission list at most 18 + 10 IVs |
| The set selector's copy (0x0076FDF0) | 40 IDs on the stack | **no** | **A set may hold at most 40 models**; the car manager enforces it |

**Not sized by it:**

- Vehicle instances: the traffic vehicle store holds 500 cars
  (`draw-distance.md`), and the number of cars on the road depends on the
  road set, not on how many models exist.
- The draw queue (16384 draws a frame, #185, `[render]`): the same number of
  cars draws about the same, since ports are within vanilla's per-car draw
  range.
- Physics and AI per instance.
- The slot index (`model + 0x564`, set at 0x00858864, read at 0x00859162):
  only the scene model reads it, and only for the graphics pool.

**The patch** (`[archive] vehicle_slots`, default 50, 35 = vanilla, at most
`64 − (kind 1 + 2 + 3 slots)` = 60):

1. From DllMain, before the game builds its pools: graphics pool 9 gets N
   slots at `ceil(vanilla × N / 35)` SYS KiB, nodes and GPU nodes, and N KiB
   GPU (GraphicsManager_Construct asserts `GPU KiB % slots == 0`). The bytes
   are checked against vanilla first.
2. Also from DllMain: the scene-model provider structures that section 5
   found sized for about 45 handlers in all get unbounded storage in the
   plugin: provider 0x00E18184's wrapper array, the 45-node pools appended by
   0x0086B6B0, the class-0x7A provider's 46-node pool (0x0048F7D0), the MODL
   provider's 157 records (0x00493FD0, 0x00494090), and the sound manager's
   bank list (0x00849870). Object pool 0x16 is made with `46 + (slots − 35)`
   and Wwise's default pool with each slot's vanilla share (the table above).
3. The SSLP load (0x005A0B50; the packages' hook, or the plugin's own when
   there are no packages): for the vehicle spool object (package type 0 at
   `+0x7300`) whose header still says 35, it moves the seven ID pools
   (each checked unused) and sets the kind-0 byte to N.
4. Every other reader of the byte follows by itself:
   `GetMaxSpooledVehicles`, the mission and IV budget, and
   `ChooseDesiredSlotCount`.
5. The car manager's AI copy is used only while the game will have the slots
   it was planned for (#583). The manager records the count beside the copy
   (`dsfcars\traffic\san_franciscoAI.fchunk.slots`). The game opens the AI
   file before the SSLP is loaded (`vm1-20261108-025033`: 0.6 s before), so the
   plugin predicts the outcome: its own count when every init patch of the
   raise landed and the vehicle archive's SSLP (read from
   `Install\dngvehicles.sp` at the first open) has the vanilla 35, else 35. A
   copy whose record asks for more, or the copy with no record while the slots
   aren't raised, isn't served; the game reads its own file and the plugin
   logs `redirect: ... not used: written for N vehicle slots, the game has M`.
   The ID pool check can't fail before a vehicle has loaded; should the SSLP
   load still refuse after the copy was served, the plugin logs it as an error.
   `[archive] slots_fail=1` makes the raise fail for tests:
   `vm1-20261108-032357` (copy written for 50) loaded the vanilla sets
   (`max 35`, only vanilla models in the census) and drove 200 s;
   `vm1-20261108-032859` (the same, raise working) served the copy at 50.

**Why 50, and the hang.** 60 is the most the slot handler arrays hold, and
every structure section 5 found sized for fewer is handled up to it. 60 loads
and drives; but with a 40-model set (what 60 gives every story chapter) the
game crashed in 2 of 4 Chapter 7 runs as the player entered the world (section
5, open in #566). 50 (Chapter 7: 36 models) passed every run, so it is the
default: the most tested, not a derived limit. Past 60 the
arrays (about 90 instructions address them in place) and the set selector's
40-ID buffer would both have to move, and no set gains from it: the budget
below reaches the selector's 40 at 59 slots for the largest pot (18). The
chapter load can still hang if a set leaves the mission pot no room, so the
car manager writes no set past the budget of 3b. That budget is
`set ≤ slots − 1 − pot`, and at most 40 from 0x0076FDF0.

The bound is conservative. In run `vm1-20261105-230205`, Chapter 7 with
+10 at 41 slots (28 models; the formula allows 27) loaded and drove. A pot
model that is already resident (the player's car, 62 in "All chapters")
probably costs no request. The plugin doesn't check the AI file itself.

## 5. Raising the slots in game (confirmed-runtime)

All runs on VM 1:

- the vanilla archive and the 24-package overlay (renamed to `dsfcars\`);
- dsfcars.asi with `packages=1`, `read_log=1`, `crash_log=1`;
- a research copy of `register.lua` with no port in the IV regions and the
  census of 3b;
- Chapter 7, the probe at the dyno start.

**First attempts crashed at package init, and each crash named one more
fixed-size structure.** All are sized for the vanilla 35 + 2 + 2 = 39 slot
handlers, with a little to spare, and none checks its bound. Every handler's
scene model takes one wrapper from each resource provider
(SceneModelType2_InitializeResourceProviders 0x00858CD0, the call at
0x00858E07):

| Run | `vehicle_slots` | What failed | Fixed by |
| --- | --- | --- | --- |
| `vm1-20261105-222408` | 60 | Fault reading at 0x006AE19B, ECX 0x2D, from 0x00858E09 ← 0x0059FA87 ← 0x005A1496. Provider class with vtable 0x00E18184 keeps its wrappers in a **45-pointer inline array** (`+0x04`, count `+0xB8`): acquire 0x006AE160, release 0x006AE1B0, lookup 0x006ADF90, clear 0x006AE140 | the four replaced; the list kept in the plugin, the first 45 mirrored inline |
| `vm1-20261105-223116` | 60 | Fault in the class factory 0x0086A2AD, class ID 0x292E8970, from 0x0086BEF7. Two generic provider classes (acquire 0x0086BEE0, pool at `+0x18`, class ID `+0x248`; acquire 0x0086B710, pool at `+0x04`, class ID `+0x234`) list wrappers in **45-node pools** of 12 bytes appended by 0x0086B6B0 (no end check; node 46 overwrites the list head and the class ID) | 0x0086B6B0 replaced: the bump pointer moves to a heap chunk before it reaches the list head |
| `vm1-20261105-223748`, `-224316`, `-225305` (50), `-225627` (45) | 60, 60, 50, 45 | Fault reading `NONR size + 4` at 0x0084BF49: the provider loop calls `+4` on an object with vtable 0x00E36D38, a vehicle's sound bank wrapper (0x18 bytes, FUN_0084C0A0), so an entry of the vehicles' provider list has been overwritten. At 45 slots the read is 0x1004, during the kind-1/2 slots that come after kind 0; at 60, 0x6D004 | found in the rows below (#566). The sound manager's bank list (FUN_00849870) wasn't it: banks join it when they load, not at package init |
| `vm1-20261107-211718` | 45 | `[archive] slots_trace` (the call at 0x005A1491 to FUN_0059F9C0 traced; the vehicles' range is the provider table's entries `0x17..0x1D`, 7 providers, filled during the first vehicle slot). Provider 2 (acquire 0x0048F7D0, class-0x7A wrappers) keeps its wrappers in a **46-node pool** at `+0x04` (storage `+0x10`, list head `+0x238`) and appends inline, not through 0x0086B6B0. One node is taken before the vehicle slots, so at the 45th kind-0 slot its bump pointer reaches the list head, and the next wrapper (the first kind-1 slot: NONR 0x1000, the 0x1004 read of #566) is written over it | 0x0048F7D0 replaced: the same function with the append through `pool_room` |
| `vm1-20261107-215725` | 60 | The same fault with ESI = provider 3. The class-0x7A objects (creator 0x0048FB40: 0x30 bytes, a sound bank wrapper of vtable 0x00E36D38 at `+0x0C`) come from **object pool 0x16** of the class factory, made with **46** objects (push 0x2E; push 0x16; call 0x0086A070). The object pools (table at `[0x01448BF8]`, 0x18 bytes each: base, next, free count, size, capacity, used; allocator 0x00869D50) bump with no end check, so the 47th object is made over provider 3, the next heap block, and the init loop calls provider 3's vtable +4 as a sound wrapper's | pool 0x16 made with `46 + (slots − 35)`, at both of its creations: the provider's constructor 0x0048F840 and its class creator 0x0048FB90 (the one the table's provider comes from); the plugin also logs any object pool that runs past its end (0x00869D50 replaced by the same code plus the check) |
| `vm2-20261107-231034`, `vm1-20261108-000458` | 60 | The detector: pool 0x16 still had 46 objects (only the constructor's call site was patched; the class creator's is the one that runs) | the second site |
| `vm1-20261108-015739` | 60 | All 64 handlers' slots made up to the 63rd; fault reading 0x9D at 0x00858E0B. Provider 6 (MODL, vtable 0x00DDA2C8, creator 0x00494240, 0x774 bytes) makes **157 records** in place (fill 0x00494150: push 0x9D) and stacks pointers to them (`+0x4FC`, count `+0x770`); acquire 0x00493FD0 pops with no empty check, so the empty stack pops its record count | acquire and release replaced: past the 157, records made as the fill makes them (game operator new; vtable 0x00DDA2B0) and kept in the plugin when the stack is full |
| `vm1-20261108-020144` | 60 | Loads and drives (vanilla traffic, no packages): all 64 handlers, the MODL provider past its 157 records, no object pool past its end | |
| `vm1-20261108-021206` | 60 | Chapter 7, 40-model set: loads, drives, starts the mission. Wwise's default pool (5731 KiB) is **full** from 30 s on (5990 to 6048 KiB counted, block headers included); 41 slots peaked at 4971 | the pool gets each slot's vanilla share, `ceil(0x599999 × slots / 35)` (0x0084A64D) |
| `vm1-20261108-020511`, `-021803` | 60 | Chapter 7, 40-model set (the second with the Wwise pool raised): fault reading 0x17B / 0x13B at 0x00AB607F, at the zap into the world (game time 0.04 s). FUN_00AB5EB0, the "[2D Zone]" page pass, walks 17 entity lists (`[this+0x280]` vtable +0x24) and calls +0x98 on an entity whose vtable is 0xE3 / 0xA3. Not reproduced in `-021206` and `-022007` (same set), nor at 41 or 50 | **open** (#566) |

**41, 50 and 60 in Chapter 7** (VM 1, 2 to 8 % competing host CPU at
launch; the 24-package overlay with the car manager's default for that slot
count; the probe driving six cars, 130 to 140 s of game time;
`launchMission(56)`, GetawayActivity4, at 100 s, which started each time;
every 10 s `[archive] read_log` window of the probe pooled):

| Run | Slots | Chapter 7 set | fps | mean frame | ≥ 33.3 ms | ≥ 50 ms | peak private | Wwise default pool peak |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `vm1-20261108-020730` | 41 | 27 (9 ports) | 56.8 | 17.6 ms | 10.0 % | 1.0 % | 1155 MB | 4971 of 5731 KiB |
| `vm1-20261108-022429` | 50 | 36 (18 ports) | 58.6 | 17.1 ms | 5.8 % | 0.3 % | 1195 MB | 5827 of 8188 KiB |
| `vm1-20261108-022818` | 50 | 36 (18 ports) | 58.3 | 17.1 ms | 5.9 % | 0.4 % | 1194 MB | 5839 of 8188 KiB |
| `vm1-20261108-022007` | 60 | 40 (22 ports) | 58.2 | 17.2 ms | 5.8 % | 0.5 % | 1212 MB | 6164 of 9827 KiB |

- More slots cost no frame time; the set's models are drawn as before.
- Memory: about 40 MB more at 50 slots, 57 MB at 60.
- Wwise's default pool grows with the set: at 50 it reaches 5839 KiB, past the
  vanilla 5731, so the pool raise is needed at 50 too.
- The sound manager's bank list stayed under 20 entries in every run.

**41 works** (`vm1-20261105-224648`, `vehicle_slots=41`): 45 handlers of 64,
with the wrapper list and pool fixes in place. Both are needed even here: the
log shows the 0x00E18184 list and three 45-node pools passing 45. It ran
Chapter 7 with **9 ports added** (27 models; 30 % of the road weight):

- it loaded, and the probe drove six cars in turn, 20 s of throttle each,
  for about 140 s of game time, with no crash;
- ports 1 to 9 were 30.1 % of all cars within 250 m (2.5 to 4.2 % each),
  against the 30 % set;
- median 100 cars within 250 m (driving);
- frame times (`[archive] read_log`, every 10 s window of the probe pooled):
  56.6 fps, mean 17.7 ms, 9.5 % of frames 33.3 ms or more, 1.1 % 50 ms or
  more. (A single window, 17.5 ms median and 24.3 ms p95, was quoted here
  before.) The probe's route had about 100 cars within 250 m, twice the
  later runs', and it ran on the other VM, so these numbers don't compare
  with the default's below;
- peak private memory 1148 MB, virtual 1351 MB. The 35-slot runs of 3b were
  1108 to 1121 MB and 1332 to 1334 MB;
- desired IV slots 8 (fewer free slots, as expected).

41 was the default until #566: the most that had loaded and run, before
what breaks between 46 and 49 handlers was found (the class-0x7A provider's
pool, above). With 41 slots the budget formula (3b) gives each story chapter
6 more models:

| Exposition | Ch. 1 | Ch. 2 | Ch. 3 | Ch. 4 | Ch. 5 | Ch. 6 | Ch. 7 | Epilogue |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 17 | 10 | 7 | 8 | 8 | 10 | 7 | 9 | 20 |

(added models = `slots − 1 − pot − set`, at most `40 − set` from the set
selector's buffer.)

**Mission start with a patched set** (`vm1-20261105-230536`, 41 slots,
Chapter 7 +9). The research Lua called `launchMission(56)` (GetawayActivity4:
traffic set Chapter 7, ChaseActivities pot) at 40 s of game time. The game
loaded and started it ("Create mission GetawayActivity4", `forceStartMission`,
instance 7). The census went on to 108 s with ports among the cars; no crash.

**+10 at 41** (`vm1-20261105-230205`): loaded and drove, one model past the
budget formula (above).

## 6. The car manager's traffic settings (as built)

The code: `src/dsfforza/game/traffic_ai.py` (the AI tables, the edits, the
copy), `src/dsfforza/build/traffic.py` (the settings, the plan, the budget,
the files), `car_manager.py` (`traffic` commands), `car_manager_gui.py` (the
window, `cars.cmd`) and `register.lua` (interesting vehicles). The owner
decided:

- ports are only added, never put in place of vanilla models;
- the weights get a mode;
- changes reach the mission and chase variants that share a set's tables;
- ports are re-dealt when the file is written and on Shuffle, not per
  session.

**Settings** (`dsfcars\cars\manager.json` → `"traffic"`, keyed by package
key):

```json
"traffic": {
  "cars": {"forza.fh1.AST_One77_10": {"traffic": false},
           "forza.fh1.cad_ctsvcoupe_11": {"regions": [9]}},
  "ambient": {"default": {"ports": 2, "share": 0.2},
              "sets": {"Chapter 7": {"weights": "equal", "vanilla_off": [201], "extra": [136]}},
              "seed": 0},
  "iv": {"ports": true, "per_region": 24}
}
```

| Set setting | Meaning |
| --- | --- |
| `pool` | `all` (default): every enabled package car with traffic on, including later installs; or a list of stable package keys. Each chapter can override the default pool. Pool membership is independent of the number of resident models; a 721-car pool is retained in full. |
| `ports` | `fill` (default): as many as the budget below leaves room for; or how many (dealt from the enabled cars in traffic, a shuffled deck the seed orders, so different sets get different ports); or a list of keys |
| `share` | `mean` (default): each added model weighs as much on a road as the mean of the vanilla models that road allows, so the added models' share of a road is k / (allowed + k); or a fixed share of every road's weight, split evenly |
| `weights` | `vanilla`: the game's weights; `equal`: every model a road allows equally likely, and the added models like them; `custom`: per-model factors |
| `blend` | 0 to 1 between the game's weights and equal (the same code; `equal` is 1) |
| `custom` | `{model ID or key: factor}` |
| `vanilla_off` | vanilla models out of the set: weight 0, and out of its spool list |
| `extra` | vanilla models from other sets added in, like ports |

**Candidate pools (2026-11-10).** The car manager's Choose Forza pool dialog
edits `pool` for the selected chapter or the default. Add/remove selections
survive filtering, and All Forza cars stores `all`, rather than a snapshot
of installed keys. Globally disabled or traffic-off cars stay excluded but
keep their saved membership. The CLI exposes the same field as `--pool`.
Status distinguishes candidate count from selected models.

This is a configuration rule, not an expanded engine list: the shuffled deck
is filtered by the requested pool before dealing `ports` cars. Native
0x0076FDF0's 40-entry selector buffer and 0x005A79F0's resident-list semantics
(section 1b) still determine the budget. The output contains only the
selected models. Apply and Shuffle rewrite that selection; it is fixed
during the drive. Existing explicit `ports` lists remain supported and are
intersected with the candidate pool. Shared road-table warnings and mission
pot reservations are unchanged.

`ambient.default` applies to every story set (Exposition, Chapters 1 to 7,
Epilogue), and `ambient.sets.<name>` is laid over it for one set.

**The default** (the owner's choice, 2026-11-07) is `ports: fill`,
`share: mean`, `weights: vanilla`. It applies to any install without traffic
settings; `traffic ambient --off` stores `{"off": true}`.

- A set gets as many ports as its budget allows (below; conservative by one
  model, `vm1-20261105-230205`).
- Each port is as likely as an average vanilla car of each road, so a set's
  port share is about ports / (ports + vanilla models). It is slightly more
  on roads that leave vanilla models out.
- `traffic status` prints each set's share from `AiTraffic.share` on the
  planned tables.

With 24 cars in traffic (`traffic status`):

| | Exposition | Ch. 1 | Ch. 2 | Ch. 3 | Ch. 4 | Ch. 5 | Ch. 6 | Ch. 7 | Epilogue |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ports at 41 slots | 8 | 2 | 4 | 4 | 4 | 4 | 7 | 9 | 12 |
| predicted share | 36 % | 10 % | 18 % | 18 % | 19 % | 19 % | 28 % | 34 % | 44 % |
| ports at 50 slots (the default since #566) | 17 | 11 | 13 | 13 | 13 | 13 | 16 | 18 | 21 |
| predicted share | 55 % | 38 % | 42 % | 42 % | 43 % | 43 % | 48 % | 51 % | 58 % |
| ports at 60 slots | 22 | 20 | 22 | 22 | 22 | 22 | 22 | 22 | 24 |
| predicted share | 61 % | 53 % | 55 % | 55 % | 56 % | 56 % | 56 % | 56 % | 62 % |

**Per road** (`traffic.road_weights`), for each weight range a set's roads
use:

1. A range with no weight stays empty: a road that spawns nothing still
   doesn't.
2. The models the range already allows, less `vanilla_off`, keep their
   vanilla shares, blended toward equal.
3. The added models take `share` of the range together. In `equal` mode
   each takes the same as a vanilla model.
4. Custom factors are applied.
5. The range keeps its total.

So `equal` is equal **among what each road allows**, plus the added models.
Some Chapter 7 roads leave out up to 4 of its 18 models (14 of 18 in ranges
56, 123 and 131), and they still do. A mod that makes every model of a set
equally likely on every road would also put those 4 on those roads. That
breaks the per-road rule, so it isn't an option here.

**Shared ranges.** The plan follows the ranges that carry weight. Every
other set and frequency whose roads use one of them (Chapter 7 reaches
9CB17769; Chapter 1 reaches Challenge traffic, Chase traffic 1 and two
unnamed sets) loads the added models too, and its budget counts. A range
that two configured sets would change differently keeps the first one's
change, and the report says so. `traffic status` and the GUI's budget line
name the sets a change reaches.

**Budget** (`traffic.budget`): at most 40 models in a set (0x0076FDF0), and
at most `slots − 1 − pot`, where:

- `slots` is the plugin's `[archive] vehicle_slots` when it has the
  capability `archive.slots`, else 35;
- `pot` is the largest mission-pot request any use of the set makes. The
  manager reads it from the game's own scripts
  (`media\ScriptsLuaScripts.fchunk`): `spooling.missionSpecificVehiclePots`
  and every mission's, challenge's and activity's `trafficSettings` and
  `missionVehiclePot` in `gamelogic\progression\data`, through
  `game_data.run_constant_chunk`. "All chapters" is added to a named pot,
  and the total is capped at 18.

A set some script names but no progression entry lists (a mission switches to
it mid-way, such as Chapter 1 Kidnapped) counts the cap of 18 as its pot. A
set no script names (9CB17769 and four more) is never selected and has only
the 40.

The reached sets bind several chapters. With 41 slots each story set's own
budget is (at 50: 35, 33, 34, 35, 35, 34, 34, 36, 36; at 60 every one is the
selector's 40):

| Exposition | Ch. 1 | Ch. 2 | Ch. 3 | Ch. 4 | Ch. 5 | Ch. 6 | Ch. 7 | Epilogue |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 26 | 24 | 25 | 26 | 26 | 25 | 25 | 27 | 27 |

Counting the sets each one reaches, a story set can hold:

| Exposition | Ch. 1 | Ch. 2 | Ch. 3 | Ch. 4 | Ch. 5 | Ch. 6 | Ch. 7 | Epilogue |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 26 | 20 | 22 | 22 | 22 | 22 | 25 | 27 | 27 |

At 41 slots that is 2 (Chapter 1) to 12 (Epilogue) models over the game's 15
to 18. Counting the sets it reaches, at 50 slots a story set can hold 35, 29, 31,
31, 31, 31, 34, 36 and 36 models (29 for Chapter 1 to 36 for Chapter 7 and the
Epilogue), and at 60 slots 38 (Chapter 1) or 40 (`traffic status`: "N models of
M" is what the set can hold of its own budget M). The variants' real pots
(from the chapter packages' mission scripts) would loosen it; open. Added
models past the budget are left out, and the report says how many.

**The copy.** It is made from the game's current `media\san_franciscoAI.fchunk`
(vanilla or another mod's) on every traffic change. It goes to
`dsfcars\traffic\san_franciscoAI.fchunk`, outside `dsfcars\generated\`, which
a GUI generation replaces. One line in `dsfcars\generated\redirect.txt` points
the game at it (`car_gui.set_redirect`, kept by `car_gui.generate` and
`clear`).

Before writing, `AiTraffic.check` makes sure every set's weighted models are
in its spool list and no list passes 40. With no ambient settings, the copy
and the line go. `traffic status` says whether the game's file is vanilla
(`VANILLA_SHA256`).

**traffic.lua** (`Common\UserLuaScripts\dsfcars\traffic.lua`). register.lua
reads it only when the plugin has loaded the packages (#511):

- `cars[id].traffic = false`: in no region;
- `cars[id].regions`: pinned, inserted at the **front** of those regions,
  where the engine's pick is most likely (section 2);
- `ports = false`: no automatic regions for the other cars;
- `per_region` is `regionCars`.

Every region stops at 128 IDs (#562). With `vanilla = false`, a region that holds no car is
left empty. 0x007EC880 doesn't store a region with no IDs, so a player inside it gets the next
stored region whose circle holds them (0x007EC170 takes the first one in order).

**Other mods that change traffic.**

- **A mod that edits `san_franciscoAI.fchunk` on disk** stacks under ours:
  the copy is built from it, and `status` says it isn't vanilla.
- **A mod that serves its own copy through DSF Enhanced's `[Files]
  Redirect`:** the plugin's redirect passes names it doesn't list on to the
  import that DSF Enhanced hooks (`archive.redirect_chain`). With our line
  present, ours wins. Without it, theirs does.
- **A Lua mod that changes traffic at runtime:**
  - `civilianTraffic.setTrafficSettingsName` to another set works on our
    tables, since that set's ranges are in the copy too.
  - `TrafficSpooler.RequestTrafficVehicles` replaces the loaded list without
    the weights (0x005A79F0). Models it leaves out that a road weights would
    be picked while not loaded. What 0x0077ADA0 does with such a pick (no
    car, or a crash) is **not known**.
  - `SetVehicleModelOverride` makes all traffic one model, ours included.
  - DSF Enhanced's `PreloadAllTrafficVehiclePools` loads every set's models
    (0x0064E2D0). With our bigger lists that is more models than the slot
    budget assumes. Keep it off, as the VM ini does.

## 7. The car manager's settings in game (confirmed-runtime)

Three runs on VM 2:

- the 24-package overlay, configured with `dsff car_manager traffic ...`
  (the CLI, end to end: manager.json, the AI copy and its redirect line,
  traffic.lua);
- dsfcars.asi `vehicle_slots=41`;
- the master `register.lua` with the census of 3b and interesting vehicles
  on;
- Chapter 7, the probe at the dyno start (region 9).

**`equal`** (`vm2-20261105-232744`; `traffic ambient --set "Chapter 7"
--weights equal`). The 18 models' share of the cars within 250 m:

| | Lowest | Highest | Std. dev. (uniform 0.056) |
| --- | ---: | ---: | ---: |
| vanilla weights (`vm1-20261105-210733`) | 0.003 (185) | 0.102 (136) | 0.030 |
| equal weights | 0.029 (185) | 0.072 (206) | 0.010 |

`AiTraffic.share` predicts each model's share from the copy's tables:

- vanilla: 0.012 to 0.105;
- equal: 0.041 to 0.058, lower for 185, 286 and 291, which some roads leave
  out.

The measured values follow both predictions, within the census's noise
(about 40 samples of 70 to 90 cars).

**Ports, off, budget** (`vm2-20261105-233109`):

- settings: car 1 `traffic off`, car 2 pinned to region 9, `traffic iv off`,
  and Chapter 7 `--ports` 3, 4, 5, 6 `--share 0.2`;
- ports 3 to 6 were **19.7 %** of all cars (target 20 %);
- car 1 and the 18 other ports were never seen;
- car 2 wasn't seen either: it was the first of 30 entries in region 9 (about
  12 % of picks) with a few picks in the run.

**Pin** (`vm2-20261105-233606`). The same settings plus
`traffic iv off --vanilla off`, so region 9 holds car 2 alone:

- car 2 was seen 28 times from t161, up to 2 at once, through the region
  only (it was dealt to no set);
- car 1 was never seen;
- ports 3 to 6 were 19.6 %.

No run crashed. The mission start with a patched set is in section 5
(`vm1-20261105-230536`).

**The default** (`vm2-20261107-194144`). The 24-package overlay with no
traffic settings; the car manager wrote the default (Chapter 7: 9 ports,
`share: mean`, predicted 34 % of its roads).

- The game loaded and drove six cars, 20 s of throttle each, about 135 s of
  game time, with no crash.
- The 9 ports were **32.2 %** of all cars within 250 m. The census also
  counts cars outside the set (interesting vehicles, mission-pot models),
  which dilutes the share a little. One interesting-vehicle port (16) was
  also seen.
- Peak private memory was 1163 MB.
- Its frame times aren't a measure of the default: the host had 61 %
  competing CPU at launch (`cpu-gate.txt`). Pooled over the probe it ran at
  30.6 fps, 25 % of frames 50 ms or more. The paired runs below measure it.

**Frame cost of the default.** One series on one VM (vm2), the same six
probe cars in Chapter 7: the default (overlay written by the car manager),
then vanilla traffic (`traffic ambient --off`: no AI copy, no redirect).
`run-series -CpuGate 15`: both launched at 11 to 13 % competing host CPU,
and the host's total CPU (the VM included) averaged 42.5 and 45.0 % during
the probes. Every 10 s `[archive] read_log` window of the probe pooled
(150 s each):

| run | traffic | fps | mean frame | frames of 25 ms or more | of 33.3 ms | of 50 ms | cars within 250 m (mean) | peak private |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `vm2-20261107-201411` | the default (9 ports) | 48.7 | 20.5 ms | 37.8 % | 22.0 % | 6.9 % | 50.1 | 1158 MB |
| `vm2-20261107-201846` | vanilla (`traffic ambient --off`) | 48.1 | 20.8 ms | 38.1 % | 23.2 % | 7.3 % | 53.6 | 1131 MB |

- The default costs no measurable frame time: 0.6 fps and every threshold
  count are within the spread of the windows. Within each run the windows'
  medians range from 16 to 29 ms with the place.
- The 9 ports were 33.5 % of sightings against the predicted 34 %, and 0.3 %
  in the vanilla run (interesting-vehicle ports only).
- Cars nearby were 7 % fewer with the default. That is one pair of runs, and
  each run's count swings about threefold between places (about 75 in the
  first half, 28 in the second), so it isn't resolved.
- Memory: 27 MB more peak private memory with the default.
- Scaling with the port count: at 9 ports (34 %) there is no cost to scale.
  The Epilogue's 12 ports (44 %) weren't run.
