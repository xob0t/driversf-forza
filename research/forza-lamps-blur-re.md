# Clear-glass tail lamps and the wheel blur disc (reverse engineering, round 4)

Updated: 2026-10-31

Two converter fixes stalled on missing knowledge: the Ford Focus RS500's clear-glass tails come
out near-white in game (#404), and its wheels' blur discs come out near-white over black rims
(#407; the Beetle 68's disc layout, #419). This note decodes what both games do and gives the
rules the lamp and wheel code can take, each from decoded maths or measured data. Nothing here
changes the converter.

Confidence levels as in `forza-lenses-re.md`: `confirmed-static` (read in shader microcode, the
data or an executable, and where it is shader code checked by running it in the emulator),
`confirmed-runtime` (seen in a running game), `inferred` (follows, not checked), `measured`
(a survey of the game's own data with the tool named).

Tools (`tools/re/`; inputs: `forza_lens.py extract` to `out/re3`, `xex.py extract` to `out/re3b`):

- `forza_lamp_hue.py`: per car, the red-glowing lamp techniques by the hue of their texels
  (`survey`, Forza side), and per built archive the DSF red lamps by the hue of the texels they
  are drawn with (`built`, ports or vanilla).
- `dsf_blur.py`: DSF's blur disc against the rim it stands for, ring by ring round the axle
  (`vanilla`, any archive with `--archive`).
- `forza_blur.py`: Forza's side of the wheel: the blur constants the game sets (`constants`),
  blur_rim's closed form against the emulator (`verify`), the wheel archive (`extract`), rim and
  disc per car (`stats`, `survey`), and the stores to the blur constants in `default.xex`
  (`stores`).
- `forza_lens.py`: `Forza.run` takes extra samplers' texels and game-set constants.

## Summary: the rules

**Lamps (for `lights.py` / `lenses.py` / `dsf_materials.py`):**

1. In both games a lamp texel's RGB is only its unlit look. Its glow is a colour fixed per
   lamp, never the texel's. In Forza that colour is the technique's ShaderSettings constant
   (`BrakeColor`, `TailLightColor`...), linearised on upload, with green and blue 0 on every
   tail technique. In DSF it is the light entry's colour b2 (`lights-re.md`). So the
   **glow alpha and the curve that shapes it must be chosen by the lamp's glow colour (its
   technique), not by the texel's hue.** Today `dsf_materials.matched_lamp_alpha` and
   `matched_lamp_texel` pick `LAMP_CURVES["white"]` and the white cap from `lamp_hue(r, g, b)`.
   That puts the Focus's clear tail glass on the white curve.
2. The glow magnitude per texel, from the decoded maths:
   - FH1: `colour_lin x state x A`, with A the texel alpha. On the Focus's
     `lights_gls_taillight2S`, `TailLightColor` 1.2 is 1.52 linear (running) and `BrakeColor`
     1.4 is 2.16 (braking).
   - FM4 `tail_light*`: `BrakeColor_lin.r x L.r`, the texel's linear red.
3. **DSF can't show Forza's lamps-off state for a running tail.** A DSF tail glows by day at
   its running level: 0x18 at 0.2, colour 0.588 red; 0x03 at 2, colour 5.88. Forza's FH1 tail
   glass doesn't glow by day unless braking: the clear glass is silver-white by day and red at
   night or under the brake. So the DSF look of such a lamp is Forza's lamps-on look.
4. **Vanilla's own convention for clear-glass tails is measured:**
   - the Aston Martin V12 Vantage (135), Rapide (134) and Cygnet (249) have every red-lamp
     texel non-red, grey at about sRGB 100 to 140;
   - their tails are type 0x03 (level 2 by day, colour 5.88) with low alpha (median 20 to 52),
     with separate 0x02 stop lamps.

   Per texel by day that is about 9 times the Focus port's ratio of red glow to white diffuse
   (section 1.3). Which type a converted clear tail takes is the lamp code's decision; this is
   the vanilla data for it.
5. Not known: the ratio of Forza's unlit lighting to DSF's (Forza's cube map brightness, open
   item 5 of `forza-lenses-re.md`; #422 asks for the same units for the glow). It would set how
   bright a converted clear glass texel should be against vanilla's. No fitted stand-in is
   proposed.

**Wheel blur disc (for `forza2dsf/wheels.py` and the blur code):**

1. DSF's disc program (SpokeDisc and Disc, GPCH 114/120, confirmed-static):
   `rgb = light x tex.r x g_vec4WheelBlurColour`, `alpha = tex.b x opacity`, with
   `opacity = 2.5 b - 0.15`. The light is the same function as the rim's program (GPCH 124,
   `light x tex.r`) and the Lights program's, with the vertex AO (`position.w / 4096`) in it.
2. A port's rim stays drawn under the disc. It is on the Chassis/gloss copies of the Lights
   material, and only the blur programs read s9 byte 3. So the port's disc must be opaque
   (blue 255, as today) and carry the whole spinning look.
3. **Vanilla does not follow "disc = rotational mean of the rim" per radius** (measured, 122
   vehicles with a disc):
   - the disc profile is one of two shared generic textures;
   - a per-vehicle grey `g_vec4WheelBlurColour` (median 0.53) brings its overall level toward
     the rim's: disc / spinning rim median 1.10 (IQR 0.84 to 1.44) where the disc shows, against
     ×2.3 spread without the colour;
   - ring by ring the ratio runs from 0.67 to 2.3 (median car);
   - with the AO term the disc reads brighter than the rim (median 1.42): vanilla discs carry
     AO 0.9, rims 0.15 to 0.6.

   So vanilla matches the level loosely and doesn't match the profile.
4. The rule the target ("a disc that looks like the converted rim spinning") implies, from our
   own rim's data (section 2.4):

   ```text
   disc texel red at radius r = encode_sRGB(M(r) / C)
   M(r) = mean over the angle of the albedo our converted rim is drawn with at that radius
          (the front-most non-disc surface of the port's wheel along the axle: its atlas texel
          decoded from sRGB, the rotor or nothing where the spokes leave gaps)
   C    = g_vec4WheelBlurColour, one RGB per car (the disc's mean chroma; tex.r carries the
          per-ring level)
   disc vertex AO on each ring = the rim's mean AO on that ring
   ```

   With the disc's AO set this way the ambient term of `light` is the rim's. The direct term
   still differs by the rim's normals against the disc's flat ones. This rule needs no Forza
   blur data. It differs from vanilla in that the profile is per car and per ring. Master's
   ports by this measure:

   | Car | Disc / spinning rim |
   | --- | --- |
   | Focus RS500 (39) | 6.5, 2.1 to 54 by ring |
   | 370Z (32) | 0.62 |
   | Beetle 68 (711) | 0.77, 0 to 150 by ring: bright and dark rings in the wrong places (#419) |
   | LP700-4 (476) | 0.52 |
   | Carlton (495) | 0.79 |

5. **The converted rim's own colour has the wrong source** (confirmed-static, both games). A
   wheel's ShaderSettings are `media/cars/shared/ShaderSettings/Tire/Normal.xml` (plus
   `Tire/Slod.xml` for the far LOD), then the wheel archive's own
   `media/Wheels/<car>/ShaderSettings.xml`. They are never the car's own `ShaderSettings.xml`
   or the shared `Normal.xml`. `forza_materials.wheel_paint_scale` reads the car's file over
   `WHEEL_PAINT_SCALE`, which comes from `Normal.xml` (rim 0.75). The game's default is
   `Tire/Normal.xml`'s 0.65, and 56 of the 512 surveyed wheel archives have their own file. The
   rim PaintScale differs on 500 of 502 cars, by more than ×1.5 on 25. The Focus RS500's
   `rim PaintScale 7.5` is in its car file and never reaches its wheels: Forza draws its rim
   at 0.65. Section 2.5 has the survey.

## 1. Clear-glass tail lamps (#404)

### 1.1 What makes a lit tail red in Forza (confirmed-static, emulator-checked)

The Focus RS500 (`FOR_FocusRS500_10`, FH1) tail cluster, from `forza_lens.py survey` (LOD 1,
mean texel per subsection):

| Section | Technique | Triangles | Mean texel (RGBA) |
| --- | --- | ---: | --- |
| glassLTL / RTL | `lights_gls_noemit` | 188 | (152, 8, 8, 60) |
| | `lights_gls_taillightst` | 12 | (199, 149, 149, 111) |
| | `lights_gls_taillight2S` | 21 | (221, 218, 219, 162) |
| | `lights_gls_indicator_left/right` | 16 | (202, 160, 160, 133) |
| taillightL / R (behind the glass) | `taillight2S` | 45 | (127, 116, 116, 61) |
| | `taillightst` | 33 | (128, 119, 116, 66) |
| | `textured_reflector` | 54 | (61, 51, 51, 85) |

The car sets `GlassMaxOpacity 0.8` on its lamp glass and `BrakeColor` 1.4 / `TailLightColor`
1.2 on `lights_gls_taillight2S`.

`lights_gls_taillight2S` pixel shader (FH1 `shaders_v16.fxobj`, `fxl.py dis`), the end of it:

```text
add   r5.xyz, BrakeColor, -TailLightColor
mad   r2.xyz, r5, dynamicLightsAmount.x, TailLightColor        // lerp(tail, brake, brake)
add_sat r3.x, dynamicLights2.x, dynamicLightsAmount.x          // sat(lamps on + brake)
tfetch r4, uv, tf1                                             // LightsSampler: the only texel
...
mul   r3, r3.yzwx, r4                                          // r3.w = state x texel alpha
min   r6.w, r5.x, GlassMaxOpacity                              // opacity
mad   r5.xyz, r3.w, r2.xyz, r5                                 // + colour x state x A
```

So `c = L x K_gls + lerp(TailLightColor, BrakeColor, brake) x sat(lampsOn + brake) x A`, alpha
`GlassMaxOpacity` (the `forza-lenses-re.md` table, now read for this technique). The pass
fetches one Lights texel (tf1) plus the environment (tf2/tf3), shadow mask (tf13) and damage
(tf4, predicated). Ruled out:

- **A different texture or channel when lit:** there is no second Lights fetch.
- **A red bulb mesh:** the `taillight2S` lamp behind does glow red, but at
  `GlassMaxOpacity` 0.8 only `0.2 x sqrt(behind)` of it passes, in the sqrt-encoded blend
  space.

The red is **the technique's colour constant times the texel alpha**: green and blue are 0
in the shared colour of every tail technique, and the cars' own values keep them red
(`forza_lamp_hue.py survey`: 6 car settings give a tail technique some green or blue, all
red-dominant: FER_512TR_91 and TOY_MR2SC_89 `BrakeColor` (590, 1, 1) and (131, 1, 1) linear,
SPY_C8LM85_10 and VOL_S60_11 (12.8, 1, 0), MAZ_16_B09_10, MAZ_RX7_97).

Emulator, the Focus's own texel and settings (`forza_lens.py probe lights_gls_taillight2S
--texel 221,218,219,162 --car out/cache/fh1/FOR_FocusRS500_10`, sqrt-encoded output):

| State | Output (r, g, b) | Linear |
| --- | --- | --- |
| off (by day, not braking) | (0.607, 0.604, 0.597) | (0.37, 0.36, 0.36): silver-white |
| brake | (1.320, 0.604, 0.597) | (1.74, 0.36, 0.36): red |

`forza_cluster.py render FOR_FocusRS500_10 --camera rear --state off|running|brake`
(`out/re3/cl-focus`) shows the cluster silver-white with lamps off, the upper glass salmon-red
with the running lamps (`dynamicLights2.x`), the lower part red under the brake.

FH1's time of day keeps `SwitchOnLights` near 0 from 08:00 to 19:00 (`forza-lenses-re.md`
section 2, inferred: what feeds `dynamicLights2.x` from that key). **By day, not braking, Forza
draws the Focus's clear tail glass silver-white.** It is red at night and under the brake.

FM4 differs only in the mask: `tail_light*` glows `BrakeColor x brakeReverseAmount.x x L`. With
`BrakeColor` (2, 0, 0) uploaded as (4.95, 0, 0), only the texel's linear red glows, red, even on
a white texel.

### 1.2 What DSF does with it (confirmed-static, `lights-re.md`; measured here)

DSF: `HDR = light x t + a x C`, `C = vehColourBoost (5) x colour[b2] x scale[b3] x brightness`.
The texel RGB `t` never enters the glow, as in Forza. The tails glow by day at their running
level: 0x18..0x1C at 0.2 (red C = 0.588), 0x03 at 2 (C = 5.88).

Master's Focus (spool 39, the main checkout's `out\build`, read only; `lamp_model.lamp_texels`,
rear facing):

| Light | Type | Area | Alpha p10 / 50 / 90 | Texel median (sRGB) |
| --- | --- | ---: | --- | --- |
| 1, 4 | 0x18 red | 1108, 1276 cm² | 0 / 28 / 180 | (148, 125, 123), (139, 125, 123) |
| 5 | 0x02 red | 206 cm² | 0 / 30 / 136 | (132, 133, 131) |
| 0, 2 | 0x04/0x05 amber | 332 cm² each | 0 / 25 / 168 | (191, 187, 189) |

By day: a white texel lit by the sun, plus at most `0.588 x a` of red. That is the near-white
of the issue's screenshots.

### 1.3 How common, and vanilla's convention (measured)

`forza_lamp_hue.py built` over master's build (`out/re4/built-master.json`, 571 ports with red
lamps) and the vanilla archive (`built-vanilla.json`, 141 vehicles). Share of each car's
rear-facing red-lamp area drawn with non-red texels (`dsf_materials.lamp_hue`):

| | Cars | p10 / p50 / p90 | Over 25% | Over 50% | Over 75% |
| --- | ---: | --- | ---: | ---: | ---: |
| vanilla | 141 | 0.00 / 0.01 / 0.20 | 13 | 6 | 4 |
| master's ports | 571 | 0.02 / 0.40 / 0.68 | 358 | 196 | 39 |

The Focus: 92% (2371 of 2590 cm²). On the Forza side (`forza_lamp_hue.py survey`), 422 of 566
cars have a red-glowing lamp triangle on non-red texels. Most are white lamps behind red lenses
that the lens bake turns red, so the built survey is the one that counts.

The vanilla cars with clear red lamps (all red-lamp texels non-red):

| Spool | Model | Red-lamp area | Types | Texel median per lamp | Alpha median |
| --- | --- | ---: | --- | --- | ---: |
| 135 | Aston Martin V12 Vantage 2010 | 596 cm² | 0x03 tails, 0x02 stop | (111 to 131, 121 to 137, 131 to 147) | 20 to 27 |
| 134 | Aston Martin Rapide 2010 | 531 cm² | 0x03, 0x02 | (90 to 145, 100 to 145, 109 to 145) | 40 to 52 |
| 249 | Aston Martin Cygnet 2010 | 979 cm² | not split | | 86 (mean) |

Partly clear: McLaren MP4-12C (225), Pagani Zonda Cinque (232), Dodge Monaco 1974 (302).

Per texel by day, the Aston's red glow over its white diffuse is
`5.88 x 35.5/255 / (0.19 D)` = 4.3 / D against the Focus port's `0.588 x 60.8/255 / (0.30 D)` =
0.47 / D (D the light on the texel; texels 120 and 148, decoded): about 9 times redder. Vanilla
gets red out of clear tails with the level-2 running type and darker grey glass, not with red
texels.

### 1.4 Checked on other clear-glass-tail cars

The built survey's top ports, with 100% of the red-lamp area on non-red texels, include spools
56, 59, 64, 67, 71, 76, 511, 512, 580, 596, 612 and 613. Same mechanism: their tail
techniques glow red by constant, and their drawn texels are white or grey. Not done:
emulator renders of each (`forza_cluster.py render <car> --state brake`). The Focus is the only
car checked that way.

## 2. The wheel blur disc (#407, #419)

### 2.1 DSF's disc (confirmed-static; `gpch.py dis 120 124`)

Materials (SSIC):

- `GLOBAL_Wheel_Blur_SpokeDisc(_LOD1)` and `GLOBAL_Wheel_Blur_Disc(_LOD1)`: programs
  114 / 115 (full entry), 120 / 121 (LOD1, the exterior);
- `GLOBAL_Wheel_Blur(_LOD1)`: the vanilla sharp rim, 108 / 124.

The vertex shader (shared, `4A51AA53`) passes `position.w / 4096` as the AO (`o2.w`, the pixel
shader's `v1.w`) and the s9 byte 3 opacity (`wheels.md`, "Speed blur": `1.15 - b` at byte 3 =
0, `2.5 b - 0.15` at 2, 1 at 1; `b = 1.15 x g_v4PerPanelData[s9.byte1].y`, set by the engine
per wheel, not traced).

Pixel shader 120 (disc), annotated from its debug lines:

```text
tex   = g_samDiffuseMap(uv)                      // the disc texture (sRGB decoded: inferred,
                                                 //  as measured for paint and Lights)
oC0.w = tex.b x opacity                          // Vehicles_Functions.hlsl line 112
amb   = (lightmap.x x 0.4 + 0.6) x ao x lerp(ambLower, ambUpper, N.-ambDir)
dir   = max(N.-sunDir, 0) x sunColour x shadow x (1 + g_vDirectionalOcclusionAmount (ao - 1))
rgb   = (amb + dir) x tex.r x g_vec4WheelBlurColour.rgb      // line 97 typeSpecificData x c35
      + (R.V)^power x tex.g x specColour x dir
      + sat(cube(R) x boost x tex.g x max(min, 1 - N.V + bias) x amount x shadowMod x texMul2.z)
then fog, the final tint
```

Program 124 (the vanilla rim) is the same with `tex.r` alone (no colour) and
`oC0.w = opacity`. The Lights program a port's rim draws with (`lights-re.md`) is the same
light function on the texel RGB.

**Compositing.** Disc materials blend (`rs_alphablendenable`, `SortMode` 1, z-write off;
`wheels.md`). The factors are inferred to be `SrcAlpha / InvSrcAlpha` (an opacity in alpha),
in a linear 16-bit float target. In vanilla the rim fades out as the disc fades in. A port's
rim is on its Chassis/gloss copies of the Lights material, whose programs don't read byte 3,
so **it keeps drawing under the disc**. Wherever the disc isn't opaque the still spokes show
through.

### 2.2 Vanilla: is the disc the rotational mean of its rim? (measured)

`dsf_blur.py vanilla` (all 150 vanilla vehicles; 122 have a disc on their exterior wheel part:
91 SpokeDisc, 29 Disc). On 10 rings × 90 angles round the axle it takes, per angle, the
front-most surface along the axle:

- **(a) the spinning rim:** the byte 3 = 0 and 1 surfaces;
- **(b) the faded-in disc:** the disc at its texel opacity over the byte 3 = 1 surfaces behind
  it, with the rim faded out.

Ring means are compared on the rings where the disc shows. Albedo: the texel decoded from sRGB,
red as the grey level on the packed shared textures, the disc's red times
`g_vec4WheelBlurColour`, its opacity the decoded blue. Gaps count as black in both.

| | p10 | p25 | p50 | p75 | p90 | within ×0.8 to ×1.25 |
| --- | --- | --- | --- | --- | --- | ---: |
| disc / spinning rim, albedo | 0.63 | 0.84 | 1.10 | 1.44 | 1.74 | 43 of 122 |
| the same with the AO factor | 0.70 | 1.05 | 1.42 | 1.97 | 2.46 | 28 |

- Per ring, per car: the lowest ring ratio has a median of 0.67, the highest 2.30.
- `g_vec4WheelBlurColour` (median 0.53, 1.0 on 15) narrows the car-to-car spread from ×2.3 to
  ×1.5 (log10 sd 0.355 to 0.18).
- Disc opacity per ring: p10 / 50 / 90 = 0.48 / 0.74 / 0.86.

Example rings, luminance (spinning rim → disc):

- Nissan 370Z (227), colour 0.58: r 41 mm 0.20 → 0.29, 95 mm 0.12 → 0.18, 149 mm 0.09 →
  0.08, 230 mm 0.16 → 0.21.
- RUF CTR (241): 53 mm 0.12 → 0.27, 117 mm 0.08 → 0.19.

So **vanilla's convention**:

- one generic blurred-wheel texture (two in all);
- a per-vehicle grey that brings the disc's overall level to about the rim's (median 1.10,
  loose);
- AO 0.9 on most discs, which makes them read brighter than the rim in ambient light. The
  in-game shots of `wheels.md` agree: vanilla 370Z's face 103 at rest, 146 at 60 mph.

Vanilla does not match the rim ring by ring.

Measurement limits:

- texels are nearest-sampled;
- a surface whose texture isn't resolved counts as grey 0.3 in both cases (rings where a cap
  covers both are left out of the ratio);
- chrome is taken at its albedo, without its reflection;
- gaps count as black;
- the direct light's dependence on the normals is left out.

### 2.3 Master's ports by the same measure (measured)

`dsf_blur.py vanilla --archive <main>\out\build\dngvehicles.sp --ids 39,32,711,476,495`:

| Car | Disc / spinning rim | With AO | Ring range |
| --- | --- | --- | --- |
| FOR_FocusRS500_10 (39) | 6.49 | 22.5 | 2.1 to 54 |
| NIS_370Z_10 (32) | 0.62 | 1.14 | 0.34 to 1.18 |
| VW_BEETLE_68 (711) | 0.77 | 1.75 | 0 to 150 (#419) |
| LAM_LP7004_12 (476) | 0.52 | 4.48 | 0 to 33 |
| LOT_Carlton_89 (495) | 0.79 | 1.44 | 0.57 to 1.62 |

The Focus's disc is near-white because master scales `wheel2.xds` (light grey under its
transparent texels) by the rim's 7.5. The Beetle's disc puts a bright ring (0.36 to 0.41) at
r 50 to 110 mm, where its rim is drawn dark (0.003), and nothing (0) at 170 to 190 mm, where the
rim is bright (0.49): the layout of #419.

### 2.4 The rule (from our own rim's data; no Forza blur data)

The brief's target is a disc that looks like the converted rim spinning. A spinning wheel's
apparent colour at radius r is the mean of what is drawn round that circle. With the decoded
disc program inverted:

```text
M(r)    = mean over angle of the albedo (linear) of the front-most non-disc surface of the
          port's wheel part (+ its brake rotor part) seen along the axle at radius r
C       = one RGB per car for g_vec4WheelBlurColour: M's area-weighted chroma, largest channel 1
tex.r   = encode_sRGB(lum(M(r)) / lum(C))     tex.g = the gloss the rim has there
tex.b   = 255 (opaque: the port's rim keeps drawing under it)
disc vertex AO (position.w) on ring r = the mean AO of those surfaces on ring r
```

The parameters all come from the port's own converted wheel (atlas texels, vertex AO,
geometry). Error left:

- the direct term's `N.L` differs between the rim's normals and the flat disc;
- `C` can't vary per ring, so per-ring chroma becomes per-car chroma;
- chrome rims are reflection-dominated, so their rotational mean as drawn needs the
  environment term, not the albedo.

Against vanilla: this matches the level (vanilla's per-vehicle grey) and also the profile,
which vanilla doesn't.

### 2.5 Which ShaderSettings a Forza wheel reads (confirmed-static, both games)

FH1 `0x82543558` (FM4 `0x82407848`) builds the settings path list for a model of type 4 (called
from FH1 `0x82548e78` when `r15 == 4`; the other types get the car list below):

```text
game:\media\cars\shared\ShaderSettings\Tire\Normal.xml;
[game:\media\cars\shared\ShaderSettings\Tire\Slod.xml;]          (the far LOD)
game:\media\Wheels\<name>[_slod]\ShaderSettings[_slod].xml;
```

For the other types: `Normal.xml` (with `Matte.xml` or `Metallic.xml`), `SLod.xml`,
`Cars\<car>\ShaderSettings.xml` and `ColorShaderSettings<n>.xml` (FH1 `0x82549300..0x82549420`).
Type 4 reading `media\Wheels` is what makes it the wheel (inferred from the path).
`Tire/Normal.xml` sets `rim PaintScale 0.65` (CubeScale 0.2, SpecularAmount 5.5) and
`blur_rim` 0.65. `Normal.xml`'s rim 0.75 is the car-model default.

`forza_blur.py survey` (`out/re4/wheel-survey.jsonl`: 512 of the 573 roster entries read; the
61 others are the `@fm4` duplicates the tool doesn't resolve and cars without blur pictures):

- 56 wheel archives have their own `ShaderSettings.xml`. The Beetle 68's sets rim 0.93 and
  blur_rim 1.33; its car file sets rim 0.73.
- The rim PaintScale master uses differs from the game's on 500 of the 502 cars with a `rim`:
  0.75 against 0.65 on most. It is more than ×1.5 off on 25, among them:

  | Car | Master | Game |
  | --- | ---: | ---: |
  | FOR_FocusRS500_10 | 7.5 | 0.65 |
  | FOR_ShelbyGT500_13 | 6.75 | 0.65 |
  | CHE_Chevelle_70 | 0.75 | 0.175 |
  | JAG_XKRS_12 | 0.75 | 2.5 |
  | LAM_SestoElemento_11 | 0.75 | 2.0 |
  | TVR_Sagaris_05 | 0.75 | 1.8 |
  | MAS_MC12Corsa_08 | 1.15 | 0.35 |
  | FER_599GTO_11 | 0.75 | 0.3 |
  | FER_F151_11 | 0.75 | 0.35 |
  | HYU_ix20_11, KIA_Ceed_11, LAN_DeltaS4_86, MIT_Starion_88 | 0.75 | 0.35 |
  | FIA_595_68 | 0.75 | 0.4 |
  | PON_FirebirdTransAm_77 | 0.75 | 1.4 |
  | CHE_CamaroSS_69 | 0.75 | 1.5 |
  | AUS_MiniCooperS_65 | 1.45 | 0.65 |

The same chain sets every wheel technique's parameters (`inner_rim`, `chrome_rim`,
`wheel_emblem`...). This changes the input of the rule above (the converted rim's albedo), so it
belongs with the wheel work.

### 2.6 Forza's own blur maths (decoded before the scope changed; for reference)

Not needed for the rule above.

- `media/cars/shared/GlobalCarAttribs.xml` `<BlurRim GeomBlendStart="0.5" GeomBlendEnd="0.5"
  MaxRPM="350" BlurRimAlphaAdjustment="0.3">`, read by FH1 `0x82d91088` into `0x832b38b0`.
- `b = clamp(|wheel rpm| / MaxRPM)` (FH1 `0x82d91e70`, a per-wheel vtable method `0x82dd33b8`).
- The car draw (FH1 `0x82435850..0x82435b48`, FM4 `0x823170b0..0x823173a0`) sets:
  - `c40 blurRimGeomTexBlend = (step at b = 0.5 (1 on LOD 4+), frac, t, 0.3 t)` with
    `t = linstep(0.5, 1, b)`;
  - `c56 blurRimTexBlendAmounts`: weights of the pictures, `2t` (FH1: wheel0, wheel1, wheel3;
    the loader `0x82da2680` skips wheel2) or `3t` (FM4: wheel0..3).
- `rim` alpha = `1 - c40.x`.
- `blur_rim` = `PaintScale x TintColor x sum w_i L_i x light`, alpha
  `c40.x x sat(sum w_i A_i + c40.w)`. `forza_blur.py verify`: the closed forms match the
  emulator exactly on 100 random cases per game.
- The rim switches to the disc at 175 rpm, and the disc shows `wheel3` from 350 rpm.

## Open

- **Lamps:** Forza's unlit lighting against DSF's (`forza-lenses-re.md` open item 5). It is
  needed to translate a Forza clear glass texel's brightness, and isn't decoded.
- **Lamps:** FH1's `dynamicLights2.x` by day is inferred from `TimeOfDayA.xml`, not traced in
  `default.xex`.
- **Blur:** the sRGB-read state of the disc's sampler is inferred (measured for paint and
  Lights only). The blend factors of the disc materials are inferred. DSF's `b` (the engine's
  per-wheel `.y`) is not traced.
- **Blur:** chrome rims need a rotational mean of the drawn colour with reflection, not the
  albedo.
