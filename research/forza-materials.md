# Forza materials and textures, and their DSF counterparts

Updated: 2026-10-29

Code: `src/dsfforza/convert/forza_materials.py` (Forza side), `src/dsfforza/convert/dsf_materials.py`
(DSF side, atlas, preview), `--textures` in `src/dsfforza/convert/forza2dsf.py` and
`src/dsfforza/convert/exp_hollow.py`. Checked on NIS_370Z_10 (FH1) and the Desanne host.

## How FH1 binds textures (confirmed-static)

A carbin subsection's name is a shader technique name. All 257 car
techniques are listed in `media/shaders/Cars/shaders_v16.fxobj`; custom
ones are in `CustomCarShaders.xml` plus one `.fxobj` each. The carbin stores
no texture binding: neither the subsection header nor the rest of the file
holds texture names or indices. Every car technique samples `baseSampler`,
and the game binds that to one of two per-car atlases by technique.
`damageSampler` and `DamageLightsSampler` take the damaged variants.

| Atlas (main carbin) | lod0 / cockpit | Damaged | Contents |
| --- | --- | --- | --- |
| `nodamage.xds` 1024² DXT5 | `nodamage_LOD0` 2048² | `damage.xds` | interior, badges, emblems, colour swatches; body area white with alpha 0 |
| `Lights.xds` 512² DXT5 | `lights_LOD0` 1024² | `damagelights.xds` | every lamp, lamp glass, reflector; alpha = glow mask |

