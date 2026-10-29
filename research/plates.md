# Licence plates

Updated: 2026-10-29

How vanilla DSF cars draw their plates, what the Forza cars have, and what
`src/dsfforza/convert/plates.py` adds to converted cars (`ConvertOptions.plates`, on by
default; `port_car.py --no-plates` turns it off). Evidence tags as
elsewhere (`dngvehicles.md`). Scratch scripts: `out/plates/` (`survey.py`,
`fleet.py`, `analyse.py`, `detail.py` for vanilla; `forza_survey.py`,
`recess.py` for Forza; `plan_all.py`, `check.py`, `render.py` for the
converter).

## Vanilla plates (confirmed-static, all 150 vanilla cars)

**One shared texture.** Every vanilla plate samples the "SAN FRANCISCO /
REFLECTIONS / DRV SF" plate in the shared chassis atlas `00122b73d1fa1227`
(SSIC, 1024 x 512 DXT1, the texture of the shared paint material;
`img/plates-atlas-plate.png`). The plate is at texels 505..613 x 454..510:
UV0 u 0.4929..0.5986, v 0.8867..0.9971 (vanilla 227's quads; 266 and 147 use
the same rect within 2 texels). It is upright in the texture: v grows down the
plate. Some cars add thin edge triangles (plate thickness) on u 0.598..0.600.

**Material.** The shared paint material: `GLOBAL_Chassis` in the full entry,
`GLOBAL_Chassis_LOD1` in the exterior. 255 of 272 plates (full entry, part 0,
mask 4); 11 use `GLOBAL_LOD_Chrome`, 6 `GLOBAL_Chassis_Simple`. The paint
shader draws the atlas's own texels outside the (240, 192, 240) paint block,
so the plate needs no texture of its own (assumed from vanilla use; plates show
in game on vanilla cars).

**Orientation.** Read from outside: on rear plates u grows towards -x, on
front plates towards +x (DSF +x is the car's left), and v grows downwards
(all four cars checked, both ends, full and exterior entries). Triangles wind
counter-clockwise seen from outside (cross product along the outward normal,
like the rest of the body).

**Size and place.** A flat quad, 2 to 26 triangles per end (median 2 rear,
4 front: two triangles, or split at x = 0, or with a frame). Median
0.262 x 0.123 m (front 0.232..0.32 wide except one 0.99 m oddity, rear
0.226..0.46); about 2.1 : 1, slightly wider than the texture rect
(108 x 57 texels, 1.9 : 1), so vanilla stretches the plate a little. Centred on
x = 0 on all cars checked, vertical or following the panel.

**Presence.** 130 cars have front and rear plates, 11 rear only (Sports Car 01,
Classic 06, Super Car 02/12, some trucks, a bus, a buggy), 1 front only
(Van 06), 8 none (trucks, Van 02, Pick Up 04, Buggy 01, Classic 13). So DSF's
San Francisco cars carry front plates as a rule, supercars included
(`out/plates/fleet.txt`).

**Vertex semantics.** The plate is part of its panel:

| | Front | Rear |
| --- | --- | --- |
| s9 panel (role) | front bumper 35 on 121 of 131, misc 0 on 5, bonnet 4 on 3, front structure 37 on 2 | rear bumper 36 on 73 of 141, boot lid 5 on 34, chassis 38 on 32 |
| s9 byte 2 (lamp) | 0 | 0 |
| sky term | 0.35..0.6 typical (vanilla 227 front 0.49..0.50, rear 0.41..0.59) | same |
| s12 crush | the bumper's (227: front 0.29..0.34 m, rear 0.15..0.16 m) | same |
| s5 bytes 2, 3 | normal deltas like the bumper | |
| UV1 | the panel's tile of the scratch atlas (227 rear u 0.973..0.987) | |
| MODL | part 0 and the panel's own part (all LODs), shadow LOD (mask 1) on the chassis | |

So plates crush, swing and fall off with their bumper or boot lid (the panel
parts hold them too; detaching itself is untested, like every panel,
`damage.md`).

## Forza plates (confirmed-static)

Forza cars mostly have no plate. In `tools/batches/testdrive.txt` (40 cars):

- Plate quads (technique `numberplate`, UVs into a global plate texture not in
  the car zip): 2 of 40, `DOD_Durango_99` (front on `bumperFrameF`, rear on
  `bumperFrameR` / `bumperRa`) and `CHE_Express2500Cargo_05` (front on
  `bumperFrameF`, rear off-centre at x 0.22..0.52 on the right rear door).
- `numplate` is not a plate: it is the plate lamp, a thin strip above the
  plate recess (Cuda: 0.17 x 0.01 m horizontal; M5: 0.34 m strip in the boot
  lid handle; Firebird: two small vertical strips beside the plate). 14 of 40
  cars have one. It stays trim (`forza_materials.TECHNIQUES`).
- Nothing else: no plate locator in `MAXData.xml`, `CarAttribs.xml`,
  `ShaderSettings.xml` or `LiveryMasks/Masks.xml`, no plate texture in
  `nodamage.xds`, no `numberplate` in the 370Z's `_lod0` carbin.
- The bodies are modelled with plate recesses or mounting faces at the rear
  on almost every car, and on many at the front (US-market models).

## The converter (`src/dsfforza/convert/plates.py`, offline-checked)

`forza2dsf.convert` calls `plates.apply` before the lamps and damage plan:

1. **Host plates.** `host_plates` reads the host's full entry (part 0,
   mask 4): triangles on a shared-atlas material whose UV0 centroid is on the
   plate, grouped by end: centre, size, panel. An end gets a plate only if the
   host has one there (the host is the most similar vanilla car, so trucks and
   front-plateless hosts stay plateless). Size: the host's plate size (vanilla
   look), 0.26 x 0.122 if odd.
