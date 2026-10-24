# Car draw distance, pop-in and distant lamps

Updated: 2026-10-23 (runtime measurements on the host)

Why traffic cars pop into view and grow parts as they approach, and why their
brake and position lamps stand out when far away. Covers the traffic spawn
radius, the vehicle LOD selector and its per-preset thresholds, which parts
each MODL LOD has, and the lamp flare sprites with their distance curves.
Each section ends with a patch plan for the user's other mod, DSF Enhanced
(`C:\Users\admin\Documents\GitHub\driver-sf-mods`, `src\dsf_enhanced.cpp`).

Evidence tags as elsewhere: `confirmed-static` (read in Driver.exe or data),
`confirmed-runtime` (measured in game; since 2026-10-23 on the host with
`run-series.ps1 -OnHost`), `hypothesis`. Addresses are
VAs in the stock Driver.exe 1.04 (image base 0x400000; DSF Enhanced uses
RVAs = VA - 0x400000). The engine singleton is `[0xFE7228]`.

Tools and outputs: `out/drawdist/re.py` (capstone helpers: dump, xref, calls,
dis), Ghidra decompiles in `out/ghidra/dd-*` (project copy
`C:\Tools\GhidraProjects\driversf-forza-drawdist`), MODL survey
`out/drawdist/modl/`, lamp shader and flare asset survey
`out/drawdist/lamps/`, in-game probe `tools/game/lua/dsfforza/drawdist.lua`
(runs with `out/drawdist/probe/run-dd.ps1 -Modes <ddconfig name>`), run parsers
`out/drawdist/analyze.py` (traffic events), `traffic2.py` (drive-window
summary), `lineup.py` / `sweep.py` (lineup crops; the sweep was finally read by
eye from the crops), key crops in `out/drawdist/evidence/`. Runs are in
`%LOCALAPPDATA%\Temp\dsfforza-wsb\runs\host-*` (listed per section).

## Summary

| Question | Answer | Evidence |
| --- | --- | --- |
| Where do civilian cars spawn? | Roads within **350 m** of the player activate whole (0x772A00). Measured from the chase camera: new traffic cars first appear **318 to 666 m** away (p10 341, median 390 m), never nearer while driving, and **66 %** of them inside the view frustum. They despawn 311 to 656 m away (median 410 m). | static (0x772800, 0x7723A0); runtime |
| Does `TrafficSpawnDistanceScale` work? | **No.** The `TrafficParameters` Lua accessors are stubs, so the script finds nothing to scale (`changed=0 applied=0`). A runtime patch of the two operands does work (`run-test.ps1 -TrafficRadius`), but see plan A: 490 and 700 m bring spawns **inside 300 m**. Removed from DSF Enhanced; replaced by `TrafficSpawnRadius` (plan A, section 1). | static, runtime |
| Does the fixed radius work? | **Yes, at 500 m (user-confirmed).** `TrafficSpawnRadius=500` with `VehicleFullDetail=1` and the plan-C flare curves: "works awesome, no pop in anywhere". **700 m crashed** after about 20 s: the traffic vehicle store (500) filled and the lane-carrier pool (12,000 nodes) overflowed into it (section 1, "700 m crash"). DSF Enhanced now accepts 350 to 500 m, default 500. | user's play session; static + minidump |
| Why does a bigger radius spawn cars near the player? | **A stack overflow in the road walk.** `0x7723A0` lists the 240 m road-grid cells of the radius square in a 21-entry stack array with no bound check. Up to 360 m the square has at most 16 cells; at 490 m 25 to 36, at 700 m 36 to 49. Entry 21 overwrites the list's own count, so about half the cells are dropped, on the probe's drive **always including the player's own cell**. Roads near the player then drop out of the wanted set and come back as the overflow pattern shifts, and their cars despawn and respawn next to the player. | static (emulated: `out/drawdist/cells.py`) |
| How is a car's LOD picked? | Metric ≈ **57.5 / (d − 7.5 m)²** for the Desanne in the user's 21:9 projection (d = camera distance), against five category-2 thresholds; first match wins, no cross-fade. | static (0x6A5010, 0x6B64D0), runtime sweep |
| Real distances on High (user's 21:9) | mask 4 to **54 m**, mask 8 to **102 m**, mask 16 to **113 m**, mask 1 (wheel-less hull) to **927 m**, then culled. DSF Enhanced's ×0.25: 100 / 197 / 220 / 1,847 m. Stock 16:9 at 60°: about 1.26 × these. | runtime |
| Why does the user see spawns? | The cull (927 m) is beyond every spawn point, so each car that spawns in view pops in where it spawns, 318 to 666 m away, as a mask-1 hull. `LODEngineThresholdScale` doesn't change that (both cull distances lie beyond the spawn ring). The parts appear later, at the hull → mask 16 switch (113 m stock, 220 m with ×0.25). | runtime |
| Which category does DSF Enhanced scale? | 1, 2, 3, 4 = Terrain, **Vehicle**, Pedestrian, Driver (its comments were off by one). Vehicle is now separate (`VehicleFullDetail`, `VehicleLODThresholdScale`, `VehicleDrawDistance`); `LODEngineThresholdScale` keeps 1, 3, 4. | static (names at 0xF104B8) |
| Setter keys per slot | `0x6A4F40/0x6A4F70(cat, key)` map key 2 → slot 0 (draws mask 4), 4 → slot 1 (mask 8), 8 → slot 2 (mask 16), 16 → slot 3 (mask 1; the cull), 1 → slot 4 (nothing). DSF Enhanced's old log order "4, 8, 16, 1, 2" is therefore T1, T2, T3, T4, T0. | static (jump table 0x6A4E44 / 0x6A4E5C) |
| Do parts have their own cutoffs? | No. Each MODL LOD is a complete set. Forced in game: mask 1 has no wheels, opaque glass, whitish unlit lamp patches; mask 16 keeps wheels and glass. | static, runtime (forced masks) |
| Distant lamps more prominent? | **Yes.** A lit tail/brake lamp's DRL flare is a red streak wider than the car itself at 146 to 286 m; with flare scale 0 it is gone. Tunable from Lua (`Sfx.DRLFlare*`, verified). | static, runtime |
| Traffic tail lamps in daylight | Off unless braking: no flare streaks on any daytime traffic, unlike the lit lineup cars at the same range. | runtime (screenshots) |
| Which lamps never flare? | Headlight-only (1), centre stop (2), reverse (8), fog (0x0C), 0x12. | static (0x8E65D0) |

## 1. Traffic spawn and despawn

### Active roads (confirmed-static)

Traffic manager `[engine+0x758]`. Its update `0x770A50` keeps a sorted set of
active road directions and merges in the newly wanted set, one road add
(`0x770860`) or remove (`0x76F990`) per update, unless a forced refresh is
pending.

The wanted set is rebuilt every 30 updates (`0x772AA0`, countdown at
`+0x1E00`) by `0x772800`. For each local player (`[engine+0x210]`, 28-byte
records: position, active flag) it calls `0x7723A0(list, position, radius)`:

```text
0x7729EB  mov edx,[eax+0x754] ; cmp dword [edx],0
0x7729F6  movss xmm0,[0xDE2454]   ; 256.0  when [[engine+0x754]] != 0
0x772A00  movss xmm0,[0xDE2344]   ; 350.0  otherwise (single player)
0x772A1C  call 0x7723A0
```

`0x7723A0` walks the road grid cells around the position and samples each
road along its length from both ends. A road direction (with or against)
becomes active if any sample is within `radius` in x/z, with no height test.
The list holds at most 512 entries (`+0x1000` count, 0x200 cap). A road
leaves the set as soon as no sample is within the radius, which is the
ping-out. Ping-in and ping-out are the same radius, and a whole road
activates at once.

`0xDE2344` (350.0) and `0xDE2454` (256.0) are shared constants: 350 is also
read by the flare defaults and three other functions, and 256 by ten. Patch
the two instruction operands, not the constants.

### The road-grid cell list overflows above 360 m (confirmed-static)

`0x7723A0` first lists the road-grid cells of the square `position ± radius`:

- cell size 240 m with a +120 m offset (`0x906000`: `(v + 120) × 0.0041667`, floored);
- `0x905EA0` only checks that a cell lies inside the map grid;
- the cells go x-major into a stack array at `esp+0xBC`, 8 bytes each, and the count lives
  right after it at `esp+0x164`: room for **21 cells, no bound check**
  (`0x772474 mov eax,[esp+0x164]; lea eax,[esp+eax*8+0xBC]; ... inc [esp+0x164]`);
- `0x773C90` then sorts the cells by distance from the player's cell, and each cell's roads are
  sampled as described above.

A square of side 2r spans up to ⌈2r/240⌉ + 1 cells per axis: at most 16 cells up to r = 360 m,
which the stock 350 m respects. Beyond that the 22nd cell's x lands on the count, so later cells
overwrite earlier ones and whole columns vanish; its z goes to `esp+0x168`, the saved ebp when the
frame is 8-aligned (harmless here: the caller `0x772800` keeps an esp frame). A count that ends
up above 21 (cell x ≥ 21, world x beyond about 4,900 m) would write over the return address, and
a negative one over the loop bounds, so a bigger radius can also crash in parts of the city.
`out/drawdist/cells.py` replays the loop along the traffic probe's drive (0 to 780 m from the
dyno start):

