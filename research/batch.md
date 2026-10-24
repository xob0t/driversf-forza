# Batch porting, model IDs and the installer

Updated: 2026-10-24

How many Forza cars one `dngvehicles.sp` can take, which model IDs they get,
what the save and the garage can hold, and how `src/dsfforza/build/port_batch.py` and
`tools/install.ps1` deal with that. Evidence tags as in `dngvehicles.md`.
The Ghidra outputs are in `out/ghidra/profile-*`; the scripts used are
`tools/ghidra/FindStrings.java` and `tools/ghidra/DecompileContaining.java`.

## Limits

| Limit | Value | Source |
| --- | --- | --- |
| Root entries in `dngvehicles.sp` | 1100. Stock uses 453: 150 cars x 3, plus SSIC, DMWK and BINF. That leaves room for 215 more cars (1100 - 453 = 647 entries, 3 per car). | confirmed-static, `spool-limits.md` |
| `dngvehicles.sp` file size | below 2 GiB: the file layer's seek clamps larger positions to 0. At about 3.45 MB per ported car that is about 480 added cars, so only the root entry limit applies without the plugin. | confirmed-runtime, `spool-limits.md` |
| Base model IDs | below 1000; N+1000 and N+2000 are the interior and exterior entries (FUN_005A7FC0 adds 1000 and 2000). 849 IDs are free: 1 to 61, the 92 gaps in 62 to 303, and 304 to 999. | confirmed-static |
| Garage list | model IDs 0 to 303 only (FUN_006c57d0, and an `int[0x130]` indexed by ID at garage object +0x3264) | confirmed-static |
| Garage maker groups | at most 40 groups (stock: 35 with selectable cars) and at most 20 cars per group (stock maximum: Chevrolet, 16). Groups are keyed by the first 15 characters of `ManufacturerName`, upper-cased. The insert code (FUN_006C7B40, FUN_006C7BB0) has no capacity check. | confirmed-static |
| Save, vehicle flag list | 304 `{id, flags}` slots at profile +0x184 (bit 0 unlocked, bit 1 owned, bit 2 new). Lookup is a linear search, and any ID works as a key. Stock cars use about 146 slots. A full list makes FUN_00507CF0 write through NULL (crash). | confirmed-static |
| Save, `SetVehicleUnlocked` bit array | 320 bits at profile +0x2088, bit = ID. IDs 320 to 639 land in the Driven array (+0x20B0, the "driven" bit of ID - 320), 640 to 959 in an unidentified array, 960 to 999 in the take-a-ride array. No crash: silent save corruption. | confirmed-static |
| Save, `SetVehicleOwned` list | 304-entry ID list at profile +0x2784 (NULL write when full) | confirmed-static |
| Save file | fixed 0x4CF8-byte block with ten 0x278A106F guard words and a version field. `autosave.dat` is written raw, without a checksum. | confirmed-static |
| Mission spool slots | about 10 `RequestMissionVehicle` cars per session (the 11th to 16th were "NOT spooled") | confirmed-runtime, `in-game-tests.md` |

**With `dsfforza.asi`** (`research/asi.md`), these rows change:

| Limit | With the plugin |
| --- | --- |
| Root entries in `dngvehicles.sp` | `archive` module: 16384 (`[archive] max_entries`). The model IDs bind first: 453 + 3 × 849 = 3000 entries. `port_batch.py` assigns 1 to 61 and 304 to 999 (at most 757 cars), or all 849 with `--id-policy asi`; pass `--root-limit` (3003 covers every ID), since the default stays 1100 for installs without the plugin. Tested in game with 2,724 entries (`spool-limits.md`, "Raising the root entry limit"). |
| `dngvehicles.sp` file size | `archive` module (`large_files`): below 4 GiB, enough for all 849 IDs (about 3.4 GB) |
| Garage list | model IDs 0 to 999 (`[garage] max_id`) |
| Garage maker groups | 120 groups (`[garage] max_groups`), still 20 cars per group; a car or group that doesn't fit is left out instead of overrunning memory |
| Save flags (list, bit arrays, owned list) | IDs 304 to 999 are kept outside the stock block, in `dsfforza-garage.dat` next to `autosave.dat`; IDs of 1000 and up are dropped by the bit setters; a full stock list no longer crashes |
| Save file | unchanged (stock-range data only), so an unmodded game still loads it |

