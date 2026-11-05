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
| NONR buffer per slot | 436 KiB, at package init | n/a | 25 more slots: 10.9 MB of heap at start |
| Model data (VB, IB, textures; D3D9 managed pool) | per resident model | n/a | Only while a slot is in use: ports 1.59 MB median kind-0 entry, vanilla traffic 1.41 MB |
| Wwise bank | in place in the slot's NONR buffer (`engine-sound.md`) | n/a | One per resident model, as now. Not measured: Wwise's own pools |
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

**The patch** (`[archive] vehicle_slots`, default 60, 35 = vanilla):

1. From DllMain, before the game builds its pools: graphics pool 9 gets N
   slots at `ceil(vanilla × N / 35)` SYS KiB, nodes and GPU nodes, and N KiB
   GPU (GraphicsManager_Construct asserts `GPU KiB % slots == 0`). The bytes
   are checked against vanilla first.
2. The SSLP load (0x005A0B50; the packages' hook, or the plugin's own when
   there are no packages): for the vehicle spool object (package type 0 at
   `+0x7300`) whose header still says 35, it moves the seven ID pools
   (each checked unused) and sets the kind-0 byte to N. N is capped at
   `64 − (kind 1 + 2 + 3 slots)`.
3. Every other reader of the byte follows by itself:
   `GetMaxSpooledVehicles`, the mission and IV budget, and
   `ChooseDesiredSlotCount` (IVs go back to 10).

**Why 60 is the default, and why no hang is left.** 60 is the ceiling the
handler arrays allow. It costs about 25 × 670 KiB = 16 MB at start, against
about 1.2 of 2 GB of address space in use (`asi.md`). With 60:

- a set is at most 40 models (0x0076FDF0);
- a pot request at most 18 (Lua);
- the player's car and one pending request.

The pot's last request then sees `40 + 17 + 1 + 1 = 59 < 60`, so no request
is refused and `isMissionVehiclesSpooled` can't wait forever. The car
manager's budget check is `set ≤ 40` per set, and with fewer than 60 slots
(an older plugin, `vehicle_slots` lowered) also
`set ≤ slots − 1 − pot − 1`.

## 6. Design: traffic settings in the car manager (revised for the whole pool)

