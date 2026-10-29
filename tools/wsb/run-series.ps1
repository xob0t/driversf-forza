<#
.SYNOPSIS
Runs tools/wsb/guest/run-test.ps1 once per archive in the persistent sandbox,
one after another, and prints each run's outcome and probe lines.

.DESCRIPTION
-OnHost runs the game on this PC instead (when nobody is using it: the game window
takes the foreground). The run folder is built under -HostRunPath, on the game's
volume so files can be hard links. For the series, the Driver profile in Documents
is backed up and switched to a -Resolution window, and the default speakers are
muted (tools\wsb\host-session.ps1); both are restored afterwards, while out\wsb-pause
holds the runs, and, if the series is killed, by a watcher process once its game has
closed (failing that, by the next series of any kind).

-Vm runs the game in a headless VMware test VM (tools\wsb\vm.ps1; built by
vm-build.ps1): nothing shows on the host desktop and the host profile and speakers
are left alone. The series takes a free VM of the pool (vm-common.ps1: $env:DSF_VMS,
else C:\VMs\dsfforza-test and dsfforza-test2 if they exist; a running one first), or
waits for the first to come free, behind the series that were waiting before it (first
come, first served; vm-common.ps1 Enter-VmPool); -VmId pins one (1, 2, or a name). Each
run takes the host game folder's current dsf_enhanced.asi and .ini (copied into the run
folder's host-game\), not the guest's C:\Game copies, unless -Override names them;
result.json "dsfEnhanced" has their hashes. Each VM has its own
mutexes (Local\dsfforza-vm-run-<id>; VM 1 also the old Local\dsfforza-vm-run), so VM
series don't wait for sandbox or host series, nor for a series on another VM. The VM
is started (no window) if it isn't running, the host's Driver profile is copied into
it as a -Resolution window, and each run lands in runs\vm<id>-<stamp> with
runs\latest.txt pointing at it. At the end the VM is left running for the next series;
one that no series has used for $env:DSF_VM_IDLE_MINUTES (default 15; negative: never)
is powered off.

