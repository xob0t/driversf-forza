# Garage pictures, HUD badge and model name

Updated: 2026-10-24

How DSF shows a car's picture, maker badge and model name, and how ported
cars get their own. Code: `src/dsfforza/game/gui_assets.py`. Evidence tags as elsewhere;
Ghidra output in `out/ghidra/hudnames*`.

## What reads vehicleStats (confirmed-static)

`Driver.exe` calls the Lua global `getVehicleStats(id)` and reads fields with
the Lua C API:

| Function | Reads | Use |
| --- | --- | --- |
| FUN_006c57d0 | `Selectable`, `Manufacturer` (int), `ManufacturerName` | garage maker list: **loops model IDs 0 to 0x12F (303) only**, groups cars by the first 15 characters of `ManufacturerName` upper-cased, and keeps `Manufacturer` as a one-character string (`"%c"`) for the badge |
| FUN_006c4f80 | `Selectable`, `Picture1` | garage picture: hashes the name (FUN_00baa250) and looks it up in gui.sp |
| FUN_006f07d0, FUN_006f3bc0 | all fields incl. `Picture2/3`, `Speed`... | online lobby and vehicle packs |
| FUN_007daef0 | `Picture1` | an online URL parameter |

`Picture2`/`Picture3` (`*_icon_large`, `*_icon_small`) are only read by the
online lobby (`onlineGetVehicleLargeImage/SmallImage`); their images are not
in gui.sp. **A car whose ID is 304 or higher never appears in the stock
garage list**, whatever `vehicleStats` says (the loop bound is in the code;
dsfforza.asi `[garage]` lists IDs up to 999, `asi.md`). Free IDs below 304:
0 to 61 (stock base IDs run 62 to 303) and the 92 IDs in 62..303 the stock
archive doesn't use (123, 131, 277 and 289 are in the archive but have no
`vehicleStats` entry; `port_batch.py`, `ASI_LOW_IDS`).

## Garage pictures: Install/gui.sp (confirmed-static)

```text
root   921 x SPOI 'Spoolable Item'  MDXN/DRSP {TEXC {UIDC u64, DXTC DDS}, MDID u64}
       SSIC, BINF ("SpoolablePackagerDLL v2.15.0.0", 17/08/11)
SSIC/NONR
  UIDL  u32 count (921), count x {u32 name hash, u64 texture UID}
  SPOL  u32 count, count x u64 texture UID, in SPOI order (= each SPOI's UIDC)
  SSLP  0x1C header (+0x02 u16 count; +0x18 slots 10, 16, 2, 0),
        count x {u16 spool id, u16 root index, u16 kind 0}
```

The name hash is the script-package CRC variant (`lua_scripts.CRC_TABLE`,
init 0xFFFFFFFF, no final xor) of the lower-cased name, without a prefix:
`nissan_370z` = 0x320E7FF2 = UIDL entry 694 = SPOI 495. 142 of the 144
`Picture1` names are in gui.sp (`chevrolet_camaro_Z28` and
`dodge_monaco_cop` are not). Car pictures are 512 x 256 DXT5, no mips (DDS
mip count 0, flags 0x81007): an opaque backdrop panel (x 0..470, y 0..197:
a darkened Golden Gate photo fading to grey fog) with the maker's logo and
the model name on the left, the car in a front three-quarter view on the
right (front to the right, its right side to the camera), overhanging the
panel at the bottom right with a soft shadow; transparent elsewhere.

### Adding pictures (offline-checked)

`gui_assets.add_pictures` writes a copy of gui.sp without moving a stock
byte: new SPOIs (texture UID shaped like stock ones, byte 1 = 0x12,
byte 7 = 0x2D) and a rebuilt SSIC (UIDL, SPOL, SSLP with the new names and
the count) are appended at the end, and the root entry table (room for 100
more rows before the first payload at 0x4000) points at them; the old SSIC
bytes stay unused. A full `chnk.py rebuild` of gui.sp is not byte-identical
(`dngvehicles.md`), so the file is patched instead. Reading the copy back
finds both the stock and the new pictures by name.

