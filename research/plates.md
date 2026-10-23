# Licence plates

Updated: 2026-10-22

How stock DSF cars draw their plates, what the Forza cars have, and what
`tools/plates.py` adds to converted cars (`ConvertOptions.plates`, on by
default; `port_car.py --no-plates` turns it off). Evidence tags as
elsewhere (`dngvehicles.md`). Scratch scripts: `out/plates/` (`survey.py`,
`fleet.py`, `analyse.py`, `detail.py` for stock; `forza_survey.py`,
`recess.py` for Forza; `plan_all.py`, `check.py`, `render.py` for the
converter).

## Stock plates (confirmed-static, all 150 stock cars)

**One shared texture.** Every stock plate samples the "SAN FRANCISCO /
REFLECTIONS / DRV SF" plate in the shared chassis atlas `00122b73d1fa1227`
(SSIC, 1024 x 512 DXT1, the texture of the shared paint material;
`img/plates-atlas-plate.png`). The plate is at texels 505..613 x 454..510:
UV0 u 0.4929..0.5986, v 0.8867..0.9971 (stock 227's quads; 266 and 147 use
the same rect within 2 texels). It is upright in the texture: v grows down the
plate. Some cars add thin edge triangles (plate thickness) on u 0.598..0.600.

**Material.** The shared paint material: `GLOBAL_Chassis` in the full entry,
`GLOBAL_Chassis_LOD1` in the exterior. 255 of 272 plates (full entry, part 0,
mask 4); 11 use `GLOBAL_LOD_Chrome`, 6 `GLOBAL_Chassis_Simple`. The paint
shader draws the atlas's own texels outside the (240, 192, 240) paint block,
so the plate needs no texture of its own (assumed from stock use; plates show
in game on stock cars).

**Orientation.** Read from outside: on rear plates u grows towards -x, on
front plates towards +x (DSF +x is the car's left), and v grows downwards
(all four cars checked, both ends, full and exterior entries). Triangles wind
counter-clockwise seen from outside (cross product along the outward normal,
like the rest of the body).

**Size and place.** A flat quad, 2 to 26 triangles per end (median 2 rear,
4 front: two triangles, or split at x = 0, or with a frame). Median
0.262 x 0.123 m (front 0.232..0.32 wide except one 0.99 m oddity, rear
0.226..0.46); about 2.1 : 1, slightly wider than the texture rect
(108 x 57 texels, 1.9 : 1), so stock stretches the plate a little. Centred on
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
| sky term | 0.35..0.6 typical (stock 227 front 0.49..0.50, rear 0.41..0.59) | same |
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

## The converter (`tools/plates.py`, offline-checked)

`forza2dsf.convert` calls `plates.apply` before the lamps and damage plan:

1. **Host plates.** `host_plates` reads the host's full entry (part 0,
   mask 4): triangles on a shared-atlas material whose UV0 centroid is on the
   plate, grouped by end: centre, size, panel. An end gets a plate only if the
   host has one there (the host is the most similar stock car, so trucks and
   front-plateless hosts stay plateless). Size: the host's plate size (stock
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
     body box). Else a plain flat face within 0.15 m of that height. The
     window is then centred on the face (walk out along its row and column
     while cells stay within 4 mm of the plane) and the plate shrunk (aspect
     kept, at least 0.7x) if the face is smaller than the host plate;
   - else (**host** fallback) x = 0 within 0.3 m of the host's height, at
     the height where the plate covers the car's face and lies closest to it,
     tilt clamped to 14 degrees (a plate on a pad or bracket).
   Finally the plate is pushed out to the nearest surface under it plus 3 mm,
   and per LOD (1..5) further out if that LOD's surface comes forward (at
   most 8 cm).