2. **Where.** In DSF coordinates (after `Placement`), per end:
   - a Forza `numberplate` quad at that end: its centre, its tilt, its size
     (host aspect, scaled 0.7..1.3 to fit), 2 mm in front of it; the Forza
     quads are dropped;
   - else a depth map of the body's end (LOD 1, 1 cm cells, nearest surface
     seen from behind / in front, with each cell's material class and
     section) is searched with a plate-sized window at x = 0 (offsets up to
     4 cm) for faces covered by paint/trim, plane fit `d = b y + c` under
     3 mm RMS, tilt under 0.8. A **recess** is a face whose 3-cell border
     ring is at least 4 mm nearer the viewer on 30% or more of its cells;
     the best recess within 0.3 m of the host's plate height wins (height =
     the host plate's height fraction of its body box mapped onto the Forza
     body box), else the best within 0.5 m (#159). Else a plain flat face
     within 0.15 m of that height. The
     window is then centred on the face (walk out along its row and column
     while cells stay within 4 mm of the plane) and the plate shrunk (aspect
     kept, at least 0.7x) if the face is smaller than the host plate;
   - else (**host** fallback, `fallback_height`) x = 0 within 0.3 m of the
     host's height, at the height where the plate covers the car's face at
     the lowest cost: the gap between plate and face (m), plus 0.1 x the
     distance from the host's height, plus 0.5 x the share of the plate over
     a badge, lamp, glass or exhaust section, plus 0.1 x the share over
     another section or material class than its main one (bonnet shut line,
     grille edge). Tilt clamped to 14 degrees (a plate on a pad or bracket).
     A grille may lie behind the plate (real cars mount plates over bumper
     grilles); badge-technique runs wider than 0.4 m are grille artwork
     (the BMW M1's nose band), not emblems (#159).
   Finally the plate is pushed out to the nearest surface under it plus 3 mm,
   and per LOD (1..5) further out if that LOD's surface comes forward (at
   most 8 cm).
3. **Geometry.** One synthetic body section per plate,
   `<surface section>_plate_front|rear`, with a quad (2 triangles) on every
   body LOD, technique `dsf_plate`, UV0 = the vanilla plate rect oriented as
   above, normal along the plate, AO bytes giving a sky term of about 0.48
   (vertical). The section joins `car.body` (a copy; `ForzaCar` is not
   mutated), so everything downstream treats it as Forza geometry:
   `forza2dsf.material_for` / `forza_materials.classify` give it the shared
   paint material, `add_geometry` keeps its UVs (`PLATE_TECHNIQUE` branch),
   `damage.section_rule` puts it on the panel of the section under it,
   whole (`bumperRa` -> rear bumper 36, `trunk` -> boot lid 5; on `body`:
   the front bumper 35 at the front, the majority of rear bumper / boot lid /
   chassis at the rear, as vanilla), with that panel's crush targets, normal
   deltas and decal UV1. The cockpit shell skips it (`cockpit.SHELL_SKIP`).
   The shadow LOD includes it, like vanilla.

`uv run dsff plates plan out/cache/fh1/NIS_370Z_10 --host 227 --png x.png`
prints the plan and draws both ends' depth maps with the plates;
`uv run dsff plates host <archive> --id 227` lists a vehicle's plates.
The convert report has `plates` (source, surface, size, centre, tilt, depth
per LOD, the host plate and the height prior).

### Results (offline)

All 40 testdrive cars on their testdrive hosts (`out/plates/plan_all.py`,
`img/plates-placement-testdrive.png`; orange = plate, depth-shaded body,
yellow = lamps):

| End | recess | flat face | Forza quad | host fallback | none (host has none) |
| --- | ---: | ---: | ---: | ---: | ---: |
| rear | 32 | 1 | 2 | 5 | 0 |
| front | 9 | 5 | 2 | 22 | 2 (Range Rover on 239, Huayra on 232) |

Sanity check: the FH1 370Z on the vanilla 370Z (227) gets its rear plate at
y -0.082, z -2.069, 0.28 x 0.13 in the bumper recess, against the vanilla car's
own plate at y -0.074, z -2.082; the front (fallback) lands at y -0.162,
z 2.112 against vanilla -0.160, 2.118.

Front plates are mostly fallbacks: Forza fronts are curved bumpers and
grilles, so the plate sits at the host's height on the centreline, pushed
out to the front surface. Questionable picks by eye: the Veyron's front
(on the bottom of the horseshoe grille), the Alfa MiTo's front (on the
grille shield), the SVT Raptor's front (on the bumper top, high), the
Skyline R32's rear (on the bumper's mounting pad, correct for the real car;
the earlier version took the boot face above it).

