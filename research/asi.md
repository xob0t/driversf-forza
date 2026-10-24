# dsfforza.asi: runtime patches of Driver.exe 1.04

Updated: 2026-10-24

One native plugin, `plugins\dsfforza.asi`, patches the running
`Driver.exe` 1.04 in memory to lift limits that block large car batches.
Nothing on disk changes, and removing the file restores stock behaviour.
Build and module interface: `tools/asi/README.md`. Install: `tools/install.ps1`
copies `out\asi\dsfforza.asi` and `dsfforza.ini` into `<game>\plugins\`
when `-Asi` is given. Evidence tags as in `dngvehicles.md`.

| Module | Lifts | Section |
| --- | --- | --- |
| `archive` | 1100 entries per CHNK → 16384 (`dngvehicles.sp` past 365 vehicles), archive files past 2 GiB | [Archive size](#archive-size) |
| `garage` | garage list IDs 0..303 → 0..999, 40 → 120 maker groups, save flags for IDs 304..999 | [Garage and save](#garage-and-save) |
| `hud` | HUD/Shift/garage-shop maker badge and model plate: one-byte glyph codes → 16-bit (`ManufacturerGlyph`/`ModelGlyph`); garage card emblem: Forza logo instead of the SF disc (`GarageEmblem`) | [HUD badges and plates](#hud-badges-and-plates) |

Every module checks the original bytes of each site before patching and
patches nothing of a feature when one site differs (another mod, another exe
build); should a write still fail, the part already written is undone.
`plugins\dsfforza.log` lists what was applied. `[<module>]
enabled=0` in `dsfforza.ini` switches a module off.

Crash hunts: `[dsfforza] crash_log=1` (default 0) adds a vectored exception
handler that logs each of the first 12 faults (first chance: also ones the
game handles itself) with registers and the Driver.exe return addresses on
the stack. DriverNGHook's fatal-error dialog often leaves no minidump.

Offline check: `tools\asi\build.ps1 -Test` maps `Driver.exe`'s image at
0x00400000 in a test process (`tools/asi/test/offline.cpp`), runs every
module's init against it and exercises the hooks that need no game state.
A second test process changes one byte in three all-or-nothing sets (archive
readers, maker groups, card emblem) and checks that nothing of each lands.

## Archive size

Module `archive` (`tools/asi/src/mod_archive.cpp`). The evidence, the
reader layout and the in-game runs are in `spool-limits.md`, "Raising the
root entry limit"; this is the patch list.

**What it lifts.** The game's CHNK reader holds 1100 entries inline, so
`dngvehicles.sp` stops at 365 vehicles (215 added), and the file layer's
seek clamps positions of 2 GiB and more to 0, so an archive stops at about
2 GiB (about 480 ported cars). With the module, the archive can hold a car
for every base model ID below 1000 (849 added: `port_batch.py --id-policy
asi` uses all of them, the default `garage` policy 757) and grow to
4 GiB - 2 KiB.

**Reader patches** (all fourteen or none; `[archive] max_entries`, default
16384, is the largest entry count accepted). Each site gets `E9 rel32` to a
C++ copy of the function that keeps entries 1100 and up in a side table:

| VA | Function | Original bytes (checked) |
| --- | --- | --- |
| 0x008FB340 | tag(i) | `8B 44 24 04 C1 E0 04 8B 44 08 08 C2 04 00` |
| 0x008FB370 | version(i) | `8B 44 24 04 40 C1 E0 04 8A 04 08 C2 04 00` |
| 0x008FB3A0 | pointer(i) | `8B 44 24 04 8B 84 81 CC 44 00 00 C2 04 00` |
| 0x008FB3C0 | size(i) | `8B 44 24 04 C1 E0 04 8B 44 08 14 C2 04 00` |
| 0x008FB570 | set entry | `56 8B 74 24 14 57 8B 7C 24 0C` |
| 0x008FB5E0 | seek | `8B 54 24 04 8B 41 04 83 E8 01` |
| 0x008FB620 | close (and destructor) | `56 8B F1 8B 46 04 83 F8 03` |
| 0x008FB6F0 | aligned size(i) | `8B 44 24 04 C1 E0 04 03 C1` |
| 0x008FB720 | read size(i) | `56 8B 74 24 0C 57 8B F9` |
| 0x008FB770 | name(i) | `53 56 57 8B 7C 24 10 8B F1` |
| 0x008FB880 | attach payload | `53 55 8B 6C 24 18 56 57` |
| 0x008FBE90 | read entry | `53 55 56 57 8B F9 E8 85 B7 F5 FF` |
| 0x008FC0A0 | async open | `83 EC 1C 53 56 8B F1 8B 86 10 56 00 00` |
| 0x008FC400 | open in memory | `83 EC 18 53 8B 5C 24 20 81 3B 43 48 4E 4B` |

**File seek** (`[archive] large_files=1`): 0x0085A9B2 `7D 07` (`JGE`) →
`EB 07` (`JMP`), checked against `83 79 30 00 7D 07` at 0x0085A9AE.

**In game** (confirmed-runtime): a 2,724-entry, 2.67 GB test archive loads,
and IDs 1, 459 (straddles entry 1100), 460, 700 and 999 spool, spawn and
drive (runs `20261022-220605`, `-220929`, `-221412`); the stock archive
behaves as before with the plugin (`20261022-221150`). Without the plugin
the same archive never reaches the menu (`20261022-221629`).

**Tools.** Archives past 1100 root entries need `--root-limit N` in
`port_batch.py`, `port_car.py` or `clone_vehicle.py` (default 1100). Such an
archive must not be installed without the plugin.

## Garage and save

Module `garage` (`tools/asi/src/mod_garage.cpp`). Background in
`batch.md` (Limits). Static analysis: `out/ghidra/garage-1..3`
(`tools/ghidra/GarageExplore.java`) and `src/dsfforza/game/xref.py`, a capstone
linear sweep of `.text` that lists every instruction with a given
displacement or immediate.

### What the stock game does (confirmed-static)

**Profile.** The profile manager is `[engine+0x36C]` (engine: `[0x00FE7228]`).
Its live save block is at +0xA4 (0x4CF8 bytes), the disk buffer at +0x4D9C.
Per-vehicle data in the live block:

| Data | Manager offset | Size | Accessors |
| --- | --- | --- | --- |
| `{id, flags}` list (bit 0 unlocked, 1 owned, 2 new) | +0x184 (object +0x14C, list +0x38) | 304 slots | FUN_00507CF0 find-or-insert, FUN_00507D30 test, FUN_00508990 set/clear |
| unlocked-ever bits | +0x2088 (stats object +0x1BA8, +0x4E0) | 320 bits | FUN_0050A1A0 (`SetVehicleUnlocked`), read inline by FUN_004F9490 (IDs < 304), FUN_006FD470 |
| driven bits | +0x20B0 (+0x508) | 320 bits | FUN_0050A120 (`SetVehicleDriven`), `GetVehicleDriven` 0x67C320 reads inline |
| unidentified bits | +0x20D8 (+0x530) | 320 bits | only cleared |
| take-a-ride bits | +0x2100 (+0x558) | 320 bits | FUN_0050A170, counted by 0x67C3C0 (IDs < 304), read inline by FUN_006FD470 and the garage car list FUN_007113A0 |
| owned list | +0x2784 (+0xA9C sub-object, +0x140) | 304 IDs | FUN_0050A500 → FUN_0050A4C0 find-or-insert |
| owned counter (byte) | +0x4D6F | 1 byte | FUN_00508990 increments it on every new owned flag |

The two find-or-insert functions write through NULL when the list is full.
The bit setters don't range-check: ID 320+ sets bits of the next array.

**Save file.** `HDD:<profile>\savegames\autosave.dat` is the raw 0x4CF8-byte
block (FUN_00853620 writes it with `fopen("wb")`, FUN_008534A0 reads it and
accepts 0x4CF8 or 0x4CF4 bytes). The block is copied with `memcpy` at:

| Site | Function | Copy |
| --- | --- | --- |
| 0x503D36, 0x5040D0 | FUN_00503CD0, FUN_00504090 | live → buffer (a save starts) |
| 0x505A77 | FUN_00505A10 | buffer → live, after the guard-word and version check (load) |
| 0x503E2C, 0x504CA0 | FUN_00503D80, FUN_00504C60 (`initialiseProfileData`) | defaults (FUN_00507500) → live |
| 0x504053 | FUN_00503FF0 | defaults → buffer (a new save) |
| 0x505DBC, 0x505E81 | FUN_00505D80 | live → stack copy, and back if the new profile fails |
| 0x679F5A, 0x679FAA | `ClearProgression`, `ClearStatistics` | default progression (+0x14C, 0x1A58) and stats (+0x1BA8, 0x2C60) → live |

**Garage list.** The garage object (operator new 0x3728 at 0x6C7D90, the
`Engine+0x718` singleton, vtable 0xE189F0) builds its maker list once, in
FUN_006C57D0 (vtable slot 0x120):

- loop 1 calls vfunc 0x134 for IDs 0..0x12F and stores a sort key per ID in
  `int[0x130]` at +0x3264;
- loop 2 calls `getVehicleStats(id)` for IDs 0..0x12F and, for each
  `Selectable` entry, files the ID under the first 15 characters of
  `ManufacturerName`, upper-cased: an existing group gets the car sorted by
  key (FUN_006C5740), a new name gets a new group, inserted sorted
  (vfunc 0x268, FUN_006C7BB0).
- Groups: 40 × 0x6C bytes at +0x20FC (20 IDs, count at +0x50, name at +0x54,
  badge string at +0x64, `Manufacturer` at +0x68), group count at +0x31DC.
  Neither insert checks capacity: a 21st car overwrites the group's count, a
  41st group overwrites the count and what follows.
- The maker menu (FUN_00711040, menu object built at 0x710C30) copies the
  index of every group with a listed car into `int[40]` at menu+0x274; the car
  list (FUN_007113A0) reads it back as `[menu+0x270 + selection*4]`.

Every other user reads groups through the garage's vfuncs (count 0x148, group
0x130, car 0x12C), so only the sites below touch the layout.

### Patches

All sites are verified byte for byte before anything is written; each group
of patches is applied completely or not at all.

**Save hooks** (`[garage] save=1`):

| Site | Original | New | Why |
| --- | --- | --- | --- |
| 0x507CF0 | `56 57 8B 7C 24` (find-or-insert) | `jmp h_flag_slot` | stock list first (so entries already in a save keep working); IDs 304..999 get a slot in the plugin's table; a full list returns a scratch slot instead of NULL |
| 0x507D30 | `8D 51 38 8B 4C` (test) | `jmp h_flag_test` | same lookup; answers the Lua detection queries (below) |
| 0x50A500 | `81 C1 9C 0A 00 00` (owned list) | `jmp h_owned_slot` | same scheme for the owned list |
| 0x50A1A0, 0x50A120, 0x50A170 | prologues | `jmp` to stubs | for IDs 304..999 the stub swaps `this` for a base that lands in the plugin's bit table and runs the original code (notifications included); IDs of 1000 and up are dropped |
| 0x67C320 | `GetVehicleDriven` | C replacement | stock bits for IDs < 304, the plugin table above (304..319 also the stock bit, which older saves may hold) |
| 0x67C3C0 | `GetNumVehiclesDrivenTakeARide` | C replacement | stock count (IDs < 304) plus IDs 304..999 |
| 0x6FD51E (0x21 bytes) | inline unlocked/take-a-ride test | `jmp` + C | correct bits for listed IDs ≥ 320 |
| 0x711759 (0x20 bytes) | inline take-a-ride test (garage car list) | `jmp` + C | same |
| 0x5089E4 | `inc byte [eax+0x4D6F]` | `call` saturating stub | the owned-cars byte stops at 255 instead of wrapping |
| the ten `memcpy` sites above | `call _memcpy` | `call` wrappers | follow the block: snapshot at save, apply the side file at load, clear on defaults, back up/restore around FUN_00505D80, clear on `Clear*` |
| 0x8536E7 / 0x85354A | `call _fopen` (autosave write / read) | `call` wrappers | write / read the side file next to `autosave.dat` |

**Garage list** (`[garage] list=1`, needs the save hooks):

| Site | Original | New |
| --- | --- | --- |
| 0x6C57FB | `lea ebp,[esi+0x3264]` | `mov ebp, sortkey` (plugin `int[1000]`) |
| 0x6C587C, 0x6C5A55 | `cmp edi,0x130` | `cmp edi,max_id` (1000) |
| 0x6C5762 | `cmp edx,0x12F` | `cmp edx,max_id-1` |
| 0x6C576A, 0x6C5771 | `[edi+reg*4+0x3264]` | `[reg*4+sortkey]` (same length) |
| 0x6C5740 | group insert prologue | `jmp`: a group with 20 cars refuses the car |
| 0x6C7BB0 | groups insert prologue | `jmp`: a full table refuses the group |

**Maker groups** (`[garage] max_groups`, default 120): the table moves behind
the object, which is allocated larger.

| Sites | Original | New |
| --- | --- | --- |
| 0x6C7D90 | `push 0x3728` | `push 0x3728 + groups*0x6C + 4` (rounded) |
| 0x6C54C2, 0x6C5534, 0x6C55AB, 0x6C55CD, 0x6C5989, 0x6C6995, 0x6C6BC0 | table +0x20FC | +0x3728 |
| 0x6C57F3, 0x6C5978, 0x6C6983, 0x6C6BAB, 0x6C7A8D, 0x6C7D40 | count +0x31DC | +0x3728 + groups*0x6C |
| 0x6C54C9, 0x6C553B, 0x6C7BE0, 0x6C7C00 | count at table+0x10E0 | table + groups*0x6C |
| 0x7110A8 | `add eax,0x274` (menu index array) | `mov eax, makers+4` (plugin array) |
| 0x7112DB, 0x711411 | `mov eax,[reg+edx*4+0x270]` | `mov eax,[edx*4+makers]` |
| 0xE18B14 (garage vtable slot 0x124) | `0x006C6940` | `fill_makers`: builds the table if needed (slot 0x120), grows the four maker menu lists (below) to the group count, calls FUN_006C6940, then clamps each list's length to what it holds |
| 0x6C6AC8, 0x6C6AE2 | `call 0x00ACE110` (string set on a list element) | `call s_menu_set_string`: a missing (NULL) element is skipped |

**The maker menu lists (confirmed-runtime).** The vehicles screen
(FUN_006DD020) looks up four menu-script lists by name and passes them to
the garage's slot 0x124, FUN_006C6940: `ManufacturerDataList` (maker name),
`ManufacturerLogoReferenceList` (badge string), `ManufacturerNumList` (car
count) and `ManufacturerNew`. They are declared in `Install\menusmaster.sp`
with 40 entries each ("dummy value" defaults). FUN_006C6940 writes entry *i*
for the *i*-th group that has a listed car, through FUN_00ABD9F0 (element
pointer, NULL past the list's length) and FUN_00ACE110 (no NULL check), then
sets each list's length to the number of groups shown (FUN_00ABD2E0). The
menu script scrolls over the list length; nothing else in the menu holds 40.
MmDk list layout (FUN_00ABD2E0, FUN_00ABD5B0, FUN_00ABDA20): +0x08 element
type (0x0C int, 0x0D float, 0x0E byte, others pointers to objects;
FUN_00ABD990 tells which), +0x0C int16 length, +0x10 data. A string element
is `{char* text, u8 external, u8 set, i16 capacity}`; with external = 0
FUN_00ACE180 copies the text into the buffer. `fill_makers` gives a list
that is shorter than the group table a new data array of `max_groups`
entries (copies of the old pointers or values, new string elements with
element 0's flags and their own 64-byte buffers) and remembers it, so later
fills only restore the length; a list it can't grow (no element 0) keeps
its length and the groups past it are not listed (logged).

### Save compatibility: the side file

Flags of IDs 304..999 never go into the stock block. The plugin keeps them
in memory and in `dsfforza-garage.dat` in the same folder as `autosave.dat`:
`DSFG`, version 1, entry count, FNV-1a of the entries, then per ID `u16 id,
u16 what` (bits 0..3: the four bit arrays, bit 4: flag-list slot, bit 5:
owned list) and `u32 flags`. It is written (to `.tmp`, then moved over) when
the game opens `autosave.dat` for writing, from the snapshot taken when the
game copied the live block into its save buffer, and read when the game
reads `autosave.dat`; the load copy applies it.

Why a side file:

- **The block can't grow.** The reader insists on 0x4CF8 (or 0x4CF4)
  bytes; the guard words sit at fixed offsets.
- **An unmodded game keeps working with the save.** The block holds only
  stock-range data, so it loads and plays as before, without the added cars.
  The side file is ignored (and survives an unmodded session, so the flags
  are back when the plugin is).
- **Old saves load unchanged.** A save without a side file simply has no
  added-car flags. Entries for IDs 304+ that older tests wrote into the stock
  list (and bits 304..319) are still found: the stock list is searched first.
- **No ID is lost to the 304-slot list.** Stock cars use about 146 slots,
  the 61 low garage IDs up to 61 more; added cars use none.

`dsff savecheck <savegames folder> --ids 305,500` lists both files.

### Telling Lua the plugin is there

DriverNGHook's Lua can't call into the plugin, so the patched FUN_00507D30
answers `ProfileSettings.GetVehicleUnlocked(8000000 + k)`. The game's Lua
numbers are single-precision floats (a literal of 2^23 or more is a compile
error: "number out of range (> +-8388606)", run 20261022-225111), so the magic
range sits just below that:

| k | true when |
| --- | --- |
| 0 | the save hooks are active |
| 1 | the garage list walks IDs past 303 |
| 0x1000 + n | n ≤ the maker group limit |
| 0x2000 + id | the garage lists that ID |

An unpatched game looks the number up in the flag list and returns false.
`register.lua` binary-searches the limits at start (`asiQuery`,
`asiLimit`) and uses them for `garage`, `garageGroup` and `guardProfile`.

### In game

Harness: `tools/asi/test/garage-tests.ps1` (four runs through
`tools/wsb/run-series.ps1 -Asi`, test Lua in `tools/asi/test/lua`, put over the
test bootstrap with `-LuaFiles`); `src/dsfforza/game/savecheck.py` reads the `save\`
copy each run keeps. Offline: `tools/asi/build.ps1 -Test` (every site
matched, hooks behave).

**Run `20261022-230004` (stock archive, plugin; confirmed-runtime):**

- `dsfforza.log`: save hooks, list (IDs 0..999) and 120 groups applied;
  "no dsfforza-garage.dat (no added-car flags yet)", then "profile loaded, 0
  added cars with flags" when the game loaded `autosave.dat`.
- Lua detection: `savetest` and `register.lua` both read "save true, garage
  true, groups 120, max listed id 999"; `guardProfile` let IDs up to 999
  through.
- In a car, `SetVehicleUnlocked/Owned/Driven/DrivenInTakeARide` for 305, 320,
  500, 650 and 999: every flag read back true (unlocked, owned, new, driven),
  `GetNumVehiclesDrivenTakeARide` went 0 → 5. `TriggerAutoSave` → "saved 5
  added-car entries to ...\savegames\dsfforza-garage.dat".
- `savecheck.py`: `autosave.dat` 19704 bytes, guard words and version OK; the
  flag list and owned list hold no ID ≥ 304 (140 of 304 slots, as before);
  the side file holds all five IDs with flags, bits and owned-list entries.
  That build still let IDs 304..319 set their bits in the stock arrays (they
  fit); the bit stubs now take 304..999 (no ID ≥ 304 in the block at all) and
  readers OR in a stock bit an older save may hold.

Earlier attempts: run `20261022-223030` never reached Story (the guest's
Start menu was open and took the Enter presses; `run-test.ps1` now releases
the Windows keys and closes it before launching). Run `20261022-225111`
found that the game's Lua numbers are single floats ("number out of range
(> +-8388606)" for the first magic value, 0x44534647), hence 8000000.

**The garage crash with 40 added cars (fixed; confirmed-runtime).** With
out/testdrive3 (IDs 1 to 40, many new makers) and `[garage]` on, opening the
garage ended in DriverNGHook's fatal-error dialog without a minidump (runs
`host-20261023-080433`, `-080858`, `-081321`). `[dsfforza] crash_log=1`
(a vectored handler in `main.cpp` that logs faults, registers and the
Driver.exe return addresses on the stack) caught it in run `-101840`:
`C0000005 at 00ACE11F (read 00000000)`, called from 0x006C6ACD with edi =
40: FUN_006C6940 writing the 41st maker into the 40-entry menu lists above.
The 49 groups themselves (relocated table, IDs past 304, save hooks) and
the [hud] card emblem hook were fine: the list build had finished, and the
emblem patch sits in the car list, which runs later. A guard-only build
(lengths clamped, no growth; run `-102604`) opened the garage and listed
the first 40 makers (up to HONDA; the other 9 groups were unreachable).
The final build grows the lists.

**In game with the fix (confirmed-runtime), out/testdrive3 + emblem data,
shipped ini (`max_groups=120`) plus `crash_log=1`:**

- Run `host-20261023-103212`: "menu list ManufacturerDataList ... grown from
  40 to 120 entries" (all four lists); the maker menu scrolls through all 49
  makers to TOYOTA; Enter on TOYOTA shows its two added cars with the Forza
  emblem (`img/garage-49-makers-run-103212.png`). No fault logged.
- Runs `host-20261023-104805` and `-105532` (one host series, so the second
  session loads what the first saved): test bootstrap with `savetest.lua`
  (`out/garagefix/cfg_full.lua`: 600 fake entries 360..959 in 60 makers "DSFT
  nn", every second one unlocked and owned, plus flags for 305, 320, 500, 650,
  999). 109 maker groups (60 DSFT, the stock makers, the new ones), header
  "480/780 OWNED"; the menu scrolls to the 109th group (TOYOTA), its car
  browser opens with Forza emblems, leaving and re-entering VEHICLES refills
  the menu (`img/garage-109-makers-run-104805.png`, `img/garage-run-105532.png`).
  Save: "saved 303 added-car entries to ...\savegames\dsfforza-garage.dat";
  the second session logs "loaded 303 added-car entries", and every flag of
  305..999 reads back true (take-a-ride count 5). `savecheck.py` on
  `-105532\save`: `autosave.dat` 19704 bytes, version OK, no ID ≥ 304 in the
  stock block (180 of 304 slots); the side file holds 303 entries with flags,
  bits and owned-list entries. No crash, no fault in either run.

Inputs of these runs: `-Override` `Install\gui.sp=out\testdrive3\gui\gui.sp`, `media\Fonts.fchunk=out\emblem\td3\Fonts.fchunk`, `Locale\English\global.fchunk=out\testdrive3\gui\Locale\English\global.fchunk`; `-LuaFiles` with
`out/emblem/garagetest_on.lua`, `out/emblem/td3/hud.lua`,
`out/testdrive3/dngvehicles.lua` (and for `-104805`/`-105532` the test
bootstrap, `savetest.lua` and `out/garagefix/cfg_full.lua`), `-MenuKeys`
Enter, Down × 110, Enter, Right, Esc, Esc, Enter, Up × 3, Esc
(`out/garagefix/keys-full.txt`).

**Pending** (the sandbox stopped surviving a game start after a host
reboot on 2026-10-23; the host runs above cover `garage` and the save round
trip. `garage-tests.ps1 -Vm` runs in the headless VM, but the VM copies the
host profile into the guest at the start of every series, so `stock` and
`check` need the sandbox or a host series that keeps the save):

- `stock`: the same save without the plugin loads and reaches Story, added
  IDs read false.
- `check`: with the plugin again the five IDs read back true from the side
  file (persistence across a session, and across an unmodded one).
- `garage`: picking a ported car at 304+ from the garage and driving it
  out (the maker menu past 40 entries and a late maker's car list are
  answered above).

### Open

- A maker group of 20 cars with `GarageEmblem` cars (card 20, see
  gui-assets.md "Garage card emblem") together with more than 40 groups:
  not run.
- Achievements and stats that loop over IDs 0..303 (FUN_004F9490 over the
  unlocked-ever bits) don't see added cars; only the take-a-ride count is
  extended.
- The save's stock flag list: 146 stock cars + up to 61 + 92 low IDs
  (`--id-policy asi`) = 299 of 304 slots. When it is full, stock-range IDs
  spill into the plugin's table (and side file) instead of crashing; without
  the plugin they would hit the stock NULL write.
- `[garage] side_file` renames the side file; a second save profile folder
  gets its own (it sits next to that profile's `autosave.dat`).

## HUD badges and plates

Module `hud` (`tools/asi/src/mod_hud.cpp`). Evidence, the full patch table
(VA, original and new bytes) and the data side are in `gui-assets.md`,
"Unlimited badges and plates". In short: the call at 0x006C6855 (the
vehicle info record ctor FUN_006C40C0) goes through a stub that reads the
optional vehicleStats fields `ManufacturerGlyph`/`ModelGlyph` while the
entry is on the Lua stack; ten `element->SetText(record+0x90/+0x92)` call
sites (driving HUD 0x006D4EA0/EB8, Shift HUD 0x006CFBDD/BF6, HUD panel
0x006DBADD/AF6, garage shop 0x0070E361/E373, vehicle panel
0x00711687/169F) call `hud_badge`/`hud_plate` instead, which copy the
16-bit glyph's record over a per-element slot record (codes 1..8, 11, 12,
14, 15) in the loaded vehicles_font and set that byte as the text. Cars
without the fields are untouched. The ctor hook is skipped unless its bytes
and at least one site match.

**Garage card emblem** (`[hud] card_emblem=1`; details, evidence and the
patch table in `gui-assets.md`, "Garage card emblem"). The ctor hook also
reads vehicleStats `GarageEmblem`. The itemState write of the car list at
0x007118A5 (`8B 91 F8 00 00 00 89 3A`) becomes a call: for a car with
`GarageEmblem = 1` card N's `Spr3D.unlocked<N>` sprite (one per menu page:
maker list and card browser) is pointed at `Header_Sprite_20` dressed as the
emblem (SF UVs copied, texture `f012ef7ef05a2e2d` from gui_assets.py's
Fonts.fchunk); other cars get the stock resource back. Four
`call FUN_00ADA7F0` sites go through checks: the challenge browser's
(0x0070D72A, 0x0070D3BF; it uses the same card objects) put every disc back
to SF, the car list's (0x00711537, 0x00711354) give the spare its own UVs
back if a picture needs it. Applied only with the ctor hook. In game: run
`host-20261023-100401`.

**In game, [garage] with 120 groups:** opening the garage with
out/testdrive3 crashed (runs `host-20261023-080433`, `-080858`, `-081321`)
in the maker menu fill, not in the emblem hook: the 40-entry menu-script
lists (Garage and save, "The garage crash with 40 added cars"). Fixed; the
emblem shows in the fixed runs `-103212`, `-104805` and `-105532`.
