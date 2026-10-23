# Shared by tools\wsb\vm.ps1, vm-build.ps1 and run-series.ps1 -Vm (dot-sourced).
# The test VM's location, its guest account, and vmrun helpers that never open a window.

$VmDir = 'C:\VMs\dsfforza-test'
$VmVmx = Join-Path $VmDir 'dsfforza-test.vmx'
$VmUser = 'dsf'
$VmPassword = 'dsfforza'          # local account of an offline test VM (tools\wsb\vm\autounattend.xml)
$VmOutputHost = Join-Path $env:LOCALAPPDATA 'Temp\dsfforza-wsb'

function Get-VmwareDir {
    foreach ($key in 'HKLM:\SOFTWARE\WOW6432Node\VMware, Inc.\VMware Workstation', 'HKLM:\SOFTWARE\VMware, Inc.\VMware Workstation') {
        $path = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallPath
        if ($path -and (Test-Path (Join-Path $path 'vmrun.exe'))) { return $path.TrimEnd('\') }
    }
    foreach ($path in "${env:ProgramFiles(x86)}\VMware\VMware Workstation", "$env:ProgramFiles\VMware\VMware Workstation") {
        if (Test-Path (Join-Path $path 'vmrun.exe')) { return $path }
    }
    throw 'VMware Workstation (vmrun.exe) is not installed'
}

function Format-VmrunArgument([string]$a) {
    # MSVCRT quoting, which vmrun's argv parsing undoes.
    if ($a -ne '' -and $a -notmatch '[\s"]') { return $a }
    '"' + (($a -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

function Invoke-Vmrun {
    # vmrun without a console window; output captured. -Guest adds the guest login,
    # -Quiet returns failures instead of throwing.
    param([Parameter(Mandatory, Position = 0)] [string[]]$VmrunArgs, [switch]$Guest, [switch]$Quiet, [int]$Timeout = 600)
    $all = @('-T', 'ws')
    if ($Guest) { $all += '-gu', $VmUser, '-gp', $VmPassword }
    $all += $VmrunArgs
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path (Get-VmwareDir) 'vmrun.exe'
    $info.Arguments = ($all | ForEach-Object { Format-VmrunArgument "$_" }) -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($info)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($Timeout * 1000)) {
        try { $process.Kill() } catch { }
        $result = [pscustomobject]@{ Exit = -1; Out = "vmrun timed out after $Timeout s: $($VmrunArgs -join ' ')" }
    } else {
        $process.WaitForExit()
        $result = [pscustomobject]@{ Exit = $process.ExitCode; Out = ($stdout.Result + $stderr.Result).Trim() }
    }
    if ($result.Exit -ne 0 -and -not $Quiet) {
        throw "vmrun $($VmrunArgs[0]) failed (exit $($result.Exit)): $($result.Out)"
    }
    return $result
}

function Test-VmRunning {
    $list = (Invoke-Vmrun @('list') -Quiet).Out -split "`r?`n"
    return [bool]($list | Where-Object { $_.Trim() -ieq $VmVmx })
}

function Test-VmSession {
    # Tools running and the autologon desktop up (explorer and the user-session vmtoolsd).
    if ((Invoke-Vmrun @('checkToolsState', $VmVmx) -Quiet -Timeout 60).Out -notmatch 'running') { return $false }
    $procs = Invoke-Vmrun @('listProcessesInGuest', $VmVmx) -Guest -Quiet -Timeout 60
    return $procs.Exit -eq 0 -and $procs.Out -match '(?i)explorer\.exe' -and $procs.Out -match '(?i)vmtoolsd\.exe"?\s+-n\s+vmusr'
}

function Enter-VmLifecycle {
    # Start/Stop/Revert one at a time (released by Windows if the owner dies).
    $mutex = New-Object System.Threading.Mutex($false, 'Local\dsfforza-vm-lifecycle')
    try { if (-not $mutex.WaitOne([TimeSpan]::FromMinutes(15))) { throw 'timed out waiting for another VM Start/Stop' } }
    catch [System.Threading.AbandonedMutexException] { }
    return $mutex
}

function Get-VmProcesses {
    # vmware-vmx is started by the vmware-authd service; a normal process can't read its
    # command line, so when that is hidden every vmware-vmx counts (this is the only VM).
    @(Get-CimInstance Win32_Process -Filter "Name='vmware-vmx.exe'" |
        Where-Object { -not $_.CommandLine -or $_.CommandLine -like "*$VmVmx*" })
}

function Set-VmPriority([string]$Priority = 'BelowNormal') {
    # The VM yields to whatever the user runs. The vmx asks for this itself
    # (priority.ungrabbed = "low", vm-build.ps1); setting it from here needs admin rights
    # (vmware-authd's token), so a refusal is expected and ignored.
    foreach ($p in Get-VmProcesses) {
        try { (Get-Process -Id $p.ProcessId).PriorityClass = $Priority } catch { }
    }
}

function Get-VmPriority {
    # WMI base priority: 8 normal, 6 below normal, 4 idle.
    (Get-VmProcesses | ForEach-Object { "vmware-vmx $($_.ProcessId) base priority $($_.Priority)" }) -join '; '
}

function Wait-VmSession([int]$Seconds = 600) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while (-not (Test-VmSession)) {
        if ((Get-Date) -gt $deadline) { throw "the VM's desktop session is not up after $Seconds s" }
        Start-Sleep -Seconds 5
    }
}

function Start-VmHeadless([string]$Priority = 'BelowNormal') {
    if (-not (Test-VmRunning)) { Invoke-Vmrun @('start', $VmVmx, 'nogui') -Timeout 600 | Out-Null }
    Set-VmPriority $Priority
    Wait-VmSession
}

function Stop-Vm {
    if (-not (Test-VmRunning)) { return 'not running' }
    $soft = Invoke-Vmrun @('stop', $VmVmx, 'soft') -Quiet -Timeout 180
    if (Test-VmRunning) { Invoke-Vmrun @('stop', $VmVmx, 'hard') -Quiet -Timeout 120 | Out-Null; return "stopped (hard; soft said: $($soft.Out))" }
    return 'stopped'
}

function Invoke-VmScript([string]$GuestScript, [string[]]$GuestArguments = @(), [int]$Timeout = 3600) {
    # The command line goes into a wrapper script copied into the guest, so no quoting passes
    # through vmrun; the wrapper starts the real script hidden in the user's desktop session
    # and hands back its exit code. Arguments are joined like sandbox.ps1 does for wsb exec.
    $commandLine = "-NoProfile -ExecutionPolicy Bypass -File $GuestScript $($GuestArguments -join ' ')"
    $id = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $wrapper = Join-Path $env:TEMP "dsfforza-vm-exec-$id.ps1"
    $guestWrapper = "C:\dsf\exec-$id.ps1"
    @(
        "`$p = Start-Process powershell.exe -ArgumentList '$($commandLine -replace "'", "''")' -WindowStyle Hidden -Wait -PassThru"
        'exit $p.ExitCode'
    ) | Set-Content -LiteralPath $wrapper -Encoding UTF8
    try {
        Invoke-Vmrun @('copyFileFromHostToGuest', $VmVmx, $wrapper, $guestWrapper) -Guest | Out-Null
        $r = Invoke-Vmrun @('runProgramInGuest', $VmVmx, '-interactive', 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
            '-WindowStyle', 'Hidden', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $guestWrapper) -Guest -Quiet -Timeout $Timeout
        Invoke-Vmrun @('deleteFileInGuest', $VmVmx, $guestWrapper) -Guest -Quiet -Timeout 60 | Out-Null
        if ($r.Exit -ne 0 -or $r.Out -match 'non-zero exit code') { throw "guest command failed ($($r.Out)): $commandLine" }
        return $r.Out
    } finally { Remove-Item -LiteralPath $wrapper -ErrorAction SilentlyContinue }
}
