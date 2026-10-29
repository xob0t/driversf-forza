# Prior art

Updated: 2026-10-29

## Fireboyd78/driver-tools

<https://github.com/Fireboyd78/driver-tools>, C#, last pushed 2022-08. The
README allows free use and redistribution ("No credit is necessary") but there
is no formal license file. Read it for format knowledge and don't copy code
until that's clarified.

Relevant parts:

- `DSCript/Spooling/FileChunker.cs`: CHNK reader and writer, including the
  per-entry name string and LZW flag.
- `DSCript/Common/Chunks/types.csv`: FourCC names, many from DSF (VEHC is
  `VehicleChunk`). `ChunkTypes.cs` next to it says of VEHC "No research has
  been done on this format".
- `DSCript/Models/`: MDXN model package (DPL and DSF), submodels, vertex
  declarations, materials and textures.
- `DSCript/Models/Hierarchies/`: the AWHF hierarchy format and vehicle parts
  for DPL. DSF's `UPVH` uses the same `AWHF` magic.
- `Antilli`: WPF model viewer with COLLADA/OBJ export, built on DSCript.
- `LuaSF`: compiled Lua script packages for DSF.

## ReHamster/DriverNGToolkit

<https://github.com/ReHamster/DriverNGToolkit>, C++, last pushed 2025-04, no
license file. SoapyMan's DriverNGHook: Lua overrides, an in-game Lua console
and ImGui. When this note was written (2026-10-21) the local game folder had
copies of `DriverNGHook.asi` only inside the unpacked Felony Rework and
snoopii mod-menu folders; `tools/install.ps1` now installs 0.7.2. Its own
Lua lives in `<game>/plugins/DriverNGHook/*.lua`; mod Lua goes to
`Common/UserLuaScripts` (`game-lua.md`). Its Lua console is a cheap way to
test vehicle IDs and spawning before writing native hooks.

## Local work

The user's local checkout of DSF Enhanced
(<https://github.com/xob0t/dsf_enhanced>), branch
`archive/shift-map-2026-10-03` (local only, not pushed):

- `tools/extract-gegl-mapping.py`: the CHNK reader this project's
  `src/dsfforza/formats/chnk.py` grew from.
- `tools/ghidra/`: headless Ghidra scripts (`analyze-driver.ps1`,
  `ApplyDriverKnowledge.java`, reference and decompilation exporters).
- `reverse_engineering/driver_sf/addresses.tsv`: 400+ named RVAs, including
  the `MODL` pool (`ModlPool_Acquire` 0x00093FD0) and resource wrapper
  registries.
- `_ghidra_projects/DriverSF`: the analyzed Ghidra database for `Driver.exe`
  1.04, SHA-256 `C79987B0...6616`.
