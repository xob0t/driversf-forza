# UPVH vehicle hierarchy, and why converted cars crashed

Updated: 2026-10-30

Static analysis of build 1.04. File offsets are relative to the UPVH payload
(Saloon 15, VEHC 3, spool 266, 0x42B0 bytes, unless stated). Code addresses
are VAs in `Driver.exe`. Evidence levels follow `dngvehicles.md`.

Tools: `dsff upvh summary <archive> --id N [--tables]` prints the
decoded hierarchy; `dsff upvh lint <archive> --id N --vanilla <vanilla.sp>`
checks a converted car against the rules below. Ghidra script:
`tools/ghidra/FindScalars.java` (scalar/FourCC search, vtables, callers).

## The crash: UIDs with bit 63 set (confirmed-static)

The engine stores a resource reference in one 8-byte slot. Before
resolution the slot holds the 64-bit UID. After resolution it holds a
pointer, and the engine marks that by setting the sign bit of the high
dword. Every resolver checks the sign first:

```c
if ((int)hi >= 0 || (lo & hi) == 0xFFFFFFFF)
    obj = lookup(lo, hi, type);   // FUN_00BAE770 -> hash table FUN_00BAEE00
else
    obj = (void *)lo;             // already resolved: use the low dword as a pointer
```

This pattern appears in FUN_00BAE010 (submodel, tag `_SML`), FUN_00BAC2B0
(material, `_MAT`), and the vertex and index buffer lookups in the SBMC loader
FUN_00BBB510 (`_VRT`, `_IDX`). The MODL loader calls the first two for every
submodel UID in every LOD (FUN_006A58F0).

So a UID whose byte 7 (in archive byte order) is 0x80 or more is not looked
up. The engine dereferences its low dword as an object pointer and crashes.

- All 46,813 resource UIDs in the vanilla archive (SBMC, VXCK, IXCK, TEXC,
  MACO) have byte 7 between 0x27 and 0x2D.
- `forza2dsf.py` used to build UIDs as `4 random bytes + struct.pack(">I", counter)`.
  Byte 7 was the counter's low byte, so UIDs 0x80 to 0xFF of each 256 were
  "resolved pointers". The vertex and index buffer UIDs are allocated after
  the submodels, so once a build makes more than 127 submodels, every
  submodel's buffer references are poisoned.
- Every archive in `out/` that crashed in game has such UIDs in the full
  entry (v-under: 130 submodels, buffer UIDs ...83 to ...86; v-keep, v-fill,
  v-both, 370z, e5-big). Every archive that loaded has none: the clone, e1,
  e4, k-body, k-none, hollow and hollow-l3 add fewer than 128 new UIDs. The
  exterior entry (41 or 42 submodels) never reached 0x80, which is why its
  full replacement always worked.

The "host submodels must exist" result was a coincidence. The builds that
kept host geometry happened to create fewer than 128 new UIDs. Nothing in
any game file references the host's submodel UIDs outside VEHC 3's own DRSP
and MODL. A sample of 24 submodel UIDs, in both half orders, appears nowhere
else in the install, `Driver.exe` included. UPVH holds no submodel, buffer,
material or texture UIDs.

The fix, vanilla-shaped UIDs (rule 1 below), loads a full replacement with
130+ new submodels (runs `20261022-004457`, `-004637`; `in-game-tests.md`).
Every ported car since uses it.

## Other cross-entry references (confirmed-static)

- The interior entry (N+1000) uses one material that only the full entry's
  MACO defines (266: `0012b0699ef6c428`).
- The exterior entry (N+2000) uses one texture that only the full entry's
  TEXC defines (266: `0012b06153f4c428`).
- UPVH's LOD-link table refers to interior and exterior MODL part UIDs
  (below). Rebuilding a MODL must keep its part records and their UIDs.
  `rebuild_modl` already does this.

## MODL part UIDs are global keys too (confirmed-static, fix confirmed-runtime)

