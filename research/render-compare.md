# Render comparison: every port against Forza's own look

Updated: 2026-10-29

`dsff render_compare` (`src/dsfforza/harness/render_compare.py`, `render_match.py`,
`forza_render.py`, `render_diff.py`, `render_sheet.py`; the renderer
`src/dsfforza/game/car_render.py`) renders every car of a build as the game draws it,
next to a reference render of the same Forza car with Forza's own materials, at the
same cameras, and scores the difference per region (lamps, trims, plates, body...).
It finds conversion problems roster-wide, offline, before players see them; its
before/after mode lists every car a converter change touches and how.

```powershell
uv run dsff render_compare run --build out\build --out out\rc\master --jobs 4 --entries exterior,interior,full --keep
uv run dsff render_compare sheet out\rc\master [--top 80]       # out\rc\master\sheet.html
uv run dsff render_compare rescore out\rc\master                 # new limits on stored renders (--keep)
uv run dsff render_compare diff --before out\build --after <pr>\out\build --out out\rc\pr --jobs 2
c\pr --jobs 2
uv run dsff render_compare thumbnail --build out\build --cars BMW_X6M_10 --out out\rc\thumbs
```

A build folder is what `port_batch build` writes: `dngvehicles.sp`, `batch-report.json`
(the cars and their spool IDs) and `work/<media>.json` (each car's converter report: the
placement, the LODs drawn, the cockpit eye). Nothing else of the build is needed, and
nothing is written outside `--out`. Evidence tags as elsewhere (`dngvehicles.md`).

## What is rendered

Per car, the entries the game draws (`car_render.load_scene(..., entry=, physical=True)`):

| Entry | What | Views |
| --- | --- | --- |
| clean exterior (kind 2) | the player's car, its wheel part on every hub, the interior entry through the glass | `front3q`, `rear3q`, `side`, `front`, `rear` |
| full entry (kind 0), LOD mask 4 | traffic's nearest LOD: part 0 and the wheel parts (the damage panel parts repeat part 0) | `full-front3q`, `full-rear3q` |
| interior (kind 1) | with its glass and needles, from the driver's eye | `cockpit` |

`front3q` is the garage picture's camera (yaw 36.5, pitch 13); `front` and `rear` frame
the last 0.75 m of each end, nearly straight on (yaw 12 and 168), where the lamps are
scored at the highest resolution; `side` is for body, trims and wheels only. The cockpit
camera is the converter's eye (`cockpit.eye` in the report, raised by `camera_rise` as
DSF's camera is), looking ahead and 11 degrees down, 75 degrees wide. 800 x 450 by
default.

