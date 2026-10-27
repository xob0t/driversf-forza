# Lamps and paint

Updated: 2026-10-27

How stock DSF cars light their lamps and pick a paint colour, and what the
converter does about both. Code: `src/dsfforza/convert/lights.py` (lamps), `src/dsfforza/convert/paint.py`
(palettes), `dsf_materials.PAINT_TEXEL_UV` and `LIGHTS_ALPHA`; lamps and
palettes run inside `forza2dsf.convert` (`ConvertOptions.lights`,
`ConvertOptions.paint`, on by default). Evidence tags as elsewhere. Survey
scripts: `out/lights/` (`fleet_flags.py`, `fleet_bytes.py`, `spsr_fleet.py`,
`verify.py`, `lamp_alpha.py`, `global_paint_uv.py`).

## Lamps

### The light table (confirmed-static)

Every full entry's UPVH has a light table (`upvh.md`: header +0x32 count,
0x40-byte entries) and one type-17 hierarchy record per entry (record +0x30 =
table index). The records hang under the part the lamp belongs to: on the
Desanne the eight rear lamps under the body record (role 38), the front lamps
under the two headlamp parts (roles 50, 51), the high-mounted stop lamp under
the boot lid (role 5). Fleet: 0 to 27 lamps per vehicle, 15 on the Desanne.

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0x00 | vec4 | position, car space (+z forward, +x the car's left, w 1) |
| 0x10 | vec4 | direction (unit, w 0): (0, 0, 1) on headlamps, roughly the lens normal elsewhere |
| 0x20 | f32, f32 | pi, pi in all 2,281 stock entries (cone angles?) |
| 0x28 | u32 | id: a permutation of 0..n-1 per vehicle |
| 0x2C | u32 | flags, below |
| 0x30 | 16 bytes | the same constant in every entry (exporter leftover) |

Flags, little-endian bytes b0..b3:

- **b3 is the lamp's function**, an index into a table in `Driver.exe` (file
  offset 0xA38F98, 30 u32) that yields the Lua light-type mask used by
  `GameVehicleResource.setLights{gameVehicle, LightType, State}`. The mask
  names are the Lua enum at 0xDFD8C0: 1 Headlights, 2 Brakes,
  4 DaytimeRunning, 8 LeftIndicator, 16 RightIndicator, 32 Reverse, 64 Siren,
  128 DippedHeadlights, 256 FogLights, 8191 All.

  | b3 | Mask | Stock use (lamps in 150 vehicles) |
  | ---: | --- | --- |
  | 0x01 | Headlights | front white headlamp (218) |
  | 0x02 | Brakes | rear red, mostly the centre high-mounted stop lamp (158) |
  | 0x03 | DaytimeRunning | front white position lamp (194; 8 amber), rear red tail lamp (93) |
  | 0x04 / 0x05 | Left / RightIndicator | amber indicators, front and rear (292 / 279) |
  | 0x06 / 0x07 | Brakes + Left/Right | US-style red rear indicators (14 / 14) |
  | 0x08 | Reverse | rear white (257) |
  | 0x09, 0x0D..0x0F | Siren | emergency lights, b0 = flash phase |
  | 0x0B | Siren + Headlights | wig-wag headlamps |
  | 0x0C | FogLights | (23) |
  | 0x12 | DaytimeRunning | (107; front and rear) |
  | 0x14..0x17 | DaytimeRunning + Left/Right | |
  | 0x18 | DaytimeRunning + Brakes | the normal tail/stop lamp (138) |
  | 0x19..0x1C | DaytimeRunning + Brakes + Left/Right | |
  | 0x1D | Headlights + DaytimeRunning | (65) |

  Type 0x0A (DippedHeadlights) is never used. "DaytimeRunning" is what lights
  tail and position lamps, so it is effectively "lights on".
- **b2 is the colour**: 4 amber, 6 red, 8 white, 9 a second white (xenon), 0
  on sirens; 5, 0x0A, 0x0B rare.
- **b0 is a variant**: 2 or 3 on indicators, 1 on centre stop lamps and a few
  others, flash phases (0x0A..0x11, 0x26..0x28) on sirens.

`left` is +x: 231 of 237 lamps with flags 0x04040002 and 2 of 228 with
0x05040002 sit at x > 0, and the Desanne's steering wheel (LHD) is at
x = +0.43.

### Lamp vertices carry their light (confirmed-static)

Vertex semantic 9 is `(record +0x34, record +0x2A, light, 1)` (`cockpit.md`).
**The third byte is the light's id + 1**, 0 for no lamp. Across the whole
fleet (`fleet_bytes.py`) 4,341 (entry, light byte) groups exist in 146 full,
149 exterior and 26 interior entries; for 4,223 (97.3%) the vertices' centroid
is within 0.25 m of the table entry with id = byte - 1. The rest are long
lamp bars and lamps that share an id (93) or bytes without an entry (20, all
in a few vehicles). Lamp geometry is drawn almost only with the vehicle's
Lights material (Lights in the full entry, Lights_LOD1 in the exterior;
231,518 + 60,521 vertices), plus a little trim, glass and paint.

The Lights material has no per-lamp parameters (one technique, `g_texDiffuseMap`
= the chassis texture, `g_texDecalNormalMap`, specular 150, reflection 1); the
glow comes from the vertex light byte and the texture's alpha ("How a lamp
lights" below).

Stock lamp texels have low alpha that follows brightness: medians 0 to 76
per lamp function on the Desanne and the 370Z (Desanne headlamp luminance
124 to alpha 63, reverse 133 to 76, brake 50 to 2; `lamp_alpha.py`).

### What converted cars had

Every converted vertex carried (0, 0, 0, 1) or (slot, model, 0, 1): light
byte 0, so no lamp could light, brake or blink, and the light table still
held the host's lamp positions (flares and headlamp beams in the wrong places
on a shorter or longer car).

### The converter (`src/dsfforza/convert/lights.py`, offline-checked)

`LampPlan.build(car.body, place, axle_mid)`:

