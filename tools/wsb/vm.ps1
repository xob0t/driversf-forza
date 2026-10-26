<#
.SYNOPSIS
Runs the dsfforza test VMs (VMware Workstation) headless for in-game tests.

.DESCRIPTION
-Vm picks the VM: an id (1 = C:\VMs\dsfforza-test, 2 = C:\VMs\dsfforza-test2) or a name; default 1.
The pool is $env:DSF_VMS (';'-separated .vmx paths) or the default VMs that exist (vm-common.ps1).
Status and Stop also take -Vm all.

  Start    power the VM on with no window (vmrun ... nogui) unless it is running,
           wait until the autologon session is up, then set up the session (display
           size, muted audio). A powered-off VM first gets the pool's memory and CPU
           settings ($VmSettings in vm-common.ps1). vmware-vmx runs at normal priority:
           lowering it needs an elevated shell, and the vmx's only lower setting (Idle)
           starves the VM under load.
  Status   print whether the VM runs, the Tools state, whether the session answers and
           the host RAM its vmware-vmx holds.
  Exec     run a guest PowerShell script in the logged-in user's desktop session
           (vmrun runProgramInGuest -interactive), hidden, and wait for it.
           -Script is a guest path (C:\Repo\tools\wsb\guest\run-test.ps1); -HostScript
           a host file that is copied into the guest (C:\dsf) first.
  Share    point the guest's C:\Repo at another checkout (-RepoPath, e.g. a git worktree):
           run-series.ps1 -Vm does for its series, then points it back at the main checkout.
  Stop     shut the guest down (hard power-off if it doesn't go within 2 min), once no
           series uses it (waits for its run mutex).
  Revert   go back to the "clean" snapshot (stopped first, likewise once no series uses it).
  StopIdle power the VM off if no series has used it for -IdleMinutes: run-series.ps1 -Vm
           starts this hidden at the end of each series (with -Stamp; a later series
           changes the stamp and so cancels it). See Get-VmIdleMinutes in vm-common.ps1.

The VMs are built by tools\wsb\vm-build.ps1. Nothing appears on the host desktop: the
VMs have no window, and every vmrun call runs without a console window.

Guest paths (as in the sandbox, so run-test.ps1 runs with its defaults):
  C:\Game      local stock copy of $env:DSF_GAME_DIR (vm-build.ps1 Provision)
  C:\Repo      -> \\vmware-host\Shared Folders\Repo     this repository     (read-only; see Share)
  C:\Output    -> \\vmware-host\Shared Folders\Output   %LOCALAPPDATA%\Temp\dsfforza-wsb (read-write)
  C:\Profile   -> \\vmware-host\Shared Folders\Profile  Documents\Ubisoft\Driver San Francisco (read-only)
  C:\Run       the run folder run-test.ps1 builds
#>
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('Start', 'Status', 'Exec', 'Share', 'Stop', 'Revert', 'StopIdle')]
    [string]$Action,
    [string]$Vm = '1',
    [string]$Script,
    [string]$HostScript,
    [string[]]$Arguments = @(),
    [int]$TimeoutSeconds = 3600,
    [string]$DisplaySize = '1600x900',   # Start: the guest desktop size
    [string]$RepoPath,                   # Share: the checkout C:\Repo shows
    [switch]$NoSession,  # Start: skip the session setup (vm-build.ps1 before provisioning)
    [int]$IdleMinutes = 0,               # StopIdle: wait this long first
    [string]$Stamp                       # StopIdle: the series stamp it was armed with
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'vm-common.ps1')
$targets = if ($Vm -eq 'all') { @(Get-VmPool) } else { @(Get-VmPool -Select $Vm) }
if ($Vm -eq 'all' -and $Action -notin 'Status', 'Stop') { throw "-Vm all works with Status and Stop only" }

