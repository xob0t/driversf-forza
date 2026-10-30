# What Forza draws for lamp lenses, covers and glass (reverse engineering, round 3)

Updated: 2026-10-30

Round 1 (`lights-re.md`) gave the DSF side: a lamp is one opaque Lights texel, its RGB the unlit
look and its alpha the glow; glass over a lamp keeps 25% of the glow and can't tint it. This
note gives the target: what Forza Horizon 1 and Forza Motorsport 4 compute for every lamp,
lens, lamp-glass and glass technique the roster uses, read out of the race shaders on the
discs, and what that means for the DSF texels. Nothing here changes the converter.

Confidence levels: `confirmed-static` (read in the shader microcode or the data, and checked
by running that microcode), `confirmed-runtime` (seen in a running Forza; nothing here is, as
no emulator is on this PC), `inferred` (follows from the above, not checked).

Tools added with this note (`tools/re/`; inputs extracted to `out/re3` by
`forza_lens.py extract`, which reads the FH1 disc and FM4 disc 1):

- `fxl.py`: parses Forza's `.fxobj` effects (the XDK FXLite format): techniques, passes,
  render and sampler states, the vertex and pixel shader of every pass, and disassembles them.
- `xenos.py`: a Xenos (Xbox 360 GPU) microcode decoder, disassembler and emulator, after
  xenia's `ucode.h` and `shader_translator.cc`.
- `forza_lens.py`: runs a technique's race pixel shaders in the emulator on one texel
  (`probe`), holds the decoded maths as closed forms (`spec`) and checks them against the
  emulator (`verify`), and surveys the roster (`survey`).
- `forza_cluster.py`: renders a car's lamp clusters with Forza's own shaders, every lamp
  pixel shaded by the emulator (`render`), and compares head lamps with Forza's thumbnails
  (`compare`).

## 1. Where the shaders are, and how they are bound (confirmed-static)

Every car technique is in one effect per game: `media/shaders/cars/shaders_v16.fxobj` (FH1:
254 techniques, 258 vertex and 241 pixel shaders; FM4: 196 techniques). The custom
`<technique>.fxobj` files next to it are loaded in the UI only (`CustomCarShaders.xml`,
`inraceload="false"`, `lights.md`, "Lamp glass"); a race uses `shaders_v16`.

An `.fxobj` is Forza's header (type `0x101` = FXLite, hash, size, technique, vertex
declaration and stride counts) around an XDK FXLite compiled effect, which starts with a
12-byte header (magic `0xA3D70141`). Every offset inside the effect counts from the end of
that header. `fxl.py` reads:

| Where (effect offsets) | What |
| --- | --- |
| `+0x200`, `+0x20c` | the technique pointer array and its count |
| technique | name, pass count, 1, flags, pass pointers |
| pass (20 bytes) | name, 0, pass block, render state block, sampler state block |
| pass block (0x2240 bytes, mostly runtime scratch) | `+0x48` vertex shader, `+0x4c` pixel shader (the shader object; its `0x102A110x` container follows at +8 or +0x10) |
| render state block | 6 dirty-mask dwords, a count, (state, value) pairs |
| sampler state block | per-sampler masks, a count, (value, sampler << 16 \| state) pairs |

The render state ids are the Xbox 360 `D3DRS` values (`0x3C` alpha blend enable, `0x48`
source blend, `0x4C` destination blend) and the blend values are the Xenos blend enum (0 zero,
1 one, 6 source alpha, 7 inverse source alpha, 8 destination colour). Read this way, the lamp
and glass techniques split in two:

| Technique | Passes (blend) |
| --- | --- |
| `detail_glass_clear`, `_red`, `_amber` (FH1 and FM4) | p0 (`DESTCOLOR, ZERO`: a multiply), p1 (`ONE, ONE`: an add), Cleanup (`SRCALPHA, INVSRCALPHA`) |
| everything else: `glass`, `window`, `red_glass`, `black_glass`, `lights_glass`, `lights_gls_*`, every lamp, `textured_reflector`, `reflector` | one pass, **no render states at all** |

So the lenses carry their own blend, and every other lamp and glass technique takes the
blend of whatever draw bucket the game puts it in, which is not in the effect (Open, item 1).
The `Cleanup` pass is shared by 30 FM4 and most FH1 paint techniques (a full paint shader
writing `alphaDirtAmount` as alpha); it is taken here as a pass the race view does not
composite for the lenses (inferred: for `body` it is an opaque pass that would replace p0).

**Texture fetch (confirmed-static).** The Lights and nodamage atlases' fetch constants
(`.xds` header +0x1C, dword 0 `0x840000FE`) set `sign_x/y/z = 3` (gamma) and `sign_w = 0`:
the hardware turns each RGB byte into linear light with the Xenos piecewise-linear gamma curve
(xenia `PWLGammaToLinear`: byte `b` to `b`, `2b - 64`, `4b - 256` or `8b - 1024` over 1023 by
segment, close to `(b/255)^2`), and reads alpha as stored. Below, `L` is that linear texel
RGB and `A` its alpha (0..1). The environment map is fetched raw and squared in the shader
(`E^2`).

