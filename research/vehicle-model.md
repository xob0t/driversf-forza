# Vehicle model format (VEHC → MDXN/NONR)

Updated: 2026-10-29

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
                  (+0x18 is 0 in all 43,790 vanilla SBBHs; the engine
                  reads it as a second vertex stream UID, `upvh.md`)
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
0–1** (confirmed-static by correlation; that it drives the lighting
confirmed-runtime by the fix below). On the Desanne it follows the
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
0.48 (full and exterior) and 0.20 to 0.25 (interior), close to vanilla. In
game the paint is coloured again and the 370Z's dials are lit (run
`20261022-023610`, `in-game-tests.md`, "Fix2 in game"; confirmed-runtime).

**Semantic 12** is the vertex's fully crushed position as float16 (x, y,
z, 1.0); the shader draws lerp(position, s12, panel damage). It equals the
position only where nothing crushes (glass, cabin, about a third of the
vertices); an earlier note here said it was always the position. Details:
`damage.md`.

**Glass UVs live in v 0–0.25** (confirmed-static). Every vanilla glass
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
| u 0–0.12, v 0–0.06 | grey smear (mean (57, 139, 90): alpha 0.35, reflective), the same in bands 0–2, a hole in band 3 | interior windows: 9% of vanilla interior glass triangles but 64–97% of each window's area (147, 227, 266; `out/glassfix/intglass.py`) |
| u 0–0.12, v 0.06–0.125, and u 0.06–0.12, v 0.125–0.25 | flat black (13, 0, 0): blue 0, discarded by the pixel shader in bands 0–2; in band 3 smear-coloured (upper block) and bright streaky shards (94, 189, 173, lower block) | interior rim shards, visible in state 3 only: 66% of vanilla interior glass triangles (3 mm slivers; 3–34% of the area) |
| u 0–0.06, v 0.125–0.25 | flat dark red (66, 0, 0), blue 0 in band 0; streaky (blue ~170) in band 1 from v 0.18 down and in all of band 2; black in band 3 | interior crack lines: rows 0.18–0.25 show in states 1 and 2, rows 0.125–0.18 in state 2 only (9% of interior glass triangles) |

`GLOBAL_Glass_LOD5` reflects far more than the exterior glass
(`g_hReflectionAmount` 1.0, `g_hReflectionFresnelBias` -0.5,
`g_hSpecularPower` 150; `GLOBAL_Glass` 0.25, -0.1, 1500), so interior glass is
a dark, reflective layer. Vanilla interior windscreens are dense shard meshes
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
the interior glass of vanilla and converted cars alike (run
`host-20261023-174148`, "Interior glass" below).

**Panel slots.** Each band holds four panel slots, and vanilla cars map each
window over its own slot so the crack pattern and the band-3 hole fit the
window (`out/glassfix/orient.py` on 147, 176, 205, 227, 266; hole extents
from the texture's blue channel):

| Slot | Band-0 rectangle (u, v) | Band-3 hole (u, v - 0.75) | Vanilla use |
| --- | --- | --- | --- |
| windscreen | 0.13–0.665, 0–0.125 | 0.18–0.64, 0.012–0.108 | windscreen: u along +x, v down the glass |
| side front | 0.67–1, 0–0.125 | 0.70–0.97, 0.014–0.107 | front door glass (u along -z), headlight covers |
| rear | 0.17–0.62, 0.125–0.25 | 0.16–0.63, 0.138–0.233 | rear window (u along ±x), defroster lines in band 0 |
| side rear | 0.67–1, 0.125–0.25 | 0.70–0.98, 0.137–0.237 | rear door and quarter glass |

In every slot band 0 is flat except the rear slot's defroster lines (8%
of its texels); band 1 has 11–14% crack texels; band 2 66–92%; band 3 is
72–84% hole and 100% hole in the slot's middle, with shattered glass only
round the rim. Vanilla windows fill their slot (147's windscreen
u 0.130–0.665, v 0.009–0.123), so a vanilla window in state 3 is an empty
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
apparently undamaged car. In run `20261022-184447` the vanilla CTS-V (147) and
the vanilla GT-R (228) also spawned with heavy cracks in their rear windows,
and 147-hosted cars were placed off the usual dyno start (x 1683.8 and
1678.7 against 1671.4), so the spawn probably knocked them. That is
unconfirmed. The bug was the misaligned pattern, not the crack state.

Every car built before the fix has the old mapping. `dsff glass check`
flags 38 of the 40 `out/testdrive2` cars: their glass triangles straddle the
windscreen and side-front slots. The Fiat 500 and Golf R pass only because
none of their triangles happens to cross u 0.665–0.672. `src/dsfforza/convert/glass.py
survey` (state 3, share of each panel's middle that is not a hole; vanilla
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

**Interior glass** (vanilla layout confirmed-static; the converted result
confirmed-runtime 2026-10-23). Vanilla interiors (147, 227, 229, 266;
`out/cockpitglass/vanillaparts.py`, `vanillacracks.py`, head-camera renders with
`eyeglass.py`) use only the strip u 0–0.12, in three layers per window, all
bound to that window's panel record (windscreen (0, 5), rear (0, 20), doors):

| Layer | Texture | Geometry | Visible in |
| --- | --- | --- | --- |
| surface | smear rows (u 0.005–0.12, v 0–0.057), about 0.03–0.08 UV per metre | side and rear windows whole; the windscreen only along its top (about 20%) and pillars, its centre open | states 0–2 |
| cracks | red block (u 0.01–0.056, v 0.13–0.24), each sliver about 0.02 x 0.013 of it | 3 mm wide slivers in 2–6 cm segments, rooted at the edges: windscreen 0.75–1.7 m for state 1 plus 0.6–1.2 m more for state 2, rear 0.3–0.4 m each, front doors about 0.1 m each | rows 0.18–0.25: states 1–2; rows 0.125–0.18: state 2 |
| shards | black blocks (one texel each in the upper block, a 0.03 x 0.02 patch in the lower) | jagged slivers round the rim: windscreen 600–900, doors 70–220 | state 3 |

So from the cockpit a vanilla windscreen has a faint smear frame when clean
(`img/fix3-desanne-vanilla-cockpit.png`), a few edge-rooted crack lines in
state 1, more and longer ones in state 2, and in state 3 an empty frame with
shards at the rim (`out/cockpitglass/vanilla227-states-big.png`). Some vanilla
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
frames), then emitted in the vanilla layout on each window's own s9:

- surface: planar, isotropic UVs over `SMEAR_RECT` (u 0.012–0.108,
  v 0.008–0.052, inset from the strip edges); windscreens of 0.3 m² or more
  keep only the part outside `WINDSCREEN_OPEN` (s 0.06–0.94, t below 0.22 of
  the window's frame), cut exactly along those lines;
- cracks and shards: those of the vanilla interior window that matches the
  window best (`glass.donor_library`; #459, "Which vanilla window a converted
  window takes its set from" below). Until 2026-11-19 they were the host
  interior's own (`glass.host_windows`).

Every host triangle that lands on its window is kept, whether or not the
driver's eye sees it: the port draws the layers its host draws, and the
host carries them on its rear window too (135 roster hosts below). (The
first version of this rule ran them through a depth-cube test from the head
camera, `cockpit.GlassSight`, which master had used to place its generated
rim shards. On the full roster it dropped every host layer of 135 cars' rear
windows; with it off, all 16 rebuilt took their host's rear layers, 49 to 755
triangles. The test and its plumbing are gone.) The per-window summary is in the car's
report (`entry.cockpit.interior.glass_windows`: `host_window`,
`crack_triangles`, `shard_triangles`) and the log line `interior glass:`.
In game: see `cockpit.md`, "Cockpit glass damage".

Lint (`glass.check`, so `upvh.py lint` and `port_batch`): every interior
glass triangle inside one block of the strip (or one exterior slot), and an
interior with surface glass must have crack or shard triangles, unless no
window took any (`port_batch.plain_glass`).

**The strip's blocks** (confirmed-static, from the texture,
`out/i459` in the #459 worktree). The strip is 64 texels of the 512-wide
texture, cut on 32/64-texel lines. Share of each block's texels that survive
the texkill (blue ≥ 0.01) in states 0/1/2/3:

| Block | u | v (band 0) | 0 | 1 | 2 | 3 |
| --- | --- | --- | --- | --- | --- | --- |
| surface (smear) | 0–0.125 | 0–0.0625 | 1.00 | 1.00 | 1.00 | 0.01 |
| shard_dull | 0–0.125 | 0.0625–0.125 | 0.04 | 0.03 | 0.04 | 0.96 |
| crack | 0–0.0625 | 0.125–0.25 | 0.01 | 0.55 | 1.00 | 0.00 |
| shard_bright | 0.0625–0.125 | 0.125–0.25 | 0.01 | 0.03 | 0.09 | 0.94 |

Fleet survey (`fleet.py`, every vanilla interior): 237,817 of 240,822
interior glass triangles lie inside one block; the other 3,005 are on
exterior slots (quarter and rear-door glass) or straddle. 143 of 150
interiors have crack and shard triangles on at least one window, each window
on its own s9 group; 6 have no interior glass.

**Shattered windscreens from the cockpit (#459, 2026-11-13).** The report
(Gallardo LP570 port, spool 474 on host 212; vanilla Charger 176 for
comparison) showed spiky shards along the A-pillar. Cause (confirmed-static):
the converter generated the cracks and shards (random-walk cracks, then
teeth 1.2–3.5 cm wide and 0.5–2.5 cm deep, up to 8.5 cm near "cluster
points", every 3.5 cm all round the rim). Vanilla's are different: on the
vanilla 212 windscreen 354 shard triangles, 3 mm wide at the median (p90
10 mm), longest edge 2.2 cm (p90 5.5 cm), in a few irregular clusters at
the corners and edges; the master port had 62 teeth 17 mm wide in a regular
comb (`out/i459/ws-ortho.png`, states 0–3 drawn on the window's plane;
`eye-gallardo.png`, `eye-m5.png` from the head camera). No windscreen moves
its vertices: interior glass has s12 = position on vanilla 212, 176, 147,
227, 266 and on the ports, and windscreens sit on the chassis transform
(slot 0), so the damage is texture only on both.

Rule (`glass.InteriorGlass`): each converted window takes the crack and
shard triangles (crack, shard_dull and shard_bright blocks) of the host
interior's matching window. A pair is of one kind: side glass (most of the
window's area projects onto the y-z plane) on the same side of the car; the
windscreen with the host's windscreen (its frontmost other window by centre z;
vanilla interiors don't put it on a fixed side of z = 0: +1.0 on the 212, -0.04
on the 248); the rear window with the host's rear glass. Roof glass takes none.
Nearest centres pair first, and each host window goes to one window only (the cockpit's sections
first, then the shell's): vanilla windows carry their own, and the vanilla
212's quarter lights have none. The host triangles move in pieces
(triangles that share a vertex, `glass._pieces`; "Broken glass moves in
pieces" below), each triangle keeping its shape, UVs and sky terms. A
piece's centroid goes in the same direction from the window's box centre,
in units of the box's half extents (so corners go to corners), and the same
depth inside the rim, in metres, along that direction (the rim per 2°
direction, `_Rim`). The piece turns with the direction as one body and its
triangles are lifted onto the converted glass, on the window's own s9. A
piece is left out when its depth is past the centre of a smaller window; a
triangle, when it reaches further past the window's glass than it reached past
its own window's glass (until 2026-11-19: when its centroid landed outside the
window or a corner more than one grid cell (1 cm) outside it; "How far a set
reaches past its window" below). Windows the host has no crack
and shard triangles for kept the surface only (the vanilla
Challenger 172's cockpit has no side glass; the Charger 176's rear side
glass is smear only). The placement still holds; which window the set comes
from is now chosen among every vanilla interior's ("Which vanilla window a
converted window takes its set from" below).

**Broken glass moves in pieces** (confirmed-static, 2026-11-14; owner's
report on the installed master 73830ef4: the Solstice GXP port, spool 620 on
host 278, broken windscreen from the cockpit, "broken glass is lumped
together"). The #459 rule above first moved each host triangle on its own:
its centroid by direction and depth, then turned by the change of direction
at that centroid. A shard cluster or crack line is many triangles sharing
vertices, and neighbours sit in different 2° directions at different depths,
so they moved and turned apart: shards fanned into spiky stars and crack
lines broke into scattered slivers (`out/ws/ws3.png`, `eye3-s23.png` in the
fix worktree: master, fix and vanilla 278 drawn in states 0–3).

Evidence from vanilla (`out/ws/shared.py`, `rigid2.py`, `shape.py`): 150
vanilla interiors have 130 distinct windscreen crack/shard sets, and many
windscreens share most of their triangles with another (same UV triples;
5,789 pairs with 100 or more shared and a median move of 5 mm or more). DSF
moved them between windows as pieces: vertices two triangles share on one
window stay shared on the other (spread median 0.0, p99 1.1 mm), and each
piece (triangles joined by shared vertices, 21 per windscreen at the median)
moves rigidly (rigid fit residual median 2.4 mm, p90 11 mm). Applied to the
same pairs, the per-triangle rule tears those vertices apart (median 5.7,
p90 29, p99 120 mm) and bends the pieces (shape residual median 9.0 mm, p90
34 mm); moving each piece as one body by the same direction-and-depth rule
at its centroid gives 2.6 mm (p90 15 mm). Where pieces land on the window is
the same as before (median vertex error over the pairs 70 to 74 mm for every
placement tried: per triangle, per piece, anchored at the piece's rim
vertex, or box-normalised), so only the pieces' shape changes, not where
they sit. The clusters at the corners and edges are the host's own: vanilla
278 shows the same five clusters in state 3 (`ws3.png`).

**Which corners are one vertex** (confirmed-static, 2026-11-14;
`out/ws/piece_tol.py`, `piece_pairs.py` in the #623 worktree). Interior
positions are int16 x, y, z in steps of 1/4096 m (0.24 mm;
`vehicle_mesh.POSITION_SCALE`). On the 528 host windows with crack and shard
triangles (150 vanilla interiors), corner pairs of different triangles within
three steps of each other in 3D: 1,350,697 decode bit-identical, 11,272 differ
by one step on some axis, 4,535 by two, 3,675 by three. Whether such a pair is
one vertex shows on the 5,789 windscreen pairs above: the same two corners on
the partner window.

| on window A | pairs | in-plane spread on B, median / p90 / p99 | > 5 mm apart on B |
| --- | --- | --- | --- |
| bit-identical | 7,409,923 | 0 / 0 / 0 mm | 0.2% |
| one step | 67,437 | 0 / 0.26 / 0.53 mm | 0.5% |
| two steps | 17,568 | 0 / 0.60 / 112 mm | 2.8% |
| three steps | 8,978 | 0 / 1.2 / 193 mm | 3.1% |
| within 0.5 mm in the plane only: crack over shard | 3,630 | 84 / 1,002 / 1,275 mm (crack over bright) | 95% |
| the same: crack over crack (3 mm apart along the normal) | 69,849 | 0.62 / 1.5 / 3.2 mm | 2.2% |
| the same: shard over shard (3 mm apart along the normal) | 5,460,853 | 0.32 / 0.79 / 2.7 mm (bright over bright) | 0.6% |

A one-step pair is bit-identical on the partner window: the same vertex,
rounded on one side. From two steps on, pairs come apart. Layers stacked
along the normal mostly travel together but are not one vertex; they join a
piece only through the vertices they share. So two corners are
one vertex when no coordinate differs by more than one step (`glass._pieces`,
`PIECE_STEP`). The PR's first rule (corners within 0.5 mm after projecting
onto the window's plane) also joined crack triangles to shard triangles that
only overlap in projection and move apart on other windows. Pieces over the
528 windows: 4,536 by that rule, 4,654 by the one-step rule, 4,872 by
bit-identical corners only; the two rules group 204 pieces (4,944 of 216,181
triangles) on 66 windows differently.

Survey (`out/ws/tear.py`: each port crack/shard triangle matched to its host
triangle by UV triple, then every host vertex two triangles share measured on
the port): on the installed master roster (721 cars) 2,406 of the 2,424
windows with host layers, on 683 cars, had shared vertices torn more than
1 cm apart (193,225 of 577,282; max per window median 73 mm, up to 15 cm);
every one of the 680 windscreens. After: 0 (the four test cars: max 0.4 mm,
`tear-4fix.json`).

Not reproduced: the owner's shot also shows a grainy, milky surface over the
whole windscreen. In state 3 the smear block is texkilled (table above) and
the converted surface is gone on master as on vanilla (offline, `ws3.png`
state 3). In game the glass ladder's front hits lift the bonnet over the
windscreen in state 3 on the port and on vanilla 278 alike (VM run
`vm1-20261114-134333`), so the run doesn't show it; in states 0–2 the port's
windscreen centre is clear there. The owner's game runs DLSS 5
(`dsf_enhanced.ini` `[DLSS5] Helper=1`), which the VM guests have off. Crack layers at state 0 stay hidden (texkill; `ws3.png` state 0).

**Crack and shard fins** (confirmed-static, 2026-11-18; the in-game result
below). Owner's report on the installed master 53b1a6c5: the BMW M5 2012
port (spool 35, `forza.fh1.BMW_M5_12`, host 147), damaged, from the cockpit:
the crack and shard layers draw almost fully transparent, only faint shard
outlines, where vanilla cars show white crack lines and frosted shards.

What the port's layers share with vanilla's (`out/i459c/cmp.py`, `attrs.py`
in the fix worktree): the material (`GLOBAL_Glass_LOD5`, shared SSIC, so the
same texture `00122b77dffa1227`, sampler and render states:
`rs_alphablendenable` 1, `rs_zwriteenable` 0, `rs_cullmode` 0, `SortMode` 1),
the blocks and UVs (copied), the s9 records ((0, 5), (0, 20), (8, 9),
(10, 11), as on 147), s12 = position, the sky terms (the glass programs 61
and 63 don't read them: their vertex shader takes only position xyz). The
vertex shader reads per vertex position, UV0, s9, the normal (semantic 5)
and s12; the pixel shader `texkill`s blue < 0.01, takes alpha
max(texel blue, specular x green) and lights the texel with the normal
(diffuse N.L, ambient by N, Fresnel 1 - N.V, the reflection vector).
So the texture is not the cause; the geometry and the normal are.

Vanilla's crack lines and bright shards are fins standing across the glass,
not flat slivers on it. Of each window's crack and shard_bright triangles,
all face within about 20 degrees of the glass's plane: |cos| between the
triangle's facing and the window's normal at the median 0.02 to 0.35 on 147
and 176 (`tilt.py`; shard_dull lies flat, |cos| about 1, but on the 147
windscreen). They stand 0 to 6 mm outward of the window's smear surface on
flat windows (side and rear: p5 about 0, median 1 to 4 mm, p95 4 to 6 mm;
`depth.py`). The bright shards are extruded slabs: their walls' across-glass
edge is the shortest edge of each shard_bright triangle and one length per
window (median 3.1 mm on 153, 3.0 on 132, 5.2 on 201, 4.1 on 286, 3.2 on
147, 3.1 on 176, p5 to p95 within 0.6 mm; `tilt153.py` in `out/i459d`), and
shard_dull is the slabs' bottom face (on the 147 windscreen, which has no
shard_bright, the dull triangles are the walls). A crack line seen from the
eye is the fin's face at a grazing angle: a thin line.

`InteriorGlass._place` lifted every corner onto the converted glass (LIFT
inside it), so a fin's root and top landed on the same point of the glass
and the fin collapsed into a line of no area. Area of the layers, vanilla
host against the master port (`areas.py`): crack 87.5 cm² on 147 against
13.9 on the M5 port; shard_bright 134.8 against 23.0; on the 911 GT2 (host
241) 123.8 / 24.5 and 395.9 / 43.1; Durango (138) 112.3 / 13.3 and 356.5 /
28.7; MT900S (162) 152.8 / 16.6 and 671.5 / 155.6. Flat shard_dull kept its
area (1,318 / 995 on the M5). What was left drew as the faint outlines in
the owner's shot.

Rule (`glass._fin_heights`): each host vertex keeps its height above its
fin's root, along the window's outward normal: how far it stands outward of
the nearest corner it shares a triangle with (0 where that corner is not
lower). A wall's top has its root as its nearest corner, a root its top.
The placed corner goes that far outward of where the lift puts it.

The first rule (58977a25) took the innermost host vertex in the vertex's
1 cm grid cell and the eight round it. Where no lower vertex lay that close
the vertex was its own floor and kept no height (CodeRabbit on #625; 39,549
of 541,737 crack and shard_bright vertices on vanilla IDs 1 to 300 are their
own floor while their triangle's corners have a lower floor). And the
reference was not the glass under the point: across 1 to 2 cm it took the
glass's slope wherever the window tilts away from its frame's plane. Some
windscreens wrap round the pillars (the smear's facing has |cos| to the
frame's normal 0.10 at p5 on 201, 0.30 on 153, 0.33 on 132), and there a
wall's root and the glass 2 cm along it differ by more than the wall.

Check (`survey4.py` in `out/i459d`, 140 vanilla interiors, 518 windows with
layers, 541,737 crack and shard_bright vertices): the local glass normal at
a vertex is the facing of the nearest flat-layer triangle (smear or
shard_dull); a vertex is a top when an edge of its crack or bright-shard
triangles, steeper than 45 degrees to that local glass (edge angles are
bimodal, the trough at 35 to 50 degrees: `edges.py`), drops from it by more
than 0.5 mm, and its true height is that edge's drop along the window's
normal; otherwise it is a root, at 0. 267,965 tops, 273,772 roots:

| | grid floor (58977a25) | fin root |
|---|---|---|
| off by > 0.5 mm | 237,201 (44%), p50 1.5, p95 7.8, max 170 mm | 5,651 (1.0%), p50 2.8, p95 4.6, max 24 mm |
| roots raised > 0.5 mm | 119,376, p95 7.7 mm | 2,954, p50 2.5, p95 4.0 mm |
| tops left at 0 | 136 | 2,016 (0.75%), losing p50 3.1, p95 4.8 mm |
| over 10 mm | 10,831 (132, 153, 284, 201, 205, 286 windscreens) | 15 |

Heights (p50 / p95 / max, mm), fin root against the check: crack 0.0 / 4.2
/ 16.8 (check 0.0 / 4.2 / 10.5), shard_bright 0.0 / 3.5 / 23.6 (0.0 / 3.6 /
9.2), shard_dull 0.0 / 0.2 / 50.3; grid floor crack 2.1 / 4.7 / 45.3,
shard_bright 2.9 / 6.9 / 172.0, shard_dull 0.5 / 5.2 / 142.9.

What the fin root still misses. Tops left at 0 are the tips of narrow
shards whose top edge is shorter than the slab's wall (on 200's side windows
the wall is 4.5 mm and the top edge 3.5 to 4 mm: the top's nearest corner is
the other top); 33 to 45 per window on 200, 203 and 258's side glass, at
most 28 elsewhere. The steepest edge instead of the nearest corner leaves
1,126 tops at 0 but is off on 7,491 vertices and puts 2,174 over 10 mm on
the wrapping windscreens: along glass edge-on to the frame's normal an edge
along the glass is the steepest. Raised roots and the 15 over 10 mm are corners whose
nearest corner is far along tilted glass: 149's windscreen at the A-pillar
(a shard corner 25 mm from its nearest, the glass there nearly edge-on to
the frame: 23.6 mm), a 181 mm crack on 128 (16.8 mm, 5 degrees of tilt),
201's cracks (10.5 mm, which is their true height). The error is at most the
nearest corner's distance times the glass's tilt there.

In game (confirmed-runtime, VM runs `vm1-20261118-231529` master and
`vm1-20261118-231941` this rule, `-GlassLadder`; IDs 1 M5 2012 on 147, 2
911 GT2 1995 on 241, 3 Corvette Grand Sport 1996 on 161, 4 MT900S on 162, 5
Durango on 138, vanilla 147): on master the Durango's crack lines at 0.25
were a faint trace and its shards at 0.5 almost nothing; with the rule its
crack lines run in from the A-pillar and its shards show as frosted clusters
with bright edges along the windscreen's top (observe-76, -78 against master
-78, -79). The MT900S's top-edge cluster at 0.5 the same (-64 against -65).
The 911's state-1 crack line was a wide grey band on master (a fin laid
flat) and is a thin line now (-35), as vanilla 176's (`vm1-20261114-134333`
observe-72). Vanilla 147's own layers are faint in these VM shots too
(observe-08 to -10: a hairline at 0.25, clusters at the top corners at 0.5
and 1.0). The M5 port's layers, at its windscreen's top edge like 147's,
stay above the probe's cockpit frame, so the M5 shows no change there; its
offline numbers are above. The Corvette's host 161 has no windscreen layers:
unchanged.

**Crack and shard normals** (confirmed-static). Vanilla stores on every
crack and shard vertex the facing of its own triangle: over 144 vanilla
interiors (`fleetnormals.py`), the stored normal (semantic 5, decoded as
the vertex shader does) is within 0.9 of the triangle's winding normal on
99.3% of crack vertices, 97.9% of shard_dull and 94.0% of shard_bright. On
the 147 windscreen two thirds of the shard_dull triangles face into the
cabin. The master port wrote the converted glass's own normal on all of
them (the window's outward side; agreement with the facing 0.15 to 0.68 per
layer on the M5, 0.00 to 0.41 on the others). Rule (`glass._turn_normal`):
each vertex keeps the host's normal, its components along the host
window's frame turned with its piece and put on the converted window's frame
(reversed with the facing if the frames' handedness differs; host
triangles without normals use their own facing). After: crack 0.87 to 0.98,
shard_bright 0.55 to 0.97, shard_dull 0.57 to 0.95 (vanilla hosts 0.57 to
1.00).

A cockpit carbin can name a window glass<suffix> where the main carbin says
lexan<suffix> (the MC12: cockpit glassF/LF/RF, body lexanF/LF/RF).
`interior_glass_skin` gives the cockpit pane the lexan section's panel binding
(the same window, #411), so its layers follow the windscreen's crack state,
not the chassis'.

**Which vanilla window a converted window takes its set from** (#459,
2026-11-19; scripts in `out/i459m` of the PR's worktree). The rule above
paired each converted window with its host's window of the kind, nearest
centre first. Where the two differ in shape, the pieces crowd, spread or fall
out; where the host has no set for the kind, the window gets none. Survey of
the roster without converting (each port's converted windows rebuilt from its
latest car-store chunk, 3,955 windows on 692 cars; `ports.py`, `portwin.py`,
`survey.py`): on the 2,201 windows with a host set, the move of the set's
corners (below) is 58 mm RMS at the median, 197 mm at p90, 700 mm at most, and
2,972 triangles go past a smaller window's centre. 161's interior has no sets
at all, 172's cockpit no door glass, 205 and 208 no rear sets, so the
windscreen, doors or rear window of their ports stayed bare.

Vanilla's sets belong to windows, not to cars (`share.py`: two windows share a
triangle when its UV triple and its edge lengths to 1 mm match). Pairs of
vanilla cars whose windscreens share 80% or more of their triangles share
their rear (12 of 12) and side sets (15 of 22, 14 of 16) too: copies of one
interior (variants of a model). Where DSF adapted a windscreen set to another
window (50 to 80% shared), the two cars share their rear set in 17 of 204
pairs (8%) and their side sets in 9 to 10%, against 1.4 to 2.9% for cars whose
windscreens share nothing. No vanilla interior carries one set on two windows.
So each converted window chooses its set on its own, and each vanilla window
goes to one window of a port.

Which vanilla windows carry a set (`vanilla.py`, 150 interiors; the driver's
eye from `cockpit.head_camera`, 147 of them):

| window | with a set | without |
| --- | --- | --- |
| windscreen | 141 | 3 (161's, smear only; one on the exterior slots; one other) |
| rear window | 117 | 21 (10 smear only, 11 on the exterior glass's slots) |
| side glass reaching the eye or ahead of it (front edge at or ahead of the eye's z) | 267 | 19, all on the right (passenger) side but 258's sliver: 126, 127, 129, 130, 149 (two), 156, 159, 160, 167, 189, 242, 246, 284, 285, 287, 290, 291 (15 smear only, 3 on the exterior slots) |
| side glass wholly behind the eye | 2 (130's rear sides) | 72: 66 on the exterior slots, 4 smear only, 2 other |
| one record holding both sides' quarter lights | 1 | 39 |

So DSF gives a set to the windscreen, the rear window and the side glass
beside or ahead of the driver, as a rule, and not to the glass behind the
driver's head, which it mostly draws with the exterior slot's cracks instead.
Where a vanilla car lacks one (161, 172's door glass, a van's passenger door),
it is that car's, not a rule of the window: the 18 bare windows ahead of the eye are the passenger side of 17 cars, 16 of which have a set on the driver's side. Forza's section names don't say
which side glass is beside the driver: on cars with a vent the vent is glassLF
and the door glassLM (Aston Martin V8 Vantage '77, Barracuda '68); over the
roster's converted side glass, glass*F is at or ahead of the eye on all 1,094
windows, glass*R behind it on 871 of 905, glass*M on 133 of 280 (doors) and
behind on 147 (rear doors, quarter lights).

Rule (`glass.InteriorGlass._kind`, `_match`, `glass.mismatch`,
`glass.donor_library`): every vanilla interior window with a set is a donor
(527: 141 windscreens, 117 rear windows, 141 left and 128 right side windows;
the one two-sided record is not, its pieces would land on one window from two).
A converted windscreen, rear window, or side glass whose front edge is at or
ahead of the driver's eye (`Donors.eye_z`, the Forza eye placed) takes the set
of the donor of its kind (and side) that the move changes least; side glass
behind the eye and roof glass take none. Pairs go best first and each donor to
one window of the port.

The measure is the RMS distance the move (`_carry`, the placement above) moves
the donor's crack and shard corners, in metres, relative to the two windows'
box centres. A piece moves by its centroid's shift d and turns by the change of
its direction's angle phi, so over its corners (mean offset 0 from the
centroid) the mean squared move is |d|^2 + 2(1 - cos phi) mean |r|^2:

- d holds the rim reach per direction in metres: the piece keeps its depth
  inside the rim, so it moves exactly by the difference of the two windows'
  rims in its direction (and with the box, by their size and aspect); a donor
  of the window's own shape moves nothing;
- the turn term holds the pieces' turning where the aspect differs (exact, but
  it changes no choice: the held-out ranks below are the same without it);
- dropped pieces need no term of their own: a piece is dropped when its depth
  is past the window's centre, and the same formula counts the move that takes
  it there;
- area is not a term: two windows of equal area can differ in reach in every
  direction, and area alone ranks DSF's own reuse worse (below).

Held out (`heldout.py`): each vanilla window with a set, as the converted
window, ranks every other car's window of its kind and side; where are the
windows DSF shared its set with? Rank 1 and the partner's rank (percentile of
the candidates), by how much of the set the pair shares:

| shared | windows | partner first (chance) | partner's median rank |
| --- | --- | --- | --- |
| 80 to 100% (copied interiors) | 150 | 85% (1.0%) | 0% |
| 50 to 80% (an adapted set) | 245 | 15% (6.0%) | 6.4% |
| 30 to 50% | 299 | 9% (8.7%) | 11% |

Other measures on the 50 to 80% pairs: without the turn term the same; the
rim reach over every direction (not only where the set's pieces are) 18%, 7.8%;
box extents 18%, 6.9%; area 12%, 7.9%. DSF adapted sets among the most similar
6% of windows, not always the most similar; the shape measures can't be told
apart this way.

On the roster (`compare.py`, the same windows chosen by each measure):

| measure | windscreen: move, dropped | side: move, dropped, set size | most used donor |
| --- | --- | --- | --- |
| set corners (the rule) | 26 mm, 4.9% | 13 mm, 3.0%, 130 | 76 ports |
| rim reach, every direction | 37 mm, 6.4% | 24 mm, 3.9%, 153 | 121 ports |
| both | 30 mm, 5.7% | 18 mm, 3.2%, 148 | 104 ports |

The rule moves the pieces least and drops the fewest, because it measures the
pieces' own move. Measuring where the set has pieces lets it prefer sets that
sit where the shapes agree: chosen side sets have 130 triangles at the median,
vanilla's 190 (windscreens 717 and 842).

Roster survey, before (the host's window) and after (`summary.py`; the
windscreen surfaces cut by `WINDSCREEN_OPEN` are closed again from their UVs'
frame, +3.8% of area at the median against the converted pane):

| kind | windows | with a set before / after | new donor | gain a set | lose it | dropped triangles before / after |
| --- | --- | --- | --- | --- | --- | --- |
| windscreen | 595 | 583 / 595 | 549 | 12 | 0 | 33,911 / 26,529 |
| rear window | 598 | 505 / 598 | 470 | 93 | 0 | 18,678 / 15,737 |
| side glass at or ahead of the eye | 1,540 | 1,194 / 1,540 | 1,101 | 346 | 0 | 18,218 / 15,635 |
| side glass behind the eye, roof | 1,222 | 27 / 0 | | | 27 | |

On the 2,282 windows with a set before and after, the move is 58 mm RMS at
the median before and 18 mm after (p90 197 and 37 mm), the share of the set's
triangles dropped 5.7% and 4.1% (p90 18.8 and 14.7%), and the triangles past
a window's centre 2,972 and 340. 162 windows keep their host's window. Of the
27 windows that lose their set, 22 had a host window at or ahead of the
host's eye (a door's set the nearest-centre pairing gave to the glass behind).

What it leaves. A vanilla set placed on its own window drops 5.9% of its
triangles (12,685 of 215,734) with the placement's "lands outside the window"
test, so that test, not the donor, is most of what is still dropped (fixed:
"How far a set reaches past its window" below).
Every measure concentrates on a few windows of average shape: 193's doors
take 86 and 75 ports' side glass, 143's windscreen 63 ports'. Side glass
behind the eye keeps the smear surface only, where vanilla mostly draws its
own with the exterior side-rear slot. (#627; the outside test: #626.)

Built (5 cars only, the rest of the roster not built: AST_V8Vantage_77,
CHE_CorvetteZ06_02, PLY_Barracuda_68, MAS_GranTurismoMC_10, BEN_7_Speed8_03 as
IDs 1 to 5, at a58a9398 and with the rule; `out/i459m/before5`, `after5`). The
logs' `interior glass:` lines: the Corvette (host 161) takes sets for its
windscreen (228's, 18 mm), both doors (184's, 195's, 9 and 10 mm) and rear
window (138's), where it had none; the Aston's and Barracuda's glassLM and glassRM doors
take sets (149's and 183's on the Aston, 196's on the Barracuda; 9 to 19 mm); their quarter glass,
behind the eye, none. The Bentley's lexan windscreen pane (0.28 m², the
cockpit carbin's, sharing its s9 with the side lexan) takes 183's at 122 mm,
the largest move of the five. `render_compare diff` (`out/rc/glass-donors`):
the five differ in the cockpit views only, along the crack and shard geometry.
`glass.check` passes on all five.

In game (VM runs `vm1-20261119-041346` before, `vm1-20261119-041815` after,
`-GlassLadder`, IDs 1 to 5, 125 (the Aston's host) and 288 (its new windscreen
donor)): no crash. The Barracuda's windscreen at 0.25 shows crack lines from
the left A-pillar and the top right corner (172's set before: one short line
at the top left). The Maserati's shards at full damage are frosted clusters at
the top corners, where 221's set crowded along the whole top edge before. The
Corvette's new crack lines are at the windscreen's edges, outside the probe's
frame at 0.25; the Aston's windscreen layers stay above it as on master.
Vanilla 288 shows its own left-pillar cluster.

**How far a set reaches past its window** (#626, confirmed-static, 2026-11-19;
scripts in `out/i626` of the fix worktree: `where.py`, `extent.py`,
`exact.py`, `edges.py`, `identity.py`, `variants.py`, `ports.py`). Placed on
its own window (the identity move), the vanilla sets lost 12,685 of their
215,734 triangles to the placement's "lands outside the window" test (centroid in a
filled 1 cm cell, every corner within one cell of one). Two causes:

- The grid. A cell counts as glass when its centre is covered, so the cells
  along the rim are half outside. Vanilla's rim shards are 3 mm slivers rooted
  at the edge: of the dropped side-glass triangles, 58% have their centroid
  on the smear itself (rear windows 37%).
- The set reaches past the glass. Measured exactly in the window's plane
  against its glass (the interior smear and the exterior entry's glass on the
  same panel record, facing like the window: the windscreen's open centre is
  glass there; the set itself is not):

| window | set triangles with a corner past the glass | that corner's reach p90 / p99 / max | centroid past the glass |
| --- | --- | --- | --- |
| windscreen (141) | 25.0% | 5 / 37 / 126 mm | 6.5% |
| rear (116) | 24.7% | 5 / 24 / 71 mm | 6.2% |
| side (268) | 22.1% | 3 / 11 / 93 mm | 2.5% |

It is all round the rim (windscreens on every edge, side glass mostly at the
bottom, into the door, and at the front end; median 3 to 7 mm per edge), and
mostly shards: 34% of shard_dull triangles reach past, 18% of shard_bright,
10% of crack. DSF roots its shards at the glass's edge and lets them run over
the frame, so the glass alone is not the extent of a window's set.

517 of the 527 donor windows have exterior glass on their own record; 2 have
neither smear nor exterior glass (their set is their only glass).

Rule (`InteriorGlass._place`, `HostWindow.glass`, `HostWindow.overhang`,
`_Pane.past`): a placed triangle is kept when none of its corners and not its
centroid lies further past the converted window's glass than it lay past its
donor window's glass, to one grid cell: `_carry` places each piece against
rims measured on cell centres (half a cell on each window), so where it lands
against the true edge is known to a cell. `_Pane.lift` now also extends the
glass's plane under a corner more than three cells out (it found no triangle
there and dropped the triangle).

Identity move, every vanilla set (215,734 triangles), dropped triangles:

| pane the set is placed on | old test | new, slack one position step | new, slack one cell |
| --- | --- | --- | --- |
| all the window's interior glass (`HostWindow.frame`, the rim `_carry` uses) | 12,685 | 0 | 0 |
| the window's glass only (smear and exterior; what a converted pane is) | 16,441 | 12,750 | 1,140 |

The 1,140 left are the donor rim's doing: `_carry` measures the donor's rim on
all its interior glass, set included, and the windscreen's smear has its open
centre, while a converted window's rim is its glass. With the donor's rim
taken on its glass too, the identity drops none (with either slack); that
changes `DonorSet` and with it the donor choice (#628's rule), so it is left
for #629.

Roster, no conversion (`ports.py`: 2,733 windows on 691 cars rebuilt from the
car store, master's donors), triangles dropped:

| window | set triangles | old test | new rule | dropped share per window, median / p90 (old, new) |
| --- | --- | --- | --- | --- |
| windscreen | 419,042 | 26,529 | 12,229 | 4.9 / 10.4% to 1.4 / 6.1% |
| rear | 250,164 | 15,737 | 6,264 | 4.3 / 12.5% to 0.5 / 6.4% |
| side | 229,530 | 15,635 | 8,001 | 3.1 / 20.2% to 0.0 / 13.7% |

2,189 windows keep more, 59 keep fewer (159 triangles: the old test let a
corner lie up to two cells out diagonally). The kept triangles reach past the
converted glass as vanilla's past theirs: p99 10 to 12 mm, p99.9 15 to 31 mm,
at most 114 mm (old test: p99 8 mm, at most 20 mm).


Survey (293 of the 721 roster cars, the rest not built; `out/i459/survey.txt`
in the #459 worktree): 899 windows take host layers, against 1,103 with
generated ones on master. The 479 without are rear doors and quarter lights
whose host side glass has none or went to the front door (348); lexan wraps and
small panes beside a window that took the layers (94); rear windows whose host has
no rear layers, or (before the sight test went) whose layers all fell out of the driver's sight (34); and three windscreens
(hosts 161 and 217 have none; the MC12's coincident cockpit pane takes them).
No interior needed decimation; `glass.check` passes on all 293. In game
(`vm1-20261113-191025` master, `vm1-20261113-195620` this rule): the M5 on 176
shows small clusters at the windscreen's top left after the glass ladder,
where master showed teeth along the A-pillar; vanilla 176 shows clusters at
its left and bottom edges.

The rear
window now shows the vanilla defroster lines when clean. Rebuilt M5, S65, Supra and Jeep (`out/glassfix/`, IDs
304–307): all glass inside one slot (`glass check`); state 3 renders as an
empty frame with a shattered rim like vanilla 147
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
state is per spawn. Binding glass to the chassis record is vanilla practice
(63 vanilla vehicles, 19,222 full-entry triangles; `out/dmc/glassroles.py`),
as is binding to bumpers (228's tail-lamp covers). What raises `.y` at spawn
is still untraced (the harness zaps into cars in traffic; the vE run crashed
into a tanker within seconds). The lamp-lens part was a converter bug:
coloured rear lenses no longer become glass (`lights.md`, "Coloured lamp
lenses drawn as glass").

**Exterior materials.** Vanilla clean exterior entries draw with the `_LOD1`
shared materials (`GLOBAL_Chassis_LOD1` `10122b9653b14e2a` in 149 of 150,
`GLOBAL_Chassis_Simple_LOD1` `10122b97…` 149, `GLOBAL_Glass_LOD1`
`10122b99…` 145, `GLOBAL_Tyre_LOD1` `10122b9b…` 150; the full-entry ones in at
most 8 vehicles, a few hundred triangles; `out/dmc/extmats.py`). Converted
exteriors used the full-entry materials; they now use the twins
(`forza2dsf.SHARED_EXTERIOR`, same strides and MODL flags). The `_LOD1`
paint shader (program 26) is program 27 without the damage terms, with the
same fresnel. No visible change on the DMC (`host-20261023-092015`); done for
fidelity with vanilla.

Vanilla glass binding: semantic 9 on glass is mostly the glass panel's
record (Desanne windscreen (0, 5) = record 29, rear window (0, 20) =
record 61, both type 1 role 8; door glass on the door records, lamp glass
on the lamp records), in the full, interior and exterior entries alike.
Some vanilla glass is on the chassis (0, 0): 147's quarter lights and
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

Part 0's UID equals the model UID in UPVH's AWHF header, and the UPVH
records name their MODL parts by UID (halves swapped; `upvh.md`, "MODL part
UIDs are global keys too"). Part UIDs are unique across the
whole vanilla archive (6,139 in 450 entries) and the engine keys a global table
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
  DXTC  a complete DDS file (DXT5 512x512 with 4 mip levels in Saloon 15)
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
