# What Forza draws for lamp lenses, covers and glass (reverse engineering, round 3)

Updated: 2026-10-30 (round 3b: section 7)

Round 1 (`lights-re.md`) gave the DSF side: a lamp is one opaque Lights texel, its RGB the unlit
look and its alpha the glow; glass over a lamp keeps 25% of the glow and can't tint it. This
note gives the target: what Forza Horizon 1 and Forza Motorsport 4 compute for every lamp,
lens, lamp-glass and glass technique the roster uses, read out of the race shaders on the
discs, and what that means for the DSF texels. Nothing here changes the converter.

Confidence levels: `confirmed-static` (read in the shader microcode, the data or the game's
executable, and checked by running that microcode where it is shader code), `confirmed-runtime`
(seen in a running Forza; nothing here is, as no emulator is on this PC), `inferred` (follows
from the above, not checked).

**Round 3b (section 7, issue #370), read out of `default.xex`:** the techniques whose passes
set no blend draw with **`SRCALPHA, INVSRCALPHA`** (the state the lens techniques' `Cleanup`
passes restore, and the only blend the game's code sets itself); **`reflectionScaler` is 1** in
both games (0 only in one special lens draw); and the game converts the RGB of **every float4
material parameter** (`GlassColor`, `BrakeColor`, `PaintColor`...) **from sRGB to linear** on
the CPU before upload, so the values the shaders see are not the XML's. Sections 1 to 6 are
updated for these.

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
  (`compare`; `--authored` uses the XML's colours as written, round 3's inputs).
- `xex.py` (round 3b): reads FH1's and FM4's `default.xex` (XEX2, AES with the retail key,
  basic compression) and writes the loaded image (`out/re3b/<game>/image.bin`).
- `ppc.py` (round 3b): PowerPC helpers over that image: functions from `.pdata`, `lis`-pair
  cross-references, callers, a capstone disassembly annotated with strings and floats.
- `forza_blend.py` (round 3b): `evidence` re-derives section 7's findings from both
  executables; `predict` recomputes section 5's numbers from the roster survey.

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
blend that is current when it draws: `SRCALPHA, INVSRCALPHA` (section 7, confirmed-static).
The `Cleanup` pass (on the lenses and on 30 FM4 and most FH1 paint techniques) is never drawn:
the game finds it by name when it loads the effect and applies its render states when the
car's material switches away from that technique, to put back the state the next draws expect
(section 7, confirmed-static). For the lenses that is `ALPHABLENDENABLE 1, SRCALPHA,
INVSRCALPHA`; for the paint techniques, state `0xF0` back to 0.

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
`Fr = sat(FresnelScalar x reflectionScaler x F)`, `reflectionScaler` = 1 (section 7); `S` the
sun colour (`shDirectionalColour`), `H` the SH ambient interpolant at the normal, `E^2` the
environment; `A`, `L` the texel. Output `sqrt(c)` with alpha `a`. A pixel in sun (shadow 1),
no specular highlight (`SpecularAmount`'s Phong term, small and view-dependent, left out), no
fog. Every material parameter below is the value **as uploaded**: for float4 parameters (written
`r g b a` in ShaderSettings) that is `srgb_to_linear` of the written RGB with the alpha as
written (section 7); scalars (`value=`) as written.

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

**Glow (FH1)**, added to `c`: `colour x state x A`, the texel alpha as the glow mask. The
colours below are as written in the shared ShaderSettings; the shader gets them linearised
(section 7): `BrakeColor` 2 -> 4.95, 3.6 -> 19.7, 1.5 -> 2.54, 0.7 -> 0.45, 0.6 -> 0.32,
`TailLightColor` 0.4 -> 0.13, 0.5 -> 0.21, `IndicatorLightColor` (1, 0.28, 0) -> (1, 0.064, 0),
`HidHeadlightColor` (1, 0.8, 0.8) -> (1, 0.60, 0.60), `OldHeadlightColor` (1.4, 0.9, 0.6) ->
(2.16, 0.79, 0.32), `XenonHeadlightColor` (1, 1, 1.1) -> (1, 1, 1.24), `SideLightsOrangeColor`
(1, 0.66, 0) -> (1, 0.39, 0); 0 and 1 are unchanged. A brake lamp's glow is therefore about
2.5 times what round 3 read, and the coloured lamps more saturated.

| Technique | Colour (shared default, as written) | State constant |
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
  own, its texel alpha is never read, and it can't glow. The clear lens's shared GlassColor 0.2
  is uploaded as 0.033, so a white clear-lens texel passes 1.03 (round 3 read 1.2).
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
p90; `forza_blend.py predict`, GlassColor as the game uploads it): clear red 0.14 / 0.41 / 0.81,
green 0.10 / 0.40 / 0.79 (5% of the texels pass more than 1); amber red 0.17 / 0.50 / 0.99,
green 0.01 / 0.09 / 0.35, blue near 0; red lens red channel 0.15 / 0.44 / 0.91, green and blue
under 0.02 (DarkValues and LightValues are scalars, not converted). So Forza's clear lenses
pass about 40% of the light behind them, a red lens 15% to 90% of the red and almost nothing
else. (Round 3, with GlassColor as written: clear 0.30 / 0.59 / 1.01 with 11% over 1, amber
red 0.17 / 0.51 / 1.07, green 0.01 / 0.10 / 0.55.)

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
the S60, also under plain glass, 0.28 to 0.35, Forza 0.61). Round 3 read that as weak evidence
for an additive bucket. Section 7 rules that out: the blend is `SRCALPHA, INVSRCALPHA` and
`reflectionScaler` 1. What closes part of the gap instead is the colour upload: with the
glass and lamp colours linearised as the game uploads them (the shared plain glass tint 0.1
becomes 0.01, so the veil is a dark grey rather than a milky one), the alpha model's contrast is
R8 0.32 (was 0.25), S60 0.34 (0.29), F50 0.27 (0.23), against Forza's 0.44 / 0.61 / 0.45
(`forza_cluster.py compare` with and without `--authored`; out/re3b/cl-game, cl-authored). The
rest is the studio environment, as above.

## 5. Mapping to DSF

DSF's side (`lights-re.md`): `HDR = (amb + dir) t + spec + refl + a C` per Lights texel, `t`
its linear RGB, `a` its alpha, `C` the lamp colour x 5 x level; window glass keeps `(1 - 0.75)`
of what is behind and can't tint. Each Forza family's best DSF form, with the error expected
from what DSF can't do:

| Forza family | DSF form | Bake (linear light) | Expected error |
| --- | --- | --- | --- |
| opaque lamp, FH1 (`tail_light`, `taillight2S`, `indicator_*`, `hidhead`...) | Lights texel | `t = L x K_t / K_dsf`, `K_t = PaintScale S hl + AmbientColor H + CubeScale E^2` with the car's values; `a` from the texel alpha `A`, scaled from Forza's `colour x state` to DSF's level for the lamp's type | the `CubeScale E^2` term (0.9 to 1 by default, up to 5 on car settings) is a view-dependent sky reflection that DSF's Lights reflection (`tex.g` mask x cube map) only approximates; glow hue is DSF's lamp colour, Forza's is the technique's (the same hue for the standard colours) |
| opaque lamp, FM4 | Lights texel | `t` as above with FM4's flat ambient; `a` from the **texel colour**: `BrakeColor x L` (its red channel) on `tail_light*`, `ReverseLightColor x L` on `reverse_light*`; none on `head_light` | none beyond the row above; an alpha-based glow mask is a flat plate on FM4 tails (their alpha is 255 on 92% of texels) |
| lens `detail_glass_*` over a lamp | the lens keeps its triangles, its texels baked from what is behind it | `t' = t_behind x filter`, `filter = L + GlassColor` (clear, amber) or `(D + (Lv - D) L.r, L.g, L.b)` (red), clipped at 1; `a' = a_behind x filter` in the lamp's colour channel (a red lamp under a red lens: x 0.15 / 0.44 / 0.91 over the roster) | the lens's own reflection (`E^2 x 0.1` straight on, up to `x 0.4` grazing) is left to DSF's Lights reflection; a lamp whose colour differs from the filtered glow (a white reverse lamp behind red glass) glows in its own colour unless the lamp plan types it by the drawn colour (`lights.seen_sampler`); 5% of clear-lens texels brighten (filter over 1; 11% with GlassColor as written) |
| lens over a non-lamp surface (trim, paint, housing) | the same bake | `t' = (that surface's colour) x filter` | none beyond the above: this is the red the Commodore VE's gloss trim takes under its tail glass in Forza (#340) |
| lamp glass `lights_gls_<lamp>` | Lights texel, its own glow | `t = L x K_gls / K_dsf`, `K_gls = GlassColor (PaintScale S hl (1 - Fr) + AmbientColor H) + Fr E^2`; `a` from `A x colour x state`; with GlassMaxOpacity under 1, `(1 - GlassMaxOpacity)` of what is behind blended in the sqrt space | small; the Fresnel share `Fr` is 0.06 straight on |
| `lights_gls_noemit` | Lights texel, no glow | as above without glow; opaque at GlassMaxOpacity 1 (26 of its 39 cars) | none: today it is baked as a texel-alpha cover over the lamp |
| cover `lights_glass` over a lamp | lamp texels baked (**decided**: bake the fraction; neither leave the cover out nor keep it as DSF glass) | `sqrt(t') = alpha sqrt(c_cover) + (1 - alpha) sqrt(t_behind)`, `c_cover = L x GlassColor x PaintScale x (lit)` (GlassColor as uploaded: the shared 0.5 is 0.21), `alpha = min(max(A, GlassColor.w) + FresnelScalar F, GlassMaxOpacity)` (`reflectionScaler` 1); glow `a' = a_behind (1 - alpha)^2` | the blend is `SRCALPHA, INVSRCALPHA` (section 7): covers keep 0.7% / 3.8% / 20% (p10 / p50 / p90) of the lamp's glow over the roster's 96 cover cars, today 100%; DSF window glass (25%, untinted) would be 1.3 to 36 times too bright and can't follow the texel alpha |
| plain `glass` over a lamp | **decided**: (b) DSF window glass; (c) a veil baked into the lamp texels where the glass can't stay; not (a) left out | (c): `sqrt(t') = alpha sqrt(GlassColor (0.4 + 0.6 hl)) + (1 - alpha) sqrt(t_behind)`, `alpha = GlassColor.w + FresnelScalar F` (`reflectionScaler` 1; 0.49 straight on with the shared values: w 0.3, FresnelIndex 3.55, FresnelScalar 0.6; more toward grazing) | Forza keeps `(1 - alpha)^2` = 0.26 of a lamp's glow under one plain glass layer straight on (less at grazing), DSF window glass 0.25: (b) matches within 1%; (a) is 4 times too bright. The veil colour is GlassColor as uploaded (shared 0.1 -> 0.01, a dark grey), which DSF glass, untinted, matches too |
| `reflector` | Lights texel | `t = L x K_r / K_dsf`, `K_r = GlassColor (PaintScale S hl + AmbientColor H) + CubeScale E^2` (0.75, 0.5 shared) | opaque unless the car sets GlassColor.w under 1 (none of the 5 cars that set it) |
| `black_glass` | gloss trim | black with the environment at `Fr` | none |
| `red_glass` (2 cars) | DSF window glass, or a veil as plain glass | an unlit red `GlassColor` (0.85, 0, 0) at opacity 0.6 or more | DSF glass can't be red |

**Errors of today's bakes against the maths** (`out/re3/errors.py` over the survey's texels:
today's rule and Forza's result as atlas bytes, CIE76 delta E median / p90, behind each lens a
grey reflector (170), a red lamp (200, 40, 40) or a white bulb (235)):

| Technique (today's rule) | grey reflector | red lamp | white bulb | glow |
| --- | --- | --- | --- | --- |
| `detail_glass_clear` (`a x lens + (1 - a) x behind`) | 14.9 / 21.8 | 38.8 / 70.8 | 8.0 / 30.7 | Forza passes x 0.14 / 0.41 / 0.81 (p10 / 50 / 90) of a red lamp's light (0.31 / 0.59 / 1.01 with GlassColor as written) |
| `detail_glass_red` (same) | 26.0 / 62.0 | 19.4 / 31.1 | 30.7 / 83.7 | x 0.15 / 0.44 / 0.91 |
| `detail_glass_amber` (same) | 27.8 / 52.9 | 22.1 / 41.6 | 23.3 / 69.0 | x 0.17 / 0.50 / 0.99 |
| `lights_glass` (blend at `min(max(A, w), cap)` in byte space, the texel's own colour) | 11.0 / 20.9 | 11.2 / 21.3 | 11.1 / 21.5 | today 1; Forza 0.007 / 0.038 / 0.197 |
| plain `glass` (left out) | 17.6 | 40.3 | 25.9 | today 1; Forza (shared values, straight on) 0.26 |

The delta E columns use the colour parameters as written (round 3); with the game's linearised
GlassColor the clear and amber lens results are darker still, so the errors against today's
bakes grow. The glow column is recomputed with the uploaded values (`forza_blend.py predict`).

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
- **Replace** `lens_filter`: for clear and amber lenses the filter is `L + GlassColor`, with
  GlassColor's RGB as the game uploads it, `srgb_to_linear` of the written value (shared 0.2
  grey on clear, i.e. 0.033; 0 on amber; car overrides on 84 and 34 cars), which `lens_filter` leaves
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
- **Replace** the covers' "whole glow of the lamp behind" (#164): a `lights_glass` cover passes
  `(1 - alpha)^2` of it (blend confirmed, section 7), a median 4% over the roster; bake that
  fraction into the lamp texels' glow, and the cover's veil (`L x GlassColor x PaintScale`, the
  GlassColor linearised) into their colour.
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
- **Replace** `Texturing.clear_cover` (plain `glass` over a lamp left out, #160, #247): one plain
  glass layer keeps 26% of the lamp straight on (blend and `reflectionScaler` confirmed,
  section 7), DSF window glass 25%, so keep the glass as DSF window glass (or, where it can't
  stay, bake the veil); leaving it out draws the lamp 4 times too bright. The veil's colour
  (shared GlassColor 0.1, uploaded 0.01) is a dark grey, which untinted DSF glass also is.
- **Replace** the lamp glow levels' Forza side: the shaders get the lamp colours linearised
  (`BrakeColor` 2 is 4.95, section 2), so a Forza brake lamp is 2.5 times brighter relative to
  its unlit look than the XML suggests, and indicators and old-style heads more saturated.
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

## 7. The game's side: blend, `reflectionScaler`, colour upload (round 3b, confirmed-static)

Read in FH1's and FM4's `default.xex` (`xex.py extract`: XEX2, AES-128 with the retail key,
"basic" compression, so no LZX; the image loads at 0x82000000), disassembled with `ppc.py`
(capstone; the Xenon's VMX128 instructions decoded by hand from xenia's opcode masks: `lvx128`
0x100000C3, `stvx128` 0x100001C3, `vmulfp128` op 5 xo 144). `forza_blend.py evidence` prints
every address and constant below for both games.

**Render states.** D3D keeps a table of `{getter, setter, default}` per render state, indexed
by state x 3 (FH1 0x8328ce00, FM4 0x82d74820), and the device copies the setters to
`device + 0x40 + state`. The blend setters write the device's shadow of `RB_BLENDCONTROL`
(`device + 0x2ff8`; the hardware copies at `+0x2938` and `+0x2958..0x2960` for the four render
targets) and confirm the ids `fxl.py` uses:

| State | Name | FH1 setter | Writes | Default |
| --- | --- | --- | --- | --- |
| `0x3C` | ALPHABLENDENABLE | 0x8244ccd8 | `+0x2ffc` bit 31 (off: the hardware gets ONE, ZERO) | 0 |
| `0x40` | SEPARATEALPHABLENDENABLE | 0x82456bb0 | `+0x2ffc` bit 30 (off: the alpha fields copy the colour ones) | 0 |
| `0x48` / `0x4C` / `0x50` | SRCBLEND / DESTBLEND / BLENDOP | 0x824562c8 / 0x82456348 / 0x823ee5e0 | bits 0-4 / 8-12 / 5-7 | ONE / ZERO / ADD |
| `0x54` / `0x58` / `0x5C` | the alpha ones | 0x823ff428 / 0x823ff3c8 / 0x823e63c8 | bits 16-20 / 24-28 / 21-23 | ONE / ZERO / ADD |
| `0x134` | HIGHPRECISIONBLENDENABLE | 0x8244cd80 | `RB_COLOR_INFO`'s format (2_10_10_10 or its 16-bit variant) | 0 |

**Who sets the blend.** The game's own code calls SRCBLEND and DESTBLEND directly only with
`SRCALPHA, INVSRCALPHA` (six and seven calls in each game), plus constant-alpha fades
(`CONSTANTALPHA, INVCONSTANTALPHA`: the ghost car, FH1 0x82d83350, which saves and restores the
blend around it), a blend-factor pass, one premultiplied `ONE, INVSRCALPHA` and FH1's one
`ONE, ONE` with BLENDOP MIN (not a car pass). `ONE, ONE` additive exists only in the effect's
lens p1 passes. Under the car passes (FH1 0x82ddb3a0 and below: the per-object draw 0x82da8cb0,
the technique substitution for the shadow, ghost and drop-shadow passes 0x82da1758, the draw
0x82da1148) the code sets only ALPHABLENDENABLE and HIGHPRECISIONBLENDENABLE: draw buckets
0x2a to 0x3e (and a few others) turn blending on (0x82daa960 sets the object flag
`+0x2e4 & 0x80`, which 0x82da8cb0 turns into ALPHABLENDENABLE 1), pass types 1, 2, 3, 5 and 8
turn it off. The factors are whatever is current.

**The `Cleanup` passes restore it.** When the game loads a car effect it looks up, for every
technique, a pass named `"Cleanup"` and stores its index (FH1 0x82c5c078, the string at
0x82c5c300; FM4 0x8286a688). When the car's material switches technique (FH1 0x82d9cdc8, which
reads that index for the previous technique, kept at `+0x4d4`), it applies the previous
technique's `Cleanup` pass's render states (0x8244c590 runs a pass's state block through
`device + 0x40 + state`) and commits them before setting up the next technique; the pass is
never drawn. A lens's `Cleanup` sets `ALPHABLENDENABLE 1, SRCALPHA, INVSRCALPHA`: after a lens's
multiply (p0) and add (p1), the game puts back alpha blending for what follows. A paint
technique's sets state `0xF0` back to 0 after p0's 4.

**So** `glass`, `window`, `red_glass`, `lights_glass`, `lights_gls_*` and the lamps, which set
no blend, draw with **`SRCALPHA, INVSRCALPHA`** where their bucket enables blending.
Confirmed-static: it is the only blend the car code establishes, and the state every lens
leaves behind. Inferred: that it is also current before the first lens of a frame (the frame
level was not traced; the only other blends the game sets directly are the special ones
above, and an opaque `ONE, ZERO` would draw glass solid), and that plain glass and
`lights_glass` are in a blending bucket (they write an opacity; in a non-blending bucket the
car's windows would be solid). Additive is ruled out.

**`reflectionScaler`** is pixel shader constant `c52.x` in every car technique of both games, a
register the game sets itself (FH1's executable doesn't even name it). Its only writes are in
the car draw (FH1 0x82da1148, FM4 0x828fa8a8), into the device's shadow of `c52`
(`device + 0x1780 + 52 x 16 = +0x1ac0`, dirty bit 50 of `+8`; the same layout puts the vertex
constant written there at `c124`). For one special draw of a `detail_glass_*` lens in the
blending buckets (object flag `+0x2e7 & 4`; depth writes on, `c55.x` set) it loads
`c52 = (0, 0, 0, 0)`, and after it restores **`c52 = (1, 1, 1, 1)`** (FH1 0x82001120, FM4
0x820017e0), unconditionally. So **`reflectionScaler` = 1 in both games, in every view and
mode, for every material and car**, and 0 only for that one lens draw (its purpose isn't
decoded; it doesn't involve plain glass, covers or paint). FH1's `HoodReflectionAmount`
(`GlobalCarAttribs.xml` `ReflectionSettings`, default 0.3 at 0x832b3968) is read only from XML
version 7 up (both games ship version 6), and no reader of the value was found. Confidence:
high. The restore is the renderer's stated invariant; whether FXL could commit the effect's
default 0 into `c52` before the first such draw of a session was not traced.

**Colour upload.** Before a float4 material parameter goes to the shader, the game converts its
RGB with the **sRGB transfer function**: `c / 12.92` up to 0.04045, else
`((c + 0.055) / 1.055)^2.4`; alpha untouched, no clamp (no `vmaxfp` or `vminfp` in the routine,
so 2 becomes 4.95). The routine is FH1 0x8244d220, FM4 0x82346b90 (constants 0.055, 1/1.055,
2.4, 1/12.92, 0.04045 and log2/exp2 polynomials). It runs on:

- every float4 ShaderSettings parameter, in the loader that also turns `D3DRS_*` entries into
  render states (FH1 0x82c44740: type float, class vector; FM4 0x8285cfd0): `GlassColor`,
  `BrakeColor`, `AmbientColor`, `SpecularColor`, `TintColor`, the lamp colours... Scalars
  (`value=`: `PaintScale`, `DarkValues`, `GlassMaxOpacity`...) go up as written;
- `PaintColor` and the other car colours (FH1 0x82dae960, called from 0x82daec98; FM4
  0x82905c80): the car's colour, sRGB to linear, times `(s, s, s, 1)` with `s` = 1 there. Not a
  square: the exact piecewise curve.

Both games skip it only when the developer switch `lineargammaout` is set (FH1 byte 0x83444ba0,
FM4 0x82dfda5c, 0 in the retail image; set from the command line, next to `rebootoncrash` and
`e3demo`). For the paint (`paint-re.md`): `PaintColor` reaches the shader as
`srgb_to_linear(colour)`, alpha as is.

**Predictions** (`forza_blend.py predict` over round 3's survey, 566 cars):

| | Round 3 | Round 3b |
| --- | --- | --- |
| cover glow kept, p10 / 50 / 90 (96 cars, 31,870 texels) | 0.007 / 0.038 / 0.197 if the bucket is alpha | the same, confirmed (opacities are scalars or alphas, not converted) |
| plain glass keeps, one layer, straight on (468 cars) | 0.26 to 0.49 if alpha | 0.26 (shared values; less toward grazing) |
| clear lens filter, red, p10 / 50 / 90 | 0.31 / 0.59 / 1.01, 11% over 1 | 0.14 / 0.41 / 0.81, 5% over 1 |
| amber lens filter, red / green p50 | 0.51 / 0.10 | 0.50 / 0.09 |
| brake glow colour (`tail_light`) | 2 | 4.95 |
| head lamp contrast against Forza's thumbnail (R8 / S60 / F50) | 0.25 / 0.29 / 0.23 | 0.32 / 0.34 / 0.27 (Forza 0.44 / 0.61 / 0.45) |

**DSF mapping (section 5).** Covers: bake the fraction `(1 - alpha)^2` of the lamp's glow (a
median 4%) and the cover's veil into the lamp texels; neither leave them out (100%) nor keep
them as DSF glass (25%). Plain glass over a lamp: keep it as DSF window glass (25% against
Forza's 26%); leaving it out is 4 times too bright. Every colour parameter the converter reads
from ShaderSettings must be linearised as above before it enters these formulas.

## Open

1. **Settled by section 7**: the blend of the passes that set none (`SRCALPHA, INVSRCALPHA`),
   `reflectionScaler` (1), the `Cleanup` passes (never drawn; a state restore). Left from them:
   the frame-level blend before the first lens of a frame, and which bucket each technique is
   assigned (both inferred, section 7); the special lens draw with `c52` 0 (FH1 0x82da1148, FM4
   0x828fa8a8) is not decoded.
2. **Round 3's renders and delta E tables** used the colour parameters as written;
   `forza_lens.py` and `forza_cluster.py` now linearise them like the game (`--authored` for
   the old inputs). The delta E columns of section 5 are not recomputed.
3. **The car colours' source**: `PaintColor` is linearised at upload; whether the colour
   stored for the car (from the database) was already transformed on load is not traced.
4. **No tail lamp ground truth** on the discs: the thumbnails show the front only. A
   confirmed-runtime check of the F50's and the Commodore VE's tails needs Forza running.
5. **Unlit-look calibration**: FH1's noon sun (0.74, 0.64, 0.42 with `SunColorMult`), hemisphere
   light (top 0.17, 0.22, 0.27; bottom 0.16, 0.16, 0.09) and `IBLAmbientScale` 1.1 are in
   `TimeOfDayA.xml`; the cube map's brightness, which carries the lamps' `CubeScale E^2`, is
   not decoded.
