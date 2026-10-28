<#
Runs inside the sandbox as the logged-in user.

Builds C:\Run from the read-only game at C:\Game: root files are copied,
directories are junctions, except Install (per-file symlinks, with
dngvehicles.sp pointing at -Archive), Common (UserLuaScripts holds the
probe when -Probe) and plugins (DriverNGHook when -Hook). Launches Driver,
presses Enter until the Story world has loaded, then waits for the probe
to finish, taking screenshots. Results go to C:\Output\runs\<stamp>\.

-Detach starts the same script hidden and returns immediately; poll
C:\Output\runs\latest.txt and the run's result.json.

-OnHost runs the same steps on the host (tools/wsb/run-series.ps1 -OnHost):
-GameDir, -RepoDir, -OutputDir and -RunPath replace C:\Game, C:\Repo, C:\Output
and C:\Run; file links fall back to hard links or copies without the symlink
privilege, and no key goes to a window that isn't the game's.

-DriveSeconds N holds the accelerate key (DSF's default, the Up arrow; -DriveKey W
for W) from the moment the probe puts the player in a car until N seconds after
its cockpit view shows up in Driver.log (which lags by seconds), so the needles
move. The probe then keeps the cockpit view longer and logs the
car's speed; each screenshot's event in events.json carries the latest speed
line. -HoldMph '30,60,90' makes the probe force those speeds in turn as well.
#>
param(
    [string]$Archive = 'C:\Game\Install\dngvehicles.sp',
    [string]$Override,   # 'Install\gui.sp=C:\Repo\out\x\gui.sp;media\Fonts.fchunk=...': game files replaced by links;
                         # a root file ('dsf_enhanced.asi=...', 'dsf_enhanced.ini=...') is copied over the run copy
    [switch]$Hook,
    [string]$Asi,        # guest path of a plugin .asi copied into plugins with the .ini beside it; plugins\*.log is collected
    [string]$LuaFiles,   # 'dsfforza\hud.lua=C:\Repo\out\x\hud.lua;...': UserLuaScripts files replaced after the repo copy (-Probe)
    [switch]$ArchiveOnly, # only the archive, not its build's dngvehicles.lua and gui\ (below: "The build's files")
    [switch]$Probe,
    [int]$WaitSeconds = 120,
    [int]$ZoomPresses = 0,
    [int]$ShotSeconds = 10, # screenshot interval while observing
    [int]$BurstShots = 1, # with -HoldMph or -DriveSeconds: shots per sample, as fast as they save (observe-NN-b2.png...; shimmer, flicker)
    [string]$ProbeIds,   # comma-separated, e.g. 266,304
    [string]$TrafficIds, # comma-separated IDs for TrafficSpooler.RequestTrafficVehicles
    [string]$AheadIds,   # comma-separated IDs spawned in front without zapping
    [switch]$KeepRunning,
    [switch]$Detach,
    [string]$RunDir,
    [int]$DriveSeconds = 0,
    [ValidateSet('Up', 'W', 'Left', 'Right')] [string]$DriveKey = 'Up',  # Left/Right: steer in place (wheel and hands)
    [string]$HoldMph,    # comma-separated mph, forced in turn in cockpit view
    [switch]$Damage,     # probe: GameVehicleResource.fullVisualDamage on each car in chase view (research/damage.md)
    [switch]$DamagePanels, # with -Damage: then forceOpenPanels (~6 s) and removeAllPanels (~9 s) in the same view
    [switch]$DamageSide, # probe: a parked side-on copy of each car takes applyDamage hits (wing, door, quarter) in chase view
    [switch]$GlassLadder, # probe: damage in steps during each cockpit view (clonetest.lua GLASS_STEPS, ~21 s)
    [string]$Views,      # probe: extra views after the chase view, ~6 s each: camera modes (Bonnet, Bumper) and fixed
                         # cameras on the driver's seat (left, right, front), the tailgate (rear), the tailpipes (exhaust), the left wheels
                         # (wheels), the front left wheel (fq, lock) and the headlamps (nose), e.g. 'Bonnet,Bumper,left'
    [int]$Palette = -1,  # probe: paint palette entry of every probed car (shaderParams[0]); -1: the game's pick
    [switch]$LightsDriveOnly, # with -Lights: only the day and dark drives, no setLights phases
    [switch]$Lights,     # probe: lamp phases per car (dsfforza\lightstest.lua, research/lights.md); the harness
                         # picks each phase through dsfforza\lightsync.lua and saves lights-<id>-<k>a/b.png
    [switch]$Dyno,       # probe: standing-start runs on a straight (dsfforza\dyno.lua); per run the harness holds
                         # the throttle DriveSeconds, then the brake (Down) BrakeSeconds
    [int]$BrakeSeconds = 6,
    [int]$DynoRuns = 2,
    [string]$DynoStart,  # 'x,y,z,heading' of the start line (default in dyno.lua)
    [string]$DynoScan,   # 'x,y,z;x,y,z;...': the first car is dropped at each point first and dyno.lua logs
                         # how many wheels are off road there ("dyno scan ..."), to find sand or dirt
    [string]$DynoKeep,  # lane keeper mode per run, e.g. 'none,pulse:5' (dyno.lua keepLane)
    [string]$DynoKeys,   # key steps per run instead of throttle/brake: 'Up:5,Up+Right:0.5,Down:5;...'
                         # (runs separated by ';', cycling; keys Up Down Left Right Space Shift W A S D)
    [string]$MenuKeys,   # menu taps after -MenuTrigger shows in Driver.log: 'Enter:2,Right:1.5,...' (key:wait s)
    [string]$MenuTrigger = 'garage\.enterGarage: true',
    [double]$TrafficRadius = 0, # metres: once the world is up, the traffic road radius (350 m vanilla, 256 m in the
                         # [[engine+0x754]] case scaled alike) is read from a new float instead (research/draw-distance.md, plan A).
                         # Operands only: above 360 m 0x7723A0's 21-cell list overflows; use DSF Enhanced's TrafficSpawnRadius.
    [switch]$OnHost,
    [switch]$CopyRepoFiles, # copy files from -RepoDir into the run folder instead of linking them (the VM's C:\Repo is a
                         # VMware shared folder; tools\wsb\run-series.ps1 -Vm)
    [int]$MemorySeconds = 5, # memory.csv sample interval (0: none); peaks go into result.json "memory"
    [string]$GameRootFrom, # folder whose files replace the game folder's root files of the same name (run-series.ps1 -Vm:
                         # the host game folder's current dsf_enhanced.asi and .ini); -Override wins. result.json
                         # "dsfEnhanced" records the .asi and .ini used, "gameRoot" the game folder's root file hashes.
    [string]$GameDir = 'C:\Game',
    [string]$RepoDir = 'C:\Repo',
    [string]$OutputDir = 'C:\Output',
    [string]$RunPath = 'C:\Run'
)

$ErrorActionPreference = 'Stop'

if ($Detach) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $RunDir = "$OutputDir\runs\$stamp"
    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    Set-Content "$OutputDir\runs\latest.txt" $RunDir
    # The same arguments again (unbound ones take the same defaults there), quoted for the
    # command line Start-Process joins with spaces.
    $forward = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-RunDir', "`"$RunDir`"")
    foreach ($name in $PSBoundParameters.Keys) {
        $value = $PSBoundParameters[$name]
        if ($name -in 'Detach', 'RunDir') { continue }
        if ($value -is [switch]) { if ($value) { $forward += "-$name" }; continue }
        $forward += "-$name", ('"' + ("$value" -replace '(\\+)$', '$1$1') + '"')
    }
    Start-Process powershell.exe -ArgumentList $forward -WindowStyle Hidden
    "started $RunDir"
    return
}

if (-not $RunDir) { $RunDir = "$OutputDir\runs\$(Get-Date -Format 'yyyyMMdd-HHmmss')" }
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$started = Get-Date
$events = [System.Collections.Generic.List[object]]::new()
function Note([string]$message) {
    $now = Get-Date   # wall: the guest clock, as in dsfforza.log (src/dsfforza/harness/emblem_check.py)
    $events.Add([pscustomobject]@{ t = [math]::Round(($now - $started).TotalSeconds, 1); wall = $now.ToString('HH:mm:ss.fff'); message = $message })
    $events | ConvertTo-Json | Set-Content (Join-Path $RunDir 'events.json')
}