The Lua bindings are `ProfileSettings.GetVehicleUnlocked` 0x678D40,
`SetVehicleUnlocked` 0x678D90, `GetVehicleOwned` 0x678E00 and
`SetVehicleOwned` 0x678E50. None of them range-checks the ID. Model ID 62
(Tanner's car) is special in one place: FUN_006CDE40 pre-marks it as seen.
Nothing else treats IDs below 62 specially.

### Does the 10-car mission limit matter in normal play?

No (confirmed-static from the game's Lua):

- **Traffic** comes from the chapter's traffic set and the interesting-vehicle
  regions. `register.lua` adds the ported IDs to those regions. The engine
  spools them into the 35 kind-0 slots like stock interesting vehicles, not
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
   - Stock base IDs run from 62 to 303, so 0 to 61 are free, and the garage
     loop covers them.
   - In the save they stay inside every array, and the flag list has room:
     146 stock + 61 = 207 of 304 slots.
   - 0 is left out as a precaution (-1 marks empty slots, so 0 is a valid
     key, but it is a likely "none" value in C code).
   - 62 is Tanner's car.
2. **IDs 304 and up for the rest.**
   - They load, drive and spawn in traffic (304 to 314 are confirmed in game).
   - They never appear in the stock garage (with dsfforza.asi they do: below).
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
   - A car joins its maker's group: the stock `"ID:nnnnnn"` maker string when
     DSF has the make (from `hud.lua`, `src/dsfforza/game/gui_assets.py`), else a plain
     name such as "BMW".
   - A new make opens a group while fewer than 39 exist (one is kept back).
   - When a make's group is full (20 cars), the car goes to the make's next
     group, labelled "<maker> 2", "<maker> 3"..., while groups are left.
     `gui_assets.continuation_groups` writes the labels to global.fchunk as
     new string IDs and lists them in `hud.lua` `_groups`; `register.lua`
     takes them in order.
     - Example, the full roster: AUDI 2, CHEVROLET 2 and 3, DODGE 2, FORD 2
       and 3, LAMBORGHINI 2, NISSAN 2, VOLKSWAGEN 2, BMW 2, FERRARI 2 and
       TOYOTA 2.
     - Simulated with register.lua and the stub API: 104 groups, no FORZA
       group, all 562 cars selectable. Before this change there were 99
       groups and 121 cars spread over seven mixed FORZA groups.
   - Otherwise the car joins a shared "FORZA" group (then "FORZA 2" ...).
   - `port_batch.py` (`garage_group`, with a margin of one per cap) and
     `register.lua` (`garageGroup`, exact caps, run against the real
     `vehicleStats` at game start, so mods that make more stock cars
     selectable are counted) apply the same rule.
   - A car that gets no group gets an ID from 304 at build time. At runtime
     it stays unselectable.
4. **List order decides** which cars get the 61 garage IDs.

`port_batch.py --id-policy high` puts every car at 304 and up, the range
confirmed in game.

`--id-policy asi` (for installs with `dsfforza.asi`) gives the cars every
free ID in list order: 1 to 61, then the 92 IDs in 62..303 that the stock
archive leaves unused (no traffic set, `vehicleStats` key or model-ID field
of the game's Lua names them; Felony Rework's dummies 622 and 623 are
skipped too), then 304 and up. The plugin lists IDs up to 999 in the garage and
saves their flags in its side file, and IDs up to 303 are listed and saved by the stock code anyway;
`register.lua` detects the plugin and makes every car selectable while its
maker group has room (120 groups of 20). The build notes cars that won't fit
a group (same rule, `ASI_GROUPS_CAP`). `install.ps1 -IdPolicy asi` builds
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
| `writeProfile` | false | on each start, marks every garage-listed added car unlocked and owned, so the game shows a "new vehicle" notice once per car. It uses one slot of the 304-slot list per car, and the flags stay in the save after an uninstall (harmless: no car, no garage entry). |
| `guardProfile` | true | drops Lua `SetVehicleUnlocked`/`Owned` calls for added IDs above 303 (above 999 with `dsfforza.asi`'s save hooks) |
| `garage` | true | `Selectable` for IDs below 304 (below 1000 with `dsfforza.asi`), subject to the group caps |
| `regionCars` | 24 | With more added cars than this, car k (in ID order) goes to every m-th region, offset by k (m = ceil(cars / 24)), so each region gets about 24 of them. Stock region lists hold 9 to 110 IDs. |
| `interesting` | true | the interesting-vehicle wrapper (traffic) |
| `missionPot` | false | experimental: keep the cars resident in the "All chapters" mission pot |

At start `register.lua` asks whether `dsfforza.asi`'s garage module is
active (`ProfileSettings.GetVehicleUnlocked(8000000 + k)`, research/asi.md)
and takes its limits: listed IDs, maker groups, the highest ID whose save
flags are safe. It logs them as `dsfforza.asi: save hooks ..., garage IDs
0..999, 120 maker groups of 20`. `tests/test_register_asi.py` runs
it against stubs with and without the plugin (600 fake cars: none selectable
at 304+ without it, all 600 with it, at most 20 per group).

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
  Idle priority pinned to the last two logical CPUs, with at most two
  workers, so a game on the same PC keeps its CPU. Delete the file to build
  at full speed.
- A new worker starts only while 1.5 GB of RAM is free. One worker peaks at
  about 420 MB.
- Each worker writes the car's three VEHC entries to `work/<media>.chnk`.
- The merge reads the stock archive and every car file as one address space,
  so nothing is copied twice. It registers the clones (SSLP, VSCT), checks
  the root entry limit (`--root-limit`, default 1100; more needs dsfforza.asi's
  `archive` module) and UID clashes, and writes the archive.
- Every car is linted, in `--jobs` (at most 4) `upvh lint-many` processes
  that each read the archive once for their share of the cars.
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
- `cockpit.visible_triangles` and `GlassSight`: the cube-face ID and depth
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
  twice per render, and at 573 cars that archive is about 2.4 GB. It is
  read once now. The nameplate rasteriser is about 3 times faster, and the
  renders (about 5 s each, every car without an FH1 thumbnail) run in
  `--jobs` processes. 4 cars: 36 s -> 15 s.
- **Worker start-up** is about 3 s per car (imports, the stock archive's
  root, host survey and sound index from `out/cache`), small next to the
  conversion.
- **`--jobs` above the physical cores**: not measured here (the machine was
  never idle). The workers are pure CPU, so SMT gives at most a few tens of
  percent more; at about 420 MB per worker, 16 workers fit in 32 GB.

### Selective rebuild

```sh
uv run dsff port_batch build --out out/all2 --list roster.txt --reuse out/all --jobs 8 --gui
uv run dsff port_batch build --out out/all2 --list roster.txt --reuse out/all \
    --trust-code --rebuild-where "host == 198" --verify-reuse 4
```

`--reuse DIR` copies a car's output from an earlier build (`DIR/work`:
`<media>.chnk`, the report, the log, `sound/<media>/`) when nothing that
went into it changed, and converts only the other cars. The merge, the lint
and the GUI step still run over every car. Files are hard-linked where the
volume allows it, so an earlier build of 573 cars costs no second 2 GB.

**What decides.** Each worker writes `work/<media>.fp.json`
(`src/dsfforza/build/fingerprint.py`) next to the chunk:

- the job: car, game, disc source, spool ID, the requested `--port`
  options, the stock archive, cache and disc paths (links and junctions
  resolved, so another worktree with the same `out/cache` matches);
- Python and numpy versions;
- every file the worker opened for reading, from an audit hook on `open`
  and `sqlite3.connect`, with its SHA-256. Files over 64 MiB (the stock
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
`dsf-fleet.json`, `stock-banks.json`), which are recorded as files. And
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

`DSF_GAME_DIR` points `host_select.py` and `gui_assets.py` at another game
folder.

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

- The archive is 590,426,112 bytes (563 MiB). Stock is 483,110,912, so the
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
| DOD_ChallengerSRT8392_12 (host 173) | `damage.py`: `Uv1Field.read` on the host's interior gets a vertex element `None` (`vehicle_mesh.py` line 60, `'NoneType' object has no attribute 'type'`) | without `--damage` | ok, no stock-like damage |
| FIA_500AbarthSS_10 (host 184) | the same `damage.py` error | without `--damage` | ok, no stock-like damage |
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
  - **Over 31 cars**, a batch needs more codes (by reusing stock glyphs, or
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
| `dngvehicles.sp` not stock | refuses (SHA-256 shown) |
| build path: `-Car FOR_FocusRS500_10 -BuildDir out\batch\bd-test` via `powershell.exe -File` | builds (Python found, `DSF_GAME_DIR` = the fake tree, gui assets from the fake tree), installs, uninstalls; tree identical |

Not tested:

- `-Download` (GitHub fetch);
- a machine without the VC++ x86 runtime (only the check: the registry key,
  else `SysWOW64\vcruntime140.dll` and `msvcp140.dll`);
- an admin-protected game folder.

### Resumable uninstall (after a failure on the real install)

**The failure.** A `-Force` reinstall on the real install restored all three
files. Then deleting `dsfforza-backup\` failed on `Install\gui.sp`, because
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
  - a backup equal to the game file, or a game file that is stock, needs
    nothing;
  - a stock backup over a non-stock game file is put back with `-Force`
    (refused without it).
  - The old manifest is renamed `install.json.<stamp>.old`.
  - `Install-File` reuses a backup that already equals the game file, so a
    locked leftover is no obstacle.
- **Stock check.** Stock SHA-256s for `Install\gui.sp`, `media\Fonts.fchunk`
  and `Locale\English\global.fchunk` (the `out/stock` copies) join the
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
| the reported case: all files restored to stock, `dngvehicles.sp` backup deleted, manifest listing everything | `-Force` reinstall works ("already restored") |
| missing backup of a still-installed file | uninstall stops; after the backup is back, a second `-Uninstall` resumes; identical tree |
| leftover stock backup, non-stock `gui.sp`, no manifest | refused; with `-Force` restored from the leftover, then installed |
| modded `Fonts.fchunk` | refused; with `-Force` installed, uninstall gives back the modded file byte for byte |

At the end the tree matched the snapshot taken before all tests, every file
by SHA-256.

## Batch 2 (out/batch/second, out/batch/second-high)

This rebuilds the same 31-car list after the fixes in `host_select.py`,
`damage.py`, `cockpit.py` and `gui_assets.py`. `port_batch.py` now names
garage groups as `gui_assets` will: Forza's own maker name via
`car_texts`/`dsf_make`, mapped to the stock `"ID:nnnnnn"` maker string.
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
  - DMC 12 uses the stock DeLorean nameplate.
  - The Jeep and Range Rover use the stock Jeep and Range Rover badges.
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
| stock makers | Nissan 5; Dodge 8, 10, 19; Pontiac 9; VW 15; Fiat 16; Jeep 20; Range Rover 21; Chevrolet 23; DMC 27; Pagani 31 |

That makes 40 groups, the largest with 17 cars.

`out/batch/second-high` is the same cars at IDs 304 to 334
(`remerge --id-policy high --gui`, 354 s). In that range, register.lua's
guard covers both 304 to 319 and 320 to 334.

## In-game test plan (batch 2)

Run from the repo root. The plan was written for the sandbox; on this PC
the sandbox guest bugchecks (`in-game-tests.md`), so add `-Vm` (headless VM,
started by `run-series.ps1`) to each command.

What the harness provides:

- `run-test.ps1` copies `tools\game\lua\*` into the guest; `-LuaFiles`
  puts the build's `ported.lua` and `hud.lua` over the copies for one run
  (the commands below predate it and copy them into `tools\game\lua\dsfforza\`,
  backing up the shared files first; `-LuaFiles 'dsfforza\ported.lua=<build>\dngvehicles.lua;dsfforza\hud.lua=<build>\gui\hud.lua'`
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
Copy-Item tools\game\lua\dsfforza\ported.lua out\scratch\ported.lua.bak -ErrorAction SilentlyContinue
Copy-Item tools\game\lua\dsfforza\hud.lua out\scratch\hud.lua.bak -ErrorAction SilentlyContinue

# Runs 1-3: IDs below 62 (garage range), 10 per session
Copy-Item out\batch\second\dngvehicles.lua tools\game\lua\dsfforza\ported.lua
Copy-Item out\batch\second\gui\hud.lua tools\game\lua\dsfforza\hud.lua
$ov = 'Install\gui.sp=out\batch\second\gui\gui.sp;media\Fonts.fchunk=out\batch\second\gui\Fonts.fchunk'
.\tools\wsb\run-series.ps1 -Archives out\batch\second\dngvehicles.sp -Override $ov -ProbeIds '1,2,3,4,5,6,7,8,9,10' -WaitSeconds 1200 -ShotSeconds 4 -DriveSeconds 8
.\tools\wsb\run-series.ps1 -Archives out\batch\second\dngvehicles.sp -Override $ov -ProbeIds '11,12,13,14,15,16,17,18,19,20' -WaitSeconds 1200 -ShotSeconds 4 -DriveSeconds 8
.\tools\wsb\run-series.ps1 -Archives out\batch\second\dngvehicles.sp -Override $ov -ProbeIds '21,22,23,24,25,26,27,28,29,30' -WaitSeconds 1200 -ShotSeconds 4 -DriveSeconds 8

# Run 4: the 304+ range (same cars), across the 304-319 / 320+ save boundary
Copy-Item out\batch\second-high\dngvehicles.lua tools\game\lua\dsfforza\ported.lua
Copy-Item out\batch\second-high\gui\hud.lua tools\game\lua\dsfforza\hud.lua
$ovh = 'Install\gui.sp=out\batch\second-high\gui\gui.sp;media\Fonts.fchunk=out\batch\second-high\gui\Fonts.fchunk'
.\tools\wsb\run-series.ps1 -Archives out\batch\second-high\dngvehicles.sp -Override $ovh -ProbeIds '304,310,318,319,320,326,333,334' -WaitSeconds 1000 -ShotSeconds 4 -DriveSeconds 8

# Run 5: garage. Car 31 is driven, then garagetest opens garage 1 (Fisherman's Wharf)
# after about 20 s in the car, first marking IDs 1..31 unlocked and owned in the
# sandbox's save copy (the host save is mapped read-only; the copy keeps the flags
# until the sandbox restarts; the VM's copy is refreshed from the host every series).
Copy-Item out\batch\second\dngvehicles.lua tools\game\lua\dsfforza\ported.lua
Copy-Item out\batch\second\gui\hud.lua tools\game\lua\dsfforza\hud.lua
Set-Content tools\game\lua\dsfforza\garagetest_on.lua 'return { garage = 1, delay = 40, unlock = true }' -Encoding ASCII
.\tools\wsb\run-series.ps1 -Archives out\batch\second\dngvehicles.sp -Override $ov -ProbeIds '31' -WaitSeconds 420 -ShotSeconds 5
Remove-Item tools\game\lua\dsfforza\garagetest_on.lua

# Put the shared files back
Copy-Item out\scratch\ported.lua.bak tools\game\lua\dsfforza\ported.lua -ErrorAction SilentlyContinue
Copy-Item out\scratch\hud.lua.bak tools\game\lua\dsfforza\hud.lua -ErrorAction SilentlyContinue
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
453 + 3 x 573 = 2172 root entries, past the stock 1100.

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
| Model IDs (`--id-policy asi`) | 573 IDs: 1..61, the 92 stock gaps in 62..303, then 304..725 | 849 free IDs below 1000 | fits |
| Root entries in `dngvehicles.sp` | 2,139 (453 + 3 x 562) | 16,384 with `[archive]` (pass `--root-limit 3003`) | fits |
| Archive size | 2,750,959,616 bytes (2.56 GiB) for 562 cars | 4 GiB with `[archive] large_files`; the stock reader stops at 2 GiB | fits, about 1.4 GiB spare |
| Per-car size, 3 VEHC entries | median 3.89 MiB (p5 3.49, p95 4.17), max 4.69 (KOE_CCXR_09), min 1.81 (AUD_A4Avant_04); 2,156 MiB for all 562 | no per-car cap besides MDXN | No car is above 5 MiB. The 1.8 to 2 MiB cars are FH1's low-detail cars (Durango, PT Cruiser, Prius, RAV4, Leaf, tour bus...), whose Forza cockpit is only `interior`, `seatR` and `steering_wheel`. |
| MDXN per entry | largest 0x200800 (2,099,200 bytes) | `--mdxn-limit 0x2C0000` | 73% of the cap. Recheck after #23 (badge cutouts) and #27 (chrome). |
| Resource hash arena (45,000 nodes) | bounded by what is resident, not by the archive | free list (below) | not a limit for the roster |
| Garage maker groups | 112 maker groups, plus 139 cars in 8 shared groups `FORZA`..`FORZA 8` (`plan`) | 120 groups of 20 (`[garage] max_groups`) | Fits. The extra cars of makers with more than 20 (Ford, Chevrolet, Nissan, Porsche...) go into the mixed FORZA groups. |
| Garage IDs | 573 cars, all below 1000 | `[garage] max_id=1000` | fits |
| HUD glyphs | one 16-bit badge or plate per car without a stock one | `--wide-glyphs`: codes 0x100..0xD7FF, about 800 plates in 4 pages | fits; not yet checked in game at this count |
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
| `forza2dsf.load_car`: "no wheel section" (8) | FER_360_99, FER_360Challenge_03, FER_Dino246_69, LAM_Gallardo_05, PAN_Esperante_05, CHE_CORVETTEGSC_10, AUS_MiniCooperS_65, AUS_ShowCaseMiniCooperS_65 | The main carbin has no `wheel` section. The first five keep it only in the wheel archive's carbin (`media/wheels/<car>`, one section `wheel`). The Minis name it `wheela`/`wheelb`, like upgrade variants, and `stock_sections` keeps `wheela` in the body, so the wheel would also be drawn as body. The Corvette Grand Sport's and the Mini's wheel-archive carbins don't parse (next row). |
| `carbin.parse`: "no parsable sections found" (1) | FIA_595_68 | The first section (`roof1`) is empty: 0 vertices, bounds FLT_MAX / -FLT_MAX. `_looks_like_section` rejects any float of magnitude 100 or more, so it accepts no start position. From 0xDB0 all 34 sections parse. The wheel-archive carbins of the Grand Sport, the Mini and the 595 fail later, in the section tail. Their LOD0 pool is skinned, so the tail's per-vertex record is 16 bytes instead of 4 and the tail starts 4 bytes earlier. The geometry before the tail parses. |
| `forza_materials`: "nodamage.xds not in ..." (2) | CHE_CamaroZ28_79, PON_TransAm_73 (FM4 1950-2000 pack) | No `nodamage.xds`, only colour variants: `nodamage_blue/gold/orange/yellow.xds` and their `_lod0`, plus `damage.xds`. |

### Lint failures (14 of 562)

| Lint check | Cars |
| --- | --- |
| kind 1: "interior glass has crack lines for states 1-2 and rim shards for state 3" (crack and shard counts all 0; surface triangles only) | AUD_14_R10_10, AUD_2_R10_08, AUD_2_R15TDI_09, AUD_2_R8_06, BMW_15_LMR_99, CHE_55/89/99_FLM09_10, MAZ_MX5SL_10, MER_SLRStirlingMoss_10, PAN_11_LMP_03: open race cars and roadsters, whose only interior glass is a small windscreen or wind deflector |
| kind 1: "interior glass triangles each inside one stock interior region" (448, 611 and 460 straddle) | CHE_Express2500Cargo_05, FOR_F350SuperDuty_08, FOR_TaurusSHO_10: the three stub cockpits that keep the host's interior since #18 (the Express on the Savana, the F-350 and the Taurus on their same-model hosts) |

### Other report flags (built cars, not failures)

| Flag | Cars | Note |
| --- | ---: | --- |
| front plate at the host's position ("no flat face of plate size") | 279 | expected for cars without a front plate recess |
| rear plate at the host's position | 82 | mostly race cars |
| cockpit without needle gauges | 128 | race cars with digital dashes, and FH1's low-detail cockpits |
| some lamps left unlit | 22 | e.g. LEX_ISF_09, NIS_370Z_10, BMW_1M_11, FER_F50_95 |
| no head lamp found | 10 | Race cars (Corvette C5-R, CLK GTR, McLaren F1 GTR, 905 EVO...), but also CHE_CorvetteGS_10, DOD_RamSRT10_06 and FOR_MustangSVTCobraR_93. |
| no tail lamp found | 2 | CHE_2_MonteCarloSS_11, FOR_1_Fusion_11 (race liveries of stock cars) |
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
| #36 | interior glass lint: stub cockpits check the host's stock interior; lamp-only cockpits can't have cracks | 14 lint | 562 of 562 lint ok |
| #37 | ET file on the other game's disc, or only for the base model | 6 | Forza engine sound 6 of 6 |
| #38 | no headlamp technique (reflector under a clear lens) | 10 | 8 get headlamps; the 2 NASCARs have decal headlights |
| #34 | `--root-limit` not passed to the GUI step | all | gui.sp 1485 entries, one SPOI per picture |
| #39 | makers with more than 20 cars spill into mixed FORZA groups | 121 | "FORD 2", "CHEVROLET 3"...: 104 groups, no FORZA group |

All PRs keep the 4-car regression archive byte-identical
(`8459AAF9...EC5A`). Not fixed: CHE_Volt_11 and NIS_Leaf_11 keep the host's
engine sound (Forza's bank for electric cars is `NA.fsb`, which isn't on
either disc).
