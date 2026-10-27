# Cockpit view: interior entry, steering wheel, gauges, head camera

Updated: 2026-10-27

How DSF draws the cockpit view, where Forza keeps the same data, and what
`src/dsfforza/convert/cockpit.py` (`--cockpit` in `forza2dsf.py`, on by default in
`port_car.py`) builds from it. Host: Saloon 15 (ASYM Desanne, spool 266,
VEHC 3/4/5). Evidence levels follow `dngvehicles.md`. First in-game run
and the fixes that followed: see "First in-game test and fixes" below.

## What DSF draws (confirmed-static unless marked)

**The interior entry (N+1000, kind 1) is the whole cockpit model.** Its
MODL has four parts, all with one mask-64 LOD:

| Part | Desanne contents | LOD link |
| --- | --- | --- |
| 0 | 7 submodels: cabin, seats, dashboard, steering wheel, door cards, roof lining, glass, and a low-detail copy of the outer body (hood, fenders, rear deck; one paint submodel spans z -2.48..2.38) | 0, chassis record |
| 1, 2 | empty | 5 and 2 (rear and front bumper records) |
| 3 | the gauge needles, one stride-20 submodel (Dials material) | 1, the gauge-cluster record |

The full entry (kind 0) has no mask-64 LOD anywhere (all 32 parts checked),
so the cockpit view takes nothing from it. Because the interior carries its
own outer body for the window views, I take the cockpit view to draw the
interior entry alone (hypothesis; the exterior entry would z-fight with
the interior's copy of the hood).

Interior materials (Desanne): two local MACOs, `_Interior` (stride 28,
MODL value 5) and `_Dials` (stride 20, MODL value 1,
`g_hAlphaTestRefValue` 0.5), both sampling the interior's own TEXC (512ВІ
DXT5, 10 mips). Shared engine materials: `10122b9653b14e2a` (paint, stride
32; like the exterior's paint it samples the shared paint atlas, not the
vehicle texture: `lights.md`, "Paint"), `0012cc8576953429` (glass, MODL value 7), TRIM
`0012833b12fe1027`, `00128338d7fd1027`. One submodel uses the full entry's
Chassis material. Eight stock interiors ship DXT1 textures, so DXT1 is
accepted in kind-1 TEXCs.

**The steering wheel is skinned, not a part.** Vertex semantic 9 (4 x u8,
undecoded until now) is a hierarchy binding: `(record +0x34, record +0x2A,
light index, 1)`. Pairs seen on the Desanne match UPVH records exactly,
for example (20, 23) is record 68 (slot 20, model 23), (8, 9) record 36, a
rear door. Chassis vertices carry (0, 0, 0, 1). The steering wheel's
vertices in all three entries carry (1, 1, 0, 1): record 11, kind-0 part 30,
hinge 0. Hinge 0's pivot is the wheel hub and its axis the steering column
((0, 0.413, -0.911), 24.4В° up from horizontal). The marker with role 0x2F
(47) sits on the hub, its quaternion turns (0, 0, -1) onto the column.

Fleet check (`out/cockpit/skin_survey.py`): for every stock car, take the
hinge whose pivot is within 5 cm of the role-47 marker, build
`(+0x34, +0x2A, 0, 1)` from its record, and compare it with the most common
semantic-9 value among the interior's vertices on the wheel rim. 146 of 146
match; 4 vehicles (123, 131, 243, 289) have no role-47 marker or no hinge
there.

**The UPVH "seat" table is the gauge table.** Each 0x30 entry is the needle
pivot (vec4), the dial-plane quaternion (turns (0, 0, 1) onto the dial
normal, which points into the dash), a u32 gauge id (1 tachometer, 2
speedometer), a u32 sweep (degrees per 5000 rpm or per 80 mph; Desanne
speedometer 95, tachometer 137) and a byte flag at +0x28. The needle
submodel's semantic 8 (the vertex declaration calls it
`DashboardInstrument`) is (u, v, gauge id, 0), so one submodel holds both
needles and the engine rotates each about its own pivot. The table's
records (type 18) are children of the record with role 185, whose hinge
sits on the cluster. How the engine turns the needles: "Gauges" below.

## Gauges (confirmed-static; confirmed-runtime 2026-10-22)

The chain, traced in Ghidra (`out/ghidra/gauges*`):

1. `FUN_008902C0` (UPVH parse) keeps a pointer to the gauge table at
   spec+0x487C (count: header byte 0x2C, entries 0x30).
   `FUN_0088E310` stores, per gauge record (type 18), the record index at
   spec+0x47D4 + seat*4, and only for gauge ids below 8 (others get seat
   -1). `FUN_00876770`/`FUN_00874700` copy the table pointer to the
   render object (+0xABC) and the seven record indices to +0x104.
2. Inputs, `FUN_00891670` (per player, an object of 0x98 bytes at
   `[mgr+4] + 0x2251C`, `FUN_00887D70`), for gauge id = index + 1:
   - id 1: engine rpm (`FUN_008C27D0`), moved at most 150 rpm per update,
     clamped to [p40, p42] (idle, redline), times 0.0002 (`DAT_00E39EE0`);
   - id 2: |speed| x 2.23704 (m/s to mph, `DAT_00DE2EB4`), clamped to
     [p606, p605], times 0.0125 (`DAT_00DDB93C`). VO3D p605 defaults to 255
     and p606 to 0; 46 stock cars set p605 (DeLorean 88, Eldorado 120:
     their speedometers' last marks), one sets p606 (15);
   - ids 3 to 7 get no input (always 0); cases 5 and 6 write other shader
     constants, not gauge inputs.
   `FUN_0087A8E0` copies the seven inputs into the render snapshot
   (+0x944) along with a byte (+0x9D0) set when the lights are off.
3. `FUN_008A4660` (and `FUN_008A4970`) build `g_InstrumentTransforms`
   (shader constant, name hash 0xF8B7F22A in `DAT_00F400F8`; 8 matrices):
   for each gauge record, `FUN_008A2610(entry, input, out)` makes the
   matrix from the entry's quaternion and pivot and turns it by
   `sweep x pi/180 x input` radians about the quaternion's z axis through
   the pivot (`FUN_00514150`, D3DX convention: right-handed, so with the
   normal pointing into the dash a positive angle is clockwise for the
   driver). If the entry's +0x28 byte is set and the snapshot byte is
   clear, the matrix is zeroed (the needle disappears); no stock car sets it.

So the needle mesh's rest pose is 0 rpm / 0 mph, the u32 is needle degrees
per 5000 rpm (tachometer) or per 80 mph (speedometer), and a
counter-clockwise needle needs the dial normal flipped (the Pacer's
tachometer and the Aston Martins' do exactly that: their quaternions turn
(0, 0, 1) onto a normal pointing out of the dash). The tachometer never
shows less than idle or more than p42; the speedometer stops at p605.

Data check (`out/gauges/dump.py`, all stock cars in `out/gauges/stock.txt`):
the stock 370Z (227) has 100 (speedometer) and 127 (tachometer); its dial
runs 0 to 180 mph and 0 to 9000 rpm, which at these rates is 225 and 229
degrees, and the Forza 370Z's needle poses sweep 226.6 and 230.4 degrees
over the same marks. `cockpit.py gauges` (below) on stock 227 puts the
needles on 0, 60 and 120 mph and 0.65, 3 and 6 thousand rpm at those
readings (`img/gauges-stock-227.png`).

### What the converter does with them

`cockpit.gauge_ticks` reads `CarAttribs.xml` `<Gauges>`: per family
(`Speed`, `Tach`, `Fuel`, ...) tick marks `(Param, Value)`, Param 0..1 from
the min to the max needle pose, speed values in m/s, tach in rpm.
`calibrate_gauge` takes angle = Param x the fitted min-to-max sweep and fits
angle = a + b x value (mph or rpm) over the ticks, then:

- gauge-table u32 = round(|b| x 80) or round(|b| x 5000);
- b < 0 (a counter-clockwise needle): the dial normal is flipped;
- the needle's min pose is turned by a about its pivot so it sits on 0
  (370Z -0.4 and -0.2 degrees, M5 -2.1 and 0, Skyline -3.1 and -2.2);
- VO3D p605 = the last speed mark in mph (370Z 180, M5 205 = 330 km/h,
  Skyline 113 = 182 km/h), p606 = 0.

Without a tick table the sweep is taken as 0 to 160 mph or 0 to 8000 rpm.
DSF's needle is linear; Forza dials whose marks are not evenly spaced are
fitted (worst tick off: 370Z 0.8 degrees, M5 4.2, Skyline 3.8, FH1 Raptor
speedometer 12.9). Kilometre dials need nothing special: the fit is in mph.

| Car | Speedometer (deg / 80 mph, host's) | Tachometer (deg / 5000 rpm, host's) | p605 |
| --- | ---: | ---: | ---: |
| NIS_370Z_10 (host 227) | 101 (100) | 128 (127) | 180 |
| BMW_M5_12 (host 147) | 92 (88) | 146 (137) | 205 |
| NIS_SkylineGTR_93 (host 229) | 170 (102) | 120 (130) | 113 |

The host has two gauge entries (ids 2 and 1); fuel, temperature and other
needles can't get a gauge without new UPVH records, and would get no input
anyway (ids 3 to 7 stay at 0). They stay static: fuel, oil, water,
temperature and voltage at their mid pose, boost and others at rest
(`PARK_MID`). Forza's `digital_gauge` subsections (370Z 56 triangles, M5
24, Raptor 4) draw digits with a shader. Through the first UV transform
their UV0 lands on the label sheet; through the second it lands on the
display's own face where the car has one, and those faces are now drawn
textured ("Digital clusters" below); the rest stay a blank near-black
display (`DISPLAY_COLOUR`). A car whose
cockpit has no speed or tach needle (a digital speedometer) simply has no
vertices with that gauge id; the host's entry then draws nothing.

`uv run dsff cockpit gauges <archive> --id N --png out.png --at 0,0 --at 60,4000`
renders the cluster from the head camera with the needles turned exactly
as the engine would at each (mph, rpm), clamps included (works on stock
cars). Offline renders of the three test cars at 0/0, 40/2000, 60/5000
and 100/8000: `img/gauges-offline-370z.png`, `img/gauges-offline-m5.png`,
`img/gauges-offline-skyline.png`; every needle sits on the matching mark.

In game (`in-game-tests.md`, "Gauges"): both needles move on all three
cars and read within about a minor mark of the HUD speed
(`img/gauges-ingame-clusters.png`).

### Static needles in the 40-car build (2026-10-23)

User report on `out/testdrive7`: no converted car's needles move. Cause
(confirmed-static): `damage.DamagePlan.interior_uv1`, which writes decal
UV1 into the rebuilt interior's 32-byte vertices, picked buffers by "semantic
8 is four halves" from the VDCL. The needle buffers (stride 20) match that
too, and their semantic 8 is DashboardInstrument (u, v, gauge id, 0), so
the gauge id became a decal UV (0.15 to 0.98). In `out/testdrive7` all 34
cars with needles had no vertex with id 1 or 2 (`out/gaugefix/needle_ids.py`);
the other six have no needles (S2000, Agera, LFA and Range Rover clusters
are digital; Durango and Express are stub cockpits). Every testdrive build
from the first (`out/testdrive`, 2026-10-22 15:10) had it; `out/r7`
(04:02 that day) did not, matching the earlier in-game confirmation. In game
the needles of the Saleen S5S Raptor (ID 22) don't show at all at 30 and
60 mph (run `host-20261023-174440`, `img/gauges-static-testdrive7-22.png`).

Fix: `interior_uv1` skips semantic 8 with VDCL name hash 0x75FAD367
(`DASHBOARD_INSTRUMENT`; every stock needle buffer, stride 20 or 16, has
it, and no other interior buffer does). `cockpit.needle_gauge_ids` counts
the ids in the finished interior, and `forza2dsf` raises if any is not 1
or 2 and puts the counts in the report (`cockpit.interior.needle_ids`).

Offline check of the rebuilt cars (`out/gauges/build`: S5S Raptor, M3 E30,
370Z, Charger Daytona, M5; `out/gaugefix/build2`: Golf R, TT RS, SVT
Raptor, Supra, Huayra): ids intact, and `cockpit.render_gauges` at 0/0,
30/2000, 60/4000, 90/6000 (`out/gaugefix/offbig-*.png`, `img/gauges-offline-charger-fixed.png`,
`offbig2-*.png`, `img/gauges-offline-golfr.png`) puts the Charger's and S5S Raptor's needles on the
matching marks.

In game (run `host-20261023-234741`, confirmed-runtime): `out/gauges/build`
with `-HoldMph '30,60,90'`, IDs 147, 1 to 5. Every car's speedometer and
tachometer needles now draw and turn with speed and rpm
(`img/gauges-ingame-fixed.png`; HUD readings in the labels). S5S Raptor
near 30 at 29 mph, then about 60 and 90 on its 0 to 240 face; 370Z near
30 and 95 at 29 and 94 mph; the M5's km/h dial low at 29 mph and near the
top of the arc at 84; Charger and M3 E30 needles move too (partly hidden
by the rim and hands). The tachometers rise with each forced speed. The
HUD and the shots lag each other by a frame or two, so this is a
plausibility check, not a tick-by-tick one.

Calibration across the 40 cars (`out/gaugefix/calib_survey.py`: DSF's
linear needle against Forza's tick table interpolated linearly):

- Within about 5 degrees from 20 mph up on most cars. Dials with
  very uneven marks can't be matched by one linear needle: Golf R (+31
  degrees at standstill, -19 at 60 mph; its km/h face has 0 to 20 spread
  out) and TT RS (+20, -29 at 60 mph) read about 20 km/h when parked; SVT
  Raptor +6 at 0 and -13 at 90 mph; Supra +12 at 60 mph, Charger tach +10
  at 6000 rpm, S65 tach +18 at 6000 rpm.
- Negative rest turns (M3 E30, RX-7, DMC-12 -14 degrees; Civic -10; 250
  GTO tach -16) put the needle a little below the first mark at
  standstill. On these dials the first mark is above 0 (a stop pin at 20
  km/h or 20 mph).
- No speedometer needle in the Forza cockpit: S65 AMG (digital) and 250
  GTO (no speedometer); their host speed entries draw nothing.

**Head camera.** FUN_0088DF80 case 0x14 (disassembled with capstone,
`out/cockpit/88df80.asm`) calls FUN_0088D3B0 to find the first record of
type 3 with role 0x2F and returns `(p430 - marker.x, p431, p432)`. Camera
vectors are in a frame turned 180В° about y: chase-camera z values are
positive, and across the fleet `p432 в‰€ 0.5 - marker.z` (bus: marker z 5.31,
p432 -4.78; Desanne: 0.49, 0.03). So in model space:

    eye = (marker47.x - p430, p431, -p432)

Stock eyes sit 0.25 to 0.37 m above and about 0.5 m behind the steering
marker. Only one stock car sets p430. `cockpit.head_camera()` computes this
from an archive.

The mirrors table (two vec4) holds the side mirror positions (records 37
and 42, the mirror parts).

## Forza side (confirmed-static)

`<car>_cockpit.carbin` parses with `src/dsfforza/formats/carbin.py` unchanged, in both
games (FH1 370Z; FM4 370Z, F40, Countach, F1, Golf R). LOD 0 only. Sections
on the 370Z: `interior` (24,192 vertices), `a_pillar`, `seatL/R`,
`steering_wheel`, `mirrorL/R`, `glassF/LF/RF`, `hooda`, `headlightL/R`,
`cagerace` (an upgrade), and needles `speed_min/mid/max`,
`tach_min/mid/max` (FM4 cars add `fuel_*`, `boost_*`). A section's offset
is its bounding-box centre, not a pivot.

