# Car packages: cars from per-car files beside the vanilla dngvehicles.sp

Updated: 2026-11-01

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

Status: the plugin code is drafted (`[archive] packages`, off by default)
and a one-car package is made (`dsff car_package`). The plugin draft has not
been compiled or run: the coordinator held builds and VM runs while the
user plays. **Nothing here is confirmed-runtime yet**; the proof run is
"Proof" below.

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
it whole. The draft logs it if one ever does.

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
  before it is, so the draft reads synchronously (ReadFile with an offset,
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
| SSLP records | merged into SSIC | **yes**: hook FUN_005A0B50 (draft) |
| VSCT records | merged into SSIC (host's three records) | **yes**: hook FUN_0086C630 (draft); the package carries them as a 4th entry |
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

- **Package (installed form):** `<game>\dsfforza\cars\<key>.sp`, a CHNK of 4
  root entries: the car's three VEHC entries (port_batch's `work/<media>.chnk`)
  and `VSCT` (3 × 28 bytes). The model ID is patched into the full VEHC
  (VSVH, VO3D, SPID, UPVH; `clone_vehicle.ID_FIELDS`) by the manager at
  install; the plugin writes it into the SSLP and VSCT records. (Patching
  the VEHC in memory after the read would make the installed file ID-free
  too; possible, not needed while IDs are stable.)
- **Distributed form (#170):** a zip per car with the 4-entry `.sp`
  (no ID, or any ID), the garage picture, badge and plate images, metadata
  (names, year, stats, maker group hint, handling summary), requirements
  (host ID, plugin version). The manager unpacks, patches the ID, and writes
  the list.
- **List:** `<game>\dsfforza\cars\cars.txt`, one `id path` per enabled car,
  written by the manager. Disable = drop the line (the file stays); remove
  = drop the line and the file (the ID stays reserved in the map).
- **Plugin (`[archive] packages=1`):** at start, open every listed package,
  check it (CHNK v3, uncompressed, 3 VEHC + optional VSCT, NONR sizes) and
  log it. Hooks:

| Site | Original bytes | New | Purpose |
| --- | --- | --- | --- |
| 0x005A13C4 (in FUN_005A11E0) | `E8 67 B1 35 00` (call 0x008FC530) | `call h_spool_open` | calls the original; remembers the reader whose file name ends in `dngvehicles.sp` |
| `h_poll` state 3 (0x008FC0A0, already replaced) | – | `pkg_inject` | appends 3 entries per package to that reader's root table (side table past 1100) |
| `h_read_entry` (0x008FBE90) and `h_name` (0x008FB770), already replaced | – | branch | for package entries, a synchronous read from the package file |
| `h_seek` (0x008FB5E0), already replaced | – | log | a nested reader over a package entry (not supported) |
| 0x005A0B50 | `8B 44 24 08 56` | `jmp h_sslp_load` | an SSLP copy with 3 records per package (clash and NONR checks) |
| 0x0086C630 | `8B 44 24 08 56` | `jmp h_vsct_load` | a VSCT copy with the packages' records; sets `[Engine+0x780]+0x2C` as the original does |
| later: 0x00857370 | `A1 C4 D1 43 01` | `jmp` | file name redirection for generated gui.sp / Fonts.fchunk / global.fchunk |

All sites are checked byte for byte before writing, and are all-or-nothing,
as for the other modules. Without the reader patches there are no packages.

## Proof (held)

Made, not run:

```sh
uv run dsff car_package make --chnk <main>/out/build/work/BMW_M3E30_91.chnk --id 700 --host 205 \
    --out out/cp/game/dsfforza/cars/BMW_M3E30_91.sp --list out/cp/game/dsfforza/cars/cars.txt
# 0: VEHC 0x1000 size 0x17D000 (ID 1 -> 700); 1: VEHC 0x17F000; 2: VEHC 0x317000; 3: VSCT 0x54
```

The run, once builds and VMs are free again:

```powershell
$env:PATH = "C:\Program Files (x86)\Microsoft Visual Studio\Installer;$env:PATH"
.\tools\asi\build.ps1 -Test
# a copy of out\asi\dsfforza.asi + dsfforza.ini with [archive] packages=1 in out\cp\asi\
.\tools\wsb\run-series.ps1 -Vm -Asi out\cp\asi\dsfforza.asi -ArchiveOnly `
    -Archives out\vanilla\Install\dngvehicles.sp,out\vanilla\Install\dngvehicles.sp `
    -Overlay '',out\cp\game -Labels without-package,with-package -ProbeIds '266,700'
```

Expected: without the overlay, 700 is refused (not in SSLP) and 266 spawns.
With it, the log shows the vehicle archive's reader, `root entries
453..455`, `SSLP ... + 3`, `VSCT ... + 3` and `700: entry 453 ... read from
its package`, and the probe spawns the M3 E30 as 700. The run also tests the
cockpit view (the 1700 interior), the chase view (2700) and a respawn after
release. The HUD shows no name without a `vehicleStats` entry
(`-LuaFiles 'dsfforza\ported.lua=...'` with an entry for 700 fixes that).

## Phased plan (PRs)

1. **Plugin loader** (this draft, made real): build and offline test
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

- Everything in "Proof": the draft has not been compiled or run.
- The sync read's cost on the main thread (phase 1).
- Whether every open of gui.sp, Fonts.fchunk and global.fchunk goes through
  FUN_00857370 (phase 1 logs it).
- A runtime string source for `"ID:n"` (FUN_00835250/FUN_008352B0) and
  runtime glyph pages, which would remove the generated GUI files.
- The garage and the owned list with an ID that has no car (after a remove).
