<img src="assets/icon.svg" width="64" height="64" alt="">

# driversf-forza

Research and tools for adding cars to Driver: San Francisco (PC, 1.04) from
the user's own copies of Forza Motorsport 4 and Forza Horizon 1 (Xbox 360
ISOs). The project ships code and per-car manifests only, never game assets.

Planned pieces:

1. **DSF side:** document the vehicle format and how `Driver.exe` registers
   vehicles. This is where the work is now.
2. **Converter:** read the ISO and write DSF-format models, textures and
   handling into a mod folder.
3. **Plugin:** an ASI hook that registers the extra vehicles at runtime.

**Players:** `INSTALL.md` explains how to build cars from your own discs and
install or uninstall them with `tools/install.ps1`.

## Tools

Python 3.10+, standard library only. They read headers and seek, so walking a
whole archive is fast.

```sh
# Chunk tree of one vehicle; runs of identical siblings are collapsed
python tools/chnk.py tree "D:/Games/Driver San Francisco/Install/dngvehicles.sp" 3

# Top-level entries, FourCC counts, or one payload
python tools/chnk.py list <file> [--tag VEHC]
python tools/chnk.py tags <file> [--depth N]
python tools/chnk.py extract <file> 3.1.2 out/vo3d.bin

# Vehicle list: VEHC index, spool ID, display class, model name
python tools/vehicles.py "D:/Games/Driver San Francisco/Install/dngvehicles.sp" [--variants]

# One VEHC's geometry: buffers, submodels, MODL parts; OBJ export; preview PNG
python tools/vehicle_mesh.py <archive> 3 --summary
python tools/vehicle_mesh.py <archive> 3 --lod 4 --obj out/car.obj --png out/car.png

# Compiled Lua scripts (media/Scripts*.fchunk) and traffic sets (san_franciscoAI.fchunk)
python tools/lua_scripts.py extract-all "D:/Games/Driver San Francisco/media" out/lua
python tools/traffic_sets.py --models
```

```sh
# Re-serialize an archive; --verify checks the output is byte-identical
python tools/chnk.py rebuild <file> out/rebuilt.sp --verify

# Clone vehicle 266 (Desanne saloon) as new ID 304
python tools/clone_vehicle.py "D:/Games/Driver San Francisco/Install/dngvehicles.sp" \
    out/dngvehicles.clone.sp --source 266 --id 304 --name "Clone 01"
```

```sh
# Forza side: disc images, STFS packages (FM4 disc 2 car packs), car roster
python tools/xiso.py list "assets/Forza 4 [NTSCU][DVD1].iso" "media/cars/*"
python tools/stfs.py list "assets/Forza 4 [NTSCU][DVD2].iso::content/0000000000000000/4d530910/00000002/4d53091000000001"
python tools/forza_roster.py --tsv research/forza-roster.tsv
# FH1 (TypeId 5) and FM4 (TypeId 1/2) carbins
python tools/carbin.py out/fm4/NIS_370Z_10/nis_370z_10.carbin --lod 1 --obj out/370z.obj
```

`out/` is ignored and holds scratch extractions.

### Porting cars (one step)

`tools/port_car.py` takes Forza media names and writes a complete
`dngvehicles.sp` with every car added under its own spool ID:

```sh
python tools/port_car.py --dsf "D:/Games/Driver San Francisco/Install/dngvehicles.sp" \
    --out out/ported.sp --car BMW_M5_12 --car NIS_SkylineGTR_93@fm4 --car NIS_370Z_10 \
    [--host auto|N] [--sound-host auto|N] [--first-id 304] [--install-lua]
```

Each car is a clone of a stock DSF car, the host: its engine sound,
hierarchy (doors, collision, damage), handling base and paint sets carry
over. `--host auto` (the default) picks the most similar stock car per Forza
car with `tools/host_select.py`: the same model if DSF has it (Forza 370Z on
the DSF 370Z), else the same model family (Skyline R32 on the R33, Raptor on
the F-150), then engine (cylinders, layout, aspiration, power), body and
doors, size, drivetrain and era. Cop, taxi, emergency and other special
entries are left out (`--allow-special-hosts`), and so are the 13 stock
cars the converter can't use. `--host 266` builds on the ASYM Desanne, as
every port before did. `--sound-host` takes only the engine sound (NONR/VSRT)
from another stock car (untested in game). Rules and survey:
`research/hosts.md`, `research/hosts.tsv`.

