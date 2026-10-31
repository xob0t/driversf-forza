# Native recompilations of Forza as a runtime probe and reference renderer (spike)

Updated: 2026-10-31

Four native PC recompilations of Forza exist (statically translated PowerPC code, a Vulkan or
D3D12 renderer for the Xenos GPU commands). This spike checks four things:

- whether they run our discs on this PC without taking over the user's screen;
- whether they can read what the game uploads per draw (the runtime half of an RE round);
- what that says about RE round 4 (`forza-lamps-blur-re.md`, #423);
- whether they can render Forza reference pictures.

The recompilations are external tools and nothing of them is in this repo. They are built on
this PC by their own setup, from our disc, under `out/recomp` (ignored by Git), and driven by
`tools/re/forza_recomp.py` through their documented interfaces. Evidence tags are as in
`forza-lenses-re.md`:

- `confirmed-static`: read in code or data;
- `confirmed-runtime`: seen in the running game (here: the recompiled FM4);
- `inferred`.

| Recomp | Game | Licence | Renderer | Discs it accepts |
| --- | --- | --- | --- | --- |
| [i2kiselev/fm4-static-recomp](https://github.com/i2kiselev/fm4-static-recomp) v0.3.0 | FM4 | BSD-3 | ShiftGlue (ReXGlue fork), Vulkan | disc 1 1.0 USA or PAL, by `default.xex` SHA-256 |
| [arcanite24/pinyon-shift](https://github.com/arcanite24/pinyon-shift) v0.7.1 | FH1 | BSD-3 | ShiftGlue, Vulkan | USA MS-2505 only, by ISO SHA-256 or every extracted file's |
| 0herculabs/hl360-forza4 v0.3.9 | FM4 | none | D3D12 | USA Rev1 |
| Alexbeav/fm4-recomp v0.1.0-alpha.2 | FM4 | GPL | ReXGlue | EU GOTY |

Neither BSD project ships a game executable. Each "release" is a launcher that downloads a
toolchain and compiles the game from the player's disc. "Using the release" here means its
tagged source and its own `setup` script, run unmodified.

## Summary

- **FM4 works.** Our DVD1 is fm4-static-recomp's "1.0 USA"; DVD2's four content packages install.
  Its own `setup.ps1` built it at Idle priority with 2 jobs (section 3). One launch reaches the
  main menu in about 20 s and a race grid in about 90 s.
- **FH1 does not, today.** Our FH1 disc is not the dump Pinyon Shift supports (section 1). Its
  build also needs a system-wide Visual Studio Build Tools install, which this spike did not
  make (section 1.1).
- **Unattended runs work, on a desktop of their own** (section 3): a hidden window on a
  separate desktop, scripted input, guest-output captures, and a watchdog. The first runs of this
  spike showed the game window on the user's screen; section 3.1 says how and what prevents it.
- **Per-draw constants work** with the Vulkan backend (section 2): every draw's shaders, textures,
  blend and every constant its shader reads, for one whole frame on request. `gpu_backend=null`
  logs no constants.
- **#423, checked at runtime on FM4** (section 4):
  - confirmed: the lamp colour upload, the brake state, the default blend, the blur constants
    and the shared tyre settings file;
  - corrected: which wheel technique a race draws. Races draw the `_V2` wheel techniques, so a
    wheel archive's `rim_V2`, not its `rim`, sets the race rim. The Chevelle 70's race rim is
    0.65, not #423's 0.175.
  - not answered: the FH1 day/night lamp question, because FH1 doesn't run.
- **Reference pictures:** FM4 captures at the game's own cameras work (showroom, race chase
  camera, braking). They can check `render_compare`'s technique models and lamp states; they
  can't replace its reconstructed reference pixel for pixel (section 5).

## 1. Our discs (confirmed-static)

`uv run python tools/re/forza_recomp.py discs` (the XEX2 execution info and the SHA-256 of
`default.xex` inside each ISO):

| ISO | Title, media, version | `default.xex` | Accepted by |
| --- | --- | --- | --- |
| `Forza 4 [NTSCU][DVD1].iso` | 4D530910, 26FF1101, 0.0.0.8, disc 1/2 | `3618E85A...` | **fm4-static-recomp "1.0 USA"** |
| `Forza 4 [NTSCU][DVD2].iso` | 4D530910, 0F5C87F9, disc 2/2 | | its four content packages: `--fm4_disc2_packages` |
| `Forza.Horizon.XBOX360.iso` | 4D5309C9, **2B7A1346**, 0.0.0.7, region free | `B95C1E02...` | **none**: Pinyon Shift wants media 2DC7007B, 0.0.0.10 (`DB40DF60...`) |

Our FH1 disc is not the USA MS-2505 dump Pinyon Shift supports (ISO `BE910E6D...` against
`83DFC7D0...`). Against Pinyon Shift's catalogue of the supported disc's 2,400 files
(`config/supported-extracted-ms-2505.json`), our ISO:

- has 35 files it lacks and lacks 21 (other radio and voice languages);
- has the same SHA-256 on 2,362 of the 2,368 common files under 60 MB;
- differs in the three executables (`default.xex`, the two facades: same sizes) and three
  FMV `.def` files.

So the game data is the same release and the executables differ at least in their headers
(media id, version, region). There are signs that the code is the same:

- Pinyon Shift's symbol table (`config/mod/fh1-symbols.toml`) addresses fall on our image's
  function starts (33 of 40);
- `time_of_day.from_world` 0x82486CF0, documented as `[world + 232]`, is `lwz r3, 0xE8(r3)` in
  our image.

Not proven. Pinyon Shift hashes the folded `.rdata` bytes of the image at load and refuses a
different image, so a build from our disc would show it.

### 1.1 What FH1 would take

1. **A supported dump, or a build past the dump check.** `setup-preview.ps1` accepts only the
   MS-2505 ISO, or an extracted folder whose every file matches the catalogue. Our files don't
   match (the executables, languages and FMVs differ), so the check has to be bypassed. That
   means putting our extracted disc in `.local/game/base` and running `build-preview.ps1` and
   `prepare-fh1-shaders.ps1` directly, which is a local change to their setup. Whether the
   recompiled code then runs is exactly the `.rdata` test above.
2. **Visual Studio.** Pinyon Shift's `Enter-PinyonBuildEnvironment` runs `VsDevCmd.bat` from
   an installed Visual Studio 2022 C++ Build Tools (17.1 or newer); its setup installs them when
   missing. That is a system-wide, administrator install, and this spike did not make it.
   fm4-static-recomp instead downloads a portable MSVC 14.44 runtime and Windows SDK into its own
   folder. Pointing Pinyon Shift's build at those would be a second local change, untested.
3. **Cost.** About 30 GB and a 20 to 60 minute build at full speed (several hours at Idle with
   2 jobs), as for FM4.

Pinyon Shift has what the FH1 questions need: a trainer with time of day
(`time_of_day.request`), a free camera (`camera.set_mode` 6), and a scripted render-test
harness with vehicle poses and guest-output captures.

## 2. What the debug options log (confirmed-static, ShiftGlue `72b7dd6f`)

ShiftGlue's command processor (`src/graphics/command_processor.cpp`,
`CommandProcessor::ExecuteDrawRecord`) logs per draw, before the backend's `IssueDraw`:

- **`--fh1_debug_log_draws`**, for each draw:
  - its index, primitive and index count;
  - the vertex and pixel shader hashes;
  - the render target, `RB_BLENDCONTROL0`, depth control and scissor;
  - for each shader, its texture fetches (address, size, format) and every float constant it
    reads (`pc=[c<n>:x,y,z,w ...]`, `vc=[...]`, in the shader's own register numbers; c0..3 and
    c252..255 for a shader that indexes dynamically).

  The option is "restart only", but the command processor reads it on every draw, and the
  scripted input's `SET:` token changes it live (`cvar.cpp` sets it and only marks a restart as
  pending). `forza_recomp.py`'s `frame` step switches it on for 0.5 s and keeps the first whole
  frame: from a draw 0 to the next draw 0.