# Memory sampler (a background runspace): every -MemorySeconds one memory.csv row with the
# machine's available and committed memory, hard page faults (pages read from disk per second:
# paging stalls), the GPU adapter's dedicated and shared use, and Driver.exe's private bytes,
# working set and address space with their peaks. WMI classes, so nothing depends on the
# Windows display language. The peaks go into result.json ("memory").
$memory = [hashtable]::Synchronized(@{ stop = $false })
$memoryJob = $null
if ($MemorySeconds -gt 0) {
    $memoryJob = [powershell]::Create()
    [void]$memoryJob.AddScript({
        param($csv, $seconds, $state, $started)
        $mb = { param($bytes) [math]::Round([double]$bytes / 1MB) }
        $state.totalMB = & $mb (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
        $state.minAvailableMB = [double]::MaxValue
        foreach ($k in 'maxCommitMB', 'maxPagesInPerSec', 'maxGpuDedicatedMB', 'maxGpuSharedMB', 'driverPeakPrivateMB',
            'driverPeakWorkingSetMB', 'driverMaxVirtualMB', 'driverMaxGpuDedicatedMB', 'driverMaxGpuSharedMB', 'samples') { $state[$k] = 0 }
        'seconds,availableMB,commitMB,cacheMB,pagesInPerSec,gpuDedicatedMB,gpuSharedMB,driverPrivateMB,driverWorkingSetMB,driverVirtualMB,driverPeakPrivateMB,driverPeakWorkingSetMB,driverGpuDedicatedMB,driverGpuSharedMB' |
            Set-Content $csv
        $lastPages = $null
        while (-not $state.stop) {
            $row = [ordered]@{ seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1) }
            try {
                $m = Get-CimInstance Win32_PerfRawData_PerfOS_Memory
                $row.availableMB = & $mb $m.AvailableBytes; $row.commitMB = & $mb $m.CommittedBytes; $row.cacheMB = & $mb $m.CacheBytes
                $now = [pscustomobject]@{ pages = [double]$m.PagesInputPersec; t = Get-Date }
                $row.pagesInPerSec = if ($lastPages) { [math]::Round(($now.pages - $lastPages.pages) / [math]::Max(0.1, ($now.t - $lastPages.t).TotalSeconds)) } else { 0 }
                $lastPages = $now
                $gpu = @(Get-CimInstance Win32_PerfRawData_GPUPerformanceCounters_GPUAdapterMemory -ErrorAction SilentlyContinue)
                $row.gpuDedicatedMB = & $mb ($gpu | Measure-Object DedicatedUsage -Sum).Sum
                $row.gpuSharedMB = & $mb ($gpu | Measure-Object SharedUsage -Sum).Sum
                $driver = Get-Process Driver -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($driver) {
                    $row.driverPrivateMB = & $mb $driver.PrivateMemorySize64; $row.driverWorkingSetMB = & $mb $driver.WorkingSet64
                    $row.driverVirtualMB = & $mb $driver.VirtualMemorySize64
                    $row.driverPeakPrivateMB = & $mb $driver.PeakPagedMemorySize64   # PeakPagefileUsage: peak private bytes
                    $row.driverPeakWorkingSetMB = & $mb $driver.PeakWorkingSet64
                    $own = @(Get-CimInstance Win32_PerfRawData_GPUPerformanceCounters_GPUProcessMemory -ErrorAction SilentlyContinue |
                        Where-Object Name -like "pid_$($driver.Id)_*")
                    $row.driverGpuDedicatedMB = & $mb ($own | Measure-Object DedicatedUsage -Sum).Sum
                    $row.driverGpuSharedMB = & $mb ($own | Measure-Object SharedUsage -Sum).Sum
                }
                $state.samples++
                $state.minAvailableMB = [math]::Min($state.minAvailableMB, $row.availableMB)
                foreach ($pair in @(@('maxCommitMB', 'commitMB'), @('maxPagesInPerSec', 'pagesInPerSec'), @('maxGpuDedicatedMB', 'gpuDedicatedMB'),
                    @('maxGpuSharedMB', 'gpuSharedMB'), @('driverPeakPrivateMB', 'driverPeakPrivateMB'), @('driverPeakWorkingSetMB', 'driverPeakWorkingSetMB'),
                    @('driverMaxVirtualMB', 'driverVirtualMB'), @('driverMaxGpuDedicatedMB', 'driverGpuDedicatedMB'), @('driverMaxGpuSharedMB', 'driverGpuSharedMB'))) {
                    if ($null -ne $row[$pair[1]]) { $state[$pair[0]] = [math]::Max($state[$pair[0]], $row[$pair[1]]) }
                }
                $state.last = $row.seconds
            } catch { $state.error = $_.Exception.Message }
            Add-Content $csv (($row.Values | ForEach-Object { "$_" -replace ',', '.' }) -join ',')
            for ($i = 0; $i -lt $seconds * 4 -and -not $state.stop; $i++) { Start-Sleep -Milliseconds 250 }
        }
    }).AddArgument((Join-Path $RunDir 'memory.csv')).AddArgument($MemorySeconds).AddArgument($memory).AddArgument($started)
    $memoryHandle = $memoryJob.BeginInvoke()
}
function Stop-MemorySampler {
    if (-not $memoryJob) { return $null }
    $memory.stop = $true
    try { [void]$memoryJob.EndInvoke($memoryHandle) } catch { $memory.error = $_.Exception.Message }
    $memoryJob.Dispose(); $script:memoryJob = $null
    $summary = [ordered]@{}
    foreach ($k in 'totalMB', 'minAvailableMB', 'maxCommitMB', 'maxPagesInPerSec', 'maxGpuDedicatedMB', 'maxGpuSharedMB', 'driverPeakPrivateMB',
        'driverPeakWorkingSetMB', 'driverMaxVirtualMB', 'driverMaxGpuDedicatedMB', 'driverMaxGpuSharedMB', 'samples', 'error') {
        if ($null -ne $memory[$k]) { $summary[$k] = $memory[$k] }
    }
    if ($summary.totalMB -and $summary.minAvailableMB -lt [double]::MaxValue) { $summary.peakUsedMB = $summary.totalMB - $summary.minAvailableMB }
    else { $summary.Remove('minAvailableMB') }
    return $summary
}

