# How DSF draws car lamps: the formulas (reverse engineering, round 1)

Updated: 2026-10-30

How Driver: San Francisco PC 1.04 turns a lamp texel into a pixel on screen: the shader
maths, the lamp state machine in `Driver.exe`, the mood values that scale both, and the
post chain after them. Each formula below has its evidence and a confidence level
(`confirmed-static`: read in disassembly or decompiled code; `confirmed-runtime`: read back
from the running game, with the run folder; `inferred`: follows from the above, not checked
directly). Addresses are VAs. Nothing here changes the converter; the last section maps each
tuned constant in today's lamp code to what these formulas say.

Tools added with this note (all under `tools/re/`):

- `gpch.py`: lists the vehicle GPU programs in `dngvehicles.sp`, maps each material's eight
  passes to them, disassembles them with `D3DDisassemble`, and scans any file for D3D9
  shaders (`scan media/SfxMisc.fchunk` finds the post chain). File names carry DSF Enhanced's
  shader hash (FNV-1a of the bytecode), the `vs=`/`ps=` names in its Trace log.
- `lampprobe/`: a research plugin (`lampprobe.asi`, built by `build.ps1`, never installed)
  that redirects the two calls of `0x008A78E0` and logs, once a second per car, every lamp
  object, the colour the game uploads for it, the tint vector, the colour table and the type
  scale table.
- `lamp_model.py`: the formulas as code, and `compare`, which predicts lamp pixels of dumped
  frames from the car's own texels and compares them with the frame (below).
- `lamp_frames.json`: the frames and lamp boxes `compare` uses.

The runtime evidence is one VM run, `vm1-20261030-142442` (vanilla archive, cars 130, 241,
266, 227, `-Lights -LightsDriveOnly`), with `lampprobe.asi` and DSF Enhanced's research build
`dsf_enhanced-trace2.asi` (`Trace=1`, `TraceDump=1`: the HDR scene target as PFM, the final
image as BMP, and the constants of every post-chain draw). Command:

```powershell
.\tools\wsb\run-series.ps1 -Vm -Archives out\vanilla\Install\dngvehicles.sp -Labels vanilla `
  -ProbeIds '130,241,266,227' -Lights -LightsDriveOnly -WaitSeconds 420 `
  -Override 'lampprobe.asi=out\re\lampprobe.asi;lampprobe.ini=out\re\lampprobe.ini;dsf_enhanced.asi=out\re\dsf_enhanced-trace2.asi;dsf_enhanced.ini=out\re\trace1.ini'
```

`lampprobe.ini` holds `[probe] log=C:\Output\re-lamps\lampprobe-%s.log`, `interval_ms=1000`;
`trace1.ini` is DSF Enhanced's ini with `Trace=1`, `TraceDump=1`,
`TraceDumpDir=C:\Output\re-lamps\dumps1`, `TraceStartSeconds=55`, `TraceIntervalSeconds=5`,
`TraceCount=24`. The guest's `C:\Output` is the host's `%LOCALAPPDATA%\Temp\dsfforza-wsb`.
apitrace was not needed: DSF Enhanced's tracer already hooks the device, and a second D3D9
hook would have had to share it.

## The whole chain in one place

For one lamp texel with alpha `a` (texture alpha, linear) and colour `t` (texture RGB, linear):

```text
C       = vehColourBoost.rgb x colour[b2] x scale[b3] x brightness          (CPU, per lamp)
HDR     = lit(t) + spec + refl + a x C            then fog, final tint, min(., 6)  (Lights PS)
HDR'    = HDR x (1 - g) + glass x g               where window glass covers it  (Glass PS)
La      = adapted exp(mean(ln(L + 1e-5)))          L = Rec. 709 luminance
e       = LC.y + (MG - LC.y) x sat(1.5 - LC.w / (2 La))
bloom   = blur(rgb x sat(Y - (La + thr)) / Y) x bloomTint                   Y = Rec. 601
c       = (HDR' + bloom) x e (1 + e L / W^2) / (1 + e L)                    Reinhard, white W
screen  = sRGB(LUT(c)) x vignette, + film grain
```

With the moods the game uses (ClearDay and nearly every other: `vehColourBoost` 5,
`middlegrey` 1.5, `HDRTonemapOffset` 2, `maxluminance` 5, `bloomThreshold` 0.5, bloom tint
0.1) a lamp's glow is `5 x colour x level x a` in linear light, exposed by about 1.7.

## 1. The shaders

### Where they are (confirmed-static)

