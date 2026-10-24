<#
.SYNOPSIS
The user's Driver profile and speaker mute state that host test series
(run-series.ps1 -OnHost) change, and their restore.

.DESCRIPTION
  Enter    restores an earlier session first, then backs up the Driver profile
           (Documents\Ubisoft\Driver San Francisco) to <state>\profile, checks the
           copy by hash, mutes the default speakers (the old state goes to
           <state>\muted.txt) and switches the profile to a -Resolution window.
           With -SeriesId it also starts a watcher (Watch, below).
  Restore  puts back what Enter changed: the speakers first, then the profile,
           copied over in place (the folder can be held for a moment by the
           indexer or antivirus, so it is never deleted), files the runs added
           dropped, compared by hash. The profile part waits while Driver.exe runs.
           To undo a series that died by hand: .\tools\wsb\host-session.ps1 Restore
  Watch    runs hidden and outside the caller's process tree (WMI), so killing the
           series doesn't kill it: waits for the series process -SeriesId to end,
           then for the run's game (Driver.exe under -RunPath, which a killed
           series' run-test.ps1 still closes at its own deadline), takes the
           series mutex and restores what is left. A series that ends normally has
           restored already, and the watcher finds nothing to do.

Enter and Restore run while the caller holds the series mutex (-MutexName);
Watch takes it itself. -ProfileDir, -StateDir, -MutexName and -NoMute are for
tests (tools\wsb\test-host-session.ps1).
#>
param(
    [Parameter(Mandatory, Position = 0)] [ValidateSet('Enter', 'Restore', 'Watch')] [string]$Action,
    [string]$Resolution = '1280x720',
    [int]$SeriesId = 0,
    [string]$RunPath,   # the host run folder (run-series.ps1 passes it); default: any ...\dsfforza-run
    [int]$WatchMinutes = 180,   # Watch: how long to wait for the run's game to close
    [string]$ProfileDir = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ubisoft\Driver San Francisco'),
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA 'Temp\dsfforza-wsb\host-session'),
    [string]$MutexName = 'Local\dsfforza-wsb-run',
    [switch]$NoMute
)

$ErrorActionPreference = 'Stop'
$backup = Join-Path $StateDir 'profile'
$complete = Join-Path $StateDir 'complete'
$muteFile = Join-Path $StateDir 'muted.txt'

if (-not $NoMute -and -not ('DsfAudio' -as [type])) {
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
}

function Get-RelativeFiles([string]$root) {
    @(Get-ChildItem -LiteralPath $root -Recurse -File -Force | ForEach-Object { $_.FullName.Substring($root.Length) })
}

function Test-SameFiles([string]$from, [string]$to) {
    # Every file under $from has an identical copy under $to; returns the first that doesn't.
    foreach ($rel in Get-RelativeFiles $from) {
        $copy = Get-FileHash -LiteralPath "$to$rel" -ErrorAction SilentlyContinue
        if (-not $copy -or $copy.Hash -ne (Get-FileHash -LiteralPath "$from$rel").Hash) { return $rel }
    }
    return $null
}

function Restore-Session {
    if (-not (Test-Path -LiteralPath $StateDir)) { return }
    # Speakers first: they don't depend on the game, and a muted PC is what the user notices.
    if (Test-Path -LiteralPath $muteFile) {
        if (-not $NoMute) {
            try { [DsfAudio]::SetMute((Get-Content -LiteralPath $muteFile) -eq 'True'); Remove-Item -LiteralPath $muteFile }
            catch { Write-Warning "speaker mute state not restored ($($_.Exception.Message)); kept in $muteFile" }
        } else { Remove-Item -LiteralPath $muteFile }
    }
    if (Test-Path -LiteralPath $complete) {
        # The game (or a scanner) can hold a file for a while after the run. Never copy over the
        # profile of a running game: it would write over the restored files (or they over its).
        Wait-Process -Name Driver -Timeout 30 -ErrorAction SilentlyContinue
        if (Get-Process Driver -ErrorAction SilentlyContinue) {
            throw "Driver.exe is running: the profile was not restored; the backup stays in $backup (close the game, then run tools\wsb\host-session.ps1 Restore)"
        }
        for ($try = 1; ; $try++) {
            try { Copy-Item (Join-Path $backup '*') $ProfileDir -Recurse -Force -ErrorAction Stop; break }
            catch { if ($try -ge 12) { throw }; Start-Sleep -Seconds 5 }
        }
        $kept = Get-RelativeFiles $backup
        Get-ChildItem -LiteralPath $ProfileDir -Recurse -File -Force |
            Where-Object { $kept -notcontains $_.FullName.Substring($ProfileDir.Length) } |
            Remove-Item -Force -ErrorAction SilentlyContinue
        $bad = Test-SameFiles $backup $ProfileDir
        if ($bad) { throw "profile restore incomplete ($bad); the backup stays in $backup" }
        # Restored: from here a leftover folder is only clutter (no 'complete', so never restored again).
        Remove-Item -LiteralPath $complete
    }
    if (-not (Test-Path -LiteralPath $muteFile)) { Remove-Item -LiteralPath $StateDir -Recurse -Force -ErrorAction SilentlyContinue }
}