- **The one-frame trigger** (`--fh1_resolve_dump_dir=<dir> --fh1_resolve_dump_frame=-1`, then a
  file `trigger` in `<dir>`) logs a frame only from its first resolve on. In a race that is after
  the cars, so it is not used for constants.
- **`--fh1_debug_log_ps=<hash>`**: every draw with that pixel shader, but only pixel constants
  c0..c7. Forza's car material constants sit at c80..c98.
- **`--fh1_debug_log_vs=<hash>`**: in a logged frame, that vertex shader's nonzero constants and
  vertex fetches 90..95.
- **Overrides and dumps:** `--fh1_debug_vs_constant=<hash>:c<n>=x,y,z,w` replaces a vertex
  constant (and `pixel_shader_constant_override.cpp` does pixel constants);
  `--fh1_debug_dump_range=<draw>:<address>:<length>` writes guest memory before a draw.

**Shader hashes (confirmed-runtime).** The hash is XXH3-64 over the microcode as guest memory
holds it, big-endian dwords as stored in the `.fxobj` (`vulkan/pipeline_cache.cpp`,
`LoadShader`).

- `forza_recomp.py hashes` computes it for every technique of every effect under FM4's
  `Media/Shaders` (627 shaders), with the constant names of each shader's table.
- `forza_recomp.py draws` names a logged frame's draws and constants with it.
- Every car draw of the logged frames maps. The unmapped ones are the tracks' own shaders.
- Several techniques can share one pixel shader (`rim`, `rim_V2`, `rim_bump_metal`); only the
  constants tell them apart.