Handling (`--handling dsf`, the default, `tools/handling_dsf.py`) keeps the
host's DSF tuning and changes only what Reflections' own fleet rules say the
car's specs require: mass, inertia, torque, redline, top speed, tyre radius,
grip and stiffness scaling, DSF-style gearing, drive split; steering, assist,
tyre-falloff and suspension sets come whole from the most typical of the
three best hosts. On a host of the car's own model (Forza 370Z on stock 227)
the stock values are kept. `--handling forza` uses the older Forza-derived
mapping (`tools/handling.py`). Rules, the leave-one-out check against the 48
cars DSF and Forza share, and in-game results: `research/handling.md`.

```sh
# Ranked hosts with reasons, or the stock fleet as hosts
python tools/host_select.py rank NIS_370Z_10 BMW_M5_12 NIS_SkylineGTR_93@fm4 [--top 8] [--sound]
python tools/host_select.py survey --tsv research/hosts.tsv
python tools/host_select.py roster --tsv research/host-picks.tsv   # all 573 roster cars, odd picks flagged
```

Per car it finds the car on the discs in `assets/` (FH1 first when a car is
on both; `@fh1`/`@fm4` chooses), extracts the main and cockpit carbins,
atlases and `MAXData.xml` to `out/cache/<game>/<NAME>/` once, clones the host to the next
free base ID (below 1000) with fresh resource and MODL part UIDs, and converts it with
`forza2dsf.convert` (the layout that works in game: hollow full entry from
Forza LOD 3/4/5, clean exterior from LOD 1, the car's atlases in the host's
chassis texture). It also rebuilds the interior entry from the Forza
cockpit and moves the steering hinge, gauges and head camera there (below;
`--no-cockpit` keeps the host's). An entry over the size limits (NONR over the SSLP buffer,
MDXN over the largest stock one of its kind) is rebuilt with coarser LODs.
Then it checks that no resource or part UID is defined twice, lints every
car (`upvh.py lint`, which also fails on a reference to a UID only another
vehicle defines), and writes:

- `out/ported.json`: car to spool IDs (N, N+1000, N+2000), VEHC indices,
  host, source disc and package, LODs used, wheel placement, entry sizes;
- `out/ported.lua`: `vehicleStats` entries copied from the host.
  `--install-lua` also writes it to `tools/game/lua/dsfforza/ported.lua`
  (ignored by git), which `clonetest.lua` loads; its IDs become the probe IDs
  unless `config.lua` sets them.

Wheel placement is the same for both games: wheelbase, front track, the
wheelbase midpoint and the stock ride height (Forza's y = 0 is the body
bottom, not the ground) from `Physics/MAXData.xml` (all FH1 and FM4 cars except
FH1's NIS_Leaf_11), else from `gamedb.slt` `Data_CarBody`; tyre size from the
car's wheel model.

`--glass-links` binds windscreen and rear-window glass to the host's
glass-panel records instead of the chassis, as stock glass is (untested in
game; see `research/vehicle-model.md`).

`--damage` makes the body damage like a stock car (`tools/damage.py`,
untested in game): each Forza triangle goes onto one of the host's panels
(bumpers, bonnet, boot, doors, wings, lamps, mirrors, glass), gets a crush
target copied from the host's, and lands in that panel's MODL part; the
panel hinges, bodies and collision boxes move onto the Forza panels.
`--damage hull` also fits the root collision hull. `python tools/damage.py
render <archive> --id N --by panel --damage 1 --png out.png` shows the
crushed car; `damage.py check` and `damage.py panels` inspect it.

`forza2dsf.py` (one car into an existing host slot) and `clone_vehicle.py`
(`--new-uids` renews the clone's resource and part UIDs, `--sound-host N`
swaps the engine sound) still work on their own.

### Cockpit view

The interior entry (N+1000) is the cockpit model. `tools/cockpit.py` builds
it from `<car>_cockpit.carbin`: cabin, seats, dashboard and gauges textured
from `nodamage_LOD0`/`interior_LOD0`, the steering wheel skinned to the
host's steering hinge, the speedometer and tachometer needles on the host's
gauges, and the main carbin's body at LOD 5 for the window views. Triangles
never seen from the driver's head are dropped to fit the size limit. The
head camera (VO3D 430 to 432) goes to the eye point from `gamedb.slt`
`CameraOverrides`. The gauges are calibrated to each Forza dial from the
`CarAttribs.xml` tick tables: the gauge table's sweep (needle degrees per
5000 rpm or per 80 mph), the needle's zero, and VO3D p605 (the speedometer's
last mark). Other needles (fuel, temperature) are parked, digital readouts
drawn as blank displays. Cars whose cockpit carbin is a placeholder or
missing (some FH1 cars) get the main carbin's cabin instead. Details and
risks: `research/cockpit.md`.

