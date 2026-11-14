# Batch porting, model IDs and the installer

Updated: 2026-10-29

How many Forza cars one `dngvehicles.sp` can take, which model IDs they get,
what the save and the garage can hold, and how `src/dsfforza/build/port_batch.py` and
`tools/install.ps1` deal with that. Evidence tags as in `dngvehicles.md`.
The Ghidra outputs are in `out/ghidra/profile-*`; the scripts used are
`tools/ghidra/FindStrings.java` and `tools/ghidra/DecompileContaining.java`.

## Limits

| Limit | Value | Source |
| --- | --- | --- |
| Root entries in `dngvehicles.sp` | 1100. Vanilla uses 453: 150 cars x 3, plus SSIC, DMWK and BINF. That leaves room for 215 more cars (1100 - 453 = 647 entries, 3 per car). | confirmed-static, `spool-limits.md` |
| `dngvehicles.sp` file size | below 2 GiB: the file layer's seek clamps larger positions to 0. At about 4.5 MB per ported car (the full roster's average: 3,058,479,104 bytes for 573 cars, `out/build`, 2026-10-28) that is about 370 added cars, so only the root entry limit applies without the plugin. | confirmed-runtime, `spool-limits.md` |
| Base model IDs | below 1000; N+1000 and N+2000 are the interior and exterior entries (FUN_005A7FC0 adds 1000 and 2000). 849 IDs are free: 1 to 61, the 92 gaps in 62 to 303, and 304 to 999. | confirmed-static |
| Garage list | model IDs 0 to 303 only (FUN_006c57d0, and an `int[0x130]` indexed by ID at garage object +0x3264) | confirmed-static |
| Garage maker groups | at most 40 groups (vanilla: 35 with selectable cars) and at most 20 cars per group (vanilla maximum: Chevrolet, 16). Groups are keyed by the first 15 characters of `ManufacturerName`, upper-cased. The insert code (FUN_006C7B40, FUN_006C7BB0) has no capacity check. | confirmed-static |
| Save, vehicle flag list | 304 `{id, flags}` slots at profile +0x184 (bit 0 unlocked, bit 1 owned, bit 2 new). Lookup is a linear search, and any ID works as a key. Vanilla cars use about 146 slots. A full list makes FUN_00507CF0 write through NULL (crash). | confirmed-static |
| Save, `SetVehicleUnlocked` bit array | 320 bits at profile +0x2088, bit = ID. IDs 320 to 639 land in the Driven array (+0x20B0, the "driven" bit of ID - 320), 640 to 959 in an unidentified array, 960 to 999 in the take-a-ride array. No crash: silent save corruption. | confirmed-static |
| Save, `SetVehicleOwned` list | 304-entry ID list at profile +0x2784 (NULL write when full) | confirmed-static |
| Save file | fixed 0x4CF8-byte block with ten 0x278A106F guard words and a version field. `autosave.dat` is written raw, without a checksum. | confirmed-static |
| Mission spool slots | about 10 `RequestMissionVehicle` cars per session (the 11th to 16th were "NOT spooled") | confirmed-runtime, `in-game-tests.md` |

**With `dsfcars.asi`** (`research/asi.md`), these rows change:

| Limit | With the plugin |
| --- | --- |
| Root entries in `dngvehicles.sp` | `archive` module: 16384 (`[archive] max_entries`). The model IDs bind first: 453 + 3 × 849 = 3000 entries. `port_batch.py` assigns 1 to 61 and 304 to 999 (at most 757 cars), or all 849 with `--id-policy asi`; pass `--root-limit` (3003 covers every ID), since the default stays 1100 for installs without the plugin. Tested in game with 2,724 entries (`spool-limits.md`, "Raising the root entry limit"). |
| `dngvehicles.sp` file size | `archive` module (`large_files`): below 4 GiB. The 573 cars take 3.06 GB. At their 4.5 MB a car, all 849 IDs would need about 4.3 GB, a little over 4 GiB; the 177 cars of #162 would bring the archive to about 3.85 GB |
| Garage list | model IDs 0 to 999 (`[garage] max_id`) |
| Garage maker groups | 120 groups (`[garage] max_groups`) of any number of cars (`[garage] max_cars`, 1000); the vehicles screen shows a group of 20 cars or more 19 cards at a time and scrolls on at the edges (`asi.md`, "Maker groups past 20 cars"); a group that doesn't fit is left out instead of overrunning memory |
| Save flags (list, bit arrays, owned list) | IDs 304 to 999 are kept outside the vanilla block, in `dsfcars-garage.dat` next to `autosave.dat`; IDs of 1000 and up are dropped by the bit setters; a full vanilla list no longer crashes |
| Save file | unchanged (vanilla-range data only), so an unmodded game still loads it |

