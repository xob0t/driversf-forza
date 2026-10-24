# Vehicle spool size limits

Updated: 2026-10-24

Where `Driver.exe` 1.04 sizes the memory a spooled vehicle uses, which of
those sizes a bigger VEHC can hit, and how to raise each one. Addresses are
VAs (image base 0x00400000, RVA = VA - 0x400000). Evidence levels follow
`dngvehicles.md`. Everything here is confirmed-static unless marked
otherwise; see "Test results" for what was tested in game.

Ghidra runs used `tools/ghidra/SpoolExplore.java` (ExploreAddresses plus
`dec:`, `asm:` with instruction bytes, `refs:`, `scalar:` and `range:`
modes) against the `-b` project copy. Outputs are in `out/ghidra/spool-*`,
`gfx-range`, `chunkreader`, `modl*`, `uidlist*`, `poolasm`, `buf280`.

## Summary

| Limit | Stock value | Hit by a bigger VEHC? | Raise by |
| --- | --- | --- | --- |
| NONR buffer per slot | SSLP +0x08/+0x0C/+0x10: 0x6D000, 0x1000, 0x1000 | yes, heap overflow if NONR is larger | data: SSLP header |
| Shared spool read buffer | 0x280000 static, heap above that | VEHC > 0x27F000 takes a path no shipped file uses | not needed: the pad test passed ("Test results") |
| Entries per CHNK | 1100 | DRSP children (submodels + buffers + materials), root entries | code: `dsfforza.asi` `[archive]` (below); without it the root caps the archive at 365 vehicles |
| File positions | below 2 GiB (signed clamp in the seek) | archives past about 480 ported cars | code: `dsfforza.asi` `[archive] large_files`, up to 4 GiB |
| Graphics pool slot, kind 0 | 239,664 bytes, 1872 range nodes | only with far more render objects (about 1800) | code: `.data` table |
| Graphics pool slot, kind 1/2 | 4 MiB, 32768 nodes | no | not needed |
| Vertex and index buffers | exact size, D3D9 managed pool | no fixed cap; indices are always 16-bit | not needed |
| Global UID list | 16384 entries, write-only | fills faster with more submodels | code: NOP the counter (hypothesis that it matters) |
| Resource hash arena | 45,000 nodes, global | more buffers/textures per car use more | not patched here |
| Slots per kind | SSLP +0x18: 35, 2, 2 | no | data plus pool table; not needed for detail |