```sh
# Steering wheel, gauge pivots, mirrors and eye point of a Forza cockpit (Forza space)
python tools/cockpit.py fit out/cache/fh1/NIS_370Z_10 NIS_370Z_10 --gamedb out/cache/fh1/gamedb.slt

# One car into a host slot, with the cockpit (port_car does this by default)
python tools/forza2dsf.py out/dngvehicles.clone.sp out/370z-cockpit.sp --id 304     --car out/fh1/NIS_370Z_10 --name NIS_370Z_10 --keep-host-materials --textures replace     --merge-submodels --lods 4:3,8:4,16:5 --cockpit --gamedb out/db/fh1_gamedb.slt

# Check it: cutaway views with the eye marked, and the view from the head camera
python tools/cockpit.py render out/370z-cockpit.sp --id 304 --png out/cockpit.png --eye-view out/eye.png

# The gauge cluster from the head camera with the needles where the engine
# puts them at each mph,rpm (works on stock cars too)
python tools/cockpit.py gauges out/370z-cockpit.sp --id 304 --png out/gauges.png --at 0,0 --at 60,4000

# cockpit.fit over every car on the discs (BelowNormal priority): out/cockpit-survey/survey.tsv
python tools/cockpit_survey.py [--car NAME ...]
```

### Lamps, paint, garage pictures and HUD names

`forza2dsf.convert` (so `port_car.py`) also makes the lamps work and gives
the car its factory colours: `tools/lights.py` puts each Forza lamp's light
id in its vertices (semantic 9, byte 2) and moves the host's UPVH light
table onto the Forza lamps (headlamps, tail/stop, indicators, reverse...);
`tools/paint.py` replaces the host's paint palette (SPSR) with the car's
`gamedb.slt` `Combo_Colors`, shaded like the closest stock paint. Details:
`research/lights.md`. `tools/gui_assets.py` builds patched copies of
`Install/gui.sp` (a garage picture per car) and `media/Fonts.fchunk` (new
badge and nameplate glyphs, e.g. BMW) plus `dsfforza\hud.lua`, which
`register.lua` applies to `vehicleStats`: `research/gui-assets.md`.
Without a plugin the HUD has about 70 free one-byte glyph codes, so past
that cars share plates; `--wide-glyphs` (off by default, not yet confirmed
in game) gives every car its own badge and plate as 16-bit glyphs on extra
font pages, drawn by dsfforza.asi's `hud` module.

```sh
python tools/lights.py plan out/cache/fh1/NIS_370Z_10       # lamp plan against the Desanne
python tools/lights.py table out/ported.sp --id 304          # a light table with Lua names
python tools/paint.py colours out/cache/fh1/gamedb.slt NIS_370Z_10
python tools/paint.py palette out/ported.sp --id 304
python tools/gui_assets.py build out/ported.json --out out/gui --name "NIS_SkylineGTR_93=SKYLINE GT-R"
# Lamp test in the sandbox (run-series.ps1 -Lights), then the per-phase sheets
python tools/lights_diff.py %LOCALAPPDATA%/Temp/dsfforza-wsb/runs/<stamp>
```

### Many cars at once, and installing them

`tools/port_batch.py` ports a list of cars into one archive. Each car is
built in its own process at Below Normal priority. Cars that fail are left
out and listed by cause. Cars that fail only in the damage or cockpit step
are retried once without that step. The tool also writes the manifest,
`dngvehicles.lua` (the `dsfforza\ported.lua` for `register.lua`), and with
`--gui` the `gui.sp`, `Fonts.fchunk` and `hud.lua` copies. Model IDs:
1 to 61 go to cars that should be in the garage while the garage's maker
list has room, and the rest get 304 and up (`research/batch.md`).

```sh
python tools/port_batch.py plan --diverse 30                 # or --list FILE, --car, --body, --make, --year, --no-counterpart
python tools/port_batch.py build --out out/batch/first --list tools/batches/first.txt --jobs 6 --gui [--resume]
python tools/port_batch.py remerge out/batch/first --out out/batch/first-high --id-policy high
python tools/game_data.py prepare --game "C:/Games/Driver San Francisco"   # derived files on a fresh machine
```

`tools/install.ps1` (PowerShell 5.1) builds or takes a build, then backs up
and replaces `Install\dngvehicles.sp`, `Install\gui.sp` and
`media\Fonts.fchunk`. It also installs DriverNGHook and an ASI loader if
they are missing, after checking for the VC++ x86 runtime, and the player
Lua (`tools/game/lua/player/bootstrap.lua` with `register.lua`,
`ported.lua` and `hud.lua`, no test probes). `-Uninstall` restores every
file and checks it by SHA-256. `-Status` shows the current state. With
`-Asi` it also installs `plugins\dsfforza.asi` (`tools/asi`, built by
`tools/asi/build.ps1`): runtime patches of `Driver.exe` 1.04 that lift the
archive, garage and save limits (`research/asi.md`).