1. Each Forza triangle whose technique is a lamp gets a function
   (`TECHNIQUE_FUNCTION`): `head_light`, `hidhead`, `xenonhead`, `oldhead`,
   `fullbeam` to headlamp; `drlwhite`, `slorange` to position lamp;
   `tail_light`, `taillightst`, `taillight2S` to tail/stop; `reverse_light` to
   reverse; `indicator_left/right` to indicator; `fogwhite/fogred` to fog;
   `brinl/brinr`, `drlinl*/drlinr*`, `lindtailside` to the combined types.
   Lenses take a function from their colour: `detail_glass_amber` to
   indicator, `detail_glass_red` at the rear to tail; `detail_glass_clear`
   lights with the lamp of the nearest lamp triangle behind it (0.35 m),
   reflectors and housings stay unlit. At the rear the lens texels decide
   over the technique: a lens triangle on red texels is a tail lamp, and a
   red lens over an indicator brakes and indicates (0x19/0x1A; "The Lotus
   Carlton showed no brake light" below). FH1's `ShaderSettings.xml` agrees: `tail_light` has a
   BrakeColor, `taillight2S` a TailLightColor.
   A car with no headlamp technique at the front takes its front clear
   lenses as headlamps. Without those it takes the reflectors of its
   `headlight*`/`glass[LR]HL` sections, else their lamp-cover glass (`glass`,
   `lights_glass`). In the full roster that applies to 10 of 562 cars:
   CHE_CorvetteGS_10, DOD_RamSRT10_06 and FOR_MustangSVTCobraR_93, plus the
   race cars CHE_3_CorvetteC5R_04, MCL_41_McLarenF1GTR_98, MER_CLKGTR_98,
   MIT_HKS_06 and PEU_3_905EVO_93. The NASCARs CHE_2_MonteCarloSS_11 and
   FOR_1_Fusion_11 have decal headlights (`badge`) and stay dark.
2. Side from the technique (`_left/_right`) or the triangle's x; a group
   whose |x| stays under 0.3 m is a centre lamp. A centre tail lamp 8 cm or
   more above the side tails becomes a stop-only lamp (type 0x02).
3. Triangles are grouped by (function, side, end) and each group is split
   into lamps 0.2 m apart on its finest LOD, so a side repeater is not the
   headlamp indicator and a bumper driving lamp is not the headlamp
   (indicators: the cluster nearest the car's end is the main one).
4. `assign(upvh)` gives lamps the host's light entries in priority order
   (headlamps, tails, stop, indicators, reverse, position, fog; secondary
   clusters last), each an entry at its own end and side of the host
   (`entry_place`; "Lamps broke with the wrong end" below). Lamps left over
   share an entry of the same function, end and side (their geometry still
   lights), else a fallback lamp's at their end, else take a free entry at
   the other end, else stay unlit (the 370Z's rear fog lamp). The table keeps
   its size and ids, so the hierarchy is untouched.
5. `patch_upvh` writes each used entry's position (area-weighted centroid),
   direction (headlamps (0, 0, 1), else the area-weighted lens normal) and
   flags (type, colour 8/9/4/6, variant 2 on indicators, 1 on the stop lamp);
   spare entries get flags 0 (type 0, never lit) and park on the nearest lamp.
6. `add_geometry` sets byte 2 of semantic 9 on every lamp triangle (full and
   exterior entries, all LODs and the shadow LOD), keeping bytes 0 and 1.

Lamp texel alpha in the atlas (`dsf_materials.LIGHTS_ALPHA`): "matched"
(the default since the evening of 2026-10-23, "Stock lamp alpha" below)
maps `Forza alpha x (0.6 + 0.4 x max(r, g, b))` through a curve per texel
hue onto stock's lamp alpha distribution; "raw" keeps Forza's alpha
(analysis builds, `DSFFORZA_LIGHTS_ALPHA=raw`). Earlier modes, since
removed from the code: "value" (briefly the default, `out/testdrive7`) was
`max(64, (max(r, g, b) - 40) * 0.8) * Forza alpha / 255` and made lenses
glow as one bright red patch; "luma" used `(r + g + b) / 3` and no floor,
which left red lamps dark; "gamma" was Forza's glow mask squared.

Result on `out/lights/build/ported-lights.sp` (`verify.py`): the 370Z gets 16
lamps on the Desanne's 15 entries (2 headlamps, 2 position, 4 indicators with
the repeaters sharing, 2 tail/stop, high stop, 2 reverse, 2 rear markers; fog
unlit); every light byte's vertices sit within 0.05 m of their entry in the
exterior entry (the front indicators' 0.55 to 0.69 m is the shared repeater).

`uv run dsff lights plan out/cache/fh1/NIS_370Z_10` prints the plan
against the stock Desanne; `uv run dsff lights table <archive> --id 304`
prints a vehicle's light table with the Lua names.

### In-game test: the harness mode

`run-test.ps1 -Lights` / `run-series.ps1 -Lights` writes
`lights = { dark = true }` to `dsfforza\config.lua`, and `clonetest.lua` hands
each car, once the player drives it, to `dsfforza\lightstest.lua` (a module)
instead of the chase/cockpit cycle. The module spawns a second copy 12 m ahead
facing the player's car (`GameVehicleResource.create` at
`position + matrix[2] * 12`, `Heading = -matrix[2]`; `Spawn.Spawn` "against"
as fallback), meant to let the chase camera see the player's rear lamps
and the copy's front lamps (the copy never faced the camera: Open 1), logs `[dsfforza.lights] id N: ready 18 phases`, and then
applies whatever phase the harness writes to `dsfforza\lightsync.lua`
(`return { phase = k }`, read with `io.open`, falling back to `user_open` on
a modify-time change). Phases 1 to 9: off, Headlights, DaytimeRunning,
Brakes, DaytimeRunning + Brakes, Reverse, Left, Right, All, in daylight;
10 to 18 the same under a dark user mood (`Mood.new` with a literal night
table, `addMoodUserDefinedOverrideZap`). The harness takes
`lights-<id>-<k>a.png` and `b.png` 0.7 s apart per phase (indicators blink)
and writes phase -1 to finish. Without the sync file the module advances
every 20 s on its own. `uv run dsff lights_diff <run dir>` makes a sheet
per car: each phase next to its brightening against the "off" phase of the
same lighting.

Since 2026-10-23 the mode does more per car (`lightstest.lua`, driven by
`run-test.ps1` `Invoke-LightPhases`):

1. The car is teleported to `dyno.lua`'s spot on marin_4l_centre_01 (the
   matrix written every frame until the area is resident; one write per
   clonetest tick did nothing), parked cars are switched off and the spot
   cleared, and two driverless copies are placed ahead (`GameVehicleResource.create`; one was meant to
   face the camera, but both show their rear, see Open). Every car is
   framed the same way.
2. Sync 90 / 91: back on the spot in daylight / under the dark mood, with
   no `setLights` call, and the harness drives: idle, throttle 2.2 s, brake
   3 s (into reverse), throttle 0.7 s (braking out of the reverse), idle.
   Shots `lights-<id>-d<n><step>.png` (day) and `-n<n><step>.png` (dark):
   `0idle`, `6throttle` (rolling, lamps off), `1brake`, `2brake`,
   `3reverse`, `4brakeup`, `5idle`. The Lua logs the distance moved per
   update and `isReversing`. With traffic on, a drive can hit a traffic car
   and break lamps (370Z in run 124438). `-LightsDriveOnly` skips the
   `setLights` phases (`lights = { phases = false }`).
3. Phases 1 to 18 as before, on the player's car and both copies.
4. No traffic phase. Two tries in `lightstest.lua` (sync 92 to 94, since
   removed): `Spawn.Spawn` plus `civilianTraffic.AddVehicleToCarrier`
   returned false and the car stayed parked (run 20261023-115652);
   `GameVehicleResource.swapWithModel` on the nearest live traffic car
   crashed the game (run 20261023-123840). Traffic is left on (switched
   off, the probe counted 0 cars and nothing to swap, run 121707).

`lightsync.lua` writes retry for 5 s on a sharing violation (the game reads
the file every update; run host-20261023-095111 stopped on one at stock 171
phase 10). `run-series.ps1` now passes `-TrafficIds`. Crops and comparisons:
`out/lamps/scripts/compare.py <run>/crop OUT ids shots [box]`, `report.py`,
`redcells.py`; offline checks `audit.py`, `lamptex.py`, `walk.py`,
`fleet_alpha.py`, `subclass.py` (same folder).

### First lamp runs (20261022-063206, -111857 to -113549)

Run 063206 (lightstest from `bootstrap.lua`) found no dark mood (`moodList`
is not a global; moods.lua loads into a module). In the harness runs `io.open`
sync and the literal dark mood worked (dark phases look like night); the
facing copy failed (`gameVehicle.heading` is a number; changed as above,
but it still shows its rear: Open 1);
`getLightTypeBrightness` returned nil for a name and for `{LightType}` (a mask
is tried next). In the dark phases the 370Z's and Skyline's tail lamps glowed
flat and saturated at the same level in every phase, never responding. No
stock control was in those runs, so whether `setLights` works on the
player's car at all was open. Settled below ("Stock control and real
driving"): the player's car holds its brake lamps on at rest whatever
`setLights` says, so a lit tail lamp looks the same in every phase.

### Coloured lamp lenses drawn as glass (DMC-12 report on `out/testdrive4`, ID 27)

Symptom (confirmed-runtime): the DMC-12's tail lamps showed a white and pink
grid, with crack lines in the lenses of the undamaged car (run
`host-20261023-082346`, `observe-06`; clean but still white and pink in
`host-20261023-090511`, `observe-10`; zoom `out/dmc/run3A-rear.png`).

Cause (confirmed-static): Forza draws the DMC's tail lamps as a white bulb
reflector (`tail_light`, `reverse_light`, `textured_reflector`) under a grid
lens (`lights_glass`, sections `glassLTL/RTL`, UVs on the red, dark red,
amber and white grid cells of `lights_LOD0`, v 0.66 to 1). The converter
classed every lamp-cover glass technique (`glass`, `lights_glass`,
`lights_gls_noemit`) as glass: the lens went to DSF's shared clear glass, bound
to the rear bumper record (21, 25) and mapped onto the side-front slot of the
glass texture, so the reflector showed through white and the lens took the
glass texture's crack bands. In the DSF atlas the tail lamps sampled
(207, 203, 204) with alpha 132 on average against the stock DeLorean's
(52, 38, 36), alpha 1 (`out/dmc/lampuv.py`). Stock DSF tail lamps are opaque
Lights-material texels; no stock car has glass over its tail lamps except the
GT-R (228).

Fix (`dsf_materials.Texturing.is_lens`, `lights.LampPlan.build(lenses=...)`):
a lamp-cover glass subsection behind Forza z 0.5 whose mean texel has red
minus max(green, blue) of at least 80 and alpha of at least 128 is drawn from
the atlas with the Lights material, and its triangles light with the lamp
behind them like a clear `detail_glass_clear` lens (within 0.35 m). Front
covers stay glass: on the Challenger SRT8, S65, 300C and Golf R their UVs land
on red texels that Forza doesn't show. Survey of the 40 testdrive cars
(`out/dmc/lensurvey.py`, `out/dmc/lenses.py`): the rule takes the tail lenses
of the M3 E30, Silvia, 300C, M5 E28, 205 T16 and DMC-12 (98 to 135 red, alpha
138 to 241); clear or smoked rear covers (Supra 31, RX-7 58, IS F 13, Range
Rover 45, Civic, 500 Abarth, S2000) and the low-alpha ones (Durango, Express,
Jeep, the 205's bumper lens) stay glass. In game (`host-20261023-091014`,
`observe-10`, `out/dmc/run3L-rear.png`) the DMC's lamps are a red grid with
white reverse cells and no crack lines, like the stock DeLorean's; final
build `out/dmc/final` in `host-20261023-094929` (`observe-06`). The lens
triangles carry the light bytes of the lamps behind them (tail 13/14, reverse
7/8 in the full and exterior entries). In the lamp-phase run
`host-20261023-095111` (`-Lights`, `out/dmc/lights-dmc.png`) the DMC's lamps
look the same in every phase, day and dark; `lights_diff.py`'s whole-frame
numbers are dominated by traffic (stock 147 scores the same) and the 171
control stopped after phase 10 (`lightsync.lua` locked by another process).
The DMC's lamps do respond in real driving (next sections).

### How a lamp lights (confirmed-static, Driver.exe 1.04)

- **Shader.** The Lights vertex shader (GPCH 130 and its variants) reads
  semantic 9 as `(panel, panel data, light, 1)` and passes on
  `g_v4LightColours[light]` (c160, 32 entries). The pixel shader adds
  `texel.a x colour.rgb` to the lit colour before fog; when the colour's w
  is set (the lamp is broken) the emission is also multiplied by the damage
  decal's alpha at UV1 (the broken-lamp blobs). A lamp glows by its texel
  alpha only; its diffuse is lit like any other surface.
- **Colours.** `0x8A78E0` fills the array per draw: entry 0 is zero, entry
  `id + 1` = `0xF3FB68[type] x lamp brightness x colour[b2] x tint`, w = the
  lamp's broken bit. With no lamp on, nothing is uploaded (all zero).
- **Lamp objects.** `0x8E82B0` walks the UPVH records; each type-17 record
  makes lamp object `id` (type b3, blink variant b0, colour b2) from table
  entry `record +0x30`. A record whose dword at +0x0C is 0 has its subtree
  skipped; statically every light of stock 147, 171, 227, 266 and of the
  converted cars is reached (`walk.py`). Light bytes, panel bindings,
  material, render class (5) and vertex format of converted lamps match
  the host's (`audit.py`, `subclass.py`).
- **Brightness.** `0x8E6640` moves a lamp towards a target from per-type
  tables built by `0x8E6C80`: 1 for single-function types when on; for the
  combined types 0x13 and 0x18..0x1D, 1 when braking and, with
  DaytimeRunning alone, 0.2 (0x18..0x1C) or 1 (0x13, 0x1D). That is scaled
  by `0xF3FB68[type]`, which `GameVehicleResource.getLightTypeBrightness(n)`
  returns for the type number n (not the Lua mask; names return nil):
  1 Headlights 2, 2 Brakes 1, 3 DaytimeRunning 2, 4..7 indicators 0.5,
  8 Reverse 1, 9..17 10, 18 3, 19 10, 20..28 1, 29 10, 30 0.2. So a 0x18
  tail/stop lamp shows 0.2 with only the running lights and 1 when
  braking; a type 3 tail lamp shows 2 with the running lights.

### Stock control and real driving (runs host-20261023-103726 to -121707)

Confirmed-runtime: stock 227 (warm-up), 266, 171, 147 against the ports
370Z, M3 E30, DMC-12, S2000 and Durango (`out/lamps/build*`) in the same
runs:

- **`setLights` on the player's car:** DaytimeRunning, Headlights and the
  indicators respond (171's tail dots in dark phase 12 and red pixel mean
  161 to 170 in daylight phases 3, 5, 9; 147's left indicator in daylight
  phase 7, run 095111); Brakes and Reverse don't: the player's vehicle sets
  them from its own input every frame. At rest the brake lamps are on (the
  Desanne's high stop lamp and tails lit at idle and in every dark phase,
  `out/lamps/r112147-darkphases.png`). The driverless copies keep their
  brake lamps on in every phase too. This, and the 0.2 of a 0x18 lamp with
  only the running lights, is why the first runs saw converted tail lamps
  "flat in every phase".
- **Real driving:** stock and converted lamps follow the same state: dark
  while rolling on the throttle, lit while braking and at rest, white
  reverse lamps while reversing (`out/lamps/r115652-drive.png`: 266, 147,
  370Z, DMC-12, M3 E30, daylight and dark). The state machine runs on the
  table types alone, so converted cars need nothing more there.
- **Converted lamps were too dim** (run 112147,
  `out/lamps/r112147-compare.png`): the 370Z's tails and high stop lamp
  hardly glowed under the dark mood while the Desanne's tails and bar lit
  up. Cause (confirmed-static): the "luma" rule took `(r + g + b) / 3`, a
  third of a red lens's red, so red texels got almost no alpha: 370Z high
  stop lamp mean 1.9, tails 10.2; in `out/testdrive5` 17 of 40 cars had a
  brake or tail mean below 10 (S2000 and Durango high stop lamps 0.0).
  Stock red lamp texels average 20 (75 vehicles, `fleet_alpha.py`; per car
  9 to 58). Fix (`dsf_materials`): the "value" rule (brightest channel) and
  a floor of 64 for lenses that are near black, which Forza lights by its
  glow mask alone (S2000 (31, 2, 2), Durango (19, 0, 0)). Means now: 370Z
  tails 49, high stop 54; S2000 high stop 37; Durango 19; DMC-12 tails 78;
  M3 E30 tails 97 (`lamptex.py`). In game (runs 113752, 115652) the
  370Z's tails and high stop lamp glow when braking and at rest like the
  Desanne's, and go dark on the throttle. Full rebuild `out/testdrive7`
  (this rule; only the Express van's tails stay low, mean 12.7) in run
  124438 (`out/lamps/r124438-final.png`): S2000, DMC-12 and Express light
  when braking and reversing like stock 266; the 370Z hit a traffic car in
  its drive and its broken lamps stayed dark (as stock lamps do when
  broken).
- **Then too bright and flat** (user report on `out/testdrive7`: "braking
  lights on custom cars are too bright, they are uniformly red"). Under
  "value" the red lamp means were 19 to 97 against stock's 9 to 58, and the
  floor and the saturation of Forza's mostly-high mask (below) gave whole
  lenses the same alpha, so a braking lamp lit as one red patch where
  stock lamps light along their lens pattern. Fixed by "matched" (next
  section).
- **Converted cars as traffic: not seen.** `-TrafficIds` with eight ports
  (run 125721): `RequestTrafficVehicles` returned nil and none appeared in
  about 90 traffic cars over 80 s; the two tries at making one traffic car
  of the model failed or crashed (harness section). Traffic lamps go
  through the same path (traffic AI `0x779960` -> `0x8D0470(3, Brakes,
  ...)` -> the lamp component and its table types), which the runs above
  show working on converted cars, so converted traffic should brake-light
  like stock (hypothesis until a converted car is seen in traffic).

### Stock lamp alpha and the "matched" rule (offline-checked)

Stock lamp texels, all texels under lamp triangles (UV0 rasterised, full
entry LOD 4) of 146 stock vehicles (`out/lamps/scripts/fleet_tex.py`,
samples `out/lamps/stock_tex.npy`):

| Lamps | Colour | Mean alpha | p10 / 25 / 50 / 75 / 90 / 99 | > 128 | Per-vehicle mean p10 / 50 / 90 / max |
| --- | --- | ---: | --- | ---: | --- |
| tail + stop (0x18..0x1C) | red | 49 | 2 / 11 / 30 / 66 / 127 / 247 | 10% | 23 / 43 / 81 / 128 |
| stop (0x02) | red | 33 | 0 / 3 / 18 / 44 / 85 / 218 | 5% | 13 / 30 / 62 / 111 |
| tail (0x03 red) | red | 22 | 0 / 0 / 7 / 22 / 59 / 213 | 4% | |
| stop + indicator (0x06, 0x07) | red | 30 | 0 / 3 / 20 / 35 / 64 / 251 | 5% | |
| indicators | amber | 50 | 1 / 11 / 32 / 72 / 125 / 242 | 9% | 23 / 46 / 84 / 153 |
| reverse | white | 58 | 1 / 11 / 36 / 82 / 156 / 253 | 14% | 24 / 53 / 108 / 150 |
| headlamps | white | 71 | 3 / 16 / 49 / 108 / 180 / 255 | 20% | 41 / 70 / 110 / 161 |

So stock alpha is an authored glow mask: low over most of a lens, with LED
dots, bulb hot spots and lit strips near 255 (Desanne tail: a dot grid over
the red part, two bright strips in the reverse part; headlamps a radial
hot centre; `out/lamps/tex-266-stock.png`). It follows the texel's
brightness only loosely (correlation +0.2 to +0.4).

Forza's lamp alpha (raw build of the 370Z, M3 E30, DMC-12, S2000, S5S
Raptor and Challenger SRT8, `out/lamps/raw6.sp`, samples
`out/lamps/raw_tex.npy`) is high almost everywhere: tail + stop mean 187,
median 195, 85% above 128; the lens body sits at 0.6 to 0.8 with the bulb
glows at 1.0 (`out/lamps/forza/NIS_370Z_10_rgb_a.png`). Indicator lenses
are mostly clear (80% of their texels white-hued; the amber comes from the
lamp colour). Any rule that scales this mask (luma, value) keeps the lens
body near one level, hence the flat patch.

"matched" (`dsf_materials.matched_lamp_alpha`): score `s = a x (0.6 + 0.4
v)` (a = Forza alpha, v = brightest channel, both 0..1: the glow mask leads,
so a near-black lens glows only where its mask is bright, and darker cells
and edges of a lens glow less), then a piecewise-linear curve per texel hue
(`lamp_hue`: red, amber, white) from quantile matching the six cars' scores
to the stock distribution of that colour (`fit_lut.py`; knots in
`LAMP_CURVES`, stock percentiles 0, 5, 10, 20 ... 90, 93, 95, 97, 98, 99,
99.5; red also 85 and 100). The curve is monotonic, so each lens keeps Forza's order of texels:
hot spots near 255, the body low (`out/lamps/forza/*_value_vs_matched.png`:
Forza RGB, "value", "matched"). Per car and lamp on the six cars: tail +
stop means 18 to 95 (370Z 25, S2000 18, DMC-12 38, S5S 37, M3 E30 75,
Challenger 95), stop lamps 10 to 124, reverse 36 to 97, indicators 29 to
132, headlamps 25 to 135, inside or at the edge of stock's per-vehicle
range (the Challenger's stop lamp, 124, is above stock's 111).

In game with one shared curve (`out/lamps/matched`, runs
host-20261024-001505 and -002212) the lens structure showed, but the
S2000's and S5S Raptor's lamps barely lit: a dim Forza mask stays dim
through a shared curve (S2000 tail median 2). Two changes
(`out/lamps/matched3`):

- **Red curve per car** (`red_lamp_curve`, `Texturing._red_lamp_curve`):
  the knots for red texels come from this car's own red texels inside its
  lights regions (percentiles of their scores -> stock's red alpha
  percentiles), so every car's red lamps land on stock's distribution in
  their own order. White and amber keep the shared curves.
- **Red lenses darkened to stock's brightness** (`matched_lamp_texel`,
  `_LENS_X_RED`/`_LENS_Y_RED`): Forza's red lamp texels are brighter than
  stock's (brightest channel median 144 against 103; M3 E30 199, DMC-12
  158), so the lenses looked lit before braking. Their brightest channel is
  mapped down by a quantile curve (never brightened); alpha is scored on
  the original texel.

