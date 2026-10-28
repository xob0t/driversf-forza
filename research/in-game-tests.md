# In-game tests

Updated: 2026-10-26

Runs up to 2026-10-22 used the persistent Windows Sandbox harness in
`tools/wsb/` (see the README); later ones run on the host (`-OnHost`) or in
the headless VM (`-Vm`), both below. All of them: the vanilla game folder
mapped read-only, DriverNGHook 0.7.2, the
probe in `tools/game/lua/`, and the player's own save. Run folders are under
`%LOCALAPPDATA%\Temp\dsfforza-wsb\runs\`.

## Host runs (`run-series.ps1 -OnHost`, from 2026-10-23)

On Windows build 26300.9711 the sandbox guest bugchecks (0x3B) on its first
DirectX use (microsoft/Windows-Sandbox #131/#132), so runs from
`host-20261023-*` on are on the host while nobody uses the PC. Same probe and
run script (`run-test.ps1 -OnHost`); what differs:

- the run folder is `D:\dsfforza-run` (on the game's volume: hard links
  instead of symlinks, which need admin);
- the Driver profile in Documents is backed up to
  `dsfforza-wsb\host-session` and switched to a 1280x720 window for the
  series, then restored file by file and compared by hash (the folder itself
  can be held briefly by a scanner, so it is never deleted);
- the default speakers are muted for the series and their state restored;
  profile and speakers come back while `out\wsb-pause` holds the runs, and
  a series that is killed (which once left the profile windowed and the
  speakers muted) is undone by a watcher process once its game has closed
  (`tools\wsb\host-session.ps1`, checked by `test-host-session.ps1` on a
  fake profile); by hand: `.\tools\wsb\host-session.ps1 Restore`;
- keys only go to the game: a press is dropped (`keysSkipped` in
  result.json) unless the game window is in the foreground, so nothing types
  into the user's apps;
- an elevated window in front (here the Throne VPN client) keeps the
  foreground from every medium-integrity process, injected clicks included;
  the shell's MinimizeAll reaches it, so the harness calls it before launch;
- Windows Firewall asks once per program path. The first launch of
  `D:\dsfforza-run\Driver.exe` raised the prompt; it is protected (no input
  from a non-admin process), dims the whole screen and stays until a person
  answers (it followed the screen when a monitor turned off). Once answered,
  the path has inbound rules and no prompt comes back, so keep
  `-HostRunPath` fixed. Meanwhile the harness puts the game window on a
  secondary monitor (topmost) when there is one and screenshots that
  monitor. The borderless window covers the monitor with the 1280x720 image
  centred; `out/scratch/sheet.py` crops to it.
  With a single monitor (2026-10-23: one 3440x1440 screen) the prompt sat
  over the chase car in every shot (runs `host-20261023-084934`, `-085115`);
  the harness now moves the game window to the primary screen's top-left
  corner then, clear of the centred prompt (`run-test.ps1`, `$targetScreen`).

## VM runs (`run-series.ps1 -Vm`, from 2026-10-23)

A VMware Workstation VM runs the same `run-test.ps1` headless, so tests no longer
need the host's desktop. VMware's own SVGA 3D device does the Direct3D (Windows
Sandbox's GPU paravirtualisation is what bugchecks). On this Hyper-V host
VMware runs on the Windows Hypervisor Platform, which is already enabled
(`HypervisorPlatform` feature, CBS selection 1): vmware.log says `Monitor Mode: ULM`,
and the 3D renderer is `ISBRenderer (VKRenderer)` on the host GPU. Installed:
Workstation 26H1u1 (26.0.1 build 25688693) in `C:\Program Files\VMware\VMware Workstation`.

- **VM:** `C:\VMs\dsfforza-test\dsfforza-test.vmx` (VM 1) and its linked clone
  `C:\VMs\dsfforza-test2\dsfforza-test2.vmx` (VM 2; "VM pool" below): 4 vCPU,
  4 GB (8 GB until 2026-10-25), 80 GB growable
  SATA disk, BIOS firmware (no TPM or Secure Boot; a vTPM needs VM encryption,
  which vmrun can't start headless), SVGA 3D with 512 MB graphics memory (2 GB
  until 2026-10-25), no
  network adapter, no USB, an HD audio card muted inside the guest. Guest:
  `D:\windows_microwin_ru.iso` (Windows 11 Pro 25H2, build 26200, ru-RU,
  MicroWin-debloated), local admin `dsf` / `dsfforza`, autologon, UAC off,
  no sleep, lock or screensaver, Windows Update off, Defender exclusions for
  `C:\Game`, `C:\Run`, `C:\Repo`, `C:\Output`, a silent firewall with rules for
  `C:\Run\Driver.exe`, desktop 1600x900, the game in a 1280x720 window.
- **Build:** `tools\wsb\vm-build.ps1` (Iso, Create, Install, Provision,
  Snapshot; Clone for VM 2). `tools\wsb\vm\autounattend.xml` and `first-logon.ps1` go on a
  small ISO built with IMAPI2FS. The media answer file replaces the one
  MicroWin bakes into the image. The account is created in specialize and put
  in Administrators by SID (S-1-5-32-544), because the Russian image names
  the group "РђРґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂС‹". First logon installs the VC++ x86 runtime and
  VMware Tools (`setup.exe /S /v"/qn REBOOT=R ADDLOCAL=ALL"`; Tools 13 has only a 64-bit
  `setup.exe`, no `setup64.exe`). The whole build takes about 35 minutes
  (Install about 30, Provision about 1, copying 5.9 GB of game).
- **Shared folders** (vmx `sharedFolderN`), linked to the sandbox's paths so
  `run-test.ps1` runs with its defaults: `C:\Repo` (repo, read-only),
  `C:\Output` (`%LOCALAPPDATA%\Temp\dsfforza-wsb`, read-write), `C:\Profile`
  (Documents\Ubisoft\Driver San Francisco, read-only). `C:\Game` is a local
  copy of the game folder (share `Game`, read-only), put back to vanilla from
  `dsfforza-backup\install.json`: replaced files restored and hash-checked,
  created ones dropped. Re-run `setup-vm.ps1 -Phase Game` (through `vm.ps1
  Exec`) after changing the host install. `dsf_enhanced.asi` and `.ini` don't
  need it: each `-Vm` run takes the host game folder's current ones ("Game
  files in VM runs" below).
- **Runs:** `run-series.ps1 -Vm` takes a free VM of the pool ("VM pool"
  below), starts it if needed (`vmrun -T ws start
  ... nogui`), copies the host profile into the
  guest as a 1280x720 window, then runs `run-test.ps1` in the guest's desktop
  session (`vmrun runProgramInGuest -interactive`, through a wrapper script
  copied into `C:\dsf`, started hidden) with `-RunDir
  C:\Output\runs\vm<id>-<stamp>`. Results land in
  `%LOCALAPPDATA%\Temp\dsfforza-wsb\runs\vm<id>-<stamp>\` (`vm-<stamp>` before
  2026-10-25: VM 1) and `runs\latest.txt`
  points at the newest folder, so `events.json`, `sheet.py` and the rest work
  unchanged. `-CopyRepoFiles` makes `run-test.ps1` copy archives and overrides
  from `C:\Repo` into `C:\Run` instead of linking them across the shared
  folder. VM mutexes are separate from the sandbox and host
  series. Keys go to the game inside the guest only; the host's speakers and
  Driver profile aren't touched. The share `Repo` is the checkout the VM was
  built from (the main one); each series points it at its own checkout
  (`vm.ps1 Share`, checked with a marker file) and back at the end, so a series
  from a git worktree tests that worktree's builds and scripts. Before that
  change a worktree's `-Vm` series ran the main checkout's `run-test.ps1` on
  the main checkout's copy of its archive path (missing, or stale).
- **Control:** `tools\wsb\vm.ps1 Start|Status|Exec|Share|Stop|Revert|StopIdle
  [-Vm 1|2|all]`. Revert goes
  back to the snapshot `clean` (powered off, right after provisioning); Stop
  and Revert wait until no series uses the VM.
  `tools\wsb\guest\fps.ps1` (through `vm.ps1 Exec`, next to a run) records 30 s
  of presents with Intel PresentMon (`C:\Output\vm\PresentMon-2.6.0-x64.exe`).

### VM pool (2026-10-25)

Two VMs, so two series run at once: VM 1 `C:\VMs\dsfforza-test` and VM 2
`C:\VMs\dsfforza-test2`, a linked clone of VM 1's snapshot `clean`
(`vm-build.ps1 -Step Clone -Vm 2`; its disk is a 10 MB child of VM 1's base
disk, and VM 1's `.vmsd` lists it as `snapshot0.clone0`, so that snapshot must
stay). The guest has no network adapter, so the shared computer name, SID and
the missing MAC don't matter; the clone gets new UUIDs at its first power-on.

- **Pool:** `$env:DSF_VMS` (`;`-separated `.vmx` paths), else those of the two
  that exist (`vm-common.ps1`). A VM's id comes from its file name
  (`dsfforza-test` 1, `dsfforza-testN` N).
- **Mutexes:** per VM `Local\dsfforza-vm-run-<id>`; VM 1 also holds the old
  `Local\dsfforza-vm-run`, so a series from an older checkout (VM 1 only) and
  a new one exclude each other. Start/Stop: `Local\dsfforza-vm-lifecycle`
  (VM 1, the old name) and `-lifecycle-<id>`.
- **Taking a VM:** `run-series.ps1 -Vm` takes a free VM at once, running
  ones first (no boot, no extra RAM), else waits for whichever comes free
  first, first come, first served (queue below). `-VmId 2` pins one. The series prints
  `VM <id>: <name>` and its run folders are `runs\vm<id>-<stamp>`. Setup logs
  go to `C:\Output\vm\<name>\`; `C:\Output\HostDll` is only rewritten when a
  DLL changed (another VM's run may be copying it).
- **Settings** (`$VmSettings` in `vm-common.ps1`): 4 GB, 512 MB graphics
  memory, 4 vCPU. `vm.ps1 Start` writes them into a powered-off VM's `.vmx`
  when they differ, so a clone or a reverted snapshot gets them too.
- **Idle power-off:** a running VM holds its RAM whether a game runs or not
  (see below). At the end of a series `run-series.ps1` starts a hidden
  `vm.ps1 StopIdle`, which powers the VM off when no series has used it for
  `$env:DSF_VM_IDLE_MINUTES` (default 15; negative: never). A series in
  between re-arms it, and it never stops a VM whose run mutex is held or
  that a series is queued for. A cold start to a ready desktop session takes
  about 75 s.
- **Queue** (2026-10-26, issue #60): a waiter writes a ticket
  (`dsfforza-wsb\vm\queue\<utc ticks>-<pid>.json`: pid, process start time,
  the VMs it can take) under the lock `Local\dsfforza-vm-queue`. A VM goes to
  the oldest live ticket whose pool has it, so a series pinned to VM 1 doesn't
  hold up a later one that can take VM 2. Only that oldest waiter waits on the
  VM's mutex (5 s kernel waits between looks at the queue); the others poll
  every 2 s. Tickets of dead processes are dropped. `dsfforza-wsb\vm\queue.log`
  logs each ticket and the VM it took. `vm.ps1 Stop` and `Revert` and
  `vm-build.ps1 -Step Clone` queue the same way. Clone now also waits for
  the old clone's own series before deleting it; it used to hold only VM 1's
  mutexes.
  - Before, a waiter blocked on the mutex alone (`WaitHandle.WaitAny`). On
    2026-10-25 a series queued at 07:09 was still waiting at 07:50 while
    series started after it took VM 1. Likely cause: PowerShell runs scripts
    on an STA thread, whose waits pump messages, and a waiter woken by an APC
    loses its place in the mutex's queue.
  - Series from checkouts without the queue still take the mutexes directly
    (VM 1: `Local\dsfforza-vm-run`). They exclude queued series but don't
    queue, so they can still get a VM ahead of an older ticket.
- **Game files in VM runs** (2026-10-26, issue #59): `C:\Game` is the host
  game folder as of provisioning (VM 1's snapshot `clean`, 2026-10-24 02:07),
  so its `dsf_enhanced.asi` (SHA-256 `1ADDEC5C...`) was a build without the
  fix for "Crashes while the world loads". Vanilla series without `-Override`
  hit that crash in about half the runs (`vm2-20261026-112701`). Now each
  `-Vm` run copies the host's current `dsf_enhanced.asi` and `.ini` into its
  run folder (`host-game\`), and `run-test.ps1 -GameRootFrom` puts them over
  `C:\Game`'s copies in `C:\Run`; `-Override` still wins. `result.json`
  `dsfEnhanced` records the `.asi` hash and source, and the source `.ini`
  hash. `gameRoot` holds the hashes of `C:\Game`'s root files; run-series
  warns once per series if one differs from the host's (then run `-Phase
  Game`). Retaking the snapshot wasn't needed: apart from `dsf_enhanced.asi`,
  the host files changed since then are the dsfforza install's, which
  `-Phase Game` puts back to vanilla anyway.
- **Checked** (2026-10-26, host `dsf_enhanced.asi` `BF829B28...` from
  dsf_enhanced main `e9c3ba0`; its log says "legal screen skipped; kept 17
  pending resource references"): 12 vanilla runs without `-Override` (IDs
  147,1,2), VM 2 `vm2-20261026-120240` to `-123911` and VM 1
  `vm1-20261026-124150` to `-124751`. None hit the fatal-error dialog. 11
  finished; `vm2-20261026-123140` ran out the 150 s Story deadline while it
  was still loading, at about 40 % of the usual speed (VM 1 was running
  another agent's game then; issue #70). Every `result.json` has
  `dsfEnhanced.asi` `BF829B28...`; `gameRoot` showed the guests' own copy
  as `1ADDEC5C...`, and no other root file differed from the host's.
- **Queue checked** (same day): three series started 3 s apart (12:02:04,
  :07, :10) took a VM in that order (12:02:04 VM 2, 12:11:41 VM 2, 12:30:33
  VM 2), and one pinned to VM 1 queued at 12:20:21 behind them took VM 1 at
  12:41:22 (`queue.log`). At 12:23 another agent's series, from a checkout
  without the queue, took VM 2 ahead of the third series; it had been
  waiting on the mutex itself. Fake-VM tests (no game): four
  waiters for one VM were served in arrival order within 20 ms of each
  release. With mixed pools (pinned to 9, either, pinned to 8, either),
  each VM went to the oldest waiter that could take it. A killed waiter's
  ticket was dropped.
- **Memory sampler:** `run-test.ps1 -MemorySeconds 5` (default) writes
  `memory.csv` in the run folder (guest available and committed memory, hard
  page faults per second, GPU adapter memory, and Driver.exe's private bytes,
  working set and address space) and the peaks to `result.json` `memory`.

**Memory measured** (full roster `out\final-gui2` with dsfforza.asi, IDs
147,705,26,1,400,600, VM 2, host sampled every 5 s):

| setting (guest / graphics) | runs | guest peak used | Driver.exe peak private / WS / virtual | vmware-vmx WS after boot | peak in game | run |
|---|---|---|---|---|---|---|
| 8 GB / 2 GB (old) | `vm2-20261025-083626` | 2.92 GB of 8 | 982 / 685 / 1219 MB | 9.6 GB | 13.2 GB | 171 s |
| 4.5 GB / 1 GB | `vm2-20261025-084216`, `-084516` | 2.86 GB, 1.75 GB available | 988 / 691 / 1219 MB | 6.0 GB | 8.05 GB | 170, 160 s |
| 4 GB / 1 GB | `vm2-20261025-090258` | 2.92 GB, 1.18 GB available | 981 / 687 / 1219 MB | not sampled | not sampled | 162 s |
| 4 GB / 512 MB (chosen) | `vm2-20261025-085007`, `-085255` | 2.92 GB, 1.17 GB available | 989 / 691 / 1229 MB | 5.5 GB | 7.03 GB | 160, 160 s |

- The game needs about 1 GB private and 1.2 GB of address space, far from
  the 32-bit 4 GB. The whole guest peaks under 3 GB committed.
- **Graphics memory is guest RAM:** the guest reports no dedicated GPU
  memory and 300 to 340 MB shared (Driver.exe 250 MB) with any setting.
- **Host side:** the vmware-vmx working set is the whole guest RAM within
  about 80 s of power-on (the guest's zero-page thread touches every free
  page, and in ULM mode on WHP there is no page sharing or trimming to give
  pages back: vmware.log has no PShare or MemTrim lines, and the working set
  never drops after the game exits), plus about 1.4 GB of fixed overhead,
  plus a 3D part once the game renders: 3.0 to 3.6 GB with 2 GB graphics
  memory, 1.8 to 2.0 GB with 1 GB, 1.2 to 1.5 GB with 512 MB. vmware.log's
  reservations follow it (`VmxMks3d` 3.2 GB to 1.66 GB, the renderer's
  DataChannel 1 GB to 256 MB). `mksSandbox.exe` (the Vulkan renderer) adds
  0.3 to 0.4 GB per VM. So `MemTrimRate` and `sched.mem.pshare.enable` have
  nothing to act on here, and `mainMem.useNamedFile = FALSE` was already set.
- **`svga.vramSize`:** VMware sets it back to 256 MB at power-on, and the
  boot working set stayed 5.5 GB with 16 MB asked.
- **No paging stalls** at 4 GB: at least 1.17 GB stays available and the
  median of hard page faults is 8 to 22 pages/s. Bursts of 1,000 to 5,000
  pages/s come while the world loads and when a probe car spools (reads of
  the 2.6 GiB archive, which a smaller guest file cache holds less of; at
  4.5 GB the same, at 8 GB under 530 after the load). Run times and
  Story-ready times (31 to 32 s) match the 8 GB VM.
- **Frame rate** (PresentMon, 30 s from 45 s after launch, full roster, VM 1
  busy with another series): 512 MB mean 37.9 fps, median 41.0, 1 % low 16.9;
  1 GB 35.7, 37.9, 18.6. The same within noise.
- 3.5 GB would bring a VM to about 6.5 GB but leaves under 0.7 GB available
  in the guest at the peak; not done.

**Two series at once** (2026-10-26, after a host reboot; VM 1 reverted to
`clean`, the new settings written, VM 2 cloned again): from 11:21, in two
processes, a vanilla series (3 runs, pool, so VM 2) and a full-roster series
(2 runs, VM 1).

- Both ran side by side, 11:22 to 11:29: full roster `vm1-20261026-112227`
  (198 s, the first after the revert) and `-112554` (163 s); vanilla
  `vm2-20261026-112238` (126 s) and `-112453` (118 s) finished.
  `vm2-20261026-112701` hit the DriverNGHook fatal error while the first
  probe car loaded (100 s, loading-screen shots): the engine fault of
  "Crashes while the world loads", as the vanilla series ran the game folder's
  dsf_enhanced without the fix. 1.4 GB was available in the guest then.
- Guests: peak used 2.7 to 2.9 GB of 4, at least 1.16 GB available,
  Driver.exe 960 to 991 MB private.
- Host: vmware-vmx working sets VM 1 at most 7.06 GB, VM 2 6.95 GB, 14.0 GB
  together, plus 0.86 GB for both `mksSandbox.exe`; host available memory
  never under 6.2 GB. One VM at the old settings held 12.6 to 13.6 GB, and
  two at the old settings (VM 2 cloned at 8 GB, 2026-10-25) left 0.7 GB free,
  so that test was stopped.
- Idle power-off: both series armed `StopIdle` at 11:28; at 11:44 VM 2 was
  powered off ("idle for 15 min: stopped" in
  `dsfforza-wsb\vm\dsfforza-test2\idle-stop.log`) and VM 1, busy with
  another agent's series, was left running ("in use").
- Run times against the old settings: full roster on VM 1 alone at 8 GB
  106 to 131 s (`vm-20261025-025351` to `-065150`); VM 2 alone at 8 GB 171 s,
  at 4 GB 160 s; with both VMs running games 163 to 198 s. The difference
  is the shared host CPU and GPU, not memory (no paging in the guests).

`.vmx` backups from before the change: `C:\VMs\dsfforza-test\dsfforza-test.vmx.orig-20261025-083356`
and `dsfforza-test.vmsd.orig-20261025-083356` (before the first clone; 8 GB,
2 GB), `dsfforza-test.vmx.before-pool-20261026-112107` (right before the
revert and the new settings).

### Slow VM sessions (2026-10-26, issues #77, #78)

From 12:02 on 2026-10-26 every run on VM 2 drew the cockpit view as a flat
grey shell (no dashboard, wheel, gauges or hands) and, on the full roster,
spooled only the first probe ID ("NOT spooled after 40 ticks"); vanilla cars
too. The cause was neither the 512 MB of graphics memory nor the 4 GB guest:
that VM 2 session was running at half speed.

- **The cause:** VMware on the Windows Hypervisor Platform syncs the vCPUs'
  TSCs while the guest boots. In VM 2's power-on at 11:57 (VM 1 was running a
  game) the sync took 3.1, 2.1 and 1.1 ms against a 1 ms threshold, and
  vmware.log says `Syncing WHP TSCs failed after 3 attempts. Falling back to
  always trapping RDTSCs.` From then on every RDTSC in the guest exits to the
  host, for the whole session. VM 1's power-on at 11:07 the same day did the
  same (`vmware-0.log`), and its runs `vm-20261026-111023` and `-111602`
  spooled slowly too (13 ticks, the spooler busy for 11 samples).
- **Measured** (`guest-bench`, both VMs idle, 15:35): a PowerShell loop 0.74 s
  on VM 2 against 0.36 s on VM 1; uncached sequential reads 200 against
  562 MB/s, random 64 KiB reads 2.1 against 0.33 ms, write-through 95 against
  324 MB/s. In game: "run folder built" 21 s against 6 s, the Story world
  ready after 52 to 59 s against 27 to 51 s.
- **What the game does with it:** `TrafficSpooler.Busy()` (logged every 20
  ticks by `register.lua`'s probe) stayed true for the whole run in every VM 2
  run after 12:02 (`vm2-20261026-120240` to `-151915`); in VM 1's runs it
  clears after one sample. While the spooler is behind, the player car's
  interior entry (N+1000, `cockpit.md`) isn't loaded, so the cockpit view
  draws the low-detail shell (after the spawns `IsInteriorLoaded(73)` was
  still false in `vm2-20261026-144237`), and a mission vehicle requested
  next waits behind the traffic loads past the probe's 40 ticks (397 in that
  run was loaded later; 73 took 19 ticks).
- **Not the settings of #61:** VM 1 at 4 GB / 512 MB drew the same vanilla car
  textured in the same hours (`vm1-20261026-124150`, `-153216`), and VM 2's
  first session after the clone (11:21 to 11:44, same settings) did too
  (`vm2-20261026-112238`). A new power-on of VM 2 (TSC sync 48 us) fixed both
  issues with nothing else changed: `vm2-20261026-153830` spooled and drove
  all six full-roster IDs (537, 397, 73, 53, 28, 372; one tick each after
  the first) with textured cockpits. VM 2's Story-stage hangs of #70
  (`vm2-20261026-124544`, `-130721`, `-133303`) were in the same slow
  session.
- **The fix:** `vm.ps1 Start` (so every `run-series.ps1 -Vm`) reads the
  session's vmware.log once the desktop is up and, while it has that line
  and no other series holds the VM, powers the VM off and on again (up to
  three power-ons, about 75 s each). `vm.ps1 Status` flags such a session.
  The probe's "NOT spooled" line now says whether the spooler was still busy.
- **Checked** (both VMs in healthy sessions, series started together, with
  the fix): full roster `out\final3`, dsfforza.asi with #79, IDs 537, 397, 73,
  53, 28, 372: `vm1-20261026-154434` and `vm2-20261026-154434` each spooled
  and drove all six (the first after 2 ticks, the rest after 1) with
  textured cockpits; vanilla archive, IDs 147 and 266: `vm1-20261026-160105`,
  `vm2-20261026-160105`, textured. The spooler was idle from the second
  sample on. Host memory with both games running: at least 10.0 GB free,
  vmware-vmx at most 4.4 (VM 1) and 6.9 GB (VM 2), mksSandbox 0.9 GB
  together. Settings unchanged: 4 GB, 512 MB graphics memory, 4 vCPU.
  Before and after: `img/vm-cockpit-147-before-after.jpg`,
  `img/vm-cockpit-397-before-after.jpg`, `img/vm1-cockpit-537-147-after.jpg`.
- Finding such sessions in old runs: the `spooler busy` field of the
  `[dsfforza.register] t<n> traffic` lines in Driver.log.

### VM results (2026-10-24)

- Vanilla smoke test `vm-20261024-015247` (vanilla archive, ID 147): finished in
  81 s, Story world ready after 3 Enter presses at 23 s (the host took 32 s
  in `host-20261023-174440`), `keysSkipped` 0. The shots show the textured
  city, traffic, HUD and Shift-view flares: real 3D, not black.
  `story.png` (8 s after "ready") still shows the loading screen.
- Ported cars `vm-20261024-020159` and `vm-20261024-020519`
  (`out\testdrive7\dngvehicles.sp`, `ported.lua` from `testdrive7`, IDs 147,1,2):
  both finished, every car driven, cockpit and chase views for each. ID 1
  (BMW M3 E30) and ID 2 (Supra) render with bodies, lamps and cockpits, gauges
  included. The HUD names are wrong (Jaguar E-Type / Nissan Skyline), as on
  the host.
- One run (`vm-20261024-015830`, same arguments) hit DriverNGHook's fatal
  error dialog while the world loaded, with no minidump. The next two
  identical runs passed, and host runs of this archive have died the same way
  (`host-20261023-174440`, exit -1), so it isn't VM-specific. Driver.exe stayed
  near 1.17 GB virtual and 0.96 GB private, far from the 32-bit limit.
- Frame rate (PresentMon, 30 s of `vm-20261024-020519` from 35 s after
  launch, Shift zoom and driving, host CPU loaded by port_batch builds): mean
  45 fps, median 46.8, 1 % low 28.1. Present mode "Composed: Copy with GPU GDI".
  dsf_enhanced's TrafficDiag reports `fps=0.0` in the VM as on the host.
- Host desktop: a window watcher (EnumWindows and the foreground window, once a
  second) ran from 01:49 to 02:07 (VM start, all runs, guest scripts). The only
  new windows were HWiNFO64's own overlays, and the foreground never changed.

### VM limitations and gotchas

- **Priority:** vmware-vmx is started by the vmware-authd service; a normal
  process can't change its priority or read its command line (access
  denied). The vmx can only ask for normal or Idle (`priority.ungrabbed =
  "low"`; a headless VM never "grabs"). Idle starved the install while
  port_batch used every core: CD reads took 2.4 s each and the disk stayed
  at 10 MB for 10 minutes. So the VM runs at normal priority, and
  `Set-VmPriority` (BelowNormal) only takes effect from an elevated shell.
- **Workstation 26 vmrun:** `checkToolsState` says "installed" even while
  Tools runs, and `listProcessesInGuest` shows no arguments (no `-n
  vmusr`). Right after logon, `runProgramInGuest -interactive` fails for a
  while with "must be logged in interactively"; `vm-common.ps1` retries for
  3 minutes.
- **Read-only copies:** files copied from the read-only `C:\Repo` share keep
  the ReadOnly flag. `run-test.ps1` clears it in the run folder and before
  deleting (run `vm-20261024-015718` failed on `clonetest.lua`).
- **Debugging a headless install:** `vm-build.ps1 -Step Create -VncPort 5971`
  turns on VMware's VNC server on 127.0.0.1 (no password). `uv run
  dsff vncshot 127.0.0.1 5971 shot.png` grabs a frame. Over VNC,
  special keys get through but letters don't (keysyms are probably mapped
  through the Russian host keymap), so `vnckeys.py` is of little use. A
  powered-off guest disk can be read with 7-Zip: `7z e dsfforza-test.vmdk
  1.ntfs` (85 GB, mostly zeros), then `7z e 1.ntfs <path>`.
- Windows isn't activated (generic Pro key, no network). No watermark is
  in the shots.

### Crashes while the world loads (2026-10-24, full roster)

About half of the VM runs ended at the DriverNGHook fatal-error dialog 20 to
100 s after launch, before or right after the first probe car. With
`[dsfforza] crash_log=1` the faults fall into two kinds.

**Engine: deferred UID list (0x00BAE85A / 0x00BAE8BE / 0x00BAEBB2).**

- Where: FUN_00BAE790 runs every frame from the game update (FUN_00699250).
  It walks the resource manager's list of pending UID slots (pointers at
  +0x127AC8, the 0x0185A6B8 list of `spool-limits.md`, count at +0x157AC8)
  and resolves or drops each one.
- The fault: a slot pointer in that list points at freed memory (reads of 2,
  3 or 0xE9xxxxxx).
- It happens with the **vanilla archive, without dsfforza.asi and without any
  ported car**: 2 of 4 runs (`vm-20261024-203720` to `-204228`); with the
  plugin, 1 of 3.
- So it isn't caused by the ported cars or the plugin.
- Cause (found 2026-10-25): dsf_enhanced's `[Startup] SkipLegalScreen`. The
  skip NOPs the legal screen's 5 s wait (0x00799C4A), so the loading-screen
  start (FUN_004A20C0, called at 0x00799C92) runs while startup files are
  still loading. That function empties the pending list
  (`mov dword [0x0188A6B8],0` at 0x004A2153), so the references waiting
  then (17 to 281 in the VM; 0 with the vanilla wait) are dropped, and the
  list is corrupt by the time the world loads.
  - Bisect, vanilla archive, user's ini: `SkipLegalScreen=0` 0 of 8 crashed;
    the skip with every other dsf_enhanced feature off 2 of 4.
  - Fix in dsf_enhanced (branch `fix/world-load-crash`): the legal screen's
    call keeps the pending list; every other loading screen empties it as
    vanilla. 0 of 10 runs crashed on the vanilla archive, 0 of 4 with the full
    roster and dsfforza.asi. Merged: dsf_enhanced main `e9c3ba0`, on the
    host since 2026-10-26; VM runs use the host's copy ("Game files in VM
    runs" above), and 0 of 12 vanilla runs crashed with it.
  - On a real PC it is the same race between startup loading and the first
    menu frames, so a slow or busy disk can hit it; the slow VM disk and CPU
    hit it in about half the runs. Notes: driver-sf-mods,
    `reverse_engineering/driver_sf/world_load_crash.md`.

**Lua VM while a big user file loads (0x005F078A / 0x005F078D / 0x005EE811).**

- Where: luaV_execute reads an upvalue through a bad pointer. It runs inside
  `user_open` (ScriptLoader.ri_dofilefromdisk в†’ luaL_loadfile + lua_call,
  FUN_0065A7D0 в†’ FUN_00607800), right after a log line "loading user file
  'dsfforza\ported.lua'". The test bootstrap loads ported.lua three times
  (register, garagetest, clonetest).
- When: only with the full roster's generated files (`ported.lua` 117 KB,
  `hud.lua` 108 KB, about 2,600 and 1,900 constants in one chunk). It never
  happened with the 40-car files.
- Runs: 9 of the runs on the full roster before the fix (`vm-20261024-193312`
  to `-231901`).
- What fixed it: stopping the collector during each load (`collectgarbage("stop")`
  / `"restart"` around `user_open`). With that, none of 10 runs had this
  fault (`vm-20261024-232307` to `vm-20261025-001221`); 4 still hit the
  engine fault above.
- Likely cause: a collection step during compile or run of a big chunk frees
  something the VM still uses, as in Lua 5.1's early parser and GC bugs. Not
  proven.
- Both bootstraps (`tools/game/lua/bootstrap.lua`,
  `tools/game/lua/player/bootstrap.lua`) now wrap `user_open` that way. It is
  read-only in DriverNGHook's `_G` metatable, so the wrapper is `rawset`.

**Lua VM after minutes of play: the Lua heap is full (issue #56, fixed).**

- What happened: on the full roster the game died in the Lua VM after a few
  minutes of garage browsing, 3 of 3 runs (`vm-20261025-094432`, `-075445`: a
  C function called through a NULL pointer from `005F01DD`, the closure's
  memory taken by a Proto; `-065422`: `0x005F078A`, a closure whose upvalue
  pointer is NULL). Both are closures used after they were freed. The
  full-roster runs also had `register.lua:578: bad argument #1 to 'concat'
  (table contains non-strings)` from the first probe with traffic around.
- Cause (confirmed-runtime): the game's Lua state allocates from one fixed
  10 MB block (`asi.md`, "Lua heap"). With the full roster Lua holds about
  5 MB after the files load and 8 to 9 MB while driving and in the garage;
  at about 8.5 MB (allocator overhead fills the rest) the block refuses
  requests. Lua 5.1 raises "not enough memory" at whatever allocation hit the
  wall, the game's pcall wrapper (FUN_005E4760) drops the error, and the state
  is left half-updated. Which half-done update frees the closure was not
  traced. The vanilla roster peaks at about 6 MB (`vm1-20261026-142558`).
- Control runs with `[lua] overflow=0` (10 MB block, failures only logged):
  `vm2-20261026-122308`: first refused request at 12:27:43 with Lua at
  8538 KB, four more, then the Lua VM fault at 12:28:48 (`005F7787`), while
  driving, before the garage. `vm1-20261026-150731` (keys at 1.5 times the
  pace): four refused requests at 15:08:37 to :44, the concat error at the
  same time, no fault this time. The concat error comes only with refused
  requests: no run with the fix has it, and its probes see 82 to 85 traffic
  cars.
- Fix: dsfforza.asi's `[lua]` module makes the block 32 MB and serves
  anything the block still refuses from a private heap. Runs (full roster
  unless noted; dsf_enhanced from `out\updown-off` as in the issue, menu keys
  rebuilt from `vm-20261025-094432`'s events.json):

  | Run | Build | Browse | Result |
  | --- | --- | --- | --- |
  | `vm1-20261026-122537` | 10 MB + overflow heap | the issue's 108 steps | 65,536+ refused requests served from the overflow heap (peak 751 KB, Lua 9.3 MB); finished, no fault |
  | `vm1-20261026-143525` | 32 MB + overflow | the issue's 108 steps: makers 1 to 19, then 39 to 42 (BMW, BUGATTI, BUICK, BUS) | Lua peak 9 MB, nothing refused; finished (555 s), no fault |
  | `vm1-20261026-145055` | 32 MB + overflow | 268 steps at 1.5 times the pace: 63 maker rows opened (the issue's 23, then 20 up and 20 down) | Lua peak 9 MB, nothing refused; finished (977 s), no fault |
  | `vm1-20261026-142558` | 32 MB + overflow, vanilla archive | the issue's 108 steps (35 makers) | Lua peak 6 MB; finished, no fault |
  | `vm1-20261026-152238` | the PR head (rebased build), 32 MB + overflow | the issue's 108 steps at 1.5 times the pace, same makers | Lua peak 9 MB, nothing refused; finished (559 s), no fault |

- Key pace: on a busy VM an Enter that comes before the maker list is ready
  is dropped and the next Esc leaves the garage, so a run browses less than
  its keys say (`vm1-20261026-133949`, `-141004`: back at the garage menu
  after maker 11 and 13). Check the makers in dsfforza.log (`menu_debug=1`,
  `savedCategorySelected`) before trusting a run.
- The stopped collector around `user_open` (above) is a separate fix; the
  loads peak at about 5 MB, well inside the block.

## Result: a cloned vehicle works (confirmed-runtime)

`out/dngvehicles.clone.sp` (`clone_vehicle.py --source 266 --id 304`) loads
like the vanilla archive. In run `20261021-224926`:

```text
vehicleStats[304] copied from [266]
id 304: requesting (loaded before: false, inZap: true, vehicle: boolean:false)
id 304: spooled after 6 ticks
spawning under the Shift camera at (-819.29, 77.88, 1835.85, 1)
id 304: spawn returned userdata
id 304: zapping into it
id 304: PLAYER IS DRIVING IT after 3 ticks (inZap: false)
```

The player drives the clone in normal traffic, textured, with the ASYM badge
and "Desanne" on the HUD: `img/clone-304-with-stats.jpg`. The control run
`20261021-224726` (vanilla archive, ID 266) looks the same:
`img/vanilla-266-control.jpg`.

What this confirms:

- the game accepts a rebuilt `dngvehicles.sp` with 151 vehicles;
- a new ID (304, outside the vanilla 62вЂ“303 range) spools through
  `TrafficSpooler.RequestMissionVehicle`, so the SSLP/VSCT edits are enough
  for the spool system;
- `GameVehicleResource.create{Model_Id = 304}` builds a drivable car from the
  copied VEHC entries and the patched VSVH/VO3D/SPID/UPVH IDs;
- sharing model and texture UIDs with the source is fine, at least when the
  source isn't loaded at the same time (both spawned: "Clone and source
  loaded together" below).

## The HUD needs a vehicleStats entry (confirmed-runtime)

Without help, the clone's HUD shows a stack of other cars' names and badges
(Fiat Abarth 695 SS, Ford GT, Continental Supersports, Hudson):
`img/clone-304-no-stats.jpg`, run `20261021-224452`.

The game's Lua table `vehicleStats[modelId]` holds the manufacturer badge
index, `ManufacturerName`/`ModelName` string IDs, garage pictures, price,
tier, unlock chapter and the speed/strength/drift bars (Felony Rework ships a
readable copy in `Common/LuaScripts/vehiclestats/vehiclestats.lua`). Copying
`vehicleStats[266]` to `[304]` at runtime fixes the HUD. Converted cars will
need their own entries: manufacturer, name strings (or new string IDs),
pictures and stat bars.

### Stray badges and plates in harness shots (2026-10-28)

Most VM before/after images until then had a row of other cars' plates and
badges floating over the scene ("SPORT SUPERCHARGED", "WRANGLER", "370Z",
"Countach", "CONTINENTAL SUPERSPORTS"...: PR #171's
`img/gauges-161-ingame.jpg`, #163's and #164's chase views), a wrong badge
in the HUD's corner (CAISSON, GMC C5500), and three red takeover icons.
They changed from shot to shot, and players don't see them.

**Cause: the same as above, in the harness.** `run-series.ps1 -Archives
<build>\dngvehicles.sp` installed the archive only. Without the build's
`dngvehicles.lua` as `dsfforza\ported.lua`, register.lua registers no car,
so the ported probe cars have no vehicleStats entry and the HUD draws what
is left in the record. A vanilla car in the same run (225) is clean
(run `vm1-20261028-131725`). The 2026-10-21 run above shows the same stack
before dsfforza.asi existed, so the plugin's `[hud]` slots don't cause it.
A player install always has ported.lua and hud.lua: `tools/install.ps1`
and `dsff install` refuse a build without them (`install.gui_problem`).

**Fix (harness).**

- `run-test.ps1`: an archive named `dngvehicles.sp` now brings its build's
  files with it, as `install.ps1` installs them: `dngvehicles.lua` as
  `dsfforza\ported.lua`, and, when `gui\` has `gui.sp`, `Fonts.fchunk` and
  `hud.lua`, those three plus `gui\Locale\<language>\global.fchunk`.
  `-Override` and `-LuaFiles` still win, and `-ArchiveOnly` (also on
  `run-series.ps1`) leaves the build's files out. result.json
  `buildFiles` and an events.json line list what was used. register.lua
  doesn't add these cars to traffic (`register = { interesting = false }`
  in config.lua), as before. With an explicit `-LuaFiles` ported.lua,
  traffic is unchanged.
- `clonetest.lua`: a probe or `-AheadIds` car that still has no entry (an
  archive with another name, `-ArchiveOnly`) gets a copy of
  `vehicleStats[266]` with `Manufacturer`/`Model` 32: no badge and no
  plate (register.lua's blank glyph).

In game (confirmed-runtime): out/hudtest (Panoz 51 Esperante GTLM at ID 1,
Audi A4 Avant at ID 2, `--gui --wide-glyphs`, dsfforza.asi), IDs 225, 1
and 2, `-HoldMph 60`.

- Before: master's harness, run `vm1-20261028-211915`. Stray rows in the
  chase and cockpit views of both cars.
- After: run `vm1-20261028-212621`. No stray glyphs, and each car shows
  its own HUD badge and plate: the Panoz its Forza logo and plate (page 1
  glyphs, slots 1 and 2 in dsfforza.log), the Audi the vanilla rings over
  "A4 AVANT". See `img/vm-stray-hud-glyphs-before-after.jpg`.
- The same archive as `bare.sp` (no build files), run
  `vm1-20261028-213136`: `vehicleStats[1]: none; blank badge and plate`,
  and no badge, plate or stray row.

## Game behaviour the harness had to handle

- **The save loads into the Shift overview** (zap level 7, `inZap` true,
  `currentVehicle` is `false`). `localPlayer.position` is (0, 0, 0) there and
  zapping into a car fails until the camera is down at street level. The
  harness presses Up (extended scan code 0x48) five times; the probe waits
  until `zapcontroller.getZapLevel()` is в‰¤ 1, or 120 ticks pass, then spawns.
  The level is only logged every 4 ticks and the last logged value was 3, so
  which condition fired isn't recorded; either way the spawn and zap worked.
- **Placement:** `zapcontroller.ZapCameraGetTargetPos()` (global table
  `zapcontroller`) gives the world position under the Shift cursor.
  `GameVehicleResource.create{SnapToTerrain = true, Position = ...}` puts the
  car there, then `localPlayer:SetZapLevel(1, nil, false, {forcedOut = true})`
  and `localPlayer:ZapIntoVehicle(car, false, false, false, nil,
  {disableZapFlash = true})` possess it.
- **Lua can't write files:** the game's `io` resolves paths through its
  `COM:` layer and refuses writes everywhere. Use `print`; DriverNGHook
  writes it to `Driver.log` next to the exe.
- **DriverNGHook needs the VC++ 2015+ x86 runtime** (`msvcp140.dll`,
  `vcruntime140.dll`), which the sandbox image lacks.

## Lua API reference

`research/lua-api.tsv` is the game's own Lua documentation, collected at
runtime by `tools/game/lua/dsfforza/docdump.lua` through
`Documentation.collectLuaDocumentation()`: 1,447 functions and 248
properties in 99 libraries, with help text. Libraries most relevant here:
`TrafficSpooler`, `GameVehicleResource` (`create`, `swapWithModel`),
`VehicleLodSpooler`, `spoolsystem`, `zapcontroller`, `ActiveVehicles`,
`VEdit`.

`VehicleLodSpooler` has `IsInteriorLoaded(id)` and `IsExteriorLoaded(id)`.
That supports reading the two smaller VEHC variants (IDs N+1000 and N+2000)
as interior and exterior LOD models (which is which: "Interior and exterior
entries" below).

## Clone and source loaded together (confirmed-runtime)

Run `20261021-225831` (`-ProbeIds 266,304`): the player possesses 266, then
304 spawns in front while 266 is still loaded and the player switches into
it. Both stay resident, no crash. Shared model and texture UIDs are fine,
at least for two cars.

## Interior and exterior entries

Rendering the entries settled it (see `vehicle-model.md`): N+1000 is the
interior, N+2000 a clean exterior. In game, `VehicleLodSpooler`
reports both loaded for a driven car. `spoolsystem.IsEntityLoaded` returns
true for any package name, even made-up ones, so it can't tell which
entries are resident.

## Traffic (open)

`TrafficSpooler.RequestTrafficVehicles` is `(count, {ids})`: the binding
(0x0066E560) reads an integer at stack -2 and a table at -1, then hands the
list to 0x005A79F0. Called as a single table it fails with "bad argument #-2
(number expected, got function)".

Run `20261021-230611`: `RequestTrafficVehicles(17, {the 16 models in traffic,
304})` returns nil without error and `AllTrafficVehiclesLoaded` stays true,
but no 304 appeared in nearby traffic over 80 s of sampling (only the
player's own car). The game probably rebuilds the traffic set from its own
district/time-of-day Lua (compiled in `ScriptsLuaScripts.fchunk`). Felony
Rework's readable `trafficspoolingdata.lua` only lists mission vehicle pots.

## Forza geometry in game (confirmed-runtime)

Converter: `src/dsfforza/convert/forza2dsf.py` (FH1 carbin в†’ DSF VEHC), `src/dsfforza/convert/exp_hollow.py`
(the layout that currently works for the full entry).

- **Geometry writing works**: stretching the clone's vertices 1.3x in x shows
  a 1.3x wider car with correct shading (`img/clone-304-wide.jpg`).
- **Normals (VDCL semantic 5)**: filling them with a constant flattens the
  lighting and reflections; they are spherical angles (byte 0 polar, byte 1
  azimuth), see `vehicle-model.md`.
- **The player's car is drawn from the N+2000 entry.** Rebuilding only that
  entry with the FH1 370Z shows the 370Z body while driving (run
  20261022... `-235127`). The full entry (N) is used for traffic, damage and
  physics.
- **Axes**: both use +y up, but Forza's front is at -z and DSF's at +z. An
  earlier note here said the axes match; that was a misread screenshot. In
  runs `-002224` and `-004928` the chase camera sees the 370Z's headlights
  and grille, so the car sits backwards on its chassis (the vanilla Desanne
  shows its tail lights from the same camera). `forza2dsf.Placement` now
  turns the body 180В° about y (confirmed in run `20261022-015851`, below).
- **Paint UV0** samples the vehicle's chassis texture; Forza UVs on the host's
  texture gave stripes. Pinned to a plain texel for now.
- **Full entry crash** (DriverNGHook fatal-error dialog while spooling,
  pure-virtual path, no dump): happens whenever the host's 243 SBMC records
  are removed from the full entry's DRSP, regardless of size (16k vertices),
  materials kept or not, empty parts filled or not. Works when the host's
  SBMC records stay in place, even "hollowed" (every host SBBH pointing at
  one degenerate triangle in a new tiny buffer, host VXCK/IXCK removed;
  runs `-001704`, `-001908`). So something needs the host submodel records
  (count/order/UIDs), not their vertex data. (Root cause: the UID entry
  below.)
- **Size**: a kind-0 MDXN of 0x29B000 crashed in the otherwise working
  layout (run `-001224`); the limits are in `spool-limits.md`.
- **First complete car** (`out/370z-v1.sp`, run `20261022-002224`): hollow
  full entry with FH1 LOD3/4/5, rebuilt clean exterior (LOD1), wheel hubs
  moved to the 370Z's wheelbase and track. Loads, spawns, drives, clean
  paint, but the body is backwards (see Axes). Lights and badges are untextured (trim material).
- **Full-entry crash root cause**: new resource UIDs that don't look like
  vanilla ones. The engine marks resolved pointers with the top bit of UID
  byte 7, so a counter there broke past 0x80. Vanilla-like UIDs (byte 1 =
  0x12, byte 7 = 0x2D, `Builder.new_uid`) let a full replacement with no host
  geometry load (runs `20261022-004457`, `-004637`).
- **Textured full replacement** (`out/370z-v2.sp`, run `20261022-004928`):
  FH1 LOD3/4/5, `--keep-host-materials --textures replace --merge-submodels`.
  Spools, spawns, drives, and a second copy spawns ahead. Badge, headlights
  and grille show textured (`img/fh1-370z-v2-textured.png`), but the body is
  backwards (see Axes). The windscreen
  looks cracked, probably a glass texture/UV mismatch; not checked yet. HUD
  still says Desanne. The rest of that series (LOD2 build, limit tests) was
  stopped before it ran.

## Three ported cars with cockpits (run `20261022-015851`)

`out/cockpit/ported-cockpit.sp`: 304 FH1 370Z, 305 FH1 BMW M5, 306 FM4
Skyline GT-R R32, host 266. All three spool, drive and face the right way;
the cockpit camera switches to the converted interior (confirmed-runtime).
Defects, with causes found offline (confirmed-static) and fixed in the
converter (in game: "Fix2 in game" below):

| Defect | Screenshot | Cause | Fix |
| --- | --- | --- | --- |
| Cracked glass on an undamaged car, worst from the cockpit | `img/cockpit-v1-a.png`, `img/skyline-chase-v1.png` | Glass UV0 spread over v 0..0.97; vanilla glass keeps v in 0..0.25, the clean band of the engine's glass texture (`vehicle-model.md`) | `forza2dsf.glass_uv` (full, exterior, interior) |
| Cockpit nearly black, only the needle pivots lit; paint black on every converted car | `img/cockpit-v1-a.png`, `img/cockpit-v1-skyline.png` | The fourth position component is a baked sky-light term; ours was 0 (needles 4096) | `forza2dsf.vertex_sky` from Forza's baked AO byte |
| Driver's head through the roof in chase view | `img/skyline-chase-v1.png`, `img/fh1-370z-v2-textured.png` | The driver sits on the UPVH driver-seat marker (role 10), still at the Desanne's seat | `cockpit.patch_seats`: seat under the new eye, host eye-to-seat offset (`cockpit.md`) |
| (found on the way) Body 0.14 to 0.23 m too low on its wheels | | Forza y = 0 is the body bottom, the ground is StockRideHeight below (`forza-sources.md`) | `CarDims.ride_height` in `Placement` |

Rebuilt archives (same IDs, `port_car.py` defaults):

- `out/fix2/ported-fix2.sp`: all four fixes; glass bound to the chassis.
- `out/fix2/ported-fix2-links.sp`: the same plus `--glass-links`
  (windscreen and rear window bound to the host's glass-panel records, like
  vanilla glass). Test only if the first still shows cracks.

Offline evidence: `img/placement-before-after.png` (side view, wheel model
on the UPVH hubs; left the tested archive, right fix2: tyres were sunk into
the arches), `img/skyline-seat-after.png` (cutaway at the driver's plane:
red head box under the roof, green seat marker, yellow eye). In the tested
archive the Skyline's driver-seat marker was also on the wrong side (the
Desanne's +x; the R32 is right-hand drive); it now follows the eye.

To check in game: clean glass in chase and cockpit view, a lit dashboard
and dials, coloured paint (the Desanne palette), the driver's head under the
roof, tyres inside the arches.

### Fix2 in game (run `20261022-023610`, confirmed-runtime)

`out/fix2/ported-fix2.sp`, screenshots every 3 s:

- **Glass**: clean in chase view on all three cars; no cracks in any cockpit.
- **Driver**: head under the roof on all three (`img/fix2-370z-chase.png`,
  `img/fix2-skyline-chase.png`).
- **Body on wheels and orientation**: correct; rear lights face the chase
  camera.
- **Paint**: coloured (M5 white, `img/fix2-m5-chase.png`).
- **Cockpit**: 370Z dials lit and readable (`img/fix2-370z-cockpit.png`);
  Skyline right-hand drive, wheel and dials in place
  (`img/fix2-skyline-cockpit.png`).
- **Still wrong**: the M5 windscreen shows broad white streaks from the
  inside and its dash-top and button icons are bright white blocks
  (`img/fix2-m5-cockpit.png`); the M5 dials are dark. HUD still says
  Desanne.

### Fix3: Forza handling, vanilla baseline (run `20261022-024010`)

`out/fix3/ported-fix3.sp` (fix2 plus Forza-derived VO3D from
`src/dsfforza/build/handling.py`, wired into `port_car.py`; `--no-handling` keeps the
host's). Probe order 266, 304, 305, 306.

- All four spool, drive and switch to cockpit; the handling VO3Ds load
  (confirmed-runtime). How they drive is untested: the probe gives no input.
- Vanilla Desanne cockpit for reference: clean windscreen
  (`img/fix3-desanne-vanilla-cockpit.png`).
- **Damaged ported car turns into the host.** The Skyline took damage while
  moving on the zap boost (cracked windscreen, `img/fix3-skyline-damaged-cockpit.png`,
  the expected damage look); the chase camera then shows a damaged gold
  Desanne in its place with continuous HUD speed
  (`img/fix3-skyline-damaged-becomes-desanne.png`). Cause and fix: Fix4
  below.

### Fix4: unique part UIDs (run `20261022-025601`, confirmed-runtime)

Cause of the host swap (confirmed-static): the clones kept the Desanne's 41
MODL part UIDs, in all three entries, and so did every ported car. Part
UIDs key a global table like resource UIDs, and all 6,139 vanilla ones are
unique (`upvh.md`, "MODL part UIDs are global keys too"). Every other
reference in the cloned entries (SBBH, MDID, VXDP/IXDP, MARP textures, MODL
LOD lists) already resolved inside the clone. In the fix3 run the swap came
right after the probe's `VehicleLodSpooler.RequestVehicle(266)`, which
re-spooled 266's interior and exterior while the Skyline was driven (log:
266 `interior=true exterior=true`, 305 evicted). The damage just before it
was probably a coincidence.

`out/fix4/ported-fix4.sp`: `clone_vehicle.renew_uids` now renews part UIDs
too and rewrites the UPVH's references to them (header, records, LOD links,
attachments). Same probe order 266, 304, 305, 306; the log shows the same
state after `RequestVehicle` (266 interior and exterior loaded, 306 still
driven). The Skyline stays a Skyline to the end
(`img/fix4-skyline-after-host-respool.png`). It took no damage in this run,
so a damaged ported car next to a loaded host is still unseen.

Regression check: `upvh.py lint` fails when a car shares a resource or part
UID with another vehicle, or references a UID only another vehicle defines
(no vanilla vehicle trips it; all three fix3 cars do); `port_car.py` runs it
on every car.

The M5 windscreen still streaked, now as thinner diagonal lines
(`img/fix4-m5-cockpit-streaks.png`): fix4 squashed interior glass u into
the 0 to 0.12 strip vanilla interiors use, but kept v 0 to 0.25.

### Fix5: interior glass in the flat block (run `20261022-030613`, confirmed-runtime)

The glass texture is in the archive after all (SSIC; `vehicle-model.md`,
"The glass texture"). The interior strip holds a grey smear texture at
v 0 to 0.06 above flat black; vanilla interiors put two thirds of their glass
in the black block. `out/fix5/ported-fix5.sp` maps interior glass into that
block (u 0.065 to 0.115, v 0.13 to 0.245) and exterior glass into the flat
windscreen rows (u 0.14 to 0.98, v 0.004 to 0.12, clear of the strip and
the rear-window defroster lines).

- M5 windscreen clean from the inside (`img/fix5-m5-cockpit.png`); 370Z and
  Skyline cockpits clean too (`img/fix5-370z-cockpit.png`,
  `img/fix5-skyline-cockpit.png`).
- Exterior glass looks as before in chase view on all three.
- Part UIDs: the Skyline again stays a Skyline after 266's re-spool
  (`img/fix5-skyline-after-host-respool.png`).
- `register.lua` (loaded from `bootstrap.lua` before the probe) runs
  without errors in fix4 and fix5: `vehicleStats[304..306] <- [266]` with
  Nissan/370Z, BMW/M5, Nissan/SkylineGTR and `Selectable`, and
  `SetInterestingVehicleRegions: 17 regions, 51 entries added`. Its traffic
  samples count ported cars near the player before the probe spawns them
  (fix4: one 306 at t101, while only 266 had been spawned; later up to three
  306s, one 304 and one 305), so the interesting-vehicle regions seem to put
  them into traffic. Not yet checked on screenshots. The HUD still says
  ASYM Desanne on all three cars, so the name fields it sets are not the
  ones the HUD reads (or the HUD caches them before the copy).
- Still wrong on the M5: white button icons on the steering wheel and dash,
  dark dials.

### Full detail, HUD names (run `20261022-031619`, confirmed-runtime)

`out/fix6/ported-fix6.sp`, built with `port_car.py --mdxn-limit 0x2C0000`
after the limit tests (`spool-limits.md`): all three cars at the first LOD
step (exterior Forza LOD 1, full 3/4/5). All load, drive and switch to the
cockpit. `register.lua` (now in `bootstrap.lua`) matches each car to a
vanilla vehicleStats entry by Picture1: the HUD shows the Nissan badge and
"370Z" for 304 (copied from 227) and the Nissan badge and "SKYLINE" for
306 (badge and Model index of 229); 305 (BMW, no BMW in DSF) keeps the
Desanne's (`img/fix6-370z-hud-name.png`). The HUD reads the numeric
Manufacturer/Model fields; ManufacturerName/ModelName strings don't reach it.

Cockpit camera too low: vanilla head cameras see the steering rim's top 7.9В°
below eye level (median of 146, `out/eye/wheel_view.py`); ours were 6.5В°
(370Z), 3.3В° (M5), 1.9В° (Skyline), so the wheel filled half the screen.
`cockpit.camera_eye` now raises the camera only (in game: "Head camera"
below).

### Static needles in testdrive7 and the fix (runs `host-20261023-174440`, `-234741`, confirmed-runtime)

`out/testdrive7` with `-HoldMph '30,60,90'`, IDs 147, 22, 1, 32 (the game
exited during ID 1). At 30 and 60 mph the Saleen S5S Raptor (22) shows its dial faces
but no speedometer or tachometer needle (`img/gauges-static-testdrive7-22.png`).
Cause: `damage.interior_uv1` overwrote the needles' gauge ids (`cockpit.md`,
"Static needles in the 40-car build"). The fixed build `out/gauges/build`
(1 S5S Raptor, 2 M3 E30, 3 370Z, 4 Charger Daytona, 5 M5), run
`host-20261023-234741`: needles draw and move on all five, tracking the
forced 30/60/90 mph (`img/gauges-ingame-fixed.png`).

### Gauges (runs `20261022-035058`, `20261022-035518`, confirmed-runtime)

`out/gauges/ported-gauges1.sp` (`port_car.py` defaults; hosts 227, 147,
229): gauge tables and VO3D p605 calibrated from each car's CarAttribs tick
table (`cockpit.md`, "Gauges"). New harness options: `-DriveSeconds 12`
(run 1) holds the Up arrow, DSF's default accelerate key, and the probe
logs `gameVehicle.speed`; run 2 adds `-HoldMph '30,60,90'` (the probe sets
the speed every frame) with `-ProbeIds '266,304,305,306'`.

- **Needles move** with speed and rpm on all three cars, clockwise, from 0
  (confirmed-runtime). Read against the HUD speed
  (`img/gauges-ingame-clusters.png`; 370Z at 29 and 94 mph, M5 at 98 and
  111, Skyline at 44 and 83): 370Z speedometer at the 100 mark at 94 mph;
  M5 (km/h dial) just left of vertical (about 155 km/h) at 98 mph and at
  the top (about 180) at 111; Skyline (km/h) near 70 at 44 mph and near 135
  at 83. Each is within about one minor mark of the HUD; the screenshots
  and the HUD lag each other by a frame or two, so this is not a precise
  check. Tachometers sit at plausible values for the gear (370Z about 5500
  at 29 mph in first). Full frames: `img/gauges-370z-94mph.png`,
  `img/gauges-m5-111mph.png`, `img/gauges-skyline-50mph.png`; vanilla
  Desanne at 91 mph for comparison: `img/gauges-desanne-vanilla-91mph.png`.
- **Throttle**: holding Up works (Skyline 17 to 50 mph in run 1). Driver.log
  reaches the disk seconds late, so the harness can't see the probe's
  phases in time; it now holds the throttle from "PLAYER IS DRIVING IT" to
  DriveSeconds after "cockpit view on". In run 1 the 370Z's cockpit phase
  had passed before the harness saw it (0 mph). Forced speeds
  (`hold_mph`) are exact in the probe's log (30.00, 60.00, 90.00 mph).
- **Head camera** (`cockpit.camera_eye`): the rim now sits in the lower
  third of the screen with the dials above it, not across half the screen
  (`img/gauges-m5-111mph.png`, `img/gauges-skyline-50mph.png`); still a
  little higher than the vanilla Desanne's (`img/fix3-desanne-vanilla-cockpit.png`).
  `out/eye/wheel_view.py`: rim top 6.5В° (370Z), 4.6В° (M5), 5.2В° (Skyline)
  below the eye; the hosts 227, 147, 229 have 8.3, 8.3, 9.1, the Desanne
  13.7. `camera_eye` aims for 7.9В° using the rim's centre-line radius and
  the wheel centre's depth, while `wheel_view.py` measures the topmost rim
  vertex (tube thickness, rim tilt toward the driver), hence the shortfall.
- Digital readouts are blank dark panels (M5 centre display, 370Z).
- Still wrong: the M5 dial faces are dark in some frames (lighting:
  visible in run 1, near black in run 2's shots), M5 button icons.

### Five cars on their own hosts, damage panels (runs `20261022-040320`, `-040545`, confirmed-runtime)

`out/r7/ported-r7.sp`: 304 370Z (host 227), 305 M5 (147 CTS-V), 306 Skyline
R32 (229 R33), 307 Mustang Boss 429 (193), 308 Raptor (251); auto hosts,
`--damage panels`, gauges, lights, factory paint, camera at the vanilla
7.9В° rim angle (all five, `out/eye/wheel_view.py`).

- All five spool, drive (throttle held, up to 100 mph) and switch to the
  cockpit; HUD shows each host's badge and name (Nissan 370Z, Cadillac
  CTS-V, Nissan Skyline, Ford Mustang, Ford F-150).
- `fullVisualDamage` on each: Forza panels crumple and swing, glass cracks
  (`img/r7-skyline-damaged.png`).
- Defects: damage overlay draws large white decal shapes on the paint
  (`img/r7-m5-damaged-decals.png`); Mustang cockpit steering rim and
  rear-view mirror show scrambled textures, bonnet speckled
  (`img/r7-mustang-cockpit-damaged.png`).

## Dyno: how cars drive (runs 20261022-041951 to -055824)

`run-series.ps1 -Dyno` with `tools/game/lua/dsfforza/dyno.lua` (loaded by
`clonetest.lua` when `config.dyno` is set) and `src/dsfforza/harness/dyno.py` for the
figures. Handling results and what they mean for the mapping:
`handling.md`, "In-game validation".

Method (confirmed-runtime unless noted):

- **Place**: after the probe puts the player in a car, `dyno.lua` writes
  `gameVehicle.matrix` every update to (1671.4, 30.6, -4452.0), heading
  -1.0257: a straight, level stretch of `marin_4l_centre_01` (route data in
  the game's own scripts, `out/dyno/roads.py`), about 2 km before the AI
  points stray 4 m from the line. It waits until
  `spoolsystem.IsLocationResident`, lets the car settle, and prints
  `dyno <id> r<k> ready`. Traffic, parked cars and interesting vehicles are
  switched off (`civilianTraffic.setTrafficOnOff(false)`,
  `ParkedVehicles.SetEnabled(false)`, `InterestingVehicleManager.Enable(false)`)
  and `GameVehicleResource.ClearAreaOfVehicles` clears the start. The
  previous car is parked behind the start line. Pedestrians still cross
  (one run in -050855 hit something at 85 mph).
- **Drive**: `run-test.ps1` reads `Driver.log` incrementally; for each new
  ready line it holds the throttle (Up) `-DriveSeconds`, then the brake
  (Down) `-BrakeSeconds`, or runs `-DynoKeys` (one schedule per run; Up,
  Down, Left, Right, Space, Shift, W, A, S, D; Right steers right, Space is
  the handbrake). Keys are pressed and released only at step changes.
  The log reaches the harness within a few seconds; the probe waits for the
  throttle, so the lag doesn't matter.
- **Log**: from the first speed above 1 m/s (with the 100 updates before
  it) one line per Lua update: `dy <id> <run> <n> <speed> <x> <y> <z>
  <lateral> <yaw> <couple>`. Lua updates run at 125 Hz (0.008 s, from
  distance / speed; positions change every update), game time keeps pace
  with real time in the sandbox. Physics steps are 0.04 s (the spec printer's
  constant at 0x00DDB90C).
- **Lane keeper**: with no steering input every car drifts left about 20 m
  in 330 m and hits the kerb (scout run -041951). Writing the matrix to
  hold the line stalls the car (0.6 m/s at full throttle, run -042557), and
  so does forcing `gameVehicle.speed` every update (run -054100: the car
  ignores steering). What works is a sideways couple:
  `GameVehicleResource.applyImpulseAtPoint({gameVehicle, Impulse, Point})`
  in vehicle space at +-1.5 m along the car, closed loop on yaw rate and
  offset. Calibration (run -043154): a couple of 50 for 40 updates turned the
  227 by about 1 rad/s, as an SI impulse on its yaw inertia (VO3D p16 =
  7524) predicts. With it the car stays within 1 to 3 m of the line over
  1.3 km. `-DynoKeep 'impulse:1:24'` lets go for good at 24 m/s, so
  scripted steering acts on a free car.
- **Figures** (`src/dsfforza/harness/dyno.py`): t = 0 where a line through the first
  samples between 1 and 5 m/s reaches the resting speed; 0-30/60/100 mph,
  quarter mile time and trap speed, speed at 10 and 20 s, top speed reached,
  60-0 mph distance from the 60 mph crossing after the peak, and for
  manoeuvres the peak yaw rate, yaw and path change over 3 s (or until 12 m
  off the line), peak slip angle and speed lost.
- **Repeatability**: the same car, run to run: 0-60 within 0.05 s, 0-100
  within 0.05 s, top speed within 0.2 mph, braking within 0.3 m. Steer
  pulses repeat within 1 deg/s; handbrake rotation varies up to 20 deg.
- **Limits**: about 10 cars per session; after that
  `TrafficSpooler.RequestMissionVehicle` stops loading (run -043801: the
  11th to 16th IDs "NOT spooled"). `out/dyno/sim.py`, a simple
  longitudinal model fitted to these runs, reproduces 0-60/0-100/1/4 mile
  only to about 10%, so it isn't used for decisions.

Vanilla and Forza-mapped ports on their own model's host (run -043801,
`out/physics/pairs-a.sp`, two runs each; the Forza mapping as it was before
the DSF-first default):

| Car | 0-60 s vanilla / port | 0-100 s vanilla / port | top in 25 s mph vanilla / port |
| --- | --- | --- | --- |
| 370Z (227 / 304) | 3.26 / 3.42 | 8.94 / 10.74 | 139.1 / 132.3 |
| GT-R (228 / 305) | 4.11 / 3.59 | 8.51 / 7.47 | 150.3 / 158.2 |
| CTS-V (147 / 306) | 3.58 / 3.35 | 9.94 / 8.18 | 130.6 / 145.1 |
| Viper ACR (182 / 307) | 2.59 / 2.80 | 7.58 / 7.02 | 145.3 / 142.2 |
| Gallardo LP570 (212 / 308) | 3.01 / 2.81 | 7.19 / 6.43 | 149.4 / 154.5 |

That spread (-11% to +20% to 100 mph on a car's own host) is what led to
the DSF-first default, which keeps the vanilla values on a same-model host.

### Round 2 (runs 20261022-063701 to -102557)

Handling results: `handling.md`, "Round 2". Harness notes:

- **Launch detection**: the first car of a session creeps from the Shift
  zoom taps (Up) and can pass 1 m/s long before the throttle; `dyno.py` now
  takes the last sample under 1 m/s before the car passes 5 m/s, which
  repaired the vanilla 225 and 228 first runs (225: 2.61 s to 60 mph, not
  11.8).
- **Diagnostic builds**: `out/dyno/build_chain.sh <list> <out>` chains
  `port_car.py` builds (one line per car: media, host or auto, dsf/forza,
  LOO flag) into one archive with a merged `ported.lua`;
  `out/dyno/patch_vo3d.py` then copies chosen parameters from a vanilla car
  into chosen ports, so one session compares several variants of one port
  against its vanilla car (GT-R: -073627; Mito and Scirocco: -081941, -083248,
  -091649, -095156).
- **Steering and handbrake inputs** run on a free car: `-DynoKeep
  impulse:1:22` lets go of the lane keeper at 22 m/s. Holding the speed with
  `gameVehicle.speed` until the input was tried (-054100) and pins the car.
- **Sessions of 10 IDs**: vanilla cars and ports alternate; the HUD bars
  (`Speed`, `Strength`, `Drift`) of ports now come from `ported.lua`
  (`handling_dsf.stat_bars`).
- Another helper's runs (-062630 to -063206, `out/cockpit3`) sit between
  these in the run folder.

### Drive-test archive (run 20261022-140456)

`out/play/play.sp` (+ `out/play/play.lua`, installed as
`tools/game/lua/dsfforza/ported.lua`), built with `out/dyno/build_chain.sh
out/play/list.txt` and the current defaults: DSF-first handling (round-3
rules), hosts auto, cockpit, gauges, lights, paint, `--damage panels`,
`--mdxn-limit 0x2C0000`; two cars on a forced host with `DSF_HANDLING_LOO=1`
(as if DSF didn't have them). HUD bars from `stat_bars`. All eight load,
spawn and drive:

| ID | Car | Host | Handling | 0-60 s | 0-100 s | 1/4 mile s | 60-0 m | handbrake deg / slip | Vanilla reference |
| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 304 | Forza 370Z | 227 370Z | vanilla values (same model) | 3.30 | 8.98 | 11.98 | 16.4 | 70 / 15 | 227: 3.26 / 8.94 / 11.96 / 16.7, 78 / 21 |
| 305 | Forza 370Z | 228 GT-R | DSF-first, as if DSF had no 370Z | 3.26 | 8.94 | 11.96 | 16.8 | 99 / 43 | 227 as above |
| 309 | Forza GT-R 2012 | 240 RUF CTR3 | DSF-first, as if DSF had no GT-R | 3.50 | 7.59 | 11.50 | 16.9 | 87 / 28 | 228: 4.12 / 8.52 / 11.96 / 18.7, 80 / 20 |
| 306 | Forza Skyline GT-R R32 (FM4) | 229 Skyline R33 | DSF-first, family host | 3.06 | 7.94 | 11.54 | - | 76 / 27 | 229 R33: 2.83 / 7.31 / 11.28 (run -061717) |
| 307 | Forza Mustang Boss 429 | 193 Shelby GT500 '67 | DSF-first, family host | 4.35 | 14.15 | 13.28 | 19.0 | 68 / 11 | 193: 4.60 / 14.47 / 13.64 (run -061717) |
| 308 | Forza BMW M5 2012 | 147 CTS-V | DSF-first (no DSF counterpart) | 3.54 | 7.94 | 11.68 | 17.1 | 91 / 37 | 147: 3.58 / 9.94 / 12.38 / 18.3, 83 / 27 |
| 310 | Forza Focus RS500 | 127 Giulietta | DSF-first (no counterpart, FWD hatch) | 4.71 | 13.07 | 13.35 | 20.3 | 54 / 9 | 127: 6.3 s to 60 mph |
| 311 | Forza SVT Raptor | 251 F-150 | DSF-first, family host (truck) | 4.78 | 13.82 | 13.52 | 25.0 | 46 / 8 | 251: 5.45 / 16.04 / 14.17 / 24.4, 26 / 3 |

The handbrake flick keeps the throttle on after the 0.4 s Space + Right
(`Up:2.5`), so the yaw change includes power-on rotation: a RWD car with
more power or less engine friction rotates further.

How to compare (the user's own drive test): spawn the vanilla car next to the
port (probe `config.lua` with `ids = { 227, 304, 305, ... }`, at most 10)
and switch between them with Shift. 304 should feel exactly like 227; 305
and 309 show how a car DSF doesn't have comes out; 306, 307, 310 and 311
show family hosts and classes; 308 (M5 on the CTS-V) slides more than the
CTS-V (106 vs 84 deg in the handbrake flick).

### Lamps, paint A/B, damage3 (runs `20261022-111857` to `-114127`, confirmed-runtime)

- `-Lights` harness mode works; the user-defined dark mood darkens the
  scene. Ported tail lamps glow at one constant level in every phase (off,
  headlights, brakes, indicators): they don't respond to `setLights`
  (`img/lamps-dark-phases.jpg`). No vanilla control yet, so it's open whether
  setLights works on the player's car at all (answered later: `lights.md`,
  "Vanilla control and real driving").
- Paint A/B (Forza or host palette x host or grey paint cell): identical in
  all four; 370Z black, M5 grey, Skyline black (`img/paint-ab-runs.jpg`).
  Neither palette nor cell decides the colour; cause open.
- `out/damage3/ported-d3.sp` with `-Damage`: no crash; the M5 rear-bumper
  chevron is gone (`img/d3-m5-damaged.png`).

### User drive test on the real install (out/testdrive, 40 cars, IDs 1-40)

Installed with `tools/install.ps1 -Build out\testdrive -WriteProfile`. A
typo in the player bootstrap (`"dsfforza\register.lua"`: `\r` is a Lua
escape) stopped registration on the first attempt; fixed in
`tools/game/lua/player/bootstrap.lua` and reinstalled. User feedback:

- Bugatti Veyron (28): handling good; materials wrong; wheels misaligned.
- Wheels look broken on every port.
- Plymouth Cuda (7): the whole body is missing.
- Lexus IS F (12) and BMW M3 E30 (1): holes in the interior.
- Garage: ported cars show no name text.
- HUD: the make/model glyphs sit over the health bar instead of above it.
- Pagani Huayra (31): in a drift a wheel seems to lock and the drift can't
  be exited.
  Diagnosis and the drift-recovery test: research/handling.md, "Driving
  feedback: Pagani Huayra".

### Wheels and the invisible Cuda (runs `20261022-163835`, `-164332`, `-164459`, confirmed-runtime)

- `out/wheels/wheels-test.sp` (wheel orientation, rear-axle size, wheelbase
  sign fix): the M3 E30 looks right, wheels in the arches
  (`img/wheels-m3-ingame.png`).
- The Cuda on host 172 renders nothing but wheels, driver and hands, in
  chase and cockpit view. Same with the paint palette disabled. Built on
  host 193 it renders fine (`img/cuda-host172-vs-193.jpg`). So it's a
  host-172-specific converter bug, not geometry or paint.

### Round 4 harness (runs 20261022-165036 to -184447)

- **Run alignment**: the trailing brake key of one schedule could reach the
  next car before Driver.log told the guest that the run had ended (the log
  reaches the guest seconds late). It reversed the car and started its run,
  so every later schedule of that car went to the wrong run (a launch run
  with handbrake keys, and so on). `dyno.lua` now starts a run only on
  forward motion from where the car last stood. `run-test.ps1` sends keys
  once per run (`$dynoDone`) and times '@mph' steps from the last standstill
  before the target speed.
- **Lifted handbrake test**: `handling_tune.KEYS` (runs 2 and 3 a 0.4 s flick
  with the throttle lifted, run 4 a 1 s step steer) repeats to 0 to 8 deg of
  rotation on the vanilla cars (6 to 21 deg with the old throttle-on flick).
  `measure()` classifies by run number: a step steer can turn the car more
  than 25 deg.
- **Drift recovery** (`handling_tune.py recovery`, `dyno.manoeuvre` recover_s
  and slip_area): see research/handling.md, "Driving feedback: Pagani
  Huayra".
- **Play archive** (`out/play/play.sp` + `play.lua`, now also
  `tools/game/lua/dsfforza/ported.lua`): IDs 304 to 314, the p4 set plus the
  Huayra, built from the vanilla archive with the 10-year donor rule and the
  tuned cache (`out/play/list5.txt`; round-3 build kept as `play-r3.sp`).
  Smoke run 20261022-201655: every car loads and drives. Huayra 314 comes
  out of the power drift in 2.7 s (slip area 77; vanilla Zonda 3.9 s, 81). The
  CTS-V, GT-R, Mito and 370Z take 2.0 to 3.6 s. The Huayra's launch run
  started with the car rolling back, and dyno.py misreads its 0-60.
- **Vanilla files**: builds and fleet/host caches read the vanilla archive (out/vanilla or
  dsfforza-backup) through `game_paths.vanilla()`. The installed test-drive
  archive at D:\...\Install put the 40 ports into `dsf-fleet.json` for a
  while; they had no vehicleStats, so the fleet fits and the donor choice
  ignored them.

### Off-road test (runs vm-20261024-093536 to -114701)

Results: `handling.md`, "Off road". Harness notes:

- **Where**: the dirt start of the game's off-road challenge
  (`Challenges/routedata/offroadroute.lua` checkpoint 2, road
  `sutro_shortcut_15`): `-DynoStart '-3908.5,36,3396.4,-1.4798'`, heading west
  along a dirt track between a pipe wall and a guard rail, about 35 m of dirt
  (to x = -3945, then tarmac), slightly downhill (`img/offroad-wrx-dirt.jpg`).
  Fast cars leave the dirt after about 2 s of throttle, so the keys are
  `Up:2,Down:5;Up:1,Up+Right:3,Down:4` and `dyno --dirt` reports 0-10 and
  0-20 mph, the largest braking deceleration over 0.5 s after the peak and the
  yaw response to the steer while still on the dirt.
- **Game time, not keys**: the guest holds keys for wall-clock seconds, and a
  VM on a loaded host runs the game slower (in -111001 the vanilla A4's 2.5 s of
  throttle acted for 1.1 s of game time). Launch times, deceleration and yaw
  rate come from the Lua update count (0.008 s each) and stay comparable;
  peak speeds and where the car brakes don't.
- **Sand**: the Ocean Beach sand west of the Great Highway (x < -4480, z 1600
  to 3000) isn't simulated: a car dropped there stays frozen in the air
  (`-DynoScan`, runs -094437, -095453); the promenade next to it is
  pavement. DSF's soft surfaces share one mechanism (surface table flag +0x10,
  `handling.md`), so the dirt track stands in for sand.
- **`gameVehicle.isOffRoad`** (logged as the last field of every `dy` line and
  by `-DynoScan`) counts wheels off the road network for the scoring, not the
  tyre surface: 4 on the beach promenade's pavement, 0 on the dirt track.
- **`-DynoScan 'x,y,z;...'`**: the first car is dropped at each point and held
  about 3 s (a screenshot with `-ZoomPresses 0 -ShotSeconds 1`), then the runs
  start. The first shots of a session are still the map zoom, so put two dummy
  points first.

## Open

1. A damaged ported car while the host is loaded (no damage in the fix4
   and fix5 runs), and how converted cars deform: their bodies are one
   part, so bumpers can't hang off (`vehicle-model.md`).
2. M5 cockpit: white button blocks, dark dials (dial faces vary with lighting).
3. HUD name and badge for ported cars: done (numeric `Manufacturer`/`Model`
   fields, then `hud.lua` glyphs; `gui-assets.md`).
4. Raising the per-vehicle size limits.
5. Garage and vehicle-select screens with a new ID: done for IDs 1-40 and,
   with dsfforza.asi, 304+ (`asi.md`, "In game").
