# Wheels: the Forza wheel model in DSF

Updated: 2026-10-23

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

## What was wrong (all hosts)

| Defect | Cause | Fix (`forza2dsf.py`) |
| --- | --- | --- |
| Every wheel showed the back of its rim outwards | the Forza wheel was copied unturned: face at -x, and DSF's quaternions put a model's +x side outwards on both sides | `wheel_point` / `wheel_normal`: 180 degrees about y, like the body |
| Tiled garbage on tyres and rims | Forza's tiling UVs sampling DSF's tyre atlas | `wheel_role` + `WHEEL_UV`: one uniform swatch per part: rubber (0.5, 0.008), rim grey (0.906, 0.008, the Desanne's), inner rim dark (0.4375, 0.055), chrome (0.117, 0.008) |
| Flat discs and broken rings over the rims (Cuda, Veyron) | blur geometry kept: only names starting with "blur" were dropped, `chrome_blur_lip` wasn't | everything with "blur" (and shadows, alpha-cut emblems) is dropped |
| Veyron, Huayra, Gallardo rear wheels too small and narrow, rear track wrong | one front-sized model on all four hubs, front track for both axles | `axle_tyres` (gamedb `Data_Car` tyre sizes): on hosts with separate front/rear wheel parts the rear copy is scaled (Veyron 1.38 wide, 1.07 radial); rear hubs go to `RearTrackOuter` (`CarDims.rear_track_outer`) minus the rear tyre width, and up by the radius difference so all four tyres stand on one ground line (`patch_hubs` rear_half_track / rear_hub_y; `HostWheels.axles` maps each part to its axle) |
| Wheels off the arches by up to 0.43 m (Cuda, Mustang, Veyron) | `BottomCenterWheelbasePos.z` sign (fixed by the damage work in `car_dims`, see `forza-sources.md`) | not part of this fix |

On hosts with one wheel part for all four (most), the rear wheels stay
front-sized; the rear hubs still go to the rear track.

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

Atlas cost: one region per car, the whole wheel texture; the uniform scale of
the other regions drops on cars whose atlas was full (Raptor 0.67 to 0.61).

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
