# Wheels: the Forza wheel model in DSF

Updated: 2026-10-26

User report from driving the testdrive build (`out/testdrive`, 40 cars): the
wheels look broken on every car, the Veyron worst. Offline investigation
(no game run); tools: `out/wheels/render.py` (exterior plus the wheel
part(s) placed the way the engine does it: hub + quaternion x wheel-local
point), `out/wheels/tyre_uv.py`, `out/wheels/tex_dump.py`. Evidence levels
follow `dngvehicles.md`.

## Stock DSF wheels (confirmed-static)

- One wheel model per MODL part (33 hosts keep front and rear in two parts),
  in its own frame: hub at the origin, spin axis x, tyre centred on x = 0.
- The UPVH wheel table gives each wheel a hub position and a quaternion:
  identity on the +x wheels, (0, 1, 0, 0) (180 degrees about y) on the -x
  wheels. One model serves both sides, so it is authored for the +x side:
  centre cap, spoke disc and blur disc sit at x +0.05 to +0.12 (227, 266,
  189).
- Materials: `GLOBAL_Tyre` (stride 20; on the Desanne the whole wheel, rim
  included), `GLOBAL_Wheel_Blur` and `GLOBAL_Wheel_Blur_SpokeDisc`
  (alpha-blended speed blur; vertex semantic 9 byte 3 is 0, 1 or 2 on
  them, probably static / always / blurred), sometimes a centre cap on the
  car's Chassis material. All tyre-texture UVs point into one shared
  512 x 512 DXT1 atlas (`00122b7bf7fa1227`): red is the grey level, green the
  gloss, blue 255 (so it decodes blue). It holds sidewall half-rings, tread
  strips and uniform grey/gloss swatches in the corners; the Desanne's rim
  uses the swatch at (0.906, 0.008) (grey 131, gloss 190).
- No brake discs or calipers in stock wheel parts.

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
  separate carbins (`<car>_rotorLF_LOD0.carbin`, `_caliperLF_...`), not
  converted.
