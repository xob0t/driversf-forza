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
| `--report` | a zip for a bug report ([below](#reporting-a-bug)) |
| `--force` | replace another mod's `bootstrap.lua` (it is backed up, put back on uninstall, and ours runs it first) |

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
| [Ultimate ASI Loader](https://github.com/ThirteenAG/Ultimate-ASI-Loader) | v9.7.4 | loads DriverNGHook and the dsfforza plugin (only if the game has no ASI loader) |

### What the installer adds

| File | What |
| --- | --- |
| `dsfforza\cars\<car>\` | one folder per car: its model file (`car.sp`), garage picture, badge and nameplate |
| `dsfforza\cars\cars.txt`, `manager.json` | the list of installed cars and the ID each car keeps for good |
| `dsfforza\generated\` | copies of the game's `Install\gui.sp`, `media\Fonts.fchunk` and `Locale\<language>\global.fchunk` with the cars' pictures, badges, nameplates, names and the garage card emblem added; the plugin opens them in place of the game's files |
| `dinput8.dll` | Ultimate ASI Loader, only if no ASI loader is installed |
| `plugins\DriverNGHook.asi`, `plugins\DriverNGHook\` | DriverNGHook, only if not installed |
| `plugins\dsfforza.asi`, `plugins\dsfforza.ini` | the dsfforza plugin (below) |
| `Common\UserLuaScripts\bootstrap.lua` | loads `dsfforza\register.lua` (no test or debug scripts), after another mod's bootstrap it replaced (`dsfforza\chained-bootstrap.lua`) |
| `Common\UserLuaScripts\dsfforza\` | `register.lua`, `packages.lua` (the car list), `packages_hud.lua` (names, badges, pictures), `config.lua` |

The game's own files (`Install\dngvehicles.sp`, `gui.sp`, `Fonts.fchunk`,
`global.fchunk`) are not changed. `dsfforza-backup\` holds the record of the
install (`install.json`) and, if one was replaced, another mod's
`bootstrap.lua`.

**The dsfforza plugin** (`dsfforza.asi`, in the zip) patches `Driver.exe` 1.04
in memory while the game runs; the exe file itself is never changed. It loads
each car from its own file beside the game's vehicle archive, opens the
generated copies of the GUI files in place of the game's, and lifts the limits
a full roster needs: a garage that lists every added car (IDs up to 999, up to
120 makers), and save flags for them. The cars don't load without it. Its
settings are in `plugins\dsfforza.ini` (each part can be switched off), and it
writes what it did to `plugins\dsfforza.log`. If the exe isn't 1.04, or
another mod changed the same code, it patches nothing and says so in the log.
Details: [research/asi.md](research/asi.md) and
[research/car-packages.md](research/car-packages.md).

Some mods also use `Common\UserLuaScripts\bootstrap.lua`. If another mod's
file is already there, the installer stops. With `--force` it backs that file
up, keeps a copy as `dsfforza\chained-bootstrap.lua` and installs its own,
which runs the other mod's bootstrap first, so both mods load. Uninstalling
puts the other mod's file back. If another mod's installer replaces the
bootstrap later, run `.\install.cmd --install-only` again. Instead, you can add
`user_open("dsfforza\\register.lua")` to the other mod's
`Bootstrapper.Launch()` yourself.

## Uninstall

`.\install.cmd --uninstall` removes the cars (`dsfforza\cars\`,
`dsfforza\generated\` and the car lists) and every file it created, but only
if the file is still the one it installed; a file you changed afterwards is
left in place and reported. Another mod's `bootstrap.lua` it replaced is put
back from the backup, checked by its SHA-256. Then it removes
`dsfforza-backup\` (`--keep-backup` keeps it). If a file can't be restored,
the backup is kept and the message says which file it was. Running it again
finishes an interrupted uninstall. It removes an install of version 0.1.8 or
earlier the same way, putting back every game file that install replaced.

One file stays: `dsfforza\cars\manager.json`, the ID of each car. Your save
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
- the last install logs, `plugins\dsfforza.log`, DriverNGHook's `Driver.log`
  and the game's `graphics.ini` (long logs are cut down to their start and end).

No save files, disc images or game files go in, only their names and sizes.
Your Windows user folder is replaced by `%USERPROFILE%` in every path. The
command only reads; it changes nothing but writing the zip.

## Your save game

The installer copies your save folder first (step 3). Then:

- The plugin keeps the flags of the added cars (unlocked, owned, new, driven)
  in a second file next to the save, `dsfforza-garage.dat`, written whenever
  the game saves. `autosave.dat` keeps exactly the vanilla layout: the game
  without the plugin (or after an uninstall) loads it as before, and the added
  cars are simply gone.
- `dsfforza-garage.dat` stays when you uninstall. It does nothing without the
  plugin, and if you install again the cars you bought are still yours. Back
  up both files together.
- By default the mod marks each new garage car unlocked and owned on every
  game start (`dsfforza\config.lua`, `writeProfile`); the game shows a "new
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
- **Size:** every car is a file of about 5 to 6 MB in `dsfforza\cars\`, and
  `dsfforza\generated\` holds about 170 MB of GUI copies with the full roster.
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
    `plugins\dsfforza.ini`: with both mods the game needs more Lua memory
    than it has without the plugin. Its garage changes use some of the
    maker-group room; `register.lua` counts what is actually there at game
    start. A felony chase in one of the added cars hasn't been tried yet.

Build details, the ID policy and what was tested: [research/batch.md](research/batch.md).