**`gpu_backend=null` logs no constants (confirmed-static).** The null backend
(`src/graphics/null/graphics_system.cpp`) still runs every PM4 packet through the command
processor, but its `LoadShader` returns no shader, so a draw has no active shader. The
`log_ps` test needs `active_pixel_shader_`, and the draw log lists fetches and constants only
for analysed shaders. A null run logs draw counts and register state, no hashes and no
constants. Constants without a GPU would need the null backend to keep the microcode (a change
to ShiftGlue).

## 3. Running it unattended

### Build (once)

`out/recomp/fm4-v030` is the release tag's source. The build was:

```powershell
powershell -File out\recomp\fm4-v030\tools\setup.ps1 -GameRoot <abs>\out\recomp\fm4\disc1 `
    -StateRoot <abs>\out\recomp\fm4\state -Parallel 2 -AcceptMicrosoftLicense
```

It was started at Idle priority; its compiler processes inherit Idle.

- `-AcceptMicrosoftLicense` lets it download MSVC 14.44's runtime and the Windows SDK into its
  own `.local` folder, under Microsoft's licences. No system install is made.
- It downloads pinned CMake, Ninja, LLVM 20 and Python, and builds the ShiftGlue SDK (727 units)
  and the translated game (1,218 units).
- It builds a shader cache from the disc: 963 shaders, 4,819 pipelines.
- About 40 minutes at Idle with 2 jobs on this PC; 30 GB with the extracted discs.

`uv run python tools/re/forza_recomp.py extract` writes disc 1 and the disc 2 packages. Disc 2 is
installed into the save by the first launch with `--fm4_disc2_packages=<abs>\out\recomp\fm4\disc2pkg`.

**Profile.** A fresh profile starts in FM4's intro race. The recomp's shader warm-up profile
(`config/warm-up/user/B13EBABEBABEBABE` in its source) skips it and goes to the main menu.
Copy it into `out/recomp/fm4/state/user`.

### Runs

```powershell
uv run --with xxhash python tools/re/forza_recomp.py run tools/re/recomp_routes/fm4-batch.route out/recomp/runs/<name> -- --fm4_log_file_opens=true
uv run --with xxhash python tools/re/forza_recomp.py draws out/recomp/runs/<name>/frames/<frame>.log --technique "tail_light/"
```

A route is a text file of steps: `press`, `hold`, `until <controls> <regex>` (press until the
log shows a line), `wait`, `sleep`, `set`, `shot`, `frame`. Screens are known by the strings
the game looks up (`--fm4_log_strings`): the recomp's render tests name them
(`tools/render_tests/routes.py`). Manufacturers sit in alphabetical order after View Garage
(Abarth 1, Chevrolet 13, Ferrari 21, Ford 23, Holden 26, Joss 33, Lamborghini 36). A car is
found by pressing RIGHT until its display name is logged. The routes in
`tools/re/recomp_routes/` are:

- `main-menu.route`: a smoke test, about 25 s;
- `fm4-batch.route`: four showrooms and one race, about 3 min;
- `fm4-wheel-layer.route` and `fm4-sesto-race.route`: the wheel checks of section 4.

Each run writes to its folder:

- `logs/runtime.log`: the log. ShiftGlue rotates at 5 MB and flushes only on a full buffer by
  default, so the runs pass `--log_max_file_size_mb=4000 --log_flush_interval=1`;
- `shots/<name>.png`: the guest output (the game's frame, before any window scaling), requested
  by writing `<script>.shot`;
- `frames/<name>.log`: one whole frame's draws.

The save and the shader cache are shared in `out/recomp/fm4/state`. Runs go one at a time; the
game process is at Idle priority and in a job that ends it with the script.

### 3.1 Never on the user's screen (the window incident)

**What happened.** The first runs of this spike put the game's window on the user's screen.
The first two used a "start minimized" mode: SDL shows its window itself, and the script
minimized it only afterwards and not again. The later ones were meant to set
`REX_WINDOW_HIDDEN=1`, ShiftGlue's switch that skips `SDL_ShowWindow` (`window_sdl.cpp`; the
recomp's own shader warm-up uses it). But the edit that added it to the environment was a plain
text replace that matched nothing after the formatter had reflowed the line. Nobody checked,
so every one of those runs showed a normal window. The user saw it.

**What every run does now** (`forza_recomp.Game`):

- **Its own desktop.** The game starts on a desktop of its own (`CreateDesktopW`, then
  `STARTUPINFO.lpDesktop = WinSta0\dsff-recomp`), which is never switched to. Windows displays
  only the input desktop, so nothing the game opens can appear on the user's screen or take
  their focus or input, whatever SDL calls. If the desktop can't be made, it doesn't launch.
- **Hidden window.** `REX_WINDOW_HIDDEN=1` is in the game's environment as well.
- **Watchdog.** A thread looks every 50 ms for any top-level window of the game's process on the
  user's desktop (`EnumWindows` lists the calling thread's desktop). If one appears, it ends the
  game and the route fails. Every run ends by printing whether the watchdog saw one.
- **Scripted input only.** The recomp's scripted-input mode replaces the input system with its
  scripted pad and keyboard drivers (`src/scripted_input.cpp`, `CreateInputSystem`), so the
  user's keyboard and pads never reach the game.

**Checks.**

- Without the game: a visible window (WS_VISIBLE) created by a thread attached to such a desktop
  is listed on that desktop and not on the user's.
- With it: all five launches since the fix ran on their own desktop, with captures identical to the
  earlier ones (main menu: mean difference 0.0) and no window on the user's desktop.

**Rules for anyone changing this:**

- Don't add a "minimized" or "visible" mode.
- Don't call `SwitchDesktop`.
- Check after any edit to `Game.__init__` that `REX_WINDOW_HIDDEN` and `lpDesktop` are still
  set (grep them).
- If the watchdog ever fires, stop launching and find out why.

Rendering on a non-displayed desktop works: Vulkan makes its swapchain and the guest output
captures are complete.

## 4. Runtime results (FM4)

Runs (out/recomp/runs, not kept in the repo):

- `jt1`, `chevelle`, `commodore88`: before the fix, visible;
- `batch1`: showrooms of the Chevelle 70, #88 Commodore, JT1 and Focus RS 09, then a race with
  the Focus RS;
- `wheel-layer`: the 599 GTO and Sesto Elemento showrooms;
- `sesto`: a race with the Sesto Elemento.

All races are at Bernese Alps, the first environment, by day, with the chase camera. The values
are from `forza_recomp.py draws`.

| What | Prediction (#423 / `forza-lenses-re.md`) | Runtime | |
| --- | --- | --- | --- |
| `tail_light` `BrakeColor` uploaded (c89) | 2.0 in `Normal.xml`, sRGB to linear on upload: 4.953846 | 4.95385 on every car without its own value | confirmed |
| other uploads | 3.6 → 19.7; 0.6 → 0.32; TOY_MR2SC_89 (131, 1, 1) | 19.7299 (`tail_light_on`: 3.6, a 1, `PaintScale` 1); 0.318548 (`tail_light2`: `PaintScale` 0.01); 131.447, 1, 1 on an AI car | confirmed |
| `AmbientColor` 0.1 | 0.0100228 linear | 0.0100228 | confirmed |
| `tail_light` `PaintScale`, `CubeScale`, `GlassMaxOpacity` | 0.3, 0.9, 1 (`Normal.xml`) | 0.3, 0.9, 1 | confirmed |
| `reflectionScaler` | 1 | 1, 1, 1, 1 | confirmed |
| FM4 tail glow state `brakeReverseAmount.x` | brake | 0 on the grid and driving; exactly 1 with the brake held (JT1, Chevelle, #88) | confirmed |
| FM4 tails by day | no running state (only the brake mask) | the grid captures show the JT1's and Focus RS's tails dark, red under the brake | confirmed |
| default blend of passes that set none | `SRCALPHA, INVSRCALPHA` | `RB_BLENDCONTROL0` 0x01060706 on `tail_light`: colour src 6 (SRC_ALPHA), dest 7 (ONE_MINUS_SRC_ALPHA) | confirmed |
| `blurRimGeomTexBlend` c40, `blurRimTexBlendAmounts` c56 | (step, frac(3t), t, 0.3t); weights placing picture 3t | stopped: (0,0,0,0), (1,0,0,0); turning: (1, 0.2023, 0.0674, 0.0202), (0.7977, 0.2023, 0, 0); at speed: (1, 0, 1, 0.3), (0, 0, 0, 1) | confirmed |
| a wheel's shared settings | `Tire/Normal.xml`, not `Normal.xml` | race `rim` 0.65, `inner_rim` 0.4, `blur_rim` 0.65 (`Normal.xml`: 0.75, 1.0, 1.0) | confirmed |
| the wheel archive's own `ShaderSettings.xml` | read after `Tire/Normal.xml`, its `rim` sets the rim | read; but races draw `<technique>_V2`, so its `rim_V2` sets the race rim. Showrooms draw the plain techniques, then `Tire/ui.xml` | **corrected**, below |

**Which wheel technique is drawn (confirmed-runtime, new).**

- **Races draw the `_V2` wheel techniques.** The Sesto Elemento's archive sets `rim` (PaintScale
  2.0, SpecularPower 1.75, CubeScale 0.4) and `rim_V2` (2.15, 3.0, 0.2). Its race `rim` draws
  upload 2.15, 3 and 0.2. The race `inner_rim` draws upload `CubeScale` 1, which is
  `Tire/Normal.xml`'s `inner_rim_V2` (plain `inner_rim` has 0).
- **The Chevelle 70's archive sets `rim` only** (0.175), so its race rim is `Tire/Normal.xml`'s
  `rim_V2`, 0.65. That was measured on its own wheel draws, next to its braking tail lamps.
- **Showrooms draw the plain techniques, and `Tire/ui.xml` is laid over them:** `rim` 0.8 on every
  car, whatever the archive says. `blur_rim` there takes the archive's value (599 GTO 0.3, Sesto
  2.0).
- **#423 section 2.5's survey therefore reads the wrong section.** The in-game rim is
  `Tire/Normal.xml`'s `<technique>_V2`, overlaid by the archive's `<technique>_V2`. Of FM4 disc
  1's 30 archives with their own settings, 11 have `_V2` sections. FH1's `Tire/Normal.xml` has
  the same `_V2` sections, and 29 of its 37 such archives set `_V2` sections. That FH1 races draw
  `_V2` too is inferred, not run. Comment on #424.

**Not answered here:**

- FH1's `dynamicLights2.x` by day (#423 "Open"). FM4 has no such state: its tails glow only
  under the brake.
- The FH1 Focus RS500. FM4 has only the Focus RS 09, whose `tail_light` follows the FM4 rule
  above.

## 5. Reference pictures and `render_compare`

What a capture is: the guest output at 1280x720, the game's own frame with its HUD, at the
game's own cameras.

- **Showroom (colour select):** the car at a front three-quarter view in the studio, slowly
  drifting, with the UI settings layer (`Tire/ui.xml`, `ui.xml`).
- **Race:** the chase camera from behind, which is the rear view the RS500 question wants.
  BACK cycles the cameras: chase far and near, cockpit, hood, bumper.
- **Lamp states:** braking (hold LT), driving.

FM4 has no time of day: its tracks are fixed daylight. The recomp has no camera control of its
own, and its options list none.

**Can it replace `render_compare`'s reconstructed Forza reference?** Not pixel for pixel:

- `render_compare` draws both sides with one lighting model, at its own cameras, so that what
  differs is the conversion.
- The recomp draws Forza's lighting, track or studio, HUD, and its own cameras.

**What it can check** is the reference's technique models, per technique, against the game
itself: the constants the reference assumes (`gloss_params`, lamp colours, glass opacities,
wheel settings: section 4 found one wrong), the lamp glow states, and the look of a lamp cluster
or a wheel at the game's own frame.

**To automate a car, view and time of day:**

- **Car:** a route per manufacturer and display name (the gamedb's `Data_Car` holds the string
  ids; the logged strings give the names). About 30 s per extra car in one launch.
- **View:** the showroom (one fixed framing), or a race (the chase camera, grid or driving).
  Fixed framings beyond those need camera control: FM4's photo mode (untested), or a guest hook,
  which would be a change to the recomp.
- **Time of day:** none in FM4. FH1 (Pinyon Shift's trainer) when it runs.
- **Matching our renders** would need either our renderer at the game's camera (from the draw
  log's vertex constants), or the recomp's frame with the same car at a known pose. Neither is
  built.

## 6. Plan

**For RE rounds**, use it as the runtime half of a round, to confirm or correct each decoded
rule at the constants the game uploads:

1. Write the prediction from the data and the decoded maths first.
2. Run a route to the scene (a showroom, a grid, braking, driving).
3. Log one frame (`frame`).
4. Read the technique's constants (`draws --technique`).

A round's check costs a few minutes and no VM. Next uses:

- the lamp glass techniques' constants (`GlassColor`, `GlassMaxOpacity`) on the clear-glass
  cars;
- the paint constants (`paint-re.md`);
- `--fh1_debug_dump_range` for vertex data where the carbin parsing is in doubt.

**As a reference renderer:** add showroom and grid captures to `render_compare`'s sheet as a
third column, "Forza, in game", for the cars of a check (#434). Score nothing against it; use it to
review the reconstructed reference's technique models.

**FH1:** see 1.1 and #433. The useful first step is the `.rdata` test of our disc. It needs
the Visual Studio decision.

## Open

- The FH1 build (section 1.1): a supported dump or a bypass of the dump check, and Visual
  Studio Build Tools or a port to the portable toolchain.
- FH1's wheel techniques in races (`_V2`, inferred from FM4 and the data).
- FM4 `tail_light_on`: which cars draw it, and when (seen on one AI car per grid, `BrakeColor`
  3.6 with a = 1).
- Photo mode or another fixed camera for references.
