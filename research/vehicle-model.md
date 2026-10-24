# Vehicle model format (VEHC → MDXN/NONR)

Updated: 2026-10-23

Decoded from `Saloon 15` (ASYM Desanne, spool ID 266, VEHC 3–5) with
`src/dsfforza/formats/vehicle_mesh.py`. Confirmed by rendering: the decoded geometry is the
Desanne, correctly assembled (side/top/front previews from
`vehicle_mesh.py --png`). DSF's chunked MDXN is not the format DSCript reads
(its MDXN reader expects DPL's flat `MGX` blob), so all of this is new.

Every resource is identified by a 64-bit UID (8 bytes, stored as-is).
References between resources are by UID.

## The three VEHC entries per vehicle (confirmed-static)

| Kind | Spool ID | Contents | MODL LOD masks |
| --- | --- | --- | --- |
| 0 | N | full car: 32 parts (body, doors, hood, bumpers, wheel…), 243 submodels, sound, handling, hierarchy, paint | 4, 8, 16, 1 |
| 1 | N+1000 | **interior**: cabin, seats, dashboard, steering wheel (rendered) | 64 |
| 2 | N+2000 | clean **exterior**: one body plus wheel, no separate damage parts | 2 |

Across the fleet (`out/dmg/kinds.py`), every kind-0 MODL uses exactly masks
{1, 4, 8, 16} and every kind-2 MODL only mask 2, with 3 to 9 parts, all
non-empty (87 vehicles have 5, 54 have 6). The extra exterior parts are the
movable ones: on the Desanne exterior part 1 is the rear suspension (UPVH
record 63, role 190, full part 6), part 2 the front suspension (record 31,
role 188, full part 21; an earlier note here called these two the bumpers,
renders show struts and arms), part 3 a small part on record 26 (role 185,
the gauge-cluster record), part 4 the wheel (all four wheel records). They
are linked to the hierarchy through the UPVH LOD links. The bumpers, bonnet
and boot are in exterior part 0, which carries every panel's binding
(semantic 9) and crush target (semantic 12), so the clean exterior crushes
and swings its panels like the full entry (`damage.md`). A damaged player car keeps drawing the exterior with the damage
applied (cracked glass state, deformation, bumper hinges; hypothesis from
the fix3 screenshot of the host's exterior drawn in place of a damaged
ported car, `in-game-tests.md`); which entry a damaged traffic car draws is
untested.

SSLP slot capacities (35 / 2 / 2) fit this: only the player's car (and maybe
a Shift target) needs the interior and the clean exterior. In game,
`VehicleLodSpooler.IsInteriorLoaded/IsExteriorLoaded` are both true for a
driven car. The LOD-mask meanings beyond that (4/8/16 = detail levels,
1 = shadow?, 2 = exterior, 64 = interior) are hypotheses.

## Geometry (confirmed-static, rendered)

Inside `VEHC/MDXN/DRSP`:

```text
VXCK  vertex buffer
  VXHD  {u64 uid, u32 vertex_count, u32 stride}
  VXDP  u64[] UIDs of resources depending on it (submodels)
  VXDT  vertices, vertex_count * stride bytes
  VDCL  {u32 count, count x {u32 semantic, u32 components, u32 type, u32 name_hash}}
IXCK  index buffer
  IXHD  {u64 uid, u32 index_count, u32 index_size=2, u64 ?}
  IXDP  u64[] dependent UIDs
  IXDT  u16 indices, triangle lists
SBMC  submodel
  SBBH (0x48, v4) {u64 uid, u64 material_uid, u64 vertex_buffer_uid, u64 0,
                   u32 base_vertex, u8[12] 0, u64 index_buffer_uid,
                   u32 first_index, u32 index_count, u64 0}
MDID  u64[] all renderer resource UIDs in the package
```

The 243 submodels' index counts sum to exactly the 100,077 indices in the
one index buffer, and each count is a multiple of 3.

VDCL element types (sizes check against the stride in all three buffers):

| Type | Component |
| ---: | --- |
| 1 | int16 |
| 2 | uint8 |
| 6 | uint8 (packed, probably normal/tangent) |
| 8 | float16 |