Add-Type -AssemblyName System.Drawing, System.Windows.Forms
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class DsfGuest {
    [DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError = true)] public static extern bool ReadProcessMemory(IntPtr p, IntPtr a, byte[] b, UIntPtr s, out UIntPtr r);
    [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr VirtualAllocEx(IntPtr p, IntPtr a, UIntPtr s, uint type, uint protect);
    [DllImport("kernel32.dll", SetLastError = true)] public static extern bool WriteProcessMemory(IntPtr p, IntPtr a, byte[] b, UIntPtr s, out UIntPtr w);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr w, out uint pid);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool v);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr w);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr w);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr w, uint msg, IntPtr wp, IntPtr lp);
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr w, out Rect r);

    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr w, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags);
    public static int Skipped;
    // On the host: when an elevated window (which no API call can take the foreground from)
    // is in front, click the game window's corner to activate it, if the game is what's there.
    public static bool ClickToActivate;
    public static string LastClick = "";
    [StructLayout(LayoutKind.Sequential)] public struct Point { public int X, Y; }
    [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(Point p);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, int x, int y, uint data, UIntPtr extra);

    public static byte[] Read(IntPtr p, long address, int count) {
        var buffer = new byte[count]; UIntPtr read;
        if (!ReadProcessMemory(p, new IntPtr(address), buffer, new UIntPtr((uint)count), out read)) return null;
        return buffer;
    }
    public static bool Write(IntPtr p, long address, byte[] bytes) {
        UIntPtr written;
        return WriteProcessMemory(p, new IntPtr(address), bytes, new UIntPtr((uint)bytes.Length), out written) && written.ToUInt64() == (ulong)bytes.Length;
    }
    public static uint ReadU32(IntPtr p, long address) {
        var buffer = new byte[4]; UIntPtr read;
        if (!ReadProcessMemory(p, new IntPtr(address), buffer, new UIntPtr(4), out read) || read.ToUInt64() != 4) return 0;
        return BitConverter.ToUInt32(buffer, 0);
    }

    // Foreground the window inside the guest desktop and tap a key. Arrow keys
    // need extended = true for DirectInput to see the right scan code.
    public static void Tap(IntPtr window, byte vk, byte scan) { Tap(window, vk, scan, false); }
    public static void Tap(IntPtr window, byte vk, byte scan, bool extended) { Send(window, vk, scan, extended, 3); }
    // Hold or release a key; Hold again to keep the game in the foreground.
    public static void Hold(IntPtr window, byte vk, byte scan, bool extended) { Send(window, vk, scan, extended, 1); }
    public static void Release(IntPtr window, byte vk, byte scan, bool extended) { Send(window, vk, scan, extended, 2); }
    // what: 1 key down, 2 key up, 3 both (a tap).
    static void Send(IntPtr window, byte vk, byte scan, bool extended, int what) {
        uint ext = extended ? 1u : 0u;
        uint pid;
        uint fg = GetWindowThreadProcessId(GetForegroundWindow(), out pid);
        uint target = GetWindowThreadProcessId(window, out pid);
        uint self = GetCurrentThreadId();
        if (fg != 0) AttachThreadInput(self, fg, true);
        if (target != 0 && target != fg) AttachThreadInput(self, target, true);
        try {
            BringWindowToTop(window); SetForegroundWindow(window);
            System.Threading.Thread.Sleep(150);
            // Never type into another window (on the host: the user's apps). Key-ups are harmless.
            uint fgPid, gamePid;
            GetWindowThreadProcessId(GetForegroundWindow(), out fgPid);
            GetWindowThreadProcessId(window, out gamePid);
            if ((what & 1) != 0 && window != IntPtr.Zero && fgPid != gamePid && ClickToActivate) {
                Rect r; GetWindowRect(window, out r);
                var corner = new Point { X = r.Left + 6, Y = r.Top + 6 };
                uint underPid; GetWindowThreadProcessId(WindowFromPoint(corner), out underPid);
                LastClick = "rect " + r.Left + "," + r.Top + "," + r.Right + "," + r.Bottom + " under pid " + underPid + " game pid " + gamePid + " fg pid " + fgPid;
                if (underPid == gamePid) {
                    SetCursorPos(corner.X, corner.Y);
                    mouse_event(2, 0, 0, 0, UIntPtr.Zero); System.Threading.Thread.Sleep(60); mouse_event(4, 0, 0, 0, UIntPtr.Zero);
                    System.Threading.Thread.Sleep(300);
                    GetWindowThreadProcessId(GetForegroundWindow(), out fgPid);
                }
            }
            if ((what & 1) != 0 && (window == IntPtr.Zero || fgPid != gamePid)) { Skipped++; return; }
            if ((what & 1) != 0) keybd_event(vk, scan, ext, UIntPtr.Zero);
            if (what == 3) System.Threading.Thread.Sleep(120);
            if ((what & 2) != 0) keybd_event(vk, scan, ext | 2u, UIntPtr.Zero);
        } finally {
            if (target != 0 && target != fg) AttachThreadInput(self, target, false);
            if (fg != 0) AttachThreadInput(self, fg, false);
        }
    }
}
'@

function Remove-RunTree([string]$Path) {
    # Delete links without following them into the read-only game folder.
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($item in Get-ChildItem -LiteralPath $Path -Force) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            if ($item.PSIsContainer) { [IO.Directory]::Delete($item.FullName) } else { [IO.File]::Delete($item.FullName) }
        }
        elseif ($item.PSIsContainer) { Remove-RunTree $item.FullName; [IO.Directory]::Delete($item.FullName) }
        else { [IO.File]::SetAttributes($item.FullName, 'Normal'); [IO.File]::Delete($item.FullName) }
    }
}

function Clear-ReadOnly([string]$Path) {
    # Copies from a read-only source (the VM's C:\Repo shared folder) keep the ReadOnly flag;
    # the harness rewrites some of them (lightsync.lua). Links are not followed.
    foreach ($item in Get-ChildItem -LiteralPath $Path -Force) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        if ($item.PSIsContainer) { Clear-ReadOnly $item.FullName }
        elseif ($item.IsReadOnly) { $item.IsReadOnly = $false }
    }
}

# On the host the game goes to a secondary monitor when there is one: a protected
# Windows Firewall prompt for the run copy of Driver.exe can sit over the middle of
# the primary one, and nothing without admin rights can close it.
$targetScreen = if ($OnHost) { [System.Windows.Forms.Screen]::AllScreens | Where-Object { -not $_.Primary } | Select-Object -First 1 }
# With one monitor, the game goes to its top-left corner, clear of the centred prompt
# (run host-20261023-084934: the prompt covered the chase car in every shot).
if ($OnHost -and -not $targetScreen) { $targetScreen = [System.Windows.Forms.Screen]::PrimaryScreen }

function Get-GameWindow {
    # The handle changes when dsf_enhanced makes the window borderless.
    $driver.Refresh()
    $window = $driver.MainWindowHandle
    if ($targetScreen -and $window -ne [IntPtr]::Zero) {
        $rect = New-Object DsfGuest+Rect
        [DsfGuest]::GetWindowRect($window, [ref]$rect) | Out-Null
        $b = $targetScreen.Bounds
        if ($rect.Left -lt $b.Left -or $rect.Top -lt $b.Top -or $rect.Left -ge $b.Right -or $rect.Top -ge $b.Bottom -or
            $window -ne $script:placedWindow) {
            # Topmost (other windows on that monitor would cover it), SWP_NOSIZE | SWP_NOACTIVATE.
            [DsfGuest]::SetWindowPos($window, [IntPtr](-1), $b.Left, $b.Top, 0, 0, 0x11) | Out-Null
            $script:placedWindow = $window
        }
    }
    return $window
}

function Test-CrashDialog {
    # DriverNGHook shows this dialog on a fatal error and writes its minidump
    # only after OK is pressed. Press it, wait for the dump, report the crash.
    $dialog = [DsfGuest]::FindWindow('#32770', 'Driver San Francisco | DriverNGHook')
    if ($dialog -eq [IntPtr]::Zero) { return $false }
    Save-Shot 'crash-dialog.png'
    [DsfGuest]::PostMessage($dialog, 0x0111, [IntPtr]1, [IntPtr]::Zero) | Out-Null  # WM_COMMAND IDOK
    for ($i = 0; $i -lt 60 -and -not (Get-ChildItem $RunPath -Filter *.dmp | Where-Object LastWriteTime -gt $started); $i++) {
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Seconds 3  # let the writer finish
    return $true
}

function Save-Shot([string]$Name) {
    try {
        $window = Get-GameWindow
        $rect = New-Object DsfGuest+Rect
        [DsfGuest]::GetWindowRect($window, [ref]$rect) | Out-Null
        # Clip to the guest screen; the borderless window can be larger.
        $screen = [System.Windows.Forms.Screen]::FromHandle($window).Bounds
        $left = [math]::Max($rect.Left, $screen.Left); $top = [math]::Max($rect.Top, $screen.Top)
        $w = [math]::Min($rect.Right, $screen.Right) - $left; $h = [math]::Min($rect.Bottom, $screen.Bottom) - $top
        if ($w -le 0 -or $h -le 0) { Note "screenshot $Name skipped: window $window rect $($rect.Left),$($rect.Top),$($rect.Right),$($rect.Bottom)"; return }
        $rect.Left = $left; $rect.Top = $top
        $bitmap = New-Object System.Drawing.Bitmap $w, $h
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, (New-Object System.Drawing.Size $w, $h))
        $bitmap.Save((Join-Path $RunDir $Name), [System.Drawing.Imaging.ImageFormat]::Png)
        $graphics.Dispose(); $bitmap.Dispose()
    }
    catch { Note "screenshot $Name failed: $($_.Exception.Message)" }
}

[DsfGuest]::SetProcessDPIAware() | Out-Null   # screen coordinates in real pixels
[DsfGuest]::ClickToActivate = [bool]$OnHost
# On the host: keep the display on (screenshots) while this run lasts.
if ($OnHost) { [DsfGuest]::SetThreadExecutionState([uint32]2147483651) | Out-Null }