The picture: Forza Horizon's own car thumbnail where there is one
(`media/ui/Textures.zip` `Thumbnails/Thumbnail_<Data_Car.Id>.xds`, 768 x 288,
32-bit `k_8_8_8_8` tiled, 153 FH1 cars; the right third is a top view),
cropped and mirrored to face right, else a software render of the converted
car's exterior entry (`render_car`: perspective, z-buffer, the palette's most
colourful paint, textured lamps and badges, wheels from the UPVH hubs). The
backdrop is recovered from the stock pictures (per pixel the most common
colour across all 142; the right part, which cars cover in most pictures, is
the photo's fog gradient), the logo is FH1's
`UI4/Logos/Manufacturer_Logos_LG/L_MAN_<Make>_Logo.xds` (format 50, the
32-bit `_AS_16_16_16_16` alias), the name a generated chrome nameplate.

## Badge and model name: media/Fonts.fchunk "vehicles_font" (confirmed-static)

The HUD and the garage print `Manufacturer` and `Model` as single characters
in the font `vehicles_font` (Fonts.fchunk PCKG 2: `FNTD` plus a 1024 x 500
DXT5 atlas of 186 glyphs, each a maker badge or a model nameplate in chrome
lettering). So the numbers in `vehicleStats` are character codes, not string
IDs, which is why `ManufacturerName`/`ModelName` strings never reached the
HUD (`in-game-tests.md`, fix6).

```text
FNTD  +0x00 u32 1, u32 glyph count (186), u32 0, u32 ?,
      +0x10 u16 1, 2, 99, 10, 56, 1024, 512, 0
      +0x20 u64 texture UID, u32 width 1024, u32 height 500
      +0x30 count x {u16 code, x, y, w, h, x offset, y offset, 99, 99, 0}, sorted by code
```

Glyphs sit in a 99 x 99 cell, bottom-aligned (y offset + h = 99) and
centred. Codes 33 to 255 exist except 60, 127 to 159, 212, 213 and 245.
`Manufacturer` uses 33 to 66 and 253 to 255; `Model` 67 to 251. Glyphs 74 to
78, 88, 160, 244 and 252 exist but no vehicle uses them. The strings behind
`ManufacturerName`/`ModelName` ("ID:242768" = "NISSAN") are in
`Locale/<language>/global.fchunk` (SLRR: SLRH, SLRC {u32 byte offset, u32 id}
ascending, SLRS UTF-16); the garage uses them for grouping and labels, the
HUD does not.

### Adding glyphs (offline-checked)

`gui_assets.add_glyphs` writes a copy of Fonts.fchunk with the new glyphs in
rows under the stock atlas (texture 1024 x 500 → 1024 x 564 for three
glyphs; the stock DXT blocks are kept, only the new rows are encoded; FNTD
count and height updated, records inserted in code order) and the vehicles
font package appended at the end of the file with its root row retargeted
(Fonts.fchunk does not round-trip through `chnk.py` either). New codes come
from 212, 213, 245 (free, same in Latin-1 and cp1252), then the unused
glyphs (252, 244, 88, 78 to 74), then 128 to 159 (they draw: the RX-7's
156 in out/testdrive3; the text path draws bytes, no code page). Not 160:
in out/testdrive3 both M5s had plate 160 ("M5", glyph present, 58 x 34) and
the HUD drew no plate (user screenshot), 0xA0 being the no-break space;
stock never uses it either (confirmed-runtime, removed from the pool).

## The build

```sh
uv run dsff gui_assets build out/ported.json --out out/gui \
    --name "NIS_SkylineGTR_93=SKYLINE GT-R"
```

For each car of a `port_car.py` manifest: a picture named
`dsfforza_<media>`, the stock badge when DSF has the maker (matched on the
upper-cased maker string), else a new badge glyph from the Forza logo; the
stock nameplate when DSF has the same model (370Z → 219), else a new
nameplate glyph. Writes `out/gui/gui.sp`, `out/gui/Fonts.fchunk`,
`out/gui/hud.lua` (per ID: `Picture1`, `Manufacturer`, `Model`,
`ManufacturerName`, `ModelName`), previews and `gui-assets.json`.
`register.lua` loads `dsfforza\hud.lua` and applies it over the copied
entry. Every entry also gets `GarageEmblem = 1` and Fonts.fchunk the
Forza emblem texture (garage card emblem, below; needs dsfforza.asi). Model names come from the media name (`SkylineGTR` → `SKYLINE GTR`)
unless `--name` gives one; Forza's own name strings are LSB2 IDs (not
decoded).

Test set (`out/gui/build2`, from `out/lights/build2/ported-lp.sp`):

| ID | Car | Picture | Manufacturer | Model |
| ---: | --- | --- | --- | --- |
| 304 | 370Z | FH1 thumbnail | 54 (stock Nissan) | 219 (stock 370Z) |
| 305 | M5 | FH1 thumbnail | 212 (new BMW badge) | 213 (new "M5") |
| 306 | Skyline GT-R R32 (FM4) | render | 54 | 245 (new "SKYLINE GT-R") |

## Scaling to a few hundred cars

### Pictures: gui.sp has room for 177, then shared SPOIs

