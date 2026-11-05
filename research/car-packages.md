# Car packages: cars from per-car files beside the vanilla dngvehicles.sp

Updated: 2026-11-02

Feasibility spike for the car mod manager (#170, #149, #162). The question is
whether `dsfcars.asi` can make `Driver.exe` 1.04 load cars from a file per
car, leaving `Install\dngvehicles.sp` as the vanilla game (or another mod)
has it. Evidence tags as in `dngvehicles.md`. Static outputs:
`out/ghidra/cp1`, `cp2` (ExploreAddresses) and `dsff xref`.

## Answer

Yes, and with little new code. A car request goes from model ID to SSLP
record to root entry index, and then to one `read entry(i)` on the vehicle
archive's root reader (confirmed-static, below). `dsfcars.asi`'s `[archive]`
module already replaces every function of that reader. So the plugin can
add three root entries per car to the vehicle archive's table and serve
their reads from the car's own file. With SSLP and VSCT records added for
them, the game sees one archive. Four hook sites besides the existing reader
replacements; the vanilla archive is never written.

Status: **confirmed-runtime for one car** (VM run 2026-11-01, "Proof"
below): the M3 E30 spawns as ID 700 from its own package beside the
vanilla `dngvehicles.sp`, its three VEHC entries (700, 1700, 2700) are read
from the package file, and without a list the plugin hooks nothing and the
run is as before. Phase 1 (#451) adds the checks, several packages at once
(entries past 1100), a respawn, the read timing and the file name log
("Phase 1" below).

## 1. How the game finds a car's data today (confirmed-static)

| Step | Code | What it does |
| --- | --- | --- |
| open | FUN_005A11E0 state 1, call at 0x005A13C4 → FUN_008FC530 | the unified spool loader opens the package file (name at spool object +0x7304) into the root reader at spool object +0x14, async |
| root table | FUN_008FC0A0 (`h_poll`) | reads the CHNK header and the root entry table into the reader (`{tag, u32 offset, ..., u32 size}`, 16 bytes each) |
| SSIC | FUN_005A11E0 state 2 | scans root entries for `SSIC`, hands it to its handler; SSIC/NONR's children `VSRT`, `SSLP`, `VSCT` load in that order |
| SSLP | FUN_005A0B50 (this = spool object + 4; args reader, index) | sets spool object +0x72F8 = SSLP header, +0x72FC = records (header + 0x1C) |
| ID lookup | FUN_005A0A80 (contains), FUN_005A0B80 (kind), FUN_005A0BD0 (root index), inline in FUN_005A2230 | linear scans of `{u16 id, u16 root index, u16 kind}`, count = u16 at header +2; every reader goes through the two pointers at +0x72F8/+0x72FC (`xref disp 0x72f8/0x72fc`: no other users) |
| request | Lua → FUN_0066E650 → FUN_005A7860 → FUN_005A6FD0 | refuse unless FUN_0059C620 → FUN_005A0A80 finds the ID; capacity(kind) check; queue (FUN_005A7E30) |
| LODs | FUN_005A7FC0 | the interior and clean exterior are requested as `id + 1000` and `id + 2000`, which are SSLP records of kinds 1 and 2 |
| slot load | SceneModelType2_BeginLoad 0x008589C0 / 0x008589F0 → FUN_00858870 | stores reader at +0x548 and root index at +0x54C; takes the shared spool buffer (owner word 0x016DA7D0, one load at a time); `read size(i)` (0x008FB720), then **`read entry(i)` on the root reader** (0x008FBE90, call at 0x008588B7) |
| wait | SceneModelType2_Poll 0x00859000 state 2 | `h_poll(reader)` with state 0 → FUN_008FB2D0 → file busy (0x008574E0) on the root reader's handle |
| parse | Poll state 3 → FUN_008FC650 → FUN_008FC400 | opens the buffer as an in-memory CHNK; FUN_00858A70 copies the NONR into the slot's fixed buffer (SSLP +0x08 + 4 × kind) |
| VSCT | FUN_0086C630 (load), FUN_0086C550 / FUN_0086C5B0 (lookup by `id & 0xFFF`) | the table goes to `[Engine+0x780]+0x2C`; FUN_0086C5B0 has 11 callers, and two of them dereference its result without a NULL check (0x0080942B `movzx eax, byte [eax+0x1B]`, 0x00890949). `exe-vehicles.md` said only VEdit read VSCT; that is true for FUN_0086C540/0086C600 only. A car needs its VSCT records |

**File handles and offsets.** The file layer (`[0x0143D1C4]`) has 32 slots;
a reader holds one slot index (+0x5600). Open is FUN_00857370(name, flags),
read FUN_008573B0(slot, buf, size) (overlapped, `OVERLAPPED.Offset` only),
busy FUN_008574E0, wait FUN_008FB080, close FUN_008575D0. Positions are
32-bit per file: CHNK offsets are u32, the seek kept them below 2 GiB until
`[archive] large_files` (`spool-limits.md`). So the 4 GiB ceiling belongs
to one file. Nothing in the request path needs the cars to share a file;
only the root reader's entry table and the SSLP and VSCT records name them.

Every caller of `read entry` (10 sites, `xref call 0x8fbe90`) passes the
reader and the index, so a redirect keyed on (reader, index) catches any
of them. Only a nested reader (mode 3) over a VEHC entry would bypass it,
because `seek` adds the parent entry's offset to a position in the parent's
file. No vanilla path was found that opens a VEHC that way: the slot reads
it whole. The plugin logs it if one ever does.

## 2. Options

| Option | What to patch | Risks | How it fails |
| --- | --- | --- | --- |
| (a) a second vehicle `.sp` registered beside the vanilla one | the spool manager keeps one 0x734C-byte object per package type (FUN_0059D740, `type * 0x734C`; vehicles are type 0). A second archive needs a second type-0 object, request routing by ID in FUN_005A7860/FUN_005A6FD0 and everything else that takes "the" vehicle object, its own 35+2+2 slots (35 × 0x6D000 NONR buffers, 15 MB each time) and SSIC loaded twice | many call sites take the type's single object; slot capacity and the shared buffer lock are per type; least known | wrong object → refused requests or two slots for one car; large surface |
| (b) one small `.sp` per car, opened as its own reader, the ID lookup picking the reader | the SSLP → (reader, index) mapping and BeginLoad's reader (+0x548); one reader (0x5618 bytes) and **one of 32 file slots per car**, or open/close around every load | the 32-slot file table (vanilla already uses some) caps open cars; per-car readers need the async open state machine each time | out of slots → open fails → car missing |
| **(c) virtual archive: per-car files behind the existing root reader** | append 3 entries per car to the root reader's table; redirect `read entry`/`name` for them to the car's file; append SSLP and VSCT records | reads bypass the game's file layer (own Win32 handle, no 32-slot limit); see the risks below | a bad package is refused at start (logged) and its ID simply doesn't exist, which is what the request path already handles for unknown IDs |
| (d) one generated archive per install (v1 of #170) | nothing new | still one file: 4 GiB, replaces the vanilla archive | as today |

**Chosen: (c).** It touches only code the plugin already owns, plus four
small sites. Notes on the risks the brief named:

- **Async I/O.** The slot polls the archive's own handle after `read entry`
  (FUN_008FB2D0). A package read on another handle would look complete
  before it is, so the plugin first read synchronously (ReadFile with an
  offset, plugin-owned handle) before returning, which put the data in the
  buffer before the poll ran. That cost one 1 to 2 MiB read per VEHC load,
  measured on the main thread (one took 80 ms in a VM). The read is now
  asynchronous: it runs on a thread-pool thread and the poll, which the
  plugin already replaces, reports busy until it is done ("Asynchronous
  reads" below).
- **Streaming.** VEHCs are read whole into the shared buffer (one at a time:
  the owner word 0x016DA7D0). SSIC's MDXN streams through nested readers
  (`spool-limits.md`), but SSIC stays in the vanilla archive, so nothing
  changes there.
- **LODs at +1000/+2000.** These are SSLP records of kinds 1 and 2 pointing
  at the package's entries 1 and 2, so they work like the base car.
  Base IDs stay below 1000 (FUN_005A7FC0 adds 1000 and 2000 as `short`).
- **The garage, saves, Lua.** Unchanged: they key on model ID, and
  `vehicleStats` comes from Lua (below).
- **Shift's preload, traffic.** These go through the same request path
  (FUN_005A7860), so they need nothing of their own. Untested.
- **ID clashes.** The plugin leaves out a package whose ID (or +1000/+2000)
  is already in the archive's SSLP or in an earlier package (logged), so an
  archive from another mod wins and our car is absent, not corrupted.
- **NONR size.** The slot's NONR buffer comes from the SSLP header
  (+0x08 + 4 × kind, vanilla 0x6D000/0x1000/0x1000). The plugin reads each
  package's NONR sizes at start and leaves out a car that doesn't fit, in
  place of the heap overflow FUN_00858A70's unchecked memcpy would do.
- **Limits.** Root entries: 453 + 3 per car, under `max_entries` (16384).
  Base IDs: 999, so at most 849 added cars; 573 + #162's 177 = 750. File
  size: each package is its own file (a car is about 5 to 6 MB), so there
  is no total size limit.
- **UIDs.** Model, texture and part UIDs are global keys (`dngvehicles.md`,
  Open 6). Two packages that define the same UID break as they do in one
  archive. The manager checks this at install (`port_car.uid_clashes`
  over the installed set); the plugin doesn't.

## 3. What else a car needs, outside the vehicle archive

| Need | Today | From a package at run time? |
| --- | --- | --- |
| SSLP records | merged into SSIC | **yes**: hook FUN_005A0B50 (proven, 2026-11-01) |
| VSCT records | merged into SSIC (host's three records) | **yes**: hook FUN_0086C630 (proven); the package carries them as a 4th entry |
| `vehicleStats` entry | `UserLuaScripts\dsfcars\ported.lua`, read by `register.lua` | yes. These are loose Lua files, so the manager writes them from the enabled packages' metadata (no archive). `register.lua` drops IDs the plugin left out, and every package car when the plugin's packages aren't active (#511; `asi.md` "Telling Lua the plugin is there") |
| HUD badge and plate (`ManufacturerGlyph`/`ModelGlyph`, `hud.lua`) | glyphs merged into `media\Fonts.fchunk` (vehicles_font atlas) | not yet. A font's glyphs are records into page textures of one FNTD, and all pages are sized like page 0 (`gui-assets.md`). Runtime glyph pages would need new texture resources; recommended instead: a **generated** Fonts.fchunk in the manager's folder, opened in place of the vanilla one (below) |
| garage picture | SPOIs merged into `Install\gui.sp` (UIDL, SPOL, SSLP) | possible with the same root-entry injection (gui.sp also loads through FUN_005A11E0 and has an SSLP), plus UIDL and SPOL records. Simpler first: a generated gui.sp, opened in place of the vanilla one |
| maker and model names | `Locale\<lang>\global.fchunk` strings 700000+ and 900000+ (`"ID:n"`) | plausible at run time: the renderer turns `"ID:n"` into a string through FUN_00835250/FUN_008352B0 (`gui-assets.md`). A hook there could serve IDs 700000+ from the packages' names (hypothesis, needs RE). Until then: generated copies |
| garage groups | `[garage]` from `vehicleStats` (`ManufacturerName`, 120 groups) | yes, nothing per car |
| save flags | IDs below 304 in `autosave.dat`, 304..999 in `dsfcars-garage.dat`, keyed by ID | yes, if IDs are stable (the manager's key → ID map). A removed car's ID stays reserved (tombstone) so a new car doesn't inherit its owned/unlocked flags |

**Generated files instead of rewritten ones.** The manager writes merged
copies of gui.sp, Fonts.fchunk and global.fchunk from the vanilla files plus
the enabled packages, into its own folder. The plugin opens those in place of
the game's (`Install\gui.sp` → `dsfcars\generated\gui.sp`), so no game file
is written. The opens go through the file layer's FUN_00857370(name, flags),
18 call sites. Hooking that function (first instruction `A1 C4 D1 43 01`,
the slot table's singleton) rewrites a name from a table. Whether *every*
open of these three files goes through it is a hypothesis; phase 1 logs
every name it sees. They are small next to the vehicle archive (the
573-car build's gui.sp is 163 MB against 79 MB vanilla, its Fonts.fchunk
4.5 MB), and gui_assets.py writes them by appending to the vanilla files, from
pictures and glyphs each package already carries. How long that takes per
change is not measured.

## Design

- **Package (installed form):** `<game>\dsfcars\cars\<key>\car.sp`, a CHNK of 4
  root entries: the car's three VEHC entries (port_batch's `work/<media>.chnk`)
  and `VSCT` (3 × 28 bytes). It is the package's car.sp byte for byte,
  with its builder's model ID in the full VEHC (VSVH, VO3D, SPID, UPVH;
  `clone_vehicle.ID_FIELDS`). The plugin writes the listed ID into those
  four fields as it serves the read, and into the SSLP and VSCT records it
  adds ("Model ID in memory" below).
- **Distributed form (#170):** a `.dsfcar` zip per car with the 4-entry
  `.sp` (any ID), the garage picture, badge and plate images, metadata
  (names, year, stats, maker group) and requirements ("Package format 1"
  below). The manager unpacks it as it is and writes the list.
- **List:** `<game>\dsfcars\cars\cars.txt`, one `id path` per enabled car,
  written by the manager. Disable = drop the line (the file stays); remove
  = drop the line and the file (the ID stays reserved in the map).
- **Plugin (`[archive] packages=1`):** at start, open every listed package,
  check it (CHNK v3, uncompressed, 3 VEHC + VSCT, NONR sizes) and
  log it. Hooks:

| Site | Original bytes | New | Purpose |
| --- | --- | --- | --- |
| 0x005A13C4 (in FUN_005A11E0) | `E8 67 B1 35 00` (call 0x008FC530) | `call h_spool_open` | calls the original; remembers the reader whose file name ends in `dngvehicles.sp` |
| `h_poll` state 3 (0x008FC0A0, already replaced) | – | `pkg_inject` | appends 3 entries per package to that reader's root table (side table past 1100) |
| `h_read_entry` (0x008FBE90) and `h_name` (0x008FB770), already replaced | – | branch | for package entries, a read from the package file (for the slot's load on a thread-pool thread, `h_poll` busy until done; "Asynchronous reads"); for a full VEHC, the listed ID written into its four ID fields (`pkg_set_ids`) |
| `h_seek` (0x008FB5E0), already replaced | – | log | a nested reader over a package entry (not supported) |
| 0x005A0B50 | `8B 44 24 08 56` | `jmp h_sslp_load` | an SSLP copy with 3 records per package (clash and NONR checks) |
| 0x0086C630 | `8B 44 24 08 56` | `jmp h_vsct_load` | a VSCT copy with the packages' records; sets `[Engine+0x780]+0x2C` as the original does |
| 0x00857370 | `A1 C4 D1 43 01` | `jmp h_file_open` (trampoline back to 0x00857375) | `[archive] open_log` logs every name (phase 1); phase 3 adds the redirection for generated gui.sp / Fonts.fchunk / global.fchunk |
| 0x00E3723C (vtable slot) | `0x0085DAD0` | `h_pump` | `[archive] read_log` only: the main thread and the main loop's frame times ("Asynchronous reads") |

All sites are checked byte for byte before writing, and are all-or-nothing,
as for the other modules. Without the reader patches there are no packages.

## Proof (confirmed-runtime, 2026-11-01)

```sh
uv run dsff car_package make --chnk <main>/out/build/work/BMW_M3E30_91.chnk --id 700 --host 205 \
    --out out/cp/game/dsfcars/cars/BMW_M3E30_91.sp --list out/cp/game/dsfcars/cars/cars.txt
# 0: VEHC 0x1000 size 0x17D000 (ID 1 -> 700); 1: VEHC 0x17F000; 2: VEHC 0x317000; 3: VSCT 0x54
```

```powershell
$env:PATH = "C:\Program Files (x86)\Microsoft Visual Studio\Installer;$env:PATH"
.\tools\asi\build.ps1 -Test
# a copy of out\asi\dsfcars.asi + dsfcars.ini with [archive] packages=1 in out\cp\asi\
.\tools\wsb\run-series.ps1 -Vm -Asi out\cp\asi\dsfcars.asi -ArchiveOnly `
    -Archives out\vanilla\Install\dngvehicles.sp,out\vanilla\Install\dngvehicles.sp `
    -Overlay '',out\cp\game -Labels without-package,with-package -ProbeIds '266,700' `
    -LuaFiles 'dsfcars\ported.lua=out\cp\lua\ported.lua' -WaitSeconds 240
```

`out\cp\lua\ported.lua` is the main build's `dngvehicles.lua` cut to one
entry, `[700]` (host 205), so the car has a `vehicleStats` entry.

- **Without the package** (`runs\vm1-20261101-120510`): the log says `no list
  ...; no car packages` and no package site is hooked. 266 spawns; the 700
  request finds no SSLP record and the player stays in Tanner's car (62).
- **With it** (`runs\vm1-20261101-120730`), `dsfcars.log`:

  ```
  packages: 700 dsfcars\cars\BMW_M3E30_91.sp: VEHC 1560576/1667072/2097152 bytes, NONR 0x59060/0x360/0x410
  packages: 005A13C4 spool open, 005A0B50 SSLP load, 0086C630 VSCT load hooked; 1 car packages
  packages: vehicle archive 'Install\dngvehicles.sp' opens in reader 11D7BDC4 (ok)
  packages: reader 11D7BDC4: root entries 453..455 are the 1 car packages
  packages: SSLP at 11D7BDB4: 450 records + 3 for 1 car packages
  packages: VSCT: 450 records + 3 from car packages
  packages: 700: entry 453 (1564672 bytes) read from its package
  packages: 700: entry 454 (1671168 bytes) read from its package
  packages: 700: entry 455 (2101248 bytes) read from its package
  ```

  The probe spawns the M3 E30 as 700 (chase view shot; the car list then
  reads `266x1 700x1`), switches to the cockpit view (1700 read from the
  package) and back, and releases it; no crash. The interval of the shots
  (10 s) missed the cockpit view; phase 1's series takes it.

**A bug found before the run.** The first draft compared
`*(Reader**)(self + 0x10)` with the vehicle reader in the SSLP hook. The
reader is *embedded* at spool object +0x14: the constructor 0x005A10F0 sets
the vtables at +0 (0x00DE4D60), +4 (0x00DE4D48, whose slot 0 is
FUN_005A0B50, so `self` = spool object + 4) and +0xC (0x00DE4D3C, whose
FUN_005A11E0 opens `lea ecx, [esi+8]`), and builds the reader in place
(`lea ecx, [esi+0x14]; call 0x008FB9D0` at 0x005A112F). The pointer read
from it is the reader's vtable, so the check never matched and the packages
would have been silently missing. Fixed: `(Reader*)(self + 0x10)`.

**Call sites, byte-checked** (`dsff xref dis`): 0x005A13C4 `E8 67 B1 35 00`
(`push 0; push 1; push edi=[esi+0x72F8] name; lea ecx,[esi+8]; call
0x008FC530`); 0x005A0B50 and 0x0086C630 `8B 44 24 08 56` (both reached only
through their vtables, 0x00DE4D48 and 0x00E3967C: `xref call` finds no
direct caller). SSLP records are `{u16 id, u16 root index, u16 kind}`:
FUN_005A0B80 returns +2, FUN_005A0BD0 returns +4 (as
`clone_vehicle.register_clones` writes them).

## Phase 1: the loader (confirmed-runtime, 2026-11-01)

**Checks at start** (`pkg_open`, `pkg_load_list`): a package is a CHNK v3
(not compressed) of exactly four entries, three VEHC and a VSCT of three
28-byte records, every entry inside the file (file size at most 4 GiB),
each VEHC a CHNK whose child entries lie inside it, and its NONR sizes
noted. The VSCT is required: two callers of FUN_0086C5B0 (0x0080942B,
0x00890949) dereference the lookup without a NULL check. A list line whose
ID isn't 1..999 or is already listed, or whose package fails a check, is
left out with a log line saying why. At the SSLP load a package whose ID
(or +1000/+2000) the archive already has, or whose NONR is larger than
the slot buffer, is left out too. When none is left, nothing is hooked.

**Offline selftest** (`tools\asi\build.ps1 -Test`,
`mod_archive_pkg_selftest`): a list of good and bad lines (bad line, ID 0,
1000, a duplicate ID, a missing file, not a CHNK, no VSCT, cut short, a
NONR past its VEHC) keeps 5 of them. Injection into a fake reader of 453
root entries and one of 1098 (the packages cross into the side table),
`pkg_of`, reads with a zeroed tail, `h_name`, the SSLP copy (the clash
and the NONR-too-big package left out, another spool object's SSLP left
alone), the VSCT copy (IDs written into the host's records), and the
three sites: a changed byte at 0x0086C630 leaves all three vanilla.

**VM series** (`run-series.ps1 -Vm`, vanilla `dngvehicles.sp`, `-ProbeIds
'266,700,701,702,703,700' -ShotSeconds 3`):

- **four packages** (`runs\vm1-20261101-122452`): the M3 E30 (700), Supra
  RZ (701), RX-7 (702) and S2000 (703) from their packages, each in the
  chase and cockpit views (1700 to 1703 read from the packages), then 700
  again after its release (the full car stayed resident, the 1700/2700
  entries were read again). Root entries 453..464, SSLP and VSCT 450 + 12.
- **past 1100** (`runs\vm1-20261101-122800`): 230 filler lines on one
  package file (IDs 704..933; their VEHCs say 703, but no filler is
  requested), then 266 (a clash), a missing file, 700 and 701. 233
  packages, root entries 453..1151, so 700 and 701 are entries
  1146..1151, in the side table. Both spawn, cockpit included. 266 is left
  out (`already in the archive`) and the vanilla Desanne spawns as 266;
  the missing file is left out at start. 702 and 703 aren't listed, so
  the probe stays in Tanner's car (62), as an unknown ID does.

**The synchronous read's cost** (`pkg_read`, QueryPerformanceCounter
around ReadFile): 1.5 to 2.4 MB a read. Run A, 14 reads: 0.37 to 7.9 ms,
median 3.3 ms. Run B, 8 reads of the same files: 0.31 to 0.49 ms (in the
cache by then). A first read from a hard disk would take longer (2 MB at
100 MB/s is 20 ms, more than a frame). Not made async here; since made
async ("Asynchronous reads" below), after a run showed the reads on the
main thread and one of 80 ms.

**File names through FUN_00857370** (`[archive] open_log=1`): 47 opens in
the run, among them `install\gui.sp` (early) and `Install\GUI.sp` (the
spool), `Media\fonts.fchunk` and `TEXT:global.fchunk` (a device prefix;
`TEXT:loc.sp`, `TEXT:billboards.sp` too). So all three GUI files of
phase 3 open through this function, under names that differ in case and,
for the locale file, by a device prefix rather than a path: a redirect
table must match without case and know the `TEXT:` device.

## Package format 1 (phase 2, #452)

`src/dsfforza/build/dsfcar.py` writes and checks it; `dsff car_package pack`
makes one per car of a port_batch build, `validate` checks them.

**`<key>.dsfcar`**, a zip (stored, not deflated: car.sp is mostly DXT
textures, the picture is DXT5), members at its root and nothing else:

| Member | What | Where the rule comes from |
| --- | --- | --- |
| `car.json` | the manifest (below) | |
| `car.sp` | the installed form: CHNK v3, not compressed, entries VEHC, VEHC, VEHC, VSCT (84 bytes, three records, all for `car.host`); each VEHC's entries inside it; NONR at most 0x6D000 / 0x1000 / 0x1000 | dsfcars.asi `pkg_open` and `h_sslp_load`; the slot buffers are the vanilla SSLP header's (+0x08 + 4 × kind) |
| `picture.dds` (optional) | the garage picture, 512 × 256 DXT5, no mips | gui.sp's car pictures (`gui-assets.md`; `gui_assets.picture_dds`) |
| `badge.png` (optional) | the maker badge as the HUD and the maker list draw it: 8-bit RGBA, at most 98 × 58 | `gui_assets.badge_glyph`: a logo fitted to BADGE_SIZE (58) square, a text badge to CELL − 1 (98) × TEXT_BADGE_H (30) |
| `plate.png` (optional) | the model nameplate: 8-bit RGBA, at most 98 × 34 | `gui_assets.nameplate` defaults |

**Manifest:**

```json
{
  "format": "dsfforza-car", "format_version": 1,
  "key": "forza.fh1.BMW_M3E30_91",
  "version": 1,
  "car": {"file": "car.sp", "sha256": "…", "model_id": 1, "host": 205, "sound_host": 205},
  "names": {"display": "BMW M3E30 (1991)", "make": "BMW", "model": "M3", "family": "M3",
            "year": 1991, "maker_group": "BMW"},
  "garage": {"speed": 0.48, "strength": 0.3, "drift": 0.65},
  "images": {"picture": "picture.dds", "badge": "badge.png", "plate": "plate.png"},
  "requires": ["archive.packages", "archive.ids"],
  "source": {"pipeline": "dsfforza", "game": "FH1", "media": "BMW_M3E30_91"}
}
```

- `key` is `<source>.<game>.<name>` and stays the same across versions; the
  manager keys its ID map and save flags on it. `version` is the
  package's (a higher one replaces a lower one in place, keeping the ID).
- `car.model_id` is the ID car.sp holds, nothing more: the plugin writes
  the listed ID over it in memory ("Model ID in memory"), and two packages
  may hold the same one (packages from different builds do). The full VEHC
  must hold it once in each of VSVH, VO3D, SPID and UPVH, as a vanilla
  car's does (`clone_vehicle.make_clones` checks the same four). Base IDs are 1..999 (the LOD
  requests add 1000 and 2000 as `short`). `host` and `sound_host` are
  vanilla IDs (0..303): the host's `vehicleStats` entry is the base of the
  car's, its VSCT records are car.sp's fourth entry.
- `names`: Forza's names (`gui_assets.car_texts`), the display name of the
  build's manifest, and `maker_group`, the DSF maker key (`dsf_make`) the
  garage groups and the vanilla badge reuse go by.
- `garage`: the stat bars (`vehicleStats` Speed, Strength, Drift, 0..1;
  `handling_dsf.stat_bars`).
- **Nothing global.** No model ID the game will use, no glyph code, string
  ID (`"ID:7000xx"`), picture name (`Picture1`) or garage group number:
  the manager allocates those against what is installed. Unknown fields
  are refused, at every level.
- `requires`: capabilities of dsfcars.asi: `archive.packages` (the loader;
  always required) and `archive.ids` (the plugin writes the listed ID into
  the data, "Model ID in memory"; port_batch's packages list both). The
  manager checks them against the installed plugin ("The plugin's
  capabilities").
- Images are what the build drew (the Forza logo or a text badge, the
  chrome plate, the rendered picture), not glyph codes. Without one the
  manager uses the game's own badge or plate for that maker or model, or
  the host's picture.

**Checked** (`dsff car_package validate`, also on read): the members, the
manifest's fields and types, car.sp against the loader's checks and the
manifest (SHA-256, `car.model_id` in each of the four ID fields, the VSCT
host), the images' sizes and formats.

**Verified:** the four cars of the loader runs, packed from the main build
(5.2 to 6.2 MB each, about 3.6 s for four) and installed as 700 to 703 with
`install_sp` (the ID written into the file; removed since), are byte for
byte the `.sp` files that ran in `runs\vm1-20261101-122452`. Run from the packages, with `ported.lua`
entries written from their manifests (`runs\vm2-20261101-124716`): all
four spawn as 700 to 703, in the chase and cockpit views.

**Open (phase 3):** engine sound templates stay with the manager, not in a
package. A pack (several cars) is a folder of `.dsfcar` files for now,
without a manifest of its own.

### The garage card emblem is in each package (#510)

A package is self-contained: it carries its source's garage card emblem as
the image `emblem.dds` (`images.emblem`), 512 × 256 DXT5, one level, laid
out like the card icon atlas. The format names no game: this pipeline writes
the Forza logo (`gui-assets.md`, "Garage
card emblem"). `dsfcar.pack` (port_batch `--packages`) writes it into every
package with `gui_assets.emblem_texture`: FH1's `UI4/Common/forza_logo3.xds`
(FM4's, else), the header of vanilla gui.sp's `nissan_370z` picture, the same
call `gui_assets.build` makes for the merged Fonts.fchunk; byte for byte the
TEXC `f012ef7ef05a2e2d` of the 721-car build's `gui\Fonts.fchunk` (master
8200cfa). About 128 KiB per package. `validate` checks it
(`dsfcar.emblem_problems`).

- The manager installs it with the car's other images, in the car's folder.
- Every GUI generation (install, enable, disable, remove, reassign):
  `car_gui.generate` adds the emblem the most enabled packages carry to the
  Fonts.fchunk copy (unless the game's font already has one), and writes
  `GarageEmblem = 1` for the cars whose package carries that emblem.
  dsfcars.asi [hud] card_emblem binds one texture for
  `gui_assets.FORZA_LOGO_UID`, so one emblem goes in; a car whose package
  carries another (a future source's) keeps the SF disc until the plugin
  binds one per car.
- A package without one (v0.2.0's) keeps the SF disc on its card. #510 was
  first fixed with one shared copy kept by the manager (#523); it moved into
  the packages so each one is complete on its own.

## Manager, part 1 (phase 3, #453)

`src/dsfforza/build/car_manager.py`, `dsff car_manager install | enable |
disable | remove | status | verify [--game G] [--archive A]`. It refuses
to change anything while Driver.exe runs from that folder, and writes only
under the game folder's `dsfcars\cars\` and
`Common\UserLuaScripts\dsfcars\packages.lua`:

| File | What |
| --- | --- |
| `dsfcars\cars\manager.json` | the state (format 2): `ids` (key → model ID, for good), `cars` (installed: what the manager owns of each, ID, version, enabled, SHA-256 of the installed `.sp`); until "Packages only" also `merged` (a merged install's cars), dropped on load since. The rest of a car is its folder's `car.json`, read where it is needed (packages.lua, the GUI files, status, verify); a car without a readable one is left out of the outputs and `verify` says so. Format 1 kept a copy of each manifest in `cars`; the manager upgrades such a state in place on load (car.json written from the copy where a folder lacks it). On the 573-car format-1 install: 0.16 s, 609 to 117 KB, `packages.lua` and `cars.txt` byte for byte as before |
| `dsfcars\cars\cars.txt` | the plugin's list: `id path` per enabled car |
| `dsfcars\cars\<key>\` | one car: the package's `car.sp`, `car.json` and images as they are (car.sp keeps its builder's ID; the plugin writes the car's ID in memory) |
| `Common\UserLuaScripts\dsfcars\packages.lua` | `ported.lua`'s fields per enabled car; `register.lua` adds them to its cars for the IDs `ported.lua` doesn't have |

**IDs.** A key keeps its ID across upgrades (a higher version replaces the
package in place; a lower one needs `--force`), disable and enable, and
after a remove: the ID stays reserved, so no new car inherits the old one's
save flags (the game and dsfcars.asi keep them per model ID). A new key
takes the first ID in port_batch's "asi" order (1..303, then 304 up; base
IDs below 1000) that the installed `Install\dngvehicles.sp`'s SSLP doesn't
use (kind 0 records: 150 in the vanilla archive, 62..303; 723 in the
573-car build), that isn't `RESERVED_IDS` (Felony Rework's 622/623) and
that no key holds. Until "Packages only" (below), a merged install's cars
(its `ported.lua`) were seeded on every run and a package of one of them
was refused, because the archive had that car; now `seed` reads
`ported.lua` once, for the migration, and nothing else does. Keys that differ
only in case are refused (they would share a file on Windows). An install
or upgrade is staged as a folder beside the installed one and swapped in with the
state; a failure at any step puts the installed files and state back. `verify` reports an installed `.sp` that is
missing or isn't the package's (its `car.sha256`; an install from before
"Model ID in memory" holds the file with the car's ID written in, whose
checksum the state recorded), a car whose ID the installed archive now uses (another
mod's archive: the plugin leaves the package out), and a list that doesn't
match the state.

**Verified in game** (`runs\vm2-20261101-125804`, vanilla archive): the
four `.dsfcar` packages installed with the manager took IDs 1 to 4 (free
in the vanilla SSLP), and the RX-7 (3) was disabled. The plugin listed 1, 2
and 4 and loaded them, chase and cockpit. `register.lua` took their entries
from `packages.lua` (`id 4 (TOYOTA SUPRA RZ)`, host 229). 3 is not in the
archive, so it doesn't load. Reads: 1.0 to 14.6 ms (the 14.6 ms one is a
first read of the Supra's interior).

## Manager, part 2: pictures, badges, plates and names (phase 3, #453)

**Generated files** (`src/dsfforza/build/car_gui.py`; the manager writes
them on every change). Copies of the game's current `Install\gui.sp`,
`media\Fonts.fchunk` and each language's `Locale\<lang>\global.fchunk`
(vanilla, or a merged install's) with the enabled packages' parts appended
by gui_assets' own writers (`add_pictures`, `add_glyphs`, `add_strings`),
under `dsfcars\generated\`, plus `redirect.txt` and
`Common\UserLuaScripts\dsfcars\packages_hud.lua` (`hud.lua`'s fields per
car; `register.lua` adds them for the IDs `hud.lua` doesn't have). What a
car gets is what `gui_assets.build --wide-glyphs` gives a ported car, from
the package's images: a maker or model DSF has keeps its vanilla badge or
plate (`vanilla_codes`), others get a 16-bit badge (one per maker group)
or plate (one per text) from `badge.png` / `plate.png` and a name string;
the picture is `dsfforza_<media>`; `GarageEmblem = 1` for a car whose
package carries the emblem the Fonts.fchunk copy holds ("The garage card
emblem is in each package", #510), or the game's font when it already has
it (a merged install's). Until #510 (v0.2.0) only the latter, so a packages
install over vanilla files showed the SF disc. Codes and strings are taken where the
current files have none: 16-bit codes past every vehicles_font code from
256 up, strings from 700000 up, reusing an ID that already has the same
text (cars of a maker share the ManufacturerName, so the garage group, a
merged install gave it), and the maker list's one-character strings at
900000 + code.

**Redirect** (`[archive] redirect_list`, with `packages=1`). The opens
of all three files go through FUN_00857370 ("Phase 1"), whose path is
resolved further down: 0x00857270 → 0x0085ACD0 → 0x0085AB80, which turns
the device path into a file path (0x0085B070) and calls CreateFileA at
0x0085AC1E (`FF 15 60 92 DC 00`, `call [0x00DC9260]`, the
KERNEL32.CreateFileA import; GetFileSizeEx follows at 0x0085AC31). That
call becomes `call h_create_file` + NOP: a listed path (compared without
case, a leading `.\` or the game folder) opens its replacement, and one
that won't open falls back to the game's file. So `TEXT:global.fchunk`
needs nothing of its own: it reaches CreateFileA as
`locale\english\global.fchunk` (`runs\vm1-20261101-141311`). The
game's files are never written. A name the table doesn't list goes on
through Driver.exe's own import slot (`[0x00DC9260]`, read at each call),
not to KERNEL32 directly: DSF Enhanced's `[Files] Redirect` hooks that
slot for its tables (worldlod's `san_francisco.dngc`, which opens at this
site too), so both plugins redirect whichever loads first
(`archive.redirect_chain` among the plugin's capabilities; offline-checked,
`tools\asi\build.ps1 -Test`).

**Verified in game** (`runs\vm1-20261101-141311`, vanilla archive and
GUI files, the four packages installed by the manager with `--gui-base`
the vanilla files): the plugin replaced all three files (`redirect:
'media\fonts.fchunk' opened as ...\generated\Fonts.fchunk`, the same for
`locale\english\global.fchunk` and both opens of `install\gui.sp`);
`hud: model 1 ManufacturerGlyph 256 ModelGlyph 257`; the driving HUD
shows the M3's BMW badge and "M3" plate from the package's images.
`register.lua` made the four cars selectable under their own groups
(ID:700000 BMW, ...). In the garage (`runs\vm1-20261101-142300`,
`garagetest_on.lua` and `-MenuKeys`) the maker list ends with BMW, HONDA,
MAZDA and TOYOTA (added groups come after the vanilla ones), each with its
name from the generated global.fchunk and its 16-bit badge, and the Supra
RZ's card shows its package's picture. The card has the SF disc, not the
Forza emblem: the vanilla Fonts.fchunk has no emblem texture (see above;
fixed by the emblem in each package, #510).

## Pipeline (phase 4, #454; opt-in until "Packages only" below)

- `port_batch build --packages` writes `<build>\packages\<key>.dsfcar`
  for every car of the build (`dsfcar.pack`), after the pack step has
  saved its pictures and images and before `--clean-work` drops the work
  chunks. They are written to `packages.new` and swapped in whole, so
  `packages\` is always one build's complete set. A failure is reported
  (`packages_failed`, `packages_error` in the report) and the build
  stands, as for the GUI step, but no packages are left (the earlier
  ones don't match the new build). `install.ps1 -Packages` and
  `dsff install --packages` (`package_problem`) refuse a build whose
  packaging failed or whose packages aren't exactly one per car of its
  manifest, and with `-Build` they check before an earlier install is
  removed.
- `install.ps1 -Packages` (needs `-Asi`) installs DriverNGHook, the
  plugin with `[archive] packages=1`, `bootstrap.lua` and `register.lua`,
  and hands `<build>\packages` to the car manager. It doesn't install the
  archive, gui.sp, Fonts.fchunk, global.fchunk, `ported.lua` or `hud.lua`,
  so it doesn't check that those are vanilla either: another mod's
  archive or GUI files can stay (#149). `install.json` records
  `packages: true` and the package keys; `-Status` names them.
  `-Uninstall` removes what install.ps1 installed and leaves the
  manager's files (#455).
- `dsff install --packages` builds with `--packages` and installs with
  `-Packages`; with `--no-plugin` it stops in the preflight.
- The manager makes the GUI copies once per command (`Manager.batch`),
  not once per package: gui.sp alone is 80 to 160 MB.
- Verified end to end: `port_batch build --gui --packages` of three cars
  (packages in 2.9 s), `install.ps1 -Packages -Build` into a temp copy of
  the vanilla files (no game file changed; `packages=1` in the installed
  ini; IDs 1 to 3; one GUI generation), and in game with that plugin, ini
  and the manager's files (`runs\vm1-20261101-150003`): all three spawn,
  chase and cockpit, the redirect serves the generated files, and the HUD
  shows each car's badge and plate (`hud: model 3 ManufacturerGlyph 260
  ModelGlyph 261`). `install.ps1 -Status` names the packages; `-Force`
  reinstalls keep every car's ID; `-Uninstall` restores the game and
  leaves `dsfcars\cars`, `dsfcars\generated` and the two Lua files.

## Uninstall, migration and save safety (phase 5, #455; the switches as they were then)

**Uninstall.** `car_manager uninstall` removes every file the manager
wrote: the cars' `.sp` files and folders, `cars.txt`,
`dsfcars\generated\`, `packages.lua` and `packages_hud.lua`. It keeps
`dsfcars\cars\manager.json` with the key → ID map (no cars in it), because
the save keeps its flags per model ID (`autosave.dat` below 304,
`dsfcars-garage.dat` above): a later install gives every car the ID its
flags are for. `--forget-ids` removes the map too. `install.ps1
-Uninstall` of a `-Packages` install runs it first (without uv it removes
the same files itself and leaves the map), then removes what install.ps1
installed. A merged install over a package install (`-Force`) does the
same, so no `packages.lua` is left to give vehicleStats entries to cars
the merged archive doesn't have.

**Migration from a merged install.** `install.ps1 -Packages -Force` over
a merged install runs `car_manager seed` first: the merged install's
`ported.lua` IDs go into the map. Then the merged install is removed as
usual (every replaced file restored from its backup; none kept, as
`-Packages` installs none of them), and the packages take the IDs the map
has. Checked on a fake game with a three-car merged build (BMW 1, Toyota 2,
Mazda 3): after the migration the packages are BMW 1, Toyota 2, Mazda 3,
where a fresh package install gives Mazda 2 and Toyota 3 (key order); the
archive is vanilla again and `ported.lua` is gone. `-Uninstall` then
leaves only `manager.json`, and a reinstall gets the same IDs again.

**Another archive takes an ID.** `verify` reports a car whose ID the
installed archive now has (another mod's: the plugin leaves the package
out); `car_manager reassign KEY` gives it a new free ID (its old flags
stay with the old ID, which isn't ours any more). Only the state and the
list change; no file is rewritten.

**A save that names cars that aren't there** (confirmed-runtime). Every
package run used the VM's save, which carries flags for the 573-car
roster: `garage: loaded 420 added-car entries` from `dsfcars-garage.dat`,
and IDs below 304 owned in `autosave.dat`, with only 3 or 4 cars
installed. `runs\vm1-20261101-151827`: four packages installed and one
removed with the manager (the RX-7, ID 3; its ID reserved, its flags in
the save). The probe asks for ID 3: no SSLP record, the player stays in
Tanner's car (62), as for any unknown ID. The garage opens, the whole
maker list scrolls to its end (VOLKSWAGEN, Y.A.R.E, BMW, HONDA, TOYOTA:
no MAZDA group, so no card for the removed car), a group's cards open
(the Supra RZ, OWNED, its picture). No fault and no crash. Earlier runs
(`vm1-20261101-142300`, `-144831`, `-150003`) did the same with hundreds
of flagged IDs that have no car. A car that is disabled, removed or
uninstalled is simply absent: the garage lists only what vehicleStats has
(`register.lua` adds entries only for the packages it is given), and the
request path refuses an ID without an SSLP record.

**The garage header** ("x/y OWNED") is 140 + the added cars in every run
(713 with the merged roster, 144 with four packages, 143 with three):
package cars are counted like merged ones (#478, closed).

## Packages only (after phase 5)

Car packages are the only install; the merged install mode (the cars in
copies of `Install\dngvehicles.sp`, `gui.sp`, `Fonts.fchunk` and
`global.fchunk`, with `ported.lua` and `hud.lua`) is gone from
`tools\install.ps1` and `dsff install`.

- **install.ps1** has no `-Packages`, `-Asi` or `-IdPolicy`. The plugin is
  required (`-AsiPath`, default `out\asi\dsfcars.asi`, else a release's
  `plugin\dsfcars.asi`) and always gets `[archive] packages=1`. A build made
  here is `port_batch build --gui --wide-glyphs --id-policy asi
  --root-limit 3003 --packages`, as `dsff install`'s; it still needs the
  vanilla archive, which it reads. No game file is replaced, so another
  mod's archive and GUI files are no reason to stop an install;
  `dsfcars-backup\` holds only `install.json` and, with `-Force`, another
  mod's `bootstrap.lua`. Everything that can stop an install (the build's
  packages, uv, the plugin and its ini, a foreign bootstrap without
  `-Force`, the VC++ runtime, the ASI loader and DriverNGHook as they will
  be once an earlier install is gone) is checked before the game folder
  changes.
- **dsff install** has no `--packages` or `--no-plugin`: it always builds
  with `--packages` and installs with the plugin. The preflight checks the
  vanilla archive only when it builds.
- **Migration.** Over a merged install (an `install.json` in place without
  `packages`: every release up to v0.1.8) install.ps1 migrates without
  `-Force`: `car_manager seed` records `ported.lua`'s IDs in the map, the
  merged install is uninstalled as before (every replaced file restored from
  its backup and checked by SHA-256), `ported.lua` and `hud.lua` are deleted
  if its uninstall kept them (changed since that install), and the packages
  take the map's IDs. A `ported.lua` without an install record is seeded and
  deleted too. A package install over a package install still needs
  `-Force`. `-Uninstall` removes a merged install as it always did.
- **car_manager** no longer reads `ported.lua` on every load: no `merged`
  state, no `merged_conflicts`, no refusal of a package "already in the
  archive". An old `manager.json`'s `merged` key is dropped on load (its IDs
  are in `ids` already). `seed` alone reads `ported.lua`; a key the map has
  keeps its ID.
- **register.lua** reads `packages.lua` and `packages_hud.lua` first, then
  `ported.lua` and `hud.lua` for the IDs they don't have. The latter stay
  because the in-game tests still run a build's merged archive
  (`run-series.ps1 -Archives <build>\dngvehicles.sp` installs its
  `dngvehicles.lua` and `gui\hud.lua` under those names; `clonetest.lua`,
  `garagetest.lua`, `port_car --install-lua` and `gui_assets --install-lua`
  use them too), so the build keeps writing the merged archive, `gui\` and
  `dngvehicles.lua`. Its no-plugin paths (vanilla garage and save limits)
  stay for the same reason: series without `-Asi`. A player install has
  neither file after a migration.

**Verified offline** (`tests/test_install_packages.py`, install.ps1 in
Windows PowerShell on a synthetic game, the car manager through uv): a
merged install of two cars (IDs 40 and 3) with a changed `hud.lua` moves to
packages without `-Force`; the three packages get 40, 3 and 5 (a fresh
install gives 3, 5 and 6); the archive and gui.sp are the originals again;
`ported.lua` and `hud.lua` are gone; a second install needs `-Force` and
keeps the IDs; `-Uninstall` leaves the game as it was but for
`manager.json`, and a later install gets the same IDs. `-Uninstall` of a
merged install restores the game exactly. A bad `-Build` (a car without a
package) or a missing plugin changes nothing.

**Verified with real files** (`out\scratch-game`: copies of the vanilla
1.04 archive, gui.sp, Fonts.fchunk and English global.fchunk): master's
install.ps1 made a merged install of a three-car build (`out\b3`: BMW M3
E30 1, Toyota Supra RZ 2, Mazda RX-7 3, `--id-policy asi`). This
install.ps1 over it, without `-Force`: seed (3 IDs), the four game files
restored (SHA-256 of `out\vanilla`'s), `ported.lua`, `hud.lua` and
`config.lua` removed, then the packages installed as BMW 1, Supra 2, RX-7 3
(a fresh install would give the RX-7 2 and the Supra 3, key order), one GUI
generation, `packages=1` in the installed ini.

**In game** (`runsm2-20261102-165110`): the vanilla archive with that install's plugin
and ini (`-Asi out\scratch-game\plugins\dsfcars.asi`) and its car
manager files laid over the run (`-Overlay`: `dsfcars\` and
`packages.lua`, `packages_hud.lua`), `-ProbeIds '1,2,3'`. The plugin loaded the three packages from
`cars.txt` (IDs 1 to 3, root entries 453..461, SSLP and VSCT 450 + 9) and
served the generated gui.sp, Fonts.fchunk and global.fchunk; `register.lua`
took its cars from `packages.lua` alone (`id 2 (TOYOTA SUPRA RZ)`). Each
car spawned, chase and cockpit, the player driving it, no crash; ID 2 is
the Supra RZ with its Toyota badge and "SUPRA RZ" plate, so the migrated
IDs are the merged install's.

## Model ID in memory (confirmed-runtime, 2026-11-02)

The installed car.sp is the package's own file; dsfcars.asi writes the
listed ID (`cars.txt`) into the full VEHC as it serves the read, whatever
ID the file holds. Before, the manager wrote it into a copy of the file at
install (`install_sp`, removed).

**How a package entry is read (confirmed-static).** The plugin serves it
itself: `h_read_entry` (the replacement of 0x008FBE90) reads the whole
entry, `align_up(entry_span, sector size)` bytes from the entry's offset,
into the caller's buffer with one synchronous `ReadFile` on the plugin's
own handle, and returns ("Async I/O" above). It is the only way to an
entry's bytes: the reader's seek (0x008FB5E0) is called only from
0x008FB79D (name), 0x008FBEDA (read entry), 0x008FC1D9 and 0x008FC26B
(the async open, position 0), and its chain read (0x008FB260) only from
0x008FB273, 0x008FB7AD, 0x008FBEF8, 0x008FC1EC and 0x008FC27D, all inside
the reader's functions this module replaces (`dsff xref call 0x8fb5e0`,
`0x8fb260`; `xref imm` finds no table holding either address). A nested
reader over a package entry is logged and was never seen. The slot parses
the buffer only after read entry returns (SceneModelType2_Poll state 3 →
FUN_008FC400; 0x008588B7 is the call, the state is set to 2 right after it),
and FUN_00858A70 copies the NONR from that buffer into the slot's own. So
the patch goes at the end of the read, on the buffer.

**The fields.** A u32 each, `clone_vehicle.ID_FIELDS`: VSVH +0, VO3D +0,
SPID +0, UPVH +0x14, the four places a vanilla car's kind 0 entry holds
its ID (`clone_vehicle.make_clones` patches them for a clone and checks
the interior and exterior hold none). Roster survey of the 573 packages of
the main checkout's package build (`out/build-pkg/packages`): every full
VEHC holds each field once, at `NONR/VSRT/VSVH`, `NONR/VO3D`, `NONR/UPVH`
and `NONR/SPSR/SPID`, all four equal to `car.model_id`; no interior or
exterior holds one; no nested CHNK is compressed; the 573 builder IDs are
distinct (one build). A VEHC has 964 to 2118 entries, so the plugin finds
the fields by walking the buffer, not the file at start.

**The hook.** `pkg_set_ids(package, kind, buffer, entry size)` after
`pkg_read` in `h_read_entry`, kind 0 only: walks the VEHC's CHNK in the
buffer (a child whose payload starts with `CHNK` and is 16 bytes or more is
nested, as `dsfforza.formats.chnk` reads it; every table and child is
bounds-checked against the CHNK it is in; depth at most 16) and writes the
listed ID at each field. It logs the first read of each package's full
VEHC, with the fields' offsets in the entry, and every read whose fields
aren't VSVH, VO3D, SPID and UPVH once with one value.

**Existing installs** hold car.sp with the listed ID written in: the
fields already hold it, so the write changes nothing. `verify` accepts the
checksum the state recorded for them, as well as the package's.

**Checks.** `dsfcar` refuses a package whose full VEHC doesn't hold
`car.model_id` once in each field (the 573 pass). The offline selftest
(`st_set_ids`) writes into a VEHC laid out as the roster's, checks only the
four words change, that a file holding the listed ID is left as it is, two
packages with one builder ID, kinds 1 and 2 untouched, and bounds.

**Two packages with one builder ID** (packages from different builds) work
under their own listed IDs: nothing in the plugin keys on the file's ID.
Their UIDs are a separate matter ("UIDs" above).

**In game (confirmed-runtime).** Vanilla archive and GUI files, the
plugin with `packages=1`, six packages installed by the manager into a
fake game folder laid over the run (`-Overlay`), car.sp unchanged for five
of them:

| Listed ID | Car | ID in its car.sp | Note |
| --- | --- | --- | --- |
| 1 | Honda S2000 (2003) | 4 | another car's builder ID |
| 2 | BMW M3 E30 (1991) | 1 | |
| 3 | Toyota Supra RZ (1998) | 2 | |
| 4 | Lamborghini Countach (1988) | 470 | |
| 5 | VW Golf GTi 16v Mk2 (1992) | 1 | rewritten to the M3's builder ID: a package from another build |
| 6 | Mazda RX-7 (1997) | 6 | installed as today's installs are, the ID written into the file |

- `runs\vm1-20261102-164116` (probes 1 to 6, then the garage with
  `garagetest_on.lua` and `-MenuKeys`): every full VEHC read whole, in one
  read, and rewritten, e.g. `packages: 2: VEHC 0 read whole (1560576
  bytes); model ID 1 -> 2 in 4 fields (VSVH+0x124b60 VO3D+0x177080
  UPVH+0x1774a4 SPID+0x17b000)`, `5: ... model ID 1 -> 5`, `1: ... 4 ->
  1`, `4: ... 470 -> 4`, `6: ... 6 -> 6`. Each car spawns as its listed
  ID, the right car, chase and cockpit (the interior and exterior entries
  read from the package), and the HUD shows its badge and plate (`hud:
  model N`). In the garage `getVehicleStats(1..6)` gives each its own
  entry (Selectable, the package's picture); the MAZDA, TOYOTA, BMW and
  HONDA groups show the RX-7, Supra RZ, M3 and S2000 cards, OWNED, and the
  VOLKSWAGEN group holds the Golf GTi 16v Mk2 (the Countach's LAMBORGHINI
  group wasn't opened).
- `runs\vm1-20261102-170022` (probes 2, 5, 1, 3, 4, 6, 2, 5): 2 and 5 are
  released and requested again (`loaded before: true`): both spawn as the
  M3 and the Golf, chase and cockpit. The full VEHC stayed resident (no
  second read of it); the interior and exterior were read again.
- Reads 0.3 to 8.4 ms; no nested reader over a package entry, no field
  count other than four, no crash, in either run.

Not covered: a full VEHC read a second time (the game kept it resident in
both runs; the write is the same on every read, `st_set_ids`), and a slow
disk.

**The plugin's capabilities.** A plugin from before this change serves
car.sp's builder ID, so the VEHC would say one ID and the SSLP record
another. The manager reads what the installed plugin can do from the DLL
file, without running the game: `tools/asi/src/patch.h` defines the marker
`DSFCARS-CAPABILITIES: ` and the names (`archive.packages archive.ids`),
`main.cpp` keeps the string in the DLL by logging it at start, and
`car_manager.plugin_capabilities` reads it from `<game>\plugins\dsfcars.asi`
(no plugin: none; no marker: an older plugin). `install`, `enable` and
`reassign` refuse before writing anything unless the plugin has
`archive.packages` and `archive.ids` (what every car the manager lists
needs, as it installs car.sp unchanged) and everything the packages'
`requires` list; the message says to update dsfcars.asi with
`tools\install.ps1`. `disable`, `remove`, `uninstall` and `seed` list no
new car and aren't checked. install.ps1 checks the plugin it will install
(`-AsiPath`) with `car_manager check-plugin --plugin <file> <build>\packages`
among its other checks, before the game folder changes, and writes
`plugins\dsfcars.asi` before it hands the packages to the manager, so the
manager's own check then sees the new plugin. `tests/test_install_packages.py`
runs install.ps1 with a plugin file without the marker: refused, the game
folder unchanged. Before #485, on a scratch game folder with a three-car
build, install.ps1 with the main checkout's older plugin stopped at the
manager's refusal and wrote no `dsfcars\cars`; with this branch's plugin
it installed, and a `-Force` reinstall worked. The game's installed plugin
and the main checkout's build read as having no capability list.

**Reading car.sp straight out of the `.dsfcar`** (not done; would work as
far as the loader is concerned). The zip is stored, not deflated, so
car.sp is one contiguous byte range: at the local header's offset + 30 +
its name and extra lengths (the local header's own, which may differ from
the central directory's). CHNK offsets are relative to car.sp's start, so
`pkg_open` would add that base to every `pread`; the plugin's handle is
buffered, so an unaligned base costs nothing. It would need: the end of
central directory and the member's entry parsed at start, method 0 and no
encryption checked, no ZIP64 (one car is far below 4 GiB). The manager
would still unpack the images (car_gui reads them) and would check the
member's checksum in place of the file's.

## Asynchronous reads (confirmed-runtime, 2026-11-04, #489)

**Which thread read the packages.** The main thread, every time. The
main thread is the one that runs the game window's message pump,
FUN_0085DAD0: PeekMessageA, GetMessageA, TranslateMessage and
DispatchMessageA until the queue is empty. It is `thiscall` with two stack
arguments (`ret 8`) and has no direct caller. It is slot 9 of the vtable at
0x00E37218 (0x00E3723C holds 0x0085DAD0). Windows delivers a window's
messages only to the thread that created the window, so that thread is the
window's thread. With `[archive] read_log=1` the plugin points the slot at
`h_pump`, takes the thread ID from its first call, and logs the interval
between calls as the main loop's frame time. The pump is called once per
frame: 566 to 600 calls per 10 s at the 60 Hz the VMs run at, median
16.6 ms while the world is quiet. Each package read logs its thread, time
and size.

**Runs** (`run-series.ps1 -Vm`, vanilla archive, `-Asi` with `read_log=1`
and `read_uncached=1`, 24 packages from `out/build-pkg` laid over the run
by `-Overlay`, `-ProbeIds '1,6,13,22,1' -Traffic`). `read_uncached`
reads through a second handle opened with FILE_FLAG_NO_BUFFERING. A test
run copies the packages into its folder just before the game starts, so
they are in the guest's cache and a VM reboot would not make them cold.
The unbuffered handle goes to the disk on every read, as a first read
after a reboot would. The disk is the VM's virtual disk; the host may
still cache it.

| Run | Reads | Thread | Read time: median, p95, max | On the main thread |
| --- | --- | --- | --- | --- |
| `vm1-20261104-184809` (sync, `-TrafficIds`) | 17 | all main | 3.24, 4.30, 4.49 ms | 55.3 ms in all |
| `vm1-20261104-185259` (sync, `async_reads=0`) | 14 | all main | 3.60, 5.32, **80.3** ms | 128.7 ms in all |
| `vm2-20261104-185415` (async) | 16 | all one pool thread | 3.99, 7.26, 15.0 ms | 0 |

Reads are 1.5 to 2.2 MB, one per VEHC (full, interior, exterior), and come
one at a time: at most 2 within 100 ms, the three entries of a car 0.1 to
1 s apart. In the sync run, the 80.3 ms read (ID 22's exterior VEHC) fell in
a 94.5 ms frame, after a 26.4 ms frame: the game stopped for about five
frames. The VM's frame times are noisy (p95 30 to 45 ms while traffic
runs, from the VM itself; spikes of 50 to 1300 ms happen without any
package read, mostly during the world load). So most single reads of 3 to
5 ms don't stand out from that noise, but a slow one does. The read time
depends on the disk: the same 2 MB took 0.3 to 0.5 ms from the cache
(phase 1, run B) and 80 ms here once. A hard disk takes about 20 ms or
more by its transfer rate alone. The `-TrafficIds` run ended with the
traffic spooler's known INT3 (`in-game-tests.md`, "Don't put a probe ID in
`-TrafficIds`"): `register.lua` had already put ID 22, the probe's mission
car, into traffic, so the harness's request listed it. The reads above all
came before that.

**The change** (`[archive] async_reads`, on by default). The game reads its
own archive asynchronously, so the plugin now reads packages the same way.
A package read for the slot's load runs on a thread-pool thread
(`TrySubmitThreadpoolCallback`). The slot's load is the call at 0x008588B7
in FUN_00858870, recognised by its return address 0x008588BC. The slot sets
its state to 2 and calls the root reader's poll (`h_poll`, from
SceneModelType2_Poll at 0x008590CC) on each update until it answers 0, and
parses the buffer only then (state 3). `h_poll` state 0 answers busy while
the read runs. Once the read is done, it asks FUN_008FB2D0 as before.
FUN_008FB2D0 needs no hook of its own: its three calls are all inside the
vanilla poll, which `h_poll` replaces. Nothing outside the reader's own
functions touches the reader's file (`xref disp 0x5600`, `xref call
0x8fb2a0`, `0x8fb080`). The work item runs `pkg_read` and `pkg_set_ids`,
so the buffer holds the data and the listed ID before the poll says done.
At most one read is pending: every package entry goes through the vehicle
root reader into the shared spool buffer. A read entry or a close of that
reader, or a second read, first waits for the pending one, so a load the
game dropped can't write into a buffer it has moved on with. A read from
any other caller, and any read when no work item can be queued, stays
synchronous.

**Checked** in `vm2-20261104-185415`: all 16 reads on pool thread 2484, 0 ms
on the main thread (6660). Each load finished 8 to 38 ms after it started:
the poll answered busy 0 to 4 times, so the car arrives a frame or two
later. That is the wait the vanilla archive has too. Every probe car
spawned and was driven, chase and cockpit (`observe-03.png`, the One-77's
cockpit; `observe-06.png`, the Reventón). ID 1 was respawned after its
release (its full and exterior VEHC read again). Traffic loaded IDs 16 and
9 from packages while the player drove. The model ID was written as before
(`packages: 1: VEHC 0 read whole ... model ID 65 -> 1 in 4 fields`).
Offline (`build.ps1 -Test`): an async read is pending until its work item
is done, a second one for another reader is refused, a read entry or a
close waits for it, and the unbuffered read matches the buffered one.
**Garage** (`vm1-20261104-185829`, async, the garage test's Lua with IDs
1, 3, 6, 12 and 13 marked owned, garage 1 opened by menu keys): the game
picked ID 14 (Fiat 131 Abarth, a package) from the garage's car list and the
player drove it (`[dsfforza.garage] player drives model 14`; `menu-09.png`
the confirm screen with its picture, `menu-12.png` the car on the street with
its badge and name). Its three VEHC were read on a pool thread in 4.7 to
7.3 ms, each done 10 to 13 ms after it started.

Not covered: a Shift preload (it goes through the same request path,
FUN_005A7860, and the same slot load, so it takes the same async read), and
a real hard disk.

## Phased plan (PRs)

1. **Plugin loader** (the spike's code, proven for one car): build and offline test
   (`offline.cpp`: inject into a fake reader, SSLP/VSCT copies, clash and
   NONR refusals), the VM proof above, then several packages at once (IDs
   past entry 1100), async reads if the sync hitch shows, file name logging
   at FUN_00857370. Off by default.
2. **Package format**: the distributed zip (layout, manifest schema with a
   validator in `src/`), `research/` spec. `car_package make` grows from
   the proof's tool; the VEHC IDs are written in memory by the plugin.
3. **Manager CLI**: `install`, `enable`, `disable`, `remove`, `status`,
   `verify`; the stable key → ID map next to the game (seeded from today's
   `ported.lua` so existing saves keep their cars), IDs chosen against the
   installed archive's SSLP and other mods' `vehicleStats` (#149); writes
   `cars.txt`, `ported.lua`, `hud.lua`, and the generated gui.sp,
   Fonts.fchunk and global.fchunk; the plugin's file redirection for those.
4. **Forza pipeline emits packages**: port_batch writes one package per car
   (from `work/`) plus its picture, glyph images and metadata; `dsff install`
   = build packs, then hand them to the manager. Unlocks #162's 177 cars.
5. **Uninstall and save safety**: the vanilla archive and GUI files are
   never written, so uninstall = remove the plugin, packages and generated
   files. Removed IDs stay reserved (tombstones). Check in game that the
   garage and the save cope with flags for IDs that have no car.

## Open

- A runtime string source for `"ID:n"` (FUN_00835250/FUN_008352B0) and
  runtime glyph pages, which would remove the generated GUI files.
