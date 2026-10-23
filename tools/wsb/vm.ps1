<#
.SYNOPSIS
Runs the dsfforza test VM (VMware Workstation) headless for in-game tests.

.DESCRIPTION
  Start    power the VM on with no window (vmrun ... nogui) unless it is running,
           drop vmware-vmx to BelowNormal priority, wait until the autologon session
           is up, then set up the session (display size, muted audio).
  Status   print whether the VM runs, the Tools state and whether the session answers.
  Exec     run a guest PowerShell script in the logged-in user's desktop session
           (vmrun runProgramInGuest -interactive), hidden, and wait for it.
           -Script is a guest path (C:\Repo\tools\wsb\guest\run-test.ps1); -HostScript
           a host file that is copied into the guest (C:\dsf) first.
  Stop     shut the guest down (hard power-off if it doesn't go within 2 min).
  Revert   go back to the "clean" snapshot (the VM is stopped first).

The VM is built by tools\wsb\vm-build.ps1. Nothing appears on the host desktop: the
VM has no window, and every vmrun call runs without a console window.

Guest paths (as in the sandbox, so run-test.ps1 runs with its defaults):
  C:\Game      local stock copy of D:\Games\Driver San Francisco (vm-build.ps1 Provision)
  C:\Repo      -> \\vmware-host\Shared Folders\Repo     this repository     (read-only)
  C:\Output    -> \\vmware-host\Shared Folders\Output   %LOCALAPPDATA%\Temp\dsfforza-wsb (read-write)
  C:\Profile   -> \\vmware-host\Shared Folders\Profile  Documents\Ubisoft\Driver San Francisco (read-only)
  C:\Run       the run folder run-test.ps1 builds
#>
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('Start', 'Status', 'Exec', 'Stop', 'Revert')]
    [string]$Action,
    [string]$Script,
    [string]$HostScript,
    [string[]]$Arguments = @(),
    [int]$TimeoutSeconds = 3600,
    [string]$DisplaySize = '1600x900',   # Start: the guest desktop size
    [switch]$NoSession   # Start: skip the session setup (vm-build.ps1 before provisioning)
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'vm-common.ps1')

switch ($Action) {
    'Status' {
        if (-not (Test-VmRunning)) { 'dsfforza VM not running'; return }
        $tools = (Invoke-Vmrun checkToolsState $VmVmx -Quiet).Out.Trim()
        "VM running ($VmVmx); tools: $tools; session answers: $(Test-VmSession)"
    }
    'Stop' {
        $lifecycle = Enter-VmLifecycle
        try { Stop-Vm } finally { $lifecycle.ReleaseMutex() }
    }
    'Revert' {
        $lifecycle = Enter-VmLifecycle
        try {
            Stop-Vm | Out-Null
            Invoke-Vmrun revertToSnapshot $VmVmx clean | Out-Null
            'reverted to snapshot clean'
        } finally { $lifecycle.ReleaseMutex() }
    }
    'Start' {
        $lifecycle = Enter-VmLifecycle
        try {
            Start-VmHeadless
            if (-not $NoSession) {
                Invoke-VmScript -GuestScript 'C:\Repo\tools\wsb\guest\setup-vm.ps1' `
                    -GuestArguments @('-Phase', 'Session', '-DisplaySize', $DisplaySize) -Timeout 300 | Out-Null
            }
            "VM ready; host output: $VmOutputHost"
        } finally { $lifecycle.ReleaseMutex() }
    }
    'Exec' {
        if (-not (Test-VmRunning)) { throw 'the dsfforza VM is not running; run Start first' }
        if ($HostScript) {
            $guestCopy = "C:\dsf\$([IO.Path]::GetFileName($HostScript))"
            Invoke-Vmrun -Guest copyFileFromHostToGuest $VmVmx (Resolve-Path $HostScript).Path $guestCopy | Out-Null
            $Script = $guestCopy
        }
        if (-not $Script) { throw '-Script (a guest path such as C:\Repo\tools\wsb\guest\run-test.ps1) or -HostScript is required' }
        Invoke-VmScript -GuestScript $Script -GuestArguments $Arguments -Timeout $TimeoutSeconds
    }
}
