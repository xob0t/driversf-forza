# driversf-forza

Adds cars from your own Forza Horizon (FH1) and Forza Motorsport 4 (Xbox 360
disc images) to Driver: San Francisco PC 1.04. The repo ships code only, never
game or Forza assets.

**Players:** see [INSTALL.md](INSTALL.md).

## What it does

Each Forza car is converted onto a similar stock DSF car (the host) and added
to `Install\dngvehicles.sp` under its own ID:

- body, cockpit, glass, lamps, plates, wheels and paint from Forza;
- stock-style damage (panels, crush, detachable parts);
- Forza's own engine sound, in a stock sound bank;
- handling derived from the car's specs on the host's DSF tuning;
- garage pictures, HUD badges and names.

DriverNGHook runs the Lua that registers the cars. The optional native plugin
`tools/asi` (`dsfforza.asi`) lifts the archive, garage, save and HUD limits.

## Setup

- Windows, Python 3.10+ with numpy (`python -m pip install numpy`),
  PowerShell 5.1.
- Forza disc images in `assets/` (`*.iso`), or pass `--iso`.
- `DSF_GAME_DIR`: the game folder (the one with `Driver.exe`). Every tool
  reads it; there is no built-in default.

```powershell
$env:DSF_GAME_DIR = 'X:\path\to\Driver San Francisco'
python tools/game_data.py prepare      # derived data under out/ (once)
```

## Build and install

```powershell
# Plan or build a list of cars (one car per line), with garage assets
python tools/port_batch.py plan --list tools/batches/testdrive.txt
python tools/port_batch.py build --list tools/batches/testdrive.txt --gui --out out/build --jobs 6

# Install the build (backs up every replaced file); -Asi adds the plugin
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Build out\build -Asi
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Status
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Uninstall
```

One car into an archive: `python tools/port_car.py --out out/one.sp --car NIS_370Z_10`.
`--car NAME@fh1` / `@fm4` picks the game when a car is on both.

## Tests

```powershell
python -m tools.tests                 # offline tests
.\tools\asi\build.ps1 -Test           # build the plugin and patch-test it
```

In-game tests run in a headless VMware VM, so nothing appears on the desktop.
Build the VM once with `tools/wsb/vm-build.ps1` (set `DSF_VM_ISO` to a
Windows 10/11 ISO), then:

```powershell
.\tools\wsb\run-series.ps1 -Vm -Archives out\build\dngvehicles.sp `
    -LuaFiles 'dsfforza\ported.lua=out\build\dngvehicles.lua' -ProbeIds '147,1,2'
```

Screenshots and logs land in `%LOCALAPPDATA%\Temp\dsfforza-wsb\runs\`.
Options and the other modes (host, sandbox): `research/in-game-tests.md`.

## Layout

| Path | What |
| --- | --- |
| `tools/port_batch.py`, `port_car.py`, `forza2dsf.py` | the converter |
| `tools/install.ps1` | installer and uninstaller |
| `tools/asi/` | `dsfforza.asi` plugin |
| `tools/game/lua/` | registration Lua and in-game test probes |
| `tools/wsb/` | in-game test harness |
| `tools/tests/` | offline tests |
| `research/` | format notes, findings and test results |
| `out/` | build output and caches (ignored) |

Start with `research/dngvehicles.md` (archive format), `research/hosts.md`
(how cars are built on stock hosts) and `research/in-game-tests.md`.