The vehicle programs are the 198 `GPCH` containers of the archive's shared chunk (SSIC,
root entry 450, five DRSPs), numbered here in walk order (`gpch.py dump`). Each GPCH is
`GPCI` (uid, 100), `GPVD` (vertex declaration), `GPPM` (parameters), `GPTS`, and `GPUS`, a
D3DX `fx_2_0` effect with one `vs_3_0` and one `ps_3_0`, compiled with debug info (source
lines from `R:\Resources\Shaders\Uber2_0\...`). A material (MACO) has eight passes (MARC);
the pass header MARH names its program by GPCI uid at +16.

| Material | Passes 0..7 (GPCH) | Colour passes |
| --- | --- | --- |
| `<car>_Lights` (full entry) | 161 130 2 192 165 174 175 7 | 161 (no damage decal), 130 (decal) |
| `<car>_Lights_LOD1` (exterior: the player's car) | 155 173 10 149 135 174 175 13 | 155, 173 (both sample the shadow map) |
| `GLOBAL_Chassis` (paint) | 14 15 2 16 17 18 19 7 | 14, 15 |
| `GLOBAL_Chassis_LOD1` | 26 27 10 28 29 30 31 32 | 26, 27 |
| `GLOBAL_Glass` | 46 47 48 49 50 51 52 53 | 46, 47 |
| `GLOBAL_Chassis_Chrome` / `GLOBAL_LOD_Chrome` | 20 21 10 22 ... / 66 67 10 68 ... | 20/21, 66/67 |

Pass 2 (or 10) is the depth pass (`PC\DepthPass.hlsl`), 7/13 the shadow-map pass. In the
VMs' traces only the pass-0 vertex shaders appear among the scene draws (`A28CD300` =
GPCH 14, `B067A394` = GPCH 46), so pass 0 is the one drawn at the VMs' settings; pass 1 is
the same maths plus the damage decal (and, for some programs, more texture work). The post
chain is in `media/SfxMisc.fchunk` (`gpch.py scan`), source `PostProcessing.fx`,
`Post Process\OperationsPixel.hlsl`, `PostProcessCombinerPixel.hlsl`; their hashes match the
ones DSF Enhanced's Trace logs (`2F0359F4` tone map, `F1076FF6` its vertex shader,
`0CFF7616` bright pass, `C0305A02` particle combine, `EAF259B6` / `0E0535E4` luminance).

### A lamp texel's glow (confirmed-static; GPCH 130, 161, 155, 173)

Vertex shader (`Vehicles_Functions.hlsl` line 495): `o4 = g_v4LightColours[s9.byte2].wxyz`,
`g_v4LightColours[32]` at c160. Byte 2 of vertex semantic 9 is the light id + 1 (0 = no
lamp, and entry 0 is always zero). Nothing else touches the colour in the vertex shader: no
distance, view angle or time term.

Pixel shader, GPCH 130 (full entry, pass 1; the others differ only where noted):

```text
tex      = g_samDiffuseMap(UV0)                            // the car's Lights texture
amb      = (lightmap.x x 0.4 + 0.6) x ao x lerp-by-normal(ambLower, ambUpper)
dir      = max(N.(-sunDir), 0) x sunColour x sat(2 lightmap.y) x (1 + occl (ao - 1))
spec     = max(R.V, 1e-4)^g_hSpecularPower x g_hSpecularIntensity x specColour x dirAmount
refl     = sat(envSphere(R) x g_hCubemapLuminanceBoost x tex.g
               x max(g_fCubemapFresnelMin, (1 - N.V)^g_hFresnelPower + g_hReflectionFresnelBias)
               x g_hReflectionAmount x shadowModifier x g_fTexMul2.z)
broken   = colour.w > 0.005                                // c160 w, the lamp's broken bit
diffuse  = tex.rgb x (broken ? decal.a : 1)
emission = tex.a x colour.rgb x (broken ? decal.a : 1)     // decal = g_samDecalNormalMap(UV1)
out      = (amb + dir) x diffuse + spec + refl + emission
out      = lerp(out, s_FogColour, fog)                      // linear fog from the vertex shader
out      = lerp(out, g_vec4FinalTintColour.rgb, g_vec4FinalTintColour.a)
out      = lerp(out, highlight colour, highlight term)       // the Shift highlight
oC0.rgb  = clamp(out, 0, 6)                                 // HDR.hlsl
```

So:

- The glow is `texel alpha x lamp colour`, added to the lit texel. The texel's RGB never
  enters the glow: a white, alpha-255 texel on a red lamp glows pure lamp red, and its white
  shows only as lit diffuse.
- The emission is added before fog and is fogged like any surface.
- The output is clamped at 6 per channel, in linear light, into the `A16B16G16R16F` scene
  target.
- Pass 0 (GPCH 161, 155) has no damage decal: the broken multiply is missing, so a broken
  lamp is dark there only because its brightness is 0 (below).
