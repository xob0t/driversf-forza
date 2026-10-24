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
- Every car is linted.
- With `--gui` the build also runs `gui_assets.build`.

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