**Test build** `out/plates/build/dngvehicles.sp` (`port_batch.py build`,
default port options `--mdxn-limit 0x2C0000 --damage`, IDs 1-4):

| ID | Car | Host | Rear | Front |
| ---: | --- | --- | --- | --- |
| 1 | BMW_M3E30_91 (FH1) | 205 E-Type | recess between the tail lamps, chassis (0, 0) | fallback on the bumper centre, front structure (0, 12) (205 has no front bumper panel) |
| 2 | NIS_370Z_10 (FH1) | 227 370Z | bumper recess, rear bumper (21, 23), as vanilla | fallback, front bumper (16, 18), as vanilla |
| 3 | NIS_SkylineGTR_93 (FM4) | 229 Skyline R33 | fallback on the bumper's mounting pad, rear bumper (21, 26) | lower grille recess, front bumper |
| 4 | DOD_Durango_99 (FH1) | 138 Q7 | Forza quad, rear bumper (22, 27) | Forza quad, front bumper (16, 19) |

All four lint clean.

`out/plates/check.py` on the build (full and exterior entries): every plate
is on `GLOBAL_Chassis` (001283358afd1027), UV0 exactly the vanilla rect, reads
correctly at both ends (du/dx negative at the rear, positive at the front,
dv/dy negative), the atlas texel at the UV centre is plate orange and the
corners hit the plate frame; s9 is the planned panel; sky 0.41..0.54 (vanilla
0.35..0.6); crush targets 0.15..0.36 m on bumper plates; UV1 on the panel's
decal tile. Renders with the shared atlas (`out/plates/render.py`, mirror
fixed): `img/plates-render-build.png` against the hosts
`img/plates-render-hosts.png`: "DRV SF" reads the same way as on the vanilla
cars.

### In game (confirmed-runtime, the first car)