Many cars also ship colour or stripe variants of `nodamage` (`nodamage_nostripe`,
`nodamageblack`, `nodamage_white`..., each with its `_LOD0`). Two cars of the
FM4 1950-2000 pack have only variants and no plain `nodamage.xds`:
CHE_CamaroZ28_79 (`nodamage_blue/gold/orange/yellow`) and PON_TransAm_73
(`nodamagegreen/red/white` and their `...b` forms). `port_car.extract_car`
then caches the variant that sorts first, with its `_LOD0`, under the plain
names (`colour_variants`; the stamp's `variants` records which one was used).

The wheel is the exception: the main carbin's `wheel` section (the same model
as LOD 1 to 5 of the wheel archive's carbin) samples the car's own
`wheel.xds` (256² on 36 of the 40 testdrive cars) from `media/wheels/<car>.zip`
on the discs or `media/wheels/<car>/` in the disc 2 packs (`wheels.md`, "Rim
textures").

Cockpit carbins: the cabin sections (`interior`, `seatL/R`,
`steering_wheel`, needles) draw with the `cockpit_<technique>` shaders,
which sample `interior_LOD0` (`CockpitInteriorHiSampler`) for every trim,
dial and label; the outside sections (`a_pillar`, `hood*`, `mirrorL/R`) keep
the exterior techniques on `nodamage_LOD0` (`cockpit.md`, "Cabin trims sample
interior_LOD0"). `interior_emissive_LOD0` holds the lit dials.
Tiling shaders use global textures (`treadSampler`, `CarbonFiberSampler`,
`leather2_NRM`), not the atlases.

Evidence: each technique's UV triangles drawn over both atlases land on
matching artwork (tail light shape on `Lights`, Nissan logo on `nodamage`,
and so on). Paint-through areas in `nodamage` have alpha 0, and the
`damage.xds` scratches sit exactly there. Their RGB is arbitrary fill: white
on most cars, mid grey with livery lines on the 205 T16, a badge plate's grey
on the Jeep GC SRT8. The converter must not take its RGB at face value
(`cockpit.md`, "Light cockpit trims"; the cabin trims that seemed to sample
it actually read `interior_LOD0`).

**UVs:** `atlas = raw * scale + offset` with `m_UVOffsetScale` (x_off,
x_scale, y_off, y_scale). There is no Y flip. Per subsection, three kinds:

- **textured:** the footprint covers real atlas content.
- **swatch:** the footprint is under 4 texels, or the scale is 0. The
  technique takes one flat colour. black, frame, bottom, plastic2 and
  weatherstrip read near-black texels around (0.37–0.43, 0.005–0.02) of
  `nodamage`; fogred reads a texel of `Lights`.
- **tiling:** the scale is far above 1 (tire ×31, carbon_fiber ×246), so a
  global tiling texture is used.

Alpha-tested techniques (emblem, badge…) drop texels below half alpha.
The `badge` subsections are backing plates drawn entirely from alpha-0
texels, so only the emblem letters show in Forza. The converter has no alpha
test on the DSF side, so it leaves out the triangles Forza's test removes
entirely: those with fewer than 1% of the texels inside the UV triangle at
alpha 128 or more (`forza_materials.triangle_alpha_cut`). Until 2026-10-24 it
tested the triangle's UV bounding box, which kept long decal triangles whose
box reached opaque artwork beside them; see "The F-150 Raptor's black
patches" below.

**Baked ambient occlusion (confirmed-static).** The first of the trailing
vertex bytes (FH1 keeps 4, FM4 8; `carbin.Vertex.extra`) is per-vertex AO,
0 to about 107 for fully open surfaces: it correlates with the normal's up
component (0.4 to 0.9 per section) and with height, open panels (hood, roof,
glass) sit at 87 to 107, the 370Z cockpit interior has median 43 and seats
32. Some FM4 sections hold a constant there instead (Skyline glass: all 0 or
all 255), so the converter treats a section with fewer than 3 distinct values
or values above 115 as having no AO (0.8 assumed). It feeds DSF's baked sky
term in the fourth position component (`vehicle-model.md`,
`forza2dsf.vertex_sky`). Bytes 1 to 3 look like a packed tangent.

**Glass UVs.** Converted glass (window, glass, glassF/LF/RF...) uses DSF's
shared glass materials, whose texture keeps the clean glass in v 0..0.25;
the converter squashes the Forza window UVs into that band
(`forza2dsf.glass_uv`, `vehicle-model.md`).

## Mapping (NIS_370Z_10, main carbin)

| Technique | Forza atlas | Kind | Role | DSF material |
| --- | --- | --- | --- | --- |
| body, body_2 | nodamage (alpha 0) | textured | paint | shared PAINT, UV0 on the paint block of the shared paint texture (`lights.md`, "Paint") |
| window | nodamage | textured | glass | shared GLASS |
| glass, lights_glass, lights_gls_noemit (lamp covers) | Lights | textured | glass | shared GLASS, except over a lamp: plain `glass` is left out (#163), the textured covers are baked into the lamp texels (#164) (`lights.md`, "Lamp covers drawn as window glass", "Lamp glass") |
| black_glass | nodamage | textured | trim | shared TRIM |
| black, frame, bottom, plastic2, weatherstrip, matte_colors, misc, rubber_trim, bumper_frame, undercarriage, race_kit_red | nodamage | mostly swatch | trim | shared TRIM (`--swatch-trims`: Chassis with the swatch colour); textured triangles seen from outside on light artwork, dark detail or flat mid-grey: host Chassis, texels times the technique's PaintScale ("Trim artwork", "Forza's trim shaders and flat black grilles" below); with a Forza specular or reflection a gloss copy of host Lights instead ("Gloss") |
| chrome, chrome_2, chrome_3, chrome_bump, lights_chrome | nodamage (lights) | textured or swatch | chrome | shared CHROME: GLOBAL_LOD_Chrome (full), GLOBAL_Chassis_Chrome (exterior); "Chrome trim" below |
| carbon_fiber | global | tiling | trim | shared TRIM |
| interior | nodamage | textured | textured | host **Chassis** (stride 28) |
| emblem | nodamage | textured, alpha-tested | textured | host Chassis, or a gloss copy of host Lights ("Gloss"); fully transparent triangles dropped |
| badge | nodamage | alpha 0 everywhere | textured | dropped |
| taillight2S, tail_light, reverse_light, textured_reflector, indicator_left/right, hidhead, oldhead, slorange, drlwhite, detail_glass_red, detail_glass_clear | Lights | textured | textured | host **Lights** (stride 32, UV1 = UV0); `detail_glass_*` lenses are glass in Forza and get the lamp behind them baked in (`lights.md`, "Lamp lenses drawn opaque") |
| fogred | Lights | swatch | textured | host Lights, solid cell |
| tire | global | tiling | tyre | shared TYRE (unwrapped onto the vanilla tread and sidewall, `research/wheels.md`) |
| rim, chrome_rim, outer_rim, inner_rim, wheel_black, wheel_emblem (`wheel` section) | the car's `wheel.xds` | textured (inner_rim sometimes a swatch) | textured | host **Chassis**, or a gloss copy of host Lights where Forza has a specular or reflection ("Gloss"), in the wheel MODL part (`wheels.md`, "Rim textures") |
| blur_rim, blur_lip, chrome_blur_* | | | skip | dropped |

Shared TRIM (`GLOBAL_Chassis_Simple`) samples DSF's global chassis texture
(SSIC `00122b73d1fa1227`, 1024x512: engine bay, undercarriage, grilles,
plate, swatches; vanilla trims unwrap onto those). Converted trims carried
Forza's UVs into it: swatch trims hit one arbitrary texel, tiling ones
(carbon x246) spread over the whole texture. The Veyron's exposed carbon
(22 m², most of its upper body) drew bright noise ("materials messed up",
`img/fix-veyron-trim.png` top). Converted trims now all use the texture's
uniform dark block (`forza2dsf.TRIM_UV`, (0.969, 0.496)), the texel vanilla
trims drawn from a single texel use most (46 hosts). With `--damage`, the
trims a missing bonnet or boot lid exposes (bay walls and floor, panel
undersides) are projected onto the texture's engine-bay pictures instead,
and panel undersides use the paint material (`damage.md`, step 6).

`liveryBadge` (livery decal slots; of the 40 testdrive cars only the Agera
has them: a strip on the rear wing in the main carbin, one on the dash top
under the windscreen in the cockpit carbin's `hooda`) is skipped, in both
carbins. Its raw UV0 spans the whole unit square, i.e. a texture of its own
(the player's livery), and `m_UVOffsetScale` squeezes it onto an arbitrary
patch of the car atlases: a 3-texel column at the edge of the seat artwork in
`nodamage` (blue and white, half opaque), and in `interior_LOD0` (where
`classify_cockpit` sent every "badge") the yellow and blue stitching bands.
Drawn from the atlas they were the Agera's bright yellow and blue streaks on
the dashboard and the blue patches seen through the windscreen (user report
on `out/testdrive9`). After (run `vm-20261024-060602`, ID 2): a plain dash top, no wing
patches (`research/img/emissive-agera-cockpit.jpg`, `emissive-agera-wing-render.png`).

Plates: `numberplate` (a plate quad on 2 of the 40 testdrive cars) and
`numplate` (the plate lamp strip) stay trim; the converter replaces
`numberplate` quads with DSF's vanilla plate and places one on cars without
(`plates.md`).

The full technique table, with prefix fallbacks for names not seen on this
car, is `TECHNIQUES` / `classify()` in `forza_materials.py`.
`uv run dsff forza_materials <car dir> --colours` prints this per car.

## DSF side (confirmed-static; in game: `lights.md`, `in-game-tests.md`)

- The host's full entry has one chassis TEXC (Desanne: 512² DXT5, 4 mips,
  0x55080 bytes) and two local MACOs that sample it through UV0: Chassis
  (stride 28, specular 0.03) and Lights (stride 32, specular 150, reflection,
  `g_texDecalNormalMap` on a global texture). The clean exterior entry has
  its own Chassis_LOD1 and Lights_LOD1 materials, which point at the full
  entry's TEXC UID. One interior submodel uses the full entry's Chassis
  material.
- The DSF texture is plain RGBA. Alpha is a specular/glow mask: on non-lamp
  texels it tracks luminance (correlation 0.9–0.99, about
  `(lum − 45) × 0.55`); on lamps it peaks at the bulbs. The converter maps
  Forza `Lights` alpha through the vanilla-matched per-hue curves
  (`LIGHTS_ALPHA = "matched"`, `lights.md`; gamma 2 was the first mapping).
  On `nodamage` texels it uses the luminance formula and turns paint-through
  texels into the Desanne's plain texel (74, 72, 74, 15; `DEFAULT_PLAIN`,
  whatever the host).
- **Atlas:** only the parts that textured subsections use are copied. A
  recursive XY cut splits the UV triangles into rectangles along empty
  seams; L-shaped groups are split at the cut with the least area. The
  rectangles are packed with a skyline packer at the largest uniform scale
  that fits, with 4 texels of real neighbouring content around each. The
  370Z needs 13–14 regions; at 512² the scale is 0.535 (nodamage ≈ 548 px,
  Lights ≈ 274 px equivalent). Solid 8×8 cells hold swatches and the paint
  texel.
- **Install, replace mode (default):** the new DDS goes into the host
  chassis TEXC and every UID stays. The full and exterior host materials
  (and the paint overlay) pick it up, and the entry size doesn't change.
  **Clone mode:** a new TEXC plus cloned Chassis/Lights MACOs in both
  entries; this adds 0x55080 to the full entry.
- `forza2dsf.add_geometry(..., textures=binding, merge=True)`:
  `merge` makes one submodel per material instead of per (section,
  material). Exterior SBMCs drop from 53 to 6, full-entry SBMCs from 376 to
  261, and lod_max_subs from 52 to 5, all below the vanilla maxima (kind 0:
  374 SBMC, kind 2: 41).

Results for the 370Z on the hollow layout (`exp_hollow.py --complete`):

| Build | kind 0 MDXN | kind 2 MDXN | SBMC 0 / 2 |
| --- | --- | --- | --- |
| `370z-v1.sp` (no textures) | 0xDF800 | 0xC3000 | 349 / 42 |
| `370z-tex.sp` (replace) | 0xE3000 | 0xCB000 | 376 / 53 |
| `370z-tex-merged.sp` (replace, merged) | 0xDD800 | 0xC9000 | 261 / 6 |

Without `--textures` the output is byte-identical to `370z-v1.sp`.
Decoding the archives with `dsf_materials.py preview` finds every
texture. It shows textured tail lights, reverse lights and reflectors, the
Nissan emblems and the 370Z script, with no black badge plates.

## The F-150 Raptor's black patches (2026-10-24)

User report on `out/testdrive9` (ID 38): black artifacts on the Raptor's bed
sides and rear doors.

**Forza's own look (confirmed-static).** The shapes are the Raptor's
mud-splash graphic: one `stripe_badge` subsection on the `body` section (44
triangles, 0.35 m² at LOD 1), alpha-tested, coloured by the technique's
`StripeColor` (`shaders_v16.fxobj` has `StripeColor`, `StripePaintScale`;
`ShaderSettings.xml` sets `stripe_badge` PaintScale 0.1). Whether it shows
depends on the factory colour: gamedb `Combo_Colors` gives every Raptor colour
twice, sequences 1, 3, 5, 7, 9 with `BaseTexture` `nodamagestripe.tga` and
`Combo_Colors_Exceptions` HasStripe 1, ColorStripe 0x232323, and sequences 2,
4, 6, 8, 10 with `nodamage.tga`. The car zip has both `nodamage.xds` and
`nodamagestripe.xds`; the converter reads `nodamage.xds`, whose alpha is 0
under every stripe triangle (one stray texel above 127 in 280 at most), so
with that texture Forza draws no graphic. 21 FH1 cars have per-colour base
textures; among the testdrive cars also the Challenger SRT8 392, 500 Abarth
and Agera, and the Cuda and Charger Daytona have stripe colours on some
factory colours. DSF has one chassis texture per car for every palette colour,
so the converter bakes `nodamage.xds` (the colours whose `BaseTexture` is
`nodamage.tga`) for all of them.

**What the converter did.** Its alpha test took the largest alpha in each
triangle's UV bounding box. The stripe's island sits in `nodamage` beside the
opaque cabin artwork, so the boxes of its long triangles reached alpha-255
texels: 44 of its triangles went on the Chassis material and drew the dark
stripe texels (RGB about 40) as random fragments of the graphic. In game
(user's shot): jagged black patches on the bed and the rear door bottoms.

**Fix.** `triangle_alpha_cut` looks at the texels inside the triangle. Survey
over the 40 testdrive cars (`out/rims/alpha_cut_survey.txt`, main carbin at
LOD 1/3/4/5 and the cockpit carbin): the Raptor's stripe loses 44/27/26
triangles at LOD 1/3/4 (0.35 to 0.50 m²), the Agera's `liveryBadge` on its
wing 22/9/8/5 (0.03 to 0.04 m²; `liveryBadge` is now left out as a whole,
PR #10), the 2000GT's body `badge` 8/6/6 (up to
0.03 m²), the Mustang Boss 429's bonnet `badge` 4/1/1 (and 14 in the
cockpit's bonnet, 0.02 m²); every other change is under 30 cm² (slivers at
emblem edges): 345 LOD 1 triangles and 136 cockpit triangles in all, none
kept that were cut before. In game (VM run `vm-20261024-064427`, fixed
camera on the Raptor's left side and front wheel): plain paint on the bed and
doors, no patches (`img/raptor-ingame-before-after.jpg`, offline
`img/raptor-stripe-before-after.png`).

## Badge plates (2026-10-24)

User report on `out/testdrive11` (ID 18, PEU_205T16_84): "red streak on its
back", a red bar on the upper right of the tailgate.

**What it is (confirmed-static).** The `trunk` section's `badge` subsection
(24 triangles at every LOD): the "205 turbo 16" lettering. Its texels in
`nodamage.xds` (u 0.133 to 0.183, v 0.651 to 0.995) are one flat red in RGB,
alpha 255 on the letters and 0 between them; Forza's alpha test leaves only
the letters (`out/inv/205_badge_crop.png`, `out/inv/frender.py` rear view with
the test: red lettering at the same place). The converter put the quad on the
Chassis material, which has no alpha test, so the whole quad showed.
`triangle_alpha_cut` only drops triangles the test removes entirely.

**Survey (40 testdrive cars, LOD 1, `out/inv/badge_survey.py`).** Area the
alpha test hides on triangles the converter kept: 0 to 14 cm² on cars whose
decals are modelled as geometry (`emblem` triangles, soft edges), 100 to 860
cm² on cars with badge quads: 2000GT 858, Mustang Boss 429 711, DMC-12 474,
S5S 438, Huayra 434, Firebird 381, Jeep 336, Durango 228, Civic Type R 216
(side skirt decals), 205 T16 201, Express 198, Raptor 232, Skyline 169, Focus
141 (seat badges), Range Rover 84. The hidden texels drew in their RGB: the
205's red, the Firebird's cream, white alpha-0 texels as the plain dark grey
texel (`_paint_through`).

**Fix (`src/dsfforza/convert/cutout.py`, `forza2dsf.convert`).** Before the
atlas is planned, every kept alpha-tested triangle whose texel centres the
test hides by 20% or more (and at least 8 texels) is replaced by triangles
over the texels that pass: the mask of passing texels within 2 texels of the
triangle, its marching-squares outlines simplified (Douglas-Peucker, 0.75
texel at LOD 1, 1.5 at LOD 3, 3 at LOD 5), ear-clipped with holes bridged,
then clipped to the source triangle so neighbouring triangles' pieces meet on
their shared edge. New vertices are the source triangle's barycentric blend
(position, UVs, normal, AO byte); winding is kept. Outlines that cross
themselves after simplification are tried with less of it, then as rectangles
(no car of the 40 needed that). Over the 40 cars (`out/inv/cut_survey.txt`,
LODs 1, 3, 5): 2384 triangles become 63119, 0.06 to 0.18 m² of hidden area
removed per car, 0.5 to 3.3 s per car. Offline render
(`out/inv/badges_pr.png`, `img/badges-before-after.png`): the 205's red bar
is its lettering again, the Firebird's "TRANS AM" loses its cream plate, the
DMC-12's rear grille strip its grey plate.

In game (confirmed-runtime, headless VM, chase and new `rear` probe views):
`vm-20261024-110508` (`out/testdrive11`, IDs 18, 9, 37) against
`vm-20261024-105751` (`out/fx/b2`, IDs 1 to 3): the 205's red bar is the red
"205 turbo 16" lettering, the Firebird's "TRANS AM" has no plate
(`img/badges-ingame-before-after.jpg`). Full entries grow by the cut
geometry (205: 0x10b000 to 0x129800 MDXN, Firebird 0xf6000 to 0x11c800),
well under the 0x2C0000 limit. All 40 testdrive cars (`out/fx/td`, `--mdxn-limit
0x2C0000 --damage`): built, 39 lint ok; the Express fails lint as in
`out/testdrive11`. Full-entry MDXN grows by 45 KB on average, 170 KB at most;
the largest is 0x1e0000.

## Chrome trim and the Boss 429's black parts (2026-10-24)

User report on `out/testdrive11` (ID 37, FOR_MustangBOSS429_70, host 193):
"its hood is missing color in chase cam but not in cockpit? actually most of
its body details are plain black". Chase view: flat black bonnet scoop, rear
spoiler and rear-window louvres, a dark lower body. Cockpit view: a
body-colour scoop with reflections.

**What Forza says, part by part (confirmed-static; `out/inv/texels.py`,
`where.py`).** All three named parts are `matte_colors` subsections on
opaque near-black texels of `nodamage.xds` (18, 17, 18, alpha 255):
`hooda` 152 triangles (the scoop, x ±0.28, z -1.98..-1.33; the cockpit
carbin's `hooda` has the same technique on the same texel, 722 triangles),
`winga` 550 (the spoiler), `body` 596 (the louvres over the rear window).
The car's `ShaderSettings.xml` gives `matte_colors` SpecularAmount 1 and
SpecularPower 5: a broad matte sheen, not paint. So Forza's vanilla Boss 429
has a black scoop, spoiler and louvres, as the 1970 car did (black scoop on
1970 cars, body colour on 1969 ones; louvres and spoiler black). The
converter's flat dark trim matches them; DSF's own black trims look the same
(`GLOBAL_Chassis_Simple`: specular intensity 0.01, power 7.5).

**The cockpit's red scoop: upgrade bonnets.** The cockpit carbin holds every
bonnet the car can wear: `hooda` (vanilla) plus `hoodCIrace` (a body-colour
cowl-induction box, 4,768 triangles) and `hoodPSrace` (6,350), and
`cockpit.fit` kept every section but `cagerace`, so all three were drawn on
top of each other. The race box sits higher and hid the vanilla scoop. The
outside never had this: `forza2dsf.vanilla_sections` keeps variant `a` and
drops `<base>race`. Fix: `cockpit.fit` applies the same rule to the cockpit
carbin. Survey (`out/inv/trim_survey.py`, every cached car, 572 folders):
112 cockpit carbins carry extra bonnets (`hoodb` to `hoodq`, 39
`hoodPSrace`, 35 `hoodCIrace`), 11 of the 40 testdrive cars (Mito, Cuda,
500 Abarth, Boss 429, Civic Type R, S2000, RX-7, Silvia, Skyline, Firebird,
Supra). Interior entries shrink (Boss 0x1a1000 to 0x199800 MDXN, Cuda
0x1c5000 to 0x1a8800, Firebird 0x193800 to 0x184000).

**Chrome drawn as black trim (the "body details").** Forza's `chrome`
technique (and `chrome_2`, `chrome_3`, `chrome_bump`, `lights_chrome`) had
role trim, so the Boss's chrome bumpers, window frames, drip rails, door
handles and tail-lamp bezels (1.85 m², 4,583 exterior vertices) drew with
the shared trim's dark texel: black. Vanilla cars draw chrome with DSF's shared
chrome materials (`out/inv/vanilla_mats.py` over all 150 vanilla vehicles):

| Entry | Material | Vehicles | Triangles | Layout |
| --- | --- | --- | --- | --- |
| full (kind 0) | `GLOBAL_LOD_Chrome` `a012f01306d57a2a` | 96 | 374k | stride 32, MODL flag 5 |
| full (kind 0) | `GLOBAL_Chassis_Chrome` `00128338d7fd1027` | 9 | 21k | stride 32, flag 5 |
| exterior (kind 2) | `GLOBAL_Chassis_Chrome` | 148 | 322k | stride 32, flag 5 |
| exterior (kind 2) | `GLOBAL_LOD_Chrome` | 8 | 12k | stride 32, flag 5 |

Both sample the shared chassis texture `00122b73d1fa1227` (reflection
amount 1, specular power 150; `GLOBAL_LOD_Chrome` adds a static cube map).
Vanilla chrome UV0 lands mostly on texels 972-980 x 404-424 (grey level about
98); UV1 is the damage-decal UV as on paint (mostly the corner texel). Host
193, the Boss's host, has 3,074 chrome vertices in its exterior; the port had
none.

Fix: a `chrome` role (`forza_materials.CHROME`, exact names above, plus the
prefix `chrome*` after the blur and badge rules), mapped to
`GLOBAL_LOD_Chrome` in the full entry and `GLOBAL_Chassis_Chrome` in the
exterior (`forza2dsf.SHARED`, `SHARED_EXTERIOR`), UV0 `forza2dsf.CHROME_UV`
(0.9536, 0.8135), UV1 Forza's (`damage.DamagePlan` replaces it with the
host's, as on paint). The cockpit keeps drawing chrome from its atlas
(`classify_cockpit` treats it as trim); `damage.split_exposed` leaves chrome
whole. Survey: Forza chrome on 571 of the 572 cached cars (478 with 0.1 m² or
more; median 0.26 m², at most 14.8 m²), all 40 testdrive cars (0.035 to
2.57 m²: 300SL, Charger Daytona, Boss 429, 2000GT, 300C at the top).

What Forza's chrome shader does with the texel colour is unknown (the
`.fxobj` isn't decoded): 4% of the chrome area of the 40 cars samples dark
opaque texels (370Z exhaust, Express mirrors, IS F front bumper strip,
Charger Daytona mirrors). They are chrome now too.

**The dark lower body (confirmed-static, and in game).** Not a baked-light
bug: the sky term (s0.w) of the Boss's paint matches its vanilla host's
(`out/inv/sky_cmp.py`, exterior paint by facing and height: side 0.44 to 0.54
against 0.39 to 0.54, downward-sloping 0.30 against 0.18 to 0.30). In the
same fixed side camera the vanilla 193 (dark green) shades its lower doors as
much (`img/lower-body-port-vs-vanilla.jpg`); the user's port had a dark red.

In game (confirmed-runtime, headless VM, the same 4 cars built from master
and from the fix: `out/fx/before`, `out/fx/after`, IDs 1 to 4 plus vanilla
193): runs `vm-20261024-133045` (before) and `vm-20261024-134146` (after).
The Boss's rear bumper, tail-lamp bezels, rear-window and side-window frames,
drip rail and door handle are chrome; the scoop, spoiler and louvres stay
black (`img/chrome-boss429-ingame-before-after.jpg`). From the cockpit the
Boss shows its black vanilla scoop instead of the race box, the Firebird its
shaker scoop instead of a box filling the windscreen
(`img/cockpit-bonnets-before-after.jpg`). The paint colour differs between
the runs: the spawn picks a palette entry. One "after" run hit DriverNGHook's
fatal error dialog while the world loaded (`vm-20261024-133536`), before any
port spawned; the rerun passed.

## Trim artwork (2026-10-25)

User report on `out/final-gui2` (ALF_GTA_65, spool ID 53, host 126): "alfa
romeo giulia sprint gta stradale has black triangles instead of decals". On
each front wing behind the wheel arch there was a solid black triangle; the
real car has a white triangle with the green quadrifoglio there.

**What it is (confirmed-static; `out/inv/near.py`, `badge2.py`).** Not the
badge cutout: the wing triangle is three `misc` triangles of the `body`
section (x ±0.79, y 0.36 to 0.44, z -0.84 to -0.81 at LOD 1), and a fourth
group of nine on the tail panel (x 0.24, z 1.93). Their texels in
`nodamage.xds` (u 874 to 996, v 404 to 504) are opaque (alpha 255 on every
texel centre inside them): white (255, 255, 255) at the top and the green
cloverleaf on white below (mean 160, 213, 175). `misc` is a trim technique
(`forza_materials.TECHNIQUES`), and every trim drew with the shared trim
material on its dark texel (`forza2dsf.TRIM_UV`, RG about 24), so the
quadrifoglio came out as a black triangle. The `body/badge` cutout record
(cut 64, made 1676) is the dark plate at the bonnet's rear edge and four
round pieces in the nose; those cuts were right. `shaders_v16.fxobj` names
`misc` next to `engine`, `cage`, `rubber_trim`, `weatherstrip`, `black` and
`frame`; what its pixel shader does with the texel isn't decoded.

**Survey (every car of the roster, 573, `out/inv/trim_survey.py`, LOD 1, all
sections).** Trim triangles whose texels are opaque (half of them alpha 128
or more) and whose mean luminance is 90 or more: 294,408 triangles on 566
cars. Most of that can't be seen: the underbody (`undercarriage`, 189 m²),
frames under the bonnet and in the boot (the Lancer Evo VI: 1.3 m² of
`frame`), race-kit variants the converter drops. A quick orthographic render
of each car's vanilla sections with the Forza texels on every trim
(`out/inv/frender.py`) against the converter's trims shows the visible cases:
light artwork drawn black. Besides the GTA's badges: the Crossfire's silver
side-vent strakes, A-pillar trim and door handles, the Veyron's aluminium roof
scoops, engine cover and fuel cap, the Monte Carlo SS vanilla car's rear spoiler,
the Kenworth T440's red box frame.

**Fix (`src/dsfforza/convert/trim_art.py`, `forza2dsf.convert`).** Before the
atlas is planned, `split_trim_art` moves trim triangles into their own
subsection with technique `dsf_trim_art` (`dsf_trim_art_lights` on the Lights
atlas), which `forza_materials` classifies as textured, so they draw through
the atlas on the host Chassis material like badges. A triangle moves when:

- at least half the texel centres inside its UV triangle (its corners and
  centroid if it holds none) are opaque, and the brightest channel of their
  mean colour is 128 or more. Alpha-0 texels are paint-through filler, not
  trim colour. A first try at 72 also moved mid-grey plastics (the Volt's and
  the DMC-12's bumpers, 73 to 90), which Forza's render shows as dark grey
  and DSF draws as its dark trim; those stay trim;
- it can be seen from outside the car: orthographic depth buffers of the
  LOD's body (2 cm pixels, glass doesn't hide anything) from 8 directions
  level with the car, 8 from 35° above and one from straight above;
  a corner pulled a quarter of the way in or the centroid within 2 cm of what
  its pixel shows counts. Hidden frames stay trim: no atlas room for them,
  and `damage.split_exposed` keeps projecting the engine bay when the bonnet
  comes off;
- its technique isn't `black*`, `mirror*`, a plate (`numberplate` quads are
  replaced by name in `plates.apply`, `numplate` is the plate lamp strip),
  `undercarriage`, `engine`, `cage` or `bottom`.

**Bare metal to chrome (review of the first version).** On the atlas path the
Veyron's roof scoops and engine cover came out near flat white in game
(`vm-20261025-075859`, 210 to 244 on screen for a texel of 177). That is the
Chassis material's ordinary gain, not a missing term: the moved triangles keep
their section's vertices, so their baked sky (s0.w, `forza2dsf.vertex_sky`
from the Forza AO byte) is computed like every other vertex's, and the Veyron's
own rims (wheel.xds through the same Chassis material, texels about 191) show
210 to 227 in the same shot. What the Chassis material lacks is a metal look:
specular 0.03, no reflection. Forza draws these parts as flat grey swatches
under a shiny technique (the Veyron's `ShaderSettings.xml`: `misc`
SpecularAmount 1.1, FresnelIndex 2.3, PaintScale 0.15; `frame` SpecularAmount
0.6, PristinePaintScale 0.3). So artwork that is bare metal goes to DSF's
shared chrome like Forza's chrome techniques ("Chrome trim" above; technique
`dsf_trim_metal`, role chrome, `GLOBAL_LOD_Chrome` / `GLOBAL_Chassis_Chrome`,
`CHROME_UV`). The rule (`trim_art.is_metal`): the technique is named for
metal (`metal`, `alum`, `steel`), or its opaque texels are one flat neutral
grey: chroma of the mean colour 24 or less, luminance spread (standard
deviation) 12 or less, brightest channel under 240. Painted artwork has colour
(the quadrifoglio), detail (lettering, the Veyron's engine-cover script) or is
white, and stays on the atlas. DSF's chrome samples the shared chassis texture,
so the Forza grey can't tint it without a material of our own.

The atlas pays for the new regions. `Texturing.plan` lays the atlas out with
and without the artwork; if the artwork leaves less than 90% of the scale
(`dsf_materials.ART_MIN_SCALE_SHARE`), each artwork subsection takes one flat
colour cell instead, the mean of its opaque texels. Without that, 60 tiny
`misc` triangles of the Jeep GC SRT8's headlamps (a 365 x 15 texel strip)
took its atlas from 0.479 to 0.324, the Lotus 2-Eleven's from 0.562 to 0.320.

Results over the roster (`out/inv/atlas_cmp.py`, LODs 1, 3, 5, 512² as the
build): 390 of 573 cars get trim artwork, 114,531 triangles and 60.3 m² at
LOD 1 (median 0.037 m² a car; `misc` 74,272 triangles on 234 cars,
`matte_colors` 20,484 on 154, `plastic2` 13,925 on 73, `frame` 2,048 on 83,
`rubber_trim`, `bumper_frame`, `weatherstrip` and a few others the rest).
74,618 of those triangles (304 cars) are bare metal and take the chrome, the
other 39,913 the atlas. The atlas scale drops on 75 cars, by 2.6% in the
median and 10% at most (Veyron 0.584 to 0.527, the Viper CC, Corvette Stingray
67, F40 Competizione); 8 pack slightly better. Before the metal rule 15 cars
needed the flat fallback (Jeep GC SRT8, 2-Eleven, CCXR, Range Rover 08, 370Z,
Cooper S, Silvia, E-Type...); with it, 12 of them still do. The split costs
about 3.4 s a car (16 s at most, the Kenworth). The GTA: 15 triangles,
0.033 m² of `misc` at LOD 1 on the atlas, and at LOD 3 20 light `rubber_trim`
triangles around the rear window on the chrome; atlas 0.527 as before. GMC_YukonXL_01 has a trim
with non-finite UVs; it samples no texels (it would have stopped the
conversion).

Offline (`img/trim-art-gta-offline.png`, `img/trim-art-crossfire-offline.png`;
rows: Forza's texels on every trim, master, this change): the GTA's wing and
tail triangles show the quadrifoglio, the Crossfire's strakes, A-pillar and
door handles are chrome (drawn in a flat light grey offline). The built full entry (`dsf_materials preview`
of `out/fx/after`, ID 1) has the quadrifoglio on the Chassis material.
Rebuilding the four test cars with the final code gave byte-identical chunks.

In game (confirmed-runtime, headless VM). First version (atlas only):
`vm-20261025-075152` runs the four cars' chunks from `out/final` (the
installed build, remerged as IDs 1 to 4 with `port_batch remerge --id-policy
garage`), `vm-20261025-075859` the same four built with the change
(`out/fx/after`). The GTA's quadrifoglio showed; the Veyron's scoops came out
flat near white (the review above). Final version, the same palette entry in
both runs (`run-series.ps1 -Palette 0`, new: clonetest.lua passes it to
`Spawn.Spawn` as `shaderParams[0]`, as the game's grid spawns do; the probe
now also holds the car still during the fixed views, after the Crossfire drove
out of them), probe IDs 1 to 4 plus vanilla 126 (the GTA's host, the TZ2) with
the `left`, `wheels` and `rear` views: before `vm-20261025-091951`
(`out/fx/before`), after `vm-20261026-113128` (`out/fx/after3`). The GTA's
wing and tail triangles are white with the green cloverleaf instead of black
(`img/trim-art-gta-ingame-before-after.jpg`). The Veyron's roof scoops, door
handles and fuel cap and the Crossfire's vent strakes are chrome instead of
black: shaded, with the chrome's reflections (scoop on screen p10/p50/p90
65/210/221, against 104/233/239 on the atlas path), the engine detail between
the scoops stays on the atlas (`img/trim-art-veyron-ingame-before-after.jpg`).

## Forza's trim shaders and flat black grilles (2026-10-26)

User report on `out/final4` (MRC_CougarEliminator_70, FM4, spool 565, host
175): "some parts still pure black". The bonnet scoop and the grille drew as
flat, featureless black. The real 1970 Eliminator has a black scoop and a
blacked-out grille, so the question was how Forza draws them.

**Which parts (confirmed-static; `out/inv/region.py`, `uvmap.py`).** The
scoop is `hooda/badge` (54 triangles, 0.39 m², a black block of
`nodamage.xds` whose alpha fades from 255 at the front to 0 at the back: a
decal blended over the paint), `hooda/misc` on texels (2, 4, 2) and
`hooda/plastic2` underneath. The grille is `hooda/misc` (the centre, a
grille picture with bars of 60 to 120), `headlightLdown/misc` and
`headlightLup/plastic2` (the hidden-headlamp doors, closed and open: vertical
bars of 40 to 90 on black), `body/plastic2` (the end pieces) and `body/frame`
(the slats, modelled, on one flat texel of 12). Every grille trim drew with
the shared trim on its one dark texel (`TRIM_UV`, R 24), so the bars were
gone.

**Forza's trim shaders (confirmed-static; `dsff fxobj`,
`src/dsfforza/formats/fxobj.py`).** Each shader in `shaders_v16.fxobj` is an
Xbox 360 shader container with a big-endian D3D constant table (names,
registers, defaults) and Xenos microcode, which `fxobj.py` disassembles. Of
240 pixel shaders, 113 have a `PaintScale` constant (default 1.0 in all), 95
of them without `PaintColor`: the trim shaders. In those (for example the one at
0x86914), the base texel (tf0, lerped with the damage texture) is multiplied by
`PaintScale` before the lighting:

```text
tfetch r6.xyz_, r0.xzz, tf0              // baseSampler
mul    r6.xyz_, r6.xyzz, c159{PaintScale}.xxxx
mul    r1.xyzw, r6.xyzz, r1.xyzw         // times the diffuse light
```

then add a specular term (`SpecularAmount`, default 4, times the directional
light; `SpecularPower` default 128) and a cube-map reflection mixed in by a
Fresnel term (`FresnelIndex` 1.65, `FresnelScalar` 1), and write
`sqrt(colour)` (gamma 2). The paint shaders (`PaintColor`, two-tone and metal
terms; the body's, 0x7a3e8) premultiply the texel by its alpha and add
`PaintColor x PaintScale` (lerped with the stripe's) where the alpha is low:
`base = texel x a + paint x PaintScale x (1 - a)`; the variants with
`PristinePaintScale` (0x85a3c) multiply the texel by it first. So `PaintScale`
never mixes the paint into an opaque trim (#58, case 3): in the trim shaders it
darkens the texel, in the paint shaders it scales the paint behind low-alpha
texels.

**Which shader a technique has (confirmed-static).** The effect's technique
tables aren't decoded, but every technique's parameters (and their defaults)
are listed in the shared `media/cars/Shared.zip` `ShaderSettings/Normal.xml`
(286 techniques; FH1 and FM4 alike for the car techniques; the car's own
`ShaderSettings.xml` overrides them). 34 have `PaintColor`, the paint shaders:
`misc`, `plastic`, `plastic2`, `matte`, `matte_colors`, `weatherstrip`,
`emblem`, `engine`, `cage`, `race_kit_red`, the bodies...
(`forza_materials.PAINT_TECHNIQUES`). 57 have `PaintScale` without
`PaintColor`, the texel-scaling ones: the rims (#109), `badge` (0.5),
`smooth_metal`, `rubber`, the lamps. `frame`, `black`, `rubber_trim` and
`bumper_frame` have neither. So the trims that matter here (`misc` 0.25,
`plastic2` 0.75 in Normal.xml; the Cougar's `misc` 0.2) are paint shaders whose
`PaintScale` doesn't touch their opaque texels. `forza_materials.texel_scale`:
`PristinePaintScale` for the paint shaders and the techniques without a scale
(the cars set it on `plastic2`, `frame`, `weatherstrip`: the Veyron's `frame`
0.3), `PaintScale` for the others, 1 otherwise. (A first version of this change
applied `PaintScale` to `misc`, which drew the Cougar's grille at a fifth of its
light.)

So Forza draws the Cougar's scoop black (texels 2 to 7) with a satin sheen
(`misc` SpecularAmount 0.5, SpecularPower 5, FresnelScalar 0.2) and its grille
with its bars at full texel (centre grille 60 to 120, the doors 40 to 90). Two
things make ours dead flat: the bars are gone (one dark texel), and DSF's
materials have almost no specular (shared trim `GLOBAL_Chassis_Simple`
intensity 0.01 x tex.G, host Chassis 0.03 at power 24). Only the first is fixed
here; the second is issue #87 (fixed by #143, "Gloss" below).

**Fix (`trim_art.classify_texels`, `split_trim_art`).** Every textured trim
triangle seen from outside (unchanged test) is classified on its texels after
the technique's texel scale (`fm.scale_texels`: linear light):

- bare metal (`is_metal`, judged on the texels as painted) to the shared
  chrome, as before;
- light artwork (brightest channel of the mean 128 or more) to the atlas, as
  before;
- **detail** (new): around the triangle's texels the atlas has contrast. Per
  texel, `local_contrast` takes the largest and smallest luminance of the
  opaque texels in a 5 x 5 window; the median texel's maximum is 40 or more and
  lies 20 or more above its minimum. A window, because grilles are drawn by
  triangles too small to hold a bar each (most of the Cougar's door triangles
  cover four texel samples). These go to the atlas (`dsf_trim_detail`);
- **grey** (new): a flat colour whose brightest channel is 48 or more (the
  Volt's and DMC-12's mid-grey bumpers, #58 case 1): one colour cell
  (`dsf_trim_grey`, the scaled mean of its texels);
- the rest (flat and near black) stays on the shared trim, which looks the
  same.

Textured classes are named `<technique>@<scale>` when the technique scales
its texels; `Texturing` then samples them from the scaled atlas, a family of
its own (`fm.scaled_family`, "nodamage*0.2": RGB scaled in linear light,
alpha kept). The atlas budget (`ART_MIN_SCALE_SHARE`, 90% of the scale without
any trim artwork) flattens the detail subsections first (one colour each),
then, if that isn't enough, the artwork too.

**Survey (all 573 roster cars, `out/inv/survey.py`, LODs 1, 3 and 5, atlas
512² as the build; master's split against this one).** 553 of 573 cars
change: 201,065 triangles at LOD 1 move off the shared trim (median 195 a car):
129,267 as detail on 544 cars, 71,766 as grey on 397, and 32 artwork triangles
on one car are drawn at its `PristinePaintScale`. The atlas scale drops on 190
cars, by 3.1% in the median and 10% at most (LEX_IS300_03 0.627 to 0.565, the
budget's limit); 13 pack slightly better. On 97 cars the detail is drawn flat
(one colour per subsection) to keep the budget; their artwork stays on the
atlas. The survey takes about 15 s a car for both splits and both plans (the
converter's split alone: 3 to 5 s). The Cougar: 535 triangles (501 detail: the
centre grille, the closed and open headlamp doors, the end pieces; 34 grey),
atlas 0.639 to 0.660. The Range Rover 12: the mesh between its grille bars
(the bars were chrome on master); the Cuda's grille inserts and fender gills;
the H1 Alpha's bonnet vent and its lettering.

Offline (`out/inv/frender2.py`: the car's vanilla sections at LOD 1 through
`car_render`'s shading; rows Forza's texels times the texel scale, master's
split (light artwork and metal), this change): the Cougar's centre grille
slats, headlamp-door bars and end pieces, the Range Rover's grille mesh, the
Cuda's grille inserts and fender gills, the H1 Alpha's bonnet vent
(`img/black-trims-offline.jpg`).

In game (confirmed-runtime, headless VM 2, the same palette entry,
`run-series.ps1 -Views nose,front -Palette 0 -ShotSeconds 2 -ZoomPresses 8
-WaitSeconds 600`, with a front three-quarter camera at bonnet height that is
`grille` in clonetest.lua now (master's own `nose`, added since, is closer); IDs 1 Cuda 426, 2 Range Rover 12, 3
Volt, 4 Cougar Eliminator, plus vanilla 175, the Cougar's host): before
`vm2-20261027-083402` (`out/fx/before`, built from master 4f5057c), after
`vm2-20261027-084411` (`out/fx/after`, this branch). The Cougar's centre grille
shows its slats instead of one black block; its headlamp doors stay dark, their
bars only a shade lighter at the in-game distance. The Range Rover looks the
same at that distance (its bars were already chrome; the mesh is a few atlas
texels). The scoop is unchanged: black without a highlight, issue #87 (since: "Gloss")
(`img/black-trims-ingame.jpg`). The after run ran out of time before IDs 1 and 3;
all four cars spooled and drove in the before run and IDs 175, 4 and 2 in the
after run, no crash.

Not changed: the scoop's `badge` decal is cut at alpha 128 (`cutout`) where
Forza blends it into the paint; the hidden-headlamp doors are drawn open and
closed at once (both vanilla sections; issue #88, fixed since by #123: only the
lowered state is drawn). The grey colour cells were among
the subsections that rebuilt sections lost until #177 (#173; "Gloss", after the
calibration runs); the detail grilles above are textured and were not affected.

## Gloss: Forza's specular and reflection on trims and rims (2026-10-27)

Issues #87 (the Cougar Eliminator's satin-black scoop, grille and headlamp doors
draw dead flat) and #108 (chrome rims look like flat grey paint).

**What Forza adds (confirmed-static).** Besides the texel, the car shaders add
`SpecularAmount x SpecularColor x light x (R.L)^SpecularPower` (Phong; see "Gloss
power and SpecularColor" below; first read as `(N.H)^p`) and a cube-map reflection,
`CubeScale x FresnelScalar x Fresnel(FresnelIndex)` in the paint and trim
shaders, `CubeScale` alone at every angle in the chrome ones (whose texel is
scaled by `ChromeDiffuseScale`). The defaults per technique are in the shared
`media/cars/Shared.zip` `ShaderSettings/Normal.xml` (the car techniques; the
file of the same name under `ShaderSettings/Tire/` holds other values for the
wheel archives' own carbins), FH1 and FM4 alike; a technique the file gives
none of them to has neither (`black`, `wheel_black`, `grille1`). The ones that
matter here (`forza_materials.GLOSS_DEFAULTS`):

| Technique | SpecularAmount | SpecularPower | FresnelScalar | CubeScale |
| --- | --- | --- | --- | --- |
| `misc` | 0.2 | 20 | 0.1 | |
| `plastic2` | 2 | 8 | | |
| `plastic` | 1 | 120 | | |
| `matte_colors` | 0.75 | 20 | | |
| `rubber_trim` | 1 | 10 | | |
| `weatherstrip` | 0.3 | 5.5 | 0 | |
| `bumper_frame` | 4 | 128 | | |
| `badge` | 4 | 128 | 1 | |
| `emblem` | 0 | 75 | 1 | 0.8 |
| `frame`, `undercarriage` | 0 | 128 | 0 | |
| `rim` | 0 | 1.5 | 1 | 1.5 |
| `inner_rim` | 4 | 128 | 0 | 1.5 |
| `outer_rim`, `rim_V2` | 4 | 128 | 1 | 1.5 |
| `chrome_rim` (chrome) | | | | 1, ChromeDiffuseScale 0.5 |

Cars override them: the Cougar's `misc` SpecularAmount 0.5, SpecularPower 5,
FresnelScalar 0.2 (a broad satin sheen; its scoop texels are 2 to 7), the Boss
429's `matte_colors` 1 / 5 and `plastic2` 12 / 14, the Focus RS500's `rim`
SpecularAmount 1, FresnelIndex 3. So Forza's black trims are mostly a broad
highlight with little reflection (FresnelScalar 0.1 to 0.2 where there is one;
the paint's is 0.9), and its rims are mostly reflection.

**What DSF offers (confirmed-static; `out/inv/ssic_mats.py`, `local_mats.py`,
`gpdis2.py`: the materials of the vanilla archive and their pixel shaders, SSIC
GPCH programs disassembled with d3dcompiler).**

| Material | Where | Pixel shader (per texel `tex`) |
| --- | --- | --- |
| `GLOBAL_Chassis_Simple(_LOD1)` | shared trim | `light x tex.R + 0.01 x tex.G x (R.V)^7.5`; no reflection |
| `GLOBAL_LOD_Chrome` / `GLOBAL_Chassis_Chrome` | shared chrome | `light x tex.R + tex.G x (R.V)^150 + env x tex.G x 1`; grey only, no Fresnel |
| `GLOBAL_Tyre(_LOD1)` | tyres | `light x tex.R + tex.G x (R.V)^30 + env x tex.G x 0.5 x max(0.2, 1 - N.V)` |
| `<car>_Chassis` (stride 28) | 143 vanilla full entries, `_LOD1` in exteriors | `light x tex.rgb + 0.03 x tex.a x (R.V)^24`; no reflection |
| `<car>_Lights` (stride 32) | 148 + 149 | `light x tex.rgb + 1 x (R.V)^150 + env x tex.G x 1 x max(min, (1 - N.V)^1 - 0.5) + tex.a x lamp colour` |

The shared materials sample the shared 1024 x 512 chassis texture
(`00122b73d1fa1227`), R the grey level, G the gloss. Vanilla cars draw their
trims with the shared trim (148 vehicles), 2.7% of its area on a gloss-black
swatch of that texture (R 20, G 252; 90 vehicles: specular 0.01, still next to
none), and their rim faces with their own Chassis material on their chassis
texture (109 of the 140 four-wheeled vehicles' wheel parts, recounted in
`wheels.md`; texel alpha median 21: no reflection either). No vanilla material gives a dark texel a reflection except the chrome
ones, which draw the R channel as grey and reflect alike at every angle. The
Lights material is the only per-car one with a reflection and an unmasked
specular; its glow is `tex.a` times the colour of the vertex's lamp (semantic 9
byte 2; 0, no lamp: no glow), so trims and rims can use it.

**Fix (`dsf_materials.GlossMaterial`, `gloss_material`, `Texturing._plan_gloss`,
`install_full`/`install_exterior` `gloss=`; `forza_materials.gloss_params`;
`trim_art` GLOSS).** Each entry (full and clean exterior) gets copies of its host's
Lights material with their own parameters (`add_gloss`: UIDs from the car's seed,
next to the host's materials), at most `GLOSS_MAX_MATERIALS` per car; the atlas
subsections whose Forza technique has a specular or a reflection draw with them
instead of the Chassis material:

- flat near-black trims seen from outside (`trim_art.GLOSS_TECHNIQUE`, swatch
  subsections too), which were on the shared trim's one dark texel: one colour
  cell each, Forza's colour (times its texel scale), on a gloss material;
- the trims already on the atlas (detail, grey, artwork: their names now carry the
  Forza technique, `dsf_trim_detail@0.2~misc`), badges and emblems;
- the rims (`wheel.xds`); `chrome_rim`'s texel takes its `ChromeDiffuseScale`.

The parameters (`gloss_material`, as the code has them now, after the calibration
below and #176): specular intensity `GLOSS_SPECULAR_SCALE` (0.01) `x SpecularAmount x
SpecularColor`, at most `GLOSS_INTENSITY_MAX` (0.0625); power `SpecularPower`
(`GLOSS_POWER_RATIO` 1; #143 divided it by 4, taking Forza's for `(N.H)^p`),
clamped to 4..150; below `GLOSS_MIN` (0.005) no specular. Reflection with Schlick's
Fresnel (power 5, bias F0 of FresnelIndex; chrome: power 16, bias 1, alike at every
angle) of amount `GLOSS_REFLECTION_SCALE` (0.5) `x CubeScale x FresnelScalar x 255 /
green`, at most 16, the green the subsection's texels
average as drawn (the program masks the reflection by it; at least 16 on atlas
regions, whose texels vary, and 2 on colour cells, which are one exact colour). Each
on a geometric grid (`GLOSS_STEPS`: intensity x2, power x1.41, reflection x4), so a car
needs 3 to 12 materials (median 8); past `GLOSS_MAX_MATERIALS` (12) the rarest merge
into the nearest. Frame, undercarriage and `black` (no specular, no reflection in
Forza) stay as they were, and so does what the cockpit draws (its own Texturing).

**Calibration (in game, headless VM; IDs 1 Cougar Eliminator, 2 Continental (2004),
3 Mustang Boss 429, 4 Focus RS500, 5 300SL; `run-series.ps1 -Views grille,wheels
-Palette 0`).** Master's build (`out/fx/before`, run `vm1-20261027-175157`) against
three tunings. The first (specular 0.25 per SpecularAmount, reflection 1; `out/fx/after1`,
`vm1-20261027-180408`) showed the copied program works on trims and in the wheel part
(no glow from the zero lamp byte, no crash), but far too strong: the Cougar's scoop
(`badge`, intensity 1) and the Boss 429's (`matte_colors`) lit up cream from end to end
when the sun's reflection came near the camera (a flat panel takes the same `R.V` all
over), and the Focus RS500's black rims (Forza SpecularAmount 1, power 4) came out
cream: DSF's sun specular colour is far brighter than its diffuse light. The second
(0.04, reflection 0.5; `vm1-20261027-191933`) still showed khaki scoops and cream
rims; broad lobes (`(R.V)^1`, from Forza's power 4) light whole panels. The third, the
one kept (0.01, at most 0.0625, power at least 4; `out/fx/after3`, `vm2-20261027-194455`):
the scoops and grilles read as satin black with a grey sheen where they face the sky
or the sun, the grille bars and the Boss's grille mesh catch highlights, the Focus's
black rims are gloss black, and the chrome rims of the Cougar and the Continental
reflect like the vanilla chrome instead of drawing flat white
(`img/gloss-trims-ingame.jpg`, `img/gloss-scoops-zoom.jpg`,
`img/gloss-rims-ingame.jpg`). The vanilla chrome reflects `0.4 to 0.48 x env` (its
texel green); `GLOSS_REFLECTION_SCALE` 0.5 puts a CubeScale 1 chrome rim there.
Vanilla trims have no sheen at all (vanilla 175, the Cougar's host, in the same runs), so
this is Forza's look drawn with a vanilla program rather than vanilla's look.

The 300SL shows a white glint on its side window in every build (the first and the
last, a 16 times different specular): DSF's glass, not the gloss materials.

These runs were made before #177 (2026-10-28). Until then a flat-colour gloss or grey
trim (a colour cell: the flat near-black trims, `dsf_trim_grey`, flattened artwork)
in a section that the plates pass or `damage.split_exposed` rebuilt drew as plain
shared trim, in the exterior entry on about 370 cars and in the full entry's LODs on
about 390 (#173; #177's survey). Textured gloss subsections (detail grilles, badges,
emblems, rims) were not affected. So the calibration was judged partly on cars
missing some of their flat gloss trims; #177's runs (`vm1-20261028-155614` before,
`-160123` after: the Camaro ZL1's and XB Falcon's flat trims on gloss) and #178's
(below) are the first with all of them.

**Cost.** Each copy is a Lights MACO (about 11 KB in the full entry, 12 KB in the
exterior), and the trims and rims that move to it take stride-32 vertices instead of
28. The five test cars' full entries grow by 0x11800 to 0x25800 (the Cougar, 12
copies: MDXN 0x101800 to 0x127000) and their exteriors by 0x15000 to 0x2f000, far
under the 0x2C0000 limit.

## Gloss power and SpecularColor (2026-10-28)

Issue #176: on the player's car (since #177 the exterior entry draws the gloss trims),
the Camaro ZL1 12's front splitter (`bumperFa`, `dsf_trim_gloss~plastic2`, a
(13, 13, 13) colour cell) drew as a cream band in the sun and its bonnet vent insert
(`hooda`) as a khaki panel. Gloss material: intensity 0.031, power 5.66.

**Forza's specular is Phong (confirmed-static; `dsff fxobj --shader 0x85a3c` and
`0x86914`, the paint-trim and trim shaders).** The vector the cube map reads (the
view reflected about the normal: `2(N.V)N - V`) is dotted with `psLightDirWS`
(`dp3_sat`), and `log`, times `SpecularPower` (`c124` / `c168`; the scalar-constant
op prints it as `r60` / `r40`, register & 63), `exp`. That is `(R.L)^SpecularPower`, the
same form as DSF's `(R.V)^g_hSpecularPower`, not Blinn's `(N.H)^p`. The term is
then multiplied by `SpecularAmount x shDirectionalColourAndLum.w` and by
`SpecularColor` (`c125`, RGB). Neither shader has an N.L or texel factor on it.

**What the converter did.** It divided the power by 4 (taking Forza's as Blinn's)
and left `SpecularColor` out. The shared `Normal.xml` gives the four big trim
techniques a dim one: plastic2, plastic and plastic_simple 0.4, matte and
matte_colors 0.3, rubber_trim 0.5, frame 0.04, `rim` 0 (others 1); 25 cars set their
own on a gloss technique (the Syclone's plastic2 0.3). The Camaro's plastic2
(SpecularAmount 2.478, power 23.55, colour 0.4 by default) took 0.031 at `(R.V)^5.66`
(half peak 28 degrees off the mirror direction) where Forza has 0.0099 at `^23.55`
(14 degrees): four times the light on a lobe twice as wide, so a flat panel lit end to
end over a wide range of sun and camera angles.

**Fix (`dsf_materials.GLOSS_POWER_RATIO` 1; `forza_materials.SPECULAR_COLOUR_DEFAULTS`,
`gloss_params`, `parse_shader_settings` keeps `SpecularColor` as its RGB mean).**
`GLOSS_SPECULAR_SCALE`, the floor of power 4 and the cap stay: the calibration cars of
#143 (the Cougar's `misc` and `badge`, colour 1, power 5 and 128) keep their sheen.
Default `matte_colors` (0.75 x 0.3) now falls under `GLOSS_MIN` and has no sun
specular, as near as DSF gets to Forza's faint one.

**Roster (offline, 573 cars, `out/s176/survey.py`, not committed: cutout, trim_art and
plan() at LOD 1 as `port_car` runs them, the gloss materials of the exterior body).**
"Lights at once": the largest area whose normals lie within half the lobe's half-peak
angle of one direction (a flat panel that lights end to end).

| | master | this change |
| --- | --- | --- |
| LOD 1 body triangles with a sun specular | 1,487,826 (573 cars) | 1,169,099 (566) |
| of them at a broad lobe (DSF power 8 or less) | 1,114,023, 1,019 m2 | 549,565, 408 m2 |
| triangles at intensity 0.0156 or more | 930,455 | 392,406 (rims and badges, power 128: 368,867) |
| cars with 0.1 m2 or more lighting at once at intensity 0.0156+, power 8 or less | 324 | 6 |
| ... 0.25 m2 or more | 125 | 2 |

Every car's gloss materials change. The ones left with broad lobes (power 4 to 8) are
those Forza gives them (plastic2's default 8, the Syclone's 3, the Kenworth's
matte_colors), now at Forza's dimmer colour.

**In game (VM 1, `-Views nose,rear,grille -Palette 0 -ShotSeconds 3`, vanilla 229, then
IDs 1 Camaro ZL1 12, 2 XB Falcon GT 73, 3 Huayra 12, 4 SVT Raptor 12, 5 Express 2500
Cargo 05).** Master `vm1-20261028-164942`, the power rule alone
`vm1-20261028-165557`, both changes `vm1-20261028-170804`
(`img/gloss-power-camarozl1-ingame.jpg`, `img/gloss-power-raptor-express-ingame.jpg`,
`img/gloss-power-falcon-huayra-ingame.jpg`). The Camaro's splitter and vent insert
are black, the Raptor's bonnet vents lose their khaki and the Express's front bumper
its cream band; the Falcon's and Huayra's trims look as before. The power rule alone
already removed the Camaro's band in these views.

## Open

1. In game, badges on the Chassis material: seen since ("Badge plates": the
   205's lettering and the Firebird's "TRANS AM", `vm-20261024-105751`), and
   since #143 the glossy ones draw on gloss copies of Lights.
2. A real alpha test: the Chassis material has none. The Dials material
   (interior entry, `g_hAlphaTestRefValue`) is a candidate template for
   cut-out emblems and grilles. Exterior badges are now cut as geometry
   ("Badge plates"), which costs triangles (the Firebird: about 7000 more in
   the full entry, MDXN 0xf6000 to 0x11c800); a material with an alpha test
   would cost none.
3. Answered: DSF's window glass over a converted lamp hid it (#160). Lamp
   covers over a lamp are now left out or baked into the lamp texels, as the
   `detail_glass_*` lenses are (`lights.md`, "Lamp lenses drawn opaque",
   "Lamp covers drawn as window glass", "Lamp glass").
4. (Done and dropped: the converted cockpit in the interior entry,
   `cockpit.md`.)
5. Damage textures (`damage`, `damagelights`) and DSF's damage decal path.
6. Parsing the `.fxobj` effect's technique tables (technique → pixel
   shader → sampler set) would make the atlas choice authoritative for custom
   shaders and say whether `chrome` tints its reflection by the texel (the dark
   chrome texels above). The shaders themselves (constant tables, microcode)
   are readable with `dsff fxobj` ("Forza's trim shaders and flat black
   grilles").
7. The cockpit draws exterior chrome (A-pillar trim, bonnet strips) flat from
   its atlas; vanilla interiors have `GLOBAL_LOD_Chrome` (host 193 uses it for
   3,700 triangles), which the interior entry could use the same way.
8. Trim artwork ("Trim artwork", "Forza's trim shaders and flat black
   grilles"), issue #58 (closed 2026-10-28 by #91 and #143): `PaintScale` /
   `PristinePaintScale` are decoded (they scale the texel) and mid-grey and
   dark detailed trims go through the atlas. Open: which technique runs which shader (the effect's technique
   tables aren't decoded; `plastic2` sets both parameters on some cars), the
   `black*` techniques (still the shared dark trim whatever their texels),
   DSF's shared chrome can't take the Forza grey as a tint (#154), and the visibility
   test is orthographic from around and above: artwork seen only from below or
   through a narrow gap counts as hidden (no car known to show it, #58).
9. Gloss ("Gloss: Forza's specular and reflection on trims and rims"): the cockpit
   (interior entry) draws the outside sections it holds (bonnet scoops, A-pillars)
   with its own Texturing, and `cockpit.py` installs no gloss materials. The
   cockpit view draws the interior entry alone (confirmed-runtime 2026-10-29), so
   from the driver's seat painted panels reflect (the interior paints with the
   exterior's `GLOBAL_Chassis_LOD1`) but trims, scoops and chrome don't: #136's
   closing observation was the paint or `_Interior`'s specular, not a change
   (`cockpit.md`, "What DSF draws"; issue #232); the reflection on atlas regions is masked by their texel
   green (a grille's bars reflect more than its gaps, unlike Forza); `black*`,
   `frame` and `undercarriage` have none in Forza and stay flat; the calibration
   (`GLOSS_SPECULAR_SCALE`, `GLOSS_REFLECTION_SCALE`) was judged on five cars by day.