The game's CHNK reader holds 1100 entries (`spool-limits.md`), and gui.sp
has 923, so 177 more pictures can each get a SPOI. `append_patch` now grows
the root table when its 100 free rows run out, by moving every root payload
down a multiple of 0x1000 (stock bytes and nested chunks unchanged; checked
with 150 pictures: the stock and the new pictures read back). Past 177,
`add_pictures` packs several pictures into one SPOI (a TEXC each in its
DRSP, MDID listing all), each with its own SPOL UID and SSLP spool ID
pointing at the shared root entry (checked offline with 260; whether the
spooler loads a shared item for each spool ID is untested).

With dsfforza.asi [archive] (another module: the CHNK reader keeps entries
past 1100 in a side table, `spool-limits.md`) the root can hold one SPOI per
picture: `gui_assets.py build --root-limit 16384` (default 1100, the stock
reader). SSLP spool ids and root indices are u16, so 800 more pictures fit.
Not checked in the garage yet (the stock garage lists IDs below 304 only;
dsfforza.asi `[garage]` up to 999).

### Glyph codes without the plugin: one byte, about 70 new glyphs per install

`Manufacturer` and `Model` reach the HUD as one character each, so the code
space is 0..255 whatever the font texture holds (FNTD codes are u16, but
the garage and the HUD record (FUN_006C40C0, next section) format
`(char)Manufacturer` with "%c" into 2-byte buffers). Free: 3 safe codes, 9
unused stock glyphs, 128..159 (32, in use by the 31-car batch, still
untested) and, behind `--control-codes`, 1..31 without tab, line feed and
return (28, untested: the text path may act on them). A second font page or
multi-glyph plates don't help while the value is one character.

`plan_glyphs` shares and prioritises instead:

1. Stock badges where DSF has the maker (`dsf_make`: Forza's maker name,
   upper-cased; SRT and Viper to DODGE, GMC Truck to GMC, Range Rover models
   to RANGE ROVER, SLR McLaren to MCLAREN). DSF has no Mercedes-Benz,
   Chrysler, Plymouth or BMW badge: each gets one new badge shared by all its
   cars.
2. Stock plates where DSF has the model or its family under the same maker.
3. New plates one per name, so cars with the same name share one.
4. Over budget: names fall back to Forza's family names (List_FamilyModel:
   MUSTANG, CORVETTE, 911...) where several cars share one; then badges and
   plates are chosen by the number of cars they cover, and two codes become
   a blank badge and a blank plate for the rest (no host name on the HUD).

Simulated on the 453 cars of `out/batch/plan-all.tsv` (first N in list
order; badges/plates are cars covered):

| Cars | Codes | Stock badge | New badge | Blank badge | Stock plate | New plate glyphs | Blank plate |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 31 (first batch) | 44 | 12 | 14 makers | 0 | 1 | 30 | 0 |
| 100 | 44 / 72 | 53 | 10 | 0 | 13 | 32 / 60 | 34 / 6 |
| 200 | 44 / 72 | 93 | 18 | 0 | 17 | 24 / 52 | 98 / 70 |
| 453 | 44 / 72 | 178 | 19 / 30 | 49 / 21 | 21 | 23 / 40 | 293 / 252 |

(44 = without, 72 = with `--control-codes`.) Every car keeps its own
garage picture and `ModelName` text; only the HUD plate is shared or blank.
Without the plugin this stays the plan; with it, see the next section.

### Unlimited badges and plates: dsfforza.asi [hud] (confirmed-static; in game: below)

**How the HUD gets a badge (confirmed-static).** Every screen that shows
a car's badge and plate goes through vtable slot 0x134 of the vehicle-data
manager (vtable 0x00E189F0, slot function FUN_006C63D0, args `(&id,
record, flag)`): it calls `getVehicleStats(id)`, reads the fields with the
game's Lua helpers (FUN_005E8210 = int field, FUN_005E85E0 = string
field) and calls FUN_006C40C0 (only caller: 0x006C6855), which fills a
0x100-byte record: `+0x90 char[2] = "%c" Manufacturer`, `+0x92 char[2] =
"%c" Model`, `+0x94 model id`, names, power and torque strings, bars. The
callers then hand `record+0x90` and `record+0x92` to the narrow SetText
(vtable +0xBC) of two text elements drawn in vehicles_font:

| Screen | Elements | SetText calls (badge, plate) |
| --- | --- | --- |
| driving HUD (FUN at 0x006D4E..., elements `vehicle_brand`/`vehicle_model` bound at 0x006D678C) | `[esi+0x47E0]`, `[esi+0x47E4]` | 0x006D4EA0, 0x006D4EB8 |
| Shift HUD (`iShift_Stats_*`) | `[esi+edi*4+0x7EC/0x7F4]` | 0x006CFBDD, 0x006CFBF6 |
| a second HUD panel | `[esi+edi*4+0x48B8/0x48C0]` | 0x006DBADD, 0x006DBAF6 |
| garage shop (`GarageShop_VehicleName`/`ModelName`) | edi, ebx | 0x0070E361, 0x0070E373 |
| vehicle panel (`vehicle_brand`/`vehicle_model`, `vehicleStats_Top`...) | ebp, `[esp+0x34]` | 0x00711687, 0x0071169F |

