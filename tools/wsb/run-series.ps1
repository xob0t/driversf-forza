<#
.SYNOPSIS
Runs tools/wsb/guest/run-test.ps1 once per archive in the persistent sandbox,
one after another, and prints each run's outcome and probe lines.

.DESCRIPTION
-OnHost runs the game on this PC instead (when nobody is using it: the game window
takes the foreground). The run folder is built under -HostRunPath, on the game's
volume so files can be hard links. For the series, the Driver profile in Documents
is backed up and switched to a -Resolution window, and the default speakers are
muted; both are restored afterwards (and on the next call if a series died).

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
    [switch]$DamageSide,          # probe applies localised hits on each car's -x front wing and door
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
    [string]$GamePath = 'D:\Games\Driver San Francisco',
    [string]$HostRunPath = 'D:\dsfforza-run',
    [string]$Resolution = '1280x720'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$outputHost = Join-Path $env:LOCALAPPDATA 'Temp\dsfforza-wsb'
$sandbox = Join-Path $PSScriptRoot 'sandbox.ps1'
$vmScript = Join-Path $PSScriptRoot 'vm.ps1'
if ($OnHost -and $Vm) { throw '-OnHost and -Vm exclude each other' }

# One series at a time: the guest runs one game and runs\latest.txt names the
# newest run, so concurrent series would read each other's results. Callers
# queue here (Windows releases the mutex if its owner dies, so it can't go stale).
# The VM is a separate machine, so -Vm series queue on their own mutex.
# (Two series of different kinds can still both write runs\latest.txt; each reads
# back its own run folder right after starting it.)
$mutexName = if ($Vm) { 'Local\dsfforza-vm-run' } else { 'Local\dsfforza-wsb-run' }
$runMutex = New-Object System.Threading.Mutex($false, $mutexName)
try {
    if (-not $runMutex.WaitOne(0)) {
        "waiting for another $(if ($Vm) { 'VM' } else { 'sandbox' }) series to finish..."
        if (-not $runMutex.WaitOne([TimeSpan]::FromHours(3))) { throw 'timed out waiting for the sandbox' }
    }
} catch [System.Threading.AbandonedMutexException] { }

# Host runs take over the user's screen: while out\wsb-pause exists (the user is on the PC),
# wait without holding the mutex, then take it again. Delete the file to let runs go.
if ($OnHost) {
    $pauseFlag = Join-Path $repo 'out\wsb-pause'
    while (Test-Path $pauseFlag) {
        $runMutex.ReleaseMutex()
        "host runs paused ($pauseFlag exists); waiting..."
        while (Test-Path $pauseFlag) { Start-Sleep -Seconds 15 }
        try { [void]$runMutex.WaitOne() } catch [System.Threading.AbandonedMutexException] { }
    }
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
    int RegisterControlChangeNotify(IntPtr p); int UnregisterControlChangeNotify(IntPtr p);
    int GetChannelCount(out uint c); int SetMasterVolumeLevel(float l, Guid g); int SetMasterVolumeLevelScalar(float l, Guid g);
    int GetMasterVolumeLevel(out float l); int GetMasterVolumeLevelScalar(out float l);
    int SetChannelVolumeLevel(uint c, float l, Guid g); int SetChannelVolumeLevelScalar(uint c, float l, Guid g);
    int GetChannelVolumeLevel(uint c, out float l); int GetChannelVolumeLevelScalar(uint c, out float l);
    int SetMute([MarshalAs(UnmanagedType.Bool)] bool m, Guid g); int GetMute([MarshalAs(UnmanagedType.Bool)] out bool m);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref Guid id, int ctx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object o); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int NotImpl1(); int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice d); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public static class DsfAudio {
    static IAudioEndpointVolume Volume() {
        var e = (IMMDeviceEnumerator)new MMDeviceEnumeratorComObject();
        IMMDevice d; Marshal.ThrowExceptionForHR(e.GetDefaultAudioEndpoint(0, 0, out d));
        var iid = typeof(IAudioEndpointVolume).GUID; object o;
        Marshal.ThrowExceptionForHR(d.Activate(ref iid, 23, IntPtr.Zero, out o));
        return (IAudioEndpointVolume)o;
    }
    public static bool GetMute() { bool m; Marshal.ThrowExceptionForHR(Volume().GetMute(out m)); return m; }
    public static void SetMute(bool m) { Marshal.ThrowExceptionForHR(Volume().SetMute(m, Guid.Empty)); }
}
'@

