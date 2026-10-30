# Damage: panels, crush morph, hinges

Updated: 2026-10-30

How a vanilla DSF car's body damages, from static analysis of the vanilla
archive and the shared vehicle shaders, and what `src/dsfforza/convert/damage/` builds
for converted Forza cars. Evidence levels follow `dngvehicles.md`
(confirmed-static, confirmed-runtime, hypothesis). Hosts checked: Saloon 15
(ASYM Desanne, 266), Sports Car 06 (vanilla 370Z, 227), Saloon 03 (147),
DriftCar 03 (229), Old 01 (130), Tanner (62), plus fleet surveys of all 150.

Scratch scripts: `out/damage/` (`fleet.py`, `roles.py`, `s5delta.py`,
`gpdis.py`, `overlap.py`). Tool: `src/dsfforza/convert/damage/` (`panels`, `render`,
`check`).

## The vertex shader (confirmed-static)

The archive's shared chunk (SSIC, root entry 450) holds the vehicle GPU
programs: 87 `GPCH` containers, each a D3DX `fx_2_0` effect (`GPUS`) with
`vs_3_0`/`ps_3_0` shaders compiled with debug info (source lines and
variable names). `out/damage/gpdis.py` cuts the shaders out and disassembles
them with `D3DDisassemble` (d3dcompiler_47). Program 2 is the depth pass,
program 1 a colour pass; both run the same code from
`R:\Resources\Shaders\Uber2_0\Vehicles\Vehicles_Functions.hlsl`:

```text
inputs  v0 position (int16 x4)      v1 texcoord1 = semantic 9 bytes 0, 1
        v2 texcoord4 = semantic 12 ("vec3DamagePos", half4)   v3 normal = semantic 5
consts  g_PanelTransforms[96]   (c0, 32 panels x 3 rows)
        g_v4PerPanelData[64]    (c96)

data          = g_v4PerPanelData[s9.byte1]
iScratch      = trunc(data.w * 0.1);  fDamageOffset = data.w - 10 * iScratch - 0.5
fDamage       = data.x - fDamageOffset * position.x          // UnpackData_VS::typeSpecificData
position'     = position + fDamage * (s12 - position)       // lerp to the crushed shape
angles        = s5 / 255 * 2pi - pi                          // all four bytes
(polar, az)   = angles.xy + fDamage * angles.zw              // damaged normal
normal        = (sin polar sin az, cos polar, sin polar cos az)
world         = g_PanelTransforms[3 * s9.byte0 .. +2] * position'   // per-panel 3x4 matrix
```

So, per vertex:

| Data | Meaning |
| --- | --- |
| s9 byte 0 | panel transform index = UPVH record +0x34 ("slot"; records without one use 0, the chassis). Swinging (bonnet, boot, bumpers, doors, wings) and detached panels move through it. |
| s9 byte 1 | panel data index = record +0x2A ("model"). Selects the panel's damage amount. |
| s9 byte 2 | lamp: light id + 1 (`src/dsfforza/convert/lights.py`, research by the lights work). |
| s9 byte 3 | 1 on body vertices (wheel buffer: 0, 1, 2). |
| s12 | the vertex's fully crushed position (car space, w = 1). |
| s5 bytes 2, 3 | change of the normal's polar and azimuth angles at full damage; 127/128 = none. |

The damage amount is per panel and varies linearly across x
(`data.x - offset * x`), so one corner of a bumper can crush more than the
other. `fDamage * g_hDecalDamageScale` also goes to the pixel shader
(damage decal), and `data.w` packs a scratch amount next to the offset.

### Scratch and dent decal: UV1 (confirmed-static)

The colour pass's pixel shader (program 1) samples `g_samDecalNormalMap`
and `g_samDirtinessDecalMap` at `v0.zw`, the vertex shader's copy of
semantic 8 components 2-3 (UV1) of the 32-byte buffers (paint and lamp
materials). Both textures are shared, in SSIC:
`00122b6db3fa1227` (1024² DXT5: dent normals in RGB, scratch mask in alpha;
`out/damage/tex-00122b6db3fa1227-*.png`) and `001251560b05e528` (1024²
DXT5 dirt). The decal's blue and alpha give `fDecalTintMask`, which fades
the paint tint to bare metal as damage rises; dent normals feed the
lighting.

Vanilla UV1 is an unwrap of each panel onto its own tile of that one atlas,
the same layout for every car (`out/damage/uv1.py`; Desanne, CTS-V,
Mustang 193, in the full, exterior and interior entries): front bumper top
left, rear bumper top right, boot in the middle, bonnet on the right, the
sides and wings in the three rows below; lamps (Lights material) on the
small tile of three dark blobs (broken lamp), hidden faces on one corner
texel, and vanilla interiors' GLOBAL_LOD_Chrome tiles UV1 over -4..5.

Converted cars carried Forza's UV1 on paint and the lamp-atlas UV on
lamps, so their panels sampled the wrong tiles: damaged cars showed bright
white shapes and speckle on the paint (run `20261022-040545`:
`img/r7-m5-damaged-decals.png`, `img/r7-skyline-damaged.png`, the bonnet
from the cockpit in `img/r7-mustang-cockpit-damaged.png`; confirmed-runtime
symptom, cause confirmed-static). The fix is in `damage.py` below (step 3b).

Checked against the data: on the Desanne the normal rebuilt from s5 bytes 0-3
at fDamage = 1 is within 2.0° (median) of the face normal of the s12 mesh,
against 10.7° for the undamaged normal (`out/damage/s5delta.py`, 4,750
moved vertices).

`vehicle-model.md` used to say s12 is "the position again"; it is only on
vertices that don't crush (glass, cabin, the middle of the body: about a
third of all vertices).

## Crush targets on vanilla cars (confirmed-static)

Every vanilla body vertex carries a crushed position. Fleet survey
(`out/damage/fleet.py`, MODL part 0, full mask 4 and exterior mask 2):

| | Full entry | Exterior |
| --- | --- | --- |
| front 10% of the length: median dz / length | -0.060 (range -0.13 to 0) | -0.061 |
| rear 10%: median dz / length | +0.022 (0 to 0.069) | +0.023 |
| middle 20%: median displacement | 1 mm | 0 mm |
| vertices with s12 = position (median per car) | 36% | 38% |
| s9 pairs that are no record's (slot, model) | at most 2.8% (DriftCar 05), 0 on most | at most 1.7% |

137 of 150 full entries push the nose back by more than 5 cm; the three
without crush targets are the trailers (123, 131, 289). The shape is artist-made:
on the Desanne the nose goes back 0.42 m (median, front 0.4 m of the car,
up to 0.62 m), the bonnet buckles up by up to 0.27 m in its middle, the
wings fold back, the tail moves forward 0.09 m (up to 0.39 m on the boot
lid), the doors dent by a few centimetres, the suspension parts rise 0.1 m
(0.2 m in the exterior) and glass doesn't move.
Render: `img/damage-vanilla266-crushed.png` (every panel at fDamage = 1,
coloured by panel), `img/damage-vanilla227-crushed.png` (vanilla 370Z).

## Panels: MODL parts and UPVH records (confirmed-static)