- GPCH 155 and 173 (the exterior entry, the player's car) add the shadow-map lookup to the
  direct light; the emission term is the same.

### Glass and lenses over a lamp (confirmed-static; the share kept confirmed-runtime)

DSF has no lens program. A vanilla lamp is one opaque Lights surface; its lens look is in
the texture. Glass is `GLOBAL_Glass` (GPCH 46/47), drawn in the blended pass:

```text
tex     = g_samDiffuseMap(UV0)                  // the shared glass texture
texkill   tex.b < 0.01                          // fully clear texels are not drawn
spec    = max(R.V, 1e-4)^g_hSpecularPower x tex.g
alpha   = max(tex.b, spec)                      // opacity: the blue channel, raised by the glint
refl    = sat(cube(R) x boost x tex.g x max(min, 1 + bias - N.V) x amount x shadowModifier x texMul2.z)
rgb     = (amb + dir) x tex.r + spec x specColour x dirAmount + refl
```

With the usual `SrcAlpha / InvSrcAlpha` blend (inferred: the output alpha is an opacity, and
DSF Enhanced's Trace shows blending on for these draws), a lamp seen through glass keeps
`(1 - alpha)` of its glow and gets the glass's lit grey, glint and reflection on top. The
window texels of the shared glass texture are blue 191 to 192 (`dsf_materials`, the Mustang GT
note). There is no colour filter: `tex.r` is a grey level, so DSF glass cannot tint a lamp.

Measured (confirmed-runtime; run `vm1-20261030-163312`, vanilla 241's nose by day, DSF Enhanced
dump `f05477` in `re-lamps\dumps5`; `out/re/glassfit.py` in the lamp-model PR's worktree): the
HDR pixels of each head lamp disc (0x1D under window glass, running lights, so the uploaded
colour is 5 x white x 1) against the lamp's own texels' `a x C`, quantiles p30 to p99 fitted as
`T x a x C + c`: T 0.500 and 0.512 (the two lamps), c 0.06 and -0.09. So a lamp under a window
keeps half its glow, not 25%: the texture is read as sRGB (its RGB decoded, its alpha not), and
blue 191 is an opacity of 0.52 (T 0.48 with the glint). `car_render` draws DSF glass that way
in the game's light (`lights.md`, "The lamp model the renderer and the harness use").

### Bloom threshold (confirmed-static, constants confirmed-runtime)

`0CFF7616` (bright pass) reads `T#6` (the scene composited with the particle layer and
clamped to [0, 6] by `C0305A02`) and the adapted luminance `La`:

```text
Y      = dot(rgb, (0.299, 0.587, 0.114))       // Rec. 601, unexposed linear
weight = sat(Y - (La + g_vThreshold.x)) / max(Y, 1e-4)
out    = (rgb, weight)
```

The 7-tap blur chain (`5F91AEC4`, 320x176 down to 40x22 and back) blurs it, and the tone map
adds `bloom.rgb x bloom.a x g_BloomTintColour`. `g_vThreshold.x` is the mood's
`bloomThreshold` and `g_BloomTintColour` its `bloomTintColour`: 0.5 and 0.1 in ClearDay,
0.15 and 0.3 in the harness's dark mood (runtime constants of every traced frame). So by day a
pixel blooms where its linear luminance exceeds about La + 0.5 = 0.67, and the bloom adds a
tenth of the excess, blurred.

### The tone map and exposure (confirmed-static, constants confirmed-runtime)

`F1076FF6` / `2F0359F4` (`PostProcessing.fx`):

```text
e   = LC.y + (MG - LC.y) x sat(1.5 - LC.w / (2 La))     // vertex shader, La by vertex fetch
c   = scene x particles.a + particles.rgb + bloom x bloom.a x bloomTint
L   = dot(c, (0.2125, 0.7154, 0.0721))
c  *= e (1 + e L / W^2) / (1 + e L)                      // g_v4LuminanceParams = (W, 1/W^2, 1, 1 - 1/W^2)
c   = lerp(LUT_from(c), LUT_to(c), g_SaturationParams.x) // 16^3 colour grade, coords c x 0.9375 + 0.03125
c  *= grain(noise) x vignette
out = sRGB(c)                                            // exact piecewise sRGB, in the shader
```

The runtime constants map to the mood (MoodParametersStruct, below): `LC` =
`(0, HDRTonemapOffset, HDRTonemapClampMin, HDRTonemapClampMax)` = (0, 2, -1, 0.3);
`MG = middlegrey` (1.5 ClearDay, 0.5 harness dark); `W = maxluminance` (5 in both). So the
exposure is `HDRTonemapOffset` (2) in a dark scene (La below 0.1), `middlegrey` in a bright one
(La above 0.3), and a blend in between. There is no other adaptation: with ClearDay's values
a dark scene gets 2 and a bright one 1.5. Measured La (log
average of the dumped scene target) was 0.15 to 0.19 by day (e = 1.64 to 1.75) and 0.002 to
0.004 in the harness's dark mood (e = 2.0). The colour grade (`idColourGrading` 1 in both
moods) is an S curve, fitted per channel from five frame pairs in `lamp_model.LUT_Y`: it maps
0.11 to 0.06, 0.31 to 0.27, 0.51 to 0.64, and tops out near 0.88 (sRGB 241 to 246). The fits
from day frames (e 1.69 to 1.75) and dark frames (e 2.0) agree within 0.01 below 0.3, which
also supports the exposure formula.

`La`: `EAF259B6` writes `ln(L + 1e-5)` of the 320x180 combined scene, `0E0535E4` averages it
down (5-tap, x 0.2), and a final pass (`F02445F9`, c0 = 0.0247 per frame) blends it with the
last frame's value (`760780ED` copies `T#20` to `T#21`); its source was not in SfxMisc, so the
adaptation step is inferred from its constant and inputs.

### Paint Fresnel (confirmed-static; GPCH 27, `GLOBAL_Chassis_LOD1` pass 1)

`research/lights.md` ("What the paint shader does with the palette") is right. The disassembly:

```text
ndv     = dot(normalize(v1), normalize(v2))        // normal, eye vector
fresnel = g_fresnelbias + g_OneMinusFresnelbias x pow(max(c57.x, 1 - sat(ndv)), g_fresnelpower)
paint   = TintColour2 + fresnel x (TintColour3 - TintColour2)
tinted  = tex.r x fColourShift x lerp(1, paint, tex.b)
```

The power is used as stored, as the DMC-12 sections found.

## 2. Lamp state in Driver.exe

### Lamp objects (confirmed-static)

A car's lamp component holds up to 32 lamp objects, 16 bytes each from +0, count at +0x230
(`0x008E82B0` makes one per type-17 UPVH record, in light-id order):

| Offset | Meaning |
| --- | --- |
| +0x00 | flags: 1 broken, 2 any function on, 4 steady (no blink), 8 combined type with DaytimeRunning on, 0x10 combined type with another function on, 0x20/0x40/0x80/0x100 per-type (0x80 the flare), 0x200 at zero |
| +0x04 | brightness (float), moved towards a target each frame |
| +0x08 | blink clock |
| +0x0C | type (light table flags b3) |
| +0x0D | variant b0: index into the blink table |
| +0x0E | colour b2 |

### Which lamps are on (confirmed-static)

`0x008E7AD0(mask, on)` (`GameVehicleResource.setLights` and the game's own calls) keeps the
car's Lua light mask at +0x220 and, for each lamp, compares it with three per-type tables:
the functions of the type (`0x00E3A998`), its steady (non-blinking) functions (`0x00E3AA18`)
and, for the combined types, the full set (`0x00E3AA98`):

| b3 | Functions | Steady | Combined | Scale (`0x00F3FB68`) |
| --- | --- | --- | --- | ---: |
| 0x01 | Headlights | | | 2 |
| 0x02 | Brakes | | | 1 |
| 0x03 | DaytimeRunning | | | 2 |
| 0x04 / 0x05 | Left / RightIndicator | | | 0.5 |
| 0x06 / 0x07 | Brakes + Left / RightIndicator | Brakes | | 0.5 |
| 0x08 | Reverse | | | 1 |
| 0x09, 0x0D..0x0F | Siren | | | 10 |
| 0x0A | DippedHeadlights | | | 10 |
| 0x0B | Headlights + Siren | Headlights | | 10 |
| 0x0C | FogLights | | | 10 |
| 0x10 / 0x11 | 2048 / 4096 | | | 10 |
| 0x12 | DaytimeRunning | | | 3 |
| 0x13 | DaytimeRunning + Siren | DaytimeRunning | yes | 10 |
| 0x14..0x17 | DaytimeRunning + Left / RightIndicator | DaytimeRunning | | 1 |
| 0x18 | Brakes + DaytimeRunning | all | yes | 1 |
| 0x19..0x1C | Brakes + DaytimeRunning + Left / RightIndicator | Brakes + DaytimeRunning | yes | 1 |
| 0x1D | Headlights + DaytimeRunning | all | yes | 10 |

What the game turns on by itself (confirmed-static): `0x008D1380` and `0x008D88E0` set
DaytimeRunning (mask 4) when a vehicle is set up or reset, so every car has its running lights
on all the time. Brakes and Reverse come from the driver (player input every frame; traffic AI
`0x00779960` through `0x008D0470(3, 2, ...)`). Headlights (mask 1) are set only by Lua
(`0x00652810`, the `setLights` binding) and by a timed flash routine (`0x008E97A0`, a 64-bit
on/off pattern, cleared by `0x008E9780`); no free-roam code turns them on. Vanilla Lua calls
`setLights` only for sirens. So in free roam a type-0x01 head lamp never lights (confirmed-runtime:
all 740 samples of 0x01 lamps in the probe log, the probe cars and traffic, day and dark, read
brightness 0, and every 0x1D lamp 0.1 or 0), and neither does fog (0x0C).

### Brightness targets (confirmed-static, values confirmed-runtime)

`0x008E6C80` builds two target tables per type in the lights manager (`[0x014551F0]`):
on at +0x6B0 and off at +0x4B0, four floats each, indexed by
`sub = (flags & 0x10 ? 1 : 0) | (flags & 8 ? 2 : 0)`. `0x008E6640` picks
`on[type][sub]` when the lamp is on and its blink pattern is on, else `off[type][sub]`, and
moves the brightness towards it at `rate` per second (linear):

| | sub 0 (single type) | 1 (combined: other function only) | 2 (DaytimeRunning only) | 3 (both) |
| --- | ---: | ---: | ---: | ---: |
| on | 1 | 1 | f / scale | 1 |
| off | 0 | 0 | 0 | f / scale |

`f` is 0.2 for 0x18..0x1C, 1.0 for 0x13 and 0x1D, 0 otherwise (`0x00F3FBE0` = 0.2,
`0x00F3FBE4` = 1.0). Because `0x008A78E0` multiplies by the scale again, the colour
multiplier is:

| Type | Running lights only (free roam) | + Brakes | + Headlights |
| --- | ---: | ---: | ---: |
| 0x01 head lamp | 0 | 0 | 2 |
| 0x02 stop lamp | 0 | 1 | 0 |
| 0x03 tail / position | 2 | 2 | 2 |
| 0x06 / 0x07 | 0 | 0.5 | 0 |
| 0x08 reverse | 0 (1 when reversing) | | |
| 0x12 | 3 | 3 | 3 |
| 0x14..0x17 | 1 | 1 | 1 |
| 0x18..0x1C tail / stop | 0.2 | 1 | 0.2 |
| 0x1D head + running | 1 | 1 | 10 |

(`lamp_model.py levels` prints the whole table.) Runtime, `lampprobe` on run
`vm1-20261030-142442`: car 130's 0x19/0x1A lamps 0.2 rolling and 1 braking or at rest, its
0x1D lamps brightness 0.1 (colour x 10 x 0.1 = 1); car 241's 0x03 tails 1 (x 2), its 0x02 stop
lamps 0 or 1; car 266's 0x18 tails 0.2 or 1 and its 0x02 high stop 0 or 1.

**Blink and fade** (confirmed-static, `0x008E6760`, table at the manager's +0x00, 24 bytes per
variant b0, filled by `0x008E6F00`): a blinking lamp is on while bit
`floor(frac(clock / period) x len)` of a 64-bit mask is set. Variant 0 and 1: period 1 s, steady;
2 and 3 (indicators): period 1 s, two slots, mask 1, so 0.5 s on and 0.5 s off. The fade rate is
3.6 per second for variants 0 and 2 (0 to 1 in 0.28 s) and 120 for 1 and 3 (instant). Sirens use
the higher variants (periods 0.5 to 2 s, 4 to 20 slots).

**Broken lamps** (confirmed-static): `0x008E65B0` (from the break walk `0x008E84A0`) sets flags
0x201 and brightness 0, and `0x008E6760` returns early for them, so a broken lamp stays at 0.
`0x008A78E0` writes the broken bit to the colour's w, which the pass-1 shaders use to mask the
diffuse and the (zero) glow by the damage decal (`lights.md`, issue #141).

### The colour the game uploads (confirmed-static, confirmed-runtime)

`0x008A78E0(out, lamps, tint)` fills `g_v4LightColours` once per car draw:

```text
out[0]      = 0
out[i + 1]  = tint.rgb x colour[b2] x scale[b3] x brightness,  w = flags & 1
```

All zero when no lamp of the car is on (`+0x220`, `+0x224`, `+0x2BC`, `+0x2BF` all clear).

**The colour table** (manager +0x3F0 + 16 x b2, set by `0x008E6F00` as sRGB bytes / 255 and
passed through `0x0049E160`, about x^2.2; read back in game):

| b2 | sRGB bytes | Linear (in game) | Used for |
| ---: | --- | --- | --- |
| 4 | 255, 130, 12 | 1, 0.229, 0.00125 | amber |
| 5 | 255, 160, 0 | 1, 0.361, 0 | amber (23 entries) |
| 6 | 200, 0, 0 | 0.588, 0, 0 | red |
| 8 | 255, 250, 225 | 1, 0.958, 0.760 | white |
| 9 | 240, 242, 255 | 0.876, 0.892, 1 | xenon white |
| 0..3, 7, 10, 11 | blues, greens, magenta, yellows | | sirens |

**The tint is the mood's `vehColourBoost`** (confirmed-runtime). The probe logged
`tint = 5, 5, 5` by day and `1, 1, 1` in the harness's dark mood, blending between the two in
0.5 s at each mood switch (1.02, 1.65, 2.57, 3.02 ... 5). ClearDay's `vehColourBoost` is 5 and
the harness mood (`tools/game/lua/dsfforza/lightstest.lua` `darkLighting`) sets 1. It is not
`emissiveColMod` (1.5 in both) and not `bloomEmissivenessScale`, which only the old lighting
Lua getters and setters touch (`0x01236280`; no renderer reference).

Examples, as uploaded (probe): car 130 braking by day `2.939, 0, 0` (5 x 0.588 x 1 x 1), rolling
`0.588` (x 0.2), in the dark `0.588` and `0.118`; its 0x1D lamps `5, 4.788, 3.802` by day; car
241's 0x03 tails `5.878` (5 x 0.588 x 2).

### The moods (confirmed-static)

`MoodParametersStruct` (the Lua `Mood` tables; setters `0x00657580` for vectors at index x 16,
`0x00657600` for floats at 0x140 + index x 4). The fields that reach the lamps:

| Field | Offset | ClearDay | Harness dark mood | Used as |
| --- | --- | --- | --- | --- |
| `vehColourBoost` | +0x130 | 5 | 1 | lamp tint (all lamps) |
| `emissiveColMod` | +0x040 | 1.5 | 1.5 | not the lamp tint |
| `middlegrey` | +0x160 | 1.5 | 0.5 | MG |
| `maxluminance` | +0x164 | 5 | 5 | W |
| `HDRTonemapOffset` / `ClampMin` / `ClampMax` | +0x170..+0x178 | 2 / -1 / 0.3 | 2 / -1 / 0.3 | LC |
| `bloomThreshold` | +0x1AC | 0.5 | 0.15 | g_vThreshold |
| `bloomTintColour` | +0x0C0 | 0.1 | 0.3 | g_BloomTintColour |
| `vehSpecularLightColour` | +0x120 | 40, 39, 30 | 2, 2, 2.5 | lamp lens glints |

`vehColourBoost` is 5 in every gameplay mood in `moods.lua` (ClearDay, Coma, LastChapter,
Rushdown, Jericoh, Frozen, the challenges), 10 in Fog, and 1 only in PauseMenu, TopZap2/3 and
the debug moods. The darkest gameplay moods (LastChapter, Rushdown) keep the tint at 5 and use
`middlegrey` 1, `HDRTonemapOffset` 1, `maxluminance` 2. The harness's dark mood, with 1, has
lamps five times dimmer in linear light than any mood a player sees.

The white blob beside car 130's plate in the dark frames is a glint, not a lamp: its colour
ratio (1.37, 1.37, 1.72) is the harness mood's `vehSpecularLightColour` (2, 2, 2.5), and the
probe shows that car's reverse lamps at 0.

### Flares

The distant-lamp flare sprites are in `research/draw-distance.md` section 4 (confirmed-static
and runtime): flag 0x80 types, colour from the lamp colour and its brightness, distance curves
`Sfx.DRLFlare*`. They add to the scene, not to the lamp mesh's own glow.

## 3. Ground truth from frames (confirmed-runtime)

Run `vm1-20261030-142442`. Dumps in `%LOCALAPPDATA%\Temp\dsfforza-wsb\re-lamps\dumps1`
(`fNNNNN-scene-end.pfm`: the linear scene target `T#3` before particles and bloom;
`fNNNNN-p29-after-d01.bmp`: the final image before the HUD), probe log
`re-lamps\lampprobe-20261030-142526.log`, constants in the run's `dsf_enhanced.log`. Frame times
were matched to the probe log by the guest clock (dump at 14:27:11.2 = probe t 105.2 s).

Read straight from the PFM, car 130 in the dark mood with its tails at running-light level
(frame f03497, uploaded colour 0.1176 red): the brightest tail pixel is 0.118 red, the tail's
densest texels have alpha 0.906, and the unlit body around it reads 0.008. 0.906 x 0.1176 +
0.008 = 0.114. The glow is linear in alpha and carries no hidden factor.

## 4. Validation: `lamp_model.py compare`

`uv run python tools/re/lamp_model.py compare --dumps <dumps1> --log <run>\dsf_enhanced.log`

For each frame and lamp group in `lamp_frames.json` it samples the car's own exterior-entry
lamp texels (the rear-facing triangles, area-weighted, 4 samples per cm²), predicts each
texel's linear value `D x t + a x C` with `C` from the formula (the probe's brightness and
tint, the colour and scale tables), and compares quantiles with the frame's lamp pixels
(red-dominant pixels in the box). `D`, the light falling on the lens, is the one fitted number
per group (on the median); the emission has no free parameter. It then runs the predicted
values through the post chain (exposure from the frame's La and constants, Reinhard, the
fitted grade, sRGB) and compares with the final image.

Result (9 frames, 3 cars, day and dark, 13 lamp groups; `out/re/compare1.json` in the worktree):

| Frame | Car | Mood | Lamps (level) | HDR p99 obs / pred | Screen p99 obs / pred |
| --- | --- | --- | --- | --- | --- |
| f02984 | 130 | day | 0x19 L (1) | 2.90 / 2.65 | 244 / 241 |
| f02984 | 130 | day | 0x1A R (1) | 2.97 / 2.68 | 245 / 241 |
| f03497 | 130 | dark | 0x19 L (0.2) | 0.113 / 0.122 | 101 / 111 |
| f03497 | 130 | dark | 0x1A R (0.2) | 0.115 / 0.121 | 103 / 110 |
| f03711 | 130 | dark | 0x19 L (1) | 0.560 / 0.645 | 233 / 241 |
| f03711 | 130 | dark | 0x1A R (1) | 0.552 / 0.536 | 233 / 239 |
| f04734 | 241 | day | 0x03 (2) + 0x02 (1) | 1.14 / 1.19 | 242 / 241 |
| f04557 | 241 | day | 0x03 (2), 0x02 off | 1.09 / 1.28 | 242 / 241 |
| f04917 | 241 | dark | 0x03 (2) + 0x02 (1) | 0.194 / 0.217 | 157 / 181 |
| f05250 | 241 | dark | 0x03 (2), 0x02 off | 0.192 / 0.218 | 157 / 182 |
| f05884 | 266 | day | 0x18 (0.2) | 0.609 / 0.542 | 233 / 234 |
| f06197 | 266 | day | 0x18 (1) | 1.13 / 1.06 | 244 / 241 |
| f06197 | 266 | day | 0x02 high stop (1) | 1.50 / 1.17 | 246 / 241 |

Brightest 1% of lamp pixels in linear light: median error x1.11, worst x1.28. On screen: median
5/255, worst 25/255 (241's dark frames, where the bloom the model leaves out lifts the small
lamp squares). The other hypotheses fail by far: with `emissiveColMod` (1.5) as the tint the day
predictions would be 3.3 times too dim, and with the type scale alone (no 0.2 for 0x18 at running
lights) car 266's rolling tail would be 5 times too bright.

What the comparison does not settle: the medians and p90s depend on which pixels the
red-dominant mask keeps (unlit lens pixels in the dark are grey and drop out, so the observed
p50 is biased up there); the grade is a per-channel fit, not the real 3D LUT; bloom and film
grain are left out; 227 and the day 0x1D head lamps were not in a traced frame (the 24 dumps
ended at 266's day phase).

Sheet of the frames with the lamp boxes and numbers: attached to the PR
(`lamp-model-vs-game.jpg`, made by the worktree's `out/re/sheet.py`).

## 5. Today's tuned constants against the formulas

| Constant (where) | Today | Calibrated against | What the formulas say |
| --- | --- | --- | --- |
| `DAYTIME_GLOW` (`dsf_materials`, used by `lenses` day glow) | 0.45 (0.2 from #167 to #286) | render_compare night renders and the VW GTI's day plates | Its comment's premise is wrong: a 0x1D lamp is lit at 1 (brightness 0.1 x scale 10) with the running lights, by day and in the harness's drive-only dark alike, and at 10 only with Headlights, which free roam never sets. Vanilla's 0x1D lamps glow at `5 x white x 1 x a` by day. A converted 0x1D lamp whose alpha follows vanilla's 0x1D distribution (`DAY_HEAD_ALPHA`) needs no extra factor: the formula value is **1.0**; 0.45 makes ports' day-lit head lamps 45% of vanilla's per texel. If the white plates come back at 1.0, the cause is the lit texel colour or the lamp's area, not the glow level. Settled in #389: removed; with the plain glass over lamps kept (#379) the ports' lamps are seen through it as vanilla's are. |
| `TAIL_DARK` (`lenses`, #312) | 0.4: the dimmest 40% of a ported tail lamp's visible pixels stay dark | ported tails cover 2.5x vanilla's red area | No counterpart in the game: each texel glows `a x C`, nothing is thresholded. It is an area correction. The quantity that matches vanilla is a lamp's summed glow (sum of `a x area` over its visible texels, times its type's level) and its spread; keep it only as a geometry heuristic and say so. Kept and documented as the area heuristic (`lights.md`, "Lamp alpha per type, measured by day"). Since #403 it only sets each rear lamp's mean alpha (`TAIL_MEAN`); the glow follows Forza's mask linearly (`lights.md`, "Rear lamps glow in proportion to Forza's mask"). |
| `TAIL_RED_ALPHA` (`lenses`) | vanilla 0x18 visible alpha percentiles | 56 vanilla cars, rear view | Right in kind: same type, same `C`, so matching the alpha distribution gives vanilla's glow. Note vanilla also uses 0x03 tails (level 2 at running lights, 10 times a 0x18's 0.2): pooling those with 0x18 overstates the 0x18 target. 0x18 only, as it is; the converter keeps 0x18 tails (same section). |
| `LAMP_CURVES` / per-car red curve ("matched", `dsf_materials`) | quantile match of Forza score to vanilla alpha per hue | vanilla texels pooled over lamp types | Linear emission makes alpha matching the right lever, but per type, not per hue: vanilla red pools 0x02 (1), 0x03 (2), 0x06/07 (0.5), 0x18..0x1C (0.2 / 1). A converted lamp should match the vanilla distribution of the type it is given. Measured per type by day (same section): the types the hue curves serve glow inside vanilla's ranges; not changed. |
| `VISIBLE_RED_VANILLA_MEDIAN` 92, `_LENS_X/Y_RED`, `VISIBLE_WHITE_VANILLA_MEDIAN` 137 | texel brightness caps | vanilla visible texel RGB | Texel RGB never enters the glow; it is only the lit lens (`light x t`). These set the unlit look and are the right lever for it; the target (vanilla's distribution) has no game constant. |
| Cover shares `COVER_MIN_SHARE` 0.3, `COVER_LAMP_SECTION_SHARE` 0.05 (#247, #284) | a cover over a lamp from 30% (5% in a lamp section) of its area is left out | screenshots of hidden lamps | No counterpart. What the game does: a cover drawn as DSF window glass (blue 191) passes half of the lamp's glow (0.50 measured; 25% before the measurement) and adds grey, glint and reflection; DSF glass can't tint. Any cover part that overlaps lamp texels with alpha above 0 costs half their glow, whatever share of the cover it is. Settled in #389 per #379: plain glass over a lamp kept as DSF window glass, covers pass (1 - a)^2. |
| `car_render` lamp model (render_compare, #313) | `EMISSION` 2 over a 0x01's level 2; colours red (1, .06, .03), white (1, .95, .85), amber (1, .45, .05), xenon (.85, .92, 1); `DARK` = Headlights + DaytimeRunning; no tint, no tone map | guesses | Game: colours red (0.588, 0, 0), white (1, .958, .760), amber b2 4 (1, .229, .001), b2 5 (1, .361, 0), xenon (.876, .892, 1); tint `vehColourBoost` (5 by day, 1 in the harness dark); levels from the table above (0x1D at 1, not 10, unless Headlights are on; 0x01 at 0 in free roam); then Reinhard with W 5 and e about 1.7 by day, the grade, bloom. #313 itself is about the Forza reference's lens shading (Forza shaders, not DSF): DSF has no lens program to compare it with. Done: `lights.md`, "The lamp model the renderer and the harness use". |
| `DAYTIME_SHARE` 0.25 / `HEAD_DAYTIME` 0x1D (`lights`) | head lamps become 0x1D where their side has little position lamp | 33 vanilla cars with 0x1D | Agrees with the game: in free roam 0x01 is always dark, 0x1D glows at 1 and 0x03 at 2. The choice is the whole day look of the front. |
| Harness dark mood (`lightstest.lua darkLighting`) | `vehColourBoost` 1, `middlegrey` 0.5, `bloomThreshold` 0.15, bloom tint 0.3 | written for the harness | The "night" calibrations (#286, #290, #312) were made in it. Lamps are 5 times dimmer in linear light than in every gameplay mood; the exposure there is the offset 2 (La below 0.1). Comparisons of ports with vanilla in the same mood stay valid as ratios; absolute levels and bloom do not carry over. A dark mood with `vehColourBoost` 5 would match the game's lamp tint. Done: the harness's dark mood is now the story's LastChapter (`vehColourBoost` 5), with the running lights only (`lights.md`, same section). |

## Open

- The exact 3D colour-grade LUTs (`g_samColourGradingFrom/To`, `idColourGrading`) were not
  found in the data; `lamp_model` uses a per-channel fit. Found since: ClearDay's is a 16³ DDS
  in `media/SfxMisc.fchunk` (`paint-re.md`, "The grade"; `paint_model.game_grade` reads it).
- `F02445F9` (the luminance adaptation step) was not found as bytecode; its formula is
  inferred from inputs and constant.
- The glass blend factors were not read from the effect states (inferred `SrcAlpha /
  InvSrcAlpha`).
- No ported car and no 0x1D head lamp were in a traced frame; the next run should put the
  dumps on a ported car's front and rear (TraceStartSeconds timed to its phases).
- Which colour pass (0 or 1) the game picks at which settings was not traced; both have the
  same emission.
