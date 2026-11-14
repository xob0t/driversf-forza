# Web builder: the converter in the player's browser (Pyodide)

Updated: 2026-11-14

Can the converter run in a static web page, in Pyodide, on the player's own files (the
Forza discs and the DSF game folder), so players don't have to install uv and Python
and their files stay on their PC? This note covers the inputs, the dependencies, the
execution model, the outputs, a one-car prototype with measurements, and an estimate.
The prototype scripts are in `tools/web-builder/` ("Prototype" below).

## Answer

**Partly.** The conversion runs in Pyodide today with three small numpy shims, but two
steps don't yet:

- **Runs:** the per-car conversion of a car without `--damage`, end to end: extraction
  from the disc image, host choice, meshes, atlases, lights, cockpit, handling, Forza
  engine sound from decoded WAVs. The garage-picture step runs too. All of it is pure
  Python, numpy and Pillow, and every module of the build path imports in Pyodide 314.0.8
  (Python 3.14.2, numpy 2.4.6, Pillow 12.2.0).
- **Disc reads are no problem.** The ISO, STFS and Forza zip readers already read by
  seek and read. Emscripten mounts the user's files lazily (WORKERFS in a browser, NODEFS
  in Node), so the 8.3 GB discs work unchanged. A car reads 47 to 64 MiB of them.
- **Blocker 1, damage (`--damage`, every default build): wasm32 numpy.** `np.intp` is
  int32 there. The converter's int64 count and index arrays fail numpy's 'safe' casts in
  `np.repeat` and `ufunc.reduceat`, and with those shimmed, `damage.core.Grid.nearest_many`
  returns a different candidate set (an out-of-bounds `reduceat` index). The code assumes
  64-bit `intp`. It needs a 32-bit audit, because there is no wasm64 Pyodide.
- **Blocker 2, engine sound from the discs: vgmstream.** Forza's XMA loops are decoded by
  `vgmstream-cli.exe` (a subprocess, which Emscripten doesn't support). vgmstream builds to
  WASM with Emscripten, but XMA needs its FFmpeg build, and whether that works under
  Emscripten is unverified. Until then a car gets the host's sound (`port_batch`'s
  existing `--no-forza-sound` fallback).
- **Output is close but not byte-identical.** Same size and tree, and 2,658 of 2,661 CHNK
  leaves identical; the diffs are two DXT textures and one vertex buffer. The garage
  pictures are identical, but 822 alpha pixels of the badge glyphs differ. Native builds
  with Pyodide's numpy and Pillow versions match native master byte for byte, so the
  differences come from running in wasm (libm or SIMD paths), not from the package
  versions.
- **Speed and memory.** About 1.6 to 2.9 times native, and memory fits wasm32 for this
  car: a 655 MB wasm heap in steady state, 1.84 GB on the run that builds the sound-bank
  index. Whole-file `mmap` copies dominate it. The largest cars (3.3 GB natively) are an
  open risk against the 4 GB wasm32 limit.
- **Install steps that need native code.** Writing into the game folder works only in
  Chromium (File System Access API). A few install checks need native code (is
  Driver.exe running, the VC++ x86 runtime), and so do the third-party DLL downloads.

## 1. Inputs

What one `port_batch` worker read, from its fingerprint (`work/<media>.fp.json`, which
records every file a worker opens) and from the prototype's byte counts (every read of a
mounted file, counted in `run-car.mjs`):