**Output (confirmed-static).** Every car pixel shader ends with `sqrt` of its linear colour,
so the render target holds `sqrt(linear)` and every blend happens on those values. A multiply
blend in that space is exact (`sqrt(a) sqrt(b) = sqrt(ab)`); an alpha blend mixes
`sqrt`-encoded colours. The scene is HDR (FH1's `TimeOfDay.xml` post effects: filmic tone
map, adaptive exposure), so a lamp's light is not clipped at 1 before the tone map (inferred
for the target format).

## 2. The decoded maths (confirmed-static)

Notation: linear light; `hl = sat(0.5 n.l + 0.5)^2` (a wrapped diffuse); `F = F0 + (1 - F0)
(1 - n.v)^5`, `F0 = ((FresnelIndex - 1) / (FresnelIndex + 1))^2` (Schlick);
`Fr = sat(FresnelScalar x reflectionScaler x F)`; `S` the sun colour (`shDirectionalColour`),
`H` the SH ambient interpolant at the normal, `E^2` the environment; `A`, `L` the texel.
Output `sqrt(c)` with alpha `a`. A pixel in sun (shadow 1), no specular highlight
(`SpecularAmount`'s Phong term, small and view-dependent, left out), no fog.

| Family (FH1) | Colour `c` | Alpha `a` |
| --- | --- | --- |
| lens: `detail_glass_clear`, `_amber` | p0: `sqrt(L + GlassColor.rgb)` multiplied onto what is behind; p1: `E^2 x (Glancing + (Direct - Glancing) x (FresnelScale (n.v)^FresnelPower + FresnelBiasA))` added | not read |
| lens: `detail_glass_red` | p0: `sqrt(DarkValues + (LightValues - DarkValues) L.r, L.g, L.b)` multiplied; p1 as above | not read |
| lamp: `tail_light*`, `reverse_light*`, `head_light*`, `textured_reflector`, `hidhead`, `oldhead`, `xenonhead`, `indicator_*`, `taillight*`, `fogred`, `drlwhite*`, `slorange`, `brin*`, `brkreverse`, `l/rindtailside` | `L (PaintScale S hl + AmbientColor H + CubeScale E^2) + glow` | `head_light*`, `tail_light*`, `reverse_light*`: `GlassMaxOpacity`; the rest 1 |
| `reflector` | `L (GlassColor (PaintScale S hl + AmbientColor H) + CubeScale E^2)` | `min(max(A, GlassColor.w) + FresnelScalar x reflectionScaler x F, GlassMaxOpacity)` |
| lamp glass: `lights_gls_*` | `L (GlassColor AmbientColor H + lerp(GlassColor PaintScale S hl, E^2, Fr x dampner)) + glow` | `GlassMaxOpacity` (texel alpha not read) |
| cover: `lights_glass` | `L lerp(GlassColor PaintScale S hl, E^2, Fr)` (its VS leaves `H` 0) | `min(max(A, GlassColor.w) + FresnelScalar x reflectionScaler x F, GlassMaxOpacity)` |
| plain glass: `glass`, `glass_2`, `window`, `red_glass` | `lerp(GlassColor (0.4 + 0.6 hl), E^2, Fr)`; no texture at all | `min(GlassColor.w + FresnelScalar x reflectionScaler x F, GlassMaxOpacity)` |
| `black_glass` | `Fr x E^2` | 1 |

`dampner` is the VS's cube-map dampener (`dp4_sat` of the SH with `shCubeMapDampnerLight`).
`head_light` and `head_light2` have no SH interpolant (their VS doesn't write it), so their
`AmbientColor H` term is 0.

**Glow (FH1)**, added to `c`: `colour x state x A`, the texel alpha as the glow mask:

| Technique | Colour (shared default) | State constant |
| --- | --- | --- |
| `tail_light`, `_lod0` | `BrakeColor` (2, 0, 0) | `dynamicLightsAmount.x` (brake) |
| `tail_light_on` | `BrakeColor` (3.6, 0, 0) | brake |
| `tail_light2` | `BrakeColor` (0.6, 0, 0) | brake |
| `reverse_light*` | `ReverseLightColor` (1, 1, 1) | `dynamicLightsAmount.y` (reverse) |
| `indicator_left`, `_right` | `IndicatorLightColor` (1, 0.28, 0) | `dynamicLightsAmount.z`, `.w` |
| `taillightst` | `TailLightColor` (0.4, 0, 0) | `dynamicLights2.x` (lamps on) |
| `taillight2S` | `lerp(TailLightColor (0.4,0,0), BrakeColor (1.5,0,0), brake) x sat(lamps on + brake)` | both |
| `hidhead` / `oldhead` / `xenonhead` | `HidHeadlightColor` (1, 0.8, 0.8) / `OldHeadlightColor` (1.4, 0.9, 0.6) / `XenonHeadlightColor` (1, 1, 1.1) | `dynamicLights2.x` |
| `drlwhite` | `DRLWhiteColor` (1, 1, 1) | `dynamicLights2.z` (DRL) |
| `drlwhiteswitch` | `DRLWhiteColor` x DRL x (1 - lamps on) | both |
| `fogred` | `FogRedColor` (1, 0, 0) | `dynamicLights2.w` |
| `slorange` | `SideLightsOrangeColor` (1, 0.66, 0) | `dynamicLights3.z` |
| `brinl` / `brinr` | `BrakeColor x lerp(brake, indicator, indicator mode)` | `dynamicLights3.x` / `.y` the mode |
| `l/rindtailside` | `lerp(max(TailLightColor lamps on, BrakeColor brake), IndicatorRedLightColor indicator, mode)` | all three |
| `lights_gls_<lamp>` | as the lamp of the same name (`lights_gls_tail_light` BrakeColor 0.7, `_reverse_light` 0.6, `_taillight2S` 0.7 / 0.5) | the same |
| `head_light*`, `textured_reflector`, `reflector`, `lights_glass`, `lights_gls_noemit`, every lens and glass | none | |

**FM4** differs (confirmed-static, same checks):

- its shaders have no sun colour constant (`S` = 1, times the track lightmap's shadow) and
  add the ambient flat: lamp `c = AmbientColor H + L (PaintScale hl + CubeScale E^2) + glow`
  (`reflector`: `GlassColor PaintScale` in place of `PaintScale`);
- the glow mask is the **texel colour, not its alpha**: `tail_light*` `BrakeColor x
  brakeReverseAmount.x x L`, `reverse_light*` `ReverseLightColor x brakeReverseAmount.y x L`;
  `head_light` has no glow, as in FH1;
- FM4 has only `head_light`, `tail_light`, `reverse_light`, `textured_reflector`, `reflector`,
  `lights_glass`, the lenses and the glass techniques, and its cars use only those (no
  indicators, no running tail glow: `brakeReverseAmount` is brake and reverse);
- lenses, `lights_glass` and plain glass have the same maths as FH1.

**Check.** `forza_lens.py verify` draws 300 random texels, material parameters, normals,
views, sun and sky colours, environment values, `reflectionScaler` and lamp states per
technique and compares the closed forms with the emulator running the microcode: the largest
difference is 0.0 for all 47 FH1 car techniques and all 21 FM4 ones (FH1's `window_traffic*`,
traffic cars only, are not covered). The closed forms were read out of a symbolic run of the
same emulator (sympy values in the registers); the numbers in them are exact: `0.4 + 0.6 hl`
for glass, `169/2809` = `((1.65 - 1)/(1.65 + 1))^2` for the default FresnelIndex. That the
emulator decodes the microcode right is checked by the formulas making physical sense
throughout (Schlick, a wrapped Lambert, `sqrt` output, the lamp colours the shared settings
name) and by the decode following xenia's; the two xenia details the repo's older
`fxobj.py` disassembler got wrong (a scalar op's operand is the swizzle's w slot; an export's
scalar result goes to the vector destination) and two it didn't show (scalar ops with an empty
write mask still feed the next op's `ps`; `cexec_pc` is a bool-constant branch) are fixed in
`xenos.py`.

**What this says about the questions.**

- *Does a lens tint the light behind it?* Yes, all of it: a `detail_glass_*` lens multiplies
  everything behind it (the reflector, the bulb, the lamp's glow, a trim) by `L + GlassColor`
  (red: its red remap), then adds a faint environment reflection. It has no lit colour of its
  own, its texel alpha is never read, and it can't glow. With the clear lens's GlassColor 0.2,
  a white clear-lens texel passes 1.2: it brightens what is behind.
- *Covers.* `lights_glass` is a lit, texel-coloured surface (GlassColor x PaintScale, default
  0.5 x 1) with the texel alpha as opacity; `lights_gls_*` is opaque lamp glass (alpha
  GlassMaxOpacity, 1 unless the car sets less) that glows by its own texel alpha;
  `lights_gls_noemit` is the same without glow, so it is not a texel-alpha cover.
- *Plain glass* reads no texel; its colour is GlassColor shaped by the sun's angle (not its
  colour), its opacity at least GlassColor.w (0.3 shared default) plus the Fresnel term.
- *Lamp states* (`dynamicLightsAmount`, `dynamicLights2`, `dynamicLights3` in FH1,
  `brakeReverseAmount` in FM4) are set by the game per car. FH1's time of day has
  `SwitchOnLights` about 0 from 08:00 to 19:00 (`media/tracks/colorado/Ribbon_00/TimeOfDayA.xml`),
  so by day FH1's head and running tail lamps (`dynamicLights2.x`) are off and only brake,
  reverse, indicator, DRL and fog states light (inferred: what feeds the state from that key).

## 3. Roster

`forza_lens.py survey` (566 of the 573 cars of `tools/batches/all.txt`, main carbin, LOD 1;
`out/re3/survey/<car>.json`): technique use and the cars' own ShaderSettings.

| Technique | Cars | Subsections | Triangles | Texel alpha p10 / 50 / 90 | Median texel |
| --- | ---: | ---: | ---: | --- | --- |
| `window` | 538 | 3,095 | 144,736 | (no texel read) | |
| `textured_reflector` | 526 | 1,928 | 382,685 | 117 / 255 / 255 | (149, 121, 118) |
| `tail_light` | 508 | 1,127 | 73,676 | 234 / 255 / 255 | (146, 105, 106) |
| `reverse_light` | 491 | 761 | 33,495 | 163 / 255 / 255 | (156, 154, 154) |
| `black_glass` | 475 | 1,030 | 119,154 | (no texel read) | |
| `glass` | 468 | 1,510 | 128,472 | (no texel read) | |
| `detail_glass_clear` | 447 | 1,432 | 81,873 | 30 / 156 / 255 (not read) | (145, 143, 141) |
| `head_light` | 396 | 789 | 77,219 | 255 / 255 / 255 | (143, 142, 142) |
| `detail_glass_red` | 391 | 920 | 107,090 | 43 / 190 / 255 (not read) | (155, 1, 2) |
| `reflector` | 373 | 1,073 | 72,628 | 156 / 255 / 255 | (154, 98, 57) |
| `detail_glass_amber` | 172 | 341 | 16,903 | 49 / 200 / 255 (not read) | (180, 72, 4) |
| `taillight2S` (FH1) | 139 | 248 | 19,156 | 52 / 178 / 255 | (148, 105, 105) |
| `indicator_left` / `_right` (FH1) | 137 | 571 | 23,630 | 28 / 169 / 255 | (170, 134, 126) |
| `oldhead` (FH1) | 101 | 189 | 12,475 | 44 / 175 / 255 | (174, 174, 174) |
| `lights_glass` | 96 | 240 | 31,870 | 84 / 203 / 253 | (140, 12, 7) |
| `xenonhead`, `hidhead` (FH1) | 70, 52 | 227 | 8,730 | 62 / 176 / 255, 66 / 195 / 255 | grey |
| `taillightst` (FH1) | 67 | 117 | 7,615 | 63 / 201 / 255 | (142, 16, 16) |
| `lights_gls_*` with a glow (FH1, 7 techniques) | 50 at most | 368 | 18,488 | about 10 / 115 / 240 | the lamp's colour |
| `lights_gls_noemit` (FH1) | 39 | 81 | 6,202 | 0 / 96 / 224 | (159, 4, 5) |
| `slorange`, `fogred`, `drlwhite` (FH1) | 46, 41, 41 | 200 | 11,143 | | |
| `lindtailside`, `rindtailside`, `tail_light_on`, `brin*`, `brkreverse`, `drlwhiteswitch`, `red_glass` | 13 or fewer | | | | |

FM4's 391 cars use only FM4's ten lamp and glass techniques (`tail_light` 389 cars, `head_light`
379, `textured_reflector` 376...); FH1's 175 the full FH1 set. On FM4's `tail_light` the texel
alpha is 255 on 92% of the texels (it doesn't correlate with the red, r = 0.11), and Forza
doesn't read it there: FM4's glow mask is the texel colour.

The cars' own ShaderSettings (cars, then min / median / max of the value; GlassColor's w after
the semicolon):

| Parameter | Cars | Values |
| --- | ---: | --- |
| `detail_glass_red` DarkValues / LightValues | 138 / 119 | 0 / 0.5 / 2; 0.29 / 1.2 / 4 |
| `lights_glass` GlassMaxOpacity | 90 | 0.1 / 0.8 / 1 |
| `detail_glass_clear` GlassColor | 84 | rgb 0 / 0.55 / 1.25; w 0 / 0.6 / 1 |
| `lights_glass` GlassColor | 73 | rgb 0.01 / 0.6 / 1.3; w 0 / 0.75 / 1 |
| `textured_reflector` PaintScale / CubeScale | 51 / 34 | 0.1 / 0.6 / 2.4; 0.02 / 1 / 3.5 |
| `lights_gls_*` GlassMaxOpacity | 42 to 46 each | 0.4 / 0.8 / 1 |
| `reflector` PaintScale / CubeScale | 41 / 26 | 0.07 / 0.35 / 1.65; 0.16 / 0.78 / 3.5 |
| `detail_glass_amber` GlassColor | 34 | rgb 0 / 0.74 / 2; w 0 / 0.8 / 1 |
| `tail_light` / `head_light` / `reverse_light` PaintScale | 26 / 24 / 22 | 0 / 0.8 / 2; 0.2 / 1 / 1.5; 0.15 / 0.85 / 1.6 |
| `glass` GlassColor / GlassMaxOpacity | 17 / 13 | rgb 0 / 0.26 / 0.8, w 0 / 0.07 / 1; 0.06 / 0.3 / 0.75 |
| `head_light` CubeScale | 15 | 0.5 / 1.5 / 5 |

The lens filters this gives over the roster's lens texels (linear, per channel, p10 / p50 /
p90): clear 0.30 / 0.59 / 1.01 (11% of the texels pass more than 1, so they brighten what is
behind), amber red channel 0.17 / 0.51 / 1.07, green 0.01 / 0.10 / 0.55, blue near 0; red lens
red channel 0.15 / 0.44 / 0.91, green and blue under 0.02. So Forza's clear lenses typically
halve the light behind them, a red lens passes 15% to 90% of the red and almost nothing else.

## 4. Ground truth and validation

**Sources.** No Xenia is installed on this PC, and a Forza run on the host would take over
the user's screen, so nothing here is confirmed-runtime. The discs hold Forza's own pictures
of each car: FM4's 503 and FH1's thumbnails (`media/ui/textures.zip`
`Thumbnails/Thumbnail_<Data_Car.Id>.xds`, 768 x 288, 8888), studio renders from the front left
with the lamps off. They show head lamps only, never tails; their lighting (dark studio, soft
boxes) is unknown; they are small (a head lamp is 50 to 100 pixels wide). Trust: shapes and
colours of the front lamps, qualitatively. The Autovista models are geometry, not pictures.
Of the brief's cars the F50, S60, Mustang GT 05, R8 LMS and Commodore VE have thumbnails; the
Commodore's front lamps are under decals.

**Validation of the maths: exact** (`verify` above): the closed forms are the microcode.

**Cluster renders** (`forza_cluster.py render`, render_compare's rasteriser, every lamp pixel
shaded by the emulator with its own texel, normal and view; car_render's studio light as the
sun, sky and environment; FM4 inputs where FM4's VS has them):

- Commodore VE race car (`HOL_1_CommodoreVE_11`), rear, by day: the tail lamp is a deep red
  lens with the reflector's structure seen through it and a clear reverse strip below it;
  braking, the round lamp glows red. render_compare's reference today draws the same lens as
  a milky pink plate (`commodore-tails.jpg` in the PR).
- Ferrari F50 (`FER_F50_95`), rear: all four round tails are even red lenses, the inner pair
  too (the red lens filters the white reverse lamp behind it); braking, the outer halves glow.
  The reference draws the inner lamps as silver discs, the "white patches in the inner lamps"
  of issue #300 (`f50-tails.jpg`).

**Against Forza's thumbnails** (`forza_cluster.py compare`, head lamps). Pixel registration
with the thumbnails failed: their camera is closer than render_compare's `THUMB_CAMERA` (a
head lamp is about 1.45 times larger there) and the reference scene has no wheels, so
neither outline fitting nor image correlation found a reliable match. The comparison is
therefore registration-free, over a hand-picked head lamp box in the thumbnail
(`THUMB_BOXES`) and the render's front-lamp pixels, after an exposure fit: local contrast over
mean luma, p90/p10 luma spread, saturation.

Contrast / spread / saturation over the head lamp (thumbnail box; the renders' front-lamp
pixels at render_compare's `THUMB_CAMERA`; out/re3/cmp-alpha and cmp-add `compare.json`). The
thumbnail boxes hold some body paint round the lamp, so their saturation reads high on red and
yellow cars:

| Car | Forza thumbnail | this maths, alpha bucket | this maths, additive bucket | render_compare reference |
| --- | --- | --- | --- | --- |
| FER_F50_95 | 0.45 / 3.2 / 0.73 | 0.23 / 1.9 / 0.35 | 0.42 / 2.5 / 0.45 | 0.54 / 5.0 / 0.62 |
| VOL_S60_11 | 0.60 / 9.4 / 0.42 | 0.29 / 3.4 / 0.05 | 0.36 / 6.0 / 0.05 | 0.54 / 30.1 / 0.07 |
| FOR_MustangGT_05 | 0.44 / 6.2 / 0.35 | 0.30 / 9.6 / 0.24 | 0.36 / 14.1 / 0.25 | 0.60 / 8.7 / 0.22 |
| AUD_R8LMS_09 | 0.34 / 25.3 / 0.03 | 0.22 / 2.1 / 0.01 | 0.30 / 3.0 / 0.01 | 0.52 / 16.0 / 0.02 |
| AUD_2_R8_06 | 0.44 / 6.5 / 0.08 | 0.25 / 1.6 / 0.01 | 0.39 / 2.3 / 0.02 | 0.74 / 6.7 / 0.06 |
| LOT_Carlton_89 | 0.56 / 7.4 / 0.05 | 0.23 / 2.0 / 0.05 | 0.23 / 2.0 / 0.05 | 0.24 / 1.8 / 0.05 |
| VW_GTIVR6_98 | 0.29 / 3.6 / 0.24 | 0.28 / 2.7 / 0.01 | 0.28 / 2.9 / 0.02 | 0.32 / 4.8 / 0.03 |

Both models draw the lamp units flatter than Forza's pictures: Forza's chrome bowls carry
bright soft-box highlights that car_render's environment doesn't have, and that term (the
lamps' `CubeScale E^2`) dominates their look. The thumbnails therefore don't separate the lens
models; they do say that the reflection environment, not the lens maths, sets most of a head
lamp's look in a studio. One case is informative: the Audi R8 race car (`AUD_2_R8_06`), whose
head lamps sit behind two plain `glass` layers (the lamp unit's and the `lexan` cover). With
the inferred alpha blend and the shared GlassColor.w 0.3, the two layers veil the lamp to a
dark plate; Forza's thumbnail shows the lamp clearly. With an additive bucket (`--bucket add`,
`SRCALPHA, ONE`) the render's contrast moves toward the thumbnail's (0.25 to 0.38, Forza 0.44;
the S60, also under plain glass, 0.28 to 0.35, Forza 0.61). That is weak evidence that the
game's transparent bucket doesn't darken what is behind as an alpha blend would, or that
`reflectionScaler` is low; it stays open (section 6).

## 5. Mapping to DSF

DSF's side (`lights-re.md`): `HDR = (amb + dir) t + spec + refl + a C` per Lights texel, `t`
its linear RGB, `a` its alpha, `C` the lamp colour x 5 x level; window glass keeps `(1 - 0.75)`
of what is behind and can't tint. Each Forza family's best DSF form, with the error expected
from what DSF can't do:

| Forza family | DSF form | Bake (linear light) | Expected error |
| --- | --- | --- | --- |
| opaque lamp, FH1 (`tail_light`, `taillight2S`, `indicator_*`, `hidhead`...) | Lights texel | `t = L x K_t / K_dsf`, `K_t = PaintScale S hl + AmbientColor H + CubeScale E^2` with the car's values; `a` from the texel alpha `A`, scaled from Forza's `colour x state` to DSF's level for the lamp's type | the `CubeScale E^2` term (0.9 to 1 by default, up to 5 on car settings) is a view-dependent sky reflection that DSF's Lights reflection (`tex.g` mask x cube map) only approximates; glow hue is DSF's lamp colour, Forza's is the technique's (the same hue for the standard colours) |
| opaque lamp, FM4 | Lights texel | `t` as above with FM4's flat ambient; `a` from the **texel colour**: `BrakeColor x L` (its red channel) on `tail_light*`, `ReverseLightColor x L` on `reverse_light*`; none on `head_light` | none beyond the row above; an alpha-based glow mask is a flat plate on FM4 tails (their alpha is 255 on 92% of texels) |
| lens `detail_glass_*` over a lamp | the lens keeps its triangles, its texels baked from what is behind it | `t' = t_behind x filter`, `filter = L + GlassColor` (clear, amber) or `(D + (Lv - D) L.r, L.g, L.b)` (red), clipped at 1; `a' = a_behind x filter` in the lamp's colour channel (a red lamp under a red lens: x 0.15 / 0.44 / 0.91 over the roster) | the lens's own reflection (`E^2 x 0.1` straight on, up to `x 0.4` grazing) is left to DSF's Lights reflection; a lamp whose colour differs from the filtered glow (a white reverse lamp behind red glass) glows in its own colour unless the lamp plan types it by the drawn colour (`lights.seen_sampler`); 11% of clear-lens texels would brighten (filter over 1) |
| lens over a non-lamp surface (trim, paint, housing) | the same bake | `t' = (that surface's colour) x filter` | none beyond the above: this is the red the Commodore VE's gloss trim takes under its tail glass in Forza (#340) |
| lamp glass `lights_gls_<lamp>` | Lights texel, its own glow | `t = L x K_gls / K_dsf`, `K_gls = GlassColor (PaintScale S hl (1 - Fr) + AmbientColor H) + Fr E^2`; `a` from `A x colour x state`; with GlassMaxOpacity under 1, `(1 - GlassMaxOpacity)` of what is behind blended in the sqrt space | small; the Fresnel share `Fr` is 0.06 straight on |
| `lights_gls_noemit` | Lights texel, no glow | as above without glow; opaque at GlassMaxOpacity 1 (26 of its 39 cars) | none: today it is baked as a texel-alpha cover over the lamp |
| cover `lights_glass` over a lamp | lamp texels baked | `sqrt(t') = alpha sqrt(c_cover) + (1 - alpha) sqrt(t_behind)`, `c_cover = L x GlassColor x PaintScale x (lit)`, `alpha = min(max(A, GlassColor.w) + FresnelScalar reflectionScaler F, GlassMaxOpacity)`; glow `a' ~ a_behind (1 - alpha)^2` | depends on the bucket (section 6): with the alpha bucket covers keep 0.7% / 3.8% / 20% (p10 / p50 / p90) of the lamp's glow over the roster, today 100%; with an additive bucket all of it |
| plain `glass` over a lamp | (a) left out; (b) DSF window glass; (c) a veil baked into the lamp texels | (c): `sqrt(t') = alpha sqrt(GlassColor (0.4 + 0.6 hl)) + (1 - alpha) sqrt(t_behind)`, `alpha = GlassColor.w + FresnelScalar reflectionScaler F` (0.3 to 0.49 with the shared values) | with the alpha bucket Forza keeps `(1 - alpha)^2` = 0.26 to 0.49 of a lamp's glow under one plain glass layer, which is DSF window glass's 0.25 at `reflectionScaler` 1: (b) matches it, (a) is 2 to 4 times brighter; with an additive bucket (a) is right |
| `reflector` | Lights texel | `t = L x K_r / K_dsf`, `K_r = GlassColor (PaintScale S hl + AmbientColor H) + CubeScale E^2` (0.75, 0.5 shared) | opaque unless the car sets GlassColor.w under 1 (none of the 5 cars that set it) |
| `black_glass` | gloss trim | black with the environment at `Fr` | none |
| `red_glass` (2 cars) | DSF window glass, or a veil as plain glass | an unlit red `GlassColor` (0.85, 0, 0) at opacity 0.6 or more | DSF glass can't be red |

**Errors of today's bakes against the maths** (`out/re3/errors.py` over the survey's texels:
today's rule and Forza's result as atlas bytes, CIE76 delta E median / p90, behind each lens a
grey reflector (170), a red lamp (200, 40, 40) or a white bulb (235)):

| Technique (today's rule) | grey reflector | red lamp | white bulb | glow |
| --- | --- | --- | --- | --- |
| `detail_glass_clear` (`a x lens + (1 - a) x behind`) | 14.9 / 21.8 | 38.8 / 70.8 | 8.0 / 30.7 | Forza passes x 0.31 / 0.59 / 1.01 (p10 / 50 / 90) of a red lamp's light |
| `detail_glass_red` (same) | 26.0 / 62.0 | 19.4 / 31.1 | 30.7 / 83.7 | x 0.15 / 0.44 / 0.91 |
| `detail_glass_amber` (same) | 27.8 / 52.9 | 22.1 / 41.6 | 23.3 / 69.0 | x 0.17 / 0.51 / 1.07 |
| `lights_glass` (blend at `min(max(A, w), cap)` in byte space, the texel's own colour) | 11.0 / 20.9 | 11.2 / 21.3 | 11.1 / 21.5 | today 1; Forza (alpha bucket) 0.007 / 0.038 / 0.197 |
| plain `glass` (left out) | 17.6 | 40.3 | 25.9 | today 1; Forza (alpha bucket, shared values) 0.26 |

The lens errors are large because today's bake mixes the lens texel's own colour in at its
alpha: a red lens texel (155, 1, 2) at alpha 190 over a white bulb comes out a pale pink,
where Forza shows the bulb through a red filter (deep red); a clear lens texel over a red lamp
greys it, where Forza dims it and keeps it red. The `lights_glass` row compares colour at a
reference light (the cover lit like the lamp); its glow column is what decides the look.

## 6. Rules in `lenses.py`, `forza_materials.py` and the reference

For the lamp rewrite (#300, #340) and the reference (#313). "Confirm": the maths agrees;
"replace": it says otherwise; "remove": the rule rests on a premise the maths contradicts.

`src/dsfforza/convert/lenses.py`:

- **Replace** the lens composite in `bake` (`out = a x lens + (1 - a) x behind`, `a` the lens
  texel alpha capped by `GlassMaxOpacity`, the module docstring's "Forza's blend"): a lens
  multiplies, `t' = t_behind x filter` in linear light, and never reads its alpha or
  `GlassMaxOpacity`. The baked glow follows the lamp behind times the filter, not the lens
  texel's `lamp_score`.
- **Replace** `lens_filter`: for clear and amber lenses the filter is `L + GlassColor` (shared
  0.2 grey on clear, 0 on amber, car overrides on 84 and 34 cars), which `lens_filter` leaves
  out, and it may pass more than 1. Its red remap `sqrt(D + (L - D) red^2)` on bytes is the
  shader's `D + (L - D) PWL(red)` in the sqrt space with the PWL curve taken as a square: it
  **confirms** above byte 64 (within 2 codes) and reads darker below (Forza's byte 16 passes
  like byte 32).
- **Replace** `opacity_rule` for lenses (floor 0, cap the car's `GlassMaxOpacity`): lenses have
  no opacity. For `lights_glass` it **confirms** `min(max(A, GlassColor.w), GlassMaxOpacity)`,
  plus a Fresnel term (`FresnelScalar x reflectionScaler x F`, 0.06 straight on at the shared
  FresnelIndex 1.65).
- **Replace** `COVERS = ("lights_glass", "lights_gls_noemit")` (and
  `dsf_materials.TEXTURED_COVERS`): `lights_gls_noemit` is lamp glass, opacity
  `GlassMaxOpacity`, no glow; only `lights_glass` is a texel-alpha cover.
- **Replace (pending the bucket)** the covers' "whole glow of the lamp behind" (#164): under
  the alpha bucket a `lights_glass` cover passes `(1 - alpha)^2` of it, a median 4% over the
  roster.
- **Confirm** `GLASS_DEFAULTS` (`lights_glass` (0, 1), `lights_gls_` (1, 1): Normal.xml), lamp
  glass baked at its cap with its own glow (#164), and that covers have no glow of their own
  (`emits`).
- **Remove** the premise of the comment at `GLASS_DEFAULTS` ("a lens colour of texel x
  GlassColor x PaintScale (a twentieth of the texel) and Forza's reflection ... milky plates"):
  a race lens has no lit colour; the `PaintScale` and `GlassColor.w` in Normal.xml's lens
  sections are not read by the race lens shaders (only `GlassColor.rgb` on clear and amber,
  `DarkValues`/`LightValues` on red, and the p1 reflection's terms).
- **Remove** the reason for leaving flat and swatch rear lenses out (`_flat`, the swatch path
  with `_tint_behind`, `SWATCH_TINT`): the filter is what Forza draws over every surface
  behind, so a lens kept and baked by the multiply gives the same picture over a lamp and the
  right one over a trim or paint (#340: the Commodore VE's trim under its red tail glass). The
  scrambled-texel detection (`_scrambled`, #141) stays useful for where each texel's colour
  comes from.
- Not affected: the head lamp day glow (`lens_glow`, `day_glow`, `DAYTIME_GLOW`) is DSF's own
  choice (0x1D lamps lit by day); Forza's head lamp texels glow only on `hidhead`, `oldhead`,
  `xenonhead` and the `lights_gls_` heads, from `dynamicLights2.x`, which FH1's time of day keeps
  near 0 by day.

`src/dsfforza/convert/dsf_materials.py`:

- **Replace for FM4** the glow mask from the texel alpha (`LIGHTS_ALPHA` "matched",
  `lamp_score`): FM4's lamp shaders glow by `BrakeColor x L` / `ReverseLightColor x L`, the
  texel colour, and FM4's `tail_light` alpha is 255 on 92% of the texels. **Confirm for FH1**:
  every FH1 lamp glows by its texel alpha.
- **Pending the bucket**, `Texturing.clear_cover` (plain `glass` over a lamp left out, #160,
  #247): under the alpha bucket one plain glass layer keeps 26% to 49% of the lamp, about DSF
  window glass (25%), plus a GlassColor veil; under an additive bucket leaving it out is right.
- The unlit look of the lamp texels: Forza's lamp techniques differ in brightness by their
  `PaintScale`, `CubeScale`, `AmbientColor` (defaults: `tail_light`, `reverse_light`,
  `head_light`, `textured_reflector` 0.3 / 0.9 / 0.1; `hidhead`, `oldhead`, `indicator_*`,
  `taillight2S` 1 / 1 / 0.1; `lights_gls_*` 0.7 x GlassColor 1 with AmbientColor 0.7; and the
  car settings), which the red and white darkening (`red_lamp_curve`, the white cap of
  #115) don't follow. A per-technique gain `K_t` is the Forza-side input for those curves.

`src/dsfforza/convert/forza_materials.py`: `classify` takes `lights_gls_noemit` as a cover with
`lights_glass` (via `dsf_materials.TEXTURED_COVERS`); replace as above.

`src/dsfforza/harness/forza_render.py` (render_compare's reference): its lens model (the lens
picture at `PaintScale x GlassColor` over the lamp, opacity `max(alpha, GlassColor.w)` capped,
`render-compare.md`) is what draws the Commodore VE's lenses milky and the F50's inner lamps
silver; `tools/re/forza_lens.spec` holds the decoded passes and `forza_cluster.py` shows them
in render_compare's renderer.

## Open

1. **The blend of the passes that set none** (plain glass, `lights_glass`, `lights_gls_*`,
   the lamps): the game's transparent bucket, not in the effect. Taken as `SRCALPHA,
   INVSRCALPHA` (the shaders write an opacity as alpha); the R8 race car's thumbnail leans
   toward a blend that doesn't darken (section 4). It decides the cover and plain-glass rows
   of section 5. Settling it needs Forza running (Xenia, a lamp cluster close up by day, with
   and without the glass) or the draw code in `default.xex`.
2. **`reflectionScaler`**, a game constant (shader default 0), scales the Fresnel term of every
   glass opacity and colour. Not found in the time-of-day data. The renders here use 1.
3. **The `Cleanup` pass** of the lenses (and of the paint techniques) is taken as not drawn in
   the race view (inferred).
4. **No tail lamp ground truth** on the discs: the thumbnails show the front only. A
   confirmed-runtime check of the F50's and the Commodore VE's tails needs Forza running.
5. **Unlit-look calibration**: FH1's noon sun (0.74, 0.64, 0.42 with `SunColorMult`), hemisphere
   light (top 0.17, 0.22, 0.27; bottom 0.16, 0.16, 0.09) and `IBLAmbientScale` 1.1 are in
   `TimeOfDayA.xml`; the cube map's brightness, which carries the lamps' `CubeScale E^2`, is
   not decoded.