| Radius | Cells in the square (mean) | Cells kept | Player's own cell lost |
| ---: | ---: | ---: | ---: |
| 350 / 360 | 15.3 / 16.0 | 100 % | never |
| 400 | 18.7 | 92 % | 9 of 79 positions |
| 490 | 26.1 | 48 % | 79 of 79 |
| 700 | 46.3 | 37 % | 79 of 79 |

That explains the plan-A measurements: at 490 m the wanted set lost the roads around the player
(only roads that also cross a kept cell stay), they ping out and back in as the player moves,
and the car count rose only 15 % for twice the area. `0x7727E0`, the only other path to `0x7723A0`,
has no callers.

The pools are not the limit (confirmed-static):

- active road directions: node pool of 512 at `manager+4` (free list; `+8` free, `+0xC` used),
  list sentinel `+0x501810`; the wanted list `+0x50182C` (count `+0x50282C`) stops at 512 as
  well, and since cells are sorted by distance the far roads are the ones dropped;
- traffic vehicles: `0x77AA50` (vehicle store `manager+0x56CDDC`) refuses a car once its count
  `+0x56CDE4` reaches **500**; stock has about 150 cars within 1.5 km;
- the model pick `0x77ADA0` → `0x8C5C80` can come back empty (then `0x8C9520` releases it);
- a carrier whose car could not be created stays empty; `0x77F9E0`, the lane balancer called
  when a road is activated next to an active one, retries through the same `0x77AA50`.

