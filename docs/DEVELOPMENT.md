# Development

How the code is laid out, how to build and test from a git checkout, and how a
release is made. Players need only the [README](../README.md). Agents and
contributors: read [AGENTS.md](../AGENTS.md) first.

## What it does

Each Forza car is converted onto a similar vanilla DSF car (the host) and added
to `Install\dngvehicles.sp` under its own ID:

- body, cockpit, glass, lamps, plates, wheels and paint from Forza;
- vanilla-style damage (panels, crush, detachable parts);
- Forza's own engine sound, in a vanilla sound bank;
- handling derived from the car's specs on the host's DSF tuning;
- garage pictures, HUD badges and names.

DriverNGHook runs the Lua that registers the cars. The native plugin
`tools/asi` (`dsfforza.asi`) lifts the archive, garage, save, HUD and Lua heap
limits. Research notes for each area are in `research/`.

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
- Visual Studio 2022 Build Tools with the C++ workload, to build `dsfforza.asi`
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

Stages: preflight (game 1.04, vanilla archive, discs, disk space, and for an
install: game not running, VC++ x86 runtime, other mods' files), prepare
(`game_data prepare` when its files are missing or stale; vgmstream), plugin
(`out/asi/dsfforza.asi`, rebuilt with `tools/asi/build.ps1 -Test` when
`out/asi/dsfforza.build.json` doesn't match the sources; a release uses its
bundled `plugin/dsfforza.asi`), build and pack (`port_batch build` into
`out/build.new`, renamed to `out/build` when it finishes), install (save
backup, then `tools/install.ps1`). Each car is reused from `out/build`, an
interrupted `out/build.partial` or `--reuse DIR` when its inputs and code are
unchanged (`research/batch.md`, "Selective rebuild"); a first run on a machine
that built by hand reuses the earlier build under `out/` with the most cars.
Tool output goes to `out/logs/install-<time>.log`. `out/gentle` (Idle priority
on two CPUs) is honoured. `uv run dsff install --help` lists every option.

Third-party tools are downloaded on first use, pinned by version and SHA-256,
into `out/tools-dl` (`src/dsfforza/build/thirdparty.py`; `uv run dsff
thirdparty list`). Nothing third-party is committed or packaged.

## The tools underneath

```powershell
uv run dsff game_data prepare      # derived data under out/ (dsff install does this)

# Plan or build a list of cars (one car per line), with garage assets
uv run dsff port_batch plan --list tools/batches/testdrive.txt
uv run dsff port_batch build --list tools/batches/testdrive.txt --gui --out out/batch/b1 --jobs 6
uv run dsff port_batch build --list tools/batches/all.txt --gui --wide-glyphs --id-policy asi `
    --root-limit 3003 --out out/final4 --reuse out/final3 --jobs 8

# One car into an archive (--car NAME@fh1 / @fm4 picks the game when a car is on both)
uv run dsff port_car --out out/one.sp --car NIS_370Z_10

# The plugin
.\tools\asi\build.ps1 -Test       # out\asi\dsfforza.asi, patch-tested against Driver.exe

# Install a build folder (backs up every replaced file); -Asi adds the plugin
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Build out\build -IdPolicy asi -Asi -Download -WriteProfile
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Status
.\tools\install.ps1 -GamePath $env:DSF_GAME_DIR -Uninstall
```

## Tests

```powershell
uv run python -m tests                 # ruff check, ruff format --check, offline tests
uv run python -m tests --ci            # without tests/ci-skip.txt (they need game files)
.\tools\asi\build.ps1 -Test           # build the plugin and patch-test it
```

In-game tests run in a headless VMware VM, so nothing appears on the desktop.
Build the VM once with `tools/wsb/vm-build.ps1` (set `DSF_VM_ISO` to a
Windows 10/11 ISO), then:

```powershell
.\tools\wsb\run-series.ps1 -Vm -Archives out\build\dngvehicles.sp -ProbeIds '147,1,2'
```

An archive named `dngvehicles.sp` goes with its build's `dngvehicles.lua`
and `gui\` (as `tools\install.ps1` installs them), so each car has its
vehicleStats entry, badge and nameplate; `-ArchiveOnly` leaves them out.

Screenshots and logs land in `%LOCALAPPDATA%\Temp\dsfforza-wsb\runs\`.
Options and the other modes (host, sandbox): `research/in-game-tests.md`.

## Releases

`.github/workflows/release.yml` runs on a pushed `v*` tag: it builds
`dsfforza.asi` (`tools/asi/build.ps1 -Test -NoRun`: the offline test is
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
   (`run-series.ps1 -Vm`, with the build's gui.sp, fonts, locale and Lua).
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
`plugin/dsfforza.asi` with its ini, and an empty `assets/` folder. Check what
goes in with `uv run dsff package --list --version x`.