Semantics seen, in order: 0 (position, 4 × int16), 8 (2 or 4 × float16,
probably UV sets), 9 (4 × uint8), 5 (4 × packed uint8), 12 (4 × float16).
Since decoded in `damage.md`: 8 is UV0 and, in the 32-byte buffers, UV1
(the damage decal), 5 the normal's polar and azimuth angles plus their
change at full damage, 12 the crushed position. Semantic 9 is now known: a hierarchy binding,
(UPVH record +0x34, record +0x2A, light index, 1), with (0, 0, 0, 1) for
the chassis; it is how the steering wheel turns (`cockpit.md`). In the
interior's stride-20 needle buffer, semantic 8 component 2 is the gauge id.

**Positions: metres = int16 / 4096.** The body buffer spans
±1.04 × ±0.76 × ±2.57 m, matching the MODL box.

**The fourth int16 is a baked sky-light / ambient-occlusion term, 0–4096 =
0–1** (confirmed-static by correlation; that it drives the lighting is
confirmed only indirectly, see below). On the Desanne it follows the
normal's up component: binned by `(1 + n_y) / 2`, the clean exterior's mean
w rises from 0.06 (facing down) through 0.34 (sideways) to 0.64 (facing up),
correlation 0.61, and it drops in occluded places (wheel wells, under the
dash). Medians: full 0.45, exterior 0.41, interior 0.21. Needles and the
wheel-blur discs carry exactly 4096. Converted cars had 0 everywhere, and in
game their paint was black (the gold Desanne palette showed only as
reflections) and the cockpit nearly black, except the needle pivots, the only
vertices with 4096 (`img/cockpit-v1-a.png`). `forza2dsf.vertex_sky` now writes
Forza's own baked AO (first trailing vertex byte, `forza-materials.md`) times
`0.25 + 0.55 (1 + n_y) / 2`; the converted cars' medians come out at 0.42 to
0.48 (full and exterior) and 0.20 to 0.25 (interior), close to stock. In-game
confirmation pending (hypothesis-runtime).

**Semantic 12** is the vertex's fully crushed position as float16 (x, y,
z, 1.0); the shader draws lerp(position, s12, panel damage). It equals the
position only where nothing crushes (glass, cabin, about a third of the
vertices); an earlier note here said it was always the position. Details:
`damage.md`.

**Glass UVs live in v 0–0.25** (confirmed-static). Every stock glass
submodel (the shared glass materials `001283369afd1027`, `0012cc8576953429`,
`10122b9953b14e2a`; MODL value 7, stride 28) keeps UV0 v at or below 0.26
for 99.2% of 585,632 glass vertices across the fleet
(`out/glassdbg/glass_survey.py`; the 4,946 above are concentrated in a few
vehicles, VEHC 153 and 45 the most; some vehicles also go slightly negative). The Desanne's
panels are laid out side by side in that band (windscreen u 0.16–0.63,
v 0–0.12; rear window u 0.40–0.62, v 0.12–0.25; doors u 0.67–0.99). With
converted glass carrying Forza window UVs over v 0–0.97 an undamaged car
showed a shattered windscreen and rear window, worst in cockpit view
(`img/cockpit-v1-a.png`, `img/skyline-chase-v1.png`).

**The glass texture** (confirmed-static). The three glass materials are
`GLOBAL_Glass` (`001283369afd1027`), `GLOBAL_Glass_LOD1` (`10122b9953b14e2a`)
and `GLOBAL_Glass_LOD5` (`0012cc8576953429`, the one interiors use), defined
in the archive's shared SSIC chunk (below). All three sample
`g_texDiffuseMap` `00122b77dffa1227` (SSIC, DXT1, 512 wide, 1024 high;
dump: `out/dmg/global_glass.py`). Its four v bands are damage states:
0–0.25 clean, 0.25–0.5 light cracks, 0.5–0.75 heavy cracks, 0.75–1
shattered with holes. Inside the clean band:

| Region | Content | Used by |
| --- | --- | --- |
| u 0.12–1, v 0–0.125 | flat (13, 233, 192) | exterior windscreen and door glass |
| u 0.17–0.62, v 0.13–0.23 | rear-window defroster lines on the flat colour | exterior rear window |
| u 0–0.12, v 0–0.06 | grey smear (mean (57, 139, 90): alpha 0.35, reflective), the same in bands 0–2, a hole in band 3 | interior windows: 9% of stock interior glass triangles but 64–97% of each window's area (147, 227, 266; `out/glassfix/intglass.py`) |
| u 0–0.12, v 0.06–0.125, and u 0.06–0.12, v 0.125–0.25 | flat black (13, 0, 0): blue 0, discarded by the pixel shader in bands 0–2; in band 3 smear-coloured (upper block) and bright streaky shards (94, 189, 173, lower block) | interior rim shards, visible in state 3 only: 66% of stock interior glass triangles (3 mm slivers; 3–34% of the area) |
| u 0–0.06, v 0.125–0.25 | flat dark red (66, 0, 0), blue 0 in band 0; streaky (blue ~170) in band 1 from v 0.18 down and in all of band 2; black in band 3 | interior crack lines: rows 0.18–0.25 show in states 1 and 2, rows 0.125–0.18 in state 2 only (9% of interior glass triangles) |

`GLOBAL_Glass_LOD5` reflects far more than the exterior glass
(`g_hReflectionAmount` 1.0, `g_hReflectionFresnelBias` -0.5,
`g_hSpecularPower` 150; `GLOBAL_Glass` 0.25, -0.1, 1500), so interior glass is
a dark, reflective layer. Stock interior windscreens are dense shard meshes
(2.2 cm triangles on the Desanne, 2,691 vertices for one windscreen), each
shard with its own small UV patch (u extent about 0.03, no spatial
continuity), presumably so the crack bands break along shard edges.

**How the engine picks the band** (confirmed-static). `GLOBAL_Glass` has
eight techniques (SSIC programs 46 to 53; `out/glassfix/glassmat.py`,
disassembly with `out/damage/gpdis.py`). Programs 46, 49, 51 and 52 pass
UV0 through unchanged and read only s9 byte 0 (the panel transform).
Programs 47 and 50 (and 55 of `GLOBAL_Glass_LOD1`) are the damage-aware
ones:

```text
v'   = v + 0.25 * floor(g_v4PerPanelData[s9.byte1].y)     // crack state 0..3
pos' = lerp(pos, s12, fDamage)                             // as on the body
```

So each glass vertex shows the crack state (`.y`) of the record its
semantic 9 byte 1 names, chassis included, and the state picks the band.
The pixel shader (`ps` of program 46) discards texels with blue below 0.01
(`texkill`): the black areas of band 3 are holes. Red scales the diffuse
light, green the reflection and specular, blue is also the alpha. What
sets `.y` on the CPU side is not traced. `GLOBAL_Glass_LOD5` (interiors)
does the same: programs 61 and 63 shift v by `0.25 * floor(c96[s9.y].y)`
(`g_v4PerPanelData`, the same per-panel array), 60 and 62 don't
(`out/cockpitglass/gp/`). In game, `GameVehicleResource.applyDamage` cracks
the interior glass of stock and converted cars alike (run
`host-20261023-174148`, "Interior glass" below).

**Panel slots.** Each band holds four panel slots, and stock cars map each
window over its own slot so the crack pattern and the band-3 hole fit the
window (`out/glassfix/orient.py` on 147, 176, 205, 227, 266; hole extents
from the texture's blue channel):

| Slot | Band-0 rectangle (u, v) | Band-3 hole (u, v - 0.75) | Stock use |
| --- | --- | --- | --- |
| windscreen | 0.13–0.665, 0–0.125 | 0.18–0.64, 0.012–0.108 | windscreen: u along +x, v down the glass |
| side front | 0.67–1, 0–0.125 | 0.70–0.97, 0.014–0.107 | front door glass (u along -z), headlight covers |
| rear | 0.17–0.62, 0.125–0.25 | 0.16–0.63, 0.138–0.233 | rear window (u along ±x), defroster lines in band 0 |
| side rear | 0.67–1, 0.125–0.25 | 0.70–0.98, 0.137–0.237 | rear door and quarter glass |

In every slot band 0 is flat except the rear slot's defroster lines (8%
of its texels); band 1 has 11–14% crack texels; band 2 66–92%; band 3 is
72–84% hole and 100% hole in the slot's middle, with shattered glass only
round the rim. Stock windows fill their slot (147's windscreen
u 0.130–0.665, v 0.009–0.123), so a stock window in state 3 is an empty
frame with shards at the edges.

