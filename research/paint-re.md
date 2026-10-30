# How DSF draws car paint, and what Forza's paint data means (reverse engineering, round 2)

Updated: 2026-10-30

How Driver: San Francisco PC 1.04 turns a palette entry into paint on screen by day: the
paint shaders' maths, the runtime constants they get, the post chain after them, and what
Forza's colour data (`Combo_Colors`, the paint shader settings) holds. Each formula has its
evidence and a confidence level, as in `lights-re.md`: `confirmed-static` (read in the
disassembly or the data files), `confirmed-runtime` (read back from the running game, with the
run folder), `inferred` (follows from the above, not checked directly). Nothing here changes
the converter; the last sections say how it should set each palette parameter and which of
today's tuned constants that replaces.

Round 1 (`lights-re.md`, #365) supplied the shader extraction (`tools/re/gpch.py`, the dumps
of the 198 vehicle programs), the post chain (exposure, Reinhard, grade, sRGB) and the mood
table. This round reused its dumps of GPCH 14 to 31 and its post-chain code
(`lamp_model.exposure`, `tonemap`, `srgb`); it did not re-extract anything.

Tools added with this note (`tools/re/`):

- `paintprobe/`: a research plugin (`paintprobe.asi`, `build.ps1`; never installed). It hooks
  `Direct3DCreate9`, creates the device without `D3DCREATE_PUREDEVICE` so state can be read
  back, and for every draw whose pixel shader is a paint, chrome or glass program (by DSF
  Enhanced's FNV hash of the bytecode) logs the pixel constants c0..c55, the vertex constants
  (panel transforms c0..c11, camera c96..c111), the sRGB-read state and the textures of
  samplers 0..5. Every few seconds it also writes the scene target the paint went into (PFM,
  linear float), the back buffer (BMP) and the textures at samplers 1 and 2 (environment and
  light map). `[probe] hooks=` picks how it hooks (below, "The probe").
- `paint_model.py`: the formulas as code. `curve` prints one palette entry's colour by angle,
  sun and shade; `log` the constants the probe read; `frames` rasterises the car's clean
  exterior with the game's own matrices, predicts every paint pixel and compares it with the
  dumped frame; `target` gives the albedo that shows a Forza swatch (the mapping in section
  6); `grade` fits the colour grade across channels from dumped frame pairs
  (`paint_grade.json`). `screen` uses the game's own grade LUT, read from `DSF_GAME_DIR`
  (`game_grade`), and falls back to that fit.
- `forza_paint.py`: Forza's paint settings per car colour, read from the disc images in place
  (`survey`).

## The whole chain in one place

Per paint pixel of the player's car (GPCH 26), linear light, ClearDay as the game sets it:

```text
f      = 0.01 + 0.99 x max(1e-4, 1 - sat(N.E))^fresnelpower          (bias as stored)
A      = Tint2 + f x (Tint3 - Tint2)                                   the paint's albedo
D      = PSSM shadow x (1 + 0.66 (ao - 1))                             1 in the sun, 0 in shadow
light  = ao x ((upper + lower)/2 + (upper - lower)/2 x N.a)  +  max(N.(-L), 0) x sun x D
spec   = D x 0.565 x vehSpec x ( (R.E)^1500  +  s2i x (R.E)^s2p x A )   R = reflect(specDir, N)
refl   = sat(0.565 x max(0.025, sat(1 - N.E)^cfp - 0.5) x reflAmount
             x (0.638 D + 0.112 ao + 0.25) x 3 x env(R))               env = last frame's scene
HDR    = min(light x A + spec + refl, 6)
screen = sRGB(LUT16(Reinhard(e x HDR)))                                e 1.5..2 (lights-re.md)
```

With the ClearDay values (`sun` (3, 2.92, 2.55), `vehSpec` (40, 39.1, 30.2), ambient
`lower` (0.307, 0.347, 0.4) / `upper` (0.602, 0.687, 0.8)), a panel facing the sun gets about
3 times its albedo in linear light and one turned away from it 0.3 to 0.8 times (`ao` is
the vertex AO; the player car's light map is white, so the ambient amount is `ao`); the
reflection is a few percent of the pixel until the panel is seen at a steep angle. The palette colours are
linear albedos; nothing between the palette and the pixel gamma-corrects them.

## 1. The paint shaders

### Which program draws paint (confirmed-static, confirmed-runtime)

The shared paint materials and their eight passes (`gpch.py materials`):

| Material | Passes 0..7 | Used by |
| --- | --- | --- |
| `GLOBAL_Chassis` | 14 15 2 16 17 18 19 7 | the full entry: traffic, parked cars, the player's car when it isn't the clean exterior |
| `GLOBAL_Chassis_LOD1` | 26 27 10 28 29 30 31 32 | the clean exterior: the car the player drives (`vehicle-model.md`) |

The colour passes differ only in extras: pass 0 (14, 26) is the paint; pass 1 (15, 27) adds
the damage decal (`g_samDecalNormalMap`) and the Shift highlight; 16 / 28 the highlight
alone; 17 / 29 the decal alone; 18, 19, 30, 31 are the locked-zone versions (a progression
map instead of the reflection). The paint maths below is the same in all of them. The
exterior programs (26..29) add the shadow map (PSSM) and `g_hCustomFresnelPower`, and reflect
a spherical environment map instead of a cube map.

In the VM runs the probe saw pass 0 for the player's car (26) and traffic (14), and passes 1,
4 and 5 (15, 17, 18) for other traffic draws (run `vm1-20261030-152849`). The player car's
vertex shader at runtime is not GPCH 26's own (`8E0B446E`, not `905B712A`): DSF Enhanced
swaps in its own vertex shaders (DLAA jitter); its outputs and constant layout match, since
the projection below fits the frames to the pixel.

### The pixel shader, GPCH 26 (confirmed-static; constants confirmed-runtime)

Inputs from the vertex shader (`gpch026_vs_905B712A`): `v1` the normal after the panel
transform (object rotation included: the panel transforms carry the car's rotation), `v1.w`
the vertex sky / AO term (position.w / 4096), `v2` the eye vector (camera minus point),
`v2.w` linear fog, `v3` the light-map UV (world x, z times `g_vLightMapOffsetScale`), `v4`,
`v5` the eye and normal in camera space (for the environment map), `v6` the world position
(shadow map).

```text
N, E      = normalize(v1), normalize(v2)
fresnel   = g_fresnelbias + g_OneMinusFresnelbias x max(1e-4, 1 - sat(N.E))^g_fresnelpower
paint     = Tint2 + fresnel x (Tint3 - Tint2)                        c16, c17 = Tint3 - Tint2
tex       = g_samDiffuseMap(UV0)                                     the atlas paint block
A         = tex.r x (1 + tex.b x (paint - 1))                        CustomBRDFTinting_PS
A         = lerp(dirt colour, A, dirt mask); lerp(A, rust, rust mask)   no effect on a clean car
specMask  = tex.g x (1 - max(dirt, rust))
lm        = g_samLightMap(v3)
ambAmount = (lm.x x 0.4 + 0.6) x ao
dirAmount = lerp(PSSM, sat(2 lm.y), distance fade) x (1 + g_vDirectionalOcclusionAmount.x (ao - 1))
ambient   = ambAmount x (g_LightAmbVariationColBoundLower x N.(-g_LightAmbVariationDirection)
                         + g_LightAmbVariationColBoundUpper)
diffuse   = max(N.(-s_LightDirectionalDirection), 0) x s_LightDirectionalColour x dirAmount
R         = s_LightSpecularDirection - 2 (N.s_LightSpecularDirection) N
s         = max(R.E, 1e-4)
spec      = dirAmount x s_LightSpecularColour x specMask
              x (s^g_hSpecularPower + g_hSecondSpecularIntensity x s^g_hSecondSpecularPower x tex.b x A)
fr        = max(g_fCubemapFresnelMin, sat(1 - N.E)^g_hCustomFresnelPower + g_hReflectionFresnelBias)
shadowMod = dirAmount x g_vecShadowModifier.x + ambAmount x g_vecShadowModifier.y + g_vecShadowModifier.z
refl      = sat(specMask x fr x g_hCustomReflectionAmount x shadowMod x g_fTexMul2.z
                x g_samSphericalEnvMap(uv(R in camera space)) x g_hCubemapLuminanceBoost)
out       = (ambient + diffuse) x A + spec + refl
out       = lerp(out, s_FogColour, v2.w);  lerp(out, g_vec4FinalTintColour.rgb, .a)
oC0.rgb   = clamp(out, 0, 6)
```

The environment lookup (`uv`): the reflection of the eye about the normal in camera space,
`u = R.x / 2 + 1/2`, `v = 1 - (R.y / 2 + 1/2) - 0.5 x sat(WorldToCam[2].y)`, mapped into
`g_vec4ViewportBoundaries`.

GPCH 14 (the full entry) is the same paint, specular and lighting maths. It differs in three
places: no shadow map (the sun term is the light map's `sat(2 lm.y)` alone), a cube map
looked up along the world reflection vector, and a reflection Fresnel without the power,
`max(0.025, 1 - 0.5 - N.E)`. That is the exterior's term with `g_hCustomFresnelPower` 1, which
890 of the 946 vanilla entries use anyway.

The shared materials' own values (`GLOBAL_Chassis` / `_LOD1`, `out/re/matparams.py` over the
MARPs; the palette overrides the eight paint parameters):

| Parameter | Value | Note |
| --- | --- | --- |
| `g_hSpecularPower` | 1500 | the sun glint: a point about 2 degrees across |
| `g_fCubemapFresnelMin` | 0.025 | the reflection's floor (facing panels) |
| `g_hReflectionFresnelBias` | -0.5 | the reflection only grows where 1 - N.E > 0.5 |
| `g_hCubemapLuminanceBoost` | 1.2 / 1.0 | full entry / exterior |
| `g_vDirectionalOcclusionAmount` | 0.66 | how much the vertex AO darkens the sun |
| `g_fSkyReflectionPower` | 6 | a material parameter no paint shader reads |
| `g_vec4TintColour1_Alpha` | | in the palette, read by no shader (round 1 and `lights.md`) |

### The atlas texel (confirmed-static, sampler state confirmed-runtime)

Paint UV0 samples the shared atlas's paint block (`lights.md`, "Paint"). Its DXT1 blocks
there have the end points (255, 190, 255) and (255, 206, 255) with every texel at index 2,
so the texel is **(255, 198, 255)**, not the (240, 192, 240) `lights.md` gives. The game
samples it with sRGB decoding (`D3DSAMP_SRGBTEXTURE` 1 on sampler 0, every paint draw in
`vm1-20261030-152849`), so in the shader `tex = (1, 0.565, 1)`:

- **A = paint exactly.** The palette colour is the albedo, with no floor and no scale.
- `specMask` = 0.565 on a clean car: the specular, second specular and reflection are all
  scaled by it.

`harness/forza_render.PAINT_TEXEL` (render_compare's Forza reference) assumes 240 and takes
0.871 for the texel, so its paint is 13% darker than the car_render side it is compared with
(issue below).

### Fresnel: the DMC-12 note is right (confirmed-runtime)

`g_fresnelpower`, `g_fresnelbias` and the two colours reach the shader as stored in the
palette: c44 = the entry's power (15 for vanilla 126, 1.7 for 224, 10 for 133), c42 its bias,
c43 = 1 - bias, c16 colour 2, c17 colour 3 - colour 2 (every dump of
`vm1-20261030-152849`). N.E is the normal against the eye vector, so a low power mixes toward
colour 3 almost everywhere and a high one only at the edges, as `lights.md` ("The DMC-12 came
out black") found. The pixel comparison below confirms it in the frame: with the stored power
the model matches every angle bin of the seven cars, including entries with power 1.0
(147) and 1.7 (224).

### The terms by size (confirmed-runtime)

What each term contributes to the sunlit paint pixels of the five vanilla cars and the ported
F40 (share of the pixel's luminance, `paint_model.frames` over run `vm1-20261030-152849`):

| Car (entry 0) | View angle | Ambient | Sun | Specular (glint and sheen) | Reflection |
| --- | --- | ---: | ---: | ---: | ---: |
| 126 red, power 15 | facing / oblique | 14 / 20% | 51 / 69% | 41 / 4% | 5 / 7% |
|  | steep / edge-on | 14 / 15% | 50 / 37% | 0 | 36 / 49% |
| 224 white, power 1.7 | facing / oblique | 16 / 21% | 65 / 76% | 20 / 2% | 1 / 2% |
|  | steep / edge-on | 22 / 21% | 71 / 61% | 0 | 7 / 17% |
| 133 black, power 1.5 | facing / oblique | 14 / 14% | 54 / 52% | 8 / 2% | 24 / 32% |
|  | steep / edge-on | 4 / 2% | 13 / 5% | 0 | 84 / 93% |
| 147 silver, power 1.0 | facing / oblique | 18 / 21% | 73 / 75% | 8 / 1% | 1 / 2% |
|  | steep / edge-on | 20 / 15% | 63 / 48% | 0 | 17 / 37% |
| 205 blue, power 1.5 | facing / oblique | 16 / 21% | 68 / 76% | 18 / 1% | 1 / 2% |
|  | steep / edge-on | 21 / 18% | 67 / 49% | 0 | 12 / 33% |
| F40 port (`#c00000` on 126's template) | facing / oblique | 15 / 13% | 74 / 71% | 5 / 5% | 6 / 12% |

(Angle bins by N.E: facing above 0.75, oblique 0.45 to 0.75, steep 0.2 to 0.45, edge-on
below 0.2.)

So, for the clear coat (#345):

- **The reflection is off on panels that face the camera.** Its Fresnel term is
  `max(0.025, (1 - N.E)^cfp - 0.5)`: the floor 0.025 until N.E drops under 0.5, then at most
  0.5 at grazing. With the usual `reflAmount` 0.5, a facing panel reflects about 2% of the
  environment (0.565 x 0.025 x 0.5 x 1 x 3), an edge-on one about 42%. Forza's clear coat
  (section 5; its formula inferred) reflects about 4.2% facing (Schlick with `FresnelIndex`
  1.55, times `FresnelScalar` 0.9) and 90% at grazing: twice DSF's facing reflection, on
  every panel.
- **The "sheen" is the second specular**, and it is tinted by the paint: `s2i x (R.E)^s2p x
  A x 40 x 0.565` around the mirror direction of `vehSpecularLightDirection`. With the common
  0.1 / power 5 it adds up to 2.3 A at the lobe's centre (sun and sky give the median sunlit
  panel 2.1 A), so near the highlight a panel shows its own colour at up to twice the
  brightness, not white.
- **The glint** (`(R.E)^1500 x 22.6`, white) is a point. With the sheen, the specular is
  more than half the diffuse on 0 to 13% of a frame's sunlit paint pixels (median 2%, 33
  dumps).
- **On the player's car the environment is the scene itself**: sampler 1 of GPCH 26 is a
  640 x 352 A16B16G16R16F render target (`T#6` in round 1's trace, the scene combined with the
  particles), read with the reflection vector in camera space. Traffic reflects a static
  256 x 256 DXT1 cube map (sRGB), times 1.2.

## 2. Runtime constants by day (confirmed-runtime)

Run `vm1-20261030-152849` (VM 1; archive `out/re/build/dngvehicles.sp`, a build of master
with seven ports; vanilla 126, 224, 133, 147, 205 and ports 1 (F40) and 3 (370Z), palette 0,
chase, nose, left and deck views):

```powershell
.\tools\wsb\run-series.ps1 -Vm -Archives out\re\build\dngvehicles.sp -Labels p345-master `
  -ProbeIds '126,224,133,147,205,1,3' -Palette 0 -Views 'nose,left,deck' -ShotSeconds 3 `
  -WaitSeconds 600 -Override 'paintprobe.asi=out\re\paintprobe.asi;paintprobe.ini=out\re\paintprobe.ini'
```

`paintprobe.ini`: `log=C:\Output\re-paint\paintprobe-%s.log`, `dir=C:\Output\re-paint\dumps`,
`dump_ms=2000`, `dump_start_s=45`, `dump_count=200`. Log and 122 dumped frames in
`%LOCALAPPDATA%\Temp\dsfforza-wsb\re-paint` (`paintprobe-20261030-152910.log`).

GPCH 26's constants in that world (`paint_model.py log`):

| Register | Name | Value | From |
| --- | --- | --- | --- |
| c5 | `s_LightDirectionalColour` | 3, 2.923, 2.554 | ClearDay `directionalLightColour` |
| c7 | `s_LightDirectionalDirection` | -0.25, -0.76, 0.6 | `directionalLightDirection` normalised |
| c6 | `s_LightSpecularColour` | 40, 39.125, 30.194 | `vehSpecularLightColour` |
| c8 | `s_LightSpecularDirection` | -0.384, -0.768, 0.512 | `vehSpecularLightDirection` normalised |
| c11 | `g_LightAmbVariationColBoundLower` | 0.147, 0.170, 0.2 | (`ambientColourUpperBound` - `LowerBound`) / 2 |
| c12 | `g_LightAmbVariationColBoundUpper` | 0.454, 0.517, 0.6 | (upper + lower) / 2 |
| c10 | `g_LightAmbVariationDirection` | -0.25, -0.76, -0.6 | the hemisphere's axis (N.(-c10) = 1: the upper bound) |
| c9 | `g_vDirectionalOcclusionAmount` | 0.66 | material |
| c13 | `g_hSpecularPower` | 1500 | material |
| c24 | `g_fTexMul2` | 1, 1, **3**, 1 | .z = `vehEnvmapmultiplier` 3 |
| c47 | `g_vecShadowModifier` | 0.638, 0.112, 0.25 | |
| c41 | `g_hCubemapLuminanceBoost` | 1.0 (exterior), 1.2 (full) | material |
| c46 | `s_FogColour` | 0.646, 0.767, 1 | ClearDay `fogColour` |
| c22 | `g_vec4FinalTintColour` | 1, 1, 1, 0 | off |
| c45 | `g_vecDirtinessDecalControl` | 0, 0, 2, 0 | clean |

Before the world loads (the front end) the same programs get a neutral set: sun 1.5 white,
ambient 0.3 / 0.7 grey, shadow modifier (0.511, 0.239, 0.25), fog (1, 0.85, 0.79).

Samplers of the player's car (GPCH 26): s0 the atlas (DXT1, sRGB read), s1 the scene copy
(640 x 352 half float, linear), s2 a 4 x 1 white light map (so `ambAmount` = ao and the sun
term is the shadow map alone near the camera), s3 the 3072 x 3072 shadow map, s4 the dirt
map. Traffic (GPCH 14): s1 the cube map, s2 the city's 528 x 528 light map (sun occlusion by
buildings), s3 dirt.

## 3. Colour space and the screen (confirmed-runtime)

- **Palette colours are linear albedos.** Colour 2/3 are multiplied with linear light in a
  16-bit float target (the atlas texel is sRGB-decoded to 1.0, so it doesn't change them), and
  the post chain encodes sRGB at the very end (`lights-re.md`). There is no exposure or curve
  of their own.
- **Exposure and tone curve** (round 1, used as is): `e = 2 + (1.5 - 2) x sat(1.5 - 0.3 / (2
  La))` with ClearDay's `HDRTonemapOffset` 2 and `middlegrey` 1.5. In these frames La (the log
  average of the scene) was 0.07 to 0.32, so e = 1.5 to 1.9. Then Reinhard on Rec. 709
  luminance with white W = 5: `c = HDR x e (1 + e L / 25) / (1 + e L)`.
- **The colour grade desaturates** (confirmed-runtime). The grade is a 16³ A8R8G8B8 volume in
  `media/SfxMisc.fchunk` ("The grade" below); it mixes channels. Round 1's per-channel fit of
  it is off by 25 to 45 / 255 on saturated paint: a red pixel's green and blue come out on
  screen at 67 to 110, where the per-channel fit gives 36 to 87 (126 and the F40, every angle).
- **What "the shown colour" is.** On screen a paint shows its albedo times the light on each
  panel, through the exposure, the Reinhard curve and the grade. The light factor (HDR of the
  sun and ambient terms over A) on the sunlit paint pixels of the 33 sunlit dumps is p10 0.80
  (panels turned from the sun), median 2.13, p90 2.91, max 3.25 (facing it;
  `out/re/lightfactor.py`). So "the shown colour" of an entry is best defined as
  `screen(2.13 x A)`, the median sunlit panel, with `screen(3 x A)` and `screen(0.8 x A)` as
  the light and dark ends of its shading. `paint_model.LIGHT_REF`, `LIGHT_SUN`, `LIGHT_AWAY`.
- **Why bright reds look flat (#345).** The Reinhard curve works on luminance, so a saturated
  colour of low luminance is barely compressed and its strong channel runs into the grade's
  top. Pure red (A, 0, 0) on screen (`paint_model.screen` with the game's LUT, La 0.2):

  | A | panel turned away (0.8) | median sunlit (2.13) | facing the sun (3.0) |
  | ---: | --- | --- | --- |
  | 0.1 | 69, 38, 49 | 142, 56, 65 | 188, 70, 79 |
  | 0.2 | 111, 45, 57 | 216, 83, 89 | 229, 89, 94 |
  | 0.3 | 158, 61, 70 | 231, 89, 94 | 233, 91, 97 |
  | 0.5 | 212, 81, 88 | 233, 91, 97 | 233, 91, 97 |

  Above A = 0.2 to 0.3 the median and sun-facing panels show the same red: the shading across
  the body is gone. And the most saturated red DSF can show by day is (233, 91, 97): the
  LUT's own top for pure red is (209, 27, 31) display-linear, so the grade lifts green and
  blue under a strong red. Vanilla 126 (colour 2 0.111) and the F40 port (0.143) sit just
  under that knee; any red with more albedo gets flatter, not redder. A grey keeps gaining
  slowly: 215, 233 and 245 on the median panel at A 0.3, 0.5 and 1.0.

## 4. Validation: `paint_model.py frames` (confirmed-runtime)

`uv run python tools/re/paint_model.py frames --log <probe log> --dumps <dumps>
--archive out/re/build/dngvehicles.sp --sids 126,224,133,147,205,1,3 --out out/re/frames3`

For each dump it takes the player car's largest GPCH 26 draw, identifies the car by the
colour 2 the draw received, rasterises the car's clean exterior (`car_render.load_scene`,
`rasterize`) through the logged panel transform (c0..c2) and clip matrix (c96..c99), and for
every paint pixel computes N and E in the world frame, the environment colour from the dumped
scene copy (s1) and the light map from the dumped s2. It predicts the pixel lit and in
shadow (the shadow map isn't dumped) and calls a pixel lit or shaded by which prediction its
luminance is within x1.5 of; the rest (3 to 17%: glints and reflections the model puts a
pixel or two away, silhouette edges) are counted, not compared. On screen it compares the
dumped back buffer (the 1280 x 720 scene centred in the 1600 x 900 window) with
`screen(prediction)` at the frame's own La. The projection lands on the frame to the pixel
(`sid126-f003468.png`, `sid1-f011938.png` in the PR).

Result, 36 dumps of 7 cars, sunlit pixels (`out/re/frames3/frames.json`; the median of each
angle bin, weighted over the car's dumps; lum = observed / predicted Rec. 709 luminance;
predicted sRGB through the game's own grade LUT, nothing fitted; round 1's per-channel grade
in brackets where it differs by more than 10):

| Car | Bin | Pixels | HDR observed | HDR predicted | lum | sRGB observed | sRGB predicted |
| --- | --- | ---: | --- | --- | ---: | --- | --- |
| 126 red | facing | 184,069 | 0.233, 0.014, 0.013 | 0.232, 0.013, 0.012 | 1.02 | 163, 67, 74 | 159, 65, 73 (163, 45, 52) |
|  | oblique | 194,774 | 0.304, 0.018, 0.017 | 0.305, 0.017, 0.016 | 1.02 | 192, 75, 81 | 194, 76, 82 (199, 47, 53) |
|  | steep | 121,445 | 0.355, 0.059, 0.056 | 0.355, 0.057, 0.055 | 1.01 | 205, 87, 90 | 205, 86, 89 |
|  | edge-on | 25,195 | 0.479, 0.145, 0.147 | 0.484, 0.141, 0.142 | 1.03 | 219, 110, 113 | 217, 107, 109 |
| 224 white | facing | 262,450 | 0.743, 0.746, 0.690 | 0.739, 0.742, 0.686 | 1.00 | 216, 207, 198 | 206, 196, 188 |
|  | oblique | 272,142 | 0.500, 0.504, 0.475 | 0.500, 0.504, 0.475 | 1.00 | 188, 177, 169 | 179, 168, 161 |
|  | edge-on | 50,960 | 0.718, 0.724, 0.682 | 0.724, 0.731, 0.689 | 1.00 | 222, 213, 206 | 217, 207, 199 |
| 133 black | facing | 322,047 | 0.018, 0.020, 0.022 | 0.017, 0.019, 0.021 | 1.06 | 39, 46, 53 | 38, 45, 51 |
|  | edge-on | 31,529 | 0.091, 0.090, 0.086 | 0.088, 0.086, 0.082 | 1.04 | 81, 79, 81 | 77, 76, 78 |
| 147 silver | facing | 431,628 | 0.402, 0.424, 0.426 | 0.401, 0.423, 0.426 | 1.00 | 173, 165, 164 | 173, 167, 164 |
|  | edge-on | 53,871 | 0.208, 0.218, 0.221 | 0.211, 0.220, 0.222 | 0.99 | 117, 113, 114 | 118, 114, 114 |
| 205 blue | facing | 225,099 | 0.426, 0.638, 0.777 | 0.423, 0.634, 0.772 | 1.01 | 175, 199, 208 | 169, 195, 205 |
|  | edge-on | 31,726 | 0.230, 0.308, 0.367 | 0.230, 0.315, 0.378 | 1.00 | 124, 142, 159 | 124, 144, 164 |
| F40 port (`#c00000`) | facing | 338,606 | 0.266, 0.003, 0.003 | 0.265, 0.002, 0.002 | 1.02 | 170, 68, 76 | 172, 68, 77 (177, 36, 44) |
|  | edge-on | 43,789 | 0.591, 0.073, 0.067 | 0.591, 0.069, 0.064 | 1.04 | 229, 96, 97 | 229, 95, 96 (236, 64, 68) |
| 370Z port (`#036be3`) | facing | 426,646 | 0.004, 0.095, 0.441 | 0.003, 0.095, 0.441 | 1.01 | 78, 98, 199 | 77, 97, 198 (32, 78, 210) |
|  | edge-on | 43,222 | 0.072, 0.146, 0.440 | 0.071, 0.145, 0.450 | 1.01 | 88, 108, 200 | 86, 107, 203 |

- **Linear light:** all 28 sunlit bins (7 cars x 4 angles) are within x0.99 to x1.07. The
  shaded bins (0.3 to 1% of the pixels: the cars stood in the sun) are within x0.92 to x1.22,
  the model's shade a little too blue (the hemisphere ambient; open).
- **On screen:** over the 28 sunlit bins the mean error is 2.5, 2.3, 2.3 / 255, the largest
  10 (the white's facing panels, slightly dark: bloom, which the model leaves out, lifts the
  brightest pixels). Nothing in this chain is fitted to these frames. With round 1's
  per-channel grade the saturated paints' weak channels were 25 to 45 / 255 too dark.

Not covered: traffic paint (GPCH 14, the city light map and the cube map; same paint maths,
not compared pixel by pixel), the shadow map (each pixel is called lit or shaded from its
own luminance), other moods (all dumps are ClearDay), dirt and damage.

### The grade (confirmed-static and confirmed-runtime)

The tone map's grade textures, dumped by `paintprobe` at the tone map's draw (`2F0359F4`) in
run `vm1-20261030-163744` (`hooks=vtable`, `paintprobe.ini` in `out/re/p3/`; archive and
cars as above, `-ProbeIds '126,1' -Views 'nose,left'`; log in
`%LOCALAPPDATA%\Temp\dsfforza-wsb\re-paint4`): samplers 3 and 4
(`g_samColourGradingFrom` / `To`) are the same managed 16 x 16 x 16 A8R8G8B8 volume. Its
16,384 bytes are, byte for byte, the DDS at offset 4,343,664 of `media/SfxMisc.fchunk`, right
after a chunk header with UID `0012e109a4d88c2d`; the file holds 64 such 16³ DDS volumes
(the other grades). `paint_model.game_grade` reads it from `DSF_GAME_DIR`; the repo ships no
copy. This answers round 1's open item ("the exact 3D colour-grade LUTs were not found").
Which `idColourGrading` picks which of the 64 was not traced.

Before the dump, `paint_model.py grade` fitted a 16³ table from 98 dumped frame pairs (Reinhard
output against the back buffer, central three quarters of the frame; `tools/re/paint_grade.json`,
used when `DSF_GAME_DIR` isn't set). Where the frames had colours it agrees with the game's:
the grey diagonal within 10 / 255 and the red axis within 2 ((211, 27, 31) against
(209, 27, 31) at full red). Elsewhere in the cube it is extrapolation (mean 21 / 255 off in
sRGB over all nodes). On 24 held-out frames' saturated pixels it gave 4.5, 3.0, 3.4 / 255
against round 1's per-channel fit's 17.8, 20.5, 17.9 (`out/re/grade1.txt`).

## 5. Forza's paint data

### What a Forza colour draws with (confirmed-static)

Forza draws car paint with the `body` technique (also `body_2`, `body_3`, `body_simple`). Its
parameters are layered (`forza_paint.py`):

1. `media/cars/Shared.zip` `ShaderSettings/Normal.xml`, the defaults (FH1 and FM4 alike);
2. `ShaderSettings/Metallic.xml` on top when the colour's `Combo_Colors.Metallic` is 1 (the
   file says: "Override settings applied when using a METALLIC paint style"); FH1's
   Shared.zip also has `Matte.xml`;
3. the car's `ShaderSettings.xml` (no FH1 car sets `body` there; one FM4 car does);
4. the car's `ColorShaderSettings<N>.xml` for its colour N.

| `body` parameter | Normal.xml | Metallic.xml | Matte.xml |
| --- | --- | --- | --- |
| `PaintScale` (the diffuse paint) | 1.0 | 0.5 | |
| `SpecularAmount` / `Power` (sun) | 0 / 75 | | 1 / 1 |
| `SpecularAmount2` / `Power2` | 0 / 4 | 7 / 6 | 1 / 1 |
| `SpecularAmount3` / `Power3` | 0 / 2 | 6 / 4 | 1 / 1 |
| `SpecularCubeScalar` | 0 | 1 | 1 |
| `FresnelIndex` / `FresnelScalar` (the clear coat) | 1.55 / 0.9 | | 1 / 0 |
| `TwoToneScale` (`TwoToneColor` by angle) | 0 | | |
| `MetalScale` (`MetalColor` by angle) | 0 | | 0.06 |

Per-colour files (`forza_paint.py survey`, `out/re/forza_paint_survey.json`): FH1 has them on
43 of its 178 car zips (114 files), FM4 on 21 of 318 (34 files). What they set, most common
first (FH1): `PaintScale` 94, `PaintColor` 92, `SpecularAmount2` 91, `SpecularPower2` 85,
`SpecularAmount3` 85, `MetalColor` 82, `SpecularPower3` 82, `MetalScale` 80, `MetalBias` 76,
`MetalPower` 41, `FresnelScalar` 31, `FresnelIndex` 16, `TwoTone*` 10. Typical values:
`PaintScale` 0.5 (30 files) to 0.87, `SpecularAmount2` 7 (42) or 6 (15), `MetalScale` 0.03
(24) to 1, `FresnelScalar` 0 on 9 (no clear coat: satin finishes).

### What `Combo_Colors.RGB` means (confirmed-static; the CPU side inferred)

- It is the colour's **swatch**: the colour the database lists for that paint choice. Where a
  colour has no file of its own, the shader's `PaintColor` is that RGB / 255 (inferred: the
  files that set it mostly repeat it, and `Normal.xml`'s own default `PaintColor`
  (0.0588, 0.0784, 0.051) is `#0f140d` / 255, a `Combo_Colors` value).
- Where a colour has a file, its `PaintColor` is often **not** the swatch. FH1 has 92 files
  that set it; against their `Combo_Colors` row (`forza_paint_survey.json` "colours"), 46 are
  equal within 3 / 255 (TVR Sagaris, Camry, GT-R, Mini, MC12), 24 darker and 22 shifted in
  hue. The darker ones are mostly bright metallics (Beetle `#e0e2e2` to (138, 150, 161), RAV4
  `#fffff5` to (178, 178, 184), `#bec8d7` to (117, 117, 122)); some solids moved too (Beetle
  `#9c002e` to (122, 0, 8), `#ffc53e` to (247, 174, 42)). FM4's 4 such files are all equal.
  The artists darkened the paint the shader gets, mostly where the metallic lobes add light.
  So the swatch is the look and `PaintColor` the means; for a port the swatch is the target.
- The metallic flag is the Metallic.xml layer above: half the diffuse paint, two broad lobes
  (7 x lobe^6 + 6 x lobe^4) and the cube reflection on.
- Forza's car shaders work in gamma 2 (`shaders_v16.fxobj`, the body pixel shader at 0x7a3e8:
  textures are squared after the fetch, the output is `sqrt(colour)`), and `PaintColor` goes
  into the lighting without being squared in the shader. Whether the game squares it on the
  CPU first (the XEX wasn't read) decides how bright a given `PaintColor` draws; it doesn't
  change that the swatch is the target.

### Forza's body shader, in outline (inferred from the microcode; stretch goal)

From `uv run dsff fxobj <shaders_v16.fxobj> --shader 0x7a3e8` (`out/re/forza/ps_7a3e8.txt`):

```text
paint  = lerp(PaintColor, StripeColor, stripe mask) x lerp(PaintScale, StripePaintScale, ..)
paint  = paint + twoTone(view) x (TwoToneColor - paint)       TwoTonePower / Bias / Scale
paint  = paint + metal(view) x (MetalColor - paint)           MetalPower / Bias / Scale
spec   = (SpecularAmount2 x (R.L)^SpecularPower2 + SpecularAmount3 x (..)^SpecularPower3)
           x shDirectionalColour x paint                      the lobes take the paint's colour
refl   = env cube (squared) x fresnel(FresnelIndex: (n - 1) / (n + 1) ...) x FresnelScalar
out    = sqrt(lit paint + spec + refl, fogged)
```

The two-tone and metal mixes have DSF's shape (a bias, power and scale on the view angle
toward a second colour): DSF's `g_fresnelpower` / `g_fresnelbias` / colour 3 are the same
idea as Forza's `TwoTone*`, and its second specular (tinted by A) is Forza's lobes. Factory
colours have `TwoToneScale` and `MetalScale` 0 unless their file sets them (`TwoToneScale`
on 5 FH1 and 4 FM4 files, `MetalScale` on 80 and 7, mostly 0.03 to 0.05), so **a factory
Forza paint has almost no colour change with the view angle** apart from the reflection.

The material format is the one the Forza lens and glass work reads too (`ShaderSettings` XML
per technique layered over `Normal.xml`, `fxobj` constant tables): `forza_paint.parse_settings`
and `shared_settings` read any technique, not only `body`.

## 6. From a Forza colour to the palette

What the converter should set for each Forza colour (swatch S = `Combo_Colors.RGB`, metallic
flag M, the colour's layered `body` settings), and the confidence of each rule. Nothing here
is implemented.

| Palette parameter | Rule | Replaces | Confidence |
| --- | --- | --- | --- |
| `g_vec3TintColour2` | `paint_model.albedo_for(S)`: the albedo whose median sunlit panel (`screen(2.13 x A)`, ClearDay) shows S, capped at the knee (0.3 for a saturated colour, 0.5 for a grey: past it the body only flattens) | `SHOWN_GAIN` 0.3, the brightness cap | model confirmed-runtime; the reference panel a choice |
| `g_vec3TintColour3` | = colour 2 (a flat entry), unless the colour's file sets `TwoToneScale` > 0 (then `TwoToneColor` through `albedo_for`) | the template's colour 3 ratio, `recolour`, `HEADROOM` | confirmed-static (Forza factory paint has no view-angle colour) |
| `g_fresnelpower` / `g_fresnelbias` | any (unused when colour 3 = colour 2); 10 / 0.01 like the vanilla flat entries | the template's | |
| `g_hCustomReflectionAmount` | 1.0 x `FresnelScalar` / 0.9 (0 for satin colours) | the template's (0.5 on 917 of 946 vanilla entries) | inferred, test in game |
| `g_hCustomFresnelPower` | 1.0 | | inferred |
| `g_hSecondSpecularIntensity` / `Power` | no lobes (solid): 0.1 / 5; with lobes: 0.3 x ((`SpecularAmount2` + `SpecularAmount3`) / 13) x (0.5 / `PaintScale`), power the lobes' powers weighted by amount | the template's | inferred |
| `g_vec4TintColour1_Alpha` | anything: no shader reads it | | confirmed-static |

Why these values:

- **Colour 2.** `albedo_for` inverts the chain this note confirms (the shader is linear in A,
  then exposure, Reinhard and the grade). Against today's `SHOWN_GAIN` x swatch
  (`paint_model.py target` with the game's LUT, `out/re/target2.txt`), on the median sunlit panel:

  | Swatch | A, model | A, 0.3 x swatch | Shows (model) | Shows (today) |
  | --- | --- | --- | --- | --- |
  | `#c00000` (F50 GT, F40) | 0.146, 0, 0 | 0.158, 0, 0 | 192, 72, 80 | 200, 75, 83 |
  | `#e10d12` (370Z red) | 0.246, 0, 0 | 0.226, 0, 0 | 225, 88, 93 | 222, 85, 91 |
  | `#036be3` (370Z blue) | 0, 0.047, 0.301 (knee) | 0, 0.044, 0.230 | 87, 105, 222 | 80, 98, 211 |
  | `#ffbf19` (S5S Raptor) | 0.301, 0.192, 0 (knee) | 0.300, 0.156, 0.003 | 220, 165, 86 | 223, 148, 88 |
  | `#fcfcfc` (white) | 0.373, 0.437, 0.462 (knee) | 0.292 | 221, 221, 221 | 214, 204, 200 |
  | `#c5c4c3` (silver) | 0.231, 0.256, 0.264 | 0.168 | 197, 196, 195 | 171, 155, 151 |
  | `#999999` (grey) | 0.144, 0.162, 0.165 | 0.096 | 153, 153, 153 | 117, 110, 110 |
  | `#808080` | 0.110, 0.120, 0.123 | 0.065 | 128, 128, 128 | 93, 89, 92 |
  | `#585c61` | 0.058, 0.071, 0.074 | 0.029, 0.032, 0.036 | 88, 92, 97 | 59, 65, 71 |
  | `#060606` (black) | 0 | 0.001 | 34, 38, 46 | 34, 39, 46 |

  So `SHOWN_GAIN` 0.3 is right for bright saturated reds (where it was fitted: the F50 and the
  twin cars), and under-shows mid tones and greys by 25 to 36 / 255, with a warm cast: the
  curve isn't linear (Reinhard's toe and the grade), and the grade warms neutrals, so a grey
  needs a slightly blue albedo. Yellows and oranges keep more of their green (`#ffbf19`
  165 against 148). Whites stop at the knee (221 on the median panel, 231 facing the sun);
  colours the screen can't show are dimmed in their own hue until they fit under it. Blacks
  bottom out at the grade's floor (34, 38, 46) either way. The brightness cap (S5S Raptor)
  becomes the knee.
- **Colour 3.** The dark rims of most vanilla entries (low power, colour 3 at 0.02) are DSF's
  art, and the reason #289's reds went maroon. Forza's factory paint is one colour at every
  angle, so a flat entry is the faithful one; the view-angle darkening that remains is DSF's
  own lighting (ambient against sun, the reflection).
- **Reflection.** DSF's reflection on a facing panel is `0.565 x 0.025 x reflAmount x 3` of
  the environment (with `shadowMod` about 1 in the sun): 0.042 x `reflAmount`; at grazing
  `0.565 x 0.5 x reflAmount x 3` = 0.85 x `reflAmount`. Forza's clear coat (inferred: Schlick
  with n = 1.55, F0 0.0465, times `FresnelScalar` 0.9) is 0.042 facing and 0.9 at grazing. So
  `reflAmount` 1.0 matches Forza at both ends (DSF's middle, N.E 0.3 to 0.5, stays at the
  floor where Forza rises to 7%). That doubles the vanilla value: the strongest lever for
  #345's missing gloss. `g_hCustomFresnelPower` below 1 lifts the middle but overshoots
  (0.5 gives 0.35 at N.E 0.5), so 1.0. The in-game check is open.
- **Second specular.** DSF's sheen is `s2i x 22.6 x A x lobe^s2p` against a sun term of about
  `3 x A x N.L`; Forza's metallic lobes are `(7 lobe^6 + 6 lobe^4) x paint` against a diffuse
  halved by `PaintScale` 0.5. Matching the ratio literally would need `s2i` near 3.5, far above
  any vanilla entry (median 0.15, p75 0.3; 10 at most) and Forza's light units are unknown,
  so the rule keeps vanilla's scale and makes it relative: `s2i` = 0.1 for a Forza colour
  without lobes, and 0.3 x ((Amount2 + Amount3) / 13) x (0.5 / `PaintScale`) with them (0.3
  for the Metallic.xml defaults, vanilla's p75; the 370Z's silver, `SpecularAmount2` 1 and
  `PaintScale` 0.7, gets 0.12), `s2p` the lobes' powers weighted by amount (5.1 for 6 and 4).
  A starting point to tune in game, not a derivation.

Do these replace today's tuned constants? `SHOWN_GAIN` and the brightness cap: yes
(`albedo_for` with its knee). The template matching: for colours 2 and 3 yes (no template
needed: flat entry); for the reflection and specular, the rules above take the Forza
settings instead of a vanilla neighbour's. `HEADROOM` (#358): no longer needed with a flat
entry. The stainless preset (DMC-12): Forza paints that car flat `#999999`, which
`albedo_for` turns into A 0.14 to 0.16, a mid grey (153 on screen); the preset's darker,
less reflective look is DSF's DeLorean, a choice rather than a mapping.

## The probe

Three things made `paintprobe` harder than `lampprobe`:

- **DSF Enhanced wraps `IDirect3D9`** and installs its device hooks after `CreateDevice`
  returns. The probe hooks `IDirect3D9::CreateDevice` on the object `Direct3DCreate9`
  returns; the device's methods it patches afterwards.
- **The device's vtable entries go back to the d3d9 functions** about every 200 ms (the
  probe's own log, `hooks (watch)` lines in run `vm1-20261030-152849`; most likely d3d9's own
  table swaps). The default (`hooks=vtable`) re-applies the hooks every 200 ms from a thread;
  it misses some frames, but the dumps it caught came out whole (run `vm1-20261030-152849` used an
  earlier build that slowed to every 2 s after two minutes; `vm1-20261030-163744` the final
  one). `hooks=chain` re-points the 5-byte jumps DSF Enhanced puts at the d3d9 functions to
  the probe instead (every frame seen), but both runs with it stalled in the story load
  (`vm1-20261030-154458` and its retry), so it is off by default.
- **The back buffer is 1600 x 900 with the 1280 x 720 3D view centred in it** (offset 160,
  90, not scaled: found by correlating the two), so the final image of a pixel is
  `bmp[y + 90, x + 160]`.

The runtime vertex shader of the player car (`8E0B446E`) differs from GPCH 26's (`905B712A`):
DSF Enhanced replaces vertex shaders (jitter). The probe identifies programs by their pixel
shader, which DSF Enhanced leaves alone.

## Open

- Which of the 64 grade LUTs in `SfxMisc.fchunk` each mood's `idColourGrading` selects
  (ClearDay's is UID `0012e109a4d88c2d`).
- Shade: the shaded bins (few pixels in these frames) come out a little bluer in the model
  than in the frame. A run with the cars parked in a building's shadow would test the
  hemisphere ambient and the shadow map's edge.
- Traffic paint (GPCH 14): the same paint maths, a static cube map and the city light map;
  not compared pixel by pixel.
- The Forza side of the mapping (reflection 1.0, the sheen rule) is inferred; the next step is
  an in-game A/B of a port with the section 6 palette against today's, by day, the same views.
- Forza's CPU-side treatment of `PaintColor` (squared or not before the gamma-2 shader).