3. **Geometry.** One synthetic body section per plate,
   `<surface section>_plate_front|rear`, with a quad (2 triangles) on every
   body LOD, technique `dsf_plate`, UV0 = the stock plate rect oriented as
   above, normal along the plate, AO bytes giving a sky term of about 0.48
   (vertical). The section joins `car.body` (a copy; `ForzaCar` is not
   mutated), so everything downstream treats it as Forza geometry:
   `forza2dsf.material_for` / `forza_materials.classify` give it the shared
   paint material, `add_geometry` keeps its UVs (`PLATE_TECHNIQUE` branch),
   `damage.section_rule` puts it on the panel of the section under it,
   whole (`bumperRa` -> rear bumper 36, `trunk` -> boot lid 5; on `body`:
   the front bumper 35 at the front, the majority of rear bumper / boot lid /
   chassis at the rear, as stock), with that panel's crush targets, normal
   deltas and decal UV1. The cockpit shell skips it (`cockpit.SHELL_SKIP`).
   The shadow LOD includes it, like stock.

`python tools/plates.py plan out/cache/fh1/NIS_370Z_10 --host 227 --png x.png`
prints the plan and draws both ends' depth maps with the plates;
`python tools/plates.py host <archive> --id 227` lists a vehicle's plates.
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

Sanity check: the FH1 370Z on the stock 370Z (227) gets its rear plate at
y -0.082, z -2.069, 0.28 x 0.13 in the bumper recess, against the stock car's
own plate at y -0.074, z -2.082; the front (fallback) lands at y -0.162,
z 2.112 against stock -0.160, 2.118.

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
| 2 | NIS_370Z_10 (FH1) | 227 370Z | bumper recess, rear bumper (21, 23), as stock | fallback, front bumper (16, 18), as stock |
| 3 | NIS_SkylineGTR_93 (FM4) | 229 Skyline R33 | fallback on the bumper's mounting pad, rear bumper (21, 26) | lower grille recess, front bumper |
| 4 | DOD_Durango_99 (FH1) | 138 Q7 | Forza quad, rear bumper (22, 27) | Forza quad, front bumper (16, 19) |

All four lint clean.

`out/plates/check.py` on the build (full and exterior entries): every plate
is on `GLOBAL_Chassis` (001283358afd1027), UV0 exactly the stock rect, reads
correctly at both ends (du/dx negative at the rear, positive at the front,
dv/dy negative), the atlas texel at the UV centre is plate orange and the
corners hit the plate frame; s9 is the planned panel; sky 0.41..0.54 (stock
0.35..0.6); crush targets 0.15..0.36 m on bumper plates; UV1 on the panel's
decal tile. Renders with the shared atlas (`out/plates/render.py`, mirror
fixed): `img/plates-render-build.png` against the hosts
`img/plates-render-hosts.png`: "DRV SF" reads the same way as on the stock
cars.

### In game (confirmed-runtime, one car)

Run `20261022-203012` (another agent's `out/glassfix/dngvehicles.sp`, built
19:50-19:58 with this converter, so with plates): the ported BMW_M5_12 (304,
host 147) shows the stock "DRV SF" plate in its boot-lid recess under the
roundel, readable, damaged car (`img/plates-ingame-m5.png`). Its plan: rear
"recess on trunk", 0.357 x 0.155 m (host 147's plate size), tilt 0.27. So
the engine draws plates from body geometry only; there is no separate plate
model or UPVH plate marker (the stock survey found plate triangles in MODL
part 0 and the panel parts of every plated car, and converted cars before
this change showed none).

## Open

1. In game: front plates (not yet seen), the other cars of the test build,
   z-fighting with recesses (3 mm proud), a plate on a bumper that falls off.
2. Paint-material plates on hosts whose paint palette gives an odd tint:
   stock uses the same material, so the plate should look as on stock cars;
   assumed, not checked.
3. Front fallbacks float in front of curved bumpers (up to a few cm at the
   edges); real cars use brackets too. A pad/bracket search (protruding
   faces) would place some better.
4. Plate lamps (`numplate`) are trim; they could light with the tail lamps
   (stock has no plate lamp entries; not done).
5. Plate size follows the host (0.25..0.37 m wide). A fixed 0.26 x 0.122
   would make all ports alike; the host's is closer to what that stock car
   shows.