### Sandbox tests (preferred)

`tools/wsb/sandbox.ps1` keeps one Windows Sandbox running for all tests. The
game folder, your Driver profile and this repo are mapped read-only; results
go to `%LOCALAPPDATA%\Temp\dsfforza-wsb`. The sandbox window opens once,
without activation, behind your other windows; every game launch happens
inside it, so tests never take focus on the host.

```powershell
.\tools\wsb\sandbox.ps1 Start      # reuses the running sandbox if there is one
.\tools\wsb\sandbox.ps1 Exec -Script C:\Repo\tools\wsb\guest\run-test.ps1 -Arguments `
    '-Archive','C:\Repo\out\dngvehicles.clone.sp','-Hook','-Probe','-ProbeIds','304','-ZoomPresses','5','-Detach'
# then read %LOCALAPPDATA%\Temp\dsfforza-wsb\runs\<stamp>\result.json, Driver.log and *.png
.\tools\wsb\sandbox.ps1 Status
.\tools\wsb\sandbox.ps1 Stop
```

`run-test.ps1` builds `C:\Run` inside the guest (game files linked, with
`dngvehicles.sp` pointing at `-Archive`), adds DriverNGHook and the Lua probe,
presses Enter until Story loads and Up to leave the Shift overview, then
collects `Driver.log`, screenshots and the Lua API dump. Results so far are in
`research/in-game-tests.md`.

### VM tests (headless)

On Windows builds where the sandbox guest crashes on its first DirectX use,
`run-series.ps1 -Vm` runs the same tests in a VMware Workstation VM with no
window on the host. Build the VM once with `tools/wsb/vm-build.ps1` (about an
hour, unattended). Manage it with `tools/wsb/vm.ps1 Start|Status|Exec|Stop|Revert`.
Results land in `%LOCALAPPDATA%\Temp\dsfforza-wsb\runs\vm-<stamp>` like any
other run. Details are in `research/in-game-tests.md` (VM runs).

```powershell
.\tools\wsb\run-series.ps1 -Vm -Archives out\testdrive7\dngvehicles.sp `
    -LuaFiles 'dsfforza\ported.lua=out\testdrive7\dngvehicles.lua' -ProbeIds '147,1,2' -WaitSeconds 600 -ShotSeconds 3
.\tools\wsb\vm.ps1 Stop
```

`run-series.ps1` runs it once per archive. To see the gauges move,
`-DriveSeconds N` holds the throttle (the Up arrow, DSF's default
accelerate key; `-DriveKey W` otherwise) for N seconds each time the probe
switches to the cockpit view; the probe then stays in the cockpit longer and
logs the car's speed, and each screenshot's entry in `events.json` carries
the latest speed line. `-HoldMph '30,60,90'` makes the probe force those
speeds in turn (`gameVehicle.speed`, every frame):

```powershell
Copy-Item out\ported.lua tools\game\lua\dsfforza\ported.lua
.\tools\wsb\run-series.ps1 -Archives out\ported.sp -ProbeIds '304,305,306' -WaitSeconds 300 `
    -ShotSeconds 3 -DriveSeconds 12
```

`-Dyno` measures how cars drive (`tools/game/lua/dsfforza/dyno.lua`): each
probe ID is put at rest on a 2 km straight with traffic off, and for each
"ready" the harness holds the throttle `-DriveSeconds`, then the brake
`-BrakeSeconds` (or runs `-DynoKeys`, one key schedule per run, e.g.
`'Up:25,Down:6;Up:5,Space+Right:0.4,Up:2.5,Down:5'`); a lane keeper holds
the line (`-DynoKeep`). At most 10 IDs per session (mission spool slots).
`python tools/dyno.py <run dir>` prints 0-60, 0-100, quarter mile, braking
and steer/handbrake response per run and car:

```powershell
.\tools\wsb\run-series.ps1 -Archives out\ported.sp -ProbeIds '227,304' -WaitSeconds 1200 -ShotSeconds 5 `
    -Dyno -DriveSeconds 25 -BrakeSeconds 6 -DynoRuns 2
python tools\dyno.py $env:LOCALAPPDATA\Temp\dsfforza-wsb\runs\<stamp>
```

