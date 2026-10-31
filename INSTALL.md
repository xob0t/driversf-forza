# Installing Forza cars in Driver: San Francisco: the details

How to install is in the [README](README.md): unzip the release, put your Forza
disc images in `assets`, run `install.cmd`. This page says what that does to
your game and your save, and what the added cars can't do.

## What install.cmd does

1. Checks the game (Driver: San Francisco 1.04, original vehicle archive, not
   running), the disc images, the Visual C++ x86 runtime and the disk space, and
   stops with what to fix before anything is built or changed.
2. Builds every car from your discs into `out\build` (at Below Normal priority).
   A later run reuses every car whose inputs and code didn't change.
3. Copies your save folder
   (`Documents\Ubisoft\Driver San Francisco\reloaded\savegames`) to
   `out\save-backups\<date-time>\`.
4. Backs up every game file it replaces to `dsfforza-backup\` in the game
   folder, with SHA-256 hashes, then installs the files below. An earlier
   install is removed first.

The screen shows each step, a progress bar over the cars and the cars being
built. Everything the tools print goes to `out\logs\install-<date-time>.log`.
`.\install.cmd --help` lists the options, among them:

| Option | |
| --- | --- |
| `--list FILE`, `--car NAME` | other cars (one Forza name per line; [research/forza-roster.tsv](research/forza-roster.tsv) lists them all) |
| `--jobs N` | cars built at a time (default: half the CPU threads, at most 8) |
| `--build-only`, `--install-only` | only build, or only install the last build |
| `--no-write-profile` | don't mark the new garage cars owned in the save (below) |
| `--status` | what is installed |
| `--report` | a zip for a bug report ([below](#reporting-a-bug)) |
| `--force` | replace files another mod changed (they are backed up and put back on uninstall) |

### Downloads

The installer downloads these on first use, each pinned to one version and
checked against its SHA-256 (they are not in the zip):

| Tool | Version | Used for |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | 0.13.5 | runs the installer with its own Python (only if uv is missing) |
| [vgmstream](https://github.com/vgmstream/vgmstream) | r2117 | decoding Forza's engine sound |
| [DriverNGHook](https://github.com/ReHamster/DriverNGToolkit) | 0.7.2 | runs the Lua that registers the cars (only if not installed) |
| [Ultimate ASI Loader](https://github.com/ThirteenAG/Ultimate-ASI-Loader) | v9.7.4 | loads DriverNGHook and the dsfforza plugin (only if the game has no ASI loader) |

### What the installer changes

| File | Change |
| --- | --- |
| `Install\dngvehicles.sp` | the vehicle archive with the new cars added (the vanilla cars are unchanged) |
| `Install\gui.sp` | garage pictures for the new cars added |
| `media\Fonts.fchunk` | new maker badges and model nameplates added to the HUD font |
| `Locale\<language>\global.fchunk` | the new cars' maker and model names |
| `dinput8.dll` | Ultimate ASI Loader, only if no ASI loader is installed |
| `plugins\DriverNGHook.asi`, `plugins\DriverNGHook\` | DriverNGHook, only if not installed |
| `plugins\dsfforza.asi`, `plugins\dsfforza.ini` | the dsfforza plugin (below) |
| `Common\UserLuaScripts\bootstrap.lua` | loads `dsfforza\register.lua` (no test or debug scripts), after another mod's bootstrap it replaced (`dsfforza\chained-bootstrap.lua`) |
| `Common\UserLuaScripts\dsfforza\` | `register.lua`, `ported.lua` (the car list), `hud.lua` (names, badges, pictures), `config.lua` |

**The dsfforza plugin** (`dsfforza.asi`, in the zip) patches `Driver.exe` 1.04
in memory while the game runs; the exe file itself is never changed. It lifts
the limits a full roster needs: the vehicle archive past 1100 entries and
2 GiB, a garage that lists every added car (IDs up to 999, up to 120 makers),
and save flags for them. Its settings are in `plugins\dsfforza.ini` (each part
can be switched off), and it writes what it did to `plugins\dsfforza.log`. If
the exe isn't 1.04, or another mod changed the same code, it patches nothing
and says so in the log. Details: [research/asi.md](research/asi.md).

Some mods also use `Common\UserLuaScripts\bootstrap.lua`. If another mod's
file is already there, the installer stops. With `--force` it backs that file
up, keeps a copy as `dsfforza\chained-bootstrap.lua` and installs its own,
which runs the other mod's bootstrap first, so both mods load. Uninstalling
puts the other mod's file back. If another mod's installer replaces the
bootstrap later, run `.\install.cmd --install-only` again. Instead, you can add
`user_open("dsfforza\\register.lua")` to the other mod's
`Bootstrapper.Launch()` yourself.

## Uninstall

`.\install.cmd --uninstall` puts every replaced file back from the backup and
checks that its SHA-256 matches the original. It deletes every file it
created, but only if the file is still the one it installed; a file you changed
afterwards is left in place and reported. Then it removes `dsfforza-backup\`
(`--keep-backup` keeps it). If a file can't be restored, the backup is kept
and the message says which file it was. Running it again finishes an
interrupted uninstall.

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
- **Size:** every car takes about 5 MB in `dngvehicles.sp`; the full roster is
  an archive of about 2.8 GB.
- **Online:** don't take the new cars online. Other players don't have them.
- **Other mods:**
  - Mods that replace `Install\dngvehicles.sp`, `Install\gui.sp` or
    `media\Fonts.fchunk` conflict with this one.
  - Felony Rework 2.3 Hotfix 1 works alongside (tested with the full roster;
    [research/felony-rework.md](research/felony-rework.md)). Install Felony
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
