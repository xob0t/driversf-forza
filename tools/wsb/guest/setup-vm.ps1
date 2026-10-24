<#
Runs inside the dsfforza test VM as its user (admin, no UAC), in the desktop session
(tools\wsb\vm.ps1 Exec). Phases:

  Provision  links C:\Repo, C:\Output, C:\Profile to the VMware shared folders, then
             runs Game, Profile and Session (tools\wsb\vm-build.ps1 Provision).
  Game       C:\Game <- the host's game folder (share "Game", read-only), mirrored, then
             put back to stock: files a dsfforza install replaced come from its
             dsfforza-backup, files it created are dropped (run-test.ps1 rebuilds plugins
             and UserLuaScripts anyway). Re-run to pick up changes to the host install.
  Profile    Documents\Ubisoft\Driver San Francisco <- C:\Profile, switched to a
             -Resolution window (run-series.ps1 -Vm, once per series).
  Session    display -DisplaySize, default audio endpoint muted (vm.ps1 Start).

Language-neutral: no localised names or command output are parsed.
#>
param(
    [Parameter(Mandatory)] [ValidateSet('Provision', 'Game', 'Profile', 'Session')] [string]$Phase,
    [string]$Resolution = '1280x720',     # the game's window
    [string]$DisplaySize = '1600x900'     # the guest desktop
)

$ErrorActionPreference = 'Stop'
$shares = '\\vmware-host\Shared Folders'
New-Item -ItemType Directory -Force -Path C:\dsf | Out-Null
$log = [System.Collections.Generic.List[string]]::new()
function Say([string]$text) { $log.Add("$(Get-Date -Format HH:mm:ss) $text") }

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