Per car now (`lamptex.py`): tail + stop means 370Z 41, M3 E30 69, DMC-12
34, S2000 44, S5S 91, Challenger 36 (stock per-vehicle 23 to 81, max 128);
stop lamps 370Z 57, S2000 35, Challenger 65 (stock up to 111); red lens
brightness DMC-12 129 (was 169), M3 E30 149 (181).

In game (runs host-20261024-003943, -004749, -005601, drive-only:
`-Lights -LightsDriveOnly`; sheet `out/lamps/lamps-stock-value-matched.png`,
stock / "value" / "matched", braking under the dark mood and in daylight;
per run `out/lamps/r003943-zoom.png`, `r004749-zoom.png`,
`r005601-zoom.png`): the converted lamps now light along their lens
pattern, the 370Z's tails along their inner edge with the high stop bar,
the S2000's round tails from a hot centre, the M3 E30's and DMC-12's
cells unevenly instead of as flat blocks, the Challenger's strip in
segments, and no brighter than the stock cars beside them (stock 370Z 227,
DeLorean 171, Desanne 266, Sports Car 07 278, Challenger-like 173). Lamps
whose Forza lens has no structure (the DMC-12's grid cells) still glow
cell by cell, more evenly than the stock DeLorean's dots.

### Red lenses too bright when unlit (user report on `out/testdrive9`, Audi TT RS)

Symptom (confirmed-runtime): the TT RS's tail lamps were one flat pink-red
patch. In run `vm-20261024-051926` (`out/testdrive10`, `-Lights
-LightsDriveOnly`; red lamp pixels of the daylight drive frames,
`out/emissive/redpix.py`) they measured red 224 to 229 in every step, rolling
on the throttle with the lamps off included, against stock 159 to 178 on the
throttle (Desanne 266, A4 136, 370Z 227) and 173 for the converted 370Z. So
the lens itself was too bright, not its glow: the alpha of what the camera
sees (below) is in stock's range.

Measure (offline-checked, `out/emissive/rearlamps.py`): an orthographic view
from straight behind, z-buffered over the lamp geometry (triangles with a
light byte) of the full entry, keeping the pixels of red brake-capable lamps;
per pixel the texel's alpha and brightest channel, so lamps count by the
surface the player sees, not by texel count, and bulbs and reflectors behind
an outer lens don't count. Stock, 140 vehicles with red lamps seen from
behind: brightest channel pooled p5 35, p25 71, median 92, p75 115, p95 153
(per-vehicle median p10 64, p50 96, p90 126); alpha per-vehicle mean p10 21,
p50 40, p90 81. `out/testdrive10`: alpha per-vehicle mean p10 19, p50 39,
p90 57 (in range), brightness median 112 per vehicle (TT 127, stock A4 101).

Cause (confirmed-static): the shared lens curve (`_LENS_X_RED`) was fitted on
every red lamp texel, hidden layers included. Forza lamps are layered (bulb,
reflector, then a lens over them: the TT's `detail_glass_red` covers the
whole lamp and is Forza 177 throughout, 128 after the curve), so the surface
the player sees sat wherever its texels fell in that mix.

Fix (`dsf_materials.red_lens_scale`, `Texturing.visible_red`): the same rear
view on the Forza side (Lights-atlas triangles of the finest planned LOD,
Forza's rear is +z, 5 mm steps) gives each car's visible red texels; after
the shared curve, every red lamp texel of the car is scaled so the median of
the visible ones is at most stock's 92 (never brightened; under 400 samples
the shared curve only). Alpha is untouched. Survey of the 40 testdrive cars
(`out/emissive/lenssurvey.py`): 31 are darkened, by x0.57 (205 T16, Forza
224) to x0.97 (Charger Daytona, Golf R); TT x0.72 (128 to 92), M3 E30 x0.68;
the RX-7, S2000, IS F, Jeep, Range Rover, S5S Raptor, Veyron, Huayra, 370Z,
M5 F10 and Mustang already sit at or below 92. Built (`out/emissive/build2`,
DSF side): TT visible brightness 127 to 87, M3 E30 124 to 84, 370Z 74
unchanged; alpha unchanged.

In game (run `vm-20261024-060832`, `out/emissive/build2`, IDs 3 = TT, 4 = M3
E30, next to stock 136 and 266; `research/img/emissive-ttrs-tails.jpg`): on
the throttle in sun the TT's tails are a deeper red (red lamp pixels 201,
was 224; stock 266 173 in the same run) and braking shows a hot centre
instead of a flat patch; the M3 E30 measures 174 on the throttle, as stock.
The TT still reads a little brighter than stock in direct sun with its
texels below stock's median: its lens tilts up into the sky (lighting, not
texture; open). The A4 control was hit by traffic in that run.

### The Lotus Carlton showed no brake light (user report on `out/final4`, spool 495)

Symptom (confirmed-runtime, user screenshot): the FM4 LOT_Carlton_89 (host
229) at rest showed dark tail lamps; stock 229 lights its tails and high
stop lamp at rest and when braking (run `vm1-20261026-220339`,
`lights-229-d1brake.png`).