The 2-byte buffers are why a code is one byte. The garage maker list
(FUN_006C57D0, IDs below 304) and the online lobby read `Manufacturer`
themselves and are not covered.

**What the font and renderer allow (confirmed-static).** FNTD (loader
FUN_00729550) starts with a page count: `+0x20` is a table of pages
`{u64 texture UID, u32 width, u32 height}`, and glyph record `+0x12` is
the page index (read at 0x0072541A, page texture from FUN_00729530). Every
page is sized from page 0's entry (the loader reads `FNTD+0x28/+0x2C` for
all pages), so all pages must match page 0. The glyph lookup FUN_00729440
is a binary search on the u16 code; `Agency FB 30pt` has 819 glyphs. The
loaded font object (font manager at engine `[0x00FE7228]+0x724`, up to ten
font pointers at `+0x08`, count `+0x30`) is `+0x00` id (FNTD `+0x0C`,
vehicles_font 0x6FFB8415), `+0x04` glyph count, `+0x20` pointer to the
records in the loaded FNTD.

The HUD text element (vtable 0x00E84B3C) can't carry a 16-bit code
(confirmed-runtime): its text is a narrow `char[capacity]` (`+0x34` buffer,
`+0x30` capacity; SetText +0xBC = 0x00ADC1D0 copies bytes, +0xB8 =
0x00ADC230 copies UTF-16 units into the same buffer, which is then drawn
as bytes). Passing `L"\x0126"` drew glyph 0x26 (Cadillac) plus the default
glyph for byte 0x01 (code 95, '_' = "CONTINENTAL SUPERSPORTS"), run
`20261022-220230`. The renderer does turn a narrow `"ID:n"` into the
localised UTF-16 string n at draw time (0x007215E0 → FUN_00835250 /
FUN_008352B0), and that drew a 16-bit badge ("B39", page 1) in run
`20261022-224711`, but the plate element's buffer is too short for
`"ID:9xxxxx"`: it kept `"I"` and drew glyph 73 (AMC "Pacer").

**The patch (`tools/asi/src/mod_hud.cpp`).** vehicleStats gets two optional
fields, `ManufacturerGlyph` and `ModelGlyph` (codes 0x100 up). The text
stays one byte, as stock: each text element that shows a car with a code
gets a slot code of its own (1..8, 11, 12, 14, 15: control characters
without tab, line feed, return; gui_assets writes empty records for them),
the plugin copies the 16-bit glyph's record (x, y, w, h, offsets, advance,
page) over the slot's record in the loaded vehicles_font, and sets the
element's text to that byte (after an empty text when the slot's glyph
changed, so the element refreshes). Twelve slots cover the ten call sites;
when elements are re-created and all are taken, the least recently set
slot is reused. Cars without the fields (all stock cars) take the stock
path unchanged.

| VA | Original bytes | New | Why |
| --- | --- | --- | --- |
| 0x006C6855 | `E8 66 D8 FF FF` (call FUN_006C40C0) | `call ctor_stub` | stub: `pushad`, read `ManufacturerGlyph`/`ModelGlyph` with FUN_005E8210(L, key, &v, -2) while the entry is on the Lua stack, store by model id (first stack arg), `popad`, `jmp FUN_006C40C0` |
| 0x006D4EA0 | `8B 92 BC 00 00 00 8D 84 24 98 00 00 00 50 FF D2` | `8D 84 24 98 00 00 00 50 E8 <hud_badge> 90 90 90` | driving HUD badge |
| 0x006D4EB8 | `8B 80 BC 00 00 00 8D 94 24 9A 00 00 00 52 FF D0` | `lea eax,[esp+0x9A]; push eax; call hud_plate; nop x3` | driving HUD plate |
| 0x006CFBDD | `8B 80 BC 00 00 00 8D 94 24 9C 00 00 00 52 FF D0` | `lea eax,[esp+0x9C]; push eax; call hud_badge` | Shift HUD badge |
| 0x006CFBF6 | `8B 92 BC 00 00 00 8D 84 24 9E 00 00 00 50 FF D2` | `lea eax,[esp+0x9E]; push eax; call hud_plate` | Shift HUD plate |
| 0x006DBADD | `8B 80 BC 00 00 00 8D 94 24 9C 00 00 00 52 FF D0` | `lea eax,[esp+0x9C]; push eax; call hud_badge` | HUD panel badge |
| 0x006DBAF6 | `8B 92 BC 00 00 00 8D 84 24 9E 00 00 00 50 FF D2` | `lea eax,[esp+0x9E]; push eax; call hud_plate` | HUD panel plate |
| 0x0070E361 | `8B 90 BC 00 00 00 8D 8C 24 B0 00 00 00 51 8B CF FF D2` | `lea eax,[esp+0xB0]; push eax; mov ecx,edi; call hud_badge; nop x3` | garage shop badge |
| 0x0070E373 | `8D 8C 24 B2 00 00 00 51 EB 3C` | `jmp stub; nop x5`; stub: `lea eax,[esp+0xB2]; push eax; mov ecx,ebx; call hud_plate; jmp 0x0070E3C5` | garage shop plate (the original jumps into a tail shared with the clear path) |
| 0x00711687 | `8B 92 BC 00 00 00 8D 84 24 DC 00 00 00 50 8B CD FF D2` | `lea eax,[esp+0xDC]; push eax; mov ecx,ebp; call hud_badge; nop x3` | vehicle panel badge |
| 0x0071169F | `8B 80 BC 00 00 00 8D 94 24 DE 00 00 00 52 FF D0` | `lea eax,[esp+0xDE]; push eax; call hud_plate` | vehicle panel plate |