| Input | Size on this PC | Read per car | Browser access |
| --- | --- | --- | --- |
| Forza discs `assets/*.iso` (FH1, FM4 x2, FH2) | 8.14 GiB each | 47 MiB (warm cache) to 64 MiB (cold: the car's zip extracted), over 4 images, 300 to 550 reads | File/Blob by slice (WORKERFS); fine |
| FM4 DLC packages (16 STFS files) | 2.2 GB | headers only (identification) | same |
| `out/vanilla/Install/dngvehicles.sp` (or the game's own) | 483 MB | 485 MB to 2.36 GB (whole-file `mmap` copies, below) | same; read whole each parse |
| `<game>/Media/WWiseDefault.pck` | 1.19 GB | 1.19 GB once, only when the sound-bank index (`out/cache/sound/vanilla-banks.json`) is built | same; the whole file is copied into the heap (mmap) |
| `<game>/media/SfxMisc.fchunk` | 5 MB | 5 MB | same |
| `out/db/<game>_gamedb.slt` | 2 to 3 MB | sqlite pages | made by `dsff game_data prepare` from the disc (pure Python) |
| `out/lua-src/.../vehiclestats.lua` | small | whole | made by `dsff game_data prepare` from the game's Lua chunk (pure Python, no Java) |
| `out/cache/<game>/<media>/` | about 7 MB per car | whole | written by the worker itself (extraction from the disc) |
| `out/cache` shared files: `host-survey.json`, `dsf-fleet.json`, `damage-host/`, `sound/vanilla-banks.json` | under 1 MB each, `damage-host` 4 MB per host | whole | made from the vanilla archive and game folder on first use (Pyodide made all of them in the cold run) |
| `out/cache/sound/<game>/<bank>/*.wav` | 0.1 to 0.2 MB per loop | the car's banks | **vgmstream output**; the one cache a browser can't make |
| `out/cache/handling-tune.json` | 16 KB | whole | developer-only in-game calibration (9 cars, `TODO(fit, #516)`); a player's build doesn't have it either |
| `research/*.tsv`, `tools/batches/*.txt`, `tools/re/paint_grade.json` | small | some | repo files: ship with the page |
| `out/tools-dl/` (vgmstream, DriverNGHook, ASI loader) | 18 MB | vgmstream: the exe's existence is checked (`engine_sound.py:660`) before the WAV cache is looked at | not runnable in a browser |
| `_ref/` | 37 MB | nothing on the build path (a fallback place for the DriverNGHook archive) | n/a |

The whole `out/cache` on this PC is 14 GB, but nearly all of it is per-car extraction
(fh1 2.3 GB, fm4 7.6 GB), decoded sound (1.6 GB) and damage hosts (2.5 GB). A browser
build would keep these in OPFS (the origin's private file system) or rebuild them.

**ISO reads by slice: already the case.** `formats/xiso.py` reads through `read_at`
(seek and read) and `XIso.open` gives zipfile a seekable view of one file. `formats/stfs.py`
reads blocks, and `formats/forzazip.py` and `lzx.py` are pure Python. Under Emscripten,
`Path(iso).open("rb")` on a WORKERFS mount reads `File.slice()` ranges, so no ISO
refactor is needed. Note that WORKERFS only works in a Web Worker (it uses FileReaderSync).

**What does need slicing: `mmap`.** Emscripten's `mmap` allocates heap and copies the
whole file into it (it reads through `stream_ops.read`). The converter maps two big files:

- `formats/chnk.py` `_mapped`: the 483 MB vanilla archive, every `read_chunk` of a file.
  The steady run copies it once (485 MB). The cold run copied it about five times
  (2.36 GB read), because the first-use surveys parse it again.
- `convert/engine_sound.py` `pck_ids`: the 1.19 GB `WWiseDefault.pck`, each time
  `vanilla-banks.json` is rebuilt. Its key holds the pck's path, so a new mount point
  rebuilds it.

These two copies are why the heap went to 1.84 GB on the runs that build the index and
stays at 655 MB otherwise ("Measurements"). The proposed refactor is in "Proposed
converter changes".

## 2. Dependencies

Third-party packages (`pyproject.toml`, `uv.lock`), as the build path imports them:

| Package | Used by the build path | Pyodide 314.0.8 |
| --- | --- | --- |
| numpy | everywhere | ships (2.4.6) |
| Pillow | `digital_dash`, `dash_scene` (conversion), `gui_assets` (pictures) | ships (12.2.0) |
| threadpoolctl | `gui_assets.one_blas_thread` | ships (3.6.0) |
| rich | `install_display` only (the installer's console) | not needed |
| truststore | `thirdparty` downloads only | not needed (the browser does TLS) |
| lupa, capstone, ruff | test / RE / dev groups | not needed |

Standard library modules the build path imports that Pyodide treats specially: `sqlite3`,
`lzma`, `mmap` and `ctypes` all import (checked in Pyodide). `multiprocessing`,
`concurrent.futures` and `subprocess` import, but processes can't be started.

Native programs and process calls on the build path:

| What | Where | In a browser |
| --- | --- | --- |
| `vgmstream-cli.exe` (XMA to WAV) | `formats/forza_audio.decode_bank`, from `engine_sound._decoded` | **blocker 2.** vgmstream builds with Emscripten (its BUILD.md, "Web"), but XMA needs FFmpeg; a WASM build with it is unverified. |
| worker processes (`python -m dsfforza.build.port_batch _worker`) | `port_batch.run_workers` | a Web Worker per car, each with its own Pyodide ("Execution model") |
| `upvh lint-many` processes | `port_batch` after the merge | can run in-process (`dsfforza.formats.upvh` is pure Python) |
| garage-picture render processes | `gui_assets` `Renders` with `jobs > 1` | `jobs=1` runs in-process; ran in the prototype |
| job pool, job objects, priority (`ctypes`, `msvcrt`) | `build/jobpool.py`, `gentle.py`, `port_batch.lower_priority` | not needed; `lower_priority` is shimmed (no `os.nice` in Emscripten) |
| `git` (`carstore`, `fingerprint`, `package`) | the car store and fingerprints | not needed by a player's build (the store is off outside a checkout) |
| renderers | `game/car_render.py` is numpy only | runs |
| the C++ plugin `dsfcars.asi` | built by MSVC (`tools/asi/build.ps1`) | not built in the browser; the page ships the release's binary |
| DriverNGHook 0.7.2 (.7z), Ultimate ASI Loader | downloaded from GitHub releases (`build/thirdparty.py`), the .7z unpacked by `formats/sevenzip.py` (pure Python) | download and unpack can run in Pyodide; whether GitHub release downloads allow cross-origin fetches is unverified |

## 3. Execution model

`port_batch` runs a process per car (`run_workers`), each holding a slot of the job pool,
and the job pool itself uses file locks and Windows job objects. In a browser:

- **A Web Worker per car slot, each with its own Pyodide.** One bare instance with numpy,
  Pillow and threadpoolctl loaded: 1.5 s to start, 0.4 s for the packages (from the cache),
  a 51 MiB wasm heap and 185 MiB Node RSS. A converting worker's heap is 655 MB for this
  car (steady), or 1.84 GB while the sound-bank index is built. The pool size is the
  browser's job: about `navigator.hardwareConcurrency`, capped by memory, since a worker
  needs 0.7 to 2 GB, and more for large cars.
- **Reuse a worker across cars.** `chnk.read_root` keeps the parsed vanilla archive per
  process, so a worker that takes car after car saves the 485 MB copy and the parse. That
  saving is most of a steady car's reads. Native workers run one car per process
  (`max_tasks_per_child=1`) and lose it.
- **The orchestration** (`port_batch` plan, merge, packages) would run in one more worker,
  or in the page with the per-car work posted to the pool. The prototype runs the worker
  function directly (`port_batch.worker(job.json)`), the same entry point the native
  process uses.
- **No shared file system between workers.** WORKERFS mounts are per worker and read-only.
  Shared caches (`host-survey.json`, `vanilla-banks.json`, `damage-host/`) would go to OPFS,
  which every worker can open. They should be built once, before the workers start, or
  each worker builds its own.

## 4. Outputs

What a build makes and what installs it (`tools/install.ps1`, `build/car_manager.py`):

| Output | Size | Browser |
| --- | --- | --- |
| per-car work chunk `work/<media>.chnk` | 2.2 MB (Beetle) | worker output |
| per-car package `packages/<key>.dsfcar` (zip: car.sp, picture.dds, emblem.dds, badge.png, plate.png) | 2.5 MB (Beetle, PT Cruiser); the 721 cars' work files are 4.4 GiB | `build/dsfcar.py` / `car_package.py` are pure Python |
| merged `dngvehicles.sp` | 466 MB for 2 cars; 4.39 GB for 721 cars, past the 4 GiB format limit | not needed: packages replace it (`research/car-packages.md`) |
| `gui/` (gui.sp, Fonts.fchunk, hud.lua, pictures) | 80 MB (gui.sp is the vanilla 79 MB plus pictures) | ran in Pyodide (`--gui` mode) |
| `dsfcars\cars\`, `dsfcars\generated\` (copies of gui.sp, Fonts.fchunk, global.fchunk), `packages.lua` | about the packages plus 85 MB | `car_manager.py` is pure Python (json, re, shutil) |
| hook, loader, plugin: `plugins\DriverNGHook.asi`, `dinput8.dll`, `plugins\dsfcars.asi` + `.ini` | 1 to 2 MB | plain file copies |
| Lua: `Common\UserLuaScripts\bootstrap.lua`, `dsfcars\register.lua` | small | plain file copies |
| `dsfcars-backup\` (originals, `install.json` with SHA-256 of each file) | | hashing and copying: doable in JS or Python |

**Writing into the game folder:** only Chromium browsers (Chrome, Edge) have
`showDirectoryPicker({mode: "readwrite"})`, and the user has to grant it. Firefox and
Safari can read a folder (`<input webkitdirectory>`) but not write one. There the page
would save a zip of the `dsfcars` tree for the user to unpack. Two points are unverified:
whether Chromium's blocklist of system folders refuses a game under
`C:\Program Files (x86)`, and whether Safe Browsing blocks writing `.asi` and `.dll` files.

**What `install.ps1` does that a page can't:**

- refuse while Driver.exe runs from that folder (`Get-Process`, install.ps1:193);
- check the VC++ 2015-2022 x86 runtime in the registry (install.ps1:278); a page can only
  tell the user;
- the DriverNGHook and ASI loader downloads (CORS, above), unless the user supplies them.

Everything else is file copies, hashes and JSON, which the page can do with the
directory handle.

## 5. Prototype

Scripts (`tools/web-builder/`):

- `run-car.mjs`: loads Pyodide in Node and mounts the repo's `src`, `tools`, `research`,
  `out/db`, `out/lua-src`, a cache folder, `out/vanilla`, `assets/`, `DSF_GAME_DIR` and
  `out/tools-dl` with NODEFS. These are lazy reads, as WORKERFS gives a browser. It
  rewrites a native job (`work/<media>.job.json`) onto those paths and runs
  `web_worker.main` (or with `--gui <build>`, `web_worker.gui`). It counts the bytes read
  per file and writes `web-stats-js.json`: times, the wasm heap, Node's peak RSS and the
  reads.
- `web_worker.py`: runs inside Pyodide. It applies the shims, calls
  `port_batch.worker(job)` and puts the worker's log where the native parent puts it.
- `pool_run.py`: runs any command in a job-pool slot and reports its peak commit
  (`PeakProcessMemoryUsed` of jobpool's job object, the measure `port_batch.WORKER_RAM` uses).
- `seed_cache.py`: copies the `out/cache` files a native car read (its fingerprint) into a
  new folder (warm run), or only the decoded WAVs (`--only-wav`, cold run).
- `chnk_diff.py`: the CHNK leaves of two files that differ.

Steps (from the worktree; Pyodide is installed under `out/`, not in the repo):

```
npm install --prefix out/web-builder pyodide@314.0.8
uv run dsff port_batch build --out out/native-nodmg --car VW_Beetle_04@fh1 --no-store --jobs 1 --port "--mdxn-limit 0x2C0000"
uv run python tools/web-builder/seed_cache.py out/native-nodmg/work/VW_Beetle_04.fp.json --cache out/cache --to out/web-builder/cache-cold --only-wav
uv run python tools/web-builder/pool_run.py --label web-cold -- node tools/web-builder/run-car.mjs \
    --job out/native-nodmg/work/VW_Beetle_04.job.json --cache out/web-builder/cache-cold --out out/web-builder/run-cold
uv run python tools/web-builder/chnk_diff.py out/native-nodmg/work/VW_Beetle_04.chnk out/web-builder/run-cold/work/VW_Beetle_04.chnk
```

The car: **VW_Beetle_04 (FH1)**, among the fastest of the roster to convert
(`out/cache/car-seconds.json`). Its host is 258 and its engine sound comes from the
Forza disc (4T2_Esprit_Exh, 9 loops). It was built without `--damage` because of
blocker 1. With `--damage` (the default port options), the native worker takes 16 s; in
Pyodide it stopped in `damage.uv.lookup_many` after 15.9 s.

### Shims (each one is a converter finding)

`web_worker.py` changes nothing in the converter. It wraps only:

1. `port_batch.lower_priority`: there is no `os.nice` in Emscripten, and a Web Worker has
   no priority.
2. `np.repeat(a, repeats)` with int64 `repeats`, in `trim_art.triangles_cells` (`size` is
   `np.int64`): TypeError "Cannot cast array data from dtype('int64') to dtype('int32')
   according to the rule 'safe'". The shim casts to `intp`.
3. `np.add.reduceat` / `np.minimum.reduceat` with int64 `indices`, in `lights._texels`
   (`at`) and `damage.uv.lookup_many` (`first`): the same TypeError. The shim casts.

With all three in place, the `--damage` run still fails: `IndexError: index 720
out-of-bounds in minimum.reduceat [0, 720)` in `damage.uv.lookup_many`. An item there has
no candidates from `damage.core.Grid.nearest_many`, which natively always gives some. That
function mixes int64 arrays with `intp` results (`np.argmax`, `np.searchsorted`,
`np.unique(return_inverse=True)`, `np.bincount`) and an `np.iinfo(np.int64).max`
sentinel; where it goes wrong on wasm32 is not isolated yet. The converter has 147
explicit `int64`s under `convert/`, `build/` and `formats/`, and 95 calls of
`repeat`/`bincount`/`add.at`/`cumsum`.

### Harness setup found on the way

- `out/lua-src` (vehiclestats.lua) has to be there, or `host_select.survey` knows no
  vanilla car's model or engine and picks another host (275 instead of 258, score 36
  instead of 84). A player's install makes it (`dsff game_data prepare`); a web builder
  must too.
- `engine_sound` asks for `vgmstream-cli.exe` before it looks at the WAV cache
  (`fa.vgmstream_path` at engine_sound.py:660, `_decoded` at :501). Without the exe, cached
  WAVs aren't used and the car keeps the host's sound. The prototype mounts `out/tools-dl`
  only so that check passes. The exe never runs: the WAVs are complete in the cache.
- `_decoded` takes a bank's folder only with one WAV per subsong. A car may use fewer
  (8 of 9 here), so `seed_cache.py` copies whole folders.

### Measurements

Same job (VW_Beetle_04, spool ID 1, `--mdxn-limit 0x2C0000`, no `--damage`), each run in a
job-pool slot, 2026-11-14, Ryzen 5800X3D (16 threads) with other agents' VMs running:

| Run | Worker time | Peak commit | wasm heap at the end | Bytes read |
| --- | ---: | ---: | ---: | ---: |
| native, master venv (numpy 2.5.4, Pillow 13.0.0), inside `port_batch build` | 9.0 s | not measured | | |
| native, numpy 2.4.6 + Pillow 12.2.0, standalone `_worker` | 7.4 s | 830 MB | | |
| Pyodide, cold (empty cache but the WAVs: extraction, fleet, host and sound-bank surveys) | 21.9 s | 3.08 GB | 1.84 GB | 3.75 GB |
| Pyodide, warm cache, sound-bank index rebuilt (new pck path) | 21.1 s | 3.08 GB | 1.84 GB | 3.22 GB |
| Pyodide, warm cache, steady | 14.3 s (+1.7 s start) | 1.38 GB | 655 MB | 564 MB |

The garage-picture step for the two cars of `out/native` (`gui_assets.build`, jobs=1,
in-process):

| Run | Time | Peak commit | wasm heap |
| --- | ---: | ---: | ---: |
| native, numpy 2.4.6 + Pillow 12.2.0 | 6.5 s | 861 MB | |
| Pyodide | 18.7 s | 988 MB | 612 MB |

So the conversion runs at about 1.9x native in steady state (14.3 / 7.4 s) and 3x on the
runs that rebuild the caches, and the pictures at 2.9x. "Peak commit" for Pyodide is
Node's whole process, wasm heap included.

### Byte identity

| Output | Native vs Pyodide |
| --- | --- |
| worker log | identical (but one timing) |
| `work/VW_Beetle_04.chnk` | same size (2,252,800 bytes) and tree; 2,658 of 2,661 leaves identical. Different: the car's DXT5 atlas (8,471 of 21,760 blocks), a DXT1 texture (46 of 21,845 blocks), one vertex buffer (1,731 of 47,782 words, all in one packed attribute at stride 0x1c) |
| garage pictures (`dsfforza_*.png`), `hud.lua`, `global.fchunk` | identical |
| `vehicles_font.png` and so `Fonts.fchunk`, `gui.sp` | 822 alpha pixels in the badge glyph row (y 502 to 532) |
| `gui-assets.json` | line endings only (CRLF natively) |
| cold run vs warm run in Pyodide | identical |

A native run with Pyodide's numpy (2.4.6) and Pillow (12.2.0) gives the master build's
chunk (sha256 `287950d3...`) and gui files byte for byte. The differences therefore come
from wasm, not from the package versions: libm (musl versus the MSVC runtime), numpy's
missing SIMD dispatch, or 32-bit integers. The source isn't isolated, and neither is how
large the texture and vertex differences are. To start: the atlas before DXT compression,
and the packed attribute's encoder. Until they match, a web build is a different platform
for the car store (fingerprints already record `platform` and the numpy version).

## 6. Estimate

**Time per car and per roster.** The roster's recorded worker times (`out/cache/car-seconds.json`,
721 cars, with damage, under full-roster parallel load) total 16.3 worker-hours (mean
81 s, median 79 s). At 1.9x that is about 31 worker-hours in Pyodide, and up to 3x on the
first cars while the shared caches are built. A browser can run as many workers as its
memory allows: at 1.4 to 3 GB of commit per worker, that is 4 to 6 on a 16 GB PC, so 5 to 8
hours for the whole roster. A 20-car subset takes 10 to 20 minutes. The pictures add about
9 s per car (3.25 s native, jobs=1, times 2.9). These are extrapolations from one small
car. The large cars (VIP_Viper_13: 3.26 GB natively) need measuring against the 4 GB
wasm32 heap, after blocker 1 is fixed.

**Work to a usable web builder (a subset of cars):**

1. Make the numpy code 32-bit clean (blocker 1): cast count and index arrays to `intp`,
   find the `nearest_many` divergence, and add a CI job that runs two or three cars in
   Pyodide under Node (this prototype) so it stays clean. This is the largest piece,
   and it is in the converter.
2. Remove the whole-file `mmap` copies (see below), to keep the heap well under 4 GB.
3. The page: file pickers for the discs and the game folder (or the two files it reads),
   a Web Worker pool with one Pyodide each, the job list (port_batch's plan), packages
   and pictures from the pool, a zip download, and OPFS for the caches. Engine sound:
   the host's until blocker 2 is solved (`--no-forza-sound` is already a fallback).
4. Install: write into the game folder in Chromium, a zip elsewhere. The one-time native
   part (DriverNGHook, ASI loader, `dsfcars.asi`, the VC++ check) stays with
   `install.cmd`, which can take a finished build (`-Build`).

**For the full roster:** add vgmstream with FFmpeg (XMA) as WASM, or another XMA decoder,
for blocker 2; check the largest cars against the 4 GB heap; and decide whether
web-built cars must match native byte for byte (find the wasm float differences) or are
accepted as their own platform, checked with `render_compare`.

**Must stay native:** building `dsfcars.asi` (MSVC; the page ships the release's binary),
the Driver.exe-running and VC++ runtime checks, and in Firefox and Safari, the writes
into the game folder.

## Proposed converter changes (not made here)

- **No whole-file mmap on the build path.** `chnk._mapped` already falls back to seek
  and read when `mmap` fails. Under Emscripten it should take that path, or `read_chunk`
  should read the root and entry headers by ranges. `engine_sound.pck_ids` needs only the
  AKPK header, the lookup tables and each bank's chunk headers, so it can read those by
  seek. Together these cut the heap from 1.84 GB to under 0.2 GB plus the conversion
  itself, and the cold reads from 3.75 GB to well under 1 GB.
- **Integer width.** Use `np.intp` for counts and indices (or cast where they meet
  `repeat`, `reduceat`, `bincount` and indexing), and test on wasm32.
- **Engine sound cache first.** `engine_sound` should use complete cached WAVs before it
  needs `vgmstream-cli` (engine_sound.py:660).
- **`vanilla-banks.json` key.** Key the pck by size and mtime only, not its path (as the
  archive is), so a new mount point doesn't rebuild the index and copy the 1.19 GB file.