Cause (confirmed-static): Forza draws the Carlton's whole tail cluster as
one `detail_glass_amber` lens (`glassLTL/RTL`, 185 triangles) whose texels
are red (mean (140, 40, 40), alpha 245), with a small orange band (160, 86,
52) and the `tail_light` bulb (68 triangles) hidden behind it. The converter
took lens colours from the technique name, so the lens became an amber
indicator, and stock lamps are opaque Lights texels: the bulb never shows.
The car had no visible lamp that brakes (master plan: indicator 589 + 599
cm², reverse 176 cm², tail/stop 0).

Survey (offline, `out/brakes/survey.py`): an orthographic view from behind
(5 mm pixels, z-buffered over the non-glass triangles of one Forza LOD;
front view with `FRONT=1`). Each visible lamp pixel is counted by the table
type of the light entry its triangle gets, plus whether its texel is red.
It covers the 573 cars of `tools/batches/all.txt` on their `out/final4`
hosts. With `BUILT=1` the LOD is the one the full entry's finest mask (4) is
built from: Forza LOD 3 for all 573 cars in `out/final4`. Without it, it is
the finest LOD (1). `compare.py` and `summary.py` diff two runs; `IMG=dir`
writes the colour-coded views (`research/img/brakelights-plan.png`, LOD 1).
On master at LOD 3, five cars show no brake-capable lamp: the Carlton (0.8
cm²), the two NASCARs (decal lamps), FOR_EscortRSCosworth_92 and
TOY_6_SC430_08 (below). 26 cars have less than half of their visible red
lamp area braking.

Fix (`lights.LampPlan.build(..., texel=texel_sampler(atlases))`):

- **Rear lens texels decide.** `texel_class` of the mean Forza texel under
  each lens triangle: red (hue under 10 degrees, saturation 0.35 or more,
  alpha 64 or more), amber, else white. Fleet, finest LOD: 99% of
  `detail_glass_red` triangles at the rear sit at -15 to +10 degrees,
  `detail_glass_amber` at the front mostly at 10 to 35 (`lensfleet.py`). At
  the rear a lens triangle on red texels is a tail lamp whatever its
  technique, and an "amber" lens more than half red by area is a red lens
  (its white rim triangles too). Transparent texels are clear: VAU_Agila_11's
  clear lens samples (123, 0, 0) at alpha 0 over its reverse lamps. Front
  lenses keep their technique.
- **The bulb behind a red lens** (`_red_lens_indicators`): the nearest
  non-lens lamp triangle whose outline, seen from behind, holds the lens
  triangle's centre (else the nearest across the view). An indicator there
  makes the lens brake and indicate (0x19/0x1A, as on stock US-style cars):
  FOR_GT_05's red lenses over its `drlinl/drlinr` lamps, MIN_Coopers_11's red
  glass over its indicator bulbs. A reverse bulb keeps a lens that is red by
  its texels (not its technique) a reverse lamp, as it lit before
  (PEU_207RC_07, TOY_CelicaSSI_03). A red "amber" lens on a side with no
  other rear indicator also brakes and indicates (MIT_GTO_97: (76, 0, 0)).
- **Clear lenses** still light with the nearest lamp centre, but the centre
  leaves out lens triangles that are red by their texels: with them the
  JAG_XFR_10's tail centre moved onto its lens and its clear lens went to the
  reverse lamp.
- **Entries.** The new brake-and-indicator lamps (52 cars) come after the
  reverse lamps; one without an entry shares its side's tail entry, and a
  position lamp without one shares the head lamp (front) or tail lamp
  (rear) entry instead of staying dark.

Result (offline, master -> branch, at the built LOD 3):

- 129 cars change at the rear.
- Visible brake-capable area over the fleet goes from 337,268 to 352,338
  cm². It is up on 107 cars and down on none; visible red lamp area that
  can't brake grows on no car.
- Cars with less than half of their red area braking: 26 -> 15.
- The Carlton: tail/stop 0.8 -> 1,117 cm², indicator band 121 + 122,
  reverse 12.
- Costs:
  - AUD_R8_08 and PEU_308GTI_11 lose their visible reverse lamps (322 and
    321 cm² of red-texel lens, now tail lamps; at LOD 3 no reverse bulb sits
    behind it).
  - AUD_RS4_06's and VW_BoraVR6_03's red lens indicators share their tail
    entries on full hosts (no blinking rear indicator).
  - Fog lamps of 7 cars lose their entry to the new lamps:
    cad_ctsvcoupe_11, FOR_Fiesta_09, FOR_GT_05, GMC_YukonXL_01, BMW_M6_13,
    FER_458Spider_12, SSC_UltimateAero_10.
