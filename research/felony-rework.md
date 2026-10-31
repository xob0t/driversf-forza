# Felony Rework 2.3 Hotfix 1 alongside the Forza cars

Updated: 2026-10-31

Whether our install (`dsff install` / `tools/install.ps1`, `dsfforza.asi`,
`register.lua` and the player bootstrap) and Felony Rework 2.3 Hotfix 1 can
run in one game folder, and what breaks. Read from the mod's unpacked files
(83 files, its `readme.txt` and `changelog.txt`), its decompiled script
packages, the DriverNGToolkit and Ultimate ASI Loader sources, and our code.
Tags as elsewhere: `confirmed-static` (read in data, code or binaries),
`hypothesis` (not yet seen in game). **Nothing here has been run in game.**

**Verdict: compatible with steps** (static). Install Felony Rework as its
readme says, then run our installer with `--force`. If our cars were
installed first, run `install.cmd --install-only` again after Felony Rework.
Our bootstrap then runs Felony's first (below). No ID collides. With
dsfforza.asi, two of Felony's placeholder entries would have shown up in our
extended garage; `register.lua` now hides them.

## What the mod ships

| Files | What they do |
| --- | --- |
| `version.dll` | Ultimate ASI Loader, a 2021 build (`InitializeASI`, `asi-loader` strings, PDB path `ultimate-asi-loader\...\dinput8.pdb`); byte-identical to the one in snoopii's Mod Menu |
| `plugins\DriverNGHook.asi` + `plugins\DriverNGHook\scripts\*.lua` | its own DriverNGHook build (1,184,256 bytes), older than our 0.7.2; reads its scripts from `plugins/DriverNGHook/scripts/` and runs `autoexec.lua` (`allowCustomGameScripts(true)`) |
| `Common\UserLuaScripts\bootstrap.lua` | `UserBootstrapper`, whose `Launch()` opens `mo.lua` (not shipped) and `felonyrework.lua` |
| `Common\UserLuaScripts\felonyrework.lua`, `felonyrework\*.lua` | the mod's runtime: update functions, patrols, roadblocks, parked cops, music |
| `Common\LuaScripts\...` (20 files) | whole-file overrides: DriverNGHook's `_open` loads `Common\LuaScripts\<path>` from disk when it exists. Among them `vehiclestats.lua`, `trafficspoolingdata.lua`, the felony and patrol managers, `unlockables.lua`, `missionfiles.lua`, `missioninfo.lua` |
| `media\Scripts*.fchunk` | Chapter1..7, Challenges, ActionActivities replaced; StimulusPackage and FelonyReworkAudio new (the vanilla game has neither) |
| `Common\fmv\...` | Bink videos |

It replaces none of the files our installer replaces or checks
(`Install\dngvehicles.sp`, `Install\gui.sp`, `media\Fonts.fchunk`,
`Locale\<language>\global.fchunk`), and our build tools read the packed
`ScriptsLuaScripts.fchunk`, never `Common\LuaScripts` (confirmed-static).
The shared files are `bootstrap.lua`, `plugins\DriverNGHook.asi` and the ASI
loader.

## 1. Loaders and hooks

