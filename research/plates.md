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
x = 0 on all cars checked, vertical or following the panel. Tilt (dd/dy seen from
the end, + = faces up; area-weighted normal, `host_plates`): front -0.70..+0.28
(median -0.02; below -0.36 only the Stratos, 217), rear -0.36..+0.63 (median
+0.08; the '68 Camaro 146 lowest, the Beetle 257 highest). So no vanilla plate
faces the road by more than 20 degrees but the Stratos's front
(`dsff plates host <archive> --id N` prints it last; #179).

**Presence.** 130 cars have front and rear plates, 11 rear only (Sports Car 01,
Classic 06, Super Car 02/12, some trucks, a bus, a buggy), 1 front only
(Van 06), 8 none (trucks, Van 02, Pick Up 04, Buggy 01, Classic 13). So DSF's
San Francisco cars carry front plates as a rule, supercars included
(`out/plates/fleet.txt`).

**Bent plates.** Plates split at x = 0 are often bent there round a curved
bumper: of 120 front plates with both halves, 25 turn by over 2 degrees
between the halves (normals of the two halves' plate-facing triangles), 4 by
over 5: the '68 Camaro (158) 24 degrees, the Eldorado (149) 14, the DB9 (133)
10, the Challenger (173) 6. Rears: 20 over 2 degrees, 1 over 5 (the V12
Vantage, 5). Converted fallback plates bend the same way (#213).

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
     3 mm RMS, tilt from -0.4 (facing the road by 22 degrees, or the host
     plate's own tilt where it faces down further) to +0.8 (#179). A **recess** is a face whose 3-cell border
     ring is at least 4 mm nearer the viewer on 30% or more of its cells;
     the best recess within 0.3 m of the host's plate height wins (height =
     the host plate's height fraction of its body box mapped onto the Forza
     body box), else the best within 0.5 m (#159). Else a plain flat face
     within 0.15 m of that height. The
     window is then centred on the face (walk out along its row and column
     while cells stay within 4 mm of the plane) and the plate shrunk (aspect
     kept, at least 0.7x) if the face is smaller than the host plate;
   - else (**host** fallback, `fallback_height`) x = 0 within 0.3 m of the
     host's height, at the height and size where the plate covers the car's
     face at the lowest cost: 2 x the mean gap between plate and face once it
     rests on the face (m, each cell's gap counted up to 10 cm), plus 0.1 x
     the distance from the host's height, plus 1.0 x the share of the plate
     over a badge, lamp, glass or exhaust section, plus 0.02 if it covers more
     than 0.5% of an emblem (a badge narrower than 0.4 m), plus 0.1 x the
     share over another section or material class than its main one (bonnet
     shut line, grille edge), plus 0.05 x the shrink: the plate may shrink to
     0.9 or 0.8 of the host's (not under 0.23 m wide, vanilla's narrowest) to
     fit between an emblem and an edge (#159, #213, #241). Cells within 2 cm of a lamp
     count as hidden; a plate whose best height still covers over 10% of a lamp is
     dropped (#251, #259). The plate **rests**
     on the face (`rest`, #213): tilted (up to 14 degrees, a plate on a pad or
     bracket) and bent back at its centre line (each half up to 11 degrees, as
     vanilla bends plates on curved bumpers) so that it touches the nearest
     cell and stands off the rest least on average.
     A grille may lie behind the plate (real cars mount plates over bumper
     grilles); badge-technique runs wider than 0.4 m are grille artwork
     (the BMW M1's nose band), not emblems (#159).
   Finally the plate is pushed out to the nearest surface under it plus 3 mm,
   and per LOD (1..5) further out if that LOD's surface comes forward (at
   most 8 cm).
3. **Geometry.** One synthetic body section per plate,
   `<surface section>_plate_front|rear`, with a quad (2 triangles; a bent
   plate two quads, the UV split at its centre line) on every body LOD,
   technique `dsf_plate`, UV0 = the vanilla plate rect oriented as
   above, normal along the plate (each half's on a bent plate), AO bytes
   giving a sky term of about 0.48 (vertical). The section joins `car.body` (a copy; `ForzaCar` is not
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

### Plates under a bumper's overhang, facing the road (#179, 2026-10-29)

The recess search took any flat window with |tilt| up to 0.8. Under a bumper that
stands out over the panel below it, that panel slopes back and down, and the bumper's
lower edge, nearer the viewer, counts as the recess's upper wall: a "recess" facing
the road. The Golf GTI 16v Mk2's rear plate went there (tilt -0.73, under its black
bumper) and was invisible in game (VM runs `vm1-20261028-184410`,
`vm1-20261028-184904`). The search now takes windows from -0.4 to +0.8, or down to
the host plate's own tilt where that is lower (the Stratos's front, -0.70).

Offline (each car on its host of the full build, cut-outs and trim artwork as in
the build, master's converter against this one): only plates whose window faced down by
more than 0.4 can move; the main checkout's full build has 13 (log lines
`plates: ... tilt`). Each, master -> now:

| Car | End | master | now |
| --- | --- | --- | --- |
| AUD_S4_00 | rear | recess on bumperRa, y +0.10, tilt -0.62 | recess on trunk, y +0.45, +0.29 |
| CHE_ElCaminoSS_70 | rear | recess on bumperRa, y -0.26, -0.57 | fallback on bumperRa, y -0.27, -0.25 |
| FOR_GT_05 | front | recess on bumperFa, y -0.04, -0.45 | fallback, same height, -0.25 |
| LAM_MurcielagoLP640_07 | front | recess on bumperFa, y -0.30, -0.42 | flat on bumperFa, y -0.33, -0.38 |
| LAN_DeltaS4_86 | front | recess on bumperFa, y -0.01, -0.70 | fallback on body, y +0.25, -0.04 |
| LOT_Esprit_02 | front | recess on bumperFa, y -0.02, -0.54 | flat on bumperFa, y +0.10, -0.13 |
| MCL_MP412C_11 | rear | recess on bumperRa, y +0.07, -0.47 | recess on bumperRa, y +0.25, +0.34 |
| PLY_Barracuda_68 | rear | recess on body, y -0.41, -0.58 | fallback on body, same height, -0.25 |
| TOY_Altezza_04 | front | recess on bumperFa, y -0.37, -0.41 | fallback on bumperFa, y -0.23, +0.25 |
| VW_Corrado_95 | rear | recess on bumperRa, y +0.09, -0.45 | flat on bumperRa, y +0.19, -0.17 |
| VW_GolfGTi16vMk2_92 | rear | recess on bumperRa, y +0.11, -0.73 | recess on trunk, y +0.56, +0.35 |
| fer_f142_10 | front | flat on bumperFa, y -0.40, -0.74 | fallback on bumperFa, y -0.31, +0.25 |
| sal_s281_06 | front | recess on bumperFa, y -0.05, -0.42 | fallback on bumperFa, y +0.10, -0.19 |

The Golf's rear plate goes to its tailgate recess between the lamps (room 0.54 x
0.16 m, a European plate's), the Audi S4's to its boot-lid recess. Plan renders:
`img/plates-179-plans.png`. Three plates (El Camino, Ford GT, Barracuda) stay
where they were as host fallbacks, tilt clamped to -0.25; the fallback clamp is
symmetric (`BRACKET_TILT`), so none of them faces down by more than 14 degrees.

**In game** (confirmed-runtime; VM runs `vm1-20261029-130353` master and
`vm1-20261029-132153` this converter, a 9-car `--list` build, vanilla 128 first,
views nose and rear; `img/plates-179-ingame.jpg`): the Golf's rear plate,
barely visible under its bumper before, reads in the tailgate recess; the Audi S4's
moves from under its bumper to its boot-lid recess; the Corrado's up onto its
bumper face; the Esprit's front from under its nose to the bumper face. The Delta
S4's front gets worse: the fallback puts it on the grille band, its top edge over
the Lancia shield's lower edge (1.7% of the plate, under the hide cost that would
move it). Its bumper face is 8 cm tall, the host's (274) plate 0.315 x 0.142 m
(#241).

### Fallback plates rest on the face, bent and tilted (#213, #241, 2026-10-29)

The fallback took the host's height and size, pushed a flat plate out to the nearest
surface under it plus 3 mm, tilted by a plain fit clamped to 14 degrees. On a bumper
curving back towards its sides the corners stood off, and the height search scored the
median gap, which never saw them. Changes (`plates.rest`, `fallback_height`):

- **Rest.** The plate is the surface d + tilt (y - cy) + bend |s|: tilt within 14
  degrees, each half turned back by up to 11 degrees at the centre line (vanilla bends
  plates so, "Bent plates" above). It touches the nearest cell and stands off the
  others least on average (each gap counted up to 10 cm). A bent plate is two quads
  with the UV split at the centre line and each half's normal.
- **The mean gap counts twice** in the height search, and the hidden share with it
  (1.0 for each share over a badge, lamp, glass or exhaust): a plate standing off by a
  few centimetres shows more than one across a section edge.
- **No emblem slivers** (#241): covering more than 0.5% of an emblem adds 0.02. The
  Delta S4's front plate took its grille band over the Lancia shield's lower edge
  (1.7%, for a cost of 0.008).
- **Smaller plates where they fit better**: 0.9 or 0.8 of the host's (not under
  0.23 m wide), at 0.05 per unit of shrink. The Delta S4's plate is 0.8 (0.25 m)
  between the shield and the bumper.

**Roster** (`dsff plates_survey`: every car of the main checkout's full build with a
host-fallback plate on master, and #179's 13; 348 cars, each on its build host, cut-outs
and trim artwork as in the build; master, the rest step alone, and the final weights).
A plate floats when its median gap is over 3 cm or over 25% of it is over an opening (a
cell over 10 cm behind it, or no surface); corner off: a 3 x 3-cell corner block's
median gap over 3 cm:

| | front master | rest only | final | rear master | rest only | final |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| host fallbacks | 316 | 316 | 316 | 81 | 81 | 81 |
| floating | 69 | 59 | 36 | 33 | 28 | 23 |
| corner off > 3 cm | 173 | 151 | 111 | 58 | 50 | 40 |
| over a badge | 16 | 9 | 1 | 5 | 3 | 3 |
| over a lamp | 0 | 0 | 2 | 8 | 9 | 7 |
| across sections | 84 | 73 | 68 | 44 | 43 | 41 |

Front fallbacks: median gap (median over plates) 14.0 -> 6.7 mm, worst corner median
34 -> 17 mm (p75 72 -> 41 mm); rear 17.9 -> 11.1 mm, 50 -> 21 mm. 389 plates move on
337 cars; recess, flat-face and Forza plates don't. 246 of the 316 front fallbacks bend
(median 0.06, 19 at the limit), 21 of 81 rear; 159 front and 49 rear shrink (40 and 5
to 0.8). Plan renders (`img/plates-213-plans.png`, master left; Delta S4, Viper GTS-R,
Corvette Grand Sport, Fiat 595, 2-Eleven, Mazda 2).

**In game** (confirmed-runtime; VM runs `vm1-20261029-154953` master and
`vm1-20261029-155537` this converter, a 10-car `--list` build, vanilla 128 first,
views nose and rear; `img/plates-213-ingame.jpg`): the Delta S4's plate below the
Lancia shield; the Viper GTS-R's from 6 cm under its nose to between its lamp covers;
the Corvette Grand Sport's onto its plate pad; the Fiat 595's onto its bumper bar; the
2-Eleven's front under its badge; the Chevelle's rear from the boot lid onto its
bumper. `render_compare diff` (`out/rc/213`): all 10 cars draw differently, plates first;
the other regions that change are the ones a moved plate covers or uncovers (the Delta
S4's shield +62 px, the 2-Eleven's front badge 0 -> 165 px).

### Fallback plates clear of lamps (#251, #259, 2026-10-29)

The lamp mask was complete (every technique `lights.py` lights, lamp covers included,
is `FEAT_LAMP`), but `fallback_height` ranks a height first by how much of the plate
lies on the face, then by cost. A lamp is face, so on race cars whose only face near the
host's plate height is the rain light's panel, the plate went onto the lamp: the ARX-01b's
rear (32% of the plate over its two rain-light strips, 12 cm apart on a crossbar 4 cm tall
above them), the Mazda B09's (15%, two round reflectors under an 8 cm bar). On the Viper
GTS-R the plate sat between its two lamp covers (0.23 m apart for a 0.236 m plate),
touching both. Changes (`fallback_height`):

- **A margin round lamps**: cells within `LAMP_MARGIN` (2 cm, about a plate frame) of a
  lamp count as hidden (1.0 per share, like emblems). Windows keep their own cells only
  (a margin under the F430 GT's rear window moved its plate off the boot lid).
- **No plate on a lamp panel**: a best height still over a lamp by more than
  `LAMP_DROP` (10%) gets no plate at that end, as the Forza car has none (`plan` reports
  the end as `none`). The B09's lamp-clear alternatives were its rear wing (`winga`, 0.3 m
  above) and a 7.7 cm gap under the reflectors.
- **No lettering instead of a lamp**: where the margin would move the plate onto more
  than `EMBLEM_AREA` (15%) of an emblem's area (strokes grown by a cell) than the plate
  the search picks with the lamps' own cells alone, that plate stays: kept clear of its
  rain light's edge, the Evora Type 124's rear went up over its EVORA letters (24% of
  their area, 5% of strokes); it stays on its bumper face under the lamp (5% of the
  plate on the lamp's lower edge), as on master. Dropping it instead lost a plate that
  reads fine; dropping on emblem area alone also took the Zonda Cinque's, which sits
  next to a badge decal where the vanilla Zonda Cinque (232) has its own plate.

**Roster** (`dsff plates_survey --ref master=origin/master`, the 339 cars of the main
checkout's full build with a host-fallback plate, each on its build host; only fallbacks
can change): 12 plates move on 12 cars.

| | front master | now | rear master | now |
| --- | ---: | ---: | ---: | ---: |
| host fallbacks | 311 | 311 | 81 | 76 |
| none (lamp panel) | 0 | 0 | 0 | 5 |
| over a lamp | 2 | 0 | 7 | 1 |
| over a badge | 1 | 1 | 3 | 3 |
| floating | 36 | 38 | 22 | 23 |
| corner off > 3 cm | 110 | 110 | 39 | 39 |
| across sections | 66 | 68 | 41 | 36 |

- No rear plate: the ARX-01b's three liveries (32% of the plate on the rain light) and
  the B09's two (15%).
- The Viper GTS-R pair's front plate goes from between the lamp covers (y -0.13) to the
  lower nose (y -0.29), where it was before #213 (-0.28). It floats there by the survey's
  measure (median gap 50 mm: the nose curves back and down to the grille), as before #213,
  and reads better in game (#259). That is the front's floating 36 -> 38.
- The Zonda C12's rear: its two centre lamps are 8 cm apart, the plate 10 cm tall, so it
  goes under the lower one onto the keel (gap 41 mm, was 7 mm over the lamp's top): the
  rear's floating 22 -> 23.
- 1 to 2 cm lower, the lamp's margin: the R390 GT1, Zonda R, S7, C8 Laviolette (rear).
- The one plate over a lamp left: the Evora Type 124's rear (above).
- Unchanged on the same 311 front and 76 rear plates: median gap 6.7 and 11.1 -> 11.2 mm,
  worst corner median 17 and 28 mm (p75 41, 117).

`render_compare diff` (`out/rc/251`, 15 cars built with master's and an earlier version
of this `plates.py` that dropped the Evora's plate): 13 draw differently, the 12 above and
the Evora, plates first on each; the other regions are what a plate uncovers (the ARX-01b's
rain light 1436 -> 2372 px). Its flag "plates: over rear_lamps" is fixed on the ARX-01b and B09, no
new flag. Renders: `img/plates-251-renders.jpg` (master left).

**In game** (confirmed-runtime; VM runs `vm1-20261029-194717` master and
`vm1-20261029-195320` this converter, the same 15-car build, IDs 1 to 9, views nose and
rear; `img/plates-251-ingame.jpg`): the ARX-01b's rain light shows, the B09 has no rear
plate, both Vipers' plates sit on the lower nose under the lamp covers, the Zonda C12's
under its centre lamp. Neither run crashed.

## Open

1. In game: front plates are seen since #180's run (`vm1-20261028-184904`:
   the Mazda 2's in its lower grille, the Golf GTI's on its bumper). Not
   checked: z-fighting with recesses (3 mm proud), a plate on a bumper that
   falls off.
2. Paint-material plates on hosts whose paint palette gives an odd tint:
   vanilla uses the same material, so the plate should look as on vanilla cars;
   assumed, not checked.
3. Fallbacks still float on 35 front and 21 rear plates (#251's survey, #277), most
   on race prototypes with an open nose or diffuser at the centre line (Acura
   ARX, Audi R10/R15, Lola); the 905 Evo's rear plate is on its wing, the Furai's on
   its exhaust section. Some trade a badge for a gap: the 2-Eleven's rear
   (4.1 cm, instead of covering 15% of a badge), the Pantera's front (3.4 cm).
   A pad/bracket search (protruding faces) might place some better.
4. Plate lamps (`numplate`) are trim; they could light with the tail lamps
   (vanilla has no plate lamp entries; not done).
5. Plate size follows the host (0.25..0.37 m wide). A fixed 0.26 x 0.122
   would make all ports alike; the host's is closer to what that vanilla car
   shows.
6. Recess plates tilted under the bumper, facing the road: fixed (#179).
