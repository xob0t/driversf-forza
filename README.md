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
`tools/asi` (`dsfforza.asi`) lifts the archive, garage, save, HUD and Lua heap limits.

## Setup

- Windows, PowerShell 5.1 and [uv](https://docs.astral.sh/uv/)
  (`winget install astral-sh.uv`). uv installs Python 3.13 and the
  dependencies (`uv.lock`) into `.venv` on the first `uv run`.
- Forza disc images in `assets/` (`*.iso`), or pass `--iso`.
- `DSF_GAME_DIR`: the game folder (the one with `Driver.exe`). Every tool
  reads it; there is no built-in default.

```powershell
$env:DSF_GAME_DIR = 'X:\path\to\Driver San Francisco'
uv run dsff game_data prepare      # derived data under out/ (once)
```

## Build and install

```powershell
# Plan or build a list of cars (one car per line), with garage assets
uv run dsff port_batch plan --list tools/batches/testdrive.txt
uv run dsff port_batch build --list tools/batches/testdrive.txt --gui --out out/build --jobs 6

# Install the build (backs up every replaced file); -Asi adds the plugin
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Build out\build -Asi
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Status
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Uninstall
```

One car into an archive: `uv run dsff port_car --out out/one.sp --car NIS_370Z_10`.
Every tool runs as `uv run dsff <tool> ...` or `uv run python -m dsfforza.<group>.<tool> ...`.
`--car NAME@fh1` / `@fm4` picks the game when a car is on both.

## Tests

```powershell
uv run python -m tests                 # ruff check, ruff format --check, offline tests
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

## Acknowledgements

- [DriverNGToolkit](https://github.com/ReHamster/DriverNGToolkit)
  (SoapyMan, ReHamster): DriverNGHook, which runs the registration Lua.
- [driver-tools](https://github.com/Fireboyd78/driver-tools) (Fireboyd78):
  Driver chunk, model and hierarchy formats.
- [carbin-garage](https://github.com/ItsNotPaths/carbin-garage) (ItsNotPaths):
  the Forza carbin format description.
- [ForzaTech-extraction-tools](https://github.com/Doliman100/ForzaTech-extraction-tools)
  (Doliman100): Forza string table and data layouts.
- [Ultimate ASI Loader](https://github.com/ThirteenAG/Ultimate-ASI-Loader) (ThirteenAG).
- [vgmstream](https://github.com/vgmstream/vgmstream): decoding Forza's XMA engine audio.
- [unluac](https://sourceforge.net/projects/unluac/): decompiling the game's Lua.
- [Ghidra](https://github.com/NationalSecurityAgency/ghidra) and
  [Capstone](https://github.com/capstone-engine/capstone): reverse engineering `Driver.exe`.
- Reflections and Ubisoft for Driver: San Francisco; Turn 10 and Playground
  Games for Forza Motorsport 4 and Forza Horizon.

## License

[MIT](LICENSE). The game files and Forza assets are not covered and are not
included.