# -OnHost: the user's Driver profile, its backup for the series, and the speakers' mute state.
$profileDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ubisoft\Driver San Francisco'
$hostState = Join-Path $outputHost 'host-session'
function Restore-HostSession {
    # Put back the profile and mute state saved by Enter-HostSession (also after a series that died).
    $backup = Join-Path $hostState 'profile'
    if (Test-Path (Join-Path $hostState 'complete')) {
        # In place: the folder itself can be held for a moment (indexer, antivirus), so it is
        # never deleted. Copy the backup over it, drop files the runs added, then compare.
        # The game (or a scanner) can hold a file for a while after the run.
        Wait-Process -Name Driver -Timeout 30 -ErrorAction SilentlyContinue
        for ($try = 1; ; $try++) {
            try { Copy-Item (Join-Path $backup '*') $profileDir -Recurse -Force -ErrorAction Stop; break }
            catch { if ($try -ge 12) { throw }; Start-Sleep -Seconds 5 }
        }
        $kept = @(Get-ChildItem $backup -Recurse -File | ForEach-Object { $_.FullName.Substring($backup.Length) })
        Get-ChildItem $profileDir -Recurse -File | Where-Object { $kept -notcontains $_.FullName.Substring($profileDir.Length) } |
            Remove-Item -Force -ErrorAction SilentlyContinue
        foreach ($rel in $kept) {
            if ((Get-FileHash "$backup$rel").Hash -ne (Get-FileHash "$profileDir$rel" -ErrorAction SilentlyContinue).Hash) {
                throw "profile restore incomplete ($rel); the backup stays in $backup"
            }
        }
    }
    $muteFile = Join-Path $hostState 'muted.txt'
    if (Test-Path $muteFile) { [DsfAudio]::SetMute((Get-Content $muteFile) -eq 'True') }
    Remove-Item $hostState -Recurse -Force -ErrorAction SilentlyContinue
}
function Enter-HostSession {
    Restore-HostSession
    if (Get-Process Driver -ErrorAction SilentlyContinue) { throw 'Driver.exe is already running (someone is playing?); not starting a host series' }
    if (-not (Test-Path (Join-Path $profileDir 'reloaded\savegames\autosave.dat'))) {
        throw "$profileDir has no reloaded\savegames\autosave.dat; refusing to start a new game."
    }
    New-Item -ItemType Directory -Force -Path $hostState | Out-Null
    Copy-Item $profileDir (Join-Path $hostState 'profile') -Recurse
    Set-Content (Join-Path $hostState 'complete') ''
    Set-Content (Join-Path $hostState 'muted.txt') ([DsfAudio]::GetMute())
    [DsfAudio]::SetMute($true)
    $width, $height = $Resolution -split 'x'
    $graphics = Join-Path $profileDir 'graphics.ini'
    (Get-Content $graphics) `
        -replace '^Fullscreen\s*=.*$', 'Fullscreen = 0' `
        -replace '^Width\s*=.*$', "Width = $width" `
        -replace '^Height\s*=.*$', "Height = $height" | Set-Content $graphics
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

function Confirm-Vm {
    # Start the VM headless if needed (also sets the guest display and mutes it), then copy
    # the host's Driver profile into it as a -Resolution window.
    & $vmScript Start | Out-Null
    & $vmScript Exec -Script C:\Repo\tools\wsb\guest\setup-vm.ps1 -Arguments @('-Phase', 'Profile', '-Resolution', $Resolution) -TimeoutSeconds 300 | Out-Null
}

try {
if ($OnHost) { Enter-HostSession } elseif ($Vm) { Confirm-Vm } else { Confirm-Sandbox }
foreach ($archive in $Archives) {
    $guestArchive = Get-SeenPath $archive
    $guestArgs = @('-Archive', $guestArchive, '-Hook', '-Probe', '-ProbeIds', $ProbeIds,
        '-ZoomPresses', $ZoomPresses, '-ShotSeconds', $ShotSeconds, '-WaitSeconds', $WaitSeconds, '-Detach')
    if ($AheadIds) { $guestArgs += '-AheadIds', $AheadIds }
    if ($TrafficIds) { $guestArgs += '-TrafficIds', $TrafficIds }
    if ($Asi) { $guestArgs += '-Asi', (Get-SeenPath $Asi) }
    if ($DriveSeconds) { $guestArgs += '-DriveSeconds', $DriveSeconds, '-DriveKey', $DriveKey }
    if ($HoldMph) { $guestArgs += '-HoldMph', $HoldMph }   # -File passes '30,60,90' as one string, like -ProbeIds
    if ($Damage) { $guestArgs += '-Damage' }
    if ($DamagePanels) { $guestArgs += '-DamagePanels' }
    if ($DamageSide) { $guestArgs += '-DamageSide' }
    if ($GlassLadder) { $guestArgs += '-GlassLadder' }
    if ($Lights) { $guestArgs += '-Lights' }
    if ($LightsDriveOnly) { $guestArgs += '-LightsDriveOnly' }
    if ($Dyno) { $guestArgs += '-Dyno', '-BrakeSeconds', $BrakeSeconds, '-DynoRuns', $DynoRuns }
    if ($DynoStart) { $guestArgs += '-DynoStart', $DynoStart }
    if ($DynoKeep) { $guestArgs += '-DynoKeep', $DynoKeep }
    if ($DynoKeys) { $guestArgs += '-DynoKeys', "`"$DynoKeys`"" }
    if ($MenuKeys) { $guestArgs += '-MenuKeys', $MenuKeys }
    if ($MenuTrigger) { $guestArgs += '-MenuTrigger', $MenuTrigger }
    if ($TrafficRadius) { $guestArgs += '-TrafficRadius', $TrafficRadius }
    if ($Override) {
        # Repo-relative replacement paths -> guest paths under C:\Repo.
        $mapped = foreach ($pair in ($Override -split ';' | Where-Object { $_ })) {
            $name, $path = $pair -split '=', 2
            "$($name.Trim())=$(Get-SeenPath $path)"
        }
        $guestArgs += '-Override', ($mapped -join ';')
    }
    if ($LuaFiles) {
        # Repo-relative Lua files -> guest paths under C:\Repo, keyed by their UserLuaScripts path.
        $mapped = foreach ($pair in ($LuaFiles -split ';' | Where-Object { $_ })) {
            $name, $path = $pair -split '=', 2
            "$($name.Trim())=$(Get-SeenPath $path)"
        }
        $guestArgs += '-LuaFiles', ($mapped -join ';')
    }
    if ($OnHost) {
        # Same script, run here and waited for (-Detach would return at once).
        $runDir = Join-Path $outputHost "runs\host-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        New-Item -ItemType Directory -Force -Path $runDir | Out-Null
        Set-Content (Join-Path $outputHost 'runs\latest.txt') $runDir
        $hostArgs = @($guestArgs | Where-Object { $_ -ne '-Detach' }) + @('-OnHost', '-RunDir', $runDir,
            '-GameDir', $GamePath, '-RepoDir', $repo, '-OutputDir', $outputHost, '-RunPath', $HostRunPath)
        # The call operator quotes arguments itself: drop the quotes added for wsb exec.
        $hostArgs = $hostArgs | ForEach-Object { "$_" -replace '^"(.*)"$', '$1' }
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$repo\tools\wsb\guest\run-test.ps1" @hostArgs *>> (Join-Path $runDir 'host.log')
    }
    elseif ($Vm) {
        # Same script in the VM's desktop session, waited for; the guest writes straight into
        # this run folder through C:\Output (a shared folder). Repo files are copied into the
        # run folder rather than linked across the shared folder.
        $stamp = "vm-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        $runDir = Join-Path $outputHost "runs\$stamp"
        New-Item -ItemType Directory -Force -Path $runDir | Out-Null
        Set-Content (Join-Path $outputHost 'runs\latest.txt') $runDir
        $vmArgs = @($guestArgs | Where-Object { $_ -ne '-Detach' }) + @('-RunDir', "C:\Output\runs\$stamp", '-CopyRepoFiles')
        $timeout = [math]::Max(600, $WaitSeconds + 420) + 300
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
    else { try { & $sandbox Exec -Script C:\Repo\tools\wsb\guest\run-test.ps1 -Arguments $guestArgs | Out-Null }
    catch {
        # The sandbox died since the last run: bring up a new one and retry once.
        "  sandbox exec failed ($($_.Exception.Message)); restarting the sandbox"
        Confirm-Sandbox
        & $sandbox Exec -Script C:\Repo\tools\wsb\guest\run-test.ps1 -Arguments $guestArgs | Out-Null
    } }
    if (-not $Vm) { $runDir = (Get-Content (Join-Path $outputHost 'runs\latest.txt')).Trim() -replace '^C:\\Output', $outputHost }
    $result = Join-Path $runDir 'result.json'
    $deadline = (Get-Date).AddSeconds([math]::Max(600, $WaitSeconds + 420))
    while (-not (Test-Path $result) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 3 }
    "=== $archive -> $runDir"
    if (-not (Test-Path $result)) { "  no result after $([math]::Max(600, $WaitSeconds + 420)) s"; continue }
    $r = Get-Content $result -Raw | ConvertFrom-Json
    "  stage=$($r.stage) crashed=$([bool]$r.crashed) error=$($r.error)"
    $r.probeLines | Where-Object { $_ -match 'id \d+:' -and $_ -notmatch 'cockpit t\d+ speed' -and $_ -notmatch '\] dy \d' } | ForEach-Object { "  $_" }
    $speeds = @($r.probeLines | Where-Object { $_ -match 'cockpit t\d+ speed ([\d.]+) m/s ([\d.]+) mph' } |
        ForEach-Object { [double]($_ -replace '.* ([\d.]+) mph.*', '$1') })
    if ($speeds.Count) { "  $($speeds.Count) speed samples in cockpit view, top $(($speeds | Measure-Object -Maximum).Maximum) mph" }
}
} finally {
    if ($OnHost) { Restore-HostSession }
    $runMutex.ReleaseMutex()
}