function Start-Watcher {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" Watch -SeriesId $SeriesId " +
        "-RunPath `"$RunPath`" -ProfileDir `"$ProfileDir`" -StateDir `"$StateDir`" -MutexName `"$MutexName`""
    if ($NoMute) { $arguments += ' -NoMute' }
    # Through WMI, so the watcher isn't in the series' process tree or job (a tool that kills
    # the series' tree would kill a plain child too).
    try {
        $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
        $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine = "powershell.exe $arguments"; ProcessStartupInformation = $startup }
        if ($r.ReturnValue -eq 0) { return }
    } catch { }
    Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden
}

function Enter-Session {
    if (Get-Process Driver -ErrorAction SilentlyContinue) { throw 'Driver.exe is already running (someone is playing?); not starting a host series' }
    Restore-Session
    if (Test-Path -LiteralPath $muteFile) { throw "the speakers' mute state from an earlier series could not be restored ($muteFile)" }
    if (Test-Path -LiteralPath $StateDir) { Remove-Item -LiteralPath $StateDir -Recurse -Force -ErrorAction SilentlyContinue }
    # A leftover folder would put the new backup inside it (profile\Driver San Francisco) and
    # the restore would then drop every profile file.
    if (Test-Path -LiteralPath $StateDir) { throw "$StateDir is left from an earlier series and can't be removed; delete it, then try again" }
    if (-not (Test-Path -LiteralPath (Join-Path $ProfileDir 'reloaded\savegames\autosave.dat'))) {
        throw "$ProfileDir has no reloaded\savegames\autosave.dat; refusing to start a new game."
    }
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    Copy-Item -LiteralPath $ProfileDir $backup -Recurse
    $bad = Test-SameFiles $ProfileDir $backup
    if ($bad) { throw "profile backup incomplete ($bad); nothing changed" }
    Set-Content -LiteralPath $complete ''
    if ($SeriesId) { Start-Watcher }
    if (-not $NoMute) {
        Set-Content -LiteralPath $muteFile ([DsfAudio]::GetMute())
        [DsfAudio]::SetMute($true)
    }
    $width, $height = $Resolution -split 'x'
    $graphics = Join-Path $ProfileDir 'graphics.ini'
    (Get-Content -LiteralPath $graphics) `
        -replace '^Fullscreen\s*=.*$', 'Fullscreen = 0' `
        -replace '^Width\s*=.*$', "Width = $width" `
        -replace '^Height\s*=.*$', "Height = $height" | Set-Content -LiteralPath $graphics
}

function Invoke-Watch {
    $log = Join-Path (Split-Path $StateDir) 'host-session-watch.log'
    function Say([string]$text) { Add-Content -LiteralPath $log "$(Get-Date -Format s) [$PID for $SeriesId] $text" }
    Wait-Process -Id $SeriesId -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $StateDir)) { return }
    Say 'series ended with its host session still in place'
    $deadline = (Get-Date).AddMinutes($WatchMinutes)
    $runGame = { @(Get-Process Driver -ErrorAction SilentlyContinue | Where-Object {
        try { if ($RunPath) { $_.Path -like "$RunPath\*" } else { $_.Path -like '*\dsfforza-run\*' } }
        catch { $true } }).Count }
    while ((& $runGame) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 5 }
    $mutex = New-Object System.Threading.Mutex($false, $MutexName)
    try { if (-not $mutex.WaitOne([TimeSpan]::FromHours(6))) { Say 'series mutex not free after 6 h; giving up'; return } }
    catch [System.Threading.AbandonedMutexException] { }
    try {
        # Under the mutex a session can only be left from a dead series (a live one holds the mutex).
        Restore-Session
        Say $(if (Test-Path -LiteralPath $StateDir) { "partly restored; left in $StateDir" } else { 'restored' })
    }
    catch { Say "restore failed: $($_.Exception.Message)" }
    finally { $mutex.ReleaseMutex() }
}

switch ($Action) {
    'Enter' { Enter-Session }
    'Restore' { Restore-Session }
    'Watch' { Invoke-Watch }
}
