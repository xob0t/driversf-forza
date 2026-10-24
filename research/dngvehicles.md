# dngvehicles.sp

Updated: 2026-10-21

Static analysis of build 1.04 `Install/dngvehicles.sp` (483,110,912 bytes).
Every claim here comes from reading the file with `src/dsfforza/formats/chnk.py` and
`src/dsfforza/formats/vehicles.py`; what the executable does with it is in
`exe-vehicles.md` and `spool-limits.md` (confirmed-static claims below cite
them).

Evidence levels follow the parent repo's convention:
`confirmed-static`, `confirmed-runtime`, `hypothesis`, `disproved`.

## Container format (confirmed-static)

The archive is a standard Reflections CHNK container, the same format as
`san_francisco.dngc` and the `.fchunk` files:

```text
header  "CHNK" u32 total_size, u32 count, u32 version=3 (bit 31 = LZW)
entry   4cc tag, u32 offset, u8 version, u8 name_len, u8 align_log2, u8 0, u32 size
```

Offsets are relative to the CHNK that holds the entry. When `name_len` is
non-zero, an ASCII name follows the entry's data at `offset + size`. Nested
containers start with their own `CHNK` header. Layout matches
`ChunkEntry` in Fireboyd78's DSCript (`Spooling/FileChunker.cs`).

## Root (confirmed-static)

| Index | Tag | Count | Notes |
| ---: | --- | ---: | --- |
| 0–449 | VEHC | 450 | 150 vehicles x 3 variants, 4 KiB aligned |
| 450 | SSIC | 1 | shared shaders, common sounds, lookup tables |
| 451 | DMWK | 1 | 0x107 bytes, `DMWMKTG` |
| 452 | BINF | 1 | exporter log: 150 source `.rimodel` paths |

Each VEHC's name is a display class such as `Saloon 15`, `Cop Car 01` or
`70's Muscle 06`. The three variants of a vehicle sit next to each other and
share the name.

BINF says the archive was built by `VehiclePackagerPlugin.dll v1.8.0.0` on
07/07/11 from `R:\Resources\Vehicles\Vehicles\<Model>\Model\<Model>.gck.rimodel`.
BINF order is not archive order.

## Lookup tables in SSIC/NONR (confirmed-static)

`SSLP` (0xAA8 bytes): 0x1C-byte header, then six-byte records
`{u16 spool_id, u16 vehc_index, u16 kind}`. Every vehicle has three IDs: `N`
(kind 0), `N+1000` (kind 1), `N+2000` (kind 2). Base IDs run from 62
(`Tanner`) to 303. The game finds an ID by scanning the records linearly
(see `exe-vehicles.md`), so order doesn't matter.

| Header offset | Value | Meaning |
| ---: | --- | --- |
| +0x02 | u16 450 | record count (confirmed-static) |
| +0x04 | 0x00EEB102 | = 35 × 0x6D000 + 4 × 0x1000 + 0x102; no code reads it |
| +0x08 | 0x0006D000 | NONR buffer per kind-0 slot (confirmed-static, `spool-limits.md`); the largest stock NONR (0x6CE30) rounded up to 4 KiB |
| +0x0C, +0x10 | 0x1000, 0x1000 | NONR buffers for kinds 1 and 2 (confirmed-static) |
| +0x18 | bytes 35, 2, 2, 0 | spool slots per kind (confirmed-static) |

`VSCT` (0x3140 bytes): `"VSCT" u16 version=1, u16 count=450`, then 28-byte
records. The game takes its vehicle count from this header. The records are `{u16 spool_id, u8, u8, u16, char name[16], u8[6]}`. Names are cut to
16 characters (`Alfa_Romeo_Giuli`). Each base ID appears three times with
identical contents. Only base IDs appear.

`src/dsfforza/formats/vehicles.py` joins SSLP, VSCT, VEHC and BINF into a vehicle list.
Truncated names that match more than one BINF model print as
`A|B|C`.

### Hypotheses

- The last VSCT byte is a vehicle category, not a bit mask: 0x10 saloon,
  0x02 cop, 0x04 taxi (the three Desanne entries), 0x13 sports car,
  0x0C 70s muscle, 0x0B pickup and so on, one value per VEHC class family
  (data; table in `hosts.md`).
- The `UPVH` hierarchy UID equals the spool ID (see below), so the spool ID is
  probably the game's vehicle ID.

## One vehicle: `Saloon 15` (ASYM Desanne, spool 266)

### Variant 0, VEHC 3 (1.4 MiB)

