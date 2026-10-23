# Prior art

Updated: 2026-10-21

## Fireboyd78/driver-tools

<https://github.com/Fireboyd78/driver-tools>, C#, last pushed 2022-08. The
README allows free use and redistribution ("No credit is necessary") but there
is no formal license file. Read it for format knowledge and don't copy code
until that's clarified.

Relevant parts:

- `DSCript/Spooling/FileChunker.cs`: CHNK reader and writer, including the
  per-entry name string and LZW flag.
- `DSCript/Common/Chunks/types.csv`: FourCC names, many from DSF. It lists
  VEHC as "No research has been done on this format".
- `DSCript/Models/`: MDXN model package (DPL and DSF), submodels, vertex
  declarations, materials and textures.
- `DSCript/Models/Hierarchies/`: the AWHF hierarchy format and vehicle parts
  for DPL. DSF's `UPVH` uses the same `AWHF` magic.
- `Antilli`: WPF model viewer with COLLADA/OBJ export, built on DSCript.
- `LuaSF`: compiled Lua script packages for DSF.

## ReHamster/DriverNGToolkit

<https://github.com/ReHamster/DriverNGToolkit>, C++, last pushed 2025-04, no
license file. SoapyMan's DriverNGHook: Lua overrides, an in-game Lua console
and ImGui. The local game folder has copies of `DriverNGHook.asi` inside the
unpacked Felony Rework and snoopii mod-menu folders, but the game's own
`plugins/` folder is empty, so it isn't active. Its scripts go in
`<game>/plugins/DriverNGHook/scripts`. Its Lua console is a cheap way to test
vehicle IDs and spawning before writing native hooks.

## Local work

`C:\Users\admin\Documents\GitHub\driver-sf-mods`, branch
`archive/shift-map-2026-10-03`:

- `tools/extract-gegl-mapping.py`: the CHNK reader this project's
  `tools/chnk.py` grew from.
- `tools/ghidra/`: headless Ghidra scripts (`analyze-driver.ps1`,
  `ApplyDriverKnowledge.java`, reference and decompilation exporters).
- `reverse_engineering/driver_sf/addresses.tsv`: 400+ named RVAs, including
  the `MODL` pool (`ModlPool_Acquire` 0x00093FD0) and resource wrapper
  registries.
- `_ghidra_projects/DriverSF`: the analyzed Ghidra database for `Driver.exe`
  1.04, SHA-256 `C79987B0...6616`.
