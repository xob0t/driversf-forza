# Forza materials and textures, and their DSF counterparts

Updated: 2026-10-24

Code: `tools/forza_materials.py` (Forza side), `tools/dsf_materials.py`
(DSF side, atlas, preview), `--textures` in `tools/forza2dsf.py` and
`tools/exp_hollow.py`. Checked on NIS_370Z_10 (FH1) and the Desanne host.

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
texels, so only the emblem letters show in Forza.

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
| glass (headlight cover) | Lights | textured | glass | shared GLASS |
| black_glass | nodamage | textured | trim | shared TRIM |
| black, frame, bottom, plastic2, weatherstrip, matte_colors, misc, chrome, rubber_trim, bumper_frame, undercarriage, race_kit_red | nodamage | mostly swatch | trim | shared TRIM (`--swatch-trims`: Chassis with the swatch colour) |
| carbon_fiber | global | tiling | trim | shared TRIM |

Shared TRIM (`GLOBAL_Chassis_Simple`) samples DSF's global chassis texture
(SSIC `00122b73d1fa1227`, 1024x512: engine bay, undercarriage, grilles,
plate, swatches; stock trims unwrap onto those). Converted trims carried
Forza's UVs into it: swatch trims hit one arbitrary texel, tiling ones
(carbon x246) spread over the whole texture. The Veyron's exposed carbon
(22 m², most of its upper body) drew bright noise ("materials messed up",
`img/fix-veyron-trim.png` top). Converted trims now all use the texture's
uniform dark block (`forza2dsf.TRIM_UV`, (0.969, 0.496)), the texel stock
trims drawn from a single texel use most (46 hosts). With `--damage`, the
trims a missing bonnet or boot lid exposes (bay walls and floor, panel
undersides) are projected onto the texture's engine-bay pictures instead,
and panel undersides use the paint material (`damage.md`, step 6).
| interior | nodamage | textured | textured | host **Chassis** (stride 28) |
| emblem | nodamage | textured, alpha-tested | textured | host Chassis; fully transparent triangles dropped |
| badge | nodamage | alpha 0 everywhere | textured | dropped |
| taillight2S, tail_light, reverse_light, textured_reflector, indicator_left/right, hidhead, oldhead, slorange, drlwhite, detail_glass_red, detail_glass_clear | Lights | textured | textured | host **Lights** (stride 32, UV1 = UV0) |
| fogred | Lights | swatch | textured | host Lights, solid cell |
| tire, rim, inner_rim | global / wheel | tiling | tyre | shared TYRE |
| blur_rim, blur_lip | | | skip | dropped |

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
on `out/testdrive9`).

Plates: `numberplate` (a plate quad on 2 of the 40 testdrive cars) and
`numplate` (the plate lamp strip) stay trim; the converter replaces
`numberplate` quads with DSF's stock plate and places one on cars without
(`plates.md`).

The full technique table, with prefix fallbacks for names not seen on this
car, is `TECHNIQUES` / `classify()` in `forza_materials.py`.
`python tools/forza_materials.py <car dir> --colours` prints this per car.

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
  Forza `Lights` alpha through the stock-matched per-hue curves
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
  261, and lod_max_subs from 52 to 5, all below the stock maxima (kind 0:
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

## Open

1. In game: whether the Chassis material lights badges well (the Lights
   glow on Forza lamp alpha ran in game: `lights.md`).
2. A real alpha test: the Chassis material has none. The Dials material
   (interior entry, `g_hAlphaTestRefValue`) is a candidate template for
   cut-out emblems and grilles.
3. Headlight glass uses shared GLASS, so the lamp internals show through it.
   Whether DSF's glass is transparent enough is unchecked.
4. Done: the interior entry (N+1000) gets the converted
   `<car>_cockpit.carbin` by default (`cockpit.md`; `port_car.py
   --no-cockpit` keeps the host's).
5. Damage textures (`damage`, `damagelights`) and DSF's damage decal path.
6. Parsing the `.fxobj` effect (technique → pixel shader → sampler set)
   would make the atlas choice authoritative for custom shaders.