- Exceptions on the discs (full-roster survey, all 573 main carbins):
  - six main carbins have no wheel section: FER_360_99, FER_360Challenge_03,
    FER_Dino246_69, LAM_Gallardo_05, PAN_Esperante_05 and
    CHE_CORVETTEGSC_10. Their wheel is only in the wheel archive's carbin
    (`media/wheels/<car>`, one section `wheel`, plus LOD 0 with a skinned
    pool);
  - the two 1965 Minis have `wheela` and `wheelb` instead, like upgrade
    variants.

  `forza2dsf.load_car` takes `wheel`, else the stock variant (`wheela`, which
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
tread and sidewall detail of stock tyres; the RX-7's wheels are pale grey
blobs with no rim, apparently bulging past the arches.

### Stock tyres (confirmed-static, 30 hosts: `out/wheels2/stock_side.py`)

`GLOBAL_Tyre` has only a diffuse map (`g_texDiffuseMap` 00122b7bf7fa1227
and a cube map); no normal map, and the stride-20 wheel vertices have no
tangents. All the tyre detail is in the diffuse atlas, reached through UVs
(`out/wheels2/tyre_r.png` is the red channel, the grey level):

| Region | UV | Stock use |
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
- `TyreMap` unwraps the tyre onto the stock regions: triangles whose face
  normal is within about 55 degrees of the axle go on the sidewall ring
  (ring radius 0.33 at the tyre's inner radius to 0.492 at its outer, 180
  degrees of tyre per half-ring, lettering included), the rest on the fine
  tread strip (u across the tyre's width, v one tile per ~0.09 x width/0.24 m
  of circumference: 8 to 31 tiles). Tile edges snap to the model's own vertex
  angles so no triangle straddles a seam. Rims keep their swatches.

### Test set (offline-checked; in game pending)

`out/wheels2/build/dngvehicles.sp` (`port_batch.py build --car MAZ_RX7_97
--car BMW_M5_12 --car VW_GolfR_10 --car DOD_ChargerDaytona_69@fm4 --port
"--mdxn-limit 0x2C0000 --damage"`; all four lint ok): IDs 1 RX-7, 2 M5,
3 Golf R, 4 Charger Daytona (FM4). The Golf R's full-entry wheel part went
from 0 triangles per LOD (testdrive3) to 2863.

Renders (`out/wheels2/render.py <archive> <id> <prefix> [kind] [mask]`, the
tyre atlas shown grey): `out/wheels2/local-before-after.png` (each wheel
model from outside, inside and the front; testdrive3 against wheels2, stock
370Z for reference), `out/wheels2/after/id3-full4-sides.png` (Golf R full
entry, mask 4).

In-game check, once the sandbox works:

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

### How stock DSF wheels are textured (confirmed-static)

`out/rims/stock_wheel_mats.txt`: the wheel parts of all 140 four-wheeled
stock vehicles use `GLOBAL_Tyre` (tyre and most of the rim, on the shared
atlas's rim and nut pictures) and the two blur materials; 110 of them also
have 4 to 244 triangles on the vehicle's own Chassis material (stride 28) in
both entries: centre caps and logos from the car's chassis texture.

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
  material with the stock unwrap; the swatches remain the fallback when there
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
against `vm-20261024-064427` (`out/rims/td`), IDs 147 (stock CTS-V for
reference), 25, 38, 28, 6, 29. The rims draw their own artwork through the
Chassis material: the 250 GTO's wire wheels with the knock-off cap, the
Raptor's dark rims with the SVT cap, the Veyron's orange (a little lighter
and redder than Forza's), the WRX's gold, the Agera's black rim with its
yellow and blue stripes; no flicker or missing triangles, brightness next to
the stock CTS-V's wheels alike. `img/rims-ingame-before-after.jpg`.

## Wheel shading (2026-10-24)

User report on `out/testdrive11` (ID 18, PEU_205T16_84 on host 274): "plain
white wheel disks". Scripts in `out/inv/` (`wrender.py`, `wobl.py`,
`stock_sky.py`, `stock_sky_agg.py`, `sky_check.py`).

### The 205's wheel is white, and the texture is applied (confirmed-static)

- `wheel.xds` (256² DXT5) is near-white everywhere: shading of the disc, the
  12 holes, the diamond recess and the nuts. FH1's thumbnail
  (`Thumbnails/Thumbnail_1533.xds`) shows the car on off-white wheels with
  dark holes; the road car's are white too.
- The converted exterior wheel part, drawn with the build's own atlas, matches
  Forza's wheel with `wheel.xds` texel for texel (`out/inv/205_wheel_cmp.png`):
  the UVs and the atlas region (0.551x) are right. Forza's wheel archive holds
  no shader settings for the rim; nothing tints it.

### What differed: the baked sky term (s0.w)

Forza's wheel vertices have no AO byte (0 on every vertex of the 40 testdrive
cars' wheels), so `vertex_sky` gave them `DEFAULT_AO` x the hemisphere factor
of the wheel-local normal. Stock wheels bake their own occlusion. Exterior
wheel parts of the 140 four-wheeled stock vehicles (median of per-vehicle
medians), against `out/testdrive11` IDs 18, 25, 6:

| Surface (DSF wheel frame) | Stock | testdrive11 | Fix (40 cars) |
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
`WHEEL_SKY_OPEN` (0.64, stock's outward faces) x the share of 24
cosine-weighted rays over its hemisphere that escape. Rays are marched through
a 3 mm voxel grid of the wheel's own triangles at that LOD (hits closer than 3
voxels are the vertex's own surface); rays heading inboard end on the car, and
rays leaving past the tyre's radius count 0.5 (arch above, road below). The
voxel march agrees with exact ray-triangle tests to 0.03 on average
(correlation 0.97, the 205 at LOD 1) and takes about 0.4 s per wheel LOD
instead of 10 s. The tyre texture, UVs and materials are unchanged.

In game (confirmed-runtime, headless VM, new probe view `wheels`: the car's
left side at hub height): `vm-20261024-110508` (`out/testdrive11`, ID 18)
against `vm-20261024-105751` (`out/fx/b2`, ID 1; stock Scirocco 274 in the
same run). The 205's holes and the recess round the nuts now read dark, as
through the stock Scirocco's spokes; the disc itself stays white, as Forza's
is (`img/wheels-205-ingame-before-after.jpg`).

### Open (wheels)

- The brake rotor (`<car>_rotorLF_LOD0.carbin`: disc and hub, about 1100
  triangles on the 205) could go into the wheel part; it turns with the wheel,
  unlike the caliper. With the barrel's inside dark it matters less.

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
  1050 kg on p1 2100) 39 mm at the front, 33 at the rear. Over the stock
  fleet the offset is small (median +4 mm front, -8 mm rear), and Reflections
  modelled each stock body around its own rest pose anyway.
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
  Commodore's in front of its bumper) and wobbled as they turned.

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
  12 to 40 mm more sink that the model doesn't explain, stock cars included
  (stock 127: 19 and 18 mm; 162: 12 and 16); it doesn't show as a wheel offset
  in the shots, so it is more likely the ground data than the wheels.
- Handling (dyno, `vm-20261025-091055` before, `-091517` after, the
  `handling_tune` test): A4 0-60 2.71 / 2.70 s, 0-100 6.90 / 6.90, 60-0
  18.7 / 18.9 m, 1 s step steer at 45 mph 56.0 / 56.7 deg/s and 1.89 / 1.85 g;
  F151 3.07 / 3.10 s, 6.64 / 6.70, 18.5 / 18.6 m, 61.3 / 60.9 deg/s. The
  handbrake flicks (62 / 54 deg on the A4, 59 / 62 on the F151) are within
  that test's 8 deg noise.

### Open (wheel clearance)

- Fixed ("Rear wheel parts" below): hosts with one wheel part for both axles
  drew all four wheels at the front tyre's size, so 63 cars with a bigger
  Forza rear tyre got a rear arch gap larger than Forza's (JAG_33_XKR_10: 50
  mm against -8).
- Full lock: 41 cars still put a front tyre more than 10 mm deeper into the
  body than Forza's tyre does at Forza's own lock; 27 of them steer further in
  DSF (p298 40 or 45 deg from the steering donor against Forza's 30 or 36:
  the race prototypes, TOY_3_GT1_99, FOR_40_GTR_09). Capping p298 at Forza's
  angle would cure them, but p298 also scales the steering response, so it is
  a handling change and not made here.
- Full bump (p188): most cars put a tyre into the body at the top of DSF's
  visual travel, as Forza's would at its MaxCompressHeight; not changed.
- The rest-pose sink against Atlas's ground beyond the model (above).

## Rear wheel parts (2026-10-26, issue #53)

107 of the 140 four-wheeled stock vehicles draw all four wheels from one MODL
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
the end of the exterior's (`add_modl_part`), each under a new stock-shaped
part UID (`Builder` UIDs from the conversion seed, its own prefix); then
points the rear wheel records at the full copy and their LOD links at the
exterior copy. `host_wheels` then sees a split host, and the existing rear
path scales the rear copy to the rear tyre, moves the rear hubs to the rear
track and stands them on Forza's ground. Run on every stock four-wheeled
vehicle in memory (`out/rw/check_stock.py`): the 107 shared hosts all come out
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

## Open

1. In game: whether wheels without blur geometry look right at speed
   (stock wheels fade a spoke disc in; ours have none).
2. Brake discs and calipers (Forza `_rotor*`/`_caliper*` carbins, LOD 0 only,
   per corner) could go into the wheel part as static geometry; not done.
   Stock DSF wheels have none, and a caliper in the wheel part would spin
   with it.
3. The Chassis material has little specular; Forza's chrome rims are
   reflective (`wheel.xds` alpha). The Lights material reflects but adds lamp
   glow through its alpha.
