# Installing Forza cars in Driver: San Francisco: the details

How to install is in the [README](README.md): unzip the release, put your Forza
disc images in `assets`, run `install.cmd`. This page says what that does to
your game and your save, and what the added cars can't do.

## What install.cmd does

1. Checks the game (Driver: San Francisco 1.04, original vehicle archive, not
   running), the disc images, the Visual C++ x86 runtime and the disk space, and
   stops with what to fix before anything is built or changed.
2. Builds every car from your discs into `out\build` (at Below Normal priority),
   each car as a package of its own (`out\build\packages\`). A later run reuses
   every car whose inputs and code didn't change.
3. Copies your save folder
   (`Documents\Ubisoft\Driver San Francisco\reloaded\savegames`) to
   `out\save-backups\<date-time>\`.
4. Installs the cars beside the game's own files, with the files below. No
   game file is replaced. An earlier install is removed first.

An install from version 0.1.8 or earlier put the cars into copies of the
game's files. Installing this version over it moves the cars to the new
layout by itself: every car keeps its ID, so the save's unlocked and owned
flags stay with it, and the game's files are put back from that install's
backup.

The screen shows each step, a progress bar over the cars and the cars being
built. Everything the tools print goes to `out\logs\install-<date-time>.log`.
`.\install.cmd --help` lists the options, among them:

| Option | |
| --- | --- |
| `--list FILE`, `--car NAME` | other cars (one Forza name per line; [research/forza-roster.tsv](research/forza-roster.tsv) lists them all) |
| `--no-fm4-dlc` | leave out the FM4 DLC packs under `assets` ([below](#forza-motorsport-4-dlc-packages)) |
| `--jobs N` | cars built at a time (default: half the CPU threads, at most 8) |
| `--build-only`, `--install-only` | only build, or only install the last build |
| `--no-write-profile` | don't mark the new garage cars owned in the save (below) |
| `--status` | what is installed |
| `--migrate` | move an install from before the rename to dsfcars to the new names ([above](#installs-from-before-the-rename-to-dsfcars)) |
| `--report` | a zip for a bug report ([below](#reporting-a-bug)) |
| `--force` | replace another mod's `bootstrap.lua` (it is backed up, put back on uninstall, and ours runs it first) |

### Installs from before the rename to dsfcars

Releases before the rename called the game-side files `dsfforza`:
`plugins\dsfforza.asi` and `dsfforza.ini`, the `dsfforza\` folder,
`Common\UserLuaScripts\dsfforza\` and `dsfforza-backup\`. This version calls
them `dsfcars`, as nothing in them is Forza's any more. An install, an
uninstall or `.\install.cmd --migrate` moves an old-named install to the new
names first, without building or reinstalling a car:

- every folder is renamed as it is, `manager.json` with it, so every car
  keeps its ID and your save its flags;
- `plugins\dsfforza.asi`, `dsfforza.ini` and `dsfforza.log` are deleted, so
  only one plugin patches the game. Settings you changed in `dsfforza.ini`
  are not carried over: the new `plugins\dsfcars.ini` has the defaults;
- this version's plugin, `register.lua` and `bootstrap.lua` replace the old
  ones. A `bootstrap.lua` of another mod is left alone; if you added
  `user_open("dsfforza\\register.lua")` to it, change that to `dsfcars`.

Running it again, also after it was interrupted, finishes the move.
`dsfforza-garage.dat` next to your save stays: the plugin reads it until the
game has saved once and written `dsfcars-garage.dat`. `--status` shows an
install of the old names. The car manager (`uv run dsff car_manager`) refuses
to work on one and tells you to migrate it first.

### Forza Motorsport 4 DLC packages

The installer also builds the cars of your downloaded FM4 car packs: Xbox 360 LIVE
packages (the hash-named files without an extension, such as
`E3470A83732B9093D220BF7D6506FB30517900064D`). Put them anywhere under `assets`, in any
folders and under any names. A package is recognised by its header (Forza Motorsport 4,
title ID 4D530910, marketplace content), not by its name. The check in step 1 lists the
packs it found and skips other games' packages, saves and second copies of a pack. The
packs need FM4 disc 1 too (its game database holds the rows the packs add to).

The packs' cars are built after the disc cars ([tools/batches/fm4-dlc.txt](tools/batches/fm4-dlc.txt)
lists them pack by pack; a pack you don't have adds nothing), with the names, makers and
years of the pack. They get new IDs, so the disc cars installed before keep theirs. The 28
cars a pack shares with Forza Horizon are built from the FH1 disc, as before.
`--no-fm4-dlc` builds the disc cars only. With `--list` or `--car` only the cars named
are built (a pack car can be named like a disc car).

### Downloads

The installer downloads these on first use, each pinned to one version and
checked against its SHA-256 (they are not in the zip):

| Tool | Version | Used for |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | 0.13.5 | runs the installer with its own Python (only if uv is missing) |
| [vgmstream](https://github.com/vgmstream/vgmstream) | r2117 | decoding Forza's engine sound |
| [DriverNGHook](https://github.com/ReHamster/DriverNGToolkit) | 0.7.2 | runs the Lua that registers the cars (only if not installed) |
| [Ultimate ASI Loader](https://github.com/ThirteenAG/Ultimate-ASI-Loader) | v9.7.4 | loads DriverNGHook and the dsfcars plugin (only if the game has no ASI loader) |

### The car manager: cars and traffic

`cars.cmd` (in the release folder) opens the car manager window; `uv run dsff
car_manager gui` does the same from a checkout. Its tabs:

- **Cars**: every installed car, filtered by name, make or source (FH1, FM4,
  DLC). Enable or disable cars (the garage and traffic), or switch them in or
  out of traffic only.
- **Traffic sets**: the cars around you come from the chapter's traffic set.
  For every story chapter, or one at a time: how many of your cars join it and
  their share of the traffic, the weights (as the game has them, all models
  equally likely, or your own factor per model), vanilla models on or off,
  and vanilla models from other chapters added in. The list shows each model's
  share now and with your settings, and how many models the chapter can hold.
  A set holds at most what the game can load beside the chapter's mission
  cars (with `[archive] vehicle_slots=60` in `dsfcars.ini`, the most the game
  takes, a story chapter holds 38 to 40 models, counting the mission sets that
  share its roads; 40 is the most a set can have); past that
  the chapter would never finish loading, so
  the car manager leaves the extra cars out and says so.
- **Interesting vehicles**: the few special cars the game adds near you. Your
  cars can come as those too, and a car can be pinned to regions of the map.

**By default** (until you change it) every story chapter's traffic is filled
with your cars up to what it can hold: with 24 cars in traffic, 20 in
Chapter 1, 22 in the Exposition and Chapters 2 to 7 and all 24 in the Epilogue
(with `vehicle_slots=60`). Each of your cars is as likely as an average car of
the game's on every road, so about half (Chapter 1) to three fifths
(Epilogue) of the traffic is your cars. The game's own cars keep their mix.
`cars.cmd traffic ambient --off` puts the traffic back as the game has it.

The settings are kept in `dsfcars\cars\manager.json` (they survive a
reinstall) and take effect at the next game start. The same from the command
line: `cars.cmd traffic status`, `cars.cmd traffic off make:FERRARI`,
`cars.cmd traffic ambient --set "Chapter 7" --ports 4 --share 0.2` (or `--ports fill --share mean`),
`cars.cmd traffic ambient --weights equal`, `cars.cmd traffic --help`.
How the game picks traffic: [research/traffic.md](research/traffic.md).

### What the installer adds

| File | What |
| --- | --- |
| `dsfcars\cars\<car>\` | one folder per car: its model file (`car.sp`), garage picture, badge and nameplate |
| `dsfcars\cars\cars.txt`, `manager.json` | the list of installed cars and the ID each car keeps for good |
| `dsfcars\generated\` | copies of the game's `Install\gui.sp`, `media\Fonts.fchunk` and `Locale\<language>\global.fchunk` with the cars' pictures, badges, nameplates, names and the garage card emblem added; the plugin opens them in place of the game's files |
| `dinput8.dll` | Ultimate ASI Loader, only if no ASI loader is installed |
| `plugins\DriverNGHook.asi`, `plugins\DriverNGHook\` | DriverNGHook, only if not installed |
| `plugins\dsfcars.asi`, `plugins\dsfcars.ini` | the dsfcars plugin (below) |
| `Common\UserLuaScripts\bootstrap.lua` | loads `dsfcars\register.lua` (no test or debug scripts), after another mod's bootstrap it replaced (`dsfcars\chained-bootstrap.lua`) |
| `Common\UserLuaScripts\dsfcars\` | `register.lua`, `packages.lua` (the car list), `packages_hud.lua` (names, badges, pictures), `config.lua`, `traffic.lua` (who comes as an interesting vehicle; the car manager writes it) |
| `dsfcars\traffic\san_franciscoAI.fchunk` | only with traffic settings: a copy of the game's traffic sets with the cars added to them; the plugin opens it in place of the game's `media\san_franciscoAI.fchunk` |

The game's own files (`Install\dngvehicles.sp`, `gui.sp`, `Fonts.fchunk`,
`global.fchunk`, `san_franciscoAI.fchunk`) are not changed. `dsfcars-backup\` holds the record of the
install (`install.json`) and, if one was replaced, another mod's
`bootstrap.lua`.

**The dsfcars plugin** (`dsfcars.asi`, in the zip) patches `Driver.exe` 1.04
in memory while the game runs; the exe file itself is never changed. It loads
each car from its own file beside the game's vehicle archive, opens the
generated copies of the GUI files in place of the game's, and lifts the limits
a full roster needs: a garage that lists every added car (IDs up to 999, up to
120 makers), and save flags for them. The cars don't load without it. Its
settings are in `plugins\dsfcars.ini` (each part can be switched off), and it
writes what it did to `plugins\dsfcars.log`. If the exe isn't 1.04, or
another mod changed the same code, it patches nothing and says so in the log.
Details: [research/asi.md](research/asi.md) and
[research/car-packages.md](research/car-packages.md).

Some mods also use `Common\UserLuaScripts\bootstrap.lua`. If another mod's
file is already there, the installer stops. With `--force` it backs that file
up, keeps a copy as `dsfcars\chained-bootstrap.lua` and installs its own,
which runs the other mod's bootstrap first, so both mods load. Uninstalling
puts the other mod's file back. If another mod's installer replaces the
bootstrap later, run `.\install.cmd --install-only` again. Instead, you can add
`user_open("dsfcars\\register.lua")` to the other mod's
`Bootstrapper.Launch()` yourself.

## Uninstall

`.\install.cmd --uninstall` removes the cars (`dsfcars\cars\`,
`dsfcars\generated\` and the car lists) and every file it created, but only
if the file is still the one it installed; a file you changed afterwards is
left in place and reported. Another mod's `bootstrap.lua` it replaced is put
back from the backup, checked by its SHA-256. Then it removes
`dsfcars-backup\` (`--keep-backup` keeps it). If a file can't be restored,
the backup is kept and the message says which file it was. Running it again
finishes an interrupted uninstall. It removes an install of version 0.1.8 or
earlier the same way, putting back every game file that install replaced.

One file stays: `dsfcars\cars\manager.json`, the ID of each car. Your save
keeps a car's flags under its ID, so a later install gives every car the ID
it had.

## Reporting a bug

In a terminal in the unzipped folder, run `.\install.cmd --report`. It writes
`driversf-forza-report-<date-time>.zip` into that folder and prints a short
summary. Attach the zip to a
[GitHub issue](https://github.com/xob0t/driversf-forza/issues/new?template=bug.yml)
or to your Discord message, with a screenshot and the car's name. If you can't
attach files, paste the summary.

The zip holds:

- the version of this copy and how the cars were installed;
- Windows, its language, the CPU, RAM and free disk space, uv, Python and the
  VC++ x86 runtime;
- the game folder: whether `Driver.exe` is 1.04, each installed file and
  whether it is still the one from the last build, other mods (ASI loaders,
  `.asi` plugins, another `bootstrap.lua`), and a summary of the newest crash
  dump;
- the names and sizes of your disc images and what each one is;
- the last build: the cars, the ones that failed and why;
- the last install logs, `plugins\dsfcars.log`, DriverNGHook's `Driver.log`
  and the game's `graphics.ini` (long logs are cut down to their start and end).

No save files, disc images or game files go in, only their names and sizes.
Your Windows user folder is replaced by `%USERPROFILE%` in every path. The
command only reads; it changes nothing but writing the zip.

## Your save game

The installer copies your save folder first (step 3). Then:

- The plugin keeps the flags of the added cars (unlocked, owned, new, driven)
  in a second file next to the save, `dsfcars-garage.dat`, written whenever
  the game saves. `autosave.dat` keeps exactly the vanilla layout: the game
  without the plugin (or after an uninstall) loads it as before, and the added
  cars are simply gone.
- `dsfcars-garage.dat` stays when you uninstall. It does nothing without the
  plugin, and if you install again the cars you bought are still yours. Back
  up both files together.
- By default the mod marks each new garage car unlocked and owned on every
  game start (`dsfcars\config.lua`, `writeProfile`); the game shows a "new
  vehicle" notice once per car. `--no-write-profile` leaves that out: then
  you buy the new cars in the garage like vanilla cars.

## Known limitations

- **Traffic:**
  - The new cars join the game's "interesting vehicle" lists for each part of
    the city, so they appear among traffic now and then, not in every street.
  - With more than 24 added cars, each car is placed in only some districts.
- **Sound, collision and handling base:**
  - Each new car is built on a similar vanilla car, its host, and keeps the
    host's crash physics shape and some handling settings. The engine sound
    is the Forza car's own where its sound bank is on your discs.
  - A Forza car that DSF also has (370Z, Skyline, Camaro and others) uses
    that vanilla car's handling.
- **Damage:**
  - Cars crumple panel by panel like vanilla cars.
  - Some FM4 cars and some hosts can't be converted with damage. Those are
    built without vanilla-like damage, and the build report says so.
- **Interiors:**
  - The cockpit view uses the Forza cockpit.
  - A few cars have no usable steering wheel model. They keep the host's
    cockpit, and the build report says so.
- **Size:** every car is a file of about 5 to 6 MB in `dsfcars\cars\`, and
  `dsfcars\generated\` holds about 170 MB of GUI copies with the full roster.
- **Online:** don't take the new cars online. Other players don't have them.
- **Other mods:**
  - A mod that replaces `Install\dngvehicles.sp`, `Install\gui.sp`,
    `media\Fonts.fchunk` or `global.fchunk` can stay: the cars are added
    beside those files (not yet tried with such a mod). The build reads the
    game's original vehicle archive, so build first, then install that mod,
    then install the cars: `.\install.cmd --build-only`, the other mod,
    `.\install.cmd --install-only`. The GUI copies are made from the game's
    files as they are at install time, so install the cars again
    (`--install-only --force`) after such a mod changes them. A car whose ID the other mod's archive
    also uses is left out; `uv run dsff car_manager verify` names it and
    `uv run dsff car_manager reassign <car>` gives it a free ID (its save
    flags stay with the old ID).
  - Felony Rework 2.3 Hotfix 1 works alongside (tested with the full roster
    installed the earlier way, merged into the game's archive; not yet with
    car packages; [research/felony-rework.md](research/felony-rework.md)). Install Felony
    Rework as its readme says, then run this installer with `--force`. If the
    cars were installed first, run `.\install.cmd --install-only --force` after
    Felony Rework: its files replace our bootstrap.lua, and ours then runs it.
    Felony Rework's own DriverNGHook build and its second ASI loader
    (`version.dll`) stay. Leave the plugin's `[lua]` section switched on in
    `plugins\dsfcars.ini`: with both mods the game needs more Lua memory
    than it has without the plugin. Its garage changes use some of the
    maker-group room; `register.lua` counts what is actually there at game
    start. A felony chase in one of the added cars hasn't been tried yet.

Build details, the ID policy and what was tested: [research/batch.md](research/batch.md).