The MDXN size itself has no separate limit. The 0x29B000 MDXN crash was
first blamed on the 0x280000 read buffer path; the pad tests ("Test
results") ruled that out, and the UID scheme of that build is the likely
cause.

## Correction: FUN_005A0DE0 measures SSIC, not vehicles

`exe-vehicles.md` says FUN_005A0DE0 records the largest MDXN and NONR
across the root entries. It doesn't. FUN_005A11E0 calls it with the SSIC
entry index, and FUN_008FC5C0 opens that entry as a nested CHNK, so it walks
SSIC's two children: MDXN 0x9CB800 and NONR 0x13EAA0. The results land at
spool object +0x72F0/+0x72F4 (FUN_005A11E0 runs with `this` = object +0xC)
and only size SSIC's own load. Nothing about vehicle VEHC sizes comes from
there.

## Spool object layout (vehicle package)

The object FUN_005A1B40 constructs is 0x734C bytes, one per package type
(spool manager array, FUN_0059D740 `type * 0x734C`). Vehicles are package
type 0 (FUN_005A1520). Fields used below:

| Offset | Meaning |
| --- | --- |
| +0x14 | root chunk reader (0x5618 bytes) |
| +0x562C | SSIC handler |
| +0x5630 | slot handlers, 64 pointers, count at +0x5730 |
| +0x5734 + k*0x104 | per-kind slot lists, 64 pointers, count at +0x100 |
| +0x72F8 / +0x72FC | SSLP header / records |
| +0x7300 | package type |

## NONR buffer per slot (confirmed-static)

At package init (FUN_005A2470 state 4), FUN_005A1420 creates one handler per
slot. For kind k it reads the u32 at SSLP header +0x08 + 4k and passes it to
FUN_0059F9C0 (`ECX = Engine+0x15C`, args: out, package type, 0, NONR size,
slot index). That builds a SceneModelType2 (vtable 0x00E37118), and
SceneModelType2_InitializeResourceProviders (0x00858CD0) allocates the NONR
buffer once with FUN_00859410: `_aligned_malloc(size, 0x80)` (no allocator is
passed, so it is the CRT heap).

On every load, FUN_00858A70 copies the VEHC's NONR into that buffer:

```c
_memcpy(*(void **)(model + 0x14), entry_data(i), entry_size(i));   // no capacity check
```

So the SSLP header fields are:

| Offset | Value | Meaning |
| --- | --- | --- |
| +0x08 | 0x6D000 | NONR buffer, kind 0 (largest stock NONR 0x6CE30) |
| +0x0C | 0x1000 | NONR buffer, kind 1 (largest 0x480) |
| +0x10 | 0x1000 | NONR buffer, kind 2 (largest 0x620) |
| +0x14 | 0 | kind 3, unused |
| +0x04 | 0xEEB102 | read by no code found (scalar scans of 0x72F4/0x72F8 cover every SSLP accessor). 35 x 0x6D000 + 2 x 0x1000 + 2 x 0x1000 = 0xEEB000, so it is probably the packager's total plus 0x102 |

FUN_005A0AF0 also reads +0x08 + 4k, only for a float load estimate.

**Raise by data.** Set +0x08 (and +0x0C/+0x10 if interiors or exteriors
grow) to the largest NONR rounded up to 4 KiB. Cost: 35 x the kind-0 value
of heap, allocated at boot (0x100000 would cost 35 MiB; the exe is large
address aware). A NONR larger than the buffer overruns the heap block. Note
that the cloned Desanne's NONR is 0x6C8D0 and the e5-big clone's is 0x6CF70,
only 0x90 bytes under the stock limit; a converted car with a bigger UPVH or
MODL will cross it.

## Shared spool read buffer (confirmed-static)

A type-2 model reads its whole VEHC into one shared buffer before parsing
it (FUN_00858870, then SceneModelType2_Poll state 3 opens the CHNK in
memory). The buffer:

- static: 0x0145A7D0, 0x280000 bytes (ends exactly at the owner variable
  0x016DA7D0);
- FUN_008FC910: if the read size is above 0x280000, `_aligned_malloc(size,
  0x10)` a heap buffer (pointer 0x016DA7D4, size 0x016DA7D8) and use it
  instead;
- FUN_008FC980 returns the heap buffer when there is one, else the static
  one; FUN_008FC8B0 frees the heap buffer when the load finishes.

The read size is `align(align(size + align4(name_len), 1 << align_log2),
0x800)` (FUN_008FB720), so a VEHC payload up to 0x27F000 with a short name
stays on the static path.

The heap path looks correct. But every root entry in every shipped `.sp`
package is at most 0x280000 (largest: menusmaster 0x26C000, MegaMesh
0x21F800, vehicles 0x193000), and SSIC's 9.8 MB MDXN is streamed one DRSP
at a time (each DRSP under 0x258980) through the type-1 path, where
FUN_008FC860 returns the same 0x280000 threshold. The packager evidently
kept every read under 2.5 MiB, so the heap path is probably never exercised
by stock data. Any code that keeps a pointer into the VEHC buffer after the
load would read a reused static buffer in stock but a freed block here. I
did not find such a pointer; this was the main hypothesis for the e5-big
crash, and the pad test checked it directly: it passed ("Test results"), so
the patch below was never needed.

**Raise by code, only if the pad test crashes.** Give the static path a
bigger buffer:

| VA (RVA) | Bytes | Instruction | Patch |
| --- | --- | --- | --- |
| 0x008FC9A3 (0x004FC9A3) | `BE D0 A7 45 01` | `MOV ESI,0x0145A7D0` | imm32 at 0x008FC9A4 = new buffer |
| 0x008FC92B (0x004FC92B) | `81 FE 00 00 28 00` | `CMP ESI,0x280000` | imm32 at 0x008FC92D = new size |

Allocate the new buffer with `VirtualAlloc` (page aligned, for example
8 MiB) in DllMain before the first spool; at that point the owner word
0x016DA7D0 is 0. Leave FUN_008FC860 (`B8 00 00 28 00` at 0x008FC860) alone:
it is the type-1 streaming threshold, and raising it would make SSIC load
its MDXN whole. These are the only references to 0x0145A7D0 (Ghidra refs)
and the only uses of 0x280000 in the spool code (scalar scan; the other two
hits, FUN_007393E0 and FUN_0073AE00, are unrelated buffers).

## Entries per CHNK: 1100 (confirmed-static)

The chunk reader (constructor FUN_008FBE30, about 0x5618 bytes) holds the
entry table inline: 16-byte records at +0x08, count at +0x44C8, a pointer
vector at +0x44CC with its count at +0x55FC. FUN_008FC400 (in-memory CHNK)
and FUN_008FC2F0 copy `count` records with no bound check, so a CHNK with
more than (0x44C8 - 8) / 0x10 = 1100 entries overwrites the reader's own
fields. Most MDXN parsers put this reader on the stack (FUN_00BBAFE0 VXCK,
FUN_00BBB510 SBMC, ...).

What it limits:

- **Root of dngvehicles.sp**: 1100 entries, so at most 1097 VEHC, which is
  365 vehicles (stock 150; the room is 215 more).
- **One DRSP**: 1100 children (TEXC + MACO + SBMC + VXCK + IXCK + MDID).
  Stock maximum is 382 (VEHC 144), e5-big has 415. A converted car with
  more than about 1090 submodels has to split its resources across several
  DRSPs: MdxnResource_PollDelayedRelease (0x00BB93F0) states 8 and 9 process
  every DRSP child of the MDXN in turn, as SSIC's five DRSPs show.
  Cross-DRSP UID references are resolved through the global hash tables,
  so that should work, but it is untested.

`dsfforza.asi`'s `archive` module lifts the limit for every reader; see the
next section.

## Raising the root entry limit

`dsfforza.asi` module `archive` (`tools/asi/src/mod_archive.cpp`) lets a
CHNK hold up to `[archive] max_entries` entries (default 16384) and lets
archives grow past 2 GiB. With it, `dngvehicles.sp` holds a car for every
free base model ID: 849 added cars (IDs 1..999 minus the 150 stock ones;
`port_batch.py --id-policy asi` assigns all of them, the default policy
1..61 and 304..999, so 757). In game, a 2,724-entry archive
(757 clones, 2.67 GB) loads, and cars from both ends of the root table spool,
spawn and drive (confirmed-runtime, runs below). Static analysis:
`tools/ghidra/ArchiveExplore.java` (one-pass `call:`/`scalar:` scan),
outputs in `out/ghidra/archive1..9`.

### The reader (confirmed-static)

Constructor FUN_008FBE30, vtable 0x00E3AF64 (one slot, the deleting
destructor FUN_008FC730), 0x5618 bytes:

| Offset | Field |
| --- | --- |
| +0x04 | mode: 0 in memory, 1 file, 3 nested in a parent entry |
| +0x08 | entries: fixed vector of 1100 × 16 bytes `{tag, offset, u8 version, u8 name_len, u8 align_log2, u8 flag, size}` |
| +0x44C8 | entry count |
| +0x44CC | payload pointers: fixed vector of 1100 |
| +0x55FC | pointer count |
| +0x5600 | file handle (index into the file layer's 32 slots) |
| +0x5604, +0x5608 | parent reader, entry index in the parent (mode 3) |
| +0x560C | open children of this reader |
| +0x5610 | async open state; +0x5615 success |

The object can't grow: it is embedded (spool object +0x14,
ShiftGridResourceMap keeps a field at +0x5618 right behind its reader) and
34 functions put one on the stack (`MOV EAX,0x5618; CALL __alloca_probe`).
The async open also reads the entry table through one static buffer, a byte
vector of 0x4800 bytes at 0x01455FC4 (size at 0x0145A7C4, current user at
0x01455FC0), so a file-backed CHNK of more than 1151 entries overflows that
too.

Who touches the table: a scan for 0x44C8, 0x44CC, 0x55FC, 0x44C0, 0x1130,
0x5600..0x5615 and the static buffer finds only the reader's own functions
(0x008FB200..0x008FCA00), plus FUN_00496FF0 reading +0x5615. Everything else
calls the accessors (FUN_008FB3A0 pointer(i) from 97 sites, FUN_008FB340
tag(i) 45, FUN_008FB370 version(i) 31, FUN_008FB3C0 size(i) 14, FUN_008FB720
read size(i) 14, FUN_008FBE90 read(i) 10). None of them is inlined anywhere,
so the exe was built without whole-program optimisation, and replacing them
with standard thiscall functions is safe.

### The patch

Entries 0..1099 stay inline. A reader whose CHNK has more entries gets a
side table for entries and pointers 1100 and up, keyed by the reader's
address (64 slots, SRW lock). Every reader function that indexes the table
or walks it is replaced by a C++ copy that uses the side table for indices
of 1100 and up. For a reader of up to 1100 entries each copy does what the
stock function did (same calls, same order). Fourteen entry-point jumps,
applied only if all fourteen sites hold the stock bytes:

| VA | Stock function | Original bytes (checked) | New |
| --- | --- | --- | --- |
| 0x008FB340 | tag(i) | `8B 44 24 04 C1 E0 04 8B 44 08 08 C2 04 00` | `E9` → `h_tag` |
| 0x008FB370 | version(i) | `8B 44 24 04 40 C1 E0 04 8A 04 08 C2 04 00` | `E9` → `h_version` |
| 0x008FB3A0 | pointer(i) | `8B 44 24 04 8B 84 81 CC 44 00 00 C2 04 00` | `E9` → `h_ptr` |
| 0x008FB3C0 | size(i) | `8B 44 24 04 C1 E0 04 8B 44 08 14 C2 04 00` | `E9` → `h_size` |
| 0x008FB570 | set size, flag and alignment of i | `56 8B 74 24 14 57 8B 7C 24 0C` | `E9` → `h_set_entry` |
| 0x008FB5E0 | seek (mode 3 adds the parent entry's offset) | `8B 54 24 04 8B 41 04 83 E8 01` | `E9` → `h_seek` |
| 0x008FB620 | close (also reached from 0x008FB9D0 and the destructor 0x008FBE80); frees owned payloads | `56 8B F1 8B 46 04 83 F8 03` | `E9` → `h_close`, which also frees the side table |
| 0x008FB6F0 | aligned size(i) | `8B 44 24 04 C1 E0 04 03 C1` | `E9` → `h_aligned_size` |
| 0x008FB720 | read size(i, align) | `56 8B 74 24 0C 57 8B F9` | `E9` → `h_read_size` |
| 0x008FB770 | name(i) | `53 56 57 8B 7C 24 10 8B F1` | `E9` → `h_name` |
| 0x008FB880 | attach a payload to i | `53 55 8B 6C 24 18 56 57` | `E9` → `h_set_data` |
| 0x008FBE90 | read entry i | `53 55 56 57 8B F9 E8 85 B7 F5 FF` | `E9` → `h_read_entry` |
| 0x008FC0A0 | async open state machine | `83 EC 1C 53 56 8B F1 8B 86 10 56 00 00` | `E9` → `h_poll`; a table of more than 1100 entries is read into a heap buffer instead of the static one |
| 0x008FC400 | open a CHNK in memory | `83 EC 18 53 8B 5C 24 20 81 3B 43 48 4E 4B` | `E9` → `h_load_mem` |

Each jump replaces the first five bytes; the rest of the stock function is
never reached. Not replaced: FUN_008FB500, FUN_008FB940, FUN_008FB9E0,
FUN_008FBB30, FUN_008FBBD0 (write a CHNK to disk), FUN_008FBF30 and
FUN_008FC2F0 (append). No call, jump or data reference reaches them.

A count above `max_entries` is treated as a bad header (the open fails)
instead of corrupting memory. Cost: nothing for readers of up to 1100
entries. For `dngvehicles.sp` the side table holds the entries past 1100
(1,624 in the test, 26 KB), and each access to one of them takes a shared
lock.

### File positions past 2 GiB (confirmed-runtime)

The first runs with the 2.67 GB archive (`20261022-215228`, `-215759`)
hung on the loading screen with the CPU busy. The plugin's log showed SSIC's
nested reader (FUN_005A0DE0 → FUN_008FC5C0) opening a CHNK of 2,724
entries, the root's own count: reads at or past 2 GiB came back from the
start of the file. FUN_0085A970, the file layer's seek, stores the position
and then does:

```text
0085A9AE  83 79 30 00     CMP dword ptr [ECX+0x30],0
0085A9B2  7D 07           JGE 0x0085A9BB
0085A9B4  C7 41 30 00...  MOV dword ptr [ECX+0x30],0
```

Everything after it is unsigned. FUN_0085AB80 keeps
`GetFileSizeEx().LowPart` rounded up to 2 KiB at +0x28. FUN_0085AD10 issues
`ReadFile` with `OVERLAPPED.Offset = pos & ~0x7FF` and `OffsetHigh = 0`
(no buffering, overlapped). FUN_0085AE50 advances +0x30 with unsigned
compares. The patch turns the `JGE` into `JMP` (0x0085A9B2: `7D` → `EB`,
checked against `83 79 30 00 7D 07`). A position past the end is still
clamped to the file size by the next check; that only differs from stock for
a relative seek to before the start of the file, which the readers never do.
The new limit comes from the u32 offsets in CHNK headers and the rounded size
at +0x28: just under 4 GiB (4 GiB - 2 KiB). `[archive] large_files=0`
leaves the seek alone.

Ported cars take about 3.45 MB each (`out/batch/first`: 31 cars,
107 MB; `out/testdrive`: 40 cars, 137 MB). Without this patch an archive
tops out at about 480 added cars. With it, all 849 free IDs fit (about
3.4 GB).

### Everything else that scales with the car count (static check)

| Structure | Limit | Matters? | Evidence |
| --- | --- | --- | --- |
| Base model IDs | below 1000. FUN_005A7FC0 (vehicle LOD spool request) sets interior = `(short)id + 1000` and exterior = `(short)id + 2000`, so a base ID of 1000 or more collides with another car's interior or exterior | yes: 999 base IDs, 849 free | confirmed-static |
| SSLP record count, VEHC index in a record | u16 (SSLP +0x02; `{u16 id, u16 index, u16 kind}`) | no (65,535) | confirmed-static |
| VSCT record count | u16 at VSCT +0x06 | no | confirmed-static |
| SSIC size | measured at load (FUN_005A0DE0), so bigger SSLP and VSCT tables are fine (2,724 records: +76 KB) | no | confirmed-runtime |
| Spool object ID lists (+0x5B44, +0x5F50, +0x6368, +0x668C, +0x6998, +0x69A4, +0x6CB0, +0x6CBC, +0x6FC8, +0x6FD4) | inline node pools of 48 or 64 nodes, sorted by u16 ID (FUN_005A1660, FUN_005A17A0, FUN_005A1D00, FUN_005A2230) | no: they hold requested and resident IDs, bounded by the 35 + 2 + 2 slots, not by the archive | confirmed-static |
| Global UID list (16384), resource hash arena (45,000 nodes), MODL part index (85,000) | grow with resident resources | no | "Global lists" below |
| Garage list, maker groups, save flags | IDs 0..303, 40 groups, 304 slots (dsfforza.asi `[garage]`: 0..999, 120 groups, flags of 304+ in a side file) | for the garage and save only | `batch.md`, `asi.md` (module `garage`) |
| Lua `vehicleStats` | a Lua table | no | |

A scan for 2000 (0x7D0) and 3000 (0xBB8) found no other code that derives a
model ID from another: FUN_00694720 and FUN_00695D90 compare camera values
with 2000, FUN_00506C90 is timing.

### In game (confirmed-runtime)

Test archive `out/lim-root-full.sp`: the stock archive plus 757 clones of
248 (Cobra), 293 (Buggy), 250 (Volt) and 266 (Desanne) in turn, at IDs
1..61 and 304..999 (2,724 root entries, 2.67 GB):

```sh
uv run dsff clone_vehicle out/stock/Install/dngvehicles.sp out/lim-root-full.sp \
    --source 248,293,250,266 --id 1-61,304-999 --root-limit 3003
```

ID 459 sits at root entries 1098..1100 (its exterior is the first
side-table entry), 460 at 1101..1103, 700 at 1821..1823, 999 at 2718..2720
and SSIC at 2721.

| Run | Archive, plugin | Result |
| --- | --- | --- |
| `20261022-215228`, `-215759` | test archive, `[archive]` without the seek patch | hangs on the loading screen (SSIC read from offset 0, above) |
| `20261022-220605` | test archive, `[archive]` only | 266, 1, 459, 460, 700 and 999 spool, spawn, take the player and switch to the cockpit |
| `20261022-220929` | test archive, whole plugin (`archive`, `garage`, `hud`) | 459, 460, 999 in cockpit view with the throttle held: 460 to 63 mph, 999 to 114 mph |
| `20261022-221412` | same | 62, 459, 700, 1: 459 to 73 mph, 700 to 46 mph, 1 to 49 mph |
| `20261022-221150` | stock archive, whole plugin | 266, 130, 303 as before (130 to 46 mph, 303 to 100 mph) |
| `20261022-221629` | test archive, no plugin (negative control) | never reaches the menu: `Driver.log` stops after the D3D9 hook, no Lua runs, and the process ends with code -1 after 10 minutes |

The first car of each run shows no speed, in the stock run too: the
harness's throttle hold starts before that car's cockpit view. The clones'
HUD names are wrong because they have no `vehicleStats` entries
(`in-game-tests.md`), which is unrelated.

### Tools

`--root-limit N` raises the 1100 check in `src/dsfforza/build/port_batch.py` (`plan`,
`build`, `remerge`), `src/dsfforza/build/port_car.py` and `src/dsfforza/build/clone_vehicle.py`. The
default stays 1100, so archives for an install without the plugin stay safe;
3003 covers every base ID below 1000. `clone_vehicle.py --id` takes a list
or ranges and `--source` a list to cycle through (the test archive above).
`upvh.py lint` has no root limit.

### Open

- Only the vehicle root was tested past 1100 entries. The same code serves
  every reader, so a DRSP with more than 1100 children should load too, but
  the graphics pool slot (about 1800 render objects, below) would run out
  soon after. Untested.
- An archive past 1100 entries or 2 GiB on a game without the plugin hangs or
  corrupts memory. `tools/install.ps1` refuses such an archive unless `-Asi` installs the plugin with it.
- `port_batch.py --id-policy asi` uses the 92 free IDs inside 62..303 (after 1..61, before 304); the other policies don't.

## Graphics pool slots (confirmed-static)

GraphicsManager_Construct (0x00BAD960) builds 21 pools from two `.data`
tables: names and slot counts at 0x00FD25D0 (8 bytes each), and
`{flag, SYS KiB, SYS nodes, GPU KiB, GPU nodes}` at 0x00FD2678 (20 bytes
each). It asserts `GPU KiB % slots == 0` (int3 otherwise) and calls
GraphicsPoolHeap_Initialize (0x00BB50D0, `ECX = 0x019B3BE0`), which splits
each pool evenly over its slots (FUN_00BB4BE0, sizes rounded down to 16):

| Pool | Name | Slots | SYS per slot | SYS nodes per slot | GPU per slot |
| ---: | --- | ---: | ---: | ---: | ---: |
| 8 | Vehicle Global | 1 | 3 MiB | 4096 | 1 KiB |
| 9 | Vehicle Spooled | 35 | 239,664 B (0x3A830) | 1872 | 1 KiB, 29 nodes |
| 10 | Vehicle Spooled Int | 2 | 4 MiB | 32768 | 1 KiB, 512 nodes |
| 11 | Vehicle Spooled Ext | 2 | 4 MiB | 32768 | 1 KiB, 512 nodes |

FUN_005A1520 picks the pool from the SSLP kind (0 → 9, 1 → 10, 2 → 11;
vehicle SSIC → 8), and the slot index is the handler's per-kind index
(FUN_00858860 stores it at model +0x564). FUN_00BB5690 selects the pool and
clamps the slot to `count - 1`.

What goes in a slot: ResourceObjectPool_Allocate (0x00BB5470) hands out the
small render objects the MDXN parser creates in SYS memory (memtype 0, align
0x10): 0x48 per vertex buffer, 0x40 per index buffer, 0x50 per submodel,
0x28 + 3 x 0x40 per texture, `0x30 + 0x1C n` per material plus 0x30 to 0xE0
blocks per render method, and the MDID list (8 bytes per UID). Nothing calls
it with memtype 1, so the GPU part is unused on PC. Each allocation also
takes about one range node (a trailing split). Vertex, index and texture
data don't go here (see the next section).

From those sizes the Desanne uses about 31 KB and 300 nodes of its slot,
and e5-big about 50 KB and 450 nodes (estimates). The node count runs out
first, at roughly 1800 render objects per kind-0 car. ResourceIndexAllocator
returns 0 when it can't fit an allocation, and the MDXN parsers dereference
the result without a check, so exhaustion crashes.

**Raise by code** (only if converted cars approach 1800 objects;
`tools/game/dump-spool-limits.ps1` shows the real usage). The table is in
`.data`, which is already writable, and the game reads it once at startup.
Write it from DllMain, not from a thread, so it lands before
GraphicsManager_Construct runs:

| VA (RVA) | Stock | Meaning | Proposed |
| --- | --- | --- | --- |
| 0x00FD2730 (0x00BD2730) | `00 20 00 00` | Vehicle Spooled SYS KiB, all 35 slots | 0x4000 (468 KiB per slot, +8 MiB total) |
| 0x00FD2734 (0x00BD2734) | `00 00 01 00` | Vehicle Spooled SYS nodes | 0x20000 (3744 per slot; the node array is malloc'd, 16 B each) |

Keep GPU KiB (0x00FD2738, 0x23) a multiple of the slot count. The parent
arena (GraphicsParentArena_Initialize) is sized from the sum of all SYS
fields, so it grows by itself.

## Vertex and index buffers (confirmed-static)

FUN_00BBAFE0 (VXCK) and FUN_00BBAD40 (IXCK) create D3D9 buffers through
FUN_00BB17A0 and FUN_00BB18F0: `CreateVertexBuffer(count * stride)` and
`CreateIndexBuffer(count * 2)`, managed pool, data memcpy'd in. The sizes
come straight from VXHD and IXHD, so there is no fixed vertex or index cap.
IXCK always passes format 0 (`D3DFMT_INDEX16`) whatever IXHD says, so a
submodel can reach at most 65536 vertices from its `base_vertex`. A
"dynamic" variant with a CPU copy exists (usage 0x208) but the MDXN path
never sets it, and I found no per-vehicle CPU copy of vertices (no damage
deformation buffer on this path). If a vertex declaration needs conversion,
FUN_00BAEF80 takes scratch from an arena and falls back to the raw data when
it is full, so that is not a limit either.

Stock maxima for reference (`src/dsfforza/game/vehicle_limits.py`): kind 0 has at most
3 VBs, 15,506 vertices in one buffer, 20,905 per car, 216,228 indices, 374
submodels, 13 submodels per part LOD; kind 1 reaches 21,446 vertices in one
buffer.

## Global lists

**UID list, 16384 entries (confirmed-static that it is unbounded;
hypothesis that it matters).** FUN_006A58F0 (MODL LOD fixup), FUN_00BAA300
(SBMC), FUN_00BAA290, FUN_00BA9FD0 and FUN_006CC640 append `{u64 uid}` to
0x0186A6B8 and a pointer to 0x0185A6B8, then `INC [0x0188A6B8]`. Both arrays
hold 16384 entries; the UID array ends at the counter, so entry 16384
overwrites the counter and the next write goes anywhere. No code reads the
arrays (no refs, no scalar hits outside the writers). Only FUN_004A20C0
(loading screen) resets the counter. A full car spool appends about two
entries per submodel: about 530 for the Desanne, 760 for VEHC 144, 830 for
e5-big. Whether normal play ever gets near 16384 is unknown; the dump
script prints the counter. If it climbs, NOP the six increments (each is
`FF 05 B8 A6 88 01`), which pins every write to entry 0:

| VA | RVA |
| --- | --- |
| 0x006A5963 | 0x002A5963 |
| 0x006A5A3D | 0x002A5A3D |
| 0x00BAA05C | 0x007AA05C |
| 0x00BAA2EE | 0x007AA2EE |
| 0x00BAA365 | 0x007AA365 |
| 0x006CC68A | 0x002CC68A |

**Resource hash arena, 45,000 nodes.** FUN_00BAEA10 registers each vertex
buffer (`_TRV`), index buffer (`_XDI`), texture (`_TEX`), shader (`_UPG`)
and material through ResourceHashPool_Acquire (0x00BAED90), a bump arena
with no end check (known from the driver-sf-mods terrain work). It is
shared by everything resident, so only cars with many more buffers and
materials than stock matter. Not patched here.

**MODL part index**, 85,000 UIDs (FUN_00494390), global. Not a concern for
a few dozen parts per car.

## Slot counts (for completeness)

SSLP +0x18.. sets slots per kind (35/2/2). The spool object holds at most 64
handlers in total and 64 per kind. The graphics pools have their own fixed
counts (0x00FD261C = 35, 0x00FD2624 = 2, 0x00FD262C = 2), and FUN_00BB5690
clamps the slot index, so raising SSLP slots alone would put extra cars into
the last graphics slot. More detail per car doesn't need more slots.

## Test results (confirmed-runtime, runs `20261022-031004`, `-031132`, `-031259`)

All three load, spawn, drive and switch to the cockpit with no crash:

| Archive | Result |
| --- | --- |
| `lim-pad-under.sp` (VEHC 0x27F000) | works |
| `lim-pad-over.sp` (VEHC 0x30A000, heap read path) | works |
| `lim-nonr-raise.sp` (NONR 0x80000, SSLP +0x08 raised) | works |

So the read path above 0x280000 is fine and a bigger NONR only needs the
SSLP header. The old 0x29B000 MDXN crash was most likely the UID scheme of
that build (counter in UID byte 7, `upvh.md`), not its size. `port_car.py
--mdxn-limit 0x2C0000` lifts the stock-maximum MDXN cap.

## Test plan (run; results in "Test results" above)

All archives are in `out/`, built from `out/dngvehicles.clone.sp` (the
confirmed working Desanne clone, ID 304 at VEHC 450..452) with
`src/dsfforza/game/spool_variants.py`. Each changes one thing. Spool and spawn 304 as
in the clone test.

| Archive | Change | Expected if the hypothesis holds |
| --- | --- | --- |
| `lim-pad-under.sp` | VEHC 450 padded to 0x27F000 (read size exactly 0x280000) | works, static buffer |
| `lim-pad-over.sp` | VEHC 450 padded to 0x30A000, e5-big's size | crashes if the heap read path is the problem |
| `lim-nonr-raise.sp` | VEHC 450 NONR padded to 0x80000, SSLP +0x08 = 0x80000 | works |
| `lim-nonr-overflow.sp` | same NONR, SSLP unchanged (optional negative control) | heap overflow; may crash late or not at all |

The padding is an unknown `PADD` child at the end of the VEHC (or NONR).
FUN_00858A70 skips unknown VEHC children, and LoadTaggedResources only loads
tags it has providers for, so the geometry the game sees is identical.

Reading the results:

- under works, over crashes: the 2.5 MiB read path is the limit. Apply the
  static-buffer patch (two imm32 writes above) and retest e5-big.
- both work: the read path is fine; e5-big's crash is in its content (vertex
  buffers of 29,919 and 18,052 vertices, 404 submodels, part LODs
  with 41 submodels; all within the limits found here). Next step would be
  to bisect those on top of the clone.
- nonr-raise works: SSLP +0x08 is all a bigger NONR needs.

Running `tools/game/dump-spool-limits.ps1 -Every 5` beside any of these
prints pool 8 to 11 slot usage (bytes and nodes, worst slot), the UID list
counter and the spool buffer state, so it also measures the real stock
headroom.

## Tools

- `src/dsfforza/game/vehicle_limits.py <archive> [--vehc N ...]`: stock maxima per kind,
  or one entry checked against the NONR buffer and the 0x280000 threshold.
- `src/dsfforza/game/spool_variants.py`: the archive variants above (`--pad-vehc`,
  `--pad-nonr`, `--sslp-nonr`, `--sslp-slots`).
- `tools/game/dump-spool-limits.ps1`: read-only runtime view of the budgets.
- `tools/ghidra/SpoolExplore.java`: the Ghidra explorer used here.