Run `20261022-203012` (another agent's `out/glassfix/dngvehicles.sp`, built
19:50-19:58 with this converter, so with plates): the ported BMW_M5_12 (304,
host 147) shows the vanilla "DRV SF" plate in its boot-lid recess under the
roundel, readable, damaged car (`img/plates-ingame-m5.png`). Its plan: rear
"recess on trunk", 0.357 x 0.155 m (host 147's plate size), tilt 0.27. So
the engine draws plates from body geometry only; there is no separate plate
model or UPVH plate marker (the vanilla survey found plate triangles in MODL
part 0 and the panel parts of every plated car, and converted cars before
this change showed none).

### Fallback plates on grilles, badges and bonnet edges (#159, 2026-10-28)

The host-height fallback (step 3) took the host's plate height and only asked
for a face close behind the plate. On the Mazda 2 (MAZ_2_11, host 128) that put
the front plate on the emblem slot under the bonnet (the issue's report), and
in the build the rear one on the boot lid. Three causes, three changes:

- **What lies under the plate.** The depth map now records per cell a feature
  (`feature`, `FEAT_*`): badge or emblem technique, lamp (lights-atlas
  textured or glass), grille technique, exhaust section (`exhaust*`).
  `fallback_height` scores each height within 0.3 m of the host's by the gap
  (as before), 0.1 x the distance, 0.5 x the share over a badge, lamp, glass or
  exhaust, and 0.1 x the share over another section or material class than
  its main one. Badge-technique runs wider than 0.4 m are not emblems: the
  BMW M1's whole nose band and the Trans Am's intakes are drawn with `badge`.
  A grille behind the plate is allowed (the Mazda 2's lower bumper grille,
  as on the real car).
- **trim_art's trims were no faces.** Since #173/#176, `trim_art` renames
  trim triangles on artwork to `dsf_trim_gloss~misc`, `dsf_trim_grey~...`,
  `dsf_trim_detail~...`, which classify as textured: the recess search (paint
  or trim cells only) no longer saw them, so the build fell back where
  `plates plan` (no trim_art) found a recess. The Mazda 2's rear bumper
  recess is gloss trim. The flat-colour ones (gloss, grey) are faces again,
  by their source technique; detail trims (grille bars: the Challenger SRT8's
  upper grille) stay no face and count as grille. `plates plan` now runs the
  build's cut-outs and trim artwork first (`--raw`: without).
- **Recesses further away.** Where there is neither a recess within 0.3 m nor
  a flat face within 0.15 m of the host's height, a recess within 0.5 m is
  taken before the fallback (the Audi RS2's rear bumper recess under its lamp
  band, the host's plate on its boot lid; the Camaro Z28 79's rear plate pad).

**Roster survey** (566 cars of `tools/batches/all.txt` on their hosts of the
2026-10-27 build, cut-outs and trim artwork applied as in the build;
master's converter against this one; `out/plates159/survey.py`). A plate is
flagged when over 2% of it lies on a badge (narrower than 0.4 m), lamp or
glass, when under 90% of it is on its main section, or when it floats (median
gap over 3 cm, or over 25% of it over an opening):

| | front master | front now | rear master | rear now |
| --- | ---: | ---: | ---: | ---: |
| recess | 68 | 98 | 392 | 433 |
| flat face | 76 | 85 | 35 | 34 |
| Forza plate | 14 | 14 | 16 | 16 |
| host fallback | 343 | 304 | 118 | 78 |
| fallbacks flagged | 125 | 86 | 65 | 38 |
| all plates over a badge | 59 | 20 | 28 | 6 |
| all plates over a lamp | 2 | 0 | 15 | 8 |
| all plates across sections | 36 | 17 | 35 | 21 |
| all plates floating | 69 | 77 | 33 | 41 |

258 cars change a plate. "Floating" grows: plates that leave a badge or a
shut line often land on a curved bumper or across an opening (on a bracket).
Plan renders (`img/plates-159-plans.png`, master left, now right):
Mazda 2, Audi RS2, Camaro Z28 79, Golf GTI 16v Mk2, BMW M1.

**In game** (confirmed-runtime; VM run `vm1-20261028-184410` master,
`vm1-20261028-184904` this converter; vanilla 128 then the Mazda 2, Golf GTI
16v Mk2, Audi RS2, BMW M1, Camaro Z28 79, Elan Sprint; views nose and rear;
`img/plates-159-ingame.jpg`): the Mazda 2's front plate in its lower grille
and its rear one in the bumper recess, the Golf's front plate on the bumper
under the grille instead of over it beside the GTI badge, the Camaro's rear
plate on its bumper pad instead of over the Z28 badge. The RS2's rear was not
seen (the car was wrecked in that shot); the Golf's rear plate is invisible
before and after (a recess under the bumper, tilt -0.73: #179).

**Full-roster build** (main checkout's `out/build`, 2026-10-29, 573 cars with
this converter; log lines `plates: front|rear <source> on <section>`): front
98 recess, 86 flat face, 14 Forza plate, 311 host fallback; rear 439 recess,
34 flat face, 17 Forza plate, 79 host fallback (offline-checked). Four cars
get none, as their hosts have none (215 Miura: both Miuras and the 330 P4;
252 F-350). 7 plates tilt below -0.5 (#179).

## Open

1. In game: front plates are seen since #180's run (`vm1-20261028-184904`:
   the Mazda 2's in its lower grille, the Golf GTI's on its bumper). Not
   checked: z-fighting with recesses (3 mm proud), a plate on a bumper that
   falls off.
2. Paint-material plates on hosts whose paint palette gives an odd tint:
   vanilla uses the same material, so the plate should look as on vanilla cars;
   assumed, not checked.
3. Front fallbacks float in front of curved bumpers (up to a few cm at the
   edges); real cars use brackets too. A pad/bracket search (protruding
   faces) would place some better. Still 66 front and 32 rear fallbacks
   float (#159 survey); race prototypes make up many of them (#213).
4. Plate lamps (`numplate`) are trim; they could light with the tail lamps
   (vanilla has no plate lamp entries; not done).
5. Plate size follows the host (0.25..0.37 m wide). A fixed 0.26 x 0.122
   would make all ports alike; the host's is closer to what that vanilla car
   shows.
6. Recess plates tilted under the bumper, facing the road (#179; 7 in
   `out/build`).