`hud_badge`/`hud_plate` are thiscall-compatible (`__fastcall`, ecx = element,
one stack argument, callee pops): they read the model id at text + 4 (badge)
or text + 2 (plate), look up the code, and call the element's own vtable
+0xBC with either the stock text or the slot byte. Each site is checked
against its original bytes and skipped (logged) if another plugin changed
it; the ctor hook is applied only if at least one site was.

**Data (`src/dsfforza/game/gui_assets.py --wide-glyphs`, default off).** The one-byte
plan above still runs and stays in `Manufacturer`/`Model` as the fallback
for a game without the plugin. On top, `plan_wide_glyphs` gives one code
per maker DSF has no badge for (Forza logo) and one per model name DSF has
no exact plate for (the full name, no family fallback), from 0x100 up, and
`add_glyphs` puts codes from 256 on extra texture pages (new TEXC in the
font's DRSP, UID in MDID and in the FNTD page table, pages the size of
page 0, empty DXT5 blocks precomputed) plus the empty slot records
(`HUD_SLOT_CODES`; `--wide-glyphs` keeps them out of the one-byte pool,
which only contains them with `--control-codes`).
`hud.lua` gets `ManufacturerGlyph`/`ModelGlyph`; `register.lua` already
copies every hud.lua field onto the entry.
`port_batch.py build|remerge --gui --wide-glyphs` passes the option on.
Install needs the Fonts.fchunk copy and the plugin; without the plugin the
fields are ignored and the one-byte codes show.