function Wait-VmUnused {
    # The VM's run mutexes (as a series holds them), so Stop and Revert never cut a run short;
    # queued behind the series already waiting for it (vm-common.ps1 Enter-VmPool).
    $got = Enter-VmPool -Pool @($VmInfo) -Timeout ([TimeSpan]::FromHours(3)) -Label "vm.ps1 $Action" `
        -Say { param($t) "  ${VmName}: $t" | Out-Host }
    if (-not $got) { throw "$VmName is still in use after 3 hours" }
    return , $got.Mutexes
}

foreach ($target in $targets) {
    Use-Vm $target
    switch ($Action) {
        'Status' {
            if (-not (Test-VmRunning)) { "$VmName (VM $($VmInfo.Id)) not running"; continue }
            $tools = (Invoke-Vmrun @('checkToolsState', $VmVmx) -Quiet).Out.Trim()
            $free = Enter-VmMutexes $VmInfo.RunMutexes
            Exit-VmMutexes $free
            "$VmName (VM $($VmInfo.Id)) running ($VmVmx); tools: $tools; session answers: $(Test-VmSession); " +
                "series: $(if ($free) { 'none' } else { 'running' }); $(Get-VmPriority)"
        }
        'Stop' {
            $held = Wait-VmUnused
            try {
                $lifecycle = Enter-VmLifecycle
                try { "${VmName}: $(Stop-Vm)" } finally { $lifecycle.ReleaseMutex() }
            } finally { Exit-VmMutexes $held }
        }
        'Revert' {
            $held = Wait-VmUnused
            try {
                $lifecycle = Enter-VmLifecycle
                try {
                    Stop-Vm | Out-Null
                    Invoke-Vmrun @('revertToSnapshot', $VmVmx, 'clean') | Out-Null
                    "${VmName}: reverted to snapshot clean"
                } finally { $lifecycle.ReleaseMutex() }
            } finally { Exit-VmMutexes $held }
        }
        'StopIdle' {
            $stateDir = Join-Path $VmOutputHost "vm\$VmName"
            $stampFile = Join-Path $stateDir 'last-series.txt'
            $log = Join-Path $stateDir 'idle-stop.log'
            New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
            function Say([string]$text) { Add-Content -LiteralPath $log "$(Get-Date -Format s) [$PID] $text" }
            $current = { "$(Get-Content -LiteralPath $stampFile -ErrorAction SilentlyContinue)".Trim() }
            if ($IdleMinutes -gt 0) { Start-Sleep -Seconds (60 * $IdleMinutes) }
            if ($Stamp -and (& $current) -ne $Stamp) { return }   # a later series re-armed it
            if (Test-VmQueued $VmName) { Say 'a series is queued for it; left running'; return }   # that series arms its own
            $held = Enter-VmMutexes $VmInfo.RunMutexes
            if (-not $held) { Say 'in use; left running'; return }   # that series arms its own
            try {
                if ($Stamp -and (& $current) -ne $Stamp) { return }
                if (-not (Test-VmRunning)) { return }
                # Series from older checkouts don't write the stamp (VM 1 only); their run folders
                # (runs\vm-<stamp>) show they used it.
                $patterns = @("vm$($VmInfo.Id)-*") + @(if ($VmInfo.Id -eq '1') { 'vm-*' })
                $lastRun = Get-ChildItem -LiteralPath (Join-Path $VmOutputHost 'runs') -Directory -ErrorAction SilentlyContinue |
                    Where-Object { $n = $_.Name; @($patterns | Where-Object { $n -like $_ }).Count } |
                    Sort-Object LastWriteTime | Select-Object -Last 1
                if ($lastRun -and $lastRun.LastWriteTime -gt (Get-Date).AddMinutes(-[math]::Max(1, $IdleMinutes))) {
                    Say "used by $($lastRun.Name) at $($lastRun.LastWriteTime.ToString('s')); left running"; return
                }
                $lifecycle = Enter-VmLifecycle
                try { Say "idle for $IdleMinutes min: $(Stop-Vm)" } finally { $lifecycle.ReleaseMutex() }
            } finally { Exit-VmMutexes $held }
        }
        'Start' {
            $lifecycle = Enter-VmLifecycle
            try {
                Start-VmHeadless
                Copy-HostDlls   # C:\Output\HostDll for run-test.ps1 (made by the sandbox's Start before)
                if (-not $NoSession) {
                    # This checkout's copy: C:\Repo still shows whichever checkout the last series left.
                    Invoke-Vmrun @('copyFileFromHostToGuest', $VmVmx, (Join-Path $PSScriptRoot 'guest\setup-vm.ps1'), 'C:\dsf\setup-vm.ps1') -Guest | Out-Null
                    Invoke-VmScript -GuestScript 'C:\dsf\setup-vm.ps1' `
                        -GuestArguments @('-Phase', 'Session', '-DisplaySize', $DisplaySize, '-Tag', $VmName) -Timeout 300 | Out-Null
                }
                "$VmName ready; host output: $VmOutputHost"
            } finally { $lifecycle.ReleaseMutex() }
        }
        'Share' {
            if (-not (Test-VmRunning)) { throw "$VmName is not running; run Start first" }
            if (-not $RepoPath) { throw '-RepoPath is required' }
            Set-VmRepoShare $RepoPath
            "${VmName}: C:\Repo -> $RepoPath"
        }
        'Exec' {
            if (-not (Test-VmRunning)) { throw "$VmName is not running; run Start first" }
            if ($HostScript) {
                $guestCopy = "C:\dsf\$([IO.Path]::GetFileName($HostScript))"
                Invoke-Vmrun @('copyFileFromHostToGuest', $VmVmx, (Resolve-Path $HostScript).Path, $guestCopy) -Guest | Out-Null
                $Script = $guestCopy
            }
            if (-not $Script) { throw '-Script (a guest path such as C:\Repo\tools\wsb\guest\run-test.ps1) or -HostScript is required' }
            Invoke-VmScript -GuestScript $Script -GuestArguments $Arguments -Timeout $TimeoutSeconds
        }
    }
}