- In game (`vm2-20261027-054204` master and `vm2-20261027-045417` branch,
  `-Lights -LightsDriveOnly`, IDs 229 stock, 1 Carlton, 2 GTO, 3 Ford GT, 4
  207 RC; `research/img/brakelights-vm-sheet.jpg`): on master the Carlton's
  lamps stay dark when braking, day and dark. On the branch they light like
  stock 229's, its orange band blinks, and the lower band lights white in
  reverse. The Ford GT's round lamps dim on the throttle and light on the
  brake. The 207 RC's side tails don't glow on master or branch: their
  Lights texels have alpha median 0 (glow mask, #103).

Two cars have no lamp at all in the full entry: FOR_EscortRSCosworth_92
and TOY_6_SC430_08 draw their lamps at Forza LOD 3 only as `lights_glass`
cover glass, with no lamp technique (#103).

The cars still under half (LOD 1 list): rear fog lamps (`fogred`, ASC_KZ1R_12,
LAM_Countach_88), tail-only position lamps (`drlred`, BMW_M3E92_08; stock
uses 0x03 red tails too), `reverse_light` drawn on red texels
(PEU_207Super2000_07, TOY_Aygo_11), and CHE_CorvetteZR1_09, whose four round
lamps are `lindtailside/rindtailside` (DRL + indicator; whether Forza brakes
them is unknown).

### Head lamps dark by day (player report)

"The headlights should be on all the time like vanilla cars." Static
(`out/brakes/stockfront.py`, front light entries of the 150 stock
vehicles): 140 have a front lamp that lights with DaytimeRunning: white
position lamps 0x03 in the head-lamp cluster (99 vehicles, next to 0x01 head
lamps; stock 229 at x ±0.60 beside its ±0.44 head lamps), or head lamps of
type 0x1D, Headlights + DaytimeRunning (33; 31 of them have no 0x01 at
all). Only 10 have neither. The converter gave Forza's head lamps 0x01 and
made position lamps only from position-lamp techniques (`drlwhite`...), so
in the front survey 66 of 573 ported cars had a visible front lamp lit by
DaytimeRunning.

Fix (`LampPlan.assign`): head lamps on a front side without a position lamp
(or DRL indicator) of its own at least a quarter of their area are 0x1D
(CHR_Cuda426_71's 10-triangle position lamps beside 109 head-lamp triangles
don't count). Front survey (LOD 1): 554 of 573 cars show a DaytimeRunning lamp from
the front (512 through 0x1D head lamps); 515 cars change at the front. At the
built LOD 3: 64 -> 549. In game (`vm1-20261027-051849` master,
`vm1-20261027-052118` branch, `-Views fq`; `research/img/headlamps-day.jpg`):
the Carlton's head lamps are dark by day on master and lit on the branch,
like stock 198's 0x1D lamps in the same run. So the game lights DaytimeRunning
on the player's car in daylight. The 19 cars without one (LOD 1):
pop-up or covered head lamps (MAZ_Miata_94, TOY_Sprinter_85,
JAG_XJ220_93, CHE_CorvetteStingray_67, race cars), decal lamps (the
NASCARs), and cars whose own position lamps sit outside the frontal view.

### Lamp lenses drawn opaque (user report on `out/final4`)

Symptom (confirmed-runtime, user's shots on the installed `out/final4`): the
Lancer Evo VI's (MIT_LancerEvoVIGSR_99, ID 559, host 136) headlamps were flat
white frosted plates, the Lotus Carlton's (LOT_Carlton_89, 495, host 229)
white blobs, the Panoz Esperante GTLM's (PAN_51_Esperante_05, 603, host 162)
solid yellow with no lamp inside, and the WRX STI's (SUB_WRXSTi_04, 668, host
274) braking tail lamps a mottled, blotchy red.

**What Forza draws (confirmed-static).** The lamp lenses are the
`detail_glass_clear`, `_red` and `_amber` techniques (`detail_glass_*.fxobj`:
a glass shader with `Glancing`, `Direct`, Fresnel terms and `DarkValues` /
`LightValues`) on the Lights atlas, and their texel alpha is the lens's
opacity: the Evo's covers are a fluted pattern whose alpha follows the flutes
(about 150 on average, a third of the texels under 128), the Esperante's
yellow lexan is alpha 163, the Subaru's red lens flat (136, 2, 2) at 216. They
cover the reflector (`oldhead`, `head_light`, `tail_light`,
`textured_reflector`). An offline render with the lenses alpha-blended over
what lies behind them (`out/lens/frender.py`) shows the reflectors and the
Evo's amber indicator through the covers, as in Forza.

**Cause (confirmed-static).** `forza_materials` classes `detail_glass_*` as
textured Lights-atlas parts, so the converter drew each lens as opaque Lights
texels in front of its lamp: the lens texture alone, a flat plate. Survey of
the roster (`out/lens/survey.py`, LOD 1): 502 of 573 cars have
`detail_glass_*` lenses; on 484 of them rays straight in from the lens meet a
lamp, reflector or housing within 0.4 m (1,413 clear, 903 red and 337 amber
lens subsections), so their lamps were hidden.

The Subaru's blotches had a second cause: the car's red curve
(`red_lamp_curve`). 60% of its red lamp texels are that flat lens, so the
score percentiles 30 to 60 all fell on 0.689 to 0.699 and the knots were
forced only 1e-4 apart; the DXT noise of the flat lens (scores 0.684 to 0.70)
became alpha 8 to 30 in blotches (`out/lens/curve.py`; atlas alpha view of
the built car `out/lens/sub4_alpha_ba.png`), which the brake lamp's red lit
up. 375 cars had such knots under 0.002 apart (most of them only in the
alpha-0 knots at the bottom, which don't matter).

**Fix (`src/dsfforza/convert/lenses.py`, `Texturing._bake_lenses`).** Stock
lamps are opaque Lights texels with the lens pattern painted in, so the lens
keeps its triangles and its texels get what Forza shows through it: before the
atlas is planned, every texel of a `detail_glass_*` lens (finest planned LOD)
casts a ray straight in along the lens normal, composites further lens layers
and stops at the first opaque surface (its Forza texel, or the flat colour of
paint, trim or chrome; a dark housing if nothing is within 0.4 m), and the
texel becomes `a x lens + (1 - a) x behind` with a = the lens alpha (capped by
a `GlassMaxOpacity` in the car's `ShaderSettings.xml`). One ray per 2 x 2
texels or more (about 3,000 a lens); the lens's own pattern stays per texel.
The baked alpha keeps the lens texel's lamp score (`lamp_score`), so the lit
lens glows as before: blending the reflector's glow mask in as well made the
Subaru's lens blotchy again (alpha p10/50/90 7/27/32). Texels other triangles
also sample are left alone (11% of the lens texels over the roster). The
`lights_gls_*` lamp glass is left as it was: its opacity is a material setting
(`GlassMaxOpacity`) and baked with its alpha the Evo's red tail glass came out
mostly white; so are the coloured covers `is_lens` moves to the atlas (the
DMC-12, checked in game). `red_lamp_curve` keeps knots of different alphas at
least 0.01 apart (`RED_CURVE_MIN_GAP`, about the score noise of a flat DXT
texel): the Subaru's lens alpha is now 8/9/13 (p10/50/90), standard deviation
1.9 (was 6.1). Planning a car's atlas takes 2 to 7 s longer (CCXR, 240,000
lens texels: 7 s). Offline (`img/lenses-offline.jpg`: Forza blend, master,
this change) the Evo's flutes lie over the reflector with the amber indicator
inside, the Carlton's and Esperante's lamps show through their covers, the
Subaru's lens shows its reflector faintly.

In game (confirmed-runtime, headless VM 2, `-Views nose,rear -Palette 0
-ShotSeconds 3 -WaitSeconds 600`): the five cars on their `out/final4` hosts,
the `out/final4` chunks remerged as IDs 304 to 308 (`out/lens/before4`), run
`vm2-20261027-070438`, against the same cars built with this change
(`out/lens/after5`), run `vm2-20261027-071018`
(`img/lenses-ingame-before-after.jpg`). The Carlton's and the Evo's headlamps
show their reflectors through the lens instead of a white plate, the Evo its
amber indicator in the corner; the Esperante's yellow lexan shows the lamps
inside it; the WRX STI 04's tail lamps, braking at rest, glow an even red
instead of blotches. Stock hosts weren't in these runs: the glow (lamp score)
of a baked lens is the lens texel's, as before. With the default
`-WaitSeconds 60` the probe ended before its Shift camera came down (run
`vm1-20261027-040419`: "zap level 3" until the end), and with `-ShotSeconds 10`
the shots missed most of the 6 s fixed views.

Rebased on #104 (head lamps lit by day as 0x1D, rear lens types from the lens
texels), run `vm2-20261027-082151` (`out/lens/after6`): the lamp plan reads the
lens colours from Forza's own texels (`forza2dsf.convert` gives
`lights.texel_sampler` a fresh `AtlasCache`, not the baked one), so #104's
decisions are unchanged. The WRX STI 04's tails look as in the run above.
The Carlton's head lamps, now lit by day, are bright white in daylight
(`img/carlton-headlamps-by-day.jpg`). Their lens glow alpha is mean 50,
median 31, inside stock's head-lamp range (mean 71, median 49), and the bake
keeps it as it was: issue #115.

### Pop-up head lamps drawn raised and lowered at once (issue #88)

Symptom (offline-checked): 27 ported cars drew their pop-up or hidden head
lamps in both states at once, the raised pods standing over the closed lids
(RX-7 FC, NSX, Countach, the Corvettes, the Cougar Eliminator's grille doors
open and shut together).

**What Forza has (confirmed-static).** The two states are separate stock
sections of the main carbin: `<base>up` (raised, doors open) beside
`<base>down` (lowered, doors shut): `headlightLup`/`headlightLdown`,
`headlightLUp` on the two NSXs, `glassLHLup`/`glassLHLdown` on the Starion.
On the Stratos and the Elan Sprint the lowered state is the plain
`headlightL`/`headlightR` and only the raised one is named (`headlightRup`;
the Stratos's `headlightLup` has no LOD 1 or 2 geometry). Nothing in
`carattribs.xml` or `ShaderSettings.xml` names them, and the cockpit carbins
hold only the lowered state (`headlightLdown` on the Countach and the M1,
nothing on the others): a parked Forza car shows its lamps down. The names
didn't end in an upgrade letter, so `stock_sections` kept both. The GTO
Judge's `headlightL_Cover` (grille doors over the lamps) is a single state
and stays.

Survey (`out/popup/survey.py`, the 573 cars of `tools/batches/all.txt`):
28 cars have a raised state; 27 drew both at LOD 1, the M1 only at LOD 5
(its raised sections have no nearer LOD). Cars: DOD_ChargerDaytona_69,
TOY_2000GT_69, ACU_NSX_97, BMW_M1_81, CHE_CamaroZ28_69,
CHE_CorvetteGrandSport_96, CHE_CorvetteZ06_02, CHE_CorvetteZR1_70,
DET_Pantera_71, FER_355Berlinetta_94, FER_512TR_91, HON_NSXR_92,
JAG_XJ220_93, LAM_Countach_88, LAN_Stratos_74, LOT_ElanSprint_72,
LOT_Esprit_02, LOT_EspritTurbo_80, MAZ_RX7_90, MAZ_RX7SpiritR_02,
MIT_Starion_88, MRC_CougarEliminator_70, PON_TransAm_02,
PON_TransAmGTA_87, TOY_CelicaSupra_84, TOY_MR2GT_95, TOY_MR2SC_89,
TOY_Supra20GT_92. After the fix: 0.

**What stock DSF does (confirmed-static).** Stock pop-up cars use chassis
variants (`upvh.md`, "Chassis variants"): the Countach (210), Diablo (211)
and Jalpa (213) have two chassis records, role 0x100 on MODL part 0 and 0x101
on part 1. Part 0 is the body with the lamps raised (308 / 164 / 270 more
triangles at the nose, with glass, trim and lamp materials), part 1 the same
body with flat paint lids (24 / 48 / 88 triangles). The Stratos (217) has
four (0x100 to 0x103: lamps up or down, with or without the centre pod). The
other 0x1xx pairs are not lamps: the taxi signs (268, 272, 282), the
Eldorado's, XLR's and Beetle's roofs, the Cutlass's spoiler, identical
copies on the Challenger (172) and Tanner; 0x5xx are convertible roofs.
`out/popup/variant_diff.py` and `chassis_records.py` list them. No stock
car animates its lamps (the 13 UPVH animation blobs are on spoilers and
the tow truck).

In game the variant doesn't follow the lamps (confirmed-runtime, run
`vm1-20261027-162907`, stock archive, `-Views 'nose,nose+lit,nose+night'`;
`img/popup-stock-countach-jalpa.jpg`): the player's Countach and Jalpa show
the raised part 0 (0x100) by day with every lamp off, and the same with
Headlights and DaytimeRunning on, by day and under the dark mood. When the
game draws the lowered 0x101 part (traffic, parked cars, a random pick per
spawn?) is not traced; the Cuda on host 172 drew it as the player's car
(`upvh.md`), so the pick is probably per vehicle or per spawn, not per lamp
state. So DSF has no lamp-driven geometry switch to give the ports.

**Fix (`forza2dsf.raised_lamp`, used by `stock_sections`).** A section
`<base>up` (any case) is dropped when `<base>down` or `<base>` exists, so
the exterior, cockpit, lamp plan and damage all see the lowered state only,
as Forza shows a parked car. The lamp plan's head lamps now sit in the
lowered pods (RX-7: 120 triangles a side, still 0x1D).
Offline (`out/popup/frender.py`, `sheet.py`; built cars with
`car_render`, `out/popup/arender.py`; `img/popup-headlamps-offline.jpg`,
`img/popup-headlamps-survey.jpg`: raised red, lowered blue, plain head
lamps green).

In game (VM runs `vm1-20261027-163143` before, `vm1-20261027-163456` and
`vm2-20261027-164542` after; `out/popup/before`, `after`: RX-7 FC, NSX,
Countach, Cougar Eliminator, Stratos as IDs 1 to 5; `-Views
'nose,grille,nose+night'`; `img/popup-headlamps-ingame-day.jpg`,
`-night.jpg`): before, the RX-7's, NSX's and Countach's pods stand raised
over their closed lids; after, the lids are shut and flush, the Cougar's
grille doors closed. At night with the lamps on, the closed pods show no
head-lamp glow (it is inside them); the bumper lamps still light
(RX-7, NSX), and the Countach's nose stays dark: the price of one state
until a switch exists (issue #122).

Harness: a `-Views` entry with `+lit` (`nose+lit`) switches the car's
Headlights and DaytimeRunning on for that view, `+night` also lays
`lightstest.lua`'s dark mood over the scene; the next view without a suffix
switches both off (`clonetest.lua` `setViewLamps`). Note that the shot
labels in `events.json` come from Driver.log and can lag the screen by
several shots.

### Head lamps white blobs by day (issue #115)

Symptom (confirmed-runtime): the Carlton's head lamps, lit by day as 0x1D
since #104, are flat blown-out white patches (run `vm2-20261027-110011`,
master, `-Views nose -Palette 0`: lamp-box luminance mean 229, standard
deviation 16). The user's earlier screenshot was on `out/final4`, built
before #93 and #104, with the head lamps off by day, and they already read as
white blobs there. So the glow isn't the main cause.

Measure (offline-checked, `out/dayglow/frontlamps.py`): the orthographic
rear view of `rearlamps.py` turned to the front (4 mm pixels, z-buffered over
the lamp triangles of the full entry, area-weighted), per visible pixel the
table type, texel RGB and alpha. Stock, 142 vehicles, white texels
(`lamp_hue`) of the front lamps: brightest channel pooled p5 41, p25 93,
median 137, p75 180, p95 230 (per-vehicle median p10 82, p50 134, p90 178).
Glow alpha of the visible surface: per-vehicle mean p10 41, p50 64, p90 108.
Stock 0x1D head lamps (32 vehicles) carry the same texel alpha as 0x01 ones
(texel-weighted mean 71 and 71, median 49 and 50, `out/lamps/stock_tex.npy`),
so stock doesn't author its day-lit lamps with less glow. Ported, master
(`out/dayglow/master`): the Carlton's visible head lamps sit at brightness
206 with alpha mean 35, median 26; Lancer Evo VI 173 (alpha 33); M3 E30 197
(alpha 69, median 26); 370Z 90.

Cause (confirmed-static): Forza's white lamp texels are much lighter than
stock's. The Carlton's head lamp is a (195, 195, 195) `detail_glass_clear`
lens at opacity 88 to 247 over a `head_light` / `textured_reflector` of 205 to
221 at alpha 255 (`out/dayglow/forzalens.py`), so the baked lens is about 205
everywhere: a light surface in sun before any lamp lights, and with the
0x1D glow on top it blows out. Only red lenses were brought to stock's
brightness (Open 5). Forza's mask carries no hot spot for the Carlton (the
reflector is 255 throughout), so there is no bulb structure to carry over.

Fix (`dsf_materials.white_lens_scale`, `Texturing.visible_white`): as
`red_lens_scale` does for red lamps, the visible white front lamp surface of
each car is measured on the Forza side (the `visible_red` view turned to the
front, triangles of the front half, lenses baked), and every white Lights
texel of the car is scaled so its median is at most stock's 137 (never
brightened; under 400 samples nothing changes). Alpha (glow) is scored on
the original texel, so it doesn't change. On the DSF side the Carlton, Evo
VI and M3 E30 go to 136, 137 and 133; the 370Z (101 on the Forza side) is
unchanged. Rear white lamps take the same factor: stock reverse lamps seen
from behind are median 119 (per vehicle p10 65, p90 177); the ports sit at
148 to 235 on master.

Survey of the roster (`out/dayglow/survey.py`, `tools/batches/all.txt`,
573 cars): 311 are darkened (scale 0.54 to 0.99, median 0.77; lowest
MAS_MC12Corsa_08, MER_SLR_05, MAZ_Furai_08, REN_Clio197_07, 0.54 to 0.58),
245 already sit at or below stock, and 17 have too few visible white texels
(race cars with covered or decal lamps, PAN_51_Esperante_05's yellow
lexan). Per car the visible white median is p10 74, p50 145, p90 204 before.