**Converted glass** (confirmed-static; the symptom confirmed-runtime). The
converter used to squash every exterior glass triangle's Forza UV into
u 0.14–0.98, v 0.004–0.12 (`GLASS_RECT`): a flat area in band 0, so clean
glass looked right, but it spans both upper slots and keeps Forza's UV
orientation. In any crack state the slot layouts then fall across the
window. Sandbox run `20261022-191859` (`out/play/p4.sp`, ID 308, the FH1
M5 on host 147, no `--damage`, all glass on the chassis record) showed it
from the first frame of every chase shot (observe-21 to -29): the rear
window was see-through with a band of shattered glass across its lower
third. The M5's rear-window UVs run u 0.55–0.93 from the bottom edge to the
top, so in band 3 the gap between the two upper hole slots (u 0.64–0.70)
became that band (offline render of state 3: `out/glassfix/compare-states.png`,
top row, matches the screenshot). The chassis record's `.y` was 3 on an
apparently undamaged car. In run `20261022-184447` the stock CTS-V (147) and
the stock GT-R (228) also spawned with heavy cracks in their rear windows,
and 147-hosted cars were placed off the usual dyno start (x 1683.8 and
1678.7 against 1671.4), so the spawn probably knocked them. That is
unconfirmed. The bug was the misaligned pattern, not the crack state.

Every car built before the fix has the old mapping. `src/dsfforza/convert/glass.py check`
flags 38 of the 40 `out/testdrive2` cars: their glass triangles straddle the
windscreen and side-front slots. The Fiat 500 and Golf R pass only because
none of their triangles happens to cross u 0.665–0.672. `src/dsfforza/convert/glass.py
survey` (state 3, share of each panel's middle that is not a hole; stock
cars 0–9%) puts the worst at the S65 AMG (76%, several windows on the
chassis record), Supra (50%, lamp covers), Jeep Grand Cherokee (44%,
hatch glass), Range Rover (43%), Charger Daytona (25%), Civic, Silvia and
RX-7 (23–24%), Mito (21%) and the M5 (20%): `out/glassfix/survey-before.txt`.

Fix (`src/dsfforza/convert/glass.py`, `forza2dsf.add_geometry`): exterior and full-entry
glass gets planar UVs per window. Every triangle is classified by Forza
section (`glassF` windscreen, `glassR` rear, `glassLF/RF` side front,
`glassLM/RM/LR/RR` side rear, `glassT` side front, lamp covers by section or
by technique `glass`/`lights_glass` outside window sections), else by
facing and position. Each window's frame is its area-weighted normal, with
u along +x (windscreen, rear) or -z (the rest) and v down the glass. Its
extent fills the slot's band-0 rectangle (`glass.SLOTS`: windscreen
u 0.135–0.665, v 0.006–0.122; side front 0.672–0.994, 0.006–0.122; rear
0.17–0.62, 0.128–0.244; side rear 0.672–0.994, 0.128–0.244). The frame
comes from all of a section's glass triangles, so the damage converter's
per-panel calls agree.

**Interior glass** (stock layout confirmed-static; the converted result
confirmed-runtime 2026-10-23). Stock interiors (147, 227, 229, 266;
`out/cockpitglass/stockparts.py`, `stockcracks.py`, head-camera renders with
`eyeglass.py`) use only the strip u 0–0.12, in three layers per window, all
bound to that window's panel record (windscreen (0, 5), rear (0, 20), doors):