**Two ASI loaders** (confirmed-static). `Driver.exe` imports `WINMM.dll`
and `DINPUT8.dll`; `DFEngine.dll` imports `VERSION.dll`. So Felony's
`version.dll` loads next to whatever loader the game already has: this PC's
`winmm.dll` (the same 2021 UAL line), or the `dinput8.dll` (UAL v9.7.4) that
our installer adds when the game has no loader. Each instance loads `*.asi`
from the root, `scripts\` and `plugins\`, but the 2021 source
(`source/dllmain.cpp` at commit a6fcc90, `FindFiles`) loads a file only
`if (GetModuleHandle(path) == NULL)`. A plugin already loaded by the other
instance is skipped, `InitializeASI` included, so no plugin loads twice. That
holds for the 2021 build on both sides; that v9.7.4 keeps the same check is a
hypothesis.

**Two DriverNGHook builds** (confirmed-static). Only one file can be
`plugins\DriverNGHook.asi`, so only one build runs. Each reads its own
folder: 0.7.2 has `DNGHookScriptPath = plugins/DriverNGHook/`, Felony's
build `plugins/DriverNGHook/scripts/` (strings in each binary). Felony's
readme says to extract over the game, so its build replaces ours, and our
installer leaves an existing DriverNGHook alone ("already present"). In
either order Felony's build ends up running. Our 0.7.2 scripts in
`plugins\DriverNGHook\` are then unused files. So the `game_autoexec.lua`
that wins is Felony's (`plugins\DriverNGHook\scripts\game_autoexec.lua`).

Felony's scripts against 0.7.2's: the same `_open`, `_user_open`,
`user_fileExists`, bootstrap reload and `launchScripts` wrapper. 0.7.2 adds
`struct.lua`, `driverNGProfileSettings.lua` (custom mission and challenge
flags in a side save), ImGui flag globals, and an `xpcall` around user update
functions. None of our Lua uses those (grep of `tools/game`, `tools/asi`).
The player `register.lua` adds no update function (only with `probe`), so the
missing `xpcall` doesn't matter to it.

**dsfforza.asi against Felony's DriverNGHook** (confirmed-static). Both
builds have the same patch classes, and Felony's binary holds the same
call-site constants as `DriverNGHook/Patches/All/LuaPatches.cpp` and
`OnlinePatches.cpp`: 0x005E7E04 (OpenScriptLoader), 0x0078E975
(DeleteLuaState), 0x0079B811 (StepLua), 0x00903EBA, 0x004C4ACE. It lacks
0.7.2's GetProfileName patch at 0x008534C2, which only feeds
`driverNGProfileSettings.lua`. No address in `tools/asi/src/*.cpp` lies within
16 bytes of any of them. The nearest is `mod_lua.cpp`'s `push` at 0x005E7DB7,
0x4D bytes before OpenScriptLoader's call. dsfforza.asi also checks every
site's original bytes before patching, so a clash would show in
`plugins\dsfforza.log` as a skipped module, not as a crash.

## 2. bootstrap.lua

Both mods want `Common\UserLuaScripts\bootstrap.lua`. Until this change,
`install.ps1` stopped on a foreign bootstrap, and with `-Force` replaced it,
so `felonyrework.lua` no longer loaded. The other way round, extracting
Felony over our install replaced ours, and our cars lost their HUD names,
garage entries and traffic (the archive and dsfforza.asi stay, so no crash;
hypothesis).

Now (this PR):

- `install.ps1 -Force` keeps the foreign file as
  `Common\UserLuaScripts\dsfforza\chained-bootstrap.lua` (a created file;
  `-Uninstall` deletes it and restores the original `bootstrap.lua` from the
  backup). It doesn't chain a bootstrap that mentions `dsfforza`, since an old
  copy of ours would load `register.lua` twice.
- The player bootstrap (and the test one) calls the chained bootstrap's
  `Init`, `Launch` and `Shutdown` before its own work, each in `pcall`. An
  error in Felony's `Launch` is logged and no longer skips the game's own
  `launchScripts()`, which DriverNGHook's wrapper calls right after it.
- `register.lua` installs the garbage-collector guard around `user_open`
  itself (the same `rawget` guard as the bootstrap). So the manual route, one
  `user_open("dsfforza\\register.lua")` line in Felony's `Launch()`, also
  loads `ported.lua` and `hud.lua` the safe way
  (`in-game-tests.md`, "Lua crashes loading big user files").

Order inside `Launch`: Felony's first, then `register.lua`. Felony's
`Launch` only opens its own files and adds update functions. It reads no
`vehicleStats` and sets no traffic regions, so the order doesn't change
what either mod sees.

Checked: `tests/test_bootstrap.py` (lupa, the Felony bootstrap's shape) and
six `install.ps1` runs against a fake game folder (a 16-byte archive, Felony's
real `bootstrap.lua`): no `-Force` stops; `-Force` chains; a reinstall
re-chains; `-Uninstall` restores Felony's file byte for byte and removes the
chained copy; a bootstrap that mentions dsfforza isn't chained; no bootstrap,
no chain. Also our install first, then Felony's bootstrap copied over it,
then a reinstall: the reinstall keeps Felony's file ("changed since the
install"), chains it, and `-Uninstall` leaves Felony's in place.

## 3. IDs

Every model ID Felony uses was collected from its user scripts, its
`Common\LuaScripts` overrides and the 602 scripts of its 11 packages
(decompiled with no failures, `game-lua.md` "Reproduce"). The collector took
`model_id`, `modelID`, `AddModelType`, `RequestMissionVehicle`,
`SetAsPlayerVehicle`, pot lists and `vehicleStats` keys, then diffed them
against vanilla (confirmed-static):

- All are vanilla archive IDs (`research/vehicles.tsv`), except:
  - 622 and 623: `vehicleStats` placeholders, already in `RESERVED_IDS`;
  - 78, 169, 219, 220: `routes.*.arrows[n].modelId`, read by
    `RouteArrowsManager.AddArrows`. Vanilla routes use the same values.
    These are arrow props, not vehicles (hypothesis from the API names), so
    they don't compete with our cars.
- **277** (prison van) is a vanilla archive model (VEHC 381,
  `Prison_Van_Fast`) that vanilla gives no `vehicleStats` entry; Felony gives
  it one, copied from 118, with `UnlockByActivity = "ComaNightmare"`. It is
  in `taken`, so port_batch never assigns it.
- User scripts: 62, 118, 188, 265, 267, 269, 271, 276, 277, 280, 287, 302.
  Packages and overrides add only further vanilla IDs (mission pots
  StimulusPackage, ClassicChase1/2, RockSteady, E3Chase, ShiftPerspective,
  JackReacher, TruckRampage, the Uplay and team-colour activity pots).

So none of our IDs (the free ones in 1..61, the 92 gaps in 62..303, 304 and
up, skipping 622 and 623) collides with Felony. **`RESERVED_IDS` stays as
it is, and no car's ID moves.**

`vehicleStats` against Felony's `vehiclestats.lua` (entry diff, vanilla 146
entries, Felony 149): 62, 130, 160, 251, 276 and 285 get other unlock
activities; 172 and 266-268 become selectable (`UnlockedInChapter = 10`;
266-268 cost 5000); 302 and 303 selectable (25000 and 20000); 277 new; 622
and 623 new. Load order (confirmed-static from `loadfiles.lua` and
`game_autoexec.lua`): the override replaces the whole table in the initial
load; `register.lua` runs later, in `Bootstrapper.Launch()`, and writes only
our IDs. Felony's runtime Lua never writes `vehicleStats`. Neither overwrites
the other.

**Felony's placeholders in our garage** (confirmed-static; fixed in this
PR). 622 and 623 have `Selectable = true` and `ManufacturerName = ""`. The
vanilla garage walks IDs 0..303 and never sees them. dsfforza.asi's garage
walks 0..999, so it would have filed them as a maker group with an empty
name holding two "NOTHING" cards (picture `default_ability`, price 1), and
`register.lua` would have counted that group against the 120. When Felony's
activities unlock them, the plugin's save hooks keep their flags. Picking one
would spool model 622, which the archive doesn't have (hypothesis: a hang or
crash in `garageSpawnVehicle`). `register.lua` now clears `Selectable` on any
entry above 303 that isn't one of our cars, when the plugin's garage is
active, and logs it. It leaves the unlock fields alone, so Felony's reward
panels are unchanged. Test:
`tests/test_register_asi.py::test_other_mods_placeholders_past_303_stay_out_of_the_plugins_garage`.

## 4. Traffic and garage room

**Traffic regions** (confirmed-static). Felony's seven chapter
`traffic\trafficspoolingdata.lua` (`spooling.interestingVehicles`) are
identical to vanilla. No Felony file calls
`SetInterestingVehicleRegions`. Its `InterestingVehicleManager.Enable`
calls in Final Destination and Breaking News 2 are vanilla's. Our wrapper
sees the same regions as without Felony.

**Mission pots** (confirmed-static). Felony's `trafficSpoolingData.lua`
adds 267 to "All chapters" (62, 269, 271, 280, 123, 267: six of the 18), moves
some chapter-pot cars into new per-activity pots, and adds pots for its new
missions (largest: ClassicChase2, 11 models, so 17 with "All chapters"). Our
cars don't go in pots by default (`missionPot = false`). A player who turns
`missionPot` on with Felony installed has one slot fewer.

**Spool slots** (hypothesis). Felony's runtime requests more mission models
during felonies (265, 118, 188, 276, 131, its backup and nightmare code) out
of the same 35 vehicle slots (`spool-limits.md`). Interesting vehicles get
what is left, so our cars may show up less often in traffic during a felony.
Nothing in the code points to a crash.

**Garage maker groups** (confirmed-static). Selectable entries, vanilla
against Felony: 35 groups and 140 cars become 37 and 149. Dodge (`ID:242755`)
goes from 10 to 13 cars, the unlicensed group gets 277, and two groups are
new: ASYM (`ID:242747`, 266-268) and the empty name (622 and 623, only
walked with dsfforza.asi and now hidden). `register.lua` counts groups from
the real table at game start, so:

- without the plugin (40 groups of 20, IDs up to 303): 36 groups. Build time
  keeps a margin of one group and one car per group (`GARAGE_GROUPS_CAP`,
  `GARAGE_GROUP_CAP`). A car the runtime count leaves without room goes to a
  FORZA group or stays unselectable, logged.
- with the plugin (120 groups): the full roster's plan is 112 maker groups
  plus 8 FORZA groups (`batch.md`). Felony's ASYM group takes one of them, so
  one more make shares a FORZA group instead of its own. No car is lost:
  FORZA groups have no car cap with the plugin.

## 5. Other things that could break

- **Cop and ambulance checks** (confirmed-static). Felony branches on
  `GameVehicleResource.getEmergencyType` and on literal `model_id`
  comparisons. Its Lua indexes no table by `model_id`, so an unknown ID can't
  raise a nil error. No Forza host in `host-picks.tsv` is an emergency class
  (VSCT 0x00-0x03), so our cars stay civilians to Felony.
- **Lua heap** (hypothesis). Felony keeps about 0.3 MB of user scripts
  resident, plus its mission packages while they run. With the plugin's
  `[lua]` arena (32 MB) that doesn't matter. A large roster without the plugin
  was already near the vanilla 10 MB (issue #56); Felony makes that worse.
  The installer requires the plugin past 1100 archive entries anyway.
- **Felony's bootstrap** opens `mo.lua`, which it doesn't ship. DriverNGHook
  prints an error line in `Driver.log`, harmless.
- **Report and status**: `install.cmd --report` now says
  `bootstrap.lua dsfforza's, chaining another mod's` when a chained bootstrap
  is present. It already lists every loader DLL and the UserLuaScripts files.

## In-game check (to do)

The VM harness can't lay a mod over the guest's game folder yet. The
`plugins` folder gets only the 0.7.2 hook (`-Hook`) and `-Asi`.
`Common\LuaScripts` and `Common\fmv` are junctions to the guest's copies.
`-Override` replaces only existing files, plus root files, and `-LuaFiles`
can't create `felonyrework\`. PR #319's `-ProfileFiles` writes into the
guest's Documents profile, not the game folder, so it doesn't help either.
The run needs an overlay option (#400). With one, the
run would be:

```powershell
# one VM series, Felony's files laid over the run folder, its bootstrap chained
.\tools\wsb\run-series.ps1 -Vm -Asi out\asi\dsfforza.asi `
  -Archives <build>\dngvehicles.sp -Labels felony -ProbeIds '<3 ids, one below 62, one in 62..303, one 304+>' `
  -Overlay "$env:DSF_GAME_DIR\Felony_Rework_2.3_Hotfix_1" `
  -LuaFiles "dsfforza\chained-bootstrap.lua=$env:DSF_GAME_DIR\Felony_Rework_2.3_Hotfix_1\Common\UserLuaScripts\bootstrap.lua" `
  -ShotSeconds 3 -WaitSeconds 600
```

The overlay must leave the test bootstrap in place: Felony's `bootstrap.lua`
goes in as the chained copy. What to read from the run:

- `Driver.log`: one "DriverNG Hook initialization"; "Custom game scripts are
  allowed"; Felony's "Bootstrapper init" and "[NOTE] loading user file
  'felonyrework.lua'" before the `[dsfforza.register]` lines; "vehicleStats[622]
  ... is not a model" and 623; no Lua errors after the chapter loads;
- `plugins\dsfforza.log`: every module patched, none skipped for changed
  bytes;
- the probe cars spawn, with their names and badges;
- `garagetest_on.lua`: no group with an empty name; ASYM listed;
- by hand later: a felony chase with a Forza car in traffic.

## Reproduce

```powershell
uv run dsff lua_scripts extract-all "$env:DSF_GAME_DIR\Felony_Rework_2.3_Hotfix_1\media" out\lua-felony
# unluac as in game-lua.md "Reproduce", Idle priority: 602 decompiled, 0 failed
$p = Start-Process $java -ArgumentList '-cp','_ref\downloads\unluac\unluac.jar','tools\unluac\BatchDecompile.java','out\lua-felony','out\lua-felony-src' -NoNewWindow -PassThru
$p.PriorityClass = 'Idle'; $p.WaitForExit()
```