In game (confirmed-runtime, `-Views nose -Palette 0`; master
`vm2-20261027-110011`, this change `vm1-20261027-110149` and
`vm2-20261027-162933`; `img/headlamps-day-whitescale.jpg`): the Carlton's
lit head lamps show the lens flutes and reflector instead of a flat patch
(lamp-box luminance mean 229 -> 211, standard deviation 16 -> 22; stock 198's
lit 0x1D lamp 211, 37 in the same box size). The Evo VI changes little (x0.80).
Stock 229, the host, has 0x01 head lamps and a small 0x03 position lamp, so
its head lamps stay dark by day.

Tried and dropped: blending the glow mask through a white lens like its
colour (`a x lens + (1 - a) x behind`, run `vm1-20261027-164203`). It lit the
M3 E30's half-clear round lamps (lens alpha 126 over an `oldhead` at 255) the
way stock 241's and 130's round 0x1D lamps light, but the Carlton's uniform
255 reflector made its whole lens one alpha (p25 47, median 51, p75 72) and
it was a flat blob again (luminance 236, standard deviation 10). Stock lit
lamps keep about a quarter of the lamp at low alpha (0x1D visible p25 18 on
average), which is where their structure comes from. The M3 E30's dim lamps
were the open part (issue #124, next section).

### Head lamp glow by day (issue #124)

Symptom (confirmed-static, confirmed-runtime in #115's runs): the ported head
lamps' daytime glow followed the lens, not the lamp. The M3 E30's round lamps
barely glowed where stock 241's and 130's round 0x1D lamps glow white, and over
the roster the visible daytime glow was spread wider than stock's both ways.

Measure (offline-checked, `out/dayglow/` in the `driversf-forza-wt-dayglow`
worktree): `dsffront.py` is `rearlamps.py` turned to the front (DSF side, full
entry, 4 mm, z-buffered over the lamp-byte triangles, per pixel the table type
and texel); `survey.py` is the Forza-side view of #115
(`Texturing._visible_lamp_texels(front=True)`, 4 mm), with each pixel's glow
alpha (the converted texel's, or the new day glow) and per lamp (connected
pixels) the mean alpha in the outer, middle and inner third of the distance
from its edge (`profile`). Stock, 142 vehicles: white front lamp alpha
per-vehicle mean p10/50/90 41/64/108; the 32 vehicles with 0x1D head lamps
pool to p10/25/50/75/90 5/19/55/116/180 and their lamps run dark to hot,
median 32 / 88 / 131 by thirds (inner over outer 4.9). On master the ports'
lamps were nearly flat (median 45 / 55 / 66 by thirds, ratio 1.1).

Cause (confirmed-static): the glow of a baked lens texel was the lens texel's
own lamp score (#93), and a `detail_glass_*` lens's alpha is its opacity. A
half-clear lens glowed little whatever lamp sat behind it (the M3 E30's at 126
maps to about 20); an opaque fluted one glowed along its flutes and edges (the
Carlton's edge third 49, its middle 28). Head lamp surfaces seen directly
(through DSF's clear glass) glowed by their Forza mask, which is 255 almost
everywhere on most cars, so their lamps came out flat and at the top of
stock's range (REN_Clio197_07 visible mean 222).

Fix (`lenses.lens_glow`, `lamp_glow`, `day_glow`; `Texturing._glow`, used by
`render`):

- **Raw glow from the lamp.** Each clear front lens texel's ray (the bake's)
  stops at the first opaque surface; when that is a Lights surface (bulb,
  reflector; atlas or tiling) or chrome (the chrome reflector bowls of the
  Civic Si 99 and the Versa), its glow is that surface's mask (at least 128:
  Forza leaves most reflectors at 0 to 10 and lights the bulb), passed by the
  lens, `(1 - a) + a x lens mask`. Over paint, trim or a housing it is 0. A
  lens with under 10% of its texels over a lamp keeps its score as before.
  Head lamp surfaces (`head_light`, `oldhead`, ...) at the front get their own
  mask (at least 128).
- **The reflector's shape.** The raw glow is scaled by the depth of the lamp
  behind each texel against the lamp's full depth (90th percentile): the
  reflector bowl is deepest behind the bulb (M3 E30 48 mm at the centre, 3 mm
  at the rim; Carlton 95 to 7), so the lamp is hot in the middle whatever its
  mask holds. A bulb in front of the reflector is a dip narrower than 2 cm and
  is filled (morphological closing), so it is as hot as the bowl around it.
  For a lamp surface seen directly the depth is behind its subsection's
  frontmost point.
- **Per lamp onto stock.** `Texturing._glow` looks at the car from straight
  ahead (the `visible_white` view, 5 mm) and, per lamp (one lens or head lamp
  subsection; at least 50 visible samples), fades the outer 40% of its
  edge-to-middle distance in to 0 (stock lamps are dark at the rim; a box
  reflector like the Carlton's stays deep to the lens edge), then ranks each
  texel's raw glow among the lamp's visible samples onto the stock 0x1D
  distribution (`DAY_HEAD_ALPHA`). Every head lamp therefore glows as much as
  stock's, brightest where Forza's light and the reflector's shape are, and a
  bumper lamp no longer pushes the main lamps down. Raw 0 stays 0. Left and
  right lamps often share texels, so positions are kept as |x|.
- `render` takes that glow over the converted alpha, averaged over the source
  texels under each DSF texel like the colour. Coloured lenses, rear lamps and
  everything else keep the converted alpha; texel colours don't change. A
  front lens's texels shared with other triangles (the DB5 Vantage's lens
  shares 95% with its reflector's coarser LODs) get the glow too; their
  colour is still left alone.

Roster (offline, `survey.py` on the 573 cars of `tools/batches/all.txt`,
master -> branch; per-car mean glow alpha of the visible white front lamp surface,
cars with 400 samples or more, against stock's per-vehicle p10 41 and p90 108):

| | p10 / 50 / 90 | under stock p10 | over p90 | by thirds outer / middle / inner |
| --- | --- | ---: | ---: | --- |
| lit lamp surfaces, master (543 cars) | 21 / 52 / 129 | 207 | 89 | 44 / 55 / 66 |
| lit lamp surfaces, branch | 61 / 74 / 77 | 10 | 2 | 32 / 90 / 126 |
| every white Lights surface, master (560) | 23 / 64 / 130 | 159 | 104 | |
| every white Lights surface, branch | 53 / 74 / 107 | 24 | 54 | |

"Lit lamp surfaces" are the triangles of lamp techniques and lenses
(`lights.TECHNIQUE_FUNCTION`), the ones the lamp plan gives a light byte. The
second measure is #115's and the issue's: it also counts unlit Lights-atlas
surfaces (`textured_reflector`, housings), which have no light byte and don't
glow in game; the 54 cars still over p90 there are those (REN_Clio197_07 lit
lamps 75, every white surface 130; MER_SLR_05's white reflector housing). 541
cars get the new glow. Of the 10 lit-surface cars still under p10, the two
Corvette Grand Sport 10s (0, 9) have lenses over a painted housing (their head
lamps come from the reflector fallback, "The converter" above) and the R390
and Agera (24, 25) lenses over trim; the other six sit at 38 to 40.

Built (DSF side, `dsffront.py`, IDs 304 to 306 of `out/dayglow/before` and
`after`), 0x1D visible alpha mean and by thirds outer / middle / inner:

| Car | master | branch |
| --- | --- | --- |
| BMW_M3E30_91 | 68, 60 / 65 / 90 | 79, 23 / 94 / 158 |
| LOT_Carlton_89 | 35, 48 / 28 / 26 | 73, 26 / 84 / 129 |
| MIT_LancerEvoVIGSR_99 | 33, 18 / 40 / 55 | 75, 25 / 90 / 150 |
| stock 0x1D (32 vehicles) | 74 median, 32 / 88 / 131 | |

In game (confirmed-runtime, VM 2, `-Views 'nose,nose+night' -Palette 0
-ShotSeconds 3 -WaitSeconds 600`; master `out/dayglow/before` run
`vm2-20261027-195131` with stock 241 and 130, branch `out/dayglow/after2` runs
`vm2-20261027-200145` and `vm2-20261027-202618`;
`img/headlamps-dayglow-ingame.jpg`, `img/headlamps-dayglow-offline.png`): by day
the M3 E30's round lamps now glow white with a hot middle, as stock 241's and
130's round 0x1D lamps do (master: pale unlit discs); the Evo VI's lamps glow
from the reflector's centre instead of barely; the Carlton's glow gathers in the
middle of the lamp. At night the three light as before or brighter in the
middle, not past stock. A traffic car hit the Evo (first branch run) and the
Carlton (second) during their views; the shots used are before the hits.

### Lamps broke with the wrong end (issue #132)

Symptom (player report on v0.1.1): the Citroen DS3's and Mazda 2's head
lamps broke in rear collisions, with the tail lamps, and not in front ones.

Cause (confirmed-static): a lamp breaks when the part its type-17 record
hangs under is damaged (`0x8E84A0`, "Open" 3), and `assign` handed out the
host's entries in table order (`free.pop(0)`). The table lists the rear
lamps first on many hosts, so the head lamps took rear entries: on host 128
(both cars) entries 5 and 4, the rear DaytimeRunning and right indicator at
z -1.67. `patch_upvh` moves an entry's position and flags, not its record.

Fix (`lights.entry_place`, `LampPlan.assign`): an entry's place is where the
host ships it, front by its z (+z forward), side by its x (centre under
0.15 m). Each lamp, most important first, takes a free entry at its end on
its side (centre entries next, then any side); one with none left at its end
shares a lamp's at its end if it can (same function and side, or its
fallback), else takes one at the other end, so a rear reverse lamp still
outranks a front fog lamp as before. Sharing (twins, fallbacks) stays at
the lamp's end.

Survey (offline, `out/lampside/survey.py` and `summary.py` in the
`driversf-forza-wt-lampside` worktree; the 573 cars of
`tools/batches/all.txt` on their `research/host-picks.tsv` hosts, entry
places from the stock tables; NIS_Leaf_11 has no car data):

| | master | branch |
| --- | ---: | ---: |
| lamps on an entry at the other end | 3,041 on 570 cars | 85 on 52 cars |
| of them head lamps | 964 on 465 cars | 0 |
| same end, other side (L/R) | 1,161 on 537 cars | 25 on 25 cars |
| lamps with an entry / unlit | 5,584 / 54 | 5,585 / 53 |

The 85 left are rear reverse (45) and rear fog lamps (32) and a few
indicators on hosts with fewer rear entries than the car has rear lamps;
they light, and break with the front.

### Tail lamps with the broken-lamp pattern (issue #141)

Symptom (user and player reports on 0.1.0 to master 18ea3c9): tail lamps showed dark
blobs over the red on cars that looked undamaged (Veyron, X5 M, 300C, G37 one lamp);
on the Cee'd only the third brake lamp lit when braking.

Two causes, both confirmed in game (VM runs `vm1-20261027-231606` and `-233632` on
master, `vm1-20261027-231924` on the #132 branch, `vm1-20261027-233940` on the branch
with the lens fix; cars 304 to 308 = Veyron, X5 M, 300C, G37, Cee'd; a debug module
logged `FUN_008A78E0`'s lamp objects, below):

1. **Tail lamps on front light entries (#132's mapping).** A lamp object's broken bit
   (flags bit 0, set only by `0x8E65B0` from the break walk `0x8E84A0`) is the colour's
   w: the Lights shader then multiplies the lamp's diffuse and glow by the damage
   decal's alpha at UV1 (the three-blob tile; GPCH 130's pixel shader:
   `cmp r3.xyz, 0.005 - w, texel, texel * decal.a`), and `0x8E6760` keeps a broken lamp
   dark. On master 659 tail and brake lamps of 317 cars hung under front records, so a
   nose-first bump broke them: the Veyron's lamps 12 and 13 after a `+hitfront`
   (flags 0x29F, brightness 0) while its rear lamps on rear entries stayed lit; the
   Cee'd's tails sat on entries 13 and 14 (front), which is why only its third brake lamp
   lit. Nothing breaks at spawn: with no hit, `0x8E84A0` is never called and every lamp
   object's bit 0 is clear (run 231606, call 1 of each car). With the #132 assignment
   (`LampPlan.assign`) 2 tail/brake lamps on 2 cars are left on front entries
   (CHE_CorvetteGrandSport_96, LEX_SC430_02); in game the Veyron's tails stay lit after
   the front hit (lamps 1, 3, 4, 6 flags 0x9E, brightness 1) and break with the rear
   one, and its head lamps no longer break with the rear (master: lamps 5, 6 bit 0 set
   after `+hitrear`; branch: 14, 15 clear).
2. **A lens stretched over a tint swatch (X5 M).** Its red tail glass (`glassLTL`,
   `glassRTL`, `trunk` `detail_glass_red`) samples a 17 x 14 texel swatch of flat red
   (202 texels over 756 cm2, 19 mm a texel on the car). `lenses.bake` casts one ray per
   block of texels, so each swatch texel got one random part of the lamp behind: a
   pink-and-black mosaic, the dark blobs seen at rest (offline: `car_render` of the
   built car shows it without any lamp state). Fix (`lenses._scrambled`): a lens whose
   texels are coarse (over 6 mm) keeps its own texels where lens points over 3 cm apart
   (|x|) share one, all of them when that is most of the lens. Roster (lens subsections
   of the 573 cars at their finest LOD): 14 cars change, 2,378 texels (X5 M whole lenses;
   Charger SRT8 12, Viper CC, Ultima GTR, MP4-12C, Scirocco and 8 more in part); the
   X5 M's tails are Forza's flat red in game (`img/taillamps-x5m-swatch-ingame.jpg`).

The Veyron's speckled round lamps by day (master and branch alike, also offline) are
its red ring texture through the red alpha curve, not broken lamps; the 300C's and
G37's tails look right at rest on both builds.

### Open

1. Front lamps: neither copy faces the camera (`setYRotation(h + pi)` and
   negating the matrix rows both left it showing its rear; the matrix
   write may set only the position), so headlamps and front position lamps
   of converted cars are unchecked in game. Traffic never sets Headlights
   or DaytimeRunning in daylight (`draw-distance.md`), so in normal play
   they stay off for stock and converted traffic alike.
2. b0 and the pi, pi pair; whether the flare sprite uses the direction.
3. Lamps under detachable parts: the light records hang under the host's
   bumper and boot records (a lamp breaks when its record's part is damaged,
   `0x8E84A0`). With `--damage` (the port_batch default) the lamp triangles
   sit on their panel's record like the rest of the body (`damage.py`
   `section_rule`: tail lights vote between boot, rear bumper and chassis);
   without it they are chassis-bound, so the geometry stays while the lamp
   breaks. Whether a converted lamp's glow and its panel break together is
   unchecked in game.
4. Converted cars in traffic (above). `swapWithModel` on a live traffic car
   crashed run 123840 (the nearest traffic car ahead, to the player's own
   model 227); register.lua's experimental `swapTraffic` and `carrier` paths
   were removed for that reason (they were off by default).
5. Converted amber lenses keep Forza's brightness; red lenses are darkened
   (`red_lens_scale`) and white ones too since issue #115
   (`white_lens_scale`). The S5S Raptor's tails (mean 91) sit above stock's
   per-vehicle p90 (81).
6. In a run, cars after the fifth sometimes don't spool ("NOT spooled after
   40 ticks", run host-20261024-003943); keep lamp runs to five IDs.
7. Lens bake ("Lamp lenses drawn opaque"): the view is straight in along the
   lens normal, so the lamp inside doesn't shift with the camera (stock lamps
   are flat textures too). `lights_gls_*` lamp glass and the lamp-cover
   techniques (`glass`, `lights_glass`) aren't baked: covers stay DSF's clear
   glass (the WRX STI 05's near-opaque light rear cover, (184, 161, 160) at
   alpha 236, shows its dark reflector instead), and what Forza's
   `DarkValues` / `LightValues` do to a lens is not decoded. Issue #89.
8. Head lamp glow by day (issue #124, "Head lamp glow by day"): only clear
   front lenses and head lamp surfaces get it. Clear rear lenses over reverse
   lamps still glow by their opacity, and so do front lenses with no lamp
   behind them within 0.4 m.

## Paint

### The palette (confirmed-static)

`NONR/SPSR` in each full entry: `SPID` (u32 spool ID), then three `SPSS`
sets. The first is the palette: 1 to 14 `SPSP` (fleet: 25 vehicles with 8,
22 with 1, 17 with 6, ... 3 with 14), one per paint choice. Each SPSP holds
nine `MARP` (seven on two vehicles):

| Parameter | Meaning (from the 948 stock entries) |
| --- | --- |
| `g_vec4TintColour1_Alpha` | (x, 1, z, 1) on metallic paints, (0, 0, 0, 1) on solid ones (91 entries, e.g. the Desanne's dark blue, Super Car whites); a few vans and trucks hold a colour here |
| `g_vec3TintColour2` | base colour, linear RGB; whites reach 0.9 to 1.0 (the Desanne's white is 0.54) |
| `g_vec3TintColour3` | second colour: with `g_fresnelpower` below 1 a dark edge tint (about 0.02 to 0.06, or the base x 0.1), with a power of 5 to 15 the main colour and colour 2 the darker facing tint |
| `g_fresnelpower`, `g_fresnelbias` | the blend between them |
| `g_hCustomFresnelPower`, `g_hCustomReflectionAmount`, `g_hSecondSpecularIntensity`, `g_hSecondSpecularPower` | reflection and clear-coat |

The second and third sets hold `g_shadowDepthOffset` and
`g_vec4WheelBlurColour`. The values override the shared paint material
`GLOBAL_Chassis` (`001283358afd1027`, SSIC), whose own defaults are a red
(colour 2 (0.8, 0, 0)). The game picks one entry per spawned car;
`garageSpawnVehicle(location, modelId, shader)` passes the index. In the
harness runs each car came out the same colour in every run, so the choice
for script-spawned cars looks deterministic.

Paint UV0 does **not** sample the vehicle's chassis texture: both paint
materials (`GLOBAL_Chassis`, `GLOBAL_Chassis_LOD1`) have `g_texDiffuseMap` =
`00122b73d1fa1227`, a shared 1024 x 512 DXT1 atlas in SSIC (parts, plates
and colour-coded mask blocks). On every stock car checked (266, 227, 229,
147) 57 to 91% of the paint triangles' UV0 lands in its (240, 192, 240)
block (50 x 50 texels centred at (0.848, 0.712); `global_paint_uv.py`), the
rest on other mask colours (panel details). UV1 is the damage-decal UV
(`damage.md`, "Scratch and dent decal: UV1"); converted cars take the
host's.

### Forza colours (confirmed-static)

`gamedb.slt` table `Combo_Colors` lists each car's factory colours:
`Ordinal` = `Data_Car.Id`, `Sequence`, `Metallic`, `RGB` (sRGB, 0xRRGGBB),
`ColorClassId` (`List_ColorClass`: Black, BlueLt, Blue, ...). The 370Z has 7
(Monterey Blue metallic, Chicane Yellow, red, white, silver, grey, black), the
M5 12, the FM4 Skyline 6. `Combo_Colors_Exceptions` has no factory two-tones
(`HasColor2` is 0 in all 652 FH1 rows); its stripes are livery options.

There is no finish beyond `Metallic` (0 or 1 in every FH1 and FM4 row;
`List_ColorFinish` names a CarbonFiber finish no row uses), and no car lacks
colours. Some cars list each colour once per livery texture (`BaseTexture`
nodamage / nodamageBlackStripe...): the FH1 Challenger SRT8 392's 26 rows
are 7 colours, the 500 Abarth's 13 are 5, the SVT Raptor's 10 are 5. Single
colours are common (30 FH1 cars, 144 FM4, mostly race cars and special
editions). Per-colour shader tweaks live in the car zip's
`ColorShaderSettings<N>.xml` (`body` `PaintColor` (= the Combo_Colors RGB),
`PaintScale`, `SpecularAmount2/3`, `MetalColor/Scale`, `FresnelIndex`...;
12 of the 40 testdrive cars have some, e.g. the Focus RS500's satin grey:
`PaintScale` 0.3, `MetalScale` 1, `SpecularAmount3` 40); not used yet.

The FM4 DMC-12 has one colour, solid `#999999` (Gray). Its body is plain
`body` paint (3,782 LOD1 triangles, all on alpha-0 white texels of
`nodamage.xds`), with no shader settings in the zip: Forza draws the
stainless body as flat grey paint.

### The converter (`src/dsfforza/convert/paint.py`, offline-checked)

For each Forza colour: pick the stock palette entry of the same finish
(metallic: `TintColour1.y` = 1) whose apparent colour (colour 2, or colour 3
when the fresnel power is 1 or more and colour 3 is brighter) is closest in
sRGB, luminance weighted; copy its nine parameters; set the main colour to
the Forza colour in linear RGB and the other to the Forza colour times the
stock entry's luminance ratio (near-black colours keep the stock sheen).
`install` replaces the host's SPSPs (the host's first SPSP is the chunk
template; every MARP gets an explicit payload) and drops colours from the end
if the full entry's NONR would pass the SSLP buffer (0x6D000) less 0x400 for
handling edits.

Since the DMC-12 report (below), `forza_colours` drops repeated
(RGB, metallic) rows, `pick_colours` keeps the 14 most distinct colours (in
Forza's order) when a car has more, instead of the first 14 (the FM4 Charger
Daytona's 20 lost its white, grey and black), templates exclude the 40
stock entries whose colour 1 is an RGB colour (`is_template`), and
`SPECIAL_FINISHES` maps a car to a `FINISH_PRESETS` entry used as is
(`DEL_DMC12_82` -> `stainless`, the stock DeLorean's (spool 171) single
entry).

### What the paint shader does with the palette (confirmed-static)

`GLOBAL_Chassis` is SSIC GPCH program 27 (26 to 29 are the variants with
`g_hCustomFresnelPower`; disassembly: `python out/damage/gpdis.py
out/stock/Install/dngvehicles.sp 27 p`, saved in
`out/scratch/paint/gp27_ps.txt`). In
`Vehicles_Functions.hlsl` / `PixelShader.hlsl`:

```text
tex      = g_samDiffuseMap(UV0)            // the shared atlas paint block (240, 192, 240)
fresnel  = g_fresnelbias + g_OneMinusFresnelbias * pow(1 - sat(dot(v1, v2)), g_fresnelpower)  // two interpolated vectors, presumably normal and eye
paint    = TintColour2 + fresnel * (TintColour3 - TintColour2)
tinted   = tex.r * fColourShift * lerp(1, paint, tex.b)          // CustomBRDFTinting_PS
base     = lerp(decal metal (decal.g), tinted, min(decal.b, decal.a))   // decal at UV1, undamaged
spec2    = g_hSecondSpecularIntensity * pow(r.l, g_hSecondSpecularPower) * tex.b * base
refl     = cubemap * g_hCustomReflectionAmount * max(g_fCubemapFresnelMin,
           pow(.., g_hCustomFresnelPower) + g_hReflectionFresnelBias)
out      = lerp(lit + refl, g_vec4FinalTintColour.rgb, g_vec4FinalTintColour.a)
```

`g_vec4TintColour1_Alpha` is an effect parameter but no vertex or pixel
shader of the 87 programs has it in its constant table (only
`g_vec4FinalTintColour`, a global): colour 1 doesn't reach the paint
shader. The stock entries fall into two kinds: flat ones (fresnel power 5
to 15, colour 2 = colour 3, e.g. a flat silver of 0.27 to 0.35 on 67 of the
150 stock vehicles), which show one colour at every angle, and low-power ones (0.05 to 1.7)
with a dark colour 3, which mix toward colour 3 by the fresnel term. Taken
literally (vectors as named, power as stored), a low-power entry would show
mostly colour 3, which doesn't fit white stock cars with colour 2 0.95 and
colour 3 0.02; the CPU may transform the power (it already derives
`g_OneMinusFresnelbias` and `TintColour3MinusTintColour2`), so how bright
the two kinds look against each other is open (hypothesis).

### The DMC-12 came out white (user report on `out/testdrive3`, ID 27)

What was built (confirmed-static): Forza's solid `#999999` (linear 0.319)
matched the stock Courier van's (spool 170) flat grey entry, giving
colour 2 = colour 3 = 0.319, fresnel power 10, reflection 0.3, second
specular 0.05 / power 1: one light grey at every angle, a plain light-silver
paint. DSF's own DeLorean (171) uses colour 2 0.267, colour 3 0.004, fresnel
power 0.75, reflection 0.1, second specular 0.3 / power 5. Why the flat
light grey reads as pure white rather than silver is the in-game question
(hypothesis: the flat entry has no darker term at all, the steel one mixes
toward near black). The decal tint mask at the DMC's paint UV1 is normal (mean 0.87,
like other ports 0.88 to 0.93 and stock 0.93 to 0.97;
`out/scratch/paint/tintmask.py`), so the paint was not lost to bare metal.

Fix: `SPECIAL_FINISHES` / `FINISH_PRESETS` (stainless = the stock 171 entry).

The `out/paintfix/` runs planned here never ran; the preset was tested on
`out/testdrive4` instead (next section).

### The DMC-12 came out black (user report on `out/testdrive4`, ID 27)

Symptom (confirmed-runtime): with the stainless preset (the stock DeLorean's
entry) the port drew near black in the chase view (`host-20261023-082346`,
`observe-06/07`), while stock 171 in the same runs is light silver
(`host-20261023-083742`, `-090511`, `-092427`, `observe-02`).

Ruled out (offline and A/B in game, all on the DMC alone, ID 1; sheets
`out/dmc/run2*.png`, `run3*.png`, `stainless-compare.png`):

- the palette: identical to stock 171 (`out/dmc/spsr.py`), and copying 171's
  entry byte for byte (`vF`, `host-20261023-093016`) changes nothing;
- vertex normals (decoded like the vertex shader, against face normals and
  outward: `out/dmc/normals.py`, `outward.py`), damaged-normal deltas, the
  baked sky term, MODL flags and submodel headers: like stock;
- paint UV0 on stock 171's own texel (0.909, 0.708) instead of the paint
  block (`vC`, `-090833`) and UV1 on its hidden-face corner (0.99, 0.01)
  (`vB`, `-090652`): still black;
- the exterior entry's materials: stock exteriors use the `_LOD1` shared
  materials (paint and trim in 149 of 150, glass 145, tyres 150;
  `out/dmc/extmats.py`), converted ones used the full-entry ones; switching
  to `_LOD1` (`vE`, `-092015`) changes nothing visible (kept, below).

A flat palette (colour 3 = colour 2 = 0.267, power 10; `vD`,
`-085437`, `observe-12`) gives a light steel body, so the fresnel term was it.
A probe with colour 2 red and colour 3 blue on both cars (`vG`,
`-092835`, `out/dmc/run7G-compare.png`) shows the mix applied literally on
both: stock 171's rear panel, facing the camera, is pink (red and blue), its
edges blue; the port's visible paint is blue at the edges. The paint shader
treats both cars alike; what differs is what the chase camera sees. Weighted
by projected area from behind and 15 degrees above (`out/dmc/facing.py`),
stock 171 shows 2.3 m² of paint at a mean n.v of 0.54 (its rear panel,
lamp surround and bumper are paint, "DeLorean" in black letters), the port
0.86 m² at 0.25: Forza's DMC has a black rear surround (`body` `black`),
a grey plastic bumper (`bumperRa` `plastic2`), like the real car, and its
stainless sides are seen almost edge-on. With power 0.75 and colour 3 0.004
those grazing panels go to colour 3, near black.

This also settles the open question above about how literally the power is
used: `pow(1 - n.v, g_fresnelpower)` as stored. A low-power entry with a dark
colour 3 darkens a lot at moderate angles (stock 171's rear panel already
mixes about halfway); the M3 E30's red #ca0000 entry (power 0.05, colour 3
0.017) reads maroon in game (`host-20261023-092609`, `observe-03`).

Fix (`paint.FINISH_PRESETS["stainless"]`): stock 171's entry with colour 3
0.2 instead of 0.004 (brushed steel stays light at grazing angles). In game
colour 3 0.13 (`-094028`) still left the flanks dark grey, 0.2 (`-094153`)
gives light steel sides like stock. Final build `out/dmc/final`
(`host-20261023-094929`, `observe-06`): a light brushed-steel body with the
black rear surround, next to stock 171's `observe-02` in the same run.
The black rear surround and grey bumper are Forza's (and the real car's);
the stock DSF model paints them.

### The S5S Raptor glowed (user report on `out/testdrive9`, ID 22)

Symptom (confirmed-runtime): the yellow body bloomed into an orange halo over
the roof, the windows and the silhouette in daylight (user screenshots; also
host run `host-20261024-013435`, `observe-06`).

Cause (confirmed-static): `shade` set the main colour to the Forza swatch in
linear RGB. The Saleen's only colour, #ffbf19 metallic, is (1.0, 0.52, 0.01);
its template, the security van's yellow (301: (0.567, 0.302, 0.032), colour 3
at 0.44 of colour 2, fresnel power 1), keeps colour 3 bright too. Luminance
of the fresnel mix taken literally (`paint.brightness`: at n.v 0.9, 0.7, 0.5
and area-weighted): 0.55, 0.49, 0.42, 0.48, where no stock car paint
(the 906 `is_template` entries) passes 0.388, 0.388, 0.388, 0.391 (the flat
greys with bright rims, 136 and 167). No other testdrive car passes the stock
maximum (`out/emissive/paintsurvey.py`; next brightest the 500 Abarth's cream
0.384 and the M5 E28's light blue 0.36).

Fix (`paint.shade`, `brightness_cap`): both colours of a recoloured entry are
scaled by the largest factor that keeps every measure at or below the stock
maximum (computed from the stock palettes, not a constant). Only the Saleen
changes: x0.705, colour 2 (0.705, 0.367, 0.007), a #dba314 yellow
(`install` reports `dimmed`).
In game (run `vm-20261024-060602`, `out/emissive/build2` ID 1;
`research/img/emissive-saleen-chase.jpg` against `host-20261024-013435`) the
body is a plain yellow with no halo.

### Survey of the 40 testdrive cars (`out/testdrive3`, offline)

| Problem | Cars | Now |
| --- | --- | --- |
| Raw-metal finish painted as flat light grey | DMC-12 (27) | stainless preset |
| Repeated rows filled the palette | Challenger SRT8 392 (14 entries for 5 colours: 4 white, 3 orange, 4 red, 3 blue; no black or greys), 500 Abarth (13 for 5), SVT Raptor (10 for 5) | 7, 5, 5 distinct |
| More than 14 distinct colours | Charger Daytona (20: lost gold, white, off-white, grey, black) | 14 most distinct |
| Template from a van, truck, buggy or two-tone car (colour 1 an RGB colour) | 21 of 40 cars had at least one such entry (Challenger 7, 500 Abarth 3, Cuda 2, Mito 2, 17 cars 1), the DMC-12 and the Veyron SS's only colour among them | car paint templates only |
| One factory colour | Firebird Trans Am (black), PEU 205 T16 (grey), S5S Raptor (yellow), Veyron SS (orange), Focus RS500 (dark grey), DMC-12 | unchanged: that is Forza's list |

No car has missing Combo_Colors, and none is white-only in the 40.

`uv run dsff paint colours out/cache/fh1/gamedb.slt NIS_370Z_10` shows
the result; `uv run dsff paint palette <archive> --id 304` reads a built
palette back.

### Black ports (runs 20261022-061717 to -113549; cause confirmed-static)

Every port built after the textured atlas came in was black or mid grey in
game, whatever its palette: the A/B series `out/lights/ab/` (Forza or host
palette x atlas paint cell as built or Desanne grey; runs 20261022-111857,
-112431, -113009, -113549) gave identical colours, so neither the SPSR nor
the vehicle texture decides it. The converter pointed paint UV0 at the
atlas's plain cell (it assumed paint overlays the vehicle texture), which in
the shared paint texture is black (0, 0, 0) for the 370Z's cell at (0.586,
0.789) and dark olive (48, 48, 0) for the M5 and Skyline. The earliest
Desanne-hosted ports were coloured because they used the constant
(0.835, 0.703), which lies in the paint block. Fix: paint UV0 is always
`dsf_materials.PAINT_TEXEL_UV` (0.8481, 0.7119); the rebuilt archives in
`out/lights/ab2/` sample (240, 192, 240) on 100% of paint triangles.

### Trim and two-tone

Forza draws trim (`black`, `matte_colors`, `frame`, `bottom`...) with its own
techniques, which go to DSF's shared trim material, so trim is never painted.
Inside the paint techniques (`body`, `body_2`) almost every triangle samples
alpha-0 texels: 9,391 of 9,431 on the 370Z (40 dark opaque ones: painted
now), all on the M5 and the Skyline. `body_2` is paint too (the 370Z's and
M5's door shuts and wing vents, the Skyline's wings), not a second colour.

### Open

1. In game: the factory colours after the paint-UV fix (`out/lights/ab2/`).
2. Which entry a spawn picks (deterministic per car in the runs so far) and
   whether the garage lists all entries.
3. Answered: paint UV1 is the damage-decal UV (`damage.md`).
4. Wheel-blur colour (third set) from the Forza rim colour.
5. A brushed grain: the paint shader has no detail or normal map of its own
   (UV0 samples one flat block of the shared atlas; UV1 the shared damage
   decal), so a brushed-steel pattern would need a non-paint material.
6. `ColorShaderSettings<N>.xml` (paint scale, metal flake, specular) could
   choose the template look (satin, pearl) instead of RGB distance alone.
