# Shared by tools\wsb\vm.ps1, vm-build.ps1 and run-series.ps1 -Vm (dot-sourced; sandbox.ps1 uses
# Copy-HostDlls). The pool of test VMs, their guest account, and vmrun helpers that never open a window.
#
# The pool: $env:DSF_VMS, ';'-separated .vmx paths, else those of $VmDefaultPool that exist.
# A VM's id comes from its file name: dsfforza-test is 1, dsfforza-testN is N, any other name is
# itself. Per VM: run mutexes (VM 1 also holds the old single-VM name Local\dsfforza-vm-run, so
# series from older checkouts and new ones exclude each other), a lifecycle mutex for
# Start/Stop/Revert, run folders runs\vm<id>-<stamp>, and C:\Output\vm\<name>\ for setup logs.
# Use-Vm selects the VM the functions below work on ($VmInfo, $VmVmx, $VmDir, $VmName).

$VmDefaultPool = 'C:\VMs\dsfforza-test\dsfforza-test.vmx', 'C:\VMs\dsfforza-test2\dsfforza-test2.vmx'
$VmUser = 'dsf'
$VmPassword = 'dsfforza'          # local account of an offline test VM (tools\wsb\vm\autounattend.xml)
$VmOutputHost = Join-Path $env:LOCALAPPDATA 'Temp\dsfforza-wsb'
# Every pool VM runs with these (research/in-game-tests.md, "VM pool"). Start writes them into a
# powered-off VM's .vmx when they differ, so a reverted snapshot or a new clone gets them too.
$VmSettings = [ordered]@{
    'memsize' = '4096'
    'svga.graphicsMemoryKB' = '524288'
    'numvcpus' = '4'
    'cpuid.coresPerSocket' = '4'
}

function Get-VmInfo([string]$Vmx) {
    $name = [IO.Path]::GetFileNameWithoutExtension($Vmx)
    $id = if ($name -match '^dsfforza-test(\d*)$') { if ($Matches[1]) { $Matches[1] } else { '1' } } else { $name }
    $run = @("Local\dsfforza-vm-run-$id")
    $lifecycle = "Local\dsfforza-vm-lifecycle-$id"
    if ($id -eq '1') { $run = @('Local\dsfforza-vm-run') + $run; $lifecycle = 'Local\dsfforza-vm-lifecycle' }   # the old names
    [pscustomobject]@{ Id = $id; Name = $name; Vmx = $Vmx; Dir = (Split-Path $Vmx); RunMutexes = $run; LifecycleMutex = $lifecycle }
}