public static class DsfVm {
    public static void Mute() {
        var e = (IMMDeviceEnumerator)new MMDeviceEnumeratorComObject();
        IMMDevice d; Marshal.ThrowExceptionForHR(e.GetDefaultAudioEndpoint(0, 0, out d));
        var iid = typeof(IAudioEndpointVolume).GUID; object o;
        Marshal.ThrowExceptionForHR(d.Activate(ref iid, 23, IntPtr.Zero, out o));
        var v = (IAudioEndpointVolume)o;
        Marshal.ThrowExceptionForHR(v.SetMasterVolumeLevelScalar(0f, Guid.Empty));
        Marshal.ThrowExceptionForHR(v.SetMute(true, Guid.Empty));
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DevMode {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        public short SpecVersion, DriverVersion, Size, DriverExtra;
        public int Fields, PositionX, PositionY, DisplayOrientation, DisplayFixedOutput;
        public short Color, Duplex, YResolution, TTOption, Collate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string FormName;
        public short LogPixels;
        public int BitsPerPel, PelsWidth, PelsHeight, DisplayFlags, DisplayFrequency;
        public int ICMMethod, ICMIntent, MediaType, DitherType, Reserved1, Reserved2, PanningWidth, PanningHeight;
    }
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern bool EnumDisplaySettings(string device, int mode, ref DevMode dm);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int ChangeDisplaySettings(ref DevMode dm, int flags);
    public static string Current() {
        var dm = new DevMode(); dm.Size = (short)Marshal.SizeOf(typeof(DevMode));
        EnumDisplaySettings(null, -1, ref dm);
        return dm.PelsWidth + "x" + dm.PelsHeight;
    }
    // 0 = changed (DISP_CHANGE_SUCCESSFUL), saved to the registry.
    public static int Set(int width, int height) {
        var dm = new DevMode(); dm.Size = (short)Marshal.SizeOf(typeof(DevMode));
        if (!EnumDisplaySettings(null, -1 /* ENUM_CURRENT_SETTINGS */, ref dm)) return -100;
        if (dm.PelsWidth == width && dm.PelsHeight == height) return 0;
        dm.PelsWidth = width; dm.PelsHeight = height; dm.Fields = 0x80000 | 0x100000;
        return ChangeDisplaySettings(ref dm, 1 /* CDS_UPDATEREGISTRY */);
    }
}
'@

function Invoke-Session {
    $w, $h = $DisplaySize -split 'x'
    $code = [DsfVm]::Set([int]$w, [int]$h)
    if ($code -ne 0) {
        # Fall back to VMware Tools' own tool.
        $tool = Join-Path $env:ProgramFiles 'VMware\VMware Tools\VMwareResolutionSet.exe'
        if (Test-Path $tool) { & $tool 0 1 ',' 0 0 $w $h | Out-Null }
    }
    Say "display $([DsfVm]::Current()) (ChangeDisplaySettings $code)"
    try { [DsfVm]::Mute(); Say 'audio muted' } catch { Say "audio not muted: $($_.Exception.Message)" }
}

function Invoke-Links {
    foreach ($pair in @(@('C:\Repo', 'Repo'), @('C:\Output', 'Output'), @('C:\Profile', 'Profile'))) {
        $link, $share = $pair
        $target = Join-Path $shares $share
        if (-not (Test-Path -LiteralPath $target)) { throw "shared folder $target is missing" }
        $item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
        if ($item -and -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "$link exists and is not a link" }
        if ($item) { [IO.Directory]::Delete($link) }
        New-Item -ItemType SymbolicLink -Path $link -Target $target | Out-Null
        Say "$link -> $target"
    }
    # Local links to remote targets (L2R) must be followed; it's the default, made sure here.
    fsutil.exe behavior set SymlinkEvaluation L2L:1 L2R:1 | Out-Null
}

function Invoke-Game {
    $source = Join-Path $shares 'Game'
    if (-not (Test-Path -LiteralPath (Join-Path $source 'Driver.exe'))) { throw "no Driver.exe in $source" }
    & robocopy.exe $source C:\Game /MIR /XD dsfforza-backup /XF *.log *.dmp /R:2 /W:2 /NP /NFL /NDL /NJH | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy of the game failed (exit $LASTEXITCODE)" }
    Say "game mirrored to C:\Game (robocopy exit $LASTEXITCODE)"
    $backup = Join-Path $source 'dsfforza-backup'
    $manifest = Join-Path $backup 'install.json'
    if (Test-Path -LiteralPath $manifest) {
        $install = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
        foreach ($entry in $install.entries) {
            $path = Join-Path C:\Game $entry.path
            if ($entry.action -eq 'replaced') {
                Copy-Item -LiteralPath (Join-Path $backup $entry.backup) $path -Force
                $hash = (Get-FileHash -LiteralPath $path).Hash
                if ($entry.original_sha256 -and $hash -ne $entry.original_sha256) { throw "stock $($entry.path) hash mismatch" }
                Say "stock $($entry.path)"
            }
            elseif ($entry.action -eq 'created') { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
        }
        foreach ($rel in @($install.runtime_files)) { if ($rel) { Remove-Item -LiteralPath (Join-Path C:\Game $rel) -Force -ErrorAction SilentlyContinue } }
        foreach ($rel in @($install.created_dirs)) {
            $dir = Join-Path C:\Game $rel
            if ((Test-Path $dir) -and -not (Get-ChildItem $dir -Recurse -File)) { Remove-Item $dir -Recurse -Force }
        }
    }
    Get-ChildItem C:\Game -Recurse -File | Where-Object IsReadOnly | ForEach-Object { $_.IsReadOnly = $false }
    $size = (Get-ChildItem C:\Game -Recurse -File | Measure-Object Length -Sum).Sum
    Say ("C:\Game {0:N2} GB" -f ($size / 1GB))
}

function Invoke-Profile {
    $source = 'C:\Profile'
    if (-not (Test-Path (Join-Path $source 'reloaded\savegames\autosave.dat'))) {
        throw 'C:\Profile has no reloaded\savegames\autosave.dat; refusing to start a new game.'
    }
    $target = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ubisoft\Driver San Francisco'
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    # Not the replays: a game running on the host keeps reloaded\replays\dng.replay open.
    & robocopy.exe $source $target /E /XD replays /R:2 /W:2 /NP /NFL /NDL /NJH | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy of the profile failed (exit $LASTEXITCODE)" }
    Get-ChildItem $target -Recurse -File | Where-Object IsReadOnly | ForEach-Object { $_.IsReadOnly = $false }
    $width, $height = $Resolution -split 'x'
    $graphics = Join-Path $target 'graphics.ini'
    (Get-Content $graphics) `
        -replace '^Fullscreen\s*=.*$', 'Fullscreen = 0' `
        -replace '^Width\s*=.*$', "Width = $width" `
        -replace '^Height\s*=.*$', "Height = $height" | Set-Content $graphics
    Say "profile copied to $target, window $Resolution"
}

$status = [ordered]@{ phase = $Phase; utc = (Get-Date).ToUniversalTime().ToString('o') }
try {
    switch ($Phase) {
        'Provision' { Invoke-Links; Invoke-Game; Invoke-Profile; Invoke-Session }
        'Game' { Invoke-Game }
        'Profile' { Invoke-Profile }
        'Session' { Invoke-Session }
    }
    $status.ok = $true
}
catch { $status.ok = $false; $status.error = $_.Exception.Message; Say "error: $($_.Exception.Message)" }
finally {
    $status.log = $log
    $json = $status | ConvertTo-Json -Depth 3
    Set-Content "C:\dsf\setup-$($Phase.ToLower()).json" $json
    if (Test-Path 'C:\Output') {
        New-Item -ItemType Directory -Force -Path 'C:\Output\vm' | Out-Null
        Set-Content "C:\Output\vm\setup-$($Phase.ToLower()).json" $json
    }
}
if (-not $status.ok) { exit 1 }