Each active road has lanes with "carriers", a deterministic conveyor driven
by the traffic clock (`0x775F10` interpolates a carrier's position along the
lane loop from time; the Lua help string "Reset the main clock so that the
traffic is always in the same place" fits). Cars are attached to carriers
(`0x779960` → `0x776670`), so when a road activates its cars appear along its
whole length (hypothesis from the decompile, see the runtime check below).

Per-road work is spread over 30 road phases and 4 lane phases
(`+0x50181C`, `+0x501820`).

The traffic AI drives the lamps itself: `0x779960` calls
`0x8D0470(3, 2 Brakes / 0x20 Reverse / 8, 0x10 indicators, on)`. No traffic
code sets DaytimeRunning (4) or Headlights (1), so in daylight a traffic
car's tail lamps light only while it brakes (hypothesis; see section 3).

### What DSF Enhanced's traffic options really touch (confirmed-static)

| Option | What it patches | Effect |
| --- | --- | --- |
| `TrafficSpawnDistanceScale` (removed 2026-10-23) | Lua: scales `road.trafficParameters.pingInRadius/pingOutRadius` | **None.** The `TrafficParameters` field table at 0xDF1AE8 (and its twin at 0xDF1CF0) maps every field to stubs (`xor eax,eax; ret` at 0x62E120..0x62E5B0), so reads give nil and `changed` stays 0. The script is also only dispatched from the error-message page hook (`AutoAcknowledgeUbisoftServerLostPrompt`), so it never runs in the city. |
| `TrafficCandidateWindowScale` | floats 0xF2A278 (20) / 0xF2A27C (6) | Read only by `0x770220`: the lateral/longitudinal window (6 m, 20 m) and the 45° heading test for adopting an existing vehicle onto a lane (e.g. a car the player leaves). Not spawn distance. |
| `TrafficSpawnIntervalScale` | manager `+0x503640` | A "road update pending until" time read by the spooler-busy test `0x76EEC0`; road adds are paced per update anyway. Negligible (hypothesis). |
| `InterestingVehicle*` | manager `[engine+0x6D8]` `+0x29A0/+0x29A4/+0x29A8` | Real fields: min 128 m (0xDE6010), max 256 m, 3 slots (constructors 0x7EB650, 0x7ECDA0). Interesting vehicles spawn 128 to 256 m away, inside the draw range. |
| `PreloadAllTrafficVehiclePools` | `0x64E2D0` for every traffic set record | Loads models. Traffic only spawns models that are already resident (`AllTrafficVehiclesLoaded`), and the full VEHC entry holds every LOD, so it doesn't cause part pop-in (hypothesis). |

### Runtime

- **Traffic count (confirmed-runtime, other agents' probe data).** In 468
  non-zero samples across the earlier sandbox runs in
  `%LOCALAPPDATA%\Temp\dsfforza-wsb\runs`, `register.lua`'s
  `findVehiclesInTraffic(player, 250 m, no script cars/cops)` counted:
  - median 86 civilian cars within 250 m;
  - 10th percentile 77, 90th percentile 98, maximum 107.
  - Interesting vehicles were enabled with 10 desired slots in those runs.

  So stock traffic is already about 100 cars inside the 350 m road set.
- **Spawn and despawn distances (confirmed-runtime, run
  `host-20261023-164511`).**
  - Method: `drawdist.lua` mode `traffic` moves the player's car along the
    straight from the dyno start at 20 m/s (matrix set every frame) with
    the chase camera behind it. Every 12 frames it lists
    `GameVehicleResource.vehiclesInArea(camera, 1500 m)` and logs each
    car's first and last sighting. `analyze.py traffic` and `traffic2.py`
    summarise the events.
  - Window: 5 to 39 s, after the initial population and before the player
    was dropped into Shift (about 800 m in).
  - The table counts traffic-system cars only. The frustum test uses the
    measured projection (section 2).

  | | First seen ("new") | Last seen ("gone") |
  | --- | ---: | ---: |
  | Events | 204 | 229 |
  | Camera distance min / p10 / median / p90 / max | 318 / 341 / 390 / 509 / 666 m | 311 / 350 / 410 / 525 / 656 m |
  | Inside the view frustum | 135 (66 %), all nearer than the 927 m cull | 28 (12 %) |

  So cars appear anywhere on a newly activated road (318 to 666 m), not
  only at its far end. Two thirds of them appear on screen, and since the
  High cull is 927 m every one of those is drawn: a mask-1 hull popping in
  300 to 650 m away, about 10 to 20 px wide on a 3440 px screen.

  Ping-in and ping-out happen at the same distances, as the static
  analysis says.
- **Count.** `vehiclesInArea` (all owners, 1.5 km) holds 134 to 174 cars
  (median 152) while driving, the farthest at a median 557 m. The object
  handles are recycled: 48 of 204 "new" cars reuse a handle that went
  "gone" elsewhere in the same sample.
- **Traffic tail lamps in daylight: off unless braking (confirmed-runtime,
  screenshots).** The daytime traffic shots (`out/drawdist/evidence/
  traffic-day*.png`) show unlit lenses and no flare streaks. In the lineup
  run, lit lamps on cars 146 to 286 m away flare plainly.
- The pre-reboot sandbox attempt failed on a Lua literal: this build rejects
  numbers beyond ±8,388,606. Write large sentinels as e.g. 1000.

## 2. Vehicle LOD selection

### LOD categories and records (confirmed-static)

The LOD manager has 10 categories of 5 records, 20 bytes each
(`mgr + (cat*5 + slot)*20`). The names are at 0xF104B8:

| Cat | Name | Cat | Name |
| ---: | --- | ---: | --- |
| 0 | LOD_Static | 5 | LOD_Raven |
| 1 | LOD_Terrain | 6 | LOD_Prop |
| 2 | **LOD_Vehicle** | 7 | LOD_PropFragment |
| 3 | LOD_Pedestrian | 8 | LOD_CarFragment |
| 4 | LOD_Driver | 9 | LOD_Tree |

Each record is `{f32 threshold, f32 blendRange, u32 drawMask, u32, u32}`.
`0x6A4E10` maps a record's mask name to its slot: 2→0, 4→1, 8→2, 16→3,
1→4. The selector `0x6A5010(mgr, cat, metric, &blend, &extra, scaleTable)`
walks slots 0 to 4 and returns `drawMask` of the first record whose
threshold the metric reaches, with `blend = min((metric - threshold) /
blendRange, 1)`. If none matches it returns 0. The defaults are at
0xF104E8, `mgr = [[engine+0x334] + view*0x2EB0 + 0x10]` (the global
`[0x123EDF0]`).

The vehicle draw `0x8A8DA0` (called per car by `0x8D56A0`, batched 32 at a
time by `0x8C97F0` / `0x8C7150`) calls the selector through `0x6A5120` with
**category 2** and throws the blend away, so there is no LOD dissolve. Then:

- mask 0 → the car is not drawn at all (this is the vehicle draw distance);
- mask 1 → kept only if the view is nearly level (`dot <= 0.1`, 0xEC8230),
  otherwise mask 16 is used;
- a caller flag (argument 18, from `0x8D56A0`'s caller) or two special
  render-pass IDs (`[0xF3FFF0]`, `[0xF3FFF4]`) force mask 2, the clean
  exterior entry (presumably the player's car, which has it loaded;
  hypothesis).

The metric (`0x6B6910` → `0x6B64D0`) is the summed area of three faces of
the car's oriented box, times a projection factor, divided by the squared
length of a view-space point. That point is the box centre pulled toward the
camera by the box half-diagonal. If the selector gets a scale table, it
first multiplies the metric by a per-record factor (`record+0xC` indexes
`table+0x50`).

**Measured (confirmed-runtime, run `host-20261023-163051`, Desanne):**

- metric ≈ **K / (d − a)²**, with d the camera distance, **K ≈ 57.5** and
  a ≈ 7.5 m. A fit over 11 cars at 19 to 407 m against 54 cutoffs, each
  2^(1/3) apart, leaves K in 57 to 59 for a = 7.5 to 8.
- That is 3.2× the static estimate (P00·P11·S/4 = 18). The a ≈ 7.5 m is
  more than this box's 2.9 m half-diagonal, and it is close to the 6.5 m
  between camera and player car. Whether the game measures from the player
  is open (a static camera can't separate the two).
- Beyond about 100 m the metric is simply K/d². K scales with the box's
  face area, so a bus's (S ≈ 70 m² against 21.6) is about 3×.
- Inverting it: **d(T) = 7.5 + √(57.5 / T)**.

### Graphics presets (confirmed-static)

`0xBAB070` writes category 2 from the graphics settings object. The presets
are filled by `0xBAB370` (id 0), `0xBAB2B0` (1) and `0xBAB1F0` (2), applied at
startup (`0xBAB430`) and again on a settings change (`0x6FCC90`).

| Slot (Lua index) | Draws | Preset 0 | Preset 1 | Preset 2 (High; the user's log) |
| --- | --- | ---: | ---: | ---: |
| 0 (5) | mask 4 | 0.26697 | 0.106834 | 0.026697 |
| 1 (1) | mask 8 | 0.0643125 | 0.0032125 | 0.00643125 |
| 2 (2) | mask 16 | 0.00511777 | 0.0025 | 0.00511777 |
| 3 (3) | mask 1 | 6.8e-5 | 6.8e-5 | 6.8e-5 |
| 4 (4) | none | 1e-5 | 1e-5 | 1e-5 |

The user's `dsf_enhanced.log` prints category 2 (in mask order 4, 8, 16, 1,
2) as `[0.006, 0.005, 0.000, 0.000, 0.027]`, which is preset 2. With
`LODEngineThresholdScale=0.25` all five are divided by 4, so every switch
distance doubles.

### Projection in the test and on the user's screen (confirmed-runtime)

The host harness runs a 1280×720 window. DSF Enhanced's borderless mode still
makes a 3440×1440 backbuffer, so its ultrawide patch acts as on the user's
screen:

- aspect constant 0x9E50B0 = 2.389;
- FOV scalar 0x9D9A80: 0.017453 → 0.023453 (×1.344).

The probe logs `CameraSystem` `fov` = 1.40717 rad (80.6°, i.e. stock 60° ×
1.344; 1.4346 while the car was rolling) and `aspect_ratio` = 2.389.

Screen positions of the lineup cars give the projection:

- P00 ≈ 1.19, from the cars' lateral pixel offsets;
- P11 ≈ 2.75, from their ground-contact rows;
- so `fov` is the horizontal FOV, P11 = P00 × 2.389, and the 16:9 test
  image is the user's 21:9 projection squeezed sideways.

Every distance below is therefore the user's in-game distance. Compared with
stock 16:9 at 60° (P00·P11 = 5.33 against 3.33), the user's metric is 0.62×,
so on stock 16:9 every distance is 1.26× longer. A proper Hor+ (keeping the
16:9 vertical FOV) would give 0.71×. DSF Enhanced's linear FOV scaling widens
the vertical FOV from 36° to 39°.

### Real switch distances (confirmed-runtime)

Lua `performance.SetLODThreshold(2, i, v)` maps i = 5, 1, 2, 3, 4 to slots
0 to 4. The forced-mask phases confirm what each slot draws: 4, 8, 16, 1,
nothing.

`GetLODThreshold` returns nothing, so there is no readback. The player's
car is exempt: it stays drawn with every threshold at 1000.

| Setting (user's 21:9) | mask 4 until | mask 8 until | mask 16 until | mask 1 (hull) until | culled beyond |
| --- | ---: | ---: | ---: | ---: | ---: |
| High (preset 2) | 54 m | 102 m | 113 m | 927 m | 927 m |
| High × 0.25 (DSF Enhanced now) | 100 m | 197 m | 220 m | 1,847 m | 1,847 m |
| Stock 16:9, 60°, High | 66 m | 127 m | 141 m | 1,170 m | 1,170 m |

Mesh contents:

| Mask | What it has (MODL survey; forced in game) |
| --- | --- |
| 4 | everything, 7,270 triangles median (part 0) |
| 8 | 0.29× mask 4; looks the same at range |
| 16 | 0.15×; wheels, glass and lamp glow; no exhausts, suspension or hubs |
| 1 | 172 triangles; hull without wheels, opaque glass, whitish unlit lamp patches |

So a car is the hull from 113 m to 927 m. The visible "parts appear" step
is hull → mask 16 at 113 m, or 220 m with × 0.25. The fog of the ClearDay
mood ends at 1,000 m.

**Correction to the static estimate** (cull "330 to 510 m", "the 0.25
option makes spawn pop-in more visible"):

- The stock cull is 927 m, beyond the farthest spawn (666 m), and so is the
  × 0.25 cull.
- In both cases a car that spawns in view pops in at its spawn point, as a
  hull. `LODEngineThresholdScale` changes how much detail the car has
  between 100 and 220 m, not where it pops.
- Lowering the cull below the spawn ring would not hide spawns either. A
  car spawned beyond the cull then pops in when it crosses the cull line,
  which is nearer.

Crops: `out/drawdist/evidence/sweep*.png` (sweep), `masks.png`, `p07.png`.

### Other DSF Enhanced quality options (confirmed-static)

- `MapDetailLODThresholdScale` scales category 0 (Static).
- `LODDistanceScale` rescales pixel-shader constants c10-c15. Vehicle LOD
  and flares are chosen on the CPU, and the lamp and vehicle shaders have
  no distance term except fog (`out/drawdist/lamps`), so it can't help here.
- Shader LOD (`performance.SetEnableShaderLOD`, flag 0x123EE98) is off unless
  Lua sets it.
- `ImposterDrawDistance` calls `0x6B3560` on the imposter manager
  `[engine+0x18]` (2 banks, stride 0x43F0). The vehicle draw never consults
  it, and no imposter code is on the vehicle path (confirmed-static).
  `Imposters.GetISystems()` returns nil in the city (runtime), so which
  systems it holds is still unknown; most likely city scenery (hypothesis).
  `performance.GetEnableShaderLOD()` returns nil too.

## 3. MODL parts per LOD (confirmed-static)

Full details: `out/drawdist/modl/summary.txt`, `features.txt`, `lamps.txt`.
Each LOD is a complete, separate set: all 43,790 stock submodels belong to
exactly one LOD record. The per-submodel u32 after the UID list is a
render-class flag (5 opaque, 7 alpha-blended, 1 needles and mirror), not a
distance, and nothing else in VEHC is a cutoff.

| Feature, median triangles (vehicles that have it) | m4 | m8 | m16 | m1 | ext (m2) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Part 0 total | 7,270 | 2,072 | 1,071 | 172 | 13,962 |
| Mirrors | 208 | 58 | 36 | 0 | 424 |
| Lamp triangles (light byte ≠ 0) | 157 | 62 | 46 | 0 | 267 |
| Glass | 166 | 54 | 30 | – | 323 |
| Trim | 1,420 | 429 | 205 | – | 2,288 |
| Chrome | 742 | 158 | 57 | – | 1,400 |
| Exhausts | 83 | 28 | 0 | – | – |
| Suspension | 170 | 4 | 0 | – | – |
| Wheel part (tyre + rim + blur) | 748 | 208 | 138 | none | 1,039 |

Mask 1 has a Lights submodel in 144 of 150 cars, but its vertices carry light
byte 0. Its lamps can't glow, so beyond the 16 → 1 switch only a flare
sprite can show a lit lamp.

## 4. Lamps at distance: the flare sprites

### Lamp geometry (confirmed-static, out/drawdist/lamps)

The Lights / Lights_LOD1 shaders add `g_v4LightColours[light byte].rgb ×
texel alpha` and then apply fog. Nothing depends on distance or screen size,
so the lamp mesh gets dimmer with fog, not brighter.

### The flare system (confirmed-static)

- **Manager:** `[[engine+0x2F0]+8]`, created in `0x510010`. The material
  is `R:\Resources\Misc\VehicleLights\VehicleLights.matxml` in
  `media/SfxMisc.fchunk`: a 256×256 DXT5 star with horizontal streaks, drawn
  by `DiffuseMapOnly.fx` (program 5), alpha-blended, no depth write. The
  installed `SfxMisc.fchunk` is dated 2026-07-07 while the stock files are
  2012, so it may be modded.
- **Per frame:** `0x8C72E0` walks every vehicle in the render manager's
  list (`+0x82D8`) that has a light component, so this does not depend on
  the car's LOD or frustum result. For each it calls `0x8E8670`, which:
  - computes the camera distance once per car (`0x50B480`);
  - emits nothing if the car is within 10 m of the camera;
  - fades in from 10 to 20 m, using the per-lamp distance there;
  - emits one sprite per lit flare lamp (`0x8E68E0` → `0x50BE10`).
- **Lamp tests:** the lamp's direction (UPVH light table +0x10) must face
  the camera (`dot >= 0`, 0x120D170), and the lamp must be in front.
- **Budget:** 128 sprites per frame (`+0xF4`, sorted u16 keys at `+0x5158`).
  The key is `clamp(d × 0.255, 0, 255) << 8`, so the nearest 128 lamps win.
- **Which lamps flare:** `0x8E65D0` sets per-lamp flag 0x80 ("DRL flare")
  when bit (type − 1) of 0x139C047C is set, and 0x40 ("emergency flare")
  from 0x45500 (lamp types as in `lights.md`).

| Flare | Lamp types (b3) |
| --- | --- |
| DRL flare | 0x03 DaytimeRunning (tail and front position lamps), 0x04/0x05 indicators, 0x06/0x07 brake + indicator, 0x0B, 0x13, 0x14/0x15 DRL + indicator, **0x18 DRL + Brakes (the usual tail/stop lamp)**, 0x19/0x1A, **0x1D Headlights + DRL** |
| Emergency flare | 0x09, 0x0B, 0x0D, 0x0F, 0x13 (sirens) |
| No flare | 0x01 Headlights, 0x02 Brakes (centre high stop lamp), 0x08 Reverse, 0x0C Fog, 0x12, 0x16, 0x17, 0x1B, 0x1C |

A flare draws while its lamp is lit (lamp state `0x8E6760`: flag 0x200 off,
flag 1 broken). Its colour is the lamp colour scaled by the lamp's current
brightness (`[0x14551F0]` light config; brightness rises when the brake
state is added). An 0x18 tail/stop lamp on a traffic car that is only
braking therefore flares while it brakes, and brighter than a plain tail
lamp would.

### Distance curves (confirmed-static)

Two curve sets: emergency at 0x120D198 and DRL at 0x120D1F8, 0x60 bytes
each (`dist[3][4]`, `val[3][4]`, columns scale, transparency, brightness,
unused). They are filled by the static initialiser `0xD31C30` and evaluated
by `0x50B6C0`:

- two linear segments, `d < d1` and `d >= d1`;
- no upper clamp, so values keep growing past the last point;
- results clamped at 0 from below;
- the curve input is `camera distance × viewport FOV factor × 0.819672`
  (`0x50BB20`, 0x120D0F0), where the factor is the camera's FOV from
  `0x50BCD0` (vfunc +0x2C). A wider FOV, such as DSF Enhanced's ultrawide
  FOV scalar, makes flares behave as if farther away.

| Curve | Distances | Values |
| --- | --- | --- |
| DRL scale | 6, 80, 300 | 0, 0.5, 1.5 (2.4 at 500, 3.3 at 700) |
| DRL transparency | 0, 20, 300 | 0.5, 0.5, 0.5 |
| DRL brightness | 0, 20, 300 | 1, 2.5, 2.5 |
| Emergency scale | 0, 150, 350 | 1, 4, 10.5 |
| Emergency transparency | 0, 150, 350 | 0.3, 0.5, 1 |
| Emergency brightness | 0, 20, 300 | 1, 2.5, 0 |

Beyond about 80 m the DRL scale grows almost linearly with distance, so the
sprite keeps a roughly constant size on screen while the car shrinks. That
is the "distant lamps are drawn more prominently" effect the user noticed.
It is real and deliberate.

**Lua control** (confirmed-static; library `Sfx`, table 0xE13DB0, name
0xE13E60): `Sfx.DRLFlareScale(dists, values)`, `DRLFlareTransparency`,
`DRLFlareBrightness`, `EmergencyFlare*`, and `Dump*FlareData` (which only
formats into a buffer). Each takes two 3-number tables. `0x67F770` rejects
distances that are not strictly increasing; a missing or invalid table
leaves that half unchanged. Setter `0x50B410(type, attr, dists, values)`.
Stock Lua never calls these.

**On screen (confirmed-runtime, runs `host-20261023-163051` and
`-165139`; crops `out/drawdist/evidence/flares*.png`, `candidates-far.png`):**

- With brake lamps on and the default curves, cars 146 to 286 m away show a
  pink-red horizontal streak wider than the car itself. At 286 m the streak
  is about 7 px wide (1280 px image) on a 5 px car.
- With `DRLFlareScale` values 0 the streaks are gone, and the lamps of
  those cars are dull. Their mesh is the hull, mask 1, whose lamp patches
  render whitish and unlit. Boosted (scale 1.5/4, alpha 1, brightness 4),
  the streaks cover whole cars from 36 m out.
- DaytimeRunning alone lights the 0x18 tail lamps and flares them like the
  brakes do.
- `Sfx.DRLFlare*` calls take effect at once and return without error.
- So the static picture holds:
  - beyond the 113 m mask 16 → 1 switch, the flare is the only red on a
    braking car;
  - its near-constant screen size makes far lamps stand out.

## Patch plans

(Written before the implementation; what shipped differs: a 500 m traffic
radius with the cell list moved, and `VehicleFullDetail`. See
"Implemented in DSF Enhanced" and the Summary. "DSF Enhanced's × 0.25"
below is its old setting.)

All three are for DSF Enhanced. The values are for the user's setup:
3440×1440 with DSF Enhanced's ultrawide FOV, so K = 57.5 and
d(T) = 7.5 + √(57.5/T) for a Desanne-sized car. On stock 16:9 at 60°,
multiply every distance by 1.26.

**What decides where cars pop in** (runtime, section 1):

- With a cull beyond the farthest spawn, about 670 m at stock radius, a car
  that spawns in view pops in where it spawns: 318 to 666 m away, median
  390 m. That is already the case on stock High (927 m) and with today's
  × 0.25 (1,847 m).
- A cull nearer than the spawn ring only moves the pop inward.
- A bigger radius moves most spawns outward, but it also adds spawns close
  to the player (plan A).

So the achievable fix:

1. Keep the radius at 350 m.
2. Keep the cull beyond the spawn ring.
3. Replace the far hull with mask 16 so the popped-in car already has
   wheels and glass and no part step follows.
4. Calm the far flares.

### Recommended settings (user's 21:9)

| Item | Value | Distance it gives | Stock High |
| --- | --- | --- | --- |
| A: traffic radius scale | **1.0** (remove the no-op Lua script; don't ship the operand patch yet) | spawns 318 to 666 m | same |
| B: T0, Lua 5, slot 0 → mask 4 | **6.7e-3** | mask 4 to 100 m | 0.026697, 54 m |
| B: T1, Lua 1, slot 1 → mask 8 | **1.9e-3** | mask 8 to 181 m | 0.00643125, 102 m |
| B: T2, Lua 2, slot 2 → mask 16 | **6.8e-5** (= T3) | mask 16 to the cull | 0.00511777, 113 m |
| B: T3, Lua 3, slot 3 → mask 1 | **6.8e-5** (keep), or slot 3's drawMask 1 → 16 | cull 927 m | 6.8e-5 |
| B: T4, Lua 4, slot 4 → none | 1e-5 (keep; slot 4 draws nothing) | – | 1e-5 |
| C: `DRLFlareScale` | **{6, 80, 300} → {0, 0.45, 0.6}** | streak at 286 m becomes a small dot | {0, 0.5, 1.5} |
| C: `DRLFlareBrightness` | **{0, 20, 300} → {1, 2.5, 1.2}** | fades to 0 near curve input 560 | {1, 2.5, 2.5} |
| C: `DRLFlareTransparency` | keep {0.5, 0.5, 0.5} | | |

Notes on these values:

- **Cull.** Any T3 ≤ about 1.2e-4 (cull ≥ 700 m) keeps the cull beyond the
  spawn ring. Going lower (e.g. DSF Enhanced's 1.7e-5, 1,847 m) is
  harmless; the fog ends at 1,000 m.
- **Other vehicles.** K scales with box area, so small cars switch nearer
  and trucks and buses farther. A city bus is about 3×: its cull is about
  1.6 km on High.
- **Cost.** Mask 16 instead of the hull from 113 to 927 m is about 1,071
  instead of 172 triangles per car (part 0 median). For the roughly 150
  cars within 1.5 km that is about 0.16 M triangles at most.
- **As scale factors on High:** T0 × 0.25, T1 × 0.30, T2 × 0.0133,
  T3 × 1. A single `LODEngineThresholdScale` can't express this.
- **Tested.** The C350, C700 and candidate-flare phases of run
  `host-20261023-165139` show the thresholds and curves take effect as
  computed:
  - with cull 300 m, the 206 m and 286 m cars are still drawn;
  - with the candidate curves, the 286 m streak becomes a small red dot.
- **Not tested.** The recommended T2 = T3 = 6.8e-5 combination itself was
  not run (the coordinator paused game runs). The table follows from the
  measured K.

### A. Traffic radius (problem 1)

- **Remove** the Lua `pingInRadius` script and its server-prompt dispatch
  (`TryApplyTrafficSpawnRadiusLuaPatch`); it can't work.
- **The operand patch works (confirmed-runtime).** Point the two operands
  at plugin-owned floats, after checking the original bytes:
  - `0x772A04` (350 m; bytes `F3 0F 10 05 44 23 DE 00` at 0x772A00);
  - `0x7729FA` (256 m; bytes `F3 0F 10 05 54 24 DE 00` at 0x7729F6).

  `run-test.ps1 -TrafficRadius R` does exactly this with `VirtualAllocEx`
  and `WriteProcessMemory` once the world is up. Don't write 0xDE2344 or
  0xDE2454 themselves: other code reads them.
- **Measured effect** (same drive, 5 to 41 s; runs `host-20261023-165913`
  and `-165602`):

  | Radius | Cars within 1.5 km (median) | Farthest car (median) | New cars: median / p10 distance | New cars < 300 m (in view) |
  | ---: | ---: | ---: | ---: | ---: |
  | 350 (stock) | 152 | 557 m | 390 / 341 m | 0 (0) |
  | 490 | 175 | 675 m | 492 / 210 m | 98 (29) |
  | 700 | 339 | 908 m | 633 / 224 m | 192 (51) |

  - A bigger radius moves most spawns out, but it brings in new cars 20 to
    300 m from the player, many on screen. That is worse pop-in than stock.
  - Some of these near cars reuse a handle that vanished 300 to 600 m away
    in the same sample. Others are fresh, so this is not only recycling.
  - Cause (found 2026-10-23, static): the operand patch alone overflows
    `0x7723A0`'s 21-cell stack list above 360 m (section 1), which drops the
    player's own cell; 490 m added only 15 % more cars for that reason. The
    pools (512 roads, 500 vehicles) are not reached.
  - These two runs are therefore not evidence about larger radii; `-TrafficRadius`
    in `run-test.ps1` still patches only the operands. Use DSF Enhanced's
    `TrafficSpawnRadius` (cell list moved to a plugin buffer) instead.

### B. Vehicle LOD and cull distance (problem 1)

- **Use the selector, not the category list.** `LODEngineThresholdScale`
  also rescales Terrain (1), Pedestrian (3) and Driver (4). Make Vehicle
  separate, with the five values above.
  - It is applied once, so a graphics-settings change (`0x6FCC90` →
    `0xBAB070`) restores the preset silently.
  - Re-apply each frame, or hook `0xBAB070`'s tail.
- **Get rid of the mask-1 hull.** Either set T2 = T3 (the 16 → 1 band is
  then empty; Lua `performance.SetLODThreshold(2, 2, T3)` works), or write
  the drawMask field (+8) of the category-2 slot-3 record from 1 to 16:
  `mgr + (2*5 + 3)*20 + 8`, where `mgr = [[[0xFE7228]+0x334]+0x10]`.
- **Don't pull the cull inside the spawn ring.** It makes cars pop in
  nearer, at the cull line. If a nearer cull is wanted for performance, put
  it where the fog hides it, not near 300 m.
- **Ultrawide (corrects the static estimate):**
  - The metric goes with P00·P11. DSF Enhanced scales the horizontal FOV
    linearly with aspect a: fov_h = 60° × a / (16/9), P00 = 1/tan(fov_h/2),
    P11 = a·P00.
  - For 21:9 (a = 2.389) that is 0.62× the stock 16:9 metric, so every
    switch is 21 % nearer, not 14 %. The table above already includes it.
  - For another aspect: K = 57.5 × P00·P11 / 3.33, and distances scale
    with √K.
- **Risks:**
  - more triangles and draw calls (batches of 32);
  - category 2 is shared by the Shift-view renderer (`0x8F12D0`) and
    per-view managers, and DSF Enhanced only patches view 0.

### C. Distant brake and position lamps (problem 2)

- **Zero-patch Lua route (confirmed-runtime):** call
  `Sfx.DRLFlareScale/Brightness/Transparency(dists, values)` once the world
  is up. DSF Enhanced runs Lua through `0x607AB0` / `0x5F1CA0` from the
  wrong hook; use a world-ready or per-frame hook.
- **Or write the table directly:** floats at
  `0x120D1F8 + row*16 + attr*4`:
  - rows 0 to 2 are distances, rows 3 to 5 values;
  - attr 0 is scale, 1 transparency, 2 brightness.
- **Values:** see the table above. Notes on them:
  - Scale beyond 300 continues the last segment's slope: 0.6 + 0.00068/m.
  - Brightness falls 0.0046 per curve unit past 300 and is clamped at 0, so
    far flares fade out instead of floating beyond the car.
  - The curve input is camera distance × the camera's FOV factor × 0.82
    (`0x50BB20`); the factor's exact value is unverified.
- With plan B's mask 16 to the cull, braking cars keep lamp glow on the
  mesh at range, so the flare is no longer the only lit thing.
- **Lamps that never flare:**
  - Brake-only centre stop lamps and headlight-only lamps are excluded
    (`0x8E65D0` mask 0x139C047C).
  - Adding them changes little in daylight: traffic lamps are off unless
    braking (runtime).
- **Risks:**
  - Flares draw from the render list even beyond the LOD cull.
  - `SfxMisc.fchunk`'s VehicleLights material may be modded on the user's
    install (dated 2026-07-07).
  - Brightness above about 4 saturates.

## Implemented in DSF Enhanced (2026-10-23, working tree, not committed)

`src/dsf_enhanced.cpp` and `src/dsf_enhanced.ini` in
`C:\Users\admin\Documents\GitHub\driver-sf-mods`:

- **Removed:** `TrafficSpawnDistanceScale`, the Lua `pingInRadius` script
  (`TryApplyTrafficSpawnRadiusLuaPatch`), its dispatch from the error-message page hook (that hook
  now serves only the Ubisoft prompt) and from `DrawIndexedPrimitive`, whose hook went too.
- **Plan A, `[Quality] TrafficSpawnRadius`** (metres, 350 to 500, shipped 500; 350 = stock, no patch;
  the 500 m cap is the pool limit found in the 700 m crash).
  At startup, after checking every original byte (all or nothing):
  - the five cell-list instructions point at a plugin buffer of 1,024 cells: `0x77247B`,
    `0x7724B7`, `0x7724E4` `lea eax,[esp+eax*8+disp]` → `lea eax,[eax*8+buffer]` (`8D 04 C5`),
    `0x7724C0` `lea edx,[esp+0xC4]` → `mov edx,buffer` + `66 90`, `0x7724DD`
    `lea edi,[esp+0xD0]` → `mov edi,buffer` + `66 90`;
  - then the operands at `0x772A04` and `0x7729FA` point at plugin floats R and R × 256/350.
- **Plan B:** `VehicleFullDetail` (T0 = T1 = T2 = T3, so mask 4 out to the cull),
  `VehicleLODThresholdScale` (when full detail is off), `VehicleDrawDistance` (metres for a
  mid-size car via K = 57.5 × P00·P11 / 3.33 from the plugin's own aspect and FOV scalar; 0 keeps
  the preset). With a patched radius the cull is kept at least at R + 350 m. `LODEngineThresholdScale`
  now covers Terrain, Pedestrian and Driver only. Every category is re-checked every 250 ms
  and re-applied when the game rewrites it (settings change → `0xBAB070`), per slot, so a check
  that lands mid-rewrite doesn't scale a value twice.
- **Plan C, `[Lights] DRLFlareScale / DRLFlareTransparency / DRLFlareBrightness`** as
  `d1:v1, d2:v2, d3:v3`, written into the table at `0x120D1F8` once the static initialiser has
  filled it, re-checked every 250 ms. Shipped: scale `6:0, 80:0.45, 300:0.6`, brightness
  `0:1, 20:2.5, 300:1.2`, transparency unchanged.
- **`[General] TrafficDiagnostics`**: every 2 s, fps (a `Present` counter, hooked only with this
  option) and the counts above (wanted roads, active roads, active lanes, vehicles).

Harness: `run-series.ps1 -Override` (and `run-dd.ps1 -Override`) now take root files,
e.g. `dsf_enhanced.asi=out\drawdist\asi\dsf_enhanced.asi;dsf_enhanced.ini=out\drawdist\asi\r700.ini`;
`run-test.ps1` copies them over the run folder's copies (an overridden ini is not rewritten).
Test builds and inis: `out/drawdist/asi/` (`stock`, `full`, `r500`, `r700`, `r700full`; all with
`TrafficDiagnostics=1`, `RemoveVSync=1`, other LOD categories at 1.0). `traffic2.py` now also
counts "new" cars nearer than 300 and 200 m.

### 700 m crash (user's session 2026-10-23 17:53, `out/drawdist/crash-700/`)

Build of 17:45 with `TrafficSpawnRadius=700`, `VehicleFullDetail=1`, `TrafficDiagnostics=1`.
`TrafficDiag` every 2 s: wanted roads 146 to 172 (all active, far below 512), active lanes 240 →
410 → 492, vehicles 325 → **500** and then 480 to 500 for about 20 s, then `vehicles=1153122238`
(0x44BB3FBE = the float 1498.0), then the crash.

- **Crash:** access violation at `0x8CD230` (`mov al,[ecx+0x5E7]`), ECX = 0x43937D70 = the float
  295.0, called from `0x780940` in the lane update `0x780730`
  (`pfVar15[3]`, a vehicle record's vehicle pointer). Minidump stack: `0x780940`, `0x773FA0`,
  `0x770EA7` (traffic manager update).
- **What overflowed (confirmed-static):** the lane-carrier pool at `manager+0x503648`:
  - `+0` bump pointer / free-list head, `+4` free count, `+8` used count, nodes of 0x24 bytes from
    `+0xC`, list sentinel at `+0x6978C`: **12,000 nodes**, 0x69794 bytes;
  - allocated by `0x77BD30` (called through `0x77BCD0` from the lane builder `0x77CF80`, once per
    carrier, when a road is added): take a free node, else `add [pool],0x24`, with **no bound
    check**; freed by `0x77BC60`; reset by four identical copies `0x77BC20/40/90/B0`;
  - a lane gets about (lane length + 1,500 m hidden loop) / spacing carriers (at most 100 /
    `[template+8]`), about 24 per lane here, so ~490 lanes need ~12,000;
  - the vehicle store `manager+0x56CDDC` sits right behind it (`0x503648 + 0x69794`), so node
    12,000 onwards writes carrier data (floats) over the store's header (its count at `+8`) and
    first records, including their vehicle pointers.
- **Second limit:** the vehicle store refuses cars at 500 (`0x77AA50`, `cmp [esi+8],0x1F4`;
  records 0x2FC bytes, list sentinel `+0x5D43C`). At 700 m demand exceeded 500 from about 410
  lanes on. Carriers whose car can't be created stay empty and are filled later, wherever a car
  frees up, which can be near the player.
- **Limits at stock density:** on this route vehicles reach 500 at ~410 active lanes and
  carriers 12,000 at ~490 lanes; scaled by area that is roughly 640 m and 690 m. 500 m (about
  250 lanes, ~300 cars, ~6,000 carriers here) leaves about 2× headroom; denser districts have
  less. Going beyond 500 m needs both pools enlarged: the carrier storage can move to plugin
  memory by changing only the reset's start pointer (all access is through the bump/free
  pointer and the sentinel list; references found with `out/drawdist/immsearch.py`), the vehicle
  store needs its 500 check and storage moved; per-vehicle engine limits beyond the store were
  not audited. Work stopped here at the user's request.
- **`TrafficDiag fps=0.0`:** the `Present` hook on the device vtable (index 17) installs but
  never counts, so the game (or DriverNGHook) presents another way. Unfixed; FPS not measured
  here (the test VM can now measure it: `tools/wsb/guest/fps.ps1`, PresentMon).

## Open

1. Probe numbers for the fixed radius (the user confirmed 500 m by eye: no pop-in): spawn
   distances and counts at 500 m, and the frame-rate cost of `VehicleFullDetail` (the
   plugin's FPS counter doesn't work, see the 700 m crash notes; `tools/wsb/guest/fps.ps1`
   measures it in the test VM). Plan (not run; the 700 m variants now get
   clamped to 500):

   ```powershell
   foreach ($v in 'r700','stock','full','r700full','r500') {
     .\out\drawdist\probe\run-dd.ps1 -Modes traffic -Override "dsf_enhanced.asi=out\drawdist\asi\dsf_enhanced.asi;dsf_enhanced.ini=out\drawdist\asi\$v.ini" }
   # per run: python -I out\drawdist\analyze.py traffic <run>; python -I out\drawdist\traffic2.py <run>
   # and the TrafficDiag / TrafficRadius / LOD / Lights lines in <run>\dsf_enhanced.log
   ```

   Pass criteria: `TrafficRadius: patched` in the log; with r700 no "new" traffic car nearer than
   300 m while driving (stock has none) and the median new-car distance near R + 40 m;
   `wantedRoads` below 512; `vehicles` well below 500; fps of `full` against `stock` and
   `r700full` against `r700` over the drive window (5 to 39 s). Then pick the shipped
   `TrafficSpawnRadius` (now 500) and `VehicleFullDetail` (now 1).
2. Whether the LOD metric's 7.5 m offset is a distance from the player car
   rather than the camera. Test in cockpit view or with a free camera. Also
   why K is 3.2× the static formula.
3. The chase camera's `fov` grows with speed (1.4072 rad parked, 1.4346 at
   3 mph). The switch distances at speed are slightly nearer; not measured.
4. The recommended B + C combination as a whole, in traffic, with
   screenshots of a spawn at 300 to 600 m. The harness's observe shots in
   `-Lights` mode carry no timestamps, so a spawn can't yet be matched to a
   frame.
5. The flare FOV factor (`0x50BCD0`), the sprite's world size per unit of
   scale (`0x50BE10`), and the near-fade value (record +0x80).
6. Whether `[[engine+0x754]]` (256 m radius) is split screen or online.
7. What uses category 1 (Terrain) records; DSF Enhanced scales them too.
8. Why the player gets dropped into Shift about 800 m into the traffic
   drive. The probe now travels 780 m.