The MODL loader registers every part UID in a global table (FUN_00494390,
table at 0x0113A1F8), like the resource UIDs. In the vanilla archive all
6,139 part UIDs across the 450 entries are unique, and none is also a
resource UID (`out/dmg/partscan.py`). Per vehicle the part UIDs appear only
in the three MODLs and in the full entry's UPVH, always with the 32-bit
halves swapped there (Desanne: 41 part UIDs, 56 UPVH references):

| UPVH field | Refers to |
| --- | --- |
| header +0x04 | MODL part 0 of the full entry (the model UID) |
| record +0x00 | a full-entry part (28 records on the Desanne; the 4 wheels share part 31) |
| LOD link, both halves | interior and exterior parts (8 links: 4 interior, 8 exterior references, 4 of them the wheel part) |
| attachment +0x10 | the exterior wheel part (all 6 attachments) |

`clone_vehicle.renew_uids` used to renew only resource UIDs, so every
ported car kept the Desanne's 41 part UIDs, and so did every other ported
car. In run `20261022-024010` (vanilla 266 spawned first, then 304, 305, 306)
the Skyline turned into a damaged gold Desanne in chase view right after
the probe called `VehicleLodSpooler.RequestVehicle(266)`, which re-spooled
266's interior and exterior (the log shows 266 `interior=true
exterior=true` afterwards, 305 evicted). The car's own UPVH then resolved
its LOD links and records through part UIDs that now also belonged to the
loaded 266 (confirmed-runtime for the symptom; which lookup returns the
other vehicle's part is a hypothesis: no Ghidra trace yet). The Skyline had
been damaged just before, but the damage was probably a coincidence: the
swap also explains the bumper hanging from the Desanne's own rear-bumper
part. Fix: `renew_uids(parts=True)` gives the MODL parts fresh vanilla-shaped
UIDs (byte 1 = 0x12, byte 7 = 0x2D) and rewrites the four UPVH fields
above (`remap_upvh_parts`), then checks that no old part UID remains in
any leaf in either half order. Result in game: see `in-game-tests.md`,
"Fix4".

Nothing else in the three cloned entries points at another vehicle: every
SBBH material and buffer, MDID entry, VXDP/IXDP dependency, MARP texture and
MODL LOD submodel UID resolves inside the clone, apart from the engine's
shared materials, which no vehicle defines (`out/dmg/uidscan.py` on
`out/fix3/ported-fix3.sp`).

`upvh.py lint` now checks this for every car (`foreign_uids`): no resource
or part UID the car defines may be defined by another vehicle, and no UID
it references (SBBH, MARP textures, MDID, VXDP/IXDP, MODL LOD lists, the
UPVH fields above) may be one only another vehicle defines. No vanilla
vehicle trips it; all three fix3 cars do (41 shared part UIDs each).
`port_car.py` runs it on every car, and checks part UIDs as well as
resource UIDs for clashes before writing. `upvh.py lint --allow-shared`
skips it for clones made without `--new-uids` (`port_car.py
--share-host-uids` passes it).

The DRSP loader FUN_00BBB8E0 makes seven passes over DRSP in a fixed tag
order (TEXC, GPCH, MACO, SBMC, VXCK, IXCK, MDID). Child order inside DRSP
doesn't matter. Resources go into global tables keyed by UID, so UIDs must
be unique across every loaded vehicle. Sharing them between a clone and its
source is fine because the data is identical.

## UPVH layout (confirmed-static)

All 150 vanilla UPVHs parse with this layout: the tables end exactly where the
PDL starts, and the PDL runs to the end of the payload. Record index fields
never exceed their table's count.

### Header, 0x60 bytes

| Offset | Type | Saloon 15 | Meaning |
| ---: | --- | --- | --- |
| 0x00 | char[4] | `AWHF` | magic |
| 0x04 | u32 | 0x28954752 | model UID, high dword |
| 0x08 | u32 | 0x31691200 | model UID, low dword; together MODL part 0's UID in all 150 |
| 0x0C | u32 | 26 | hierarchy type |
| 0x10 | u32 | 77 | record count |
| 0x14 | u32 | 266 | hierarchy ID = spool ID |
| 0x18 | u32 | 0xC80 | PDL size |
| 0x1C | 10 bytes | 0 | |
| 0x26 | u16 | 0 | animation blob size in 0x14-byte units (13 vehicles, 4 to 24) |
| 0x28 | u16 | 28 | markers |
| 0x2A | u8 | 20 | hinges |
| 0x2B | u8 | 24 | bodies |
| 0x2C | u8 | 2 | seats |
| 0x2D | u8 | 2 | mirrors |
| 0x32 | u16 | 15 | lights |
| 0x34 | u16 | 0x0CE8 | in 148 of 150; 192 and 257 hold other values here and at 0x54 |
| 0x36 | u16 | 8 | LOD links |
| 0x38 | 24 bytes | 0xFF | |
| 0x50 | u16 | 6 | attachments |
| 0x52 | u16 | 4 | wheels (6 in 10 vehicles: VEHC 96, 210, 270, 330, 345, 399, 414, 417, 423, 432) |
| 0x54 | 12 bytes | `ecf31c3d 4dcafb46 82e5e6a5` | the same in 148 of 150 (not 192, 257) |

DPL kept a format byte in the low byte of 0x04. DSF doesn't: 0x52 is part of
the UID.

### Tables, in file order after the records

| Table | Entry | Saloon 15 at | Contents |
| --- | ---: | ---: | --- |
| records | 0x50 | 0x0060 | see below |
| hinges | 0x30 | 0x1870 | vec4 pivot, vec4 axis, f32 limit, f32 param, 8 zero bytes |
| bodies | 0xB0 | 0x1C30 | 3x4 rotation, vec4 position, vec4 (inertia xyz, mass), 4x4 matrix, 0x20 zero bytes |
| markers | 0x20 | 0x2CB0 | vec4 position, vec4 quaternion |
| seats | 0x30 | 0x3030 | really gauges (`cockpit.md`, "Gauges"): vec4 needle pivot, vec4 dial quaternion, u32 gauge id (1 tachometer, 2 speedometer), u32 sweep (degrees per 5000 rpm / per 80 mph), u8 hide flag, 7 zero bytes |
| mirrors | 0x10 | 0x3090 | vec4 position |
| anim | 0x14 | 0x30B0 | undecoded; floats like 1/3, 2π, 0.61 |
| lights | 0x40 | 0x30B0 | vec4 position, vec4 direction, f32 π, f32 π, u32 id, u32 flags, vec4 junk |
| LOD links | 0x10 | 0x3470 | u64 interior part UID, u64 exterior part UID, halves swapped, -1 = none |
| attachments | 0x20 | 0x34F0 | vec4 position, 16 bytes (an exterior part UID, then junk) |
| wheels | 0x20 | 0x35B0 | vec4 hub position, vec4 quaternion ((0,1,0,0) left, identity right) |
| PDL | | 0x3630 | collision, see below |

Several fields hold values that look like leftover exporter memory: record
+0x44 to +0x4F, the fourth vec4 of each light, and half of each attachment.

Saloon 15's hinges, from the records that use them:

- doors: vertical axes at (±0.923, -0.052, 1.001) and (±0.922, -0.052, -0.108)
- bonnet: hinge 12, x axis, param 24
- boot: hinge 17, x axis, param 15
- bumpers: hinges 11 and 19, z axis, limit 0.1257, param 4000
- wheels: hinges 4, 6, 14, 15, limit 0, at the hub positions

The wheel hubs appear twice, as hinges 4, 6, 14 and 15 (0x1930, 0x1990,
0x1B10, 0x1B40) and as the wheel table (0x35B0 to 0x3610).
`forza2dsf.patch_hubs` takes the hubs from the host's own wheel table and
moves every hinge pivot within 10 cm of a hub with it (on the Desanne
hinges 3 to 6, 14 and 15; `hosts.md`, "What used to assume the Desanne").
Hinges 3 and 5 (0x1900, 0x1960, records 32 and 34, roles 156 and
155) pivot next to the front hubs, so moving them with the wheels is
probably right.

### Records, 0x50 bytes, depth-first

| Offset | Type | Meaning | Evidence |
| ---: | --- | --- | --- |
| 0x00 | u64 | part UID with halves swapped (MODL order = bytes 4..7, 0..3), or -1 | confirmed-static |
| 0x08 | u16 | record type: 4 root, 5 chassis, 1 part, 2 wheel, 3 marker, 17 light, 18 seat | confirmed-static |
| 0x0A | u16 | role enum: wheels 27 to 30, body part kinds, marker kinds. Not a submodel index (tested). | confirmed-static |
| 0x0C | u16 | bit field: bit 0 the record has a part (type 1) child, bit 1 a wheel below it, bit 2 on most part records with a body (doors, mirrors, glass, the chassis part; not bonnets, boots, bumpers), bit 4 the root (23). Bits 0 and 1 hold on all 10,959 vanilla records; a door left with bit 0 and no part child crashed `forceOpenPanels` (`damage.md`, "Mirrors that hang beside the car") | bits 0, 1 confirmed-static and runtime; bit 2 unknown |
| 0x0E | u16 | flags (0x4000 group, low bits 0..3) | unknown |
| 0x10, 0x12 | u16 | small values and bit flags | unknown |
| 0x14 | u16 | subtree size, self included (root = record count) | confirmed-static |
| 0x16, 0x18 | u16 | 0x18: how many hinged records among this one and its ancestors end their subtree where this one's ends (the hinge transforms popped there); 0x16 the same on a leaf (not the list's last), else 0. 10,956 of 10,959 vanilla records (recounted 2026-10-29 with `damage._tree_fields`); three headlights on hosts 210 and 217 differ. Left stale by a record move, the game crashed (`damage.md`, "Mirrors that hang beside the car") | confirmed-static; the crash confirmed-runtime |
| 0x1A | i16 | marker index | confirmed-static |
| 0x1C | i16 | hinge index | confirmed-static |
| 0x1E | i16 | body index | confirmed-static |
| 0x20 | i32 | collision model offset from PDL+0x10, or -1 | confirmed-static |
| 0x26 | i8 | seat index | confirmed-static |
| 0x27 | i8 | mirror index | confirmed-static |
| 0x2A | i16 | running number over part and wheel records (wheels share 0): the panel-data index (vertex semantic 9 byte 1, `damage.md`) | confirmed-static |
| 0x2C | u8, u8 | 1 or 3; 0 or 4 | unknown |
| 0x30 | i16 | light index | confirmed-static |
| 0x34 | i16 | running number over most part records: the panel-transform slot (vertex semantic 9 byte 0, `damage.md`) | confirmed-static |
| 0x3E | i16 | LOD-link index | confirmed-static |
| 0x40 | i16 | attachment index | confirmed-static |
| 0x42 | i16 | wheel index | confirmed-static |
| 0x44 | 12 bytes | usually 1, 4, then an exporter pointer | junk |

Marker roles seen (type 3 records): 47 steering-wheel hub (`cockpit.md`);
**10 driver seat and 11 passenger seat** (Desanne (±0.425, -0.306, 0.067);
role 10's x is within 2 cm of the steering marker's in all 147 vanilla cars with one, so it is
the driver's side; the eye from VO3D 430..432 sits (0, 0.737, -0.080) above
it, fleet median, y 0.64 to 0.84; `out/glassdbg/seat_survey.txt`). The game
seats the driver model there (confirmed-static by the fleet relation;
in-game: converted cars kept the Desanne's seat and the driver's head stuck
out of their lower roofs, `img/skyline-chase-v1.png`). Role 92 sits on the
centre line near the roof (Desanne y 0.759), 63/64 front and rear on the
centre line with tilted quaternions, 86 four roof/mirror corners; not
decoded. Records of type 1 role 8 are glass panels (Desanne record 29
windscreen, 61 rear window), which the glass vertices bind to through
semantic 9 (`vehicle-model.md`).

Saloon 15 has 77 records: root, chassis (MODL part 0), 26 parts, 4 wheels
(all pointing at MODL part 31), 28 markers, 15 lights, 2 seats. MODL parts 7,
8, 23 and 25 have no record. They are the parts with a shadow (mask 1) LOD,
apart from 0 and 1.

### Chassis variants (confirmed-static; the symptom confirmed-runtime)

39 vanilla vehicles have more than one chassis record (type 5): roles 257,
258, 259, 513, 1280, 1281, 1282 or 1 next to the usual 256 (Taxi 01-04,
Tanner, 172, 210, 211, 217 Rally 04 with four...). Each has its own full-entry
MODL part and its own LOD link to an interior and an exterior part. On 172
(70's Muscle 04) the role-257 parts hold a second copy of the body (part 0's
triangles plus a few more, in all three entries). The game draws that copy:
converted cars filled only part 0, so the Forza Cuda on 172 showed only its
wheels, the driver and his hands (runs 20261022-163835, -164332, -164459,
`out/cuda/variants.jpg`); on 193 (one chassis record) it rendered.
`src/dsfforza/convert/chassis_variants.py` copies part 0's submodels (new UIDs, same
vertex and index ranges) into every extra chassis part after conversion, and
`upvh.py lint` fails a car whose variant parts are empty while part 0 is
not. 24 of the 137 usable hosts have variant parts (`uv run
dsff chassis_variants <vanilla> --ids ...`). In the 40-car test drive, 8
cars sat on such hosts; only the 257-role ones are known to go invisible
(the Cuda; the 250 GTO on 210 is untested); cars on 1281 hosts (M3 E30, RX-7,
S2000, Firebird, 300SL, 2000GT) were not reported, so the game probably
draws 257 but not 1281 by default (hypothesis). Which variant the game
picks, and when, is not traced.

### Exhaust markers (confirmed-static; fix confirmed-runtime)

**Where the game reads them.** `FUN_0088E310` (the UPVH parser, decompiled
in `out/ghidra/gauges7`) takes every marker record (type 3) with role 0x4D
(77) as an exhaust: it copies the marker's position (+ the model offset at
spec+0x35F0) and quaternion into a 0x2C-byte slot at spec+0x39A0, with the
record index and the body of the nearest ancestor that has one, and counts
the slots in the byte at spec+0x4931. The role-0x57 array starts at
spec+0x3A50, so there is room for 4 slots and no bounds check.
`FUN_00881DD0` builds a matrix from each slot's quaternion and position and
emits particle effect 13 there (`FUN_00498F60`), with an intensity that falls
with the car's speed: the smoke shows most at a standstill. The engine
sound's emitter B sits at the first slot (`engine-sound.md`).

**Vanilla fleet** (`out/exh/vanilla_markers.py`): 0, 1, 2 or 4 exhaust markers
(13, 33, 80 and 24 cars), one per tailpipe, as children of the exhaust parts
(role 136 at -x, 15 at +x) or of the body (38). Rear pipes carry the
identity quaternion; the flow is the marker's local -z: Classic 06's
right-hand side exit has (0, -0.71, 0, 0.71), which turns -z onto +x, and
turned-down pipes tilt about x (Classic 04: (-0.27, 0, 0, 0.96)).

**Why ports were wrong.** The converter left the host's markers alone, so
smoke and backfire came out where the host's pipes were. The Focus ST
(host 127, Giulietta) kept two markers at x ±0.49, 15 cm inside the bumper;
its only outlet is the twin-tip trapezoid in the middle of the diffuser
(`img/exhaust-focusst-before-after.jpg`).

**Fix** (`src/dsfforza/convert/exhaust.py`, last UPVH edit in
`forza2dsf.convert`): the Forza `exhaust*` sections of the vanilla variant
give the openings: each connected piece's outermost 4 cm along its flow
(straight back, or out of the side when the piece ends within 8 cm of the
body's side: GT1, V8 Supercar and DTM side exits), split where the
triangles don't connect; inner tubes, discs and brackets behind an opening
are dropped; ends far inside the body or 15 cm ahead of the rearmost tip
(silencers, pipe ends at the bumper corners) don't count. The host's
markers become one per opening, up to 4: copies of the host's own marker
records are inserted next to them (same parent, so the effect still follows
the exhaust part's body), extras are removed (innermost first; ancestors'
subtree sizes, the record count and later marker indices follow). A twin tip
modelled as one piece (the Focus) takes two markers at its halves when the
host has two. No tip: the host's markers move with the host-body-to-Forza-body
box map of the damage plan.

**Survey** over the 573 cars of `out/final` (`dsff exhaust survey`,
`out/exh/survey5.json`), per car the worst marker's distance to the nearest
Forza opening:

| | cars | median | 90th pct | max | over 10 cm |
| --- | ---: | ---: | ---: | ---: | ---: |
| before (host's markers) | 524 | 0.29 m | 1.55 m | 3.31 m | 424 |
| after | 524 | 0.00 m | 0.00 m | 0.11 m | 1 |

The remaining offsets are the two halves of twin openings (a quarter of the
opening's width from its centre). Openings per car: 132 one, 312 two, 4
three, 87 four, 1 ten (the 250 Testa Rossa's side pipes, 4 markers). Marker
counts changed on 200 cars (4 -> 2: 67, 2 -> 1: 69, 2 -> 4: 44, 1 -> 2: 11,
others 9). No tip on 37 cars: 29 have no `exhaust*` section (exhaust modelled
in the body: F40, Viper, 360, SLR, LMPs...), 8 have one with no opening the
rules accept (Leaf, CT200h, RAV4 Sport, R15 TDI, Sagaris, Corvette
Stingray 67, Kenworth T440, the tour bus: a flat panel, pipes along the sills
or far inside the body, flows other than back or sideways); 34 of the 37 get
the box-mapped host markers. The 15 cars on hosts
without exhaust markers (Classic 10, Bus 01, Saloon 13, Mini Car 06) stay
without exhaust effects, as their hosts are (issue #64; the 37 tipless cars:
#65).

**In game** (VM, `out/exh/before` built with `--no-exhaust` against
`out/exh/after`, IDs 1-6: Focus ST, Corsa VXR, M3 E92, 370Z, GT-R GT1,
MX-5 Miata; `run-series.ps1 -Vm -Views exhaust`, the new fixed probe view
low behind the tailpipes, `wheels` for the GT-R's side exits): the idle smoke
now comes out of the Forza tips: the Focus's twin trapezoid and the Corsa's
centre triangle instead of the bumper corners, all four M3 tips (two marker
records added), the Miata's single right-hand pipe instead of nothing (host
126's marker is a left side exit), the GT-R's side pipes behind the front
wheels instead of under its diffuser (two of four records dropped). No crash
from the changed record counts. Runs `vm-20261025-080435`/`-080641`,
`vm-20261025-090734`, `vm-20261026-115910`/`-120124`, `-121526`, `-125136`
(`img/exhaust-focusst-before-after.jpg`, `img/exhaust-ingame-before-after.jpg`;
offline markers per car: `img/exhaust-offline-markers.png`).

**Pipes outside the exhaust sections (issue #65).** 29 cars model their
pipes in the bumper or body (no `exhaust*` section), 8 have exhaust sections
with no opening the rules accept. Seen on the F40 in game: the box-mapped
host markers put the smoke either side of the plate, the car's three pipes
sit together low in the centre. Those pipes are three chrome tubes of
`bumperRa` (x -0.08, 0, +0.08; 10 x 5 and 6 x 6 cm; 3.7 cm long), each its
own connected piece whose walls face every way round z. `exhaust.find_pipes`
takes such pieces when the exhaust sections give no tip: a trim or chrome
subsection's connected piece (finest LOD) whose wall triangles (|n . axis|
< 0.5, 30% of its area or more) cover at least 10 of 12 directions round the
flow axis (back, or x for a side exit), 2 to 25 cm across, 1.5 cm long or
more, in the lower 45% of the body (side exits the lower 25%), ending within
0.3 m of the body's rear nearby or within 8 cm of its side, with no lamp or
glass triangle inside its cross-section (a lamp's bezel); not
`undercarriage` (springs and dampers are tubes too) or `weatherstrip`.
The pieces then go through the same opening rules as exhaust sections.

Survey (`dsff exhaust survey` of `out/build/work`, master against this
change): tipless cars 37 to 17, every other car's tips as before. The 20 cars
(contact sheet: rear, rear three-quarter and side renders of the Forza body
with the openings marked, `out/e65/sheet/`): rear pipes on the F40 (3
markers, host 225 has 2), 360 and 360 Challenge (4), Corvette 60 (bumper
ends; host 161 has no exhaust marker, #64), Genesis Coupe RMR, Exige 240,
Speedster (two stacked in the centre), R390 (bumper corners), R390 GT1,
Speed 8, RAV4 Sport; side exits under the doors on the Cerbera (4), Astra V8,
C5-R, both Fairlady Z race cars, C-Class DTM (one, right), Esperante GTLM and
both 908s (ahead of the rear wheels; not checked against photos). Ruled out by
the length and side-height limits on the first contact sheet: a 3 x 2 cm
chrome ring by the R390's badge (0.6 cm long), the Esperante's front-fender
vents (47% up the body), two 1.4 cm `matte_colors` rings on the 908 11's tail.
Still tipless, no tube found (not looked into car by car): Viper SRT-10,
SLR, Continental, TT-R, CC8, HKS, Eliminator, Skyline 03, LMP 03 and 905, and
of the 8 with unaccepted exhaust sections all but the RAV4 (Leaf, CT200h, R15
TDI, Sagaris, Corvette Stingray 67, Kenworth, tour bus). They keep the
box-mapped host markers.

**Copied markers and the tree fields (issue #273).** `insert_marker` copied
the record whole, so under a hinged rear bumper (role 36) whose last leaf was
the marker, both the old leaf and the copy popped the hinge (0, 1, 1); vanilla
writes (0, 0, 0) on a leaf before the last. `insert_marker` and
`remove_marker` now write +0x0C bit 0 and +0x16/+0x18 again from
`damage._tree_fields` on every record that followed them (`exhaust._retree`,
as `damage.move_subtree` does). AUD_R8_08 (host 139): `upvh lint` failed on
record 71 in master's build, passes now; inserting and then removing a copy
gives back the host's bytes (hosts 139, 214, 127, 225).

### PDL (collision)

`{u32 1, u32 model count, u32 size, u32 0x10}`, then the models. Each model
is `{u32 0x10 + n*0x60, u32 n*0x60, f32 radius, u32 n}` followed by n
primitives of 0x60: a 4x4 matrix, `{u32 0x60, u32 flags (0x900, 0x901), 8
bytes}`, vec4 extents, and more. Records point at models through +0x20. The
root's model (5 primitives) comes last. In 149 vehicles the model count
equals the number of records with a collision offset.

## Engine side

| What | Where | Evidence |
| --- | --- | --- |
| UPVH resource vtable 0x00E39630; slot 2 (FUN_0086C0C0) passes the payload to the vehicle spec manager at `[Engine+0x778]`, vtable +0x48; slot 4 releases it (+0x4C) | FUN_0086C0C0, FUN_0086C110, FUN_0086C160 | confirmed-static |
| MODL resource vtable 0x00DDA2B0 (GetType 0x00493FC0); load FUN_004940C0 → FUN_006A5A70 | | confirmed-static |
| MODL load: offsets become pointers, each part UID is registered in a global table (FUN_00494390 at 0x0113A1F8, 85,000 buckets), each LOD's submodel UIDs go into the global fix-up queue at 0x0186A6B8 (count 0x0188A6B8) | FUN_006A52B0, FUN_006A58F0 | confirmed-static |
| MODL LOD: if the first per-submodel u32 has bit 0 clear, submodels whose material reports 1 for property DAT_00F3FFB4 are sorted to the front | FUN_006A58F0 | confirmed-static; meaning hypothesis |
| DRSP loader: seven passes in tag order TEXC, GPCH, MACO, SBMC, VXCK, IXCK, MDID | FUN_00BBB8E0 | confirmed-static |
| SBBH v3/v4 parse: two vertex stream UIDs (+0x10, +0x18; the second is 0 in all 43,790 vanilla SBBHs), base vertex, first index, index count / 3, +0x40 copied into the submodel | FUN_00BB9990, FUN_00BB9A60 | confirmed-static |
| Submodel objects live in a UID hash table (0x3AA5 buckets) | FUN_00BBBE40 | confirmed-static |
| MDID is copied as a plain array of `size / 8` UIDs | FUN_00BB9F00 | confirmed-static |

Not traced yet: what the spec manager does with UPVH, and how it links
records to MODL parts at runtime. The data side says it goes by UID. The
interior and exterior are linked through the LOD-link table.

## Rules for converted cars

1. Every new resource and part UID must have byte 7 below 0x80. Copy the
   vanilla shape, as `forza2dsf.Builder.new_uid` and
   `clone_vehicle.vanilla_like_uid` do: `[counter<<4] 0x12 [counter>>4]`,
   a per-builder 4-byte prefix with byte 6's top bit clear, then byte 7 =
   0x2D (confirmed-runtime, runs `20261022-004457`, `-004637`). Uniqueness
   comes from the prefix, and `upvh.py lint` checks it (rule 5). An earlier
   rule here, a high byte of 0x2E or more with arbitrary bytes 0..6, crashed
   too according to `new_uid`'s docstring (no run folder recorded).
2. Keep the full entry's TEXC and MACO, or at least the material and
   texture the interior and exterior take from it.
3. Keep every MODL part record, in all three entries. UPVH refers to
   kind-0 parts by record UID and to kind-1/2 parts through the LOD links,
   so when part UIDs change, the UPVH fields that hold them must change too
   (`clone_vehicle.remap_upvh_parts`).
4. The host's SBMC, VXCK and IXCK can go.
5. Keep UIDs unique across the archive, part UIDs included. The resource
   and part tables are global (`upvh.py lint` checks it).

## Test archives (superseded)

`out/upvh/uidtest.py` built two variants of a 130-submodel full
replacement: `out/uid-safe.sp` (UIDs `<u32 seed + 16n> <u32 0x2E000000 |
seed24>`) and `out/uid-signbit.sp` (the same with bit 63 set). Neither run
is recorded. The vanilla-shaped scheme above was tested instead and loads;
a byte-7 = 0x2E scheme like uid-safe's is the one `new_uid`'s docstring says
crashed.

## Open

1. The spec manager's UPVH consumer (Engine+0x778 vtable +0x48), and the
   runtime use of record fields 0x0C (bits past 1), 0x0E to 0x12 and 0x2C.
   Decoded since: 0x0C bits 0 and 1 and the hinge pop counts at 0x16/0x18
   (#196, `damage.md`), 0x2A and 0x34 (the shader's panel indices,
   `damage.md`).
2. Light flags (decoded in `lights.md` but b0), attachment kinds (the gauge entry's second u32 is the
   needle sweep: `cockpit.md`, "Gauges"). Steering hinge, role-47 marker and head camera:
   `cockpit.md`.
3. The animation blob (13 vehicles).
