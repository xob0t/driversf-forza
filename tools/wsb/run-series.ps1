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

-Vm runs the game in the headless VMware test VM (tools\wsb\vm.ps1; built by
vm-build.ps1): nothing shows on the host desktop and the host profile and speakers
are left alone. The VM is started (no window) if it isn't running, the host's Driver
profile is copied into it as a -Resolution window, and each run lands in
runs\vm-<stamp> with runs\latest.txt pointing at it. It holds its own mutex
(Local\dsfforza-vm-run), so VM series don't wait for sandbox or host series.

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
    [string]$ProbeIds = '304',
    [int]$WaitSeconds = 60,
    [int]$ZoomPresses = 5,
    [int]$ShotSeconds = 10,
    [string]$AheadIds,
    [string]$TrafficIds,          # IDs for TrafficSpooler.RequestTrafficVehicles after the probe IDs (clonetest.lua)
    [int]$DriveSeconds = 0,       # hold the throttle this long in each cockpit view
    [string]$DriveKey = 'Up',
    [string]$HoldMph,             # e.g. '30,60,90': speeds forced in turn in each cockpit view
    [switch]$Damage,              # probe applies full visual damage 3 s into each chase view
    [switch]$DamagePanels,        # with -Damage: then opens (~6 s) and removes (~9 s) every panel
    [switch]$DamageSide,          # probe hits a parked side-on copy of each car (wing, door, quarter; clonetest.lua SIDE_COPY)
    [string]$DamageRam,           # probe rams a parked copy of each car: 'side,front,rear' (clonetest.lua DAMAGE_RAM)
    [switch]$GlassLadder,         # probe damages each car in steps during its cockpit view (~21 s; glass crack states)
    [switch]$LightsDriveOnly,     # with -Lights: only the day and dark drives (no setLights phases)
    [switch]$Lights,              # lamp phases per car, two shots each (dsfforza\lightstest.lua); allow ~120 s per car
    [switch]$Dyno,                # standing-start runs (dsfforza\dyno.lua); analyse with tools/dyno.py
    [int]$BrakeSeconds = 6,
    [int]$DynoRuns = 2,
    [string]$DynoStart,           # 'x,y,z,heading'
    [string]$DynoKeep,            # lane keeper mode per run, e.g. 'none,pulse:5'
    [string]$DynoKeys,            # key steps per run, e.g. 'Up:25,Down:6;Up:5,Up+Right:0.5,Down:5'
    [string]$MenuKeys,            # menu taps after -MenuTrigger appears in Driver.log, e.g. 'Enter:2,Right:1.5'
    [string]$MenuTrigger,
    [double]$TrafficRadius = 0,   # traffic road radius in metres, patched in memory once the world is up (350 stock)
    [switch]$OnHost,
    [switch]$Vm,
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
# The VM is a separate machine, so -Vm series queue on their own mutex.
# (Two series of different kinds can still both write runs\latest.txt; each reads
# back its own run folder right after starting it.)
$mutexName = if ($Vm) { 'Local\dsfforza-vm-run' } else { 'Local\dsfforza-wsb-run' }
$runMutex = New-Object System.Threading.Mutex($false, $mutexName)
$script:ownsMutex = $false
function Enter-RunMutex([TimeSpan]$Timeout) {
    try { $script:ownsMutex = $runMutex.WaitOne($Timeout) } catch [System.Threading.AbandonedMutexException] { $script:ownsMutex = $true }
    return $script:ownsMutex
}
function Exit-RunMutex { if ($script:ownsMutex) { $runMutex.ReleaseMutex(); $script:ownsMutex = $false } }
if (-not (Enter-RunMutex ([TimeSpan]::Zero))) {
    "waiting for another $(if ($Vm) { 'VM' } else { 'sandbox' }) series to finish..."
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
    & $vmScript Start | Out-Null
    & $vmScript Share -RepoPath $repo | Out-Null
    & $vmScript Exec -Script C:\Repo\tools\wsb\guest\setup-vm.ps1 -Arguments @('-Phase', 'Profile', '-Resolution', $Resolution) -TimeoutSeconds 300 | Out-Null
}

$resultSeconds = [math]::Max(600, $WaitSeconds + 420)
try {
    if (-not $OnHost) { Restore-DeadHostSession }
    if ($OnHost) { Wait-HostPause; Enter-HostSession } elseif ($Vm) { Confirm-Vm } else { Confirm-Sandbox }
    foreach ($archive in $Archives) {
        if ($OnHost) { Wait-HostPause }
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
            $stamp = "vm-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            $runDir = Join-Path $outputHost "runs\$stamp"
            New-Item -ItemType Directory -Force -Path $runDir | Out-Null
            Set-Content (Join-Path $outputHost 'runs\latest.txt') $runDir
            $vmArgs = @(ConvertTo-ArgList $guestValues -CommandLine) + @('-RunDir', "C:\Output\runs\$stamp", '-CopyRepoFiles')
            $timeout = $resultSeconds + 300
            try { & $vmScript Exec -Script C:\Repo\tools\wsb\guest\run-test.ps1 -Arguments $vmArgs -TimeoutSeconds $timeout | Out-Null }
            catch {
                if (Test-Path (Join-Path $runDir 'result.json')) { "  vm exec: $($_.Exception.Message)" }
                else {
                    # The VM died or was stopped: bring it up again and retry once.
                    "  vm exec failed ($($_.Exception.Message)); restarting the VM"
                    Confirm-Vm
                    & $vmScript Exec -Script C:\Repo\tools\wsb\guest\run-test.ps1 -Arguments $vmArgs -TimeoutSeconds $timeout | Out-Null
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
        $r = Get-Content $result -Raw | ConvertFrom-Json
        "  stage=$($r.stage) crashed=$([bool]$r.crashed) error=$($r.error)"
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
                try { & $vmScript Share -RepoPath $main | Out-Null } catch { "  C:\Repo not pointed back at ${main}: $($_.Exception.Message)" }
            }
        }
    } finally { Exit-RunMutex }
}
