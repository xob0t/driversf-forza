# Wheels: the Forza wheel model in DSF

Updated: 2026-10-31

User report from driving the testdrive build (`out/testdrive`, 40 cars): the
wheels look broken on every car, the Veyron worst. The first sections are
offline (no game run); the later ones have in-game runs. Tools of the first: `out/wheels/render.py` (exterior plus the wheel
part(s) placed the way the engine does it: hub + quaternion x wheel-local
point), `out/wheels/tyre_uv.py`, `out/wheels/tex_dump.py`. Evidence levels
follow `dngvehicles.md`.

## Vanilla DSF wheels (confirmed-static)

- One wheel model per MODL part (33 hosts keep front and rear in two parts),
  in its own frame: hub at the origin, spin axis x, tyre centred on x = 0.
- The UPVH wheel table gives each wheel a hub position and a quaternion:
  identity on the +x wheels, (0, 1, 0, 0) (180 degrees about y) on the -x
  wheels. One model serves both sides, so it is authored for the +x side:
  centre cap, spoke disc and blur disc sit at x +0.05 to +0.12 (227, 266,
  189).
- Materials: `GLOBAL_Tyre` (stride 20; on the Desanne the whole wheel, rim
  included), `GLOBAL_Wheel_Blur` and `GLOBAL_Wheel_Blur_SpokeDisc`
  (alpha-blended speed blur; vertex semantic 9 byte 3 is 0 (fades out), 1
  (always) or 2 (fades in) on them: "Speed blur"), sometimes a centre cap on the
  car's Chassis material. All tyre-texture UVs point into one shared
  512 x 512 DXT1 atlas (`00122b7bf7fa1227`): red is the grey level, green the
  gloss, blue 255 (so it decodes blue). It holds sidewall half-rings, tread
  strips and uniform grey/gloss swatches in the corners; the Desanne's rim
  uses the swatch at (0.906, 0.008) (grey 131, gloss 190).
- No brake discs or calipers in vanilla wheel parts.

## The Forza wheel (confirmed-static)

- One `wheel` section in the main carbin, same frame (hub at the origin,
  spin axis x, centred), but the rim face (spokes, lug nuts, `blur_rim`,
  `blur_lip`, `chrome_blur_lip`) is at -x, the car's left in Forza space.
- Sized for the front axle: model radius and width match `Data_Car`'s front
  tyre (370Z 0.339 / 0.3395 m, Veyron 0.333 / 0.3335, M3 0.305 / 0.3045);
  Forza scales it for the rear (Veyron rear 365/25 R21: 0.716 m by 0.365 m
  against 0.667 by 0.265 at the front).
- `tire`, `rim`, `inner_rim` UVs tile (scale x31) into Forza's shared wheel
  textures, which are not in the car's zip. Brake rotors and calipers are
  separate carbins (`<car>_rotorLF_LOD0.carbin`, `_caliperLF_...`); the rotors
  are converted since #212 ("Brake rotors"), the calipers not.
- Exceptions on the discs (full-roster survey, all 573 main carbins):
  - six main carbins have no wheel section: FER_360_99, FER_360Challenge_03,
    FER_Dino246_69, LAM_Gallardo_05, PAN_Esperante_05 and
    CHE_CORVETTEGSC_10. Their wheel is only in the wheel archive's carbin
    (`media/wheels/<car>`, one section `wheel`, plus LOD 0 with a skinned
    pool);
  - the two 1965 Minis have `wheela` and `wheelb` instead, like upgrade
    variants.

  `forza2dsf.load_car` takes `wheel`, else the vanilla variant (`wheela`, which
  is kept out of the body), else the `wheel` section of the wheel archive's
  carbin. `port_car.extract_wheel` caches that carbin as `wheel.carbin`.

## What was wrong (all hosts)

