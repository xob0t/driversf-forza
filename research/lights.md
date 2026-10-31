# Lamps and paint

Updated: 2026-10-30

How vanilla DSF cars light their lamps and pick a paint colour, and what the
converter does about both. Code: `src/dsfforza/convert/lights.py` (the lamp
plan and light table), `src/dsfforza/convert/lenses.py` (lens and lamp glass
bake, day glow), the lamp parts of `dsf_materials.py` (`LIGHTS_ALPHA`, the
lamp curves and scales, `Texturing` lens, cover and tiling rules),
`src/dsfforza/convert/paint.py` (palettes) and
`dsf_materials.PAINT_TEXEL_UV`; lamps and palettes run inside
`forza2dsf.convert` (`ConvertOptions.lights`, `ConvertOptions.paint`, on by
default). Evidence levels as defined in `dngvehicles.md`. The survey scripts
named below (`out/lights/`, `out/lamps/`, `out/l6/`...) are scratch scripts
in the worktrees of the PRs that made them, not in the repo.

## Current lamp rules (summary)

What the converter does as of the seventh lamp pass (after PR #223). The sections after this one are in
the order the lamp PRs landed (#93 to #223) and hold the evidence; where a
later PR replaced a rule, the earlier section says so in one line.

**Planning: which Forza triangles light, and as what** (`LampPlan.build`).

- A triangle whose technique is in `lights.TECHNIQUE_FUNCTION` is a lamp:
  head, position, tail/stop (0x18), stop, reverse, indicator, fog,
  brake + indicator (`brinl`/`brinr`, and `lindtailside`/`rindtailside`
  since #220) or DRL + indicator ([converter](#the-converter-srcdsfforzaconvertlightspy-offline-checked),
  [lindtailside](#tail-lamps-that-brake-and-indicate-lindtailside-issue-103)).
- Lenses (`detail_glass_*`, `linred`/`rinred`) and the red covers
  `Texturing.is_lens` moves to the atlas take their function from their
  place and texels. At the rear a triangle on red texels is a tail lamp, and
  one over an indicator bulb brakes and indicates (0x19/0x1A); amber lenses
  are indicators, but a front selective-yellow one with a head lamp behind
  it is a head lamp ([Carlton](#the-lotus-carlton-showed-no-brake-light-user-report-on-outfinal4-spool-495),
  [covers](#lamp-covers-drawn-as-window-glass-issue-160)).
- Clear lenses, baked covers and rear reflectors light with the lamp right
  behind them (for a reflector, inside its outline), else with a lamp of
  their own Forza section, else (lenses and covers) with a lamp whose
  outline overlaps theirs, else stay unlit. Front reflectors never light
  ([#174](#clear-covers-over-reflectors-with-no-lamp-issue-174)).
- Last, what each rear triangle is drawn with (the mean of the baked
  atlas's texels under it, `lights.seen_sampler`): an indicator lamp drawn
  90% red brakes and indicates (0x19/0x1A), and any other triangle drawn red
  on a reverse or amber indicator lamp moves to the nearest red lamp behind
  or inside its piece, of its section, or within 0.35 m, else stays unlit.
  Lenses keep the lamp they lie over
  ([red drawn texels](#red-texels-lit-as-reverse-and-indicator-lamps-issue-103)).
- Head lamps without a head lamp technique: the front clear lenses, else
  the headlight sections' reflectors, else their cover glass. With one, the
  reflector bowls under a headlight cover that has no head lamp in it light
  as head lamps too ([#158](#head-lamp-units-with-no-lamp-technique-issue-158)).
  Pop-up lamps are planned in their lowered state, the only one drawn
  ([#88](#pop-up-head-lamps-drawn-raised-and-lowered-at-once-issue-88)).
- Side from the technique or x (a subsection within 0.3 m of the centre
  line is a centre lamp); each (function, side, end) group splits into lamps
  0.2 m apart; a centre tail lamp more than 8 cm above the side tails is the
  high stop lamp (0x02). A head lamp technique behind the wheelbase
  midpoint is placed like a clear lens, not as a head lamp, and a lamp the
  red-drawn move empties takes no entry
  ([#264](#head-lamp-techniques-at-the-rear-issue-264)).

**Light entries** (`LampPlan.assign`, `patch_upvh`).

- The host's table keeps its size, ids and records. Lamps take entries in
  priority order (head, tail, stop, indicator, reverse, brake + indicator,
  position, fog; second clusters after every first one), each an entry the
  host ships at the lamp's own end, on its side if one is free (else a
  centre one, else any) ([#132](#lamps-broke-with-the-wrong-end-issue-132)).
- A lamp with no entry left at its end shares one there: same function and
  side, else the other side's for types with no side, else its fallback's
  (brake + indicator: the tail lamp; position: head or tail lamp). A rear
  lamp with none to share has a left and right pair of one type with no side
  (brake lamps, then tails) share one entry and takes the other. Only then
  does it take a free entry at the other end; fog lamps never do and stay
  unlit ([#146](#rear-lamps-short-of-a-rear-entry-issue-146),
  [pairs](#a-rear-entry-freed-by-a-left-and-right-pair-issue-146)).
- Head lamps on a front side whose own position or DRL-indicator lamps
  (with an entry) are under a quarter of their area are 0x1D, Headlights +
  DaytimeRunning, so they light by day as on 33 vanilla vehicles
  ([day](#head-lamps-dark-by-day-player-report)).
- Used entries get the lamp's position, direction and flags; spare ones
  flags 0, parked on the nearest lamp. Every lamp triangle of the full and
  exterior entries gets id + 1 in byte 2 of vertex semantic 9.

**Glow and colour** (Lights texels; `dsf_materials`, `lenses`).

- Texel alpha is the glow. "matched" (`LIGHTS_ALPHA`) ranks
  `a x (0.6 + 0.4 x brightest channel)` onto vanilla's lamp alpha per hue,
  red on a curve fitted per car
  ([matched](#vanilla-lamp-alpha-and-the-matched-rule-offline-checked)).
- After the lamp plan, each rear red lamp it makes (tail 0x18, brake 0x02,
  brake and indicator 0x19/0x1A) glows on its own: every texel of its triangles
  (lenses included, any colour) in proportion to Forza's glow mask (the matched
  score; on FM4 cars the texel's linear red, their reflectors 0), at the mean
  alpha #391's ranking gave a lamp over what a camera straight behind sees of it
  ([#290](#tail-lamps-at-half-vanilla-at-night-issue-290),
  [rear lamps ranked per planned lamp](#rear-lamps-ranked-per-planned-lamp-issue-391),
  [in proportion to Forza's mask](#rear-lamps-glow-in-proportion-to-forzas-mask-issues-403-404)).
- Red lamp texels are darkened (shared curve, then per car) until the
  visible red median is at most vanilla's 92; white ones until the visible
  white front median is at most 137; amber ones keep Forza's brightness
  ([TT RS](#red-lenses-too-bright-when-unlit-user-report-on-outtestdrive9-audi-tt-rs),
  [#115](#head-lamps-white-blobs-by-day-issue-115)).
- Clear front lenses and head lamp surfaces glow from the lamp behind them,
  hot where the reflector is deep, ranked per lamp onto vanilla's 0x1D
  distribution ([#124](#head-lamp-glow-by-day-issue-124)), following the
  lamp's own picture (its bright and dark detail,
  [#286](#head-lamps-at-a-fifth-of-vanilla-at-night-issue-286)), times what
  the glass or lens in front of it passes. Day-lit (0x1D) lamps keep that alpha,
  unscaled. History: until [glass over lamps as Forza blends it](#glass-over-lamps-as-forza-blends-it-379)
  (#389) a `DAYTIME_GLOW` factor scaled them (0.2 from #167 to #286, then 0.45);
  it and the second render of the atlas it needed are gone.

**Glass over lamps** (`lenses.bake`, `Texturing._bake_lenses`). What Forza's race shaders actually compute for lenses, covers and
glass, and which of these rules that confirms or replaces: `forza-lenses-re.md`.

- A `detail_glass_*` lens keeps its triangles; each texel becomes what a ray
  straight in meets (lamp, reflector, trim, paint) times the lens's filter, the
  lens texel plus the car's GlassColor (sRGB to linear), the red remap on red
  lenses (`lenses.lens_filter`), and glows with the lamp behind times the filter;
  its own colour and alpha don't count ([lenses as Forza draws them](#lenses-as-forza-draws-them-issues-300-340)).
  Rays that meet no lamp along the normal are cast again along the car's axis, for
  lenses that face out of the car's end
  ([#93](#lamp-lenses-drawn-opaque-user-report-on-outfinal4),
  [#190](#flat-lenses-lens-skins-laid-back-head-lamp-lenses-and-tiling-bowls-player-reports-on-3ca28fd),
  [#218](#tiles-across-edges-streak-parts-flat-front-lenses-rear-axis-rays-issue-189)).
- A tint swatch stretched over a lens (its texels scrambled: each stands for lens
  points far apart) is left out when half its rays meet a lamp (a clear one when
  any ray does, #250), and its filter goes onto the lamp texels behind it. A lens
  of one flat colour is baked like any other since "Lenses as Forza draws them".
- Plain `glass` over a lamp stays DSF window glass, as Forza draws it over
  the lamp ([#379](#glass-over-lamps-as-forza-blends-it-379); left out from #160
  to then). Textured covers (`lights_glass`) over a lamp are baked at Forza's
  opacity and pass (1 - opacity)^2 of the lamp's glow; `lights_gls_*` lamp glass with a
  cap under 1 is baked at that cap (`lights_gls_noemit` too, lamp glass
  without a glow, not a cover); a cover over its own lenses' texels (a
  second skin) is left out; covers over no lamp stay DSF window glass (the
  red rear ones `is_lens` takes, opaque lamp texels)
  ([#160](#lamp-covers-drawn-as-window-glass-issue-160), #164, #190).
- Tiling lamp parts are laid triangle by triangle into one tile when at
  most 25% of their UV area falls outside it, or when they are a coarse
  LOD's copy of a part whose finer LODs lie in one tile; streak parts onto
  one texel column; the rest as one colour (#218,
  [coarse LODs](#coarse-lod-lamp-parts-drifting-over-a-tile-edge-issue-229)). Lamp parts keep `plan()`'s material
  in the sections later passes rebuild, so the exterior entry draws them as
  planned ([#175](#no-front-lamp-on-the-players-car-lamp-parts-drawn-as-trim-in-the-exterior-entry-issue-158)).

**Breaking.** A lamp breaks when the part its type-17 record hangs under is
damaged (`0x8E84A0`); the broken bit darkens it and lays the damage decal's
broken-lamp blobs over it ([How a lamp lights](#how-a-lamp-lights-confirmed-static-driverexe-104),
[#141](#tail-lamps-with-the-broken-lamp-pattern-issue-141)). Records stay
where the host has them, so a lamp breaks with the end its entry is at; 6
lamps on 4 cars still sit on an entry of their own at the other end (#146). With `--damage` the lamp geometry rides on its panel's record
(`damage.section_rule`, `damage.md`).

**Open** ([list](#open)): #89 (lamp glass colour and reflection), #103
(cars under half braking, the Escort and SC430 with no lamp at LOD 3), #122
(pop-ups can't rise), #146 (lamps on the other end's entries), #166 (red
lamps darkened below a light body colour), #229 (lamp texture leftovers of
#145 and #189).

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
| 0x20 | f32, f32 | pi, pi in all 2,179 vanilla entries (cone angles?) |
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

  | b3 | Mask | Vanilla use (lamps in 150 vehicles) |
  | ---: | --- | --- |
  | 0x01 | Headlights | front white headlamp (218) |
  | 0x02 | Brakes | rear red, mostly the centre high-mounted stop lamp (158) |
  | 0x03 | DaytimeRunning | front white position lamp (194; 8 amber), rear red tail lamp (93) |
  | 0x04 / 0x05 | Left / RightIndicator | amber indicators, front and rear (292 / 279) |
  | 0x06 / 0x07 | Brakes + Left/Right | US-style red rear indicators (14 / 14) |
  | 0x08 | Reverse | rear white (257) |
  | 0x09, 0x0D..0x0F | Siren | emergency lights, b0 = flash phase (54; 25 / 15 / 26) |
  | 0x0B | Siren + Headlights | wig-wag headlamps (6) |
  | 0x0C | FogLights | (23) |
  | 0x10, 0x11 | 2048, 4096 (no Lua name) | (2, 1) |
  | 0x12 | DaytimeRunning | (107; front and rear) |
  | 0x13 | DaytimeRunning + Siren | (2) |
  | 0x14..0x17 | DaytimeRunning + Left/Right | (21 / 21 / 22 / 22) |
  | 0x18 | DaytimeRunning + Brakes | the normal tail/stop lamp (138) |
  | 0x19..0x1C | DaytimeRunning + Brakes + Left/Right | (47 / 47 / 3 / 3) |
  | 0x1D | Headlights + DaytimeRunning | (65) |

  Type 0x0A (DippedHeadlights) is never used; two entries have type 0.
  "DaytimeRunning" is what lights tail and position lamps, so it is
  effectively "lights on". (Counts rechecked over the 150 full entries of
  the vanilla archive on 2026-10-29.)
- **b2 is the colour**: 4 amber, 6 red, 8 white, 9 a second white (xenon), 0
  on sirens; 5 (23 entries), 0x0A, 0x0B and 0x02 rare.
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

Vanilla lamp texels have low alpha that follows brightness: medians 0 to 76
per lamp function on the Desanne and the 370Z (Desanne headlamp luminance
124 to alpha 63, reverse 133 to 76, brake 50 to 2; `lamp_alpha.py`).

### What converted cars had

Every converted vertex carried (0, 0, 0, 1) or (slot, model, 0, 1): light
byte 0, so no lamp could light, brake or blink, and the light table still
held the host's lamp positions (flares and headlamp beams in the wrong places
on a shorter or longer car).

### The converter (`src/dsfforza/convert/lights.py`, offline-checked)

`LampPlan.build(car.body, place, axle_mid, lenses, texel)`:

1. Each Forza triangle whose technique is a lamp gets a function
   (`TECHNIQUE_FUNCTION`): `head_light`, `hidhead`, `xenonhead`, `oldhead`,
   `fullbeam` (and their `lights_gls_*` glass) to headlamp; `drlwhite`,
   `drlred`, `slorange` to position lamp; `tail_light`, `taillightst`,
   `taillight2S` to tail/stop; `white_brake` to stop; `reverse_light` to
   reverse; `indicator_left/right` to indicator; `fogwhite/fogred` to fog;
   `brinl/brinr` and, since #220, `lindtailside/rindtailside` to brake +
   indicator; `drlinl*/drlinr*` to DRL + indicator.
   Lenses take a function from their colour: `detail_glass_amber` to
   indicator (a selective-yellow one at the front over a head lamp lights
   with it, "Lamp covers drawn as window glass" below), `detail_glass_red`
   at the rear to tail; `detail_glass_clear` and rear reflectors
   (`textured_reflector`, `reflector`) light with the lamp behind or inside
   them ("Clear covers over reflectors with no lamp", #223; before it, with
   the nearest lamp within 0.35 m); front reflectors and housings stay
   unlit. At the rear the lens texels decide
   over the technique: a lens triangle on red texels is a tail lamp, and a
   red lens over an indicator brakes and indicates (0x19/0x1A; "The Lotus
   Carlton showed no brake light" below). FH1's `ShaderSettings.xml` agrees: `tail_light` has a
   BrakeColor, `taillight2S` a TailLightColor.
   A car with no headlamp technique at the front takes its front clear
   lenses as headlamps. Without those it takes the reflectors of its
   `headlight*`/`glass[LR]HL` sections, else their lamp-cover glass (`glass`,
   `lights_glass`). A car that has a head lamp technique also lights the
   reflectors under a headlight cover with no head lamp in it
   (`_bare_head_units`, #168). In the full roster (562 cars then) the
   fallback applies to 10 cars:
   CHE_CorvetteGS_10, DOD_RamSRT10_06 and FOR_MustangSVTCobraR_93, plus the
   race cars CHE_3_CorvetteC5R_04, MCL_41_McLarenF1GTR_98, MER_CLKGTR_98,
   MIT_HKS_06 and PEU_3_905EVO_93. The NASCARs CHE_2_MonteCarloSS_11 and
   FOR_1_Fusion_11 have decal headlights (`badge`) and stay dark.
2. Side from the technique (`_left/_right`) or the triangle's x; a
   subsection whose |x| stays under 0.3 m is a centre lamp. A centre tail
   lamp more than 8 cm above the side tails becomes a stop-only lamp (type
   0x02).
3. Triangles are grouped by (function, side, end) and each group is split
   into lamps 0.2 m apart on its finest LOD, so a side repeater is not the
   headlamp indicator and a bumper driving lamp is not the headlamp
   (indicators: the cluster nearest the car's end is the main one).
4. `assign(upvh)` gives lamps the host's light entries in priority order
   (`PRIORITY`: headlamps, tails, stop, indicators, reverse, brake +
   indicator, position, fog; secondary clusters last), each an entry at its
   own end and side of the host (`entry_place`; "Lamps broke with the wrong
   end" below). A lamp with none left at its end shares a lamp's entry
   there: same function and side, else the other side's when the type has
   no side (`SIDELESS`, #219), else its fallback's. Only then does it take a
   free entry at the other end; a fog lamp never does (#219) and stays
   unlit, as the 370Z's rear fog lamp does. The table keeps its size and
   ids, so the hierarchy is untouched. Head lamps become 0x1D where the side
   has no position lamp of its own ("Head lamps dark by day").
5. `patch_upvh` writes each used entry's position (area-weighted centroid),
   direction (headlamps (0, 0, 1), else the area-weighted lens normal) and
   flags (type, colour 8/9/4/6, variant 2 on indicators and the combined
   indicator types, 1 on the stop lamp);
   spare entries get flags 0 (type 0, never lit) and park on the nearest lamp.
6. `add_geometry` sets byte 2 of semantic 9 on every lamp triangle (full and
   exterior entries, all LODs and the shadow LOD), keeping bytes 0 and 1.

Lamp texel alpha in the atlas (`dsf_materials.LIGHTS_ALPHA`): "matched"
(the default since the evening of 2026-10-23, "Vanilla lamp alpha" below)
maps `Forza alpha x (0.6 + 0.4 x max(r, g, b))` through a curve per texel
hue onto vanilla's lamp alpha distribution; "raw" keeps Forza's alpha
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
against the vanilla Desanne; `uv run dsff lights table <archive> --id 304`
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
   and break lamps (370Z in run 124438); since #228 the probe switches
   traffic off unless `-Traffic` is given. `-LightsDriveOnly` skips the
   `setLights` phases (`lights = { phases = false }`).
3. Phases 1 to 18 as before, on the player's car and both copies.
4. No traffic phase. Two tries in `lightstest.lua` (sync 92 to 94, since
   removed): `Spawn.Spawn` plus `civilianTraffic.AddVehicleToCarrier`
   returned false and the car stayed parked (run 20261023-115652);
   `GameVehicleResource.swapWithModel` on the nearest live traffic car
   crashed the game (run 20261023-123840). Traffic was left on then (switched
   off, the probe counted 0 cars and nothing to swap, run 121707).

`lightsync.lua` writes retry for 5 s on a sharing violation (the game reads
the file every update; run host-20261023-095111 stopped on one at vanilla 171
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
vanilla control was in those runs, so whether `setLights` works on the
player's car at all was open. Settled below ("Vanilla control and real
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
(207, 203, 204) with alpha 132 on average against the vanilla DeLorean's
(52, 38, 36), alpha 1 (`out/dmc/lampuv.py`). Vanilla DSF tail lamps are opaque
Lights-material texels; no vanilla car has glass over its tail lamps except the
GT-R (228).

Fix (`dsf_materials.Texturing.is_lens`, `lights.LampPlan.build(lenses=...)`):
a lamp-cover glass subsection behind Forza z 0.5 whose mean texel has red
minus max(green, blue) of at least 80 and alpha of at least 128 is drawn from
the atlas with the Lights material, and its triangles light with the lamp
behind them like a clear `detail_glass_clear` lens (within 0.35 m). Later PRs
narrowed this: since #163 `is_lens` takes only the textured covers
(`lights_glass`, `lights_gls_noemit`), since #164 such a cover over a lamp is
baked over it ("Lamp glass"), and since #223 its triangles light with the lamp
right behind them. Front
covers stay glass: on the Challenger SRT8, S65, 300C and Golf R their UVs land
on red texels that Forza doesn't show. Survey of the 40 testdrive cars
(`out/dmc/lensurvey.py`, `out/dmc/lenses.py`): the rule takes the tail lenses
of the M3 E30, Silvia, 300C, M5 E28, 205 T16 and DMC-12 (98 to 135 red, alpha
138 to 241); clear or smoked rear covers (Supra 31, RX-7 58, IS F 13, Range
Rover 45, Civic, 500 Abarth, S2000) and the low-alpha ones (Durango, Express,
Jeep, the 205's bumper lens) stay glass. In game (`host-20261023-091014`,
`observe-10`, `out/dmc/run3L-rear.png`) the DMC's lamps are a red grid with
white reverse cells and no crack lines, like the vanilla DeLorean's; final
build `out/dmc/final` in `host-20261023-094929` (`observe-06`). The lens
triangles carry the light bytes of the lamps behind them (tail 13/14, reverse
7/8 in the full and exterior entries). In the lamp-phase run
`host-20261023-095111` (`-Lights`, `out/dmc/lights-dmc.png`) the DMC's lamps
look the same in every phase, day and dark; `lights_diff.py`'s whole-frame
numbers are dominated by traffic (vanilla 147 scores the same) and the 171
control stopped after phase 10 (`lightsync.lua` locked by another process).
The DMC's lamps do respond in real driving (next sections).

### How a lamp lights (confirmed-static, Driver.exe 1.04)

`lights-re.md` has the full chain with runtime values and corrects two points here: the
colour is also multiplied by the mood's `vehColourBoost` (5 by day, 1 in the harness's dark
mood), and a 0x1D lamp with the running lights alone glows at 1 (brightness 0.1 x 10), not 10;
it reaches 10 only with Headlights, which free roam never turns on.

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
  skipped; statically every light of vanilla 147, 171, 227, 266 and of the
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

### Vanilla control and real driving (runs host-20261023-103726 to -121707)

Confirmed-runtime: vanilla 227 (warm-up), 266, 171, 147 against the ports
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
- **Real driving:** vanilla and converted lamps follow the same state: dark
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
  Vanilla red lamp texels average 20 (75 vehicles, `fleet_alpha.py`; per car
  9 to 58). Fix at the time (`dsf_materials`; replaced by "matched" the next
  day, below, and since removed): the "value" rule (brightest channel) and
  a floor of 64 for lenses that are near black, which Forza lights by its
  glow mask alone (S2000 (31, 2, 2), Durango (19, 0, 0)). Means now: 370Z
  tails 49, high stop 54; S2000 high stop 37; Durango 19; DMC-12 tails 78;
  M3 E30 tails 97 (`lamptex.py`). In game (runs 113752, 115652) the
  370Z's tails and high stop lamp glow when braking and at rest like the
  Desanne's, and go dark on the throttle. Full rebuild `out/testdrive7`
  (this rule; only the Express van's tails stay low, mean 12.7) in run
  124438 (`out/lamps/r124438-final.png`): S2000, DMC-12 and Express light
  when braking and reversing like vanilla 266; the 370Z hit a traffic car in
  its drive and its broken lamps stayed dark (as vanilla lamps do when
  broken).
- **Then too bright and flat** (user report on `out/testdrive7`: "braking
  lights on custom cars are too bright, they are uniformly red"). Under
  "value" the red lamp means were 19 to 97 against vanilla's 9 to 58, and the
  floor and the saturation of Forza's mostly-high mask (below) gave whole
  lenses the same alpha, so a braking lamp lit as one red patch where
  vanilla lamps light along their lens pattern. Fixed by "matched" (next
  section).
- **Converted cars as traffic: not seen.** `-TrafficIds` with eight ports
  (run 125721): `RequestTrafficVehicles` returned nil and none appeared in
  about 90 traffic cars over 80 s; the two tries at making one traffic car
  of the model failed or crashed (harness section). Traffic lamps go
  through the same path (traffic AI `0x779960` -> `0x8D0470(3, Brakes,
  ...)` -> the lamp component and its table types), which the runs above
  show working on converted cars, so converted traffic should brake-light
  like vanilla (hypothesis until a converted car is seen in traffic).

### Vanilla lamp alpha and the "matched" rule (offline-checked)

Vanilla lamp texels, all texels under lamp triangles (UV0 rasterised, full
entry LOD 4) of 146 vanilla vehicles (`out/lamps/scripts/fleet_tex.py`,
samples `out/lamps/vanilla_tex.npy`):

| Lamps | Colour | Mean alpha | p10 / 25 / 50 / 75 / 90 / 99 | > 128 | Per-vehicle mean p10 / 50 / 90 / max |
| --- | --- | ---: | --- | ---: | --- |
| tail + stop (0x18..0x1C) | red | 49 | 2 / 11 / 30 / 66 / 127 / 247 | 10% | 23 / 43 / 81 / 128 |
| stop (0x02) | red | 33 | 0 / 3 / 18 / 44 / 85 / 218 | 5% | 13 / 30 / 62 / 111 |
| tail (0x03 red) | red | 22 | 0 / 0 / 7 / 22 / 59 / 213 | 4% | |
| stop + indicator (0x06, 0x07) | red | 30 | 0 / 3 / 20 / 35 / 64 / 251 | 5% | |
| indicators | amber | 50 | 1 / 11 / 32 / 72 / 125 / 242 | 9% | 23 / 46 / 84 / 153 |
| reverse | white | 58 | 1 / 11 / 36 / 82 / 156 / 253 | 14% | 24 / 53 / 108 / 150 |
| headlamps | white | 71 | 3 / 16 / 49 / 108 / 180 / 255 | 20% | 41 / 70 / 110 / 161 |

So vanilla alpha is an authored glow mask: low over most of a lens, with LED
dots, bulb hot spots and lit strips near 255 (Desanne tail: a dot grid over
the red part, two bright strips in the reverse part; headlamps a radial
hot centre; `out/lamps/tex-266-vanilla.png`). It follows the texel's
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
to the vanilla distribution of that colour (`fit_lut.py`; knots in
`LAMP_CURVES`, vanilla percentiles 0, 5, 10, 20 ... 90, 93, 95, 97, 98, 99,
99.5; red also 85 and 100). The curve is monotonic, so each lens keeps Forza's order of texels:
hot spots near 255, the body low (`out/lamps/forza/*_value_vs_matched.png`:
Forza RGB, "value", "matched"). Per car and lamp on the six cars: tail +
stop means 18 to 95 (370Z 25, S2000 18, DMC-12 38, S5S 37, M3 E30 75,
Challenger 95), stop lamps 10 to 124, reverse 36 to 97, indicators 29 to
132, headlamps 25 to 135, inside or at the edge of vanilla's per-vehicle
range (the Challenger's stop lamp, 124, is above vanilla's 111).

In game with one shared curve (`out/lamps/matched`, runs
host-20261024-001505 and -002212) the lens structure showed, but the
S2000's and S5S Raptor's lamps barely lit: a dim Forza mask stays dim
through a shared curve (S2000 tail median 2). Two changes
(`out/lamps/matched3`):

- **Red curve per car** (`red_lamp_curve`, `Texturing._red_lamp_curve`):
  the knots for red texels come from this car's own red texels inside its
  lights regions (percentiles of their scores -> vanilla's red alpha
  percentiles), so every car's red lamps land on vanilla's distribution in
  their own order. White and amber keep the shared curves.
- **Red lenses darkened to vanilla's brightness** (`matched_lamp_texel`,
  `_LENS_X_RED`/`_LENS_Y_RED`): Forza's red lamp texels are brighter than
  vanilla's (brightest channel median 144 against 103; M3 E30 199, DMC-12
  158), so the lenses looked lit before braking. Their brightest channel is
  mapped down by a quantile curve (never brightened); alpha is scored on
  the original texel.

Per car now (`lamptex.py`): tail + stop means 370Z 41, M3 E30 69, DMC-12
34, S2000 44, S5S 91, Challenger 36 (vanilla per-vehicle 23 to 81, max 128);
stop lamps 370Z 57, S2000 35, Challenger 65 (vanilla up to 111); red lens
brightness DMC-12 129 (was 169), M3 E30 149 (181).

In game (runs host-20261024-003943, -004749, -005601, drive-only:
`-Lights -LightsDriveOnly`; sheet `out/lamps/lamps-vanilla-value-matched.png`,
vanilla / "value" / "matched", braking under the dark mood and in daylight;
per run `out/lamps/r003943-zoom.png`, `r004749-zoom.png`,
`r005601-zoom.png`): the converted lamps now light along their lens
pattern, the 370Z's tails along their inner edge with the high stop bar,
the S2000's round tails from a hot centre, the M3 E30's and DMC-12's
cells unevenly instead of as flat blocks, the Challenger's strip in
segments, and no brighter than the vanilla cars beside them (vanilla 370Z 227,
DeLorean 171, Desanne 266, Sports Car 07 278, Challenger-like 173). Lamps
whose Forza lens has no structure (the DMC-12's grid cells) still glow
cell by cell, more evenly than the vanilla DeLorean's dots.

### Red lenses too bright when unlit (user report on `out/testdrive9`, Audi TT RS)

Symptom (confirmed-runtime): the TT RS's tail lamps were one flat pink-red
patch. In run `vm-20261024-051926` (`out/testdrive10`, `-Lights
-LightsDriveOnly`; red lamp pixels of the daylight drive frames,
`out/emissive/redpix.py`) they measured red 224 to 229 in every step, rolling
on the throttle with the lamps off included, against vanilla 159 to 178 on the
throttle (Desanne 266, A4 136, 370Z 227) and 173 for the converted 370Z. So
the lens itself was too bright, not its glow: the alpha of what the camera
sees (below) is in vanilla's range.

Measure (offline-checked, `out/emissive/rearlamps.py`): an orthographic view
from straight behind, z-buffered over the lamp geometry (triangles with a
light byte) of the full entry, keeping the pixels of red brake-capable lamps;
per pixel the texel's alpha and brightest channel, so lamps count by the
surface the player sees, not by texel count, and bulbs and reflectors behind
an outer lens don't count. Vanilla, 140 vehicles with red lamps seen from
behind: brightest channel pooled p5 35, p25 71, median 92, p75 115, p95 153
(per-vehicle median p10 64, p50 96, p90 126); alpha per-vehicle mean p10 21,
p50 40, p90 81. `out/testdrive10`: alpha per-vehicle mean p10 19, p50 39,
p90 57 (in range), brightness median 112 per vehicle (TT 127, vanilla A4 101).

Cause (confirmed-static): the shared lens curve (`_LENS_X_RED`) was fitted on
every red lamp texel, hidden layers included. Forza lamps are layered (bulb,
reflector, then a lens over them: the TT's `detail_glass_red` covers the
whole lamp and is Forza 177 throughout, 128 after the curve), so the surface
the player sees sat wherever its texels fell in that mix.

Fix (`dsf_materials.red_lens_scale`, `Texturing.visible_red`): the same rear
view on the Forza side (Lights-atlas triangles of the finest planned LOD,
Forza's rear is +z, 5 mm steps) gives each car's visible red texels; after
the shared curve, every red lamp texel of the car is scaled so the median of
the visible ones is at most vanilla's 92 (never brightened; under 400 samples
the shared curve only). Alpha is untouched. Survey of the 40 testdrive cars
(`out/emissive/lenssurvey.py`): 31 are darkened, by x0.57 (205 T16, Forza
224) to x0.97 (Charger Daytona, Golf R); TT x0.72 (128 to 92), M3 E30 x0.68;
the RX-7, S2000, IS F, Jeep, Range Rover, S5S Raptor, Veyron, Huayra, 370Z,
M5 F10 and Mustang already sit at or below 92. Built (`out/emissive/build2`,
DSF side): TT visible brightness 127 to 87, M3 E30 124 to 84, 370Z 74
unchanged; alpha unchanged.

In game (run `vm-20261024-060832`, `out/emissive/build2`, IDs 3 = TT, 4 = M3
E30, next to vanilla 136 and 266; `research/img/emissive-ttrs-tails.jpg`): on
the throttle in sun the TT's tails are a deeper red (red lamp pixels 201,
was 224; vanilla 266 173 in the same run) and braking shows a hot centre
instead of a flat patch; the M3 E30 measures 174 on the throttle, as vanilla.
The TT still reads a little brighter than vanilla in direct sun with its
texels below vanilla's median: its lens tilts up into the sky (lighting, not
texture; open). The A4 control was hit by traffic in that run.

### The Lotus Carlton showed no brake light (user report on `out/final4`, spool 495)

Symptom (confirmed-runtime, user screenshot): the FM4 LOT_Carlton_89 (host
229) at rest showed dark tail lamps; vanilla 229 lights its tails and high
stop lamp at rest and when braking (run `vm1-20261026-220339`,
`lights-229-d1brake.png`).

Cause (confirmed-static): Forza draws the Carlton's whole tail cluster as
one `detail_glass_amber` lens (`glassLTL/RTL`, 185 triangles) whose texels
are red (mean (140, 40, 40), alpha 245), with a small orange band (160, 86,
52) and the `tail_light` bulb (68 triangles) hidden behind it. The converter
took lens colours from the technique name, so the lens became an amber
indicator, and vanilla lamps are opaque Lights texels: the bulb never shows.
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
  makes the lens brake and indicate (0x19/0x1A, as on vanilla US-style cars):
  FOR_GT_05's red lenses over its `drlinl/drlinr` lamps, MIN_Coopers_11's red
  glass over its indicator bulbs. A reverse bulb keeps a lens that is red by
  its texels (not its technique) a reverse lamp, as it lit before
  (PEU_207RC_07, TOY_CelicaSSI_03). A red "amber" lens on a side with no
  other rear indicator also brakes and indicates (MIT_GTO_97: (76, 0, 0)).
- **Clear lenses** still light with the nearest lamp centre, but the centre
  leaves out lens triangles that are red by their texels: with them the
  JAG_XFR_10's tail centre moved onto its lens and its clear lens went to the
  reverse lamp. (Since #223 a clear lens lights with the lamp right behind
  it; the nearest centre, still without those triangles, only picks among the
  lamps its piece has behind it.)
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
  `-Lights -LightsDriveOnly`, IDs 229 vanilla, 1 Carlton, 2 GTO, 3 Ford GT, 4
  207 RC; `research/img/brakelights-vm-sheet.jpg`): on master the Carlton's
  lamps stay dark when braking, day and dark. On the branch they light like
  vanilla 229's, its orange band blinks, and the lower band lights white in
  reverse. The Ford GT's round lamps dim on the throttle and light on the
  brake. The 207 RC's side tails don't glow on master or branch: their
  Lights texels have alpha median 0 (glow mask, #103).

Two cars have almost no lamp in the full entry (the Escort only its front
indicators): FOR_EscortRSCosworth_92 and TOY_6_SC430_08 draw their lamps at Forza LOD 3 only as `lights_glass`
cover glass, with no lamp technique (#103).

The cars still under half (LOD 1 list): rear fog lamps (`fogred`, ASC_KZ1R_12,
LAM_Countach_88), tail-only position lamps (`drlred`, BMW_M3E92_08; vanilla
uses 0x03 red tails too), `reverse_light` drawn on red texels
(PEU_207Super2000_07, TOY_Aygo_11), and CHE_CorvetteZR1_09, whose four round
lamps are `lindtailside/rindtailside` (then DRL + indicator; they brake since
#220, "Tail lamps that brake and indicate"). #103 tracks the rest.

### Head lamps dark by day (player report)

"The headlights should be on all the time like vanilla cars." Static
(`out/brakes/vanillafront.py`, front light entries of the 150 vanilla
vehicles): 140 have a front lamp that lights with DaytimeRunning: white
position lamps 0x03 in the head-lamp cluster (99 vehicles, next to 0x01 head
lamps; vanilla 229 at x ±0.60 beside its ±0.44 head lamps), or head lamps of
type 0x1D, Headlights + DaytimeRunning (33; 31 of them have no 0x01 at
all). Only 10 have neither. Recount (2026-10-29, the vanilla archive's
light tables, front = entry z > 0, not `vanillafront.py`'s rule): 99
vehicles have a front 0x03 lamp, 33 have 0x1D head lamps (31 with no front
0x01), 131 one or the other; counting 0x12 and the DRL-indicator types too,
146 have a front lamp that DaytimeRunning lights and 4 none. The 140 and 10
above weren't reproduced. The converter gave Forza's head lamps 0x01 and
made position lamps only from position-lamp techniques (`drlwhite`...), so
in the front survey 66 of 573 ported cars had a visible front lamp lit by
DaytimeRunning.

Fix (`LampPlan.assign`): head lamps on a front side without a position lamp
(or DRL indicator) of its own at least a quarter of their area are 0x1D
(CHR_Cuda426_71's 10-triangle position lamps beside 109 head-lamp triangles
don't count). Front survey (LOD 1): 554 of 573 cars show a DaytimeRunning lamp from
the front (512 through 0x1D head lamps); 515 cars change at the front. At the
built LOD 3: 64 -> 549. In game (confirmed-runtime, `vm1-20261027-051849` master,
`vm1-20261027-052118` branch, `-Views fq`; `research/img/headlamps-day.jpg`):
the Carlton's head lamps are dark by day on master and lit on the branch,
like vanilla 198's 0x1D lamps in the same run. So the game lights DaytimeRunning
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

**Fix (`src/dsfforza/convert/lenses.py`, `Texturing._bake_lenses`).** Vanilla
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
Subaru's lens blotchy again (alpha p10/50/90 7/27/32). (Still so for coloured
and rear lenses; clear front lenses take the day glow since #142, and baked
covers and flat front lenses glow with the lamp behind since #164 and #218.)
Texels other triangles also sample are left alone (11% of the lens texels over the roster). The
`lights_gls_*` lamp glass is left as it was: its opacity is a material setting
(`GlassMaxOpacity`) and baked with its alpha the Evo's red tail glass came out
mostly white; so are the coloured covers `is_lens` moves to the atlas (the
DMC-12, checked in game); both are baked since #164 ("Lamp glass").
`red_lamp_curve` keeps knots of different alphas at least 0.01 apart (`RED_CURVE_MIN_GAP`, about the score noise of a flat DXT
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
instead of blotches. Vanilla hosts weren't in these runs: the glow (lamp score)
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
median 31, inside vanilla's head-lamp range (mean 71, median 49), and the bake
keeps it as it was: issue #115.

### Pop-up head lamps drawn raised and lowered at once (issue #88)

Symptom (offline-checked): 27 ported cars drew their pop-up or hidden head
lamps in both states at once, the raised pods standing over the closed lids
(RX-7 FC, NSX, Countach, the Corvettes, the Cougar Eliminator's grille doors
open and shut together).

**What Forza has (confirmed-static).** The two states are separate vanilla
sections of the main carbin: `<base>up` (raised, doors open) beside
`<base>down` (lowered, doors shut): `headlightLup`/`headlightLdown`,
`headlightLUp` on the two NSXs, `glassLHLup`/`glassLHLdown` on the Starion.
On the Stratos and the Elan Sprint the lowered state is the plain
`headlightL`/`headlightR` and only the raised one is named (`headlightRup`;
the Stratos's `headlightLup` has no LOD 1 or 2 geometry). Nothing in
`carattribs.xml` or `ShaderSettings.xml` names them, and the cockpit carbins
hold only the lowered state (`headlightLdown` on the Countach and the M1,
nothing on the others): a parked Forza car shows its lamps down. The names
didn't end in an upgrade letter, so `vanilla_sections` kept both. The GTO
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

**What vanilla DSF does (confirmed-static).** Vanilla pop-up cars use chassis
variants (`upvh.md`, "Chassis variants"): the Countach (210), Diablo (211)
and Jalpa (213) have two chassis records, role 0x100 on MODL part 0 and 0x101
on part 1. Part 0 is the body with the lamps raised (308 / 164 / 270 more
triangles at the nose, with glass, trim and lamp materials), part 1 the same
body with flat paint lids (24 / 48 / 88 triangles). The Stratos (217) has
four (0x100 to 0x103: lamps up or down, with or without the centre pod). The
other 0x1xx pairs are not lamps: the taxi signs (268, 272, 282), the
Eldorado's, XLR's and Beetle's roofs, the Cutlass's spoiler, identical
copies on the Challenger (172) and Tanner; 0x5xx are convertible roofs.
`out/popup/variant_diff.py` and `chassis_records.py` list them. No vanilla
car animates its lamps (the 13 UPVH animation blobs are on spoilers and
the tow truck).

In game the variant doesn't follow the lamps (confirmed-runtime, run
`vm1-20261027-162907`, vanilla archive, `-Views 'nose,nose+lit,nose+night'`;
`img/popup-vanilla-countach-jalpa.jpg`): the player's Countach and Jalpa show
the raised part 0 (0x100) by day with every lamp off, and the same with
Headlights and DaytimeRunning on, by day and under the dark mood. When the
game draws the lowered 0x101 part (traffic, parked cars, a random pick per
spawn?) is not traced; the Cuda on host 172 drew it as the player's car
(`upvh.md`), so the pick is probably per vehicle or per spawn, not per lamp
state. So DSF has no lamp-driven geometry switch to give the ports.

**Fix (`forza2dsf.raised_lamp`, used by `vanilla_sections`).** A section
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
switches both off (`clonetest.lua` `setViewLamps`). Since "The lamp model the
renderer and the harness use" `+night` is the story's dark mood with the
running lights only, and a view without a suffix goes back to the running lights. Note that the shot
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
table type, texel RGB and alpha. Vanilla, 142 vehicles, white texels
(`lamp_hue`) of the front lamps: brightest channel pooled p5 41, p25 93,
median 137, p75 180, p95 230 (per-vehicle median p10 82, p50 134, p90 178).
Glow alpha of the visible surface: per-vehicle mean p10 41, p50 64, p90 108.
Vanilla 0x1D head lamps (32 vehicles) carry the same texel alpha as 0x01 ones
(texel-weighted mean 71 and 71, median 49 and 50, `out/lamps/vanilla_tex.npy`),
so vanilla doesn't author its day-lit lamps with less glow. Ported, master
(`out/dayglow/master`): the Carlton's visible head lamps sit at brightness
206 with alpha mean 35, median 26; Lancer Evo VI 173 (alpha 33); M3 E30 197
(alpha 69, median 26); 370Z 90.

Cause (confirmed-static): Forza's white lamp texels are much lighter than
vanilla's. The Carlton's head lamp is a (195, 195, 195) `detail_glass_clear`
lens at opacity 88 to 247 over a `head_light` / `textured_reflector` of 205 to
221 at alpha 255 (`out/dayglow/forzalens.py`), so the baked lens is about 205
everywhere: a light surface in sun before any lamp lights, and with the
0x1D glow on top it blows out. Only red lenses were brought to vanilla's
brightness (Open 5). Forza's mask carries no hot spot for the Carlton (the
reflector is 255 throughout), so there is no bulb structure to carry over.

Fix (`dsf_materials.white_lens_scale`, `Texturing.visible_white`): as
`red_lens_scale` does for red lamps, the visible white front lamp surface of
each car is measured on the Forza side (the `visible_red` view turned to the
front, triangles of the front half, lenses baked), and every white Lights
texel of the car is scaled so its median is at most vanilla's 137 (never
brightened; under 400 samples nothing changes). Alpha (glow) is scored on
the original texel, so it doesn't change. On the DSF side the Carlton, Evo
VI and M3 E30 go to 136, 137 and 133; the 370Z (101 on the Forza side) is
unchanged. Rear white lamps take the same factor: vanilla reverse lamps seen
from behind are median 119 (per vehicle p10 65, p90 177); the ports sit at
148 to 235 on master.

Survey of the roster (`out/dayglow/survey.py`, `tools/batches/all.txt`,
573 cars): 311 are darkened (scale 0.54 to 0.99, median 0.77; lowest
MAS_MC12Corsa_08, MER_SLR_05, MAZ_Furai_08, REN_Clio197_07, 0.54 to 0.58),
245 already sit at or below vanilla, and 17 have too few visible white texels
(race cars with covered or decal lamps, PAN_51_Esperante_05's yellow
lexan). Per car the visible white median is p10 74, p50 145, p90 204 before.

In game (confirmed-runtime, `-Views nose -Palette 0`; master
`vm2-20261027-110011`, this change `vm1-20261027-110149` and
`vm2-20261027-162933`; `img/headlamps-day-whitescale.jpg`): the Carlton's
lit head lamps show the lens flutes and reflector instead of a flat patch
(lamp-box luminance mean 229 -> 211, standard deviation 16 -> 22; vanilla 198's
lit 0x1D lamp 211, 37 in the same box size). The Evo VI changes little (x0.80).
Vanilla 229, the host, has 0x01 head lamps and a small 0x03 position lamp, so
its head lamps stay dark by day.

Tried and dropped: blending the glow mask through a white lens like its
colour (`a x lens + (1 - a) x behind`, run `vm1-20261027-164203`). It lit the
M3 E30's half-clear round lamps (lens alpha 126 over an `oldhead` at 255) the
way vanilla 241's and 130's round 0x1D lamps light, but the Carlton's uniform
255 reflector made its whole lens one alpha (p25 47, median 51, p75 72) and
it was a flat blob again (luminance 236, standard deviation 10). Vanilla lit
lamps keep about a quarter of the lamp at low alpha (0x1D visible p25 18 on
average), which is where their structure comes from. The M3 E30's dim lamps
were the open part (issue #124, next section).

### Head lamp glow by day (issue #124)

Symptom (offline-checked, confirmed-runtime in #115's runs): the ported head
lamps' daytime glow followed the lens, not the lamp. The M3 E30's round lamps
barely glowed where vanilla 241's and 130's round 0x1D lamps glow white, and over
the roster the visible daytime glow was spread wider than vanilla's both ways.

Measure (offline-checked, `out/dayglow/` in the `driversf-forza-wt-dayglow`
worktree): `dsffront.py` is `rearlamps.py` turned to the front (DSF side, full
entry, 4 mm, z-buffered over the lamp-byte triangles, per pixel the table type
and texel); `survey.py` is the Forza-side view of #115
(`Texturing._visible_lamp_texels(front=True)`, 4 mm), with each pixel's glow
alpha (the converted texel's, or the new day glow) and per lamp (connected
pixels) the mean alpha in the outer, middle and inner third of the distance
from its edge (`profile`). Vanilla, 142 vehicles: white front lamp alpha
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
vanilla's range (REN_Clio197_07 visible mean 222).

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
- **Per lamp onto vanilla.** `Texturing._glow` looks at the car from straight
  ahead (the `visible_white` view, 5 mm) and, per lamp (one lens or head lamp
  subsection; at least 50 visible samples), fades the outer 40% of its
  edge-to-middle distance in to 0 (vanilla lamps are dark at the rim; a box
  reflector like the Carlton's stays deep to the lens edge), then ranks each
  texel's raw glow among the lamp's visible samples onto the vanilla 0x1D
  distribution (`DAY_HEAD_ALPHA`). Every head lamp therefore glows as much as
  vanilla's, brightest where Forza's light and the reflector's shape are, and a
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
cars with 400 samples or more, against vanilla's per-vehicle p10 41 and p90 108):

| | p10 / 50 / 90 | under vanilla p10 | over p90 | by thirds outer / middle / inner |
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
| vanilla 0x1D (32 vehicles) | 74 median, 32 / 88 / 131 | |

In game (confirmed-runtime, VM 2, `-Views 'nose,nose+night' -Palette 0
-ShotSeconds 3 -WaitSeconds 600`; master `out/dayglow/before` run
`vm2-20261027-195131` with vanilla 241 and 130, branch `out/dayglow/after2` runs
`vm2-20261027-200145` and `vm2-20261027-202618`;
`img/headlamps-dayglow-ingame.jpg`, `img/headlamps-dayglow-offline.png`): by day
the M3 E30's round lamps now glow white with a hot middle, as vanilla 241's and
130's round 0x1D lamps do (master: pale unlit discs); the Evo VI's lamps glow
from the reflector's centre instead of barely; the Carlton's glow gathers in the
middle of the lamp. At night the three light as before or brighter in the
middle, not past vanilla. A traffic car hit the Evo (first branch run) and the
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
the lamp's end. Since #219 a lamp of a type with no side can also share its
other side's entry at its end, and a fog lamp never takes an entry at the
other end ("Rear lamps short of a rear entry").

Survey (offline, `out/lampside/survey.py` and `summary.py` in the
`driversf-forza-wt-lampside` worktree; the 573 cars of
`tools/batches/all.txt` on their `research/host-picks.tsv` hosts, entry
places from the vanilla tables; NIS_Leaf_11 has no car data):

| | master | branch |
| --- | ---: | ---: |
| lamps on an entry at the other end | 3,041 on 570 cars | 85 on 52 cars |
| of them head lamps | 964 on 465 cars | 0 |
| same end, other side (L/R) | 1,161 on 537 cars | 25 on 25 cars |
| lamps with an entry / unlit | 5,584 / 54 | 5,585 / 53 |

The 85 left are rear reverse (45) and rear fog lamps (32) and a few
indicators on hosts with fewer rear entries than the car has rear lamps;
they light, and break with the front. After #219: 37 lamps on 20 cars
(#146).

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
   (CHE_CorvetteGrandSport_96, LEX_SC430_02; one brake lamp after #219); in game the Veyron's
   tails stay lit after
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
   Since #164 the X5 M's whole swatch lenses are left out and their tint filters the
   lamp behind them ("Lamp glass").

The user's Veyron showed the blobs on its outer round lamps: their centres are the
reverse lamps, which master hung under host 279's wing indicator records (entries 14,
15, x 1.06, z +0.43), so any front or side contact broke them. The 300C's and G37's
tails look right at rest on both builds; their reports (0.1.0, 0.1.1) predate the
current host picks and weren't reproduced.

### Lamp covers drawn as window glass (issue #160)

Symptom (player and user reports on v0.1.2): lamps that look painted on, flat
or black, and don't glow. The Mustang GT 05's head lamps were a black strip
(#158), the Devon GTX's tail lamps a flat red plate, the Panoz Esperante GTLM's
yellow head lamps didn't glow, the Veyron's, 300C's and G37's tails glowed
little or not at all at night.

**What Forza draws (confirmed-static, `dsff fxobj` on FH1's
`media/shaders/Cars/shaders_v16.fxobj`).** The pixel shaders with a
`GlassColor` constant split in two. Those of the plain `glass` and `window`
techniques (0x8df3c, 0x8eb40 and their variants) sample no texture at all, only
the environment and the damage-glass maps: colour `GlassColor` (default 0) plus
the reflection, opacity `max(GlassColor.w, Fresnel term)` capped by
`GlassMaxOpacity`. So a `glass` lamp cover is nearly clear glass, and its UVs'
texels (the Devon's covers sit on flat red ones, its front covers on
(143, 51, 52)) are never seen. `lights_glass` (0xd0894, 0xd11cc) does read the
Lights atlas: texel x `PaintScale` x `GlassColor`, opacity from the texel alpha,
capped by `GlassMaxOpacity`.

**What the ports had (offline-checked, `out/lamps/survey.py`: the main carbin of
the 573 cars of `tools/batches/all.txt`, finest LOD; a cover is over a lamp when a
Lights-atlas surface lies 0 to 0.3 m behind 30% of its area or more).**

| Failure | Cars | Subsections |
| --- | ---: | ---: |
| plain `glass` lamp cover over a lamp, drawn as DSF window glass (front 912, rear 266) | 423 | 1,178 |
| of them rear covers on red texels moved to the atlas as an opaque plate (`Texturing.is_lens`) | 15 | 24 |
| lamp parts with tiling UVs on the Lights atlas drawn as dark shared trim | 15 | 34 |
| front `detail_glass_amber` lens over head lamps, selective yellow (hue 49 to 51) | 1 | 3 |
| rear reflectors (no light byte) | 508 cars light more of their rear lamps after the change | |

DSF's window glass texel is (13, 233, 192): blue is the alpha, so the pane is 75%
opaque with a dark diffuse. Vanilla cars use the same slot over their head lamps
(134 of 148 vanilla vehicles have glass over 30% or more of their front lamp
area, `out/lamps/vanillacover.py`; their cover UVs sit in the flat window region),
but in game the pane over a converted lamp hid it: with the covers left out
(`out/lamps/nocover`, run `vm1-20261028-092416`) the Veyron's and 300C's tails
glowed red at night, under the window glass (run `vm1-20261028-092017`) barely.
The 15 opaque covers (BUG_Veyron_09, CHE_Volt_11, CHR_300C_08 (FM4), DEV_GTX_10,
INF_G37Sport_08, JAG_XFR_10, JAG_XKRS_12, MAZ_Mazdaspeed3_10, MIN_Coopers_11,
NIS_Skyline350GT_03, PAG_ZondaCinque_09, SUB_WRXSTi_04, VAU_AstraVXR_06,
VAU_CorsaVXR_09, fer_f142_10) showed Forza's unseen red texels over the lamp.
The Devon's tail lamp is mostly a red `textured_reflector` round a
15-triangle `tail_light`; reflectors had no light byte, so with the cover gone
its tails didn't glow either. The Mustang's head lamp bowls are a tiling
`textured_reflector` (UVs -2.9 to 3.9): the atlas regions can't hold a tiling
footprint, and `Texturing.material` drew it in the shared trim's dark texel.

**Fix.**

- `Texturing.clear_cover`: a plain `glass` subsection on the Lights atlas, outside
  the window sections (`WINDOW_SECTIONS`), with a Lights surface behind 30% of
  its area (`COVER_MIN_SHARE`, `COVER_DEPTH` 0.3 m) is left out. 30 cars draw
  windows (148 subsections, BUS_TOUR_12's windscreen among them) with the same
  technique; they stay glass.
- `Texturing.is_lens` takes only the covers whose texels Forza draws
  (`TEXTURED_COVERS`: `lights_glass`, `lights_gls_noemit`; the DMC-12's grid lens).
- `Texturing._tiling_swatch`: a tiling lamp part is one Lights colour, the mean
  texel at its corners and centres with the UVs wrapped (the Mustang's bowls
  (160, 145, 149)). Since #190 and #218 only parts that `unwrap_tiles` can't
  lay into one tile.
- `lights.yellow_lens`, `_yellow_head_covers`: a front amber lens whose mean texel
  hue is 45 or more lights as a head lamp when a head lamp triangle lies within
  0.15 m (the Panoz; FER_83_F430GT_10's yellow bumper lens has none and stays an
  indicator), and the lens bake gives it the day glow of a clear lens.
- Rear reflectors light with the nearest rear lamp within 0.35 m, like clear
  lenses (`LampPlan.build`; since #223 with the lamps inside their outline):
  rear lamp triangles with a light byte 674,063 ->
  1,197,438 over the roster (host 266 plan, `out/lamps/plan_survey.py`).

In game (confirmed-runtime, `-Views 'nose,rear,nose+night,rear+night' -Palette 0
-ShotSeconds 3 -WaitSeconds 600`; master `out/lamps/before14`, branch
`out/lamps/after14` / `final14`; runs `vm1-20261028-093612` / `-094231` (Devon,
Mustang, Panoz, X5 M, Veyron, 300C, vanilla 182), `vm2-20261028-093737` / `-094430`
(G37, Cee'd, Supra, Eclipse, 9-3, ZR1, vanilla 227) and the rerun below): the Devon's
tails show the lamp's structure by day and glow red at night; the Panoz's yellow
lamps glow by day and night; the G37's, Eclipse's, 9-3's and ZR1's tails show
their lamp graphics and glow at night (the ZR1's four round lamps, #103), the
Supra's brighter. In run `-094231` traffic hit the X5 M and the Veyron from
behind before their rear views (dented bumper, broken-lamp pattern).

### Lamp glass (issues #89, #145)

Symptom: on 129 cars the textured lamp covers (`lights_glass`, `lights_gls_noemit`) over a
lamp were drawn as DSF's dark window glass (97 cars) or moved to the atlas as an opaque plate
of their own texels (`is_lens`, 46 cars), so their lamps showed dimly or not at all; the
`lights_gls_*` lamp glass (61 cars) was opaque Lights texels; and the BMW X5 M's red tail
glass, a 17 x 14 texel swatch stretched over the lens, showed one flat red over its L-shaped
lamps, and only the LED strip glowed (#145).

**What Forza draws in a race (confirmed-static, `dsff fxobj` on FH1's
`media/shaders/Cars/shaders_v16.fxobj`, `CustomCarShaders.xml` and
`media/cars/Shared.zip` `ShaderSettings/Normal.xml`, the same on FM4).**

- `CustomCarShaders.xml` lists 145 custom techniques (`detail_glass_red`, `_clear`,
  `_amber`, `lights_glass` among them), every one `uiload="true" inraceload="false"`: their
  own `.fxobj` shaders are loaded for the UI only, and a race uses the `shaders_v16.fxobj`
  techniques of the same name.
- The custom `detail_glass_*` shaders (UI) draw in two passes: the first (0x2d44, 84 to 108
  bytes) writes the texel with no lighting and no texel alpha (`detail_glass_red`: red
  remapped to `sqrt(DarkValues + (LightValues - DarkValues) x red^2)`, defaults 0 and 1),
  which makes sense only as a filter multiplied over what is behind; the second adds the
  environment reflection (`Glancing`, `Direct`, Fresnel). The blend states themselves are in
  the effect's pass tables, which `fxobj.py` doesn't decode.
- The `shaders_v16` glass shaders: `lights_glass` (0xd0894, 0xd71ac) colour
  `texel x GlassColor x (PaintScale x diffuse + AmbientColor)` plus reflection, opacity
  `max(texel alpha, GlassColor.w)` plus Fresnel and specular, capped by `GlassMaxOpacity`.
  The lamp shaders (`tail_light` 0xb800c; the lamp glass with `GlassColor`, 0xd1bf4 to
  0xd6684) start the opacity at 1 instead of the texel alpha (literal `c254.y` / `c255.y`
  = 1), so their opacity is `GlassMaxOpacity`, and the texel alpha is only the glow mask
  (times `BrakeColor` and the like).
- Shared defaults (`Normal.xml`): `lights_glass` GlassColor (0.5, 0.5, 0.5, 0), cap 1;
  `lights_gls_*` GlassColor (1, 1, 1, 1), cap 1, PaintScale 0.7; `detail_glass_*` cap 0.7,
  GlassColor (0.25, 0, 0, 0) red, (0.2, 0.2, 0.2, 0.6) clear, (0, 0, 0, 0.8) amber, PaintScale
  0.2. Cars set `GlassMaxOpacity` on 229 `lights_gls_*` techniques (0.8 on 182) and 86
  `lights_glass` ones (0.5 to 0.95).

**Roster (offline, `out/lamps2/survey.py` on master and `out/lamps2/after.py` with this
change; the main carbin of the 573 cars of `tools/batches/all.txt`, finest LOD; a cover is
over a lamp when a Lights surface lies 0 to 0.3 m behind 30% of its area or more).**

| Class | Before (cars / subsections) | After |
| --- | --- | --- |
| `lights_glass` / `lights_gls_noemit` cover over a lamp | 129 / 303 (DSF window glass 97 / 210, opaque plate 46 / 93) | 129 / 304 baked over the lamp |
| `lights_gls_*` lamp glass with `GlassMaxOpacity` under 1 | 44 / 222 opaque | 44 / 222 baked at that opacity |
| swatch lenses (scrambled texels, #141) | 14 cars, 51 subsections keep a flat tint | 6 cars, 11 whole swatch lenses left out, their tint on the lamp behind (131,330 texels); the partly scrambled ones keep their tint |

The swatch lenses left out: BMW_X5M_11 (`glassLTL`, `glassRTL`, `trunk` red), CHE_Spark_11,
KIA_ForteKoup_10, ULT_GTR_12, VOL_242Turbo_83 and VW_SciroccoGT_09. The bake takes 2.9 s a
car (median; at most 7.4).

**Change (`lenses.py`, `Texturing._bake_lenses`).**

- Covers over a lamp (`Texturing.lamp_cover`) go to the atlas and are baked like the lenses,
  with Forza's opacity `min(max(texel alpha, GlassColor.w), GlassMaxOpacity)`
  (`lenses.opacity_rule`, car settings first, then the shared defaults). They have no glow of
  their own: a baked cover texel glows with the lamp behind it, its whole glow (Forza's lamp
  emission is HDR and outshines a cover at 0.9; passing only `1 - opacity` of it left the GTI
  VR6's tails dimmer in the dark mood than under the window glass, run `vm1-20261028-103705`).
  The rear red covers `is_lens` moves to the atlas are baked when they lie over a lamp
  (2 cars, 3 subsections, are not over one and keep their texels).
- `lights_gls_*` glass with a cap under 1 is baked at that cap (0.8 on most cars: a fifth of
  the lamp behind shows) and keeps its own glow (its lamp score).
- The lenses keep their texel alpha as opacity (capped by the car's own `GlassMaxOpacity`):
  the shared defaults (opacity at least 0.6 and at most 0.7 on clear lenses) go with a lens
  colour of a twentieth of the texel plus Forza's reflection; with the texel's own colour,
  which the bake draws, they would make milky plates.
- A swatch lens whose texels are mostly scrambled (`lenses._scrambled`) and whose rays meet
  Lights surfaces for half or more of the lens is left out at every LOD
  (`Texturing.dropped_lenses`); every texel of the Lights surfaces it covers is multiplied by
  its filter (`lenses.lens_filter`, the custom shader's first pass). Blended by its texel alpha
  instead (61 on the X5 M), the X5 M's red lens over pale grey reflectors came out pink. The
  tinted reflectors keep their glow mask, and since #163 they light with the tail lamp.

In game (VM 1, `-Views 'nose,rear,nose+night,rear+night' -Palette 0 -ShotSeconds 3
-WaitSeconds 600`; IDs 304 X5 M, 305 Lancer Evo VI, 306 GTI VR6, 307 300C (FM4), 308 Mustang
GT 05, vanilla 182; master `out/lamps2/beforeA` run `vm1-20261028-103158`, this change
`out/lamps2/afterA` run `vm1-20261028-103705`, with the cover glow above `out/lamps2/afterA2`
run `vm1-20261028-104816`, IDs 306, 305, 304, 182):

- X5 M: the tails show the L-shaped lamps in red by day instead of a flat dark plate, and the
  whole L glows red in the dark mood (master: barely visible) (`img/lampglass-x5m-ingame.jpg`).
- GTI VR6: red tail lamps behind their covers instead of dark grey glass; the head lamps
  show their reflectors, and glow by day as day-lit head lamps (0x1D) do on the other ports;
  in the dark mood the tails glow along the whole lamp, as vanilla 182's do
  (`img/lampglass-gtivr6-ingame.jpg`, `img/lampglass-gtivr6-dark-ingame.jpg`).
- Lancer Evo VI: the red tail glass a little deeper by day; lit in the dark mood (master's
  shot of that view missed the car) (`img/lampglass-evo6-ingame.jpg`).
- 300C and Mustang GT 05 (unchanged by this; clean shots of both, item 7 of #163's report):
  the 300C's tails as on master; the Mustang's round head lamp bowls stay dark (#158).

### Head lamps white plates by day (after #164)

Symptom (confirmed-runtime): after #164 the VW GTI VR6's head lamps by day were solid white
plates; on master before it the lamp could be read through DSF's dark window glass
(`img/lampglass-gtivr6-ingame.jpg`, right half). The Lancer Evo VI and BMW M3 E30 looked the
same way by day without any cover (run `vm1-20261028-112351`, `-Views nose`).

Measure (offline-checked; `out/t1/front.py` in the `t3-cc1295ad` worktree, an orthographic
front view of the full entry at 4 mm, z-buffered, per visible lamp pixel the light type and
texel; the 150 vanilla vehicles and the built ports). Vanilla 241's 0x1D head lamps carry more
glow than the ports' (alpha median 98, mean 112, against the GTI's 60 and 76), yet by day they
read as lamps: their glow follows the lens's flutes, stripes of high alpha with dark texels
between, so where it clips the stripes stay apart. The ports' day glow (#142) is a smooth blob
hot in the middle and dark only at the rim; clipped, it is a plate. What differs is the
level: the game scales a lamp's glow by its type (0x1D 10, a 0x01 head lamp or 0x03 position
lamp 2), and 99 of the 130 vanilla vehicles with a day-lit white front lamp light only a small
0x03 position lamp by day beside 0x01 head lamps (vanilla 229, an R33 Skyline). Per vehicle,
the day-lit front lamp emission (alpha / 255 x the type's scale x area, cm2) is p50 144,
p75 311, p90 1254 over those 130; the day-lit area p10/50/90 66 / 248 / 454 cm2.

Cause: the ports' head lamps are 0x1D on 524 of 573 cars ("Head lamps dark by day") and take
vanilla's day-lit alpha (`DAY_HEAD_ALPHA`) over the whole Forza lamp, so they light at 0x1D's
10 over an area as large as vanilla's: by day the ports' median emission was 686 and 100 cars
were above vanilla's p90.

Fix (`dsf_materials.DAYTIME_GLOW`, `Texturing.daytime_mask`, `Binding.set_daytime`,
`lights.LampPlan.is_daytime`): every Lights texel that a 0x1D head lamp triangle samples glows
at 0.2 of what it did, a 0x01 or 0x03 lamp's glow: the day glow (#142) where it has one, else
the converted alpha, and the swatch cell of a tiling lamp part. Texels of a lens that also
covers an indicator (the Evo VI's) keep theirs. `Texturing.resolved` is looked up by
subsection: the plates and damage passes replace the section objects after `plan()`. The atlas is rendered in `prepare()` before the lamp
plan exists (it keys on the subsections `damage.split_exposed` makes later), so it is
rendered again after the plan for the cars that have such texels (one more render, about a
second). 0x01 head lamps (49 cars with their own position lamps) are unchanged.

Roster (offline, `out/t1/survey.py`: Forza side, the `visible_white` view of the
`Texturing._glow` texels on 0x1D head lamps; 572 cars, NIS_Leaf_11 has no car data;
emission scaled by 0.67, the DSF / Forza ratio of the four built cars):

| | p10 / 50 / 90 | over vanilla p90 | over vanilla p75 |
| --- | --- | ---: | ---: |
| ports with a day-lit head lamp glow (514), master | 188 / 686 / 1545 | 100 | 421 |
| the same, this change | 38 / 137 / 309 | 0 | 50 |
| vanilla (130 vehicles) | p50 144, p75 311, p90 1254 | | |

The highest after the change: SAA_93Aero_02 595, VW_GTIVR6_98 574, LOT_Carlton_89 529.

In game (confirmed-runtime, VM 1, `-Views 'nose,rear,nose+night,rear+night' -Palette 0
-ShotSeconds 3 -WaitSeconds 600`; IDs 1 GTI VR6, 2 Lancer Evo VI, 3 M3 E30, 5 Devon GTX, vanilla
241 (0x1D) and 229 (0x01); master `out/t1/before` run `vm1-20261028-112351`, this change
`out/t1/after` run `vm1-20261028-112849`; then IDs 1 GTI VR6, 2 Mustang GT 05, 3 Focus RS500,
4 M3 E36, vanilla 241 and 229, master `out/t1/before2` run `vm1-20261028-115057`, this change with
the next PR's head lamp units `out/t1/after2` run `vm1-20261028-115446`; the first car of a run
misses its first views, so vanilla 241 went first): by day the GTI VR6's, Evo VI's, M3 E30's and
M3 E36's head lamps show their lens and reflector with a soft glow, as vanilla 241's do (master:
clipped white plates); in the dark mood they glow, a little smaller than on master
(`img/headlamps-daytime-glow-ingame.jpg`). Built (DSF side, `front.py`), day-lit emission
master -> this change: GTI VR6 2717 -> 547, Evo VI 2184 -> 478, M3 E30 1990 -> 398, Devon GTX
407 -> 82.

### Head lamp units with no lamp technique (issue #158)

Symptom: the Ford Mustang GT 05's round head lamp bowls never glow (#158). Its only head lamp
technique is the `head_light` under the amber bumper lenses (`bumperFa`). The bowls are a
tiling `textured_reflector` in `body` under the plain glass of `headlightL/R`, and front
reflectors stay unlit (#124). So they had no light byte.

Fix (`lights._bare_head_units`): on a car that has a head lamp technique at the front, a
headlight section's cover (`headlightL/R`, `glassLHL/RHL`, the `LAMP_COVERS` techniques; one
box a side) with no head lamp triangle inside its outline (2 cm round it, `HEAD_UNIT_PAD`)
and up to 0.3 m behind it (`HEAD_UNIT_DEPTH`) makes the front reflectors in that box head lamp
triangles. They cluster with the car's other head lamps (0x1D like them), and since "Head
lamps white plates by day" their texels and swatch cells glow at `DAYTIME_GLOW`.

Roster (offline, `out/t1/survey2.py` in the `t3-cc1295ad` worktree: `LampPlan.build` on the
host-picks hosts, 572 of 573 cars, NIS_Leaf_11 has no car data): 9 cars light head lamp
units this way: FOR_MustangGT_05 (236 triangles at LOD 1), FOR_FocusRS500_10 (104),
BMW_E36M3_97 (150), BMW_M635CSi_84 (104), FOR_9_FGFalcon_11 (156), LAN_DeltaS4_86 (144),
SEA_LeonCupra_07 (48), SEA_LeonCupraR_10 (40), VOL_S60_11 (210). Built, the Mustang's day-lit
front lamp emission is 362 (cm2 x alpha / 255 x 10; the bowls at their swatch's alpha x 0.2).

In game (VM 1, `-Views 'nose,rear,nose+night,rear+night' -Palette 0 -ShotSeconds 3
-WaitSeconds 600`; master `out/t1/before2` run `vm1-20261028-115057`, this change on top of
"Head lamps white plates by day" `out/t1/after2` run `vm1-20261028-115446`;
`img/headlamp-units-ingame.jpg`): the Focus RS500's projector bowls glow round the bulb in
the dark mood. The Mustang GT 05 shows no front lamp at all in the dark mood, on master and
with this change alike (bumper lamps included), while its tails light; its light table has
the head lamps on host 192's front entries 14 and 15 (0x1D), under the headlamp part records
(roles 50, 51). The cause was outside the lamp plan; #175 fixed it (next section).

### No front lamp on the player's car: lamp parts drawn as trim in the exterior entry (issue #158)

Symptom (confirmed-runtime): the Ford Mustang GT 05 showed no front lamp at all, by day or in the
dark mood, on master and with "Head lamp units with no lamp technique" alike, while its tails lit.

Ruled out (confirmed-static): the lamp objects. `0x8E82B0` makes lamp object `id` (table entry
+0x28) for every type-17 record (`ecx = this + id x 16` before `call 0x8E6720`) and skips a record's
subtree only when its dword +0x0C is 0; on host 192 every record above a light has it set. `0x8A78E0`
uploads every lamp object's colour. The full entry was right too (orthographic front view,
`out/l4/py/zfront.py`): the round bowls carry the head lamp bytes 15/16 on the Lights material.

Cause (confirmed-static, confirmed-runtime): the player's car draws the clean exterior entry
(`vehicle-model.md`), and there the bowls (`body/textured_reflector`, 800 triangles at Forza LOD 1)
were drawn with the shared trim (`GLOBAL_Chassis_Simple_LOD1`), so their light byte did nothing; the
bumper `head_light` lamps sit behind the amber indicator lenses. `Texturing.plan()` resolves each
subsection (`Texturing.resolved`, keyed by section and subsection object), but the plates pass and
`damage.split_exposed` run after it and hand the untouched subsections to `add_geometry` inside new
section objects. `submaterial()` then resolved them again: a tiling lamp part (`_tiling_swatch`)
re-sampled the Lights atlas after `lenses.bake` had written into it ((156, 144, 150) instead of
(160, 145, 149)), a plain swatch had no colour, and lamp glass that `_bake_lenses` made a lens
(`lamp_cover`) lost that role. No atlas cell, so `material()` fell back to shared trim or window glass.

Fix (`Texturing.planned`, `submaterial`): a Lights-family subsection keeps plan()'s answer in any
section object around it. Trim artwork had the same fault (#173); every planned subsection now keeps
plan()'s answer (below).

Roster (offline, `out/l4/py/survey2.py` in the `t3-f6fc8832` worktree: plan, then the plates pass and
`split_exposed` on the host-picks host, `material()` on master against this change, Forza LODs 1, 3,
4, 5; 572 cars, NIS_Leaf_11 has no car data): 49 cars change. In the exterior entry (LOD 1) 23 cars'
lamp glass goes from window glass to its baked lens (2,200 triangles; VW_GTIVR6_98's head lamp covers
138, RUF_Yellowbird_87 194, ACU_IntegraR_01 262) and 11 cars' lamp swatches from trim to Lights (1,549
triangles; FOR_MustangGT_05 800, VIP_Viper_13's tail lamps 260, fer_f142_10's red tail lenses 139); at
LODs 3 to 5 (full entry) 19 and 18 cars. The same mechanism draws trim artwork (`trim_art`: gloss,
grey and detail trims) as plain shared trim on about 400 cars (157,000 exterior triangles): issue #173.

Trim artwork and swatches (issue #173, confirmed-static, confirmed-runtime): the same re-resolve drew
them as shared trim. `_layout` samples a swatch's colour into plan()'s `SubMaterial` in place
(`fm.sample_colours`) and the ART_MIN_SCALE_SHARE step swaps flat artwork for its `_art_swatch`; resolved
again, a swatch had no colour and flat artwork was its textured self outside `atlas_subs`, so neither had
a cell and `material()` never reached the gloss index (#143). `Texturing.planned` now holds plan()'s
answer for every subsection, not only Lights ones. Subsections the later passes make (plates,
`split_exposed`'s split pieces) are new objects and are still resolved in `submaterial()`.
Roster (offline, `out/s173/survey.py` in the #173 worktree: as above on 566 cars; master's Lights-only
plan against all families): exterior entry (LOD 1) trim -> gloss on 371 cars (150,707 triangles), trim
-> Chassis cell on 99 (4,617); LODs 3 and 5, 386 and 110 cars (50,233 and 2,777); nothing else changes,
and every subsection in the rebuilt sections now draws plan()'s answer. By technique (LOD 1): plastic2
55,811, rubber_trim 33,021, misc 30,575, matte_colors 27,369. In game (VM 1, `-Views nose,rear,grille
-Palette 0`, vanilla 229, IDs 1 Camaro ZL1 12, 2 Huayra 12, 3 XB Falcon GT 73, 4 Yellowbird 87, 5 Fiat
595 68; master `vm1-20261028-155614`, this change `vm1-20261028-160123`): black trims get #143's satin
sheen on the player's car; the Camaro's front splitter (`bumperFa`, `plastic2`, gloss specular 0.031,
power 5.66) draws as a cream band in the sun, #143's broad-lobe look on a flat panel (#176).

In game (VM 1, `-Views 'nose,rear,nose+night,rear+night' -Palette 0 -ShotSeconds 3 -WaitSeconds 600`,
vanilla 229 first; master `out/l4/before` run `vm1-20261028-140509`, this change on the full roster
of section objects `out/l4/after` run `vm1-20261028-140904`, IDs 1 Mustang GT 05, 2 Mustang Mach 1 71,
3 XB Falcon GT 73): the Mustang's bowls show their lamp colour by day (master: black) and glow in the
dark mood (master: no front lamp); `img/headlamps-mustanggt05-exterior-ingame.jpg`.
Lamp parts only (this change), same views: master `out/l4/before2` run `vm1-20261028-153131`, this
change `out/l4/after2` run `vm1-20261028-153451`, IDs 1 Mustang GT 05, 2 VW GTI VR6, 3 Viper 13,
4 Ferrari F142. The Mustang glows as before. The GTI VR6's bumper covers are baked lenses now,
not window glass, and light with its head lamps (0x1D) by day and in the dark mood: the full entry
has done that since #164, but those bumper lamps have no lamp technique in Forza (#174). The Viper's
and F142's tails look the same in both runs.

### Flat lenses, lens skins, laid-back head lamp lenses and tiling bowls (player reports on 3ca28fd)

Symptoms (user's build of master 3ca28fd): the BMW X6 M's tail lamps were flat dark red with no lamp
graphics and only the LED strip lit, as the X5 M's before #164; the Panoz Esperante GTLM's (the road
car, PAN_Esperante_05, host 151; PAN_51_Esperante_05 is the #51 race car) left tail lamps were brighter
than the right, and its head lamps looked painted on; the Mustang GT 05's head lamp bowls were plain
white domes.

**X6 M: flat lenses (offline-checked).** Its red tail glass (`glassLTL/RTL`, `trunk`
`detail_glass_red`) is 40,000 texels of one colour, (183, 0, 0, 118): not a swatch stretched over the
lens, so `lenses._scrambled` (texels lens points far apart share) never took it as #164's swatch lens.
It was baked as a lens: 46% flat red over the lamp and the lens's own flat alpha as glow, so the lamp's
L-shaped graphics showed faintly and the glow mask was gone. A rear lens whose texels are one colour
(`lenses._flat`: 90% within 6 of the median per channel, `FLAT_SHARE`, `FLAT_SPREAD`) is now a swatch
lens too: left out when half its rays meet a lamp, its filter (`lens_filter`) laid over the lamp
behind. Front lenses keep their bake (their day glow, #124, comes from the lens).

**Panoz tails: lens skins (offline-checked).** Each tail lamp is a red and a clear `detail_glass_*`
lens under a `lights_glass` cover (cap 0.604) whose texels are the lenses' own: both covers sample
the left lenses' texels (20,966 = 10,357 + 10,608), the right lenses have texels of their own.
`lenses.bake` averages every bake that writes a texel, so the left lenses' texels were the mean of
the left lens's bake and both covers' bakes (paler, with the covers' glow, the lamp's whole mask);
the right lenses' were their own. A lamp cover (`lamp_cover`) whose texels are 90% or more lens
texels (`COVER_ON_LENS`) is a second skin drawn with the lens artwork: `Texturing._drop_lens_skins`
leaves it out (`dropped_lenses`) before the bake.

**Panoz head lamps: rays along the axis (offline-checked).** The head lamp lens (`headlightL/R`
`detail_glass_clear`, texels (174, 172, 174) at alpha 66 over the two reflector bowls) lies back on the
wing: its normal is (-0.13, 0.65, -0.58). Rays along the normal met the housing floor (`body/misc`,
1,743 of 2,484 rays through the see-through circles), so the bowls baked black. Along the car's axis
2,179 of them meet the bowls (`body/head_light`, the foil texels (208, 208, 208)). A front lens ray
that meets no lamp surface along the normal is cast again along the car's axis, into the car (Forza's
+z; `lenses._axis_fallback`) and takes that result when it meets a lamp. Rear lenses were unchanged
until #218, which also limited the axis rays to lenses facing out of the car's end.

**Mustang GT 05 bowls: tiles unwrapped (offline-checked).** The bowls (`body/textured_reflector`,
LOD 1, u -2.9 to 3.9) sit in tiles -3, 0 and 3 with no triangle across a tile edge.
`dsf_materials.unwrap_tiles` moves each triangle into the 0..1 tile (`TileWrapped`: a vertex's
shift by its raw UV); a tiling lamp part that unwraps is an ordinary textured part. Up to 10% of its
triangles could cross a tile edge (`TILE_CROSS_SHARE` then; the Range Rover 12's head lamp bowls, 3 of
304): they were clamped into the tile of their lowest corner. `_tiling_swatch` (#163) stayed for parts
with more, or with a vertex in triangles of two tiles: the emitter shares a vertex within a region, so
it has one UV. #218 replaced both limits (next section).
`resolve_lamp` gives lenses (`_Surfaces`, the bake's shared-texel pass, `lamp_glow`) and the lamp
plan's texel sampler the same unwrapped material (the Mustang's lamp plan is unchanged by it).

**Roster (offline; `out/l5/survey.py`, resolve and texels only, and `out/l5/bake_survey.py`,
`Texturing._bake_lenses` on master's rules and on this change, LODs 1, 3, 4, 5, the tiling parts
unwrapped in both; `out/l5/tile_count.py` for the tiling parts; the 573 cars of
`tools/batches/all.txt`):**

| Change | Cars | Subsections |
| --- | ---: | ---: |
| flat rear lenses left out, their filter on the lamp (master: baked as flat plates) | 82 | 220 |
| lamp covers over their own lenses' texels left out (master: baked into the lenses' texels) | 23 | 37 |
| front lens rays that met no lamp, now along the axis onto one | 262 | 85,468 rays |
| tiling lamp parts unwrapped (master: one flat colour), all LODs | 29 | 166 |
| of them at LOD 1 (the exterior entry; 5,290 triangles) | 16 | 36 |
| tiling lamp parts still one colour, all LODs | 39 | 104 |
| of them at LOD 1: FOR_ShelbyGT500_13's tails (every triangle across an edge), KIA_ForteKoup_10's head lamp bowls and MIN_JohnCooperWorks_09's reflector (vertices in two tiles) | 3 | 6 |
| cars whose baked Lights texels change (more than 8 in a channel) | 273 | |

The flat rear lenses: 155 red and 82 clear on the static count (`out/l5/s_all.tsv`), alpha 0 to 255;
the Golf R's red tail glass is flat and opaque, (131, 8, 5, 255), so master drew it as an opaque plate
over the lamp. The lens skins: ACU_IntegraR_01, BEN_ContinentalSS_10, BMW_M5E39_03,
BMW_M635CSi_84, CHE_CamaroIROCZ_90, CIT_C1_11, FER_355Berlinetta_94, FER_89_F430GT_10,
FER_90_F430GT_08, FIA_500AbarthSS_10, HON_CRXDelSol_95, HYU_ix20_11, LAN_Delta_92,
LOT_EspritTurbo_80, MER_C32AMG_04, NIS_FairladyZ_03, NIS_SkylineGTRVspec_95, PAN_Esperante_05,
REN_5Turbo_80, RUF_Yellowbird_87, TOY_CelicaST205_94, VAU_Agila_11, VW_GTIVR6_98. Most axis rays:
HYU_GenesisCoupe_13 5,559, PON_TransAmGTA_87 3,677, TOY_Sprinter_85 3,405, NIS_240SX_94 3,125.

In game (confirmed-runtime; VM 2, `-Views 'nose,rear,nose+night,rear+night' -Palette 0
-ShotSeconds 3 -WaitSeconds 600`, vanilla 182 first; IDs 1 X6 M, 2 Esperante GTLM (road),
3 Esperante #51, 4 Mustang GT 05; master's lamp code `out/l5/before` run `vm2-20261029-004501`, this
change `out/l5/after` run `vm2-20261029-005310`):

- X6 M: by day the tails show their L-shaped lamp graphics instead of a flat plate; in the dark mood
  the graphics glow red (master: only the third brake light) (`img/lamps5-x6m-tails-ingame.jpg`).
- Esperante (road): the left and right red lenses look alike by day (master: the left a flat bright
  disc, the right with the lamp's pattern) and glow alike in the dark mood; the clear lenses are
  grey on both sides (`img/lamps5-panoz-tails-ingame.jpg`). The head lamps show their two reflector
  bowls through the lens by day and glow over them in the dark mood (master: a white crescent at
  the top) (`img/lamps5-panoz-headlamps-ingame.jpg`).
- Esperante #51: the yellow head lamps show more of the lamp; the tails as on master.
- Mustang GT 05: the bowls show Forza's crinkled reflector and the bulb (master: plain domes)
  (`img/lamps5-mustanggt05-headlamps-ingame.jpg`).

Cars the rules reach beyond the reports, same views (`out/l5/before2` / `after2`: IDs 1 Golf R 10, 2 Genesis
Coupe 13, 3 Sprinter 85, 4 GTI VR6; runs `vm2-20261029-012814` / `-014349` for the Golf R, the VMs were
slow and the probe stopped after it; `vm1-20261029-022411` / `-023026` for IDs 2 to 4, `-WaitSeconds
1500`; `out/l5/before3` / `after3`: IDs 1 Range Rover 12, 2 IS300, runs `vm2-20261029-023034` /
`-023621`, with the 10% edge rule):

- Golf R: the tails show the smoked lamp with its red L by day (master: its flat opaque red lens, a
  salmon plate) and light red in the dark mood (`img/lamps5-golfr-tails-ingame.jpg`).
- Genesis Coupe 13: the tails are red lamps with their graphics (master: its tiling lenses as one
  white and red flat colour each) and glow red in the dark mood (master: barely). The amber
  reflector inside each head lamp reads orange by day: its amber lens now sees it along the axis
  (its glow is the lens's own score, as before).
- Sprinter 85, GTI VR6 (its tail covers left out as lens skins), Range Rover 12, IS300: as on master
  by day and in the dark mood, the IS300's bumper fog lenses with a little more detail.

### Tiles across edges, streak parts, flat front lenses, rear axis rays (issue #189)

Left open by the previous section: tiling lamp parts that `unwrap_tiles` gave up on stayed one flat
colour, flat front lenses were baked as plates, and only front lenses looked along the axis.

**Tiles laid per triangle (offline-checked).** `unwrap_tiles` needed one UV per vertex, so a mesh
continuous across a tile edge (a vertex in triangles of two tiles) or one with more than 10% of its
triangles across an edge stayed `_tiling_swatch`'s one colour. Now each triangle is laid from the
tile its UV centroid is in (`TileWrapped.triangle`, `dsf_materials.triangle_uv`), and the tile goes
into the vertex tag (`Texturing.triangle_uvs`), so a vertex on the edge gets a UV in each tile. What
lies past the tile is clamped into it; a part is unwrapped when that is at most `TILE_CROSS_SHARE`
(25%) of its UV area (`_outside_share`, sampled at the centres of 64 equal sub-triangles; the KIA
Forte Koup's bowls 0.4%, the Mini JCW's reflector 0.9%, the Shelby GT500 13's boot tail lamp 14%;
`out/l6/tile_area.py`). Every lamp consumer reads the per-triangle UVs (the lens bake's surfaces and
samples, `lamp_glow`, the lamp plan's `texel_sampler`, `daytime_mask`, `plan()`).

**Streak parts.** The Shelby GT500 13's tail strips (`taillightst`) repeat a red band 187 times along
u (u -2.5 to 185, v 0.91 to 1.0), and every triangle spans several tiles. Forza's sampler shows the
lowest mip there: the band's rows. A part whose triangles' median span is a tile or more along one
axis and that fits one tile along the other is laid on one texel column (`_streak_value`: the column
closest to each row's mean over the whole atlas; rows 460 to 511 of the Shelby's atlas run from a
dark edge (30) to red (176)).

**Flat front lenses (offline-checked).** A front lens of one flat colour (`lenses._flat`) stays (its
clear texels carry the day glow, `lens_glow`), but is baked as a filter over what lies behind it
(`lens_filter`, as a swatch lens's), and glows with the lamp behind it as a cover does, not with its
own flat alpha. Before, `a x lens + (1 - a) x behind` drew it as a flat plate at its opacity.

**Rear axis rays.** `_axis_fallback` now runs for rear lenses too, along -z. Only for a lens that faces
out of the car's end (`AXIS_MIN_FACING` 0.25, the mean z of its normals; the Panoz Esperante's
laid-back head lamp lens 0.59, the Maserati MC12's tails 0.57), and only its outward-facing points. A
per-ray guard first tried lost the Fiat 595's round rear lamps' bulb: they are glass balls (mean 0.0)
whose texels the front indicators share. Side markers and domes (the Dino 246's tails, 0.2) keep the
rays along their normal, as on master.

**Roster (offline; `out/l6/bake_survey.py`, `Texturing.plan` with the lens bake on LODs 1, 3, 5, master
`out/l6/bk_master` against this change `out/l6/bk_branch3`, `bake_compare.py`; 566 cars):**

| | master | this change |
| --- | --- | --- |
| tiling lamp parts as one flat colour, LODs 1/3/5 | 68 subsections on 34 cars | 9 on 6 (LOD 5 parts with few large triangles) |
| of them at LOD 1 (the exterior entry) | 6 on 3 (Shelby, Forte, JCW) | 0 |
| streak parts laid on one column | | 7 (Shelby GT500 13, LODs 1/3/5) |
| flat front lenses baked as a filter, LOD 1 | | 48 subsections on 27 cars |
| rear lens rays along the axis | | 37,032 rays on 328 cars |
| front lens rays along the axis (side-facing lenses no longer) | 85,468 on 262 (#190's count) | 51,082 on 207 |
| flat rear lenses left out (two more meet a lamp along the axis) | 253 | 255 (MCL_MP412C_11's clear tail lenses) |
| cars whose baked Lights texels change (4x4 blocks, more than 8 in a channel) | | 274 |

In game (confirmed-runtime; `-Views 'rear,nose,rear+night,nose+night' -Palette 0 -ShotSeconds 3`, vanilla 241
first; master's lamp code `out/l6/before1` / `before2`, this change `out/l6/after1` / `after2`):

- Forte Koup (VM 2, `vm2-20261029-061539` / `-062957`): the head lamp bowls show their chrome reflector and
  bulb instead of one flat tan colour (`img/lamps6-forte-headlamps-ingame.jpg`).
- Shelby GT500 13 (same runs): the tail strips read as red bands with a darker edge; master's car was hit by
  traffic before its rear views (`img/lamps6-shelby-tails-ingame.jpg`).
- Ferrari 599 (VM 1, `vm1-20261029-081043` / `-082631`): its flat amber head lamp lenses show the reflector
  behind them, tinted amber, instead of an orange plate (`img/lamps6-f599-headlamps-ingame.jpg`). The Audi
  Q7's flat bumper lens and the Mini JCW's reflector look as on master by day and in the dark mood.
- Panoz Esperante #51 and Maserati MC12 (VM 2, `vm2-20261029-081228` / `-083320`): the yellow lexan covers
  stay yellow with the bowls inside; the MC12's round tails as on master with a little more ring detail.
  The Ford GT GTR's yellow covers show two projectors in a black housing (branch only: master's views
  fell in the Shift effect; `img/lamps6-fordgtr-headlamps-ingame.jpg`).
- Earlier runs of the same pairs stopped at "Story world not ready" on both VMs (`vm2-20261029-071914`,
  `vm1-20261029-071930`, a busy host); the reruns above loaded.

**Panoz Esperante head lamps by day (after #190's axis rays).** A report had them close to a white
glow. Built (`out/l6/front_emission.py`: the full entry from straight ahead, 4 mm, the day-lit types'
alpha / 255 x type scale x area): day-lit emission 652 over 1,113 cm2, glow alpha mean 15, lamp texels'
brightest channel median 138. Vanilla (#167): emission p75 311, p90 1254; vanilla 241's 0x1D lamps
2355 at alpha mean 95, vanilla 198's 1658 at 79. The Panoz is inside vanilla's range, and per unit of
area it glows far less than 241 (its lamps are large: the area is above vanilla's p90 of 454 cm2). In
game (VM 2, `vm2-20261029-073325`, `-Views 'nose,nose+night,nose'`, vanilla 241 and 151 first;
`img/lamps6-panoz-day-ingame.jpg`) its two bowls show as white discs by day, as 241's lamp reads white
with its flutes: the white is Forza's flat foil reflector (208, 208, 208) in sun under a lens at
opacity 0.26, brought to vanilla's median brightness by `white_lens_scale`, not the glow. Left as it is.

### Rear lamps short of a rear entry (issue #146)

Symptom (offline-checked): after #144, lamps still took a host light entry at the other end of the car
when the host had fewer entries at their end than the car has lamps there, and broke with that end's
panels ("Lamps broke with the wrong end", "Tail lamps with the broken-lamp pattern").

Change (`LampPlan.assign`, `lights.SIDELESS`):

- A lamp with no free entry at its end shares the entry of the same function on the other side at its
  end when the table type has no side (head, position, tail, brake, reverse, fog): the right reverse lamp
  lights on the left one's entry rather than take a front one. Indicators and the combined types with one
  are per side and can't.
- A fog lamp never takes an entry at the other end. No game script sets FogLights (the decompiled Lua sets
  only Siren through `setLights`), so there it would only break with that end's panel; it stays unlit
  (its texels draw as before, with no light byte).

Roster (offline, `out/l6/lamp_survey.py` in the #189 worktree, the lamp plan on each car's host-picks host,
entry places from the vanilla tables; 566 cars):

| Lamps on an entry at the other end | master | this change |
| --- | ---: | ---: |
| all | 88 on 52 cars | 37 on 20 cars |
| rear reverse | 45 | 29 |
| rear / front fog | 29 / 5 | 0 (unlit) |
| rear indicators, DRL indicators | 3, 2 | 3, 2 |
| rear tail, rear brake | 1, 1 | 0, 1 |
| front reverse, front indicator | 1, 1 | 1, 1 |

Unlit lamps 53 -> 76: the 34 fog lamps, less the ones that now share a twin. The 29 reverse lamps left are
pairs (or centre lamps) on hosts with no rear entry left for either side, e.g. HON_S2000_03 and LEX_ISF_09
on 274, AUD_RS4_06, the C5 and C6 Corvettes' centre reverse lamps. Sharing them with a red lamp would light
them red, and leaving them unlit would lose the reverse light, so they keep the front entry. They
stay open in #146; the fix would be hosts with more rear entries (`host_select`). Not checked in game
on the cars it changes (#220's and #223's runs include it).

### Tail lamps that brake and indicate: lindtailside (issue #103)

Symptom (offline-checked): the Corvette ZR1 09's four round tail lamps never braked; of its 883 cm2 of
visible red rear lamp at LOD 3 only 59 did (a small `tail_light` and the high stop lamp).

Cause (confirmed-static): they are `lindtailside`/`rindtailside`, which the converter took as DaytimeRunning
+ indicator (`DRL_IND`, 0x14/0x15). The technique is in FH1's `shaders_v16.fxobj` but not in the shared
`ShaderSettings/Normal.xml`, so its defaults are unknown. 12 cars set it in their own `ShaderSettings.xml`,
and 10 of them give it a `BrakeColor` (Camaro ZL1 12 3.0, Charger R/T 69 2.2, Corvette Stingray 67 1.8,
Cuda 426 71 1.5, Explorer 10 1.0, the F-350 08's right lamp 1.5...); the Express 2500 and the F-350's left
lamp set only a `TailLightColor`. So the lamp is a tail, brake and indicator lamp in one, as on the real C6
ZR1.

Change (`lights.TECHNIQUE_FUNCTION`): both are `BRAKE_IND` (0x19/0x1A: DaytimeRunning + Brakes + left or
right indicator, as on vanilla US-style cars), like `brinl`/`brinr`. 16 roster cars use them.

Roster (offline, `out/l6/lamp_survey.py` in the #189 worktree: the lamp plan on each car's host-picks host,
an orthographic view from behind at LOD 3, 5 mm, visible lamp pixels by the type of their entry and whether
the baked texel is red; 566 cars). The visible red area that brakes changes on 7 cars:

| Car | master: brakes / red that doesn't (cm2) | this change |
| --- | --- | --- |
| CHE_CorvetteZR1_09 | 59 / 824 | 883 / 0 |
| FOR_Explorer_10 | 345 / 227 | 571 / 0 |
| FOR_SVTRaptor_12 | 48 / 402 | 295 / 155 |
| FOR_F350SuperDuty_08 | 429 / 486 | 658 / 256 |
| DOD_Durango_99 | 468 / 298 | 519 / 247 |
| cad_ctsvcoupe_11, FOR_FocusZX4_05 | within 10 cm2 | |

The other users draw them where the view from behind doesn't see them, or under red lenses that already
brake. Cars with less than half of their visible red lamp area braking: 46 -> 43 by this survey's red test
(red minus the larger of green and blue at least 40 on the baked texel; stricter than #104's, so the count
is higher). Of the 43, 32 have red texels lit as another lamp (reverse or indicator lenses on red texels,
the M3 E92's 0x03 `drlred` tails, rear fog lamps), 10 red Lights texels with no lamp (the NASCARs' decal
lamps, unlit reflectors), 1 an unlit lamp.

In game (VM 1, `-Views 'rear,nose,rear+night,nose+night,rear+hitfront' -Palette 0 -ShotSeconds 3`, vanilla 241
first; master's lamp code `out/l6/before3` run `vm1-20261029-073318`, this change with #174 and #146
`out/l6/after3` run `vm1-20261029-084829`): at rest, with the brakes on as the player's car always has them,
the ZR1's four round lamps and the Explorer's tails glow as on master (`img/lamps6-zr1-tails-dark-ingame.jpg`):
master lit them at full as DaytimeRunning lamps whatever the brakes did. The difference is on the throttle
(0x19/0x1A dim to a tail lamp's 0.2 off the brakes), which these runs don't drive; not checked in game.

### Clear covers over reflectors with no lamp (issue #174)

Symptom (confirmed-runtime, #175's run `vm1-20261028-153451`): the VW GTI VR6's bumper fog lamps glowed with its head lamps, by
day and in the dark mood. In Forza they never glow: `bumperFa` holds `lights_glass` covers over a
`reflector` and a `textured_reflector`, with no lamp technique.

Cause (confirmed-static): `LampPlan._place_clear` lit a clear lens, a cover baked as a lens and a rear
reflector with the nearest lamp at its end within 0.35 m. The GTI's head lamp in `headlightL/R` was 0.29 m
from the bumper covers.

Change (`_place_clear`; pieces are triangles joined by their corners, `_pieces`):

- A rear reflector piece lights with the lamps whose triangles lie inside its outline along z (its convex
  hull: a bowl often has a hole where the bulb sits), within 0.35 m either way, each triangle with the
  nearest of them.
- A lens or cover triangle lights with the lamp surface right behind it (inside its outline along z, up to
  0.35 m deep); the rest of its piece with the nearest of the lamps the piece has behind it. Front
  reflector bowls of a lamp unit (by the reflector rule) count as what lies behind a lens, though they stay
  unlit themselves: the Alfa Brera's lens over three projector bowls with one bulb lights over all three.
- A piece with nothing behind or inside it lights with the nearest lamp of its own Forza section (the GTI's
  small reflector cups beside its tail bulbs); a lens or cover piece, else with a lamp whose outline overlaps
  its own (the Dodge SRT-4's lens wrapped round its bulb); else it stays unlit.
- Head lamp triangles made from reflectors or covers (`_headlight_reflectors`, `_bare_head_units`) now carry
  their corners, so a cover finds them behind it (the BMW E36 M3's head lamp units).

Roster (offline, `out/l6/lamp_survey.py` in the #189 worktree, master's `lights.py` against this one on the
same texturing plan; LOD 1 triangles of clear lenses, covers baked as lenses and reflectors; 565 cars):

| | master | this change |
| --- | ---: | ---: |
| cover triangles lit / unlit | 32,258 / 136 | 31,830 / 564 |
| lens triangles lit / unlit | 181,263 / 4,395 | 179,850 / 5,808 |
| reflector triangles lit / unlit (front reflectors are never lit) | 231,160 / 223,975 | 228,525 / 226,610 |
| subsections that lose light | | 131 on 119 cars (6,062 triangles) |
| subsections that gain light | | 100 on 52 cars (1,586) |
| subsections lighting with another lamp, as many triangles lit | | 906 on 334 cars |

The losses are 83 reflectors (rear bumper retro-reflectors with no bulb: Z4 M Coupe, Zonda Cinque, MC12s,
V12 Vantage, R8 V10; red reflector bands and lower lamps with no lamp technique: Camaro Z28 70, Cobra 427),
43 lenses and 5 covers over reflectors with no lamp (the GTI's and RS2's bumper covers, the 205 T16
ShowCase's bumper lenses, the Saab 99's wrap-around corner lenses). Forza lights none of them: emission comes
from the lamp techniques only. The gains and moves are lenses over a lamp other than the nearest centre
(the M3 E30's tail reflectors now light with the tail lamp around them rather than the reverse lamp's
centre).

In game (VM 1, `-Views 'rear,nose,rear+night,nose+night,rear+hitfront' -Palette 0 -ShotSeconds 3`, vanilla
241 first; master's lamp code `out/l6/before3` run `vm1-20261029-073318`, this change with #146 and #103
`out/l6/after3` run `vm1-20261029-084829`): the GTI VR6's bumper fog lamps no longer glow in the dark mood;
its head lamps do as before (`img/lamps6-gtivr6-bumper-dark-ingame.jpg`).

### Red texels lit as reverse and indicator lamps (issue #103)

Symptom (offline-checked): after #220, 43 cars braked with under half of their visible red rear lamp area
(#103's recheck); 32 of them had red texels lit as reverse or indicator lamps.

Cause (confirmed-static): Forza draws reverse and indicator bulbs, the reflector bowls round them and the
lenses and covers over them on red texels as often as on white or amber ones: the Mercedes S65 AMG's lower
tail lamp halves are a red `textured_reflector` round its reverse and indicator strips, the Lexus IS F's
boot lid lamps a red bowl round its reverse and fog bulbs, half the Koenigsegg Agera's ring is indicator
and its lower lamp reverse, half the Ford Raptor's tail lamp is reverse, the Dodge Durango's
`lights_gls_noemit` covers are red over both. The lamp plan gave each such triangle the lamp it lies over
or round (#174), whatever it is drawn with. A lamp glows in its entry's colour (`texel.a x colour[b2]`,
"How a lamp lights"), so the red lit white-pink when reversing (the IS F's whole inner lamp, run
`vm1-20261029-142330`) or amber when indicating, and never braked. Vanilla draws its reverse lamps white
and its indicators amber, and lights its red rear lamp texels with the tail and brake lamps (0x18, 0x02,
0x19/0x1A).

Change (`LampPlan.build` `seen`, `_place_clear`; `forza2dsf.convert` passes `lights.seen_sampler` over the
lens bake's atlas, `Texturing.atlases`):

- Every rear lamp triangle, lens, cover and reflector gets the texel class of what it is drawn with: the
  mean of its baked texels, alpha aside (the alpha is the glow mask). The mean, as the eye averages a
  lamp's pattern: most texels of the Raptor's red bowls are pale highlights (a majority test kept them
  white), while a triangle across its white reverse window and the red is red on the whole.
- After the plan is placed, a rear indicator lamp drawn at least 90% red (`RED_INDICATOR_SHARE`, over all
  its triangles, lenses included) brakes and indicates (BRAKE_IND, 0x19/0x1A), as vanilla's US-style cars
  and `_red_lens_indicators`' lenses do: the Durango, the Infiniti IPL G coupe, the Tour bus. The
  indicators left keep cluster 0 first (`assign` ranks second clusters last: the Sierra RS500's main amber
  indicator lost its entry when its red side lamp, cluster 0, went).
- Then every rear triangle drawn red on a lamp that isn't red (reverse, indicator) moves to the nearest red
  lamp among those behind or inside its piece, else of its section, else the red lamp with a triangle
  within 0.35 m; else it stays unlit.
- Lenses choose the lamp behind them by what they lie over before any of this (a reflector counts as the
  lamp it surrounds): the Honda Integra Type R Mugen's and VW Golf R's white lenses over red-drawn reverse
  bulbs and the Pontiac Trans Am 02's white lens over a red bowl stay reverse lamps; the Countach's amber
  lenses over red-drawn indicator bulbs stay indicators. Tried first: turning red-drawn reverse bulbs into
  tail lamps before the lenses were placed lost the Golf R's reverse lamp (284 cm2) and most of the
  Integra's; deciding red indicators on the bulbs alone made the Countach's amber lenses brake red, and a
  50% share the S65 AMG's clear indicator strip.

Roster (offline, `out/l7/lamp_survey.py` in this PR's worktree: master's and this `lights.py` on one
`Texturing.plan` per car, each car's host-picks host, an orthographic view from behind at LOD 3, 5 mm,
visible Lights pixels by the type of their entry and the class of their triangle's mean baked texel;
565 cars, NIS_Leaf_11 needs the gamedb the survey doesn't pass):

| | master | this change |
| --- | ---: | ---: |
| cars braking with under half of their visible red | 34 | 19 |
| visible red that brakes | 325,711 cm2 | 351,869 cm2 |
| visible red on reverse entries (cars over 10 cm2) | 19,701 cm2 (149) | 0 |
| visible red on indicator-only entries (cars over 10 cm2) | 6,694 cm2 (44) | 0 |
| visible white on reverse entries | 59,304 cm2 | 59,304 cm2 |
| visible amber on indicator entries | 18,931 cm2 | 18,931 cm2 |
| cars whose braking red area changes by over 10 cm2 | | 161 |
| rear indicators that become red brake-indicators | | 6 cars |

This survey's red test is the triangle's mean texel; #220's (each pixel's texel, red minus the larger of
green and blue at least 40) counted 43 on master. The 19 left: unlit rear retro-reflectors and NASCAR decal
lamps (3,538 cm2; Forza lights neither, #174), red fog lamps (495), the BMW M3 E92's `drlred` tails (0x03,
414; vanilla has 93 red 0x03 tail lamps), the Dodge Stealth Turbo's full-width reflector panel, and
TOY_6_SC430_08, which has no lamp technique at LOD 3 (Open 10).

In game (VM 1, `-Lights -LightsDriveOnly`: idle, throttle, brake, brake into reverse, by day and in the dark
mood; vanilla 241 first, then the S65 AMG, Raptor, IS F and Durango; master's lamp code `out/l7/before`
against this change `out/l7/after`). The final code `vm2-20261029-155636` /
`vm2-20261029-160154` (`-Palette 0`); an earlier form of the change `vm1-20261029-142330` / `-142909`. On the throttle the tails are dark on both. Braking, the S65 AMG's whole tail
lamp lights where master lit its upper rows only, and the IS F's lamps light further in. Reversing,
master's IS F lit its whole inner lamp white-pink, this change only its white reverse window; the Durango
likewise (`img/lamps7-s65-brake-reverse-ingame.jpg`, `img/lamps7-isf-brake-reverse-ingame.jpg`; the offline
plan `img/lamps7-red-texels-plan.jpg`).

### A rear entry freed by a left and right pair (issue #146)

Symptom (offline-checked): after #219, rear reverse lamps on hosts with no rear entry left took a front one,
and broke with the front (16 rear reverse lamps; the Honda S2000 03 and Mazda RX-7 97 on host 278, the Lexus
IS F on 176, the Ford Focus RS500 on 274, the AMC Javelin AMX's centre reverse lamp on 161). On hosts with no
free entry at either end they stayed unlit: the BMW 2002 Turbo's, Nissan 510's, VW Beetle 68's and Alfa
GTA's reverse lamps, the Mazda Miata 94's, Lotus Elise 99's and Shelby Series 1's right rear indicator.

Cause (confirmed-static): `LampPlan.assign` gives every lamp its own entry while there are some at its end.
A left and a right lamp of a type with no side (tail 0x18, brake 0x02, reverse, position, fog) each took one,
so the tails used two of the host's four or five rear entries, and the reverse lamps that came after had
none.

Change (`assign`, `pair_entry`): a rear lamp with no entry left at its end and no lamp to share one with
there (its twin, or its fallback's) frees one. A left and a right rear lamp of one SIDELESS function (head
lamps aside) that hold an entry each share the left one's, and the right one's goes to the lamp; the least
important such pair (PRIORITY: brake lamps before tails), else as before an entry at the other end. The two
keep their type and light as before; the right one breaks with the left one's record, which on vanilla
hosts hangs under the same part (the body or boot).

Roster (offline, `out/l7/lamp_survey.py` in this PR's worktree, master's and this `lights.py` on one
`Texturing.plan` per car, each car's host-picks host; only cars with a lamp on an entry at the other end
or a rear lamp unlit for want of an entry can change: 26 cars):

| | master | this change |
| --- | ---: | ---: |
| lamps on their own entry at the other end | 22 on 19 cars | 6 on 4 cars |
| of them rear reverse / rear indicators / rear brake | 16 / 3 / 1 | 2 / 2 / 0 |
| front reverse, front indicator (hosts short at the front) | 1, 1 | 1, 1 |
| rear reverse and indicator lamps unlit (no entry anywhere) | 15 on 7 cars | 5 on 3 cars |

(#219 counted 37 on 20 cars: lamps sharing an entry at the other end too; this survey counts each entry
once.) Left: the Chevrolet Express's left reverse lamp and the Mustang Boss 429's front right indicator
(hosts with too few entries at the front: front lamps aren't paired), the Corvette Grand Sport 96's centre
reverse lamp and the Lexus SC430 02's rear indicators and left reverse lamp on host 161 (two rear entries:
one pair frees one entry), and the Miata 94's, Elise's and Series 1's reverse lamps (their freed entry goes to
the right indicator, which ranks higher).

In game (VM, `-Lights -LightsDriveOnly -Palette 0`, vanilla 241 first, then the S2000, Javelin AMX, 2002
Turbo and Miata 94; master's lamp code `out/l7/before` against this change `out/l7/after`): master `vm1-20261029-162920`, this change `vm1-20261029-163437`. The 2002 Turbo, which had no reverse light, lights white reverse lamps (`img/lamps7-2002turbo-reverse-ingame.jpg`); the S2000 and Javelin look the same (their reverse lamps lit on master too, from a front entry). A lamp breaking with its new end (`+hitfront`, `+hitrear`) is not checked in game.

### Coarse LOD lamp parts drifting over a tile edge (issue #229)

Symptom (offline-checked): after #218, 9 tiling lamp subsections on 7 cars stayed one flat colour
(`_tiling_swatch`): the Dodge Viper SRT-10 03's LOD 5 reflector, the Ferrari F50's LOD 3 `oldhead` bumper
lamps (left and right), the Mazda 2's (`MAZ_16_B09_09`, `_10`) LOD 5 tail reflectors, the Mazdaspeed3 10's
LOD 5 head lamp reflectors and the Nissan 510's LOD 5 `tail_light` (`out/l7/lamp_survey.py`, which records
each `_tiling_swatch` call; #218 counted 9 on 6 and all at LOD 5). The F50's is drawn in the full entry
(mask 4, traffic's nearest LOD).

Cause (confirmed-static): none of them tiles. Their finer LODs lie inside tile 0 (the Viper's u 0.01 to
0.84, the Mazda 2's 0.02 to 0.98, the 510's 0.01 to 0.96); decimated, the coarse copy's UVs drift over the
edge (the Viper's LOD 5: u -0.06 to 0.15; the F50's LOD 3: u 0.8 to 2.6, v -0.43 to 0.97), and 27 to 41% of
their UV area lies outside each triangle's tile, over `TILE_CROSS_SHARE` (25%).

Change (`dsf_materials.unwrap_tiles`, `_finer_in_a_tile`): a part whose finer LODs (same section and
technique, a lower LOD) lie in one tile, every triangle inside it, is laid triangle by triangle whatever it
clamps, as the finer LODs are. Its texels don't count as ones other parts sample in the lens bake
(`TileWrapped.drifted`, `lenses.bake`): the F50's LOD 3 part's drifted UVs land on its left tail lens's
texels, which then went unbaked (18,468 to 13,645 texels) and changed the exterior's tail lamps.

Roster: the 9 subsections on 7 cars are laid textured; no other car can change (the rule only runs for
parts `_tiling_swatch` took). In the F50's atlas the two Lights regions merge into one and the pack scale
goes from 0.518 to 0.516.

Checked offline only (the parts are drawn far away or by traffic):
- `render_compare run --entries full` on master's and this build (`out/rc/full-before`, `full-after`):
  the F50's bumper head lamp at LOD 3 shows its reflector as Forza's does, where master drew it flat grey
  (`img/lamps7-f50-lod3-bumper-lamp.jpg`: master, this change, Forza). Its amber half is dark brown on both
  (not this change).
- `render_compare diff` of the exterior (`out/rc/lamps7-229`): 3 of the 5 cars drawn differently, all
  resampling of the repacked atlas (the F50's and Viper's front badges 45% and 16% of the region's pixels,
  the Mazda 2's side markers 19%), alike to the eye; scores unchanged (F50 2.525 to 2.524).

Not done: swatch lenses that are only partly scrambled (the issue's first item). Recounted on the roster
after #190 and #218 (`lens_report` "scrambled", lenses not dropped): most are a few texels on a clear front
lens (Spark 9, Genesis Coupe 8, Volvo 242 1, Forte Koup 6); the only sizeable ones are the Viper CC's left
head lamp lens (343 of about 1,000 texels) and the Scirocco R's tail lenses (39 of about 1,400). The lens
texels that no lamp lies behind (Venom GT's and Noble M600's lexan, the MP4-12C's wing lens) are scrambled
with nothing to bake, so they keep their own texels as before.
### Head lamps white plates by day: Volvo S60 11, Venom GT 12, Boss 429 (issue #250)

Symptom (offline-checked, `dsff render_compare` roster run on master ede81a1): after #167 two ports'
head lamps still glowed as white plates with the running lights on: `VOL_S60_11` (glow p95 7.7x a full
0x01 lamp texel) and `HEN_VenomGT_12` (6.1x); `FOR_MustangBOSS429_70` was at the limit (3.1 in one
view).

Cause (confirmed-static, `out/l8/plandiag.py` and `glowdiag.py` in the t3-a7c21235 worktree). Three
ways a day-lit (0x1D) lamp's texels kept their full alpha:

- **The DSF texel, not the source texel.** `Texturing.daytime_mask` marked the source (Forza) texels
  inside each 0x1D triangle's UVs, else its centre's; `render` averages a box of source texels into
  each DSF texel (2 x 2 at the S60's scale 0.40). 79 of the S60's 437 head lamp reflector triangles
  are slivers whose UVs fall on one texel of alpha 255 (u 0.835, v 0.301): one marked source texel
  in a box of unmarked ones, so the DSF texel kept 0.8 to 1 of its alpha. Built: those triangles
  sampled alpha 255 by day; 0.2 of it is 51.
- **A clear swatch lens kept as an opaque plate.** The Venom's `lexanLHL/RHL` `detail_glass_clear`
  covers the whole head lamp unit with 71 triangles on a flat grey swatch (every texel scrambled,
  `lenses._scrambled`). A swatch lens is left out with its tint on the lamp behind when half its rays
  meet a lamp; these rays meet the projectors' housing more than the projectors, so the lens stayed
  drawn as its own Lights texel (136, 137, 136, converted alpha 155): an opaque grey plate over the
  lamp by day (Forza draws it see-through), lit by day as 0x1D. Its swatch texel is shared with the
  indicator part of the lens, and was averaged with its unmarked neighbours as above.
- **A lamp on a head lamp's entry.** The Boss 429's amber `slorange` side markers (position lamps)
  share the left head lamp's 0x1D entry when host 193 has no entry left for them (#146's fallback).
  `LampPlan.is_daytime` asked the lamp's own type (0x03), so their texels (alpha up to 254) glowed at
  0x1D's level (p95 9.9).

Change:

- `Texturing.daytime_mask(sections, daytime, lit)` (`_dsf_boxes`): besides the texels inside each
  0x1D triangle's UVs, every source texel under the DSF texels it samples (its DSF UVs through its
  region's mapping; for a triangle covering 4 DSF texels or fewer, the 2 x 2 a bilinear sample at
  its centre reads), less the source texels that other lamps' triangles sample (`lit`:
  `LampPlan.light_byte`). Unlit triangles don't glow (a lamp glows by its texel alpha only), so the
  wider mask changes nothing else.
- `lenses.bake`: a clear swatch lens (`texel_class` white) is left out, its tint on the Lights
  surfaces its rays meet, when any ray meets a lamp; coloured ones keep the half-the-rays rule (a red
  lens over a small bulb is the tail lamp surface).
- `LampPlan.is_daytime` takes the type of the entry the lamp lights with (its owner in `slots`).

Roster (offline, `out/l8/day_survey.py`: `Texturing.plan` at LODs 1, 3, 5 and the lamp plan on each
car's host-picks host, set_daytime's mask, the rendered DSF atlas; per car the 0x1D triangles of LOD 1
(chosen by their entry's type both times) and the DSF texels each samples; 566 cars, 514 with day-lit
head lamps; master 62999f2 against this change):

| | master | this change |
| --- | ---: | ---: |
| cars with over 1 cm2 of day-lit lamp sampling alpha over 60 (0.2 x 255 = 51) | 93 | 21 |
| cars whose area-weighted p95 alpha is over 60 | 9 | 1 |
| per-car p95 alpha, p10 / 50 / 90 | 24 / 35 / 45 | 23 / 34 / 44 |
| per-car day emission (sum area x alpha / 255 x 10, cm2), p10 / 50 / 90 | 85 / 410 / 1226 | 79 / 392 / 1147 |
| cars whose emission changes by over 5% or p95 by over 5 | | 125 |

Largest changes (p95 alpha; area over 60 cm2): RUF_Yellowbird_87 184 -> 37 (692 -> 0), the Venom 124 ->
9 (618 -> 0), REN_5Turbo_80 57 -> 44 (191 -> 0), CHE_Express2500Cargo_05 91 -> 18 (167 -> 0, a lamp on a
head lamp's entry), the S60 153 -> 51 (156 -> 4), SAT_Ion_06 66 -> 23. Lenses newly left out: the
Venom's lexan, and the clear rear swatch lenses of ACU_TSX_10, SUZ_SX4_11, VW_Fox_11 (`glassLTL/RTL`)
and MCL_MP412C_11 (`winga`), over their reverse and tail lamps. Left over 60: VW_RabbitGTI_84 (p95 82;
its `oldhead` mean 43).

`render_compare diff` (master 1481ae6 against this change, 15 cars, `out/rc/l8-250` in the t3-a7c21235
worktree): 14 cars' data change, 2 draw differently (the Venom's front lamps 99 to 100% of the region
in every mode, score 16.1 -> 0.7; the Boss 429's front lamps 22% lit by day and dark, 6.0 -> 2.9). The
S60's white plate flag goes (12.0 -> 6.0) with under the comparison's 6 delta E changed: its slivers
are a few pixels. No region but the front lamps changes.

In game (VM 2, `-Views 'nose,nose+night' -Palette 0`, vanilla 241 first, then the S60, Venom and Boss
429; master `vm2-20261029-195645`, this change `vm2-20261029-200139`;
`img/lamps8-venom-ingame.jpg`, `img/lamps8-s60-ingame.jpg`): by day the Venom's head lamps show their
projectors under the lens instead of two white plates; the S60's lamp edge is less white. In the dark
mood the Venom glowed from its two projectors only (render p95 0.35 against master's 6.1).

That was a fourth fault: the left-out lexan stayed in the front lamp view (`Texturing._lamp_view`,
`lamp_tris`), so it hid the head lamps from `day_glow` and `visible_white`, and they kept their dim
converted alpha (median 20, 4 by day) and a white scale set by the grey lexan (0.66). Lenses left out
(`dropped_lenses`) now stay out of the front views (the rear view, `visible_red`, is unchanged: the
left-out rear lenses of the 98 cars with a lens left out still count there, open). Cars with a front lens left out: the Venom, BMW
M635CSi 84, Dodge Viper CC 03, Hyundai ix20 11 (`out/l8/day_survey.py`, `wscale.py`):

| Car | 0x1D lamp mean alpha (LOD 1) | day emission, cm2 | white scale |
| --- | --- | ---: | --- |
| HEN_VenomGT_12 | `head_light` 3.5 -> 12.1, inner lens 5.0 -> 7.7, `hidhead` 1.9 -> 0 | 59 -> 85 | 0.66 -> 0.99 |
| DOD_ViperCC_03 | `headlightL/R` 17.7 -> 19.3, bumper lamps 18.7 -> 17.7 | 974 -> 986 | none (too few samples) |
| HYU_ix20_11 | 15.2 -> 15.3 | 190 -> 191 | 1.0 |
| BMW_M635CSi_84 | unchanged | 353 | 1.0 |

The Venom's head lamps now glow as the other ports' 0x1D lamps do: render_compare's renderer, front
view, dark mood, p95 1.37 over 1,417 lamp pixels (the other ports about 1.65; master 6.1 over 5,439,
the lexan plate that was white by day). Its `hidhead` projectors stay dark: their few visible samples
sit in `day_glow`'s faded rim. Master's night level came from the same unscaled alpha as its day
plate; a 0x1D lamp has one alpha for both, and #167 sets it at a fifth by day and at night (#286).
In game (VM 1, the S60 and Venom rebuilt, run `vm1-20261029-211107`; `img/lamps8-venom-ingame2.jpg`,
rows master, this change before the view fix, with it): by day the projectors show, a little lighter;
at night they glow brighter than before the view fix, far below master's lit plate.

### Head lamp techniques at the rear (issue #264)

Symptom (offline-checked, the seventh pass's `out/l7/lamp_survey.py`): on 9 cars the lamp plan made
head lamps (0x01) at the rear, from a Forza head lamp technique on rear parts, and gave them rear light
entries taken from the real rear lamps: 13 rear head lamps, all with an entry. The 510's (host 130)
took two of four rear entries and left its reverse lamps without one (#261 later freed one by
pairing); every one lit white at the rear with the head lamps.

| Car | What |
| --- | --- |
| NIS_510_70 | `body` `head_light` LOD 2 (its tail lamps at that LOD) |
| AUD_A4Avant_04 | `taillightR` `fullbeam`, LODs 1 to 5, red-drawn (#257 moved its triangles to the tail lamps; the empty head lamps kept their two entries) |
| AUS_MiniCooperS_65, AUS_ShowCaseMiniCooperS_65 | `trunk` `oldhead`, LODs 1 to 4, on the centre line of the boot: the plate lamp |
| MCL_43_McLarenF1GTR_97 | `body` `head_light` LOD 1, 16 triangles |
| TOY_Camry_07 | `taillightR` `oldhead` and its reflector, LOD 4 |
| TOY_CorollaDX_95 | `glassRTL` `lights_gls_oldhead`, LOD 2 |
| PON_GTOJudge_69 | `body` `oldhead` LOD 4, one triangle |
| CHE_CorvetteZ06_06 | `body` `head_light` LODs 1 and 2, one triangle each |

Vanilla has no rear 0x01 entry and lights no plate lamp: its white rear entries are reverse lamps
(0x08, 257) and sirens (`out/l8/vanilla_rear.py`, every vanilla vehicle).

The same survey found a second way rear entries go to lamps that draw nothing: #257's move of
red-drawn rear triangles to the nearest red lamp can empty a lamp, which `assign` still ranked by the
area measured before the move. 9 lamps on 5 cars held an empty entry: the A4 Avant's two rear head
lamps, and reverse lamps all of whose triangles are drawn red (BMW_M3E30_91's centre one, AUD_RS6_09,
PEU_207RC_07 and PEU_207Super2000_07, two each).

Change (`LampPlan.build`, `assign`):

- A head lamp technique triangle behind the wheelbase midpoint goes with the clear lenses
  (`_place_clear`): it lights with the lamp it lies over or in, else one of its own section within
  reach, else stays unlit.
- `assign` gives entries only to lamps that still have triangles.

Roster (offline, `out/l8/plan_survey.py` in the t3-a7c21235-b worktree: master's `lights.py` and this
one on one `Texturing.plan` per car, host-picks hosts, 566 cars): rear head lamps 13 on 9 cars -> 0;
lamps with an entry and no triangle 9 -> 0. The triangles of exactly those 9 cars change, and the
emptied lamps' entries are free on the other 4:

- the 510's LOD 2 parts light with its tail lamps; the A4 Avant's `fullbeam` with its tail and
  brake + indicator lamps; the Camry's LOD 4 and the Corolla DX's LOD 2 parts with the tail and
  reverse lamps they lie in;
- the Minis' plate lamp, the F1 GTR's 16 triangles and the GTO Judge's one stay unlit (no lamp behind
  them or in their section within reach);
- the Corvette Z06's rear `body` reflectors (24 triangles at LODs 1 and 2) are unlit now: they lit
  with the tail lamp only through the stray head lamp triangle inside them. Unlit rear
  retro-reflectors are the plan's rule (#174).

The emptied-lamp rule ran after that survey; `out/l8/plan_survey.py` on the M3 E30, RS6 09 and 207 RC
shows their emptied reverse lamps without an entry (the 207 Super 2000 shares the 207 RC's layout).

`render_compare diff` (master 1481ae6 against this change, the 15 cars of this pass, `out/rc/l8-264`):
7 cars' data change, 1 draws differently, MCL_43_McLarenF1GTR_97 (`rear_lamps` rear3q dark 6%); the
others change only in light entries or at LODs these views don't draw. Scores unchanged.

In game (VM 1, `-Lights -LightsDriveOnly -Palette 0`, vanilla 241 first, then the A4 Avant and the
Mini 65; master `vm1-20261029-200145`, this change `vm1-20261029-200533`;
`img/lamps8-mini-plate-ingame.jpg`, `img/lamps8-a4-tails-ingame.jpg`): the A4 Avant's tails light and
brake as on master; the Mini's plate area no longer lights white in the dark mood.

### Head lamp covers over a whole head lamp unit (issue #247)

Symptom (offline-checked, `dsff render_compare` roster on master ede81a1): the Audi R8 race car's
(`AUD_2_R8_06`) head lamps glowed at about 4% of Forza's level. Four of the five worst-ranked lamp
cars of that roster also had their head lamps "drawn as glass": FER_458Spider_12, DOD_StealthTurbo_96,
CAD_6_LMP_02 (78 to 100% of the front lamp region) and the R8.

Cause (confirmed-static). Their head lamp covers are plain `glass` in the lamp unit's own section
(`lexanLHL/RHL`, `glassLHL/RHL`, `headlightL/R`) and span the unit's housing as well as its lamp:
`Texturing._over_lamp` puts the R8's lexan at 0.2999 over a lamp, the 458's glass at 0.18 and 0.21,
the Stealth's 0.23, the Cadillac's 0.28. `clear_cover` leaves out a plain glass cover from
`COVER_MIN_SHARE` (0.3, kept that high for the cars that draw windows with the technique), so these
stayed DSF's window glass in front of a 0x1D lamp whose texels glow at a fifth (`DAYTIME_GLOW`).

What vanilla does (offline, `out/l8/vanilla_glow.py` in the t3-a7c21235 worktree: render_compare's
renderer, front view, dark mood, the glow over the pixels whose opaque triangle is a head lamp).
Vanilla draws window glass over its head lamps too, but its 0x1D lamps glow through it at p95 4.6 to
5.2 (241, 130, 198; 0x01 lamps 229, 227, 266: 0.45 to 0.82). Ports whose covers are left out glow at
p95 1.65 to 1.69 (the converter's level for a 0x1D lamp); under the glass the R8 was at 0.38, the
Cadillac 0.90, the 458 1.06. So the glass over these lamps put them at a tenth of vanilla's and a
quarter of the other ports'.

Change (`dsf_materials.LAMP_SECTION`, `COVER_LAMP_SECTION_SHARE`): in a section named for a lamp unit
(`headlight`, `taillight`, `glass`/`lexan` + `HL`/`TL`, with or without the side letter) a plain glass
cover is left out from 5% of its area over a lamp; elsewhere the 30% rule stays.

Roster (offline, `out/l8/plan_survey.py` in the t3-a7c21235-b worktree: `Texturing.plan` at LODs 1,
3, 5 on each car's host-picks host, every plain glass subsection on the Lights atlas at LOD 1 outside
the window sections with a lamp behind; 566 cars): 168 such covers on 88 cars stayed window glass on
master; 156 of them, on 79 cars, are in a lamp unit's section and are now left out (`glassLHL/RHL`
104, `lexanLHL/RHL` 37, `headlightL/R` 8, `glassLTL/RTL` 7 on the Reventons' and Sagaris' tails and the
Prius). The 12 left are outside lamp sections (`body` on the RS6 03 and Insignia VXR, `trunk` on the
Mini 65s and Axela, the Volt's `bumperFa` and mirrors) and stay as before.

Vanilla's covers (offline, `out/l8/vanilla_covers.py`): 241, 130, 198, 229, 227, 266 and 147 all draw
the glass over their head lamps with `GLOBAL_Glass_LOD1` at the flat window texel (UVs u 0.13 to 0.98,
v 0 to 0.12, 75% opaque), the same as these covers on master. Their lamps glow through it because their
texel alpha is high and unscaled (0x1D median 149 to 172 on 241 and 130; 0x01 46 to 123). The ports'
0x1D texels are a fifth of their glow by design (`DAYTIME_GLOW`, #167), so the same glass over them
gives a tenth of vanilla's glow. DSF's shared glass has one other, less opaque patch, the interior
windows' grey smear (u 0 to 0.12, v 0 to 0.06, alpha 0.35, streaked); a cover drawn on it would keep a
reflection, but it is untried on the outside and `render_compare` ignores the glass texture, so only an
in-game run could judge it.

`render_compare diff` (master 1481ae6 against this change; the R8, 458 Spider, Stealth and Cadillac in
`out/rc/l8-247`, 8 more of the 79 in `out/rc/l8-247x`; `img/lamps8-covers-look.jpg`): all 12 draw
differently, front lamps 76 to 100% of the region. Scores 17.4 -> 10.7 (R8), 19.5 -> 15.9 (458), 17.6
-> 12.3 (Stealth), 17.8 -> 12.8 (Cadillac), 9.5 -> 6.9 (DB9), 5.2 -> 2.3 (Gallardo), 18.4 -> 15.4 (GT-R
GT1), 5.0 -> 2.1 (Evora), 3.7 -> 0.8 (E63 AMG), 7.9 -> 2.3 (Golf R), 13.1 -> 5.5 (Prius), 17.8 -> 14.9
(Sagaris). Non-lamp regions that change are the chrome and trims inside the lamp units, seen through
where the cover was: chrome on the R8, Prius, Gallardo, Evora, Golf R (100%), Sagaris (98%), GT-R GT1
(86%), E63 (12%); trims 3 to 47% on all but the GT-R. The reference labels a plain cover's pixels as
lamp, so these come up as "chrome extra" and "trims extra" flags. No hole or gap shows: the housing,
bowls and projectors are what Forza draws through its clear cover. What is lost is the cover's
reflection by day: master's window glass and Forza's clear glass both draw a highlight on the Golf R's,
E63's and DB9's lamps that the bare lamp doesn't.

In game (VM 2, `-Views 'nose,nose+night' -Palette 0`, vanilla 241 first, then the R8, 458 Spider,
Stealth and Cadillac; master `vm2-20261029-195645`, this change `vm2-20261029-200749`;
`img/lamps8-r8-ingame.jpg`, `img/lamps8-stealth-ingame.jpg`, `img/lamps8-cadillac-ingame.jpg`): in the dark mood the R8's and Stealth's
head lamps glow brighter and larger, and so do the Cadillac's; the 458's look about as on master. By day the
lamp units look as on master (the 458's housing a little lighter without the dark glass); no hole or gap.

### Head lamps at a fifth of vanilla at night (issue #286)

Symptom (offline-checked, `out/l8/head_survey.py` in the t3-a7c21235 worktree: render_compare's
renderer, front view, the head lamp pixels' glow in the dark mood): ported head lamps glowed at
night at a third to a fifth of vanilla's. Vanilla's 0x1D head lamps (28 vehicles), under their window
glass: p95 2.5 / 3.8 / 5.3 (p10 / median / p90), lit area 266 / 1227 / 3322 px; vanilla 0x01 lamps
(105): p95 median 0.83. 15 ports (S60, Venom GT, X6 M, Mustang GT 05, Esperante GTLM #51, GTI VR6,
Evo VI, M3 E30, Carlton, Yellowbird, Golf R, E63 AMG, Thunderbird 57, 458 Spider, R8 race car) on
master: p95 1.60 / 1.69 / 1.92. Likely the "painted on" reports.

Cause (confirmed-static). The game lights a 0x1D lamp at its type's 10 by day and in the dark mood
alike ("How a lamp lights"); texel alpha is its only glow input, so one alpha serves both. #167 set it
at a fifth of #124's day glow (`DAYTIME_GLOW` 0.2), against white plates by day, measured against
every vanilla vehicle with a day-lit front lamp, most of which light only a small 0x03 lamp by day.
Vanilla's own 0x1D lamps glow at p95 3.8 (median) by day too, above the white-plate limit #167
calibrated on ports (3.0). In game vanilla 241's lamps by day are bright discs that keep their lens
flutes; 130's are near-white discs.

The other levers, ruled out (`lights.md` "The light table", "How a lamp lights"): no head lamp type
lights at a different level by day and at night. 0x01 is 2 (a fifth of 0x1D) in both; the other
types at 10 are sirens; 0x0A (`DippedHeadlights`) is never used by vanilla and nothing shows the game
setting that mask.

Structure (render_compare's renderer, dark mood, over the lit pixels: the glow's local contrast,
std over 5 x 5 / mean, and the share of lit pixels at 90% of p95 or more): vanilla 0x1D contrast 0.21
/ 0.31 / 0.46, top share 0.11 / 0.17 / 0.23; the 15 ports on master 0.19 / 0.30 / 0.40 and 0.14 /
0.16 / 0.25. So the ports' lamps are already structured like vanilla's; what differs is the level.
Scaling big lamps down by area (tried: a lamp seen over more than 120 cm2 a side scaled by its area)
cut the GTI VR6's, Evo VI's, Carlton's and Esperante's round lamps to p95 0.8; vanilla 241 lights its
big round lamps over 3,416 px at 5.2, so the area rule was dropped.

Change:

- `lenses.day_glow(glow, view, colour)`: before the ranking, each lamp's raw glow is multiplied by its
  visible texels' brightness against their neighbourhood's (`_detail`, over `GLOW_DETAIL_RADIUS` 15
  mm, squared, clipped to 0.25 to 2): the bright parts of the lamp's own picture (flutes, the bulb,
  LED dots) rank high, the smooth stretches between them low.
- `DAYTIME_GLOW` 0.2 -> 0.45: 0.45 puts the 15 ports' median p95 (1.69 at 0.2) at vanilla's 3.8.

Results, 15 ports (render_compare's renderer, dark mood; master 62736c2's lamp code against this):
p95 1.60 / 1.69 / 1.92 -> 2.46 / 3.41 / 4.27 (vanilla 2.5 / 3.8 / 5.3); contrast 0.19 / 0.30 / 0.40 ->
0.18 / 0.28 / 0.39; top share 0.14 / 0.16 / 0.25 -> 0.15 / 0.18 / 0.26. The Golf R's head lamps are
0x01 (it has running lights of its own), unchanged at 1.4 to 1.7, above vanilla's 0x01 cars.

Roster (offline, `out/l8/day_survey.py`: 514 cars with day-lit head lamps, the area-weighted p95 of
the DSF alpha their 0x1D triangles sample at LOD 1, times 10 / 255 x 1.33 to render_compare's p95,
the factor from the 15 rendered ports, correlation 0.57): p10 / 50 / 90 1.18 / 1.78 / 2.29 -> 2.12 /
3.29 / 4.65; cars inside vanilla's p10 to p90 19 -> 391; 27 above 5.3 (at most 6.6, VW_RabbitGTI_84),
96 below 2.5. The median sits about 13% under vanilla's; 0.5 would centre it (an estimated 2.4 / 3.7
/ 5.2).

`render_compare diff` (master 62736c2's lamp code against this, the 15 ports, `out/rc/l8-286`): 15 drawn
differently, all in the front lamps (13 to 80% of the region, most in the dark mood); no other region
changes. Its "white plate by day" flag (#167's glare limit, 3.0) now fires on 11 of them at 3.0 to
4.5. Vanilla's 0x1D lamps measure 2.5 / 3.8 / 5.3 on the same glow, so the limit sits below vanilla's
median; vanilla's p90 (5.3) would be the limit that matches the acceptance here.

In game (VM 1 and 2, `-Views 'nose,nose+night,rear,rear+night' -Palette 0`, vanilla 241 and 130 first;
master `vm1-20261029-230737` / `vm2-20261029-230813`, this change `vm1-20261029-231717` /
`vm2-20261029-231715`; `img/lamps8-headlamps-night-a.jpg`, `-b.jpg`, each with Forza's render of the
same front from the diff): in the dark mood the ports' head lamps glow larger and brighter, as vanilla
241's and 130's do (GTI VR6, M3 E30, Carlton, Thunderbird, X6 M, S60, Yellowbird, R8); the Venom's
projectors and the E63's lamps a little brighter. By day the round and wide lamps (GTI VR6, Evo VI,
Carlton) are whiter than on master, about as bright as vanilla 241's and 130's own day lamps, and keep
their lens pattern; no lamp reads as a flat plate.

Rear lamps at night (the same measure, rear view, red lamp pixels): vanilla, 141 vehicles, p95 0.04 /
0.10 / 0.29; the 15 ports 0.02 / 0.05 / 0.12, over larger areas (the X6 M 2,912 px at 0.04): the same
problem at the rear, #290.

### Tail lamps at half vanilla at night (issue #290)

Symptom (offline-checked, `out/l8/head_survey.py` with `HEAD_REAR`: render_compare's renderer, rear view,
dark mood, the red lamp pixels' glow; the tail lamps at the running lights' 0.2): ported tail lamps
glowed at half vanilla's at their brightest and over larger areas: vanilla, 141 vehicles, p95 0.04 /
0.10 / 0.29 (p10 / median / p90); 17 ports on master 0.025 / 0.048 / 0.12. The BMW X6 M's tails (the
user's "painted on taillights") at 0.045 over 2,912 px.

Cause (confirmed-static). A red lamp texel's alpha is its rank among every red texel of the car
(`red_lamp_curve`, #93) mapped onto vanilla's red alpha. Hidden bulbs and reflectors take the top
ranks, so the surface a camera behind the car sees sat in the middle of vanilla's range. Vanilla's
visible 0x18 tails (56 vehicles, 137,610 pixels, rear view) are alpha p50 49, p90 141, p95 186.

Change (`lenses.tail_glow`, `Texturing._glow`, `_lamp_view(rear_half=True)`; replaced by the ranking per
planned lamp, #391):

- The red lamps seen from behind (the rear half's Lights triangles, lenses left out excluded as from
  the front) are split into lamps, each a connected patch of red samples (`TAIL_MIN_SAMPLES` 40 or
  more). Per lamp, each sample's lamp score times its picture's detail (`_detail`, as #286's)
  makes a pool; every texel of a triangle seen on the lamp is ranked in it by its own score times the
  triangle's detail, onto vanilla's visible tail alpha (`TAIL_RED_ALPHA`). The view samples one texel
  in many; ranking only the sampled ones left 88 to 94% of a tail lamp's texels at their old alpha.
- The dimmest 40% of each lamp stay dark (`TAIL_DARK`): a ported tail is drawn over about 2.5 times
  vanilla's red area (median 7,108 px against 2,867: reflectors, bezels, lens edges), and ranked
  whole 56% of it lit where vanilla lights 26% of its own, an even glow.
- A texel never ends below its converted alpha: tails that already glowed at vanilla's level (the
  GTI VR6's, M3 E30's, Evo VI's) keep it; ranked down they dropped to half.
- Each lamp is ranked on its own, so a car's two sides get the same range (the Panoz Esperante's left
  and right tails, the user's report).

Results, 19 ports (the front set with the Esperante road car, the TT RS, the Devon GTX, the F50 and the
Commodore VE #1; master 5b346c0's lamp code against this; rear view, dark mood):

| | vanilla | master | this change |
| --- | --- | --- | --- |
| p95 | 0.042 / 0.097 / 0.29 | 0.025 / 0.060 / 0.121 | 0.055 / 0.082 / 0.136 |
| contrast (local std / mean over the lit pixels) | 0.23 / 0.36 / 0.57 | 0.19 / 0.35 / 0.45 | 0.26 / 0.36 / 0.45 |
| lit share of the red lamp pixels | 0.10 / 0.26 / 0.48 | 0.02 / 0.27 / 0.48 | 0.19 / 0.31 / 0.52 |

No car is dimmer. X6 M 0.045 -> 0.057, Devon GTX 0.033 -> 0.081, Golf R 0.016 -> 0.098, Mustang GT 05
0.027 -> 0.082, S60 0.040 -> 0.088, Thunderbird 57 0.020 -> 0.073, TT RS 0.120 -> 0.155. The
Esperante's sides: p95 0.068 and 0.059.

In game (VM, `-Views 'rear,rear+night' -Palette 0`, vanilla 130 first; master `vm1-20261030-021734`
and `vm2-20261030-024709`, this change (without the "never below" rule) `vm1-20261030-022240` and (as
merged) `vm2-20261030-025201`): at night the X6 M's, Mustang GT 05's, S60's and Golf R's tails glow
brighter, along their lamp graphics; the Esperante's two sides glow alike; the TT RS's and Devon GTX's
as before. By day the tails look as on master.

Roster (offline, `out/l8/rear_survey.py` in the t3-a7c21235-c worktree: `Texturing.plan` at LODs 1, 3, 5
per car, the same rear view of the rear half's lamps both times, each red texel it sees at its
effective alpha, the glow where there is one, else the converted alpha; master 5b346c0's lamp code
against this; 552 cars with visible red rear lamps): p95 p10 / 50 / 90 29 / 106 / 171 -> 70 / 130 /
183 (vanilla's visible 0x18 tails per vehicle 52 / 106 / 210); share over alpha 46 0.01 / 0.22 / 0.41
-> 0.15 / 0.33 / 0.47. 319 cars rise by more than 5, none falls; inside vanilla's p10 to p90 423 ->
512; below it 121 -> 22 (race cars with decal or hidden lamps: RAD_SR8LM_10, BEN_7_Speed8_03, the
ARX-01bs, LOT_Evora_09, NIS_GTR_12, NIS_Versa_09, the Viper CCs); above it 8 -> 18.

Not this: the F50's starburst and white patches and the Commodore VE race cars' flat red block with a
white patch (#300) are the same in both builds by day: their tail lens and cover bake, not the glow.

### Lenses as Forza draws them (issues #300, #340)

`research/forza-lenses-re.md` (#369, #379) decoded Forza's race lens shaders: a `detail_glass_*` lens
multiplies whatever lies behind it (reflector, bulb, the lamp's glow, a trim, paint) by
`L + GlassColor` (clear, amber; the car's GlassColor converted from sRGB to linear as Forza uploads
it, shared 0.2 = 0.033 on clear lenses) or by its red remap `DarkValues + (LightValues -
DarkValues) L.r` (red), in Forza's square light space, then adds a faint reflection. It reads no
alpha, has no colour of its own and doesn't glow. The bake mixed the lens texel in at its alpha
(`a x lens + (1 - a) x behind`, off by a median delta E of 15 to 39 against the decoded maths): a
red lens over a white bulb came out pale pink, a clear lens over a red lamp grey.

Change (`lenses.bake`, `lens_filter`):

- A lens texel is what its ray meets times the filter (bytes: behind x sqrt(filter)); its glow is
  the lamp behind's times the filter in the channel the result shows brightest (a red lamp under a
  red lens: its red). The clear front lenses' head lamp glow (`lens_glow`) passes the filter too
  (`day_glow` ranks it, so only its shape counts). A lens ray that meets nothing keeps the lens
  texel, unlit.
- Lenses of one flat colour are no longer left out (`_flat`, `FLAT_SHARE`, `FLAT_SPREAD` gone):
  baked as filters, they show the lamp behind as Forza does and, over a trim, the trim through
  the filter (#340: the Commodore VE's trim under its red tail glass is red in Forza). Scrambled
  swatch lenses still put their filter onto the lamp behind (one texel can't hold many points).
- `lens_filter` follows the shader: linear, `L + GlassColor` with the car's or the shared value
  converted, clipped at 1; `opacity_rule` no longer has a lens case.
- `lights_gls_noemit` is lamp glass without a glow, not a cover (`COVERS`, `TEXTURED_COVERS`,
  `forza_materials` classify it with the other `lights_gls_*`): baked at its cap when it is under
  1, its glow only what the cap lets through of the lamp behind.
- render_compare's Forza reference draws `detail_glass_*` lenses as the same filter (it drew the
  lens picture at its opacity: milky lenses, #313).

Not this: the covers' glow and plain glass over lamps (`forza-lenses-re.md` section 7, next PR);
FM4's glow mask (the texel colour, not its alpha) with the lamp curves per type.

Roster (the build reports of the 573 cars): 499 cars have `detail_glass_*` lenses, every one baked
differently (42 M lens texels sampled); 85 cars get back 238 flat lens subsections that were left
out; 3 scrambled swatch lenses keep the drop. `render_compare diff` on 16 cars (the usual 9 and
the Commodore VE #1 and #88, F50, X5 M, Q7, M5 E39, R390; render_compare with #372's renderer and
this reference; `out/rc/lens`): 15 drawn differently, all in the lamp regions; flags fixed 8 (rear
lamps "colour off" on the X6 M, Venom, both Commodores; "washed out" on the M5 E39's rear and the
R390's front; the F50's "glows elsewhere"; the Mustang's "different detail"), new 0; scores fall
on 13 (M5 E39 3.14 -> 0.72, Venom 2.29 -> 0.67, X6 M 1.60 -> 0.27, Commodore #88 7.40 -> 5.78),
none rises. In game by day (VM 1, `-Views 'rear,rear+night'`, before `vm1-20261030-175324`, after
`vm1-20261030-175726`): the M5 E39's tails red where they were pale pink, the Commodore VE's
cluster red over its trim with its clear reverse strip, the F50's inner lamps red (#300), the
X6 M's flat red lens kept over its lamp graphics; the Venom and vanilla 130 as before.

### The lamp model the renderer and the harness use

`research/lights-re.md` decoded how the game draws a lamp. Two of the tools every lamp change above
was measured with drew lamps otherwise:

- **The harness's dark mood** (`lightstest.lua` `darkLighting`, the `+night` views and the `-Lights`
  dark phases) set `vehColourBoost` 1, where every gameplay mood has 5 (10 in Fog): its lamps were 5
  times dimmer in linear light than any mood a player sees. The `+night` views also switched
  Headlights on, which free roam never does. DSF has no night in free roam; the darkest mood a
  player meets is the story's LastChapter (dusk light, `vehColourBoost` 5).
- **render_compare's renderer** (`car_render`) drew a lamp texel at `2 x alpha x level / 2` with
  guessed colours (red 1, 0.06, 0.03), no tint, a 0x1D lamp at 10 with the running lights alone
  (the game: 1), its dark mode with Headlights on, and the garage tone instead of the game's.

Change (no converter change):

- `lights.TYPE_SCALE`, `LAMP_COLOUR`, `RUNNING_LEVEL`, `VEH_COLOUR_BOOST` and `lamp_level(type,
  mask)`: the game's tables (lights-re.md), one copy for the renderer and `tools/re/lamp_model.py`.
- `car_render.dsf_lamp_rgb`: each lamp at 5 x its colour x its level; `Lighting` carries the mood's
  exposure and white, `display()` the game's tone map and colour grade (ClearDay's 3D LUT from
  `SfxMisc.fchunk`, research/paint-re.md, "The grade") for the
  lit modes. `day_lit` is ClearDay with the running lights (e 1.7, white 5); `dark` is LastChapter
  (sun 7%, ambient 1.4 times, reflections half, e 1, white 2) with the running lights. The `day`
  scores and the garage pictures keep the garage tone.
- DSF glass in the lit modes is drawn as the game draws `GLOBAL_Glass`: opaque by the shared glass
  texel's blue read as sRGB (the windows' (13, 232, 191): 52%), raised by the glint, untinted, so
  a lamp under a window keeps about half its glow (measured below; it kept about 60%, the garage
  pictures' clear glass, which the `day` mode and Forza's glass keep). A lit mode's glow is the render less the same light with
  the lamps off (`<mode>_base`), not less the `day` render.
- A lamp's glow is now the same in every gameplay mood (tint 5, the running lights); only the tone
  map differs. render_compare's exterior views drop their `dark` mode, which repeated `day_lit`'s
  glow; the cockpit keeps its dark render. The white plate limit (`glare`) is vanilla's p90 (below).
- `lightstest.lua` `darkLighting` is LastChapter's own table from `moods.lua`; `+night` shows it
  with the running lights only, and a view without a suffix goes back to the running lights (it
  switched every lamp off). `+lit` (Headlights by day) stays for a check of 0x01 lamps.

Check against the game (`lamp_model.py compare`, now with the renderer's colours, levels and post
chain, on run `vm1-20261030-142442`'s dumped frames of vanilla 130, 241, 266): brightest 1% of the
lamp pixels median x1.11, worst x1.28; on screen median 5/255, worst 25/255, as the note's model.
The renderer's own rear render of the three cars against the same frames (`out/re/render_vs_game.py`
in the PR's worktree; the dark frames rendered in a light like the old dark mood's): p99 median
x1.18, worst x1.44 (car 241's tails 1.4 times the game's by day and dark alike: which texels the
view sees, not the lamp term).

Vanilla under the game's model (render_compare's renderer, front and rear views, 142 vehicles;
`out/ls/lamp_survey.py`): glow p95 over each vehicle's lamp pixels by day, p10 / median / p90:

| | running lights | braking |
| --- | --- | --- |
| front, vehicles with day-lit lamps (140) | 0.88 / 2.3 / 4.25 | the same |
| rear (139) | 0.105 / 0.244 / 0.95 | 0.44 / 0.93 / 2.13 |
| front 0x03 white (66) | 1.51 / 3.53 / 4.79 | |
| front 0x1D white (21, all under window glass) | 1.1 / 1.85 / 2.32 | |
| rear 0x18 red (56) | 0.128 / 0.239 / 0.469 | 0.64 / 1.19 / 2.34 |
| rear 0x03 red (44) | 0.21 / 0.92 / 2.62 | the same |

render_compare's white plate limit (`glare`) is vanilla's front p90, 4.3 (it was 3.0 in the old
units, where a 0x01 texel glowed 2).

The ported head lamps that #286 raised to vanilla's 0x1D level "at night" were measured against a
0x1D lamp drawn at 10; by day, which is what players see, it glows at 1, as vanilla's do.

### Glass over lamps as Forza blends it (#379)

`research/forza-lenses-re.md` section 7 (#379) read Forza's blend out of `default.xex`: plain `glass`,
`lights_glass` and `lights_gls_*` draw with `SRCALPHA, INVSRCALPHA` on Forza's square-root output,
so a layer at opacity `a` keeps `(1 - a)^2` of what is behind. Plain glass is at `GlassColor.w`
(0.3 shared) plus the Fresnel term: one layer keeps 26% of a lamp head-on. A `lights_glass` cover
keeps `(1 - a)^2` of its lamp's glow, a = `min(max(texel alpha, GlassColor.w), GlassMaxOpacity)`
(p10 / median / p90 over the roster 0.7% / 3.8% / 20%).

What the converter did: plain glass over a lamp left out (#160, #247, #284: a cover from 30% of
its area over a lamp, 5% in a lamp unit's own section), so the lamp showed whole, about four times
Forza's; `lights_glass` covers passed the lamp's whole glow (#164). And the day-lit head lamps
glowed at `DAYTIME_GLOW` (0.45) of vanilla's alpha, against a 0x1D lamp drawn at 10 by the renderer
of the time (#286).

Change:

- Plain `glass` over a lamp stays DSF window glass (`Texturing.clear_cover`,
  `COVER_LAMP_SECTION_SHARE` and its section pattern removed): it keeps about half a lamp's glow
  (measured, `lights-re.md`), against Forza's 26%, the closest DSF has.
- A `lights_glass` cover baked over its lamp passes `(1 - opacity)^2` of the lamp's glow, with
  the cover's colour blended in at its opacity as before.
- The day-lit head lamps glow at vanilla's 0x1D alpha (`DAYTIME_GLOW`, `Binding.set_daytime`,
  `Texturing.daytime_mask`, `_dsf_boxes`, `LampPlan.is_daytime` removed: the atlas is rendered
  once). Vanilla's are seen through window glass (all 28 vehicles with 0x1D lamps); with the
  plain glass kept, the ports' are too, wherever Forza has it.
- What the glass or lens in front of a head lamp passes (a lens's filter, a cover's
  `(1 - opacity)^2`) multiplies its day glow after `day_glow`'s ranking (the ranking onto
  vanilla's distribution used to cancel it); the glow map holds it as a fifth channel.
- render_compare: Forza's plain glass at `GlassColor.w` (the car's or 0.3) and every alpha-blended
  glass passing `(1 - a)^2` in the reference; behind window glass the interior's copy of a panel
  loses to a lamp as behind a lens (it hid recessed bowls under the kept glass); a lamp region's
  pixel under DSF glass is "drawn as" the surface behind it.

Results, 16 cars (render_compare's renderer with the game's lamp model, by day, the running
lights; before: #385's build):

- The 0x1D head lamps' glow p95 over the front view: p10 / median / p90 0.82 / 1.75 / 2.48
  (vanilla's 1.1 / 1.85 / 2.32). Under kept glass (X6 M, Mustang, S60, both Commodores, F50, X5 M,
  Q7, M5) about what they were; without glass in Forza (Carlton 1.63 -> 2.53, Venom 1.22 -> 2.27,
  M3 E30 1.75 -> 2.65, Esperante 1.43 -> 2.08, R390 1.45 -> 1.86) up to vanilla's alpha; behind a
  clear lens that passes little (GTI VR6 1.73 -> 0.67, X5 M 0.97) lower. The Golf R's front 0x03
  running lamps 6.71 -> 3.91 under their glass (vanilla's front 0x03 1.51 / 3.53 / 4.79).
- `render_compare diff` (`out/rc/cov`): all 16 drawn differently, in the lamp regions; fixed 6
  flags (the Golf R's white plate by day, "chrome extra" on 5 cars, the F50's front "colour off"),
  new 2 ("drawn as" paint or trim under the F50's and Mustang's kept covers, where they lie over
  bodywork or housing). Q7 rear braking 1.83 -> 0.89 under its kept rear glass.
- In game by day (VM 1, `-Views 'nose,rear,nose+night'`, before `vm1-20261030-185809`, after
  `vm1-20261030-190326`): the Carlton's head lamps brighter, as bright as vanilla 241's; the Golf
  R's running lamps dimmer; the Mustang's, S60's, X6 M's and M5's about as before.

### Lamp alpha per type, measured by day

`research/lights-re.md` asked for the lamp alpha curves per lamp type rather than per hue, for
FM4's glow mask from the texel colour, for `TAIL_DARK` and for 0x03 against 0x18 tails. Measured
with render_compare's renderer under the game's lamp model (#372), the converter as of #389, by
day: vanilla's 142 vehicles against the 16 usual ports and 60 random ones (`out/ls/lamp_survey.py`,
`pertype.py` in the PR's worktree), glow p95 over each vehicle's pixels of the type:

| Type | Mode | Vanilla p10 / median / p90 (vehicles) | Ports, 60 random: median (cars), below / above vanilla's p10 / p90 |
| --- | --- | --- | --- |
| 0x1D head | running lights | 1.10 / 1.85 / 2.32 (21) | 1.44 (50), 13 / 5 |
| 0x03 white front | running lights | 1.51 / 3.53 / 4.79 (66) | 2.20 (7), 1 / 0 |
| 0x18 tail | running lights | 0.13 / 0.24 / 0.47 (56) | 0.19 (60), 14 / 1 |
| 0x18 tail | braking | 0.64 / 1.19 / 2.34 | 0.93, 14 / 1 |
| 0x02 high stop | braking | 0.32 / 0.80 / 2.17 (86) | 0.90 (31), 4 / 5 |
| 0x19 / 0x1A | braking | 0.36 / 1.19 / 2.66 (40) | 1.13 (3), 0 / 0 |

- **Curves per type:** the per-hue curves convert every Lights texel before the lamp plan
  exists. After that, two rankings override them where a camera sees the lamp: `day_glow` ranks
  the front head lamp surfaces onto vanilla's 0x1D alpha (`DAY_HEAD_ALPHA`), and (#391)
  `rear_lamp_glow` ranks each planned rear red lamp (0x18, 0x02, 0x19/0x1A) onto vanilla's 0x18
  alpha (`TAIL_RED_ALPHA`). There the per-hue conversion is only the floor (`never below the converted alpha`). The
  per-hue curves alone decide the texels no camera straight ahead or behind sees, and the
  white and amber ones (indicators, reverse). All these types glow inside vanilla's per-type
  ranges by day (the table). Vanilla's white types share one alpha
  distribution (per vehicle p50 / p90: 0x01 0.20 / 0.59, 0x1D 0.23 / 0.67, front 0x03 0.18 / 0.57,
  0x08 0.14 / 0.53). Per-type curves would need the lamp plan before the texels are converted
  (the second render #389 removed), with no measured gain: not done.
- **Dim head and tail lamps:** the 0x1D cars below vanilla's p10 (Aston Martin V12 Vantage 0.42,
  CL65 AMG 0.55, Audi A4 0.64, Jaguar XFR 0.79...) have their texel alpha at vanilla's but
  multiplied by the clear lens's filter (#385) and sit under the plain glass Forza has there
  (#389): head lamp alpha p90 42 to 76 where vanilla's is 97 / 171 / 232. That is what Forza draws
  in front of those lamps; not tuned. The dim tails (14 of 60, as many FM4 as FH1 cars by share)
  are mostly not the lens and glass rules: of three checked, the Nissan Versa (0.041 before #385,
  0.021 after) and the Aston Martin V12 Vantage (0.018 before, 0.035 after) were as dim before,
  their visible tail texels at alpha 4 to 19 (the Versa's red, the V12 Vantage's grey under a clear
  lens, so `tail_glow`'s red-texel pool doesn't take them); the Mazda Axela's (0.177 -> 0.061) is
  its kept glass and lens. #391, since fixed by [ranking per planned lamp](#rear-lamps-ranked-per-planned-lamp-issue-391).
- **FM4's glow mask:** FM4's lamp shaders glow by the texel colour (`BrakeColor x L`), its
  `tail_light` alpha is 255 on 92% of the texels (`forza-lenses-re.md`), and the "matched" score
  `a x (0.6 + 0.4 x brightest channel)` at alpha 255 ranks a tail's texels by their brightest
  channel, on a red texel its red: the same order. The texels under 255 are where it moves: the
  V12 Vantage's grey tail texels at alpha 4 to 8 glow by their colour in Forza and stay dark here
  (#391). FM4's reflectors don't glow in Forza (lit by the lamp plan here): the `reflector`
  shader reads their texel alpha as opacity (`min(max(A, GlassColor.w) + Fresnel,
  GlassMaxOpacity)`), not as a glow mask, so the change needs a rule for them too. Since #391 the
  colour orders an FM4 lamp's texels within its ranking, its reflectors last
  ([ranking per planned lamp](#rear-lamps-ranked-per-planned-lamp-issue-391)).
- **`TAIL_DARK`:** no counterpart in the game (each texel glows alpha x colour); it stands for a
  ported tail lamp's lit area, 2.5 times vanilla's red area. Kept, documented as the heuristic.
- **0x03 against 0x18 tails:** vanilla uses both (44 vehicles 0x03, at 2 with the running lights,
  glow 0.92; 56 vehicles 0x18, at 0.2, glow 0.24). Forza by day keeps the running tail lamps near
  0 (FH1's `SwitchOnLights` about 0 from 08:00 to 19:00, `forza-lenses-re.md` section 2), so 0x18,
  dim with the running lights and full when braking, is the closer day look; the converter keeps it.

### Rear lamps ranked per planned lamp (issue #391)

Symptom (render_compare's renderer under the game's lamp model, by day, the visible 0x18 tail pixels'
glow p95): 15 of 76 ports below vanilla's p10 (0.13; vanilla 0.13 / 0.24 / 0.47 over 56 vehicles),
the Nissan Versa at 0.021 and the Aston Martin V12 Vantage at 0.035.

Cause. The tails' glow came from `lenses.tail_glow`, which ran before the lamp plan existed and took a
lamp to be a connected patch of red texels seen from behind. It missed what isn't red: grey FM4 tail
texels (FM4 lights them red with `BrakeColor x L`), a clear lens's texels over a tail (the V12 Vantage:
28,793 lens texels over a 38-triangle `tail_light`), and red texels at the bottom of a flat pool (the
Versa's 18). On FM4 cars it also ordered by the texel alpha, which FM4's shaders don't read
(`forza-lenses-re.md`). Changing FM4's alpha to the colour mask before the plan (#391's first try)
moved cars both ways: the hue curves were fitted on alpha scores.

Change (`lenses.rear_lamp_glow`, `Texturing.rear_lamp_tris`, `Binding.set_rear_lamps`,
`LampPlan.rear_tail_lamp`):

- After the lamp plan, each rear red lamp it makes (tail 0x18, brake 0x02, brake and indicator
  0x19/0x1A; `lights.REAR_TAIL_FUNCTIONS`) is ranked on its own onto `TAIL_RED_ALPHA`: every texel
  of its triangles at the finest lamp LOD (lenses included, any colour), in the pool of what the
  converter's straight-behind view sees of that lamp, by its picture's detail. A texel the view sees
  takes its own samples' detail (the pool's), the others their triangle's mean (with the triangle's
  mean for every texel, a lens of a few large triangles over a whole lamp squeezed every rank to the
  middle). The rest as before: the dimmest `TAIL_DARK` dark, never below the converted alpha.
- The order within a lamp: the matched score; on FM4 cars the texel's linear red (Forza's
  `BrakeColor x L`), and the texels only `head_light`, `textured_reflector` or `reflector` triangles
  sample last (`dsf_materials.fm4_order`): Forza never glows them, and ordered by colour a bright
  grey reflector outranked the red tail graphics (the Toyota SC430 race car's tails fell from 0.36
  to 0.12 until then).
- The Lights atlas is rendered again after the plan (`set_rear_lamps`): the regions whose source and
  glow didn't change are kept and `dds_file` re-encodes only the changed DXT blocks. The first render
  has no rear ranking (no lamp glows before the plan). `tail_glow` and `_components` are removed.
  Cost: 0.56 s a car (median; p10 0.32, p90 0.97, max 3.78 over 76 cars, one Idle worker next to a
  running game).

Results, 76 cars (the 16 usual and 60 random), master against this, vanilla's 142 vehicles:

| | vanilla p10 / median / p90 | master: median, below / above | this: median, below / above |
| --- | --- | --- | --- |
| 0x18 tails, running lights | 0.13 / 0.24 / 0.47 | 0.19, 15 / 1 | 0.23, 3 / 1 |
| 0x18 tails, braking | 0.64 / 1.19 / 2.34 | 0.96, 15 / 1 | 1.16, 3 / 1 |
| 0x02 high stops, braking | 0.32 / 0.80 / 2.17 | 0.92, 4 / 5 | 1.11, 0 / 6 |
| 0x1D head lamps | 1.10 / 1.85 / 2.32 | 1.46, 16 / 9 | unchanged |

Every car that changes range: tails (running lights and braking alike) 12 into it from below (Alfa
Brera 0.113 -> 0.277, Aston Martin One-77 0.095 -> 0.136, V12 Vantage 0.035 -> 0.191, Audi A4
0.124 -> 0.254, Sport Quattro 0.108 -> 0.251, BMW M5 E39 0.120 -> 0.219, Falcon FG 0.081 -> 0.152,
Jaguar XFR 0.096 -> 0.130, Mazda Axela 0.061 -> 0.128, Versa 0.021 -> 0.148, Radical SR8 0.122 ->
0.402, Audi R8 V10 0.120 -> 0.196), none out; high stops 4 into it from below (V12 Vantage, Ford
Ka, Koenigsegg CCX, Versa), one above it (the Lexus SC430 02, 1.31 -> 2.61). Still below vanilla's
p10: the Lexus SC430 02 (0.127 -> 0.095), the Pagani Zonda R (0.074 -> 0.081) and the Lamborghini
Sesto Elemento (0.122 -> 0.124). Cars that dropped but stayed in range: both SC430 race cars
(0.362 -> 0.209 and 0.161), the GTI VR6 (0.332 -> 0.164).

### Rear lamps glow in proportion to Forza's mask (issues #403, #404)

Symptoms (user's build of master 5fef56e, by day, the player's car at rest with its brakes on):
- the Commodore VE #88's, DS3 Racing's, M3 GTR's and Viper GTS-R's tails lit as blotches and stripes;
- the Joss JT1's round tails squashed, a smaller red ellipse inside each lens (#403);
- the Focus RS500's tails far brighter than vanilla's (#404).

**Cause 1: the ranking stretches every lamp to one spread.** #391 ranked each planned lamp's
texels onto vanilla's tail alpha distribution: a quantile map, which gives every lamp the same
alpha spread whatever its mask. Forza's glow is linear in its mask (`colour x state x mask`,
`forza-lenses-re.md` section 2), so an even lens glows evenly there. Over the visible samples
(`out/survey_all.py`, `rankamp.py` in this PR's worktree; CV = standard deviation over mean):

| Lamp | Forza's mask (the order) p90/p50, CV | Ranked alpha p10 / 50 / 90, CV |
| --- | --- | --- |
| Joss JT1 tail | 1.11, 0.08 | 0 / 19 / 117, 1.36 |
| DS3 Racing brake and indicator | 1.09, 0.11 | 0 / 25 / 111, 1.29 |
| Commodore VE #88 tail | 2.80, 0.75 | 0 / 19 / 116, 1.36 |
| M3 GTR tail | 2.07, 0.56 | 0 / 19 / 116, 1.36 |

Small differences of an even lens (a bake's few levels, DXT noise) became the whole 0..255
range: blotches, and on the JT1 only the top-ranked texels lit, the ellipse.

**Cause 2: the floor at the converted alpha.** The rear glow was never below the texel's
converted alpha (#290: "the tails that already glowed at vanilla's level keep it"). That alpha is
the per-hue "matched" curves' rank, the white curve on white texels, fitted on vanilla's white
lamps. The RS500's tails are clear `lights_gls_*` lamp glass over white texels: the floor raised 79%
of their visible samples (60% white ones), alpha p95 150 to 251, over vanilla's per-vehicle tail
p95 p90 of 210. Under the linear glow below it raises 66% of them (p95 70 to 250) and the Viper's
40% (58 to 136): it puts the per-hue curves' blotches back over an even lamp.

Change (`lenses.rear_lamp_glow`, `Texturing._glow_parts`):
- Each planned rear red lamp glows at `g x` its texels' mask, capped at 255, `g` set so the mean
  over its visible samples is `TAIL_MEAN`, 40.1: the mean #391's ranking gave every lamp (its
  dimmest `TAIL_DARK` dark, the rest on `TAIL_RED_ALPHA`). So each lamp's summed glow, the quantity
  that matches vanilla (`lights-re.md`, `TAIL_DARK`), is #391's, spread as Forza's mask spreads it.
  The detail factor (`GLOW_DETAIL_*`) no longer enters: the mask is the lamp's picture.
- No floor at the converted alpha.
- FM4 cars: the mask is the texel's linear red (`fm4_order`); the texels only no-glow techniques
  sample (reflectors, head lamp bowls) are 0, as Forza never glows them.

Roster (offline, master's code, 573 cars; `out/survey_all.py` in this PR's worktree: the lamp plan on
each car's host-picks host, the converter's straight-behind view; 570 cars have ranked rear lamps,
1,327 lamps over 40 visible samples):

- Ranked (master), every lamp's alpha CV was 1.36 (median; the same for every lamp), its mask's
  0.61. 494 lamps on 316 cars have an even mask (CV under 0.5), and on 443 of them the ranking made
  the alpha spread more than the mask's twofold or more (CV over 1). With this change each lamp's
  alpha CV is its mask's.
- The floor raised more than 10% of the visible samples on 519 cars, white texels on 135 (the AST DBS
  98%, both Peugeot 908s, the Astra V8, the Mazdaspeed3, the SLR Stirling Moss). It put the visible
  tail alpha p95 over vanilla's per-vehicle p90 (210) on 45 cars; ranked without it, 3 (the MC12s,
  the CTS-V coupe, at 214).

Visible rear alpha per car, the mean (the summed glow per sample) and the p95 against vanilla's
per-vehicle tail p95 (p10 / median / p90 52 / 106 / 210):

| | mean, median over cars | p95 median | cars over 210 | cars under 52 |
| --- | ---: | ---: | ---: | ---: |
| master (ranked, floor) | 47.6 (p90 70) | 153 | 45 | 0 |
| this change (linear, no floor) | 40.1 (every lamp) | 87 | 22 | 27 |

The 27 under 52 are even lamps at the same mean, lit evenly (the JT1 49, the TT RS 50, the Veyron SS
50). The 22 over 210 are lamps whose mask is mostly 0 (FM4 reflectors and grey surround), so the few
lit texels carry the lamp's mean and some reach the 255 cap: the VW Golf GTI 06, both SC430 race cars,
the Corsa VXR, the Z4 M Coupe (open).

In game (VM 1, by day, `-Views 'rear,nose' -ShotSeconds 3`, vanilla 130 first; master
`vm1-20261031-054752`, the four #403/#404/#405 changes together `vm1-20261031-055429`):
- the Commodore's, Viper's, M3's and DS3's tails glow as even red lenses;
- the JT1's inner round tails glow as whole discs (master: dim ellipses);
- the RS500's tails a little dimmer at rest. Its tail texels are near white in sunlight (brightest
  channel median 208, vanilla's red tails 98), and that, not the glow, is most of their look by day.

### Open

1. Front lamps in the lamp-phase mode (`-Lights`): neither copy faces the
   camera (`setYRotation(h + pi)` and negating the matrix rows both left it
   showing its rear; the matrix write may set only the position). Answered
   another way: since #104 and #123 front lamps are checked with the fixed
   views (`-Views nose`, `nose+lit`, `nose+night`), which show the converted
   head lamps lit by day and at night. Traffic never sets Headlights or
   DaytimeRunning in daylight (`draw-distance.md`, hypothesis from the
   decompile), so in normal play traffic's front lamps stay off by day for
   vanilla and converted cars alike.
2. b0 and the pi, pi pair. The flare sprite does use the direction: it draws
   only while the entry's direction faces the camera (`draw-distance.md`,
   "The flare system").
3. Lamps under detachable parts: the light records hang under the host's
   bumper and boot records (a lamp breaks when its record's part is damaged,
   `0x8E84A0`). With `--damage` (the port_batch default) the lamp triangles
   sit on their panel's record like the rest of the body (`damage.py`
   `section_rule`: tail lights vote between boot, rear bumper and chassis);
   without it they are chassis-bound, so the geometry stays while the lamp
   breaks. Partly answered in game by #141's runs (`+hitfront`, `+hitrear`,
   runs `vm1-20261027-231924`, `-233940`): a lamp on an entry at its own end breaks with
   that end, as the Veyron's tails and head lamps did. A panel that falls off
   with its lamp is unchecked (`damage.md`, "Risks and open questions" 5).
4. Converted cars in traffic (above). `swapWithModel` on a live traffic car
   crashed run 123840 (the nearest traffic car ahead, to the player's own
   model 227); register.lua's experimental `swapTraffic` and `carrier` paths
   were removed for that reason (they were off by default).
5. Converted amber lenses keep Forza's brightness; red lenses are darkened
   (`red_lens_scale`) and white ones too since issue #115
   (`white_lens_scale`). The S5S Raptor's tails (alpha mean 91) sit above
   vanilla's per-vehicle p90 (81). The other way, #166: the Devon GTX's
   darkened tails read dark maroon by day on its bright red body (a roster
   survey there kept the rule).
6. In a run, cars after the fifth sometimes don't spool ("NOT spooled after
   40 ticks", run host-20261024-003943); keep lamp runs to five IDs.
7. Lens bake ("Lamp lenses drawn opaque"): the view is straight in along the
   lens normal (or the car's axis, #190 and #218), so the lamp inside doesn't
   shift with the camera (vanilla lamps are flat textures too). Since "Lamp
   glass" the `lights_gls_*` glass and the textured covers are baked too;
   covers not over a lamp stay DSF's window glass. The lenses keep the
   texel's colour and alpha: Forza's race shaders scale the lens colour by
   `GlassColor x PaintScale` (a twentieth of the texel by default) and add a
   reflection the bake doesn't draw, and the pass tables (blend states) of
   the effects aren't decoded. Issue #89.
8. Head lamp glow by day (issue #124, "Head lamp glow by day"): only clear
   front lenses and head lamp surfaces get it; on 0x1D lamps at a fifth
   ("Head lamps white plates by day"). Clear rear lenses over reverse lamps
   still glow by their own lamp score, and so do front lenses that meet no
   lamp within 0.4 m along the normal or the axis.
9. Swatch lenses that are only partly scrambled keep a flat tint over part of
   the lens: after #190 and #218 only the Viper CC's left head lamp lens (a
   third of its texels) and the Scirocco R's tails (3%) more than a few texels
   ("Coarse LOD lamp parts"). The tiling parts that stayed one colour are laid
   textured since then. Issue #229.
10. Brake coverage (#103): after "Red texels lit as reverse and indicator
    lamps" 19 of 565 cars brake with under half of their visible red rear
    lamp area, all by design (unlit retro-reflectors and decal lamps, red fog
    lamps, the M3 E92's 0x03 `drlred` tails, the Stealth's reflector panel)
    but FOR_EscortRSCosworth_92 and TOY_6_SC430_08, which have no lamp
    technique at Forza LOD 3, so their full entries have almost no lamps
    (the Escort only its front indicators). The ZR1's
    `lindtailside` lamps (#220) weren't driven on the throttle in game.
11. Lamps on the other end's entries (#146): 6 on 4 cars after "A rear entry
    freed by a left and right pair" (hosts short at the front, and host 161's
    two rear entries); the reverse lamps of the Miata 94, Elise 99 and Series 1
    stay unlit (their freed entry goes to the right indicator). Fog lamps on
    full hosts stay unlit (#219).
12. Pop-up head lamps can't rise with the lights (#122).

## Paint

`paint-re.md` has the paint shaders' full maths with runtime values, a model checked against
dumped frames, and Forza's paint data. It corrects one point here: the atlas paint block is
(255, 198, 255), sampled with sRGB decoding, so the shader's albedo is the palette colour
exactly (not 240 x ...). It confirms the fresnel reading of "The DMC-12 came out black".

### The palette (confirmed-static)

`NONR/SPSR` in each full entry: `SPID` (u32 spool ID), then three `SPSS`
sets. The first is the palette: 1 to 14 `SPSP` (fleet: 25 vehicles with 8,
22 with 1, 17 with 6, ... 3 with 14), one per paint choice. Each SPSP holds
nine `MARP` (seven on two vehicles):

| Parameter | Meaning (from the 948 vanilla entries) |
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
and colour-coded mask blocks). On every vanilla car checked (266, 227, 229,
147) 57 to 91% of the paint triangles' UV0 lands in its (255, 198, 255)
block (50 x 50 texels centred at (0.848, 0.712); `global_paint_uv.py`), the
rest on other mask colours (panel details). The game samples it with sRGB
decoding, so the albedo is the palette colour exactly (`paint-re.md`, "The
atlas texel"). UV1 is the damage-decal UV
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
`ColorShaderSettings<N>.xml` (`body` `PaintColor` (often the Combo_Colors RGB, darker on
bright metallics: `paint-re.md`),
`PaintScale`, `SpecularAmount2/3`, `MetalColor/Scale`, `FresnelIndex`...;
12 of the 40 testdrive cars have some, e.g. the Focus RS500's satin grey:
`PaintScale` 0.3, `MetalScale` 1, `SpecularAmount3` 40); read since #380.

The FM4 DMC-12 has one colour, solid `#999999` (Gray). Its body is plain
`body` paint (3,782 LOD1 triangles, all on alpha-0 white texels of
`nodamage.xds`), with no shader settings in the zip: Forza draws the
stainless body as flat grey paint.

### The converter (`src/dsfforza/convert/paint.py`, offline-checked)

Since the paint rewrite (#345) the converter sets every palette parameter from the game's paint formulas
(`paint-re.md`, "From a Forza colour to the palette"). It no longer copies a vanilla
template. For each Forza colour (swatch S = `Combo_Colors.RGB`, metallic flag M):

| Parameter | Set to |
| --- | --- |
| `g_vec3TintColour2`, `g_vec3TintColour3` | both `albedo_for(S)`: the albedo whose median sunlit panel (`screen(2.13 x A)`: ClearDay's exposure at La 0.2, Reinhard on luminance with W 5, the game's grade LUT from `media/SfxMisc.fchunk`, sRGB) shows S, capped at the knee (0.3 for a saturated colour, 0.5 for a grey). Past the knee more albedo only flattens the body. One colour at every angle, as Forza's factory paint is |
| `g_fresnelpower`, `g_fresnelbias` | 10 / 0.01, the vanilla flat entries' (no effect when colour 3 = colour 2) |
| `g_hCustomReflectionAmount` | 1.0 x `FresnelScalar` / 0.9 (Normal.xml's 0.9: 1.0; `reflectionScaler` is 1 in both games, #379) |
| `g_hCustomFresnelPower` | 1.0 |
| `g_hSecondSpecularIntensity`, `Power` | without lobes 0.1 / 5. With Forza's lobes 0.3 x (`SpecularAmount2` + `3`) / 13 x 0.5 / `PaintScale`, at the lobes' powers weighted by amount. Metallic.xml (M = 1) gives 0.3 / 5.08 |
| `g_vec4TintColour1_Alpha` | (0, 1, 0, 1) metallic, (0, 0, 0, 1) solid (no shader reads it; `is_metallic`) |

Without `DSF_GAME_DIR` (CI), `grade_lut` falls back to the fit in
`tools/re/paint_grade.json` (within 10 / 255 of the game's on the grey axis). Builds read
the game's LUT. The removed constants: `SHOWN_GAIN` (0.3), the brightness cap, the template
match (`is_template`, `_distance`, `vanilla_paints`), `recolour` and `NEAR_BLACK`.
`SHOWN_GAIN` was right for bright reds and under-showed greys and mid tones by 25 to 36 /
255. The brightness cap is now the knee. The dark template rims (colour 3 at 0.02 on
low-power entries) were DSF's art, not Forza's.

**Per-colour settings (#380).** A colour's own `ColorShaderSettings<Sequence>.xml` in the
car zip is layered over Normal.xml and Metallic.xml (`body_settings`). `port_car.extract_colour_settings`
puts the files beside the car's other files, with their own stamp
(`colour_settings_source.json`), so caches made before stay valid; 66 roster cars have them
(147 files). `parse_body` reads the `body` technique. Its colours are converted from sRGB to
linear as Forza converts them before the upload (unclamped; #379); `PaintColor` isn't used
(the swatch is the target). What the files change, over the roster's picked colours (129
entries on 56 cars):

- `FresnelScalar`: the reflection follows it (1.0 x FresnelScalar / 0.9). 9 colours are satin
  (0: Focus RS500 #1, LP700-4 #4, #6, #10, Reventon #1, Sesto Elemento #1, GranTurismo S #6, #11,
  C63 AMG #5); 19 others take 0.03 to 1.11.
- The lobes and `PaintScale` set the second specular on 82 entries (median 0.3 -> 0.18; up to
  1.85 at power 5.3 on the Focus RS500's and the GranTurismo S's `SpecularAmount3` 40 over
  `PaintScale` 0.3).
- The two-tone (`TwoToneScale` > 0: TVR Sagaris #1, #8, TVR Tuscan S #1). Forza's body
  shader mixes the paint toward `TwoToneColor` by `TwoToneScale x sat(N.E + TwoToneBias)^TwoTonePower`.
  That is toward the second colour facing the camera, the opposite of DSF's fresnel. The
  body pixel shader was run in `tools/re/xenos.py` (`forza_lens.Forza.run("body", ...)`, the
  paint black, the two-tone white, the sun off): with power 2 and bias 0 the output is
  (N.E)^2 at every angle, and with the Sagaris's 28 / 0.01 it is 0.755 at N.E 0.98 and 0.071
  at 0.9 (= (N.E + 0.01)^28). So colour 2 (DSF's facing colour) is `albedo_for` of the facing mix
  (swatch + TwoToneScale x (TwoToneColor - swatch), linear), colour 3 the swatch's albedo,
  and `g_fresnelpower` is fitted to Forza's curve over the projected area
  (`two_tone_power`: 0.098 for 28 / 0.01, bias 0).

`MetalColor` / `MetalScale` (Forza's other view-angle mix, 80 FH1 files, mostly 0.03 to 0.05)
isn't mapped: DSF has one second colour.

`install` replaces the host's SPSPs (the host's first SPSP is the chunk
template; every MARP gets an explicit payload) and drops colours from the end
if the full entry's NONR would pass the SSLP buffer (0x6D000) less 0x400 for
handling edits. `forza_colours` drops repeated (RGB, metallic) rows, and
`pick_colours` keeps the 14 most distinct colours (in Forza's order) when a car has more.

The DMC-12 has no preset any more (`SPECIAL_FINISHES` / `FINISH_PRESETS`, the vanilla
DeLorean's stainless entry, are gone). Forza paints it `#999999` under its default clear
coat, and the mapping gives that: A (0.144, 0.162, 0.165), flat, reflection 1.0. In game by
day (VM 2, `-ProbeIds '171,9' -Palette 0 -Views 'nose,left,rear'`; preset
`vm2-20261030-180017`, mapping `vm2-20261030-180252`) the left door's median is (203, 190,
177) with the preset, which reads near-white (dE76 16.8 to `#999999`), and (142, 143, 135)
with the mapping (dE 6.1). The vanilla 171 reads (190, 177, 165), dE 12.7. The mapped body
reads mid grey with the clear coat's reflections; brushed steel would have none.

`uv run dsff paint survey` measures what each roster colour's entry shows against
its swatch: dE76 on the median sunlit panel, and the L* a sun-facing panel adds over it
(`sun_gain`; under 1 the body's shading is flat). `--build` reads a build's palettes
instead. Master at 268cfce (its `out/build`) against this mapping, 3230 entries of
572 cars (the DMC-12's preset left out on both sides):

| Group | Entries | dE median | dE p90 | dE > 10 | L* median vs swatch | flat |
| --- | ---: | --- | --- | --- | --- | --- |
| all | 3230 | 16.0 -> 8.6 | 36.4 -> 31.6 | 3001 -> 1493 | -8.3 -> 0.0 | 514 -> 554 |
| grey | 673 | 15.2 -> 0.0 | 16.1 -> 0.0 | 622 -> 0 | -14.2 -> 0.0 | 0 -> 0 |
| blue | 535 | 15.0 -> 6.3 | 24.4 -> 16.4 | 478 -> 118 | -3.2 -> 1.5 | 41 -> 39 |
| red | 585 | 32.0 -> 28.9 | 39.4 -> 39.1 | 584 -> 562 | 4.9 -> 6.5 | 43 -> 69 |
| white | 429 | 15.8 -> 8.4 | 17.1 -> 11.3 | 429 -> 163 | -15.2 -> -8.4 | 0 -> 0 |
| black | 488 | 14.2 -> 13.7 | 17.0 -> 15.4 | 370 -> 342 | 13.2 -> 12.3 | 405 -> 427 |
| yellow | 236 | 34.9 -> 24.8 | 42.1 -> 35.0 | 236 -> 170 | -14.0 -> -6.5 | 0 -> 0 |
| orange | 113 | 34.2 -> 29.7 | 41.9 -> 37.9 | 112 -> 86 | -5.6 -> 0.0 | 2 -> 5 |
| green | 162 | 18.6 -> 4.8 | 34.1 -> 20.9 | 161 -> 48 | -12.4 -> 0.2 | 22 -> 13 |

Reds, oranges, saturated yellows and blacks keep a large dE. These are the screen's
limits, not the mapping's: no red on screen is purer than (233, 91, 97), and blacks bottom
out at the grade's floor (34, 38, 46) (`paint-re.md`, "Why bright reds look flat"). The
"flat" count grows on reds (43 -> 69): bright reds now sit at the knee, where the median
and the sun-facing panel show the same red, as on vanilla's brightest reds.

### What the paint shader does with the palette (confirmed-static)

`GLOBAL_Chassis` is SSIC GPCH program 27 (26 to 29 are the variants with
`g_hCustomFresnelPower`; disassembly: `python out/damage/gpdis.py
out/vanilla/Install/dngvehicles.sp 27 p`, saved in
`out/scratch/paint/gp27_ps.txt`). In
`Vehicles_Functions.hlsl` / `PixelShader.hlsl`:

```text
tex      = g_samDiffuseMap(UV0)            // the shared atlas paint block (255, 198, 255)
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
shader. The vanilla entries fall into two kinds: flat ones (fresnel power 5
to 15, colour 2 = colour 3, e.g. a flat silver of 0.27 to 0.35 on 67 of the
150 vanilla vehicles), which show one colour at every angle, and low-power ones (0.05 to 1.7)
with a dark colour 3, which mix toward colour 3 by the fresnel term. Taken
literally (vectors as named, power as stored), a low-power entry would show
mostly colour 3, which doesn't fit white vanilla cars with colour 2 0.95 and
colour 3 0.02; the CPU may transform the power (it already derives
`g_OneMinusFresnelbias` and `TintColour3MinusTintColour2`), so how bright
the two kinds look against each other is open (hypothesis). Answered in
the next sections: the power is used as stored ("The DMC-12 came out black").

### The DMC-12 came out white (user report on `out/testdrive3`, ID 27)

What was built (confirmed-static): Forza's solid `#999999` (linear 0.319)
matched the vanilla Courier van's (spool 170) flat grey entry, giving
colour 2 = colour 3 = 0.319, fresnel power 10, reflection 0.3, second
specular 0.05 / power 1: one light grey at every angle, a plain light-silver
paint. DSF's own DeLorean (171) uses colour 2 0.267, colour 3 0.004, fresnel
power 0.75, reflection 0.1, second specular 0.3 / power 5. Why the flat
light grey reads as pure white rather than silver is the in-game question
(hypothesis: the flat entry has no darker term at all, the steel one mixes
toward near black). The decal tint mask at the DMC's paint UV1 is normal (mean 0.87,
like other ports 0.88 to 0.93 and vanilla 0.93 to 0.97;
`out/scratch/paint/tintmask.py`), so the paint was not lost to bare metal.

Fix: `SPECIAL_FINISHES` / `FINISH_PRESETS` (stainless = the vanilla 171 entry).

The `out/paintfix/` runs planned here never ran; the preset was tested on
`out/testdrive4` instead (next section).

### The DMC-12 came out black (user report on `out/testdrive4`, ID 27)

Symptom (confirmed-runtime): with the stainless preset (the vanilla DeLorean's
entry) the port drew near black in the chase view (`host-20261023-082346`,
`observe-06/07`), while vanilla 171 in the same runs is light silver
(`host-20261023-083742`, `-090511`, `-092427`, `observe-02`).

Ruled out (offline and A/B in game, all on the DMC alone, ID 1; sheets
`out/dmc/run2*.png`, `run3*.png`, `stainless-compare.png`):

- the palette: identical to vanilla 171 (`out/dmc/spsr.py`), and copying 171's
  entry byte for byte (`vF`, `host-20261023-093016`) changes nothing;
- vertex normals (decoded like the vertex shader, against face normals and
  outward: `out/dmc/normals.py`, `outward.py`), damaged-normal deltas, the
  baked sky term, MODL flags and submodel headers: like vanilla;
- paint UV0 on vanilla 171's own texel (0.909, 0.708) instead of the paint
  block (`vC`, `-090833`) and UV1 on its hidden-face corner (0.99, 0.01)
  (`vB`, `-090652`): still black;
- the exterior entry's materials: vanilla exteriors use the `_LOD1` shared
  materials (paint and trim in 149 of 150, glass 145, tyres 150;
  `out/dmc/extmats.py`), converted ones used the full-entry ones; switching
  to `_LOD1` (`vE`, `-092015`) changes nothing visible (kept, below).

A flat palette (colour 3 = colour 2 = 0.267, power 10; `vD`,
`-085437`, `observe-12`) gives a light steel body, so the fresnel term was it.
A probe with colour 2 red and colour 3 blue on both cars (`vG`,
`-092835`, `out/dmc/run7G-compare.png`) shows the mix applied literally on
both: vanilla 171's rear panel, facing the camera, is pink (red and blue), its
edges blue; the port's visible paint is blue at the edges. The paint shader
treats both cars alike; what differs is what the chase camera sees. Weighted
by projected area from behind and 15 degrees above (`out/dmc/facing.py`),
vanilla 171 shows 2.3 m² of paint at a mean n.v of 0.54 (its rear panel,
lamp surround and bumper are paint, "DeLorean" in black letters), the port
0.86 m² at 0.25: Forza's DMC has a black rear surround (`body` `black`),
a grey plastic bumper (`bumperRa` `plastic2`), like the real car, and its
stainless sides are seen almost edge-on. With power 0.75 and colour 3 0.004
those grazing panels go to colour 3, near black.

This also settles the open question above about how literally the power is
used: `pow(1 - n.v, g_fresnelpower)` as stored. A low-power entry with a dark
colour 3 darkens a lot at moderate angles (vanilla 171's rear panel already
mixes about halfway); the M3 E30's red #ca0000 entry (power 0.05, colour 3
0.017) reads maroon in game (`host-20261023-092609`, `observe-03`).

Fix (`paint.FINISH_PRESETS["stainless"]`): vanilla 171's entry with colour 3
0.2 instead of 0.004 (brushed steel stays light at grazing angles). In game
colour 3 0.13 (`-094028`) still left the flanks dark grey, 0.2 (`-094153`)
gives light steel sides like vanilla. Final build `out/dmc/final`
(`host-20261023-094929`, `observe-06`): a light brushed-steel body with the
black rear surround, next to vanilla 171's `observe-02` in the same run.
The black rear surround and grey bumper are Forza's (and the real car's);
the vanilla DSF model paints them.

### The S5S Raptor glowed (user report on `out/testdrive9`, ID 22)

Symptom (confirmed-runtime): the yellow body bloomed into an orange halo over
the roof, the windows and the silhouette in daylight (user screenshots; also
host run `host-20261024-013435`, `observe-06`).

Cause (confirmed-static): `shade` set the main colour to the Forza swatch in
linear RGB. The Saleen's only colour, #ffbf19 metallic, is (1.0, 0.52, 0.01);
its template, the security van's yellow (301: (0.567, 0.302, 0.032), colour 3
at 0.44 of colour 2, fresnel power 1), keeps colour 3 bright too. Luminance
of the fresnel mix taken literally (`paint.brightness`: at n.v 0.9, 0.7, 0.5
and area-weighted): 0.55, 0.49, 0.42, 0.48, where no vanilla car paint
(the 906 `is_template` entries) passes 0.388, 0.388, 0.388, 0.391 (the flat
greys with bright rims, 136 and 167). No other testdrive car passes the vanilla
maximum (`out/emissive/paintsurvey.py`; next brightest the 500 Abarth's cream
0.384 and the M5 E28's light blue 0.36).

Fix (`paint.shade`, `brightness_cap`): both colours of a recoloured entry are
scaled by the largest factor that keeps every measure at or below the vanilla
maximum (computed from the vanilla palettes, not a constant). Only the Saleen
changes: x0.705, colour 2 (0.705, 0.367, 0.007), a #dba314 yellow
(`install` reports `dimmed`).
In game (run `vm-20261024-060602`, `out/emissive/build2` ID 1;
`research/img/emissive-saleen-chase.jpg` against `host-20261024-013435`) the
body is a plain yellow with no halo.

### Paint shows darker than Forza (issue #289)

Symptom (player report): the Ferrari F50 GT (`FER_F50GT_96`, Forza solid
`#c00000`) drew dark maroon where Forza shows bright red; the M3 E30's
`#ca0000` read maroon in the DMC-12 runs too (above).

Cause (confirmed-static): `apparent` still took colour 2 for a low fresnel
power and colour 3 for a high one with a brighter colour 3, the opposite of
the literal mix the red/blue probe showed. `shade` matched the F50's red to
the MP4-12C's (225) red entry by its colour 2 (1.0, 0, 0) and put the swatch
(0.527, 0, 0) there, with colour 3 at 0.016. With power 0.05 and bias 0.75
98% of colour 3 shows: `#300000` at n.v 0.9. Read as the mix, `apparent`
was off the shown luminance by more than 25% on 735 of vanilla's 906 car
paint entries: the low-power ones (whites with colour 2 0.9 to 1.0 and colour
3 0.02 show 0.14 to 0.2) and the flat or high-power ones with brighter rims.

How bright DSF paints (offline-checked): on the 50 vanilla cars Forza also has
(`handling_dsf.PAIRS`), the vanilla entries show (area-weighted mix) about 0.3
of the linear luminance of the nearest Forza colour of the same car: whites
0.25 to 0.33 against Forza's 0.86 to 0.99, the Ruf's yellow (flat) 0.33
against 0.58. Matching each Forza colour to the car's vanilla entries by
chromaticity and luminance at a gain g, the median shown/swatch ratio is 0.24
at g 0.2, 0.31 at 0.3, 0.35 at 0.4 and still 0.42 at g 1 (`dsff paint
survey`, `twin_median_ratio`); the fit cost is lowest at 0.3 to 0.35. The
swatch as is was too bright wherever it showed: the DMC-12's flat `#999999`
(0.32) read white, the S5S Raptor glowed. So the target is 0.3 x the swatch
(`SHOWN_GAIN`), and the brightness cap (the S5S note) stays as a guard; it
dims one roster entry now (the Continental SS's `#fff5a7`, x0.996) where it
dimmed 25.

Roster (`tools/batches/all.txt`, 573 cars, 3,231 palette entries; 2,807
measured, leaving out 423 near-black ones and the DMC-12's stainless; shown =
area-weighted mix luminance):

| | master | this fix |
| --- | --- | --- |
| shown under a third of the swatch's luminance (the issue's measure) | 1,537 | all (0.3 by design) |
| shown outside ±25% of the target (0.3 x swatch) | 2,254 on 533 cars | 0 |
| of those too dark (< 1/3 of the target) | 478 | 0 |
| of those too bright (> 1.25 x the target) | 1,166 | 0 |
| median, 10th and 90th percentile of shown / target | 1.05, 0.16, 2.66 | 1.00 |
| vanilla's own entries as Forza colours (832; 73 brighter than 0.3 per channel left out): shown outside ±25% of their own | 520 | 0 |

So master was both: low-power templates (most reds, blues, dark colours) far
too dark, flat and high-power ones (silvers, greys, whites on the flat
templates) up to 3x brighter than vanilla. Silvers come out darker than on
master (the TSX's `#a6abab` 0.33 to 0.12, the Grand Cherokee's `#9aa1ab`
0.29 to 0.11), reds, blues, oranges and yellows on low-power templates
brighter (F50 GT 0.005 to 0.034 (red channel 0.025 to 0.158), 370Z blue
0.009 to 0.048, Boss 429 orange 0.019 to 0.135).

In game (confirmed-runtime; master and this branch built for 8 cars,
`-Palette 0`, chase and `grille` views, vanilla 126 (red) and 224 (McLaren
F1 white) for reference; runs `vm2-20261029-232404` / `-232914` and, for
the F50 GT, which as the first probed car lost its exterior shots,
`vm2-20261029-234033` / `-234402`; `img/paint-289-f50-m3-tsx.jpg`,
`img/paint-289-ingame.jpg`): the F50 GT goes from dark maroon to bright red
like Forza's, the 370Z from navy to Forza's bright blue, the Boss 429 from
brown to Grabber orange; the S5S Raptor stays a plain yellow (no glow), the
DMC-12 is unchanged (stainless preset), the M3 E30's white is lighter (still
a little cream next to the F1's white), the Firebird stays black with a
faint green (Forza's `#0f140d` is green-tinted). The TSX's `#a6abab`
silver goes from near white to a mid grey: on the twin cars light metallic
greys show 0.32 of the swatch (median of 54), so 0.3 is right on average
for silvers, but no vanilla silver was in the run to compare (open).

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
`out/lights/ab2/` sample the paint block ((255, 198, 255)) on 100% of paint triangles.

### Trim and two-tone

Forza draws trim (`black`, `matte_colors`, `frame`, `bottom`...) with its own
techniques, which go to DSF's shared trim material, so trim is never painted.
Inside the paint techniques (`body`, `body_2`) almost every triangle samples
alpha-0 texels: 9,391 of 9,431 on the 370Z (40 dark opaque ones: painted
now), all on the M5 and the Skyline. `body_2` is paint too (the 370Z's and
M5's door shuts and wing vents, the Skyline's wings), not a second colour.

### Open

1. Answered: ports show their factory colours in game since the paint-UV fix
   (the DMC-12's and S5S Raptor's runs above, `vm-20261024-060602`).
2. Which entry a spawn picks (deterministic per car in the runs so far) and
   whether the garage lists all entries.
3. Answered: paint UV1 is the damage-decal UV (`damage.md`).
4. Wheel-blur colour (third set) from the Forza rim colour.
5. A brushed grain: the paint shader has no detail or normal map of its own
   (UV0 samples one flat block of the shared atlas; UV1 the shared damage
   decal), so a brushed-steel pattern would need a non-paint material.
6. Answered (#380): `ColorShaderSettings<N>.xml` feed `body_settings` (satin, lobes,
   two-tone); `MetalColor` isn't mapped.
