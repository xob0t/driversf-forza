# UPVH vehicle hierarchy, and why converted cars crashed

Updated: 2026-10-22

Static analysis of build 1.04. File offsets are relative to the UPVH payload
(Saloon 15, VEHC 3, spool 266, 0x42B0 bytes, unless stated). Code addresses
are VAs in `Driver.exe`. Evidence levels follow `dngvehicles.md`.

Tools: `tools/upvh.py summary <archive> --id N [--tables]` prints the
decoded hierarchy; `tools/upvh.py lint <archive> --id N --stock <stock.sp>`
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

- All 46,813 resource UIDs in the stock archive (SBMC, VXCK, IXCK, TEXC,
  MACO) have byte 7 between 0x27 and 0x2D.
- `forza2dsf.py` builds UIDs as `4 random bytes + struct.pack(">I", counter)`.
  Byte 7 is the counter's low byte, so UIDs 0x80 to 0xFF of each 256 are
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

Untested in game: `out/uid-safe.sp` and `out/uid-signbit.sp` (see the end).

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
table at 0x0113A1F8), like the resource UIDs. In the stock archive all
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
car. In run `20261022-024010` (stock 266 spawned first, then 304, 305, 306)
the Skyline turned into a damaged gold Desanne in chase view right after
the probe called `VehicleLodSpooler.RequestVehicle(266)`, which re-spooled
266's interior and exterior (the log shows 266 `interior=true
exterior=true` afterwards, 305 evicted). The car's own UPVH then resolved
its LOD links and records through part UIDs that now also belonged to the
loaded 266 (confirmed-runtime for the symptom; which lookup returns the
other vehicle's part is a hypothesis: no Ghidra trace yet). The Skyline had
been damaged just before, but the damage was probably a coincidence: the
swap also explains the bumper hanging from the Desanne's own rear-bumper
part. Fix: `renew_uids(parts=True)` gives the MODL parts fresh stock-shaped
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
UPVH fields above) may be one only another vehicle defines. No stock
vehicle trips it; all three fix3 cars do (41 shared part UIDs each).
`port_car.py` runs it on every car, and checks part UIDs as well as
resource UIDs for clashes before writing. `--allow-shared` skips it for
clones made without `--new-uids`.

The DRSP loader FUN_00BBB8E0 makes seven passes over DRSP in a fixed tag
order (TEXC, GPCH, MACO, SBMC, VXCK, IXCK, MDID). Child order inside DRSP
doesn't matter. Resources go into global tables keyed by UID, so UIDs must
be unique across every loaded vehicle. Sharing them between a clone and its
source is fine because the data is identical.

## UPVH layout (confirmed-static)

All 150 stock UPVHs parse with this layout: the tables end exactly where the
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
| 0x34 | u16 | 0x0CE8 | constant |
| 0x36 | u16 | 8 | LOD links |
| 0x38 | 24 bytes | 0xFF | |
| 0x50 | u16 | 6 | attachments |
| 0x52 | u16 | 4 | wheels (6 in VEHC 96) |
| 0x54 | 12 bytes | `ecf31c3d 4dcafb46 82e5e6a5` | constant in all vehicles |

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
`forza2dsf.patch_hubs` matches by value and also moves hinges 3 and 5
(0x1900, 0x1960, records 32 and 34, roles 156 and 155). Those pivot next to
the front hubs, so moving them with the wheels is probably right.

### Records, 0x50 bytes, depth-first

| Offset | Type | Meaning | Evidence |
| ---: | --- | --- | --- |
| 0x00 | u64 | part UID with halves swapped (MODL order = bytes 4..7, 0..3), or -1 | confirmed-static |
| 0x08 | u16 | record type: 4 root, 5 chassis, 1 part, 2 wheel, 3 marker, 17 light, 18 seat | confirmed-static |
| 0x0A | u16 | role enum: wheels 27 to 30, body part kinds, marker kinds. Not a submodel index (tested). | confirmed-static |
| 0x0C | u16 | 0..7, 23 on the root | unknown |
| 0x0E | u16 | flags (0x4000 group, low bits 0..3) | unknown |
| 0x10, 0x12 | u16 | small values and bit flags | unknown |
| 0x14 | u16 | subtree size, self included (root = record count) | confirmed-static |
| 0x16, 0x18 | u16 | 0..3 | unknown |
| 0x1A | i16 | marker index | confirmed-static |
| 0x1C | i16 | hinge index | confirmed-static |
| 0x1E | i16 | body index | confirmed-static |
| 0x20 | i32 | collision model offset from PDL+0x10, or -1 | confirmed-static |
| 0x26 | i8 | seat index | confirmed-static |
| 0x27 | i8 | mirror index | confirmed-static |
| 0x2A | i16 | running number over part and wheel records (wheels share 0) | confirmed-static, meaning unknown |
| 0x2C | u8, u8 | 1 or 3; 0 or 4 | unknown |
| 0x30 | i16 | light index | confirmed-static |
| 0x34 | i16 | running number over most part records | unknown |
| 0x3E | i16 | LOD-link index | confirmed-static |
| 0x40 | i16 | attachment index | confirmed-static |
| 0x42 | i16 | wheel index | confirmed-static |
| 0x44 | 12 bytes | usually 1, 4, then an exporter pointer | junk |

Marker roles seen (type 3 records): 47 steering-wheel hub (`cockpit.md`);
**10 driver seat and 11 passenger seat** (Desanne (±0.425, -0.306, 0.067);
role 10's x is within 2 cm of the steering marker's in all 147 stock cars with one, so it is
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

39 stock vehicles have more than one chassis record (type 5): roles 257,
258, 259, 513, 1280, 1281, 1282 or 1 next to the usual 256 (Taxi 01-04,
Tanner, 172, 210, 211, 217 Rally 04 with four...). Each has its own full-entry
MODL part and its own LOD link to an interior and an exterior part. On 172
(70's Muscle 04) the role-257 parts hold a second copy of the body (part 0's
triangles plus a few more, in all three entries). The game draws that copy:
converted cars filled only part 0, so the Forza Cuda on 172 showed only its
wheels, the driver and his hands (runs 20261022-163835, -164332, -164459,
`out/cuda/variants.jpg`); on 193 (one chassis record) it rendered.
`tools/chassis_variants.py` copies part 0's submodels (new UIDs, same
vertex and index ranges) into every extra chassis part after conversion, and
`upvh.py lint` fails a car whose variant parts are empty while part 0 is
not. 24 of the 137 usable hosts have variant parts (`python
tools/chassis_variants.py <stock> --ids ...`). In the 40-car test drive, 8
cars sat on such hosts; only the 257-role ones are known to go invisible
(the Cuda; the 250 GTO on 210 is untested); cars on 1281 hosts (M3 E30, RX-7,
S2000, Firebird, 300SL, 2000GT) were not reported, so the game probably
draws 257 but not 1281 by default (hypothesis). Which variant the game
picks, and when, is not traced.

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
| SBBH v3/v4 parse: two vertex stream UIDs (+0x10, +0x18), base vertex, first index, index count / 3, +0x40 copied into the submodel | FUN_00BB9990, FUN_00BB9A60 | confirmed-static |
| Submodel objects live in a UID hash table (0x3AA5 buckets) | FUN_00BBBE40 | confirmed-static |
| MDID is copied as a plain array of `size / 8` UIDs | FUN_00BB9F00 | confirmed-static |

Not traced yet: what the spec manager does with UPVH, and how it links
records to MODL parts at runtime. The data side says it goes by UID. The
interior and exterior are linked through the LOD-link table.

## Rules for converted cars

1. Every new resource UID must have byte 7 below 0x80. Copy the stock shape:
   a varying low dword, and a fixed high dword whose top byte is 0x2E or
   higher but below 0x80, so new UIDs can't collide with stock ones.
2. Keep the full entry's TEXC and MACO, or at least the material and
   texture the interior and exterior take from it.
3. Keep every MODL part record, in all three entries. UPVH refers to
   kind-0 parts by record UID and to kind-1/2 parts through the LOD links,
   so when part UIDs change, the UPVH fields that hold them must change too
   (`clone_vehicle.remap_upvh_parts`).
4. The host's SBMC, VXCK and IXCK can go.
5. Keep UIDs unique across the archive, part UIDs included. The resource
   and part tables are global (`upvh.py lint` checks it).

## Test archives (built, not yet run)

Both come from `out/upvh/uidtest.py`, which runs `forza2dsf.py` with only
`Builder.new_uid` replaced. Both use the same command, a full replacement
that removes host SBMC/VXCK/IXCK (the mode that always crashed), with 130
submodels, the same count as v-under:

```sh
python out/upvh/uidtest.py safe|signbit out/dngvehicles.clone.sp out/uid-<mode>.sp --id 304 \
    --car out/fh1/NIS_370Z_10 --name NIS_370Z_10 --keep-host-materials \
    --lods 4:3,8:4,16:5 --shadow-lod 5 --exterior-lod 3
```

- `out/uid-safe.sp`: UIDs `<u32 seed + 16n> <u32 0x2E000000 | seed24>`. Expected
  to load and drive.
- `out/uid-signbit.sp`: the same, with bit 63 set on every new UID. Expected
  to crash on spooling.

## Open

1. The spec manager's UPVH consumer (Engine+0x778 vtable +0x48), and the
   runtime use of record fields 0x0C to 0x18, 0x2A, 0x2C and 0x34.
2. Light flags, attachment kinds (the gauge entry's second u32 is the
   needle sweep: `cockpit.md`, "Gauges"). Steering hinge, role-47 marker and head camera:
   `cockpit.md`.
3. The animation blob (13 vehicles).