`tools/handling_tune.py` builds on it: `run` calibrates ported cars' p18
(torque) and p345 (axle balance) against stock 0-60 and lifted-handbrake
rotation and caches the result per car and host in
`out/cache/handling-tune.json` (port_car applies it; hand-set values from
driving feedback go there too). `recovery` measures how fast a car comes out
of a power drift with counter-steer.

### In-game test on the real install

`tools/game/dsf-test.ps1` installs and removes test files, each step
reversible. It backs up the stock archive once (verified by hash) and only
deletes files it installed itself.

```powershell
.\tools\game\dsf-test.ps1 InstallArchive -Archive out\dngvehicles.clone.sp
.\tools\game\dsf-test.ps1 InstallHook     # DriverNGHook 0.7.2, from _ref/downloads
.\tools\game\dsf-test.ps1 InstallProbe    # Lua: spool and spawn ID 304 in front of you
# Play: drive any car; [dsfforza] lines appear in <game>\Driver.log.
# While the game runs, optionally:
.\tools\game\dump-handling-defaults.ps1   # writes research/handling-defaults.tsv
# Afterwards:
.\tools\game\dsf-test.ps1 RemoveProbe
.\tools\game\dsf-test.ps1 RemoveHook
.\tools\game\dsf-test.ps1 RestoreArchive
.\tools\game\dsf-test.ps1 Status
```

The hook download lives in `_ref/downloads/dnghook-0.7.2/` (from
<https://github.com/ReHamster/DriverNGToolkit/releases/tag/0.7.2>).

### Ghidra

`tools/ghidra/run-headless.ps1` runs one script against a copy of the
analyzed `Driver.exe` database at `C:\Tools\GhidraProjects\driversf-forza`.
It never re-runs auto-analysis, opens the database read-only unless you pass
`-Save`, and runs Ghidra at Idle priority on processors 12–15, so it can run
while the game is open. A run of `FindVehicleLookup.java` takes about 35
seconds.

```powershell
.\tools\ghidra\run-headless.ps1 FindVehicleLookup.java out\ghidra\vehicle-lookup
```

## Research

- `research/dngvehicles.md`: the vehicle archive layout, from one fully
  mapped vehicle down to handling data.
- `research/vehicles.tsv`: the 150 stock vehicles with spool IDs.
- `research/exe-vehicles.md`: what `Driver.exe` does with the archive, and
  the `VEdit` Lua handling editor.
- `research/in-game-tests.md`: sandbox test results, including the working
  clone.
- `research/vehicle-model.md`: the vehicle geometry, part/LOD, material and
  texture formats.
- `research/lua-api.tsv`: the game's Lua API with help text, dumped at
  runtime.
- `research/game-lua.md`: the decompiled game scripts: traffic sets,
  interesting-vehicle regions, `vehicleStats`, garage and unlocks, and the
  proposed `tools/game/lua/dsfforza/register.lua`;
  `research/traffic-sets.tsv` lists every traffic set.
- `research/damage.md`: how stock cars damage (panel transforms and damage
  per panel through vertex semantic 9, crushed positions in semantic 12,
  the vehicle shaders) and the `--damage` converter.
- `research/hosts.md`: choosing the stock car a port is built on, the
  engine sound (VSRT), and what the converter reads from the host;
  `research/hosts.tsv` lists the stock fleet as hosts.
- `research/cockpit.md`: how DSF draws the cockpit view (interior entry,
  skinned steering wheel, gauge table, head camera) and the converter.
- `research/forza-materials.md`: which FH1 atlas each shader technique
  samples, and the DSF chassis atlas and materials for textured parts
  (`tools/forza_materials.py`, `tools/dsf_materials.py`).
- `research/lights.md`: the UPVH light table, lamp functions and the light
  byte in vertex semantic 9; the paint palettes (SPSR) and Forza's factory
  colours.
- `research/gui-assets.md`: garage pictures in `gui.sp`, the
  `vehicles_font` badge and nameplate glyphs behind `vehicleStats`
  Manufacturer/Model, and the garage's ID limit (below 304).
- `research/batch.md`: how many cars fit, the model ID policy (garage IDs
  1 to 61, the rest 304 and up), what the save and the garage's maker list
  can hold, `register.lua`'s player defaults, and the batch results.
- `research/prior-art.md`: community tools and the parent repo's Ghidra work.
- `research/forza-sources.md`: what is on the FH1 and FM4 discs, the FM4
  disc 2 STFS car packs, and FM4 vs FH1 carbin differences.
- `research/forza-roster.tsv`: every car across the discs, by media name.

`_ref/` holds local clones of reference repositories and is ignored.
