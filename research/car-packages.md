# Car packages: cars from per-car files beside the vanilla dngvehicles.sp

Updated: 2026-11-02

Feasibility spike for the car mod manager (#170, #149, #162). The question is
whether `dsfforza.asi` can make `Driver.exe` 1.04 load cars from a file per
car, leaving `Install\dngvehicles.sp` as the vanilla game (or another mod)
has it. Evidence tags as in `dngvehicles.md`. Static outputs:
`out/ghidra/cp1`, `cp2` (ExploreAddresses) and `dsff xref`.

## Answer

Yes, and with little new code. A car request goes from model ID to SSLP
record to root entry index, and then to one `read entry(i)` on the vehicle
archive's root reader (confirmed-static, below). `dsfforza.asi`'s `[archive]`
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
  before it is, so the plugin reads synchronously (ReadFile with an offset,
  plugin-owned handle) before returning; the data is in the buffer when
  the poll runs. Cost: one 1 to 2 MiB read per VEHC load on the thread that
  polls the spooler (likely the main thread): a hitch of a few ms from an SSD
  or the cache. Phase 1 measures it. Should it matter, hook FUN_008FB2D0
  (5 bytes `8B 41 04 85 C0`): keep the redirected read pending per reader and
  report busy until it completes.
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
| `vehicleStats` entry | `UserLuaScripts\dsfforza\ported.lua`, read by `register.lua` | yes. These are loose Lua files, so the manager writes them from the enabled packages' metadata (no archive). `register.lua` should drop IDs the plugin left out (it already queries the plugin, `asi.md` "Telling Lua the plugin is there") |
| HUD badge and plate (`ManufacturerGlyph`/`ModelGlyph`, `hud.lua`) | glyphs merged into `media\Fonts.fchunk` (vehicles_font atlas) | not yet. A font's glyphs are records into page textures of one FNTD, and all pages are sized like page 0 (`gui-assets.md`). Runtime glyph pages would need new texture resources; recommended instead: a **generated** Fonts.fchunk in the manager's folder, opened in place of the vanilla one (below) |
| garage picture | SPOIs merged into `Install\gui.sp` (UIDL, SPOL, SSLP) | possible with the same root-entry injection (gui.sp also loads through FUN_005A11E0 and has an SSLP), plus UIDL and SPOL records. Simpler first: a generated gui.sp, opened in place of the vanilla one |
| maker and model names | `Locale\<lang>\global.fchunk` strings 700000+ and 900000+ (`"ID:n"`) | plausible at run time: the renderer turns `"ID:n"` into a string through FUN_00835250/FUN_008352B0 (`gui-assets.md`). A hook there could serve IDs 700000+ from the packages' names (hypothesis, needs RE). Until then: generated copies |
| garage groups | `[garage]` from `vehicleStats` (`ManufacturerName`, 120 groups) | yes, nothing per car |
| save flags | IDs below 304 in `autosave.dat`, 304..999 in `dsfforza-garage.dat`, keyed by ID | yes, if IDs are stable (the manager's key → ID map). A removed car's ID stays reserved (tombstone) so a new car doesn't inherit its owned/unlocked flags |

**Generated files instead of rewritten ones.** The manager writes merged
copies of gui.sp, Fonts.fchunk and global.fchunk from the vanilla files plus
the enabled packages, into its own folder. The plugin opens those in place of
the game's (`Install\gui.sp` → `dsfforza\generated\gui.sp`), so no game file
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

- **Package (installed form):** `<game>\dsfforza\cars\<key>\car.sp`, a CHNK of 4
  root entries: the car's three VEHC entries (port_batch's `work/<media>.chnk`)
  and `VSCT` (3 × 28 bytes). The model ID is patched into the full VEHC
  (VSVH, VO3D, SPID, UPVH; `clone_vehicle.ID_FIELDS`) by the manager at
  install; the plugin writes it into the SSLP and VSCT records. (Patching
  the VEHC in memory after the read would make the installed file ID-free
  too; possible, not needed while IDs are stable.)
- **Distributed form (#170):** a `.dsfcar` zip per car with the 4-entry
  `.sp` (any ID), the garage picture, badge and plate images, metadata
  (names, year, stats, maker group) and requirements ("Package format 1"
  below). The manager unpacks, patches the ID, and writes the list.
- **List:** `<game>\dsfforza\cars\cars.txt`, one `id path` per enabled car,
  written by the manager. Disable = drop the line (the file stays); remove
  = drop the line and the file (the ID stays reserved in the map).
- **Plugin (`[archive] packages=1`):** at start, open every listed package,
  check it (CHNK v3, uncompressed, 3 VEHC + VSCT, NONR sizes) and
  log it. Hooks:

| Site | Original bytes | New | Purpose |
| --- | --- | --- | --- |
| 0x005A13C4 (in FUN_005A11E0) | `E8 67 B1 35 00` (call 0x008FC530) | `call h_spool_open` | calls the original; remembers the reader whose file name ends in `dngvehicles.sp` |
| `h_poll` state 3 (0x008FC0A0, already replaced) | – | `pkg_inject` | appends 3 entries per package to that reader's root table (side table past 1100) |
| `h_read_entry` (0x008FBE90) and `h_name` (0x008FB770), already replaced | – | branch | for package entries, a synchronous read from the package file |
| `h_seek` (0x008FB5E0), already replaced | – | log | a nested reader over a package entry (not supported) |
| 0x005A0B50 | `8B 44 24 08 56` | `jmp h_sslp_load` | an SSLP copy with 3 records per package (clash and NONR checks) |
| 0x0086C630 | `8B 44 24 08 56` | `jmp h_vsct_load` | a VSCT copy with the packages' records; sets `[Engine+0x780]+0x2C` as the original does |
| 0x00857370 | `A1 C4 D1 43 01` | `jmp h_file_open` (trampoline back to 0x00857375) | `[archive] open_log` logs every name (phase 1); phase 3 adds the redirection for generated gui.sp / Fonts.fchunk / global.fchunk |

All sites are checked byte for byte before writing, and are all-or-nothing,
as for the other modules. Without the reader patches there are no packages.

## Proof (confirmed-runtime, 2026-11-01)

```sh
uv run dsff car_package make --chnk <main>/out/build/work/BMW_M3E30_91.chnk --id 700 --host 205 \
    --out out/cp/game/dsfforza/cars/BMW_M3E30_91.sp --list out/cp/game/dsfforza/cars/cars.txt
# 0: VEHC 0x1000 size 0x17D000 (ID 1 -> 700); 1: VEHC 0x17F000; 2: VEHC 0x317000; 3: VSCT 0x54
```

```powershell
$env:PATH = "C:\Program Files (x86)\Microsoft Visual Studio\Installer;$env:PATH"
.\tools\asi\build.ps1 -Test
# a copy of out\asi\dsfforza.asi + dsfforza.ini with [archive] packages=1 in out\cp\asi\
.\tools\wsb\run-series.ps1 -Vm -Asi out\cp\asi\dsfforza.asi -ArchiveOnly `
    -Archives out\vanilla\Install\dngvehicles.sp,out\vanilla\Install\dngvehicles.sp `
    -Overlay '',out\cp\game -Labels without-package,with-package -ProbeIds '266,700' `
    -LuaFiles 'dsfforza\ported.lua=out\cp\lua\ported.lua' -WaitSeconds 240
```

`out\cp\lua\ported.lua` is the main build's `dngvehicles.lua` cut to one
entry, `[700]` (host 205), so the car has a `vehicleStats` entry.

- **Without the package** (`runs\vm1-20261101-120510`): the log says `no list
  ...; no car packages` and no package site is hooked. 266 spawns; the 700
  request finds no SSLP record and the player stays in Tanner's car (62).
- **With it** (`runs\vm1-20261101-120730`), `dsfforza.log`:

  ```
  packages: 700 dsfforza\cars\BMW_M3E30_91.sp: VEHC 1560576/1667072/2097152 bytes, NONR 0x59060/0x360/0x410
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
100 MB/s is 20 ms, more than a frame). Not made async here: the runs
showed no hitch, and the hook it needs (FUN_008FB2D0, `8B 41 04 85 C0`)
waits for a measurement on a real disk.

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
| `car.sp` | the installed form: CHNK v3, not compressed, entries VEHC, VEHC, VEHC, VSCT (84 bytes, three records, all for `car.host`); each VEHC's entries inside it; NONR at most 0x6D000 / 0x1000 / 0x1000 | dsfforza.asi `pkg_open` and `h_sslp_load`; the slot buffers are the vanilla SSLP header's (+0x08 + 4 × kind) |
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
  "requires": ["archive.packages"],
  "source": {"pipeline": "dsfforza", "game": "FH1", "media": "BMW_M3E30_91"}
}
```

- `key` is `<source>.<game>.<name>` and stays the same across versions; the
  manager keys its ID map and save flags on it. `version` is the
  package's (a higher one replaces a lower one in place, keeping the ID).
- `car.model_id` is the ID car.sp holds, nothing more: the manager writes
  its own into the full VEHC at install (`car_package.install_sp`, the same
  `clone_vehicle.patch_ids` as the merge). Base IDs are 1..999 (the LOD
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
- `requires`: capabilities of dsfforza.asi, today only `archive.packages`.
- Images are what the build drew (the Forza logo or a text badge, the
  chrome plate, the rendered picture), not glyph codes. Without one the
  manager uses the game's own badge or plate for that maker or model, or
  the host's picture.

**Checked** (`dsff car_package validate`, also on read): the members, the
manifest's fields and types, car.sp against the loader's checks and the
manifest (SHA-256, the ID in VO3D, the VSCT host), the images' sizes and
formats.

**Verified:** the four cars of the loader runs, packed from the main build
(5.2 to 6.2 MB each, about 3.6 s for four) and installed as 700 to 703 with
`install_sp`, are byte for byte the `.sp` files that ran in
`runs\vm1-20261101-122452`. Run from the packages, with `ported.lua`
entries written from their manifests (`runs\vm2-20261101-124716`): all
four spawn as 700 to 703, in the chase and cockpit views.

**Open (phase 3):** shared assets (engine sound templates, the Forza emblem
texture) stay with the manager, not in a package; a pack (several cars)
is a folder of `.dsfcar` files for now, without a manifest of its own.

## Manager, part 1 (phase 3, #453)

`src/dsfforza/build/car_manager.py`, `dsff car_manager install | enable |
disable | remove | status | verify [--game G] [--archive A]`. It refuses
to change anything while Driver.exe runs from that folder, and writes only
under the game folder's `dsfforza\cars\` and
`Common\UserLuaScripts\dsfforza\packages.lua`:

| File | What |
| --- | --- |
| `dsfforza\cars\manager.json` | the state: `ids` (key → model ID, for good), `cars` (installed: ID, version, enabled, SHA-256 of the installed `.sp`, manifest); until "Packages only" also `merged` (a merged install's cars), dropped on load since |
| `dsfforza\cars\cars.txt` | the plugin's list: `id path` per enabled car |
| `dsfforza\cars\<key>\` | one car: `car.sp`, the installed form with the car's ID written in (`car_package.install_sp`), and the package's `car.json` and images (for part 2's GUI files) |
| `Common\UserLuaScripts\dsfforza\packages.lua` | `ported.lua`'s fields per enabled car; `register.lua` adds them to its cars for the IDs `ported.lua` doesn't have |

**IDs.** A key keeps its ID across upgrades (a higher version replaces the
package in place; a lower one needs `--force`), disable and enable, and
after a remove: the ID stays reserved, so no new car inherits the old one's
save flags (the game and dsfforza.asi keep them per model ID). A new key
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
missing or changed, a car whose ID the installed archive now uses (another
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
under `dsfforza\generated\`, plus `redirect.txt` and
`Common\UserLuaScripts\dsfforza\packages_hud.lua` (`hud.lua`'s fields per
car; `register.lua` adds them for the IDs `hud.lua` doesn't have). What a
car gets is what `gui_assets.build --wide-glyphs` gives a ported car, from
the package's images: a maker or model DSF has keeps its vanilla badge or
plate (`vanilla_codes`), others get a 16-bit badge (one per maker group)
or plate (one per text) from `badge.png` / `plate.png` and a name string;
the picture is `dsfforza_<media>`; `GarageEmblem` only when the game's
Fonts.fchunk already has the emblem texture (a merged install's: the
manager has no disc to make it). Codes and strings are taken where the
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
game's files are never written.

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
Forza emblem: the vanilla Fonts.fchunk has no emblem texture (see above).

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
  leaves `dsfforza\cars`, `dsfforza\generated` and the two Lua files.

## Uninstall, migration and save safety (phase 5, #455; the switches as they were then)

**Uninstall.** `car_manager uninstall` removes every file the manager
wrote: the cars' `.sp` files and folders, `cars.txt`,
`dsfforza\generated\`, `packages.lua` and `packages_hud.lua`. It keeps
`dsfforza\cars\manager.json` with the key → ID map (no cars in it), because
the save keeps its flags per model ID (`autosave.dat` below 304,
`dsfforza-garage.dat` above): a later install gives every car the ID its
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
stay with the old ID, which isn't ours any more).

**A save that names cars that aren't there** (confirmed-runtime). Every
package run used the VM's save, which carries flags for the 573-car
roster: `garage: loaded 420 added-car entries` from `dsfforza-garage.dat`,
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
  required (`-AsiPath`, default `out\asi\dsfforza.asi`, else a release's
  `plugin\dsfforza.asi`) and always gets `[archive] packages=1`. A build made
  here is `port_batch build --gui --wide-glyphs --id-policy asi
  --root-limit 3003 --packages`, as `dsff install`'s; it still needs the
  vanilla archive, which it reads. No game file is replaced, so another
  mod's archive and GUI files are no reason to stop an install;
  `dsfforza-backup\` holds only `install.json` and, with `-Force`, another
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
and ini (`-Asi out\scratch-game\plugins\dsfforza.asi`) and its car
manager files laid over the run (`-Overlay`: `dsfforza\` and
`packages.lua`, `packages_hud.lua`), `-ProbeIds '1,2,3'`. The plugin loaded the three packages from
`cars.txt` (IDs 1 to 3, root entries 453..461, SSLP and VSCT 450 + 9) and
served the generated gui.sp, Fonts.fchunk and global.fchunk; `register.lua`
took its cars from `packages.lua` alone (`id 2 (TOYOTA SUPRA RZ)`). Each
car spawned, chase and cockpit, the player driving it, no crash; ID 2 is
the Supra RZ with its Toyota badge and "SUPRA RZ" plate, so the migrated
IDs are the merged install's.

## Phased plan (PRs)

1. **Plugin loader** (the spike's code, proven for one car): build and offline test
   (`offline.cpp`: inject into a fake reader, SSLP/VSCT copies, clash and
   NONR refusals), the VM proof above, then several packages at once (IDs
   past entry 1100), async reads if the sync hitch shows, file name logging
   at FUN_00857370. Off by default.
2. **Package format**: the distributed zip (layout, manifest schema with a
   validator in `src/`), `research/` spec. `car_package make` grows from
   the proof's tool; the VEHC IDs are patched at install.
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

- The sync read on a slow disk (a first read from a hard disk); async
  through FUN_008FB2D0 if it shows.
- A runtime string source for `"ID:n"` (FUN_00835250/FUN_008352B0) and
  runtime glyph pages, which would remove the generated GUI files.