```text
VEHC 'Saloon 15'
  MDXN 'MDXN Root'
    DRSP 'Resource Chunks' [251]
      TEXC   x1   texture (UIDC uid + DXTC DDS, 0x55080 bytes)
      MACO   x2   material containers (MAHE, MATX, MARC x8 render methods)
      SBMC   x243 submodels (SBBH header, 0x48 bytes, v4)
      VXCK   x3   pooled vertex buffers (VXHD, VXDP, VXDT, VDCL decl)
      IXCK   x1   pooled index buffer (IXHD, IXDP, IXDT)
      MDID        renderer data UIDs
  NONR
    MODL  0x1F64  model container
    VSRT          engine sound: VSVH header, SBAK bank, GRBK granular bank
    VO3D  0x368   handling data
    UPVH  0x42B0  vehicle hierarchy (parts, damage)
    SPSR          shader param sets: SPID vehicle id, SPSS x3 (SPSP x10 each)
```

### Variant 1, VEHC 4 (0.9 MiB)

MDXN with one texture, 2 material containers, 8 submodels, 3 vertex buffers,
1 index buffer; NONR with only a MODL (0x230).

### Variant 2, VEHC 5 (0.55 MiB)

MDXN with no texture, 2 material containers, 14 submodels, 3 vertex buffers,
1 index buffer; NONR with only a MODL (0x2E8).

Variant 0 is the complete vehicle. Variants 1 and 2 are smaller models that
borrow everything else from variant 0. Variant 1 (N+1000) is the interior
and variant 2 (N+2000) a clean exterior (`vehicle-model.md`,
`spool-limits.md`: FUN_005A7FC0 spools them as the interior and exterior).

### UPVH (confirmed-static header, hypothesis for the rest)

Starts with `AWHF`, the hierarchy format DSCript reads for Driver: Parallel
Lines (`Models/Hierarchies/HierarchyInfo.cs`,
`Vehicles/VehicleHierarchyData.cs`). Header fields for Saloon 15:

| Field | Value |
| --- | --- |
| model UID | `0x31691200:0x28954700`, format byte `0x52` |
| type | 26 |
| part count | 77 |
| hierarchy UID | 0x10A = 266, the spool ID |
| PDL size | 0xC80 |

DPL's layout (parts, moving parts, damaging parts, marker points) is the first
thing to test against DSF's.

### VO3D handling data (confirmed-static layout)

```text
u16 spool_id, u16 0, "VO3D", u32 version=1, u32 count
count x {u16 param_id, u16 0xAAAA, f32 value}
```

Saloon 15 has 107 parameters. Across all 150 vehicles (variant 0 only, the
others have no VO3D):

- every VO3D is version 1 and its size is exactly `0x10 + count * 8`;
- counts range from 86 to 128;
- 192 distinct parameter IDs appear, between 0 and 606; only 27 appear in
  every vehicle;
- 161 IDs vary between vehicles and 31 have the same value wherever they
  appear.

So VO3D is a sparse override list (hypothesis): an unlisted ID probably falls
back to a default held somewhere else. `Driver.exe` defines exactly 607
parameter IDs (0–606) with a static type table: 367 float, 3 bool, 237
unused. The defaults are filled in at startup (see `exe-vehicles.md`). That suits generating handling from
Forza's physics data, since the converter only needs the IDs it can fill. The
meaning of each ID still has to come from the executable or from comparing
vehicles.

## Open questions

1. Answered: variants 1 and 2 are the interior and the clean exterior
   (above).
2. The VEHC-to-BINF mapping for ambiguous names (Camaro 1968/2010/1986, cop
   and taxi versions).
3. Answered: SSLP header fields (table above, `spool-limits.md`).
4. The VSCT byte fields other than the role mask.
5. Whether SBMC/SBBH and the MDXN model package match DSCript's DPL/DSF
   `ModelPackage` reader closely enough to reuse its layout.
6. Whether two vehicles may share model and texture UIDs (MDID/UIDC) while
   both are loaded. A clone sharing them works on its own (see
   `in-game-tests.md`), and so does the clone with its source loaded
   (`in-game-tests.md`, "Clone and source loaded together").

## Rewriting the archive

`src/dsfforza/formats/chnk.py rebuild` re-serializes an archive from its parsed tree, and
the output is byte-identical to the stock `dngvehicles.sp`. The layout rules
are in the `chnk.py` docstring. The same rules round-trip
`GUI_FrontEnd.fchunk`, but not `san_francisco.dngc`, `gui.sp` or
`ScriptsLuaScripts.fchunk`. Those come from other exporters, with a
different fill pattern and nested sizes that don't match their headers.

## Clone

`src/dsfforza/build/clone_vehicle.py <dngvehicles.sp> <out.sp> --source 266 --id 304` copies the three VEHC
variants, patches the ID at VSVH+0, VO3D+0, SPID+0 and UPVH+0x14 (the only
places variant 0 stores it in all 150 vehicles; variants 1 and 2 hold none),
inserts the copies after the last VEHC, and appends three SSLP records and
three VSCT records. Reading the result back with `src/dsfforza/formats/vehicles.py` gives
151 vehicles. In game the clone spools, spawns and drives
(confirmed-runtime, `in-game-tests.md`); its HUD name and badge also need a
Lua `vehicleStats[304]` entry.
