# driversf-forza

Adds the cars from your own Forza Horizon and Forza Motorsport 4 discs (Xbox 360)
to Driver: San Francisco on PC. The cars are built on your PC from your discs; this
project ships no game or Forza files.

## What you need

- Windows 10 or 11.
- Driver: San Francisco for PC, version 1.04, with its original game files.
- Your Forza disc images (`.iso`, any file names): Forza Horizon, and Forza
  Motorsport 4 discs 1 and 2. A car that is only on a disc you don't have is skipped.
  Forza Horizon 2 and Forza Motorsport 4's downloadable car packs aren't used yet
  ([#162](https://github.com/xob0t/driversf-forza/issues/162)).
- The Microsoft Visual C++ 2015-2022 Redistributable, **x86**
  (<https://aka.ms/vs/17/release/vc_redist.x86.exe>). The installer checks for it.
- About 22 GB of free disk space (the installer checks it before it starts). The
  first build takes one to a few hours, depending on the PC; it runs at low
  priority, so the PC stays usable.

## Install

1. Download the latest `driversf-forza-<version>.zip` from
   [Releases](https://github.com/xob0t/driversf-forza/releases) and unzip it.
2. Put your Forza `.iso` files in its `assets` folder.
3. Close the game and double-click `install.cmd`. Windows may say it can't
   verify the publisher (the zip came from the internet): click **Run**. To
   skip that warning, right-click the zip before unzipping it, choose
   **Properties** and tick **Unblock**.

The first time, it asks for your game folder (the one with `Driver.exe`) and
remembers it. It installs [uv](https://docs.astral.sh/uv/) if you don't have it,
builds every car, backs up your save and every game file it replaces, and
installs the cars. Run it again after an update: only what changed is rebuilt.

## Uninstall

In a terminal in the unzipped folder, `.\install.cmd --uninstall` puts every replaced
game file back.

What the installer changes, your save game, and the known limits:
[INSTALL.md](INSTALL.md).

## Reporting a bug

Reporting a bug: run `.\install.cmd --report` and attach the zip, with a
screenshot and the car's name ([what is in it](INSTALL.md#reporting-a-bug)).

## Acknowledgements

- [DriverNGToolkit](https://github.com/ReHamster/DriverNGToolkit)
  (SoapyMan, ReHamster): DriverNGHook, which runs the registration Lua.
- [driver-tools](https://github.com/Fireboyd78/driver-tools) (Fireboyd78):
  Driver chunk, model and hierarchy formats.
- [carbin-garage](https://github.com/ItsNotPaths/carbin-garage) (ItsNotPaths):
  the Forza carbin format description.
- [ForzaTech-extraction-tools](https://github.com/Doliman100/ForzaTech-extraction-tools)
  (Doliman100): Forza string table and data layouts.
- [Ultimate ASI Loader](https://github.com/ThirteenAG/Ultimate-ASI-Loader) (ThirteenAG).
- [vgmstream](https://github.com/vgmstream/vgmstream): decoding Forza's XMA engine audio.
- [unluac](https://sourceforge.net/projects/unluac/): decompiling the game's Lua.
- [Ghidra](https://github.com/NationalSecurityAgency/ghidra) and
  [Capstone](https://github.com/capstone-engine/capstone): reverse engineering `Driver.exe`.
- Reflections and Ubisoft for Driver: San Francisco; Turn 10 and Playground
  Games for Forza Motorsport 4 and Forza Horizon.

## License

[MIT](LICENSE). The game files and Forza assets are not covered and are not
included.

Developers: see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