Three lights (`car_render.Lighting`): `day` (the garage picture's studio, lamps off),
`day_lit` (the same light, the running lights on, Lua mask 4: what DSF lights by day) and
`dark` (environment and ambient at 5%, no key light; running lights and head lamps on,
mask 5). The game has no night; the dark mood is what it has.

## Our side: the built entry with its own materials

`load_scene(physical=True)` keeps the garage picture's loader and geometry and shades
each material as its DSF program does (`car_render.dsf_shading`; the programs are the
vanilla ones disassembled in `forza-materials.md`, "Gloss"):

| Program | Materials | Terms beyond light x texel |
| --- | --- | --- |
| `paint` | shared paint | the garage tone's paint (palette entry by the view angle) |
| `trim` | `GLOBAL_Chassis_Simple(_LOD1)` | 0.01 x tex.G x (R.V)^7.5 |
| `chrome` | the shared chrome | tex.G x (R.V)^150 + env x tex.G at every angle (texel R as the grey) |
| `tyre` | `GLOBAL_Tyre` | tex.G x (R.V)^30 + env x tex.G x 0.5 x max(0.2, 1 - N.V) |
| `chassis` | `<car>_Chassis`, the interior's | 0.03 x tex.a x (R.V)^24 |
| `lights`, `gloss` | `<car>_Lights` and its gloss copies (`dsf_materials.GlossMaterial`) | the MACO's own `g_hSpecularIntensity`, `g_hSpecularPower`, `g_hReflectionAmount` and Fresnel; glow tex.a x the lamp's colour |
| `glass` | window glass | as the garage picture's |

A copy whose `g_hSpecularIntensity` is under 0.5 is a gloss copy, else the host's Lights
material. Every triangle's lamp is vertex semantic 9 byte 2 (light id + 1) looked up in
the full entry's UPVH light table (`car_render.light_table`): its type (flags b3) and
colour (b2). A lamp glows by `texel.a x colour x level`, the level DSF's for the type with
the lamps that are on (`car_render.lamp_level`, from `lights.md` "How a lamp lights":
the type's brightness over a 0x01 head lamp's 2; the combined types 0x18..0x1C at 0.2
with the running lights alone, 1 when braking; 0x1D at 10). The cabin's own light bytes
are not lamps of the outside and are dropped in the exterior views.

Specular is Phong, `(R.L)^power`, as both games have it. Besides the garage picture's key
light from the camera's upper left, the comparison renders have a sun beyond the car,
25 degrees up, specular only (`SUN_POWER`): the light that put the Camaro ZL1 12's cream
band on its splitter (#176) when the sun was ahead of the camera.

The garage picture is unchanged (the default `physical=False`): the same pixels on three
cars before and after (`render()` = `rasterize()` + `shade()`), and the converter's
build fingerprints too: of `car_render` it runs only `_submodel_triangles`, `_elements`,
`_decode_normals` and `TYRE_UIDS` (`wheel_clearance`), which this doesn't touch.

## The Forza reference

`forza_render.body_scene` draws the main carbin's vanilla body sections at the LOD the
converter drew the entry from (`lods` in the report: the exterior from LOD 1, the full
entry's mask 4 from LOD 3), turned and moved into DSF space by the conversion's own
`Placement(dy, dz)` (`placement` in the report; `render_match.register` finds it from the
geometry for a build without reports), the pop-up lamps lowered as the converter has
them. The Forza wheel model sits on the built car's rest hubs, sized per axle as the
converter sizes it (`front_fit`, `rear_fit`, `tyre_centre`). The reference sees our cabin
through its windows (the same triangles: the cabin never differs). The paint is the
built car's palette entry in both renders, so paint conversion isn't what is compared.

Each subsection is drawn as Forza's shaders draw its technique, from what is decoded of
them (`forza-materials.md`, `lights.md` "Lamp glass", "Lamp covers drawn as window
glass"):

| Technique | Drawn as |
| --- | --- |
| paint (`body`...) | texel x alpha + paint x (1 - alpha), the paint at the shared atlas's plain texel's level, so a correct body matches |
| paint-shader trims (`misc`, `plastic2`...) | the same blend on their texels; trims' texel x `texel_scale` (PristinePaintScale or PaintScale) |
| trims, badges, emblems | `forza_materials.gloss_params` (the car's ShaderSettings.xml over Normal.xml's defaults): SpecularAmount x SpecularColor, SpecularPower, CubeScale x FresnelScalar with FresnelIndex's F0; alpha-tested ones (`ALPHA_TESTED`) cut at 128 |
| chrome | texel x ChromeDiffuseScale (0.1) under CubeScale (2) at every angle |
| lamps (Lights atlas) | their texel, glowing by its alpha (Forza's glow mask) |
| `glass` (plain lamp cover) | no texture: clear, the reflection only (#160) |
| `lights_glass`, `lights_gls_noemit` | see-through, opacity max(texel alpha, GlassColor.w) capped by GlassMaxOpacity |
| `lights_gls_*` | opacity GlassMaxOpacity (the lamp shaders start at 1), glows by its alpha |
| `detail_glass_*` | see-through at max(alpha, GlassColor.w) capped by 0.7 (or the car's), the texel tinting what is behind |
| `black_glass` | gloss black: its UVs land on the atlas's white alpha-0 body area |
| windows | DSF's window glass, as ours |
| tiling parts | the Lights atlas wraps (the Mustang GT 05's bowls, tiles -3, 0 and 3); elsewhere a global texture we don't have (tread, carbon, grille mesh): flat dark |

Forza's specular and reflection go onto this renderer's scale the way the converter maps
them (`FORZA_SPECULAR` = `DSF_SPECULAR` x `GLOSS_SPECULAR_SCALE`, `FORZA_REFLECTION` =
`GLOSS_REFLECTION_SCALE`), so a gloss trim converted within the converter's caps draws
alike on both sides and what differs is what the conversion changed.

**Lamps in the lit modes.** The reference's lamps light as the converter's lamp plan types
them (`lights.LampPlan.build` on the same sections: function, side, colour, the high stop
lamp brake-only; head lamps 0x1D when the built car has 0x1D lamps), at DSF's level for
that type, a 0x1D head lamp like a 0x01 one (the converter draws those texels at
`DAYTIME_GLOW`, a fifth, against the type's five times higher level). How much each texel
glows is Forza's alpha. So the reference says where Forza's glow mask puts the light, at
DSF's level; the plan's choices themselves are not judged.

**The cockpit** (`forza_render.cockpit_scene`): the cockpit carbin's static sections and
its needles at rest, as `cockpit.fit` measures them (the main carbin's cabin standing in
for a stub), drawn with `classify_cockpit` on the `_LOD0` atlases, paint not shown
through the cabin's paint-shader trims (their PaintColor isn't the body's), and the main
carbin's body round it at its finest LOD (the converter's coarser shell can close the
cockpit, #157). Forza's digital displays (`digital_gauge*`) and DSF's live digital dials
are drawn live by the games: region `displays`, not compared.

**Against Forza's own pictures** (`render_compare thumbnail`): FM4's 503
`Thumbnails/Thumbnail_<id>.xds` (by FM4 gamedb id) and FH1's, at a camera like theirs and
in the picture's paint: the reference has Forza's shapes, the X6 M's body-coloured
mirrors, the Devon's black stripe and lower body; Forza's studio is darker and its rims
read darker than both renders (`img/render-compare-thumbnails.jpg`).

## The ID buffer: which Forza triangle each pixel is

`car_render.rasterize` returns a `Frame`: per pixel the nearest opaque triangle and up to
two see-through layers in front of it. Both scenes carry per-triangle data, so a pixel's
triangle gives its region and lamp function on both sides and, on ours, its DSF program.

The built triangles get their Forza origin by geometry (`render_match.match`), which works
on any build, including those made before this tool: the converter writes every body
vertex at `round(Placement.point(p) x 4096)` (`forza2dsf.encode_vertex`), so each built
triangle lies on the Forza triangle it came from, exactly for the copied ones and inside
it for the pieces the converter cuts (cutout's badges, damage's split panels). Per built
triangle the match is the reference triangle with the smallest largest corner distance
(Ericson's point-triangle distance), among candidates from a grid: a first pass over the
reference centroids in the neighbouring 1 mm cells finds the copies, a second over 4 cm
cells holding every reference triangle's box widened by 2 cm the rest. Up to 1.5 mm a
corner lies on it; up to 2 cm it is a near match (decimated cockpits, plates and lenses
moved off a face); farther, none. Nothing is added to the archive or the converter.

| Car (out/build) | Exterior triangles | on | near | none |
| --- | ---: | ---: | ---: | ---: |
| FOR_MustangGT_05 | 21,162 | 20,708 | 42 | 412 |
| BMW_X6M_10 | 25,100 | 24,892 | 206 | 2 |
| NIS_370Z_10 | 21,283 | 20,995 | 19 | 269 |
| FOR_MustangGT_05 interior | 44,621 | 44,100 | 511 | 10 |

The built car's own parts: plates are paint quads on the shared atlas's plate
(`plates.PLATE_UV`, region `plates`), wheel parts are what load_scene set on the hubs
(`wheels`), the interior entry seen through the glass is `cabin`; unmatched glass is
`glass`, the rest `other` (not scored; the Mustang's 408 are one Chassis material along
the car's length, open below). Regions: `body`, `trims`, `chrome`, `glass`, `plates`,
`badges`, `wheels`, `front_lamps`, `rear_lamps` (lamp techniques, lenses, covers,
reflectors and the Lights parts of lamp sections, by end), `cabin`, `cockpit`, `gauges`,
`displays`. Window glass is see-through for the labels (the pixel is what is behind it);
lamp glass labels its pixels.

## Scores

Per view and region (`region_scores`, on the region's box):

- **coverage**: `missing`, the share of Forza's region pixels ours doesn't draw as that
  region, and `extra`; for lamps, the share where ours draws nothing at all (a plain cover
  is left out on ours and clear on Forza's, so either side's lamp pixels count);
  `ours_as` / `over`: what the other side shows there (a plate over badges);
- **brightness and colour**: mean L* each side and mean delta E (CIE Lab);
- **detail**: the local contrast of L* (standard deviation over 7 x 7 within the region)
  each side, and `structure`, the correlation of the region's fine structure (L* less its
  9 x 9 mean). Painted on is detail collapsing;
- **drawn as**: the share of our pixels per DSF program (a lamp drawn as shared trim or
  window glass can't glow).

In the lit modes (`glow_scores`), each side's glow is the light its lamps add (`day_lit`
less `day`; the dark render itself), its brightest channel. The levels differ by design:
the converter maps Forza's glow masks onto vanilla's alpha distribution (`lights.md`,
"Vanilla lamp alpha") and darkens red lamps to vanilla's. So each side is taken against
itself: a region's lit shape is where it glows at a quarter of its 95th percentile or more
(and over 0.03); `covered` / `inside` are the shares of Forza's lit shape ours lights and
of ours Forza lights; `spread` (std / mean of the glow over the lit shape) tells a flat
plate from a lamp with structure; `green_red` its hue; `stray` counts glow off the lamps.

**Flags** (`flags_for`, limits in `LIMITS`, the worst view per region and kind kept):
missing or extra over 35%; too dark or washed out by 18 L*; colour off by delta E 25;
detail lost over 50% (where Forza's contrast is 2 or more); structure under 0.25; a lamp
region drawn 30% or more by a program that can't draw a lamp (not lights, gloss or
chassis); no glow (Forza's 95th percentile over 0.06, ours under 0.03); glow elsewhere
(covered and inside both under 30%); flat glow by day (spread under 45% of Forza's);
glow in another colour; a plate 10% or more over badges, lamps or chrome (over a trim it
may be on its holder). Each flag's severity is its size times the region's weight (lamps
3, trims 1.5, the rest 1); a car's score is their sum, and the roster ranks by it.

## Calibration

Known cases, each car rendered from a build that had the bug and one that has its fix
(`out/rc/k-*` in the `t3-1b4e7c10` worktree, `--keep`; `out/rc/calib.py` prints the
table). Bad builds: the main checkout's `out/master` (master of 2026-10-27, before
#143/#163/#164/#175/#177) and `out/build` (master at 50b7cb6, before #190), and two small
builds of these cars from old commits (5e6ce2d, before #178; c5fce5c, before #167). Good:
the same cars built from master d1fbc57 (17 cars), and `out/build` for what was fixed
before it. The flags of the case's region:

| Case | Car | Bad build: flags (car score) | Fixed: flags (score) |
| --- | --- | --- | --- |
| #158 bowls drawn as glass, no front lamp | Mustang GT 05 | drawn as glass 89%, no glow by day and dark, detail lost 78% (14.6) | glows elsewhere than Forza's mask, 29% (2.6) |
| #190 white domes | Mustang GT 05 | flat glow by day (4.4) | as above (2.6) |
| #145 swatch lens tint | X5 M | no glow by day and dark (ours 0.008 / 0.02, Forza 0.16) (12.9) | darker than Forza by day, L* -22 (1.9) |
| #190 flat tail lenses | X6 M | no glow by day and dark (7.4) | colour off, delta E 26 (2.7) |
| #160 flat red tail plate | Devon GTX | detail lost 85%, too dark, flat glow by day (15.2) | colour off, delta E 32 (#166, open) (6.5) |
| #176 cream splitter, vent insert | Camaro ZL1 12 | trims washed out, L* +21 (3.4) | - (1.8) |
| #176 khaki vents | SVT Raptor 12 | trims washed out, L* +10 (2.0) | - (1.5) |
| #176 cream bumper | Express 2500 | trims washed out, L* +23 (11.0) | - (8.7) |
| #167 white plates by day | GTI VR6 | white plate by day, glow 7.2 (7.2) | - (0) |
| #167 | Lancer Evo VI | white plate by day, glow 8.5 (11.1) | washed out by day, structure (4.1) |
| #167 | M3 E30 | white plate by day, glow 7.6; detail lost 54%; drawn as glass 38% (12.3) | - (1.6) |
| #159 plate on the badge | Mazda 2 | plate 18% over the badge (14.7) | - (10.3) |
| #157 shell lid in the cockpit | Audi R10 (10) | cockpit 3.1% covered (12.1) | - (8.8) |
| #157 | Audi R10 (08) | cockpit 4.6% covered (11.0) | - (8.0) |
| #173 trims as shared trim on the player's car | Camaro ZL1 12 | trims detail lost 66% (11.6) | - (1.8) |
| #190 painted-on head lamps | Esperante GTLM | head lamps washed out, L* +26 (7.9) | washed out, L* +29 (9.2): not separated |
| #190 left and right tails differ | Esperante GTLM | glows elsewhere (7.9) | glows elsewhere (9.2): not separated |

15 of the 17 cases separate: the bad build carries the case's flag, the fixed build
doesn't. The limits came from these cars (`LIMITS`, the numbers in its comments): the
glare limit 3 (#167 7.2 to 8.5, fixed 1.6; a 0x01 lamp's texel at full alpha glows 2), the
cockpit cover 2% (#157 3.1 and 4.6%, fixed and controls 0.4% at most), trims 8 L* lighter
(good builds +6 at most, #176 +10 to +23), a plate 10% over badges, lamps or chrome
(#159 18%; the fixed Mazda 2's plate sits on its trim holder). The Esperante's head lamps
look alike to the scores before and after #190 (the reference draws its clear lens over
dark bowls, both builds lighter); left/right symmetry is not measured.

The build of 2026-10-27 shows #160 on 9 of the 10 cars rendered from it: their front lamp
covers are DSF's window glass ("front lamps: drawn as glass" 70 to 100%); on master
(d1fbc57) 3 of 17 still have such covers (Devon GTX, Cougar Eliminator, Golf R).

Flags that remain on fixed builds are differences from Forza that the converter makes on
purpose or that are open: rear lamps darker than Forza by day (the red lamp texels are
darkened to vanilla's, #166), front lamps glowing where Forza's mask doesn't (the
converter lights reflectors and gives head lamps vanilla's daytime glow, #124), the
Esperante's lamps lighter than Forza's.

## Roster

Master ede81a1's full build (the main checkout's `out/build` of 2026-10-29 13:33, copied
before the run; `out/rc/roster-final` in the `t3-1b4e7c10` worktree), exterior views, 573
cars, no errors: car score median 5.2, p90 11.6. The cockpit and traffic views are a
second pass (`out/rc/roster-final-cockpit`). Flags by kind (cars):

| Region | Flag | Cars | What it is |
| --- | --- | ---: | --- |
| trims | detail lost | 242 | Forza's trim texels against the flat shared trim; mostly the converter's design (Open 7) |
| rear lamps | colour off | 222 | the red lamp texels mapped onto vanilla's (#166; comment there) |
| rear lamps | structure | 216 | the lenses baked, Forza's lamp seen through glass |
| chrome | extra | 124 | bare-metal trims on the shared chrome (#154) |
| rear lamps | no glow | 109 | under 0.03 with the running lights: vanilla's tails glow 0 to 0.24 in this renderer (8 cars, 3 under 0.04), so mostly vanilla-like (Open 8) |
| front lamps | structure | 96 | |
| rear lamps | glows elsewhere | 94 | reflectors and lenses lit (#163) where Forza's mask is dark |
| front lamps | drawn as glass | 90 | lamp covers that stay DSF's window glass; vanilla has glass over most of its head lamps too |
| rear lamps | too dark by day | 40 | #166 |
| plates | over badges, lamps or chrome | 44 | over chrome bumpers on most; over a lamp on five race cars (#251) |
| front lamps | white plate by day | 3 | Volvo S60 11 7.7, Venom GT 12 6.1, Boss 429 3.1 (#250) |

The worst-ranked cars (score): FER_458Spider_12 20.7 (head lamps under window glass;
rear lamps' structure), PON_FirebirdTransAm_69 18.0 (tails unlit; trims L* 6 against 69),
DOD_StealthTurbo_96 17.7, FOR_Thunderbird_57 17.6, CAD_6_LMP_02 17.5, AUD_2_R8_06 17.3
(head lamps at about 4% of Forza's glow under lexan covers drawn as window glass, #247),
BMW_15_LMR_99 16.7, CHE_CorvetteGrandSport_96 16.4, NIS_12_GTR_10 16.3,
CHE_ElCaminoSS_70 16.0; `sheet.html` has all 573 and the flag table. Ten cars, ours |
Forza: `img/render-compare-sample.jpg`.

Issues from the run: #247 (R8 head lamps), #250 (white plates by day), #251 (plates over
rain lights), a comment on #166. Not filed: the categories above that are the
converter's design or vanilla's behaviour.

## Before/after

`render_compare diff --before A --after B` (`render_diff.py`): the cars whose drawn
data differ are found by hashing, in both archives, each car's three VEHC entries: their
MDXN and NONR's MODL, UPVH and paint palette (SPSR). VO3D (handling, cameras) and VSRT
are left out, so a handling change touches no car. Hashing 573 cars in both archives
reads a few GB; on 17 cars it takes 4 s. Only those cars are rendered, from both builds
against the same Forza reference. Per view, mode and region it reports the share of the
region's pixels that changed (moved, or delta E 6 or more), the delta E, what draws the
region before and after, and the flags each build raises against Forza (fixed, new).
`diff.html` shows before | after (changed pixels outlined) | Forza.

On #190 (the 17 calibration cars built from 50b7cb6 and from d1fbc57 at the same IDs,
`out/rc/diff-190`): all 17 cars' drawn data changed, 6 are drawn differently: the X6 M's
tails (82 to 88% of the region's pixels; "no glow by day and dark" fixed, score 7.4 to
2.7), the Mustang GT 05's head lamps (68 to 79%; 4.4 to 2.6), the Esperante GTLM's head
lamps (91 to 94%), the Golf R's and Mazda 2's tails, the X5 M a little (8 to 15% in the
lit modes). The other 11 render alike: their data differ because #192's handling change
moves the settled hubs (UPVH, a few bytes) and the full entry's atlas is not built byte
for byte the same twice (#84); `diff.json` lists them as `renders_alike`
(`img/render-compare-diff-190.jpg`). 359 s for the 17 cars at 2 jobs (21 s a car
and build pair).

## Runtime

A car (exterior five views, cockpit, full entry two views; 800 x 450): 25 to 45 s, median
about 37 s on this PC (load 1 s, Forza scene and lamp plan 2 to 4 s, cockpit fit 5 s, the
geometry match about 1 s, 2 x 8 rasters and 2 x 19 shades, the scores 1 s a view). The
roster: 573 cars in about 2 h 30 at 4 jobs and then 2 (Idle priority; the tool lowers
itself, its workers inherit it). `--keep` stores 6.6 MB a car (3.8 GB for the roster);
`rescore` scores the stored renders again, 2 jobs about 25 min for 573 cars, and resumes.

## Open

1. The Esperante GTLM's head lamps and its left/right tails (#190) don't separate
   (Calibration). Left/right symmetry is not measured.
2. The cockpit coverage flag (calibrated on the Audi R10s' lid, #157) fires on 134 cars,
   most from the shell: the reference's is the finest LOD, ours the converter's coarser
   one, so the bonnet seen through the windscreen differs by design. It is weighted down;
   a mask of the cabin below the windscreen would make it specific.
3. Glow levels are compared shape against shape, not in absolute terms, except the
   day-time glare limit: the converter maps glow onto vanilla's distribution on purpose.
4. Forza's shaders are approximated: tread, carbon and grille-mesh tiling textures are
   flat; blend states of the lamp glass are guessed (lenses tint what is behind); the
   paint is the built car's palette entry on both sides. Forza's FM4 thumbnails are
   darker and their rims darker than both renders. Calibrating against Forza itself
   (Xenia) is a later task.
5. About 2% of a car's body triangles match no Forza triangle: the main-carbin steering
   wheel, which the converter moves onto the cockpit's rim (#117), and a few others; they
   are region `other`, not scored.
6. The reference uses the current converter's lamp plan for every build (calibration
   builds from older commits included).
7. Trims "detail lost" fires on 242 cars: Forza's trim texels against DSF's flat shared
   trim (the one dark texel). The converter moves detailed trims to the atlas
   (`trim_art`); which of these are real (grille bars drawn flat) is not sorted.
8. "No glow" uses a fixed floor (0.03); vanilla's own tails reach 0 to 0.24 with the
   running lights in this renderer (266 0.04, 182 0.06, 147 0.20, 227 0.03, 229 0.04,
   171 0.09, 205 0, 241 0.24). A limit taken from vanilla's distribution per light type
   would make it a test of "darker than vanilla".
