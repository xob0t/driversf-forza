# Host-free hierarchy: what a port keeps from its host, and a plan to stop

Updated: 2026-10-30

Every port is a clone of one vanilla host, and whatever the converter doesn't
rebuild stays the host's (`hosts.md`, "What comes from the host"). The body,
cockpit, wheels, textures, paint, engine sound and stat bars are ours. The
hierarchy (NONR/UPVH) and the handling base (VO3D) are mostly the host's,
patched in place and moved around. This note counts that dependency field by
field, lists the rules the vanilla fleet follows that a generator could rely on,
and lays out the stages that would replace a per-car host with Forza's own part
structure on one fixed template. It also covers stage 1's spike, a light table
generated from the Forza lamps. Evidence levels as in `dngvehicles.md`.

Tools (read-only on both archives; no converter code changed):

```powershell
uv run python tools/re/host_free.py rules --json out/hf/rules.json        # vanilla rules, 150 vehicles
uv run python tools/re/host_free.py diff --build <build dir> --json out/hf/diff.json   # ports vs their hosts
uv run python tools/re/host_free.py inventory --rules out/hf/rules.json --diff out/hf/diff.json
uv run python tools/re/light_table_spike.py build --out out/spike/after/dngvehicles.sp --car SHE_Series1_99 --car MAZ_Miata_94
uv run python tools/re/light_table_spike.py check out/spike/after/dngvehicles.sp --id 1
```

`diff` compares every port of a `port_batch` build with its host. Records are
matched by (type, role, occurrence) and table entries through the matched
records, and each field is counted per port and per entry. The numbers below
come from the main checkout's master build of 2026-10-30 (`out/build`, 573 cars,
built before #389, which touched only lamp textures).

## Rules the vanilla fleet follows (confirmed-static)

`host_free.py rules` over the 150 vanilla UPVHs. These are the rules a
generated hierarchy has to keep, and whether a generator can rely on them.