| Layer | Texture | Geometry | Visible in |
| --- | --- | --- | --- |
| surface | smear rows (u 0.005–0.12, v 0–0.057), about 0.03–0.08 UV per metre | side and rear windows whole; the windscreen only along its top (about 20%) and pillars, its centre open | states 0–2 |
| cracks | red block (u 0.01–0.056, v 0.13–0.24), each sliver about 0.02 x 0.013 of it | 3 mm wide slivers in 2–6 cm segments, rooted at the edges: windscreen 0.75–1.7 m for state 1 plus 0.6–1.2 m more for state 2, rear 0.3–0.4 m each, front doors about 0.1 m each | rows 0.18–0.25: states 1–2; rows 0.125–0.18: state 2 |
| shards | black blocks (one texel each in the upper block, a 0.03 x 0.02 patch in the lower) | jagged slivers round the rim: windscreen 600–900, doors 70–220 | state 3 |

So from the cockpit a stock windscreen has a faint smear frame when clean
(`img/fix3-desanne-stock-cockpit.png`), a few edge-rooted crack lines in
state 1, more and longer ones in state 2, and in state 3 an empty frame with
shards at the rim (`out/cockpitglass/stock227-states-big.png`). Some stock
rear-door and quarter glass (147 and 266: (19, 22), (21, 24); 229) samples
the exterior side-rear slot instead.

Converted interiors got this wrong three times. Forza UVs spread over the
smear rows drew white streaks (`img/fix3-m5-cockpit.png`,
`img/fix4-m5-cockpit-streaks.png`); fix5's black block meant no glass in
states 0–2 (`img/fix5-m5-cockpit.png`, the RX-7 report on `out/testdrive3`)
and stretched shard streaks in state 3; then a 10 x 10 texel smear patch
(u 0.0664–0.0859, v 0.0371–0.0469) was uniform in states 0–2 and gone in 3.
The panel binding was already right (`forza2dsf.interior_glass_skin` with
`--damage`: M5 windscreen (0, 5), rear (0, 20), doors (8, 9), (10, 11)), so
the crack state did reach the glass, but nothing in the patch changes
between states 0 and 2. Hence the user's report on `out/testdrive7`:
"cockpit windows always unbroken".

Fix (`glass.InteriorGlass`, used by `forza2dsf.add_geometry` when
`glass_rect=INTERIOR_GLASS_RECT`, i.e. for the interior entry): interior
glass triangles are collected per window (the `SlotMapper` groups and
frames), then emitted in the stock layout on each window's own s9:

- surface: planar, isotropic UVs over `SMEAR_RECT` (u 0.012–0.108,
  v 0.008–0.052, inset from the strip edges); windscreens of 0.3 m² or more
  keep only the part outside `WINDSCREEN_OPEN` (s 0.06–0.94, t below 0.22 of
  the window's frame), cut exactly along those lines;
- cracks: random walks (2–5 cm steps) from the rim inward, each segment a
  3 mm quad lifted onto the glass 2 mm inside it, UVs in `CRACK_LIGHT`
  (v 0.19–0.244) or `CRACK_HEAVY` (v 0.131–0.174); budgets per √m² of window
  (`CRACK_BUDGET`: windscreen 1.0 + 0.8 m, rear 0.5 + 0.4, sides 0.3 +
  0.25), state 2 lengthening most state-1 cracks first;
- shards: teeth 1.2–3.5 cm wide and 0.5–2.5 cm deep every 3.5 cm along the
  rim, deeper round a few cluster points, 60% on `SHARD_BRIGHT`, the rest
  one `SHARD_DULL` texel each. The rim is the border of the window's 1 cm
  occupancy grid with the outside (flood fill from the grid edge), so gaps
  between Forza triangles never sprout shards mid-window.

`cockpit.GlassSight` (a 128² depth cube of the opaque cockpit from the head
camera) restricts crack starts and shards to rim the driver can see: a
Forza windscreen runs well below the dash, and without it most of the
budget landed out of sight. Windows the driver cannot see keep the surface
only. Output is deterministic per car (seeded by the builder UID prefix and
the window). The per-window summary is in the car's report
(`entry.cockpit.interior.glass_windows`) and the log line `interior glass:`.
Offline (`out/cockpitglass/compare-a.png`, `compare-b.png`: stock 147 and
227, then M5, RX-7, 370Z and Challenger before and after, states 0–3 from
the head camera) the converted cockpits follow the stock progression. In
game: see `cockpit.md`, "Cockpit glass damage".

Lint (`glass.check`, so `upvh.py lint` and `port_batch`): every interior
glass triangle inside one of the regions (or one exterior slot), and an
interior with surface glass must have state-1 and state-2 cracks and rim
shards. Old builds fail the second rule.

The rear
window now shows the stock defroster lines when clean. Rebuilt M5, S65, Supra and Jeep (`out/glassfix/`, IDs
304–307): all glass inside one slot (`glass check`); state 3 renders as an
empty frame with a shattered rim like stock 147
(`out/glassfix/compare-states.png`, `jeep-compare.png`, `s65-compare.png`;
`out/glassfix/glassrender.py` draws a state). The classifier gives every
glass section of the 40 testdrive cars its own non-degenerate frame
(`out/glassfix/slot_survey.py`). In game: the clean rear windows of the
DMC-12 runs below.

Lint: `upvh.py lint` (and so `port_batch`) now runs `glass.check`. It fails
when converted exterior glass has v outside 0–0.25 or a triangle not inside
one slot.

**Cracked glass on undamaged ports (DMC-12, `out/testdrive4`).** In run
`host-20261023-082346` (`observe-06`) the DMC's rear window (chassis record
(0, 0), rear slot) and its tail-lamp lenses (then glass on the rear bumper
record (21, 25)) showed band-1 cracks. The same archive and five rebuilt
variants showed a clean rear window in runs `-083742`, `-090511`, `-090652`,
`-090833` and `-091014` (`out/dmc/rearwindows.png`), so the converted window
itself is clean (band 0, inside its slot; `glass.py check`), and the crack
state is per spawn. Binding glass to the chassis record is stock practice
(63 stock vehicles, 19,222 full-entry triangles; `out/dmc/glassroles.py`),
as is binding to bumpers (228's tail-lamp covers). What raises `.y` at spawn
is still untraced (the harness zaps into cars in traffic; the vE run crashed
into a tanker within seconds). The lamp-lens part was a converter bug:
coloured rear lenses no longer become glass (`lights.md`, "Coloured lamp
lenses drawn as glass").

**Exterior materials.** Stock clean exterior entries draw with the `_LOD1`
shared materials (`GLOBAL_Chassis_LOD1` `10122b9653b14e2a` in 149 of 150,
`GLOBAL_Chassis_Simple_LOD1` `10122b97…` 149, `GLOBAL_Glass_LOD1`
`10122b99…` 145, `GLOBAL_Tyre_LOD1` `10122b9b…` 150; the full-entry ones in at
most 8 vehicles, a few hundred triangles; `out/dmc/extmats.py`). Converted
exteriors used the full-entry materials; they now use the twins
(`forza2dsf.SHARED_EXTERIOR`, same strides and MODL flags). The `_LOD1`
paint shader (program 26) is program 27 without the damage terms, with the
same fresnel. No visible change on the DMC (`host-20261023-092015`); done for
fidelity with stock.

Stock glass binding: semantic 9 on glass is mostly the glass panel's
record (Desanne windscreen (0, 5) = record 29, rear window (0, 20) =
record 61, both type 1 role 8; door glass on the door records, lamp glass
on the lamp records), in the full, interior and exterior entries alike.
Some stock glass is on the chassis (0, 0): 147's quarter lights and
sunroof, 227's hatch. Converted glass is on the chassis unless `--damage`
(each window on its panel: `damage.py`, glassF/glassR to the host's front
or rear role-8 record, side glass to its door) or `--glass-links`
(windscreen and rear window only). Chassis-bound glass shows the chassis
record's crack state. The 20-byte buffer is the wheel, centred on
the origin (±0.13 m wide, 0.33 m radius). Body parts are stored in car
space, not part space.

## MODL: parts and LODs (confirmed-static)

In `VEHC/NONR/MODL`:

```text
header   {u32 part_count, u32 lod_count, u32 lod_table_offset, u32 0}
part     x part_count, 0x60 bytes each:
           f32 origin[3], pad, f32 axis_a[3], pad, f32 axis_b[3], pad, f32 axis_c[3], pad
           u64 part_uid, u32 lod_count, u32 lod_offset, u32[3] 0, u32 (5; 1 for empty parts)
lod      x lod_count, 16 bytes: {u32 mask, u32 submodel_count, u32 uid_list_offset, u32 extra_offset}
u64[]    submodel UIDs (all 243, in DRSP order)
u32[]    one value per submodel, meaning unknown
```

Part 0's UID equals the model UID in UPVH's AWHF header, so MODL parts and
UPVH hierarchy parts presumably share UIDs. Part UIDs are unique across the
whole stock archive (6,139 in 450 entries) and the engine keys a global table
by them; clones must not reuse the host's (`upvh.md`). The oriented boxes give each
part's extent: part 0 is the whole car (5.14 × 2.08 × 1.51 m), parts 3, 5,
17, 19 are 1.39 × 1.3 m (the four doors), part 31 is the wheel.

## Materials (confirmed-static)

```text
MACO  material
  MAHE  {u64 material_uid, u32 render_method_count, u32 0}
  MATX  source path, e.g. R:\Resources\Vehicles\Vehicles\ASYM_Desanne_2007_Civ\Textures\..._Chassis.matx
  MARC  x render_method_count
    MARH  (0x28) {u32 technique_hash, ..., u32 parameter_count}
    MARP  x n: {char name[32], u8 value[16], u32 type}
TEXC  texture
  UIDC  u64 texture_uid
  DXTC  a complete DDS file (DXT5 512x512 with mips in Saloon 15)
```

MARP types: 1 float, 3 vec4, 9 texture UID (e.g. `g_texDiffuseMap` =
the TEXC's UIDC), 10 sort mode, 11 render state (`rs_cullmode`), 16 sampler
state (`ss_minfilter`...). Parameters seen: `g_hSpecularIntensity`,
`g_hSpecularPower`, `g_shadowCastOffset`, `g_shadowReceiveOffset`,
`g_texDiffuseMap`, `g_vec4FinalTintColour`, `SortMode`.

**Shared materials.** Of 243 submodels, 78 use the car's two local
materials; 165 use material UIDs (e.g. `001283358afd1027`, 72 submodels)
that appear thousands of times across all 150 cars. They are defined once,
in the archive's shared chunk `SSIC/MDXN` (root entry 450): 23 `GLOBAL_*`
materials (`R:\Resources\Vehicles\Vehicles\_Global\Textures\GLOBAL_Chassis.matxml`
= `001283358afd1027` paint, `GLOBAL_Glass*`, `GLOBAL_Chassis_Chrome`,
`GLOBAL_Chassis_Simple` = TRIM, `GLOBAL_Tyre`, `GLOBAL_Mirror`,
`GLOBAL_Wheel_Blur*`, `GLOBAL_carbon_fibre*`, `GLOBAL_props*`) and 11 textures,
next to 198 GPU programs. An earlier note here said they were defined in no
game file; that search missed SSIC (confirmed-static). A converter can
assign them by role and only author materials for textured parts.

## Paint (confirmed-static)

`NONR/SPSR`: `SPID` (vehicle ID), then shader parameter sets. In Saloon 15,
the first set has 10 entries, each `g_vec4TintColour1_Alpha` and
`g_vec3TintColour2`: the paint palette. Others hold `g_shadowDepthOffset`
and `g_vec4WheelBlurColour`.

## Open

1. Answered: semantics 8 and 5 (`damage.md`), 9 (`cockpit.md`), 12 the
   crushed position (`damage.md`); the fourth position component is baked
   sky light (above).
2. Answered above ("v' = v + 0.25 * floor(...)": a UV band per crack state
   of the record s9 byte 1 names); still open: what sets `.y` on the CPU
   side.
3. The per-submodel u32 list after the UID list in MODL.
4. MARH fields besides the technique hash; which technique is which pass.
5. How UPVH parts map onto MODL parts and their submodels: answered in
   `damage.md` (part 0 = all panels merged, one part per panel; detach untraced).
6. Answered: the shared vehicle materials are defined in `SSIC/MDXN`
   ("Shared materials" above).