$result = [ordered]@{ archive = $Archive; hook = [bool]$Hook; probe = [bool]$Probe; stage = 'start' }
try {
    Get-Process Driver -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 500

    # Build the run folder (C:\Run in the sandbox).
    $run = $RunPath
    Remove-RunTree $run
    New-Item -ItemType Directory -Force -Path $run | Out-Null
    $result.gameRoot = [ordered]@{}   # SHA-256 of the game folder's root files (run-series.ps1 -Vm compares the host's)
    foreach ($file in Get-ChildItem $GameDir -File | Where-Object { $_.Extension -notin '.log', '.dmp' }) {
        Copy-Item -LiteralPath $file.FullName -Destination $run
        $result.gameRoot[$file.Name] = (Get-FileHash -LiteralPath $file.FullName).Hash
    }
    # -GameRootFrom: its files replace the game folder's root files of the same name (run-series.ps1
    # -Vm: the host game folder's current dsf_enhanced.asi and .ini; C:\Game in the VM is a copy
    # from when it was provisioned). -Override still wins (below).
    $rootFrom = @{}
    if ($GameRootFrom) {
        foreach ($file in Get-ChildItem -LiteralPath $GameRootFrom -File) {
            Copy-Item -LiteralPath $file.FullName (Join-Path $run $file.Name) -Force
            $rootFrom[$file.Name.ToLower()] = $file.FullName
        }
        Note "root files from ${GameRootFrom}: $(($rootFrom.Keys | Sort-Object) -join ', ')"
    }
    if (-not $OnHost) { Copy-Item "$OutputDir\HostDll\*.dll" $run }
    $iniFrom = if ($rootFrom['dsf_enhanced.ini']) { $rootFrom['dsf_enhanced.ini'] } else { Join-Path $GameDir 'dsf_enhanced.ini' }
    (Get-Content -LiteralPath $iniFrom) `
        -replace '^TrafficSpawnDistanceScale=.*$', 'TrafficSpawnDistanceScale=1.0' `
        -replace '^LODEngineThresholdScale=.*$', 'LODEngineThresholdScale=1.0' |
        Set-Content (Join-Path $run 'dsf_enhanced.ini')
    Remove-Item (Join-Path $run 'Driver.log'), (Join-Path $run 'dsf_enhanced.log') -ErrorAction SilentlyContinue

    # -Override: relative game path (lower-case) -> replacement file.
    $overrides = @{}
    foreach ($pair in ($Override -split ';' | Where-Object { $_ })) {
        $name, $path = $pair -split '=', 2
        $overrides[$name.Trim().ToLower()] = $path.Trim()
    }
    # A key without a folder names a root file (e.g. 'dsf_enhanced.asi=out\x\dsf_enhanced.asi',
    # 'dsf_enhanced.ini=out\x\test.ini'): copied over the run folder's copy, so a test plugin
    # never touches the game folder. An overridden dsf_enhanced.ini is used as given (no rewrite above).
    foreach ($key in @($overrides.Keys | Where-Object { $_ -notmatch '\\' })) {
        Copy-Item $overrides[$key] (Join-Path $run $key) -Force
        Note "root file $key <- $($overrides[$key])"
    }
    # Which dsf_enhanced the run uses: the .asi's hash and source, the source .ini's hash (the
    # run copy has TrafficSpawnDistanceScale and LODEngineThresholdScale set to 1.0 unless overridden).
    $sourceOf = { param($name) if ($overrides[$name]) { $overrides[$name] } elseif ($rootFrom[$name]) { $rootFrom[$name] } else { Join-Path $GameDir $name } }
    $hashOf = { param($path) if (Test-Path -LiteralPath $path) { (Get-FileHash -LiteralPath $path).Hash } }
    $iniSource = & $sourceOf 'dsf_enhanced.ini'
    $result.dsfEnhanced = [ordered]@{
        asi = & $hashOf (Join-Path $run 'dsf_enhanced.asi'); asiFrom = & $sourceOf 'dsf_enhanced.asi'
        ini = & $hashOf $iniSource; iniFrom = $iniSource; iniRewritten = -not $overrides['dsf_enhanced.ini'] }
    $overrides['install\dngvehicles.sp'] = $Archive
    # The build's files: an archive named dngvehicles.sp in a port_batch build folder goes with
    # the files tools\install.ps1 installs beside it: dngvehicles.lua as dsfforza\ported.lua
    # (register.lua makes each car's vehicleStats entry from it) and gui\ (gui.sp, Fonts.fchunk,
    # hud.lua and Locale\<language>\global.fchunk: the car's own badge, plate and pictures).
    # Without an entry the HUD draws a row of other cars' badges and plates over the screen
    # (research/in-game-tests.md, "The HUD needs a vehicleStats entry"). -Override and -LuaFiles
    # win; -ArchiveOnly takes none. register.lua doesn't add these cars to traffic here, as before.
    $buildLua = [ordered]@{}   # UserLuaScripts name -> file, copied before -LuaFiles
    $buildFiles = @()
    $buildFolder = Split-Path $Archive
    if (-not $ArchiveOnly -and (Split-Path $Archive -Leaf) -eq 'dngvehicles.sp' -and
        (Test-Path -LiteralPath (Join-Path $buildFolder 'dngvehicles.lua'))) {
        $buildLua['dsfforza\ported.lua'] = Join-Path $buildFolder 'dngvehicles.lua'
        $gui = Join-Path $buildFolder 'gui'
        if (@('gui.sp', 'Fonts.fchunk', 'hud.lua' | Where-Object { Test-Path -LiteralPath (Join-Path $gui $_) }).Count -eq 3) {
            $buildLua['dsfforza\hud.lua'] = Join-Path $gui 'hud.lua'
            $guiFiles = @{ 'install\gui.sp' = Join-Path $gui 'gui.sp'; 'media\fonts.fchunk' = Join-Path $gui 'Fonts.fchunk' }
            foreach ($file in Get-ChildItem -LiteralPath (Join-Path $gui 'Locale') -Recurse -File -Filter 'global.fchunk' -ErrorAction SilentlyContinue) {
                $guiFiles["locale\$($file.Directory.Name.ToLower())\global.fchunk"] = $file.FullName
            }
            foreach ($key in $guiFiles.Keys) {
                if (-not $overrides.ContainsKey($key)) { $overrides[$key] = $guiFiles[$key]; $buildFiles += $guiFiles[$key] }
            }
        }
        foreach ($pair in ($LuaFiles -split ';' | Where-Object { $_ })) { $buildLua.Remove(($pair -split '=', 2)[0].Trim()) }
        $buildFiles += @($buildLua.Values)
        Note "build files from ${buildFolder}: $(($buildFiles | ForEach-Object { $_.Substring($buildFolder.Length + 1) }) -join ', ')"
    }
    $result.buildFiles = $buildFiles
    # A dsfforza install on the host (tools\install.ps1) replaces some game files. Tests use
    # the repo's copy of the originals (out\vanilla, see src\dsfforza\game\game_paths.py) unless -Override
    # names a file. Never link the install's own dsfforza-backup: a running test would hold
    # those files open and block install.ps1.
    $backup = Join-Path $RepoDir 'out\vanilla'
    if ((Test-Path (Join-Path $GameDir 'dsfforza-backup')) -and (Test-Path $backup)) {
        foreach ($file in Get-ChildItem $backup -Recurse -File | Where-Object { $_.Name -ne 'install.json' }) {
            $key = $file.FullName.Substring($backup.Length + 1).ToLower()
            if ($key -like 'common\*') { continue }   # UserLuaScripts come from the repo
            if (-not $overrides.ContainsKey($key)) { $overrides[$key] = $file.FullName }
        }
    }

    # A file link: a symlink, else (no symlink privilege on the host) a hard link on the
    # same volume, else a copy.
    function New-FileLink([string]$path, [string]$from) {
        if ($CopyRepoFiles -and $from.StartsWith("$RepoDir\", [StringComparison]::OrdinalIgnoreCase)) { Copy-Item $from $path; return }
        try { New-Item -ItemType SymbolicLink -Path $path -Target $from -ErrorAction Stop | Out-Null; return } catch { }
        if ([IO.Path]::GetPathRoot((Resolve-Path $from).Path) -eq [IO.Path]::GetPathRoot($path)) {
            try { New-Item -ItemType HardLink -Path $path -Target $from -ErrorAction Stop | Out-Null; return } catch { }
        }
        Copy-Item $from $path
    }

    # A folder with no overridden file below it becomes one junction; otherwise a real folder of
    # file links (overridden ones to their replacement) and, recursively, the same for subfolders.
    function Add-LinkTree([string]$source, [string]$target, [string]$key) {
        if (-not @($overrides.Keys | Where-Object { $_.StartsWith($key + '\') }).Count) {
            New-Item -ItemType Junction -Path $target -Target $source | Out-Null
            return
        }
        New-Item -ItemType Directory -Path $target | Out-Null
        foreach ($item in Get-ChildItem $source) {
            $itemKey = "$key\$($item.Name.ToLower())"
            if ($item.PSIsContainer) {
                Add-LinkTree $item.FullName (Join-Path $target $item.Name) $itemKey
            } else {
                $from = if ($overrides.ContainsKey($itemKey)) { $overrides[$itemKey] } else { $item.FullName }
                New-FileLink (Join-Path $target $item.Name) $from
            }
        }
    }

    foreach ($dir in Get-ChildItem $GameDir -Directory) {
        $target = Join-Path $run $dir.Name
        switch ($dir.Name) {
            'Install' {
                New-Item -ItemType Directory -Path $target | Out-Null
                foreach ($file in Get-ChildItem $dir.FullName -File) {
                    $key = "install\$($file.Name.ToLower())"
                    $source = if ($overrides.ContainsKey($key)) { $overrides[$key] } else { $file.FullName }
                    New-FileLink (Join-Path $target $file.Name) $source
                }
            }
            'Common' {
                New-Item -ItemType Directory -Path $target | Out-Null
                foreach ($sub in Get-ChildItem $dir.FullName -Directory) {
                    if ($sub.Name -eq 'UserLuaScripts') { continue }
                    New-Item -ItemType Junction -Path (Join-Path $target $sub.Name) -Target $sub.FullName | Out-Null
                }
                $userLua = New-Item -ItemType Directory -Path (Join-Path $target 'UserLuaScripts')
                if ($Probe) {
                    Copy-Item (Join-Path $RepoDir 'tools\game\lua\*') $userLua.FullName -Recurse
                    foreach ($name in $buildLua.Keys) { Copy-Item $buildLua[$name] (Join-Path $userLua.FullName $name) -Force }
                    foreach ($pair in ($LuaFiles -split ';' | Where-Object { $_ })) {
                        $name, $path = $pair -split '=', 2
                        Copy-Item $path.Trim() (Join-Path $userLua.FullName $name.Trim()) -Force
                    }
                    $luaList = { param($text) ($text -split ',' | Where-Object { $_ } | ForEach-Object { [int]$_ }) -join ', ' }
                    $fields = @()
                    if ($ProbeIds) { $fields += "ids = { $(& $luaList $ProbeIds) }" }
                    if ($TrafficIds) { $fields += "traffic = { $(& $luaList $TrafficIds) }" }
                    if ($AheadIds) { $fields += "ahead = { $(& $luaList $AheadIds) }" }
                    if ($DriveSeconds) { $fields += "drive = $DriveSeconds" }
                    if ($HoldMph) { $fields += "hold_mph = { $(& $luaList $HoldMph) }" }
                    if ($Damage) { $fields += "damage = true" }
                    if ($DamagePanels) { $fields += "damage_panels = true" }
                    if ($DamageSide) { $fields += "damage_side = true" }
                    if ($GlassLadder) { $fields += "glass_ladder = true" }
                    if ($Palette -ge 0) { $fields += "palette = $Palette" }
                    if ($buildLua.Contains('dsfforza\ported.lua')) { $fields += "register = { interesting = false }" }
                    if ($Views) { $fields += "views = { " + (($Views -split ',' | Where-Object { $_ } | ForEach-Object { "`"$($_.Trim())`"" }) -join ', ') + " }" }
                    if ($Lights) {
                        $fields += $(if ($LightsDriveOnly) { "lights = { dark = true, phases = false }" } else { "lights = { dark = true }" })
                        # Reset every run: a lightsync.lua left at an old phase (-1) would end the
                        # Lua's first car at once.
                        [IO.File]::WriteAllText((Join-Path $userLua.FullName 'dsfforza\lightsync.lua'), "return { phase = 0 }`r`n")
                    }
                    if ($Dyno) {
                        $dynoFields = "runs = $DynoRuns"
                        if ($DynoStart) { $dynoFields += ", start = { $DynoStart }" }
                        if ($DynoScan) { $dynoFields += ", scan = { " + (($DynoScan -split '[;,]' | Where-Object { $_ }) -join ', ') + " }" }
                        if ($DynoKeep) { $dynoFields += ", keep = { " + (($DynoKeep -split ',' | ForEach-Object { "`"$_`"" }) -join ', ') + " }" }
                        $fields += "dyno = { $dynoFields }"
                    }
                    if ($fields) {
                        "return { $($fields -join ', ') }" |
                            Set-Content (Join-Path $userLua.FullName 'dsfforza\config.lua') -Encoding ASCII
                    }
                }
            }
            'plugins' {
                New-Item -ItemType Directory -Path $target | Out-Null
                if ($Hook) { Copy-Item (Join-Path $RepoDir '_ref\downloads\dnghook-0.7.2\extracted\plugins\*') $target -Recurse }
                if ($Asi) {
                    Copy-Item $Asi $target
                    $asiIni = [IO.Path]::ChangeExtension($Asi, '.ini')
                    if (Test-Path $asiIni) { Copy-Item $asiIni $target }
                }
            }
            'dsfforza-backup' { }
            default { Add-LinkTree $dir.FullName $target $dir.Name.ToLower() }
        }
    }
    Clear-ReadOnly $run
    $result.archiveSize = (Get-Item $Archive).Length
    Note 'run folder built'

    # Launch and wait for the window.
    $result.stage = 'launch'
    # A guest Start menu left open (run 20261022-223030: a Windows key stuck down from an
    # earlier session) takes every Enter press. Release both Windows keys and close it.
    foreach ($vk in 0x5B, 0x5C) { [DsfGuest]::keybd_event([byte]$vk, 0, 2, [UIntPtr]::Zero) }
    if (-not $OnHost) { [DsfGuest]::keybd_event(0x1B, 0x01, 0, [UIntPtr]::Zero); [DsfGuest]::keybd_event(0x1B, 0x01, 2, [UIntPtr]::Zero) }
    # On the host an elevated window in front (a VPN client, say) would keep the foreground:
    # nothing at medium integrity can take it, not even injected clicks. The shell's
    # "minimize all" reaches it, and the game can take the foreground from the desktop.
    if ($OnHost) { (New-Object -ComObject Shell.Application).MinimizeAll(); Start-Sleep -Seconds 1 }
    Start-Sleep -Milliseconds 300
    $driver = Start-Process (Join-Path $run 'Driver.exe') -WorkingDirectory $run -PassThru
    $deadline = (Get-Date).AddSeconds(90)
    do {
        Start-Sleep -Milliseconds 250
        $driver.Refresh()
        if ($driver.HasExited) { throw "Driver exited during startup (code $($driver.ExitCode))" }
    } until ($driver.MainWindowHandle -ne 0 -or (Get-Date) -gt $deadline)
    if ($driver.MainWindowHandle -eq 0) { throw 'Driver window did not appear within 90 s' }
    Note 'window up'

    # Enter Story: Driver.exe+0xBE7228 -> engine; +0x240 -> world-sector manager;
    # a first node that isn't the sentinel means the world has loaded.
    $result.stage = 'story'
    $handle = [DsfGuest]::OpenProcess(0x1010, $false, [uint32]$driver.Id)
    $worldReady = {
        $engine = [DsfGuest]::ReadU32($handle, 0x00400000 + 0x00BE7228)
        if ($engine -lt 0x10000) { return $false }
        $manager = [DsfGuest]::ReadU32($handle, $engine + 0x240)
        if ($manager -lt 0x10000) { return $false }
        $first = [DsfGuest]::ReadU32($handle, $manager + 0x347F0)
        return $first -ge 0x10000 -and $first -ne ($manager + 0x347EC)
    }
    Start-Sleep -Seconds 5
    $pulses = 0
    $deadline = (Get-Date).AddSeconds(150)
    while (-not (& $worldReady)) {
        $driver.Refresh()
        if ($driver.HasExited) { throw "Driver exited in the frontend (code $($driver.ExitCode))" }
        if (Test-CrashDialog) { $result.crashed = $true; throw "game crashed while loading (DriverNGHook fatal error dialog)" }
        if ((Get-Date) -gt $deadline) { throw "Story world not ready after $pulses Enter presses" }
        [DsfGuest]::Tap((Get-GameWindow), 0x0D, 0x1C)
        $pulses++
        if ($pulses % 5 -eq 0) { Save-Shot ("frontend-{0:D2}.png" -f $pulses) }
        for ($i = 0; $i -lt 20 -and -not (& $worldReady); $i++) { Start-Sleep -Milliseconds 250 }
    }
    $result.enterPresses = $pulses
    Note "story world ready after $pulses presses"
    if ($TrafficRadius -gt 0) {
        # Plan A as a runtime patch: point the two movss operands (0x772A00 350 m, 0x7729F6 256 m)
        # at floats in a new page; the shared constants 0xDE2344 / 0xDE2454 stay untouched.
        $rw = [DsfGuest]::OpenProcess(0x1038, $false, [uint32]$driver.Id)
        $page = [DsfGuest]::VirtualAllocEx($rw, [IntPtr]::Zero, (New-Object UIntPtr 4096), 0x3000, 0x04)
        $sites = @(@(0x772A00, 0xDE2344, $TrafficRadius), @(0x7729F6, 0xDE2454, ($TrafficRadius * 256 / 350)))
        $notes = @()
        for ($i = 0; $i -lt $sites.Count; $i++) {
            $site, $orig, $value = $sites[$i]
            $want = [byte[]](@(0xF3, 0x0F, 0x10, 0x05) + [BitConverter]::GetBytes([uint32]$orig))
            $have = [DsfGuest]::Read($rw, $site, 8)
            $float = $page.ToInt64() + 4 * $i
            if ($page -eq [IntPtr]::Zero -or -not $have -or (Compare-Object $want $have -SyncWindow 0)) {
                $notes += ('0x{0:X} unexpected bytes {1}' -f $site, (($have | ForEach-Object { '{0:X2}' -f $_ }) -join ' ')); continue
            }
            $ok = [DsfGuest]::Write($rw, $float, [BitConverter]::GetBytes([single]$value)) -and
                [DsfGuest]::Write($rw, $site + 4, [BitConverter]::GetBytes([uint32]$float))
            $notes += ('0x{0:X} -> [0x{1:X}] = {2} {3}' -f $site, $float, $value, $(if ($ok) { 'ok' } else { 'WRITE FAILED' }))
        }
        [DsfGuest]::CloseHandle($rw) | Out-Null
        $result.trafficRadius = $notes -join '; '
        Note "traffic radius patch: $($result.trafficRadius)"
    }
    Start-Sleep -Seconds 8
    Save-Shot 'story.png'

    # A loaded save starts in the Shift overview; press Up (ZOOM IN) to descend.
    for ($zoom = 1; $zoom -le $ZoomPresses; $zoom++) {
        [DsfGuest]::Tap((Get-GameWindow), 0x26, 0x48, $true)
        Start-Sleep -Seconds 4
        Save-Shot ("zoom-{0:D2}.png" -f $zoom)
    }
    if ($ZoomPresses) { Note "pressed Up $ZoomPresses times" }

    # Wait for the probe (or just watch the game for -WaitSeconds).
    $result.stage = 'observe'
    $log = Join-Path $run 'Driver.log'
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    $shot = 0
    # Throttle: Up is an extended key (scan 0x48), W is scan 0x11.
    $key = switch ($DriveKey) { 'W' { @(0x57, 0x11, $false) } 'Left' { @(0x25, 0x4B, $true) } 'Right' { @(0x27, 0x4D, $true) } default { @(0x26, 0x48, $true) } }
    $cockpits = 0; $holdUntil = $null
    # -Dyno: read Driver.log incrementally (it grows by a line per Lua update while a car runs)
    # and, for each "dyno <id> r<k> ready" line, hold the throttle, then the brake.
    $logPos = 0; $dynoQueue = [System.Collections.Generic.Queue[string]]::new()
    $brakeKey = @(0x28, 0x50, $true)
    $keyCodes = @{ Up = @(0x26, 0x48, $true); Down = @(0x28, 0x50, $true); Left = @(0x25, 0x4B, $true)
        Right = @(0x27, 0x4D, $true); Space = @(0x20, 0x39, $false); Shift = @(0xA0, 0x2A, $false)
        W = @(0x57, 0x11, $false); A = @(0x41, 0x1E, $false); S = @(0x53, 0x1F, $false); D = @(0x44, 0x20, $false)
        Enter = @(0x0D, 0x1C, $false); Esc = @(0x1B, 0x01, $false); Tab = @(0x09, 0x0F, $false)
        Q = @(0x51, 0x10, $false); E = @(0x45, 0x12, $false) }
    # A name missing here would be sent as key code 0, i.e. nothing, for the whole run.
    $keyNames = @($MenuKeys -split ',' | ForEach-Object { ($_ -split ':')[0] }) +
        @($DynoKeys -split '[;,]' | ForEach-Object { ($_ -split ':')[0] -split '\+' })
    foreach ($name in ($keyNames | Where-Object { $_ -and $_ -ne 'None' })) {
        if (-not $keyCodes.ContainsKey($name)) { throw "unknown key '$name' in -MenuKeys/-DynoKeys (known: $(($keyCodes.Keys | Sort-Object) -join ' '))" }
    }
    # -DynoKeys: one schedule per run, steps 'Key+Key:seconds'. A key is pressed when a
    # step needs it and released when the next step doesn't (no re-pressing in between),
    # so step boundaries are as sharp as keybd_event allows (about 0.15 s per key change).
    # Step times: seconds, or '@mph': as long as this car took to reach that speed in its
    # run 1 (the acceleration run, from its 'dy <id> 1 ...' lines), so every car meets a
    # manoeuvre at the same speed. Keys 'None' = no key held (throttle lifted).
    $runOne = @{}
    $dynoDone = @{}
    function Get-StepSeconds([string]$text, [string]$car) {
        if (-not $text.StartsWith('@')) { return [double]$text }
        $target = [double]$text.Substring(1)
        $trace = $runOne[$car]
        if (-not $trace) { return 5.0 }
        # From the last standstill before the target speed: the run-1 trace can open with
        # the car reversing on the previous car's brake key (run 20261022-175640).
        $start = $null
        foreach ($pt in $trace) {
            if ($pt[1] -le 1.0) { $start = $pt[0] }
            elseif ($null -ne $start -and $pt[1] * 2.23694 -ge $target) { return ($pt[0] - $start) * 0.008 }
        }
        return 5.0
    }
    function Invoke-KeySteps([string]$schedule, [string]$car) {
        $held = @()
        try {
            foreach ($step in ($schedule -split ',')) {
                $names, $seconds = $step -split ':'
                $seconds = Get-StepSeconds $seconds $car
                $want = @(if ($names -ne 'None') { $names -split '\+' })
                foreach ($k in @($held | Where-Object { $want -notcontains $_ })) {
                    $spec = $keyCodes[$k]; [DsfGuest]::Release((Get-GameWindow), $spec[0], $spec[1], $spec[2])
                }
                foreach ($k in @($want | Where-Object { $held -notcontains $_ })) {
                    $spec = $keyCodes[$k]; [DsfGuest]::Hold((Get-GameWindow), $spec[0], $spec[1], $spec[2])
                }
                $held = $want
                $until = (Get-Date).AddSeconds([double]$seconds)
                if ([double]$seconds -ge $ShotSeconds) {
                    $name = "observe-{0:D2}.png" -f (++$script:shot)
                    Save-Shot $name; Note "$name ($names)"
                }
                while ((Get-Date) -lt $until) { Start-Sleep -Milliseconds 10 }
            }
        }
        finally {
            foreach ($k in $held) { $spec = $keyCodes[$k]; [DsfGuest]::Release((Get-GameWindow), $spec[0], $spec[1], $spec[2]) }
        }
    }
    function Read-LogTail {
        if (-not (Test-Path $log)) { return @() }
        $stream = [IO.File]::Open($log, 'Open', 'Read', 'ReadWrite')
        try {
            if ($stream.Length -lt $script:logPos) { $script:logPos = 0 }
            $stream.Seek($script:logPos, 'Begin') | Out-Null
            $reader = New-Object IO.StreamReader($stream)
            $text = $reader.ReadToEnd()
            $cut = $text.LastIndexOf("`n")
            if ($cut -lt 0) { return @() }
            $script:logPos += [Text.Encoding]::UTF8.GetByteCount($text.Substring(0, $cut + 1))
            return $text.Substring(0, $cut).Split("`n")
        }
        finally { $stream.Dispose() }
    }
    # -Lights: for each "[dsfforza.lights] id N: ready K phases" line, ask lightstest.lua for
    # phases 1..K in turn (dsfforza\lightsync.lua, re-read by the Lua every update) and take two
    # shots per phase 0.7 s apart (indicators blink), then tell it to finish (-1).
    $lightSync = Join-Path $RunPath 'Common\UserLuaScripts\dsfforza\lightsync.lua'
    # The game reads lightsync.lua every update (io.open), so a write can hit a sharing
    # violation (run host-20261023-095111 died on one at vanilla 171 phase 10). Retry for up
    # to 5 s, then note the miss and go on: the Lua advances by itself after its timeout.
    function Set-LightSync([int]$phase) {
        $text = "return { phase = $phase }`r`n"
        for ($try = 1; $try -le 100; $try++) {
            try {
                $stream = [IO.File]::Open($lightSync, 'Create', 'Write', 'ReadWrite')
                try { $bytes = [Text.Encoding]::ASCII.GetBytes($text); $stream.Write($bytes, 0, $bytes.Length) }
                finally { $stream.Dispose() }
                if ($try -gt 1) { Note "lights: lightsync.lua phase $phase written after $try tries" }
                return $true
            }
            catch [System.IO.IOException] { Start-Sleep -Milliseconds 50 }
            catch [System.UnauthorizedAccessException] { Start-Sleep -Milliseconds 50 }
        }
        Note "lights: lightsync.lua phase $phase NOT written (file locked for 5 s)"
        return $false
    }
    # Real-driving lamps before any setLights call on the car, once in daylight (sync 90) and
    # once under the dark mood (91); lightstest.lua puts the car back on its spot for each.
    # From rest: idle, throttle, brake while rolling forward, brake into reverse, throttle
    # out of the reverse (braking), idle. Shots lights-<id>-k<d|n><step>.png; the Lua logs
    # the distance moved per update and isReversing meanwhile.
    function Invoke-LightKeys([int]$id, [string]$tag) {
        # keys | seconds | shots ('seconds-into-step:name ...')
        $steps = @(
            "None|2.5|2.0:${tag}0idle",
            "Up|2.2|1.6:${tag}6throttle",
            "Down|3.0|0.25:${tag}1brake 0.7:${tag}2brake 2.7:${tag}3reverse",
            "Up|0.7|0.35:${tag}4brakeup",
            "None|2.5|2.2:${tag}5idle"
        )
        $held = @()
        try {
            foreach ($step in $steps) {
                $names, $seconds, $shotText = $step -split '\|'
                $seconds = [double]$seconds
                $shots = @($shotText -split ' ' | Where-Object { $_ } | ForEach-Object { $at, $label = $_ -split ':'; ,@([double]$at, $label) })
                $want = @(if ($names -ne 'None') { $names -split '\+' })
                foreach ($k in @($held | Where-Object { $want -notcontains $_ })) {
                    $spec = $keyCodes[$k]; [DsfGuest]::Release((Get-GameWindow), $spec[0], $spec[1], $spec[2])
                }
                foreach ($k in @($want | Where-Object { $held -notcontains $_ })) {
                    $spec = $keyCodes[$k]; [DsfGuest]::Hold((Get-GameWindow), $spec[0], $spec[1], $spec[2])
                }
                $held = $want
                $t0 = Get-Date
                foreach ($shotAt in $shots) {
                    $wait = $shotAt[0] - ((Get-Date) - $t0).TotalSeconds
                    if ($wait -gt 0) { Start-Sleep -Milliseconds ([int](1000 * $wait)) }
                    $name = "lights-{0}-{1}.png" -f $id, $shotAt[1]
                    Save-Shot $name; Note "lights: $name ($names)"
                }
                $wait = $seconds - ((Get-Date) - $t0).TotalSeconds
                if ($wait -gt 0) { Start-Sleep -Milliseconds ([int](1000 * $wait)) }
            }
        }
        finally {
            foreach ($k in $held) { $spec = $keyCodes[$k]; [DsfGuest]::Release((Get-GameWindow), $spec[0], $spec[1], $spec[2]) }
        }
    }
    function Invoke-LightPhases([int]$id, [int]$count, [bool]$noDrive) {
        # "(no drive)" on the ready line (drawdist.lua): phases only, no drive steps.
        foreach ($drive in @(if (-not $noDrive) { @(90, 'd'), @(91, 'n') })) {
            Set-LightSync $drive[0] | Out-Null
            Start-Sleep -Seconds 3
            Invoke-LightKeys $id $drive[1]
        }
        for ($k = 1; $k -le $count; $k++) {
            Set-LightSync $k | Out-Null
            Start-Sleep -Seconds 3
            Save-Shot ("lights-{0}-{1:D2}a.png" -f $id, $k)
            Start-Sleep -Milliseconds 700
            Save-Shot ("lights-{0}-{1:D2}b.png" -f $id, $k)
            Note "lights: id $id phase $k of $count"
        }
        # No traffic phase (removed from lightstest.lua): swapWithModel on a live traffic car
        # crashed the game (run host-20261023-123840) and AddVehicleToCarrier on a spawned
        # car returned false (run 20261023-115652). Check converted traffic with
        # -TrafficIds instead (research/lights.md).
        Set-LightSync -1 | Out-Null
        Start-Sleep -Seconds 2
    }
    function Add-DynoReady {
        foreach ($line in (Read-LogTail)) {
            if ($line -match '\[dsfforza\] dyno \d+ r\d+ ready') { $dynoQueue.Enqueue($line.Trim()) }
            elseif ($line -match '\[dsfforza\] dy (\d+) 1 (\d+) ([\d.]+)') {
                if (-not $runOne.ContainsKey($Matches[1])) { $runOne[$Matches[1]] = [System.Collections.Generic.List[object]]::new() }
                $runOne[$Matches[1]].Add(@([int]$Matches[2], [double]$Matches[3]))
            }
        }
    }
    function Invoke-KeyHold($keySpec, [double]$seconds, [string]$what) {
        if ($seconds -le 0) { return }
        $until = (Get-Date).AddSeconds($seconds); $nextShot = (Get-Date).AddSeconds($ShotSeconds)
        try {
            while ((Get-Date) -lt $until) {
                [DsfGuest]::Hold((Get-GameWindow), $keySpec[0], $keySpec[1], $keySpec[2])
                if ((Get-Date) -ge $nextShot) {
                    $name = "observe-{0:D2}.png" -f (++$script:shot)
                    Save-Shot $name; Note "$name ($what)"
                    $nextShot = (Get-Date).AddSeconds($ShotSeconds)
                }
                Start-Sleep -Milliseconds 250
            }
        }
        finally { [DsfGuest]::Release((Get-GameWindow), $keySpec[0], $keySpec[1], $keySpec[2]) }
    }
    try {
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds $ShotSeconds
        $driver.Refresh()
        if ($driver.HasExited) { $result.exitCode = $driver.ExitCode; throw "Driver exited while observing (code $($driver.ExitCode))" }
        if (Test-CrashDialog) { $result.crashed = $true; throw "game crashed (DriverNGHook fatal error dialog)" }
        if ($MenuKeys -and -not $script:menuDone) {
            # -MenuKeys: once a log line matches -MenuTrigger, tap each key ('Enter:2,Right:1.5,...';
            # key:seconds to wait after it) and screenshot after every tap (menu-NN.png).
            if (@(Read-LogTail | Where-Object { $_ -match $MenuTrigger }).Count) {
                $script:menuDone = $true
                Start-Sleep -Seconds 3
                $menuShot = 0
                Save-Shot ("menu-{0:D2}.png" -f $menuShot)
                foreach ($step in ($MenuKeys -split ',')) {
                    $name, $wait = $step -split ':'
                    $spec = $keyCodes[$name]
                    [DsfGuest]::Tap((Get-GameWindow), $spec[0], $spec[1], $spec[2])
                    Start-Sleep -Milliseconds ([int](1000 * [double]$(if ($wait) { $wait } else { 1.5 })))
                    Save-Shot ("menu-{0:D2}.png" -f (++$menuShot))
                    Note "menu: $name -> menu-$('{0:D2}' -f $menuShot).png"
                }
            }
            Save-Shot ("observe-{0:D2}.png" -f (++$shot))
        }
        elseif ($Lights -and $Probe) {
            foreach ($line in (Read-LogTail)) {
                if ($line -match '\[dsfforza\.lights\] id (\d+): ready (\d+) phases') {
                    Note "lights: $($line.Trim())"
                    Invoke-LightPhases ([int]$Matches[1]) ([int]$Matches[2]) ($line -match '\(no drive\)')
                }
            }
            Save-Shot ("observe-{0:D2}.png" -f (++$shot))
        }
        elseif ($Dyno -and $Probe) {
            Add-DynoReady
            Save-Shot ("observe-{0:D2}.png" -f (++$shot))
            while ($dynoQueue.Count) {
                $ready = $dynoQueue.Dequeue()
                # dyno.lua says "ready" again when it re-places a car that rolled back before
                # the throttle; drive each run once.
                $runKey = if ($ready -match 'dyno (\d+ r\d+) ready') { $Matches[1] } else { $ready }
                if ($dynoDone.ContainsKey($runKey)) { Note "dyno: $runKey again (re-placed), keys already sent"; continue }
                $dynoDone[$runKey] = $true
                Note "dyno: $ready; throttle $DriveSeconds s, brake $BrakeSeconds s"
                if ($DynoKeys) {
                    $runs = @($DynoKeys -split ';')
                    $k = if ($ready -match ' r(\d+) ready') { [int]$Matches[1] } else { 1 }
                    $schedule = $runs[($k - 1) % $runs.Count]
                    $car = if ($ready -match 'dyno (\d+) r') { $Matches[1] } else { '' }
                    Note "dyno keys: $schedule"
                    Invoke-KeySteps $schedule $car
                }
                else {
                    Invoke-KeyHold $key $DriveSeconds 'throttle'
                    Invoke-KeyHold $brakeKey $BrakeSeconds 'brake'
                }
                Note 'dyno: keys released'
                Add-DynoReady
            }
        }
        elseif (($DriveSeconds -gt 0 -or $HoldMph) -and $Probe) {
            $lines = if (Test-Path $log) { @(Get-Content -LiteralPath $log -ErrorAction SilentlyContinue) } else { @() }
            # Driver.log reaches the disk seconds late, so the throttle goes on as soon as
            # the player drives a new car (chase view) and stays on until DriveSeconds
            # after its cockpit view shows up in the log.
            $driving = @($lines | Where-Object { $_ -match '\[dsfforza\] id (\d+): PLAYER IS DRIVING IT' })
            # Not $views: PowerShell names are case-insensitive, and the [string] -Views parameter would
            # turn the list into a string ("" with no match, so $views[-1] was null: run vm-20261025-070010).
            $cockpitViews = @($lines | Where-Object { $_ -match '\[dsfforza\] id (\d+): cockpit view on' })
            if ($DriveSeconds -gt 0 -and $driving.Count -gt $cockpitViews.Count) {
                if (-not $holdUntil -or $holdUntil -lt [datetime]::MaxValue) { Note "throttle ($DriveKey) on: $($driving[-1].Trim())" }
                $holdUntil = [datetime]::MaxValue
            }
            elseif ($DriveSeconds -gt 0 -and $cockpitViews.Count -gt $cockpits) {
                $cockpits = $cockpitViews.Count
                $holdUntil = (Get-Date).AddSeconds($DriveSeconds)
                Note "throttle ($DriveKey) held $DriveSeconds s more: $($cockpitViews[-1].Trim())"
            }
            if ($holdUntil) {
                if ((Get-Date) -lt $holdUntil) { [DsfGuest]::Hold((Get-GameWindow), $key[0], $key[1], $key[2]) }
                else { [DsfGuest]::Release((Get-GameWindow), $key[0], $key[1], $key[2]); $holdUntil = $null; Note 'throttle released' }
            }
            $speed = @($lines | Where-Object { $_ -match '\[dsfforza\] id \d+: (cockpit t\d+ speed|holding)' })
            $name = "observe-{0:D2}.png" -f (++$shot)
            Save-Shot $name
            for ($b = 2; $b -le $BurstShots; $b++) { Save-Shot ("observe-{0:D2}-b{1}.png" -f $shot, $b) }
            Note ("$name" + $(if ($holdUntil) { ' (throttle)' } else { '' }) + $(if ($speed.Count) { ' ' + $speed[-1].Trim() } else { '' }))
        }
        else {
            # Which car and view each shot shows: the probe's latest "id N: ..." line.
            $name = "observe-{0:D2}.png" -f (++$shot)
            Save-Shot $name
            $probeLine = if ($Probe -and (Test-Path $log)) {
                Select-String -LiteralPath $log -Pattern '\[dsfforza\] id \d+: ' | Select-Object -Last 1 | ForEach-Object { $_.Line.Trim() -replace '^\[Lua\] \[dsfforza\] ', '' }
            }
            Note "$name $probeLine"
        }
        if ($Probe -and (Test-Path $log) -and (Select-String -LiteralPath $log -Pattern '\[dsfforza\] (done|error)' -Quiet)) {
            Start-Sleep -Seconds 5
            Save-Shot 'probe-finished.png'
            break
        }
    }
    }
    finally {
        if ($holdUntil) { [DsfGuest]::Release((Get-GameWindow), $key[0], $key[1], $key[2]) }
    }
    $result.stage = 'finished'
}
catch {
    $result.error = "$($_.Exception.Message) (line $($_.InvocationInfo.ScriptLineNumber): $($_.InvocationInfo.Line.Trim()))"
    Note "error: $($result.error)"
}
finally {
    foreach ($path in (Join-Path $RunPath 'Driver.log'), (Join-Path $RunPath 'dsf_enhanced.log')) {
        if (Test-Path $path) { Copy-Item $path $RunDir }
    }
    if ($Asi) { Get-ChildItem (Join-Path $RunPath 'plugins') -Filter *.log -ErrorAction SilentlyContinue | Copy-Item -Destination $RunDir }
    # The guest's save folder (the host profile's copy, kept until the sandbox restarts): autosave.dat
    # and dsfforza.asi's side file dsfforza-garage.dat (research/asi.md).
    $saves = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ubisoft\Driver San Francisco\reloaded\savegames'
    if ($Asi -and (Test-Path $saves)) {
        $saveOut = New-Item -ItemType Directory -Force -Path (Join-Path $RunDir 'save')
        Get-ChildItem $saves -File -ErrorAction SilentlyContinue | Copy-Item -Destination $saveOut.FullName
    }
    # DriverNGHook's crash handler writes a minidump next to the exe.
    Get-ChildItem $RunPath -Filter *.dmp -ErrorAction SilentlyContinue |
        Where-Object LastWriteTime -gt $started | Copy-Item -Destination $RunDir
    # docdump.lua prints the Lua API as "[dsfapi]<TAB>kind<TAB>library<TAB>name<TAB>docs".
    $api = @(Select-String -LiteralPath (Join-Path $RunDir 'Driver.log') -Pattern '^\[Lua\] \[dsfapi\]\t' -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Line -replace '^\[Lua\] \[dsfapi\]\t', '' })
    if ($api.Count) {
        Set-Content (Join-Path $RunDir 'dsfforza-lua-api.tsv') -Encoding UTF8 -Value (@("kind`tlibrary`tname`tdocs") + $api)
    }
    if (Test-Path (Join-Path $RunDir 'Driver.log')) {
        $result.probeLines = @(Select-String -LiteralPath (Join-Path $RunDir 'Driver.log') -Pattern '\[dsfforza\] ' |
            ForEach-Object { $_.Line.Trim() })
    }
    if (-not $KeepRunning) { Get-Process Driver -ErrorAction SilentlyContinue | Stop-Process -Force; Wait-Process -Name Driver -Timeout 15 -ErrorAction SilentlyContinue }
    $memorySummary = Stop-MemorySampler
    if ($memorySummary) { $result.memory = $memorySummary }
    $result.running = [bool](Get-Process Driver -ErrorAction SilentlyContinue)
    $result.keysSkipped = [DsfGuest]::Skipped   # presses dropped: the game wasn't in the foreground
    $result.lastClick = [DsfGuest]::LastClick
    $result.seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
    $result | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $RunDir 'result.json')
}