.EXAMPLE
.\tools\wsb\run-series.ps1 -Archives out\a.sp,out\b.sp -ProbeIds 304
.\tools\wsb\run-series.ps1 -OnHost -Archives out\a.sp -ProbeIds 304
.\tools\wsb\run-series.ps1 -Vm -Archives out\a.sp -ProbeIds 304
#>
param(
    [Parameter(Mandatory)] [string[]]$Archives,
    [string]$Override,            # game files to replace, repo-relative: 'Install\gui.sp=out\gui\gui.sp;media\Fonts.fchunk=out\gui\Fonts.fchunk';
                                  # root files too ('dsf_enhanced.asi=out\x\dsf_enhanced.asi;dsf_enhanced.ini=out\x\test.ini')
    [string]$Asi,                 # repo-relative plugin, e.g. out\asi\dsfforza.asi (its .ini is copied too)
    [string]$LuaFiles,            # UserLuaScripts files replaced after the repo copy: 'dsfforza\hud.lua=out\x\hud.lua;dsfforza\ported.lua=out\x\dngvehicles.lua'
    [switch]$ArchiveOnly,         # an archive named dngvehicles.sp goes with its build's dngvehicles.lua and gui\ (each car's
                                  # vehicleStats entry, badge and plate) unless this is set (run-test.ps1)
    [string]$ProbeIds = '304',
    [int]$WaitSeconds = 60,
    [int]$ZoomPresses = 5,
    [int]$ShotSeconds = 10,
    [int]$BurstShots = 1,         # with -HoldMph or -DriveSeconds: shots per sample in quick succession (observe-NN-bK.png)
    [string]$AheadIds,
    [string]$TrafficIds,          # IDs for TrafficSpooler.RequestTrafficVehicles after the probe IDs (clonetest.lua); none of the probe IDs (the game traps: research/in-game-tests.md, "Traffic"); keeps traffic on
    [switch]$Traffic,             # leave traffic on during the probe (default: off, and the road cleared around each probe car)
    [int]$DriveSeconds = 0,       # hold the throttle this long in each cockpit view
    [string]$DriveKey = 'Up',
    [string]$HoldMph,             # e.g. '30,60,90': speeds forced in turn in each cockpit view
    [switch]$Damage,              # probe applies full visual damage 3 s into each chase view
    [switch]$DamagePanels,        # with -Damage: then opens (~6 s) and removes (~9 s) every panel
    [switch]$DamageSide,          # probe hits a parked side-on copy of each car (wing, door, quarter; clonetest.lua SIDE_COPY)
    [switch]$GlassLadder,         # probe damages each car in steps during its cockpit view (~21 s; glass crack states)
    [string]$Views,               # extra views after each chase view, ~6 s each: 'Bonnet,Bumper,left,right,front' (clonetest.lua)
    [int]$Palette = -1,           # paint palette entry of every probed car (clonetest.lua shaderParams[0]); -1: the game's pick
    [switch]$LightsDriveOnly,     # with -Lights: only the day and dark drives (no setLights phases)
    [switch]$Lights,              # lamp phases per car, two shots each (dsfforza\lightstest.lua); allow ~120 s per car
    [switch]$Dyno,                # standing-start runs (dsfforza\dyno.lua); analyse with src/dsfforza/harness/dyno.py
    [int]$BrakeSeconds = 6,
    [int]$DynoRuns = 2,
    [string]$DynoStart,           # 'x,y,z,heading'
    [string]$DynoScan,            # 'x,y,z;x,y,z;...': surface scan before the first car's runs (dyno.lua)
    [string]$DynoKeep,            # lane keeper mode per run, e.g. 'none,pulse:5'
    [string]$DynoKeys,            # key steps per run, e.g. 'Up:25,Down:6;Up:5,Up+Right:0.5,Down:5'
    [string]$MenuKeys,            # menu taps after -MenuTrigger appears in Driver.log, e.g. 'Enter:2,Right:1.5'
    [string]$MenuTrigger,
    [double]$TrafficRadius = 0,   # traffic road radius in metres, patched in memory once the world is up (350 vanilla)
    [int]$StallSeconds = 120,     # Story stage: a load whose Driver.exe private bytes make no new high this long is hung
    [int]$StorySeconds = 900,     # Story stage: the cap for a slow but progressing load
    [int]$CpuGate = 70,           # before each game launch, wait until the host's competing CPU (processes above below-normal
                                  # priority) stays under this percent for 15 s; 0: don't wait (issue #70)
    [int]$CpuGateSeconds = 600,   # longest wait at the CPU gate; the run starts anyway after it
    [switch]$OnHost,
    [switch]$Vm,
    [string]$VmId,                # with -Vm: this VM only (1, 2, or a name; vm-common.ps1)
    [string]$GamePath = $env:DSF_GAME_DIR,
    [string]$HostRunPath,          # default: <game drive>\dsfforza-run (same volume, for hard links)
    [string]$Resolution = '1280x720'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$outputHost = Join-Path $env:LOCALAPPDATA 'Temp\dsfforza-wsb'
$sandbox = Join-Path $PSScriptRoot 'sandbox.ps1'
$vmScript = Join-Path $PSScriptRoot 'vm.ps1'
$hostSession = Join-Path $PSScriptRoot 'host-session.ps1'
$runTest = Join-Path $PSScriptRoot 'guest\run-test.ps1'
$hostState = Join-Path $outputHost 'host-session'
$pauseFlag = Join-Path $repo 'out\wsb-pause'
if ($OnHost -and $Vm) { throw '-OnHost and -Vm exclude each other' }

# run-test.ps1 options with the same name here are passed on as given, so a new option needs
# only its two param lines. Archive, Asi, Override and LuaFiles become guest paths (Get-GuestArgs);
# OnHost, Vm, GamePath, HostRunPath and Resolution are this script's own.
if (-not $GamePath) { throw 'Set DSF_GAME_DIR (or pass -GamePath) to your Driver: San Francisco folder, the one with Driver.exe.' }
if (-not $HostRunPath) { $HostRunPath = Join-Path ([IO.Path]::GetPathRoot($GamePath)) 'dsfforza-run' }
$seriesArgs = $PSBoundParameters
$ownParameters = $MyInvocation.MyCommand.Parameters
$passThrough = @((Get-Command $runTest).Parameters.Keys | Where-Object {
    $ownParameters.ContainsKey($_) -and $_ -notin 'Archive', 'Asi', 'Override', 'LuaFiles', 'OnHost' })

# One series at a time: the guest runs one game and runs\latest.txt names the
# newest run, so concurrent series would read each other's results. Callers
# queue here (Windows releases the mutex if its owner dies, so it can't go stale).
# Each VM is a separate machine, so -Vm series queue per VM instead: a free VM of the
# pool, else the first to come free (vm-common.ps1 Enter-VmPool).
# (Two series can still both write runs\latest.txt; each reads back its own run folder
# right after starting it.)
$runMutex = New-Object System.Threading.Mutex($false, 'Local\dsfforza-wsb-run')
$script:ownsMutex = $false
$script:vmHeld = $null
function Enter-RunMutex([TimeSpan]$Timeout) {
    try { $script:ownsMutex = $runMutex.WaitOne($Timeout) } catch [System.Threading.AbandonedMutexException] { $script:ownsMutex = $true }
    return $script:ownsMutex
}
function Exit-RunMutex {
    if ($script:ownsMutex) { $runMutex.ReleaseMutex(); $script:ownsMutex = $false }
    if ($script:vmHeld) { Exit-VmMutexes $script:vmHeld.Mutexes; $script:vmHeld = $null }
}
if ($Vm) {
    . (Join-Path $PSScriptRoot 'vm-common.ps1')
    $pool = if ($VmId) { @(Get-VmPool -Select $VmId) } else { @(Get-VmPool) }
    if (-not $pool) { throw 'no test VM: build one with tools\wsb\vm-build.ps1, or set DSF_VMS' }
    $script:vmHeld = Enter-VmPool -Pool $pool -Timeout ([TimeSpan]::FromHours(3)) -Say { param($t) $t | Out-Host } `
        -Label "run-series $(Split-Path $repo -Leaf): $($Archives -join ', ')"
    if (-not $script:vmHeld) { throw "timed out waiting for a free VM ($(($pool | ForEach-Object Name) -join ', '))" }
    $vmTarget = $script:vmHeld.Vm
    Use-Vm $vmTarget
    "VM $($vmTarget.Id): $($vmTarget.Name)"
}
elseif (-not (Enter-RunMutex ([TimeSpan]::Zero))) {
    'waiting for another sandbox series to finish...'
    if (-not (Enter-RunMutex ([TimeSpan]::FromHours(3)))) { throw 'timed out waiting for the sandbox' }
}

# -OnHost: the user's Driver profile and speakers (tools\wsb\host-session.ps1). Enter starts a
# watcher that restores them if this series is killed; Restore runs at the end and before a pause.
$script:inHostSession = $false
function Enter-HostSession {
    $script:inHostSession = $true   # first: Restore undoes a half-done Enter too
    & $hostSession Enter -Resolution $Resolution -SeriesId $PID -RunPath $HostRunPath
}
function Exit-HostSession {
    if ($script:inHostSession) { & $hostSession Restore; $script:inHostSession = $false }
}
function Wait-HostPause {
    # Host runs take over the user's screen: while out\wsb-pause exists (the user is on the PC),
    # wait with their profile and speakers back and without holding the mutex, then go on.
    # Checked before every run. Delete the file to let runs go.
    if (-not (Test-Path $pauseFlag)) { return }
    $resume = $script:inHostSession
    Exit-HostSession
    while (Test-Path $pauseFlag) {
        Exit-RunMutex
        "host runs paused ($pauseFlag exists); waiting..."
        while (Test-Path $pauseFlag) { Start-Sleep -Seconds 15 }
        [void](Enter-RunMutex ([TimeSpan]::FromMilliseconds(-1)))
    }
    if ($resume) { Enter-HostSession }
}
function Restore-DeadHostSession {
    # A killed -OnHost series leaves the profile windowed and the speakers muted until its
    # watcher restores them; after a reboot that never happens, so any series tries too,
    # when no host series runs (the sandbox mutex is the host series' mutex).
    if (-not (Test-Path (Join-Path $hostState 'complete')) -and -not (Test-Path (Join-Path $hostState 'muted.txt'))) { return }
    $hostMutex = $runMutex; $taken = $false
    if ($Vm) {
        $hostMutex = New-Object System.Threading.Mutex($false, 'Local\dsfforza-wsb-run')
        try { $taken = $hostMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $taken = $true }
        if (-not $taken) { return }
    }
    try { & $hostSession Restore; '  restored the Driver profile and speakers a killed -OnHost series left' }
    catch { "  host session not restored: $($_.Exception.Message)" }
    finally { if ($taken) { $hostMutex.ReleaseMutex() } }
}

function Confirm-Sandbox {
    # Start (or replace) the sandbox when none is running or its guest doesn't answer.
    if ("$(& $sandbox Status)" -notmatch 'guest answers: True') { & $sandbox Start | Out-Null }
}

# Repo-relative path -> the path the test script sees (C:\Repo in the sandbox).
$repoSeen = if ($OnHost) { $repo } else { 'C:\Repo' }
function Get-SeenPath([string]$path) {
    $full = (Resolve-Path (Join-Path $repo $path.Trim())).Path
    "$repoSeen\$($full.Substring($repo.Length).TrimStart('\'))"
}
function Get-SeenPairs([string]$pairs) {
    # 'key=repo path;...' -> 'key=seen path;...' (-Override, -LuaFiles)
    @(foreach ($pair in ($pairs -split ';' | Where-Object { $_ })) {
        $name, $path = $pair -split '=', 2
        "$($name.Trim())=$(Get-SeenPath $path)"
    }) -join ';'
}

function Get-GuestArgs([string]$archive) {
    # run-test.ps1's arguments for one archive, by name; run-series' own defaults always go.
    $values = [ordered]@{ Archive = (Get-SeenPath $archive); Hook = $true; Probe = $true
        ProbeIds = $ProbeIds; ZoomPresses = $ZoomPresses; ShotSeconds = $ShotSeconds; WaitSeconds = $WaitSeconds }
    foreach ($name in $passThrough) { if ($seriesArgs.ContainsKey($name)) { $values[$name] = $seriesArgs[$name] } }
    if ($Asi) { $values.Asi = Get-SeenPath $Asi }
    if ($Override) { $values.Override = Get-SeenPairs $Override }   # game files, repo-relative replacements
    if ($LuaFiles) { $values.LuaFiles = Get-SeenPairs $LuaFiles }   # UserLuaScripts files
    return $values
}

function ConvertTo-ArgList($values, [switch]$CommandLine) {
    # -Name value ...; a switch is -Name alone (or nothing when false). -CommandLine: the list is
    # joined with spaces into one command line (wsb exec, the VM's wrapper script), so values
    # with spaces or quotes are quoted the way powershell.exe -File splits its arguments.
    foreach ($name in $values.Keys) {
        $value = $values[$name]
        if ($value -is [switch] -or $value -is [bool]) { if ($value) { "-$name" }; continue }
        $text = "$value"
        if ($text -eq '') { continue }   # as if not given
        "-$name"
        if ($CommandLine -and $text -match '[\s"]') {
            '"' + (($text -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
        } else { $text }
    }
}

function Confirm-Vm {
    # Start the VM headless if needed (also sets the guest display and mutes it), point its
    # C:\Repo at this checkout (a git worktree's builds and scripts, not the main checkout's),
    # then copy the host's Driver profile into it as a -Resolution window.
    # Start's notes (settings written, a slow session restarted) are shown; "<name> ready" isn't.
    & $vmScript Start -Vm $vmTarget.Id | Where-Object { $_ -notmatch ' ready; host output' }
    & $vmScript Share -Vm $vmTarget.Id -RepoPath $repo | Out-Null
    & $vmScript Exec -Vm $vmTarget.Id -Script C:\Repo\tools\wsb\guest\setup-vm.ps1 -TimeoutSeconds 300 `
        -Arguments @('-Phase', 'Profile', '-Resolution', $Resolution, '-Tag', $vmTarget.Name) | Out-Null
}

# -Vm: root files each run takes from the host game folder rather than the guest's C:\Game.
$hostRootFiles = 'dsf_enhanced.asi', 'dsf_enhanced.ini'
$script:hostRootHashes = $null
$script:driftSaid = $false
function Get-HostRootHashes {
    # SHA-256 of the host game folder's root files, once per series: the guest's C:\Game should
    # have the same (result.json "gameRoot").
    if ($null -eq $script:hostRootHashes) {
        $script:hostRootHashes = @{}
        Get-ChildItem -LiteralPath $GamePath -File | Where-Object { $_.Extension -notin '.log', '.dmp' } |
            ForEach-Object { $script:hostRootHashes[$_.Name.ToLower()] = (Get-FileHash -LiteralPath $_.FullName).Hash }
    }
    return $script:hostRootHashes
}
function Show-GameFiles($r) {
    # The dsf_enhanced the run used; with -Vm, root files of the guest's C:\Game that differ from
    # the host's (said once per series).
    $e = $r.dsfEnhanced
    $short = { param($hash) if ($hash) { "$hash".Substring(0, 8) } else { 'none' } }
    if ($e) { "  dsf_enhanced.asi $(& $short $e.asi) ($($e.asiFrom)), ini $(& $short $e.ini) ($($e.iniFrom))" }
    if (-not $Vm -or -not $r.gameRoot -or $script:driftSaid) { return }
    $guest = @{}
    foreach ($p in $r.gameRoot.PSObject.Properties) { $guest[$p.Name.ToLower()] = $p.Value }
    $hostHashes = Get-HostRootHashes
    $differ = @(@($hostHashes.Keys) + @($guest.Keys) | Sort-Object -Unique |
        Where-Object { $_ -notin $hostRootFiles -and $hostHashes[$_] -ne $guest[$_] })
    if ($differ) {
        $script:driftSaid = $true
        "  WARNING: the guest's C:\Game differs from the host game folder in $($differ -join ', '); refresh it:" +
            " vm.ps1 Exec -Vm $($vmTarget.Id) -Script C:\Repo\tools\wsb\guest\setup-vm.ps1 -Arguments '-Phase','Game'"
    }
}

$resultSeconds = [math]::Max(600, $WaitSeconds + 420 + [math]::Max(0, $StorySeconds - 150))

function Get-HostCpu([double]$Seconds = 5) {
    # Host CPU over $Seconds as a percent of all logical processors, from per-process CPU time:
    # total, and "competing", i.e. without Idle and BelowNormal priority processes (builds and
    # surveys run at Idle and yield to the VMs). A process whose priority can't be read (vmware-vmx
    # and other elevated ones) counts as competing; the other VM's game is competition too.
    $sample = { $t = @{}; foreach ($p in Get-Process) {
            $cpu = $p.TotalProcessorTime.TotalSeconds; if ($null -eq $cpu) { continue }
            $low = $false; try { $low = $p.PriorityClass -in 'Idle', 'BelowNormal' } catch { }
            $t[$p.Id] = @($cpu, $low) }
        $t }
    $a = & $sample; Start-Sleep -Milliseconds ([int]($Seconds * 1000)); $b = & $sample
    $total = 0.0; $competing = 0.0
    foreach ($id in $b.Keys) {
        if (-not $a.ContainsKey($id) -or $id -eq 0) { continue }
        $d = $b[$id][0] - $a[$id][0]
        $total += $d; if (-not $b[$id][1]) { $competing += $d }
    }
    $scale = 100 / ($Seconds * [Environment]::ProcessorCount)
    [pscustomobject]@{ Total = [math]::Round($total * $scale); Competing = [math]::Round($competing * $scale) }
}

function Wait-CpuGate {
    # Issue #70: on a busy host the game's load slows to a crawl in the VM (the guest gets little
    # CPU). Wait until three 5 s samples in a row have the competing CPU under -CpuGate, at most
    # -CpuGateSeconds. Returns the line to log.
    if ($CpuGate -le 0) { return $null }
    $start = Get-Date; $calm = 0; $last = $null
    while ($true) {
        $last = Get-HostCpu 5
        $calm = if ($last.Competing -lt $CpuGate) { $calm + 1 } else { 0 }
        $waited = [math]::Round(((Get-Date) - $start).TotalSeconds)
        if ($calm -ge 3) { return "CPU gate: passed after $waited s (competing $($last.Competing) %, total $($last.Total) %)" }
        if ($waited -ge $CpuGateSeconds) { return "CPU gate: competing CPU still $($last.Competing) % (total $($last.Total) %) after $waited s; starting anyway" }
        if ($calm -eq 0 -and ($waited -lt 10 -or $waited % 60 -lt 5)) { "  CPU gate: competing $($last.Competing) %, total $($last.Total) %; waiting" | Write-Host }
    }
}

function Test-LoadFailure($r) {
    # A run that never got the Story world up: stalled, capped, crashed or exited while loading.
    $r.stage -eq 'story' -and ($r.storyStalled -or $r.crashed -or "$($r.error)" -match 'Story world not ready|exited in the frontend')
}
try {
    if (-not $OnHost) { Restore-DeadHostSession }
    if ($OnHost) { Wait-HostPause; Enter-HostSession } elseif ($Vm) { Confirm-Vm } else { Confirm-Sandbox }
    # Each archive once; a run that fails while the world loads (issue #70) is retried once,
    # right after, from a new game start.
    $queue = [System.Collections.Generic.List[object]]::new()
    foreach ($a in $Archives) { $queue.Add([pscustomobject]@{ Archive = $a; Retry = $false }) }
    for ($qi = 0; $qi -lt $queue.Count; $qi++) {
        $archive = $queue[$qi].Archive
        if ($OnHost) { Wait-HostPause }
        $gate = if ($OnHost -or $Vm) { Wait-CpuGate }
        if ($gate) { "  $gate" }
        $guestValues = Get-GuestArgs $archive
        if ($OnHost) {
            # Same script, run here and waited for.
            $runDir = Join-Path $outputHost "runs\host-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            New-Item -ItemType Directory -Force -Path $runDir | Out-Null
            Set-Content (Join-Path $outputHost 'runs\latest.txt') $runDir
            $hostArgs = @(ConvertTo-ArgList $guestValues) + @('-OnHost', '-RunDir', $runDir,
                '-GameDir', $GamePath, '-RepoDir', $repo, '-OutputDir', $outputHost, '-RunPath', $HostRunPath)
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runTest @hostArgs *>> (Join-Path $runDir 'host.log')
        }
        elseif ($Vm) {
            # Same script in the VM's desktop session, waited for; the guest writes straight into
            # this run folder through C:\Output (a shared folder). Repo files are copied into the
            # run folder rather than linked across the shared folder.
            $stamp = "vm$($vmTarget.Id)-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            $runDir = Join-Path $outputHost "runs\$stamp"
            New-Item -ItemType Directory -Force -Path $runDir | Out-Null
            Set-Content (Join-Path $outputHost 'runs\latest.txt') $runDir
            # The host game folder's current dsf_enhanced.asi and .ini, copied into the run folder:
            # the guest's C:\Game is a copy from when the VM was provisioned and goes stale
            # (issue #59). -Override still wins. result.json "dsfEnhanced" has the hashes.
            $hostGame = New-Item -ItemType Directory -Force -Path (Join-Path $runDir 'host-game')
            foreach ($name in $hostRootFiles) {
                $path = Join-Path $GamePath $name
                if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination $hostGame.FullName }
            }
            $vmArgs = @(ConvertTo-ArgList $guestValues -CommandLine) + @('-RunDir', "C:\Output\runs\$stamp", '-CopyRepoFiles',
                '-GameRootFrom', "C:\Output\runs\$stamp\host-game")
            $timeout = $resultSeconds + 300
            try { & $vmScript Exec -Vm $vmTarget.Id -Script C:\Repo\tools\wsb\guest\run-test.ps1 -Arguments $vmArgs -TimeoutSeconds $timeout | Out-Null }
            catch {
                if (Test-Path (Join-Path $runDir 'result.json')) { "  vm exec: $($_.Exception.Message)" }
                else {
                    # The VM died or was stopped: bring it up again and retry once.
                    "  vm exec failed ($($_.Exception.Message)); restarting the VM"
                    Confirm-Vm
                    & $vmScript Exec -Vm $vmTarget.Id -Script C:\Repo\tools\wsb\guest\run-test.ps1 -Arguments $vmArgs -TimeoutSeconds $timeout | Out-Null
                }
            }
        }
        else {
            # run-test.ps1 -Detach returns at once; the run folder is read back from runs\latest.txt.
            $guestArgs = @(ConvertTo-ArgList $guestValues -CommandLine) + '-Detach'
            try { & $sandbox Exec -Script C:\Repo\tools\wsb\guest\run-test.ps1 -Arguments $guestArgs | Out-Null }
            catch {
                # The sandbox died since the last run: bring up a new one and retry once.
                "  sandbox exec failed ($($_.Exception.Message)); restarting the sandbox"
                Confirm-Sandbox
                & $sandbox Exec -Script C:\Repo\tools\wsb\guest\run-test.ps1 -Arguments $guestArgs | Out-Null
            }
            $runDir = (Get-Content (Join-Path $outputHost 'runs\latest.txt')).Trim() -replace '^C:\\Output', $outputHost
        }
        $result = Join-Path $runDir 'result.json'
        $deadline = (Get-Date).AddSeconds($resultSeconds)
        while (-not (Test-Path $result) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 3 }
        "=== $archive -> $runDir"
        if (-not (Test-Path $result)) { "  no result after $resultSeconds s"; continue }
        if ($gate) { Set-Content (Join-Path $runDir 'cpu-gate.txt') $gate }
        $r = Get-Content $result -Raw | ConvertFrom-Json
        "  stage=$($r.stage) crashed=$([bool]$r.crashed) error=$($r.error)"
        Show-GameFiles $r
        if ((Test-LoadFailure $r) -and -not $queue[$qi].Retry -and ($OnHost -or $Vm)) {
            "  the world didn't load; retrying once"
            $queue.Insert($qi + 1, [pscustomobject]@{ Archive = $archive; Retry = $true })
        }
        $r.probeLines | Where-Object { $_ -match 'id \d+:' -and $_ -notmatch 'cockpit t\d+ speed' -and $_ -notmatch '\] dy \d' } | ForEach-Object { "  $_" }
        $speeds = @($r.probeLines | Where-Object { $_ -match 'cockpit t\d+ speed ([\d.]+) m/s ([\d.]+) mph' } |
            ForEach-Object { [double]($_ -replace '.* ([\d.]+) mph.*', '$1') })
        if ($speeds.Count) { "  $($speeds.Count) speed samples in cockpit view, top $(($speeds | Measure-Object -Maximum).Maximum) mph" }
    }
} finally {
    try {
        Exit-HostSession
        if ($Vm) {
            # Back to the main checkout (the first worktree), which series from there expect.
            $main = ((git -C $repo worktree list --porcelain) | Where-Object { $_ -like 'worktree *' } |
                Select-Object -First 1).Substring(9) -replace '/', '\'
            if ($main -and (Resolve-Path $main).Path -ne $repo) {
                try { & $vmScript Share -Vm $vmTarget.Id -RepoPath $main | Out-Null } catch { "  C:\Repo not pointed back at ${main}: $($_.Exception.Message)" }
            }
        }
    } finally {
        $idle = -1
        if ($Vm) {
            # Arm the idle stop: a hidden vm.ps1 StopIdle powers the VM off unless a series uses
            # it within the idle time (that series re-arms it with a new stamp).
            try {
                $idle = Get-VmIdleMinutes
                $stampFile = Join-Path $VmOutputHost "vm\$($vmTarget.Name)\last-series.txt"
                New-Item -ItemType Directory -Force -Path (Split-Path $stampFile) | Out-Null
                $seriesStamp = [guid]::NewGuid().ToString('N')
                Set-Content -LiteralPath $stampFile $seriesStamp
            } catch { $idle = -1; "  idle stop not armed: $($_.Exception.Message)" }
        }
        Exit-RunMutex
        if ($idle -ge 0) { Start-Detached $vmScript "StopIdle -Vm $($vmTarget.Id) -IdleMinutes $idle -Stamp $seriesStamp" }
    }
}