Part records (type 1) carry the panel identity. Roles (+0x0A) across the
fleet, by where their MODL part sits in the car (`out/damage/roles.py`;
x/y/z normalised by the part-0 box, +z front, +x the car's left):

| Role | Records | Panel | Hinge |
| --- | ---: | --- | --- |
| 38 | 150 | chassis shell, s9 (0, 0) | none |
| 37 | 147 | front structure (wings' inner faces, front panel), fixed (slot 0), own damage index | none |
| 8 | 401 | glass panels (windscreen, rear window) | none |
| 1 | 147 | steering wheel | column |
| 185 | 147 | gauge cluster (no geometry; parent of the gauge records) | x |
| 31 / 32 | 144 / 147 | front doors +x / -x | vertical, limit 0 |
| 54 / 55 | 46 / 48 | rear doors | vertical, limit 0 |
| 33 / 34 | 110 / 110 | front wings +x / -x | vertical, limit 0 |
| 4 | 140 | bonnet | x axis at its rear edge, limit 0.5655, param 24 |
| 5 | 134 | boot lid | x axis at its front edge, limit 0.5655, param 15 |
| 124 | 49 | hatch / spoiler (y 0.45, z -0.87) | mostly none |
| 35 / 36 | 139 / 126 | front / rear bumper | z axis through the bumper's centre, limit 0.1257, param 4000 |
| 50 / 51 | 144 / 144 | headlights +x / -x | none |
| 40 / 41 | 143 / 134 | mirrors +x / -x (also the UPVH mirror table) | none |
| 188 / 190 | 143 / 146 | front / rear suspension (struts, arms) | x axis at the axle |
| 155 / 156 | 130 / 130 | front hubs (brake disc) +x / -x | at the hub |
| 15 / 136 | 91 / 99 | exhausts +x / -x | none |
| 52 / 53 | 4 / 7 | side doors: the Vista Cruiser's rear doors (231), the vans' side doors (198, 200), the airport fire truck's lockers (186). Doors in `damage.py` (`DOORS`) since #209 ("Side doors 52/53" below) | vertical, limit 0 |
| 0 | 209 | miscellaneous | |

(The "+x/-x" naming is ours; DSF's +x is the car's left, the driver's side
on left-hand-drive cars.)

On the Desanne (`uv run dsff damage panels <vanilla> --id 266`):

- **MODL part 0 is the whole intact car**: one submodel per material holding
  every panel's triangles (8,027 vertices = the sum of all other parts but
  the wheel). Each panel's own MODL part holds the same vertices with its own
  index lists (zero shared index ranges, all part-0 triangles also in some
  panel part: 227, 147, 229, 130 and 62 checked the same way). So the intact
  car is drawn from part 0, skinned per panel through s9; the per-panel
  parts are presumably what a detached panel is drawn from (hypothesis).
- Parts 7, 8, 23 and 25 have no record and are exact copies of part 1 (the
  chassis panel), with their own submodels. Purpose unknown; converted cars
  leave them empty, as before.
- The rigid-body table entry of a panel (`bodies`, 0xB0) stores the panel's
  centre of mass c as `pos = -(Rᵀ c)` (R the stored 3x3 rows; the bonnet and
  boot are tilted), inertia and mass (bumpers 12-20 kg, bonnet 15 kg, doors
  8-9 kg, small parts 5 kg). The panel's collision model (PDL, record +0x20)
  is one box in that body frame (identity matrix), half extents at +0x50.
  Both bumpers' hinge pivots are exactly their body centres.
- The root record's collision model (5 boxes) is the car hull.

**The exterior entry** (N+2000) has the same merged part 0 with every
panel's s9 and its own crush targets (14,181 vertices), so the driven car
crushes and swings like the full entry. Its other parts are the front and
rear suspension (records 31, 63), a 6-vertex part on the gauge record and
the wheel, linked through the UPVH LOD links. `vehicle-model.md` used to
call the two suspension parts the bumpers; renders show struts and arms
(`out/damage/desanne-ext-parts.png`).

## Hypotheses (not traced in the exe)

1. The CPU side sets `g_v4PerPanelData[model].x` from impacts on the
   panel's collision box and `g_PanelTransforms[slot]` from its hinge
   (swing within `limit`, `param` maybe a break-off strength: 4000 on
   bumpers, 24/15 on bonnet/boot) and, once detached, from the debris body.
   Doors and wings have limit 0, but they do swing: `forceOpenPanels`
   opens vanilla doors wide (confirmed-runtime, see "Doors and wings swing"
   below), so limit 0 is not "locked"; what it means is untraced.
2. A detached panel is drawn from its own MODL part.
3. ~~The glass crack state follows the glass panel's damage index~~ Now
   confirmed-static: the damage-aware glass techniques (SSIC programs 47,
   50, 55) draw `v + 0.25 * floor(g_v4PerPanelData[s9 byte 1].y)`, so glass
   shows the crack state (`.y`, 0 to 3) of whatever record its vertices are
   bound to, chassis included (`vehicle-model.md`, "Glass"; `src/dsfforza/convert/glass.py`).
   What sets `.y` on the CPU side is still untraced.

## What `src/dsfforza/convert/damage/` does (ConvertOptions.damage, `port_car.py --damage`)

Converted cars used to put every body triangle on the chassis panel with
s12 = position: no crush, no swinging or falling parts, and the panel parts
empty. `port_car.py` and `ConvertOptions.damage` still default to that, but
`port_batch` passes `--damage` (`DEFAULT_PORT_OPTIONS`), so every roster
build and release has it. With the option:

1. **Panel per triangle.** Forza sections map to host roles by name:
   `bumperF*` front bumper (35), `bumperR*` rear bumper (36), `bumperFrameF`/`R`
   their bumper, `hood*` bonnet (4), `trunk` boot (5, else 124),
   `headlight*`/`glass?HL` and `mirror*` and `exhaust*` the record of their
   side (never the other side's, split per side if a section has both;
   "One panel for both mirrors" below), `glassF`/`glassR` the centred glass panel in that half (the vanilla
   370Z has none at the rear: its rear window goes with the hatch), tail
   lamps and wings the majority of their nearest centred panel (left and
   right voting together), side windows their door or the chassis,
   `interior`/seats/undercarriage the chassis, the steering wheel its
   record. Everything else (`body`, skirts) goes per triangle to the
   nearest body-type panel (chassis, front structure, doors, wings, side doors 52/53):
   a vote of the 3 host part-0 triangles nearest to its centroid, in
   coordinates normalised by each car's box, scored by surface distance
   plus 0.15 per unit of normal mismatch; a paint triangle asks the host's
   paint triangles first (score up to 0.12), since outer skins are shared
   paint and the structure under them is not. Then
   the labels are smoothed over the Forza surface (`smooth_labels`: two
   area-weighted majority passes over edge neighbours, and connected
   patches under 0.08 m², or of at most 3 triangles under 0.3 m², join the
   panel they share most edges with). See "Tears in crushed doors" below for
   why. (`forza2dsf.vanilla_sections`
   now drops upgrade variants past `d` too: the FM4 Skyline kept bumperFe/f/g
   and hoodg stacked on the vanilla parts.)
2. **Crush targets.** For each new vertex, the host's crush displacement of
   the same panel, inverse-distance weighted over the 6 nearest host
   vertices (normalised coordinates), scaled per axis by the car size
   ratio. A position that triangles of two or more never-swinging panels
   share (a seam inside one Forza surface: chassis, front structure, doors,
   wings, side doors 52/53), or that lies within 3 mm of another rigid panel's
   vertex, gets the mean of those panels' displacements on every copy
   (`_seam_map`). Since 2026-10-23 the seam is also pinned: on body paint
   always, on trim where the host's own border of those two roles stays
   still, the displacement is 0 at the seam and fades in over 0.2 m
   (`_pin_map`, `_pin_weight`), so the seam stays shut whatever damage
   amounts the two panels get. See "Seam gaps after side hits" below. The
   full entry uses the host's full-entry field, the exterior its exterior
   field. Since 2026-10-24 rigid panels are drawn through the chassis'
   transform slot (s9 byte 0) with their own damage index (byte 1), so
   doors and wings never swing open along those seams ("Doors and wings
   swing" below).
3. **Normal deltas** (s5 bytes 2, 3) from how each vertex's area-weighted
   face normal turns between the intact and crushed mesh.
4. **Parts.** Each panel's triangles go into the panel's own MODL part
   (detail masks 4, 8, 16), and part 0 gets them all again, merged per
   material on the same vertices (split when a vertex span passes 16 bits).
   The exterior gets the merged part 0 only. The shadow LOD stays on the
   chassis in part 0.
5. **UPVH.** Per panel, from the host panel's vertex box to the Forza
   panel's (2nd-98th percentile box): hinge pivots (bonnet rear edge, boot
   front edge, bumper centres, door and wing lines; centred hinges stay at
   x = 0; a move over 0.35 m keeps the host's), body centre of mass, inertia
   by the size ratio, collision box and translation scaled (clamped to
   0.67-1.5x). Steering, gauges, suspension and hubs are left to the
   cockpit and wheel code; the chassis body/box only with the hull option.
   The hull option (`--damage`, the default since #210; `--damage panels`
   leaves it out) also maps the root collision hull from the host's body
   box to the Forza one ("Collision hull" below).
6. **Exposed inner geometry** (`split_exposed`, `DamagePlan._expose`). Before
   the lamp plan, `forza2dsf.convert` splits trim subsections into their own:
   trim triangles of `hood*`/`trunk*` facing away from the section's nearest
   paint triangle (within 0.2 m) are the panel's underside and become
   technique `dsf_inner` (shared paint, like vanilla undersides); trim
   triangles of other sections below the hood surface (a 5 cm height map
   of the hood, 1 cm clearance) are the bay. Each split subsection is
   tagged by facing (`bay+y` floor, `bay-y` underside, `bay x` side walls,
   `bay+z`/`bay-z`, `inner y/z/x`) and carries a sentinel UV0 that
   `add_geometry(exposed=True)` writes. `_expose` replaces it with a planar
   projection of the vertex, in the bay box (hood footprint, 0.55 m deep)
   or the panel's box, onto the tag's picture of the shared chassis texture
   (`EXPOSED_MAP`: engine and wiring for floors, subframe for undersides,
   inner wing/strut tower for side walls, bulkhead and radiator for the
   front and rear walls, the inner-frame picture for panel undersides), and
   the baked sky with a facing-based value like vanilla (bay 0.62 up, 0.35
   sideways, 0.04 down; undersides 0.2). Without `--damage` (and in the
   shadow LOD and the interior shell) the split subsections draw as before
   (`dsf_inner` as plain paint). Since 2026-10-23 the bay is one of four
   zones (`exposed_zones`): under the engine cover (`bay`), under the other
   cover (`boot`: luggage well), behind the rear and the front bumper skin
   (`rear`, `front`); a triangle joins the zone that hides most of its
   sample points. See "Black boot well and rear panel" below.

3b. **Decal UV1.** Every new 32-byte vertex (paint, lamps) gets the host's
   UV1 at the closest point of the nearest host triangle (normalised
   coordinates, barycentric interpolation), searching the same panel and
   material first, then the same panel and material class (shared vs the
   entry's own), then any panel. Host triangles whose UV1 leaves 0..1
   (tiled chrome) are used only as a last resort, and a normal-mismatch
   penalty keeps outer faces on outer faces. Host triangles whose UV1
   collapses to one texel (vanilla's unscratched surfaces: hidden faces,
   and everything above the waist) are candidates like the rest since
   #298 ("Scratches above the waist" below). The rebuilt interior's 32-byte vertices (its outer shell, seen
   through the windscreen) get the host interior's UV1 the same way, any
   panel (`DamagePlan.interior_uv1`, called after `cockpit.convert_interior`).
   A Forza triangle whose corners land on different host charts (UV1
   islands, found by joining host triangles through shared position+UV1)
   is put on the chart most of its corners use; the other corners get a
   copy of their vertex with UV1 from that chart (`_split_seams`).
   Offline: the panels land on the vanilla tiles (`img/damage-uv1-305.png`,
   vanilla `img/damage-uv1-vanilla266.png`). Paint area on exterior triangles
   whose UV1 spans more than 0.15 (vanilla: 0%): 2.4-6.2% without the seam
   split (a white streak along the Mustang's roof centre in the preview),
   0.1% with it (Mustang). Lights-material triangles without a lamp byte
   (s9 byte 2 = 0: badges, plate, reflector trim) take their panel's paint
   layout, not the lamp layout, which would put them on the broken-lamp tile. `uv run dsff damage render ... --by decal`
   previews the scratch mask at UV1 (`img/damage-decal-305.png`
   against `img/damage-decal-vanilla147.png`).

The lamp bytes (`src/dsfforza/convert/lights.py`) pass through: damage writes s9 bytes 0
and 1 only.

Checks (`uv run dsff damage check <archive> --id N`): every part-0 s9
pair is a host panel, part 0 uses many panels and has crush targets, every
panel part's triangles carry that panel's pair and are in part 0. All three
test cars pass; `upvh.py lint` passes.

### Test build (2026-10-22)

`port_car.py ... --car NIS_370Z_10 --car BMW_M5_12 --car NIS_SkylineGTR_93
--mdxn-limit 0x2C0000 --damage` (hosts chosen by `host_select`: 227 vanilla
370Z, 147 Saloon 03, 229 Skyline R33 drift):

| Car | Panels used | Notes |
| --- | ---: | --- |
| 304 370Z | 18 | hatch glass on the boot panel; tail lamps on the chassis |
| 305 M5 | 21 | spoiler and boot on the boot panel; rear-door windows on the rear doors |
| 306 Skyline R32 | 19 | wing on the hatch/spoiler panel (124); side windows on the chassis |

Crush statistics against the hosts (MODL part 0; dz/L = median z
displacement of the front or rear 10% over the car length; "still" =
vertices that don't move):

| Car (host) | Full: front dz/L, rear dz/L, still | Host full |
| --- | --- | --- |
| 304 370Z (227) | -0.049, +0.032, 40% | -0.048, +0.030, 31% |
| 305 M5 (147) | -0.056, +0.024, 30% | -0.065, +0.023, 39% |
| 306 Skyline (229) | -0.075, +0.036, 37% | -0.081, +0.037, 32% |

The exteriors match the same way. MDXN sizes grow by 0x1B000 to 0x20800 per full
entry (mostly the second index list) and 0x4800 to 0x7000 per exterior; all fit
`--mdxn-limit 0x2C0000`. Lamp bytes are unchanged against the same build
without `--damage` (`out/damage/baseline.sp`). `chnk.py rebuild --verify`
is byte-identical.

Renders: `img/damage-304-panels.png` (intact, by panel),
`img/damage-304-crushed.png` against the vanilla 370Z
`img/damage-vanilla227-crushed.png`, `img/damage-304-parts.png` (the panel
parts without part 0), `img/damage-305-exterior-crushed.png` against
`img/damage-vanilla147-exterior-crushed.png`.

### In-game test plan (2026-10-22)

The panels variant ran as the r7 and later builds below, with this probe;
`damage-hull.sp` has no recorded run (the hull ran in game later, #210, "Collision hull" below). Archives in `out/damage/` (IDs 304 370Z, 305 M5, 306 Skyline; the same
three builds apart from the damage option):

- `damage-panels.sp`: `--damage`.
- `baseline.sp`: no damage option (control).
- `damage-hull.sp`: `--damage hull` (bare `--damage` since #210).

`run-test.ps1 -Damage` makes the probe call
`GameVehicleResource.fullVisualDamage` on each car 3 s into its chase view
(logged as `fullVisualDamage ok`), so screenshots catch the crushed car in
chase and cockpit view. Look for: the nose pushed in and the bonnet buckled
(not the host's shape), bumpers, bonnet and boot ajar or hanging, cracked
glass, dented doors; no triangles flung across the car (a wrong panel
binding) and no invisible panels; the control should only crack its glass.
In normal crashes (drive into traffic), a bumper that falls off should lie
on the road as the Forza bumper.

### Run 20261022-062852 (confirmed-runtime) and the bumper frames

`out/cockpit3/ported-cockpit3.sp` (with the UV1 fix), `-Damage`: scratches
look vanilla-like and the Mustang bonnet from the cockpit is clean
(`img/r8-mustang-cockpit.png`). The damaged M5 still showed a white chevron
low on the rear bumper (`img/r8-m5-damaged.png`); the r7 build showed the
same shape while its bumper UV1 was a constant (0, 0), so it was not the
decal. Cause (confirmed-static): the Forza `bumperFrameR` section (bumper
frame, trim material with Forza UVs over the whole trim texture) was on the
chassis panel, right behind the bumper skin. Crushed, the skin moves forward
more than the chassis there, and the frame pokes through near the centre
(`out/damage3/305-rear-before-after.png`: chassis-coloured bits in the
crushed bumper, gone after the fix). Fix: `bumperFrameR`/`bumperFrameF` go
on their bumper panels (vanilla bumper parts carry their own trim), so they
move and crush with the skin. `out/damage3/poke.py` counts pixels, over
five views, of triangles hidden on the intact car that show when every
panel is crushed by t: comparable to vanilla (M5 at t = 0.5: 2,816 px against
2,430 for vanilla 147).

### Run 20261022-040545 (confirmed-runtime)

`out/r7/ported-r7.sp` (`--damage panels`, five cars 304-308, hosts 227,
147, 229, 193, 251), probe `-Damage`: `fullVisualDamage ok` on all five;
panels crumple, bumpers, bonnet and boot deform, glass cracks, no crash; the
undamaged run `20261022-040320` is fine too. Defect: the wrong decal UV1
above. Fixed build: `out/damage2/ported-d2.sp` (same five cars and options).

### Host coverage

The 31-car batch (`src/dsfforza/build/port_batch.py`) crashed on hosts 173 and 184:
their interior entries have 32-byte buffers without semantic 9, and
`Uv1Field.read` read it unconditionally. Every semantic-9 read in
`damage.py` now goes through `s9_pair` ((0, 0), the chassis, when a buffer
has none), and `interior_uv1` takes the UV1 and normal offsets from each
buffer's VDCL instead of the converter's layout. `out/damage3/hostsurvey.py`
runs the damage path on every usable host in `research/hosts.tsv` (137):
`DamagePlan` (panels, crush fields, UV1 fields of the full, exterior and
interior entries), a full-entry panel build and merge, the UPVH patch
(re-parsed), the exterior build, and `interior_uv1` on the host's own
interior, with the 370Z at its coarsest LOD. Host local materials (Chassis,
Lights) use only 28- and 32-byte buffers on all of them, the layouts
`damage.py` writes.

That VDCL test (semantic 8 as four halves) also matched the interior's
stride-20 needle buffers, whose semantic 8 is DashboardInstrument (u, v,
gauge id, 0): `interior_uv1` wrote a decal UV over the gauge id, and no
converted needle turned (`cockpit.md`, "Static needles in the 40-car
build"). It now skips semantic 8 with VDCL name hash 0x75FAD367
(`DASHBOARD_INSTRUMENT`), and `forza2dsf` fails a car whose needle ids
aren't 1 or 2 after the interior is finished.

## Testdrive3 report: tears in crushed doors and the black bay (2026-10-23)

The user's installed build (`out/testdrive3`, 40 cars, `--damage`) showed
two defects on the damaged BMW M5 (ID 35, host 147): black angular slits
across the crushed doors and side panels, and a solid black engine bay once
the bonnet had come off. Scratch scripts and renders: `out/dmgfix/`.

### Tears in crushed doors (cause confirmed-static)

Not folding: triangles that turn over at full crush cover 0.10% of the M5's
exterior, the same as vanilla 147 (`flips.py`), and the crushed-normal turn per
panel matches vanilla (`ndelta.py`). The slits are panel borders. The old vote
(7 nearest host vertices) put long chassis-bound triangle islands into the
M5's door skins (`td3-m5-roles.png`, `m5-ext-panels.png`): the host's door
skins are large triangles with vertices only at their edges, so mid-door
points sat nearer to chassis vertices behind the skin (jambs, cabin). Every
island border is a seam inside one Forza surface whose two sides follow
different panels (crush field and damage amount), so it opened when crushed;
behind it is the unlit inside of a single-skinned Forza body, hence black.
Vanilla panels meet at real shut lines, backed by jambs and door cards.

Islands per car (`survey.py`: connected same-panel patches under 0.3 m²
touching another panel, `nearest`-rule sections, exterior LOD, testdrive3
hosts), old rule against the new one (step 1: nearest host triangles, paint
first, then `smooth_labels`): 4,376 patches (46.5 m²) over the 40 cars to 73
(13.4 m²), every car at 0 to 8 (M5 186 to 3, WRX STI 203 to 4, Raptor 256
to 2; `out/dmgfix/survey-testdrive.txt`). The remaining patches are real
small panel pieces (sills, front-structure corners). The Durango and the
Express van have no `hood*` section (the bonnet is part of `body`), so no
bay is found and none is exposed: their host bonnet record has no
geometry. Renders: `av-m5-new2.png`, `av-mustang-old.png` (sawtooth door
borders) against `av-mustang-new.png` (doors as clean patches).

Seams that remain between rigid panels now share the crushed position
(step 2). Converted M5 exterior, positions on more than one panel whose
crushed positions differ by more than 1 cm: 375 (testdrive3) to 129, all on
seams with panels that swing or detach (boot, bumpers, headlights, mirror),
as on vanilla (147 has 185 such positions). They still open when two rigid
panels get different damage amounts, along the shut line, as vanilla seams do.

Cost: big flat Forza triangles with one seam corner fold over at full crush
under the nose (undertrays facing down: Mustang 1.0% to 1.8% of the
exterior area, Skyline 0.36% to 0.55%, M5 0.10% to 0.23%; vanilla 0.01-0.10%).
They face the road and only show from below.

### Black bay and panel undersides (cause confirmed-static)

Vanilla bays (147, `vanilla147-ext-nobonnet-sky.png`) are drawn from the shared
chassis texture's bay pictures (GLOBAL_Chassis_Simple: radiator, inner
wings, subframe; `out/dmgfix/trimtex-grid-*.png`) plus the host's own engine
cover, with baked sky 0.4-0.7 on floors and ~0.34 on walls; bonnet and boot
undersides are GLOBAL_Chassis paint on the texture's inner-frame picture
(`inner.py` on 147 and 266). The M5's bay (Forza `frame`/`black` trims,
`undercarriagea` at LOD 1; Forza has no engine model, the hood never opens)
was 1,729 triangles on `TRIM_UV` (R 24) with Forza AO baked shut (sky 0.26),
and its hood underside (2 m² of `black`) the same texel. Fix: step 6. The
exposed geometry per car is large (testdrive cars at LOD 1: 700 to 3,200 bay
triangles, 90 to 490 underside triangles; `survey-testdrive.txt`); the
material budget is unchanged (shared trim and paint only). Renders of the
four test cars from above without the bonnet: `s2-bays-top.png` (right:
vanilla 147). An earlier variant copied the host's UV0 per vertex; Forza's bay
floors are a few large triangles (the M5's: 44 over 1.65 m²) and the
pictures came out as streaks (`m5new-ext-nobonnet-sky.png`).

### Test build `out/dmgfix/set2`

No recorded run; the next section's `out/underside/build` holds the same
four cars with this fix and the boot-well fix, and ran in game.
`port_batch.py build --out out/dmgfix/set2 --car BMW_M5_12 --car NIS_370Z_10
--car NIS_SkylineGTR_93@fm4 --car FOR_MustangBOSS429_70` (IDs 1 to 4, hosts
147, 227, 229, 193): all lint, `damage.py check` passes on all four. Probe:
`run-series.ps1 -Archives out\dmgfix\set2\dngvehicles.sp -ProbeIds '1,2,3,4,147'
-Damage -DamagePanels -ShotSeconds 3` (`-DamagePanels`: after
`fullVisualDamage` at ~3 s the probe calls `forceOpenPanels` at ~6 s and
`removeAllPanels` at ~9 s in the same chase view; vanilla 147 is the control).

## Black boot well and rear panel (testdrive4, 2026-10-23)

User report on `out/testdrive4`: "when body parts fall off, car is solid
black underneath". Host run `host-20261023-092156` (`-Damage
-DamagePanels`): with every panel removed, the M5 (ID 35, observe-03) and
the FM4 Skyline (ID 36, observe-12) showed a flat, near-black, featureless
surface where the boot lid and the rear bumper had been, and in the boot
well. Scratch: `out/underside/` (`view.py` shaded render from any angle with
panels removed and a census of what is visible in a screen box,
`vanillaref.py`, `forzarear.py`, `atlas.py`, `survey.py`).

### What vanilla cars show there (confirmed-static, confirmed-runtime)

Vanilla run `host-20261023-093807` (vanilla archive, probe `62,147,227,229,266`,
`-Damage -DamagePanels`): 147 with its panels removed (observe-03) shows a
grey boot tub and, behind the rear bumper, the rear panel and crash beam
with bright bolts; 229 (observe-12) the same beam under its opened boot lid.
Static, every vanilla trim triangle hidden by a removable panel (inside the
panel's footprint and inward of it; `vanillaref.py`, exterior entries of 147,
227, 229, 266, 193, 251; `vanillaref-ext.txt`): almost all
`GLOBAL_Chassis_Simple` on pictures of the shared chassis texture (SSIC
`00122b73d1fa1227`): the boot tub panels (u 0.40-0.48, v 0.10-0.29), the
rear panel/crash beam picture (u 0.31-0.50, v 0.35-0.53), radiator,
subframe and undercarriage pictures, a little body paint (the Mustang 193's
boot is 23% paint), texel R 16-90 (mean around 40). Baked sky 0.23-0.34 in
boot wells, 0.09-0.41 behind the bumpers. Renders: `out/underside/cmp-rearlow.png`
and `cmp-frontlow.png` (147, 227, 266, testdrive4 M5 and Skyline without
boot lid and bumpers; tiles left to right).

The trim shader (`GLOBAL_Chassis_Simple_LOD1`, SSIC program 41, disassembly
`out/underside/gp41_ps.txt`): colour = (ambient x sky + sun x N.L) x tex.R
+ specular x tex.G; the paint shader (program 15) = tex.R x lerp(1, paint
colour, tex.B). R is brightness, B the paint mask; the chassis atlas is
grey with B = 0 outside the paint blocks.

### Cause (confirmed-static)

The previous fix (step 6) only re-textured trims under the bonnet. The
M5's boot well and rear panel are Forza `body` trims (`frame`, `black`:
0.4-1.3 m² each, a few large triangles; `forzarear.py`), and every
converted trim sits on `TRIM_UV`, one texel (R 24) of the uniform dark
block. The census of the testdrive4 M5 from behind with the boot lid and
bumper removed: 100% `GLOBAL_Chassis_Simple` at uv (0.97, 0.50), against
vanilla 147's tub and beam pictures. Normals, sky (0.2-0.45) and material
binding were fine (`view.py --mode backface`: the stored normals face the
viewer); the surface was just one dark texel.

### Fix (`src/dsfforza/convert/damage/`, `src/dsfforza/convert/forza2dsf/`)

`split_exposed` now finds four zones (`exposed_zones`, `zone_maps`,
`zone_vote`), each from a 5 cm map of the covering panel's sections:

| Zone | Panel | Hidden when | Pictures (`EXPOSED_MAP`) |
| --- | --- | --- | --- |
| `bay` | engine cover | below its lowest point (as before) | engine, subframe, inner wing, bulkhead, radiator |
| `boot` | the other cover | below its lowest point | boot tub panels (u 0.42-0.495, v 0.17-0.30) on floor and walls, subframe below |
| `rear` | `bumperR*`, `bumperFrameR*` | 2 cm to 0.6 m inward of its outer skin | crash beam / rear panel facing back, subframe, inner wing |
| `front` | `bumperF*`, `bumperFrameF*` | the same | radiator facing forward, beam, subframe, inner wing |

The engine cover is the bonnet unless Forza's gamedb lists the car as
mid- or rear-engined (`engine_placement`: `Data_Car.EnginePlacementID`;
then the boot lid covers the bay and the bonnet a luggage well). A trim
triangle joins the zone that hides most of its sample points (corners,
centroid, points 10 cm apart; at least 25%), so large Forza triangles that
poke out of a map are no longer missed (the M5's rear wall and the
Skyline's boot walls are a few triangles; centroids alone left half of
them dark: `cmp3-tu.png` against `cmp4-tu.png`, red = still `TRIM_UV`).
Bumper, exhaust and plate sections are never re-textured outside the bay.
Projection boxes: the cover's box deepened by 0.55 m, the bumper's box
deepened inward by 0.6 m. Sky by facing: 0.08 down, 0.28 sideways, 0.40 up
(vanilla 0.09-0.41). The material budget is unchanged (shared trim only).

### Survey (40 testdrive cars, exterior LOD; `out/underside/survey-testdrive4.txt`)

New exposed geometry on 38 cars: boot zone on 36 (112 m² in total), rear
on 35 (32 m²), front on 36 (14 m²); bay and inner counts as before (a few
% more bay triangles from the vote). Mid/rear-engined cars swap covers:
205 T16, Agera, Huayra, DMC-12 get the engine pictures under the boot lid
and the tub under the bonnet; the S5S Raptor and Veyron have no `trunk*`
section, so their front lid now covers a luggage well and no bay is
found. No zone: Express van (no covers), Veyron and SVT Raptor (no boot
lid or rear bumper sections), 250 GTO / 2000GT / Agera (no bumper
sections). Offline renders: `out/underside/offline-rear-before-after.png`
(vanilla 147/229 against the M5, Skyline and RX-7 before and after),
`mid-check.png` (Agera, DMC-12; red = untouched trims).

### Test build and in-game result

`port_batch.py build --car BMW_M5_12 --car NIS_SkylineGTR_93@fm4 --car
FOR_MustangBOSS429_70 --car MAZ_RX7_97 --car NIS_370Z_10 --out
out/underside/build` (IDs 1-5): lint and `damage.py check` pass on all five.
Host run `host-20261023-101158` (probe `147,1,3,2,4,5`, `-Damage
-DamagePanels`; confirmed-runtime): no crash, all phases ok. With every
panel removed the M5 (observe-03) shows a grey tub wall where the boot was
and the crash beam and undercarriage pictures, with bright bolts, below it;
the Skyline (observe-12) a textured grey rear panel under the lamps and a
grey boot well. Side by side with testdrive4 and vanilla 147/229:
`out/underside/ingame-before-after.png`. Both are clearly textured now,
not flat black, but still darker than vanilla 147's boot. That boot has a
floor that faces up into the sun; the M5's Forza "boot" is one rear-facing
wall (risk 7). An earlier build of the same cars with the centroid rule and
the darker tub crop (run `host-20261023-100134`) was visibly darker.

M5 gaps (user report "damaged body parts have gaps"): in the damaged shots
(testdrive4 observe-02, `out/underside/ingame-damaged-gaps.png`) the doors
are closed and untorn. The gaps are at the boot lid: at full damage it
crushes forward and lifts, so dark wedges open between it and the tail
lamps or wings, and a slot opens under the plate where the lid parts from
the bumper. Vanilla 147 opens the same shut lines, but less. Through the
gaps you see the boot zone, now on the tub picture instead of the black
texel. The lid's crush and hinge come from the host's field (147), not
from this fix.

## Seam gaps after side hits (testdrive7, 2026-10-23)

User report on `out/testdrive7`: "saleen has those body gaps that I earlier
told you about, on bmw". After a crash into the S5S Raptor's (ID 22, host
163) front corner, wide, jagged black slits opened in front of the door
and through the rear quarter, from the window down to the sill (user
screenshot, crop `out/seams/user-crop.png`). Scratch: `out/seams/`
(`border.py`, `vanillaside.py`, `dview.py`, `panelview.py`, `comps.py`,
`survey.py`, `zoom.py`, `summary.py`).

### Cause (confirmed-static)

- The slits are panel borders, not the Forza shut lines. The S5S Raptor's
  `body` is one connected surface: body, both bumper skins and the door
  share edge positions, and the door's shut line is a groove in that
  surface (`comps.py`: 15,402 of 21,880 LOD-1 triangles in one
  component). The panel vote follows the host's layout, so the "door
  front -x" patch runs past the real door into the rear quarter's intake
  and ends in a sawtooth of Forza triangle edges in front of the door and
  behind the intake (`sal-td7-panels-crop.png`; green = door).
- A vertex is drawn at p + f·d, f being its panel's damage amount
  (`data.x - offset * x`, unclamped). The testdrive3 fix gave both copies
  of a seam vertex the same d, which keeps a seam shut only while both
  panels get the same f. `fullVisualDamage` does that (every probe so far
  used it, so seams looked shut: `sal-td7-full-crop.png`); a real impact
  damages the door and wing more than the chassis, and each seam opens by
  |fA - fB|·|d|. Behind it is the unlit inside of a single-skinned body.
  With the door and wing at 1, bumper 0.8, front structure 0.7 and chassis
  0.15 (`dview.py --scenario side-x`) the offline render shows the same
  jagged slits as the screenshot (`z-sal-cmp-side-x.png`, top).
- Positions of two rigid panels 0.5-3 mm apart (separate Forza shells
  touching) were not seams at all and opened even at uniform damage
  (S5S Raptor: up to 0.29 m, chassis against front structure).

Vanilla cars (`border.py`, `vanillaside.py`, exterior part 0 of 147, 163, 205,
227, 229, 274): door, wing, sill and chassis vertices where two panels
meet barely move (median 0-3 mm; p90 up to 30 mm, max 77 mm), the dents
are in the panels' middles. Wings against the front structure move
0.14-0.33 m, but that border is under the bonnet. Vanilla borders still part
by up to 5-8 cm with uneven damage; they are real shut lines with jambs and
inner panels behind them, so no hole shows.

### Fix (`src/dsfforza/convert/damage/`)

- `_seam_map`: positions of different rigid panels within 3 mm
  (`SEAM_NEAR`) join one seam group (union-find); every copy takes the
  group's panels and the crush computed at the group's first position.
- `_border_motion`: per role pair, the host's median crush at its own
  borders (rigid host vertices within 15 mm of another rigid panel's).
- `_pin_map`, `_pin_weight`: a seam group on body paint (the Forza skin
  has no backing), or on trim where the host's border of those two roles
  moves less than 3 cm (`SEAM_STILL`), is pinned. Every rigid-panel vertex
  scales its crush by smoothstep(distance to the nearest pinned seam of its
  panel / 0.2 m) (`SEAM_REACH`); copies of one seam position use the
  smallest weight of their panels, so unpinned seams also stay shut at
  equal damage. Pinned seams have d = 0 and stay shut for any damage
  amounts.
- Swinging and detaching panels (bonnet, boot, bumpers, lamps, mirrors)
  are not rigid and are untouched: their seams open by hinge and crush as
  before (the M5's boot lid above), and they still detach from their own
  MODL parts. The exposed-surface work (step 6) is unchanged; the test
  build logs the same exposed counts as testdrive7.

### Survey (40 testdrive cars, exterior LOD 1; `out/seams/survey-testdrive7.txt`)

`survey.py` runs `DamagePlan` and the exterior build on the vanilla host
without a full conversion, once with the testdrive7 rules and once with
the fix, and measures every pair of rigid-panel vertices within 3 mm:
separation at uniform damage, worst differential (one panel at 1, the
other at 0) and both side scenarios.

| | Before (testdrive7) | After |
| --- | --- | --- |
| Paint seams, side scenarios: max separation per car | 12-475 mm (S5S Raptor 211, M5 173, Cuda 475, Mustang 444) | 0.4-3.5 mm (the vertices' own 3 mm offset) |
| Paint seams, worst differential, max per car | 12-678 mm | 0.5-3.5 mm |
| Paint seam positions opening > 1 cm (both side scenarios, all cars) | 15,394 | 0 |
| All rigid seams at uniform damage, max per car | up to 551 mm (S5S Raptor 293) | 2-4 mm (Veyron 15 mm, trim) |
| Trim seams, side scenarios | up to 514 mm | under 4 mm on 18 cars; on 22 cars 11-336 mm, all between a wing and the front structure (inner arches, under the bonnet; the host's own border moves 0.1-0.3 m there), plus the Express van's rear cargo-door trims (91 mm) |
| Mean crush of rigid-panel vertices | | 72% of before (median), 41-100% |
| Front 10% median dz/L | | unchanged on 33 cars; 250 GTO -0.045 to -0.019, Express -0.020 to -0.002, Mustang -0.106 to -0.079 (no bumper sections: the nose is wing/front-structure paint, now pinned at its seams) |

Renders (shaded, magenta background, -x side, side scenario): built
archives `out/seams/archive-before-after-side-x.png` (exterior),
`archive-full-before-after-side-x.png` (full entry LOD 3),
`archive-before-after-wing-x.png` (wing and door hit only: the arch flare
no longer peels off the front structure).

### Test build `out/seams/build` and in-game result (confirmed-runtime)

`port_batch.py build --car SAL_S5SRaptor_10 --car BMW_M5_12 --car
SUB_WRXSTi_11 --out out/seams/build` (IDs 1-3, hosts 163, 147, 136): lint
and `damage.py check` pass on all three.

New probe option `run-series.ps1 -DamageSide` (`clonetest.lua`
`SIDE_COPY`): the chase camera sees only the car's back, so 2 ticks into
the chase view the probe parks a copy of the model (`GameVehicleResource.create`,
no occupants) 9 m ahead and 3 m to the left, turned a quarter turn so one
side faces the camera, holds the player's car still, and calls
`GameVehicleResource.applyDamage` (damage 1.0, `overridePlayerOnlyDamage`)
on the copy at car-space (1.0, 0.4, 1.3), (1.0, 0.4, 0.2) and (1.0, 0.4,
-0.8) (front wing, door, rear quarter) at ticks 4, 8 and 12. Found on the
way: `applyDamage`'s location is in car space (world points beside the copy,
run `host-20261024-002829`, barely marked it); hitting the player's own car
sent the game to the Shift view for most chase shots (`host-20261024-001258`,
`-002016`); a car spawned in front of a moving one keeps rolling and the
camera leaves the copy behind (`host-20261024-003745`).

- Run `host-20261024-005404` (`-DamageSide`, probe `147,1,2,3`, 2 s shots;
  sheet `out/seams/ingame-side8-sheet.png`, crops
  `out/seams/ingame-side-copies.png`; also `host-20261024-003238`): the hits
  crush each copy's near front corner, pop the bonnet, knock the front
  bumper off and dent the doors and rear quarter, so the panels get
  uneven damage, as in the user's crash. The S5S Raptor (observe-03, -04),
  M5 (observe-10, -11) and WRX STI (observe-16, -17) copies show dented,
  scratched doors with their shut lines closed: no jagged black slits in
  front of or behind the doors, no hole along the S5S Raptor's intake. The
  M5's rear quarter, crushed into the wheel, shows the darker shut line
  behind the rear door.
- Run `host-20261024-010046` (`-Damage`, uniform `fullVisualDamage`; sheet
  `out/seams/ingame-full-sheet.png`, rear crops `ingame-full-rear.png`):
  no crash, all phases ok; cockpit views clean. The WRX's boot lid crumples
  and parts from the rear quarters (a swinging panel; not pinned, as
  vanilla). In observe-03 a grey plate stands up through the S5S Raptor's
  tail, not there in observe-02 (debris or a detached panel in flight;
  not traced).
- Host starts are flaky at present: two runs of this build and one of
  another agent's (`out/steering/exp`) stopped at "Story world not ready"
  (`host-20261023-235029`, `-235409`); the next starts worked.

## Doors and wings swing; mirrors that hang in the air (testdrive9, 2026-10-24)

User report on `out/testdrive9` (same cars as `out/testdrive10`): after real
crashes the S5S Raptor (ID 22) showed a jagged black slit behind its door,
from the window to the sill, and the 250 GTO (ID 25) tears across its nose
and a slit through its door; the S2000 (ID 4) had a side mirror floating in
the air above the bonnet and none on its doors. Scratch: `out/dmg5/`
(`asurvey.py` survey of built archives, `crashview.py` renders,
`sections.py`, `hostpanels.py`).

### Doors and wings swing (cause confirmed-runtime)

The seam fix above pins seams between rigid panels: both copies of a seam
vertex get crush 0, so the seam stays shut for any damage amounts. That
holds only while both panels share a transform. Doors (roles 31, 32, 54,
55) and wings (33, 34) have their own transform slot (s9 byte 0) and a
hinge with limit 0, and limit 0 does not lock them: `forceOpenPanels`
opens vanilla doors wide (run `host-20261023-093807`: 227 observe-07, 266
observe-16), and on converted cars it swings the door and wing patches out
as thin arcs (run `host-20261023-101158`, RX-7 and Mustang; VM run
`vm-20261024-061315`, S5S Raptor observe-10, 250 GTO observe-17). A crash
knocks them ajar the same way. A converted door is a patch of the
one-piece Forza skin cut along the host's panel layout, not the Forza shut
line (the S5S Raptor's "door" runs into the rear quarter's intake), with
nothing behind it (door cards are on the chassis). Swung by a few degrees,
its border opens into exactly the user's slits; on the 250 GTO, which has
no bumper sections, both wings meet in the middle of the nose, so a wing
swinging about the A-pillar tears the nose apart. Every probe so far used
`fullVisualDamage` or `applyDamage` hits, which crush without swinging, so
the earlier seam survey (no transforms) could not see it.

Fix (`DamagePlan.bound`): every rigid panel's vertices take the chassis'
transform slot and keep their own panel-data index (s9 byte 1), so they
still dent with their own damage amount but never swing or fly off. The
interior's door glass follows (`forza2dsf.interior_glass_skin`), and
`damage.py check` accepts the (chassis slot, panel) pairs. Swinging and
detaching panels (bonnet, boot, bumpers, lamps, mirrors, exhausts) keep
their own slots; their borders are Forza section borders, real shut lines.

Survey (`asurvey.py`, exterior part 0 of the built cars; "swing" = every
door and wing record turned 0.1 rad about its hinge through its slot, as
the shader would draw it):

| | testdrive10 | fix (`out/dmg5/all40`) |
| --- | --- | --- |
| Cars whose paint seams open > 1 cm with doors/wings ajar | 40 of 40 | 0 |
| Max separation per car, swing | 44-187 mm (250 GTO 187, Skyline 183, S5S Raptor 160) | 0-3 mm (the vertices' own offsets) |
| Paint seams between rigid panels, front, rear and side crush | 0.1-3.4 mm | unchanged |

Seams between a rigid and a swinging panel (bonnet against wing, bumper
against front structure) open under uneven crush by 0-340 mm per car,
the same range as vanilla hosts (147: 205-267 mm; 163: 156-268 mm; 229: up
to 494 mm, same survey on the vanilla archive): those are Forza section
borders and vanilla shut lines, backed by the exposed-surface texturing
above. Renders (crush as `side-x`/`front` plus the 0.1 rad swing, magenta
= hole): `img/damage-swing-s5s-side.png` (S5S Raptor, testdrive10 top, fix
bottom), `img/damage-swing-250gto-front.png` (250 GTO, testdrive10 left).

### One panel for both mirrors (cause confirmed-runtime)

Side sections (`mirror*`, `headlight*`, `exhaust*`) take the host record on
their side, chosen among records with vertices in the host's full-entry
part 0. Host 278 (Sports Car 07, the S2000's and RX-7's host) has a +x
mirror record (slot 11, model 13, body at (0.83, 0.25, 0.03)) but no +x
mirror vertices in its full entry (its exterior has them), so both Forza
mirrors went onto the -x mirror panel. `patch_upvh` then mapped that
panel's rigid body from the host's left mirror into the box of both Forza
mirrors: (0.00, 0.25, 0.07), inside the cabin. When the panel comes off, both
mirrors leave the doors as one body that starts inside the car and ends up
on top of the hull: VM run `vm-20261024-061315` observe-03, after
`forceOpenPanels`, shows the S2000's mirror hanging in the air ahead of the
car and none on the doors, as in the user's screenshot. A single section
spanning both sides (the Durango's and Express van's headlights, the WRX's,
Fiat 500's and Range Rover's exhaust pipes) also went onto one side's
record whole.

Fix: a side section never goes on the other side's record. A record
without vertices in an entry borrows its partner's (the other side's role)
vertices mirrored in x, for its side test, its crush field and its UPVH box
(`_source`, `_host_box`, `_disp`); a section with triangles on both sides
splits per side; a centred record (278's twin exhaust) takes either side,
and a side with neither its own record nor a centred one goes on the
chassis. `patch_upvh` keeps a centred
hinge or body centred only if the Forza panel spans the centre line too
(the RX-7's one-sided pipe on 278's centred exhaust record left the body
0.43 m from the pipe).

In game (VM, `-Damage -DamagePanels`, testdrive10 against the fix build
`out/dmg5/all40`, same IDs): with `forceOpenPanels` the S5S Raptor's and
250 GTO's door and wing patches no longer swing out (runs
`vm-20261024-061315` observe-10, -17 against `vm-20261024-062957`
observe-11, -18; `img/damage-forceopen-s5s-250gto.jpg`), and the S2000 and
RX-7 keep a mirror on each door where testdrive10 lost the left one (runs
`vm-20261024-063859` observe-07, -20 against `vm-20261024-064047`
observe-06, -19; `img/damage-forceopen-mirrors-s2000-rx7.jpg`). No crash in
these runs; one start of the fix build crashed while the story world
loaded, before any converted car was requested (`vm-20261024-061516`), and
the same archive ran clean three times around it.

Survey (detachable panels of the full entry: geometry on both sides where
the host's isn't, or the rigid body more than 0.15 m from the panel's
geometry): testdrive10 8 cars (RX-7 and S2000 mirrors, RX-7 exhaust body,
WRX, Fiat 500, Range Rover exhausts, Durango and Express headlights), fix 1
(the Mito: host 128's own -x headlight body sits 1.65 m from its lamp,
mass 29 kg, the same on the vanilla car).

## Doors and wings as Forza parts (2026-10-24, follow-up)

PR #14 held every converted door and wing shut. Vanilla doors swing ajar in
crashes and open wide with `forceOpenPanels`, so this round finds the doors
and wings in the Forza mesh and lets those swing. Scratch: `out/dmg6/`
(`islands.py`, `parts.py`, `panelrender.py`, `jambrender.py`).

### What Forza has (confirmed-static)

FH1 and FM4 carbins have no door or wing sections (all 40 testdrive cars:
`body`, `hood*`, `trunk`, `bumper*`, `wing*` = spoiler, `skirt*`, lamps,
glass, `mirror*`, `interior`; `out/dmg6/names.txt`). The per-vertex tail
table carbin-garage calls "damage / LOD remap" holds packed 10:10:10
vectors, not part ids (`tail.py`). But `body` is modelled in pieces: its
vertices are not shared across the real shut lines. Joining triangles
through shared vertex indices splits the M5's paint into front wing, front
door, rear door, rear quarter, roof and pillars, exactly along the shut
lines (`out/dmg6/m5-idx.png`); the S5S Raptor's door is one such component,
while its wing, sill and rear quarter are one shell (`sal-idx.png`).

### Rule (`part_components`, `part_stats`, `part_relabel`, `part_spans`, `part_trim`)

- After the host vote and smoothing, each component of a `nearest`-rule
  section (triangles joined through shared vertex indices, any material) is
  scored by the panel labels on it (area-weighted).
- A door or wing (`SWINGS`) is a Forza part when the components it wins
  with at least `PART_SHARE` (60%) of their area add up to `PART_MIN_AREA`
  (0.15 m2) and to at least 60% of the area the vote gave it, and its
  partner on the other side qualifies too.
- A part takes exactly those whole components; its other triangles go to
  the best other panel of their component, and a component won by another
  panel goes to that panel whole. Components on the part that lie mostly
  outside the length of its largest component (the S5S Raptor's intake
  surround behind the door) go to the chassis.
- Side-window glass within a part door's length and facing sideways goes
  with the door (the S5S Raptor's door and quarter windows are one Forza
  section; sunroofs and T-tops stay); `interior` triangles on the door's
  side, within its length and height, at most 0.2 m inward of its skin and
  facing the cabin are its door card and go with it, as vanilla door cards
  are skinned to the door records.
- Parts keep their own transform slot (`DamagePlan.bound`): they swing and
  come off like vanilla. Doors and wings that are not parts keep the PR #14
  binding (chassis slot, own damage index) and stay shut.

Behind an open door is what Forza models there: the door-opening trims
(jambs and sills, shared trim) and the cabin, seats and dashboard of the
`interior` section (`out/dmg6/jamb.png`, the M5 and S5S Raptor without
their doors). The door shows its card on the inside.

### Survey (40 testdrive cars, LOD 1; `out/dmg6/parts2-*.jsonl`)

- Swinging doors on 37 cars, swinging wings on 25. No part found (doors and
  wings held as in PR #14): BMW M3 E30 (host 205's door vote covers little
  of the Forza door), 300 SL (gullwing doors, no side door component), 250
  GTO (doors and wings in one shell with the nose). Wings held while the
  doors swing: RX-7, S2000, Charger Daytona, Durango, Express, S5S Raptor,
  Agera, LFA and Mustang; the Silvia's, 2000GT's and Huayra's hosts have no
  wing records.
- Length of panel border running through a Forza surface (two triangles
  sharing an edge on different panels, one a door or wing that swings):
  3.9 to 30.7 m per car with the host's panel layout (testdrive10, where
  all of them swung), 0 now on every car.
- Pinned rigid seams under front, rear and side crush, exposed-surface
  counts and `damage.py check` are unchanged (`out/dmg6/after-all40.txt`).

### In game (VM run `vm-20261024-080153`, `-Damage -DamagePanels`)

S5S Raptor (ID 1), M5 (ID 2) and S2000 (ID 3) of the test build: with
`forceOpenPanels` the doors open wide as solid doors with dark cards inside,
along their shut lines, like vanilla 266 (`host-20261023-093807` observe-16);
testdrive10's door patches swung as thin jagged arcs and master's (#14)
stayed shut (`img/damage-doorparts-forceopen.jpg`). `removeAllPanels`
removes them. No crash in the run; one start before it hit the known
world-load crash (`vm-20261024-074707`, before any converted car spawned).

Offline: `img/damage-doorparts-panels.png` (panels: swinging parts in
colour, held door/wing patches blue, M5, S5S Raptor, Grand Cherokee, 250
GTO), `img/damage-doorparts-ajar.png` (doors 0.15 rad ajar under a side
crush, testdrive10 left).

## Visible trims keep their trim (2026-10-26)

User report on `out/final4` (FER_355Challenge_95, FH1, spool 353 on host 125):
"a blurry panel on its back". In the chase view the black tail panel between
the tail lamps (with the F355 lettering and the horse) showed blurred light
shapes; in Forza (render of the vanilla sections with their atlases,
`out/inv/frender.py`; FH1 thumbnail 339) it is flat black.

### Cause (confirmed-static)

The panel is Forza trim (`body`: `black`, `frame`, `matte_colors`, and the
`bumperFrameR` frame below it). In the built exterior entry those triangles
were on the shared trim material, but not on `TRIM_UV`: `split_exposed` had
put them in the `bay` zone, and `DamagePlan` projected the shared chassis
texture's engine-bay pictures onto them (`out/inv/dsf_view.py`: red = trim off
`TRIM_UV`). The F355 is mid-engined, so its boot lid (`trunk`) is the engine
cover and the bay is everything below the cover's map; the tail panel sits
under the lid's rear edge (lid y 0.69 to 0.80, panel 0.50 to 0.69, both
ending at z 2.0 to 2.07). A picture a few centimetres wide stretched over a
0.5 m panel is the blur. The zones only test position against the panel's
map (under the cover, or 2 cm to 0.6 m inward of a bumper skin); nothing
asked whether the panel actually hides the triangle.

### Fix (`damage.split_exposed`, `_seen_zone_triangles`)

After the zone votes, the zone triangles of each LOD (not the panel
undersides, `inner`) go through `trim_art.seen_from_outside`: orthographic
depth buffers of the LOD's non-glass triangles from 8 directions level with
the car, 8 from 35 degrees above and one from straight above (as for "Trim
artwork" in `forza-materials.md`). A triangle that is seen keeps its trim, so
it draws as Forza's flat trim; hidden ones keep their zone pictures for when
the panel comes off. The build log counts them (`seen bay lod 1 357`).
`split_exposed(visible=False)` is the old behaviour.

### Survey (every roster car, `out/exp_survey.py`)

Zone triangles the test returns to trim, LOD 1: 577,066 triangles on all 573
cars, median 3.7 m2 a car (34% of the zone area; at most 9.2 m2, the SC430s,
Zonda, Sesto Elemento: open or skeletal bodies). LODs 2 to 5 alike (median 3.2
to 3.8 m2). What it is: grille backings and radiator faces seen through
grilles (the Carlton's front grille), wheel-arch liners, the scuttle under the
windscreen, the underbody seen from low, and recessed tail panels (the F355's,
`body/black` and `matte_colors` facing back). The F355 at LOD 1: bay 894 to
537 triangles, boot 1055 to 361, rear 264 to 39, front 88 to 43. The test
adds about 6 s a car (18 s at most).

`img/exposed-f355-before-after.png` (exterior entry from behind, red = trim on
projected pictures): the tail panel and the diffuser go back to flat trim.

### In game

Headless VM, the same five cars built from master and from this change
(`out/t/before`, `out/t/after`; ID 5 is the F355 Challenge, 1 the Carlton),
`-Views wheels,rear -Palette 0 -ShotSeconds 3`: runs `vm1-20261027-045825`
(before) and `vm2-20261027-050401` (after). From behind, the F355's tail panel
between the lamps is flat black with the horse and the lettering, instead of
the blurred grey bay pictures (`img/exposed-f355-ingame-before-after.jpg`).
Damage (`-Damage -DamagePanels`, IDs 1 and 5, run `vm1-20261027-054158`): full
visual damage, the F355's engine cover opened and swung off; the tail panel stays
flat black, no crash (`img/exposed-f355-damage.png`). The bay pictures under the
opened cover aren't readable in those shots.

## Mirrors that hang beside the car (issue #187, 2026-10-29)

User report on the 3ca28fd build: the Devon GTX (DEV_GTX_10, spool 328, host
182) once damaged, both side mirrors hanging outward beside the body; a player
reported the same on the Lancer Evo X (mit_lancerevoxgsr_08, host 136).
Scratch: `out/m187/` (`mirrors.py`, `survey3.py`, `simulate.py`, `fields.py`).

### Cause (confirmed-runtime)

The mirror's own rigging was right: each side on the host's mirror record
(roles 40/41), its rigid body, collision box and UPVH mirror-table point within
0.1 m of the Forza mirror, no crush (as vanilla). What was wrong is its
**parent record**. Vanilla mirrors are separate meshes on the door skin, and
their record hangs under the door's (253 of 277 vanilla mirror records; the
rest under the front structure, the chassis or the bonnet: 276, 167, 197, 246,
232...), so a door knocked ajar carries its mirror (vanilla 182 and 136 under
`fullVisualDamage`, run `vm1-20261029-011834`). Forza mirrors are often
mounted on the A-pillar or the sail ahead of the window, which the converted
car draws through the chassis (or on a door held shut, `DamagePlan.bound`):
the Evo X's mirror touches the chassis (92 vertices within 2 cm) and is 30 mm
from its door, the Devon's 18 mm from the chassis and 23 mm from its door.
Under the host's door record the mirror swung out with the door while its
mount stayed on the body. With `forceOpenPanels` the Devon's mirrors hang in
the air beside the car exactly as in the report (run `vm1-20261029-024106`).
On the DBR9 (host with both mirrors under the bonnet record) they went up with
the bonnet.

### Fix (`DamagePlan._remount_mirrors`, `move_subtree`)

- The mirror's mount is the panel with the most mirror points within 2 cm
  (`MOUNT_TOUCH`) at the finest full-entry LOD, else the nearest within 6 cm
  (`MOUNT_REACH`); the record to hang under is the one whose transform slot
  draws that panel (`bound`).
- `move_subtree` moves the mirror record and its marker (role 73/74) under
  it. Every vanilla hierarchy numbers its part records' transform slots
  (+0x34) and panel-data indices (+0x2A) in record order, and vertices carry
  those numbers, so the subtree goes only where that order holds, else the
  move is refused. A mirror under a bonnet after it (ARX-01b, slots 9 and 11
  under slot 17) crashed `forceOpenPanels` (run `vm2-20261029-030631`).
  When the mount's record can't take it in order, the mirror goes under the
  chassis: it then stays on the body when its mount (bonnet, door) swings,
  instead of leaving with a panel it isn't on.
- Three record fields follow the tree's shape (`_tree_fields`, `upvh.md`), and
  `move_subtree` writes them again for the new tree on every record that
  followed them before: +0x0C bit 0 (the record has a part child; all 10,959
  vanilla records), and +0x18/+0x16, the number of hinged records among a
  record and its ancestors whose subtree ends where its own does (on leaves
  but the list's last, also +0x16), i.e. how many hinge transforms the engine
  pops there (10,956 of 10,959; the rest are three headlights on hosts 210 and
  217; recounted in `upvh.md`, #201). Moves that left them stale crashed: a door with bit 0 and no mirror
  (Devon, `forceOpenPanels`, runs `vm1-20261029-024550`,
  `vm2-20261029-025144`, `vm1-20261029-025513`); bit 0 right but the +x
  mirror's old pop counts (ARX-01b under `forceOpenPanels`, run
  `vm1-20261029-033943`; Devon under `fullVisualDamage` alone, run
  `vm2-20261029-034112`). A door given a mirror without bit 0 didn't carry it
  (DBR9, run `vm1-20261029-025726`). `damage check` checks all three.
- Two mirrors under one record (DBR9's bonnet): the one with the higher slot
  has to leave first, so the rest are retried until none moves.

### In game (headless VMs; before = master, after = this fix, same IDs)

Seven test cars: 1 Devon GTX, 2 Lancer Evo X, 3 Sentra (held doors), 4 Miata
'94, 5 Civic Type R '04, 6 DBR9 '06 (both mirrors under its host's bonnet
record), 7 ARX-01b (mirrors on the bonnet, under door records).

- `-Damage -DamagePanels` (`forceOpenPanels`), IDs 7, 1, 2, 5, 6: before
  `vm2-20261029-055515`, after `vm2-20261029-060831`, both clean. The Devon's
  mirrors hang off the tips of its open doors before and stay on the body at
  the A-pillars after (`img/damage-mirrors-devon-forceopen.jpg`); the DBR9's
  ride up on the open bonnet before and stay on the body after
  (`img/damage-mirrors-dbr9-forceopen.jpg`).
- `-Damage` with left, right and rear views, IDs 1, 2, 3, 5, 6: before
  `vm1-20261029-055544`, after `vm1-20261029-061037` (shots through every
  car and view; the series hit the 30-minute background limit while it
  collected the run, so that folder has no `result.json` or `Driver.log`).
  No door swung ajar in these shots, before or after.
- The first build of the fix, with the same mirror placement but stale
  record fields (runs `vm2-20261029-020126` before, `vm2-20261029-021554`
  after, `-Damage` and views): where the Evo X's and Civic's doors hung ajar,
  their mirrors stood off the A-pillar sail before and sit on it after
  (`img/damage-mirrors-evox-civic-ajar.jpg`); the Sentra's and DBR9's alike.

### Survey (master build 3ca28fd, 566 built cars; `out/m187/sim-master.jsonl`)

`simulate.py` runs `_remount_mirrors` on each built car's hierarchy and full
entry. 469 mirrors on 266 cars move, all under the chassis in slot order:
340 from a door with the mirror on the chassis, 39 on the front structure, 24
on a held wing, 11 on a held door; 18 from a door with the mirror on the
bonnet; 37 from the bonnet record (16 on the chassis, 19 on a door, 2 on the
front structure). Left as before (issue #193): 40 mirrors on 24 cars. On 22 of them the
host's door record has a door-glass record after the mirror (AST_DB9_05,
BMW_Z4_11, MAZ_RX7_90, LOT_Elise111S_05...; on the Lotus 2-Eleven and the
Cougar Eliminator the mirror is on its door, which is held shut), so no place
under the chassis keeps both orders; the BMW V12 LMR's -x mirror can't leave
its bonnet record in order either, and the Radical SR8's two sit on its front
bumper but hang under the front structure ahead of its other parts. The other
276 cars have their mirrors under the right record already, no mirror panel,
or none within 6 cm of a panel. The 22 door-glass hosts are fixed since
(#193, next section).

### Door glass after the mirror (issue #193)

On those 22 cars (8 hosts: 129, 133, 146, 172, 175, 241, 258, 278) the
mirror's door record holds, after the mirror, a door-glass record (role 8):
DB9 (host 133) door -x slot 8 / index 10, mirror 9 / 11, the door marker
(role 67), glass with no slot / index 12, then door +x 10 / 13. Outside the
door the mirror alone had no place that keeps both orders. The converted car
draws nothing through these glass records: side windows go to their door's
panel or the chassis (`section_rule`), and no build report lists geometry on
a door-glass pair. The records hold a body and a collision box; on 172 each
holds a second glass record, on 175 two follow the -x mirror; on 146 and 278
(10 of the cars) they have a transform slot of their own, and on 278 a hinge
and a marker child.

Fix (`DamagePlan._blockers`, `_move_in_turn`): when the mirror can't go under
the chassis alone, the part records after it among its siblings go there first,
last one first, as long as none is a mirror, holds a record other than parts
and markers, or draws a panel through its own transform slot (`drawn`, every
pair given geometry in a LOD built so far). Each lands after the door's
subtree, then the mirror between the door and them, all in order. Markers stay
on the door. The report's mirror entry lists them under `"with"`.

Roster simulation (`out/m193/simulate.py`: `_remount_mirrors` on each built
car's vanilla host hierarchy with its build report's mounts, swinging panels
and drawn panels; master's rule reproduces the master build's report exactly):
292 cars with mirror entries, 473 mirrors moved and 40 kept before, 510 and 3
after. 37 mirrors on the 22 cars change, all to the chassis, no other car
changes, and every record's +0x0C bit 0, +0x16 and +0x18 follow the tree.
Still kept: the BMW V12 LMR's -x mirror (under its bonnet, slot 7, ahead of
the +x mirror that stays there) and the Radical SR8's two (on the front bumper,
slot 14, but under the front structure ahead of its headlights and exhausts).
Both need the mirror records renumbered (vertex s9 bytes 0 and 1 in every
entry), issue #269.

In game (headless VMs, `-Damage -DamagePanels`; before = master's rule, after =
this fix, same 24-car builds `out/m193/before` and `out/m193/after`, all runs
clean): IDs 1, 3, 7, 9, 11, 16 (DB9, Z4, Challenger, 2-Eleven, Elise, Cougar)
with left and right views, before `vm2-20261029-174814`, after
`vm2-20261029-175452`; IDs 1, 3, 7, 16 with a shot a second, before
`vm1-20261029-180206`, after `vm1-20261029-180536`. Under `forceOpenPanels`
the Z4's mirrors swing out with its open doors before and stay on the body
after; the 2-Eleven's, on doors held shut, stand off the body before and sit
on it after (`img/damage-mirrors-193-forceopen.jpg`); the DB9's stay at the
A-pillars beside its open doors after (observe-02). `render_compare diff`
(`out/rc/mirrors-193`): the 22 cars' data differ, none draws differently.

## Side doors 52/53 (issue #209, 2026-10-29)

`damage.py` called UPVH roles 52/53 "sill +x/-x" and held them like the
chassis (`BODY`, drawn through the chassis transform). In the vanilla
archive they are side doors with the doors' hinge (vertical axis, limit 0,
`dsff damage panels <vanilla> --id 231/198`): the Vista Cruiser's rear
doors (231: records 17/19, hinges at (+-0.93, 0.35, -0.22), just behind the
front doors' at z 0.81; it has no 54/55), the GMC Savana's two right-side
cargo doors (198: two role-53 records, hinges at (-0.97, 0.02, 0.37) and
(-0.94, 0.02, -1.09), opening opposite ways; 54/55 are its rear doors), the
Vandura's side door (200, no hinge) and the airport fire truck's lockers
(186). Under `forceOpenPanels` vanilla 198's side door swings out like its
other doors (run `vm1-20261029-113635`, observe-39/40).

Fix: 52/53 are "side door +x/-x" and members of `DOORS` (so of `SWINGS` and
`BODY` as before). They take part in the Forza-part test like the other
doors (a side door that is a Forza part keeps its own transform slot and
swings and comes off like vanilla; one that is not stays held), side-window
glass and door cards can go with them, and `patch_upvh` moves their hinge,
body and collision box as before. A Savana side door has no partner on the
other side (`_partner` finds no role 52), so each qualifies alone. The
mirror re-parenting (#187/#196) is unchanged: it hangs a mirror under the
record that draws its mount, and no Forza mirror is mounted on a side door
on these hosts (the Express's go under the chassis, from the front doors,
as before).

Roster: only CHE_Express2500Cargo_05 sits on such a host (198,
`host-picks.tsv`, before and after #208). Its build (`port_batch`,
default options) now has both right-side cargo doors as swinging Forza
parts ("side door -x" 0.88 and 0.61 m2 of whole Forza components, report
`shut_line_parts`; hinges moved 0.08 and 0.34 m onto the Forza doors), where
before they were held on the chassis. To see the Vista Cruiser's rear doors
on a converted car, AUD_A4Avant_04 was built on 231 (`--host 231`): both
rear doors become swinging parts (0.75 m2 each), held before. `damage check`:
0 problems on both.

In game (headless VMs, `-Damage -DamagePanels`, before = master, after =
this change, same builds otherwise), all runs clean:

- Express (ID 1) and vanilla 198: before `vm1-20261029-113635`, after
  `vm1-20261029-115305`. Under `forceOpenPanels` the after build's right-side
  cargo doors swing out past the body (seen nearly edge-on from the chase
  camera), as vanilla 198's do; before, nothing moved on that side
  (`img/damage-side-doors-express.jpg`: before observe-26, after observe-30,
  vanilla 198 observe-39).
- A4 Avant on 231 (ID 1) and vanilla 231, with the `wheels` view (left side,
  after `removeAllPanels`): before `vm2-20261029-132300`, after
  `vm2-20261029-132540`. Before, the front door is gone and the rear door
  stays on the body; after, both doors are gone, as on vanilla 231
  (`img/damage-side-doors-a4on231.jpg`: observe-01, -01, -07).

## Collision hull (issue #210, 2026-10-29)

The root record's collision model (PDL, 5 boxes: a bounding box with flags 0,
then the lower body, the cabin and the two deck slopes, tilted) follows the
host's part-0 box closely (Desanne: hull z +-2.50, body +-2.56). Until #210 a
port kept it, so it collided with its host's outline: the T440 truck
(11.6 m on the 8.4 m GMC C5500) registered hits 1.5 m inside its nose and
1.3 m inside its tail. `DamageOptions.hull` maps each primitive's centre by
its relative position from the host's box to the Forza body's and scales
its extents (`_map_collision`), and moves and scales the chassis record's
body and box like the other panels'. It is on by default now: bare
`--damage` (port_car, port_batch's default options) means `hull`;
`--damage panels` keeps the host's hull.

Both boxes leave out 0.2% of the vertices at each end per axis
(`trimmed_box`, `HULL_TRIM`): 8 stray vertices 0.4 m above the Spark's roof
(x 0, z -0.14) made its hull's top 1.64 m instead of 1.29. On the roster
the untrimmed box's top is 0.25 m or more above the trimmed one on 23
cars, most of them race cars (SC430, DBR9, Z4 GT3, R8 LMS: 0.44 to 0.47 m).

The mapped hull ends no lower than the host's own (`_floor_collision`):
every primitive whose lowest corner falls below the host hull's lowest
corner is cut back to it along its most vertical axis, its top kept. The
car is placed with its hubs at the host's hub height, so the hull clears
the ground at least as much as the host's hull would on that car. Mapped
onto low race cars' sills the hull's bottom came down to 5.1 cm above the
ground (R8 LMS; HKS Evo 5.2, R8 race car 5.7, Furai 6.7), lower than any
vanilla hull (8.2 cm), where a low car could catch on kerbs and crests.
The report's `upvh.hull.bottom_raised` gives the cut (R8 LMS 7.3 cm,
Furai 1.9 cm).

Kerb at speed (`-Dyno -DynoKeep none -DynoKeys 'Up:12,Down:6'`: with no lane
keeper the car drifts off the dyno straight; AUD_R8LMS_09 and MAZ_Furai_08,
`--damage panels` against this change, before `vm1-20261029-201030`, after
`vm1-20261029-201318`, both clean): the Furai leaves the road at about
100 mph 19 m to the left, crosses the kerb onto the pavement and brakes to
a stop there in both builds, with no step in its height or heading over
3 cm or 0.02 rad between updates. The R8 LMS drifted 13 and 6.6 m and the
shots don't show it reaching the kerb. Hill crests weren't driven.

### In game (confirmed-runtime)

Test cars (IDs 1-5): MAS_MC12_04 (long, 5.35 m on the 4.49 m Zonda, host
232), LOT_ElanSprint_72 (shorter than its host, 129), CHE_Spark_11 (small
car, longer than the Cygnet, 249), KEN_T440_12 (truck, 197) and
AUD_2_R10_08 (prototype, 232). Builds `out/hull/before` (`--damage
panels`), `out/hull/after` (the hull with untrimmed boxes) and
`out/hull/final` (this change).

Where contacts register: `tools/game/lua/dsfforza/ramtest.lua` (run with
`-Dyno`, put in place of `dyno.lua` through `-LuaFiles` as its header
says; the ordinary dyno runs follow) parks a copy of the car side-on 12 m
ahead on the dyno straight, pushes the car into it at 4 m/s (velocity set
every update), nose first, then tail first, and logs the distance between
the two centres along the car when the copy first moves (1 cm). That gap is
the car's hull extent ahead (or behind) plus the copy's half width,
predicted offline from the archive's hull (scratch `out/hull/extent.py` in
the #210 worktree). The car's speed is no use as the signal: `gameVehicle.velocity` reads change only at physics
steps (every ~5 Lua updates), and the 232-host cars lose 0.6 m/s a step
reversing (runs `vm2-20261029-185122`, `-190839`).

| Car | front gap: host hull (predicted) | mapped, untrimmed | final | rear: host hull | mapped, untrimmed | final |
| --- | --- | --- | --- | --- | --- | --- |
| MC12 | 3.114 (3.171) | 3.458 (3.518) | 3.472 (3.531) | 3.228 (3.234) | 3.618 (3.645) | 3.623 (3.651) |
| Elan Sprint | 2.713 (2.773) | 2.317 (2.371) | 2.279 (2.337) | 2.868 (2.905) | 2.466 (2.504) | 2.425 (2.462) |
| Spark | 2.305 (2.366) | 2.683 (2.746) | 2.675 (2.737) | 2.290 (2.333) | 2.420 (2.441) | 2.441 (2.454) |
| T440 | 5.362 (5.368) | 6.597 (6.617) | 6.630 (6.649) | 5.484 (5.494) | 6.689 (6.756) | 6.628 (6.691) |
| R10 | 3.113 (3.171) | 3.087 (3.142) | 3.126 (3.183) | 3.193 (3.234) | 3.286 (3.310) | 3.286 (3.320) |

(m, measured (predicted); the Forza body's own extents would give 3.665 /
3.724 for the MC12 and 7.018 / 6.995 for the T440.)

Runs (both archives in one series; all clean, no crash):
`vm1-20261029-191736` (before) and `vm1-20261029-192419` (after), final
`vm2-20261029-193826`. Contacts register 1 to 6 cm short of the prediction on every
car and hull alike, so the root hull is what car-to-car contacts use, and
mapped, it puts them at the Forza body's ends.

No sinking or hovering: the rest height of each car on the straight is the
same to 0.2 mm before and after (e.g. MC12 30.2759 / 30.2758, T440 30.1043
/ 30.1044). `gameVehicle.height` follows the hull's top (MC12 1.076 ->
1.087, Elan 0.971 -> 0.876, Spark 1.281 -> 1.404 (1.713 untrimmed), T440
2.849 -> 3.054). Handling doesn't change: 0-30 and 0-60 mph within 0.05 s and
braking within 0.1 g over three series (`vm2-20261029-185122`/`-185941`,
`vm1-20261029-191736`/`-192419`, `vm2-20261029-193826`; the MC12's run in
the last is spoilt, its start not found), as expected with mass,
inertia and CoG from the handling parameters (`handling.md`), not the
chassis record's body. `-Damage -DamagePanels -Traffic -DriveSeconds 10`
(before `vm1-20261029-185304`, after `vm1-20261029-185925`):
`fullVisualDamage`, `forceOpenPanels` and `removeAllPanels` ran on every
car, no crash, the drives in traffic look as before. `render_compare diff
--before out/hull/before --after out/hull/final` (`out/rc/hull-210`): 5
changed, 0 drawn differently (only the hierarchy differs).

### Roster survey (master build of 2026-10-29, 573 cars; scratch `out/hull/survey.py`)

Each car's host hull mapped from the host's part-0 box to the built car's
(both trimmed), against the host hull:

- The Forza body reaches more than 0.25 m past the host hull at the front
  on 73 cars and at the rear on 103 (the tour bus 2.65 / 1.48 m, the T440
  1.50 / 1.28 m, the Commodore VE race cars 0.85 m at the rear, the
  Thunderbird 0.63 m); past the mapped hull on 3 and 0.
- Hull extents change by more than 0.1 m at the front on 213 cars, at the
  rear on 282, on top on 139, in half width on 35 and at the bottom on 19
  (at most 0.17 m).
- The hull's bottom above the ground (the hubs' height less the tyre
  radius; scratch `out/hull/survey2.py`, the converter's own functions):
  mapped alone, 5.1 cm at least and 12 cars under vanilla's 8.2 cm; with
  the floor, 9.5 cm at least (the Furai, its host's own), 13 cm at the 5th
  percentile, median 20 cm. The floor cuts 214 cars' hulls, by 2.6 cm
  (median) to 13.4 cm.

## Scratches above the waist (issue #298, 2026-10-30)

User report (full-roster release candidate): after side damage, the black
BMW X6 M (BMW_X6M_10, host 239) showed large bright scratch patches over its
side and roof. Scratch: `out/d298/` (`scripts/sidearea.py` paint area per
damage index, `uvdens.py` UV1 density, `degen.py` where the one-texel UV1
sits, `survey.py` the roster survey below).

### The report itself is vanilla (confirmed-runtime)

The pixel shader (program 1, `Vehicles_Functions.hlsl` lines 191-256)
samples the decal at UV1 and fades the paint toward the decal's green
channel (bare metal) where the scratch mask's alpha plus the per-vertex
scratch amount drops below 1. The scratch amount is per panel and linear in
x, as the crush is (vertex shader `UnpackData_VS::fScratchOffset`). On a dark
paint the bare metal is bright whatever the car. With every panel at full
damage (`-Damage -Views left,wheels -Palette 0`, palette 0 is black on 239),
vanilla 239 (run `vm1-20261030-025250` observe-06, -07) shows the same
scrape marks on both doors and the rear quarter, the same size and
brightness, as the X6 M (`vm1-20261030-024423` observe-06, -07). The bright
"roof patches" in the user's shot are the sun's specular highlight on the
C-pillar and roof.

### What does differ: surfaces vanilla never scratches (cause confirmed-static)

Vanilla UV1 puts everything above the waist (pillars, window frames, roof,
spoiler, the tailgate band) and the hidden faces on one clean texel near
(0.98, 0.01): on 239, all paint above 1.0 m, 71% of the chassis panel's
side paint and 32-37% of each door's (`degen.py`, `uvdens.py`). `Uv1Field`
used those one-texel triangles only as a last resort, so every converted
surface took the nearest scratch tile instead; a vertex beyond the tile's
triangles took the closest point on its edge, which squashes the tile into
streaks. Survey of master's build (`survey.py`, full entry part 0 mask 4,
paint triangles whose UV1 is not on one of the host's clean texels): the
X6 M had 99% of its side paint on tiles (host 50%), 20% of it above the
height under which 98% of the host's tile area lies; its chassis panel had
2.9 m2 of side paint on tiles up to the roof (host 0.9 m2, all at 0.4-0.9 m).

### Fix (`Uv1Field.__init__`)

One-texel host triangles are candidates like the rest (same panel and
material first, the normal-mismatch penalty as before); only tiled UV1
(outside 0..1) stays a last resort. `_split_seams`, added after the
last-resort rule, already puts a Forza triangle that spans a tile and the
clean texel on one of them, which was the reason for keeping them back.

| X6 M (full entry) | master | fix | host 239 |
| --- | ---: | ---: | ---: |
| side paint on tiles | 99% | 68% | 50% |
| side paint on tiles above the host's line | 20% | 4% | 0% |
| chassis panel side paint on tiles | 2.86 m2 | 0.85 m2 | 0.92 m2 |
| lamp area on tiles (broken-lamp tile) | 0.380 m2 | 0.380 m2 | 0.246 m2 |

The survey's simulation of the lookup on master's vertices matches the real
builds (X6 M: simulated 100% / 20% for master, 69% / 4% for the fix,
against 99% / 20% and 68% / 4% built).

Roster survey (`survey.py`, all 573 cars of master's `out/build` against
their hosts; paint triangles by facing, shares of area on scratch tiles;
"above the line": side paint on tiles above the height under which 98% of
the host's tile area lies; medians over the roster):

| Full entry (exterior in brackets) | hosts | master | fix |
| --- | ---: | ---: | ---: |
| side paint on tiles | 59% (67%) | 96% (87%) | 80% (80%) |
| side paint on tiles above the host's line | 0% (0%) | 8.7% (6.8%) | 1.7% (1.5%) |
| roof-facing paint on tiles | 53% (51%) | 97% (82%) | 55% (54%) |
| front- and rear-facing paint on tiles | 49% (47%) | 91% (77%) | 68% (65%) |
| cars with > 5% of side paint above the line | | 463 (375) | 78 (73) |
| cars with roof-facing tiles > host + 20 points | | 503 (388) | 33 (36) |

Every car changes. The cars left with the most side paint above the line
are low or open cars on taller hosts (250 Testa Rossa and GT40 on 248: 25%
and 23%; DBR1 on 132, Elise on 129, Pantera on 161, Celica and tC on 258:
14-18%); not looked into (hypothesis: an open or low body puts the
host's line, a share of the box height, in the wrong place on it).

### In game (headless VM, `-Damage -Views left,wheels -Palette 0`)

Master's converter (`out/d298/set-before`, run `vm1-20261030-024423`)
against the fix (`out/d298/set-after`, run `vm1-20261030-024752`), IDs 1
X6 M, 2 Express 2500 Cargo, 3 TT RS, 4 Range Rover 2012: the 2012 Range
Rover's tailgate band, spoiler and roof edge lose the grey scratch wash
(observe-25), as vanilla 239's tailgate stays clean at full damage
(`vm1-20261030-025250` observe-05). The doors, quarters and sills of all
four look the same before and after (observe-06/07, -13/14, -19/20,
-25/26): those were already on the host's tiles. The side hits of
`-DamageSide` (runs `vm1-20261030-023246`, `-023624`) mostly crush the
near front corner and showed no scratch difference at that distance. No
crash in any of the runs.

## Risks and open questions

1. Crush, panel swing and removal (`-DamagePanels`), decals and seams have
   run in game without a crash (runs above). If the engine expects
   something else of the panel parts (for example that only some panels
   have geometry), a converted car could still misdraw in a case not run.
2. Body triangles are assigned whole, so door and wing borders follow the
   Forza mesh's triangles (jagged at LOD 3 to 5), and they follow the
   host's panel layout, not the Forza car's shut lines (the S5S Raptor's
   "door" covers the rear quarter's intake). Converted doors and wings
   swing only where they are Forza parts ("Doors and wings as Forza parts");
   the rest are drawn through the chassis transform and stay shut;
   seams between rigid panels on paint are pinned, so they don't
   open, but a door dent stops short of the borders, and the crush near
   them is lost (mean rigid-panel crush down by a median 28%, at most 59%). Trim seams the host
   moves (wing against front structure on 163) still open with differing
   damage amounts, as vanilla under-bonnet borders do. On a door that is a
   Forza part, its card (`interior` triangles next to the skin) goes with
   the door; on a held door the card stays on the chassis, while vanilla
   door cards are on the door, so a deep door dent can push the skin
   through the card.
3. The crush field is the host's, mapped by box. A Forza car much longer or
   shorter in the nose than its host crushes in the host's proportions.
4. The interior entry's outer shell still has s12 = position and chassis
   panels (cockpit work).
5. Lamp light entries hang under the host's records (lights work,
   `lights.md`); a lamp on a bumper that falls off may leave its light
   behind (hypothesis; #146 tracks rear lamps on front light entries).
6. The collision hull (default since #210) is the host's five boxes mapped
   by relative position, so it keeps the host's shape (cabin box, deck
   slopes) on the Forza car's box; a Forza car shaped very differently from
   its host (a cab-over truck on a bonneted host) still collides with the
   host's profile. Not run in game: car against a wall, and crests.
7. Exposed geometry is found by geometry (under a cover, behind a bumper
   skin, facing away from the panel's paint). Trims outside every panel's
   map (walls above a cover's lowest edge, wheel-arch liners, the RX-7's
   slam panel ahead of its short bonnet, the Agera's frunk walls), or
   panels not named `hood*`/`trunk*`/`bumper*`, keep the dark texel. Forza
   models the structure behind panels coarsely (the M5's "boot" is one
   rear-facing wall 0.5 m behind the lid, no floor), so it catches less sun
   than a vanilla tub and still reads darker than vanilla in game.