| Rule | Holds on |
| --- | ---: |
| record 0 is the only root (type 4), subtree = record count | 150 / 150 |
| the root's children are the chassis records (type 5), then one role-38 part holding everything else | 143 / 150 |
| markers, lights and gauges (types 3, 17, 18) are leaves | 148 / 150 (267, 268: a role-0 marker holds the hubs) |
| records with a part UID are exactly types 1, 2, 5; every UID is a full-entry MODL part | 150 / 150 |
| marker, hinge, body, gauge, mirror, light, LOD-link, attachment and wheel indices: 0..n-1, each once, in record order | 150 / 150 each |
| collision offsets fall in record order, the root's model last in the PDL | 150 / 150 |
| PDL model count = records with a collision model | 149 / 150 (273) |
| +0x34 transform slot over part records: 0, 1, 2 ... in record order, some parts without one | 150 / 150 |
| +0x2A panel-data index over part records: from 0 in record order, steps of 0 or 1 | 150 / 150 (strictly +1: 129) |
| +0x0C bit 0, +0x16, +0x18 = `damage._tree_fields` | 10,956 / 10,959 records |
| +0x0C bit 1 = a wheel record below this one (not counting itself) | 10,959 / 10,959 |
| +0x0C bit 4 only on the root | 10,959 / 10,959 |
| a parent lists its light children before all its other children | 2,917 / 2,917 parents |
| gauge children come right after the lights (all under role 185) | 145 / 145 |
| a light's parent is a part record | 2,179 / 2,179 |
| light ids are a permutation of 0..n-1 | 150 / 150 |
| light id = table index | 1 / 150 (ids run backwards within each parent's group) |
| light +0x20, +0x24 = pi, pi; position w 1, direction w 0 | 2,179 / 2,179 |
| light +0x30..0x3F: one value per vehicle (3 values cover 123 entries) | 150 / 150 |
| hinge +0x28..0x2F zero, pivot w 1, axis w 0 | 2,616 / 2,616 |
| hinge (limit, param) = the most common value for its (type, role) | at least 2,565 / 2,616 |
| body 4x4 identity, +0x90 zero | 3,438 / 3,462 |
| wheel quaternion (0, 1, 0, 0) at -x, identity at +x | 594 / 620 (the rest: camber, 0.2 deg) |
| attachments hang on role 85/86 markers; +0x10 is an exterior MODL part | 150 / 150; 864 / 875 |
| header +0x2E u32 = animation blob size / 4 | 150 / 150 (new: it was "0 or 1") |
| header type 26, +0x1C..0x25 zero, +0x38..0x4F 0xFF | 150 / 150 |
| header +0x34 = 0x0CE8 and +0x54 constant | 148 / 150 (192, 257) |

The record words `upvh.md` lists as undecoded, by how much of them a (type,
role) table would fill (records whose value is the most common one for their
type / for their (type, role), of 10,959):

| Word | By type | By (type, role) | Values |
| --- | ---: | ---: | --- |
| +0x0C bits 2..15 | 9,114 | 10,607 | 0, 4 |
| +0x0E | 9,272 | 10,513 | 0, 1, 0x4000 ... (7) |
| +0x10 | 8,651 | 10,589 | 22 values |
| +0x12 | 8,907 | 10,572 | 15 values |
| +0x2C | 8,705 | 10,184 | 3, 1, 0x403 ... (8) |
| +0x24, +0x2E | 10,959 | 10,959 | -1 |
| +0x28, +0x32, +0x36..0x3C | 10,878 to 10,958 | same | -1 or 0, a few others |
| +0x44, +0x48 | 10,817 | 10,817 | 1, 4 |
| +0x4C | 8,215 | 8,226 | exporter pointers |

So these words are about 96% a function of (type, role). A template can carry
them as a lookup table without knowing what they mean. The 4% that isn't is
where an unknown runtime meaning may hide.

Where lights hang (parent role, 2,179 lights): rear lamps under the chassis
part 38 (802), the rear bumper 36 (131), the boot 5 (115), misc 0 (61), the
hatch 124 (11). Front lamps under the head lamp parts 50 and 51 (298 each),
the front bumper 35 (130), the front structure 37 (101), misc 0 (68), the
bonnet 4 (42), the wings 33/34 (33 each), the mirrors 40/41 (20 each, side
repeaters).

Marker parents (most common): 47 steering, 10/11 seats, 71/72, 76, 92 and the
85/86 attachment markers under the chassis part 38; 77 exhaust under the exhaust
parts 15/136 (237) or 38 (43); 69/70 under the head lamp parts; 65/67 under the
front doors; 73/74 under the mirrors; 63 under glass; 64 under glass or the boot;
46 under the front bumper; 75/81 under the front structure.

Engine limits that bound a generated hierarchy: 32 lamp objects per car and 32
`g_v4LightColours` (entry 0 = no lamp), so at most 31 lamps
(`lights-re.md`); 32 panel transforms (`g_PanelTransforms[96]`, slot at most 31)
and 64 panel-data entries (`g_v4PerPanelData[64]`) (`damage.md`); 4 exhaust
slots with no bounds check (`upvh.md`). The vanilla maxima are 27 lights, 32
markers, 25 hinges, 31 bodies, 91 records, slot 26, panel-data index 31,
10 to 32 collision models with 1 to 10 primitives each.

## Field inventory

`host_free.py inventory` (the table is the tool's `FIELDS`, with the rule and
diff counts filled in). Classes: **generated** (we write it from Forza),
**adjusted** (the host's value moved or scaled), **copied** (the host's value as
is, understood), **copied blind** (the host's value, not understood). "Ports
changed" counts table entries or records over all 573 ports where the port's
value differs from its host's.

| Chunk | Field | Class | Written by | Meaning, evidence | Vanilla rule holds | Ports changed |
| --- | --- | --- | --- | --- | --- | --- |
| header | magic AWHF | copied | | format magic | | |
| header | +0x04 model UID | generated | `clone_vehicle.renew_uids` | MODL part 0's UID | 150/150 | 573/573 |
| header | +0x0C type 26 | copied blind | | | 150/150 | 0/573 |
| header | +0x10 record count | adjusted | exhaust, `add_gauge_records` | | | 209/573 |
| header | +0x14 hierarchy id | generated | `clone_vehicle.patch_ids` | spool id; the VO3D is found by it | 150/150 | 573/573 |
| header | +0x18 PDL size | copied | | PDL edited in place | 150/150 | 0/573 |
| header | +0x1C..0x25 zero | copied blind | | | 150/150 | |
| header | +0x26, +0x2E animation size and count, the blob | copied blind | | 13 vehicles; undecoded | 150/150 | 0/573 |
| header | marker, gauge counts | adjusted | exhaust, `add_gauge_records` | | | 209/1146 |
| header | light count | copied | | the host's entries, whatever the Forza lamps | | 0/573 |
| header | hinge, body, mirror, LOD-link, attachment, wheel counts | copied | | | | 0/3438 |
| header | +0x34 0x0CE8, +0x54 constant | copied blind | | | 148/150 | 0/1146 |
| header | +0x38..0x4F 0xFF | copied blind | | | 150/150 | |
| records | the record set: which parts, markers, lights, gauges, in what order | copied | | the host's tree; record order fixes every index below | see rules | |
| records | +0x00 part UID | generated | `renew_uids`, `remap_upvh_parts`, `split_rear_wheel` | MODL part, halves swapped | 150/150 | |
| records | +0x08 type | copied | | | | |
| records | +0x0A role (parts) | copied | | panel kind (`damage.md` roles) | | |
| records | +0x0A role (markers) | copied blind | | 47, 10, 11, 77 decoded; 92, 63/64, 65/67, 69/70, 71..76, 81, 46, 85/86 not | | |
| records | +0x0C bits 0, 1 | adjusted | `move_subtree`, `add_gauge_records` | a part child; a wheel below | 10956/10959; 10959/10959 | 490/42189 |
| records | +0x0C bit 2 | copied blind | | on most part records with a body | | 0/42189 |
| records | +0x0E, +0x10, +0x12 | copied blind | | (type, role) fixes 96-97% | | 0/126567 |
| records | +0x14 subtree size | adjusted | exhaust, damage remounts, gauges | | | 1218/42189 |
| records | +0x16, +0x18 hinge pops | adjusted | `_tree_fields` | | 10956/10959 | 867/42189 |
| records | +0x1A marker index | adjusted | exhaust insert/remove | | 150/150 | 2970/42189 |
| records | hinge, body, mirror, LOD-link, attachment, wheel indices | copied | | | 150/150 each | 0/253134 |
| records | +0x26 gauge index | adjusted | `add_gauge_records` | | 150/150 | (added records) |
| records | +0x30 light index | copied | | | 150/150 | 0/42189 |
| records | +0x20 collision offset | copied | | | 150/150; 149/150 | 0/42189 |
| records | +0x2A panel-data index | copied | | s9 byte 1 of every vertex | 150/150 | 0/42189 |
| records | +0x34 transform slot | copied | | s9 byte 0 of every vertex | 150/150 | 0/42189 |
| records | +0x2C | copied blind | | 1 or 3; 0 or 4 | | 0/42189 |
| records | +0x24, +0x28, +0x2E, +0x32..0x3D | copied blind | | -1 / 0 fill | | 0/42189 |
| records | +0x44..0x4F | copied blind | | exporter leftovers | | 0/42189 |
| hinges | pivot: wheels, hub knuckles 155/156 | adjusted | `patch_hubs`, `settle_wheels` | moved with the hub | | 3420/4550 |
| hinges | pivot: struts 188/190 | copied | | not moved | | |
| hinges | pivot: steering column | generated | `cockpit.patch_hierarchy` | Forza hub | | 570/573 |
| hinges | pivot: gauge cluster 185 | adjusted | `cockpit.patch_hierarchy` | mean of the gauges | | 442/525 |
| hinges | pivot: panels | adjusted | `DamagePlan.patch_upvh` | host panel box to Forza panel box | | 3588/4523 |
| hinges | axis: steering column | generated | `cockpit.patch_hierarchy` | | | 570/573 |
| hinges | axis: the rest | copied | | door verticals, bonnet x ... | 2616/2616 | 0/9598 |
| hinges | limit, param | copied blind | | per role; doors with limit 0 still swing | 98% by role | 0/9046 |
| bodies | rotation | copied | | panel frame | | 0/12885 |
| bodies | position: panels | adjusted | `DamagePlan.patch_upvh` | panel centre, box-mapped | | 8853/10593 |
| bodies | position: wheels | adjusted | `patch_hubs` | negated hub | | 2292/2292 |
| bodies | inertia: panels | adjusted | `DamagePlan.patch_upvh` | scaled by the box ratio | | 8853/10593 |
| bodies | inertia: wheels; mass | copied | | kg | | 0/15177 |
| bodies | +0x50 4x4, +0x90 zeros | copied blind | | identity | 3438/3462 | 0/10593 |
| markers | steering 47 | generated | `cockpit.patch_hierarchy` | wheel, hands, chase driver, head camera x | | 1152/1158 |
| markers | seats 10/11 position | generated | `cockpit.patch_seats` | driver model | | 1140/1153 |
| markers | seats 10/11 quaternion | copied | | seat tilt | | 0/1153 |
| markers | exhaust 77 | generated | `exhaust.patch_upvh` | smoke, backfire, sound | | 1773/2326 |
| markers | attachment markers 85/86 | copied blind | | | 150/150; 875/875 | 0/6760 |
| markers | other markers (92, 63/64, 65/67, 69/70, 71..76, 81, 46, 0 ...) | copied blind | | at the host's positions | | 0/16908 |
| gauges | needle pivot, dial quaternion, sweep | generated | `cockpit.patch_hierarchy` | `cockpit.md` Gauges | | 2659/3378 |
| gauges | gauge id, hide flag | copied | `add_gauge_records` adds missing ids | | 270/270 | 0/2252 |
| mirrors | position | generated | `cockpit.patch_hierarchy` | Forza mirror | | 1004/1092 |
| lights | position, direction, flags | generated | `LampPlan.patch_upvh` | `lights.md` | | 35717/43810 |
| lights | entry count, ids, type-17 records and parents | copied | | a lamp breaks with its record's parent | 150/150; 2179/2179 | 0/8762 |
| lights | +0x20 pi, pi | copied | | cone angles? | 2179/2179 | 0/8762 |
| lights | +0x30 tail | copied blind | | exporter leftover | 2179/2179 | 0/8762 |
| LOD links | interior, exterior part UIDs | generated | `renew_uids`, `split_rear_wheel` | | 1237/1237; 150/150 | |
| attachments | position, +0x18 | copied blind | | never moved | | 0/6760 |
| attachments | +0x10 exterior part UID | generated | `renew_uids` | | 864/875 | 3351/3380 |
| wheels | hub position | generated | `patch_hubs`, `settle_wheels` | Forza wheelbase, track, ride height | | 2292/2292 |
| wheels | quaternion | copied | | side, camber | 594/620 | 0/2292 |
| PDL | header | copied | | | 150/150 | |
| PDL | root hull | adjusted | `DamagePlan.patch_upvh` (hull) | host body box to Forza body box | | 573/573 |
| PDL | panel boxes | adjusted | `DamagePlan.patch_upvh` | scaled 0.67-1.5x | | 8853/10593 |
| PDL | wheel cylinders | adjusted | `fit_wheel_collision` | radius, width from the tyres | | 2292/2292 |
| PDL | primitive flags (0x900/0x901) and other words | copied blind | | | | |
| VO3D | +0 spool id | generated | `clone_vehicle.patch_ids` | | 150/150 | |
| VSRT | engine sound | generated | `engine_sound` | host's on failure | | |
| SPSR | paint sets | generated | `paint.install` | | | |
| MODL | LOD tables, submodels | generated | `rebuild_modl` | | | |
| MODL | part records: which parts, their boxes | copied | | the host's oriented boxes; runtime use untraced | | |
| DRSP | materials (MACO) | adjusted | `dsf_materials` | host materials, Forza atlas | | |
| SSIC | VSCT category byte | copied | | vehicle class | | |
| SSIC | VSCT other bytes | copied blind | | | | |
| Lua | vehicleStats | adjusted | `register.lua`, `stat_bars` | bars generated, the rest the host's | | |

VO3D, all 181 parameters any port or host sets, classed from the diff: a
parameter some port changes is generated when `handling_dsf` writes it from the
Forza car (rules Z, A, TP, CY, CZ) or the cockpit, camera and gauge code writes
it, else adjusted (rules M, F, W, G, the feel and drivetrain groups taken from a
donor vanilla car, the settle). A parameter no port changes is copied if
`handling.PARAMS` or a research note gives its meaning, else copied blind.

| Class | VO3D parameters |
| --- | --- |
| generated (27) | p0 p15 p16 p17 p18 p30 p32 p42 p87 p95 p160 p298 p360 p366 p367 p392 p430 p431 p432 p516 p535 p536 p537 p538 p559 p605 p606 |
| adjusted (68) | p1 p2 p3 p4 p13 p25 p166 p188 p276..p281 p292 p301 p304 p344 p345 p352..p358 p361 p362 p364 p365 p368 p369 p371 p373 p374 p376 p379..p382 p385..p391 p393 p411 p470 p496 p497 p505..p511 p517 p519..p521 p526 p529 p530 p569 p599 |
| copied (19) | p24 p40 p183..p187 p239 p275 p282 p302 p305 p469 p522 p523 p524 p525 p596 p597 |
| copied blind (67) | p14 p38 p39 p138 p310 p313 p349..p351 p375 p410 p433..p435 p438 p443 p451..p456 p458 p492 p493 p502..p504 p512 p513 p527 p528 p532 p540 p542 p546 p547 p561 p571..p595 p598 p600 p601 p604 |

Of the 67 blind ones, 12 have one value wherever a vanilla car sets them (p310,
p349..p351, p435, p438, p443, p540, p594, p595, p600, p604), and 21 are set on
140 or more of the 150 cars (p38, p375, p434, p452, p453, p455, p492, p502..p504,
p527, p528, p532, p561, p583..p585, p589, p592). The rest vary from car
to car with no meaning attached. p571..p593 are read by `FUN_00874B00` and
`FUN_0088F630` (`handling.md` appendix) and nobody has traced them.

### Counts per class

| | generated | adjusted | copied | copied blind |
| --- | ---: | ---: | ---: | ---: |
| UPVH fields (table rows) | 14 | 16 | 22 | 18 |
| VO3D parameters | 27 | 68 | 19 | 67 |
| other per-vehicle chunks | 4 | 2 | 2 | 1 |

### What the counts mean

Field counts hide the volume, so here it is in entries. Few UPVH fields are
copied blind, but some of them are large: 8,454 marker entries and
3,380 attachment entries sit at the host's positions on every port (0 moved on
573 cars), and nobody knows what reads most of them. The record flags
(+0x0E, +0x10, +0x12, +0x2C, bit 2) never change and are a (type, role)
lookup 96% of the time. The fill words and exporter leftovers are constant.

What is copied and understood is mostly structure: the record set, its order and
every table index. Those follow mechanical rules on 150 of 150 vehicles, so a
generator can write them. The hard part is the record set itself: which panels,
markers and lights exist and under which parent. That is the host's today, and
every UPVH bug in the list below comes from it.

VO3D is the bigger unknown: 67 parameters copied blind, 68 adjusted from the host
or a donor. Handling has no crash risk (VO3D is a sparse override list, and
missing entries fall back to the engine's defaults), but it decides how the car
drives, and `handling_dsf` was tuned against host values.

## What the host dependency costs

| Bug | Field it comes from |
| --- | --- |
| #146, #261: lamps with no entry left at their end; 5 reverse lamps unlit on 3 cars and 58 fog lamps unlit (master build); 103 cars light several lamps through one shared entry | light count, type-17 records (copied) |
| #193, #269: mirrors that can't move to an in-order slot; 3 mirrors on 2 cars still under the wrong record | record set, +0x34 / +0x2A numbering (copied) |
| #318, PR #333: two steering markers on host 132 | record set (markers, copied) |
| #334: 243 (RUF RT12) excluded as a host by a marker quirk | record set (markers, copied) |
| #210, #317: collision hull, wheel cylinders | PDL (adjusted from the host's shapes) |
| #306: scratch maps | UV1 decal layout from the host's field (`damage.md`) |
| #239, #243: door count matching for hosts | record set (door records) |
| #273: copied exhaust markers leave stale hinge pops on 3 cars (R8 08, M6 10, F151; lint fails on them in the master build) | tree fields (adjusted by an insert) |

## Design: generating the hierarchy from Forza's parts

The aim is a hierarchy written from the Forza car's sections against one fixed,
well-understood template car, rather than a host chosen per car and patched.
The template supplies only what we can't derive: the blind words, the PDL
primitive kinds, a crush field and a decal layout. Everything below is written
in one pass, in record order, before any geometry is written, because vertices
carry the transform slot, panel-data index and light id (s9 bytes 0, 1, 2).

### Record order and indices

Build the tree as nodes (the spike's `Node`), then flatten depth-first. All
indices follow from the flattened order, so nothing renumbers after a later
edit:

- root (type 4), then the chassis record(s) (type 5, leaves), then one part with
  role 38 holding everything else (143 of 150);
- under each parent: its lights first, then gauges (only under 185), then parts
  and markers;
- marker, hinge, body, gauge, mirror, light, LOD-link, attachment and wheel
  indices: 0..n-1 in record order (150 of 150 each);
- transform slots +0x34 and panel-data indices +0x2A over part records in
  record order, slot only on parts that move on their own, panel-data indices
  shared only where vanilla shares them; slot at most 31, index at most 63;
- collision models written in reverse record order with the root's last, the
  offsets taken from the written PDL;
- subtree sizes from the tree, then `_tree_fields` for +0x0C bit 0, +0x16 and
  +0x18, and bit 1 from the wheels below. Writing these from the final tree
  rather than patching them after each edit removes the class of crash behind
  #187/#193 (stale pops) and #273.

### Roles, flags and the blind words

Roles come from the Forza section that makes each part (the mapping
`damage.section_rule` uses today, in reverse). The flags (+0x0C bit 2, +0x0E,
+0x10, +0x12, +0x2C) and fill words come from a (type, role) table built from
the template, or from the fleet's most common value for that pair (96-97% of
vanilla records match it). The 3-4% that don't are the risk. Before trusting the
table, check which of those words `FUN_0088E310` (the UPVH parser) and the
damage code read.

### Hinges and bodies

Hinge pivot and axis come from the Forza panel. Doors and wings get a vertical
axis on the front shut line, the bonnet the x axis at its rear edge, the boot at
its front edge, bumpers the z axis through their centre, the steering column
from the cockpit, wheels and knuckles from the hubs. Limit and param come from
the role's vanilla mode (98% of hinges). Bodies get the panel's centre of mass,
an inertia from its box, and the role's vanilla mass (bumpers 12-20 kg, doors
8-9 kg). The rotation stays identity, except for tilted panels (bonnet and boot)
where vanilla tilts it.

### Light entries

One type-17 record and entry per Forza lamp. The engine holds 31; the spike stops at
27 (`MAX_LAMPS`, the vanilla maximum) and leaves any lamp past that unlit, so 28 to
31 lamps are untested. The parent is
the panel record the lamp's geometry rides (`damage.section_rule`), so a lamp
breaks with the panel it's drawn on. Without damage panels: the head lamp part of
its side at the front, the chassis part at the rear, the boot for a centre stop
lamp (the vanilla majorities). Ids are a permutation (vanilla's run backwards
per parent; the spike's follow the lamp priority, and the game takes them).

### Markers

- Steering 47, seats 10/11, exhaust 77: generated today; in a generated tree
  they are written once, one steering marker only (no #318/#334 cases), and
  as many exhaust markers as Forza tips, up to 4 (no copy-and-renumber).
- Head camera: VO3D p430..p432 from the eye, measured from the steering marker
  (`cockpit.md`). Bonnet camera p535..p537 from the windscreen.
- The rest (92, 63/64, 65/67, 69/70, 71..76, 81, 46, 0, 85/86 with the
  attachments): 11,834 entries copied blind today. A research step comes first.
  Decompile `FUN_0088E310`'s role switch and list which marker roles it stores and
  where, then find the readers of those spec offsets. Roles nothing reads can be
  left out or placed by the fleet's relative position. Roles that are read get a
  rule from Forza geometry (head lamp markers 69/70 at the lamp, door markers
  65/67 at the handle, mirror markers 73/74 at the mirror ...).

### Collision (PDL)

- Wheels: one cylinder per wheel, sized to the tyre (today's
  `fit_wheel_collision`).
- Panels: one box per panel record in the body frame, from the Forza panel's
  oriented box (vanilla panels have one primitive; 3,462 of the 3,613 models).
- Root hull: vanilla's five boxes (a bounding box with flags 0, the lower body,
  the cabin and two deck slopes, tilted). Fit them to slices of the Forza body
  instead of box-mapping the host's (no `_floor_collision` clamp needed: the
  hull bottom goes at the Forza sills plus vanilla's 8 cm floor).
- Primitive flags (0x900 on 3,439 primitives, 0x901 on 633, 0 on the root's
  bounding box) from the template per primitive kind until someone decodes
  them.

### Damage panels

Panel records come from Forza's parts: the shut-line components `damage.md`
("Doors and wings as Forza parts") already finds, `bumperF/R`, `hood`, `trunk`,
`headlight*`, `mirror*`, `exhaust*`, glass. The number of doors comes from the
car (2 or 4, side doors 52/53 for vans), not from the host. Mirrors go under
their mount from the start, so `_remount_mirrors` and its blockers are no
longer needed. Each panel gets its MODL part (full entry) and, where vanilla has
one, an exterior part and LOD link. Crush targets (s12) and decal UV1 still need a
source field. Map the template's field per role through the normalised panel
box, as today's host field does, but always from the same well-understood car.

### What stays from the template, and why

- The (type, role) table of the blind record words, the PDL primitive flags,
  the light +0x30 tail, the header constants: not understood, constant or
  near-constant in the fleet.
- The crush displacement field (s12) and the decal UV1 layout per panel role:
  per-vertex data with no Forza counterpart.
- MODL parts without records (vanilla's shadow copies of the chassis, parts 7,
  8, 23, 25 on the Desanne), the part box kinds: untraced at runtime.
- Materials (MACO) and the chassis texture slot, the SSIC category, the VSRT
  template bank (already), the blind VO3D parameters (stage 4).
- The animation blob: none. Pick a template without one.

Candidate: 266 (Desanne). It is the best-documented car in these notes. It has
every standard role (4 doors, wings, bonnet, boot, both bumpers, head lamp parts,
mirrors, two exhausts, the 185 cluster) and no animation blob, variant chassis
or marker quirk. A coupe template (227, 370Z) as a second one for the 2-door
door layout is worth checking against the generator's needs. With generated
door records it probably isn't needed.

## Stages

In the order of the plan. Each stage still runs on today's host clone and swaps
one area to generated data, so each can be built and tested alone.

### 1. Light table

- Removes: the sharing logic in `LampPlan.assign` (`partner`, `pair_entry`,
  the other-end fallback, `entry_place`, `_best_entry`) and the spare-entry
  parking in `patch_upvh`, about 150 lines; host picking that weighs light
  entries (#146's suggestion) won't be needed.
- Targets (offline result only; the game hasn't run it, see the spike): #146 (the
  rest: the reverse lamps of the Miata 94, Elise, Series 1,
  the Express's left reverse lamp, the Boss 429's front indicator, the Grand
  Sport and SC430 on host 161), the 58 fog lamps left unlit for want of an
  entry, and 103 cars lighting several lamps through one entry placed at their
  middle (up to 0.5 m from some of them, `check` below).
- Crash risks: record indices change, so the rebuild runs last, after every
  edit that holds record indices (the spike does). Stale tree fields (rewritten
  from the tree); more than 27 lamps (the spike's cap; the engine's is 31); a lamp record under a panel the
  damage code moves.
- VM test: `-Lights` (phases and drives), `-Damage -DamagePanels` (the
  `forceOpenPanels` walk over the new records), `-DamageSide`, on cars whose
  lamp count changes most.

### 2. Markers and collision

- First: decompile the marker role switch in `FUN_0088E310` (research only).
- Removes: `host_cockpit`'s marker search and the two-marker handling, the
  hinge-at-first-marker host exclusion (#334), exhaust `insert_marker` /
  `remove_marker` / `set_count`, `_map_collision`, `_scale_collision` and
  `_floor_collision`.
- Would close: #334, #273 (no copied markers to renumber), the box-mapped exhaust
  markers of the 37 cars without a Forza tip (#65), and the
  host's hull outline for the rest of #210's class.
- Crash risks: the PDL changes size, so the collision offsets and the header PDL
  size change; unknown primitive flags; marker counts beyond the engine's arrays
  (exhaust has 4 slots, no check).
- VM test: `-Dyno` with the kerb keys (`damage.md`, "Collision hull"),
  `-DamageSide`, `-Views exhaust`, cockpit and chase views for the steering marker.

### 3. Damage panels

- Removes: `move_subtree`, `_move_in_turn`, `_remount_mirrors`, `_blockers`, the
  host panel box to Forza panel box mapping of hinges and bodies, door-count
  matching in `host_select` (#239, #243), `split_rear_wheel` (a generated tree
  gives rear wheels their own part).
- Would close: #193, #269 (mirrors placed under their mount), #209-class door cases.
  The scratch map (#306) moves to the template's UV1, so it would need checking
  again.
- Crash risks: the biggest. An out-of-order slot or panel-data index crashed
  `forceOpenPanels` (run `vm2-20261029-030631`). A door with +0x0C bit 0 and no
  part child crashed it too (Devon, runs `vm1-20261029-024550` and others).
  Slots stay within 32 and panel-data indices within 64; every panel record
  needs its MODL part.
- VM test: `-Damage -DamagePanels` and `-DamageSide` on every body class (2-door,
  4-door, van with side doors, convertible), `-GlassLadder` for the glass panels,
  before and after on the same cars.

### 4. Handling

- Replace the host's VO3D as a base with a fleet base: the 12 constant blind
  parameters as they are, the rest of the blind ones from the template, and the
  adjusted ones from the fleet model's absolute prediction (rule A) instead of
  host x ratio (F, M), where `handling_dsf loo` says that's no worse.
- Removes: host ranking for handling (`ranked_hosts`, feel donors) once the LOO
  test passes.
- No crash risk (sparse overrides, engine defaults behind them); the risk is
  driving feel. VM test: `-Dyno` launches and the handbrake flick runs of
  `handling.md` round 4, on the 48 pairs.

### 5. One fixed template

- Every car on the template; `host_select` only picks the sound fallback and
  the category.
- Removes the per-host cases: 132/243 markers, the 210/217 head lamp quirks,
  chassis variants (`chassis_variants.py`), the special-host exclusions, host
  hub and axle reading (`host_wheels`).
- Crash risk: everything above at once on 573 cars. Before switching, a roster
  survey of what each car needs that the template lacks (6-wheel trucks and the
  bus, pop-up lamps, convertible roofs, side doors), then VM series of about 10
  cars each (the harness limit) over the whole roster.

## Spike: the light table from Forza lamps

`tools/re/light_table_spike.py` wraps `port_car` at run time. It patches
`LampPlan.assign`, `LampPlan.patch_upvh` and `forza2dsf.convert` in its own
process and changes no converter code. Every lamp the plan lights gets its own
light id in priority order. After the conversion, the host's type-17 records and
light table are dropped and new ones written: one leaf per lamp as the first
child of its parent (front: head lamp part of its side, else the front structure
37; rear: chassis part 38; the centre stop lamp: boot 5), the table in record
order with id at +0x28, pi, pi and the host's +0x30 tail. Then the record and
light counts, subtree sizes and tree fields.

Cars: the Shelby Series 1 99 (FM4, host 248 Classic 08) and the Mazda Miata 94
(FH1, host 126 Classic 06). Both had their reverse lamps unlit for want of an
entry on master. `port_car` with `port_batch`'s default options, spool ids 1 and
2, the same build with `--normal` as the before.

| | Series 1 before | after | Miata before | after |
| --- | ---: | ---: | ---: | ---: |
| lamps | 17 | 17 | 15 | 15 |
| light entries | 8 (the host's) | 17 | 8 | 15 |
| unlit lamps | 2 (reverse) | 0 | 2 (reverse) | 0 |
| records | 63 | 72 | | |
| farthest light-id centroid from its entry, full entry | 0.45 m | 0.02 m | 0.50 m | 0.06 m |
| lint | ok | ok | ok | ok |

(`check`: every light id the full and exterior entries' vertices carry has an
entry; before, a shared entry sits between the lamps it serves.)

In game: not run yet. The VM series (`run-series.ps1 -Vm -Archives
out\spike\before\dngvehicles.sp,out\spike\after\dngvehicles.sp -Labels before,after
-ProbeIds '1,2' -Lights -ShotSeconds 3 -WaitSeconds 600`) was stopped during its first
run when VM series were put on hold, so whether the game accepts the generated table
and lights the reverse lamps is open. The lint's tree, index and layout checks pass.

## Open

1. The marker role switch in `FUN_0088E310`: which of the 11,834 blind marker
   and attachment entries the engine reads.
2. The 3-4% of records whose +0x0E/+0x10/+0x12/+0x2C differ from their (type,
   role) mode: what reads those words.
3. PDL primitive flags and the root hull's five boxes: what the physics reads.
4. Whether MODL part boxes are used at runtime (culling?); ports keep the host's.
5. VO3D p571..p593 (`FUN_00874B00`, `FUN_0088F630`) and the other blind
   parameters.
