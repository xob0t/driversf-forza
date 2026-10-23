# In-game tests

Updated: 2026-10-23

All runs used the persistent Windows Sandbox harness in `tools/wsb/` (see the
README): the stock game folder mapped read-only, DriverNGHook 0.7.2, the
probe in `tools/game/lua/`, and the player's own save. Run folders are under
`%LOCALAPPDATA%\Temp\dsfforza-wsb\runs\`.

## Host runs (`run-series.ps1 -OnHost`, from 2026-10-23)

On Windows build 26300.9711 the sandbox guest bugchecks (0x3B) on its first
DirectX use (microsoft/Windows-Sandbox #131/#132), so runs from
`host-20261023-*` on are on the host while nobody uses the PC. Same probe and
run script (`run-test.ps1 -OnHost`); what differs:

- the run folder is `D:\dsfforza-run` (on the game's volume: hard links
  instead of symlinks, which need admin);
- the Driver profile in Documents is backed up to
  `dsfforza-wsb\host-session` and switched to a 1280x720 window for the
  series, then restored file by file and compared by hash (the folder itself
  can be held briefly by a scanner, so it is never deleted);
- the default speakers are muted for the series and their state restored;
- keys only go to the game: a press is dropped (`keysSkipped` in
  result.json) unless the game window is in the foreground, so nothing types
  into the user's apps;
- an elevated window in front (here the Throne VPN client) keeps the
  foreground from every medium-integrity process, injected clicks included;
  the shell's MinimizeAll reaches it, so the harness calls it before launch;
- Windows Firewall asks once per program path. The first launch of
  `D:\dsfforza-run\Driver.exe` raised the prompt; it is protected (no input
  from a non-admin process), dims the whole screen and stays until a person
  answers (it followed the screen when a monitor turned off). Once answered,
  the path has inbound rules and no prompt comes back, so keep
  `-HostRunPath` fixed. Meanwhile the harness puts the game window on a
  secondary monitor (topmost) when there is one and screenshots that
  monitor. The borderless window covers the monitor with the 1280x720 image
  centred; `out/scratch/sheet.py` crops to it.
  With a single monitor (2026-10-23: one 3440x1440 screen) the prompt sat
  over the chase car in every shot (runs `host-20261023-084934`, `-085115`);
  the harness now moves the game window to the primary screen's top-left
  corner then, clear of the centred prompt (`run-test.ps1`, `$targetScreen`).

## Result: a cloned vehicle works (confirmed-runtime)

`out/dngvehicles.clone.sp` (`clone_vehicle.py --source 266 --id 304`) loads
like the stock archive. In run `20261021-224926`:

```text
vehicleStats[304] copied from [266]
id 304: requesting (loaded before: false, inZap: true, vehicle: boolean:false)
id 304: spooled after 6 ticks
spawning under the Shift camera at (-819.29, 77.88, 1835.85, 1)
id 304: spawn returned userdata
id 304: zapping into it
id 304: PLAYER IS DRIVING IT after 3 ticks (inZap: false)
```

The player drives the clone in normal traffic, textured, with the ASYM badge
and "Desanne" on the HUD: `img/clone-304-with-stats.jpg`. The control run
`20261021-224726` (stock archive, ID 266) looks the same:
`img/stock-266-control.jpg`.

What this confirms:

- the game accepts a rebuilt `dngvehicles.sp` with 151 vehicles;
- a new ID (304, outside the stock 62–303 range) spools through
  `TrafficSpooler.RequestMissionVehicle`, so the SSLP/VSCT edits are enough
  for the spool system;
- `GameVehicleResource.create{Model_Id = 304}` builds a drivable car from the
  copied VEHC entries and the patched VSVH/VO3D/SPID/UPVH IDs;
- sharing model and texture UIDs with the source is fine, at least when the
  source isn't loaded at the same time (not yet tested with both spawned).

## The HUD needs a vehicleStats entry (confirmed-runtime)

Without help, the clone's HUD shows a stack of other cars' names and badges
(Fiat Abarth 695 SS, Ford GT, Continental Supersports, Hudson):
`img/clone-304-no-stats.jpg`, run `20261021-224452`.

The game's Lua table `vehicleStats[modelId]` holds the manufacturer badge
index, `ManufacturerName`/`ModelName` string IDs, garage pictures, price,
tier, unlock chapter and the speed/strength/drift bars (Felony Rework ships a
readable copy in `Common/LuaScripts/vehiclestats/vehiclestats.lua`). Copying
`vehicleStats[266]` to `[304]` at runtime fixes the HUD. Converted cars will
need their own entries: manufacturer, name strings (or new string IDs),
pictures and stat bars.

## Game behaviour the harness had to handle

- **The save loads into the Shift overview** (zap level 7, `inZap` true,
  `currentVehicle` is `false`). `localPlayer.position` is (0, 0, 0) there and
  zapping into a car fails until the camera is down at street level. The
  harness presses Up (extended scan code 0x48) five times; the probe waits
  until `zapcontroller.getZapLevel()` is ≤ 1, or 120 ticks pass, then spawns.
  The level is only logged every 4 ticks and the last logged value was 3, so
  which condition fired isn't recorded; either way the spawn and zap worked.
- **Placement:** `zapcontroller.ZapCameraGetTargetPos()` (global table
  `zapcontroller`) gives the world position under the Shift cursor.
  `GameVehicleResource.create{SnapToTerrain = true, Position = ...}` puts the
  car there, then `localPlayer:SetZapLevel(1, nil, false, {forcedOut = true})`
  and `localPlayer:ZapIntoVehicle(car, false, false, false, nil,
  {disableZapFlash = true})` possess it.
- **Lua can't write files:** the game's `io` resolves paths through its
  `COM:` layer and refuses writes everywhere. Use `print`; DriverNGHook
  writes it to `Driver.log` next to the exe.
- **DriverNGHook needs the VC++ 2015+ x86 runtime** (`msvcp140.dll`,
  `vcruntime140.dll`), which the sandbox image lacks.

## Lua API reference

`research/lua-api.tsv` is the game's own Lua documentation, collected at
runtime by `tools/game/lua/dsfforza/docdump.lua` through
`Documentation.collectLuaDocumentation()`: 1,447 functions and 248
properties in 99 libraries, with help text. Libraries most relevant here:
`TrafficSpooler`, `GameVehicleResource` (`create`, `swapWithModel`),
`VehicleLodSpooler`, `spoolsystem`, `zapcontroller`, `ActiveVehicles`,
`VEdit`.

`VehicleLodSpooler` has `IsInteriorLoaded(id)` and `IsExteriorLoaded(id)`.
That supports reading the two smaller VEHC variants (IDs N+1000 and N+2000)
as interior and exterior LOD models (hypothesis; which is which is open).

## Clone and source loaded together (confirmed-runtime)

Run `20261021-225831` (`-ProbeIds 266,304`): the player possesses 266, then
304 spawns in front while 266 is still loaded and the player switches into
it. Both stay resident, no crash. Shared model and texture UIDs are fine,
at least for two cars.

## Interior and exterior entries

Rendering the entries settled it (see `vehicle-model.md`): N+1000 is the
interior, N+2000 a clean exterior. In game, `VehicleLodSpooler`
reports both loaded for a driven car. `spoolsystem.IsEntityLoaded` returns
true for any package name, even made-up ones, so it can't tell which
entries are resident.

## Traffic (open)

`TrafficSpooler.RequestTrafficVehicles` is `(count, {ids})`: the binding
(0x0066E560) reads an integer at stack -2 and a table at -1, then hands the
list to 0x005A79F0. Called as a single table it fails with "bad argument #-2
(number expected, got function)".

Run `20261021-230611`: `RequestTrafficVehicles(17, {the 16 models in traffic,
304})` returns nil without error and `AllTrafficVehiclesLoaded` stays true,
but no 304 appeared in nearby traffic over 80 s of sampling (only the
player's own car). The game probably rebuilds the traffic set from its own
district/time-of-day Lua (compiled in `ScriptsLuaScripts.fchunk`). Felony
Rework's readable `trafficspoolingdata.lua` only lists mission vehicle pots.

## Forza geometry in game (confirmed-runtime)

Converter: `tools/forza2dsf.py` (FH1 carbin → DSF VEHC), `tools/exp_hollow.py`
(the layout that currently works for the full entry).

- **Geometry writing works**: stretching the clone's vertices 1.3x in x shows
  a 1.3x wider car with correct shading (`img/clone-304-wide.jpg`).
- **Normals (VDCL semantic 5)**: filling them with a constant flattens the
  lighting and reflections; they are spherical angles (byte 0 polar, byte 1
  azimuth), see `vehicle-model.md`.
- **The player's car is drawn from the N+2000 entry.** Rebuilding only that
  entry with the FH1 370Z shows the 370Z body while driving (run
  20261022... `-235127`). The full entry (N) is used for traffic, damage and
  physics.
- **Axes**: both use +y up, but Forza's front is at -z and DSF's at +z. An
  earlier note here said the axes match; that was a misread screenshot. In
  runs `-002224` and `-004928` the chase camera sees the 370Z's headlights
  and grille, so the car sits backwards on its chassis (the stock Desanne
  shows its tail lights from the same camera). `forza2dsf.Placement` now
  turns the body 180° about y; not yet confirmed in game.
- **Paint UV0** samples the vehicle's chassis texture; Forza UVs on the host's
  texture gave stripes. Pinned to a plain texel for now.
- **Full entry crash** (DriverNGHook fatal-error dialog while spooling,
  pure-virtual path, no dump): happens whenever the host's 243 SBMC records
  are removed from the full entry's DRSP, regardless of size (16k vertices),
  materials kept or not, empty parts filled or not. Works when the host's
  SBMC records stay in place, even "hollowed" (every host SBBH pointing at
  one degenerate triangle in a new tiny buffer, host VXCK/IXCK removed;
  runs `-001704`, `-001908`). So something needs the host submodel records
  (count/order/UIDs), not their vertex data. Root cause under investigation.
- **Size**: a kind-0 MDXN of 0x29B000 crashed in the otherwise working
  layout (run `-001224`); limits under investigation.
- **First complete car** (`out/370z-v1.sp`, run `20261022-002224`): hollow
  full entry with FH1 LOD3/4/5, rebuilt clean exterior (LOD1), wheel hubs
  moved to the 370Z's wheelbase and track. Loads, spawns, drives, clean
  paint, but the body is backwards (see Axes). Lights and badges are untextured (trim material).
- **Full-entry crash root cause**: new resource UIDs that don't look like
  stock ones. The engine marks resolved pointers with the top bit of UID
  byte 7, so a counter there broke past 0x80. Stock-like UIDs (byte 1 =
  0x12, byte 7 = 0x2D, `Builder.new_uid`) let a full replacement with no host
  geometry load (runs `20261022-004457`, `-004637`).
- **Textured full replacement** (`out/370z-v2.sp`, run `20261022-004928`):
  FH1 LOD3/4/5, `--keep-host-materials --textures replace --merge-submodels`.
  Spools, spawns, drives, and a second copy spawns ahead. Badge, headlights
  and grille show textured (`img/fh1-370z-v2-textured.png`), but the body is
  backwards (see Axes). The windscreen
  looks cracked, probably a glass texture/UV mismatch; not checked yet. HUD
  still says Desanne. The rest of that series (LOD2 build, limit tests) was
  stopped before it ran.

## Three ported cars with cockpits (run `20261022-015851`)

`out/cockpit/ported-cockpit.sp`: 304 FH1 370Z, 305 FH1 BMW M5, 306 FM4
Skyline GT-R R32, host 266. All three spool, drive and face the right way;
the cockpit camera switches to the converted interior (confirmed-runtime).
Defects, with causes found offline (confirmed-static) and fixed in the
converter; not yet confirmed in game, because game testing was paused:

| Defect | Screenshot | Cause | Fix |
| --- | --- | --- | --- |
| Cracked glass on an undamaged car, worst from the cockpit | `img/cockpit-v1-a.png`, `img/skyline-chase-v1.png` | Glass UV0 spread over v 0..0.97; stock glass keeps v in 0..0.25, the clean band of the engine's glass texture (`vehicle-model.md`) | `forza2dsf.glass_uv` (full, exterior, interior) |
| Cockpit nearly black, only the needle pivots lit; paint black on every converted car | `img/cockpit-v1-a.png`, `img/cockpit-v1-skyline.png` | The fourth position component is a baked sky-light term; ours was 0 (needles 4096) | `forza2dsf.vertex_sky` from Forza's baked AO byte |
| Driver's head through the roof in chase view | `img/skyline-chase-v1.png`, `img/fh1-370z-v2-textured.png` | The driver sits on the UPVH driver-seat marker (role 10), still at the Desanne's seat | `cockpit.patch_seats`: seat under the new eye, host eye-to-seat offset (`cockpit.md`) |
| (found on the way) Body 0.14 to 0.23 m too low on its wheels | | Forza y = 0 is the body bottom, the ground is StockRideHeight below (`forza-sources.md`) | `CarDims.ride_height` in `Placement` |

Rebuilt archives (same IDs, `port_car.py` defaults):

- `out/fix2/ported-fix2.sp`: all four fixes; glass bound to the chassis.
- `out/fix2/ported-fix2-links.sp`: the same plus `--glass-links`
  (windscreen and rear window bound to the host's glass-panel records, like
  stock glass). Test only if the first still shows cracks.

Offline evidence: `img/placement-before-after.png` (side view, wheel model
on the UPVH hubs; left the tested archive, right fix2: tyres were sunk into
the arches), `img/skyline-seat-after.png` (cutaway at the driver's plane:
red head box under the roof, green seat marker, yellow eye). In the tested
archive the Skyline's driver-seat marker was also on the wrong side (the
Desanne's +x; the R32 is right-hand drive); it now follows the eye.

To check in game: clean glass in chase and cockpit view, a lit dashboard
and dials, coloured paint (the Desanne palette), the driver's head under the
roof, tyres inside the arches.

### Fix2 in game (run `20261022-023610`, confirmed-runtime)

`out/fix2/ported-fix2.sp`, screenshots every 3 s:

- **Glass**: clean in chase view on all three cars; no cracks in any cockpit.
- **Driver**: head under the roof on all three (`img/fix2-370z-chase.png`,
  `img/fix2-skyline-chase.png`).
- **Body on wheels and orientation**: correct; rear lights face the chase
  camera.
- **Paint**: coloured (M5 white, `img/fix2-m5-chase.png`).
- **Cockpit**: 370Z dials lit and readable (`img/fix2-370z-cockpit.png`);
  Skyline right-hand drive, wheel and dials in place
  (`img/fix2-skyline-cockpit.png`).
- **Still wrong**: the M5 windscreen shows broad white streaks from the
  inside and its dash-top and button icons are bright white blocks
  (`img/fix2-m5-cockpit.png`); the M5 dials are dark. HUD still says
  Desanne.

### Fix3: Forza handling, stock baseline (run `20261022-024010`)

`out/fix3/ported-fix3.sp` (fix2 plus Forza-derived VO3D from
`tools/handling.py`, wired into `port_car.py`; `--no-handling` keeps the
host's). Probe order 266, 304, 305, 306.

- All four spool, drive and switch to cockpit; the handling VO3Ds load
  (confirmed-runtime). How they drive is untested: the probe gives no input.
- Stock Desanne cockpit for reference: clean windscreen
  (`img/fix3-desanne-stock-cockpit.png`).
- **Damaged ported car turns into the host.** The Skyline took damage while
  moving on the zap boost (cracked windscreen, `img/fix3-skyline-damaged-cockpit.png`,
  the expected damage look); the chase camera then shows a damaged gold
  Desanne in its place with continuous HUD speed
  (`img/fix3-skyline-damaged-becomes-desanne.png`). Cause and fix: Fix4
  below.

### Fix4: unique part UIDs (run `20261022-025601`, confirmed-runtime)

Cause of the host swap (confirmed-static): the clones kept the Desanne's 41
MODL part UIDs, in all three entries, and so did every ported car. Part
UIDs key a global table like resource UIDs, and all 6,139 stock ones are
unique (`upvh.md`, "MODL part UIDs are global keys too"). Every other
reference in the cloned entries (SBBH, MDID, VXDP/IXDP, MARP textures, MODL
LOD lists) already resolved inside the clone. In the fix3 run the swap came
right after the probe's `VehicleLodSpooler.RequestVehicle(266)`, which
re-spooled 266's interior and exterior while the Skyline was driven (log:
266 `interior=true exterior=true`, 305 evicted). The damage just before it
was probably a coincidence.

`out/fix4/ported-fix4.sp`: `clone_vehicle.renew_uids` now renews part UIDs
too and rewrites the UPVH's references to them (header, records, LOD links,
attachments). Same probe order 266, 304, 305, 306; the log shows the same
state after `RequestVehicle` (266 interior and exterior loaded, 306 still
driven). The Skyline stays a Skyline to the end
(`img/fix4-skyline-after-host-respool.png`). It took no damage in this run,
so a damaged ported car next to a loaded host is still unseen.

Regression check: `upvh.py lint` fails when a car shares a resource or part
UID with another vehicle, or references a UID only another vehicle defines
(no stock vehicle trips it; all three fix3 cars do); `port_car.py` runs it
on every car.

The M5 windscreen still streaked, now as thinner diagonal lines
(`img/fix4-m5-cockpit-streaks.png`): fix4 squashed interior glass u into
the 0 to 0.12 strip stock interiors use, but kept v 0 to 0.25.

### Fix5: interior glass in the flat block (run `20261022-030613`, confirmed-runtime)

The glass texture is in the archive after all (SSIC; `vehicle-model.md`,
"The glass texture"). The interior strip holds a grey smear texture at
v 0 to 0.06 above flat black; stock interiors put two thirds of their glass
in the black block. `out/fix5/ported-fix5.sp` maps interior glass into that
block (u 0.065 to 0.115, v 0.13 to 0.245) and exterior glass into the flat
windscreen rows (u 0.14 to 0.98, v 0.004 to 0.12, clear of the strip and
the rear-window defroster lines).

- M5 windscreen clean from the inside (`img/fix5-m5-cockpit.png`); 370Z and
  Skyline cockpits clean too (`img/fix5-370z-cockpit.png`,
  `img/fix5-skyline-cockpit.png`).
- Exterior glass looks as before in chase view on all three.
- Part UIDs: the Skyline again stays a Skyline after 266's re-spool
  (`img/fix5-skyline-after-host-respool.png`).
- `register.lua` (loaded from `bootstrap.lua` before the probe) runs
  without errors in fix4 and fix5: `vehicleStats[304..306] <- [266]` with
  Nissan/370Z, BMW/M5, Nissan/SkylineGTR and `Selectable`, and
  `SetInterestingVehicleRegions: 17 regions, 51 entries added`. Its traffic
  samples count ported cars near the player before the probe spawns them
  (fix4: one 306 at t101, while only 266 had been spawned; later up to three
  306s, one 304 and one 305), so the interesting-vehicle regions seem to put
  them into traffic. Not yet checked on screenshots. The HUD still says
  ASYM Desanne on all three cars, so the name fields it sets are not the
  ones the HUD reads (or the HUD caches them before the copy).
- Still wrong on the M5: white button icons on the steering wheel and dash,
  dark dials.

### Full detail, HUD names (run `20261022-031619`, confirmed-runtime)

`out/fix6/ported-fix6.sp`, built with `port_car.py --mdxn-limit 0x2C0000`
after the limit tests (`spool-limits.md`): all three cars at the first LOD
step (exterior Forza LOD 1, full 3/4/5). All load, drive and switch to the
cockpit. `register.lua` (now in `bootstrap.lua`) matches each car to a
stock vehicleStats entry by Picture1: the HUD shows the Nissan badge and
"370Z" for 304 (copied from 227) and the Nissan badge and "SKYLINE" for
306 (badge and Model index of 229); 305 (BMW, no BMW in DSF) keeps the
Desanne's (`img/fix6-370z-hud-name.png`). The HUD reads the numeric
Manufacturer/Model fields; ManufacturerName/ModelName strings don't reach it.

Cockpit camera too low: stock head cameras see the steering rim's top 7.9°
below eye level (median of 146, `out/eye/wheel_view.py`); ours were 6.5°
(370Z), 3.3° (M5), 1.9° (Skyline), so the wheel filled half the screen.
`cockpit.camera_eye` now raises the camera only; to confirm in game.

### Gauges (runs `20261022-035058`, `20261022-035518`, confirmed-runtime)

`out/gauges/ported-gauges1.sp` (`port_car.py` defaults; hosts 227, 147,
229): gauge tables and VO3D p605 calibrated from each car's CarAttribs tick
table (`cockpit.md`, "Gauges"). New harness options: `-DriveSeconds 12`
(run 1) holds the Up arrow, DSF's default accelerate key, and the probe
logs `gameVehicle.speed`; run 2 adds `-HoldMph '30,60,90'` (the probe sets
the speed every frame) with `-ProbeIds '266,304,305,306'`.

- **Needles move** with speed and rpm on all three cars, clockwise, from 0
  (confirmed-runtime). Read against the HUD speed
  (`img/gauges-ingame-clusters.png`; 370Z at 29 and 94 mph, M5 at 98 and
  111, Skyline at 44 and 83): 370Z speedometer at the 100 mark at 94 mph;
  M5 (km/h dial) just left of vertical (about 155 km/h) at 98 mph and at
  the top (about 180) at 111; Skyline (km/h) near 70 at 44 mph and near 135
  at 83. Each is within about one minor mark of the HUD; the screenshots
  and the HUD lag each other by a frame or two, so this is not a precise
  check. Tachometers sit at plausible values for the gear (370Z about 5500
  at 29 mph in first). Full frames: `img/gauges-370z-94mph.png`,
  `img/gauges-m5-111mph.png`, `img/gauges-skyline-50mph.png`; stock
  Desanne at 91 mph for comparison: `img/gauges-desanne-stock-91mph.png`.
- **Throttle**: holding Up works (Skyline 17 to 50 mph in run 1). Driver.log
  reaches the disk seconds late, so the harness can't see the probe's
  phases in time; it now holds the throttle from "PLAYER IS DRIVING IT" to
  DriveSeconds after "cockpit view on". In run 1 the 370Z's cockpit phase
  had passed before the harness saw it (0 mph). Forced speeds
  (`hold_mph`) are exact in the probe's log (30.00, 60.00, 90.00 mph).
- **Head camera** (`cockpit.camera_eye`): the rim now sits in the lower
  third of the screen with the dials above it, not across half the screen
  (`img/gauges-m5-111mph.png`, `img/gauges-skyline-50mph.png`); still a
  little higher than the stock Desanne's (`img/fix3-desanne-stock-cockpit.png`).
  `out/eye/wheel_view.py`: rim top 6.5° (370Z), 4.6° (M5), 5.2° (Skyline)
  below the eye; the hosts 227, 147, 229 have 8.3, 8.3, 9.1, the Desanne
  13.7. `camera_eye` aims for 7.9° using the rim's centre-line radius and
  the wheel centre's depth, while `wheel_view.py` measures the topmost rim
  vertex (tube thickness, rim tilt toward the driver), hence the shortfall.
- Digital readouts are blank dark panels (M5 centre display, 370Z).
- Still wrong: the M5 dial faces are dark in some frames (lighting:
  visible in run 1, near black in run 2's shots), M5 button icons.

### Five cars on their own hosts, damage panels (runs `20261022-040320`, `-040545`, confirmed-runtime)

`out/r7/ported-r7.sp`: 304 370Z (host 227), 305 M5 (147 CTS-V), 306 Skyline
R32 (229 R33), 307 Mustang Boss 429 (193), 308 Raptor (251); auto hosts,
`--damage panels`, gauges, lights, factory paint, camera at the stock
7.9° rim angle (all five, `out/eye/wheel_view.py`).

- All five spool, drive (throttle held, up to 100 mph) and switch to the
  cockpit; HUD shows each host's badge and name (Nissan 370Z, Cadillac
  CTS-V, Nissan Skyline, Ford Mustang, Ford F-150).
- `fullVisualDamage` on each: Forza panels crumple and swing, glass cracks
  (`img/r7-skyline-damaged.png`).
- Defects: damage overlay draws large white decal shapes on the paint
  (`img/r7-m5-damaged-decals.png`); Mustang cockpit steering rim and
  rear-view mirror show scrambled textures, bonnet speckled
  (`img/r7-mustang-cockpit-damaged.png`).

## Dyno: how cars drive (runs 20261022-041951 to -055824)

`run-series.ps1 -Dyno` with `tools/game/lua/dsfforza/dyno.lua` (loaded by
`clonetest.lua` when `config.dyno` is set) and `tools/dyno.py` for the
figures. Handling results and what they mean for the mapping:
`handling.md`, "In-game validation".

Method (confirmed-runtime unless noted):

- **Place**: after the probe puts the player in a car, `dyno.lua` writes
  `gameVehicle.matrix` every update to (1671.4, 30.6, -4452.0), heading
  -1.0257: a straight, level stretch of `marin_4l_centre_01` (route data in
  the game's own scripts, `out/dyno/roads.py`), about 2 km before the AI
  points stray 4 m from the line. It waits until
  `spoolsystem.IsLocationResident`, lets the car settle, and prints
  `dyno <id> r<k> ready`. Traffic, parked cars and interesting vehicles are
  switched off (`civilianTraffic.setTrafficOnOff(false)`,
  `ParkedVehicles.SetEnabled(false)`, `InterestingVehicleManager.Enable(false)`)
  and `GameVehicleResource.ClearAreaOfVehicles` clears the start. The
  previous car is parked behind the start line. Pedestrians still cross
  (one run in -050855 hit something at 85 mph).
- **Drive**: `run-test.ps1` reads `Driver.log` incrementally; for each new
  ready line it holds the throttle (Up) `-DriveSeconds`, then the brake
  (Down) `-BrakeSeconds`, or runs `-DynoKeys` (one schedule per run; Up,
  Down, Left, Right, Space, Shift, W, A, S, D; Right steers right, Space is
  the handbrake). Keys are pressed and released only at step changes.
  The log reaches the harness within a few seconds; the probe waits for the
  throttle, so the lag doesn't matter.
- **Log**: from the first speed above 1 m/s (with the 100 updates before
  it) one line per Lua update: `dy <id> <run> <n> <speed> <x> <y> <z>
  <lateral> <yaw> <couple>`. Lua updates run at 125 Hz (0.008 s, from
  distance / speed; positions change every update), game time keeps pace
  with real time in the sandbox. Physics steps are 0.04 s (the spec printer's
  constant at 0x00DDB90C).
- **Lane keeper**: with no steering input every car drifts left about 20 m
  in 330 m and hits the kerb (scout run -041951). Writing the matrix to
  hold the line stalls the car (0.6 m/s at full throttle, run -042557), and
  so does forcing `gameVehicle.speed` every update (run -054100: the car
  ignores steering). What works is a sideways couple:
  `GameVehicleResource.applyImpulseAtPoint({gameVehicle, Impulse, Point})`
  in vehicle space at +-1.5 m along the car, closed loop on yaw rate and
  offset. Calibration (run -043154): a couple of 50 for 40 updates turned the
  227 by about 1 rad/s, as an SI impulse on its yaw inertia (VO3D p16 =
  7524) predicts. With it the car stays within 1 to 3 m of the line over
  1.3 km. `-DynoKeep 'impulse:1:24'` lets go for good at 24 m/s, so
  scripted steering acts on a free car.
- **Figures** (`tools/dyno.py`): t = 0 where a line through the first
  samples between 1 and 5 m/s reaches the resting speed; 0-30/60/100 mph,
  quarter mile time and trap speed, speed at 10 and 20 s, top speed reached,
  60-0 mph distance from the 60 mph crossing after the peak, and for
  manoeuvres the peak yaw rate, yaw and path change over 3 s (or until 12 m
  off the line), peak slip angle and speed lost.
- **Repeatability**: the same car, run to run: 0-60 within 0.05 s, 0-100
  within 0.05 s, top speed within 0.2 mph, braking within 0.3 m. Steer
  pulses repeat within 1 deg/s; handbrake rotation varies up to 20 deg.
- **Limits**: about 10 cars per session; after that
  `TrafficSpooler.RequestMissionVehicle` stops loading (run -043801: the
  11th to 16th IDs "NOT spooled"). `out/dyno/sim.py`, a simple
  longitudinal model fitted to these runs, reproduces 0-60/0-100/1/4 mile
  only to about 10%, so it isn't used for decisions.

Stock and Forza-mapped ports on their own model's host (run -043801,
`out/physics/pairs-a.sp`, two runs each; the Forza mapping as it was before
the DSF-first default):

| Car | 0-60 s stock / port | 0-100 s stock / port | top in 25 s mph stock / port |
| --- | --- | --- | --- |
| 370Z (227 / 304) | 3.26 / 3.42 | 8.94 / 10.74 | 139.1 / 132.3 |
| GT-R (228 / 305) | 4.11 / 3.59 | 8.51 / 7.47 | 150.3 / 158.2 |
| CTS-V (147 / 306) | 3.58 / 3.35 | 9.94 / 8.18 | 130.6 / 145.1 |
| Viper ACR (182 / 307) | 2.59 / 2.80 | 7.58 / 7.02 | 145.3 / 142.2 |
| Gallardo LP570 (212 / 308) | 3.01 / 2.81 | 7.19 / 6.43 | 149.4 / 154.5 |

That spread (-11% to +20% to 100 mph on a car's own host) is what led to
the DSF-first default, which keeps the stock values on a same-model host.

### Round 2 (runs 20261022-063701 to -102557)

Handling results: `handling.md`, "Round 2". Harness notes:

- **Launch detection**: the first car of a session creeps from the Shift
  zoom taps (Up) and can pass 1 m/s long before the throttle; `dyno.py` now
  takes the last sample under 1 m/s before the car passes 5 m/s, which
  repaired the stock 225 and 228 first runs (225: 2.61 s to 60 mph, not
  11.8).
- **Diagnostic builds**: `out/dyno/build_chain.sh <list> <out>` chains
  `port_car.py` builds (one line per car: media, host or auto, dsf/forza,
  LOO flag) into one archive with a merged `ported.lua`;
  `out/dyno/patch_vo3d.py` then copies chosen parameters from a stock car
  into chosen ports, so one session compares several variants of one port
  against its stock car (GT-R: -073627; Mito and Scirocco: -081941, -083248,
  -091649, -095156).
- **Steering and handbrake inputs** run on a free car: `-DynoKeep
  impulse:1:22` lets go of the lane keeper at 22 m/s. Holding the speed with
  `gameVehicle.speed` until the input was tried (-054100) and pins the car.
- **Sessions of 10 IDs**: stock cars and ports alternate; the HUD bars
  (`Speed`, `Strength`, `Drift`) of ports now come from `ported.lua`
  (`handling_dsf.stat_bars`).
- Another helper's runs (-062630 to -063206, `out/cockpit3`) sit between
  these in the run folder.

### Drive-test archive (run 20261022-140456)

`out/play/play.sp` (+ `out/play/play.lua`, installed as
`tools/game/lua/dsfforza/ported.lua`), built with `out/dyno/build_chain.sh
out/play/list.txt` and the current defaults: DSF-first handling (round-3
rules), hosts auto, cockpit, gauges, lights, paint, `--damage panels`,
`--mdxn-limit 0x2C0000`; two cars on a forced host with `DSF_HANDLING_LOO=1`
(as if DSF didn't have them). HUD bars from `stat_bars`. All eight load,
spawn and drive:

| ID | Car | Host | Handling | 0-60 s | 0-100 s | 1/4 mile s | 60-0 m | handbrake deg / slip | Stock reference |
| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 304 | Forza 370Z | 227 370Z | stock values (same model) | 3.30 | 8.98 | 11.98 | 16.4 | 70 / 15 | 227: 3.26 / 8.94 / 11.96 / 16.7, 78 / 21 |
| 305 | Forza 370Z | 228 GT-R | DSF-first, as if DSF had no 370Z | 3.26 | 8.94 | 11.96 | 16.8 | 99 / 43 | 227 as above |
| 309 | Forza GT-R 2012 | 240 RUF CTR3 | DSF-first, as if DSF had no GT-R | 3.50 | 7.59 | 11.50 | 16.9 | 87 / 28 | 228: 4.12 / 8.52 / 11.96 / 18.7, 80 / 20 |
| 306 | Forza Skyline GT-R R32 (FM4) | 229 Skyline R33 | DSF-first, family host | 3.06 | 7.94 | 11.54 | - | 76 / 27 | 229 R33: 2.83 / 7.31 / 11.28 (run -061717) |
| 307 | Forza Mustang Boss 429 | 193 Shelby GT500 '67 | DSF-first, family host | 4.35 | 14.15 | 13.28 | 19.0 | 68 / 11 | 193: 4.60 / 14.47 / 13.64 (run -061717) |
| 308 | Forza BMW M5 2012 | 147 CTS-V | DSF-first (no DSF counterpart) | 3.54 | 7.94 | 11.68 | 17.1 | 91 / 37 | 147: 3.58 / 9.94 / 12.38 / 18.3, 83 / 27 |
| 310 | Forza Focus RS500 | 127 Giulietta | DSF-first (no counterpart, FWD hatch) | 4.71 | 13.07 | 13.35 | 20.3 | 54 / 9 | 127: 6.3 s to 60 mph |
| 311 | Forza SVT Raptor | 251 F-150 | DSF-first, family host (truck) | 4.78 | 13.82 | 13.52 | 25.0 | 46 / 8 | 251: 5.45 / 16.04 / 14.17 / 24.4, 26 / 3 |

The handbrake flick keeps the throttle on after the 0.4 s Space + Right
(`Up:2.5`), so the yaw change includes power-on rotation: a RWD car with
more power or less engine friction rotates further.

How to compare (the user's own drive test): spawn the stock car next to the
port (probe `config.lua` with `ids = { 227, 304, 305, ... }`, at most 10)
and switch between them with Shift. 304 should feel exactly like 227; 305
and 309 show how a car DSF doesn't have comes out; 306, 307, 310 and 311
show family hosts and classes; 308 (M5 on the CTS-V) slides more than the
CTS-V (106 vs 84 deg in the handbrake flick).

### Lamps, paint A/B, damage3 (runs `20261022-111857` to `-114127`, confirmed-runtime)

- `-Lights` harness mode works; the user-defined dark mood darkens the
  scene. Ported tail lamps glow at one constant level in every phase (off,
  headlights, brakes, indicators): they don't respond to `setLights`
  (`img/lamps-dark-phases.jpg`). No stock control yet, so it's open whether
  setLights works on the player's car at all.
- Paint A/B (Forza or host palette x host or grey paint cell): identical in
  all four; 370Z black, M5 grey, Skyline black (`img/paint-ab-runs.jpg`).
  Neither palette nor cell decides the colour; cause open.
- `out/damage3/ported-d3.sp` with `-Damage`: no crash; the M5 rear-bumper
  chevron is gone (`img/d3-m5-damaged.png`).

### User drive test on the real install (out/testdrive, 40 cars, IDs 1-40)

Installed with `tools/install.ps1 -Build out\testdrive -WriteProfile`. A
typo in the player bootstrap (`"dsfforza\register.lua"`: `\r` is a Lua
escape) stopped registration on the first attempt; fixed in
`tools/game/lua/player/bootstrap.lua` and reinstalled. User feedback:

- Bugatti Veyron (28): handling good; materials wrong; wheels misaligned.
- Wheels look broken on every port.
- Plymouth Cuda (7): the whole body is missing.
- Lexus IS F (12) and BMW M3 E30 (1): holes in the interior.
- Garage: ported cars show no name text.
- HUD: the make/model glyphs sit over the health bar instead of above it.
- Pagani Huayra (31): in a drift a wheel seems to lock and the drift can't
  be exited.
  Diagnosis and the drift-recovery test: research/handling.md, "Driving
  feedback: Pagani Huayra".

### Wheels and the invisible Cuda (runs `20261022-163835`, `-164332`, `-164459`, confirmed-runtime)

- `out/wheels/wheels-test.sp` (wheel orientation, rear-axle size, wheelbase
  sign fix): the M3 E30 looks right, wheels in the arches
  (`img/wheels-m3-ingame.png`).
- The Cuda on host 172 renders nothing but wheels, driver and hands, in
  chase and cockpit view. Same with the paint palette disabled. Built on
  host 193 it renders fine (`img/cuda-host172-vs-193.jpg`). So it's a
  host-172-specific converter bug, not geometry or paint.

### Round 4 harness (runs 20261022-165036 to -184447)

- **Run alignment**: the trailing brake key of one schedule could reach the
  next car before Driver.log told the guest that the run had ended (the log
  reaches the guest seconds late). It reversed the car and started its run,
  so every later schedule of that car went to the wrong run (a launch run
  with handbrake keys, and so on). `dyno.lua` now starts a run only on
  forward motion from where the car last stood. `run-test.ps1` sends keys
  once per run (`$dynoDone`) and times '@mph' steps from the last standstill
  before the target speed.
- **Lifted handbrake test**: `handling_tune.KEYS` (runs 2 and 3 a 0.4 s flick
  with the throttle lifted, run 4 a 1 s step steer) repeats to 0 to 8 deg of
  rotation on the stock cars (6 to 21 deg with the old throttle-on flick).
  `measure()` classifies by run number: a step steer can turn the car more
  than 25 deg.
- **Drift recovery** (`handling_tune.py recovery`, `dyno.manoeuvre` recover_s
  and slip_area): see research/handling.md, "Driving feedback: Pagani
  Huayra".
- **Play archive** (`out/play/play.sp` + `play.lua`, now also
  `tools/game/lua/dsfforza/ported.lua`): IDs 304 to 314, the p4 set plus the
  Huayra, built from the stock archive with the 10-year donor rule and the
  tuned cache (`out/play/list5.txt`; round-3 build kept as `play-r3.sp`).
  Smoke run 20261022-201655: every car loads and drives. Huayra 314 comes
  out of the power drift in 2.7 s (slip area 77; stock Zonda 3.9 s, 81). The
  CTS-V, GT-R, Mito and 370Z take 2.0 to 3.6 s. The Huayra's launch run
  started with the car rolling back, and dyno.py misreads its 0-60.
- **Stock files**: builds and fleet/host caches read the stock archive (out/stock or
  dsfforza-backup) through `game_paths.stock()`. The installed test-drive
  archive at D:\...\Install put the 40 ports into `dsf-fleet.json` for a
  while; they had no vehicleStats, so the fleet fits and the donor choice
  ignored them.

## Open

1. A damaged ported car while the host is loaded (no damage in the fix4
   and fix5 runs), and how converted cars deform: their bodies are one
   part, so bumpers can't hang off (`vehicle-model.md`).
2. M5 cockpit: white button blocks, dark dials (dial faces vary with lighting).
3. HUD name and badge for ported cars (register.lua's names don't reach it).
4. Raising the per-vehicle size limits.
5. Garage and vehicle-select screens with a new ID.