The main carbin's `interior`, `seatL/R` and `steering_wheel` sections (LOD
1 to 4) are part of `stock_sections`, so they go into the full and exterior
bodies. That is right: the stock exterior also carries a low-detail cabin
with a skinned steering wheel.

Textures, by sampling each technique's UV triangles in every candidate
(`out/cockpit/atlas_probe.py`):

| Techniques | Texture |
| --- | --- |
| `gauge*`, `z_gauge_emissive_alpha`, `digital_gauge`; `badge` in cabin sections | `interior_LOD0.xds` (512ВІ: dial faces, switch labels; alpha is the label cut-out mask). The needle swatch is red-orange (252, 45, 8) |
| cabin sections' `leather`, `plastic`, `metal`, `headliner`, `bump_*`, `reflector`, ... | `interior_LOD0.xds` too: the cabin's leather, plastic and metal artwork and its colour swatches (corrected 2026-10-23, see "Cabin trims sample interior_LOD0"; this row used to say the strips along the bottom of `nodamage_LOD0`) |
| outside sections (`a_pillar`, `hood*`, `mirrorL/R`): `chrome`, `black`, `plastic2`, `badge`, ... | `nodamage_LOD0.xds` (2048ВІ; badges there since 2026-10-24, "Badges seen through the windscreen") |
| lamps | `lights_LOD0.xds` |
| `mirrorLeft/Middle/Right` | render targets in Forza |

`forza_materials.classify_cockpit` encodes this.

**Eye point.** `gamedb.slt` table `CameraOverrides` (FH1 167 rows, FM4 503)
has `CamCockpitOffsetX/Y/Z`. It is the eye relative to the steering wheel
centre with +z forward, so in Forza space (front at -z):

    eye = wheel centre + (x, y, -z)

On the 370Z (0.05, 0.22, -0.43) this puts the eye 0.28 m ahead of the
headrest and 0.17 m under the headliner; the Countach, F1 (centre seat,
x в‰€ 0) and others land just as plausibly. The other z sign would put the
eye ahead of the wheel. A third of the rows are all zero ("no override");
for those the converter uses the median of the non-zero rows, (0, 0.21,
-0.46). `CarAttribs.xml` has `DriverHandsOffset` and gauge tick tables but
no camera.

## What the converter does

`cockpit.fit()` measures the Forza cockpit:

- steering wheel: axis = least-spread direction of its vertices; rim = points
  at 75% of the largest radius or more; centre = middle of the rim's extent
  in the wheel plane (a vertex mean is biased by uneven tessellation);
- needles: the min, mid and max poses are one mesh rotated about the pivot.
  The dial normal is the least-spread direction of the displacements, and
  each step is a 2D rotation whose fixed point is the pivot. Sweeps come out
  clockwise, 230В° to 290В°;
- side mirror glass centres; eye from `CameraOverrides`.

`forza2dsf.convert(..., interior=)` with `ConvertOptions.cockpit`:

1. Skins the main carbin's `steering_wheel` in the full and exterior entries
   to the host's wheel record (semantic 9 = (1, 1, 0, 1) on the Desanne).