The Lua bindings are `ProfileSettings.GetVehicleUnlocked` 0x678D40,
`SetVehicleUnlocked` 0x678D90, `GetVehicleOwned` 0x678E00 and
`SetVehicleOwned` 0x678E50. None of them range-checks the ID. Model ID 62
(Tanner's car) is special in one place: FUN_006CDE40 pre-marks it as seen.
Nothing else treats IDs below 62 specially.

### Does the 10-car mission limit matter in normal play?

No (confirmed-static from the game's Lua):

- **Traffic** comes from the chapter's traffic set and the interesting-vehicle
  regions. `register.lua` adds the ported IDs to those regions. The engine
  spools them into the 35 kind-0 slots like vanilla interesting vehicles, not
  through mission requests.
- **The garage** spools one car at a time (`Garages.SpoolVehicleModel`, then
  `IsPlayerVehicleLoaded`).
- **Shift** possesses cars that are already loaded.

Only the test probe (`clonetest.lua`, `dyno.lua`) and `register.lua`'s
optional `missionPot` use `RequestMissionVehicle`. The mission pots already
hold about 8 resident models (5 in "All chapters" plus the chapter's own), so
`missionPot` is only for one or two cars.

## ID policy

1. **IDs 1 to 61 for cars that should be in the garage.**
   - Vanilla base IDs run from 62 to 303, so 0 to 61 are free, and the garage
     loop covers them.
   - In the save they stay inside every array, and the flag list has room:
     146 vanilla + 61 = 207 of 304 slots.
   - 0 is left out as a precaution (-1 marks empty slots, so 0 is a valid
     key, but it is a likely "none" value in C code).
   - 62 is Tanner's car.
2. **IDs 304 and up for the rest.**
   - They load, drive and spawn in traffic (304 to 314 are confirmed in game).
   - They never appear in the vanilla garage (with dsfcars.asi they do: below).
   - The save can't hold them safely, so nothing may call
     `SetVehicleUnlocked` or `SetVehicleOwned` for them: at 320 and up it
     corrupts other bits, and many such IDs would overflow the 304-slot
     list.
   - The game's own Lua does call these setters for every key of
     `vehicleStats` in the cheat `flagCheatMode` → `unlockAllVehicles`, and
     `updateVehicleUnlocks` does for entries with unlock fields. So
     `register.lua` wraps both setters (`guardProfile`, on by default) and
     drops calls for added IDs above 303. Only Lua callers go through the
     wrapper, and the engine itself never calls them with these IDs.
3. **Garage IDs only while a maker group has room.**
   - A car joins its maker's group: the vanilla `"ID:nnnnnn"` maker string when
     DSF has the make (from `hud.lua`, `src/dsfforza/game/gui_assets.py`), else a plain
     name such as "BMW".
   - A new make opens a group while fewer than 39 exist (one is kept back).
   - With `dsfcars.asi` a make's group takes all its cars (Ford in the full
     roster: 35 ported + 13 vanilla); `register.lua` reads the plugin's
     cars-per-group limit. Without the plugin (or with `[garage] max_cars=20`)
     a group holds 20, and a car past that joins a shared "ADDED CARS" group (then
     "ADDED CARS 2" ...; "FORZA" before the rename to dsfcars). The name is only a key: the
     garage groups by `ManufacturerName` at each game start (asi.md, "Garage list"), plain
     names don't render, and the save keeps flags per model ID. The "<maker> 2"
     continuation groups of PR #39 are gone.
   - `port_batch.py` (`garage_group`, with a margin of one per cap) and
     `register.lua` (`garageGroup`, exact caps, run against the real
     `vehicleStats` at game start, so mods that make more vanilla cars
     selectable are counted) apply the same rule.
   - A car that gets no group gets an ID from 304 at build time. At runtime
     it stays unselectable.
4. **List order decides** which cars get the 61 garage IDs.

`port_batch.py --id-policy high` puts every car at 304 and up, the range
confirmed in game.

`--id-policy asi` (for installs with `dsfcars.asi`) gives the cars every
free ID in list order: 1 to 61, then the 92 IDs in 62..303 that the vanilla
archive leaves unused (no traffic set, `vehicleStats` key or model-ID field
of the game's Lua names them; Felony Rework's dummies 622 and 623 are
skipped too), then 304 and up. The plugin lists IDs up to 999 in the garage and
saves their flags in its side file, and IDs up to 303 are listed and saved by the vanilla code anyway;
`register.lua` detects the plugin and makes every car selectable while its
maker list has room (120 groups, any number of cars each). The build notes cars that
won't fit a group (same rule, `ASI_GROUPS_CAP`, `ASI_GROUP_CAP`). `install.ps1 -IdPolicy asi` builds
that way and refuses without the plugin. `remerge` turns an existing build into the other policy
without converting again: only the four ID fields of each full entry (VSVH,
VO3D, SPID, UPVH) change.

**Since tested in game** (this list was open when the policy was written):

- cars at IDs below 62 spool, spawn, drive and are listed in the garage
  with their pictures (out/testdrive and testdrive3, IDs 1 to 40;
  `in-game-tests.md`, `gui-assets.md` "Garage card emblem");
- the "FORZA" group (12 added cars, run `host-20261023-100401`);
- plain `ManufacturerName` strings don't render in the garage; the build
  gives them string IDs instead (`gui-assets.md`, "Fixes after the first
  test drive").

## register.lua for players

The defaults are player-safe:

| Option | Default | Effect |
| --- | --- | --- |
| `probe` | false; true when `config.lua` has `ids` (the test harness) | logs traffic counts and profile flags for 3 minutes |
| `writeProfile` | false (`tools/install.ps1 -WriteProfile` turns it on; `dsff install` passes that unless `--no-write-profile`) | on each start, marks every garage-listed added car unlocked and owned, so the game shows a "new vehicle" notice once per car. It uses one slot of the 304-slot list per car, and the flags stay in the save after an uninstall (harmless: no car, no garage entry). |
| `guardProfile` | true | drops Lua `SetVehicleUnlocked`/`Owned` calls for added IDs above 303 (above 999 with `dsfcars.asi`'s save hooks) |
| `garage` | true | `Selectable` for IDs below 304 (below 1000 with `dsfcars.asi`), subject to the group caps |
| `regionCars` | 24 | With more added cars than this, car k (in ID order) goes to every m-th region, offset by k (m = ceil(cars / 24)), so each region gets about 24 of them. Vanilla region lists hold 9 to 110 IDs. |
| `interesting` | true | the interesting-vehicle wrapper (traffic) |
| `missionPot` | false | experimental: keep the cars resident in the "All chapters" mission pot |

At start `register.lua` asks whether `dsfcars.asi`'s garage module is
active (`ProfileSettings.GetVehicleUnlocked(8000000 + k)`, research/asi.md)
and takes its limits: listed IDs, maker groups, the highest ID whose save
flags are safe. It logs them as `dsfcars.asi: save hooks ..., garage IDs
0..999, 120 maker groups of 1000`. `tests/test_register_asi.py` runs
it against stubs with and without the plugin (600 fake cars: none selectable
at 304+ without it, all 600 with it; 55 cars of one make stay in one group, or
fill 20 and spill into FORZA groups with a plugin that keeps 20 per group).

Without `ported.lua` (or `config.lua` `register.cars`) the car list is empty.
The old built-in sample (304 as a Desanne copy) is gone, so nothing gets
registered for a car that isn't in the installed archive. A stub-API run of
216 fake cars (`out/scratch/test_register.py`, lupa Lua 5.1) gives:

- 61 IDs below 304 tried for the garage;
- at most 40 groups and at most 20 cars per group;
- a FORZA group of 20;
- 26 IDs per region;
- the cheat path reaching the save only for IDs up to 303.

## src/dsfforza/build/port_batch.py

```sh
uv run dsff port_batch plan --diverse 30                      # or --list / --car / filters
uv run dsff port_batch build --out out/batch/first --list tools/batches/first.txt --jobs 6 --gui
uv run dsff port_batch remerge out/batch/first --out out/batch/first-high --id-policy high
```

How a build runs:

- Each car runs in a worker process (`port_car.build_car`) at Below Normal
  priority.
  While the file `out/gentle` exists (`src/dsfforza/build/gentle.py`), builds run at
  Idle priority with at most two workers, so a game on the same PC keeps its
  CPU: Idle threads only get CPU time no other thread wants. A job object
  holds every process of the build at Idle (workers ask for Below Normal), and
  nothing is pinned to a CPU: until issue #325 gentle builds were pinned to the
  last two logical CPUs, so every agent's queued on the same two while the rest
  idled (the same 10-car build at 2 jobs: 447 s pinned, 287 s not, the same output;
  issue #325). Delete the file to build at full speed.
- Each worker takes a slot of the PC's job pool first ("Job pool" below): every build and
  survey of every worktree shares it, and `out/gentle` caps the pool at 2.
- A new worker starts only while 1.9 GB of RAM is free (`FREE_RAM_FOR_WORKER`). A
  worker peaks under 1.8 GB, the slowest supercars at about 3.1 GB (32- and 30-car
  builds, 2026-10-30; the job object's peak per process), the garage-picture
  render processes at about 1.55 GB on the full roster (each holds the archive's
  tree), so the gui step runs at most as many of them as the free RAM holds at
  1.7 GB each (`render_jobs`).
- Each worker writes the car's three VEHC entries to `work/<media>.chnk`.
- The merge reads the vanilla archive and every car file as one address space,
  so nothing is copied twice. It registers the clones (SSLP, VSCT), checks
  the root entry limit (`--root-limit`, default 1100; more needs dsfcars.asi's
  `archive` module) and UID clashes, and writes the archive.
- Every car is linted, in `upvh lint-many` processes that each read the
  archive once for their share of the cars (64 or more each): up to `--jobs`
  of them, as many as the free RAM holds at 2.6 GB each (`lint_jobs`). The
  573 cars of a full build: 73 s with 4 processes, 42 s with 9, 39 s with 16.
- With `--gui` the build also runs `gui_assets.build`; every car's garage
  picture is rendered, in `--jobs` processes.

### Conversion speed

Measured 2026-10-24 on the 5800X3D while another build ran 8 workers, so
the absolute numbers are high; the ratios are what to read. Seconds per
car, worker wall time, 2 workers (`out/perf` in the perf worktree; the
4-car regression plus 6 more cars, all byte-identical to the master build):

| Car | Game | Host | Before | After |
| --- | --- | ---: | ---: | ---: |
| MAZ_RX7_97 | FH1 | 278 | 165 | 56 |
| DOD_ChargerDaytona_69 | FM4 | 175 | 166 | 58 |
| CHE_Express2500Cargo_05 | FH1 | 198 | 461 | 36 |
| SAL_S5SRaptor_10 | FH1 | 163 | 113 | 46 |
| BUG_VeyronSS_11 | FH1 | 214 | 126 | 62 |
| AST_007_DBR9_06 (race) | FM4 | 232 | 498 | 66 |
| DEL_DMC12_82 | FM4 | 171 | 126 | 42 |
| DOD_ChallengerSRT8392_12 | FH1 | 173 | 237 | 47 |
| CHR_JeepGCSRT8_12 | FH1 | 138 | 155 | 42 |
| TOY_2000GT_69 | FM4 | 205 | 176 | 55 |

The slow cars were slow in one place. `damage.Grid.nearest` walked every
cell of each ring of a 3D hash grid until it had k points, and for the
sparse groups of `Uv1Field` (one panel and material of the host) that
meant rings of radius 30 and more: 830 million `abs()` calls for the RX7,
two thirds of its time. The Express van and the DBR9 spent 7 of their 8
minutes there. It now looks at the occupied cells only and solves the
stop test for the empty rings in between, which gives the same points in
the same order.

Where the time went, per stage (one car at a time through
`port_batch._worker`, seconds before -> after):

| Stage | RX7 | Charger Daytona | Express | Veyron | DBR9 |
| --- | --- | --- | --- | --- | --- |
| total | 148 -> 50 | 154 -> 53 | 455 -> 33 | 119 -> 56 | 473 -> 59 |
| textures (`dsf_materials.prepare`) | 6.1 -> 1.3 | 5.9 -> 1.0 | 3.4 -> 0.7 | 6.0 -> 1.3 | 5.8 -> 0.9 |
| damage, full entry LODs | 35.5 -> 15.1 | 45.8 -> 15.7 | 187 -> 17.6 | 40.1 -> 24.3 | 182 -> 19.0 |
| damage, exterior | 32.0 -> 5.1 | 33.8 -> 6.1 | 249 -> 3.9 | 16.0 -> 5.6 | 200 -> 6.5 |
| damage, interior UV1 | 18.3 -> 1.8 | 7.6 -> 1.6 | - | 3.8 -> 0.0 | 7.3 -> 1.5 |
| cockpit interior | 38.3 -> 10.4 | 41.4 -> 13.0 | - (host's) | 34.5 -> 8.6 | 59.2 -> 16.6 |
| engine sound fit | 4.2 -> 2.9 | 5.2 -> 3.3 | 5.2 -> 3.4 | 5.2 -> 3.2 | 5.3 -> 3.3 |

What changed, all with the same float operations in the same order as the
loops they replace (`tests/test_speed.py` keeps each original loop and
compares on random inputs):

- `damage.Grid.nearest` and `DamagePlan._vote`: occupied cells only past
  the first rings; the vote scores many triangles at once
  (`closest_on_triangles`, the branches of `closest_on_triangle` as masks).
- `cockpit.visible_triangles` (and `GlassSight`, since removed, #459): the cube-face ID and depth
  buffers rasterised with numpy (per pixel the nearest fragment, the
  earliest triangle among equal depths, as the strict `>` of the loop did).
- `xds.untile`, `xds.decode_rgba`, `gui_assets.untile32`, the DXT1/DXT5
  encoder and the mip filter in `dsf_materials`, and `Texturing.render`
  (box sums from a summed-area table, each distinct average converted once).
- `wwise_bank`: ADPCM encode and decode with the step tables inlined.
- `chnk.read_chunk`: one read per entry table, files through a memory map.
- `host_select._carbin_doors` is cached per process (three calls per car,
  each parsing the main carbin).

What was left after that: the panel votes of `DamagePlan.assignment` (about
15 s per car, 16,000 votes each looking at around 190 host triangles) and the
cockpit cull (8 eyes x 6 cube faces).

**Panel votes, by ring counts.** `DamagePlan._votes` takes all of a
section's triangles at once (`_nearest_pairs`). `_vote`'s result depends only
on the scores and on how often each triangle is counted: a triangle binned into
a box of cells is met in the rings dmin..dmax around the point's cell, so by
ring r it is counted max(0, min(dmax, r) - dmin + 1) times. The ring loop stops
at the first r where at least 3 counted scores are within r * cell, and the
three best are among those. So `_votes` scores each point's candidates (dmin <=
rmax) in one numpy pass, finds the stop ring by counting, and sorts only the
scores within it by (score, panel). rmax goes 1, 2, 3, 5, 7 (`VOTE_RINGS`); a
point that doesn't stop by ring 7, or has a non-finite score, goes to `_vote`.
Votes went from 8-20 s to 1.5-4 s per car, and the full-entry damage stage from
11-24 s to 4-7 s. The 6-car build output is byte-identical.

Left after that (a profile of the RX7): `Uv1Field.lookup` (one
`Grid.nearest` and up to 16 `closest_on_triangle` per new vertex, about 4 s per
car), the cockpit cull (`visible_triangles`, about 4 s), and `zone_maps` /
`_tri_samples` (about 1 s).

**UV1 lookups, crush and zones, batched.** `DamagePlan._finish`,
`_split_seams` and `interior_uv1` first fill the `_uv1` and `_disp` caches for
all the vertices they will ask about (`_prefetch`), then run their loops as
before. Both rest on `Grid.nearest_many`: nearest's result is the k first by
(distance, index) of the points in rings 0..R, and a point counts toward the
stop from ring max(its ring, the first t with d <= t * cell) on, so R is the
want-th smallest of those. numpy measures the distances within rmax rings of
each query point, finds R by counting, picks the k nearest within it, and
math.dist sorts them as nearest does. A point that isn't decided gets a ring
bound from what was found and goes again. A distance within 1e-9 of a ring
boundary, or another position within 2e-9 of the k-th distance, goes to
`Grid.nearest` (copies of a position, the host's split vertices, are no tie).
`Uv1Field.lookup_many` scores the 16 candidates with `_closest` and takes the
first lowest in nearest's order. `zone_maps` and `zone_votes` sample all
triangles in numpy with `_tri_samples`' float operations; the centroid stays
Python's `sum()`, which is compensated in 3.12+, not left to right. On the RX7:
UV1 lookups 5.0 -> 1.4 s, crush displacements 2.4 -> 0.9 s, the exposed-geometry
split 1.7 -> 0.6 s. The 6-car build output is byte-identical
(`tests/test_speed_damage.py` has the equivalence tests).

**Cockpit cull, cockpit atlas, engine sound.** `cockpit._raster` cuts each
part's pixel box into tiles of 1 to 8 pixels a side (the smallest power of two
that fits) and evaluates all tiles of one size as (rows, columns, tiles) arrays,
the tile axis innermost; the edge terms (qx - xc), (ry - yc) ... are computed per
row and per column, the same subtractions the loop does per pixel, and tile
pixels outside the box get NaN centres. The loop's result doesn't depend on
pixel order (per pixel the largest 1 / z if above the depth, from the earliest
part among equals), so np.maximum.at and np.minimum.at replace the lexsort. The
near-plane clip and visible_triangles' bookkeeping are numpy too.
`visible_triangles` 4.7-11 s -> 1.2-2.0 s, the cockpit step 7-14 s -> 3-5 s per
car. An ideal early depth cull (parts behind the final depth over their whole
box) would skip only 30% of the box pixels on the RX7, so there is none.
`wwise_bank.encode_ima_many` guesses each block's running step index (the end
of the block before), encodes every block's candidates of all of a car's loops
at once, and repeats until the guesses stop changing (the first blocks are
right from the start, the next ones a round later; 7-10 rounds); if they don't
settle in ENCODE_ROUNDS, the original loop runs on the table. ADPCM encoding
1.4-1.9 s -> 0.2-0.3 s. Also: the decoder (blocks in parallel), the `seamless`
cut search (np.correlate for all cuts, the original expression for those within
1e-9 of the best), the sweeps, the WAV writer, the region cut, summed-area tables
per region, and decoded Forza atlases shared per process (the cockpit decoded
interior_LOD0 twice). `tests/test_speed_cockpit.py` has the equivalence tests.
Left in the cockpit step: `add_geometry` (per-vertex Python, about 1 s),
`Texturing.plan` (about 1 s, mostly decoding the cockpit atlases) and the
sweeps (about 1 s, numpy-bound).

Batch-level costs at full-roster scale:

- **Lint was quadratic.** Each `upvh lint` read the whole output archive,
  every vehicle's defined UIDs and every vehicle's sound bank. At 4 cars
  that is 6 s per lint; at 573 cars the archive holds about 2,200 VEHC
  entries and 720 banks, so an estimated 13 to 15 s per lint, 30 to 40 min
  at 4 at a time. `upvh lint-many` reads them once per process (6 cars:
  3.6 s instead of 19 s of single lints) and prints the same text.
- **GUI** is linear in the cars, but `render_car` read the whole archive
  twice per render, and at 573 cars that archive is about 3 GB. It is
  read once now. The nameplate rasteriser is about 3 times faster, and the
  renders (about 5 s each, every car without an FH1 thumbnail) run in
  `--jobs` processes. 4 cars: 36 s -> 15 s. Since 2026-10-24 every car is
  rendered (numpy `car_render.py`, about 1 s a car; `gui-assets.md`,
  "Camera and render"): the 40 cars of out/testdrive11 85 s -> 44 s at
  `--jobs 2`.
- **Worker start-up** is about 3 s per car (imports, the vanilla archive's
  root, host survey and sound index from `out/cache`), small next to the
  conversion.
- **`--jobs` above the physical cores** (8 cores, 16 threads, 32 GB; 32 cars, Idle,
  2026-10-30): convert 374 s with 8 workers, 345 s with 12, 323 s with 16 (whole
  build 7.0, 10.0 and 12.9 GB at its peak). So `--jobs` (and `dsff install`) default
  to one worker per logical CPU, as many as 1.9 GB each fits in the RAM
  (`port_batch.default_jobs`: 16 on this PC; `out/gentle` caps it at 2).

### Job pool

Several agents build and survey at once, each in its own worktree, and each build
started a worker per logical CPU: three of them ran 48 heavy processes on 16 CPUs, and
the test VMs' game loads starved (issue #70). Every heavy process now takes a slot of one
pool for the whole PC (`src/dsfforza/build/jobpool.py`): the conversion workers and the
`upvh lint-many` processes (the build process holds their slots), the garage-picture
render processes, `render_compare` (run, diff, rescore), `cockpit_survey`,
`plates_survey` and the door-count memo of the plan (each process holds its own slot,
taken in its initializer before it takes work).

- **Slots.** `%LOCALAPPDATA%\dsfforza\pool\slot-NN.lock` (`DSFFORZA_POOL` names another
  folder; `off` turns the pool off). A process holds a slot while it holds a lock on a
  byte of the file at 1 GiB (past its end, so the file's text, the holder's pid, label and
  worktree, stays readable). The OS drops the lock when the process dies, so a killed
  build frees its slots; its workers are in a job object that ends them with it
  (`jobpool.die_with_me`), so none runs on without a slot. Slots are counted and taken
  under `pool.lock`.
- **Size.** One slot per logical CPU (16 here), at most `gentle.CORES` (2) while
  `out/gentle` exists in this worktree or in the main checkout (`gentle.anywhere`). The RAM
  budget (below) bounds what the slots hold.
- **RAM.** Each slot is taken for a job of a kind and reserves that kind's measured peak
  (`port_batch.WORKER_RAM`, `LINT_RAM`, `RENDER_RAM`, `DIFF_RAM`, ...; "RAM measured" below)
  while it is held. Every slot, a process's first too, is granted only while the budget has
  that much (`jobpool.budget`, read under `pool.lock` at grant time): the host's available
  RAM (`GlobalMemoryStatusEx`, so the owner's programs and running VMs count as they are),
  less what the busy slots' jobs may still take (each reservation less the private working
  set of the job's process tree now), less what the VMs that series hold still need (RAM
  holds, below), less `jobpool.SPARE`, less what earlier waiters want (a waiter's ticket
  carries its RAM, so a later, smaller job can't keep taking the RAM a larger one waits for).
- **`--jobs`** is a per-build maximum; the pool decides how many run.
- **Fair share.** A process that finds no free slot waits in line (`queue/<pid>.lock`,
  locked while it waits, with the time it joined); a slot that frees goes to the one that
  has waited longest. Without the queue, a build that freed a slot took it again before
  the other build's next try (1 s): two 4-car builds at 2 slots ran one after the other.
- **VM loads.** `tools\wsb\run-series.ps1` holds the pool at its CPU gate, before it
  launches the game: `holds\series-<pid>-<time>.json` with the VM's vCPUs (`$VmSettings`
  numvcpus, 4; every CPU for a host run). While the hold lasts, the pool hands out that
  many fewer slots, and the gate waits (within `-CpuGateSeconds`) until the busy slots
  leave them free. The pool ends the hold when the run's `events.json` says the Story world
  is ready (run-test.ps1's "story world ready" note), when the run has a `result.json`,
  at its time limit (gate + `-StorySeconds` + 600 s), or when the series' process is gone.
- **VM RAM.** A `-Vm` series holds its VM's RAM before it starts or uses the VM
  (`Wait-RamGate`): `holds\ram-<pid>-<time>.json` with the `.vmx`, for the whole series.
  While it lasts the pool reserves `jobpool.VM_RAM` (7.7 GB) less what that VM's
  vmware-vmx and its mksSandbox hold now (`vm_need`; the vmx pid is the one vmware.log's
  first line names, if a vmware-vmx of that pid started then), so a VM that is up isn't
  counted twice, and no new job is granted against it. The gate asks `dsff jobpool vm-gate`
  every 10 s until the VM's need fits in what the running jobs leave (at most
  `-RamGateSeconds`, then it starts anyway and says so, as the CPU gate does). A VM nobody
  holds reserves nothing: an idle VM that is up is the host's used RAM, one that is off
  holds none.
- `uv run dsff jobpool status` lists the slots, their holders, the holds and the RAM
  budget; `uv run dsff jobpool watch --out <csv>` logs the host's memory, the budget and
  each job process's peaks (`<csv>.peaks.json`).

Measured 2026-11-05, two worktrees on the same commit, each `port_batch build --jobs 4`
of 6 cars with `DSFFORZA_GENTLE=1` (pool of 2), the second started 25 s after the first
(`jobpool status` and the worker processes sampled every 3 s): 2 slots busy throughout,
never more than 2 workers; with the queue the two builds ran one worker each, side by
side (before it, the first build's 4 cars all ran before the second's started). The first
build's lint waited for a slot while the second converted.

#### RAM before #613

On 2026-11-14 two `render_compare diff` runs (`--jobs 16`) took the host to 0.9 GB and
then 2 MB available, with 16 of 16 slots busy and 51 to 57 GB private in their workers; both
test VMs were paged out and their series stalled (#613). What the pool did then:

- **Every executor worker passed the RAM check.** `try_acquire` checked the RAM only when
  the process already held a slot (`if ram and _HELD`, so "each build gets at least one
  slot"). `_HELD` is per process, and every `jobpool.executor`/`mp_pool` worker is a new
  process that takes its one slot in its initializer: none of them was ever checked. Only
  a build's second and later conversion workers and lint processes were.
- **The check compared the job's figure with the free RAM, once, at grant time.** A slot
  reserved nothing afterwards: a job granted with 1.7 GB free grew to 4.2 GB, and the next
  job saw the RAM it had not taken yet as free. The figure was the host's real available
  RAM (`GlobalMemoryStatusEx`), so the owner's programs and the running VMs did count, as
  they were at that moment; nothing was kept back for a VM about to start (a hold reserved
  CPUs only) or for the owner.
- **The figures were not the jobs' peaks.** `RENDER_RAM` (1.7 GB, a garage-picture render
  process) was used for `render_compare` run and diff processes, which held 3.8 to 4.4 GB.

#### RAM measured

Peaks of each kind's processes, 2026-11-14, this PC (31.9 GB, 16 logical CPUs). Method:
`jobpool watch --every 1` (and, for a process run by hand, a sampler of its process tree)
reads the kernel's `PeakPagefileUsage` (peak private bytes) and `PeakWorkingSetSize` of
each job's process tree from `NtQuerySystemInformation(SystemProcessInformation)` every
second and keeps the highest; the sums over the tree count the interpreter a venv's
`python.exe` launcher starts. A process's growth in its last second can be missed. MiB.

| kind | inputs | processes | peak private | peak working set | constant |
|---|---|---|---|---|---|
| conversion worker | 12 cars `--no-store`: the 7 slowest and largest of the last full build (KOE_CCXR_09, AST_V12Zagato_12, CHR_JeepGCSRT8_09, HYU_GenesisCoupeRMR_10, VIP_Viper_13, MER_S65AMG_12, LAM_LP7004_12) and 5 median ones | 12 | 3264 (VIP_Viper_13); AST 3165, LAM 2033, HYU 1737; the other 8 930 to 1359 | 2982 | `WORKER_RAM` 3300 |
| `upvh lint-many` | 45 cars of the 721-car roster, `--plan` (the cars merged in memory, as a roster build lints) | 1 | 3036 | 2563 | `LINT_RAM` 3100 |
| garage-picture render | the 573-car archive of a full build, 77 pictures in 420 s | 2 | 1650, 1627 | 1206 | `RENDER_RAM` 1700 |
| `render_compare diff`, a process per car | 80 changed cars of #608 (two full-roster builds: the first 20, then 60 in the check below) | 80 | 1460 to 2805; 2382 or more for 5 of the 60 | 2053 | `DIFF_RAM` 2900 |
| `render_compare diff`, one process for 20 cars | the same 20 cars, `--jobs 1` | 1 | 3381 (it rose from 2.0 to 3.1 GB over the cars, falling back between) | 2774 | |
| `render_compare run` | 4 large cars of a roster build | 1 | 1123 | 615 | `COMPARE_RAM` 1200 |
| `render_compare rescore` | the 12-car run | 2 | 736 | 235 | `RESCORE_RAM` 800 |
| door-count memo | `port_batch plan --roster` with an empty memo: 716 carbins | 16 | 1153 | 654 | `MEMO_RAM` 1200 |
| `cockpit_survey` | the 6 largest cars above, and 8 roster cars | 4 | 711 | 212 | `COCKPIT_SURVEY_RAM` 800 |
| `plates_survey` | the 12-car build | 1 | 1172 | 673 | `PLATES_SURVEY_RAM` 1200 |

Each constant is the kind's highest peak private bytes rounded up to 100 MiB. Private bytes
are at least the private working set a job takes from the available RAM, so a job stays
under its reservation. A conversion worker is reserved its kind's highest peak although
most cars stay near 1 GB: no field of a car predicts its peak (the four highest are not the
largest outputs; KOE_CCXR_09, the largest and slowest car of the last full build, peaked at
1.0 GB here). A diff process drew a whole chunk of cars before (676 cars at `--jobs 16`:
about 42 each), and its memory grew over them, to the 3.8 to 4.4 GB of #613; it now draws
one car per process (`max_tasks_per_child=1`): the same 20 cars, the same scores, 311 s at
4 processes against 708 s in one.

#### The floor and the wait

- **Floor.** While a series holds a VM, the host's available RAM stays above
  `jobpool.SPARE` plus what that VM still needs: at most 3 + 7.7 = 10.7 GB while the VM is
  off, 3 GB once it is up and in game (its 7.7 GB is then its own working set), for any
  number of builds in any worktrees, as long as each job stays under its kind's peak.
  With no hold, above `SPARE`. A job's reservation covers it from its grant (a child job's
  slot counts its whole reservation until the child is named), so two builds that ask at
  the same time can't both take the same RAM (grants are made under `pool.lock`).
- **`VM_RAM`, 7.7 GB:** one VM in game at 4 GB of guest RAM and 1 GB of graphics memory,
  vmware-vmx working set 7.0 to 7.2 GB plus mksSandbox 0.5 GB (in-game-tests.md, "Host
  cost"; the two VMs together 14.2 + 0.7 = 14.9 GB; the vmx holds the whole guest RAM
  within 80 s of power-on, "Memory measured").
- **`SPARE`, 3 GB, is a policy choice:** room for the owner's programs to grow while
  builds run. Its size is how far their use moved with no VM and no build running, 24 to
  27 GB available on 2026-11-14 05:18 to 05:32 ("Host cost"). `DSFFORZA_POOL_SPARE` (GB)
  sets another.
- **The wait at the RAM gate** is the rest of the running jobs that hold the RAM the VM
  needs: from the hold on no new job is granted against it, so the RAM only frees. How long
  a slot is held, by kind: a conversion worker one car (35 to 316 s in the last full build,
  at most `WORKER_TIMEOUT`, 45 min); a diff process one car (about 1 min); lint one step
  (3 s for 12 cars, 39 to 73 s for 573); the memo one plan (716 cars in 280 s at 16
  processes); a garage-picture render process, a `render_compare` run process and a
  survey process their whole step (about 11 s per picture per render process: a full
  roster's 721 pictures at 4 processes about 33 min). So the worst case is the longest
  such step still running when the hold starts. `-RamGateSeconds` (2700, the conversion
  worker's limit) caps it: after that the series starts anyway and says so.
- **Cost.** With no hold, the pool can use what the owner leaves less 3 GB: 19 GB on
  2026-11-14 with both VMs off, about 5 conversion workers at once or 6 diff processes.
  While a series holds a VM that is off, 7.7 GB less.

Checked 2026-11-14 11:08 to 11:27, both VMs off, the owner's programs running (22 GB
available before): a RAM hold for a VM that is off (a stand-in for `Enter-RamHold`, so
the floor was 3 + 7.7 = 10.7 GB), then at once `render_compare diff --jobs 16 --limit 60`
(the #608 builds) and, 20 s later, `port_batch build --jobs 16 --no-store` of the 12 cars
above, `jobpool watch --every 1` logging (`out/mem/check.csv` in the worktree of #613's PR):

- Host available RAM never fell below 16.8 GB (lowest at 11:11:50); no sample under the
  floor. Commit charge at most 32.0 GB.
- The pool queued: at most 6 slots busy (4 diff processes, or 1 conversion worker and 3
  diff processes, or 3 conversion workers), up to 17 processes waiting (16 diff processes
  and the build) for up to 40.7 GB; the headroom for new jobs stayed between -0.5 and
  3.7 GB. Before, all 16 diff processes and the build's conversion workers would each
  have passed the check.
- The diff drew its 60 cars in 1025 s, the build converted its 12 in 1063 s, 0 failed.
  When both ended, `jobpool vm-gate` passed for the held VM (7.7 GB needed, 18.4 GB free).
- The headroom went below 0 once: diff processes on these 60 cars peaked at up to 2805
  MiB, past the 2400 the first 20 cars gave `DIFF_RAM`; it is 2900 now.

### Selective rebuild

```sh
uv run dsff port_batch build --out out/all2 --list roster.txt --reuse out/all --jobs 8 --gui
uv run dsff port_batch build --out out/all2 --list roster.txt --reuse out/all \
    --trust-code --rebuild-where "host == 198" --verify-reuse 4
```

`--reuse DIR` copies a car's output from an earlier build (`DIR/work`:
`<media>.chnk`, the report, the log, `sound/<media>/`) when nothing that
went into it changed, and converts only the other cars. The pack step after it (merge, lint,
garage pictures) redoes only what changed too: see "Pack reuse" below.
Files are hard-linked where the
volume allows it, so an earlier build of 573 cars costs no second 2 GB.

**What decides.** Each worker writes `work/<media>.fp.json`
(`src/dsfforza/build/fingerprint.py`) next to the chunk:

- the job: car, game, disc source, spool ID, the requested `--port`
  options, the vanilla archive, cache and disc paths (links and junctions
  resolved, so another worktree with the same `out/cache` matches);
- Python and numpy versions;
- every file the worker opened for reading, from an audit hook on `open`
  and `sqlite3.connect`, with its SHA-256. Files over 64 MiB (the vanilla
  archive, the disc images) get size, mtime and the SHA-256 of their first
  and last 4 MiB. Python's own files and the package sources don't count;
- every folder it listed (`os.listdir`, `os.scandir`), as a hash of the
  names, so a file that appears in the car's cache folder counts too;
- the code it ran. `sys.monitoring` reports each code object the first
  time it runs. The car then depends on the units it ran: a function, a
  method, a class body without its methods. It also depends on the
  module-level assignments and imports whose names its code uses, the
  classes it names, and the remaining top level of each package module
  it loaded. A unit's hash is its AST without docstrings, so comments,
  docstrings and `ruff format` don't count.

A car is reused only when the job and environment match and every
recorded file, folder and unit hashes the same now. The build prints why
the others are built, grouped (`code dsfforza.convert.cockpit.convert_interior:
3 (...)`, `input ...`, `job: spool_id`, `no fingerprint`). A failed car is
never reused. A car built with a fallback is reused only if its first try's
fingerprint is unchanged too, since a fix might make that try work.

The spool ID is part of the job. The chunk's UIDs are seeded from it, so a
car whose ID moved (a car added before it in the list) is built again. Keep
the list order, or add new cars at the end.

**What it can't see.** A file whose absence matters but that sits in a
folder nobody listed. A `stat()` without an open. Code run in the parent
before the workers start, except what reaches the job (spool IDs, options)
and the shared caches the workers read (`host-survey.json`,
`dsf-fleet.json`, `vanilla-banks.json`), which are recorded as files. And
anything outside Python, numpy and the package, such as a different
Windows build of the C runtime. `--verify-reuse` exists for these.

**Declared scope.** `--rebuild-where EXPR` builds the cars whose earlier
report matches a Python expression even when their fingerprints match.
The expression sees `r` (the report), `entry` (its manifest entry),
`media`, `game` and `host`. `--trust-code` drops the code from the
comparison, so a fix that knows its cars only names them:
`--trust-code --rebuild-where "'cockpit' in entry"`. Inputs still count.

**Verification.** `--verify-reuse N` (default 2 with `--reuse`; 0 turns it
off) converts N random reused cars again into `work/verify/` with their
earlier effective options, and stops the build before the merge if any
chunk differs. The seed is printed; `--verify-seed` repeats a pick.
`batch-report.json` gets a `reuse` record: source, reused cars, rebuilt
cars with their reasons, verification results.

`--verify-mismatch rebuild` (what `dsff install` passes; the default `stop` is
for developers) doesn't stop: a car that came out different takes its fresh
copy, and the other cars reused from the same earlier build are converted
again, since the mismatch can't be pinned to its car. The `reuse` record gets
`verify_mismatch` (the cars, the ones converted again), the install summary
a note, and `dsff report` the record (issue #133: a player's install stopped
on BMW_Z4_11 and the player lost the cars built so far).

**Repeatability (issues #84, #133).** BMW_Z4_11 converted three times with
the v0.1.1 code and three times with master gave a different chunk each time:
75 to 360 bytes in the full entry's first texture (DXTC) and two vertex
buffers (the low bytes of vertex positions). Without `--damage` it came out
the same. `PYTHONHASHSEED=0` didn't help; one OpenBLAS thread
(`OPENBLAS_NUM_THREADS=1`) did, three runs identical, at the same speed (the
workers are the parallelism). port_batch starts its workers with one BLAS
thread (`fingerprint.WORKER_ENV`). That is not all of it: AST_DB9_05 (#84)
still differs between runs with one thread (280 to 420 bytes, the same kinds
of entries; 1250 bytes without `--damage`, so it isn't the damage step), and
BMW_Z4_11 still now and then (two of seven runs). So `--verify-reuse` can still fail on
a car whose inputs didn't change; `port_car` run on its own still uses BLAS
threads.

Tested (4 cars: MAZ_RX7_97, CHE_Express2500Cargo_05, SAL_S5SRaptor_10,
DEL_DMC12_82@fm4, `out/perf/r*` in the perf worktree):

| Run | Result |
| --- | --- |
| `--reuse` of an unchanged tree | 4 reused, 0 built; 2 verified identical; archive byte-identical; 67 s instead of 129 s (56 s of it the verification) |
| a statement added to `cockpit.convert_interior` | 3 built ("code dsfforza.convert.cockpit.convert_interior"); the Express, which keeps the host's interior, reused and verified identical; archive byte-identical |
| `SKY_FLOOR` changed in `forza2dsf`, with `--trust-code --rebuild-where "host == 198"` (a wrong claim) | the Express built, 3 reused; verification rebuilt the RX7, found it different and stopped the build (exit 1, no archive) |
| the same change without `--trust-code` | the fingerprint check names `code dsfforza.convert.forza2dsf.SKY_FLOOR` for every car |

Recording costs about 2 s per car (34.7 s instead of 32.8 s for the
Express): mostly hashing the 10 to 30 MiB a car reads and parsing the
package modules it loaded. Checking 573 cars hashes about 15 GB once,
then reuses the digests of shared files.

### Pack reuse

The pack step (`port_batch.finish`: merge, upvh lint, manifest and Lua,
garage pictures, HUD badges, fonts and locale files) took 13 minutes on
the full roster even when every car was reused (issue #100). It now
redoes only what changed since an earlier pack
(`src/dsfforza/build/pack_reuse.py`). The earlier packs it looks at are
the build folder's own (a rerun, `--resume`) and the `--reuse` builds'
(`dsff install`: the last finished build and an interrupted one).
`--no-pack-reuse` turns it off.

**The record.** Each pack writes `<build>/pack/pack.json`:

- the settings that shape the pack besides the cars: ID policy, root
  limit, `--gui`, `--wide-glyphs`, the disc images, the vanilla archive's
  digest;
- the cars in merge order, each with a key: the SHA-256 of its chunk plus
  the report fields the merge reads (entry, host, game, spool IDs). A
  chunk's digest is kept with its file ID, size and mtime, so a
  hard-linked chunk (`--reuse`) isn't read again;
- a fingerprint of the pack, made like a car's (above): the files it read
  (vanilla archive, the game's `gui.sp`, `Fonts.fchunk`, locale files, disc
  images, string tables, `vanilla-banks.json`) and the code units it ran,
  in this process, the `upvh lint-many` processes (`--record`) and the
  render processes (`gui_assets._Renders`). The build folders themselves
  and the chunks don't count: the keys cover them;
- per car: its lint result with what ties it to other cars, and its
  garage picture's key;
- the nameplates and Forza logos it drew (`pack/images/`, by text and
  size or by maker);
- the size and mtime of every file it wrote.

A record is used only when its settings are this build's and its
fingerprint holds. A game file read from the game folder before an
install and from `dsfcars-backup\` after it (`gui_assets.vanilla_file`)
counts as the same input when the digest is the same.

**Whole pack.** When an earlier record has the same cars with the same
keys and its outputs are unchanged, nothing is redone: the archive is
hard-linked, `gui/` copied (the next pack writes it in place), the
manifest and the Lua written again from the earlier manifest's cars.

**Some cars changed.** The merge runs over every car (15 to 20 s since
`MultiStream.read` bisects its parts and `chnk.write_chunk` lays out each
nested chunk once; it took 50 to 80 s). Then:

- lint: the cars whose key changed, plus every unchanged car a change
  could reach. upvh lint's cross-vehicle checks look at UIDs another
  vehicle defines and at sound bank IDs another bank uses, so lint-many
  `--facts` reports per car the UIDs it references but doesn't define and
  the cars whose banks share an ID with its own (`near`). An unchanged
  car is linted again when it was `near` an earlier version of a changed
  or removed car, is `near` a changed car now, or references a UID a
  changed car now defines. A failed result is always linted again. The
  lint of one car still reads the archive's tables first (10 to 20 s).
- garage pictures: a car's picture is taken from the earlier build when
  its key (car key, VEHC records, Forza names) is the same; only the
  others are rendered.
- nameplates and logos: taken from the earlier builds. Drawing the
  chrome nameplates (`gui_assets.text_plate`, pure Python) was most of
  the GUI step: about 500 s of the full roster's, against 35 s for the
  logos and about 1 s per rendered car.
- the badge plan, `Fonts.fchunk`, `gui.sp`, the locale files and
  `hud.lua` are made again: they number every car's badges and names.

**Check.** `--verify-pack` (`dsff install --verify-pack`) makes a full
pack of the same cars into `<build>/pack-verify/` afterwards and stops
when any file differs: the archive, the Lua, the manifest (but its time
and archive path), and every file of `gui/`. The comparison is
`port_batch.compare_packs`; `tests/test_pack_reuse.py` checks the
decisions with stand-ins for the merge, lint and pictures.

Measured on the full roster (573 cars, all reused with `--resume` from a
copy of `out/final4`'s work folder, `--jobs 4`, `--gui --wide-glyphs
--id-policy asi --root-limit 3003`; changed cars are older outputs of the
same cars from `out/final3`):

| Run | Before (origin/master) | After |
| --- | --- | --- |
| full pack (no earlier record) | 810 s (merge 80, lint 38, GUI 679) | 819 s (merge 15, lint 77, GUI 707) |
| nothing changed, same folder | 810 s | 10 s |
| nothing changed, another folder (as `dsff install`) | 810 s | 13 s |
| 1 car changed | 810 s | 78 s (merge 18, lint 10, GUI 37) |
| 50 cars changed | 810 s | 117 s (merge 20, lint 15, GUI 67) |

Before, the pack did the same work whatever changed. The first pack's
lint takes longer than before (`--facts`: 45 s instead of 35 s over the
whole roster, measured on its own; 77 s in the run above on a busy
machine). The full packs of
both versions are byte-identical (`compare_packs`), and so are the
whole-pack copy and the 50-car pack with `--verify-pack`.

### Car store

Every worktree's "before" build of master converted the cars another worktree had
already built from the same commit. Now every build keeps its converted cars in one store
on the PC (`src/dsfforza/build/carstore.py`, `%LOCALAPPDATA%\dsfforza\store`;
`DSFFORZA_STORE` names another folder, `off` turns it off, as does `port_batch build
--no-store`) and takes a matching car from it without `--reuse`. It is on in a git
checkout and off in a player's copy (no `.git`) unless `DSFFORZA_STORE` names a folder.

- **What it holds.** `cars/<media>/<id>/`: the car's work files (chunk, report, job,
  log, fingerprint, the first try's of a fallback build) and its `sound/<media>/`, with
  `meta.json` (spool ID, options, the checkout it was built in). Files from 1 MiB are hard
  links (the build's own files are never written in place), the small ones copies. The id
  is the digest of the fingerprints with the checkout's folder written `<repo>`, so the
  same car built in two worktrees is one entry. `pictures/<key>-<pack>.*`,
  `images/<name>-<pack>.*` and `packs/<pack>.json`: the pack step's garage pictures
  (by `PictureCache` key), nameplates and logos, under the fingerprint of the pack that
  made them, kept once per pack fingerprint; one is used only while that fingerprint
  holds (as an earlier build's pack record).
- **Matching** is `--reuse`'s (`fingerprint.Checker`), after the store, for the cars no
  `--reuse` build has: the same job (spool ID included), environment, inputs and code.
  Two things made a car of another worktree not match:
  - files of the worktree itself: each has its own `out\db\<game>_gamedb.slt` (and
    `out\lua-src`). A fingerprint now records its checkout (`"repo"`; for older ones the
    store takes the checkout its build folder is in), and `fingerprint.relocate` moves the
    paths under it to this checkout before the check. `out\cache`, `out\vanilla` and
    `assets\` are junctions to the main checkout, resolved when recorded, so they match
    as they are.
  - a DLC car's pack file: the job recorded `source.package` as given, under the
    worktree's `assets\` junction, so DLC cars never matched across worktrees (#501's
    worker). `job_key` now records it resolved (`norm`), and the check resolves the
    recorded one too, so earlier fingerprints still match. A pack inside a disc image
    (`content/...`) is a path in the image and stays.
- **Verification.** `--verify-reuse` defaults to 2 with `--reuse` and to 0 for store
  hits alone; `--verify-reuse N` asks for N.
- **Putting.** After the conversion (and `--verify-reuse`), every car of the build with
  a complete fingerprint goes in, the ones taken from `--reuse` or `--resume` too (kept
  once); after the pack, its pictures and images.
- **Size.** At most `DSFFORZA_STORE_GB` (default 25 GiB: a full build's cars are about
  6 GiB counted file by file, so a few builds' worth). Past it, the entries (cars, pictures, images) used
  longest ago go first, down to 90% (`Store.evict`, after each build). A hit marks its
  entry used. Hard-linked files count in full, though they cost nothing while a build
  folder holds the same file.
- `uv run dsff carstore status | seed <build> ... | evict [--cap-gb N]`. `seed` puts an
  earlier build's cars and its pack's pictures and images in.

Measured 2026-11-05:

- `carstore seed` of the 8200cfa release build (`driversf-forza-release\out\build`,
  721 cars, read only): 36 s, 721 cars (hard links).
- `--list tools/batches/all.txt --max 4 --id-policy asi --root-limit 3003 --gui
  --wide-glyphs --packages`, with the IDs the release build gave the cars: 4 of 4 from the
  store, 47 s; the 4 packages' members byte-identical to the release build's.
- 3 cars (`--car DEL_DMC12_82@fm4 --car SAL_S5SRaptor_10 --car BMW_M1Design_81`, the
  last an FM4 DLC car), `--gui --packages`: converted in worktree a (104 s); then the same
  command in worktree b: 3 of 3 cars, 3 of 3 pictures and 10 of 10 images from the store,
  27 s. `dngvehicles.sp`, `gui/gui.sp`, `Fonts.fchunk`, `hud.lua`, `dngvehicles.lua` and
  every package member byte-identical to worktree a's fresh build.

### Which cars a change affects

```sh
uv run dsff port_batch affected --roster                     # against the merge base with origin/master
uv run dsff port_batch build --out out/after --roster --affected --gui --packages
uv run dsff port_batch build --out out/before --roster --affected --before --gui --packages
uv run dsff render_compare diff --before out/before --after out/after --out out/rc/<topic>
```

A stored fingerprint names the code units the car ran and the files it read, so the
store answers offline which cars a change touches (`carstore.affected`). For each car of
the selection it looks for a base entry: one whose recorded units are those of the
revision `--base` (default the merge base of HEAD and origin/master), read from git
(`fingerprint.GitUnits`), preferring the car's own spool ID. The car is

- **affected** when a unit it recorded differs in this checkout, or an input it read
  changed (every such unit is listed, and the first input);
- **the same** when none did: this checkout makes the same car;
- **unknown** when the store has no base entry: a build of the base commit anywhere on the
  PC puts one in.

`build --affected` builds the affected and unknown cars only, with the IDs of the whole
selection. `--roster` is dsff install's selection and IDs (`tools/batches/all.txt`, then
the FM4 DLC packs' cars, `--id-policy asi --root-limit 3003`), so a subset gets the IDs a
full build gave its cars and the store's full builds match it. `build --before` takes
every car from the store as `--base`'s code made it (the store's entries checked against
that revision's units) and converts none; a car with no such entry is listed as failed,
"not in the car store". The pack step of a `--before` build runs this checkout's pack
code.

Checked 2026-11-05 against the seeded release build (base 8200cfa):

- No converter change: `affected --roster` 0 affected, 721 the same, 0 unknown, 68 s.
- `lights._headlight_reflectors` rewritten as an equivalent expression (a test edit,
  not kept): 12 affected, exactly the 12 cars whose stored fingerprints record that
  unit (AUD_3_R18TDI_11, CHE_2_MonteCarloSS_11, DOD_Challenger392_12, DOD_RamSRT10_06,
  FOR_1_Fusion_11, HYU_VelosterRMR_11, MCL_41_McLarenF1GTR_98, MCL_43_McLarenF1GTR_97,
  MIT_HKS_06, PEU_3_905EVO_93, POR_7_RSSpyder_08, SUZ_1_SX4_11), 709 the same. `build
  --affected` converted those 12 (411 s at 2 slots), and each new fingerprint carries the
  changed unit; their chunks are byte-identical to the release build's (the rewrite
  changes no output). `build --affected --before` took the 12 from the store (127 s). The
  first 4 roster cars, "the same", converted fresh with the change in place (`--no-store`):
  no changed unit recorded, chunks identical.
- A fingerprint records the code a car ran, which can depend on a cache: a car whose
  sound banks or damage host were cached in one build and computed in the other records
  the decoding units only in the second. A car that read the cache depends on the cache
  file (an input), not on the code that wrote it, as for `--reuse`; `affected` answers the
  same way.

### Builds past 4 GiB

A CHNK's `total_size` and every entry offset are u32 (`chnk.HEADER`,
`chnk.ENTRY`), so one archive holds at most 4 GiB - 1 bytes
(`chnk.SIZE_LIMIT`). The 573 disc cars plus the 148 FM4 DLC cars (#499)
lay out to more than that (vanilla 0.45 GiB plus 3.71 GiB of car chunks),
and the merge failed in `write_chunk` (`struct.error`). Players install
car packages (#485), which hold each car's own chunk and never read the
merged archive; only the in-game tests (`run-series.ps1 -Archives`) do.

`register_cars` now lays the archive out before writing it. When it is
over the limit, or with `--no-archive`, it writes nothing (and removes an
earlier build's `dngvehicles.sp`), and the steps that read it work
without it:

- **lint**: `<build>/dngvehicles.plan.json` lists what the merge
  registered (each car's chunk, the ID it holds, its spool ID and host, in
  merge order). `upvh lint-many --plan` rebuilds the merged root in memory
  (`port_batch.merged_view`: the same `MultiStream` and `register_clones`
  calls as the merge, nothing written), so lint sees the same entries,
  SSLP and VSCT records, and its cross-vehicle checks (UIDs another
  vehicle defines, sound bank IDs) cover every car as before. Readers go
  through `read_leaf`/`payload`, which take the merge's edited SSLP and
  VSCT payloads.
- **garage pictures**: a render reads the car's three VEHC entries and the
  SSIC's shared materials and textures (`car_render._load_scene`);
  `register_clones` changes only SSIC's SSLP and VSCT. So
  `port_batch.Shards` writes archives of the vanilla one plus runs of the
  cars `gui_assets.build` will render (not those it takes from an earlier
  pack), each laid out below the limit, into `<build>/shards/`, and
  removes them after the GUI step.
- **packages**: `dsfcar.pack` reads `work/<media>.chnk`, the manifest and
  the pack's pictures and images, never the archive.

The pack record is unchanged (`--no-archive` is in its settings only when
given), so whole-pack reuse and `--verify-pack` work as before;
`compare_packs` counts an archive neither pack wrote as the same.

Measured 2026-11-04 (16 cores, `--gui --packages --wide-glyphs --id-policy
asi --root-limit 3003`):

- 721 cars (573 disc + 148 FM4 DLC): merge 18 s laid it out at 4.16 GiB
  and wrote nothing; lint over the plan 81 s, all 721 ok; the renders read
  two shards (690 cars, 4.00 GiB; 31 cars, 0.61 GiB), GUI 300 s; 721
  packages.
- The 573 disc cars with the archive (3.37 GiB) and with `--no-archive`
  (one shard): every package member (car.sp, picture.dds, badge.png,
  plate.png, car.json) byte-identical, and so are `gui/`, the Lua and the
  manifest (spool IDs, VEHC records, lint). Both again against the 721-car
  build (two shards): every member of the 573 shared packages identical.
  The zip files differ only in their entries' timestamps.
- A rerun of the `--no-archive` build in its folder took the pack whole.

A batch archive is byte-identical to a `port_car.py` archive with the same
cars and IDs: tested with `FOR_FocusRS500_10` at 304, SHA-256 equal.

Failed cars are left out, and `batch-report.json` lists each one with its
cause. Every `port_car.py` option passes through `--port "..."`. The default
is `--mdxn-limit 0x2C0000 --damage`, the configuration tested in game.

`dsff game_data prepare` makes the derived files the tools read on a
machine that has only the game and the discs:

- `vehiclestats.lua`, made from `ScriptsLuaScripts.fchunk` by a small Lua 5.1
  constant-chunk evaluator, byte-identical to unluac's output;
- each game's `gamedb.slt`.

Every tool reads the game folder from `DSF_GAME_DIR`; there is no built-in
default.

## Batch 1 (tools/batches/first.txt, out/batch/first)

The batch has 31 cars across seven classes: sports, muscle, saloons,
hatches, SUVs/pickups/vans, classics and exotics. It was built with:

```sh
port_batch.py build --out out/batch/first --list tools/batches/first.txt --jobs 6 --gui
```

The port options were the defaults (`--mdxn-limit 0x2C0000 --damage`, auto
hosts, DSF-first handling, cockpit) on a Ryzen 7 5800X3D (8 cores) with
32 GB. Nothing has been tested in game yet.

**Result: all 31 built, linted clean, and all got garage IDs 1 to 31.**

- The archive is 590,426,112 bytes (563 MiB). Vanilla is 483,110,912, so the
  31 cars add about 3.4 MB each.
- The archive has 546 root entries, and `chnk.py rebuild --verify`
  reproduces it byte for byte.

| | First run | Rerun with `--resume` and fallbacks |
| --- | --- | --- |
| Converted | 31 cars, 27 ok | 4 cars (27 reused), 4 ok |
| Conversion time (6 workers) | 666 s | 334 s |
| Merge (write 551 MiB) | 3.6 s | 3.1 s |
| Lint | 13 s | 18 s |
| GUI assets (24 FH1 thumbnails, 7 renders) | 214 s | 206 s |
| Total | 898 s (15 min) | 563 s |

- Per car (worker wall time, 6 at a time): 37 to 334 s, median about 125 s.
- A single `port_car.py` run of one car takes 153 s.

Failures on the first try:

| Car | Cause | Fallback | Result |
| --- | --- | --- | --- |
| DOD_ChallengerSRT8392_12 (host 173) | `damage.py`: `Uv1Field.read` on the host's interior gets a vertex element `None` (`vehicle_mesh.py` line 60, `'NoneType' object has no attribute 'type'`) | without `--damage` | ok, no vanilla-like damage |
| FIA_500AbarthSS_10 (host 184) | the same `damage.py` error | without `--damage` | ok, no vanilla-like damage |
| DOD_Durango_99 | `cockpit.py fit`: "cockpit has no steering_wheel section" | `--no-cockpit` (host interior) | ok |
| CHE_Express2500Cargo_05 | the same cockpit error | `--no-cockpit` | ok |

| ID | Car | Game | Host | s | Note |
| ---: | --- | --- | --- | ---: | --- |
| 1 | BMW_M3E30_91 | FH1 | 205 Jaguar E-Type | 156 | |
| 2 | TOY_SupraRZ_98 | FH1 | 229 Skyline R33 | 122 | |
| 3 | MAZ_RX7_97 | FH1 | 278 Solstice GXP | 263 | |
| 4 | HON_S2000_03 | FH1 | 278 Solstice GXP | 226 | |
| 5 | NIS_SilviaSpecR_00 | FH1 | 229 Skyline R33 | 147 | |
| 6 | SUB_WRXSTi_11 | FH1 | 136 Audi A4 | 104 | |
| 7 | CHR_Cuda426_71 | FH1 | 172 Challenger R/T 1970 | 113 | |
| 8 | DOD_ChallengerSRT8392_12 | FH1 | 173 Challenger SRT8 | 40 | without damage |
| 9 | PON_FirebirdTransAm_77 | FH1 | 238 Trans Am 1977 | 121 | |
| 10 | DOD_ChargerDaytona_69 | FM4 | 176 Charger SRT8 | 163 | (`plan` ranked 175 Charger R/T 1969 first; with the extracted carbin, `_carbin_doors` counts the 2-door Daytona as 4 doors (glassLM), which tips it to the 2009 four-door) |
| 11 | MER_S65AMG_12 | FH1 | 134 Aston Martin Rapide | 81 | |
| 12 | LEX_ISF_09 | FH1 | 206 Jaguar XF-R | 89 | |
| 13 | CHR_300C_08 | FM4 | 176 Charger SRT8 | 128 | |
| 14 | BMW_M5E28_88 | FM4 | 124 Alfa 159 | 107 | |
| 15 | VW_GolfR_10 | FH1 | 274 Scirocco | 85 | |
| 16 | FIA_500AbarthSS_10 | FH1 | 184 Fiat 500 Abarth 1971 | 37 | without damage |
| 17 | HON_CivicR_04 | FH1 | 127 Giulietta | 160 | |
| 18 | PEU_205T16_84 | FH1 | 127 Giulietta | 138 | |
| 19 | DOD_Durango_99 | FH1 | 138 Audi Q7 | 215 | host interior |
| 20 | CHR_JeepGCSRT8_12 | FH1 | 138 Audi Q7 | 100 | |
| 21 | LR_RangeRover_12 | FH1 | 239 Range Rover Sport | 138 | |
| 22 | SAL_S5SRaptor_10 | FH1 | 163 Corvette ZR1 | 66 | the S5S Raptor is a mid-engined sports car, so the host fits; batch 1's `plan` called it a pickup ("raptor") |
| 23 | CHE_Express2500Cargo_05 | FH1 | 161 Corvette 1960 | 334 | host interior; wrong class of host (van) |
| 24 | MER_300SL_54 | FH1 | 205 Jaguar E-Type | 105 | |
| 25 | FER_250GTO_64 | FH1 | 210 Countach | 155 | |
| 26 | TOY_2000GT_69 | FM4 | 205 Jaguar E-Type | 102 | |
| 27 | DEL_DMC12_82 | FM4 | 171 DeLorean (same model) | 88 | |
| 28 | BUG_VeyronSS_11 | FH1 | 214 Murcielago LP670 SV | 67 | |
| 29 | KOE_Agera_11 | FH1 | 225 MP4-12C | 182 | |
| 30 | LEX_LFA_10 | FH1 | 212 Gallardo | 128 | no nameplate glyph left (shows the host's) |
| 31 | PAG_Huayra_12 | FH1 | 232 Zonda Cinque | 150 | no nameplate glyph left (shows the host's) |

Every car got the first LOD step: full entry Forza LOD 3/4/5, exterior LOD 1.

### Findings for other tools

- **`host_select.py`:** FH1 vans, buses and trucks have Forza
  `FamilyBodyID` coupe, so they get car hosts.
  - CHE_Express2500Cargo_05 went onto the Corvette 1960.
  - BUS_TOUR_12 and KEN_T440_12 would also get car hosts (seen in `plan`).
  - Batch 1's `plan` called the Saleen S5S Raptor a pickup (the word
    "raptor"), but it is a sports car, so the Corvette ZR1 host fits.
  - These and the door count below were fixed in `host_select.py` after
    batch 1 (`hosts.md`, `host-picks.tsv`). Batch 2 gets the Express van on
    198 GMC Savana and the Charger Daytona on 175.
- **`host_select.py` door count:** `_carbin_doors` takes glassLM/RM as rear-door windows, so the 2-door Charger Daytona counts as 4 doors and gets host 176 (2009 four-door) over 175 (1969 R/T).
- **`damage.py`:** fails on hosts 173 and 184 (see above).
- **`cockpit.py`:** two FH1 cockpits (Durango, Express van) have no
  `steering_wheel` section. `port_car` could fall back to the host's
  interior by itself.
- **`gui_assets.py`:**
  - **Glyph budget.** 44 codes are free. This batch used all of them: 15 new
    badges plus 29 nameplates. 32 of the codes come from the untested
    cp1252 range 128 to 159.
  - **Over 31 cars**, a batch needs more codes (by reusing vanilla glyphs, or
    a second font page) or has to drop nameplates.
  - **Nameplate text comes from the media name.** The results look like
    "WRXS TI", "S65AMG", "M5E28", "205T16" and "CHALLENGER SRT8392". A
    `--name` table or Forza's own strings would fix them.
  - **Maker matching.** "MERCEDES-BENZ" didn't match DSF's Mercedes, so it
    got a new badge glyph and garage group. Neither did "CHRYSLER" (DSF uses
    Dodge). The upper-cased maker string match needs aliases.
  - **gui.sp** takes about 98 new pictures in one patched file (the root
    table has 100 free rows).
- **Shared caches:** `out/cache/host-survey.json` and `dsf-fleet.json` are
  keyed by the archive path. A build against another game folder
  overwrites them, and they are recomputed (correct, just slower).

### Installer test (scratch tree only)

`out/fakegame/` holds copies of exactly the files the build and the
installer touch:

- `Install\dngvehicles.sp`, `gui.sp`;
- `media\Fonts.fchunk`, `ScriptsLuaScripts.fchunk`;
- `Locale\English\global.fchunk`;
- a dummy `Driver.exe`;
- a foreign `Common\UserLuaScripts\bootstrap.lua`.

No links were used. The real game folder was only read. The fake ASI loader
is a copy of the user's Ultimate ASI Loader (`winmm.dll`, renamed
`dinput8.dll`).

| Test | Result |
| --- | --- |
| foreign `bootstrap.lua` without `-Force` | refuses before changing anything (no backup folder) |
| install `-Build out\batch\first -Force -WriteProfile` | 3 files replaced, 16 created, 5 s; `-Status` shows every file "as installed" |
| `-Uninstall` | 4 files restored and SHA-256-checked, 16 removed, empty folders removed; the whole tree is byte-identical to before (SHA-256 of every file) |
| install interrupted mid-way (pipeline stopped), then `-Uninstall` | recovers: "already gone" files are skipped, the rest restored; tree identical |
| `-Force` over an existing install (2 cars, then 31) | uninstalls first, then installs; then `-Uninstall` gives an identical tree |
| hook and ASI loader already present (dummy `DriverNGHook.asi`, `winmm.dll` loader) | left alone, not in the manifest |
| a user-edited `hud.lua` | kept on uninstall and reported |
| `dngvehicles.sp` not vanilla | refuses (SHA-256 shown) |
| build path: `-Car FOR_FocusRS500_10 -BuildDir out\batch\bd-test` via `powershell.exe -File` | builds (Python found, `DSF_GAME_DIR` = the fake tree, gui assets from the fake tree), installs, uninstalls; tree identical |

Not tested:

- `-Download` (GitHub fetch);
- a machine without the VC++ x86 runtime (only the check: the registry key,
  else `SysWOW64\vcruntime140.dll` and `msvcp140.dll`);
- an admin-protected game folder.

### Resumable uninstall (after a failure on the real install)

**The failure.** A `-Force` reinstall on the real install restored all three
files. Then deleting `dsfcars-backup\` failed on `Install\gui.sp`, because
another process (a test run) held it. `Remove-Item -Recurse` had already
deleted the `dngvehicles.sp` backup. `install.json` still listed every file,
so the next run stopped with "MISSING backup".

**What `install.ps1` does now:**

- **Uninstall marks progress.** Each `install.json` entry gets `done` as soon
  as it is handled, and the file is rewritten atomically (`.tmp` + move).
- **Uninstall skips finished work.** A replaced file whose game copy already
  has the original SHA-256 counts as restored, with or without a backup.
- **Uninstall ends cleanly.** The manifest is marked `state: uninstalled`
  before the folder goes.
  - Backups are deleted first, retrying five times two seconds apart, and
    `install.json` last.
  - A file that stays locked only gets a warning.
- **Install records as it goes.**
  - It writes `install.json` (`state: installing`) before the first file
    and after every file.
  - A replaced file is recorded before it is overwritten, so a killed install
    can be undone.
- **A leftover backup folder** (no install recorded, or one marked
  uninstalled) is checked file by file:
  - a backup equal to the game file, or a game file that is vanilla, needs
    nothing;
  - a vanilla backup over a non-vanilla game file is put back with `-Force`
    (refused without it).
  - The old manifest is renamed `install.json.<stamp>.old`.
  - `Install-File` reuses a backup that already equals the game file, so a
    locked leftover is no obstacle.
- **Vanilla check.** Vanilla SHA-256s for `Install\gui.sp`, `media\Fonts.fchunk`
  and `Locale\English\global.fchunk` (the `out/vanilla` copies) join the
  archive check.
  - A changed archive is always refused.
  - The other three are refused unless `-Force`.
  - Other languages' `global.fchunk` hashes are unknown and not checked.
- `Locale\<lang>\global.fchunk` from the build's `gui\Locale` is installed
  when the game has that language.

**Tests** (`out/scratch/test_install.ps1`, `out/fakegame` only).
`out/scratch/testbuild` hard-links batch 2 plus a modified `global.fchunk`.
A second `powershell.exe` holds the backup `gui.sp` open (read sharing,
undeletable). All 22 checks pass:

| Case | Result |
| --- | --- |
| install with `Locale\English\global.fchunk`, uninstall | identical tree |
| uninstall with the backup `gui.sp` locked | succeeds; files restored, warning, manifest `uninstalled`, `-Status` "not installed (a leftover ...)" |
| `-Force` install while the leftover is still locked | reuses the backup; installs |
| the reported case: all files restored to vanilla, `dngvehicles.sp` backup deleted, manifest listing everything | `-Force` reinstall works ("already restored") |
| missing backup of a still-installed file | uninstall stops; after the backup is back, a second `-Uninstall` resumes; identical tree |
| leftover vanilla backup, non-vanilla `gui.sp`, no manifest | refused; with `-Force` restored from the leftover, then installed |
| modded `Fonts.fchunk` | refused; with `-Force` installed, uninstall gives back the modded file byte for byte |

At the end the tree matched the snapshot taken before all tests, every file
by SHA-256.

## Batch 2 (out/batch/second, out/batch/second-high)

This rebuilds the same 31-car list after the fixes in `host_select.py`,
`damage.py`, `cockpit.py` and `gui_assets.py`. `port_batch.py` now names
garage groups as `gui_assets` will: Forza's own maker name via
`car_texts`/`dsf_make`, mapped to the vanilla `"ID:nnnnnn"` maker string.
Built with the default policy:

```sh
port_batch.py build --out out/batch/second --list tools/batches/first.txt --jobs 6 --gui
```

**Result: all 31 ok on the first try, no fallbacks needed, lint clean.**

- **Time:** 1408 s in total (conversion 1053 s, GUI 331 s). The PC was
  shared with other agents' jobs. The FM4 renders and the newer damage and
  cockpit steps also take longer.
- **Size:** the archive is 590,573,568 bytes.
- **Glyphs:**
  - 14 new badges and 30 nameplates, from Forza's own string tables (for
    example "WRX STI", "CUDA 426 HEMI", "VEYRON SUPER SPORT"). All 44 codes
    are used and none is blank.
  - DMC 12 uses the vanilla DeLorean nameplate.
  - The Jeep and Range Rover use the vanilla Jeep and Range Rover badges.
- **New hosts:**
  - 23 Express van → 198 GMC Savana;
  - 10 Charger Daytona → 175 Charger R/T 1969;
  - 12 IS F and 13 300C → 176 Charger SRT8;
  - 17 Civic R and 18 205 T16 → 274 Scirocco;
  - 29 Agera → 163 Corvette ZR1;
  - 30 LFA → 182 Viper ACR.

`register.lua` (stub run with this `ported.lua`/`hud.lua`) puts every car in
the garage:

| Maker group | IDs |
| --- | --- |
| new: BMW | 1, 14 |
| new: TOYOTA | 2, 26 |
| new: MAZDA | 3 |
| new: HONDA | 4, 17 |
| shared: FORZA | 6, 7, 11, 12, 13, 18, 22, 24, 25, 28, 29, 30 (12 cars) |
| vanilla makers | Nissan 5; Dodge 8, 10, 19; Pontiac 9; VW 15; Fiat 16; Jeep 20; Range Rover 21; Chevrolet 23; DMC 27; Pagani 31 |

That makes 40 groups, the largest with 17 cars.

`out/batch/second-high` is the same cars at IDs 304 to 334
(`remerge --id-policy high --gui`, 354 s). In that range, register.lua's
guard covers both 304 to 319 and 320 to 334.

## In-game test plan (batch 2)

Run from the repo root. The plan was written for the sandbox; on this PC
the sandbox guest bugchecks (`in-game-tests.md`), so add `-Vm` (headless VM,
started by `run-series.ps1`) to each command. Since #184 an archive named
`dngvehicles.sp` brings its build's `dngvehicles.lua`, `hud.lua`, `gui.sp`,
`Fonts.fchunk` and `global.fchunk` into the run by itself
(`in-game-tests.md`, "Stray badges and plates in harness shots"), so the
`Copy-Item` lines and `-Override` below are no longer needed.

What the harness provides:

- `run-test.ps1` copies `tools\game\lua\*` into the guest; `-LuaFiles`
  puts the build's `ported.lua` and `hud.lua` over the copies for one run
  (the commands below predate it and copy them into `tools\game\lua\dsfcars\`,
  backing up the shared files first; `-LuaFiles 'dsfcars\ported.lua=<build>\dngvehicles.lua;dsfcars\hud.lua=<build>\gui\hud.lua'`
  does the same without touching them).
- `-Override` links (under `-Vm`: copies) the build's `gui.sp` and
  `Fonts.fchunk` over the game's.
- `garagetest.lua` (loaded by the test bootstrap) logs the engine's
  `getVehicleStats` calls for ported IDs, and "garage list built" when the
  garage maker loop runs.

Pass criteria per run:

- `result.json` stage finished, not crashed;
- `id N: PLAYER IS DRIVING IT` for each probe ID;
- the chase shots show the badge and nameplate from `hud.lua`;
- the cockpit shots show the converted interior.

```powershell
# Shared Lua files: keep the current ones
Copy-Item tools\game\lua\dsfcars\ported.lua out\scratch\ported.lua.bak -ErrorAction SilentlyContinue
Copy-Item tools\game\lua\dsfcars\hud.lua out\scratch\hud.lua.bak -ErrorAction SilentlyContinue

# Runs 1-3: IDs below 62 (garage range), 10 per session
Copy-Item out\batch\second\dngvehicles.lua tools\game\lua\dsfcars\ported.lua
Copy-Item out\batch\second\gui\hud.lua tools\game\lua\dsfcars\hud.lua
$ov = 'Install\gui.sp=out\batch\second\gui\gui.sp;media\Fonts.fchunk=out\batch\second\gui\Fonts.fchunk'
.\tools\wsb\run-series.ps1 -Archives out\batch\second\dngvehicles.sp -Override $ov -ProbeIds '1,2,3,4,5,6,7,8,9,10' -WaitSeconds 1200 -ShotSeconds 4 -DriveSeconds 8
.\tools\wsb\run-series.ps1 -Archives out\batch\second\dngvehicles.sp -Override $ov -ProbeIds '11,12,13,14,15,16,17,18,19,20' -WaitSeconds 1200 -ShotSeconds 4 -DriveSeconds 8
.\tools\wsb\run-series.ps1 -Archives out\batch\second\dngvehicles.sp -Override $ov -ProbeIds '21,22,23,24,25,26,27,28,29,30' -WaitSeconds 1200 -ShotSeconds 4 -DriveSeconds 8

# Run 4: the 304+ range (same cars), across the 304-319 / 320+ save boundary
Copy-Item out\batch\second-high\dngvehicles.lua tools\game\lua\dsfcars\ported.lua
Copy-Item out\batch\second-high\gui\hud.lua tools\game\lua\dsfcars\hud.lua
$ovh = 'Install\gui.sp=out\batch\second-high\gui\gui.sp;media\Fonts.fchunk=out\batch\second-high\gui\Fonts.fchunk'
.\tools\wsb\run-series.ps1 -Archives out\batch\second-high\dngvehicles.sp -Override $ovh -ProbeIds '304,310,318,319,320,326,333,334' -WaitSeconds 1000 -ShotSeconds 4 -DriveSeconds 8

# Run 5: garage. Car 31 is driven, then garagetest opens garage 1 (Fisherman's Wharf)
# after about 20 s in the car, first marking IDs 1..31 unlocked and owned in the
# sandbox's save copy (the host save is mapped read-only; the copy keeps the flags
# until the sandbox restarts; the VM's copy is refreshed from the host every series).
Copy-Item out\batch\second\dngvehicles.lua tools\game\lua\dsfcars\ported.lua
Copy-Item out\batch\second\gui\hud.lua tools\game\lua\dsfcars\hud.lua
Set-Content tools\game\lua\dsfcars\garagetest_on.lua 'return { garage = 1, delay = 40, unlock = true }' -Encoding ASCII
.\tools\wsb\run-series.ps1 -Archives out\batch\second\dngvehicles.sp -Override $ov -ProbeIds '31' -WaitSeconds 420 -ShotSeconds 5
Remove-Item tools\game\lua\dsfcars\garagetest_on.lua

# Put the shared files back
Copy-Item out\scratch\ported.lua.bak tools\game\lua\dsfcars\ported.lua -ErrorAction SilentlyContinue
Copy-Item out\scratch\hud.lua.bak tools\game\lua\dsfcars\hud.lua -ErrorAction SilentlyContinue
```

Reading the garage run (`Driver.log`, `[dsfforza.garage]` lines, screenshots):

- **"opening garage 1"** then `garage.enterGarage: true`: the garage menu
  opened. The menu has its own camera and screens; without `-MenuKeys` the
  shots show its first screen (`-MenuKeys 'Enter:3,Down:0.5,...'` steps
  through it once that line appears).
- **"garage list built"**: the engine ran the maker-list loop (it asked for
  model ID 0).
  - `getVehicleStats(N) first call` lines for IDs 1 to 31 then show the
    engine read our entries, with `Selectable true` and the group name.
  - IDs 304 and up never appear here by design.
- **To check by hand** (or with `-MenuKeys`):
  - each maker group in the list should hold the cars in the table above;
  - the FORZA group label;
  - the `dsfforza_*` garage pictures;
  - picking a car spawns it (`Garages.SpoolVehicleModel`, one car).
- **If no "garage list built" appears:** the maker list is built only on a
  later screen. Then go into the garage by hand in a normal session (drive
  into Fisherman's Wharf, the first garage in the story) with the build
  installed.

## Full roster, first build (tools/batches/all.txt, out/all)

The roster is every car on the discs: the 573 `car` rows of
`forza-roster.tsv`. The 25 Autovista models have no LOD1-4 main carbin and
are left out. The list puts the 40 test-drive cars first, so they keep IDs
1 to 40, and the rest follow in roster order. Built from master 92fddd4
(before the package move, so without #21 and later) on the 5800X3D while the
PC was in use:

```sh
python tools/port_batch.py build --list tools/batches/all.txt --gui --wide-glyphs \
    --id-policy asi --root-limit 3003 --out out/all --jobs 8
```

`--root-limit 3003` is needed on top of `--id-policy asi`: 573 cars are
453 + 3 x 573 = 2172 root entries, past the vanilla 1100.

**Result: 562 of 573 converted, 11 failed, and 14 of the 562 fail lint.**

- Conversion took 4 h 40 min (8 workers, about 300 s per car with the old
  code). Merge and lint took another 30 min.
- The GUI step was stopped part-way: the old code rendered one car at a
  time and re-read the 2.75 GB archive for each render. #21 fixed that, so
  the final build makes the GUI.
- No car needed a batch fallback (no `--damage` or cockpit retry).

### Capacity for 573 cars

| Limit | Needed | Room | Verdict |
| --- | --- | --- | --- |
| Model IDs (`--id-policy asi`) | 573 IDs: 1..61, the 92 vanilla gaps in 62..303, then 304..725 | 849 free IDs below 1000 | fits |
| Root entries in `dngvehicles.sp` | 2,139 (453 + 3 x 562) | 16,384 with `[archive]` (pass `--root-limit 3003`) | fits |
| Archive size | 2,750,959,616 bytes (2.56 GiB) for 562 cars | 4 GiB with `[archive] large_files`; the vanilla reader stops at 2 GiB | fits, about 1.4 GiB spare |
| Per-car size, 3 VEHC entries | median 3.89 MiB (p5 3.49, p95 4.17), max 4.69 (KOE_CCXR_09), min 1.81 (AUD_A4Avant_04); 2,156 MiB for all 562 | no per-car cap besides MDXN | No car is above 5 MiB. The 1.8 to 2 MiB cars are FH1's low-detail cars (Durango, PT Cruiser, Prius, RAV4, Leaf, tour bus...), whose Forza cockpit is only `interior`, `seatR` and `steering_wheel`. |
| MDXN per entry | largest 0x200800 (2,099,200 bytes) | `--mdxn-limit 0x2C0000` | 73% of the cap. Recheck after #23 (badge cutouts) and #27 (chrome). |
| Resource hash arena (45,000 nodes) | bounded by what is resident, not by the archive | free list (below) | not a limit for the roster |
| Garage maker groups | 112 maker groups, plus 139 cars in 8 shared groups `FORZA`..`FORZA 8` (`plan`) | 120 groups of 20 (`[garage] max_groups`) | Fits. The extra cars of makers with more than 20 (Ford, Chevrolet, Nissan, Porsche...) go into the mixed FORZA groups. |
| Garage IDs | 573 cars, all below 1000 | `[garage] max_id=1000` | fits |
| HUD glyphs | one 16-bit badge or plate per car without a vanilla one | `--wide-glyphs`: codes 0x100..0xD7FF, about 800 plates in 4 pages | fits. In game they drew nothing until the renderer's blank-text checks were patched (`gui-assets.md`, "Why the 16-bit glyphs drew nothing") |
| gui.sp pictures | 562 pictures; gui.sp already holds 923 entries | 177 one-per-SPOI below 1100, shared SPOIs beyond | `port_batch` passes no root limit to `gui_assets.build`, so 385 pictures share SPOIs (untested in game). The fix passes the archive's root limit through. |

**Resource hash arena (confirmed-static; Ghidra on the `-b` project,
`out/ghidra/arena1..3`).**

- ResourceHashPool_Acquire (0x00BAED90) takes a node from the free list at
  owner +0x127AC0 first. It bumps the arena pointer (+0x127AC4) only when
  that list is empty.
- ResourceManager_ReleaseHandlePair (0x00BAECA0) drops a holder's reference
  count. At zero it calls ResourceHashPool_Remove (0x00BAEEA0), which unlinks
  the node and pushes it back onto the free list.
- So the 45,000 nodes bound the resources resident at once (35 + 2 + 2
  vehicle slots plus the world), not the number of distinct cars ever
  spooled. Traffic that cycles through 562 cars reuses nodes.
- The handle-ID pool next to it (0x32DF = 13,023 u16 IDs at +0x157ACC) is
  also a free list and behaves the same way.

### Failures (11)

| Cause | Cars | What is wrong |
| --- | --- | --- |
| `forza2dsf.load_car`: "no wheel section" (8) | FER_360_99, FER_360Challenge_03, FER_Dino246_69, LAM_Gallardo_05, PAN_Esperante_05, CHE_CORVETTEGSC_10, AUS_MiniCooperS_65, AUS_ShowCaseMiniCooperS_65 | The main carbin has no `wheel` section. The first five keep it only in the wheel archive's carbin (`media/wheels/<car>`, one section `wheel`). The Minis name it `wheela`/`wheelb`, like upgrade variants, and `vanilla_sections` keeps `wheela` in the body, so the wheel would also be drawn as body. The Corvette Grand Sport's and the Mini's wheel-archive carbins don't parse (next row). |
| `carbin.parse`: "no parsable sections found" (1) | FIA_595_68 | The first section (`roof1`) is empty: 0 vertices, bounds FLT_MAX / -FLT_MAX. `_looks_like_section` rejects any float of magnitude 100 or more, so it accepts no start position. From 0xDB0 all 34 sections parse. The wheel-archive carbins of the Grand Sport, the Mini and the 595 fail later, in the section tail. Their LOD0 pool is skinned, so the tail's per-vertex record is 16 bytes instead of 4 and the tail starts 4 bytes earlier. The geometry before the tail parses. |
| `forza_materials`: "nodamage.xds not in ..." (2) | CHE_CamaroZ28_79, PON_TransAm_73 (FM4 1950-2000 pack) | No `nodamage.xds`, only colour variants: `nodamage_blue/gold/orange/yellow.xds` and their `_lod0`, plus `damage.xds`. |

### Lint failures (14 of 562)

| Lint check | Cars |
| --- | --- |
| kind 1: "interior glass has crack lines for states 1-2 and rim shards for state 3" (crack and shard counts all 0; surface triangles only) | AUD_14_R10_10, AUD_2_R10_08, AUD_2_R15TDI_09, AUD_2_R8_06, BMW_15_LMR_99, CHE_55/89/99_FLM09_10, MAZ_MX5SL_10, MER_SLRStirlingMoss_10, PAN_11_LMP_03: open race cars and roadsters, whose only interior glass is a small windscreen or wind deflector |
| kind 1: "interior glass triangles each inside one vanilla interior region" (448, 611 and 460 straddle) | CHE_Express2500Cargo_05, FOR_F350SuperDuty_08, FOR_TaurusSHO_10: the three stub cockpits that keep the host's interior since #18 (the Express on the Savana, the F-350 and the Taurus on their same-model hosts) |

### Other report flags (built cars, not failures)

| Flag | Cars | Note |
| --- | ---: | --- |
| front plate at the host's position ("no flat face of plate size") | 279 | expected for cars without a front plate recess |
| rear plate at the host's position | 82 | mostly race cars |
| cockpit without needle gauges | 128 | race cars with digital dashes, and FH1's low-detail cockpits |
| some lamps left unlit | 22 | e.g. LEX_ISF_09, NIS_370Z_10, BMW_1M_11, FER_F50_95 |
| no head lamp found | 10 | Race cars (Corvette C5-R, CLK GTR, McLaren F1 GTR, 905 EVO...), but also CHE_CorvetteGS_10, DOD_RamSRT10_06 and FOR_MustangSVTCobraR_93. |
| no tail lamp found | 2 | CHE_2_MonteCarloSS_11, FOR_1_Fusion_11 (race liveries of vanilla cars) |
| dropped Forza plates | 18 | FH1 low-detail cars and stub cockpits |
| host engine sound | 8 | CHE_Volt_11 and NIS_Leaf_11 are electric (bank `NA.fsb`, expected). AUD_Q7_09, CHE_Corvette_60, HYU_GenesisCoupe_10, MAZ_16_B09_09, sal_s281_06 and VW_IECorrado_95 have no `<car>_ET.xml` in any EngineTuning zip. |
| host score below 50 | 9 | small city cars on the Cygnet (C1, 107, Aygo), the tour bus, the T440 truck, the Leaf, both Veyrons |

- All 562 cars got DSF-first handling (`handling.source` dsf).
- They use 85 distinct hosts. The most used are 162 Corvette Z06 (55 cars,
  mostly race cars), 227 370Z (30) and 274 Scirocco (29).
- Games: 392 FM4, 170 FH1.
- Every car got full-entry LOD steps 3/4/5 and exterior LOD 1 at the first
  attempt.

### Fixes (phase 2, one PR each)

| PR | Cause | Cars | Result |
| --- | --- | ---: | --- |
| #33 | carbin parser: empty sections (FLT_MAX bounds), skinned section tails, binary names from wrong starts | FIA_595_68 (+ 102 wheel-archive carbins) | over all 1,665 carbins of the roster: 1,561 identical, 103 newly parse, 1 changed (Miata 94's wheel archive, from a garbage start to `wheel`) |
| #35 (on #33) | no `wheel` section in the main carbin | 8 | wheel from `wheela` (Minis) or the wheel archive's carbin (360, 360 Challenge, Dino, Gallardo 05, Esperante, Corvette GS) |
| #32 | only colour variants of `nodamage.xds` | 2 | Camaro Z28 79, Trans Am 73 build |
| #36 | interior glass lint: stub cockpits check the host's vanilla interior; lamp-only cockpits can't have cracks | 14 lint | 562 of 562 lint ok |
| #37 | ET file on the other game's disc, or only for the base model | 6 | Forza engine sound 6 of 6 |
| #38 | no headlamp technique (reflector under a clear lens) | 10 | 8 get headlamps; the 2 NASCARs have decal headlights |
| #34 | `--root-limit` not passed to the GUI step | all | gui.sp 1485 entries, one SPOI per picture |
| #39 | makers with more than 20 cars spill into mixed FORZA groups | 121 | "FORD 2", "CHEVROLET 3"...: 104 groups, no FORZA group |

All PRs keep the 4-car regression archive byte-identical
(`8459AAF9...EC5A`). Not fixed: CHE_Volt_11 and NIS_Leaf_11 keep the host's
engine sound (Forza's bank for electric cars is `NA.fsb`, which isn't on
either disc).

Later: #42 replaced #39's "<maker> 2" groups with one group of any size
(a moving window of cards), and #43 lifted the SPOL cap below.

## Full roster, final build (out/final)

Built from master 67b66f0, which has every fix above:

```sh
uv run dsff port_batch build --list tools/batches/all.txt --gui --wide-glyphs --id-policy asi --root-limit 3003 --out out/final --jobs 8
```

The GUI step was re-run on master e992b4d with
`port_batch remerge out/final --out out/final-gui2 ...`. That rebuilt the
archive byte-identically (SHA-256 `60288d80...a031e8`), with #41's pictures
and #42's groups.

- **573 of 573 cars built, 0 failed, all lint ok.** Conversion took 2286 s
  and the whole build 3446 s on 8 workers (out/final has fingerprints for
  `--reuse`).
- **Archive:** 2,839,646,208 bytes (2.64 GiB), 2172 root entries; gui.sp
  1485 entries (one SPOI per picture).
- **Per car:** median 3.96 MiB, max 5.25 MiB (KOE_CCXR_09). Largest MDXN
  0x205800 (MAS_15_MC12_05), 74% of `--mdxn-limit 0x2C0000`; it was 0x200800
  in the first build, so chrome (#27) and badge cutouts (#23) added little.
- **Engine sound:** Forza sound on 571 of 573 cars (the Volt and the Leaf
  keep the host's).
- **Badge cutouts (#23):** 44,104 quads cut. 8 cars have 1 to 4 fallback
  quads each: AST_V12Vantage_10, BER_Mantide_10, FER_FXX_05, GMC_Syclone_92,
  LOT_EvoraType124_10, NIS_Fairlady432Z_69, REN_Clio197_07, SUZ_SX4_11.
- **Cockpit bonnets (#27):** no cockpit lost its bonnet against the first
  build. 35 cockpits have no `hood*` section in either build: FH1's
  low-detail cars and mid-engined cars.
- **`dsff driver_survey cars`** (573 cars, 537 measured):
  - anchor off the vanilla point: 0.0 cm for every car;
  - grips off the drawn rim: median 0.0 cm, 20 cars over 3 cm (worst
    FOR_17_FGFalcon_11, 9.1 cm);
  - room over the driver's eyes: median 18.1 cm. 25 cars are under the
    lowest vanilla car's 8.7 cm and 5 under 3 cm: BEN_7_Speed8_03 0.0 cm,
    DOD_ChallengerHemi_70 0.5, ACU_NSX_05 2.1, GUM_ApolloS_10 2.1,
    FOR_1_Fusion_11 2.1. Then MIT_HKS_06, BUS_TOUR_12,
    CHE_2_MonteCarloSS_11, TOY_2000GT_69 and the FG Falcons follow, at 3.8
    to 5.0 cm.

### In game (headless VM, runs vm-20261024-205021 to vm-20261025-011752)

- **Drive sample, IDs past 304** (`vm-20261024-205021`, with traffic of all
  562 cars): spooled, driven, chase and cockpit views. The cars are 463 KEN_T440_12 (truck),
  724 VW_TouaregR50_08, 725 WIE_GTMF5_10 (supercar; 705 to 725 are past
  2 GiB in the archive), 382 FIA_595_68 (fixed in #33) and 705
  VOL_242Turbo_83.
- **Low roofs** (`vm-20261025-011752`): 26 TOY_2000GT_69, 102
  BEN_7_Speed8_03, 333 DOD_ChallengerHemi_70, 47 ACU_NSX_05 and 222
  BUS_TOUR_12 all drive. In the chase views no head shows through a roof.
- **Garage:** with more than 1100 SPOL items the pictures were locks; fixed
  by #43 (`asi.md`, "Spool item lists"). With it:
  - every card shows its picture (`vm-20261025-000547`, `-001221`);
  - maker groups larger than 20 scroll (TOYOTA, 21 cards, to the last).
- **Crashes while the world loads:** two causes (`in-game-tests.md`,
  "Crashes while the world loads").
  - The Lua VM fault with the big generated files: fixed by #44, 0 in 15
    runs after the fix.
  - The engine fault in the deferred UID list: it follows **dsf_enhanced**.
    - Vanilla archive, no dsfcars.asi, no added cars:
      - with the user's dsf_enhanced.ini: 2 of 4 runs crashed;
      - with a neutral ini (traffic radius 350, no AF, no full-detail
        traffic, `vm-20261025-005639` to `-010158`): 4 of 4 crashed;
      - with dsf_enhanced.asi replaced by an empty stub DLL
        (`vm-20261025-011001` to `-011514`): 0 of 4 crashed.
    - So it isn't the roster or dsfcars.asi. The trigger is dsf_enhanced's
      legal-screen skip, which lets the loading screen empty the pending
      list while startup files still load (`in-game-tests.md`, "Crashes
      while the world loads"). Fixed in dsf_enhanced (`fix/world-load-crash`):
      0 of 14 runs crashed, 4 of them with the full roster.

### GUI fixes after the install (2026-10-25)

User reports on out/final-gui2: no HUD nameplate (and no badge for new
makers) on most ported cars, no maker list logo for BERTONE, BUGATTI,
BUICK..., garage pictures too glossy. Causes and fixes in gui-assets.md
("Why the 16-bit glyphs drew nothing", "Maker list logos", "Tone"): every
16-bit glyph (533 plates, 303 badges) drew nothing, 36 of the 92 maker
groups had no logo or the host's, and the renderer washed the paint out.
The archive doesn't change; only the GUI step and dsfcars.asi do. The GUI
step alone, on the installed build's manifest and archive (11 minutes with
4 jobs):

```sh
uv run dsff gui_assets build out/final-gui2/dngvehicles.json --out out/final-gui3/gui \
    --archive out/final-gui2/dngvehicles.sp --wide-glyphs --root-limit 3003 --jobs 4
```

Install it with the rebuilt plugin (`.\tools\asi\build.ps1`): the HUD needs
both, the maker list logos need the new `global.fchunk` too.

### Install (not run here)

Today `uv run dsff install --install-only --build <folder>` does this, with
`-WriteProfile` unless `--no-write-profile` (`docs/DEVELOPMENT.md`). As
written then, from the worktree that holds the build, with dsfcars.asi built from master
(`.\tools\asi\build.ps1`):

```powershell
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Build out\final-gui2 -IdPolicy asi -Asi -Force
```

- `-IdPolicy asi` and `-Asi` are required: the archive has 2172 root
  entries and is 2.64 GiB.
- `-Force` replaces the earlier install.
- `-WriteProfile` is optional, and it writes the save. Through
  `config.lua` `register.writeProfile = true`, `register.lua` then does this
  on every game start, for every garage-listed added car (all 573 with the
  plugin):
  - calls `ProfileSettings.SetVehicleUnlocked` and `SetVehicleOwned`, and
    sets the entry's `UnlockedInChapter = 1`;
  - IDs up to 303 (153 cars) go into `autosave.dat`'s 304-slot flag and
    owned lists, about 146 + 153 of 304 slots;
  - IDs 304 to 999 (420 cars) go into `dsfcars-garage.dat` next to it;
  - the game shows its "new vehicle" notice once per car;
  - the flags stay in the save after an uninstall: no car, no garage entry.
- Without `-WriteProfile` the cars show in the garage with their pictures
  but as not owned, as in the VM save, where 2 of 21 Toyota cards are owned.
  The user can still Shift into them in traffic.