**Capacity.** Codes 0x100..0xD7FF (55 000) before the surrogate range.
Glyph space: shelf-packing 800 plates (98 x 18..34) and 60 badges fits in
4 pages of 1024 x 652 (5 of 1024 x 500), about 2.5 MB of DXT5. Model ids
up to 4095 (the plugin's table). Garage-shop and panel screens use the same
calls, so they show the wide glyphs too (not yet looked at in game).

`uv run dsff gui_assets glyphtest --ids 1,2,... --out out/hud/glyphtest`
writes a stress-test Fonts.fchunk (40 numbered badges "B1".., 400 plates
"P1".. on pages 1..3) and a hud.lua giving the listed cars
codes from the far end (car 1: B40 / P400 on page 3).

### Names from Forza's string tables (confirmed-static)

`src/dsfforza/formats/forza_strings.py` decodes the games' LSB2 string tables
(`media/stringtables/EN.zip`; `LOC.zip` holds id placeholders) and resolves
gamedb's "_&N" references: N = table hash << 16 | string hash, a 16-bit
rotate-left-7 xor hash of the lower-cased table name and string id. All 176
FH1 and 400 FM4 `Data_Car.DisplayName` values resolve: "370Z", "M5",
"Skyline GT-R V-Spec", "Shelby GT500" (family "Mustang"), "LF-A", "WRX STI"
(family "Impreza"), "Challenger SRT8 392". `car_texts` uses DisplayName,
List_CarMake and List_FamilyModel, from the car's game, else the other game,
else the media-name guess; `--name MEDIA=TEXT` still overrides.

## Garage card emblem: Forza logo for added cars (dsfforza.asi [hud] card_emblem)

**What the "SF" disc is (confirmed-runtime).** The garage vehicles screen
(GUI_FrontEnd.fchunk, MmDk menu engine; screen object vtable 0x00E27394, car
list FUN_007113A0) has 20 cards. Card N is a set of scene objects whose
names are in the SDF string pool: `Spr3D.Spr<N>` (picture), `Spr3D.unlocked<N>`
(the disc), `Spr3D.uPlayIcon<N>`, `Spr3D.lockIconSprite<N>`,
`Spr3D.itemWPicon<N>`, `Txt3D.vehicle_title<N>`, `Txt3D.vehicle_field_date<N>`,
`Spr3D.browserPanelBG<N>_*`. The disc is `Spr3D.unlocked<N>`, a
`MmDk::CObjectSprite3D` (vtable 0x00E8CC54, RTTI) with one resource slot at
object +0x104 (vfunc +0x94 get, +0xA0 raw set; FUN_00AB3800
`SetResource(slot, entry, flag)` is the full setter). Its resource is the same
registry entry for all 20 cards: a sprite resource of Driver's
`CResSprite` subclass (vtable 0x00E28570, factory 0x006CACE0; base
`MmDk::CResSprite` 0x00E849AC) whose UVs (4 float pairs at resource +0x30)
are 0.4727..0.6738 x 0.0078..0.2930, i.e. texels 242..345 x 2..75 of the
card icon atlas, texture `20125a7f761d9d2c` in GUI_FrontEnd.fchunk (512 x 256
uncompressed BGRA, TEXC at file offset 0x75EB50; it also holds the uPlay,
star, lock and WP icons and the challenge-type hexagons). Logged in run
`host-20261023-081745`: `card resource entry 2A3C8B28 kind 1 ... uv 0.4727,0.0078
0.6738,0.0078 0.4727,0.2930 0.6738,0.2930` for cards 1 to 6.

**How it is chosen (confirmed-static).** Not by the car: the car list writes
only per-card variables and lists the menu script reads. Per car it stores
`itemState_<N>` (FUN_00ACBE20 variable lookup; 1 locked, 3 unlocked, 4
unlocked with the take-a-ride bit, write at 0x007118A5), `item_lockedList`,
`item_priceList`, `item_purchasedList` and `item_newList` (FUN_00ABD5B0 list
set), the title and status text, and the picture (`Header_Sprite_<NN>`, a
kind-1 sprite resource whose texture slot it points at the gui.sp picture's
UID); then it broadcasts `itemStateChanged`. The script shows or hides the
disc per state. The record's UPlay/UniqueKey bytes are not read here.
So one sprite resource and one atlas texture serve every card, and neither
can change for added cars alone; the card's sprite object has to get another
resource.

**Engine calls used (confirmed-static, used in game).**

| VA | Call | Use |
| --- | --- | --- |
| 0x00ABED70 | thiscall `page->Find("Spr3D.unlocked1", -1)` | the card object in one menu page (the vehicles screen's own page is screen +8, FUN_006CC9F0; the plugin asks every page) |
| `[[[screen+4]+8]+0x280]` vtable +0x14 | thiscall `registry->Find(name, kind, -1)` | resources; kind 1 = sprite, 2 = text; returns an entry, resource at entry +0x1C, kind at +0x14 |
| 0x00ADA7F0 | fastcall `(resource) -> texture slot` | slot +0x30 u64 UID (a resolved handle with bit 63 set once bound), +0x0C/+0x10 texture size |
| 0x006CC6F0 | thiscall `slot->Set(lo, hi)` | release (FUN_00BAEA80), set, bind (FUN_006CC640); the picture update uses it |
| 0x00BAC620 | thiscall `(engine+0xC)->Find(lo, hi, info)` | is a texture UID loaded |
| 0x00AB3800 | thiscall `object->SetResource(slot, entry, flag)` | re-point the disc sprite |

The sprite takes its size from the bound texture (FUN_00B42100, vfunc
+0x58, `FUN_00B2D630(w, h)` from the texture slot), the UVs pick the part
shown. In game a 100 x 72 logo with full UVs drew at about a fifth of the
disc's size (run `host-20261023-082918`), a 512 x 256 one with full UVs
several times too big (run `host-20261023-083922`). So the replacement copies
both: a 512 x 256 texture laid out like the atlas and the SF resource's UVs.

**Two pages, and the challenge browser (confirmed-runtime).** Each card's
disc exists twice: the maker list page and the card browser page (Enter on
a maker) each hold their own `Spr3D.unlocked<N>` (run `host-20261023-094324`
logs two objects per card; re-pointing only the first left SF discs in the
browser, run `-091416` menu-08). The challenge browser shows its challenges
with the same card objects and the same `Header_Sprite_01..20` picture
resources (run `-094324` menu-19: challenge cards wore the Forza logos left
by the last car list).

**The patch (`tools/asi/src/mod_hud.cpp`, part of [hud], `card_emblem=1`).**

| VA | Original | New | Why |
| --- | --- | --- | --- |
| 0x007118A5 | `8B 91 F8 00 00 00 89 3A` (`mov edx,[ecx+0xF8]; mov [edx],edi`: itemState_N) | `E8 <card_stub> 90 90 90` | per car of the list: `card_emblem(screen=esi, id=[esp+0x18], card=ebp, cars=[esp+0x28])` under `pushfd/pushad` (a `je` follows), then the two original moves |
| 0x0070D72A, 0x0070D3BF | `E8 rel32` (call FUN_00ADA7F0) | `call slot_stub_challenges` | challenge browser fill and picture update: every disc back to the SF resource, spare's UVs back |
| 0x00711537, 0x00711354 | same | `call slot_stub_cars` | the car list's picture fill and update: if the resource is the spare, its UVs go back |

`card_emblem`: the ctor hook (0x006C6855) also reads the optional
vehicleStats field `GarageEmblem` (1 = Forza logo). For such a car, in a
maker group of fewer than 20 cars, the spare sprite resource
`Header_Sprite_20` (card 20's picture slot, unused then) gets the SF
resource's UVs and texture `f012ef7ef05a2e2d`, once per list fill, and card
N's `Spr3D.unlocked<N>` in every menu page (menu = `[[screen+4]+8]`, pages
`[menu+0x1B4]`, count `[menu+0x1B0]`, as FUN_00AB83C0 walks them) is pointed at
it (`SetResource(0, spare, flag kept)`). Every other car gets its disc's
original resource back. The spare's own UVs are saved and restored when a
group of 20 cars needs card 20 or a picture site reaches it. Leaving the
vehicles screen puts the SF resource back on the discs by itself (seen as
resource sets at the exit in run `-095535`, by a probe hook on
CObjectSprite3D's raw resource set that the final build no longer has); entering it again refills the
list and the hook re-applies. Cars without
the field, every stock car, keep the stock resource; a game without the
plugin ignores the field. All site bytes are checked first (all or
nothing); game calls run under `__try`, a fault turns the feature off and
is logged.

**Data (`src/dsfforza/game/gui_assets.py`).** `build` writes `GarageEmblem = 1` into every
hud.lua entry and always writes Fonts.fchunk, now with one more TEXC in the
vehicles_font package (`add_glyphs(..., textures=)`; MDID lists it, FNTD
doesn't): `FORZA_LOGO_UID` = `f012ef7ef05a2e2d`, 512 x 256 DXT5, FH1's
`UI4/Common/forza_logo3.xds` (the orange and silver logo with alpha) fitted
into `CARD_SF_BOX` (242, 2, 345, 75), transparent elsewhere
(`forza_emblem_image`, preview `garage_emblem.png`). `add_textures` adds it to
an existing Fonts.fchunk copy (how `out/emblem/td3/Fonts.fchunk` was made from
out/testdrive3's). The font package is always loaded, so the texture is
resident before the garage opens.

**In game (out/testdrive3, IDs 1 to 40, plugin with [garage] off; with it on: Open 9).**
Final build, run `host-20261023-100401` (confirmed-runtime; crops in
`img/garage-emblem-run-100401.png` and `img/garage-emblem-abarth-mixed.png`):
FORZA group (12 added cars): Forza logo on every card, in the maker list
(menu-01) and the card browser (menu-08). ABARTH (stock 695 SS Assetto
Corse, added 500 Esseesse ID 16, stock 500): SF, Forza, SF in both views
(menu-02, menu-13). ALFA ROMEO (stock 159 Ti, added MiTo ID 40): SF, Forza
(menu-03). AMC (stock Pacer only): SF (menu-04). Challenge browser: SF on
every card, log "challenge browser: 20 card discs back to the SF resource"
(menu-19). Back to VEHICLES: Forza logos again (menu-23, -24). No crash, no
fault logged. The logo sits exactly where and as large as the disc (same
texture size, same UVs). Test inputs: `out/emblem/td3/Fonts.fchunk`
(out/testdrive3's plus the emblem texture, `add_textures`),
`out/emblem/td3/hud.lua` (testdrive3's plus `GarageEmblem = 1`), the plugin
with `[garage] enabled=0` (`out/emblem/asi/`).

## Fixes after the first test drive (out/testdrive, 40 cars)

**Garage names (user: "imported cars missing names").** Plain-text
`ManufacturerName`/`ModelName` ("BMW", "M5") don't render; the game wants
"ID:n" string IDs. `build` now gives every plain maker and model name a new
ID from 700000 up (stock IDs run 36190 to 249144) and writes a copy of each
installed language's `Locale/<lang>/global.fchunk` (`add_strings`: SLRC
{byte offset, id} rows stay sorted by id, SLRS gets the UTF-16 text, SLRH
its count; the package is appended and its root row retargeted, stock bytes
unchanged; only English is installed here, other languages get the English
text). hud.lua then points at "ID:7000xx". Checked offline: all 4537 stock
strings read back unchanged and every hud.lua ID resolves.

**HUD glyphs too low (user: badge/plate "on top of the health bar").** Stock
glyphs have two vertical conventions: maker badges sit on the cell's bottom
(y offset + h = 99, about 56 x 56), model nameplates are centred near the
top (y offset = 25 - h / 2; median 97 x 21, up to 47 high). The first
ported glyphs were all bottom-aligned, so new nameplates sat about 60 units
low, over the boost bar (`img/r8-m5-hud-badge.png` shows it too). Now
plates are centred on y = 25 and at most 98 x 34 (two lines below 18 px),
badges 58 wide and bottom-aligned (`glyph_y`, `BADGE_SIZE`). The font atlas
still grows below row 500 (to 652 for this set); stock glyph records are
unchanged.

Stock sources: with `tools/install.ps1` having replaced the game's gui.sp
and Fonts.fchunk, `gui_assets` now reads the originals from
`<game>/dsfforza-backup/` (`stock_file`).

## Open

1. Answered: the patched gui.sp and Fonts.fchunk load, the HUD shows the new
   badges and nameplates and the garage the `dsfforza_*` pictures (out/testdrive
   and testdrive3 runs above; "Fixes after the first test drive").
2. Answered: the default `--id-policy garage` gives the first cars IDs 1..61
   (`--first-id` only moves the high range); the garage lists them (runs above,
   `asi.md` for unlock and save flags).
3. Answered: a plain `ManufacturerName` groups the cars but doesn't render as
   a label ("Fixes after the first test drive").
4. Done: `add_strings` adds the localisation strings and the build writes
   the `global.fchunk` copies ("Fixes after the first test drive").
5. dsfforza.asi [hud] slot-record version (the one in `mod_hud.cpp` now):
   not yet run in game. The sandbox died at "window up" on every run after
   the 2026-10-23 reboot (all agents). Pending: `glyphtest` on out/testdrive
   (IDs 1..8, codes up to page 3), the 10-car real build (`out/hud/td10`),
   and all 40 cars of out/testdrive3 with `--wide-glyphs` (`out/hud/td3`:
   50 wide glyphs; the one-byte fallback leaves 7 cars on the blank plate).
   What did run: the ctor hook and site patches (10 of 10, every run), the
   per-car field read, and a 16-bit glyph from page 1 drawn through the
   `"ID:"` route (run `20261022-224711`, badge "B39").
6. Slot records are rewritten in the loaded FNTD; if an element caches the
   glyph quad and ignores the refresh, the old glyph would stay until the
   text changes. The empty-then-slot SetText is meant to force it.
7. Garage-shop and vehicle-panel screens (sites 0x0070E361.. and
   0x00711687..) use the same hook; not looked at in game.
8. Garage card emblem: answered in "Garage card emblem" above. The "SF" disc
   is the sprite object `Spr3D.unlocked<N>` of each card, one shared sprite
   resource (UVs 242..345 x 2..75 into atlas `20125a7f761d9d2c`) shown or
   hidden by the menu script per `itemState_<N>`; nothing per car picks it.
   dsfforza.asi [hud] re-points it for cars with `GarageEmblem = 1` (in game:
   run `host-20261023-100401`). Still open: a maker group of exactly 20 cars
   keeps the SF disc on all its cards (card 20 needs the spare; untested in
   game); the spare's UV restore when a 20-car group or the challenge
   browser's 20th card uses Header_Sprite_20 is wired but was not exercised.
9. Answered: the crash with `[garage]` on (runs `host-20261023-080433`,
   `-080858`, `-081321`) was not the emblem patch. The garage fills its maker
   menu (FUN_006C6940) into four menu-script lists of `Install\menusmaster.sp`
   (`ManufacturerDataList` and three more) that hold 40 entries. With the
   plugin, register.lua gave the new makers their own groups (49), and the
   41st maker's string set read through NULL at 0x00ACE11F (caught with
   `[dsfforza] crash_log=1`, run `-101840`). dsfforza.asi now grows those
   lists to `max_groups` when the menu is filled and skips a missing element
   (asi.md, "Garage and save"). In game with the shipped ini (`max_groups=120`)
   and this set's emblem data: 49 makers listed and the last one opened with
   the Forza emblem (run `-103212`, `img/garage-49-makers-run-103212.png`);
   with 600 extra test entries, 109 makers, the save side file written and
   read back in a second session (runs `-104805`, `-105532`,
   `img/garage-109-makers-run-104805.png`).