The car manager controls both kinds of traffic. **Ambient traffic** is the
main part: about 80 cars from the chapter's set, with a patched AI file
served by the plugin's redirect list (section 1b). **IVs** are secondary:
on or off per car, pinning, and the #562 cap. After the rename (#565) the
paths are `dsfcars\`, `plugins\dsfcars.asi` and
`Common\UserLuaScripts\dsfcars\`. The work stays in Python; the GUI is
tkinter.

### Settings (`dsfcars\cars\manager.json`, `"traffic"`, keyed by package key)

```json
"traffic": {
  "cars": {"forza.fh1.ALF_8C_08": {"traffic": false},
           "forza.fm4.AUD_TT_07": {"regions": [6, 7, 9]}},
  "ambient": {
    "default": {"ports": 2, "share": 0.2},
    "sets": {"Chapter 7": {"ports": ["forza.fm4.AUD_TT_07"], "share": 0.3,
                           "replace": {"136": "forza.fm4.MAZ_RX7_90"},
                           "vanilla_off": [201]}}
  },
  "iv": {"ports": true, "per_region": 24}
}
```

- `cars.<key>.traffic: false` keeps a car out of both kinds, without
  touching the garage. Bulk: by make, by source (FH1, FM4, DLC), all, none.
- `ambient.default` applies to every story set (Exposition, Chapters 1 to 7,
  Epilogue), and `ambient.sets.<name>` overrides it for one set:
  - `ports`: how many ports join (the manager deals them from the in-traffic
    cars, so different sets get different ports), or a list of keys;
  - `share`: the ports' share of the weight on every road of the set, split
    evenly between them (`w = share * T / (k * (1 - share))`, from the
    engine's pick);
  - `replace`: a vanilla model whose column and spool slot a port takes over.
    It inherits that model's weight on every road, costs no slot, and can be
    used without limit;
  - `vanilla_off`: vanilla models with their weight set to 0 and taken out
    of the spool list. Each frees a slot.
- **Budget**: added ports ≤ the chapter's room (table in 3b) + the vanilla
  models taken out. The manager refuses more and says which set and why.
  The room comes from the pot data, not from a constant.
- **Shared ranges**: a change to a set's ranges also reaches the sets that
  share them, mostly its mission and chase variants. Those sets get the same
  ports in their spool lists, so no set weights a model it doesn't load, and
  their budgets are checked too. `status` names them.
- **Rotation**: the game's Lua has no clock, and a set's ports live in the
  file. So the manager deals the ports when it writes the file (on every
  change, and on `traffic shuffle`). Rotating per session would need the
  plugin to rewrite the columns at load, which can come later.
- `iv`: ports in the IV regions on or off, plus the per-car pins, through
  `traffic.lua`. It fixes #562 (no list over 128). It doesn't change the
  IVs' placement weights.

### Files the manager writes

- `dsfcars\generated\san_franciscoAI.fchunk`: made from the game's current
  file (vanilla, or another mod's) on every traffic change, in about a
  second. It adds one line to `redirect.txt`. A self-check runs before it is
  written:
  - every set's weighted models must equal its spool list;
  - every list must be at most 40 entries (0x0076FDF0);
  - every chapter must stay in budget.
  With no ambient setting, the copy and the line aren't written.
- `Common\UserLuaScripts\dsfcars\traffic.lua`: the per-car IV settings,
  read by register.lua only when the plugin has loaded the packages (#511).
- A traffic change doesn't regenerate gui.sp, Fonts.fchunk or packages.lua.
- The AI patcher goes in `src/dsfforza/game/traffic_sets.py` (with
  `aipatch.py`'s logic and tests): read, the operations, the container edit,
  the self-check.

### CLI (`dsff car_manager traffic ...`)

```text
traffic status [--set NAME]          per set: models and their weights, ports, room left, sets sharing its roads
traffic on|off <selector>...         a key, make:<MAKE>, game:FH1|FM4, dlc, all
traffic ambient [--set NAME] --ports N | --ports KEY,... --share S
traffic ambient [--set NAME] --replace ID=KEY | --vanilla-off ID,... | --vanilla-on ID,...
traffic ambient --off                no patched AI file
traffic shuffle                      deal the ports to the sets again
traffic iv on|off; traffic pin <selector> --regions 6,9; traffic unpin <selector>
```

### GUI (`dsff car_manager gui`, and `cars.cmd` in the release)

The GUI is tkinter with ttk, which ships with uv's Python 3.13 (Tk 8.6;
checked). It calls the same Manager functions as the CLI, in a worker thread,
with the log in a pane.

- **Cars tab.** A table: enabled, in traffic, make, model, year, source
  (FH1 / FM4 / DLC), ID. A filter box and make and source filters
  (multi-select). Buttons for Enable / Disable and Traffic on / off, plus
  "select all / none / this make / this source".
- **Traffic tab.** One row per story set: its vanilla models (names from
  `research/vehicles.tsv`) with a check each, plus replace-with. Then ports
  and share, a budget bar (used of room), and which variant sets it also
  changes. An "all sets" row holds the default. Apply writes the files.
- **IV tab.** Ports in IV regions on or off, pinning.

`cars.cmd` runs `tools\release\launcher.ps1`, which gets uv and finds the
game folder, then runs `uv run dsff car_manager gui`.

### Phase 2 checks (one VM series)

- a car with `traffic: false` never appears, in either kind;
- a port pinned to region 9 appears there;
- a set with ports added at a share gets about that share;
- Chapter 7 at the budget (+3) loads, and one more (+4) hangs as predicted.
  The manager must refuse that setting, so the test writes the file
  directly;
- with the plugin off, register.lua registers nothing (#511).
