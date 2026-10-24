# Installing Forza cars in Driver: San Francisco

This project adds cars from your own copies of Forza Horizon (FH1) and Forza
Motorsport 4 (FM4) to Driver: San Francisco on PC. It ships no game files. The
installer builds the cars on your machine from your disc images and changes a
few game files. It backs up each original first, and `-Uninstall` puts them
back byte for byte.

## Requirements

- **Driver: San Francisco for PC, version 1.04**, with the original
  `Install\dngvehicles.sp`. The installer checks its SHA-256 and stops if the
  file is different. If another mod has replaced it, restore the original
  first (for example with your launcher's "verify files" option).
- **Your Forza disc images (Xbox 360 ISOs):**
  - Forza Horizon: `Forza.Horizon.XBOX360.iso`.
  - Forza Motorsport 4, discs 1 and 2: `Forza 4 [NTSCU][DVD1].iso` and
    `[DVD2].iso`. Disc 2 holds the FM4 car packs.

  The file names don't matter. A car that is only on a disc you don't have is
  skipped.
- **uv**, from <https://docs.astral.sh/uv/getting-started/installation/>
  (or `winget install astral-sh.uv`; open a new PowerShell window after
  installing it). The installer runs the build with `uv run`, and uv
  downloads Python 3.13 and numpy for it by itself (into `.venv` in the repo
  folder), so you don't install Python yourself. You don't need uv if you
  install a finished build with `-Build`.
- **Windows PowerShell 5.1**, which comes with Windows 10 and 11.
- **The Microsoft Visual C++ 2015-2022 Redistributable, x86 (32-bit)**:
  <https://aka.ms/vs/17/release/vc_redist.x86.exe>. DriverNGHook needs it. The
  game is a 32-bit program, so having only the x64 runtime installed is not
  enough. The installer checks for it and stops with this link if it is
  missing.
- **DriverNGHook 0.7.2** (<https://github.com/ReHamster/DriverNGToolkit/releases/tag/0.7.2>)
  and an **ASI loader**, the Ultimate ASI Loader `dinput8.dll` from
  <https://github.com/ThirteenAG/Ultimate-ASI-Loader/releases>. The installer
  does not install either one if it is already in the game folder. Otherwise
  it uses files you pass with `-HookArchive` and `-AsiLoader`, or downloads
  them from those pages if you add `-Download`. DriverNGHook loads the small
  Lua script that gives the cars their names, badges, garage entries and
  traffic spawns. Without the hook the cars are in the game files but never
  appear.
- **Disk space and time:**
  - Disk: about 2 GB for the build and its cache, plus about 600 MB for the
    backup in the game folder.
  - Time: building 30 cars takes about 15 minutes on an 8-core PC. Each car
    is built in its own process at Below Normal priority, so the PC stays
    usable.

## Install

Close the game first. Then, from the repository folder:

```powershell
# Build the default car list (tools\batches\first.txt) and install it
.\tools\install.ps1 -GamePath '<game folder>' `
    -Iso 'E:\isos\Forza.Horizon.XBOX360.iso','E:\isos\Forza 4 [NTSCU][DVD1].iso','E:\isos\Forza 4 [NTSCU][DVD2].iso'

# Choose your own cars: a list file (one Forza media name per line), or -Car
.\tools\install.ps1 -GamePath '...' -Iso ... -List my-cars.txt
.\tools\install.ps1 -GamePath '...' -Iso ... -Car NIS_370Z_10,BMW_M5_12,NIS_SkylineGTR_93@fm4

# Install a build you already made with `uv run dsff port_batch build`
.\tools\install.ps1 -GamePath '...' -Build out\batch\first
```

If Windows refuses to run the script, start PowerShell with
`powershell -ExecutionPolicy Bypass -File .\tools\install.ps1 ...`. If the game
is under `C:\Program Files`, run PowerShell as administrator.

To pick cars, see `research/forza-roster.tsv` (every car on the discs) or ask
the build tool (it reads the game folder from `DSF_GAME_DIR`):

```powershell
$env:DSF_GAME_DIR = '<game folder>'
uv run dsff port_batch plan --diverse 30 --iso ...               # a spread over body classes
uv run dsff port_batch plan --body suv,pickup --no-counterpart --iso ...
```

### What the installer changes

| File | Change |
| --- | --- |
| `Install\dngvehicles.sp` | the vehicle archive with the new cars added (the stock cars are unchanged) |
| `Install\gui.sp` | garage pictures for the new cars added |
| `media\Fonts.fchunk` | new maker badges and model nameplates added to the HUD font |
| `dinput8.dll` | Ultimate ASI Loader, only if no ASI loader is installed |
| `plugins\DriverNGHook.asi`, `plugins\DriverNGHook\` | DriverNGHook, only if not installed |
| `Common\UserLuaScripts\bootstrap.lua` | loads `dsfforza\register.lua` (no test or debug scripts) |
| `Common\UserLuaScripts\dsfforza\` | `register.lua`, `ported.lua` (the car list), `hud.lua` (names, badges, pictures) |
| `plugins\dsfforza.asi`, `plugins\dsfforza.ini` | the dsfforza plugin, only with `-Asi` (build it first with `tools\asi\build.ps1`) |

**The dsfforza plugin** (`dsfforza.asi`) patches `Driver.exe` 1.04 in memory
while the game runs; the exe file itself is never changed. It lifts limits
that matter for big batches: with it the garage lists every added car (IDs
up to 999, up to 120 makers) and the save keeps their unlocked/owned flags.
Build it once with `.\tools\asi\build.ps1` (needs Visual Studio 2022 Build
Tools with the C++ workload), then add `-Asi` to the install command; the
installer copies `out\asi\dsfforza.asi` (or `-AsiPath <file>`). Its settings are in `plugins\dsfforza.ini` (each part
can be switched off), and it writes what it did to `plugins\dsfforza.log`.
If the exe isn't 1.04, or another mod changed the same code, it patches
nothing and says so in the log. Details: `research/asi.md`.

With the plugin, build with `-Asi -IdPolicy asi`: cars take every free ID
(1 to 61, the gaps between stock cars in 62 to 303, then 304 up) and all of
them can be in the garage. An archive with more than 1100 root entries
(about 215 added cars) or over 2 GiB is only installed with `-Asi`.

Originals are copied to `dsfforza-backup\` in the game folder, and
`dsfforza-backup\install.json` lists every file with its SHA-256 hash.

Some mods also use `Common\UserLuaScripts\bootstrap.lua`. If another mod's
file is already there, the installer stops. With `-Force` it backs that file
up, replaces it, and restores it on uninstall. While the cars are installed,
the other mod's Lua does not load. To run both, add
`user_open("dsfforza\\register.lua")` to that mod's `Bootstrapper.Launch()`
yourself.

## Uninstall

```powershell
.\tools\install.ps1 -GamePath '<game folder>' -Uninstall
.\tools\install.ps1 -GamePath '<game folder>' -Status
```

`-Uninstall` puts every replaced file back from the backup and checks that its
SHA-256 matches the original. It deletes every file it created, but only if
the file is still the one it installed. A file you changed afterwards is
left in place and reported. Then it removes `dsfforza-backup\`. Add
`-KeepBackup` to keep that folder. If a file can't be restored, the backup is
kept and the script says which file it was.

Uninstall can be resumed:

- `dsfforza-backup\install.json` records each file as it is handled.
- A file that already has its original bytes counts as restored, even if its
  backup is gone.
- If the backup folder can't be deleted because another program holds a file
  in it, you get a warning. The install is still marked as uninstalled, and
  the next install reuses or clears the folder.
- Running `-Uninstall` again finishes any interrupted run.

`-Force` on an install first uninstalls an earlier install, then installs
again. The installer also checks that `Install\gui.sp`, `media\Fonts.fchunk`
and `Locale\English\global.fchunk` are the stock 1.04 files. If another mod
changed them, it stops. With `-Force` it replaces them anyway, and
`-Uninstall` puts the other mod's versions back.

## Your save game

By default the mod writes nothing to your save:

- It does not unlock or buy cars in the save.
- It stops the game's own "unlock all vehicles" path (a cheat) from writing
  flags for cars with IDs 304 and up. The save has no room for those IDs; for
  IDs 320 and up the write would land on other cars' "driven" flags.

The new cars appear in traffic on their own. Cars with IDs 1 to 61 are also
listed in the garage, and you can buy them there like stock cars. Buying a
car is a normal save write that the game makes itself.

**With the dsfforza plugin**, cars with IDs 304 to 999 are listed too, and
their flags (unlocked, owned, new, driven) are not written into
`autosave.dat`. The plugin keeps them in a second file next to it,
`dsfforza-garage.dat`, written whenever the game saves. So:

- `autosave.dat` keeps exactly the stock layout. The game without the plugin
  (or after an uninstall) loads it as before; the added cars are simply gone.
- `dsfforza-garage.dat` stays when you uninstall. It does nothing without the
  plugin, and if you install again the cars you bought are still yours.
- Back up both files together. Deleting `dsfforza-garage.dat` resets only the
  added cars (not bought, not unlocked).

With `-WriteProfile`, the installer writes `dsfforza\config.lua`, which turns
on `writeProfile`. Then on every game start the mod marks each garage-listed
new car (IDs 1 to 61) as unlocked and owned in the save. The game shows a
"new vehicle" notice once per car. Each car takes one of the save's 304
vehicle slots. The stock cars use about 146, so even 61 added cars fit. These
flags stay in the save after an uninstall. They do no harm there, because the
garage skips IDs that have no car. Back up your save before using this
option: `Documents\Ubisoft\Driver San Francisco\reloaded\savegames\autosave.dat`.

## Known limitations

- **Garage** (without the dsfforza plugin; with it: IDs up to 999, 120
  makers of 20 cars each, see above):
  - Only cars with IDs 1 to 61 can be listed; the game's garage code stops at
    ID 303.
  - The garage groups cars by maker. It holds at most 40 makers with at most
    20 cars each, and the stock game already uses 35 makers.
  - New makers (BMW, Toyota, Honda and so on) get their own group while there
    is room. After that they share a group named "FORZA".
  - Cars beyond these limits get IDs from 304. They drive in traffic and you
    can Shift into them, but they are not in the garage.
- **Traffic:**
  - The new cars join the game's "interesting vehicle" lists for each part of
    the city, so they appear among traffic now and then, not in every street.
  - With more than 24 added cars, each car is placed in only some districts.
- **Sound, collision and handling base:**
  - Each new car is built on a similar stock car, its host, and keeps the
    host's engine sound, crash physics shape and some handling settings.
  - A Forza car that DSF also has (370Z, Skyline, Camaro and others) uses
    that stock car's sound and handling.
- **Damage:**
  - Cars crumple panel by panel like stock cars.
  - Some FM4 cars and some hosts can't be converted with damage. Those are
    built without stock-like damage, and the build report says so.
- **Interiors:**
  - The cockpit view uses the Forza cockpit.
  - A few cars have no usable steering wheel model. They keep the host's
    cockpit, and the build report says so.
  - Digital displays are blank.
- **How many cars:**
  - The archive can hold at most 215 added cars. dsfforza.asi's `archive`
    module raises that to 849 (757 with the default ID policy, all 849 with
    `-IdPolicy asi`). `install.ps1 -Asi` builds with room for them; a build
    made by hand needs `uv run dsff port_batch build --root-limit 3003`. Such
    an archive works only with the plugin installed.
  - Every car takes about 4 MB in `dngvehicles.sp`.
  - The garage picture file has room for about 98 new pictures, and the HUD
    font for about 44 new badges and nameplates. Cars past those limits keep
    a stock picture or nameplate.
- **Online:** don't take the new cars online. Other players don't have them.
- **Other mods:**
  - Mods that replace `Install\dngvehicles.sp`, `Install\gui.sp` or
    `media\Fonts.fchunk` conflict with this one.
  - Not tested together with Felony Rework. Its garage changes would use up
    some of the maker-group room; `register.lua` counts what is actually
    there at game start.

Build details, the ID policy and what was tested: `research/batch.md`. The
car conversion itself: `README.md`.