function Get-VmPool {
    # -Select: an id ('2') or name ('dsfforza-test2'); one VM of the pool, or of the default list.
    param([string]$Select)
    $paths = if ($env:DSF_VMS) { @($env:DSF_VMS -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
             else { @($VmDefaultPool | Where-Object { Test-Path -LiteralPath $_ }) }
    $pool = @($paths | ForEach-Object { Get-VmInfo $_ })
    if (-not $Select) { return $pool }
    $found = @(@($pool) + @($VmDefaultPool | ForEach-Object { Get-VmInfo $_ }) | Where-Object { $_.Id -eq $Select -or $_.Name -eq $Select })
    if (-not $found) { throw "no test VM '$Select' (pool: $(($pool | ForEach-Object { "$($_.Id)=$($_.Vmx)" }) -join ', '); set DSF_VMS)" }
    return $found[0]
}

function Use-Vm($Vm) {
    # Point the helpers below at one VM (a Get-VmPool entry, an id or a name).
    if ($Vm -is [string]) { $Vm = Get-VmPool -Select $Vm }
    $script:VmInfo = $Vm
    $script:VmVmx = $Vm.Vmx; $script:VmDir = $Vm.Dir; $script:VmName = $Vm.Name
}
Use-Vm (Get-VmInfo $VmDefaultPool[0])

function Enter-VmMutexes([string[]]$Names, [int]$WaitMs = 0) {
    # All of a VM's run mutexes, or none ($null). Windows releases a mutex whose owner dies.
    $held = @()
    foreach ($name in $Names) {
        $m = New-Object System.Threading.Mutex($false, $name)
        $ok = $false
        try { $ok = $m.WaitOne($(if ($held.Count) { 0 } else { $WaitMs })) } catch [System.Threading.AbandonedMutexException] { $ok = $true }
        if (-not $ok) { Exit-VmMutexes $held; $m.Dispose(); return $null }
        $held += $m
    }
    return , $held
}

function Exit-VmMutexes($Held) {
    foreach ($m in @($Held)) { if ($m) { try { $m.ReleaseMutex() } catch { }; $m.Dispose() } }
}

function Enter-VmPool {
    # Take a free VM of $Pool: running ones first (no boot, no extra RAM), then in pool order, each
    # tried at once; when all are busy, wait for whichever comes free first. Returns
    # @{ Vm; Mutexes } or $null after $Timeout. -Say gets the 'waiting' line.
    param([object[]]$Pool, [TimeSpan]$Timeout, [scriptblock]$Say = { param($t) $t })
    $running = (Invoke-Vmrun @('list') -Quiet).Out -split "`r?`n" | ForEach-Object { $_.Trim() }
    $order = @($Pool | Where-Object { $running -contains $_.Vmx }) + @($Pool | Where-Object { $running -notcontains $_.Vmx })
    foreach ($vm in $order) {
        $held = Enter-VmMutexes $vm.RunMutexes
        if ($held) { return @{ Vm = $vm; Mutexes = $held } }
    }
    & $Say "waiting for a free VM ($(($Pool | ForEach-Object Name) -join ', ') busy)..."
    $deadline = (Get-Date) + $Timeout
    while ((Get-Date) -lt $deadline) {
        $first = @($Pool | ForEach-Object { New-Object System.Threading.Mutex($false, $_.RunMutexes[0]) })
        $left = [math]::Max(0, [int]($deadline - (Get-Date)).TotalMilliseconds)
        try { $index = [System.Threading.WaitHandle]::WaitAny([System.Threading.WaitHandle[]]$first, $left) }
        catch [System.Threading.AbandonedMutexException] { $index = $_.Exception.MutexIndex }
        if ($index -eq [System.Threading.WaitHandle]::WaitTimeout) { $first | ForEach-Object { $_.Dispose() }; break }
        $vm = $Pool[$index]
        $held = @($first[$index])
        for ($i = 0; $i -lt $first.Count; $i++) { if ($i -ne $index) { $first[$i].Dispose() } }
        # VM 1's second name: only taken by holders of the first, so normally free at once.
        $rest = if ($vm.RunMutexes.Count -gt 1) { Enter-VmMutexes $vm.RunMutexes[1..($vm.RunMutexes.Count - 1)] }
        if ($vm.RunMutexes.Count -eq 1 -or $rest) { return @{ Vm = $vm; Mutexes = @($held + @($rest | Where-Object { $_ })) } }
        Exit-VmMutexes $held
        Start-Sleep -Seconds 1
    }
    return $null
}

function Copy-HostDlls {
    # 32-bit runtime pieces the guests (sandbox and VM) lack; run-test.ps1 copies them next to the
    # run copy of Driver.exe from C:\Output\HostDll: DirectX 9 extras for the game, the VC++ 2015+
    # runtime for DriverNGHook.asi. Unchanged files are left alone: another VM's run may be reading them.
    $dlls = Join-Path $VmOutputHost 'HostDll'
    New-Item -ItemType Directory -Force -Path $dlls | Out-Null
    foreach ($file in Get-Item C:\Windows\SysWOW64\d3dx9_36.dll, C:\Windows\SysWOW64\xinput1_3.dll,
        C:\Windows\SysWOW64\msvcp140.dll, C:\Windows\SysWOW64\vcruntime140.dll) {
        $copy = Get-Item -LiteralPath (Join-Path $dlls $file.Name) -ErrorAction SilentlyContinue
        if ($copy -and $copy.Length -eq $file.Length -and $copy.LastWriteTimeUtc -eq $file.LastWriteTimeUtc) { continue }
        Copy-Item -LiteralPath $file.FullName -Destination $dlls -Force
    }
}

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
    # Tools answering and the autologon desktop up: explorer and a vmtoolsd owned by the user
    # (the session instance runProgramInGuest -interactive needs). Workstation 26's
    # checkToolsState says "installed" even while Tools runs, and the process list shows no
    # command-line arguments, so both are matched loosely.
    if ((Invoke-Vmrun @('checkToolsState', $VmVmx) -Quiet -Timeout 60).Out -notmatch 'running|installed') { return $false }
    $procs = Invoke-Vmrun @('listProcessesInGuest', $VmVmx) -Guest -Quiet -Timeout 60
    $user = [regex]::Escape($VmUser)
    return $procs.Exit -eq 0 -and $procs.Out -match "(?im)owner=[^,]*\\$user, cmd=.*explorer\.exe" -and
        $procs.Out -match "(?im)owner=[^,]*\\$user, cmd=.*vmtoolsd\.exe"
}

function Enter-VmLifecycle {
    # Start/Stop/Revert of the selected VM one at a time (released by Windows if the owner dies).
    $mutex = New-Object System.Threading.Mutex($false, $VmInfo.LifecycleMutex)
    try { if (-not $mutex.WaitOne([TimeSpan]::FromMinutes(15))) { throw "timed out waiting for another Start/Stop of $VmName" } }
    catch [System.Threading.AbandonedMutexException] { }
    return $mutex
}

function Get-VmProcesses {
    # vmware-vmx is started by the vmware-authd service, and a normal process can't read its
    # command line; the first line of the VM's vmware.log names its pid.
    $first = Get-Content -LiteralPath (Join-Path $VmDir 'vmware.log') -TotalCount 1 -ErrorAction SilentlyContinue
    if ($first -notmatch '\bpid=(\d+)') { return @() }
    @(Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue | Where-Object ProcessName -eq 'vmware-vmx')
}

function Set-VmPriority([string]$Priority = 'BelowNormal') {
    # The VM yields to whatever the user runs. vmware-vmx is started by vmware-authd, so this needs
    # admin rights and is normally refused (ignored). The vmx can only ask for normal or Idle
    # (priority.ungrabbed = "low"), and Idle starved the VM under host builds; vm-build.ps1 uses normal.
    foreach ($p in Get-VmProcesses) {
        try { $p.PriorityClass = $Priority } catch { }
    }
}

function Get-VmPriority {
    # Base priority (8 normal, 6 below normal, 4 idle) and host RAM of the selected VM's vmware-vmx.
    (Get-VmProcesses | ForEach-Object {
        "vmware-vmx $($_.Id) base priority $($_.BasePriority), working set $([math]::Round($_.WorkingSet64 / 1GB, 2)) GB"
    }) -join '; '
}

function Set-VmxValues([hashtable]$Values, [string]$Vmx = $VmVmx) {
    # Replace or append "key = value" lines (only while the VM is off). Returns the keys changed.
    $lines = [System.Collections.Generic.List[string]](Get-Content -LiteralPath $Vmx)
    $changed = @()
    foreach ($key in $Values.Keys) {
        $line = "$key = `"$($Values[$key])`""
        $at = -1
        for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match "^\s*$([regex]::Escape($key))\s*=") { $at = $i; break } }
        if ($at -ge 0 -and $lines[$at] -eq $line) { continue }
        if ($at -ge 0) { $lines[$at] = $line } else { $lines.Add($line) }
        $changed += $key
    }
    if ($changed) { [IO.File]::WriteAllLines($Vmx, $lines) }
    return $changed
}

function Wait-VmSession([int]$Seconds = 600) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while (-not (Test-VmSession)) {
        if ((Get-Date) -gt $deadline) { throw "the VM's desktop session is not up after $Seconds s" }
        Start-Sleep -Seconds 5
    }
}

function Start-VmHeadless([string]$Priority = 'BelowNormal') {
    if (-not (Test-VmRunning)) {
        $changed = Set-VmxValues $VmSettings
        if ($changed) { "  ${VmName}: set $(($changed | ForEach-Object { "$_ = $($VmSettings[$_])" }) -join ', ')" }
        Invoke-Vmrun @('start', $VmVmx, 'nogui') -Timeout 600 | Out-Null
    }
    Set-VmPriority $Priority
    Wait-VmSession
}

function Stop-Vm {
    if (-not (Test-VmRunning)) { return 'not running' }
    $soft = Invoke-Vmrun @('stop', $VmVmx, 'soft') -Quiet -Timeout 180
    if (Test-VmRunning) { Invoke-Vmrun @('stop', $VmVmx, 'hard') -Quiet -Timeout 120 | Out-Null; return "stopped (hard; soft said: $($soft.Out))" }
    return 'stopped'
}

function Set-VmRepoShare([string]$Path) {
    # The guest's C:\Repo is the shared folder "Repo", which vm-build.ps1 points at the checkout it
    # ran from. A series from another checkout (a git worktree) must see that one: its out\ builds
    # and its harness scripts. A marker file proves the guest sees $Path.
    $Path = (Resolve-Path -LiteralPath $Path).Path
    Invoke-Vmrun @('setSharedFolderState', $VmVmx, 'Repo', $Path, 'readonly') | Out-Null
    $name = "vm-share-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    $marker = Join-Path $Path "out\$name"
    New-Item -ItemType Directory -Force -Path (Split-Path $marker) | Out-Null
    Set-Content -LiteralPath $marker $Path
    try {
        $seen = $false
        for ($try = 1; $try -le 10 -and -not $seen; $try++) {
            $r = Invoke-Vmrun @('fileExistsInGuest', $VmVmx, "C:\Repo\out\$name") -Guest -Quiet -Timeout 60
            $seen = $r.Exit -eq 0 -and $r.Out -match 'exists' -and $r.Out -notmatch 'not exist'
            if (-not $seen) { Start-Sleep -Seconds 2 }
        }
        if (-not $seen) { throw "the VM's C:\Repo does not show $Path after setSharedFolderState" }
    } finally { Remove-Item -LiteralPath $marker -ErrorAction SilentlyContinue }
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
        # Right after logon the session's vmtoolsd runs before VMware counts the user as logged in
        # interactively; that error is retried for up to 3 minutes.
        $retryUntil = (Get-Date).AddMinutes(3)
        do {
            $r = Invoke-Vmrun @('runProgramInGuest', $VmVmx, '-interactive', 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
                '-WindowStyle', 'Hidden', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $guestWrapper) -Guest -Quiet -Timeout $Timeout
            $notYet = $r.Out -match 'must be logged in interactively'
            if ($notYet) { Start-Sleep -Seconds 10 }
        } while ($notYet -and (Get-Date) -lt $retryUntil)
        Invoke-Vmrun @('deleteFileInGuest', $VmVmx, $guestWrapper) -Guest -Quiet -Timeout 60 | Out-Null
        if ($r.Exit -ne 0 -or $r.Out -match 'non-zero exit code') { throw "guest command failed ($($r.Out)): $commandLine" }
        return $r.Out
    } finally { Remove-Item -LiteralPath $wrapper -ErrorAction SilentlyContinue }
}

function Start-Detached([string]$Script, [string]$Arguments) {
    # A hidden PowerShell outside this process tree and job (through WMI; a tool that kills the
    # caller's tree leaves it alone), as host-session.ps1 starts its watcher.
    $commandLine = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" $Arguments"
    try {
        $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
        $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $commandLine; ProcessStartupInformation = $startup }
        if ($r.ReturnValue -eq 0) { return }
    } catch { }
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" $Arguments" -WindowStyle Hidden
}

function Get-VmIdleMinutes {
    # $env:DSF_VM_IDLE_MINUTES: a VM no series has used for that long is powered off (run-series.ps1
    # -Vm arms it at the end of each series); unset: 15; a negative number: never.
    if ("$env:DSF_VM_IDLE_MINUTES" -match '^\s*-?\d+\s*$') { return [int]$env:DSF_VM_IDLE_MINUTES }
    return 15
}
