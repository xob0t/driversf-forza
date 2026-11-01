# Forza sources: what is on the user's discs

The three images the tools use are XGD3 discs (partition at 0x2080000).
`default.xex` gives the title and disc number:

| Image | Title | Disc |
|---|---|---|
| `Forza.Horizon.XBOX360.iso` | FH1, 4D5309C9 | 1 of 1 |
| `Forza 4 [NTSCU][DVD1].iso` | FM4, 4D530910 | 1 of 2 |
| `Forza 4 [NTSCU][DVD2].iso` | FM4, 4D530910 | 2 of 2 |

The names are this PC's; the tools never go by them. `forza_roster.identify`
reads each image's `default.xex` (cached by path, size and time), and every
step asks `forza_roster.game_discs` / `discs_of` for "the FH1 disc" or "FM4
disc 1" out of the build's images (`assets/*.iso` or `--iso`). A file that is
no Forza disc is skipped with a note, and a game without its disc just has no
cars (issue #113).

Two more sources sit in `assets/` on this PC (#162):

- `Forza Horizon 2 (USA).iso`, the Xbox 360 version (title 4D530AA4, one
  disc). `forza_roster` doesn't know the title and skips it as another game.
  Its 215 main and cockpit carbins parse with the FH1 parser (#162's survey
  of 2026-10-28, not rechecked here).
- `Forza Motorsport 4 Full DLC Collection/`: 16 LIVE packages, one folder
  per pack. `forza_roster` reads them as a source when asked to (below,
  "FM4 DLC packs"). 176 cars, 148 of them not in the roster.

Together they add 177 cars the roster doesn't have (29 only on FH2).

## FM4 DLC packs

Surveyed 2026-11-01 on the user's 16 packages (`out/scratch` scripts of the
#162 PR, not kept).

**Identification (confirmed-static).** Every package is an STFS `LIVE` file:
title ID 0x4D530910 at header 0x360, content type 0x00000002 (marketplace
content) at 0x344, header size 0xAD0E, the read-only ("female") layout. The
file names are hashes without an extension (`E3470A83...4D`) and the folders
are the collection's own. So `forza_roster.identify_package` goes by the
header only: magic, title and content type. `dlc_packages` finds them under
`assets/` in any folder and under any name (disc images are left to
`identify`). The display name in the header (0x411) names the pack: 15 car
packs, plus the Limited Collector's Edition's second package, "Launch Track
Pack" (tracks only, no `Data_Car`).

**Layout.** It is the same as the disc 2 packs:

- `Media/DLCZips/<N>_pri_65.zip` holds the cars as `media/cars/<NAME>/`
  folders, `media/wheels/<NAME>/` and, new against disc 2:
  - `media/db/patch/<N>_merge.slt`: SQLite, the pack's rows for gamedb.slt.
    `Data_Car` holds one row per car of the pack (the Porsche pack: 30 rows
    for 30 cars). Next to it are `Data_CarBody`, `Combo_Colors`, `Data_Engine`,
    `CarPartPositions`, the upgrade tables and so on, and `List_CarMake` /
    `List_FamilyModel` rows for the pack's makers (Porsche's row 39 is in
    the Porsche pack).
  - The Porsche pack also has a `100001_execute.sql` (UPDATEs to career
    events only).
  - `media/audio/cars/Engines/EngineTuning/<car>_ET.xml` and
    `HarmonicTuning/*_HT.xml` as loose files.
- The engine banks sit outside the zip, as loose files in the package:
  `Media/DLCZips/<N>_pri_65/Media/audio/cars/Engines/Soundbanks/LOD1/*.fsb`.
- `Media/DLCZips/StringTables_pri_<M>.zip` holds one folder per language
  (`EN/`, `DE/`, ...), each with `Data_Car.str`, `List_CarMake.str` and
  `List_FamilyModel.str`, among others. These are whole tables, not only the
  pack's strings: the Porsche pack's EN `Data_Car.str` has 1275 strings and
  its `List_CarMake.str` 173, against 159 in disc 1's.

**What the cars carry (confirmed-static, all 176).** Every car has its main
carbin, `<car>_lod0.carbin` and `<car>_cockpit.carbin`. Every car's `_ET.xml`
is in its own pack and in none of disc 1's `enginetuning.zip`. 3 more
folders are Autovista `_SLOD` models.

**Names and years.** `forza_roster.pack_info` reads them from the pack:
`Data_Car` of its merge database (Year, DisplayName, ModelShort, MakeID,
FamilyModelID), and the text from its EN tables. A maker or family row the
pack doesn't carry is taken from FM4's disc gamedb, and its text from the
disc's tables (`forza_strings.names_from`, pack first).

**Overlaps.** None of the 176 cars is on an FM4 disc. 28 are on the FH1 disc
(Veyron SS, Agera, Huayra, Venom GT, ZL1, ...). See "Cars in several
sources" below.

**Not used yet.** The cars join the roster only when asked to
(`forza_roster.roster(isos, packages)`, `dsff forza_roster --dlc`). The
default roster, the car lists and the installer's build stay the discs'
until the car manager can install cars beside the one vehicle archive
(#170). The preflight lists the packs it finds. Building a DLC car also
needs the pack's merge rows in the gamedb the converter reads, and the pack's
ET, HT and banks in the engine-sound lookup (#457).

## Where the cars are

FH1 keeps 179 zips in `media/cars`. 175 are cars. The other four are
`Shared`, `Driver`, `AppearancePresets` and the `nul_car_00` placeholder.

FM4 disc 1 keeps 319 zips in `Media/cars`: 312 cars, 4 Autovista models
(`FOR_GT_05_SLOD`, `MER_SLSAMG_11_SLOD`, `LEX_LFA_10_slod`,
`fer_f142_10_slod`), plus `Shared`, `Driver` and `nul_car_00`. The folder
`Media/cars/HOL_11_CommodoreVE_11/` holds only a `carAttribs.xml`, which
overrides a car that ships on disc 2.

FM4 disc 2 has no loose cars. Its cars sit in four PIRS packages under
`content/0000000000000000/4d530910/00000002/`:

| Package | Display name | Blocks | Cars | Inner zip | Loose audio |
|---|---|---:|---:|---:|---:|
| `...01` | 1950-2000 Model Year Car Pack | 177,214 | 63 | 719 MB | 25 files, 6 MB |
| `...02` | 2001-2005 Model Year Car Pack | 204,051 | 69 | 830 MB | 21 files, 5 MB |
| `...03` | 2006-Current Model Year Car Pack | 140,705 | 56 | 574 MB | 8 files, 2 MB |
| `...04` | Autovista Car Pack | 207,293 | 21 Autovista | 397 MB | 434 files, 451 MB |

Every package has the same layout:

```
Media/N.puboffer, Media/LicenseMasks
Media/DLCZips/000N_pri_65.zip          one big zip, cars + wheels
Media/DLCZips/StringTables_pri_15N.zip
Media/DLCZips/zipmount.xml             mounts the string tables zip
Media/DLCZips/000N_pri_65/Media/audio/...   loose .fsb/.fev engine and Autovista audio
```

Inside `000N_pri_65.zip` each car is a folder, `media/cars/<NAME>/...`, not
a nested zip. Its files use method 21 (XMemCompress LZX) like the disc car
zips, so `ForzaZip` reads them. Each car also has a matching
`media/wheels/<NAME>/` folder. There are no tracks in the packages.

The Autovista pack holds `_SLOD` cars only. Each one has
`<car>_s_lod0.carbin` (the F50's is 13 MB and 414k triangles, with the
engine bay modelled), `<car>_cockpit_s.carbin`, `.gr2` skeleton and
animation files, `camanim_*.xml` and the narration audio. None of them has
an LOD1-4 main carbin.

## Cars in several sources

Since #162, a car in a DLC pack and on a disc is built from the version that holds
more of the 10 files the conversion reads (`port_car.WANTED`: the main and
cockpit carbins, `nodamage.xds` and `lights.xds` with their `_lod0`,
`interior_lod0.xds`, MAXData.xml, CarAttribs.xml, ShaderSettings.xml). On a tie the game order decides
(`GAME_PREFERENCE`, FH1 first), and a disc comes before a pack
(`forza_roster.choose_source`, used by `port_car.pick_source`).

**The 28 cars in FH1 and the FM4 DLC (confirmed-static, 2026-11-01).** For each
car, both versions were opened and compared:

- **Inputs.** Both versions hold all 10 files the conversion reads, on 28 of
  28 cars, so the rule gives a tie and FH1 everywhere. The default roster
  doesn't change.
- **Same model.** The two versions are the same model, packed to each game's
  convention:
  - The main carbins have the same sections and LOD 1-5 index counts within
    a few percent. Larger differences: Veyron SS LOD 1, 58,259 (FH1) against
    45,985 (DLC); SVT Raptor 96,792 / 88,139; Camaro ZL1 53,080 / 57,479.
  - FH1 keeps the `headlightL`/`headlightR` sections in the cockpit carbin and
    FM4 in `<car>_lod0.carbin`. The section names were listed on 5 cars (M5 12,
    Agera, FR-S, Veyron SS, 205 T16). FH1's cockpit carbin has more vertices on
    28 of 28.
- **Other files.**
  - FH1 adds `interior_emissive_lod0.xds` on 27 cars, which the conversion
    doesn't read.
  - Engine sound: FH1's `_ET.xml` is in its disc's `EngineTuning.zip`.
    The DLC's is only in the pack, which the engine-sound lookup doesn't read
    yet.

The data picks neither version on these cars, so they keep the version they
are built from today.

**Disc overlaps keep the game order.** The same rule over the 102 cars on
both FH1 and the FM4 discs would move one built car: `NIS_Leaf_11`. Its FH1
zip has no `physics/maxdata.xml` and its FM4 one does (car_dims falls back to
the gamedb for it). On 11 others the FM4 version lacks `ShaderSettings.xml`,
and the rule keeps them on FH1. So `pick_source` uses the rule only where a
DLC version is one of the candidates, and disc cars keep FH1-first as before.

## Roster

`src/dsfforza/build/forza_roster.py` reads only zip central directories, so a full run
over all three images takes under a second. The result is in
`research/forza-roster.tsv`. It has 598 rows: 573 unique cars and 25
Autovista models.

| | Cars |
|---|---:|
| FM4 only | 398 |
| FH1 only | 73 |
| In both, by media name | 102 (76 on FM4 disc 1, 26 in the disc 2 packs) |
| FM4 total (disc 1 + packs) | 500 |
| FH1 total | 175 |

FM4 disc 1 and the disc 2 packs share no car names. As stored, the car data
is 3.1 GB on FM4 disc 1, 2.0 GB in the disc 2 packs and 1.5 GB on FH1.
Both games' `Media/db/gamedb.slt` are plain SQLite. `Data_Car` maps
`MediaName` to the model year and IDs. Make and model names are string IDs
(`_&3444160236`) that resolve through `Media/StringTables/<lang>.zip`
(`LSB2` files, decoded by `src/dsfforza/formats/forza_strings.py`); the roster still lists
the year only.

## STFS (src/dsfforza/formats/stfs.py)

The three discs hold 47 STFS files, all PIRS. Besides the car packs they
are system updates, `Database.xmplr`, `HeadPosition.xmplr`, `nxeart`,
`AvatarAwards`, `NuiIdentity.bin.be` and the NUI speech packs. Every one
uses the read-only ("female") layout. The car packs have header size 0xAD0E
and data from 0xB000. The tool checks the hash tree: the top table against
the volume descriptor hash, and each lower table against its parent entry.
All 47 pass. The car packs have 834 to 1229 tables over three levels. Pack 3
also passes a full SHA-1 check of all 140,705 data blocks, and ten of the
small packages pass full data checks too.
Pack 4's file table spans 8 blocks, which exercises the block-chain
walking.

The read-write ("male") layout, with paired tables picked by status bit
0x40 or flag bit 1, is implemented but untested. None of the discs has such
a package.

## Carbin: FM4 vs FH1 (src/dsfforza/formats/carbin.py)

The parser follows carbin-garage's docs (`FM4_CARBIN_CONDENSED.md`,
`CARBIN_TRANSCODE.md`). These points were confirmed on the files:

- TypeId 2 is FM4 main/lod0/cockpit and 5 is FH1's. Caliper and rotor
  carbins are TypeId 1 in both games, so the parser tells the games apart
  by section version (2 = FM4, 3 = FH1). TypeId 0 (`stripped_*`) files are
  headers only.
- FM4 vertices are 32 bytes: the same first 24 bytes as FH1 (position,
  uv0, uv1, quaternion), then 8 opaque bytes. FH1 keeps 4. `Vertex.extra`
  holds them.
- FM4 has no bone-weight words before either pool. Its subsections have a
  single u32 3 before the index count. Its tail is 9 bytes, a*b, 4, c*d.
  Sections run back to back and the file ends with a 24-byte footer.
- The section list starts at a variable offset: 0x1020 in the FM4 370Z
  main carbin, 0x12A8 in the Golf R, 0x36C or 0x370 in calipers. The parser
  tries each candidate offset.
- lod0 and cockpit index strips restart at 0x00FFFFFF, the 24-bit index
  limit, as well as 0xFFFF and 0xFFFFFFFF. Before this fix, every lod0
  export failed with an out-of-range index.
- FH1 lod0 tails have a lod0_count*4 stream after the c*d table. The old
  probe stopped at a false "section" match before it and parsed 4 of the 33
  sections. The probe now walks the header to a sane name and version, and
  all 33 parse.

Validation. 73 carbins were checked: the FH1 370Z set, every carbin of five
FM4 disc 1 cars (NIS_370Z_10, FER_F40_87, LAM_Countach_88, VW_GolfR_10,
MCL_F1_93), and the main, lod0 and cockpit carbins of four disc 2 cars
(NIS_SkylineGTR_93, LAM_Murcielago_05, AUD_TT_07, FER_F50_95_SLOD). All 73
parse to their declared section count and end at the footer. FH1 370Z main
still gives 46 sections. The FM4 and FH1 370Z LOD1 exports both have
41,118 triangles, and their preview PNGs are byte-identical. Renders of all
the cars above look right (`out/fm4/render/*.png`).

## Wheel placement data (src/dsfforza/convert/forza2dsf/ car_dims)

FM4 cars carry the same `physics/maxdata.xml` as FH1 (lower-case path; the
370Z's values are identical in both games): `<Misc Wheelbase
FrontTrackOuter ...>` and `<BottomCenterWheelbasePos z>`. A scan of every
car zip and pack folder found it in every FM4 car (disc 1 and the 188 disc 2
pack cars) and every FH1 car except `NIS_Leaf_11`. Both
games' `gamedb.slt` also have `Data_CarBody` (`ModelWheelbase`,
`ModelFrontTrackOuter`, `BottomCenterWheelbasePosZ`, pristine bounding box),
keyed by `Data_Car.Id * 1000`: 503 rows in FM4's, 176 in FH1's. Both have
the 370Z (row 1103000, wheelbase 2.5516) and the Leaf (2.690).
Since #406 `car_dims` takes the wheelbase and its midpoint from the gamedb's
`Data_CarBody` (the body's arches centre there on the 35 cars where the two
differ; research/wheels.md, "Body height and wheelbase"), track and ride height
from `maxdata.xml` (the gamedb's tracks are off some bodies by up to 0.32 m), and
everything from the gamedb for the Leaf. `CarPartPositions`
rows 5 to 8 sit near the hubs (the 370Z's are 2.5516 apart, its wheelbase),
but what the part IDs mean isn't known. Tyre radius and width are measured
on the car's own `wheel` section.

**Forza y = 0 is not the ground (confirmed-static, 2026-10-22).** It is the
bottom of the body at the wheelbase centre (`BottomCenterWheelbasePos` y = 0);
the ground is `StockRideHeight` below it (`<Misc FrontStockRideHeight
RearStockRideHeight>`, gamedb `ModelFront/RearStockRideHeight`). On 15 cars
`BottomCenterWheelbasePos` y is negative and the body's bottom is that far below
0 (VW_BEETLE_68 -0.158, its ground 0.316 m down); see research/wheels.md,
"Body height and wheelbase". Evidence on
the 370Z / Skyline R32 / M5: body vertices reach y = -0.055 / -0.091 / -0.049
(below a ground at 0 is impossible); MAXData's collision box reaches MinY
-0.14 / -0.2355 / -0.14 against ride heights 0.144 / 0.231 / 0.164; MaxY +
ride height gives 1.34 / 1.35 / 1.46 m, the real roof heights (1.315, 1.34,
1.47 m), while MaxY alone is 12 to 23 cm short; with ground at 0 the tyre
top (y = 2 x radius) sits 3 cm (370Z) to 15 cm (Skyline) above the lowest
wheel-arch edge at the axle; with ground at -ride height it sits 4 to 16 cm
below it.
`forza2dsf.Placement` used ground = 0 until 2026-10-22, so every converted
body sat 0.14 to 0.23 m too low on its wheels (eye and roof with it);
`CarDims.ride_height` now lifts it.

**`BottomCenterWheelbasePos` z counts towards the front (confirmed-static,
2026-10-22).** The model's front is at -z, so the wheelbase midpoint sits at
model z = -BottomCenterWheelbasePos.z (gamedb `BottomCenterWheelbasePosZ`
is that model z itself: MAXData's value negated, on 558 of the 573 roster cars). Evidence: the body's wheel-arch cut-outs
(lowest outer body vertices per 10 cm of z) centre on -z within a few cm on
the Cuda (-0.23 against z = +0.203), Mustang Boss 429 (-0.20 / +0.213),
Veyron (-0.12 / +0.122), Charger 69, 300SL, Range Rover, 250 GTO, Fiat 500
(+0.10 / -0.107); 25 of 29 cached cars with |z| > 0.06 fit -z, the other
four are arch-detection misses (side intakes, mid-engine cars;
`out/fixes/cache_check.py`). Overhangs agree: the Cuda comes out 0.92 m
front, 1.00 m rear, against 1.33 / 0.60 with +z. `forza2dsf.car_dims` used +z
until 2026-10-22, which put every car's wheels 2|z| off its arches (Cuda
0.41 m, Mustang 0.43, Veyron 0.24; cars with z near 0, like the 370Z and M5
tested in game, were unaffected); `CarDims.mid_z` is now the model-space
midpoint. Render with the wheels at the hubs: `damage.py render ... --wheels`
(`img/fix-cuda-veyron-wheels.png`, before and after).

## Commands

```sh
uv run dsff stfs info    "assets/Forza 4 [NTSCU][DVD2].iso::content/0000000000000000/4d530910/00000002/4d53091000000001"
uv run dsff stfs list    <package> [pattern]
uv run dsff stfs extract <package> out/dir [pattern] [--verify]
uv run dsff stfs verify  <package> [--data]
uv run dsff forza_roster [--tsv research/forza-roster.tsv] [--all]
uv run dsff forza_roster --dlc [DIR]   # also the FM4 DLC packages under DIR (default assets/)
uv run dsff carbin out/fm4/NIS_370Z_10/nis_370z_10.carbin --lod 1 --obj out/370z.obj
```

In Python, `Stfs.open(entry)` returns a seekable file. Pass it to
`ForzaZip` to read one car out of a pack without extracting the 700 MB
inner zip:

```python
pkg = stfs.open_package(iso + "::content/.../4d53091000000001")
z = ForzaZip(pkg.open("Media/DLCZips/0001_pri_65.zip"))
data = z.read("media/cars/NIS_SkylineGTR_93/nis_skylinegtr_93.carbin")
```

`XIso.open(entry)` does the same for files on a disc.
