<#
.SYNOPSIS
Runs one persistent Windows Sandbox for in-game tests without stealing focus.

.DESCRIPTION
  Start    reuse the running dsfforza sandbox, or create one. The sandbox window
           is opened without activation, pushed behind other windows, and the
           previously focused window is given focus back. Then the guest is
           configured (firewall as SYSTEM, game profile as the user).
  Status   print the sandbox ID and whether the guest answers.
  Exec     run a guest PowerShell script (path inside the sandbox) as the
           logged-in user; output lands in C:\Output (host: see $OutputHost).
  Stop     shut the sandbox down.

Start and Stop take a named mutex, so concurrent callers queue instead of
failing; tools\wsb
un-series.ps1 also holds a run mutex for its whole run and
starts the sandbox itself when none is running.

Game launches all happen inside the guest, so after Start nothing new appears
on the host desktop.

Mappings (guest path <- host path):
  C:\Game      <- D:\Games\Driver San Francisco              (read-only)
  C:\Profile   <- Documents\Ubisoft\Driver San Francisco     (read-only)
  C:\Repo      <- this repository                            (read-only)
  C:\Output    <- %LOCALAPPDATA%\Temp\dsfforza-wsb           (read-write)
#>
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('Start', 'Status', 'Exec', 'Stop')]
    [string]$Action,
    [string]$Script,
    [string[]]$Arguments = @(),
    [int]$MemoryMB = 8192,
    [int]$Processors = 6,
    [switch]$NoVGpu   # software rendering (WARP) instead of the host GPU
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$OutputHost = Join-Path $env:LOCALAPPDATA 'Temp\dsfforza-wsb'
$stateFile = Join-Path $OutputHost 'sandbox.json'
$gameHost = 'D:\Games\Driver San Francisco'
$profileHost = Join-Path $env:USERPROFILE 'Documents\Ubisoft\Driver San Francisco'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class DsfFocus {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr window, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint attach, uint attachTo, bool value);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();

    // Move a window to the bottom of the z-order without activating it.
    public static void SendToBack(IntPtr window) {
        SetWindowPos(window, new IntPtr(1) /* HWND_BOTTOM */, 0, 0, 0, 0,
            0x0002 | 0x0001 | 0x0010 /* NOMOVE | NOSIZE | NOACTIVATE */);
    }

    // Give focus back to a window, borrowing the current foreground thread's
    // input state so the foreground lock allows it.
    public static bool Restore(IntPtr window) {
        if (window == IntPtr.Zero) return false;
        uint pid;
        uint foregroundThread = GetWindowThreadProcessId(GetForegroundWindow(), out pid);
        uint self = GetCurrentThreadId();
        bool attached = foregroundThread != 0 && foregroundThread != self && AttachThreadInput(self, foregroundThread, true);
        try { return SetForegroundWindow(window); }
        finally { if (attached) AttachThreadInput(self, foregroundThread, false); }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct StartupInfo {
        public int cb; public string reserved, desktop, title;
        public int x, y, xSize, ySize, xChars, yChars, fill, flags;
        public short showWindow, reserved2; public IntPtr reserved3, stdIn, stdOut, stdErr;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo { public IntPtr process, thread; public int pid, tid; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcess(string app, string commandLine, IntPtr pa, IntPtr ta, bool inherit,
        uint flags, IntPtr env, string dir, ref StartupInfo si, out ProcessInfo pi);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);

    // Start a process whose first window should appear without activation.
    public static int StartNoActivate(string commandLine) {
        var si = new StartupInfo();
        si.cb = Marshal.SizeOf(typeof(StartupInfo));
        si.flags = 0x1;          // STARTF_USESHOWWINDOW
        si.showWindow = 4;       // SW_SHOWNOACTIVATE
        ProcessInfo pi;
        if (!CreateProcess(null, commandLine, IntPtr.Zero, IntPtr.Zero, false, 0x08000000 /* CREATE_NO_WINDOW */,
                IntPtr.Zero, null, ref si, out pi))
            throw new System.ComponentModel.Win32Exception();
        CloseHandle(pi.thread); CloseHandle(pi.process);
        return pi.pid;
    }
}
'@

# Start and Stop are serialised across processes: Windows runs one sandbox at a
# time, so a second Start while another is creating it fails. A named mutex is
# released by Windows if its owner dies, so it can't go stale.
function Enter-Lifecycle {
    $mutex = New-Object System.Threading.Mutex($false, 'Local\dsfforza-wsb-lifecycle')
    try { if (-not $mutex.WaitOne([TimeSpan]::FromMinutes(10))) { throw 'timed out waiting for another sandbox Start/Stop' } }
    catch [System.Threading.AbandonedMutexException] { }
    return $mutex
}

function Get-RunningIds { @((wsb list --raw | ConvertFrom-Json).WindowsSandboxEnvironments | ForEach-Object Id) }

function Get-ActiveId {
    if (-not (Test-Path -LiteralPath $stateFile)) { return $null }
    $id = (Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json).id
    if ($id -in @(Get-RunningIds)) { return $id }
    return $null
}

function Test-Guest([string]$Id) {
    # PowerShell 5.1 turns native stderr into a terminating error under 'Stop'.
    $ErrorActionPreference = 'Continue'
    wsb exec --id $Id --command 'cmd.exe /c exit 0' --run-as ExistingLogin --raw 2>&1 | Out-Null
    return $LASTEXITCODE -eq 0
}

function Invoke-Guest([string]$Id, [string]$GuestScript, [string[]]$GuestArguments, [string]$RunAs = 'ExistingLogin') {
    # SYSTEM can't read the read-only mappings, so guest scripts under
    # C:\Repo\tools\wsb\guest run from a fresh copy in C:\Output\guest.
    $prefix = 'C:\Repo\tools\wsb\guest\'
    if ($GuestScript.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        $guestCopy = Join-Path $OutputHost 'guest'
        New-Item -ItemType Directory -Force -Path $guestCopy | Out-Null
        Copy-Item (Join-Path $PSScriptRoot 'guest\*.ps1') $guestCopy -Force
        $GuestScript = 'C:\Output\guest\' + $GuestScript.Substring($prefix.Length)
    }
    $command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GuestScript $($GuestArguments -join ' ')"
    $raw = wsb exec --id $Id --command $command --run-as $RunAs --raw
    # With --raw, wsb exits 0 and reports the guest's exit code in JSON.
    $exit = try { ($raw | ConvertFrom-Json).ExitCode } catch { $LASTEXITCODE }
    if ($LASTEXITCODE -ne 0 -or $exit -ne 0) { throw "guest command failed (exit $exit): $command" }
}

function Get-SandboxWindows {
    @(Get-Process | Where-Object { $_.ProcessName -like 'WindowsSandbox*' -and $_.MainWindowHandle -ne 0 } |
        ForEach-Object MainWindowHandle)
}

function New-Config {
    $mapped = @(
        @($gameHost, 'C:\Game', 'true'),
        @($profileHost, 'C:\Profile', 'true'),
        @($repo, 'C:\Repo', 'true'),
        @($OutputHost, 'C:\Output', 'false')
    ) | ForEach-Object {
        "<MappedFolder><HostFolder>$($_[0])</HostFolder><SandboxFolder>$($_[1])</SandboxFolder><ReadOnly>$($_[2])</ReadOnly></MappedFolder>"
    }
    @"
<Configuration>
  <VGpu>$(if ($NoVGpu) { 'Disable' } else { 'Enable' })</VGpu>
  <Networking>Disable</Networking>
  <MemoryInMB>$MemoryMB</MemoryInMB>
  <ProcessorCount>$Processors</ProcessorCount>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <PrinterRedirection>Disable</PrinterRedirection>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <MappedFolders>$($mapped -join '')</MappedFolders>
</Configuration>
"@
}

switch ($Action) {
    'Status' {
        $id = Get-ActiveId
        if (-not $id) { 'no dsfforza sandbox running'; return }
        "sandbox $id; guest answers: $(Test-Guest $id)"
    }
    'Stop' {
        $lifecycle = Enter-Lifecycle
        $id = Get-ActiveId
        if ($id) { wsb stop --id $id --raw | Out-Null; "stopped $id" } else { 'nothing to stop' }
        Remove-Item -LiteralPath $stateFile -ErrorAction SilentlyContinue
        $lifecycle.ReleaseMutex()
    }
    'Exec' {
        $id = Get-ActiveId
        if (-not $id) { throw 'no dsfforza sandbox running; run Start first' }
        if (-not $Script) { throw '-Script is required (a guest path such as C:\Repo\tools\wsb\guest\run-test.ps1)' }
        Invoke-Guest $id $Script $Arguments
    }
    'Start' {
        $lifecycle = Enter-Lifecycle
        try {
        $id = Get-ActiveId
        if ($id -and (Test-Guest $id)) {
            "reusing sandbox $id"
            # Finish any setup a previous Start didn't get to.
            if (-not (Test-Path (Join-Path $OutputHost 'setup-system.json'))) {
                Invoke-Guest $id 'C:\Repo\tools\wsb\guest\setup-system.ps1' @() 'System'
            }
            if (-not (Test-Path (Join-Path $OutputHost 'setup-user.json'))) {
                Invoke-Guest $id 'C:\Repo\tools\wsb\guest\setup-user.ps1' @()
            }
            return
        }

        if ($id) {
            # Running but the guest doesn't answer (crashed or half-started): replace it.
            wsb stop --id $id --raw 2>&1 | Out-Null
            Remove-Item -LiteralPath $stateFile -ErrorAction SilentlyContinue
            $id = $null
        }
        Remove-Item -LiteralPath (Join-Path $OutputHost 'setup-system.json'), (Join-Path $OutputHost 'setup-user.json') -ErrorAction SilentlyContinue

        New-Item -ItemType Directory -Force -Path $OutputHost | Out-Null
        # 32-bit runtime pieces the sandbox image lacks: DirectX 9 extras for
        # the game, the VC++ 2015+ runtime for DriverNGHook.asi.
        $dlls = Join-Path $OutputHost 'HostDll'
        New-Item -ItemType Directory -Force -Path $dlls | Out-Null
        Copy-Item C:\Windows\SysWOW64\d3dx9_36.dll, C:\Windows\SysWOW64\xinput1_3.dll,
            C:\Windows\SysWOW64\msvcp140.dll, C:\Windows\SysWOW64\vcruntime140.dll -Destination $dlls -Force

        if (-not $id) {
            $before = @(Get-RunningIds)
            $result = wsb start --config (New-Config) --raw | ConvertFrom-Json
            $id = $result.Id
            if (-not $id) { $id = @(Get-RunningIds | Where-Object { $_ -notin $before })[0] }
            if (-not $id) { throw 'wsb start did not report a sandbox ID' }
            @{ id = $id; startedUtc = (Get-Date).ToUniversalTime().ToString('o') } |
                ConvertTo-Json | Set-Content -LiteralPath $stateFile
        }

        # A user session only exists once a client connects, so open one quietly.
        $previous = [DsfFocus]::GetForegroundWindow()
        $existing = Get-SandboxWindows
        [DsfFocus]::StartNoActivate("wsb.exe connect --id $id") | Out-Null
        $deadline = (Get-Date).AddSeconds(60)
        $window = $null
        while (-not $window -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 200
            $window = @(Get-SandboxWindows | Where-Object { $_ -notin $existing })[0]
        }
        if ($window) {
            [DsfFocus]::SendToBack($window)
            if ([DsfFocus]::GetForegroundWindow() -ne $previous) { [DsfFocus]::Restore($previous) | Out-Null }
        }

        $deadline = (Get-Date).AddSeconds(120)
        while (-not (Test-Guest $id)) {
            if ((Get-Date) -gt $deadline) { throw "sandbox $id has no user session after 120 s" }
            Start-Sleep -Seconds 2
        }
        # Re-assert in case the session logon raised the window again.
        if ($window) {
            [DsfFocus]::SendToBack($window)
            if ([DsfFocus]::GetForegroundWindow() -ne $previous) { [DsfFocus]::Restore($previous) | Out-Null }
        }

        Invoke-Guest $id 'C:\Repo\tools\wsb\guest\setup-system.ps1' @() 'System'
        Invoke-Guest $id 'C:\Repo\tools\wsb\guest\setup-user.ps1' @()
        "sandbox $id ready; host output: $OutputHost"
        } finally { $lifecycle.ReleaseMutex() }
    }
}