2. Rebuilds the interior entry with `Placement`: the cockpit sections minus
   upgrade parts (`forza2dsf.stock_sections`, as on the outside: `cagerace`
   and the extra bonnets `hoodb`.., `hoodCIrace`, `hoodPSrace` that 112 of
   572 cockpit carbins carry, all drawn over the stock `hooda` until
   2026-10-24; `forza-materials.md`, "Chrome trim and the Boss 429's black
   parts") and the headlights, needles without a DSF gauge as static
   geometry (fuel and temperatures at mid, others at min; see "Gauges"), and the main carbin's remaining
   body sections at LOD 5 as the outer shell. The steering wheel gets the
   same skinning. Textured subsections use the host's `_Interior` material
   through a new atlas in the interior's TEXC (DXT1, the host's size;
   cabin sections from `interior_LOD0` at 4x the weight of `nodamage_LOD0`,
   which only the outside sections use; there, alpha-0 fill is made near
   black first and trim texels are lifted to a floor of 40, see "Light
   cockpit trims"; label regions get a dark neutral where alpha is below
   half). Paint
   (`INTERIOR_PAINT`) and glass use the host interior's own shared materials;
   trims go to `_Interior` on a flat cell ("Shared-trim garbage in
   cockpits"). Speed and
   tach needles (min pose) go into the host's needle part on `_Dials`, with
   the gauge id in semantic 8 and an opaque solid cell of the Forza needle
   colour.
3. Drops triangles never seen from the head: an ID buffer per cube-map face
   (256ВІ) from the final head camera and a head-movement volume around it
   (`CULL_OFFSETS`: 12 cm either side, 8 cm up, 6 cm down, 8 cm forward and
   back) plus the Forza eye, glass transparent, everything within 25 cm
   kept, plus one ring of neighbours (see "Holes in converted interiors").
   On the IS F, 43,824 of 65,314 triangles stay.
4. If the MDXN is over the limit (the largest stock interior, 0x12D000), it
   vertex-clusters the cockpit with growing cells (4 to 30 mm), then moves
   the shell to its coarsest LOD. None of the three test cars needed it.
5. Patches the full entry's UPVH: the steering hinge pivot and axis, the
   role-47 marker (position and quaternion; hinge and marker at the wheel
   centre plus the hand shift along the column, VO3D p538/p559 zeroed: "Driver's hands",
   "The steering wheel turns about its hub"), the speed and tach gauge
   entries (pivot, dial quaternion, sweep from the tick table), the gauge cluster hinge pivot (mean
   of the gauges), the mirrors table, and the driver/passenger seat markers
   (roles 10/11: where they put the steering marker at the stock point of the
   seat's frame, "The driver's seat follows the wheel").
6. Writes VO3D 430 to 432 from the eye: `p430 = marker.x - eye.x`,
   `p431 = eye.y`, `p432 = -eye.z`.

## Test archive

`out/cockpit/ported-cockpit.sp` (`port_car.py`, host 266, defaults), lint
clean, `chnk.py rebuild --verify` identical:

| Car | Spool IDs (VEHC) | Interior MDXN | Triangles kept | Eye (DSF) | VO3D 430 / 431 / 432 | Eye source |
| --- | --- | ---: | ---: | --- | --- | --- |
| NIS_370Z_10 (FH1) | 304 / 1304 / 2304 (450-452) | 0x120000 | 33,364 of 62,522 | (0.337, 0.217, -0.172) | 0.050 / 0.217 / 0.172 | CameraOverrides |
| BMW_M5_12 (FH1) | 305 / 1305 / 2305 (453-455) | 0x10C800 | 27,919 of 68,012 | (0.398, 0.218, -0.068) | 0 / 0.218 / 0.068 | zero row: median |
| NIS_SkylineGTR_93 (FM4, RHD) | 306 / 1306 / 2306 (456-458) | 0x11B800 | 29,983 of 82,240 | (-0.396, 0.136, -0.096) | 0 / 0.136 / 0.096 | zero row: median |

No clustering was needed (cell 0 mm). The Desanne's own values are 0 / 0.49
/ 0.03 with the marker at (0.425, 0.164, 0.495). Interior atlas scales:
370Z nodamage_LOD0 0.234x and interior_LOD0 0.469x, M5 0.176x / 0.352x,
Skyline 0.369x / 0.738x. At the M5's scale, small white button icons blur
into blobs.

Renders in `out/cockpit/`: `<car>-kind1.png` (`vehicle_mesh.py --lod 64`),
`<car>-combined.png` (top row interior entry, bottom row exterior plus
interior; cutaways at the driver's plane, above the eye and behind it;
yellow eye, cyan steering marker, magenta gauge pivots) and `<car>-eye.png`
(perspective from the head camera, glass left out).

## First in-game test and fixes (2026-10-22)

Run `20261022-015851` (`out/cockpit/ported-cockpit.sp`): the cockpit camera
switches to the converted interior, the head camera sits at the Forza eye,
the steering wheel turns with the hands on it. Three defects, all found and
fixed offline (game testing paused before a confirming run):

1. **Nearly black cockpit** (`img/cockpit-v1-a.png`, `img/cockpit-v1-skyline.png`):
   only the two needle pivots were visible. Cause (confirmed-static): the
   fourth position component is a baked sky-light term (`vehicle-model.md`)
   and every converted vertex had 0 except the needles (4096). Stock
   interiors have median 0.21. Fix: `forza2dsf.vertex_sky` from Forza's
   baked AO; converted interiors now have median 0.20 to 0.25. The same
   zero made the converted bodies' paint black. Semantic 9's third byte
   ("light") is 0 on every vertex of the Desanne's interior and glass, so it
   isn't the cause.
2. **Cracked windscreen**: glass UVs outside the clean band; see
   `vehicle-model.md`. Interior glass is fixed the same way.
3. **Driver's head through the roof** in chase view
   (`img/skyline-chase-v1.png`, `img/fh1-370z-v2-textured.png`): the driver
   model sits on the UPVH driver-seat marker (role 10, `upvh.md`), which kept
   the Desanne's position (y -0.306, its eye 0.796 above), while the eye and
   roofs moved down. Fix: `cockpit.patch_seats` puts the role-10 marker
   under the new eye with the host's eye-to-seat offset, and role 11
   (passenger) at the mirrored x (since replaced: the seat now follows the
   steering marker, "The driver's seat follows the wheel"). Offline check (`out/glassdbg/verify.py`,
   head top taken as eye + 0.12 m): in the tested archive the Desanne seat
   puts the head top at y 0.61 against roofs at 0.38 to 0.55 over the eye (through by 0.06 to
   0.23 m, matching the screenshots); with the moved seat roof - eye is
   0.23 / 0.33 / 0.25 m (stock: median 0.25, minimum 0.15). Part of the low
   eye came from a placement bug: Forza bodies sat 0.14 to 0.23 m too low
   on their wheels (`forza-sources.md`), now fixed, which lifts eye, roof
   and seat together.

## M5 windscreen streaks (2026-10-22)

The M5's windscreen showed broad white streaks from the inside in fix2 and
fix3 (`img/fix2-m5-cockpit.png`, `img/fix3-m5-cockpit.png`); the stock
Desanne cockpit glass is clean (`img/fix3-desanne-stock-cockpit.png`).
Ruled out offline: a second windscreen layer (the shell skips every section
the cockpit carbin has, so the main carbin's glassF never lands in the
interior), flipped or noisy normals (vertex normal within 1-2В° of the face
normal, median, on all three cars), the sky term (0.6-0.7, like the
Desanne's interior glass), semantic 5's last two bytes (128/127 on stock
glass too).

Cause (confirmed-static; fix confirmed-runtime in run `20261022-030613`,
`img/fix5-m5-cockpit.png`): the interior glass
material `GLOBAL_Glass_LOD5` samples the same texture as the exterior glass,
and the part of its clean band that stock interiors use is not uniform: a
grey smear texture with long horizontal streaks at u 0-0.12, v 0-0.06,
flat black below it (`vehicle-model.md`, "The glass texture"). Two thirds
of stock interior glass triangles sit in the black block u 0.06-0.12,
v 0.125-0.25. Converted interiors spread their glass over the whole clean
band, so the smear rows landed on the windscreen. Fix4 squashed u into the
0-0.12 strip but kept v 0-0.25, and the M5 then showed thinner diagonal
streaks along the iso-lines of v (`img/fix4-m5-cockpit-streaks.png`). Fix5
maps interior glass into the black block only
(`forza2dsf.INTERIOR_GLASS_RECT`). The 370Z and Skyline showed fewer streaks
because less of their windscreen area mapped onto the smear rows; the M5's
glassF UVs run diagonally across the glass.

Superseded (2026-10-23): the black block is discarded by the glass pixel
shader (blue 0), so fix5 removed interior glass altogether, and in the
shattered band it showed stretched shard streaks. The flat smear patch that
followed never showed cracks; interior glass is now laid out like stock
(next section).

## Cockpit glass damage (2026-10-23, confirmed-runtime)

User report on `out/testdrive7`: from the cockpit camera converted cars'
windows never crack or shatter, while the exterior glass does. Cause and
fix: `vehicle-model.md`, "Interior glass". In short, stock interior windows
draw damage with their own geometry: crack-line slivers on rows of the
glass texture that only show in states 1вЂ“2, and rim shards that only show in
state 3, over a smear surface that shows in states 0вЂ“2 (on the windscreen
only a frame round an open centre). Converted interiors had one uniform
smear patch. `glass.InteriorGlass` now builds the three layers on every
converted window, on that window's panel record.

The stock layout also supports the hypothesis above that the cockpit view
draws the interior entry alone: a stock interior windscreen is open in the
middle and carries its own crack lines, which would be pointless over the
exterior glass. In run `host-20261023-174148` the stock 227's windscreen
centre stayed clear until shattered.

**In-game test** (run `host-20261023-174148`, `out/cockpitglass/build`:
IDs 1 M5 (host 147), 2 RX-7 (278), 3 370Z (227), 4 Challenger (173);
probe IDs 147, 227, 1вЂ“4; `run-series.ps1 -GlassLadder -ShotSeconds 3`).
`-GlassLadder` (new; `clonetest.lua` `GLASS_STEPS`) switches to the cockpit
view on a clean car, then calls `GameVehicleResource.applyDamage
{gameVehicle, damage = 0.25 / 0.5 / 1.0, location = (0, 1, 2), override}`
at cockpit ticks 8, 16 and 24, then `fullVisualDamage` at 32, logging each
step. Glass has no Lua call of its own (`lua-api.tsv`); `applyDamage`
raises the glass panels' crack states. The game ran at about 150 fps, so
a tick (30 frames) took 0.2 s and each step about 1.6 s (observe times in
`events.json`): 2вЂ“3 cockpit shots per car. Results (`out/cockpitglass/run1-sheet.png`,
`run1-m5-zoom.png`, `run1-227-vs-370z.png`, `run1-rx7-challenger.png`):

- M5: crack lines across the windscreen's left half and the left door glass
  after 0.25 (observe-11); after 0.5 smear gone and jagged shards along
  both edges of the A-pillar (observe-12, state 3).
- 370Z: faint top/left frame when clean (observe-22), a crack line after
  0.25 (observe-23), shards along the windscreen's top edge after 1.0 and
  fullVisualDamage (observe-24, -25). Stock 227 at 0.5: its top-left shard
  cluster (observe-06).
- RX-7 and Challenger: shards along the windscreen top after 1.0 (RX-7
  observe-18, -19); the Challenger's clean and 0.5 shots show the frame
  (observe-28, -29); after 1.0 the game dropped to the Shift view
  (observe-30), as it did for the M5 after fullVisualDamage (observe-13).
- No crash, no white streaks, no glass missing in the clean state.

Run 1's build drew shards 1.5вЂ“4 cm wide every 2.5 cm, a near-continuous
fringe on the 370Z's top edge where stock 227 shows a corner cluster; they
are now 1.2вЂ“3.5 cm every 3.5 cm, and gaps inside a window no longer count as
rim.

**Run `host-20261024-000609`** (final `out/cockpitglass/build`, same IDs,
`-GlassLadder -ShotSeconds 1`; 85 shots, 7вЂ“8 per cockpit ladder; strips
`out/cockpitglass/run2-ladder.png`, zooms `run2-cracks-zoom.png`,
`run2-227-370z.png`): no crash. M5: clean, then two crack lines from the
windscreen's top edge after 0.25 (observe-29 against -27). 370Z port: faint
cracks after 0.25, smear gone and shards along the top edge after 0.5 and
1.0 (observe-61, -63). Stock 227 at the same steps shows no clear shards in
view this time (run 1 showed its top-left cluster at 0.5), so the crack
state per hit varies between spawns. The M5 and Challenger dropped to the
Shift view after the 1.0 step, as in run 1. Stock 147's ladder again ran
before the first shot (the zoom phase); the first shot of a series comes
after `-ZoomPresses`.

**40-car survey** (`out/cockpitglass/testdrive`, `tools/batches/testdrive.txt`,
`out/cockpitglass/survey.py` against `out/testdrive7`;
`survey-testdrive.txt`): 40 built, all lint ok, no extra interior
decimation. Every car has a windscreen frame of 0.10вЂ“0.28 mВІ with
0.83вЂ“1.15 m of state-1 and 0.67вЂ“0.95 m of state-2 cracks; shards seen from
the eye 16вЂ“115 per windscreen (fewest where the dash hides most of the rim:
Cuda 0.13 of the rim seen, Firebird 0.15, Mustang 0.30, 205 T16 0.31). Up to
eight cars (the first in list order) were built before gaps inside a window
stopped counting as rim; the rest with the final code. A replay on all 40 `out/testdrive7` interiors
(`fastsurvey.py`, windows grouped by record and position because section
names are gone) puts every triangle in its region; its stray mid-window
shards on the 250 GTO, 300SL and 2000GT came from that grouping, not the
converter (real builds `out/cockpitglass/outliers`, `300sl-states.png`,
`oldcars-states.png`).

The Express worker died once in a gentle-mode batch (no traceback, not a
timeout; log ends after the full entry); it built cleanly standalone and in
the 8-job batch (800 s), so it was outside the converter. The crash exposed a
`port_batch` bug: the retry (triggered because the log tail says "stub
cockpit carbin") renamed a report the dead worker never wrote and took the
whole batch down; `run_workers` now renames only files that exist.

## Shared-trim garbage in cockpits (2026-10-22, fixed offline)

Runs `20261022-040320` / `-040545` (`out/r7/ported-r7.sp`): the Mustang's
wood steering rim and dash panel showed a scrambled pattern of white blocks
and labels (`img/r7-mustang-cockpit-damaged.png`), the rear-view mirrors of
the Mustang and Raptor and the Skyline's side mirror showed unrelated
texture cells, a speckled band ran under the M5's cluster, and the
Raptor's wheel top had white smears.

Cause (confirmed-static): subsections the cockpit atlas can't hold went to
the host interior's "trim" material with Forza's own UVs: mirror glass
(`mirrorLeft/Middle/Right`, a render target in Forza) and tiling detail
textures (the Mustang's `wood`, UVs beyond 0..1). `host_interior` picks
that trim by stride and MODL flag, and on hosts without the usual trim it
picked something else (Mustang host 193: its Chassis_LOD1 material, 4,231
triangles; Raptor host 251: `10122b9753b14e2a`, 1,754), so the Forza UVs
landed on arbitrary parts of those textures. The offline renderer draws
shared materials flat, which hid it. Thin-strip and noise detail
techniques (`bump_stitching`, `mottled`) smeared into neighbouring cells at
the atlas's 0.2x scale. The UPVH mirror table only places the side
mirrors; no stock interior material is a mirror (the shared material at
stock mirror positions, `10122b9753b14e2a`, is also on grilles and rear
details of 116 interiors), so mirrors are drawn flat.

Fix (`cockpit.CockpitBinding`): those subsections use the interior's own
material with one flat atlas cell: mirrors (46, 50, 56), wood (84, 48, 26),
chrome/metal (150, 150, 155), else (34, 34, 36); `FORCE_FLAT` sends
stitching and mottled there too. No converted interior uses a shared trim
material any more (`out/cockpit2/shared_count.py`: only paint, glass and
the local Interior/Dials). The Mustang's interior swatches are pure
(249, 0, 0) at full alpha; nodamage texels now get brightness 0.85 and
saturation 0.55 after the trim lift (`CockpitOptions.trim_tone`,
`dsf_materials.Texturing.tone`), which turns that red into (148, 31, 31).
Interior paint: `host_interior` took the largest stride-32 shared material as
paint. On host 193 that is GLOBAL_LOD_Chrome (`a012f01306d57a2a`, 3,700
triangles against 2,179 of paint), so the Mustang's shell and painted
interior parts drew with chrome (the white-speckled bonnet from the
cockpit, the red A-pillar on a black car). Every stock host checked (227,
147, 229, 193, 251, 266) paints its interior with `10122b9653b14e2a`; the
converter now uses it whenever the host interior has it, as all checked
hosts do (`cockpit.INTERIOR_PAINT`), and the engine
trim `0012833b12fe1027` (193 has none; the old pick took its
Chassis_LOD1). Only 193 was affected. Offline: `img/cockpit2-mustang-raptor-before-after.png` (left r7, right
the fix), `img/cockpit2-offline-eyes.png` (all five). Not yet run in game.

## Cars without a usable cockpit carbin (2026-10-22, offline)

The 31-car batch fell back to the host interior for the FH1 Dodge Durango
and Chevrolet Express: `cockpit.fit` stopped on "no steering_wheel section".
Their `<car>_cockpit.carbin` is a placeholder: every section (interior,
seats, needles, mirrors) is a 17-vertex box about 0.2 m wide, and the
`_LOD0` atlases next to it hold another car's leftovers (the Durango's
`nodamage_LOD0` shows Prius badges). The Ford Taurus SHO's has 8 triangles
per section; three FH1 cars (Explorer, F350 Super Duty, Focus ZX4) have no
cockpit carbin at all.

`src/dsfforza/convert/cockpit_survey.py` runs `cockpit.fit` over every car on the discs
(573 in `research/forza-roster.tsv`), reading the cockpit carbin, main
carbin and CarAttribs straight from the ISOs, and writes
`out/cockpit-survey/survey.tsv`. Before the fixes: 554 fitted, 13 stub
cockpits and 3 without one failed, 2 stubs that passed the size test lacked
a steering wheel (Taurus SHO, Kenworth T440), and the FM4 CTS-V and others
lost their needles because the min/mid/max poses are welded differently
(136/135/137 vertices).

What `fit` does now (`main_sections` = the main carbin):

- **Stub or missing cockpit carbin** (`is_stub`: under 1000 triangles, an
  `interior` section under 100, or under 0.5 m across): the cabin comes
  from the main carbin's `interior`, `seatL/R` and `steering_wheel` at their
  finest LOD (`promote_lod`), textured from the main atlases
  (`CockpitFit.lod0_atlases = False`). The driver's seat is left out of the
  cabin and of the shell (`shell_skip`): in these main carbins it carries a
  baked low-poly driver (Durango seatL 574 triangles, legs to the pedals,
  against seatR's 54), whose head sat at the eye. No needles (the main
  carbin has none); the dials are painted on the dash texture.
- **Cockpit without a steering wheel**: the main carbin's wheel at its
  finest LOD, drawn flat (its UVs don't fit the `_LOD0` atlases).
- **Needle poses welded differently**: matched by triangle corners.
- **Shell closing the windscreen**: some main carbins (Durango, Express,
  F350, Beetle 04) close the window openings at LOD 5, so the interior's
  outer shell blocked the view (`shell_blocks_view`: a 3 x 3 fan of rays
  from the eye, 0 to 11 degrees up; blocked if two thirds stop within
  2.5 m). `convert_interior` steps to finer shell LODs until it's open
  (LOD 4 for all of them). Single rays gave false alarms on the windscreen
  frit or sun strip (`black_glass`) of 40 cars.
- `forza2dsf.convert` keeps the host interior if `fit` still fails, or (from
  2026-10-24) when the cockpit is a placeholder and the host is the same
  vehicle ("Placeholder cockpits on a same-vehicle host").

Offline renders of the fallback cars (`out/cockpit4/fallbacks.sp`, IDs 304
Durango, 305 Express, 306 F350, 307 Taurus SHO, 308 FM4 CTS-V):
`img/cockpit-fallback-eyes.png`. The cabins are the main carbin's LOD 1:
low detail but closed, with the steering wheel on the host's hinge and the
eye from CameraOverrides or the fleet median.

Survey after the fixes (`out/cockpit-survey/survey.tsv`, 573 cars): no
failures. 555 fit their own cockpit (one needed shell LOD 4), 18 FH1 cars
get the main carbin's cabin: A4 Avant 04, tour bus, Express 2500, PT
Cruiser, Durango 99, Explorer 10, F350 Super Duty, Focus ZX4, Taurus SHO,
Yukon XL, Kenworth T440, Leaf, Camry 07, Corolla DX 95, Prius 11, RAV4
Sport 04, XC70 12, Beetle 04. A main-carbin wheel may use a `blur_*`
technique, which the converter drops as wheel motion blur (Taurus SHO);
`promote_lod` renames it to a dark trim. Warnings, not errors: 165 cars
lack a speed or tach needle (84 race cars, digital clusters such as the
458, MP4-12C, LFA, S65, and the 18 fallbacks; those simply get no
needle), 20 race-car wheels under 12 cm radius, 12 near-vertical columns,
the FM4 MC12 GT1 race car keeps a closed windscreen at every shell LOD,
and the FH1 Fiat 595's main carbin doesn't parse (`carbin.py`: "no
parsable sections found"; that car can't be ported at all, cockpit or
not).

## Holes in converted interiors (2026-10-22, fixed offline)

Driving `out/testdrive` (40 cars), the user saw holes in the Lexus IS F
(ID 12) and BMW M3 E30 (ID 1) interiors. No decimation ran (cell 0 mm on
all 40) and the interior materials draw both faces (`rs_cullmode` 1, which
is D3DCULL_NONE, on every host interior material checked), so the cull was
the cause. Three faults in `visible_triangles` (confirmed-static, measured
with `out/fixes/cull_check.py`):

1. It culled from the Forza eye and 8 cm either side, but `camera_eye`
   raises the head camera to see the wheel rim 7.9 degrees down: 3.4 cm on
   the M3, 2.8 cm on the IS F, up to 8 cm (Raptor; Charger Daytona 7.1)
   on the 40 test-drive cars. DSF's cockpit camera also bobs and turns
   with the car.
2. A triangle with a vertex behind a cube face's plane was skipped for that
   face instead of clipped, so large dash, cluster-hood and floor
   triangles next to the head never registered and were dropped unless a
   neighbour saved them.
3. Triangles smaller than a pixel at 256ВІ covered no pixel centre and were
   dropped even where visible.

Fix: the cull runs from the final camera (`CockpitFit.camera`, set by
`forza2dsf` from `camera_eye`) plus `CULL_OFFSETS` (12 cm either side, 8 cm
up, 6 cm down, 8 cm forward and back) and the Forza eye; triangles crossing
a face's near plane are clipped; triangles that cover no pixel centre count
as seen when their depth is within 3% of what the pixel shows.

Check: `cull_check.py` renders depth from 24 head poses (the camera, 10 cm
either side, 5 cm up and 4 cm forward; ahead, 45 degrees left and right,
down, both doors) of the full Forza cockpit and the culled one and counts
pixels where the culled one shows something more than 2 cm farther (a
hole). M3: 1,497 hole pixels before, 3 after (the clipping alone: 378);
IS F: 1,960 before, 1 after. All 40 test-drive cars had holes under the
old cull (median 1,497 hole pixels of 384,000; worst Charger Daytona 69
5,748, Cuda 3,803, Challenger 2,854, S65 2,510, M5 E28 2,455; the two
main-carbin fallbacks 95 and 25) and at most 23 after (300SL; edge
noise). `out/fixes/cull_check_all.log`. Kept triangles grow by about half (IS F
30,907 to 44,778; interior MDXN 0x123000 to 0x18B800; the batch's
`--mdxn-limit 0x2C0000` covers it; under the stock 0x12D000 limit the
decimation ladder would start). `uv run dsff cockpit holes <archive>
--id N --png out.png` (writes `out-N.png`) renders 4 head positions x 6 directions with glass
drawn and empty pixels magenta (`img/holes-isf-head-views.png`); before and
after: `img/holes-isf-before-after.png` (the cluster hood behind the wheel
was missing in the left-look view), `img/holes-m3-before-after.png`.
Remaining magenta in the M3's door views is a gap the Forza cockpit itself
leaves under the seat.

## Light cockpit trims (2026-10-22, fixed offline)

Superseded 2026-10-23: the cabin trims never sampled `nodamage_LOD0`; see
"Cabin trims sample interior_LOD0". The filler below now only affects the
outside sections (A-pillars, mirrors, bonnet).

Run `20261022-203317` (`out/glassfix/dngvehicles.sp`, ID 307): the FH1 Jeep
Grand Cherokee SRT8's dashboard top, door cards and steering wheel drew
light grey, while the real car's interior is black and the M5, S65 and Supra
in the same run were dark (`img/jeep-light-cockpit-ingame.png`). The offline
head-camera view (`cockpit.render`) of the same archive shows it too.

Cause (confirmed-static): `nodamage_LOD0` is shared by the exterior
`_lod0.carbin` and the cockpit. Its alpha-0 texels are the exterior's
paint-through area, and their RGB is arbitrary fill. Most cockpit carbins
point the bulk of their cabin trims at that fill anyway, not at the opaque
trim strips (the 370Z is the exception), as `out/jeepfix/alpha_survey.py`
shows by sampling every cabin vertex's texel:

| Car | Cabin vertex texels with alpha < 16 | RGB there |
| --- | ---: | --- |
| NIS_370Z_10 | 3% | white |
| BMW_M5_12 | 71% (dash `plastic` 99%, wheel `bump_leather` 100%) | white (254-255) |
| CHR_JeepGCSRT8_12 | 68% (wheel `bump_*` 85-100%) | grey 163-197: the background of a "GRAND CHEROKEE" badge plate |
| PEU_205T16_84 | 76% | grey 123 with red livery lines |

The converter turned only white alpha-0 texels into the dark plain colour
(`_convert_texel`: `a < 16 and min(r, g, b) > 200`, a rule meant for the
exterior's white paint-through). That made the M5 right by luck. The Jeep's
and the 205's grey fill passed through, and the trim lift (40) brightened
it further: the Jeep's dash `plastic` came out at 89 on average, `bump_leather`
at 151 and the wheel's swatches at 150. Forza draws these cabins dark whatever
the fill (the M5's fill is white and its interior black), so the fill's RGB
is not what the cockpit shaders show. They bind `CockpitInteriorHiSampler`,
`CockpitAOSampler` and `PaintColor` (strings in
`out/fh1/shaders/cockpit_bump_leather.fxobj`), but the exact colour path is
undecoded.

Fix (`CockpitOptions.filler`, `dsf_materials.Texturing.filler`,
`forza_materials.fill_transparent`): before the cockpit atlas is planned,
every `nodamage_LOD0` texel with alpha below 16 gets the RGB (10, 10, 12),
near black like Forza's own trim strips (the Jeep's opaque plastic is
(10, 12, 10)). After the lift and tone that is about (40, 40, 41), the colour
white fill already became. Alpha is kept, so alpha-tested techniques still
drop those texels. A swatch whose footprint is mostly fill (mean alpha
below 128) takes the fill colour outright. The Jeep's wheel leather is a
4-texel footprint about a third on badge letters, which would otherwise
average to 72. Region edges where fill meets opaque strips no longer blend
into grey seams either.

Offline (`out/jeepfix/atlas_check.py`, triangle-weighted output colour):
Jeep dash `plastic` 89 to 41, `bump_leather` 151 to 41, seats `leather` 111
to 41, wheel `bump_plastic3`/`bump_leather`/`plastic` 150/141/61 to 41. Only
opaque artwork (badges, the brake disc, the dial faces from interior_LOD0)
keeps its colour (`img/jeep-cockpit-atlas-before-after.png`).

Affected cars in `tools/batches/testdrive.txt` (cabin triangles on non-white
fill): Peugeot 205 T16 76%, Jeep GC SRT8 44%, Ford SVT Raptor 1.4% (leather
and stitching patches at 170-190), LFA 0.8% (windscreen `black_glass` frit
at 176), Agera 1.3% (`black` at 62, barely lighter). The other 35 put their
cabins on white fill or opaque strips; for them the change is invisible
(plain (40, 40, 42) becomes (40, 40, 41)).

Test build `out/jeepfix/dngvehicles.sp` (`port_batch.py build --id-policy
high`, lint clean): 304 M5 (control), 305 Jeep, 306 205 T16, 307 Raptor,
308 LFA. Head-camera views before and after
(`img/jeep-cockpit-before-after.png`, rows Jeep, 205, Raptor, LFA, M5;
before = `out/glassfix` for the Jeep and M5 and `out/testdrive2` for the
others): the Jeep's wheel, dash and door cards and the 205's whole cabin
go from light grey to dark, the Raptor and LFA change only in small trims,
and the M5 is unchanged. Not yet run in game.

## Cabin trims sample interior_LOD0 (2026-10-23, fixed offline)

Runs `host-20261023-080616` and `-081503` (`out/testdrive4`): the FH1
Challenger SRT8 392 (ID 8) drew its dashboard top, steering wheel and door
cards white to light grey, and the FH1 Range Rover's (ID 21) steering-wheel
rim and hub were pure white. Run `host-20261023-082633`: the FH1 Mustang Boss
429 (ID 37) had a saturated red dash and red mirror frame. Forza draws all
three cabins dark.

Cause (confirmed-static): the converter read the cockpit's cabin trims from
the wrong texture. The table under "Forza side" put `leather`, `plastic`,
`metal`, `bump_*` and the rest on `nodamage_LOD0`, from luminance statistics
(`out/cockpit/atlas_probe.py`) rather than artwork. Drawing each technique's
UV triangles over both candidates (`out/whitetrim/uv_overlay.py`) settles it:
in `interior_LOD0` the islands land on matching artwork, in `nodamage_LOD0`
on unrelated content.

| Car | interior_LOD0 (match) | nodamage_LOD0 (what the converter took) |
| --- | --- | --- |
| BMW_M5_12 | dial bezel rings, the pedal plate, the blue stitch strip, the swatch palette | white alpha-0 filler |
| NIS_370Z_10 | the seat leather band at v 0.94-0.99 (the islands have its exact extent) | a Nissan emblem on black |
| LR_RangeRover_12 | the analogue clock face, harman/kardon badge, AIRBAG label, speaker grille | badge artwork and white filler (the wheel's swatch sits on white (235)) |
| DOD_ChallengerSRT8392_12 | a dark swatch palette (13, 13, 13) | a brushed brake-disc picture (160, 160, 168), which the lift raised to about 150 |
| FOR_MustangBOSS429_70 | dark plastic and leather (34-52) | the red badge swatch (249, 0, 0) |
| DOD_ChargerDaytona_69 (FM4) | wood-grain panel, steering-wheel hub and horn logo, clock | white filler |

The shader library agrees. Every technique in the cabin sections
(`interior`, `seatL/R`, `steering_wheel`, the needle sections) has a
`cockpit_<name>` variant in `shaders_v16.fxobj` / `CustomCarShaders.xml`
(`cockpit_plastic`, `cockpit_bump_leather`, `cockpit_headliner`,
`cockpit_reflector`, ...; 79 names), and those bind
`CockpitInteriorHiSampler`. On all 38 non-stub test-drive cars, no cabin-section technique outside
`cagerace` lacks a variant (`out/whitetrim/cab_plain.py`). The outside
sections seen through the windows (`a_pillar`, `hood*`, `mirrorL/R`) do not
follow this: their `chrome`, `black`, `plastic2` UVs hit the exterior
swatches of `nodamage_LOD0` (M5 A-pillar chrome on the white chrome swatch,
where `interior_LOD0` has black; Mustang mirror chrome 220 vs 46), so they
keep the exterior techniques and `nodamage_LOD0`. The M5, Jeep and 205 only
looked right after "Light cockpit trims" because the filler turned
everything into the same dark grey: the old output was luminance 40 to 46 for
nearly every car's cabin, whatever its real colours.

Fix:

- `forza_materials.classify_cockpit(name, section)`: in cabin sections
  (anything not matching `COCKPIT_EXTERIOR_SECTIONS`, `^(a_pillar|hood|mirror|glass|headlight|taillight)`)
  every trim or textured technique maps to `interior_LOD0`; outside
  sections keep `nodamage_LOD0`; `resolve` passes the section name to
  classifiers marked `section_aware`. Stub cockpits (main-carbin cabin,
  main atlases) keep the old technique-only mapping.
- `CockpitOptions.atlas_weights` interior 2 to 4, and
  `dsf_materials.Texturing` caps each family at 1:1. The cabin is now
  almost all `interior_LOD0`, so it gets most of the atlas: 0.48x to 0.98x
  on the six rebuilt cars, against 0.32x to 0.57x before.
- `trim_lift`, `trim_tone` and `filler` stay on `nodamage_LOD0`, so they now
  only touch the outside sections. `interior_LOD0` texels pass unchanged.
  Dark cabins come out at luminance 25 to 60 (the Desanne's interior
  texels: median 44).

Survey (`out/whitetrim/atlas_check.py`, `atlas_check.txt`; mean output
luminance of the cabin's atlas texels, triangle-weighted, old to new; the
atlas images are in `out/whitetrim/atlas/`): every car with its own
cockpit changes (38 of 40; the Durango and Express stubs don't). Groups:

- Light or coloured cabins that should be dark, now fixed: Challenger SRT8 392
  (dash 143 to 34, wheel 146 to 45, seats 140 to 21), Range Rover wheel (125
  to 40), Mustang Boss 429 (red dash, wheel 100 to 54; the remaining 90 mean
  is real chrome and brushed-metal trim), 300C (63-70 to 45-57).
- Flat ~41 grey that now carries real artwork and colour: the rest. Most
  stay dark (M3, Supra, S2000, WRX, Cuda, Firebird, 500 Abarth, Civic, 205,
  Jeep, Veyron, GT-R, M5, M5 E28 dash, Skyline, DMC-12, Charger wheel and seats).
  Some come out lighter because Forza's cabin is light or has much
  bright metal: Range Rover seats and headliner (cream, 149), 300SL dash (151,
  its painted and chrome dash), Agera (177, a brushed-aluminium console band),
  Huayra (140-150), S5S Raptor (99-107), 2000GT (115), IS F wheel and seats
  (~97), 250 GTO wheel (187, aluminium spokes), Charger interior (94, chrome
  window and door trim).

Offline evidence: `out/whitetrim/eye-before-after.png` (head camera,
`cockpit.render`; left `out/testdrive4`, right `out/whitetrim/build`; rows
Challenger, Range Rover, Mustang 429, Agera, 300SL, Charger Daytona),
`out/whitetrim/m5-uv-interior.png`, `rr-uv-interior.png`,
`charger-uv-interior.png`, `agera-uv-interior.png` (UV islands over
nodamage_LOD0 left and interior_LOD0 right).

Test build `out/whitetrim/build/dngvehicles.sp` (`port_batch.py build --car
... --gui`, lint clean): 1 Challenger SRT8 392, 2 Range Rover, 3 Mustang Boss
429, 4 Agera, 5 300SL, 6 Charger Daytona (FM4).

In game (run `host-20261023-084652`, IDs 147, 1-6; partly confirmed-runtime):
all six load and switch to the cockpit view. A Windows Firewall prompt for
Driver.exe covered the middle of every screenshot, steering wheels
included. Around it: the Challenger's dash top, vents and door trims are
dark (observe-04/05; they were white-grey), the Range Rover's dash is dark
(observe-08/09), the Mustang's dash and mirror frame are dark instead of red
(observe-13/14), the Agera shows its silver vent rings (observe-18/19), and
the Charger has chrome gauge bezels and window frames (observe-27/28). The
HUD badges are stock ones (RAM 3500, Range Rover Sport...) because the run
loads no gui.sp. Steering-wheel rims still need a run without the prompt.

**Charger Daytona rear deck (not a converter bug).** In run
`host-20261023-080616` (observe-15 to -17) the deck between the rear window
and the boot lid showed as a brown trapezoid. Those triangles are `body`
over alpha-0 texels in `nodamage` (paint shows), while the boot lid and tail
panel are `body` over opaque black texels: the Daytona's black tail. Forza
composes it the same way (`out/whitetrim/charger-forza-top-rear.png`,
nodamage blended over a red test paint): body colour on the deck, black
from the boot lid back. The brown is the car's dark bronze factory paint on
the most sky-facing panel; roof and sides read darker at that angle.

## Driver's hands and the steering wheel (2026-10-24, confirmed-runtime)

User reports: the S5S Raptor's hands float above the rim ("some cars' wheels
do not line up with the hands, some are spot on"); the WRX STI's wheel looks
upside down.

**What places the hands.** The cockpit hands are one rig for every car. The
game builds its pose in a per-player driver object of 0xD8 bytes
(`FUN_005743E0`, pool at `FUN_00573110`), from the steering marker read by
the UPVH parser (`FUN_0088E310`, role 0x2F: position to spec+0x37E0, a
direction from its quaternion to spec+0x37F0):

- anchor A = marker position + (0, VO3D p538, VO3D p559) (spec+0x34D8 and
  spec+0x352C are p538/p559, `spec + 0x2C70 + 4 * id`);
- the steering angle comes from the driver animation state (`FUN_005756E0`;
  some states use fixed angles of В±1.7456 rad, the hand-over-hand pose at
  full lock). `FUN_00514150` turns by that angle about the marker's
  direction through A (the same helper as the gauge needles), and
  `FUN_00573E10` copies the matrix to the driver.

The in-game experiment (`out/steering/make_exp.py` patches one value per car in a
copy of testdrive7; run `host-20261024-000936`, crops and sheets in
`out/steering/exp-*.png`) shows what each value moves:

| Change (car) | Hands | Wheel |
| --- | --- | --- |
| marker y -10 cm (Silvia, 5) | down 10 cm | down 10 cm, hands still on the rim |
| hinge y -10 cm (370Z, 32) | stay | up 10 cm, hands below the rim |
| marker quaternion +20В° about x (M3, 1) | no visible change at rest | no visible change |
| hinge axis +20В° (stock 136) | no visible change at rest | no visible change |
| p538 -0.04 to +0.10 (300SL, 24) | up | up, hands still on the rim |
| p559 +0.075 to -0.15 (Cuda, 7) | back and down | back and down |

So the hands sit at A + H and the skinned wheel at A + (v - hinge), with H
fixed. Only the hinge, the point of the wheel mesh that is drawn at A, moves
the hands against the wheel. When the car steers, both turn about A.
Stock cars make this work by construction. In all 146 with a
steering marker, the hinge and the marker are the centre of the same wheel mesh:
fit_wheel radius 0.176 m (median; quartiles 0.174 to 0.180; trucks 0.19).
The column is 24В° up in the seat marker's frame, and the hinge sits at (0,
0.469, 0.428) from the driver seat marker in its frame (`out/steering/seat_frame.py`,
`stock_rim.py`). The hands grip about 45В° either side of the top (stock 147
in game against an offline render). Stock p538/p559 (-0.1 to 0.1 m; buses
p559 to -0.3) move the wheel with the hands, relative to the driver's body.

**Why converted cars missed.** The converter put the hinge and marker on the
Forza wheel centre, so the rig gripped a 0.176 m rim at a 24В° column. Forza
rims run 0.146 (S2000) to 0.203 m (Charger Daytona), and columns 10В° (S5S
Raptor) to 30В°. The grips missed the rim by up to 4.3 cm (Raptor; S2000 3.5,
RX-7 2.9, 370Z 2.8, Agera 3.0, Firebird 2.8). The host's p538/p559 also
moved the converted wheel away from its Forza place (Cuda 8.5 cm, FOR
SVT Raptor 13 cm, M3 E30 4 cm).

Forza's own data has no hand target beyond the wheel centre:
`CarAttribs.xml` `DriverHandsOffset` is the wheel centre + (0, -0.41, +0.42)
on every car checked (WRX, 370Z, M5, TT RS, GT-R), with `DriverHandsScale`
1 and `Use10_2Animations` 0. The arm animation is Forza's own rig, so it is
not used.

**Fix** (`cockpit.hand_shift`, `patch_hierarchy`, `hand_params`): the hinge
and marker go to the Forza wheel centre + shift, where shift is the least-squares
translation that puts the stock grips on the Forza rim (y and z only;
capped at 6 cm). p538 and p559 are written as 0, so the wheel is drawn
exactly where Forza puts it. The marker quaternion still follows the Forza
column. The wheel then turns about centre + shift. The part of the shift along the
column costs nothing. The part in the wheel plane (up to 4.4 cm, S2000; 3.7
Raptor; most cars under 2.5 cm) makes the hub circle by 2В·shiftВ·sin(angle/2)
while steering. With a fixed rig the alternative would be to rescale the
Forza wheel. The converter report has `steering.hands` (shift, in-plane and
along-column parts, grip error before/after).

(Superseded 2026-10-26: the shift is now along the column only and the wheel
is scaled to the grips instead, so it turns about its own hub; see "The
steering wheel turns about its hub".)

Verified: offline, `out/steering/grip_survey.py` on `out/steering/build`
(WRX, S5S Raptor, Silvia, S2000, 370Z, Cuda, M3 E30) puts the grips within
0.3 cm of the rim (before: 1.1 to 4.3 cm). In game: run `host-20261024-003455`
at rest, every car's hands on the rim. Before/after (Raptor, Silvia, WRX):
`img/steering-hands-before-after.png`. With Left held (run
`host-20261024-004448`) every car turns the same way, hand over hand, the
right arm crossing to the left.

40-car survey of the installed test drive (`out/steering/before/delta40.txt`):
largest grip miss before, then the hinge shift in the wheel plane (all fall
under 0.5 cm after):

| Cars | Miss before | In-plane shift |
| --- | --- | --- |
| S5S Raptor, S2000 | 3.9, 3.7 cm | 3.7, 4.4 cm |
| RX-7, Charger Daytona, Agera, 2000GT, 370Z, Firebird, Veyron, Cuda | 2.5 to 3.1 cm | 1.0 to 3.9 cm |
| Express, DMC-12, Silvia, Huayra, Supra | 2.0 to 2.4 cm | 1.3 to 3.0 cm |
| 21 cars from the TT RS to the Mito | 1.1 to 1.9 cm | 0.4 to 2.3 cm |
| Civic R, M5, Jeep, Range Rover, LFA, 300SL, 300C | 0.4 to 0.9 cm | 0.0 to 1.3 cm |

**WRX "upside down".** Not reproduced. Offline the cockpit and main-carbin
wheels are upright (STI badge readable; `out/steering/before/6-wheel.png`,
`full-entry-wheels.png`). In game it sits like every other car at rest
(`host-20261024-000936` shots 31/32 at ID 6, `host-20261024-003455` at ID 1),
and its hierarchy is the same as the others' (axis, quaternion, skin
binding). At full lock with the car standing (DSF's hand-over-hand pose) the wheel
turns well past 90В° on every car, which may be what was seen. Open: a
user screenshot of the moment.

## Digital clusters (2026-10-24)

User report on `out/testdrive9`: the S2000 (ID 4) shows no instrument
cluster in the cockpit view, only a dark hood behind the wheel; the
Express's (ID 23) dashboard is low-detail.

**Survey** (`out/cc/survey_eye.py`, head-camera views of all 40 cars of
`out/testdrive10`, `out/cc/before-1-20.png`, `before-21-40.png`): six cars
show no readable cluster: S2000 (4), Durango (19), Range Rover (21),
Express (23), Agera (29), LFA (30, an empty ring). The other 34 draw their
dials.

**Cause for the S2000 and LFA (confirmed-static).** Their whole cluster face
is `digital_gauge` geometry (S2000 76 triangles, the LFA's dial 110), which
the converter drew as a blank display. Picking from the Forza eye
(`out/cc/pick.py`) and drawing each technique in turn
(`out/cc/forzaeye.py --hl`) shows this; the gauge_emissive_alpha
subsections are switch labels on the console. The UV0 of these subsections
is a planar 0..1 map across the face. Through the subsection's first UV
transform it lands on the label sheet (AUDIO, MODE, SRS AIRBAG on the
S2000: what the old note called "unrelated parts"); through the second
transform (the uv1 slot of `uv_transform`) it lands on the display's own
artwork: the S2000's bar tachometer arc with 0 to 9, x1000 r/min, the
temperature and fuel scales; the LFA's central dial with the gear N; the
370Z's two LCD panels (`out/cc/dg-sheet1.png`, `dg-sheet2.png`,
`dg-sheet3.png`: highlight, first transform, second transform per car).

Over the 22 test-drive cars with `digital_gauge` (`out/cc/dganiso.py`), the
second transform on UV0 is one of three things:

| Kind | Cars | Median anisotropy | UV units per metre |
| --- | --- | ---: | ---: |
| a picture of the display | S2000, Challenger, 500 Abarth, Civic R, LFA, 370Z | 1.00 to 1.40 | 2.9 to 4.6 |
| a swatch (one texel) | Silvia, WRX, IS F, 300C, Golf R, Jeep, Veyron, Agera, Huayra, GT-R, TT RS, M5, Focus | any | 0 to 0.13 |
| a stretched strip | S65 (36.7), Range Rover (320), SVT Raptor (29.9) | 17 to 320 | |

The first transform is always a dense, undistorted map (anisotropy 1.2 to
2.2), so it can't be told apart from a face by shape; it is the digit/label
sheet the shader composes from.

**Fix** (`cockpit.display_face`, `with_display_faces`, called in `fit` for
cockpit-carbin cabins): a `digital_gauge` subsection whose second transform
maps UV0 with median anisotropy at most 2 (`FACE_MAX_ANISOTROPY`) and at
least 1 UV unit per metre (`FACE_MIN_DENSITY`, about 512 texels per metre on
the 512ВІ interior_LOD0; cabins run 3 to 9) becomes `digital_gauge_face`
with that transform first: textured from interior_LOD0 like any cabin
trim. The rest stay the blank display. `CockpitFit.display_faces` and the
interior report's `display_face_triangles` count them.

Survey after (`out/cc/faces-survey.txt`): faces on S2000 76, Challenger 29,
500 Abarth 30, Civic R 8, LFA 110, 370Z 56; no other car changes. Offline
head-camera views, before and after (`out/cc/faces-before-after.png`, build
`out/cockpitfaces/build`): the S2000 shows its tachometer arc and scales,
the LFA its central dial, the 370Z its LCDs; the Challenger, 500 and Civic
displays change little (dark or the white dial face around them).

In game (VM, confirmed-runtime): run `vm-20261024-061707`
(`out/cockpitfaces/build`, IDs 147, 1 S2000, 2 LFA, 3 370Z, `-ShotSeconds
3`) against `vm-20261024-063218` (`out/testdrive10`, IDs 147, 4, 30, 32,
same settings): the S2000's tachometer arc, scale and the temperature and
fuel marks now show behind the wheel (observe-05), the LFA's central dial
with N and its scale (observe-10), the 370Z's LCD panels under the HUD
(observe-15); `img/cockpit-digital-faces-ingame.jpg`. Run
`vm-20261024-060338` (IDs 147, 1-5) finished too. Offline:
`img/cockpit-digital-faces-offline.png` (S2000, LFA, 370Z; left before).

**Still without a readable cluster, and why.**

- Agera (182 triangles) and Range Rover (12): the cluster is drawn at run
  time by Forza's digital shader. Their second transform is a one-texel
  swatch (Agera) or a stretched strip (Range Rover), and no other
  subsection, LOD or texture holds the artwork: the Agera's
  `interior_emissive_LOD0` has only switch icons; the Range Rover's
  cluster is one flat 12-triangle panel (its virtual dials), it has no
  emissive texture, and neither transform maps the panel onto the dial
  pictures in its interior_LOD0 (the shader composes them at run time).
  Neither car is on the FM4 discs. They keep a blank display.
- Durango and Express: FH1 traffic cars. Their `_cockpit.carbin` and
  `_lod0.carbin` are placeholders (17-vertex boxes; the `_LOD0` atlases
  hold a Prius's), neither is on the FM4 discs, and no GM or Dodge sibling
  ships a real cockpit (the FH1 Yukon XL is a stub too). The converter
  already uses the finest Forza data, the main carbin's LOD 1 cabin
  (Express interior 314 triangles, dash texture from nodamage.xds at 1.0x).
  The only detailed alternative is the host's stock interior
  (`out/cc/stubs-vs-host.png`): the Express's host 198 is the GMC Savana,
  whose dash is the Express's; the Durango's host 138 is an Audi Q7. No
  geometric test separated the two (rays over the dash from the eye agree
  on 43% vs 30%; the hosts' exterior greenhouses differ from both Forza
  bodies), so the converter kept the Forza cabin for both. Superseded for
  the Express: "Placeholder cockpits on a same-vehicle host" below tells
  the two apart by identity, not geometry.

## Placeholder cockpits on a same-vehicle host (2026-10-24)

When Forza's cockpit carbin is a placeholder (`cockpit.is_stub`: the 18
FH1 traffic cars of "Cars without a usable cockpit carbin") and the host is
the same vehicle, the host's stock interior entry stays: cockpit, gauges,
steering wheel and hands, head camera, interior glass. It is that
vehicle's real cabin, where Forza only has the main carbin's LOD 1 stand-in.

**Same vehicle** (`host_select.same_vehicle`; `port_car.pick_hosts` puts it
in the host choice, `ConvertOptions.same_vehicle_host`): same body class,
model years within 4 and wheelbase within 2.5% and length within 4%, and
either

- the same model by `identity_score` (make, model name, year within 4), or
- a badge twin: another make of the same corporate group
  (`MAKE_GROUPS`: GM, Chrysler, Ford, VW, Toyota, Nissan, Honda) with
  wheelbase and length both within 1%. Platform mates share a wheelbase but
  not the cabin, and their lengths differ more: 300C on the Charger 1.6%,
  Cuda on the Challenger (wheelbase 2.1%), Golf R on the Scirocco (same
  badge).

A shared model family alone (A4 Avant 2004 on the A4 2010, Beetle 2004 on
the Beetle Convertible 2009) or a platform (Yukon XL 2001 on the Escalade
2007) is not the same vehicle.

Roster check (`out/cc/samevehicle_roster.py` over `research/host-picks.tsv`,
`out/cc/samevehicle.txt`): 27 of 573 picks are the same vehicle, 26 by
model (370Z, TT RS, Mito, Ford GT, ...) and one badge twin (Express on the
GMC Savana). The rule only acts where the Forza cockpit is a placeholder:
the Express (198 GMC Savana), the F-350 Super Duty (252, its stock self)
and the Taurus SHO (196, its stock self). The other 15 stub cars keep the
Forza cabin (Durango on the Audi Q7, Yukon XL on the Escalade, A4 Avant on
the A4 2010, ...). Of the 40 test-drive cars only the Express changes.

What stays consistent: the host's hierarchy is not patched for the cockpit
(steering hinge and marker, gauge table, seats, head camera VO3D 430-432
stay stock, as with `--no-cockpit`); the Forza steering wheel in the full
and exterior entries is not skinned (it isn't drawn from the cockpit);
damage still moves the panel records onto the Forza panels, so the stock
interior's door cards and glass follow the Forza doors and windows. The
report has `cockpit_host` instead of `cockpit`.

Offline (`out/stubhost/build`; before: `out/testdrive10` for the Express,
`out/stubhost/before` built with the rule off for the F-350 and Taurus):
`img/cockpit-stub-host-offline.png` (rows Express, F-350, Taurus; left
before).

In game (VM, confirmed-runtime): run `vm-20261024-070952`
(`out/stubhost/build`, IDs 147, 1 Express, 2 Durango, 3 F-350, 4 Taurus)
against `vm-20261024-075905` (`out/testdrive10`, IDs 147, 23): the
Express's cockpit is the Savana's, with its speedometer and four gauges,
the hands on its wheel and the head camera at the host's eye
(`img/cockpit-stub-host-express-ingame.jpg`). Glass ladder
`vm-20261024-081025` (IDs 147, 1, 3, 4, `-GlassLadder`): no crash; the
Express's windscreen goes from clean to shattered with shards along the
A-pillar (observe-11), the Taurus shows cracks after 0.5 (observe-28), the
F-350's clean cockpit draws (observe-17) and, like other cars before, the
game drops to the Shift view after the 1.0 step
(`img/cockpit-stub-host-glass-ladder.jpg`). Three runs in this period got
stuck in the Shift view or crashed while the stock warm-up car 147 loaded,
before any converted car (the same happened on unchanged `testdrive10`
and in other areas' runs); their reruns are the ones above.

## Badges seen through the windscreen (2026-10-24)

Found during the emissive work (PR #10): `classify_cockpit` sent every
`badge` technique to interior_LOD0, also in the outside sections
(`a_pillar`, `hood*`, `mirror*`). Over the 40 test-drive cars
(`out/cc/badges.py`; `img/cockpit-outside-badges-uv.png`: each subsection's
UV outline over nodamage_LOD0 RGB and alpha, then interior_LOD0 RGB and
alpha) these are: Firebird `hooda` and `a_pillar` `badge` (33 triangles
each: the "6.6" decal), M5 E28 `mirrorL/R` `badge` (2: the mirror badge
shape), S5S Raptor `a_pillar` `badge` (16), Mustang Boss 429 `hooda`
`badge` (286: the scoop plate), SVT Raptor `hooda` `stripe_badge` (490) and
`badge` (24: "F-150"), and the Agera's `hooda` `liveryBadge` (skipped since
PR #10). In nodamage_LOD0 each outline sits on matching artwork and its
alpha mask; in interior_LOD0 on unrelated content (gauge faces, labels), the
same split as the other outside techniques. Fix: badges map to
interior_LOD0 only in cabin sections; in outside sections they follow
`classify` (nodamage_LOD0), alpha-tested as before. Rebuilt
(`out/cockpitbadges/build`): the five cars' interior atlases gain one
nodamage region each (M5 E28, Mustang, SVT Raptor; the Firebird's and
Saleen's footprints fall inside existing regions). From the head camera the
change is small: most of these triangles are below the cowl or culled.

## Cameras (2026-10-24, confirmed-runtime)

User report on `out/testdrive9`: the hood camera of some cars sits behind or
inside the glass (Agera: the view is a dark band of dashboard with the road
above it).

**Camera modes.** `LuaScripts/visuals/camera/cameras/cameraselection.lua`
cycles `Normal` (chase), `DriverEye` (cockpit), `Bonnet` and `Bumper`
(`localPlayer.cameraMode`; Bumper hides the player's car). `Driver.exe` maps
the names to modes 1, 4, 9 and 3 (`FUN_0065F640`). `FUN_0088DF80` returns a
mount per camera index: 0x10 and 0x12 VO3D p327..329, 0x11 p300..302, 0x13
p394..396, 0x14 the head camera, 0x18 p535..537, 0x19 p549..551; other indices
read a table the UPVH parser fills from records of type 16 (no stock car has
one).

**Measured in game.** `clonetest.lua` (`run-series.ps1 -Views`, below) logs
the game camera in the car's frame at the end of each view. That frame's
origin is the ground under the model origin: the head camera logs at model y
+ 0.73 on 147, 266 and the converted Agera. With ground = highest hub y -
p360 of the host as shipped (within 1 cm on 147, 266, 205, 278 and 163; the
converted cars keep their host's), runs `vm-20261024-053821` and `-071236`:

| Mode | Position over the ground, +z forward | Set by |
| --- | --- | --- |
| Bonnet | (p535, p536 + 0.30, p537) | 140 stock cars set p536/p537 |
| Bumper | (p327, p328 + 0.30, z from the host + p329): 0.78 m up on every car | no stock car sets them |
| Normal | p300..302 (147: (0, 1.89, -5.89)) | |
| DriverEye | the head camera (above) | |

Moving p327..329 of one car by (0.3, 0.5, 0.6) moved its Bumper camera by
exactly that (run `-053821`); every Bonnet position logged matched p535..537.

**Where stock cars put the Bonnet camera.** The role-63 marker sits on the
windscreen, on the centre line 57% of the way up (median over the fleet;
`out/cameras/m63check.py`), and the Bonnet camera sits just in front of it:
camera - marker = (0, 0.058, 0.085) (medians; quartiles y 0.014 to 0.094, z
0.028 to 0.154; `out/cameras/bonnet_fleet.py`). It clears the car on every
stock car: nothing above it within 2 m or ahead within 1.5 m, 0.087 to 0.258 m
(10th to 90th percentile, median 0.12) over what is below it, mostly the
windscreen (`out/cameras/stock_over.py`).

**Why converted cars missed.** The converter left p535..537 at the host's, so
the camera kept the host's height over the ground while the Forza body around
it changed. On a car lower than its host the camera sat inside the cabin
behind the windscreen (Agera and S5S Raptor on host 163: 6 cm under the roof,
the windscreen 8 cm ahead).

**Fix** (`src/dsfforza/convert/vehicle_cameras.py`, called from `forza2dsf.convert`): the
windscreen centre on the Forza `glassF` (centre line, 57% up, found by rays
from ahead) + (0, 0.058, 0.085), raised until it is 0.09 m over the body below
it, written to p535..537 against the host's ground. The report has
`bonnet_camera` (windscreen, camera, ray clearances). Bumper and Normal stay
the host's: Bumper is a fixed 0.78 m over the road with the car hidden, and
the chase camera follows the car from 5 to 6.5 m behind (not changed here).

40-car survey of the Bonnet camera against the converted exterior
(`out/cameras/survey40.py` on `out/testdrive10`; inside the cabin = glass or
body above it within 2 m, blocked = within 1.5 m ahead):

| | Before (host's p535..537) | After |
| --- | --- | --- |
| Inside or blocked | 5: M3 E30, S65 AMG, S5S Raptor, Agera, LFA | none |
| Height over the car below | 0.00 to 0.63 m (RX-7 0.04, Focus RS500 0.04) | 0.09 to 0.14 m |

How much bonnet the view shows (`out/cameras/bonnet_line.py`: the smallest
angle below the camera's horizon at which a ray straight ahead, on the centre
line or 0.3 m to either side, meets the car within 6 m; DSF's Bonnet camera
looks 2 degrees up for every car, so it can't be set per car): stock fleet
median 16.3 degrees, 10th to 90th percentile 11.8 to 24.3 (149 cars). The
40 converted cars after the fix: median 17.5, 12.5 to 24.1; three are past
the stock 90th percentile, all with short or steeply falling noses: 500
Abarth 28.3, Express 32.0 (vans in the stock fleet go that far too) and
Agera 24.5. Moving the camera along the bonnet doesn't help there: on the
Agera no position 6 to 12 cm over the body within 0.4 m of the chosen one
brings it under 18.5 degrees (`bonnet_grid.py`). So the Agera looks out over
a bonnet that drops away below the frame, as stock cars with the steepest
noses do.

In game (runs `vm-20261024-071236` and `-072757`, `out/cameras/build3` and
`build2`): Agera, S5S Raptor, M3 E30, S65, LFA, Skyline R32, Supra, 370Z and
TT RS look out over their bonnets like stock 147 (the Agera's bonnet drops
away out of the frame), and the logged cameras match the converter's to the
millimetre (S5S Raptor: model (0, 0.252, 0.953), logged (0, 0.983, 0.953) with
ground 0.731). Before and after: `img/bonnet-camera-before-after.jpg`.

## The driver in the chase views (2026-10-24, confirmed-runtime)

User report: in the chase view the driver's hands are off the steering wheel
(S5S Raptor, side view). The cockpit view had them on the rim since "Driver's
hands" above.

**What moves what** (stock cars, runs `vm-20261024-063440` against the
unchanged `-064829`; fixed side cameras): the driver seat marker (role 10)
10 cm down moved Tanner's body down with his hands still on the wheel (not
borne out by the later runs: the hands move with the seat, see "The driver's
seat follows the wheel"); the
steering marker 10 cm down moved hands and wheel down together; the steering
hinge 10 cm down moved the wheel up and left the hands. So the chase-view
hands use the same rig as the cockpit's (marker and hinge), and only the
body follows the seat.

**Why converted cars missed.** The cockpit hands grip the cockpit carbin's
wheel, but the full and exterior entries draw the main carbin's
`steering_wheel`, and Forza models the two apart: the S5S Raptor's main-carbin
wheel sits 15 cm ahead of its cockpit wheel, the 1993 Skyline's 17 cm, the
Supra's 10, the 370Z's 9, the TT RS's 8 (`out/cameras/wheel_pair.py`).

**Fix** (`cockpit.align_main_wheel`): the main-carbin wheel is moved, turned
and scaled onto the cockpit wheel fit before the body is built (a
`main_steering_wheel` entry in the report). Where a column modelled with the
wheel spoils the fit (FH1 Mustang Boss 429), only the points around the
cockpit wheel are fitted. Stub cockpits use the main wheel already, so they
don't move.

40-car survey (`out/cameras/survey_hands.py` on `out/testdrive10`; the
stock rig's grips at the archive's hinge against the rim the chase view draws):
grips more than 3 cm off the rim on 19 cars before (S5S Raptor 15.9 cm,
Skyline 14.8, Supra 8.8, 370Z 8.1, TT RS 7.1, Silvia 6.3, IS F 6.1, Golf R 5.9,
M3 E30 5.8, 500 Abarth 5.5, SVT Raptor 5.1, 300SL 4.7, Range Rover 4.3,
Mustang Boss 429 4.2, Focus RS500 4.0, S2000 3.8, 300C 3.4, RX-7 3.3, 2000GT 3.0),
none after (at most 0.4 cm, the cockpit's own fit).

In game (run `vm-20261024-071236`): the S5S Raptor's and Skyline's hands now
hold the drawn rim like stock 147's
(`img/chase-hands-s5s-before-after.jpg`).

**Harness.** `run-series.ps1 -Views 'Bonnet,Bumper,left,right,front'` adds
views after each chase view, about 6 s each: camera modes, and fixed cameras
looking at the driver's seat from 2.4 m to either side or 3.4 m ahead, 1.9 m
up (`clonetest.lua` `FIXED_VIEWS`; made like the game's debug free camera).
Camera-mode views log the camera in the car's frame.

## The driver's seat follows the wheel (2026-10-24, confirmed-runtime)

User report on `out/testdrive11` (with the fix above): the Mustang Boss 429
(ID 37, host 193) still has its hands off the wheel in the chase view; the
cockpit view is right.

**Not the wheel.** Offline, the main-carbin wheel the chase view draws is
on the cockpit wheel (`align_main_wheel` moved it 4.7 cm), and the stock
grips at the hinge sit within 0.4 cm of its rim at every LOD. In game (run
`vm-20261024-125959`, fixed left view) the hands are about 10 cm ahead of
the drawn rim.

**What places the chase-view hands.** One seat marker moved per stock car,
fixed side cameras (`vm-20261024-131412` against the unchanged stock archive
`vm-20261024-132025`; earlier `vm-20261024-130737`): 147's seat 13 cm forward
puts the hands through the dashboard ahead of the wheel; 136's seat 9 cm
down puts them 9 cm lower on the wheel; 124's seat 10 cm back leaves them in
the air 10 cm behind the rim. The driver model is one animated rig on the
seat marker, hands included; they don't reach for the wheel. (The earlier
note above, that the seat moved the body with the hands still on the wheel,
was a misreading.)

**The stock relation** (`uv run dsff driver_survey stock`, every stock car
with a driver seat and a steering marker): in 138 of 147 the steering marker
sits at (0, 0.469, 0.428) in the seat marker's frame, within 1 cm, and in all
but 202 (0, 0.473, 0.530) and the bus 243 within 2 cm. The frame is turned by
the seat's tilt (0 or -5 degrees), so in model space host 193's marker is at
(0, 0.505, 0.385) from its seat.

**Why converted cars missed.** `patch_seats` put the seat under the Forza
eye with the host's eye-to-seat offset. The host's head camera isn't tied
to its seat (eye in the seat frame: y 0.64 to 0.84, z -0.29 to 0.13 over
the fleet), and the Forza eye isn't tied to the wheel the way DSF's seat is.
So the seat landed up to 25 cm from where the wheel needs it. On the
Mustang the wheel ended up 9 cm high and 13 cm close in the seat's frame
(host 193's eye sits unusually high and far back over its seat). PR #16's
survey measured the grips at the hinge (the cockpit rig), so it couldn't
see this.

**Fix** (`cockpit.patch_seats`, `seat_for_marker`): the driver seat goes
where it puts the steering marker (already moved to the Forza wheel centre
plus the hand shift) at `STOCK_MARKER_FROM_SEAT` in the seat's frame, x
included. The seat tilt stays the host's. The passenger stays mirrored. The
head camera (VO3D 430..432) still comes from the Forza eye, so the cockpit
view doesn't change. The report's `hierarchy.seats` has `body_eye`, the
driver model's eyes (seat + `FLEET_EYE_ABOVE_SEAT`, the stock median), and
`body_eye_from_camera`.

**Survey** (`uv run dsff driver_survey cars`, offline from out/cache and the
stock hosts). It measures the grips of the driver model (the stock rig at
the stock point of the seat) against the drawn rim (the cockpit wheel fit):

| Cars | Grips off the rim, before | After |
| --- | --- | --- |
| 40 test-drive cars (hosts of `out/testdrive11`) | median 6.0 cm, worst 10.5 (Mustang Boss 429; Supra 9.9, M5 9.9, Skyline 9.8, LFA 9.7), 31 over 3 cm | worst 0.4 cm, none over 3 cm |
| 562 of `all.txt` (hosts of the full-roster build; 11 had no report) | median 6.2 cm, 90th percentile 12.0, worst 24.3 (330 P4), 468 over 3 cm | median 0.0, 90th percentile 0.5, 20 over 3 cm |

The 20 left over 3 cm are the cockpit's own grip error, the same in the
cockpit view: rims far from the stock 0.176 m, where the 6 cm cap on the
hand shift binds (five FG Falcon racers 9.1 cm, Tour bus 6.2, TT-R 5.6,
LMP and FLM racers 3.0 to 3.5).

**Head room.** The driver model's head moves with the seat, by the
difference between the Forza eye and DSF's eye relative to the wheel. Over
the 562 cars the seat moves up by a median 3.9 cm (10th to 90th percentile
-0.4 to 7.5; Mustang 5.7, F-350 17) and back by a median 3.7 cm (-4.0 to
9.9). The room from the
driver model's eyes straight up to the Forza body (LOD 1) against the stock
fleet's (full entry, 143 cars with a roof: median 18.1 cm, 5th percentile
12.3, lowest 8.7):

| Cars | Before: median, under 8.7 cm | After |
| --- | --- | --- |
| 40 test-drive cars | 21.9 cm, 1 (2000GT 8.6) | 19.0 cm, 1 (2000GT 4.9) |
| 562 of `all.txt` | 22.3 cm, 16 | 18.1 cm, 25 |

One vertical ray is a coarse measure: some cars were already near 0
before (Prius 0.3, Challenger 1970 3.6) and look normal. In game (run
`vm-20261024-135500`, `out/chase/roof`: NSX 2005, Apollo S, Fusion racer,
Corvette Grand Sport, Aventador LP700-4, Challenger 1970, Regal GNX; all
under 8 cm) no head shows through a roof from the fixed views, and the hands
hold the wheel.

In game (`out/chase/after` against `out/testdrive11`, runs
`vm-20261024-133745`, `-140036` and `-134603`): the Mustang's, M5's and
LFA's hands are on the drawn rim now
(`img/chase-hands-seat-before-after.jpg`). The Fusion racer, Corvette Grand
Sport, Aventador, Challenger 1970 and Regal GNX of `out/chase/roof` also hold
their rims.

## Gauges under the cowl, and virtual dials (2026-10-25)

User report on the installed full roster (`out/final-gui2`): the Mercedes
CL65 AMG (FM4, ID 537, host 279) shows no gauges in the cockpit view.

**The CL65 (confirmed-static).** Its Forza cluster has three parts: fuel and
temperature dials on the left, the tachometer on the right (a needle and a
`gauge_emissive` face), and in the middle a 17 x 12 cm `digital_gauge`
display (2 triangles) on which Forza's shader draws the speedometer at run
time. `CarAttribs.xml` has `<Speed NumTicks="0"/>` and the cockpit carbin
no `speed_*` needle. So:

- the converted car had a tachometer needle only, and the centre display
  was a blank near-black panel (`DISPLAY_COLOUR`);
- most of the tachometer was hidden: `camera_eye` raised the head camera
  5.5 cm above the Forza eye to see the rim 7.9 degrees down, and from there
  the cluster's cowl covers the top of the recessed dial (the marks from 2
  to 6). Forza's own geometry rendered from the raised camera shows the same.
  From the Forza eye all of the needle's sweep is in sight, from the camera
  62%.

**Survey** (`out/gaugesurvey/`, offline over the 573 cars of `out/final-gui2`:
the manifest's `cockpit.interior.needles`, each cockpit carbin's needle
sections and `digital_gauge` patches). 408 cars had both needles working, 3
keep the host's stock cockpit. The 162 missing a speedometer or tachometer, by
cause:

| Cause | Cars | Fixed here |
| --- | ---: | ---: |
| the gauge is a blank `digital_gauge` display in the dashboard (CL65, S65, Viper 2013, Agera, Reventon, Range Rover, most GT racers) | 51 | 48 |
| the display has its own face (PR #11: S2000, LFA, A4 DTM, FG Falcons...) | 35 | |
| the only display is on the steering wheel (LMP, ARX, FLM racers) | 28 | |
| placeholder cockpit carbin or none (FH1 traffic cars) | 15 | |
| the car has no such gauge (250 GTO, GT40: no speedometer; Mini 1965, Beetle 1968: no tachometer) | 15 | |
| small displays only (bar or digit strips, under 25 cm2: Commodore VE, Corvette C5-R...) | 15 | |
| the needle is there but `fit_gauge` failed (Cooper S 2003, Lancer Evo IX MR, Insignia VXR) | 3 | 3 |

Separately, the raised camera hid more than 5% of the needle sweep the Forza
eye sees on 70 cars (RS6 2003 and CL65 39%, S4 2004 and Dino 246 33%, Pantera
27%).

**Fixes** (`cockpit.py`):

1. *Camera under the cowl.* `camera_eye(..., sight=)` raises the camera in 5 mm
   steps and stops before a step hides more than `GAUGE_SIGHT_LOSS` (5%) of
   the needle-sweep samples the Forza eye sees (`GaugeSight`: rays to the
   tip and to a point 60% out at 13 angles over each speed and tach needle's
   sweep, against the opaque LOD-0 cockpit; glass doesn't block). The rim
   rule still sets the most it may rise. 70 cars rise less (median 1.9 cm
   less; CL65 5.5 -> 2.0 cm), none sees less of its needles than before, and
   none loses more than 5% to the cowl. The report has `cockpit.gauge_sight`.
2. *Virtual dials.* DSF's gauges are needles the engine turns (FUN_00891670,
   ids 1 and 2); it can't draw changing digits. Where Forza has no speed or
   tach needle, `virtual_gauges` puts a dial on the largest blank
   `digital_gauge` patch in the dashboard (not on the steering wheel: needles
   aren't skinned and wouldn't turn with it) that is at least 25 cm2, faces
   the eye within 60 degrees and is half in sight. The dial: tick marks, an
   outer arc, a red zone from the redline and seven-segment numerals as
   flat-colour geometry (`dsf_dial_mark`, `dsf_dial_red`: interior atlas
   cells), and a needle with a hub on the host's needle material, driven by the
   host's gauge entry like any Forza needle. 240 degrees, or 180 where the
   display is wide and low; two side by side (tachometer left) when both are
   missing. The scale comes from the Forza car: speedometer to the top speed
   + 5% in steps of 20 or 40 mph (at least 120; p605 stops the needle
   there), tachometer to the next 1000 rpm at least 250 rpm over the redline.
   The sweep is a whole number of degrees per 80 mph or 5000 rpm, and the
   marks are drawn at that sweep, so the needle sits on them. Report:
   `cockpit.interior.virtual_gauges`.
   - A host whose gauge table has no entry for the id gets no dial
     (`drop_virtual`): stock 250, the Volt's host and the Volt itself, has
     no gauge entries at all, so its virtual needles would not turn.
     Since #62 a one-gauge host gets the missing record instead, and only a
     host without any gauge record drops the needle (`drop_unturned`, "One-gauge
     hosts" below).
   - The CL65's TFT showed a virtual round speedometer in the real car;
     the race-car LCDs showed digits. A dial there is a stand-in that reads
     right, not what Forza draws.
3. *Needle poses welded differently.* The min pose of three FM4 needles has
   a few more triangles than mid and max, so `fit_gauge` found no point
   match and dropped the needle. The matched poses now give the pivot and
   dial normal, and the unmatched step's angle is the turn about that pivot
   that lays one pose's points onto the other's (`_turn_onto`).
4. *Stride-16 needles.* Hosts 250 and 293 draw their Dials material from a
   16-byte buffer (no normal; 145 stock interiors use 20). `_needle_vertex`
   writes the host's layout. No car on those hosts had needles before.

After (offline, same survey with the new code): 459 cars with both, 111
missing one. What is left is digital readouts the converter can't place a
needle on (faces, steering-wheel displays, small strips, the Volt), stub
cockpits and cars that have no such gauge.

Offline renders at 60 mph and 4000 rpm (`cockpit.render_gauges`, before:
master, after: this branch; `out/verify`): `img/gauges-cowl-virtual-offline.jpg`
(CL65, RS6 2003, Viper 2013, Cooper S 2003).

In game (VM, confirmed-runtime; the same 10 cars at IDs 1 to 10 built from
master and from this branch, `-HoldMph '40,90'`): runs `vm-20261025-081521`
(before) and `vm-20261025-082011` (after). The CL65 had no gauge in sight
(the tachometer under the cowl, the centre display black); now its virtual
speedometer reads 40 and 90 at the held speeds, with the tachometer beside
it. The RS6's dials were cut off at the top by the cowl; now both show whole
with the needles at the readings. `img/gauges-cowl-virtual-ingame.jpg`. All
cars that spooled drove without a crash (IDs 8 to 10 didn't spool after
seven cars in one session, in both runs).

## One-gauge hosts and needle layouts (2026-10-26, issue #62)

On 12 cars of the full roster (`out/final3`) a speed or tach needle was drawn
and never turned. `uv run dsff cockpit needles <archive>` (`needle_survey`)
lists, per ported car, the gauge ids its interior needles carry against the
gauge records its hierarchy has, and the needle triangles in a buffer that
declares no gauge id. `upvh lint` now checks the same per car.

**Gauge records (confirmed-static).** `FUN_0088E310` keeps, per gauge-table
index (seat byte +0x26), the index of every type-18 record whose role isn't
185 and whose table entry has a gauge id below 8, at spec+0x47D4 + index x 4.
The render loop (`FUN_008A4660`) goes over those 7 slots: for each kept
record it reads the record's table entry, takes the gauge id from it, and
writes matrix `id` of `g_InstrumentTransforms` from the entry and input `id`.
Nothing else ties a needle to a record, and the needle vertices only name the
id. Over the 150 stock hierarchies (`out/g62/stock_gauges.py`): 125 have both
records, 19 a speedometer record only (146, 149, 153, 156, 178, 187, 191,
198, 200, 202, 230, 231, 249, 257, 259, 271, 272, 287, 302), 225 (MP4-12C) a
tachometer record only, 5 none (123, 131, 250, 289, 293). Every gauge record
is a leaf under the cluster part (role 185); the speedometer's has role 0,
the tachometer's role 187, and all other fields match between the two.

**Needle layout (confirmed-static).** `rebuild_drsp` gave each new vertex
buffer the declaration of the host's last buffer of the same stride. Hosts
119, 146 and 208 have a second stride-20 layout in the interior after the
needles' (semantic 8 two halves, then a semantic 9 skin), so the needles of
the Audi S5 (119) and the three Camaros (146) had no DashboardInstrument
element: the gauge id was read as a skin byte, and the needles stayed put.
No other entry of any stock host has a material whose own buffer's
declaration differs from that last one, apart from one exterior material of
287 the converter doesn't use (`out/g62/vdcl_survey2.py`).

| Cause | Cars (spool ID) |
| --- | --- |
| host 249, speedometer record only | C1 (323), Ka (404), 107 (605), Agila (698): tach |
| host 230, speedometer record only | 442 (595): tach |
| host 225, tachometer record only | F40 (373), Venom GT (420), Ultimate Aero (662): speed |
| host 146: needles in the wrong layout, and a speedometer record only | Camaro SS 69 (294), Z28 69 (295), Z28 70 (296): both |
| host 119: needles in the wrong layout | S5 (96): both |

**Fix.**

1. `cockpit.add_gauge_records`: for a speed or tach needle the host has no
   record for, a copy of the host's gauge record goes in next to it (same
   parent, the stock role for the id; the speedometer's first, as in stock
   hierarchies), and a copy of its table entry with the new id is appended
   to the table; `patch_hierarchy` then sets its pivot, dial and sweep like
   any other. Record count, seat count and the ancestors' subtree sizes
   follow. Report: `cockpit.hierarchy.gauges_added`. On all 20 one-gauge
   hosts the result parses and `host_cockpit` finds both slots
   (`out/g62/stock_add.py`). The host choice keeps its -3 for a host without
   both gauges; it no longer matters for the needles.
2. `rebuild_drsp` takes each buffer's declaration from the host buffer its
   materials are drawn from (where the host has one of that stride), else
   the last of the stride as before.
3. A needle a host still can't turn (no gauge record at all: 250, the Volt's
   host, whose Forza cockpit has no needles) is dropped, Forza's as well as
   virtual dials (`drop_unturned`, which replaces `drop_virtual`).

**Survey, before and after** (`dsff cockpit needles`; before `out/final3`,
after `out/g62/final`: `port_batch build --reuse out/final3 --trust-code
--rebuild-where "host in (...)"` over the 23 hosts with one or no gauge
record or a second needle-stride layout, 35 cars built, 538 reused, lint
clean): cars with both needles turning 449 -> 461, cars with a needle that
never turns 12 -> 0; the 12 above and no other car changed. The other 23
rebuilt cars (on the same hosts without the problem, and 16 whose
fingerprint was incomplete) came out byte-identical to `out/final3`.

Offline at 0/0, 40/3000 and 90/6000 (mph/rpm), same view before and after
(`img/gauges-one-gauge-hosts-offline.jpg`): the F40's speedometer, the 442's
and the C1's tachometers now turn; before they stayed at rest.

In game (VM, confirmed-runtime; `run-series.ps1 -Vm -Asi out\asi\dsfforza.asi
-HoldMph '40,90' -ShotSeconds 3`, before `out/final3`, after `out/g62/final`,
same IDs): runs `vm2-20261026-164751` and `vm1-20261026-171004` (before),
`vm1-20261026-165841` and `vm2-20261026-171113` (after). Before, the F40's
speedometer and the C1's and 107's tachometers sat at rest at 40 and 90 mph;
after, they turn with the speed and rpm (`img/gauges-one-gauge-hosts-ingame.jpg`).
The Camaro's and the 442's tachometers are behind the driver's hands in the
cockpit view, in both builds. All eight cars of the after runs (373, 294, 595,
323, 420, 404, 662, 605) spooled and drove without a crash, so the inserted
record (and on 225 the record after it moving down one) didn't upset the
engine.

## Hands on small race wheels and wheels with columns (2026-10-25)

PR #29's survey left 20 cars with the driver's grips over 3 cm off the
drawn rim, in the cockpit view and the chase view alike (the seat follows
the marker, so both use the same anchor). `uv run dsff driver_survey cars`
on the installed build (`--hosts out/final/work`) gives the same: median 0.0
cm, 90th percentile 0.5, worst 9.1 cm, 20 over 3 cm. They fall in three groups:

| Cars | Rim (fit) | Why the hands miss |
| --- | --- | --- |
| five FG Falcon racers (9.1 cm) | 0.06 m, column 74 deg | the `steering_wheel` section carries its 1 m column to the bulkhead; the least-spread direction of all points is not the wheel's |
| TT-R 2004 (5.6 cm) | 0.13 m, column 43 deg | a deep quick-release spacer behind an open-top yoke tilts the fit the same way |
| tour bus (6.2 cm) | 0.22 m, column 70 deg | a flat bus wheel: the shift needed along the column ate the 6 cm cap |
| ARX, FLM, B09, R15, AMR-Lola racers (3.0 to 3.5 cm; 908, R8 LMP, A4 DTM and others 2 to 3 cm) | 0.11 to 0.12 m, column 2 to 12 deg | the rig grips 0.25 m apart, 45 deg either side of the top of a 0.176 m rim: a rim narrower than that can't hold both hands for any translation |

**What the cap was for.** The hand rig is rigid: it doesn't stretch its
arms, it moves as a whole with the steering marker (cockpit view) and with
the seat (chase view, where the seat follows the marker). What a large shift
costs is (1) the hub's orbit while steering: the wheel and the hands turn
about the marker, so the in-plane part of the shift makes the hub circle by
up to 2 |shift| sin(angle / 2); (2) the driver model moves with the seat by
the shift. The part along the column has no orbit. Moving the seat alone
doesn't help: the chase-view hands would rest on the rim but turn about a
point the wheel doesn't, and leave it while steering. The hand targets
themselves (the rig's grip points) can't be moved per car.

**Fixes** (`cockpit.py`):

1. `fit_wheel_points` fits candidates and keeps the one whose rim is the best
   circle (`rim_scatter`: RMS radial misfit plus spread along the axis): the
   least-spread axis of all points (as before), the rim's own plane refitted
   three times, and both again on the 0.22 m at the driver's end when the
   points run deeper than 0.30 m. Over the fleet 208 fits are unchanged,
   the median centre moves 0.1 mm (90th percentile 1.1 mm); the Falcons'
   rim comes out at 0.164 m and 17 degrees, the TT-R's at 0.131 m and 5.
2. `hand_shift` caps the in-plane part (the orbit) at 6 cm as before, and
   the part along the column on its own at 12 cm.
3. `hand_wheel_scale`: where the grips still miss the rim by more than 5 mm,
   the Forza wheel is scaled about its centre (0.8 to 1.3) to the radius
   the grips sit at: the cockpit wheel, and through `align_main_wheel` the
   chase view's. 36 cars, 1.04 to 1.21 (ARX-01b 0.114 -> 0.137 m), the bus
   0.88. The report has `steering.hands.wheel_scale`.

**Survey after** (same command, this branch): grips off the drawn rim median
0.0 cm, 90th percentile 0.2, worst 1.2 cm (Jaguar D-Type 1956; Mini 1965
1.1), none over 3 cm (20 before). Worst cases before -> after: FG Falcon racers
9.1 -> 0.1 cm, bus 6.2 -> 0.0, TT-R 5.6 -> 0.2, ARX-02a 3.5 -> 0.3, B09 3.4
-> 0.2, R15 TDI 3.3 -> 0.2. Room over the driver model's eyes is unchanged
(median 18.3 cm). Offline, the chase-view wheel from behind the seat with the
driver's grips (`out/verify`, before: master): `img/hands-wheel-fit-scale-offline.jpg`
(the Falcon's chase-view wheel had been aligned onto the column fit, a metre
ahead).

In game (VM, confirmed-runtime; `out/verify/before` from master and
`out/verify/hands` from this branch, the same 10 cars at IDs 1 to 10; with
`-Override dsf_enhanced.ini` with `SkipLegalScreen=0`, see "Crashes while the
world loads" in `in-game-tests.md`): before `vm-20261025-092305`; after
`vm-20261026-122057` (147, CL65, 279, Falcon) and `vm-20261026-123628` (147,
TT-R, ARX-01b, bus).
- The FG Falcon had no cockpit: the eye is placed from the wheel fit, so the
  camera sat a metre ahead, over the bonnet. Now it shows its right-hand-drive
  cockpit with the hands on the rim.
- On the TT-R and the ARX-01b the 2 to 3 cm the survey measures barely shows
  from the head camera: both before and after, the hands hold the sides of the
  rim and the yoke's grips. `img/hands-cockpit-ingame.jpg`.

**The CL65 (user report: chase-view hands off the wheel).** Not one of the 20:
its cockpit grips miss by 0.01 cm, and the chase view's wheel (full entry,
skinned to the steering record) sits on the cockpit fit with the stock grips on
its rim (`out/handsurvey/drawn_wheel.py`, `grip_view.py`). Its host 279 has the
stock seat relation. In game, the fixed side views of the CL65 against stock 279
itself in the same session (`vm-20261026-122057`): the driver sits and holds the
wheel the same way in both, the right hand on the top right of the rim and the
left lower on it; from the passenger side the far hand shows high, at the
windscreen header, on both cars (the camera looks down from 1.9 m at a hand a
metre further away). `img/hands-cl65-vs-host279.jpg`. At rest the CL65's driver
holds the wheel as the stock host's does. What the user saw is not reproduced;
the open question is whether it was while steering (the hand-over-hand pose at
full lock) or another view.

## The steering wheel turns about its hub (2026-10-26, confirmed-runtime)

User report on `out/final4` (master c395515): the Buick Regal GNX's
(BUI_RegalGNX_87, FM4, host 229) wheel sits under the hands, but "when it
turns, it's not turning on its axle".

**What turns the wheel** (Ghidra, `out/ghidra/steer4`, `steer9`):
`FUN_0088E310` keeps the steering marker's position (spec+0x37E0) and a
direction from its quaternion (spec+0x37F0); `FUN_005743E0` copies that
direction into the driver object; `FUN_005756E0` builds the steering matrix
with `FUN_00514150` (axis = that direction, angle from the animation state).
With the experiments of "Driver's hands" (the hinge moves the wheel against
the hands, the marker moves both): the skinned wheel is drawn at
A + R(v - hinge), so in the mesh's own space it turns about the line through
the hinge pivot along the marker's direction.

**Cause.** `hand_shift` put the hinge and marker at the Forza wheel centre
plus a shift that brings the stock grips onto the rim, and up to 6 cm of it
lay in the wheel plane. The wheel then turned about a point off its hub, so
the hub circled with that radius (2 |shift| sin(angle / 2) away at an angle):
the Regal's 3.2 cm (a 0.200 m rim, the grips 2.4 cm inside it at rest) puts
the hub 4.5 cm away at 90 degrees. The axis was never the problem: the
marker's direction is within 2 degrees of the rim's normal on every car.

**Measure** (`uv run dsff steering_axle <archive>`, new): per car, the rim the
view draws (the interior entry's skinned vertices for the cockpit, the full
entry's finest LOD for the chase views), fitted with `fit_wheel_points`,
against the line the engine turns it about: the hub's distance from the line
and the angle between the line and the rim's normal.

| Archive | Hub off the turn line (cockpit) | Over 0.5 / 1 / 2 cm | Line vs rim normal |
| --- | --- | --- | --- |
| stock fleet (146 cockpits) | median 0.03 cm, 90th pct 0.35 | 13 / 10 / 1 (School Bus) | median 0.4 deg |
| `out/final4` (573 cars) | median 1.91 cm, 90th pct 4.97, worst 11.2 (tour bus), Esperante 7.9, Corrado 6.6, ARX-01b 5.5, Regal 3.2 | 497 / 424 / 272 | median 0.2 deg, worst 1.9 |
| this fix, full roster (`out/axle/full2`) | median 0.01 cm, 90th pct 0.01 | 6 / 4 / 1 | median 0.2 deg, worst 1.9 |

The stock host 229 itself: 0.1 cm (cockpit and chase). The six cars left
over 0.5 cm have the hinge on the converter's own fit (in-plane shift 0); the
survey refits the culled interior mesh, and on these the two fits differ
(tour bus 5.1 cm and Kenworth T440 2.0: flat wheels whose lower half is
culled; rendered turned, their hubs stay put; Corrados 1.1, TT-R 0.8, E36 M3
0.6). The chase views are the same except for seven cars whose chase-view
wheel isn't the cockpit's (below).

**Where the grips come from: not per-vehicle data.** The hands are DSF's
animated driver rig, placed at the marker (cockpit view) or at the stock point
of the seat's frame (chase views), and turned with the steering matrix. The
only per-vehicle input `FUN_005743E0` reads besides the marker and VO3D
p538/p559 is the vehicle's VSCT record (`FUN_0086C5B0`, keyed by spool ID):
bytes +0x18 and +0x19 pick two of seven animation hashes at `DAT_00F08024`
(`2614F404`, `2614F404`, `3715F405`, `2225FB4E`, `3109045A`, `41104D45`,
`8AC4BCAD`; index 7 is 0) and +0x1A is a flag (driver +0xCE; set on 57 stock vehicles). The idle-state
code (`FUN_00574A10`, `FUN_00575150`) queues +0x50 or +0x4C when the gear
(driver +0x58, from `FUN_008AD7F0`) goes down or up: they are gear-change
animations; with the flag set, the shift animation plays only when one of
the two gears is neutral. Stock trucks and buses (rims
0.197 to 0.206 m) use set 0, as the Audi R8 does (0.178 m). So the grip radius
is baked into the shared driver animations, not set per car. The stock fleet
handles larger wheels two ways: the trucks keep the hinge on the hub (0.1 to
0.4 cm), which leaves the stock grips about 2.5 cm inside their 0.20 m rims
(offline); the School
Bus (0.258 m) moves its hinge 8.6 cm off the hub, the way the converter did
for every car.

The per-frame side (`FUN_005756E0`, `FUN_00574A10`, `FUN_00573E10`): the
steering code fills an output block with a 4x4 matrix (the turn about the
marker's direction through A), a queue of animation hashes and named
parameters (`FUN_00575AF0(hash, float)`, e.g. 0x726F64CA), and
`FUN_00573E10` hands them to the driver's animation graph
(`FUN_0085E010` queues the hashes, `FUN_0085E180` sets each parameter by
hash in the graph's parameter table at +0x8C/+0x90, typed float, vec2 or
vec4). No parameter carries a wheel size; the grips are in the graph's
animations. The matrix is the only per-car geometric input, and the wheel
uses it too (p538/p559, which feed it, move the wheel with the hands), so a
per-car scale of it in dsfforza.asi would scale wheel and hands together.
Moving the hands alone would mean editing the driver's hand bones after the
graph has posed them (a new hook, not attempted: issue #99).

**Fix** (`cockpit.hand_shift`, `hand_wheel_scale`, `seat_anchor`):

- The hinge and marker move along the column only (least squares, capped at
  12 cm as before), so the wheel turns about its own hub.
- The grips then sit about 0.17 m from the column. Forza's wheel keeps its size
  unless the grips would miss its rim by more than 2 cm (`HAND_FIT_TOLERANCE`,
  about what stock trucks leave); then it is scaled just enough to bring them
  to 2 cm, within 0.85 to 1.3 (`WHEEL_SCALE_RANGE`). The chase-view wheel
  follows through `align_main_wheel`. (Since 2026-10-27 only the chase-view
  wheel is scaled; the cockpit wheel keeps Forza's size and dsfforza.asi moves
  the hands: "Hands on Forza's rim".)
- The seat doesn't move: `seat_anchor` reproduces the old placement (the
  translation that put the grips on Forza's rim, scaled as before), and
  `patch_seats` puts the stock point of the seat's frame there instead of at the
  marker. Over the 570 reports of `out/final4` it lands within 0.4 mm of the
  old marker (0.43 cm on the Viper CC, the report's rounding). The cost: in
  the chase views the driver's hands now turn about the old anchor while the
  wheel turns about its hub, so they drift off the rim while steering by up
  to the old in-plane shift (median 1.9 cm, at most 6), where before the hub
  drifted under them.

Scaling against `out/final4` (36 wheels scaled by 1.04 to 1.21, plus the bus
0.88), predicted from the reports: 414 of 570 wheels keep Forza's size; 156 are scaled, 71 of them by
under 5% (Regal 0.200 -> 0.196 m, 0.98; S5S Raptor 1.03). The large ones:
tour bus 0.85 (grips still 3.5 cm off: its 70-degree column), Kenworth T440
0.91, rims of 0.201 to 0.209 m 0.94 to 0.97; race yokes 1.2 to 1.3 (A4 DTM,
R8 LMP, FLM, ARX-02a, 908, B09, AMR-Lola, Furai, SR8, 905), the three ARX-01b
at the 1.3 cap with the grips 2.4 cm off. Grips off the rim in the cockpit
view: median 1.3 cm, 90th percentile 2.0, worst 3.5 (bus); over 2.05 cm
only the bus, the three ARX-01b (2.4) and the two Mini Cooper S 1965 (2.2).
The full-roster build (`out/axle/full2`, 573 built, all lint ok) has 153
scaled (the fit on the built mesh differs slightly), grips median 1.3 cm,
90th percentile 2.0, worst 3.5, and every driver seat within 0.2 mm of
`out/final4`'s. PR #69's survey had 0.0 median: at
rest the hands sit up to 2 cm inside or outside the rim now, the price of a
wheel that turns about its hub without resizing Forza's wheel.

**Verified.** `uv run python -m tests`. Offline, the head-camera view with the
wheel turned the way the engine turns it (`out/axle/steer_view.py`; master
`out/axle/before`, fix `out/axle/after`, the same 5 cars):
`img/steering-axle-offline.jpg`. In game (VM, confirmed-runtime;
`run-series.ps1 -Vm -DriveKey Left -DriveSeconds 10 -ShotSeconds 2`, probe
IDs 1 to 4, 229, 152, 246): before `vm1-20261027-050904` (master), after
`vm1-20261027-051314`. At DSF's full lock (hand over hand, standing) master's
Regal wheel swings up and to the left off its column; with the fix it turns
in place, and at rest both hands hold its rim
(`img/steering-axle-ingame-regal.jpg`). All seven cars spooled and drove in
the after run (the before run skipped the School Bus: "NOT spooled after 40
ticks"). From the head camera the stock trucks' grips sit too low in the
frame to judge against their rims.

**Chase-view wheels not on the cockpit wheel** (issue #86, not fixed here): the
Express and F-350 keep their same-vehicle host's cockpit while the chase view
draws Forza's main-carbin wheel, and the E36 M3, M Coupe, Camaro ZL1 and
XKR-S's main-carbin wheel fits are off (`align_main_wheel`), so their chase-view
hubs sit 3 to 8 cm off the turn line.

## Hands on Forza's rim (2026-10-27, issue #99)

PR #90 left the stock hands gripping a fixed circle 0.177 m from the column and
scaled 153 Forza wheels (0.85 to 1.3) where they missed it by more than 2 cm.
Now `dsfforza.asi [hands]` (`tools/asi/src/mod_hands.cpp`) moves the hands
onto each car's own rim after the animation has posed them, and the wheel
always keeps Forza's size.

**The cockpit arms** (confirmed-static; `out/ghidra/hg1`..`hg14`):

- Class vtable 0x00DE3D38 (ctor FUN_00566F20, 0x3A4 bytes). The character
  manager (FUN_0055AF30, `[engine+0x2A8]`) holds two at +0x10, one per local
  player. FUN_0055A350 -> FUN_005672B0 attaches one to the player's vehicle
  (+0x348) with two models from FUN_00559F50, chosen by the VSCT record's byte
  +3 (3: one set, 0/1/2/4 the other): +0x340 sets up the animation graph,
  +0x344 is drawn.
- Each frame, the vehicle's draw (FUN_0087A5D0 -> FUN_005676B0, FUN_0087A6A0 ->
  FUN_00566FD0) does:
  - FUN_005676B0 evaluates the graph (+0xD4; skeleton at +0xD8) with
    FUN_008609C0 (the call at 0x005676FF) into the global pose buffer
    0x01214400. That is 64 joint transforms of 0x20 bytes: quaternion (x, y,
    z, w; `p -> q p q* + t`) and translation, already composed with each
    joint's inverse bind.
  - FUN_00BCA840 builds them from the local pose. The skeleton keeps, per
    joint j at +0x54 + 0x70 j: the parent index, the name hash (FUN_00BC9340:
    byte i mod 4 += character i), and the inverse bind at +0x2C.
  - FUN_005676B0 then lerps the steering matrix (+0x44 previous, +4 current,
    written by FUN_00573E10) into +0x360. Joint 1's rotation times that matrix
    turns the car's wheel (FUN_00875A50).
  - FUN_00566FD0 counter-turns the arm that leaves the wheel (joints 24..45 by
    +0x34C, 2..23 by +0x350) and draws the model through FUN_0055FAE0.
- The steering matrix is R(column, angle) T(A), so the pose is in the car's
  orientation with its origin at A (marker + p538/p559). Its translation row
  is the marker, as the probe below shows.
- No IK pass touches the arms. The only IK in the exe is the pedestrians'
  feet (FUN_0057F5B0, LeftFoot/RightFoot).

**The skeleton** (confirmed-runtime, `[hands] debug=1`; run
`vm2-20261027-074532`, Regal GNX). It has 46 joints and hangs from the wheel:

- 0 root and 1 the wheel joint, both at A.
- 2 RightHand: 15 finger joints, and RightForearm (at the wrist).
  RightForearm leads to RightForearmRoll and RightArm (at the elbow).
  RightArm leads to RightArmRoll, RightElbow and RightShoulder (at the shoulder).
- 24 LeftHand: the same.

The fist (the wrist and its 15 finger joints) sits on a circle about A, 0.177 m
from the column (to 0.3 cm), its normal 26.6 degrees up (the stock column is 24).
That holds at rest and at DSF's full lock: there the pose is the same and the
steering matrix turns it. Only the hand-over-hand frames leave the circle (12.6 cm).
`cockpit.hand_grips` (0.176 m, 45 degrees either side of the top, stock column)
is the same circle to within 0.3 cm and 2.4 degrees.

**Fix, cockpit view** (`dsfforza.asi [hands]`, research/asi.md "Hands on the rim"):

- **Per-car value.** The converter writes the rim into the car's VO3D
  (`cockpit.grip_params`): p95 is the drawn radius and p87 the centre from the
  steering marker along the engine's column direction. The marker sits on the
  hub's column (`hand_shift`), so the centre is on that line. p95 and p87 are
  "unused" in the type table (0x00F3E450), nothing reads them, and no stock VO3D
  sets them. The values travel with the car into any archive and to any spool ID.
- **The move.** The module wraps the call at 0x005676FF. In the pose frame
  (origin A, the car's axes) it moves each fist to the nearest point of that rim,
  by at most 10 cm.
- **The arm.** A two-bone IK: the shoulder stays, the elbow stays in the arm's
  bend plane, and both bones keep their length. The hand is only translated, so
  the wrist bends a little: on the 0.113 m ARX-01b yoke the hands move 6 cm in
  and the elbows about 3.5 cm.
- **Hands off the wheel.** A hand whose fist is more than 4 cm from both the
  animations' circle and the rim fades out of the move (none past 9 cm). So the
  hand going over the top in the hand-over-hand pose, or at the gear stick, plays
  as animated. The release weights (+0x34C, +0x350) scale the move too.
- **The wheel.** The cockpit wheel keeps Forza's size (`hand_wheel_scale` is
  gone). The hinge and marker still move along the column only (`hand_shift`):
  that keeps the wheel turning about its hub, and puts the rim in the plane of
  the animations' grips, so the move stays in the wheel plane.

**The chase views' driver is not the arms.** The probe never sees the arms
object outside the cockpit view (caller 0x0087A695, FUN_0087A5D0, only while
DriverEye is on). The driver in the chase and side views is a full-body
occupant: class set up by FUN_00567E60 (vehicle at +0x360, seat index +0x364,
the anim set by vehicle class: DriverSport, DriverSport_t, DriverSaloon,
DriverTruck), drawn by FUN_00568590.

- FUN_00568590 skips the player's own driver in the cockpit camera modes (3, 4).
- None of the four FUN_0055FAE0 call sites (0x0055FF55, 0x0056726D, 0x00568C1E,
  0x00580536) drew him in run `vm2-20261027-093214`, nor anything else but the
  cockpit arms. Pedestrians weren't drawn there either.
- So FUN_00573450's other branch (+0x32B set: FUN_0055A230 queues the
  character for a batched draw) is the likely path. It is not followed here
  (issue #117).
- Until that is hooked, the chase views keep PR #90's arrangement:
  - the seat holds the stock grips at `seat_anchor`;
  - the main-carbin wheel they draw is scaled by `chase_wheel_scale` (PR #90's
    rule: only past 2 cm, 0.85 to 1.3), so the chase-view hands miss its rim by
    the same median 1.3 cm, at most 3.5 cm;
  - while steering, the hands still turn about the old anchor.
- The cockpit and chase wheels now differ in size on those cars (the bus 0.220
  against 0.187 m, the ARX-01b 0.113 against 0.147 m). The two views never show
  together.

**Survey.**

- Roster, 570 reports of `out/final4`, Forza's radius = report radius / its wheel
  scale, with the converter's rig model (`cockpit.hand_grips`):
  - PR #90 scales 153 wheels, and its grips miss the drawn rim by a median
    1.31 cm, 90th percentile 2.00, worst 3.51 (tour bus).
  - Unscaled wheels with the stock grips would miss by a median 1.31, 90th
    percentile 3.25, worst 6.81; the largest move the module makes is 6.8 cm.
  - With the module, the cockpit grips land on the rim: 0.0 cm, none capped.
- `uv run dsff steering_axle` now also reports the cockpit grips (`grip_stock`,
  `grip_asi`) and the module's rim against the drawn one (`asi_rim`).
  - On the four test cars (`out/hg/after2`): grips 5.80 cm median (6.81 bus) ->
    0.03 cm (0.34, the Regal's 0.33 cm VO3D rounding); the module's rim is
    0.08 cm from the drawn one.
  - Hubs on the turn line: 0.01 cm. The chase view's numbers are PR #90's (bus
    5.1 cm, the culled flat wheel).

**In game** (VM 2, `run-series.ps1 -Vm -VmId 2 -DriveKey Left -DriveSeconds 10
-ShotSeconds 2 -Views left -WaitSeconds 900`, IDs 1 to 4 and stock 229):

- Before: `vm2-20261027-093214`, master's converter on Regal GNX, tour bus,
  ARX-01b and Mini Cooper S 1965.
- After: `vm2-20261027-100256`, this branch with `[hands] debug=1`.
- The log has each hand's move: Regal 2.3 to 2.4 cm out to its 0.200 m rim, Mini
  1.3 to 2.5 cm, ARX-01b 5.9 to 6.3 cm in to its 0.113 m yoke. Stock 229 is
  untouched.
- In the hand-over-hand frames one hand's weight drops (0.14, 0.61).
- `img/hands-rim-cockpit.jpg`: full lock and rest, before and after.
- `img/hands-rim-chase-left.jpg`: the side view, unchanged.
- The tour bus draws no cockpit (no arms, no interior) from its DriverEye camera
  in both runs, so it can't be judged there.

## Risks and open questions

1. Whether the cockpit view draws only the interior entry (hypothesis). If
   it also draws the exterior, the outer shell in the interior will z-fight.
2. Gauges: DSF needles are linear in speed and rpm, so Forza dials with
   unevenly spaced marks are only fitted (within about 5 degrees on most
   cars, up to about 30 on very uneven faces such as the Golf R and TT RS;
   see the offline gauge check above). The sweep is a whole number of degrees per 80 mph or 5000
   rpm (under half a degree off at full scale). Only two gauges exist per
   car; fuel and temperature needles are parked. The tachometer reads at
   least idle (p40) even with the engine stopped, as on stock cars.
   Taking angle = Param x sweep assumes Forza moves its needle linearly
   from the min to the max pose; the mid pose isn't always half-way (FH1
   Raptor speedometer 118 of 201 degrees), so Forza may run piecewise
   through it, which would move some marks by a few degrees.
3. The interior atlas is DXT1. Stock interiors use DXT1 textures, but none
   for the `_Interior` material; it loses the alpha specular mask.
4. Visibility culling assumes the camera stays near the eye. Looking back or
   a head-bob larger than 12 cm sideways (8 cm up, 6 cm down, 8 cm fore and
   aft; `CULL_OFFSETS`) may show holes in hidden areas
   (under the dash, behind the seats).
5. Paint in the interior points at the shared paint block
   (`PAINT_TEXEL_UV`), as stock interiors do (`lights.md`, "Paint").
6. Right-hand-drive Forza cars (the 1993 Skyline) put the wheel, marker and
   eye on the right. The stock fleet has no right-hand-drive car, so the
   head-camera code has never seen a negative marker x.
7. Door cards don't follow the doors (stock door-card vertices are skinned
   to the door records; the converter leaves them on the chassis).
8. The trim lift (40) and the label cut-out colour are by eye from renders,
   not from in-game lighting.
