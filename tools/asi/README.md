# dsfforza.asi

One native plugin for the runtime code patches this project needs in
`Driver.exe` 1.04. An ASI loader (one already in the game folder, or the
Ultimate ASI Loader `dinput8.dll` that `tools\install.ps1` adds) loads it from
`<game>\plugins\`. Nothing on disk is edited: every patch is applied in memory
when the plugin loads.

Build (MSVC 2022 x86, static CRT, reproducible with `/Brepro`):

```powershell
.\tools\asi\build.ps1          # -> out\asi\dsfforza.asi + out\asi\dsfforza.ini
.\tools\asi\build.ps1 -Test    # plus out\asi\offline.exe, run against Driver.exe (below)
```

`-Test` builds `test/offline.cpp` with every module (not `main.cpp`) and runs
it: it maps `Driver.exe`'s image at 0x00400000 in a child process (the range
is reserved before the child starts), runs each module's init against it and
exercises the hooks that need no game state. A changed byte at any patch site
shows up there, and in `out\asi\dsfforza.log`, without starting the game.
Another child first changes one byte in three all-or-nothing patch sets
(archive readers, maker groups, card emblem) and checks that nothing of each
set is written.

`tools\install.ps1` copies both into `<game>\plugins\` and removes them on
`-Uninstall`. Design notes and the patch list live in `research/asi.md`.

## Layout

| File | Role |
| --- | --- |
| `src/patch.h`, `src/patch.cpp` | Core: exe check, log, ini, patch helpers |
| `src/main.cpp` | `DllMain`: core init, exe check, runs the modules in order |
| `src/modules.h` | The module list, one `DSF_MODULE(name)` line per module |
| `src/mod_<name>.cpp` | One module; exports `void mod_<name>_init()` |
| `dsfforza.ini` | Default settings, copied next to the asi |
| `test/` | `offline.cpp` (above), in-game test Lua for the sandbox harness, installer and `register.lua` tests |
| `xref.py` | capstone linear sweep of `.text`: instructions with a given displacement/immediate, callers, disassembly |
| `savecheck.py` | dumps `autosave.dat` and the garage module's `dsfforza-garage.dat` |

## Adding a module

1. Create `src/mod_<name>.cpp` with `#include "patch.h"` and
   `void mod_<name>_init() { ... }`.
2. Add `DSF_MODULE(<name>)` on its own line in `src/modules.h`.
3. Add a `[<name>]` section with `enabled=1` to `dsfforza.ini`.

`build.ps1` compiles every `src/*.cpp`; no project file to edit.

## Core interface (`patch.h`)

All addresses are VAs of `Driver.exe` 1.04 (image base 0x00400000, no ASLR).

| Function | Does |
| --- | --- |
| `dsf_log(fmt, ...)` | One printf-style line to `plugins\dsfforza.log` (truncated at start; `[dsfforza] log=0` turns it off) |
| `cfg_int / cfg_bool / cfg_str(section, key, def, ...)` | Read `plugins\dsfforza.ini` |
| `patch_bytes(va, bytes, n, expect = nullptr)` | Write `n` bytes. With `expect`, the current bytes must match or nothing is written (logged, returns false) |
| `patch_jmp(va, target, len = 5, expect = nullptr)` | `E9 rel32` to `target`, NOP-padded to `len` |
| `patch_call(va, target, expect = nullptr)` | `E8 rel32` to `target` |
| `bytes_equal(va, bytes, n)` | Compare without writing |
| `check_bytes(va, bytes, n)` | `bytes_equal` that logs the current and the expected bytes on a mismatch |
| `alloc_exec(n)` | RWX memory for trampolines and relocated tables; never freed |
| `dsf_exe_ok()` | The exe check result (see below) |
| `dsf_plugin_dir()`, `dsf_ini_path()` | Paths |

Rules for modules:

- Always pass `expect` with the original bytes. `dsf_enhanced.asi` and
  `DriverNGHook.asi` patch the same exe; a mismatch must leave the game
  alone instead of corrupting code.
- Check every return value. If a module's patches only work together, apply
  none of them when the first check fails (check every site with
  `bytes_equal` or `check_bytes` first, then patch), and undo the ones
  already written if a later write fails.
- Module init runs inside `DllMain`: no `LoadLibrary`, no waiting on threads.
  Allocate and patch only.
- Log one line per patch applied, and the reason when one is skipped.

## Exe check

`main.cpp` runs no module unless the in-memory PE header matches 1.04:
TimeDateStamp `0x4EC0D92B`, SizeOfImage `0x019C0000`, `.text` at RVA
0x1000 with VirtualSize `0x009C7171`. A hash of `.text` would fail as soon as
another ASI patched code before us, so the header identifies the build and
each patch's `expect` bytes guard its own site.

## Config

```ini
[dsfforza]
enabled=1   ; 0: load but patch nothing
log=1
crash_log=0 ; 1: log the first faults (eip, registers, stack return addresses)

[garage]
enabled=1   ; every module has this switch; main.cpp checks it
max_cars=1000 ; cars per maker group (20: stock cap); groups of 20 or more get a 19-card window
```