| Defect | Cause | Fix (`forza2dsf.py`) |
| --- | --- | --- |
| Every wheel showed the back of its rim outwards | the Forza wheel was copied unturned: face at -x, and DSF's quaternions put a model's +x side outwards on both sides | `wheel_point` / `wheel_normal`: 180 degrees about y, like the body |
| Tiled garbage on tyres and rims | Forza's tiling UVs sampling DSF's tyre atlas | `wheel_role` + `WHEEL_UV`: one uniform swatch per part: rubber (0.5, 0.008), rim grey (0.906, 0.008, the Desanne's), inner rim dark (0.4375, 0.055), chrome (0.117, 0.008) |
| Flat discs and broken rings over the rims (Cuda, Veyron) | blur geometry kept: only names starting with "blur" were dropped, `chrome_blur_lip` wasn't | everything with "blur" (and shadows, alpha-cut emblems) is dropped |
| Veyron, Huayra, Gallardo rear wheels too small and narrow, rear track wrong | one front-sized model on all four hubs, front track for both axles | `axle_tyres` (gamedb `Data_Car` tyre sizes): on hosts with separate front/rear wheel parts the rear copy is scaled (Veyron 1.38 wide, 1.07 radial); rear hubs go to `RearTrackOuter` (`CarDims.rear_track_outer`) minus the rear tyre width, and up by the radius difference so all four tyres stand on one ground line (`patch_hubs` rear_half_track / rear_hub_y; `HostWheels.axles` maps each part to its axle) |
| Wheels off the arches by up to 0.43 m (Cuda, Mustang, Veyron) | `BottomCenterWheelbasePos.z` sign (fixed by the damage work in `car_dims`, see `forza-sources.md`) | not part of this fix |

On hosts with one wheel part for all four (most), the rear wheels stayed
front-sized; the rear hubs still went to the rear track. Since 2026-10-26 such
a host gets a rear copy of its wheel part when the tyres differ ("Rear wheel
parts" below).

## Test archive

`out/wheels/wheels-test.sp` (`port_car.py --mdxn-limit 0x2C0000 --damage`;
lint clean, `chnk.py rebuild --verify` identical):

| ID | Car | Host | Wheel parts (full / exterior) | Rear |
| ---: | --- | --- | --- | --- |
| 304 | BUG_VeyronSS_11 | 214 Lamborghini LP670-4 SV | 32 front, 33 rear / 4, 5 | scaled 1.38 x 1.07, half track 0.819 (front 0.867), hub 0.025 higher |
| 305 | BMW_M3E30_91 | 205 Jaguar E-Type | 29 / 5 | same size, half track 0.714 (front 0.707) |
| 306 | CHR_Cuda426_71 | 172 Challenger R/T 1970 | 35 / 5 | same |
| 307 | NIS_370Z_10 | 227 Nissan 370Z | 29 / 4 | front-sized (one part), half track 0.805 (front 0.800) |

Renders (before = `out/testdrive`, after = this archive):
`out/wheels/sides-before-after.png` (both sides of each car, wheels at their
hub poses), `out/wheels/closeups-before-after.png` (front wheels from
outside, -x and +x side), `out/wheels/local-before-after.png` (one wheel
model in its own frame from +x, -x and the front). Offline renders use the
decoded tyre texture, so tyres and rims look blue; in game the red channel
is the grey level.

## Tyre texture and wheel LODs (2026-10-23)

User report on `out/testdrive3` (40 cars): tyres are plain, without the
tread and sidewall detail of vanilla tyres; the RX-7's wheels are pale grey
blobs with no rim, apparently bulging past the arches.

### Vanilla tyres (confirmed-static, 30 hosts: `out/wheels2/vanilla_side.py`)

`GLOBAL_Tyre` has only a diffuse map (`g_texDiffuseMap` 00122b7bf7fa1227
and a cube map); no normal map, and the stride-20 wheel vertices have no
tangents. All the tyre detail is in the diffuse atlas, reached through UVs
(`out/wheels2/tyre_r.png` is the red channel, the grey level):

| Region | UV | Vanilla use |
| --- | --- | --- |
| Lower sidewall half-ring ("ZRT PLUS", arrows) | centre (0.5, 1.0), radius 0.31 to 0.50; lettering band 0.33 to 0.39, ridge at 0.40 | every surveyed host's sidewalls, both faces. 180 degrees of tyre cover the whole half-ring (the full circle is mapped onto it twice); rim edge at ring radius 0.33 to 0.40, tread edge at 0.40 to 0.50 |
| Fine tread strip | u 0.508 to 0.757, v 0.89 to 1.0 | tread: u across the tyre (0.527 to 0.73, shoulders 0.503 to 0.526), v one tile (0.897 to 0.989) per segment of 14 to 25 degrees, repeated round the tyre |
| Upper half-ring ("UBISOFT TUBELESS"), block tread, rim and nut pictures, gradient swatches | | rims, centre caps and barrels of some hosts; no surveyed host's tyre |

### What was wrong

| Defect | Cars (of 40) | Cause |
| --- | --- | --- |
| Plain tyres | all | `WHEEL_UV["tyre"]`: every tyre vertex on one uniform texel (0.5, 0.008) |
| RX-7: grey blob wheels, no rim | MAZ_RX7_97, HON_CivicR_04 (exterior entry, LOD 1); BMW_M3E30_91 (Forza LOD 2, not used by the default LODs) | that Forza wheel LOD isn't made of `tire`/`rim`/`blur_*` but of `body` (tyre and rim in one mesh) plus `interior` (the lip ring, same UV transform as `blur_lip`). `wheel_role` put both on the rim-grey swatch, so tyre, rim and the blur ring all came out one pale grey, which also makes the tyre read as part of a bigger wheel. Placement and size are right (tyre radius 0.316 from LOD 2, same as LOD 1) |
| No wheels in the full entry | VW_GolfR_10, DEL_DMC12_82 | their wheel has LOD 1 only; the full entry asks for LODs 3/4/5 and got nothing (wheels vanish when the full entry draws: damage, distance) |
| One grey mesh at LOD 5 | DOD_Durango_99, CHE_Express2500Cargo_05 | LOD 5 is one `wheel_LOD5` mesh |

Survey: `out/wheels2/tyremap_check.py` (all 40 cars, wheel materials per LOD
and the new mapping).

### Fix (`forza2dsf.py`)

- `wheel_lod`: a wheel LOD without a `tire`/`tyre` subsection is replaced by
  the nearest one that has it (finer on a tie): RX-7 and Civic exterior
  1 -> 2, Golf R and DMC-12 full 3/4/5 -> 1, Durango and Express 5 -> 4.
  `convert` logs the swaps.
- `TyreMap` unwraps the tyre onto the vanilla regions: triangles whose face
  normal is within about 55 degrees of the axle go on the sidewall ring
  (ring radius 0.33 at the tyre's inner radius to 0.492 at its outer, 180
  degrees of tyre per half-ring, lettering included), the rest on the fine
  tread strip (u across the tyre's width, v one tile per ~0.09 x width/0.24 m
  of circumference: 8 to 31 tiles). Tile edges snap to the model's own vertex
  angles so no triangle straddles a seam. Rims keep their swatches.

### Test set (offline-checked)

`out/wheels2/build/dngvehicles.sp` (`port_batch.py build --car MAZ_RX7_97
--car BMW_M5_12 --car VW_GolfR_10 --car DOD_ChargerDaytona_69@fm4 --port
"--mdxn-limit 0x2C0000 --damage"`; all four lint ok): IDs 1 RX-7, 2 M5,
3 Golf R, 4 Charger Daytona (FM4). The Golf R's full-entry wheel part went
from 0 triangles per LOD (testdrive3) to 2863.

Renders (`out/wheels2/render.py <archive> <id> <prefix> [kind] [mask]`, the
tyre atlas shown grey): `out/wheels2/local-before-after.png` (each wheel
model from outside, inside and the front; testdrive3 against wheels2, vanilla
370Z for reference), `out/wheels2/after/id3-full4-sides.png` (Golf R full
entry, mask 4).

No in-game check of the unwrap itself is recorded; every later build carries
it. The planned check (its `-LuaFiles` is no longer needed: an archive named
`dngvehicles.sp` brings its Lua since #184):

```powershell
.\tools\wsb\run-series.ps1 -Archives out\wheels2\build\dngvehicles.sp -LuaFiles 'dsfforza\ported.lua=out\wheels2\build\dngvehicles.lua' -ProbeIds '1,2,3,4' -WaitSeconds 900 -ShotSeconds 3
```

## Rim textures (2026-10-24)

User report on `out/testdrive9`: every wheel disc is plain silver; the 250
GTO's wire wheels, spokes and hub show no texture.

### Where Forza's rim textures are (confirmed-static)

Every car of both games has its own wheel archive beside the cars:
`media/wheels/<car>.zip` on the FH1 and FM4 discs (298 and 443 zips, with
aftermarket wheels and tyres), `media/wheels/<car>/` in the disc 2 pack zips.
All 40 testdrive cars have one (`out/rims/wheels/index.json`). It holds:

| File | What |
| --- | --- |
| `<car>.carbin` | the wheel model: LOD 1 to 5 identical to the main carbin's `wheel` section, plus LOD 0 |
| `wheel.xds` | what the rim techniques sample at LOD 1+: 256² (36 cars), 512² (Veyron, 300C), 128² (Durango, Express); DXT1 or DXT5 |
| `wheel_LOD0.xds` | the LOD 0 rim texture |
| `wheel0..3.xds` | the spinning-blur discs (pictures of the whole wheel) |

`wheel.xds` is spoke shading, the centre cap with the maker's logo (Nissan,
Audi rings, Bugatti EB, SVT, STI, the Borrani knock-off on the 250 GTO),
nuts and the barrel; on DXT5 textures alpha is a gloss mask (0 on most rim
texels, 255 on chrome), not a cut-out. Drawing the 40 cars' rim UV triangles
over it lands them on that artwork. Techniques on the `wheel` section
(`out/rims/wheel_survey.txt`): `rim` (all 40), `inner_rim` (all; a swatch on
the 250 GTO and S5S), `chrome_rim` (8), `wheel_emblem` (alpha-tested, 5),
`tire` (tiling, global tread texture), `blur_rim`/`blur_lip`/`chrome_blur_*`.
The 250 GTO's wire spokes are geometry (2445 `rim` triangles at LOD 1); no
car's rim relies on an alpha cut.

### What was wrong (all 40 cars)

`wheel_role` put every rim technique on the shared tyre material with one
texel of DSF's tyre atlas (`WHEEL_UV`: rim grey, inner dark, chrome): plain
silver discs, no caps, logos, nuts or rim colours (the Veyron's orange, the
WRX's gold, the Agera's black with stripes).

### How vanilla DSF wheels are textured (confirmed-static)

The wheel parts of all 140 four-wheeled vanilla vehicles use `GLOBAL_Tyre`
(tyre and most of the rim, on the shared atlas's rim and nut pictures), most
also the two blur materials; 109 of them also have triangles on the
vehicle's own Chassis material (stride 28; 108 in both entries, 4 to 1,184
triangles over the exterior wheel parts' LODs): centre caps and logos from
the car's chassis texture. Five more (180, 197, 198, 231, 233) use a stride-32
material of their own instead. (Recounted 2026-10-29 over the vanilla
archive, offline-checked; the first survey, `out/rims/vanilla_wheel_mats.txt`,
is gone and said 110.)

### Fix

- `port_car.extract_wheel` puts `wheel.xds` into the car's cache folder (its
  own stamp, `wheel_source.json`).
- `forza_materials.classify_wheel`: rim techniques are textured from the new
  family `WHEEL`; the tyre stays `TYRE`, blur discs `SKIP`.
- `dsf_materials.Texturing.plan(wheels=(sections, lods))` packs the used part
  of `wheel.xds` into the car's chassis atlas next to nodamage and Lights
  (weight 1, a 512² wheel texture weighted to 256²). Replace mode keeps the
  host's texture size, so entry sizes don't change.
- `forza2dsf.wheel_material`: rims, caps and emblems use the entry's Chassis
  material (full entry) / Chassis_LOD1 (exterior), the tyre the shared tyre
  material with the vanilla unwrap; the swatches remain the fallback when there
  is no wheel texture or `--textures none`.

Atlas cost (`out/rims/atlas_scales.txt`, 40 cars against `out/testdrive10`):
one region per car, the whole wheel texture. The uniform scale of the other
regions stays on 15 cars (slack in the packing) and drops by 4% on average,
16% at most (Mustang Boss 429 0.77 to 0.65, S2000 0.87 to 0.75). Entry sizes
and the LOD choices of all 40 cars are unchanged; all 40 lint ok.

Survey (`out/rims/wheels_all.png`, all 40 cars: Forza's wheel with
`wheel.xds`, `out/testdrive10`, the fixed build `out/rims/td`, exterior wheel
part from outside): every converted wheel now matches its Forza wheel (caps,
logos, nuts, the Veyron's orange, the WRX's and Firebird's gold, the Agera's
black with its rim stripes, the Focus RS500's black); before, all 40 were the
same silver. The full entry (LOD 3, `out/rims/wheels_full.png`) the same.
`img/rims-before-after.png`.

In game (confirmed-runtime, headless VM; fixed cameras on the car's left
side, outside and at each wheel): `vm-20261024-065630` (`out/testdrive10`)
against `vm-20261024-064427` (`out/rims/td`), IDs 147 (vanilla CTS-V for
reference), 25, 38, 28, 6, 29. The rims draw their own artwork through the
Chassis material: the 250 GTO's wire wheels with the knock-off cap, the
Raptor's dark rims with the SVT cap, the Veyron's orange (a little lighter
and redder than Forza's), the WRX's gold, the Agera's black rim with its
yellow and blue stripes; no flicker or missing triangles, brightness next to
the vanilla CTS-V's wheels alike. `img/rims-ingame-before-after.jpg`.

## Wheel shading (2026-10-24)

User report on `out/testdrive11` (ID 18, PEU_205T16_84 on host 274): "plain
white wheel disks". Scripts in `out/inv/` (`wrender.py`, `wobl.py`,
`vanilla_sky.py`, `vanilla_sky_agg.py`, `sky_check.py`).

### The 205's wheel is white, and the texture is applied (confirmed-static)

- `wheel.xds` (256² DXT5) is near-white everywhere: shading of the disc, the
  12 holes, the diamond recess and the nuts. FH1's thumbnail
  (`Thumbnails/Thumbnail_1533.xds`) shows the car on off-white wheels with
  dark holes; the road car's are white too.
- The converted exterior wheel part, drawn with the build's own atlas, matches
  Forza's wheel with `wheel.xds` texel for texel (`out/inv/205_wheel_cmp.png`):
  the UVs and the atlas region (0.551x) are right. Forza's wheel archive holds
  no shader settings for the rim; nothing tints it. (Wrong: the shared
  defaults scale every `rim` texel by 0.75, "Rim brightness" below.)

### What differed: the baked sky term (s0.w)

Forza's wheel vertices have no AO byte (0 on every vertex of the 40 testdrive
cars' wheels), so `vertex_sky` gave them `DEFAULT_AO` x the hemisphere factor
of the wheel-local normal. Vanilla wheels bake their own occlusion. Exterior
wheel parts of the 140 four-wheeled vanilla vehicles (median of per-vehicle
medians), against `out/testdrive11` IDs 18, 25, 6:

| Surface (DSF wheel frame) | Vanilla | testdrive11 | Fix (40 cars) |
| --- | --- | --- | --- |
| outward faces (normal +x) | 0.64 (tyre material), 0.635 (chassis caps) | 0.42 | rim 0.56, tyre 0.61 |
| inward faces (-x: back of the disc, inner sidewall) | 0.02 | 0.42 | 0.00 |
| facing the axle (inside of the barrel) | 0.15 | 0.42 | 0.24 |
| tread (facing out radially) | 0.29 | 0.42 | 0.29 |

With one value everywhere the inside of the wheel lit like its face: through
the 205's holes the camera saw the white barrel, and the disc read as flat.
In Forza the brake rotor, hub and caliper sit behind the holes
(`<car>_rotor*_LOD0.carbin` and `_caliper*`) and the inside of the wheel is in
shadow, so the holes are dark (`out/inv/205_wobl.png`: oblique views without
and with the rotor and caliper).

Fix (`forza2dsf.wheel_sky`, `wheel_skies`): each wheel vertex gets
`WHEEL_SKY_OPEN` (0.64, vanilla's outward faces) x the share of 24
cosine-weighted rays over its hemisphere that escape. Rays are marched through
a 3 mm voxel grid of the wheel's own triangles at that LOD (hits closer than 3
voxels are the vertex's own surface); rays heading inboard end on the car, and
rays leaving past the tyre's radius count 0.5 (arch above, road below). The
voxel march agrees with exact ray-triangle tests to 0.03 on average
(correlation 0.97, the 205 at LOD 1) and takes about 0.4 s per wheel LOD
instead of 10 s. The tyre texture, UVs and materials are unchanged.

In game (confirmed-runtime, headless VM, new probe view `wheels`: the car's
left side at hub height): `vm-20261024-110508` (`out/testdrive11`, ID 18)
against `vm-20261024-105751` (`out/fx/b2`, ID 1; vanilla Scirocco 274 in the
same run). The 205's holes and the recess round the nuts now read dark, as
through the vanilla Scirocco's spokes; the disc itself stays white, as Forza's
is (`img/wheels-205-ingame-before-after.jpg`).

The brake rotor behind the holes is converted since #212 ("Brake rotors").

## Wheel clearance (2026-10-25)

User report on the full-roster build (`out/final-gui2`): the #2 Audi A4 DTM
(`AUD_2_A4_11`, FM4, ID 73 on host 162) stands with its front tyres through
the front wing flares, the rears looking off too. Tools: `dsff
wheel_clearance` (`src/dsfforza/convert/wheel_clearance.py`), scratch
scripts in `out/wc/` (renders with the probe's cameras, the in-game rest
pose, variants).

### How a port's wheels are placed (confirmed-static)

- `forza2dsf.convert` puts the Forza body so that Forza's ground (its
  `StockRideHeight` under `BottomCenterWheelbasePos`) is a tyre radius under
  the host's own hub height, and moves the hub table (`patch_hubs`) to the
  Forza wheelbase and track outer. Before this fix the radius and width were
  the wheel model's own (`tyre_size`), the rear copy scaled to Data_Car's rear
  tyre only on hosts with separate rear wheel parts.
- The engine does not draw a wheel at its hub-table position. With c the
  wheel's suspension compression, FUN_0088D620 (called from FUN_00874EA0, the
  per-wheel draw state, on wheel+0xE4) gives the visual offset

      up = clamp(c * (1 - p1 / p469) - sag - p25, p292, p188)

  with sag = max(p4, 17 m / (100 p1)) from FUN_0088D750 (p140 = -17, four
  wheels, the constant 25 at 0x00DD6970). At rest c is where FUN_008BA900's
  spring force meets the axle's load: k = p1, times p505 on the rear axle
  (the wheels whose flag +0x15C is 0), no force at all below p4 and a ramp to
  full force over the next 0.1 m (0x00F3F2E8), progression p507, 5x beyond
  p3. So a VO3D with a ride offset p25 or a dead zone p4 draws its wheels
  higher (or lower) at rest than the hub table, and the body settles by that
  much: on the A4 (p4 0.05 and p25 -0.02 from the Alfa 8C suspension set,
  1050 kg on p1 2100) 39 mm at the front, 33 at the rear. Over the vanilla
  fleet the offset is small (median +4 mm front, -8 mm rear), and Reflections
  modelled each vanilla body around its own rest pose anyway.
- Forza draws its single wheel model at each axle's Data_Car tyre size
  (`FrontTireWidthMM`, aspect, rim inches): 160 of the 573 wheel models are
  more than 5 mm off their front tyre's radius (88 bigger, up to
  FER_F151_11's 0.393 m against 0.340; 72 smaller, down to SUZ_Liana_02's
  0.248 against 0.298). Forza's thumbnails (`media/UI/Textures.zip`) show
  the tyre size: the F151's tyres fit their arches, the Liana's fill them
  (`out/wc/thumbs/`).
- 35 wheel models sit more than 10 mm off their origin (the tyre's bounding
  box): the five Commodore VE race cars 300 mm along z, TOY_6_Supra_05 49 mm
  out and 80 mm down, MER_ML63AMG_09 and VW_BEETLE_68 39 mm across. The port
  turned them about the origin, so those wheels hung off their hubs (the
  Commodore's in front of its bumper) and wobbled as they turned. Centred on
  the hub, the Commodore's tyres still stood 0.3 m off their arches: see
  "Wheels off their arches" (#301).

### The check (`dsff wheel_clearance`)

Each wheel of a built archive is a cylinder with the drawn part's radius,
tread width and centre; its signed distance to the clean exterior entry's
body, over the tyre's outboard half (the liner over the inner half is hidden
by the tyre; body points inside 0.75 R are hubs and calipers), at `design`
(the hub table), `rest` (+ the rest offset above), `bump` (p188), `lock`
(rest, steered p298 both ways) and the Forza reference: Data_Car's tyre at
Forza's own static pose, and that tyre at Data_Car's `SteerMaxAngle`.
Negative is a tyre through the body. `--as-fixed` measures an archive built
before this fix as the converter now builds it, so the old full-roster
archive can be surveyed without converting 573 cars again; on the ten cars
rebuilt both ways it agrees with the rebuilt cars within 3 mm.

### What was wrong (the survey, all 573 cars of `out/final-gui2`)

Cars with a tyre more than 10 mm deeper in the body than Forza's own tyre is
in the same pose (before: the build as made; after: --as-fixed):

| Pose | Before | After |
| --- | --- | --- |
| at rest | 74 of 573 (median 20 mm, worst FER_F151_11 91, JAG_33_XKR_10 60, MAS_MC12_04 60, NOB_M600_10 57, LAM_LP7004_12 56) | 1 (NIS_3_GTR_08 15 mm, an artefact of --as-fixed: rebuilt, it matches Forza) |
| at full lock (p298 against Forza's SteerMaxAngle) | 110 (median 20 mm, worst 77) | 41 (27 of them have a larger lock than Forza: 40 or 45 deg against 30 or 36) |
| rest gap off Forza's by more than 10 mm either way | 372 (217 tighter, 155 looser) | 69 (6 tighter, 63 looser) |

The rest offset itself, all 573 (mm, deciles): front -15 -6 1 5 10 12 15 21
28, rear -21 -16 -10 -5 -1 0 4 11 18; 441 cars are off by more than 10 mm on
an axle, 68 by more than 30, KEN_T440_12 (host 197, p25 -0.13) by 149. The 63
"looser" cars that remain are on hosts with one wheel part for both axles and
a bigger Forza rear tyre: their rear wheels are drawn at the front size (below).
At full bump (p188) about 500 cars put a tyre in the body before and after;
Forza's own MaxCompressHeight would too (the A4: 60 mm above its ride height
in Forza, 36 in DSF after the fix), so that isn't counted as a fault.

Ten cars rebuilt both ways (out/wc/before3 with origin/master,
out/wc/after3; gap in mm at rest, front / rear, Forza's in brackets):

| Car | Before | After |
| --- | --- | --- |
| AUD_2_A4_11 | -49 / -9 (-12 / 14) | -12 / 13 |
| FER_F151_11 | -72 / -91 (41 / 41) | 41 / 41 |
| JAG_33_XKR_10 | -67 / 7 (-7 / -8) | -7 / 50 |
| MAS_MC12_04 | -49 / -60 (-5 / 3) | -4 / 3 |
| NOB_M600_10 | -48 / -79 (2 / -22) | 2 / -22 |
| LAM_LP7004_12 | -56 / -86 (3 / -52) | 3 / -52 |
| SUZ_Liana_02 | 82 / 96 (32 / 35) | 32 / 35 |
| TOY_6_Supra_05 | 17 / 63 (-11 / -1) | -10 / -1 |

The A4 itself: the front tyres 49 mm into the body at rest against Forza's
12 (its flares sit that close in Forza too), 56 at full lock; after the fix
12 and 37 (Forza's own tyre at its 36 deg: 24).

### Fix

- `forza2dsf.axle_fits` / `convert`: each axle's wheel scaled to its
  Data_Car tyre (radius and width, clamped 0.77..1.3 and 0.6..1.6), the front
  tyres standing on Forza's ground, the rear hubs up or down by the radius
  difference; a host with one wheel part for both axles drew all four at the
  front size (fixed since: "Rear wheel parts"). The wheel model is turned about its tyre's centre
  (`tyre_centre`, `wheel_point`'s fit carries it).
- `forza2dsf.settle_wheels`, called by `port_car` after the handling (it
  needs the final VO3D; `--no-settle` leaves it out): each axle's hubs (and
  the hinges and wheel bodies on them) lowered by the rest offset, p30 and
  p599 by the axles' mean, so the hubs keep their place against the CoG and
  the physics is unchanged, and the VO3D camera mounts' heights (p301, p328,
  p536, ...) raised by the same amount, since the car frame they hang from
  follows the CoG (the head camera, a hierarchy marker, logged 40 mm higher in
  that frame after a 36 mm drop: runs `vm-20261025-074620` and `-074146`).
- `car_render` draws the wheels at the rest pose too (garage pictures).

`img/wheel-clearance-renders.jpg` (`car_render`, at rest; before | after):
SUZ_Liana_02's small wheels under a high body, TOY_6_Supra_05's off-centre
wheel, a Commodore VE's wheels 300 mm off their hubs.

### In game (headless VM)

Runs (headless VM, the fixed dsf_enhanced from `out/scratch/wlc/v/fix`
against the world-load crash): `vm-20261025-085610` (`out/wc/before3`,
origin/master) and `vm-20261025-090051` (`out/wc/after3`), the fixed
probe views `wheels` (the left side at hub height), `fq` (front left
three-quarter) and `Bonnet`; the A4's own before shot is from
`vm-20261025-064019` (same build and view). `img/wheel-clearance-ingame.jpg`:

- AUD_2_A4_11: the front tyre through the flare before, inside it after,
  the flare's edge clear above it as in Forza's thumbnail.
- FER_F151_11, MAS_MC12_04, JAG_33_XKR_10, NOB_M600_10, LAM_LP7004_12: the
  oversize tyres through the arches before (the F151's fill them, the M600's
  and Aventador's rears cut the rear arch), at Forza's size and gap after.
- Full lock (`run-series -DriveKey Left -DriveSeconds 2 -Views lock,fq`:
  Left held while the car stands, runs `vm-20261026-111023` before and
  `-111602` after; `img/wheel-clearance-lock.jpg`): the A4's locked front tyre
  sweeps through the wing and its flare before, after it stays inside the
  flare but for the tyre's leading edge, as the check says (37 mm against
  Forza's own 24 at its smaller lock); the MC12's the same.
- The Bonnet camera sits where it did against the bonnet (the mounts rose with
  the body).
- The rest pose against the road (the probe's new `rest` line: the car
  matrix and `Atlas.GetGroundInfo` under the car; `performance.GetTerrainHeight`
  returns -500 on these roads): between VO3D variants of the A4 (p4, p25, p505
  removed, `vm-20261025-073350`, against the build, `-074620`) the body
  moved 40.7 mm at the front and 36.1 at the rear, `rest_offsets` says 41.2
  and 34.9. The absolute height against Atlas's ground carries a car-dependent
  12 to 40 mm more sink that the model doesn't explain, vanilla cars included
  (vanilla 127: 19 and 18 mm; 162: 12 and 16); it doesn't show as a wheel offset
  in the shots, so it is more likely the ground data than the wheels.
- Handling (dyno, `vm-20261025-091055` before, `-091517` after, the
  `handling_tune` test): A4 0-60 2.71 / 2.70 s, 0-100 6.90 / 6.90, 60-0
  18.7 / 18.9 m, 1 s step steer at 45 mph 56.0 / 56.7 deg/s and 1.89 / 1.85 g;
  F151 3.07 / 3.10 s, 6.64 / 6.70, 18.5 / 18.6 m, 61.3 / 60.9 deg/s. The
  handbrake flicks (62 / 54 deg on the A4, 59 / 62 on the F151) are within
  that test's 8 deg noise.

### Left open (wheel clearance)

Two gaps this round left were fixed since: the rear tyres of the 63 cars on
hosts with one wheel part for both axles (#82, "Rear wheel parts"), and the
41 cars with a front tyre deeper in the body at full lock than Forza's
(#138, "Steering lock"; most of that depth was the check's). What stays:

- Full bump (p188): most cars put a tyre into the body at the top of DSF's
  visual travel, as Forza's would at its MaxCompressHeight; not changed.
- The rest-pose sink against Atlas's ground beyond the model (above;
  vanilla cars sink too, so not filed).

## Rear wheel parts (2026-10-26, issue #53)

107 of the 140 four-wheeled vanilla vehicles draw all four wheels from one MODL
part, so a port on such a host drew its rear wheels at the front tyre's size.
After the wheel-clearance fix that left 63 cars (of `out/final3`: 67) with a
rear arch gap more than 10 mm larger than Forza's, up to 60 mm
(FER_330P4_67; JAG_33_XKR_10 50 mm against Forza's -8).

### How split hosts do it (confirmed-static, `out/rw/survey.py`)

The 33 hosts with separate front and rear wheel parts differ from the others
only in data the hierarchy already references by UID:

- the rear wheel records' UID (record +0x00) names a second full-entry MODL
  part (214: front 32, rear 33, both copies of the same wheel layout);
- in all 140 vehicles the front and rear wheel records use different LOD
  links (on 207 and 214 each wheel has its own); on split hosts the rear
  links' exterior half names a second exterior part;
- nothing else changes: hinges, bodies, wheel indices (+0x42), roles 27 to
  30, the panel index (+0x2A, 0 on every wheel) and the PDL. The six
  attachments name the rear exterior wheel part on split hosts and the shared
  one on the others (exporter junk, `upvh.md`); no other record uses a wheel
  part. All MODLs have the LOD table right after the part records.

### Fix (`forza2dsf.split_rear_wheel`, `convert`)

When the host shares its wheel part and Data_Car's rear tyre would be drawn
more than 5 mm off in radius (`REAR_PART_RADIUS`, 10 mm of arch gap) or 10 mm
in width (`REAR_PART_WIDTH`), `convert` first copies the shared part, box and
LOD lists, to the end of the full entry's MODL and the exterior wheel part to
the end of the exterior's (`add_modl_part`), each under a new vanilla-shaped
part UID (`Builder` UIDs from the conversion seed, its own prefix); then
points the rear wheel records at the full copy and their LOD links at the
exterior copy. `host_wheels` then sees a split host, and the existing rear
path scales the rear copy to the rear tyre, moves the rear hubs to the rear
track and stands them on Forza's ground. Run on every vanilla four-wheeled
vehicle in memory (`out/rw/check_vanilla.py`): the 107 shared hosts all come out
split, the 33 split ones are left alone, the other parts' LOD lists are
unchanged, the full MODL grows by 0x120 bytes at most. `port_car
--no-rear-wheel-part` keeps the old behaviour. `car_render` now draws each
exterior wheel part only on the hubs whose records name it (before, a split
host's garage picture drew both wheel parts on all four hubs).

Cost (134 cars rebuilt, against `out/final3`): the full entry's MDXN grows by
36 KiB (median, at most 302), the exterior's by 94 KiB (at most 134); no car
changed LODs and all 134 lint ok. The largest full MDXN among them is
JAG_33_XKR_10's 0x198800, 58% of `--mdxn-limit`.

### Survey (`dsff wheel_clearance`)

Before: all 573 cars of `out/final3` as built (`out/rw/survey-before.tsv`).
After: the 134 cars `split_rear_wheel` changes, rebuilt in `out/rw/after`
(`port_batch build --list out/rw/after.txt`), in place of theirs
(`out/rw/compare.py`). Cars more than 10 mm off Forza's tyre in the same pose:

| | Before | After |
| --- | --- | --- |
| rest gap off Forza's either way | 68 (1 tighter, 67 looser; worst FER_330P4_67 60, AUD_R8LMS_09 58, JAG_33_XKR_10 58, SHE_CobraDaytona_65 56, REN_5Turbo_80 48) | 3 (KEN_T440_12 15 tighter, CHE_ImpalaSS_64 14 and FOR_Thunderbird_57 12 looser) |
| at rest deeper in the body | 1 (KEN_T440_12) | 1 |
| at full lock deeper | 29 | 29 |

The rebuilt cars' rear wheels: |rest gap - Forza's| median 10 -> 0 mm, max
60 -> 8. The three left have the same tyre front and rear; their offsets are
at the front axle (Impala, Thunderbird: the nearest arch point is to the side,
so the settle moves the gap less than the hub) or KEN_T440_12's 149 mm rest
offset clamped by its travel, not the wheel size.

| Car (host) | Rear gap at rest, before / after (Forza) | Rear tyre radius / width |
| --- | --- | --- |
| JAG_33_XKR_10 (207) | 50 / -8 (-8) | 0.319 / 0.282 -> 0.361 / 0.311 |
| SHE_CobraDaytona_65 (248) | 56 / 0 (0) | 0.313 / 0.192 -> 0.350 / 0.248 |
| AUD_R8LMS_09 (139) | 61 / 3 (3) | 0.323 / 0.264 -> 0.353 / 0.303 |
| FER_458Spider_12 (125) | 57 / 34 (34) | 0.336 / 0.228 -> 0.357 / 0.286 |
| REN_5Turbo_80 (184) | 74 / 26 (26) | 0.270 / 0.183 -> 0.299 / 0.212 |
| BUG_Veyron_09 (279) | 76 / 50 (50) | 0.333 / 0.249 -> 0.358 / 0.343 |

### In game (headless VM, dsfforza.asi `A2DBAE5D...`)

The six cars above built both ways (`out/rw/vm-before` with
`--no-rear-wheel-part`, `out/rw/vm-after`), IDs 1 to 6, probe views
`wheels,fq`: `vm2-20261026-163835` before, `vm2-20261026-162523` after
(`img/rear-wheel-part-ingame.jpg`). The rear tyres now fill their arches as
in Forza; the fronts are unchanged. The paint differs between the runs (the
game's palette pick).

- Drive: `vm1-20261026-164334` (IDs 1, 3, 6, throttle 8 s): each car drove
  off (top 101.5 mph), no crash. `vm1-20261026-164914` (IDs 2, 1, throttle
  held through the fixed views): the Cobra rolls through the wheels camera
  with its rear wheel turning (`img/rear-wheel-part-drive-damage.jpg`).
- Damage: `vm2-20261026-164521` (IDs 1, 6, `-Damage -DamagePanels`): full
  visual damage, panels opened and removed; both wheel parts stay on their
  hubs.
- LODs: the rear copy has the front part's masks (4, 8, 16 in the full
  entry, 2 in the exterior), each with its own submodels
  (`out/rw/lods.py`).

## Rim brightness (2026-10-26)

User report on `out/final4` (LOT_Carlton_89, FM4, spool 495 on host 229): "rims
that glow bright white". In game the Carlton's five-spoke rims were flat near
white; Forza's thumbnail (FM4 `Media/UI/Textures.zip`,
`Thumbnails/Thumbnail_291.xds`) shows them mid to dark grey.

### What Forza does with the rim texel (confirmed-static)

- The Carlton's `wheel.xds` (256² DXT1) is a plain light grey gradient (157 to
  219) with a turned centre cap and a dark square for `inner_rim`. The rim UVs
  land where they should (`out/inv/rimuv.py`: the spoke unwrap on the
  gradient, mean 182; `inner_rim` on the dark square, 64). The converter
  copied those texels into the atlas as they are, and the Chassis material
  drew them near white.
- Forza's car shaders take per-technique parameters from the car's
  `ShaderSettings.xml`, over defaults in `media/cars/Shared.zip`
  `ShaderSettings/Normal.xml` (FH1 and FM4 alike for the wheel techniques).
  `rim` has `PaintScale` 0.75 (with `CubeScale` 1.5, `FresnelIndex` 1.65,
  `SpecularAmount` 0); `inner_rim`, `outer_rim`, `rim_V2` and `rim_bump_metal`
  1.0, `rim_simple` 0.5; `chrome_rim`, `wheel_emblem` and `wheel_black` have no
  `PaintScale`. The Carlton's own file sets nothing for its wheel.
- `PaintScale` is a multiplier on the rim texel. The 19 cars that set their
  own rim value (`out/inv/rim_survey.py`) tune it against the texture's
  brightness: FOR_FocusRS500_10's near-black rim texels (about 4) at 7.5,
  FOR_ShelbyGT500_13's (12) at 6.75, BEN_CONTINENTALGT_11's near-white ones
  (213) at 0.5, CHE_Express2500Cargo_05's (183) at 0.5, VW_BEETLE_68's
  `inner_rim` (138) at 0.043.

### How many cars (every roster car, LOD 1)

521 of the 573 cars have a `wheel.xds` (52 race cars have none and keep the
flat swatches); 508 of them have a `rim` technique. The area-weighted rim
luminance per car, texels as converted before and times `PaintScale` now:

| | 10% | 25% | median | 75% | 90% | cars above 170 | above 190 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| before (texel) | 57 | 119 | 156 | 191 | 212 | 205 | 131 |
| now (texel x PaintScale) | 43 | 90 | 117 | 145 | 160 | 21 | 3 |

The brightest before: SUB_LegacyB4GT_05 253, FIA_500AbarthSS_10 246,
MAZ_MX5SL_10 239, NIS_SkylineMinesR32_93 239; the Carlton 182 to 137. Vanilla
DSF rims sample the tyre atlas's grey swatch of 131 (red channel).

### Fix (`forza_materials.WHEEL_PAINT_SCALE`, `shader_settings`, `wheel_paint_scale`; `dsf_materials.Texturing._wheel_paint_scale`)

When `Texturing.plan` takes the wheel, it scales the RGB of the car's
`wheel.xds` texels by the `PaintScale` of the technique whose UV triangles
cover them (the car's `ShaderSettings.xml`, else the shared default, else 1;
since #143 a `chrome_rim` texel takes its `ChromeDiffuseScale` instead, 0.5 by
default, `forza-materials.md`, "Gloss");
texels no triangle covers take the scale that covers the most texels. Then the
atlas is packed as before, so the alpha (specular mask, from luminance) follows
the darker colour. The build log's atlas line names the scales other than 1
(`rim PaintScale rim 0.75`). Entry sizes and atlas layouts don't change.

### In game

Headless VM, the same five cars built from master and from this change
(`out/t/before`, `out/t/after`: IDs 1 LOT_Carlton_89, 2 SUB_LegacyB4GT_05, 3
FOR_FocusRS500_10, 4 BEN_CONTINENTALGT_11, 5 FER_355Challenge_95, plus vanilla
229), `-Views wheels,rear -Palette 0 -ShotSeconds 3`: runs `vm1-20261027-045825`
(before) and `vm1-20261027-050351` (after; the Carlton's side shot is its
`zoom-05`, the wheels view came before the first observe shot). The Carlton's
and the Legacy's rims are light grey with their spoke shading instead of flat
white, the Continental's (PaintScale 0.5) mid grey, the Focus RS500's (7.5)
black rims a little lighter, with the blue cap; next to vanilla 229's silver
rims they now look alike (`img/rims-paintscale-ingame.jpg`).

## Steering lock (2026-10-27, issue #54)

The ports now steer their front wheels to Forza's lock, `Data_Car.SteerMaxAngle`
(VO3D p298, research/handling.md, "Steering lock": how the engine uses it, the
handling cost). Before, p298 came with the host's or the donor's steering set: 72
cars turned further than in Forza (40 or 45 deg against 30 or 36; KEN_T440_12 50
against 42), 501 less (35, 37 or 40 against 42).

### The check was part of the problem

`wheel_clearance.gap` counted every body point outboard of the turned tyre's centre
plane. Past about 35 deg the trailing inner corner of the tyre swings over body
points under the car, behind and below the hub and inboard of the tyre's inner wall
(AUD_TTRS_10: a point 13 cm inboard of the hub, 18 cm behind, 12 cm down: -83 mm
from 36 deg, 3 mm at 30). Those are suspension and liner parts hidden behind the
tyre, and they made most of #51's "full lock" depths (TOY_3_GT1_99 -83 mm at 40
deg, 8 without them; BMW_15_LMR_99 -42 and 6). `gap` now leaves out body points
inboard of the unsteered tyre's inner wall (in the car's frame); at rest nothing
changes (the outboard-half rule already left them out). `--forza-lock`
(`survey`, `car`) measures a build from before this change at Forza's lock, as the
converter now builds it.

### Survey (`out/final4`, all 573 cars, front wheels)

`out/steer/lock_survey.py` (left in the PR's worktree, gone): each car's front tyres at its p298 and at Forza's lock,
against Forza's own tyre at Forza's lock (`gap_forza_lock`). Cars with a tyre more
than 10 mm deeper in the body than Forza's:

| Check | At the build's p298 | At Forza's lock (this change) |
| --- | --- | --- |
| master's `gap` | 29 (28 of them with a larger lock than Forza; worst TOY_3_GT1_99 -79, ACU_66_ARX02a_09 -51, BMW_15_LMR_99 -50, AST_007_AMRLola_09 -47, KEN_T440_12 -42) | 0 |
| `gap` without the underbody points | 10 (4 with a larger lock: the ARX-02a and AMR-Lola pairs, -51 and -47; 6 with a smaller one, VW_Beetle_04 -22 down to REN_Twingo_09 -11) | 0 |

With the new `gap`: median full-lock gap 15 -> 16 mm; tyres into the body at all
(gap < 0) 155 -> 157 cars, every one of them as deep as Forza's own tyre there
(within 10 mm). Raising the lock to 42 deg costs the 501 raised cars at most 8 mm
(median 0); on the 72 lowered cars the gap moves to Forza's (ACU_66_ARX02a_09 -67 ->
-16 mm, Forza -16; AST_007_AMRLola_09 -71 -> -25, Forza -24).

### In game (headless VM 1)

The three test cars (IDs 1 ACU_66_ARX02a_09, 2 AST_007_AMRLola_09, 3 BMW_15_LMR_99,
all 40 -> 30 deg) built with this change (`out/steer/after`), and the same archive
with p298 set back to master's 40 (`out/steer/before.sp`: 6 bytes differ),
`run-series -Vm -DriveKey Left -DriveSeconds 10 -Views lock,fq,wheels`: before
`vm1-20261027-185802` (an earlier try, `-182035`, stuck loading the story world;
the LMR, last in line, got no views before the time-out, so `-194209` for it alone,
where as the first car of a run it got only the side view), after
`vm1-20261027-182651`. `img/steer-lock-ingame.jpg`: at 40 deg the ARX-02a's and the
AMR-Lola's locked tyres stick out of the side of the wheel fairing; at Forza's 30
they stay inside it, as the survey says. The LMR's tyre turns less into its arch.
From the cockpit the wheel and the hands stand at the same hand-over-hand pose at
full lock before and after (the rig turns by angle / p298). Handling: research/
handling.md, "Steering lock" (turning circle 3.35 -> 4.56 m on the LMR; the 50 mph
step steer unchanged).

## Speed blur (2026-10-29, issue #211)

Ported wheels had no speed blur: `forza2dsf` drops every Forza wheel subsection
with "blur" in its name (above, "What was wrong"), and nothing replaced them.
Scratch scripts: `out/blur/` (`survey.py`, `fleet.py`, `progs2.py`,
`gpdis2.py`, `disc_geo.py`, `tex.py`, `roster2.py`, `sheet.py`), left in the
PR's worktree.

### How vanilla wheels blur (confirmed-static)

- **Materials** (SSIC): `GLOBAL_Wheel_Blur` (`001251d20eeac228`, `_LOD1`
  `10122b9d53b14e2a`; samples the shared tyre atlas),
  `GLOBAL_Wheel_Blur_SpokeDisc` (`001262258aa7c428`, `_LOD1`
  `10122b9e53b14e2a`; texture `0012622354a4c428`) and `GLOBAL_Wheel_Blur_Disc`
  (`0012622488a6c428`, `_LOD1` `10122b9c53b14e2a`; texture
  `0012622143a4c428`). All three: `rs_alphablendenable`, `SortMode` 1,
  `rs_cullmode` 1, the discs also `rs_zwriteenable` 0; stride 20, MODL flag 7
  (opaque wheel submodels: 5).
- **Programs.** Their passes run SSIC programs 21 to 40 of the second DRSP in
  `SSIC/MDXN` (the GPCH survey in `damage.md` covered the first DRSP only). The
  vertex shader (`Vehicles_Functions.hlsl`, `CustomVertexTransform_VS`):

  ```text
  b         = 1.15 * g_v4PerPanelData[s9.byte1].y         // the wheel's blur amount
  t         = s9.byte3 - 1                                 // typeSpecificData
  fadeIn    = saturate(t);  base = 1 - fadeIn
  opacity   = saturate(t * (b * (1 + 1.5 fadeIn) - 0.15) + base)
            // byte 3 = 0: 1.15 - b (fades out); 1: 1 (always);
            // 2: 2.5 b - 0.15 (fades in, opaque from b = 0.46)
  ```

  The Blur pixel shader draws texel red as the grey level (green: specular and
  reflection, `g_hReflectionAmount` 1), alpha = opacity. The SpokeDisc / Disc
  pixel shader draws red x `g_vec4WheelBlurColour` (the vehicle's SPSR third
  set; vanilla: a grey per vehicle, 1.0 on 37 of 150), green the gloss, alpha =
  blue x opacity. No other program reads byte 3: the car's Chassis and Lights
  materials cannot fade.
- **Use** (140 four-wheeled vanilla vehicles, exterior wheel part): 116 draw
  their sharp rim on `Blur_LOD1` at byte 3 = 0 (25 also have some byte 3 = 1
  there), 93 add a SpokeDisc and 26 a Disc at byte 3 = 2, 111 a centre cap on
  their own Chassis material at byte 3 = 1; the tyre is `Tyre_LOD1` at 1. The
  disc is a shallow dish over the whole rim face (227: x +0.044 at the hub,
  +0.123 at r 0.11, +0.109 at its edge r 0.27, out to the tyre's bead), mapped
  flat (u = 0.5 - z / 2R, v = 0.5 - y / 2R), normals along +x, sky 0.9 to 1.0.
  The SpokeDisc texture is one generic blurred wheel (red 136, green 60, blue
  210 on average: 82% coverage).
- **What sets `.y`**: not traced. `FUN_00874EA0` (the per-wheel draw state)
  keeps a rate-limited spin speed and the spin angle per wheel; the step from
  there to the per-panel constant is not found. A hashed name
  `VehicleWheelBlur` (`FUN_00DC44F0`) is written and never read. Glass uses
  the same `.y` slot for its crack state (`vehicle-model.md`), so the engine
  sets it per draw. A port's wheel vertices have s9 (0, 0, 0, 1), like vanilla
  wheels, so the engine treats them alike.

### Forza's blur (confirmed-static)

The wheel archive's `wheel0..3.xds` (all 520 cars that have one): `wheel0`
the sharp rim face with alpha cut out between the spokes, `wheel1` to
`wheel3` the same face blurred more and more round the axle, alpha partial
in the spoke ring (`img/wheel-blur-forza-pictures.jpg`: the Carlton, 370Z,
Veyron SS and Huayra, `wheel0` to `wheel3` with their alpha). `blur_rim` is a disc mapped flat onto
them (a plain planar map on all but 23 roster cars), at the face (Carlton,
370Z) or inside the dished rim (250 GTO: behind the knock-off); `blur_lip` /
`chrome_blur_lip` a copy of the lip. Forza's LOD 5 wheel has no sharp rim on
503 of 573 cars, only `blur_rim`: there the disc is the rim. Shared
`Normal.xml` lists `blur_rim` twice (PaintScale 1.0, then 0.65).

### What was wrong

- Every port's sharp spokes stayed as they are at any speed.
- The full entry's far LODs drew wheels without a rim where Forza's LOD has
  only `blur_rim` (dropped): mask 8 on 526 cars, mask 16 on 539, mask 4 (the
  full-detail LOD) on 15 (DOD_Durango_99, CHE_Express2500Cargo_05,
  AUD_A4Avant_04, BUS_TOUR_12, FOR_Explorer_10, FOR_TaurusSHO_10, KEN_T440_12,
  VW_Beetle_04, ...; full roster, LODs of `out/build`).

### Fix (`forza2dsf.wheel_blur_section`, `dsf_materials.wheel_blur_texels`)

A port's rims keep their artwork on the Chassis / gloss materials, which
cannot fade, so the port gets vanilla's disc only, made to cover the rim when
faded in:

- **Geometry.** Per Forza wheel LOD drawn, a dish round the axle
  `WHEEL_BLUR_LIFT` (3 mm) in front of the rim face: per ring the outermost
  point of all non-tyre geometry within a ring of it, so no spoke pokes
  through; out to the rim's edge (at most the tyre's inner radius); rings
  where the face is flat are left out. 24 to 264 triangles (median 168).
- **Material.** A copy of `GLOBAL_Wheel_Blur_SpokeDisc` (`_LOD1` in the
  exterior) in each entry, its texture swapped for the car's atlas
  (`blur_disc_material`, `prepare(blur_materials=...)`; the shared MACOs from
  `shared_macos`). Stride 20, MODL flag 7, s9 (0, 0, 0, 2): fades in like
  vanilla discs. On a LOD with no sharp rim (`wheel_has_rim`) s9 (0, 0, 0, 1):
  always drawn, the rim's stand-in as in Forza. Sky 0.9, as on vanilla discs
  (first tried 0.56, the converted rim's outward faces: the disc came out
  darker than the rim, below).
- **Texture.** `wheel2.xds` (the middle blur level; `port_car.extract_wheel`
  now takes it, family `fm.WHEEL_BLUR`) through Forza's own planar map,
  scaled by the rim's PaintScale like the rim's texels, laid over a mid-grey
  backdrop (60) for what shows between the spokes, so the disc is opaque at
  full blur; red the grey level over the tint, green 0.44 x grey (the vanilla
  disc's ratio), blue 255. In the atlas at weight 128 / its size (0.25x to
  0.30x on the test cars).
- **Colour.** `g_vec4WheelBlurColour` = the picture's coverage-weighted mean
  colour, largest channel 1 (`paint.set_wheel_blur_colour`): the Veyron SS's
  orange 1.00/0.19/0.05, grey rims about 1/1/1.
- **Without the picture** (52 cars without a wheel archive, `--textures none`)
  the disc uses the shared SpokeDisc and its generic texture, mapped like
  vanilla discs; the host's blur colour stays. `port_car --no-wheel-blur`
  leaves the disc out.

### Survey (all 573 roster cars, `out/blur/roster2.py`, LODs from `out/build`)

| | Cars |
| --- | ---: |
| disc on the wheel | 572 (NIS_Leaf_11: the survey's error, no gamedb passed) |
| with Forza's picture / the shared disc | 520 / 52 |
| exterior disc fades in | 572 |
| full entry mask 4 / 8 / 16: fades in | 557 / 46 / 33 |
| full entry mask 4 / 8 / 16: always (the rim was missing) | 15 / 526 / 539 |

Cost: the five test cars' full entries grow by 24 to 32 KiB (six discs and
one MACO), the exteriors by 16 to 20 KiB; the archive by about 48 KB a car
(27 MB for the roster). Each wheel part gains one submodel per LOD: mask-4
submodels +2 (ports' maximum 121 -> 123, vanilla's 129; `spool-limits.md`,
"Draw queue"). Atlas: in a sample of 72 cars the uniform scale is unchanged on
46, 0.992 on average, at worst 0.945 (FOR_MustangKingCobra_78).

### In game

Headless VM, probe view `wheel` (new: the front left wheel from its side,
close) and `wheel+mph<N>` (new in `clonetest.lua`: the car held at N mph through
the view, the camera moving with it). `out/blur/before` (master) and
`out/blur/after` / `after2` (this change; IDs 1 FER_250GTO_64, 2
LOT_Carlton_89, 3 NIS_370Z_10, 4 BUG_VeyronSS_11, 5 PAG_Huayra_12; vanilla 227
for reference), `-Views 'wheel,wheel+mph15,wheel+mph35,wheel+mph60'`:

- `vm2-20261029-094256` (before) and `-094943` (after, sky 0.56, backdrop
  20): the vanilla 370Z's spokes crossfade into a bright silver disc between
  15 and 35 mph. The ports' spokes stayed sharp at every speed before (frozen
  in the shots: strobing); after, each crossfades the same way into a blurred
  disc of its own wheel: the 250 GTO's wire wheels to a light grey disc, the
  Carlton's white five-spokes to smeared lobes, the Veyron SS's orange spokes to
  an orange disc. But the faded-in disc was darker than the rim: mean grey of
  the 370Z's wheel face 90 at rest, 70 at 60 mph (vanilla 370Z 103 and 146);
  the Huayra's thin spokes went nearly black.
- `vm2-20261029-102813` (after2: sky 0.9, backdrop 60): the 370Z's face 96 at
  60 mph against 90 at rest; GTO, Carlton, Veyron as before but lighter.
- `vm2-20261029-105151` (before) and `-110000` (after2), traffic off (#228):
  `img/wheel-blur-ingame-before-after.jpg` (rows vanilla 227, 370Z, Huayra,
  Veyron SS, each before then after; columns rest, 15, 35, 60 mph).

Garage pictures: `car_render` leaves the fading disc out since #222 (#221;
`img/wheel-blur-garage-after.jpg`: Huayra and Veyron SS, before and after,
identical).

### Left open (speed blur)

- What sets the blur amount (`.y`) and its speed curve: in the shots the
  vanilla and the ported discs are half in at 15 mph and fully in by 35.
- One picture for every speed: Forza swaps `wheel1..3` with speed, DSF has
  one disc that fades in.

## Brake rotors (2026-10-29, issue #212)

Through open spokes a port showed the dark inside of its barrel (since "Wheel
shading"); Forza draws a brake disc and caliper there. Scratch scripts in
`out/brakes/` (`survey.py`, `summ.py`, `summ2.py`), left in the PR's worktree.

### What Forza has (confirmed-static, all 573 roster cars)

- 567 cars have `<car>_rotor{LF,RF,LR,RR}_LOD0.carbin` and
  `_caliper{LF,RF,LR,RR}_LOD0.carbin` in the car's zip (beside
  `stripped_*` copies of a few hundred bytes); the six without are
  CHR_PTCruiser_04, FOR_Explorer_10, FOR_F350SuperDuty_08, FOR_FocusZX4_05,
  TOY_Prius_11 and VW_Beetle_04. LOD 0 only, TypeId 1, one section each.
- Frame: the hub's, like the wheel model's (spin axis x, the hub face at -x on
  the left-hand corners, where the wheel's rim face is). The rotor is centred on
  the axis and on x = 0: the 205 T16's disc at x +0.011..+0.040, r up to 0.137,
  its hub hat at x -0.040..+0.011, r up to 0.083. The right-hand carbins mirror
  the left ones. The caliper sits off the axis at one clock position (the 205's
  front one at z -0.07..-0.16, y -0.06..+0.10), so it must not turn with the
  wheel.
- Size: rotor triangles per car, front / rear: median 540 / 638, 90th
  percentile 2144 / 2472, at most 9410 (SEA_IbizaCupra_09's drilled front disc)
  / 4038 (rear drums of the late-60s Americans). Calipers median 1138 / 1016, up
  to 7903.
- Materials: `hub` (497 rotor carbins), `rotor` (415), `brake` (184), `drum`
  (36), `misc`, `matte_colors`, `front`, ...; 201 rotors have only one of `hub`,
  `misc`, `front`, `right_side` for disc and hub alike. They sample Forza's
  shared `media/cars/Shared.zip` `rotor0..4.xds` (pictures of four disc types and
  a caliper), not the car's textures.

### Fix (`forza2dsf.brake_rotor_section`, `port_car.extract_brakes`)

- `port_car.extract_brakes` caches the left front and rear rotor carbins as
  `rotorLF_LOD0.carbin` / `rotorLR_LOD0.carbin` (own stamp
  `brake_source.json`); `load_car` reads them into `ForzaCar.rotors`.
- `convert` adds the axle's rotor to each wheel part on each entry's finest LOD
  (the exterior, Forza LOD 1, and the full entry's mask 4, Forza LOD 3):
  the front rotor to the front or only wheel part, the rear one to a rear part
  (split hosts, "Rear wheel parts"). One model serves both sides, so the left
  rotor stands for the right (its mirror image). The rotor keeps Forza's size
  (the part's fit scales the wheel model, not the rotor) and its place against
  the tyre's centre.
- Drawn on the shared tyre material with plain swatches of the tyre atlas, no
  artwork: the friction ring (triangles beyond 0.6 of the rotor's radius, since
  the names don't tell) on the uniform mid-grey square (red 106, gloss 58, at
  (0.07, 0.57); the rims' swatch is 131 / 190), the hat and hub on the inner-rim
  dark (65 / 5). The baked sky is the wheel's (`wheel_skies` with the rotor among
  the occluders), so the rotor lies in the shadow of the barrel and spokes.
- A rotor of more than `BRAKE_ROTOR_MAX_TRIANGLES` (2000; 145 of the 1146 front
  and rear rotors) is replaced by `plain_rotor`, a plain disc of its size: ring
  faces, rim and inner edge over the ring's axial extent and a hat over the
  rest's, 32 segments, at most 352 triangles, wound like the carbin's own.
- `port_car` drops the rotors before stepping a car down to coarser LODs when an
  entry is over the limits, and `--no-brake-rotors` leaves them out.

### Cost and test set

The six test cars (`out/brakes/cars.txt`: IDs 1 PEU_205T16_84, 2 NIS_370Z_10,
3 FER_250GTO_64, 4 BUG_VeyronSS_11, 5 VW_BEETLE_68 (closed hubcaps), 6
SEA_IbizaCupra_09), built from master (`out/brakes/before`) and with this change
(`out/brakes/after`): each entry's MDXN grows by 14 to 30 KiB; all six lint ok,
no LOD changes. Over the roster (survey triangles, about 30 bytes each) a car's
entry grows by 23 KiB (median), 75 (90th percentile), 112 at most; no car of
master's full build comes near the static read buffer with that. The 250 GTO
(2944-triangle drilled disc), the Beetle (drums) and the Ibiza get the plain
disc. Offline renders: `img/brake-rotors-render.jpg` (front left wheel, outside
and from the front, before | after).

`render_compare diff --before out/brakes/before --after out/brakes/after --out
out/rc/brake-rotors`: 5 of the 6 cars drawn differently, only between the spokes
(370Z: "trims" side 16 %, wheels side 7 %, front3q 4 %; 250 GTO 13 / 11 / 7 %;
Ibiza 12 / 10 %, rear3q 4 %; Veyron SS wheels side 9 %; 205 3 / 2 %; the Beetle
alike), scores unchanged; the "trims" share is the region Forza's reference
gives the pixels between the spokes.

### In game (headless VM 1)

`run-series -Vm -ProbeIds '1,2,3,4,5,6' -Views 'wheel,wheel+night,wheel+mph8,wheels'
-ShotSeconds 3 -Palette 0`: `vm1-20261029-172424` (before), `vm1-20261029-173123`
(after), `img/brake-rotors-ingame.jpg`. A grey disc and hat now stand behind the
spokes of the 370Z, Veyron SS, Ibiza and 250 GTO, at rest and rolling at 8 mph,
in the wheel's shadow; at night they stay dark (no glow, no white). The Beetle's
hubcap hides its drum as before; the 205's holes still show the barrel's dark
(its disc ends inside the hole circle). No flicker between disc and rim in the
shots.

Damage and steering (`-ProbeIds '2,3,4' -Damage -DamagePanels -DriveKey Left
-DriveSeconds 2 -Views 'lock,wheels'`): `vm1-20261029-174017` (before),
`vm1-20261029-174645` (after). The same crush, panels opened and dropped, wheels
on their hubs and the front wheel turned alike in both; no crash.

### Calipers (not converted, #212)

The caliper has to stay still while the wheel turns, and steer and ride with
it. In the vanilla hierarchy (`upvh summary`, host 227) the front wheel records
(type 2) hang under part records of roles 155 / 156 ("front hubs (brake
disc)", `damage.md`), which hang under the front suspension part (188); the rear
wheels hang straight under the rear suspension (190), with no hub records. The
hub records have a full-entry MODL part, a hinge at the hub (axis x, limit
0.5655), a panel-transform slot and no LOD link, so the exterior entry, which
the player's car draws, has no part to carry a caliper on any corner; whether
the engine steers the hub parts is not known. A caliper would need new LOD
links (and new records at the rear): the hierarchy changes that crashed
`forceOpenPanels` before when they went wrong. Left for an issue.

## Wheels off their arches (2026-10-30, issue #301)

User report on the full-roster release candidate (c4ee059): on the Commodore VE race
cars (HOL_1/8/11/33/88_CommodoreVE_11, host 162) the front wheels stand out of the
bodywork and every wheel sits off its arch. Scratch scripts in `out/w301/` (left in the
PR's worktree): `survey.py`, `analyse.py`, `sources.py`, `fine.py`, `rend.py`.

### The survey (confirmed-static, all 573 roster cars)

Per car, in Forza space, each corner's arch opening against the hub `convert` places
(MAXData's wheelbase, midpoint and track, the tyre centred on the hub): body points
of the car's LOD 1 outboard of the tyre's centre plane and above the hub; the arch is
the z along the car where the largest empty circle at hub height centres (the middle of
the plateau). Front and rear offsets, deciles 5 / 25 / 50 / 75 / 95%: front -15 -5 0 5
22 mm, rear -15 -5 0 5 25 mm. Cars more than 60 mm off with both axles agreeing:

| Car | Front / rear arch off the hub | Cause |
| --- | --- | --- |
| HOL_1/8/11/33/88_CommodoreVE_11 | +315 / +305 mm (behind) | the wheel model's tyre sits 0.300 m behind its origin (`wheel` section offset z +0.300) |
| CIT_C1_11 | -165 / -185 mm (ahead) | its MAXData.xml is PEU_107_11's, value for value (`BottomCenterWheelbasePos` z -0.151, wheelbase 2.340); the C1's body sits 0.172 m further forward in its model (body length midpoint -0.124 against the 107's +0.048) |

The other large readings have axles that disagree and are the finder's, not the cars'
(arch shapes: PAG_Huayra_12 +95 / +160, the 1965 Minis +150 / +5, BUS_TOUR_12 +155 / +2,
ASC_KZ1R_12 +70 / +135, SAL_S7_04 +180 / +380 with its rear wheel half under the
bodywork as on the real car): renders of the built cars show their wheels in their
arches. HOL_HSVw427_09 (host 176) is 15 mm / 0 off: it is not affected. The wheel
models off their origin across or up (TOY_6_Supra_05 49 mm out and 80 mm down,
MER_ML63AMG_09 and VW_BEETLE_68 39 mm across) have their arches on the hub, so the
tyre stays centred there.

FM4's gamedb `Data_CarBody.BottomCenterWheelbasePosZ` was judged no better source: it
differs from MAXData's by more than 30 mm on 461 of the 572 cars that have both. That
compared it with MAXData's sign flipped; as it is, it equals the converter's midpoint on
558 cars and fits the C1's and the Commodores' arches, and since #406 it is the source
("Body height and wheelbase").

### Fix (`forza2dsf.car_dims`, `MID_Z_FIXES`; replaced by the gamedb's midpoint since #406)

- Forza draws the wheel model unturned at each hub, so a tyre that sits off its origin
  along the car stands that far from the physics' hubs, on both sides, and the arches
  are cut round it. `car_dims` adds the tyre centre's z (`tyre_centre`) to the
  wheelbase midpoint (`CarDims.mid_z`); the wheel still turns about the tyre's centre,
  so the hubs go to the tyres. Only the five Commodores have such an offset (z under
  1 mm counts as 0 on every other car).
- `MID_Z_FIXES` gives CIT_C1_11 its own midpoint (MAXData's +0.151 less 0.170 = -0.019:
  its arches centre on -0.024, the same finder reads the 107's 5 mm short of its own
  midpoint).

After the fix (the same survey with the fixed `car_dims`, `survey_after.py`): the
Commodores' arches 10 / 5 mm off the hubs, the C1's 5 / -15, within the roster's spread
(HOL_HSVw427_09 and PEU_107_11 unchanged, -15 / 0 and 5 / -15).

Every `Placement` user takes the corrected midpoint (convert, `lights`, `plates`,
`host_select`'s window panes), so body, lamps, plates and cockpit move together
against the hubs; the hubs, wheelbase, track and handling are unchanged.

Test set (`out/w301/cars.txt`): IDs 1 to 5 the Commodores, 6 HOL_HSVw427_09, 7
CIT_C1_11, 8 PEU_107_11, built with master's converter (`out/w301/build-before`) and
with the fix (`out/w301/build-after`); all lint ok, no LOD or size changes.

In game (headless VM 2, `run-series -Vm -ProbeIds '1,2,3,4,5,6,7' -Views
'wheels,front,fq,lock' -DriveKey Left -DriveSeconds 2 -ShotSeconds 3 -Palette 0`, the
car standing, steering held left through the views): `vm2-20261030-021153` (before),
`vm2-20261030-021654` (after). Before, the Commodores' wheels stand forward of their
arches (the arch's open back edge behind each tyre in the side view) and the steered
front tyre stands out ahead of the flared wing; after, all four sit in their arches
and the steered tyre stays inside the flare. The C1's wheels move forward into their
arches. HOL_HSVw427_09 draws the same in both runs. The first car of the after run got
only its chase and side shots; HOL_8 (ID 2) shows the same views.

`render_compare diff --before out/w301/build-before --after out/w301/build-after --out
out/rc/wheels-301`: the six changed cars draw differently, PEU_107_11 and
HOL_HSVw427_09 alike. Every view of them changes, since the renders frame the car by
its hubs and the body moves 0.3 m (0.17 m on the C1) against them. The cockpit eye
moves with the body: before, it sat 0.3 m forward, in the Commodore's steering wheel;
after, behind it, as in Forza's reference. The Commodores' scores rise (HOL_1 12.5 to
16.5) because their whole wheels now show in the arches: these race cars have no
`wheel.xds` and keep the flat swatch rims ("Rim textures"), light against Forza's dark
ones (`wheels: washed out`). The C1's falls (3.34 to 2.61).


## Tyres in the road (2026-10-30, issue #317)

User report on the v0.1.3 release candidate: the Hummer H1 Alpha's (HUM_H1Alpha_06, host 138
Audi Q7) tyres sink into the road at rest, their bottoms cut flat by it. Scratch scripts in
`out/w317/` (left in the PR's worktree): `rest.py` (the probe's rest lines against the
archive), `pdl.py`, `vsurvey.py`, `rsurvey.py`, `patch_vo3d.py`, `patch_cyl.py`.

### What sets where a drawn wheel meets the road (confirmed-runtime)

- **Not `p360`.** The H1 with VO3D p360 (driven wheel radius, Forza's 0.468) set back to the
  Q7's 0.39 rests at the same height to 0.1 mm, its tyres drawn the same
  (`vm1-20261030-071831` against `-072121`). p360 is the gearing's radius only.
- **Not the visual travel.** p188 raised from 0.09 to 0.30: the same
  (`vm1-20261030-073401`).
- **The wheel record's collision model.** Every wheel record (type 2, +0x20) has its own PDL
  model: one primitive, flags 2305, extents (half width, radius, 0) about the hub, header
  radius sqrt(x² + 2y²). With the H1's four set to its tyre (0.468, half width 0.1475;
  `vm1-20261030-073020`, `-075915`) the tyres stand on the road, round, and in the
  `wheels` view (a camera fixed in the car's frame) the wheels and the body rise together
  by the same 30 px (hub cap 295 -> 265, the H badge 158 -> 130): the car stands on its
  wheels' cylinders, and with the host's under Forza's tyres it was drawn the radius
  difference too low, body and wheels alike. The frame the probe's rest line logs doesn't
  move (77.8455 in every variant), so it is no measure of the drawn height.
- Two variants on top of the sized cylinders, to rule them out: the body raised another
  82 mm (`Placement.dy`; `vm1-20261030-080916`) stood that much too high; the hub table
  raised 82 mm (`vm1-20261030-081116`) left the body where it was and put the wheels up in
  the arches.
- **Vanilla** (all 140 four-wheeled vehicles, `vsurvey.py`): the cylinder's radius is the
  drawn tyre's on 132 to 1 mm (the other 8 are the survey picking up a bigger part), its
  width 1.005 x the tyre's (median), the header radius sqrt(x² + 2y²).

### What was wrong

`convert` draws Forza's tyre (Data_Car size, "Wheel clearance") on hubs whose collision
cylinders stay the host's. Over the roster (`rsurvey.py` on the full build, the drawn tyre
against the cylinder per axle): 544 of 573 cars have a cylinder more than 1 mm off their
drawn tyre's radius. 168 have a tyre more than 10 mm bigger (into the road; 49 more than 20,
23 more than 30: KEN_T440_12 113 mm on host 197, HUM_H1Alpha_06 82, LAM_Miura_06 48 at the
rear, FER_250GTO_64 44, BUS_TOUR_12 38), 197 one more than 10 mm smaller (AUS_MiniCooperS_65
86 mm, GMC_Syclone_92 68, MER_C32AMG_04 54).

### Fix (`forza2dsf.fit_wheel_collision`)

After `patch_hubs`, each wheel record's cylinder takes its axle's drawn tyre: extents (width
/ 2, radius), the header radius recomputed. A model of another shape (not one primitive, or
z extent other than 0) is left alone; `--no-wheel` leaves them all. The log names the sizes
(`wheel collision cylinders: 4 sized to the tyres (front r 0.468 w 0.315, ...)`). Entry
sizes don't change.

### Test set and in game

`out/w317/cars.txt`: IDs 1 HUM_H1Alpha_06 (host 138), 2 KEN_T440_12 (197), 3
FOR_SVTRaptor_12 (251), 4 BUS_TOUR_12 (246), 5 LR_RangeRover_12 (239), 6
AUS_MiniCooperS_65 (249), built from master (`out/w317/build-master`) and with the fix
(`out/w317/build-after`); all lint ok, entry sizes unchanged. `render_compare diff`
(`out/rc/tyres-317`): 6 changed, none drawn differently (the cylinders aren't drawn).

Headless VM 1, `-Views wheels`, the cars standing: `vm1-20261030-065913` (master, with
vanilla 138 and 197) and `vm1-20261030-075915` (the fix). The H1's tyres stood about 8 cm
in the road, cut flat by it; with the fix they stand on it, round, the car 8 cm higher on
them. The Raptor rises a little (26 mm). The Mini (0.229 m tyres on the Cygnet's 0.315 m
cylinders) floated above its shadow and now stands on the road. The Range Rover (22 mm)
changes little; vanilla 138 and 197 are the same in both runs. The bus's and the T440's
side shots show no wheel (the view's camera sits too low for them). Driving
(`vm1-20261030-074743` master, `-075253` the fix, throttle held 4 s): all six drive off,
no crash.

## Body height and wheelbase (2026-10-31, issue #406)

User report on the full-roster build of master 5fef56e: VW_BEETLE_68 (FH1, host 257) has
all four wheels tucked up into its fenders, the body sitting on the tyres;
AST_V8Vantage_77 (FM4, host 125) has its front wheels too far back in their arches.
Scratch scripts in `out/w406/` (left in the PR's worktree): `arch.py` (arch fits),
`sources.py`, `arch_after.py`, `maxdata_y.py`, `dims_diff.py`, `rest_model.py`.

### What the converter placed, and where Forza's arches are (confirmed-static)

`arch.py` fits each wheel arch of the Forza body (LOD 1, vanilla sections): body points
outboard of the tyre's centre plane seen from the side, the nearest one per 5-degree ray
from -70 to +70 degrees about straight up (the arch lip), a circle fitted to them, the
centre moved there, four rounds. Over the roster (master's hubs, mean of both sides) the
arch centres lie along the car within -6..+4 mm of the hub (quartiles; median -1), so a
miss along the car is reliable; their height spreads -25..+8 mm (median -7), since arches
are not concentric with the tyre, so only large height misses count.

| Car | Hub (master) | Arches against it | Cause |
| --- | --- | --- | --- |
| VW_BEETLE_68 | y 0.164 above the model's 0 (ground at -StockRideHeight -0.158, tyre 0.3145) | centres 171 mm (front) and 156 mm (rear) below | MAXData's `BottomCenterWheelbasePos` y is -0.158: the body's bottom (its floor reaches -0.143 to -0.169, below the ground master used) |
| AST_V8Vantage_77 | front z -1.334, rear +1.218 (MAXData wheelbase 2.551, midpoint -0.058) | front 78 mm ahead, rear 5 mm ahead | MAXData's wheelbase and midpoint are not the body's: gamedb `Data_CarBody` has 2.602 and -0.099, 11 / -11 mm off the arches |

The engine's rest offset is not the cause: on both cars `settle_wheels` raises the hubs
by what the rest model says the engine draws them low (`rest_model.py`: Beetle 43 / 38 mm,
Vantage 23 / 37 mm), and the renders of master's build with Forza's own geometry on those
hubs (`render_compare`, `out/rc/w406-master`) show the same tuck and offset as the game.
Forza's own thumbnails show the Beetle high on its wheels and the Vantage's front wheel
centred in its arch.

### The two sources (all 573 roster cars)

- `Data_CarBody.BottomCenterWheelbasePosZ` (gamedb.slt) is MAXData's z negated on 558
  cars, the converter's `mid_z` as it is. "Wheels off their arches" (#301) compared it
  without the sign flip and wrote that it differed on 461 cars; it differs on 15, the
  wheelbase on 22.
- Where they differ (35 cars more than 10 mm apart on an axle), the arches pick the
  gamedb on 23 (PAG_Huayra_12 -94 / -154 mm to -2 / 3, ASC_KZ1R_12 -78 / -133 to -1 / 6,
  SAL_S7_04 -178 / -380 to 6 / 9, BUS_TOUR_12 -158 to -2, CIT_C1_11 166 / 186 to -12 / 8,
  AST_V8Vantage_77 78 / 5 to 11 / -11, VIP_Viper_13, JAG_XKRS_12, LEX_CT200h_11,
  LAM_LP7004_12 rear -71 to -12, ...), 4 alike, 3 off by up to 20 mm (the two 1965
  Minis' rear -15, EAE_Speedster_12 front -20, the Beetle's front -19), and the five
  Commodores alike once the gamedb's midpoint is taken as it is: it already sits at the
  tyre (-0.068 against MAXData's +0.300 offset's -0.053).
- The handling's wheelbase (VO3D p392) has been the gamedb's `ModelWheelbase` all along
  (`handling.py`); vanilla has it equal to the hub wheelbase on 140 of 140 vehicles, so on
  those 22 cars it didn't match the hubs.
- Height: on the 15 cars with a negative y, the gamedb either has the same y and ride
  height or folds y into its ride height (y 0): VW_BEETLE_68 0.314 against 0.158 + 0.158,
  AUD_A4Avant_04 0.170 against 0.161, FIA_595_68, FOR_FocusZX4_05, TOY_Camry_07,
  TOY_RAV4Sport_04 the same way within 20 mm; CHE_CobaltSS_10 drops y (30 mm). The 9 with
  a positive y (+0.024 to +0.031: BUG_Veyron_09, BMW_Z4_11, FER_575_02, MAZ_Furai_08,
  RAD_SR8LM_10, FOR_40_GTR_09, ...) have their floors below 0, not at y; raised by y, the
  Furai's floor would be 8 mm into its ground. Elsewhere the gamedb's ride heights differ
  from MAXData's on 62 cars, and the arches don't decide between them (35 for the gamedb,
  15 for MAXData, mostly race cars, 30 alike): left open (#417).
- Track: the gamedb's differs from MAXData's by more than 10 mm on 67 cars, by up to
  0.32 m (SAL_S7_04), off the bodies: not used.

### Fix (`forza2dsf.car_dims`, `cached_gamedb`)

- Wheelbase and midpoint from the car's game's gamedb `Data_CarBody` (`port_car` now
  extracts the gamedb before measuring the car; other callers find the cached copy beside
  the game's car folders), MAXData's (plus the wheel model's own z offset) without a row.
  `MID_Z_FIXES` (the C1) is gone: the gamedb has the C1's own midpoint.
- The ground is the ride height plus a negative `BottomCenterWheelbasePos.y` below the
  model's 0 (`CarDims.ride_height`).
- Track and ride height otherwise stay MAXData's.

58 cars change (`dims_diff.py`): 47 by more than 10 mm or a wheel swap; 38 along the car
by more than 10 mm, 15 in height (the cars with a negative y: the Beetle 158 mm,
TOY_RAV4Sport_04 70, AUD_A4Avant_04 55, ...). The arch survey
(`arch_after.py`, master's build against the new hubs): cars with an axle more than 40 mm
off its arch along the car 13 -> 3 (HUM_H1Alpha_06, KEN_T440_12, SHE_GT500KR_68: both
sources agree there; square arches), more than 25 mm 28 -> 13; the Beetle's arch centres
-171 / -156 mm -> -13 / 2 mm. A little worse: the five Commodore VEs -5 / 6 -> -20 / -9 mm
(the gamedb's midpoint is 15 mm behind MAXData's plus the wheel model's offset), the two
1965 Minis' rear 2 -> -15, EAE_Speedster_12's front 2 -> -20, the Beetle's front -6 -> -19.

SAL_S7_04 (FH1, host 162; added to #406: wheels off-centre, a large gap behind the rear
wheel) is the same cause: MAXData's wheelbase is 204 mm short and its midpoint 287 mm off;
its arches lay -178 / -380 mm off master's hubs, 6 / 9 mm off the gamedb's. The host is not
it: of the 56 ports on host 162, 53 were within 25 mm of their arches along the car on
master; the others are ASC_KZ1R_12 (-78 / -133 -> -1 / 6, the same cause) and SAL_2_S7_03
(25 / 25 -> 7 / 7).

### In game (headless VM 1)

Built from master (`out/w406/vm-before`, `vm2-before`) and with the fix (`vm-after`,
`vm2-after`), `-Views wheels,wheel,wheel+mph35[,wheel+mph60] -Palette 0`: IDs 1
VW_BEETLE_68, 2 AST_V8Vantage_77, 3 MER_SLR_05, 4 MER_SLRStirlingMoss_10, 5
FOR_FocusRS500_10 and vanilla 257: `vm1-20261031-045849` (master), `vm1-20261031-050530`
(fix); IDs 1 SAL_S7_04, 2 FOR_FocusRS500_10 and vanilla 162: `vm1-20261031-054026`
(master), `vm1-20261031-054347` (fix). The fix's builds also carried the blur-disc change
then under review for #407 (branch `fix/wheel-blur-disc`), which changes only the disc. The Beetle stands on its wheels (master: body on the
tyres, wheels in the fenders); the V8 Vantage's front wheels move forward to the arch
centre; the S7's wheels sit centred in both arches, no gap behind the rear wheel; the SLR's
wheels draw their rims and tyres instead of grey discs; the vanilla cars are the same in
both runs.

## Wheels without a tyre in the main carbin (2026-10-31, issue #407)

MER_SLR_05 (FM4, host 226) drew grey discs for wheels. Its main carbin's `wheel` section
is one mesh per LOD on the technique `4_MER_SLR_05_damage`: no tyre, rim or blur_rim, so
the rims got no texture in the atlas (`wheel: 0 regions`) and the blur disc the vanilla
map. AST_V12Zagato_12's is `body` and `interior` at every LOD. Both cars' wheel archives
(`wheel.carbin`) hold the real wheel (tire, rim, inner_rim, blur_rim, the same tyre size).
Survey of the 573 main carbins (`wheel_sections.py`): these two have no tyre technique at
any LOD; DOD_StealthTurbo_96 has a tyre and draws its rim as `wheel_emblem` (handled,
"Speed blur"). MER_SLRStirlingMoss_10 has a proper wheel.

Fix (`forza2dsf.load_car`): a main-carbin wheel without a tyre at any LOD gives way to the
wheel archive's.

## Wheel fit (2026-10-31)

The user found SAL_S7_04's wheels still misaligned in #421's after shot and asked for a
measure instead of judging by eye. `dsff wheel_fit` (`src/dsfforza/convert/wheel_fit.py`)
measures each wheel of a **built** archive against its arch as the game draws it. Scripts in
the PR's worktree, `out/wf/`: `validate.py` (the in-game check), `analyse.py` (flags and
the sources' what-if), `grid.py`.

### What it measures

- The wheel at rest: the hub table's hub moved up by the engine's static wheel offset
  (`wheel_clearance.rest_offsets`, the model `settle_wheels` uses), plus the tyre's centre
  offset; radius, width and centre from the drawn wheel part (`wheel_clearance.load_car`).
- The arch as seen from the side: an orthographic side view of the clean exterior entry's
  body, per 5 mm cell the outermost surface, the tyre a disc at its outer face hiding what
  lies inside it and inboard of that face (`side_depth`). Along a ray out from the tyre's
  centre the surface steps out from the wheel well (or nothing) to the fender; the edge is the
  first climb at least half as high as the ray's biggest, at half its height (`step_edge`):
  the 50% point of a step, with no tuned depth. Two earlier definitions failed in the shots:
  the nearest body point per ray was often the well's liner, 43 mm inside the S7's visible
  arch top; the largest step per ray jumped to wider bodywork farther out (shoulder lines,
  a mid-engined car's cabin sides: the Gallardo LP570's front read -151 mm lateral, -20 with
  the first-step rule).
- Per wheel: `fore_aft_mm` (the median midpoint of the arch's front and rear edges at 0.3 to
  0.9 tyre radii above the tyre's centre, against the tyre's centre; + ahead), `top_gap_mm`
  (tyre top to the edge straight above), `lateral_mm` (the tyre's outer face against the
  crest of the edge step, the fender line, within 60 degrees of straight up; + outboard), and
  a circle fitted to the edge (centre height, radius, rms) as a description of its shape.

### Against the game (confirmed-runtime)

Headless VM 1, `run-series -Vm -ProbeIds '313,51,9,162,257' -Views 'wheel,wheels'
-ShotSeconds 3 -Palette 2` on the #421 build of the S7 04, Beetle 68 and V8 Vantage 77
(`out/w406/rc-after`): `vm1-20261031-075759` (light paints: yellow, blue, light yellow); the
white vanilla 162 from `vm1-20261031-054347` (palette 0). The probe's `wheel` view is a fixed
camera in the car's frame, from (2.5, 0.5, 1.3) to (0.75, 0.35, 1.3); its field of view is
the camera system's current one and the car frame's height against the model isn't known, so
each shot fits its own camera to the tyre where wheel_fit puts it (3 unknowns: the frame's y
and z offset, the focal length; from the rim's centre and the tyre's ground contact picked in
the shot). Then the body's visible edge is predicted from that camera (a z-buffer of the body,
the same first-step rule in the camera's view) and compared ray by ray (every 5 degrees from
80 behind to 80 ahead of straight up) with the shot's edge (the first strong rise in
brightness out of the dark well). Over the arch's top (within 30 degrees of straight up),
seen minus predicted, median per car:

| Car | Rest offset (front) | Over the top | All rays (median, MAD) |
| --- | ---: | ---: | --- |
| VW_BEETLE_68 (port) | -43 mm | +14 mm | +30, 18 |
| vanilla 257 (VW Beetle 1963) | -43 | +19 | +22, 2 |
| AST_V8Vantage_77 (port) | -23 | +18 | +29, 9 |
| SAL_S7_04 (port) | +5 | +22 | +33, 10 |
| vanilla 162 (Corvette Z06) | +20 | +51 | +59, 15 |

- A common +14..22 mm: the shot's brightness edge (dark well to lit paint, where the lip's
  curl turns to the light) lies outside the geometric half-step on every car, vanilla
  included. On the Corvette the lip is a grey flange about 50 mm wide; the shot's edge is its
  outer border, the geometric edge its inner one (`out/wf/overlay-vanilla162.png`).
- The rest-pose model: from rest offsets of -43 to +5 mm the over-the-top offset moves 8 mm
  (14 -> 22); were the engine drawing the wheels at the hub table (no rest offset), it would
  move by the full 48 mm. The model holds to about 8 mm, a sixth of the offset.
- The predicted tyre circle, the predicted arch outline and the shots overlay closely
  (`out/wf/overlay-*.png`); fore/aft and lateral are not checked separately (a side view
  doesn't show lateral).

### Vanilla yardstick

`wheel_fit survey` of the 140 four-wheeled vanilla vehicles; the cars and SUVs (120, without
trucks, buses, vans, pickups and the trailer), 5 / 50 / 95 %:

| Measure | Front | Rear |
| --- | --- | --- |
| fore_aft_mm | -12.5 / 0 / +16.6 | -17.5 / -1.2 / +12.5 |
| top_gap_mm | -18.2 / +29.3 / +117.8 | +3.4 / +40.1 / +141.3 |
| lateral_mm | -99.9 / -21.3 / +16.2 | -90.7 / -23.8 / +20.2 |

The upper tails are SUVs and 1950s-60s cars (skirts, tall arches); vanilla's lateral reaches
-180 mm (Countach, Diablo VT flares).

### The S7 04 after #421

| Wheel | fore_aft | top_gap | lateral |
| --- | ---: | ---: | ---: |
| front | +25 (just past the 95%) | +131 (vanilla 95%: 118) | -156 (5%: -100) |
| rear | +5 | +129 | -162 (5%: -91) |

Fore/aft is fixed since #421; its tyres sit 0.16 m inboard of the fenders and 0.13 m below
the arch tops (`out/wf/overlay-S7_04.png` and the side view: wheel well around the whole
tyre). Cause (confirmed-static): SAL_S7_04's `Physics/MAXData.xml` isn't the S7's. Its
wheelbase 2.470, track outer 1.613 and ride height 0.190 / 0.177 match neither its own gamedb
row (2.674, 1.935, 0.107) nor the car (wheelbase 2.667 m, 1.99 m wide); #421 took wheelbase
and midpoint from the gamedb, track and ride height still come from this file. With the
gamedb's track and ride height the same wheels would read lateral +4 / -1 and top gap +48 /
+59, inside vanilla's range.

### The roster (#421's cars, `out/w406/rc-after`, over master's build for the rest)

148 of 573 ports have a wheel outside vanilla's 5-95 % on a measure:

- 47 have MAXData and gamedb track or ground more than 10 mm apart; on 37 of them the
  gamedb's values would bring every flagged lateral and top-gap reading inside (SAL_S7_04,
  ASC_KZ1R_12, AST_V8Vantage_77, the five Commodore VEs' front lateral, KEN_T440_12,
  PAG_ZondaC12_99's rear, FOR_ShelbyGT500_13, TOY_6_Supra_05, ...), on 10 not
  (`out/wf/flagged.json`).
- 101 have agreeing sources: 45 only fore/aft, mostly within 10 mm of the band (-15 to -28);
  31 only top gap, mostly rear wheels a few mm under 0 (the F430 GTs' -17, Miura -34) and the
  tall SUVs (Raptor +170 / +240, Yukon +120 / +150); a few large fore/aft readings on square
  or skirted arches (FOR_Explorer_10 -192, VOL_XC70_12 -155, TOY_CorollaDX_95 -128,
  AUD_RS2_95 -118, FER_250TestaRossa_57 -200) and the muscle cars' rear lateral (-95 to -113:
  Cuda, Charger Daytona, 442, GTO Judge, Cougar Eliminator, Mustang King Cobra), where the
  two sources agree; not explained here.
- Taking the gamedb's track and ride height for every car that has both would put more wheel
  readings in range on 28 of the 122 cars where they differ by more than 10 mm, fewer on 9
  (AUD_A4Avant_04, CHR_JeepGCSRT8_12, the BMW M3 GTRs, KOE_CCGT_08, PAN_51_Esperante_05,
  BUS_TOUR_12, OPE_5_AstraV8_03, VAU_VX220_04) and the same on 85. Neither source is right
  for every car, so no converter change here (#417).

## Track and ride height per car (2026-10-31, issue #417)

After #421 the wheelbase and its midpoint come from the gamedb's `Data_CarBody`; track and
ride height still came from `Physics/MAXData.xml`. `wheel_fit` ("Wheel fit") showed where
that fails: SAL_S7_04's tyres 0.16 m inboard of its fenders and 0.13 m under its arch tops,
because its MAXData.xml is another car's. But the gamedb isn't right everywhere either: of
the 122 cars whose two sources differ by more than 10 mm, the gamedb's values put more wheel
readings within vanilla's range on 28 cars, fewer on 9 (AUD_A4Avant_04's tyres 30 mm outside
its fenders with the gamedb's track, CHR_JeepGCSRT8_12's body 110 mm higher over its wheels
with its ride height) and the same on 85 (`out/wf/` of #436's worktree).

### Rule (`forza2dsf.car.arch_fit`, `car_dims`)

When the two sources' track (front, rear) or ride height differ, `car_dims` measures both:
the wheels at each source's track and ride height (the axles' Data_Car tyres as `convert`
draws them) against the Forza body (LOD 1, the exterior's) with `wheel_fit.measure_wheel`,
at Forza's static pose (which `settle_wheels` keeps at rest in game). Each wheel's top gap
and lateral offset is a reading; it counts if it lies within the vanilla cars' 5-95 %
(`wheel_fit.VANILLA_BAND`, "Wheel fit"). The gamedb's track and ride height are taken only
when they give more readings in range; a tie keeps MAXData.xml's (the build log's placement
line names the source and both counts). No margin is tuned: the band is vanilla's, the
decision a count.

### Roster (offline, all 573 cars)

38 cars take the gamedb's track and ride height. Built with master and with the change
(`out/w417/before`, `after`; NIS_370Z_10 and VW_BEETLE_68 as controls) and measured with
`wheel_fit` on the built archives: 32 have more readings in range (or fewer mm outside), none
fewer, 6 were in range and stay there (CAD_CTSV_09, CHR_Crossfire_06, FER_Enzo_02,
MER_SL65AMG_09, VAU_VX220_04, VIP_Viper_13); the controls are unchanged. SAL_S7_04: 2 of 8
readings in range (282 mm outside) -> 8 of 8, front lateral -156 -> +5 mm, top gap 130 -> 56;
ASC_KZ1R_12 6/8 -> 8/8 (-97 / 103 -> 13 / 38); RUF_Yellowbird_87 2/8 -> 8/8 (rear top gap
273 -> 13); AST_V8Vantage_77 4/8 -> 8/8 (top gap -42 -> 38). Still outside after the change:
AUD_8_TTR_04's rear top gap (-5, 6/8), FER_Enzo_02's front lateral (-146, not a source
question: both agree there) and PAG_Huayra_12 (7/8). Cars where neither source brings the
wheels into range keep MAXData.xml: AST_DB5Vantage_64, BUS_TOUR_12, EAE_Speedster_12,
JAG_Etype_61, PEU_205T16_84 (and its ShowCase copy), and the others of #436's list whose
sources agree.

## Open

1. Brake calipers (Forza `_caliper*` carbins) are not converted: they must not
   turn with the wheel, and the exterior entry has no hierarchy part on the hubs
   to carry them ("Brake rotors", "Calipers").
2. Blur disc brightness (#407): the disc takes Forza's picture times the rim's
   PaintScale over a fixed mid-grey backdrop, so FOR_FocusRS500_10's disc is near
   white on black rims and dark rims' discs are grey. A rule waits on how Forza draws
   `blur_rim` and how DSF draws its disc (RE in progress; branch `fix/wheel-blur-disc`
   holds a fitted rule, not merged). VW_BEETLE_68's disc lays out its brightness
   unlike its rim (#419).
3. A positive `BottomCenterWheelbasePos.y` is unexplained ("Body height and wheelbase");
   track and ride height are chosen per car since #417 ("Track and ride height per car").

Done since and dropped from this list: brake rotors (#212, "Brake rotors"); speed blur (#211, "Speed blur"); rim gloss (issue #108, PR #143). Rims
draw with gloss copies of the host's Lights material (its lamp glow needs a
lamp byte, which rims don't have), Forza's rim reflection and `chrome_rim`'s
chrome on them (forza-materials.md, "Gloss"). The reflection is masked by the
texel's green, not `wheel.xds` alpha. Vanilla rims have no reflection: their
caps and logos are on the car's own Chassis material, the rest on
`GLOBAL_Tyre` ("How vanilla DSF wheels are textured").
