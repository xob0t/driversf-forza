# Development

How the code is laid out, how to build and test from a git checkout, and how a
release is made. Players need only the [README](../README.md). Agents and
contributors: read [AGENTS.md](../AGENTS.md) first.

## What it does

Each Forza car is converted onto a similar vanilla DSF car (the host) and packed
as a car package (`<key>.dsfcar`), which the car manager installs beside the
game's files under an ID of its own, and `dsfcars.asi` loads beside the vanilla
`Install\dngvehicles.sp` (`research/car-packages.md`):

- body, cockpit, glass, lamps, plates, wheels and paint from Forza;
- vanilla-style damage (panels, crush, detachable parts);
- Forza's own engine sound, in a vanilla sound bank;
- handling derived from the car's specs on the host's DSF tuning;
- garage pictures, HUD badges and names.

DriverNGHook runs the Lua that registers the cars. The native plugin
`tools/asi` (`dsfcars.asi`) loads the car packages, opens the car manager's
GUI copies in place of the game's, lifts the archive, garage, save, HUD and Lua heap
limits, puts the driver's hands on each car's own steering rim, shows km/h on
the converted digital dashboards, and drops draws past the per-frame draw
queue instead of letting the game end (`research/asi.md`). Research notes for
each area are in `research/`.

The Python package is `src/dsfforza/` (`formats/`, `convert/`, `build/`,
`game/`, `harness/`). Every tool runs as `uv run dsff <tool> ...`
(`uv run dsff --help` lists them) or `uv run python -m dsfforza.<group>.<tool>`.

## Setup (git checkout)

