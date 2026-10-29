# Cockpit view: interior entry, steering wheel, gauges, head camera

Updated: 2026-10-29

How DSF draws the cockpit view, where Forza keeps the same data, and what
`src/dsfforza/convert/cockpit.py` (`--cockpit` in `forza2dsf.py`, on by default in
`port_car.py`) builds from it. The vanilla examples use the first test host,
Saloon 15 (ASYM Desanne, spool 266, VEHC 3/4/5); ports now take a host picked
per car (`hosts.md`). Evidence levels follow `dngvehicles.md`. The sections
after "Current rules" are in the order the work was done, and later sections
replace rules in earlier ones; each replaced rule says where it went.

## Current rules (2026-10-29)

**What the cockpit view draws.** The interior entry (N+1000) alone
(confirmed-runtime 2026-10-29, "What DSF draws"). The bonnet, wipers, mirrors
and pillars seen from the driver's seat are the interior entry's own copies,
and so are their materials.

**What the converter puts in the interior entry** (`cockpit.fit`,
`convert_interior`; "What the converter does"):

- The Forza cockpit carbin's LOD-0 sections, without `cagerace`, the upgrade
  bonnets (`forza2dsf.vanilla_sections`) and the headlights, culled to what a
  head moving around the camera can see (`CULL_OFFSETS`; "Holes in converted
  interiors").
- An outer shell for the window views: the main carbin's remaining body
  sections at LOD 5 (`CockpitOptions.shell_lod`). It steps to a finer LOD while
  it closes the windscreen (`shell_blocks_view`) or lids the cockpit
  (`shell_lids_cockpit`: 3 or more of 9 rays from the eye to the wheel stopped,
  issue #157). In `out/build` (offline-checked, the 573 logs) 20 cars step
  finer: 17 for the windscreen (the 15 FH1 cars that draw the main carbin's
  cabin, the 205 T16, and the MC12 GT1 down to LOD 1) and 3 for a lid
  (both Audi R10s, the Monaro VXR) ("Cars without a usable cockpit carbin").
  If the MDXN is still over the
  limit, the cockpit is vertex-clustered (`DECIMATE_LADDER`), then the shell
  goes to its coarsest LOD unless that one closes the view. The limit is the
  largest vanilla interior (0x12D000) or `--mdxn-limit` if larger; `port_batch`
  passes 0x2C0000.
- A placeholder Forza cockpit (`is_stub`) gives the main carbin's cabin at its
  finest LOD instead, or the host's whole vanilla interior when the host is the
  same vehicle (3 cars in `out/build`: Express, F-350, Taurus SHO).
- Materials: cabin trims and dial faces come from `interior_LOD0`, and the
  outside sections (`a_pillar`, `hood*`, `mirror*`) from `nodamage_LOD0`, through
  one DXT1 atlas on the interior's own `_Interior` material
  (`forza_materials.classify_cockpit`). Chrome is drawn from its swatch there
  too: the cockpit has no chrome material, where vanilla interiors use
  `GLOBAL_LOD_Chrome` (`forza-materials.md`, Open 7). Mirror glass, tiling
  detail textures and stitching are flat atlas cells (`CockpitBinding`).
  Paint uses `GLOBAL_Chassis_LOD1`
  (`INTERIOR_PAINT`), the clean exterior's shared paint material, as vanilla
  interiors do. Glass uses `GLOBAL_Glass_LOD5` in the vanilla three-layer
  layout (`vehicle-model.md`, "Interior glass"). The exterior's gloss copies
  and chrome materials aren't used: from the cockpit, painted panels reflect
  as they do outside, while trims, scoops and chrome have only `_Interior`'s
  specular and no reflection (issues #136 and #232, "What DSF draws").
- Gauges: Forza's speed and tach needles go on the host's `_Dials` material
  with gauge ids 2 and 1, swept per the `CarAttribs.xml` tick table ("Gauges").
  A host missing a gauge record gets one (`add_gauge_records`, issue #62).
  Fuel, temperature and other needles are parked. Forza's digital displays are
  drawn live from their dash scenes on DSF's digital dials (`digital_dash`;
  "Digital displays"), with rpm pieces made of 1 mm cells and whole segments
  ("Shimmer"). Where no scene drives a readout, a virtual dial or a virtual
  display stands in ("Gauges under the cowl", "Virtual dials on display
  faces"). Open: #147 (7 racers with only a steering-wheel display), #148 (15
  stub cockpits), #172 (stray hundreds digit), #195 (1-pixel specks).
- Head camera: VO3D p430..432 from the Forza eye (`CameraOverrides`, else the
  fleet median), raised to see the rim 7.9 degrees down, but not so far that
  the cowl hides more than 5% of the needles' sweep (`camera_eye`,
  `GAUGE_SIGHT_LOSS`).
- Steering wheel and hands: the wheel is skinned to the host's steering record
  and keeps Forza's size in both views. The hinge and the steering marker sit
  on the Forza wheel's hub, shifted along the column only (at most 12 cm), so
  the wheel turns about its hub ("The steering wheel turns about its hub").
  p538/p559 are 0. VO3D p95 and p87 carry the rim to dsfforza.asi `[hands]`,
  which moves the cockpit arms' and the chase-view and AI drivers' hands onto
  it ("Hands on Forza's rim", "Hands in the chase views"). The driver seat
  stays at `seat_anchor` (PR #90's placement). The main carbin's wheel, which
  the chase views draw, is aligned onto the cockpit wheel (`align_main_wheel`);
  seven cars still differ (#86). The wheel turns by the steering angle's share
  of p298, so the lock from Forza's `SteerMaxAngle` (PR #138) doesn't change
  its pose at full lock.

**Builds from 2026-10-28 to 10-29 lack Forza's handling.** From #169 (e9a7f8c)
to #192 (dd1ec4a), `handling.load_forza` failed for every car (#191). Those
builds, the main checkout's `out/build` of 2026-10-29 00:13 among them, keep
the host's VO3D (so the tachometer's p40/p42 clamp is the host's), scale every
scene-drawn rpm bar to a 7000 rpm fallback redline, and draw virtual dials on
the fallback scales (160 mph, 7000 rpm). Measurements in this doc that came
from that window are marked where they appear ("Flicker and glare",
"Shimmer").

## What DSF draws (confirmed-static unless marked)

**The interior entry (N+1000, kind 1) is the whole cockpit model.** Its
MODL has four parts, all with one mask-64 LOD:

| Part | Desanne contents | LOD link |
| --- | --- | --- |
| 0 | 7 submodels: cabin, seats, dashboard, steering wheel, door cards, roof lining, glass, and a low-detail copy of the outer body (hood, fenders, rear deck; one paint submodel spans z -2.48..2.38) | 0, chassis record |
| 1, 2 | empty | 5 and 2 (rear and front bumper records) |
| 3 | the gauge needles, one stride-20 submodel (Dials material) | 1, the gauge-cluster record |

The full entry (kind 0) has no mask-64 LOD anywhere (all 32 parts checked),
so the cockpit view takes nothing from it.

**The cockpit view draws the interior entry alone** (confirmed-runtime
2026-10-29; until then a hypothesis from the interior's own outer body). The
FM4 Cougar Eliminator (spool 304, host 175) was built twice, once as usual and
once with an interior that has no outer shell and none of the cockpit carbin's
outside sections (`hood*`, `a_pillar`, `mirror*`; a local change, not
committed). From the driver's seat the second shows the road where the bonnet
was: no bonnet, scoop, wipers or door mirror (runs `vm1-20261029-104030` and
`vm1-20261029-104335`, observe-16 and -26; `img/cockpit-interior-only-ingame.jpg`).
So the exterior entry isn't drawn in the cockpit view, and everything seen of
the car's outside there is the interior entry's.

**Gloss from the cockpit (issue #136, offline-checked).** The Cougar's interior
entry in `out/build` (565) draws with four materials: `_Interior` (42,610
triangles), `_Dials` (8), `GLOBAL_Chassis_LOD1` paint (9,973) and
`GLOBAL_Glass_LOD5` (969). It references none of the gloss copies of the Lights
material, which the exterior entry defines and uses (`forza-materials.md`,
"Gloss"), and no chrome material. Painted panels seen from the cockpit
(bonnet, wings, painted pillars) therefore reflect as they do outside: the
paint material is the same one. The outside trims (the scoop's `matte_colors`,
`black`, chrome) come from the atlas on `_Interior`, which has
`g_hSpecularIntensity` 0.03 at power 24 and no reflection. #136 was closed on
2026-10-28 after the user saw glossy exterior trims from the cockpit on
v0.1.2, but nothing merged between the issue and v0.1.2 touched the cockpit's
materials (#143's gloss copies are exterior-only: `Texturing._plan_gloss` skips
a Texturing with a classifier, as the cockpit's is).
What looked glossy is the paint (its reflection on the Cougar's bonnet shows
in observe-16), or `_Interior`'s specular, which on the DXT1 atlas may be
unmasked (Risks 3) (hypothesis: the user's screenshot isn't on record). Chrome and gloss trims seen from inside
still have no reflection: issue #232.

Interior materials (Desanne): two local MACOs, `_Interior` (stride 28,
MODL value 5) and `_Dials` (stride 20, MODL value 1,
`g_hAlphaTestRefValue` 0.5), both sampling the interior's own TEXC (512²
DXT5, 10 mips). Shared engine materials (defined in the archive's `SSIC`,
`vehicle-model.md`, "Materials"): `10122b9653b14e2a` `GLOBAL_Chassis_LOD1`
(paint, stride 32: the same material the clean exterior paints with; it
samples the shared paint atlas, not the vehicle texture: `lights.md`,
"Paint"), `0012cc8576953429` `GLOBAL_Glass_LOD5` (glass, MODL value 7),
`0012833b12fe1027` `GLOBAL_Chassis_Simple` (TRIM), `10122b9753b14e2a`
`GLOBAL_Chassis_Simple_LOD1` and `00128338d7fd1027` `GLOBAL_Chassis_Chrome`
(not a trim, as this line used to say). One submodel uses the full entry's
Chassis material. Of these, `GLOBAL_Chassis_LOD1` (specular power 1500, a
second specular, sky and cube-map reflection), `GLOBAL_Glass_LOD5` and
`GLOBAL_Chassis_Chrome` reflect; `_Interior` has only `g_hSpecularIntensity`
0.03 at power 24, the Chassis material's values (offline-checked,
`MARP` values of the vanilla archive). Eight vanilla interiors ship DXT1 textures, so DXT1 is
accepted in kind-1 TEXCs.

**The steering wheel is skinned, not a part.** Vertex semantic 9 (4 x u8,
undecoded until now) is a hierarchy binding: `(record +0x34, record +0x2A,
light index, 1)`. Pairs seen on the Desanne match UPVH records exactly,
for example (20, 23) is record 68 (slot 20, model 23), (8, 9) record 36, a
rear door. Chassis vertices carry (0, 0, 0, 1). The steering wheel's
vertices in all three entries carry (1, 1, 0, 1): record 11, kind-0 part 30,
hinge 0. Hinge 0's pivot is the wheel hub and its axis the steering column
((0, 0.413, -0.911), 24.4° up from horizontal). The marker with role 0x2F
(47) sits on the hub, its quaternion turns (0, 0, -1) onto the column.

Fleet check (`out/cockpit/skin_survey.py`): for every vanilla car, take the
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
     and p606 to 0; 46 vanilla cars set p605 (DeLorean 88, Eldorado 120:
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
   clear, the matrix is zeroed (the needle disappears); no vanilla car sets it.

So the needle mesh's rest pose is 0 rpm / 0 mph, the u32 is needle degrees
per 5000 rpm (tachometer) or per 80 mph (speedometer), and a
counter-clockwise needle needs the dial normal flipped (the Pacer's
tachometer and the Aston Martins' do exactly that: their quaternions turn
(0, 0, 1) onto a normal pointing out of the dash). The tachometer never
shows less than idle or more than p42; the speedometer stops at p605.

Data check (`out/gauges/dump.py`, all vanilla cars in `out/gauges/vanilla.txt`):
the vanilla 370Z (227) has 100 (speedometer) and 127 (tachometer); its dial
runs 0 to 180 mph and 0 to 9000 rpm, which at these rates is 225 and 229
degrees, and the Forza 370Z's needle poses sweep 226.6 and 230.4 degrees
over the same marks. `cockpit.py gauges` (below) on vanilla 227 puts the
needles on 0, 60 and 120 mph and 0.65, 3 and 6 thousand rpm at those
readings (`img/gauges-vanilla-227.png`).

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
24, Raptor 4) draw digits with a shader. Since PR #131 the converter draws
their dash scene on them live ("Digital displays"); a display with no scene
stays a textured face ("Digital clusters") or a blank near-black display
(`DISPLAY_COLOUR`). (This paragraph used to call the first UV transform a
label sheet; it is the display's window into the scene's render target.)
A car whose cockpit has no speed or tach needle used to have no vertices with
that gauge id, so the host's entry drew nothing; such a readout now gets a
scene-drawn display, a virtual dial or a virtual display ("Gauges under the
cowl", "Digital displays").

`uv run dsff cockpit gauges <archive> --id N --png out.png --at 0,0 --at 60,4000`
renders the cluster from the head camera with the needles turned exactly
as the engine would at each (mph, rpm), clamps included (works on vanilla
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
(`DASHBOARD_INSTRUMENT`; every vanilla needle buffer, stride 20 or 16, has
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
vectors are in a frame turned 180° about y: chase-camera z values are
positive, and across the fleet `p432 ≈ 0.5 - marker.z` (bus: marker z 5.31,
p432 -4.78; Desanne: 0.49, 0.03). So in model space:

    eye = (marker47.x - p430, p431, -p432)

Vanilla eyes sit 0.25 to 0.37 m above and about 0.5 m behind the steering
marker. Only one vanilla car sets p430. `cockpit.head_camera()` computes this
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
1 to 4) are part of `vanilla_sections`, so they go into the full and exterior
bodies. That is right: the vanilla exterior also carries a low-detail cabin
with a skinned steering wheel.

Textures, by sampling each technique's UV triangles in every candidate
(`out/cockpit/atlas_probe.py`):

| Techniques | Texture |
| --- | --- |
| `gauge*`, `z_gauge_emissive_alpha`, `digital_gauge`; `badge` in cabin sections | `interior_LOD0.xds` (512²: dial faces, switch labels; alpha is the label cut-out mask). The needle swatch is red-orange (252, 45, 8) |
| cabin sections' `leather`, `plastic`, `metal`, `headliner`, `bump_*`, `reflector`, ... | `interior_LOD0.xds` too: the cabin's leather, plastic and metal artwork and its colour swatches (corrected 2026-10-23, see "Cabin trims sample interior_LOD0"; this row used to say the strips along the bottom of `nodamage_LOD0`) |
| outside sections (`a_pillar`, `hood*`, `mirrorL/R`): `chrome`, `black`, `plastic2`, `badge`, ... | `nodamage_LOD0.xds` (2048²; badges there since 2026-10-24, "Badges seen through the windscreen") |
| lamps | `lights_LOD0.xds` |
| `mirrorLeft/Middle/Right` | render targets in Forza |

`forza_materials.classify_cockpit` encodes this.

**Eye point.** `gamedb.slt` table `CameraOverrides` (FH1 167 rows, FM4 503)
has `CamCockpitOffsetX/Y/Z`. It is the eye relative to the steering wheel
centre with +z forward, so in Forza space (front at -z):

    eye = wheel centre + (x, y, -z)

On the 370Z (0.05, 0.22, -0.43) this puts the eye 0.28 m ahead of the
headrest and 0.17 m under the headliner; the Countach, F1 (centre seat,
x ≈ 0) and others land just as plausibly. The other z sign would put the
eye ahead of the wheel. A third of the rows are all zero ("no override");
for those the converter uses the median of the non-zero rows, (0, 0.21,
-0.46). `CarAttribs.xml` has `DriverHandsOffset` and gauge tick tables but
no camera.

## What the converter does

`cockpit.fit()` measures the Forza cockpit:

- steering wheel: axis = least-spread direction of its vertices; rim = points
  at 75% of the largest radius or more; centre = middle of the rim's extent
  in the wheel plane (a vertex mean is biased by uneven tessellation). Since
  PR #69 `fit_wheel_points` also tries the rim's own plane and the driver's
  end of a section that carries its column, and keeps the best circle ("Hands
  on small race wheels");
- needles: the min, mid and max poses are one mesh rotated about the pivot.
  The dial normal is the least-spread direction of the displacements, and
  each step is a 2D rotation whose fixed point is the pivot. Sweeps come out
  clockwise, 230° to 290°;
- side mirror glass centres; eye from `CameraOverrides` (the fleet median
  for an all-zero row).
- digital displays: the dash scene's plan (`digital_displays`, "Digital
  displays") and, where no scene drives a readout, virtual dials
  (`virtual_gauges`).
- the shell LOD (`shell_closes`, below).

`forza2dsf.convert(..., interior=)` with `ConvertOptions.cockpit`:

1. Skins the main carbin's `steering_wheel` in the full and exterior entries
   to the host's wheel record (semantic 9 = (1, 1, 0, 1) on the Desanne).
2. Rebuilds the interior entry with `Placement`: the cockpit sections minus
   upgrade parts (`forza2dsf.vanilla_sections`, as on the outside: `cagerace`
   and the extra bonnets `hoodb`.., `hoodCIrace`, `hoodPSrace` that 112 of
   572 cockpit carbins carry, all drawn over the vanilla `hooda` until
   2026-10-24; `forza-materials.md`, "Chrome trim and the Boss 429's black
   parts") and the headlights, needles without a DSF gauge as static
   geometry (fuel and temperatures at mid, others at min; see "Gauges"), and the main carbin's remaining
   body sections at LOD 5 as the outer shell, or a finer LOD where LOD 5
   closes the windscreen or lids the cockpit ("Cars without a usable cockpit
   carbin"). The steering wheel gets the
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
   colour. A dash scene's plan goes into the same part, on a copy of the
   vanilla Volt's digital-dials material with a texture of its own ("Digital
   displays").
3. Drops triangles never seen from the head: an ID buffer per cube-map face
   (256²) from the final head camera and a head-movement volume around it
   (`CULL_OFFSETS`: 12 cm either side, 8 cm up, 6 cm down, 8 cm forward and
   back) plus the Forza eye, glass transparent, everything within 25 cm
   kept, plus one ring of neighbours (see "Holes in converted interiors").
   On the IS F, 43,824 of 65,314 triangles stay.
4. If the MDXN is over the limit, it vertex-clusters the cockpit with
   growing cells (4 to 30 mm), then moves the shell to its coarsest LOD
   unless that LOD closes the view. The limit is the largest vanilla interior
   (0x12D000) or `port_car --mdxn-limit` if larger; `port_batch` passes
   0x2C0000 (larger VEHCs load: `spool-limits.md`). None of the three test
   cars needed it.
5. Patches the full entry's UPVH: the steering hinge pivot and axis, the
   role-47 marker (position and quaternion; hinge and marker at the wheel
   centre plus the hand shift along the column, VO3D p538/p559 zeroed and
   p95/p87 set to the rim for dsfforza.asi: "Driver's hands", "The steering
   wheel turns about its hub", "Hands on Forza's rim"), the speed and tach
   gauge entries (pivot, dial quaternion, sweep from the tick table; missing
   records added, "One-gauge hosts"), the gauge cluster hinge pivot (mean
   of the gauges), the mirrors table, and the driver/passenger seat markers
   (roles 10/11: the vanilla point of the seat's frame goes to `seat_anchor`,
   "The steering wheel turns about its hub"; before PR #90 it went to the
   steering marker, "The driver's seat follows the wheel").
6. Writes VO3D 430 to 432 from the eye: `p430 = marker.x - eye.x`,
   `p431 = eye.y`, `p432 = -eye.z`.

## Test archive

The first test build (2026-10-22, all three cars on host 266; history):
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
   and every converted vertex had 0 except the needles (4096). Vanilla
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
   0.23 / 0.33 / 0.25 m (vanilla: median 0.25, minimum 0.15). Part of the low
   eye came from a placement bug: Forza bodies sat 0.14 to 0.23 m too low
   on their wheels (`forza-sources.md`), now fixed, which lifts eye, roof
   and seat together.

## M5 windscreen streaks (2026-10-22)

The M5's windscreen showed broad white streaks from the inside in fix2 and
fix3 (`img/fix2-m5-cockpit.png`, `img/fix3-m5-cockpit.png`); the vanilla
Desanne cockpit glass is clean (`img/fix3-desanne-vanilla-cockpit.png`).
Ruled out offline: a second windscreen layer (the shell skips every section
the cockpit carbin has, so the main carbin's glassF never lands in the
interior), flipped or noisy normals (vertex normal within 1-2° of the face
normal, median, on all three cars), the sky term (0.6-0.7, like the
Desanne's interior glass), semantic 5's last two bytes (128/127 on vanilla
glass too).

Cause (confirmed-static; fix confirmed-runtime in run `20261022-030613`,
`img/fix5-m5-cockpit.png`): the interior glass
material `GLOBAL_Glass_LOD5` samples the same texture as the exterior glass,
and the part of its clean band that vanilla interiors use is not uniform: a
grey smear texture with long horizontal streaks at u 0-0.12, v 0-0.06,
flat black below it (`vehicle-model.md`, "The glass texture"). Two thirds
of vanilla interior glass triangles sit in the black block u 0.06-0.12,
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
followed never showed cracks; interior glass is now laid out like vanilla
(next section).

## Cockpit glass damage (2026-10-23, confirmed-runtime)

User report on `out/testdrive7`: from the cockpit camera converted cars'
windows never crack or shatter, while the exterior glass does. Cause and
fix: `vehicle-model.md`, "Interior glass". In short, vanilla interior windows
draw damage with their own geometry: crack-line slivers on rows of the
glass texture that only show in states 1–2, and rim shards that only show in
state 3, over a smear surface that shows in states 0–2 (on the windscreen
only a frame round an open centre). Converted interiors had one uniform
smear patch. `glass.InteriorGlass` now builds the three layers on every
converted window, on that window's panel record.

The vanilla layout also supports the hypothesis above that the cockpit view
draws the interior entry alone: a vanilla interior windscreen is open in the
middle and carries its own crack lines, which would be pointless over the
exterior glass. In run `host-20261023-174148` the vanilla 227's windscreen
centre stayed clear until shattered.

**In-game test** (run `host-20261023-174148`, `out/cockpitglass/build`:
IDs 1 M5 (host 147), 2 RX-7 (278), 3 370Z (227), 4 Challenger (173);
probe IDs 147, 227, 1–4; `run-series.ps1 -GlassLadder -ShotSeconds 3`).
`-GlassLadder` (new; `clonetest.lua` `GLASS_STEPS`) switches to the cockpit
view on a clean car, then calls `GameVehicleResource.applyDamage
{gameVehicle, damage = 0.25 / 0.5 / 1.0, location = (0, 1, 2), override}`
at cockpit ticks 8, 16 and 24, then `fullVisualDamage` at 32, logging each
step. Glass has no Lua call of its own (`lua-api.tsv`); `applyDamage`
raises the glass panels' crack states. The game ran at about 150 fps, so
a tick (30 frames) took 0.2 s and each step about 1.6 s (observe times in
`events.json`): 2–3 cockpit shots per car. Results (`out/cockpitglass/run1-sheet.png`,
`run1-m5-zoom.png`, `run1-227-vs-370z.png`, `run1-rx7-challenger.png`):

- M5: crack lines across the windscreen's left half and the left door glass
  after 0.25 (observe-11); after 0.5 smear gone and jagged shards along
  both edges of the A-pillar (observe-12, state 3).
- 370Z: faint top/left frame when clean (observe-22), a crack line after
  0.25 (observe-23), shards along the windscreen's top edge after 1.0 and
  fullVisualDamage (observe-24, -25). Vanilla 227 at 0.5: its top-left shard
  cluster (observe-06).
- RX-7 and Challenger: shards along the windscreen top after 1.0 (RX-7
  observe-18, -19); the Challenger's clean and 0.5 shots show the frame
  (observe-28, -29); after 1.0 the game dropped to the Shift view
  (observe-30), as it did for the M5 after fullVisualDamage (observe-13).
- No crash, no white streaks, no glass missing in the clean state.

Run 1's build drew shards 1.5–4 cm wide every 2.5 cm, a near-continuous
fringe on the 370Z's top edge where vanilla 227 shows a corner cluster; they
are now 1.2–3.5 cm every 3.5 cm, and gaps inside a window no longer count as
rim.

**Run `host-20261024-000609`** (final `out/cockpitglass/build`, same IDs,
`-GlassLadder -ShotSeconds 1`; 85 shots, 7–8 per cockpit ladder; strips
`out/cockpitglass/run2-ladder.png`, zooms `run2-cracks-zoom.png`,
`run2-227-370z.png`): no crash. M5: clean, then two crack lines from the
windscreen's top edge after 0.25 (observe-29 against -27). 370Z port: faint
cracks after 0.25, smear gone and shards along the top edge after 0.5 and
1.0 (observe-61, -63). Vanilla 227 at the same steps shows no clear shards in
view this time (run 1 showed its top-left cluster at 0.5), so the crack
state per hit varies between spawns. The M5 and Challenger dropped to the
Shift view after the 1.0 step, as in run 1. Vanilla 147's ladder again ran
before the first shot (the zoom phase); the first shot of a series comes
after `-ZoomPresses`.

**40-car survey** (`out/cockpitglass/testdrive`, `tools/batches/testdrive.txt`,
`out/cockpitglass/survey.py` against `out/testdrive7`;
`survey-testdrive.txt`): 40 built, all lint ok, no extra interior
decimation. Every car has a windscreen frame of 0.10–0.28 m² with
0.83–1.15 m of state-1 and 0.67–0.95 m of state-2 cracks; shards seen from
the eye 16–115 per windscreen (fewest where the dash hides most of the rim:
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

## Shared-trim garbage in cockpits (2026-10-22, offline-checked)

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
mirrors; no vanilla interior material is a mirror (the shared material at
vanilla mirror positions, `10122b9753b14e2a`, is also on grilles and rear
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
cockpit, the red A-pillar on a black car). Every vanilla host checked (227,
147, 229, 193, 251, 266) paints its interior with `10122b9653b14e2a`; the
converter now uses it whenever the host interior has it, as all checked
hosts do (`cockpit.INTERIOR_PAINT`), and the engine
trim `0012833b12fe1027` (193 has none; the old pick took its
Chassis_LOD1). Only 193 was affected. Offline: `img/cockpit2-mustang-raptor-before-after.png` (left r7, right
the fix), `img/cockpit2-offline-eyes.png` (all five). Not yet run in game.

## Cars without a usable cockpit carbin (2026-10-22, offline-checked)

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
- **Shell lidding the cockpit** (2026-10-28, issue #157): the windscreen
  rays pass over a lid below the eye. The Audi R10s' LOD 5 body closes the
  open cockpit with flat panels at the height of the wheel's top (DSF y 0.08
  to 0.17, eye 0.23, hub 0.02); the Monaro VXR 05's LOD 5 `frame` is two
  triangles, a car-wide plane at y 0.72 to 0.82 (Forza) through the cabin.
  In game the lid drew as a reflective silver plane through the driver's
  hands and the lower half of the view. `shell_lids_cockpit` casts rays from
  the eye to the wheel's hub and 8 rim points; `shell_closes` steps to a
  finer LOD when 3 or more stop short (`LID_RAYS`). Roster (573 cars,
  offline): 570 stop 0 rays at their shell LOD, the R10 10, R10 08 and
  Monaro 6; all three get LOD 4, where 0 stop. `render_eye_view` skips
  triangles that cross behind the eye, so the cockpit render didn't show the
  lid: look for it with the rays, not the render.
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
and the FH1 Fiat 595's main carbin didn't parse (`carbin.py`: "no
parsable sections found"). PR #33 fixed that parse: the 595 is in
`out/build` with its own cockpit (offline-checked, its log).

## Holes in converted interiors (2026-10-22, offline-checked)

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
3. Triangles smaller than a pixel at 256² covered no pixel centre and were
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
`--mdxn-limit 0x2C0000` covers it; under the vanilla 0x12D000 limit the
decimation ladder would start). `uv run dsff cockpit holes <archive>
--id N --png out.png` (writes `out-N.png`) renders 4 head positions x 6 directions with glass
drawn and empty pixels magenta (`img/holes-isf-head-views.png`); before and
after: `img/holes-isf-before-after.png` (the cluster hood behind the wheel
was missing in the left-look view), `img/holes-m3-before-after.png`.
Remaining magenta in the M3's door views is a gap the Forza cockpit itself
leaves under the seat.

## Light cockpit trims (2026-10-22, offline-checked)

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

## Cabin trims sample interior_LOD0 (2026-10-23, offline-checked; partly confirmed-runtime)

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
HUD badges are vanilla ones (RAM 3500, Range Rover Sport...) because the run
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
  some states use fixed angles of ±1.7456 rad, the hand-over-hand pose at
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
| marker quaternion +20° about x (M3, 1) | no visible change at rest | no visible change |
| hinge axis +20° (vanilla 136) | no visible change at rest | no visible change |
| p538 -0.04 to +0.10 (300SL, 24) | up | up, hands still on the rim |
| p559 +0.075 to -0.15 (Cuda, 7) | back and down | back and down |

So the hands sit at A + H and the skinned wheel at A + (v - hinge), with H
fixed. Only the hinge, the point of the wheel mesh that is drawn at A, moves
the hands against the wheel. When the car steers, both turn about A.
Vanilla cars make this work by construction. In all 146 with a
steering marker, the hinge and the marker are the centre of the same wheel mesh:
fit_wheel radius 0.176 m (median; quartiles 0.174 to 0.180; trucks 0.19).
The column is 24° up in the seat marker's frame, and the hinge sits at (0,
0.469, 0.428) from the driver seat marker in its frame (`out/steering/seat_frame.py`,
`vanilla_rim.py`). The hands grip about 45° either side of the top (vanilla 147
in game against an offline render). Vanilla p538/p559 (-0.1 to 0.1 m; buses
p559 to -0.3) move the wheel with the hands, relative to the driver's body.

**Why converted cars missed.** The converter put the hinge and marker on the
Forza wheel centre, so the rig gripped a 0.176 m rim at a 24° column. Forza
rims run 0.146 (S2000) to 0.203 m (Charger Daytona), and columns 10° (S5S
Raptor) to 30°. The grips missed the rim by up to 4.3 cm (Raptor; S2000 3.5,
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
translation that puts the vanilla grips on the Forza rim (y and z only;
capped at 6 cm). p538 and p559 are written as 0, so the wheel is drawn
exactly where Forza puts it. The marker quaternion still follows the Forza
column. The wheel then turns about centre + shift. The part of the shift along the
column costs nothing. The part in the wheel plane (up to 4.4 cm, S2000; 3.7
Raptor; most cars under 2.5 cm) makes the hub circle by 2·shift·sin(angle/2)
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
turns well past 90° on every car, which may be what was seen. Open: a
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
sheet the shader composes from. (Corrected in "Digital displays": the first transform is the
display's window into the dash scene's render target.)

**Fix** (`cockpit.display_face`, `with_display_faces`, called in `fit` for
cockpit-carbin cabins): a `digital_gauge` subsection whose second transform
maps UV0 with median anisotropy at most 2 (`FACE_MAX_ANISOTROPY`) and at
least 1 UV unit per metre (`FACE_MIN_DENSITY`, about 512 texels per metre on
the 512² interior_LOD0; cabins run 3 to 9) becomes `digital_gauge_face`
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
  The only detailed alternative is the host's vanilla interior
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
the same vehicle, the host's vanilla interior entry stays: cockpit, gauges,
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
the Express (198 GMC Savana), the F-350 Super Duty (252, its vanilla self)
and the Taurus SHO (196, its vanilla self). The other 15 stub cars keep the
Forza cabin (Durango on the Audi Q7, Yukon XL on the Escalade, A4 Avant on
the A4 2010, ...). Of the 40 test-drive cars only the Express changes.

What stays consistent: the host's hierarchy is not patched for the cockpit
(steering hinge and marker, gauge table, seats, head camera VO3D 430-432
stay vanilla, as with `--no-cockpit`); the Forza steering wheel in the full
and exterior entries is not skinned (it isn't drawn from the cockpit);
damage still moves the panel records onto the Forza panels, so the vanilla
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
stuck in the Shift view or crashed while the vanilla warm-up car 147 loaded,
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
read a table the UPVH parser fills from records of type 16 (no vanilla car has
one).

**Measured in game.** `clonetest.lua` (`run-series.ps1 -Views`, below) logs
the game camera in the car's frame at the end of each view. That frame's
origin is the ground under the model origin: the head camera logs at model y
+ 0.73 on 147, 266 and the converted Agera. With ground = highest hub y -
p360 of the host as shipped (within 1 cm on 147, 266, 205, 278 and 163; the
converted cars keep their host's), runs `vm-20261024-053821` and `-071236`:

| Mode | Position over the ground, +z forward | Set by |
| --- | --- | --- |
| Bonnet | (p535, p536 + 0.30, p537) | 140 vanilla cars set p536/p537 |
| Bumper | (p327, p328 + 0.30, z from the host + p329): 0.78 m up on every car | no vanilla car sets them |
| Normal | p300..302 (147: (0, 1.89, -5.89)) | |
| DriverEye | the head camera (above) | |

Moving p327..329 of one car by (0.3, 0.5, 0.6) moved its Bumper camera by
exactly that (run `-053821`); every Bonnet position logged matched p535..537.

**Where vanilla cars put the Bonnet camera.** The role-63 marker sits on the
windscreen, on the centre line 57% of the way up (median over the fleet;
`out/cameras/m63check.py`), and the Bonnet camera sits just in front of it:
camera - marker = (0, 0.058, 0.085) (medians; quartiles y 0.014 to 0.094, z
0.028 to 0.154; `out/cameras/bonnet_fleet.py`). It clears the car on every
vanilla car: nothing above it within 2 m or ahead within 1.5 m, 0.087 to 0.258 m
(10th to 90th percentile, median 0.12) over what is below it, mostly the
windscreen (`out/cameras/vanilla_over.py`).

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
looks 2 degrees up for every car, so it can't be set per car): vanilla fleet
median 16.3 degrees, 10th to 90th percentile 11.8 to 24.3 (149 cars). The
40 converted cars after the fix: median 17.5, 12.5 to 24.1; three are past
the vanilla 90th percentile, all with short or steeply falling noses: 500
Abarth 28.3, Express 32.0 (vans in the vanilla fleet go that far too) and
Agera 24.5. Moving the camera along the bonnet doesn't help there: on the
Agera no position 6 to 12 cm over the body within 0.4 m of the chosen one
brings it under 18.5 degrees (`bonnet_grid.py`). So the Agera looks out over
a bonnet that drops away below the frame, as vanilla cars with the steepest
noses do.

In game (runs `vm-20261024-071236` and `-072757`, `out/cameras/build3` and
`build2`): Agera, S5S Raptor, M3 E30, S65, LFA, Skyline R32, Supra, 370Z and
TT RS look out over their bonnets like vanilla 147 (the Agera's bonnet drops
away out of the frame), and the logged cameras match the converter's to the
millimetre (S5S Raptor: model (0, 0.252, 0.953), logged (0, 0.983, 0.953) with
ground 0.731). Before and after: `img/bonnet-camera-before-after.jpg`.

## The driver in the chase views (2026-10-24, confirmed-runtime)

User report: in the chase view the driver's hands are off the steering wheel
(S5S Raptor, side view). The cockpit view had them on the rim since "Driver's
hands" above.

**What moves what** (vanilla cars, runs `vm-20261024-063440` against the
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
vanilla rig's grips at the archive's hinge against the rim the chase view draws):
grips more than 3 cm off the rim on 19 cars before (S5S Raptor 15.9 cm,
Skyline 14.8, Supra 8.8, 370Z 8.1, TT RS 7.1, Silvia 6.3, IS F 6.1, Golf R 5.9,
M3 E30 5.8, 500 Abarth 5.5, SVT Raptor 5.1, 300SL 4.7, Range Rover 4.3,
Mustang Boss 429 4.2, Focus RS500 4.0, S2000 3.8, 300C 3.4, RX-7 3.3, 2000GT 3.0),
none after (at most 0.4 cm, the cockpit's own fit).

In game (run `vm-20261024-071236`): the S5S Raptor's and Skyline's hands now
hold the drawn rim like vanilla 147's
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
on the cockpit wheel (`align_main_wheel` moved it 4.7 cm), and the vanilla
grips at the hinge sit within 0.4 cm of its rim at every LOD. In game (run
`vm-20261024-125959`, fixed left view) the hands are about 10 cm ahead of
the drawn rim.

**What places the chase-view hands.** One seat marker moved per vanilla car,
fixed side cameras (`vm-20261024-131412` against the unchanged vanilla archive
`vm-20261024-132025`; earlier `vm-20261024-130737`): 147's seat 13 cm forward
puts the hands through the dashboard ahead of the wheel; 136's seat 9 cm
down puts them 9 cm lower on the wheel; 124's seat 10 cm back leaves them in
the air 10 cm behind the rim. The driver model is one animated rig on the
seat marker, hands included; they don't reach for the wheel. (The earlier
note above, that the seat moved the body with the hands still on the wheel,
was a misreading.)

**The vanilla relation** (`uv run dsff driver_survey vanilla`, every vanilla car
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
plus the hand shift) at `VANILLA_MARKER_FROM_SEAT` in the seat's frame, x
included. (Superseded by PR #90: since the marker moved onto the hub, the seat
puts that point at `seat_anchor`, the old placement, instead of at the marker;
"The steering wheel turns about its hub".) The seat tilt stays the host's. The passenger stays mirrored. The
head camera (VO3D 430..432) still comes from the Forza eye, so the cockpit
view doesn't change. The report's `hierarchy.seats` has `body_eye`, the
driver model's eyes (seat + `FLEET_EYE_ABOVE_SEAT`, the vanilla median), and
`body_eye_from_camera`.

**Survey** (`uv run dsff driver_survey cars`, offline from out/cache and the
vanilla hosts). It measures the grips of the driver model (the vanilla rig at
the vanilla point of the seat) against the drawn rim (the cockpit wheel fit):

| Cars | Grips off the rim, before | After |
| --- | --- | --- |
| 40 test-drive cars (hosts of `out/testdrive11`) | median 6.0 cm, worst 10.5 (Mustang Boss 429; Supra 9.9, M5 9.9, Skyline 9.8, LFA 9.7), 31 over 3 cm | worst 0.4 cm, none over 3 cm |
| 562 of `all.txt` (hosts of the full-roster build; 11 had no report) | median 6.2 cm, 90th percentile 12.0, worst 24.3 (330 P4), 468 over 3 cm | median 0.0, 90th percentile 0.5, 20 over 3 cm |

The 20 left over 3 cm are the cockpit's own grip error, the same in the
cockpit view: rims far from the vanilla 0.176 m, where the 6 cm cap on the
hand shift binds (five FG Falcon racers 9.1 cm, Tour bus 6.2, TT-R 5.6,
LMP and FLM racers 3.0 to 3.5).

**Head room.** The driver model's head moves with the seat, by the
difference between the Forza eye and DSF's eye relative to the wheel. Over
the 562 cars the seat moves up by a median 3.9 cm (10th to 90th percentile
-0.4 to 7.5; Mustang 5.7, F-350 17) and back by a median 3.7 cm (-4.0 to
9.9). The room from the
driver model's eyes straight up to the Forza body (LOD 1) against the vanilla
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
keep the host's vanilla cockpit. The 162 missing a speedometer or tachometer, by
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
     (`drop_virtual`): vanilla 250, the Volt's host and the Volt itself, has
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
   16-byte buffer (no normal; 145 vanilla interiors use 20). `_needle_vertex`
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
declares no gauge id. `upvh lint` now checks the same per car. (Both also
count a digital dash's rpm bar and mph digits, ids 1 and 2 of `g_fDigiData`,
as needles: in `out/build` the Spark and the Volt fail lint for that alone,
issue #234.)

**Gauge records (confirmed-static).** `FUN_0088E310` keeps, per gauge-table
index (seat byte +0x26), the index of every type-18 record whose role isn't
185 and whose table entry has a gauge id below 8, at spec+0x47D4 + index x 4.
The render loop (`FUN_008A4660`) goes over those 7 slots: for each kept
record it reads the record's table entry, takes the gauge id from it, and
writes matrix `id` of `g_InstrumentTransforms` from the entry and input `id`.
Nothing else ties a needle to a record, and the needle vertices only name the
id. Over the 150 vanilla hierarchies (`out/g62/vanilla_gauges.py`): 125 have both
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
No other entry of any vanilla host has a material whose own buffer's
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
   parent, the vanilla role for the id; the speedometer's first, as in vanilla
   hierarchies), and a copy of its table entry with the new id is appended
   to the table; `patch_hierarchy` then sets its pivot, dial and sweep like
   any other. Record count, seat count and the ancestors' subtree sizes
   follow. Report: `cockpit.hierarchy.gauges_added`. On all 20 one-gauge
   hosts the result parses and `host_cockpit` finds both slots
   (`out/g62/vanilla_add.py`). The host choice keeps its -3 for a host without
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

## Virtual dials on display faces and virtual displays (2026-10-26, issue #63)

Since "Digital displays" below, these virtual dials are the fallback: where no
dash scene can be drawn (steering-wheel displays, cars without a scene) and for a
readout a drawn scene doesn't drive.

User report on the installed full roster (`out/final4`): the Panoz Esperante
GTLM #51 (FM4 `PAN_51_Esperante_05`, ID 603, host 162) shows no gauges in the
cockpit view. It is one of the 111 cars of #63, in its first group: the only
display is a 17.5 x 7.1 cm `digital_gauge` behind the wheel whose second UV
transform maps onto a picture (a grey LCD with a bar-tachometer outline), so
PR #11 drew it textured, and `virtual_gauges` only looked at blank displays.
Forza's cockpit has no needle; its shader draws the rpm bar and digits on
that LCD at run time.

**Survey** (`out/pz/survey.py`, offline over `out/final4`: per car without both
needles, the digital patches of its Forza cockpit by section, size and kind;
`out/pz/faces_sheet.png` draws every face head-on). Of the 573 cars, 114 lack
a speedometer or tachometer: the 111 of #63 and the 3 that keep the host's
cockpit. By cause, as in #63: a display face 35, the only display on the
steering wheel 28, small or hidden blank displays 18, no such gauge 15,
placeholder cockpit 15. The faces are mostly plain LCD backgrounds: dark teal
(A4 DTM), olive (Viper Competition Coupe, Saleen S7), grey-green (599XX, R390,
Impreza), some with a printed bar outline (Panoz, FG Falcons, Vipers) or scale
(MC12, McLaren F1 GTR); a few are real artwork (the S2000's bar tachometer,
the LFA's round dial); the 207 Super 2000's lands on a decal.

**Fix** (`cockpit.py`):

1. *Faces.* When no blank display can hold the dials, `virtual_gauges` takes the
   largest display face that passes the same tests (25 cm2, facing the eye
   within 60 degrees, half in sight). `flatten_face` turns that face back into
   a blank display drawn in its median texel colour (`face_colour`,
   `sub.display_rgba`), so the static outline, scale or "0 MPH" doesn't show
   through the dial; on a face lighter than luminance 100 the marks and
   numerals are dark (`dsf_dial_mark_dark`). Digital patches standing in front
   of the face (the LFA's round dial, 4.7 mm) lift the dial with them
   (`_face_front`).
2. *Virtual displays.* When neither a blank display nor a face can hold them
   and the car has a digital readout somewhere (on the steering wheel, small
   strips), `pod_display` puts a screen in a housing on the dashboard
   (`dsf_dial_panel`: `dsf_dial_display` screen, `dsf_dial_bezel` housing whose
   sides reach back into the dash). It renders a depth and triangle-id image
   from the eye (`_screen_parts`, `_raster`, 256 px over 90 degrees) and tries
   centres every 3 px, nearest the top of the rim first, no lower than the
   wheel's centre, at least 3 degrees under the eye line, at most 35 degrees
   to either side of the wheel and 40 from the rim's top; sizes 16 x 8 cm
   down to 10 x 5 (two dials) or 10 x 7.5 down to 7 x 5.25 (one). Every pixel
   it covers must reach the cabin within 1.1 m (not glass, the bonnet or the
   A-pillars), lie outside the rim plus 4 cm at any steering angle (the
   wheel turns, the hands hold it) and clear of Forza's own needles (1.2 x
   their length), digital displays and gauge faces; the screen faces the eye
   4 mm in front of the nearest dash point behind it, with at most 15 cm of
   dash falling away behind it (a box sitting on the dash top).
   Report: `cockpit.interior.virtual_gauges.virtual_display`.
3. A car with no digital patch at all gets nothing: it has no such gauge (250
   GTO, GT40 and the rest).

Blank displays still come first, so the 48 cars with dials from PR #49 are
unchanged.

**Survey after** (`uv run dsff cockpit needles`; before `out/final4`, after
`out/vd/final`: `port_batch build --reuse out/final4 --trust-code
--rebuild-where` over the 111 cars without both needles, then the same over
the 81 of them with a digital patch after a restart): cars with both needles
turning 461 -> 534, none lost, none with a needle that never turns. Of the 111
of #63, 73 gain both gauges:

| Cause (#63) | Cars | Now with both | Left |
| --- | ---: | ---: | --- |
| display face | 35 | 35 (34 on the face; the RX-8 Mazdaspeed's 2.7 x 1.9 cm face is too small: virtual display) | |
| only display on the steering wheel | 28 | 21 (virtual display) | ARX-02a x2, R10 x2, R15 TDI, GT-R GT1 #3 and #24: open cockpits whose bodywork closes round the wheel, no dash in sight |
| small or hidden blank displays | 18 | 17 (virtual display) | the Volt: its host has no gauge record |
| no such gauge | 15 | | by design |
| placeholder cockpit | 15 | | the main carbin's cabin has no digital patch |

Three of them are on hosts with one gauge record and got the other one
(`add_gauge_records`): Spark, MP4-12C, Aygo. Four unaffected cars (Fusion, Corolla DX, DB9, M5) built again with
this code came out byte-identical to `out/final4` (the new flat cells,
`VIRTUAL_ONLY_FLAT`, go only into atlases that draw them).

Offline at 60 mph and 4000 rpm from the head camera, before and after
(`out/pz/ba.py`, same eye and look): `img/gauges-faces-virtual-offline.jpg`
(Panoz Esperante GTLM, Viper Competition Coupe, S2000, LFA) and
`img/gauges-virtual-display-offline.jpg` (Corvette C6.R FLM09, DBR9, MP4-12C,
SC430 Super GT). The needles sit on 60 and 4 on every dial.

Known rough edges: the LFA's round bezel stands over the middle of its cluster
face, so it covers the inner side of both dials; the faces of the Panoz and
other GTs sit behind the wheel's hub, which hides the lower part of the dials
(the camera rise keeps the needles' sweep in sight, `GaugeSight`); a virtual
display may cover switch-panel labels (only gauges and displays are kept clear).

## Digital displays: Forza's dash scenes on DSF's digital dials (2026-10-27, issue #63)

User feedback on PR #92 (virtual dials on faces and pods): "better than
nothing, but still very jank". Forza's digital displays should show what
Forza shows, live: speed digits, rpm bar, gear, in Forza's own artwork and
glyphs. This section does that for every display a dash scene draws, except
on the steering wheel. #92's virtual dials (the section above) remain only
where a scene can't be drawn: steering-wheel displays and cars without a scene.

### What Forza draws (confirmed-static)

**The scene.** Every car with a `digital_gauge` display has a
`DigitalGauge/Dash_<car>.bsg/.fbf/.bgf` in its zip. It is an Anark Gameface
UI scene: "FGBkranA" is "AnarkBGF" reversed. Formats are in
`formats/anark.py`.

- The scene is 1280 x 720, seen by an orthographic camera.
- Its parts are layers, groups, textured quads (per-car `.xds` files, plus
  `Media/cars/Shared.zip` `dashboards/`) and text elements. The text uses
  Forza's vector fonts (`dg1`..`dg5_vector_aa.dt` in `Media/UI/Fonts.zip`,
  mapped through `fontmap.xml`).
- Behaviours drive it through a "UIContract" whose fields bind node paths:
  - `TEXT_SPEED`, `TEXT_GEAR`, `TEXT_TACH` set text;
  - `METER_TACH`, `METER_SPEED`, `METER_FUEL`... set a "PROGRESS" slide's time
    to value / max. That slide animates masks, covers, needles and colours;
  - `COCKPIT_LIGHT` switches ON/OFF slides;
  - `SHIFT_LIGHTS` runs a progress slide, then FLASH.
- The scene carries its own tach full scale (`TACH_MAX`: S2000 9500, LFA 10500,
  Panoz 8500).
- Speed is mph or km/h depending on the scene; the label is baked into it
  (45 of 376 scenes say km/h).

**How a display shows it.** The `digital_gauge` shader samples
`digitalGaugeSampler`, the render target the scene is drawn into.
`uv_transform[0:4]` on UV0 is the display's window into that target: the
first transform, not a "label sheet" as earlier notes said. Overlays of a
render on the baked face pictures line up: S2000, 370Z, LFA, Panoz
(`out/dd-anark/r/*_overlay.png`).

`dash_scene.py` renders a scene in any state: text, progress slides with
their Bezier tracks, materials, images with UV animation. All 376 scenes on
both games' discs decode and render with no missing texture or font
(`out/dd-anark/survey2.py`). Two fonts with curves (dg1 arial, dg3 moire) keep
small specks inside closed counters.

### What DSF can draw live (confirmed-static; confirmed-runtime below)

DSF has a digital-dashboard path of its own, used by 14 vanilla interiors (Volt,
LP670, Alfa 8C, Maserati GT, MP4-12C...): materials `*_Digital_Dials.matxml`.

**The vertex shader** (`Dashboard_Functions.hlsl` line 215, programs
151/153/163/181...) adds `g_fDigiData[(id - 1) x 4 + comp]` to v. Here
(u, v, id, comp) is the vertex's DashboardInstrument element: the needles'
format.

**What the game writes** (`FUN_00891670` into the per-player gauge object at
+0x1C; a copy goes to the render snapshot; uploaded in `FUN_008A4660` as
`g_fDigiData`, 28 floats):

| id, comp | value |
| --- | --- |
| 1, 0 | rpm / redline x 0.5 (p42; the rpm is clamped to [p40, p42]) |
| 2, 0..3 | mph digits, units to thousands: (digit + 1) x 23/256, 0 for a leading blank (the speed clamped to [p606, p605]) |
| 6, 0 | gear / 7 (0 = reverse; there is no neutral) |
| 7, 0 | 0.5 when the gear isn't reverse |
| 3..5 | never written (0); dsfforza.asi `[dash]` writes km/h digits on 3 |

**What the shaders don't do** (in game, runs below):

- *No alpha test.* The vanilla digital textures are fully opaque. A
  transparent texel draws black.
- *The Volt's own vertex layout is required.* That is stride 16: s0 position
  4 x i16, s8 4 x f16, no normal. The same quads in the host's stride-20
  needle buffer drew as screen-filling triangles (run `vm2-20261027-091948`).
- *No steering-wheel display.* The digital shader uses `g_ObjectToWorld` alone,
  so its vertices can't follow the wheel's skin.

### The conversion (`convert/digital_dash.py`, `cockpit.digital_displays`)

**Inputs and placement.**

- `port_car` extracts the car's `DigitalGauge/` into
  `<cache>/<game>/<car>/digitalgauge/` (own stamp, `extract_dash`). The shared
  dashboards and fonts go to `<cache>/<game>/dash_media/`
  (`extract_dash_media`).
- `fit` draws the scene on every LOD-0 `digital_gauge` subsection (or its
  #11 face) in the cabin, but not on the steering wheel.
- `DisplayPatch` maps a render-target pixel to the display: barycentric in the
  triangle holding its UV0, extrapolated from the nearest one outside.
- The patches leave the cabin; the plan draws them.

**One texture per car,** 512 x 512 DXT5, single mip (never dropped). The
scene is decomposed into what `g_fDigiData` can drive:

1. **Artwork.** The scene at rest is rendered at 2 px per render-target
   pixel over the displays' windows:
   - speed and gear text blank;
   - readouts DSF has no value for blank too (rpm digits, laps, place, lap
     times);
   - fuel at 75%, temperatures mid.

   The display triangles draw it on id 5 (no offset).
2. **Speed digits.**
   - Each of three slots (units, tens, hundreds) gets its own column of 11
     cells, 23/256 apart: blank, then 0..9.
   - Each cell is the artwork under that slot with Forza's glyph composited
     over it. Cells are opaque, so each slot's column carries its own artwork.
   - Slots sit where the text element lays out "888" with its alignment, font
     size and tracking. A centred field therefore keeps its digits in the
     three-digit places.
   - The quads use id 2 (mph) or id 3 (km/h).
3. **Gear.** One column of 7 cells, 1/7 apart: R, 1..6 (DSF has no N). The
   quad uses id 6.
4. **The rpm.**
   - The scene is rendered at 40 rpm steps (`RPM_STEPS`: each 1/40 of the
     smaller of the Forza redline and the scene's `TACH_MAX`), not steps of
     40 rpm as this line used to read.
   - Per pixel: the first step it changes at and its colour, plus a second
     change (the LFA's ring goes white -> green -> orange).
   - Pixels that go back to their rest colour are left at rest; a needle
     sweeping past does that.
   - The changes are filtered onto cells of 1 mm or more (`PIECE_SIZE`; blurred, then
     averaged), only where the displays' triangles are. Runs of cells along a
     row with one step and one column become pieces on id 1, sharing every
     vertex with their neighbours ("Shimmer" below; before that, "Flicker and
     glare": rectangles of pixels).
     Each piece samples one texture column, from the top: the rest
     colour, from v 0.505 the first change's colour, and a later change some
     steps further.
   - A piece's v is 0.505 - 0.5 x rpm / Forza redline, so the engine's
     rpm / p42 x 0.5 reaches its colour at the same share of DSF's redline.
     The bar shows the rpm as a share of the redline: full at DSF's redline as
     at Forza's. Needles stay in absolute rpm, as before.
   - Columns are k-means colours (12) in sequences; at most 16 columns, and
     rarer sequences fold onto the most common one with the same first
     change.
5. **Needles the scene turns** (10 on 9 cars in `out/build`: 7 speedometers,
   the tachometers of the LFA and the LP700-4, and both of the Range Rover's)
   become DSF needles:
   - a flat quad over the needle's rest pose, in its colour;
   - the pivot is the fixed point of the element's pose at rest and at full
     scale;
   - the rate is fitted over the upper three quarters of the scale (the LFA's
     dial is compressed below 4000 rpm);
   - the Dials material on gauge id 1 or 2, through the gauge records as any
     Forza needle (`cockpit.dash_needles`).

**Geometry.**

- Quads are double-sided: bar pieces 0.8 mm over the display, digits 1.6 mm,
  needles 2.5 mm. A bar piece is a strip of triangles over a run of cells (its
  neighbours' ends on its top and bottom edges), not always a quad.
- They go into a stride-16 buffer with the vanilla Volt's declaration
  (`vanilla_buffer`, `rebuild_drsp(templates=)`).
- The material is a copy of the Volt's (`e012cc80f0aa8829`) on the car's
  texture, in the needle part, MODL flag 1.
- p605 goes to 255 when a scene drives the speed and Forza has no speed needle.
  Otherwise its clamp would stop the digits at a dial's last mark.

**Report.** `cockpit.interior.digital_displays`:

- `units`, `speed`, `gear` (slots placed);
- `bar_pieces`, `bar_columns`, `needles`, `artwork`, `provides`;
- `skipped` when nothing was drawn: no scene, no display off the wheel, or
  an exception.

**What's left to #92.**

- A readout the scene doesn't drive (`provides`) still gets #92's virtual dial
  or virtual display. The scene's displays count as a readout and are kept
  clear (`virtual_gauges(readout=, provided=)`).
- So do steering-wheel-only displays.

### Survey

Method: `out/dd/fit_survey.py`, `cockpit.fit` over the 573 roster cars,
against the needles of `out/build` (master). A car counts as having a gauge if
a needle turns or the scene drives that readout.

Totals:

- **Cars with both a working speedometer and tachometer:** 458 -> 533. None
  lost.
- **Displays drawn from their scene:** 300 cars.
  - speed digits on 153 (36 of them in km/h);
  - gear on 145;
  - rpm pieces on 93 (91 in `out/build` and in "Shimmer"'s survey);
  - scene needles 10.
- **Not drawn:**
  - 225 cars have no `digital_gauge` display, so no scene is needed;
  - 30 have displays on the steering wheel only.

Of #63's 111:

| #63 cause | Cars | Now with both | How |
| --- | ---: | ---: | --- |
| display face | 35 | 35 | the scene (2 with a Forza needle for the other) |
| small or hidden blank displays | 18 | 18 | the scene (11), scene + needle (4), scene + #92 virtual display (2), #92 (1) |
| only display on the steering wheel | 28 | 21 | #92's virtual display; 7 open cockpits have no room |
| no such gauge | 15 | | by design |
| placeholder cockpit | 15 | | |

The Volt counts above, but its host has no gauge record, so a needle or
virtual dial there would be dropped at the build. Its scene drives the speed
digits and needs no record: `g_fDigiData` doesn't go through the gauge table.

### In game (VM, confirmed-runtime)

**First proofs.** S2000 `vm2-20261027-095858`, LFA `vm2-20261027-101258`:

- the digits read 40 and 94 at the held speeds;
- the bar lights with the rpm;
- the LFA's needle turns over its scene.

**Before and after.**

- Settings: `run-series.ps1 -Vm -Asi -HoldMph '0,30,60,90' -ShotSeconds 2
  -ZoomPresses 0`.
- Before: `out/build`, master. After: `out/dd/test2.sp`, the six cars at IDs
  304..309.
- Runs:

| Car | Before | After |
| --- | --- | --- |
| Panoz Esperante GTLM #51 | `vm2-20261027-105000` | `vm2-20261027-163220` |
| S2000 | `vm2-20261027-165814`, `-111537` | `vm2-20261027-164830` |
| LFA | `vm2-20261027-170816` | `vm2-20261027-170616` |
| C63 AMG | `vm2-20261027-171753` | `vm2-20261027-171401` |
| FLM09 | `vm2-20261027-172233` | `vm2-20261027-172016` |
| Viper Competition Coupe | `vm2-20261027-172640` | `vm2-20261027-172444` |

Images: `img/digital-displays-ingame-1.jpg`, `-2.jpg`.

**Panoz, S2000, LFA and Viper.** Before, each display was a static face (a
dim LCD, the S2000's bar outline, the LFA's dial with its needle on 0). After:

- the speed digits read 0, 30, 60 and 94;
- the gear shows 1, 2, 3;
- the rpm bars light with the engine;
- the LFA's needle turns over its ring, which turns orange near the redline.

**C63 AMG.** Its centre display shows the km/h digits from dsfforza.asi
`[dash]` next to Forza's needles. They are too small to read at 1600 x 900;
On larger km/h displays the digits read 48 at 30 mph and 153 at 90 mph (Virage `vm2-20261027-173156`, RCZ `vm2-20261027-173346`, `out/dd/test3.sp`; `img/digital-displays-kmh-ingame.jpg`).

**FLM09.** Its only display is on the wheel, so it keeps #92's virtual
display: two dials turning on the dash, left of the wheel.

**Mistakes these runs found.** Earlier builds drew nothing or screen-sized
triangles (needle buffer layout: `vm2-20261027-091948`). Transparent texels
came out black blocks (no alpha test: `vm2-20261027-111010`).

### Flicker and glare (2026-10-28, issue #161)

User report on v0.1.2: the digital displays flicker (the Panoz's rpm bar torn in
different places from shot to shot, the Devon's shift-light marks scrambled) and look
washed out (pale cyan or green faces brighter than the cabin, the LFA's white ring
glowing).

**Causes (confirmed-static, `cockpit.fit` over the 300 cars with a plan, against
`out/master`).**

- *Pieces in one plane overlap.* Bar pieces are rectangles merged from each rpm step's
  pixel runs with 2 px of slack. On slanted or curved bars (the Panoz's arc, the
  Vipers', the LMP and GT racers') a step's rectangle reaches into the next step's
  pixels. Both sit 0.8 mm over the display, and positions are stored in 1/4096 m
  (0.24 mm) steps, so the two planes cross from vertex to vertex and the nearer one
  changes with the view: z-fighting. 63 cars, 14571 overlapping pairs.
- *Digit slots overlap.* Each speed slot's cell is the union of the ten glyph boxes;
  with tight tracking neighbouring slots overlap, again in one plane: 65 cars
  (Aston Martins, Audis, Vipers, S2000...).
- *Live quads off the display.* Pieces and cells were placed where a display's
  *window* (the bounding box of its triangles in the render target) shows them, so
  parts the triangles don't show (the corners of round dials, the space above a
  slanted top edge) were extrapolated into the display's plane over the bezel or
  the dial ring. The Devon's shift lights sit above its round tachometer face: 88 of
  its 90 pieces were mostly off the face, over Forza's `gauge_emissive` ring (0.28 mm
  in front of the display) and the cowl. 53 cars.
- *Unlit and pale.* The digital dials' pixel shader has an unlit variant (SSIC
  program 163: texel to output, clamped at 6; the lit variants 151, 153 and 181
  multiply by the sun and ambient terms). In game the display shows at its texel
  colour while the cabin round it is lit and dim. Vanilla digital textures are dark
  screens with bright glyphs (the 14 `*_Digital_Dials` textures: median luminance 0
  to 47); Forza's LCD scenes are often pale (Panoz 119, ARX-01b 157, Devon 189) and the
  LFA's ring is white (95th percentile 255). The game's tone curve flattens bright
  texels: the Panoz's face at texel luminance 157 shows at 205, scaled to 108 still
  at 181 (`vm1-20261028-124014`, `-124528`). 122 displays have a mean luminance over
  48, 70 a 95th percentile over 160.
- The ghosted TCS/ABS/STM and the empty half of the bar are Forza's own look: the
  scene's `COCKPIT_LIGHT` OFF slides draw them at 25% opacity. They stay; dimming the
  face takes their glare away with it.

**Fix (`digital_dash.build_plan`).**

1. Only what the triangles show: `coverage` rasterizes the displays' triangles over
   the view; rpm pixels off them are dropped, pieces may not grow off them, and digit
   and gear cells keep to the rows and then columns at least 90% shown (`_shown_box`,
   UVs cut to match). A slot whose centre is off the triangles (`DisplayPatch.covers`)
   is left out: the F430 GT's and SLR's gear sat wholly off their displays.
2. No overlap: a piece may grow over unlit pixels but not into another step's pixels or
   an earlier piece (`_runs_to_rects(forbid=)`); overlapping digit slots meet half way
   (`_shown_spans`); pieces 0.8 mm over the display, digits 1.6 mm, needles 2.5 mm.
   Merging exactly (no growth over unlit pixels) would have taken the Viper 2013 from
   3326 to 5104 pieces; splitting each step by colour column, to 14904.
3. Dimmed: the car's whole dial texture is scaled until the displays' mean luminance
   is at most 48 (`DISPLAY_MEAN_LUMINANCE`, the brightest vanilla background) and
   their 95th percentile at most 160 (`DISPLAY_PEAK_LUMINANCE`). Dark displays keep
   their colours. Report: `digital_displays.luminance`, `gain`.

**Survey, before (`origin/master`) and after (this code), same 300 cars:**

| | Before | After |
| --- | ---: | ---: |
| rpm pieces overlapping each other | 63 cars, 14571 pairs, 143919 px2 | 34 cars, 520 pairs, 1191 px2 |
| digit slots overlapping | 65 cars | 0 |
| live quads mostly off the display | 53 cars, 6657 quads | 0 |
| displays with mean luminance over 48 | 122 | 1 |
| rpm pieces in all | 25290 | 40948 (largest: Viper 2013, 2146 -> 3326) |

What overlaps is left is between pieces of one rpm step, which light at the same rpm:
only two neighbouring shades of the 12-colour palette can trade places, over at most
504 px2 a car (V12 Zagato). 160 cars' displays got darker; speed, rpm bars and needles
are drawn on the same cars as before, gear on all but the two above.

*Redline (offline-checked, 2026-10-29).* These rpm-piece counts were made with
the 7000 rpm fallback redline, not the cars' own (#191; "Current rules" at the
top). This code (af1c1ef) on the Viper 2013 gives the table's 3326 pieces with
the fallback and 2892 with its gamedb redline (#192's query applied). The
digit, live-quad and luminance rows don't depend on the redline. The in-game
runs below were made on 2026-10-28 after #169, so their bars are probably
scaled to 7000 rpm as well. "Shimmer" below has the survey with real redlines
(43898 pieces on 91 cars with this code).

**In game (VM, confirmed-runtime).** `run-series.ps1 -Vm -Asi -ProbeIds
'225,1,2,3,4,5,6' -HoldMph '40,100' -ShotSeconds 3 -WaitSeconds 600 -ZoomPresses 0`,
the six cars built from `origin/master` and from this code at IDs 1 to 6 (Panoz
Esperante GTLM, Devon GTX, LFA, ARX-01b #66, Viper Competition Coupe, S2000), the
vanilla MP4-12C (225) for reference. Before `vm1-20261028-124014`, after
`vm1-20261028-131725` (earlier steps: `-124528` with the first gain, `-130141`/`-130503`
with the cap of 48 before cells kept to the display). All seven drove without a crash.
The Devon's shift lights were green and black blobs spilling off the tachometer face in
every frame; now a row of LEDs on it (`img/gauges-161-devon-ingame.jpg`). The faces
are darker and less washed out (`img/gauges-161-ingame.jpg`); the LFA's ring is grey
rather than glowing. At 1600 x 900 the Panoz's bar is about 100 px long, too small to
show the z-fighting in a screenshot either way; the overlaps are measured offline.

Open: in frames taken right after the probe forces 40 mph (t2), two after-builds
showed a stray "1" in the hundreds place of the Panoz and the ARX (`139` while the HUD
read 39) and a doubled gear glyph; frames from t4 on read right on every car, and the
plan offline shows 39 with the hundreds blank. Not explained. Issue #172.

### Shimmer: bar pieces finer than a pixel (2026-10-29, issue #188)

User report on the 3ca28fd build (with #171): the S2000's digital dash has a "weird
shimmer" in the cockpit view, which a still frame doesn't show.

**Cause (confirmed-static, confirmed-runtime).** The rpm pieces of #171 were rectangles
of render-target pixels. Forza's bars often have detail finer than a screen pixel:

- The S2000's arc is radial stripes 1 to 2 render-target pixels wide (0.27 mm each) and
  3.5 apart. One render-target pixel is about 0.35 screen pixels in the cockpit view at
  1600 x 900 (the 255 mm display spans 340 pixels).
- So the arc became 1106 pieces, mostly a single row of pixels (0.27 mm tall). That is
  thinner than a pixel and close to the 0.24 mm position step.
- Each pixel shows whichever sliver its centre falls on, orange or dark orange. As the
  head camera moves at speed, that pattern changes every frame.
- The pieces also met at 1948 T-junctions, and 2 still overlapped (one of #171's 34 cars).

Ruled out:

- *Mips:* the artwork is 1.39 texels per mm, about 1 texel per screen pixel, and its
  simulated shimmer is low.
- *Z-fighting with the cabin:* cabin surfaces within 4 mm of the S2000's display cover it
  in only 3.5% of its area. That is the bezel rim and four buttons, all more than
  0.8 mm in front. Within 0.05 mm: 185 px, before or after rounding to the position step.

**Offline shimmer measure (`out/s188/shimmer.py`).**

- The plan is rasterized as the GPU draws it: pixel-centre coverage, bilinear texels,
  vertices on the 1/4096 m lattice, one camera.
- It is rendered at 0.35 screen px per render-target px, with random sub-pixel camera
  offsets, and compared with an 8x supersampled render.
- The measure is the per-pixel deviation of that error from frame to frame (luminance,
  0 to 255) inside the bar (pixels whose 3 x 3 neighbourhood is all bar). Pixels on the
  bar's outline alias on any polygon and are left out.
- S2000 at 60% of the redline: 7.13 before, with 14.9% of bar pixels over 20 (the
  survey below; the 10.1 and 23% this line used to give were measured with the 7000
  rpm fallback redline of #191).

**Fix (`digital_dash.build_plan`).**

1. *Cells.* The rpm's changes are filtered onto cells of `PIECE_SIZE` (1 mm) or more
   (`bar_cells`):
   - each state is blurred (Gaussian, sigma 0.4 cell, weighted by the display mask),
     then averaged over the cell;
   - a cell takes the median of its pixels' steps;
   - it is drawn when it is 90% on the display and changes visibly.
   - Without the blur, the 3.5-pixel stripes beat with the cells (4 pixels on the S2000)
     into a mottle of two shades.
2. *Watertight pieces.* Runs of cells along a row with one step and one column become
   one piece (`cell_runs`). Each piece is a strip of triangles whose top and bottom
   edges carry every end of the runs above and below (`run_outline`, `zipper`).
   Neighbours share every vertex, the same Forza point (no T-junction), so rounding to
   the position step can't open a crack.
3. *Segments.* Some parts of the lit mask are joined to the rest only by bridges a pixel
   or two wide (`_segment_labels`: erode by a pixel, label, grow back by two inside the
   mask). Such a part is one piece over the convex hull of its core when it is lit
   within 6 steps, nearly convex (hull at most 1.3 x its area) and clear of static
   detail and other pieces (`bar_segments`); it lights at its median step. This covers
   shift lights and LEDs and the segments of segmented bars (GranTurismo S, 599XX,
   LP700-4).
   - Cells would cut a segment into parts. On lights with display triangles of their
     own (the Zonda Cinque's, the CLK GTR's) every part was mostly off the display, and
     they vanished. On segmented bars the parts fell over the separators.
   - The hull is the core's, the pixels whose four neighbours are lit: a segment's
     antialiased rim reaches over the separator next to it.
   - Cells and edge pieces stay off every segment, so no two pieces overlap.
4. *Static detail.* A cell isn't drawn over never-lit pixels whose rest luminance is 0.2
   or more off the lit pixels near them (`static_detail`), such as the Viper 2013's
   numerals and ticks inside its lit fan.
5. *Edge pieces.* What the segments and cells leave along a display's edge becomes, per step and
   group, a rectangle kept to the display, a cell or more a side (`edge_pieces`).
   Otherwise it is left out (report `rpm_pixels_left_out`).
6. *Columns.* A colour signature that doesn't fit the 16 columns folds onto the nearest
   kept one. Before, it was dropped when no kept column had the same first change,
   which left holes in the S2000's band.
7. *Second change.* `step_v` (the second colour change, the LFA ring's green to orange)
   used `top`, which the digit loop overwrites with a cell's pixel row. It now uses the
   rpm top again, so the second change sits at its rpm on cars with digits.

**Survey, before (`origin/master`) and after (this code), the 91 cars with rpm pieces
(`out/s188/survey.py` over `cockpit.fit` inputs).**

| | Before | After |
| --- | ---: | ---: |
| rpm pieces | 43898 | 27201 |
| pieces thinner than 1 mm | 40840 (83 cars) | 589 (22 cars): segments, whose shape is that thin |
| T-junctions between pieces | 74041 (81 cars) | 9 (3 cars: edge pieces beside cells) |
| overlapping pieces | 609 pairs (34 cars) | none (0.1 px2 on the Viper 2013, rasterized at 4x) |
| simulated shimmer inside the bar, mean over cars, at 50% / 90% of the redline | 4.27 / 6.05 | 3.33 / 3.15 |
| cars over 5 at 50% / 90% | 9 / 16 | 6 / 8 |
| lit pixels drawn by no piece | 0 | 30639 of 1547261 (2.0%) |

- The S2000 at 60% of its redline: 7.13 inside the bar before, 14.9% of its pixels over 20; after, 2.35 and 0.1%.
- Speed digits are drawn on 80 cars and gear on 76, before and after.
- Left out are mostly lines under a third of a screen pixel. The Viper 2013's logo outline
  and underline account for 14609 pixels. The Agera's are 3120 pixels, the Speed 8's 2070.
- Worse inside the bar by more than 1: the TT-R at 90% (0.52 -> 3.41), the F40
  Competizione at 50% (32.1 -> 35.0) and the 599XX at 50% (35.7 -> 38.9). These are small
  high-contrast lights and segments, whose outlines alias like any polygon. The 599XX at
  90%: 48.3 -> 33.8.

**In game (VM, confirmed-runtime).** `run-series.ps1 -Vm -Asi out\asi\dsfforza.asi -ProbeIds
'232,1,2,3,4,5' -HoldMph '40,100' -ShotSeconds 3 -WaitSeconds 900 -ZoomPresses 0
-BurstShots 4 -Resolution 1600x900`. The cars are the vanilla Zonda Cinque (232), then
the S2000, Viper 2013, R8 LMS, Devon GTX and Corvette C6.R, built from master (`before3`)
and from this code.

- Runs: before `vm1-20261029-063050`; after `vm1-20261029-064126` (built before
  segments) and `vm1-20261029-094525` (the final code).
- The S2000's plan is the same in both after builds.
- Two earlier tries of the final build stalled on Story's loading screen
  (`vm2-20261029-085943`, `vm1-20261029-091436`) while the host was busy.
- `-BurstShots 4` takes four shots per sample about 0.1 s apart (new in
  `tools\wsb\guest\run-test.ps1`).
- The S2000's arc was a torn orange mosaic that changed from frame to frame. It is now a
  smooth band in every frame (`img/dash-188-s2000-ingame.jpg`). At 107 mph the mean
  frame-to-frame change over its dash, with the head camera's motion aligned away, went
  from 5.78 to 3.50.
- The Devon's and the R8 LMS's shift lights show as round lights.
- All six cars drove without a crash.
- Offline, at in-game size: `img/dash-188-offline.png`.

**Open.**

- *1-pixel dark specks.* Rare 1-pixel dark specks inside the S2000's lit band show in
  some frames, before and after (`img/dash-188-specks.jpg`). Ruled out:
  - cracks: the built mesh is watertight at its stored positions (every interior edge
    used 4 times, double-sided);
  - holes in the plan: no unlit piece sits between two lit ones in a row at any of 200
    rpm values;
  - gaps showing the artwork: an underlay of grown pieces at 0.4 mm (run
    `vm2-20261029-024513`, built before #192, so its bar ran to 7000 rpm)
    didn't remove them.
  - Issue #195.
- *Fine detail is averaged.* The Viper 2013's logo outline and its underline are lines
  under a third of a screen pixel. They come out dim or are left out: about how a
  filtered image shows them, and they would flicker if drawn sharp.

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
   the part along the column on its own at 12 cm. (Since PR #90 the shift is
   along the column only, `MAX_HAND_ALONG` 12 cm; the 6 cm in-plane cap,
   `MAX_SEAT_SHIFT`, now only places the seat, `seat_anchor`.)
3. `hand_wheel_scale` (gone since PR #118; neither wheel is scaled since PR
   #126, "Hands in the chase views"): where the grips still miss the rim by more than 5 mm,
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
skinned to the steering record) sits on the cockpit fit with the vanilla grips on
its rim (`out/handsurvey/drawn_wheel.py`, `grip_view.py`). Its host 279 has the
vanilla seat relation. In game, the fixed side views of the CL65 against vanilla 279
itself in the same session (`vm-20261026-122057`): the driver sits and holds the
wheel the same way in both, the right hand on the top right of the rim and the
left lower on it; from the passenger side the far hand shows high, at the
windscreen header, on both cars (the camera looks down from 1.9 m at a hand a
metre further away). `img/hands-cl65-vs-host279.jpg`. At rest the CL65's driver
holds the wheel as the vanilla host's does. What the user saw is not reproduced;
the open question is whether it was while steering (the hand-over-hand pose at
full lock) or another view. Issue #68, closed as not reproduced; since PR #126
dsfforza.asi moves the chase-view driver's hands onto the rim at any angle.

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
plus a shift that brings the vanilla grips onto the rim, and up to 6 cm of it
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
| vanilla fleet (146 cockpits) | median 0.03 cm, 90th pct 0.35 | 13 / 10 / 1 (School Bus) | median 0.4 deg |
| `out/final4` (573 cars) | median 1.91 cm, 90th pct 4.97, worst 11.2 (tour bus), Esperante 7.9, Corrado 6.6, ARX-01b 5.5, Regal 3.2 | 497 / 424 / 272 | median 0.2 deg, worst 1.9 |
| this fix, full roster (`out/axle/full2`) | median 0.01 cm, 90th pct 0.01 | 6 / 4 / 1 | median 0.2 deg, worst 1.9 |

The vanilla host 229 itself: 0.1 cm (cockpit and chase). The six cars left
over 0.5 cm have the hinge on the converter's own fit (in-plane shift 0); the
survey refits the culled interior mesh, and on these the two fits differ
(tour bus 5.1 cm and Kenworth T440 2.0: flat wheels whose lower half is
culled; rendered turned, their hubs stay put; Corrados 1.1, TT-R 0.8, E36 M3
0.6). The chase views are the same except for seven cars whose chase-view
wheel isn't the cockpit's (below).

**Where the grips come from: not per-vehicle data.** The hands are DSF's
animated driver rig, placed at the marker (cockpit view) or at the vanilla point
of the seat's frame (chase views), and turned with the steering matrix. The
only per-vehicle input `FUN_005743E0` reads besides the marker and VO3D
p538/p559 is the vehicle's VSCT record (`FUN_0086C5B0`, keyed by spool ID):
bytes +0x18 and +0x19 pick two of seven animation hashes at `DAT_00F08024`
(`2614F404`, `2614F404`, `3715F405`, `2225FB4E`, `3109045A`, `41104D45`,
`8AC4BCAD`; index 7 is 0) and +0x1A is a flag (driver +0xCE; set on 57 vanilla vehicles). The idle-state
code (`FUN_00574A10`, `FUN_00575150`) queues +0x50 or +0x4C when the gear
(driver +0x58, from `FUN_008AD7F0`) goes down or up: they are gear-change
animations; with the flag set, the shift animation plays only when one of
the two gears is neutral. Vanilla trucks and buses (rims
0.197 to 0.206 m) use set 0, as the Audi R8 does (0.178 m). So the grip radius
is baked into the shared driver animations, not set per car. The vanilla fleet
handles larger wheels two ways: the trucks keep the hinge on the hub (0.1 to
0.4 cm), which leaves the vanilla grips about 2.5 cm inside their 0.20 m rims
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
  about what vanilla trucks leave); then it is scaled just enough to bring them
  to 2 cm, within 0.85 to 1.3 (`WHEEL_SCALE_RANGE`). The chase-view wheel
  follows through `align_main_wheel`. (Since 2026-10-27 only the chase-view
  wheel is scaled; the cockpit wheel keeps Forza's size and dsfforza.asi moves
  the hands: "Hands on Forza's rim". Since PR #126 neither wheel is scaled:
  "Hands in the chase views".)
- The seat doesn't move: `seat_anchor` reproduces the old placement (the
  translation that put the grips on Forza's rim, scaled as before), and
  `patch_seats` puts the vanilla point of the seat's frame there instead of at the
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
ticks"). From the head camera the vanilla trucks' grips sit too low in the
frame to judge against their rims.

**Chase-view wheels not on the cockpit wheel** (issue #86, not fixed here): the
Express and F-350 keep their same-vehicle host's cockpit while the chase view
draws Forza's main-carbin wheel, and the E36 M3, M Coupe, Camaro ZL1 and
XKR-S's main-carbin wheel fits are off (`align_main_wheel`), so their chase-view
hubs sit 3 to 8 cm off the turn line.

## Hands on Forza's rim (2026-10-27, issue #99)

PR #90 left the vanilla hands gripping a fixed circle 0.177 m from the column and
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
from the column (to 0.3 cm), its normal 26.6 degrees up (the vanilla column is 24).
That holds at rest and at DSF's full lock: there the pose is the same and the
steering matrix turns it. Only the hand-over-hand frames leave the circle (12.6 cm).
`cockpit.hand_grips` (0.176 m, 45 degrees either side of the top, vanilla column)
is the same circle to within 0.3 cm and 2.4 degrees.

**Fix, cockpit view** (`dsfforza.asi [hands]`, research/asi.md "Hands on the rim"):

- **Per-car value.** The converter writes the rim into the car's VO3D
  (`cockpit.grip_params`): p95 is the drawn radius and p87 the centre from the
  steering marker along the engine's column direction. The marker sits on the
  hub's column (`hand_shift`), so the centre is on that line. p95 and p87 are
  "unused" in the type table (0x00F3E450), nothing reads them, and no vanilla VO3D
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
  character for a batched draw) is the likely path. (Confirmed and hooked since:
  "Hands in the chase views" below, issue #117.)
- Until then the chase views kept PR #90's arrangement: the seat held the vanilla
  grips at `seat_anchor`, the main-carbin wheel they draw was scaled by
  `chase_wheel_scale` (PR #90's rule), and while steering the hands turned about
  the old anchor. The cockpit and chase wheels differed in size on those cars
  (the bus 0.220 against 0.187 m, the ARX-01b 0.113 against 0.147 m).

**Survey.**

- Roster, 570 reports of `out/final4`, Forza's radius = report radius / its wheel
  scale, with the converter's rig model (`cockpit.hand_grips`):
  - PR #90 scales 153 wheels, and its grips miss the drawn rim by a median
    1.31 cm, 90th percentile 2.00, worst 3.51 (tour bus).
  - Unscaled wheels with the vanilla grips would miss by a median 1.31, 90th
    percentile 3.25, worst 6.81; the largest move the module makes is 6.8 cm.
  - With the module, the cockpit grips land on the rim: 0.0 cm, none capped.
- `uv run dsff steering_axle` now also reports the cockpit grips (`grip_vanilla`,
  `grip_asi`) and the module's rim against the drawn one (`asi_rim`).
  - On the four test cars (`out/hg/after2`): grips 5.80 cm median (6.81 bus) ->
    0.03 cm (0.34, the Regal's 0.33 cm VO3D rounding); the module's rim is
    0.08 cm from the drawn one.
  - Hubs on the turn line: 0.01 cm. The chase view's numbers are PR #90's (bus
    5.1 cm, the culled flat wheel).

**In game** (VM 2, `run-series.ps1 -Vm -VmId 2 -DriveKey Left -DriveSeconds 10
-ShotSeconds 2 -Views left -WaitSeconds 900`, IDs 1 to 4 and vanilla 229):

- Before: `vm2-20261027-093214`, master's converter on Regal GNX, tour bus,
  ARX-01b and Mini Cooper S 1965.
- After: `vm2-20261027-100256`, this branch with `[hands] debug=1`.
- The log has each hand's move: Regal 2.3 to 2.4 cm out to its 0.200 m rim, Mini
  1.3 to 2.5 cm, ARX-01b 5.9 to 6.3 cm in to its 0.113 m yoke. Vanilla 229 is
  untouched.
- In the hand-over-hand frames one hand's weight drops (0.14, 0.61).
- `img/hands-rim-cockpit.jpg`: full lock and rest, before and after.
- `img/hands-rim-chase-left.jpg`: the side view, unchanged.
- The tour bus draws no cockpit (no arms, no interior) from its DriverEye camera
  in both runs, so it can't be judged there.

## Hands in the chase views (2026-10-27, issue #117)

The driver in the chase and side views, and every AI driver, is a full-body
vehicle occupant, not the cockpit arms. Since this change `dsfforza.asi [hands]`
moves his hands onto each ported car's rim too. The chase-view wheel keeps Forza's
size, the same as the cockpit wheel. The seat doesn't move.

**How he is drawn** (confirmed-static, `out/ghidra/ch1`..`ch13` in the worktree):

- The occupants come from a pool in the character manager
  (`[[0x00FE7228]+0x2A8] + 0x8B540`, FUN_0055A250 -> FUN_0055B2D0): 0x90 items of
  0x39C bytes from +8. FUN_00567E60 sets one up: +0x00 (and +0x40) the seat
  marker's matrix in the car (FUN_00561BF0), +0x360 the vehicle, +0x364 the seat
  (0 the driver), +0x368 the animation set.
- FUN_00569120 calls FUN_00568590 for every character on a list, pedestrians
  included. Outside an enter/exit blend it calls FUN_00573450 with the character
  in ECX. +0x32B is set whenever the skeleton fits (always, in practice), so it
  takes the queued branch: FUN_0055A230 hands out a slot of 0x280 bytes (at most
  0x80 per frame; slot +0x144 the character, +0x40 his world matrix, +0x160 his
  scale), and FUN_0055ACE0 submits an engine task, FUN_005668B0 -> FUN_005650A0,
  on the async task group. FUN_0055B090 waits for the group before the draw.
- FUN_005650A0 blends his clips (FUN_00861460, twice for a blend) into a local
  pose on its own stack, then builds the model-space and skin transforms with
  FUN_00BCA840 (call at 0x005652FB). When the head looks at something it bends
  the head and rebuilds them (0x00565910). The skin transforms go to the shader
  as 3x4 rows (FUN_00BC9C70, FUN_00BA9CE0 with the constant at 0x00F40534).
- His pose frame is the seat marker's. The steering marker sits at (0, 0.469,
  0.428) in it on 138 of 147 vanilla cars ("The driver's seat follows the wheel").
  The skeleton has 60 joints, rooted at the hips (RightShoulder -> RightArm ->
  RightForeArm -> RightHand -> fingers). Some occupants use a 16-joint skeleton
  without a Shoulder joint (seen on AI drivers).
- That is why the probe at FUN_0055FAE0 (#118) never saw him: the queued branch
  doesn't go through it.

**His grips** (confirmed-runtime, `[hands] debug=1`, runs `vm1-20261027-164654`
and `vm1-20261027-165637`, the fists in the seat frame):

- The fist (wrist and finger joints) runs on a circle about the vanilla steering
  marker's place, (0, 0.469, 0.428), in the column's plane (along it: -0.4 cm).
  In the car and sports sets it is 0.193 m from the column, 1.7 cm outside a vanilla
  0.176 m rim (vanilla 229 0.194 m). The truck set (tour bus) holds at a quarter to
  three, 0.206 m out.
- Standing at full lock his hands go hand over hand, like the cockpit arms; one
  fist leaves the circle by up to 15 cm.

**Fix** (`tools/asi/src/mod_hands.cpp`):

- Hooks: the call to FUN_005650A0 in the task (0x005668D3) keeps the slot per
  thread, and the two calls to FUN_00BCA840 (0x005652FB, 0x00565910) go to a
  wrapper. It builds the transforms and then moves the hands, in both the skin
  and the model-space transforms, before anything reads them. All three sites
  or none.
- Only pool occupants in seat 0 of a car whose VO3D carries the rim (p95, p87)
  are touched. The rim goes into the seat frame through the inverse of +0x00.
- Each fist goes onto the rim (1.7 cm outside its centre line, as on vanilla
  wheels) at the angle the animation gives it about (0, 0.469, 0.428), not to
  the nearest point. So the hands turn with the wheel about its hub, also where
  the seat's anchor is off the hub. For the cockpit arms that centre is on the
  column and the move is the same as before.
- The arm follows through the same two-bone IK as the arms'. A hip-rooted arm
  turns about RightArm (the shoulder joint), so a skeleton without Shoulder works.
- Hands off the wheel are told as before: a fist more than 4 cm from the
  animations' circle (0.193 m, give or take 1.5 cm for the truck set) and from
  the rim fades out of the move, gone at 9 cm.
- The largest move is now 12 cm (`max_move_mm`, was 10). The tour bus needs 11.6
  cm at rest: its wheel lies 70 degrees back, the animations' circle 24.
- Threads: the task runs on the engine's worker threads. The skeleton cache is
  filled under a lock; everything else is on the stack.

**The converter** (`cockpit.py`, `forza2dsf.py`):

- `chase_wheel_scale`, `hand_reach`, `HAND_FIT_TOLERANCE` and
  `WHEEL_SCALE_RANGE` are gone. `align_main_wheel` puts the main-carbin wheel on
  the cockpit wheel at its size, so both views draw Forza's wheel.
- The seat stays at `seat_anchor` (PR #90's placement; no seat moves, none rises).
  Putting the seat on the steering marker instead would make the animations turn
  the hands about the hub by themselves. Over the 573 cars of the roster it would
  raise the seat on 415 cars, by a median 1.5 cm and up to 5.9 cm (JT1, Bentley
  Speed 8, Viper CC, CCX; `out/ch/seat_survey.tsv`). Keeping the hand's angle
  about the anchor in the plugin gives the same steering without that.

**Survey** (`uv run dsff steering_axle`, which now also reports the chase views'
grips: the vanilla circle about the vanilla point of the seat's frame, `turn_off` the
distance of that centre from the drawn rim's column, `asi_move` the plugin's
largest move up to full lock, 105 degrees either way):

| | Chase wheel | Hands at rest (vanilla rig model) | While steering |
| --- | --- | --- | --- |
| master (roster prediction, 573 cars) | 157 scaled, 0.85 to 1.3; differs from the cockpit wheel on 106 by over 5 mm (up to 3.6 cm) | median 0.02 cm off the scaled rim, 90th pct 1.05, worst 1.95 | turn about the seat's anchor, median 1.9 cm (worst 6.0) off the hub: at full lock off the rim by a median 3.0 cm, 90th pct 7.9, worst 9.5 |
| this branch (roster prediction) | Forza's, as in the cockpit | on the rim (0.00 cm) | on the rim at the animation's angle; largest move a median 4.3 cm, 90th pct 9.8; 27 cars need more than 12 cm at full lock (tour bus 23.8, Kenworth T440 15.7, the LMP yokes 14 to 15) |

On the four test cars (`out/ch/before`, `out/ch/after2`): chase rim radius
0.196 / 0.187 / 0.147 / 0.189 m (Regal, bus, ARX-01b, Mini) before, 0.200 /
0.220 / 0.113 / 0.188 m after, the cockpit's. Chase hubs on the turn line 0.00 cm
(the bus's 5.1 cm before was its scaled, culled flat wheel). Seats byte-equal in
both builds.

**In game** (VM 1 and 2, `run-series.ps1 -Vm -ProbeIds 1,2,3,4,229 -ShotSeconds 2
-Views left,front -WaitSeconds 900`, with `-DriveKey Left -DriveSeconds 10` for
full lock):

- Before: master's converter and master's dsfforza.asi.
- After: this branch's converter and plugin.
- Runs: before at lock `vm2-20261027-165034`, at rest `vm2-20261027-170129`;
  after at lock `vm1-20261027-170826`, at rest `vm2-20261027-171042` (both with
  `[hands] debug=1 crash_log=1`), and for the Regal, whose side views the first
  car of a run misses, `vm1-20261027-171458` (lock) and `vm2-20261027-171553`
  (rest). All finished; no fault.
- The log has each occupant hand's move: the Regal 2.7 cm at rest, the Mini 1.8
  to 3.0 cm, the ARX-01b 5.4 to 5.5 cm at rest (in to its 0.113 m yoke) and up to
  11.9 cm at full lock, the tour bus 11.4 to 11.5 cm at rest (its flat wheel; the
  old 10 cm cap left it 1.6 cm short). Vanilla 229 and the vanilla traffic drivers are
  left alone. Standing at full lock one hand goes over the top and fades out of
  the move (weight 0.5 to 0.9).
- A second Regal spawned ahead of the player (`-AheadIds 1`, another vehicle) had
  its driver's hands moved too, through the same hook: the path AI drivers take.
- `img/chase-hands-regal.jpg`, `img/chase-hands-arx.jpg`,
  `img/chase-hands-mini.jpg`: side and front views at full lock and at rest,
  before and after. The Regal's wheel was 0.98 of Forza's before and is Forza's
  now; the ARX-01b's yoke was 1.3x and is Forza's now, with the hands in on it;
  the Mini's hand at full lock floated under the rim before and holds it now.
- The tour bus can't be judged from the harness: its side and front cameras sit
  inside the 12 m body, and the chase camera sees its back.

**Open.**

- An AI driver of a ported car wasn't seen: the traffic requested in the runs
  (`-TrafficIds 1,2,3,4`) never spawned a ported car. The AI drivers the plugin
  logged (traffic spools 136 to 298, all seat 0) go through the same hook; vanilla
  cars carry no rim and are left alone.
- Where a move exceeds 12 cm (the 27 cars above at full lock) the hand stops short.
- Some occupants use a 16-joint skeleton (AI drivers seen in traffic; no Shoulder
  joint). The plugin handles it, but no ported car was seen with it.

## Risks and open questions

1. Answered: the cockpit view draws only the interior entry
   (confirmed-runtime 2026-10-29, "What DSF draws").
2. Gauges: DSF needles are linear in speed and rpm, so Forza dials with
   unevenly spaced marks are only fitted (within about 5 degrees on most
   cars, up to about 30 on very uneven faces such as the Golf R and TT RS;
   see the offline gauge check above). The sweep is a whole number of degrees per 80 mph or 5000
   rpm (under half a degree off at full scale). Only two gauges exist per
   car; fuel and temperature needles are parked. The tachometer reads at
   least idle (p40) even with the engine stopped, as on vanilla cars.
   Taking angle = Param x sweep assumes Forza moves its needle linearly
   from the min to the max pose; the mid pose isn't always half-way (FH1
   Raptor speedometer 118 of 201 degrees), so Forza may run piecewise
   through it, which would move some marks by a few degrees.
3. The interior atlas is DXT1. Vanilla interiors use DXT1 textures, but none
   for the `_Interior` material; it loses the alpha specular mask, so if
   `_Interior` masks its specular by texel alpha as the Chassis material does
   (`dsf_materials`, "gloss"; not traced for `_Interior`), every converted
   cockpit surface on it gets the full 0.03 at power 24.
4. Visibility culling assumes the camera stays near the eye. Looking back or
   a head-bob larger than 12 cm sideways (8 cm up, 6 cm down, 8 cm fore and
   aft; `CULL_OFFSETS`) may show holes in hidden areas
   (under the dash, behind the seats).
5. Paint in the interior points at the shared paint block
   (`PAINT_TEXEL_UV`), as vanilla interiors do (`lights.md`, "Paint").
6. Right-hand-drive Forza cars (the 1993 Skyline) put the wheel, marker and
   eye on the right. The vanilla fleet has no right-hand-drive car. Seen in
   game since: the FG Falcon's right-hand-drive cockpit with the hands on the
   rim (run `vm-20261026-122057`, "Hands on small race wheels"), so the head
   camera and the hand rig take a negative marker x.
7. Door cards in the interior entry don't follow the doors (vanilla door-card
   vertices are skinned to the door records; the converter leaves them on the
   chassis). The full entry's door cards go with doors that are Forza parts
   (`damage.md`).
8. The trim lift (40) and the label cut-out colour are by eye from renders,
   not from in-game lighting.