- Windows, PowerShell 5.1 and [uv](https://docs.astral.sh/uv/)
  (`winget install astral-sh.uv`). uv installs Python 3.13 and the locked
  dependencies (`uv.lock`) into `.venv` on the first `uv run`.
- Forza disc images in `assets/` (`*.iso`), or pass `--iso`.
- `DSF_GAME_DIR`: the game folder (the one with `Driver.exe`). Every tool
  reads it; there is no built-in default.
- Visual Studio 2022 Build Tools with the C++ workload, to build `dsfcars.asi`
  (a checkout has no prebuilt plugin; a release zip does).

## Build and install: `dsff install`

The same command a player runs through `install.cmd`
(`src/dsfforza/build/install.py`):

```powershell
$env:DSF_GAME_DIR = 'X:\path\to\Driver San Francisco'
uv run dsff install                                  # every car (tools/batches/all.txt), then install
uv run dsff install --build-only --list tools/batches/testdrive.txt --jobs 2
uv run dsff install --install-only                   # install out/build as it is
uv run dsff install --status
uv run dsff install --uninstall
```

Stages: preflight (game 1.04, vanilla archive for a build, discs, disk space,
and for an install: game not running, VC++ x86 runtime, DriverNGHook and an ASI
loader present or downloadable, another mod's bootstrap.lua), prepare
(`game_data prepare` when its files are missing or stale; vgmstream), plugin
(`out/asi/dsfcars.asi`, rebuilt with `tools/asi/build.ps1 -Test` when
`out/asi/dsfcars.build.json` doesn't match the sources and Visual Studio
Build Tools are installed, else used as it is; a release uses its
bundled `plugin/dsfcars.asi`), build and pack (`port_batch build --packages`
into `out/build.new`, renamed to `out/build` when it finishes), install (save
backup, then `tools/install.ps1`, which installs the plugin and the Lua and hands
`out/build/packages` to the car manager). The install replaces no game file;
over a merged install of v0.1.8 or earlier (the cars in copies of the game's
archive and GUI files) it records that install's IDs in the car manager's map
(`car_manager seed`), restores the game's files and installs the packages on
the same IDs. Each car is reused from `out/build`, an
interrupted `out/build.partial` or `--reuse DIR` when its inputs and code are
unchanged (`research/batch.md`, "Selective rebuild"); a first run on a machine
that built by hand reuses the earlier build under `out/` with the most cars.
Tool output goes to `out/logs/install-<time>.log`. `out/gentle` (Idle priority,
at most two workers) is honoured. `uv run dsff install --help` lists every option.

Third-party tools are downloaded on first use, pinned by version and SHA-256,
into `out/tools-dl` (`src/dsfforza/build/thirdparty.py`; `uv run dsff
thirdparty list`). Nothing third-party is committed or packaged.

## The tools underneath

```powershell
uv run dsff game_data prepare      # derived data under out/ (dsff install does this)

# Plan or build a list of cars (one car per line), with garage assets
uv run dsff port_batch plan --list tools/batches/testdrive.txt
uv run dsff port_batch build --list tools/batches/testdrive.txt --gui --packages --out out/batch/b1 --jobs 6
uv run dsff port_batch build --list tools/batches/all.txt --gui --wide-glyphs --id-policy asi `
    --root-limit 3003 --packages --out out/final4 --reuse out/final3 --jobs 8

# One car into an archive (--car NAME@fh1 / @fm4 picks the game when a car is on both)
uv run dsff port_car --dsf "$env:DSF_GAME_DIR\Install\dngvehicles.sp" --out out/one.sp --car NIS_370Z_10

# The plugin
.\tools\asi\build.ps1 -Test       # out\asi\dsfcars.asi, patch-tested against Driver.exe

# Install a build folder's car packages (<build>\packages, port_batch --packages) with the
# plugin (-AsiPath, default out\asi\dsfcars.asi); no game file is replaced
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Build out\build -Download -WriteProfile
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Status
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Uninstall

# The installed cars (the car manager; install.ps1 calls it)
uv run dsff car_manager status
uv run dsff car_manager disable forza.fh1.BMW_M3E30_91
uv run dsff car_manager verify
```

## Tests

```powershell
uv run python -m tests                 # ruff check, ruff format --check, ruff ratchet, offline tests
uv run python -m tests --ci            # without tests/ci-skip.txt (they need game files)
.\tools\asi\build.ps1 -Test           # build the plugin and patch-test it
```

`tests/test_install_packages.py` runs `tools/install.ps1` on a synthetic game
folder: a fresh install, a merged install of v0.1.8 moved to packages on the
same IDs, and uninstalls that leave the game as it was.

In-game tests run in a headless VMware VM, so nothing appears on the desktop.
Build the VM once with `tools/wsb/vm-build.ps1` (set `DSF_VM_ISO` to a
Windows 10/11 ISO), then:

```powershell
.\tools\wsb\run-series.ps1 -Vm -Archives out\build\dngvehicles.sp -ProbeIds '147,1,2'
```

The build still writes the merged archive (`out\build\dngvehicles.sp`, all its
cars in a copy of the vanilla archive) for these runs; players get the car
packages. An archive named `dngvehicles.sp` goes with its build's
`dngvehicles.lua` and `gui\` (as `dsfcars\ported.lua`, `hud.lua` and the GUI
files, which `register.lua` reads for the tests), so each car has its
vehicleStats entry, badge and nameplate; `-ArchiveOnly` leaves them out. To
run the cars as players get them, install the build into a scratch game folder
(real copies of `out\vanilla`'s files and an empty `Driver.exe`; never the
installed game), then run the vanilla archive with that folder's plugin and the
car manager's files laid over the run (`research/car-packages.md`, "Packages
only"):

```powershell
.\tools\install.ps1 -GamePath out\scratch-game -Build out\b3 -SkipHook
# out\vm-overlay: the scratch game's dsfcars\ and Common\UserLuaScripts\dsfcars\packages*.lua
.\tools\wsb\run-series.ps1 -Vm -Asi out\scratch-game\plugins\dsfcars.asi -ArchiveOnly `
    -Archives out\vanilla\Install\dngvehicles.sp -Overlay out\vm-overlay -ProbeIds '1,2,3'
```

The probe switches traffic, parked cars and interesting vehicles off and
clears the road around each probe car, so traffic can't ram the stopped car
between shots; `-Traffic` leaves them on (`-TrafficIds` implies it).

Screenshots and logs land in `%LOCALAPPDATA%\Temp\dsfforza-wsb\runs\`.
Options and the other modes (host, sandbox): `research/in-game-tests.md`.

## Releases

`.github/workflows/release.yml` runs on a pushed `v*` tag: it builds
`dsfcars.asi` (`tools/asi/build.ps1 -Test -NoRun`: the offline test is
compiled, not run, as CI has no `Driver.exe`), runs `uv run python -m tests
--ci`, packages the zip (`uv run dsff package`, `src/dsfforza/build/package.py`)
and attaches it to a draft GitHub Release. `gh workflow run release.yml --ref
<branch>` runs the same steps and uploads the zip as a workflow artifact
instead of releasing it (GitHub dispatches a workflow only once it is on
`master`). The draft's notes are GitHub's list of the PRs since the last
release; a tag with a suffix (`-rc.1`, `-beta`) is marked as a pre-release.

Making a release (master keeps taking PRs meanwhile):

1. Freeze: `git worktree add ..\driversf-forza-release -b release/vX.Y.Z
   origin/master` (or the commit to release), then `.\tools\worktree-setup.ps1`
   there. Nothing else goes onto this branch except fixes for what the next
   steps find.
2. Verify from the branch: `uv run dsff install` (a full build and an install
   into the game), then a VM series on its `out\build` with a few changed cars
   (`run-series.ps1 -Vm -Archives out\build\dngvehicles.sp`, which brings the
   build's gui.sp, fonts, locale and Lua with it). Check the upgrade too: the
   install over the last release's (`car_manager status` gives every car the
   ID it had: the merged install's `ported.lua`, or the last package install's
   `manager.json`), and say in the notes whether players must install again.
   Over a release from before the rename to dsfcars (its `plugins\dsfforza.asi`,
   `dsfforza\` and `dsfforza-backup\`) check the move to the new names too:
   `install.ps1 -Migrate` (`dsff install --migrate`) on a copy of that install,
   then `car_manager status` and `verify`, and say in the notes that it runs by
   itself with the next install or uninstall (INSTALL.md, "Installs from before
   the rename to dsfcars").
3. Tag the branch head and push the tag: `git tag -a vX.Y.Z -m vX.Y.Z; git push
   origin vX.Y.Z`. Check the release run (`gh run list --workflow
   release.yml`); it leaves a draft release with the zip.
4. Write the notes on the draft, above GitHub's PR list (what changed for
   players, whether every car is converted again, known issues; see an
   earlier release), then publish it: `gh release edit vX.Y.Z --notes-file
   <file> --draft=false`. Later corrections are edits of the release.
5. Bring any fix made on the branch to master in a PR, then delete the
   branch; the tag keeps its commit.

The zip holds the git-tracked files a player's install needs (the package,
`pyproject.toml`, `uv.lock`, `tools/install.ps1`, the registration Lua, the car
lists, `install.cmd` and `tools/release/launcher.ps1`), the built
`plugin/dsfcars.asi` with its ini and `dsfcars.build.json`, and an `assets/`
folder that holds only `PUT-YOUR-FORZA-ISOS-HERE.txt`. Check what
goes in with `uv run dsff package --list --version x`.
