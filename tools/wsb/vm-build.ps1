<#
.SYNOPSIS
Builds the dsfforza test VM (VMware Workstation) headless: unattended Windows install,
VMware Tools, game copy, snapshot "clean". See tools\wsb\vm.ps1 for using it.

.DESCRIPTION
Steps (default: all, in order; each can be run alone):
  Iso        C:\VMs\dsfforza-test\config.iso: autounattend.xml, dsf-first-logon.ps1 and
             the VC++ x86 runtime (C:\VMs\downloads\vcredist\vc_redist.x86.exe).
  Create     the VM: 4 vCPU, 8 GB, 80 GB growable SATA disk, BIOS firmware (no TPM or
             Secure Boot; the Windows 11 checks are bypassed in autounattend.xml), SVGA
             3D with 2 GB graphics memory, no network, no USB, shared folders
             Repo (read-only), Output (read-write), Game and Profile (read-only).
  Install    boots the Windows ISO with no window and waits for the unattended install,
             first-logon.ps1 and VMware Tools; then powers off, detaches the CDs and adds
             the sound card (muted in the guest by setup-vm.ps1 Session).
  Provision  boots, runs guest\setup-vm.ps1 -Phase Provision (links, C:\Game, profile,
             display, mute), powers off.
  Snapshot   snapshot "clean" of the powered-off VM (vm.ps1 Revert goes back to it).

.EXAMPLE
.\tools\wsb\vm-build.ps1                    # everything (about an hour)
.\tools\wsb\vm-build.ps1 -Step Provision    # re-provision an installed VM
#>
param(
    [ValidateSet('All', 'Iso', 'Create', 'Install', 'Provision', 'Snapshot')] [string]$Step = 'All',
    [string]$WindowsIso = $env:DSF_VM_ISO,   # a Windows 10/11 x64 install ISO
    [string]$VcRedist = 'C:\VMs\downloads\vcredist\vc_redist.x86.exe',
    [string]$GamePath = $env:DSF_GAME_DIR,
    [int]$Cpus = 4,
    [int]$MemoryMB = 8192,
    [int]$DiskGB = 80,
    [int]$InstallMinutes = 120,
    [int]$VncPort = 0,             # Create: VNC on 127.0.0.1:<port> (no password) to watch a headless install;
                                   # grab a frame with: python -I tools\wsb\vm\vncshot.py 127.0.0.1 <port> shot.png
    [switch]$Force                 # Create: replace an existing VM
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'vm-common.ps1')
if (-not $GamePath) { throw 'Set DSF_GAME_DIR (or pass -GamePath) to your Driver: San Francisco folder, the one with Driver.exe.' }
if (-not $WindowsIso) { throw 'Set DSF_VM_ISO (or pass -WindowsIso) to a Windows 10/11 x64 install ISO.' }
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$profileHost = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ubisoft\Driver San Francisco'
$configIso = Join-Path $VmDir 'config.iso'
function Say([string]$text) { "$(Get-Date -Format HH:mm:ss) $text" }

function New-IsoFile([string]$Source, [string]$Path, [string]$Label) {
    # ISO 9660 + Joliet through Windows' IMAPI2FS; the image stream is copied out in C#.
    if (-not ('DsfIso' -as [type])) {
        Add-Type -TypeDefinition @'
using System.IO;
using System.Runtime.InteropServices.ComTypes;
public static class DsfIso {
    public static void Write(string path, object stream, int blockSize, int blocks) {
        var input = (IStream)stream; var buffer = new byte[blockSize];
        using (var output = File.Create(path)) {
            for (int i = 0; i < blocks; i++) { input.Read(buffer, blockSize, System.IntPtr.Zero); output.Write(buffer, 0, blockSize); }
        }
    }
}
'@
    }
    $image = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
    $image.FileSystemsToCreate = 3
    $image.VolumeName = $Label
    $image.Root.AddTree($Source, $false)
    $result = $image.CreateResultImage()
    [DsfIso]::Write($Path, $result.ImageStream, $result.BlockSize, $result.TotalBlocks)
}

function Set-VmxValues([hashtable]$Values) {
    # Replace or append "key = value" lines (only while the VM is off).
    $lines = [System.Collections.Generic.List[string]](Get-Content -LiteralPath $VmVmx)
    foreach ($key in $Values.Keys) {
        $line = "$key = `"$($Values[$key])`""
        $at = -1
        for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match "^\s*$([regex]::Escape($key))\s*=") { $at = $i; break } }
        if ($at -ge 0) { $lines[$at] = $line } else { $lines.Add($line) }
    }
    [IO.File]::WriteAllLines($VmVmx, $lines)
}

function Step-Iso {
    foreach ($file in $WindowsIso, $VcRedist) { if (-not (Test-Path $file)) { throw "missing $file" } }
    if ((Get-AuthenticodeSignature $VcRedist).Status -ne 'Valid') { throw "$VcRedist is not validly signed" }
    New-Item -ItemType Directory -Force -Path $VmDir | Out-Null
    $stage = Join-Path $VmDir 'config-iso'
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    New-Item -ItemType Directory -Path $stage | Out-Null
    Copy-Item (Join-Path $PSScriptRoot 'vm\autounattend.xml') $stage
    Copy-Item (Join-Path $PSScriptRoot 'vm\first-logon.ps1') (Join-Path $stage 'dsf-first-logon.ps1')
    Copy-Item $VcRedist $stage
    New-IsoFile $stage $configIso 'DSFCONFIG'
    Say "built $configIso ($((Get-Item $configIso).Length) bytes)"
}

function Step-Create {
    $vmware = Get-VmwareDir
    $toolsIso = Join-Path $vmware 'windows.iso'
    if (-not (Test-Path $toolsIso)) { throw "VMware Tools ISO not found at $toolsIso" }
    if (-not (Test-Path $configIso)) { throw "run -Step Iso first ($configIso missing)" }
    if (Test-Path $VmVmx) {
        if (-not $Force) { throw "$VmVmx exists; use -Force to replace the VM" }
        if (Test-VmRunning) { Stop-Vm | Out-Null }
        Get-ChildItem $VmDir -Force | Where-Object { $_.Name -notin 'config.iso', 'config-iso' } | Remove-Item -Recurse -Force
    }
    $disk = Join-Path $VmDir 'dsfforza-test.vmdk'
    $out = & (Join-Path $vmware 'vmware-vdiskmanager.exe') -c -s "${DiskGB}GB" -a lsilogic -t 0 $disk 2>&1
    if ($LASTEXITCODE -ne 0) { throw "vmware-vdiskmanager failed: $out" }

    $shared = @(
        @('Repo', $repo, 'FALSE'),
        @('Output', $VmOutputHost, 'TRUE'),
        @('Game', $GamePath, 'FALSE'),
        @('Profile', $profileHost, 'FALSE')
    )
    New-Item -ItemType Directory -Force -Path $VmOutputHost | Out-Null
    $vmx = [ordered]@{
        '.encoding' = 'UTF-8'; 'config.version' = '8'; 'virtualHW.version' = '21'
        'displayName' = 'dsfforza-test'; 'guestOS' = 'windows9-64'; 'firmware' = 'bios'
        'numvcpus' = "$Cpus"; 'cpuid.coresPerSocket' = "$Cpus"; 'memsize' = "$MemoryMB"
        'mainMem.useNamedFile' = 'FALSE'
        'pciBridge0.present' = 'TRUE'; 'pciBridge4.present' = 'TRUE'; 'pciBridge4.virtualDev' = 'pcieRootPort'; 'pciBridge4.functions' = '8'
        'pciBridge5.present' = 'TRUE'; 'pciBridge5.virtualDev' = 'pcieRootPort'; 'pciBridge5.functions' = '8'
        'vmci0.present' = 'TRUE'; 'hpet0.present' = 'TRUE'
        'sata0.present' = 'TRUE'
        'sata0:0.present' = 'TRUE'; 'sata0:0.fileName' = 'dsfforza-test.vmdk'
        'sata0:1.present' = 'TRUE'; 'sata0:1.deviceType' = 'cdrom-image'; 'sata0:1.fileName' = $WindowsIso; 'sata0:1.startConnected' = 'TRUE'
        'sata0:2.present' = 'TRUE'; 'sata0:2.deviceType' = 'cdrom-image'; 'sata0:2.fileName' = $configIso; 'sata0:2.startConnected' = 'TRUE'
        'sata0:3.present' = 'TRUE'; 'sata0:3.deviceType' = 'cdrom-image'; 'sata0:3.fileName' = $toolsIso; 'sata0:3.startConnected' = 'TRUE'
        'bios.bootOrder' = 'hdd,cdrom'
        'ethernet0.present' = 'FALSE'; 'usb.present' = 'FALSE'; 'ehci.present' = 'FALSE'; 'usb_xhci.present' = 'FALSE'
        'floppy0.present' = 'FALSE'; 'serial0.present' = 'FALSE'; 'parallel0.present' = 'FALSE'
        'sound.present' = 'FALSE'
        'svga.present' = 'TRUE'; 'mks.enable3d' = 'TRUE'; 'svga.graphicsMemoryKB' = '2097152'
        'tools.syncTime' = 'TRUE'; 'tools.upgrade.policy' = 'manual'
        'msg.autoAnswer' = 'TRUE'; 'uuid.action' = 'keep'
        'isolation.tools.hgfs.disable' = 'FALSE'; 'isolation.tools.copy.disable' = 'TRUE'; 'isolation.tools.paste.disable' = 'TRUE'
        'isolation.tools.dnd.disable' = 'TRUE'
        'sharedFolder.maxNum' = "$($shared.Count)"; 'hgfs.mapRootShare' = 'TRUE'; 'hgfs.linkRootShare' = 'TRUE'
        # A headless VM is never 'grabbed'. 'low' would be the Idle class (CPU and I/O): with port_batch
        # builds on all cores the install stalled (2.4 s per CD read), so normal.
        'priority.grabbed' = 'normal'; 'priority.ungrabbed' = 'normal'
        'powerType.powerOff' = 'soft'; 'powerType.reset' = 'soft'; 'powerType.suspend' = 'soft'
    }
    if ($VncPort) { $vmx['RemoteDisplay.vnc.enabled'] = 'TRUE'; $vmx['RemoteDisplay.vnc.ip'] = '127.0.0.1'; $vmx['RemoteDisplay.vnc.port'] = "$VncPort" }
    for ($i = 0; $i -lt $shared.Count; $i++) {
        $name, $path, $write = $shared[$i]
        $vmx["sharedFolder$i.present"] = 'TRUE'; $vmx["sharedFolder$i.enabled"] = 'TRUE'
        $vmx["sharedFolder$i.readAccess"] = 'TRUE'; $vmx["sharedFolder$i.writeAccess"] = $write
        $vmx["sharedFolder$i.hostPath"] = $path; $vmx["sharedFolder$i.guestName"] = $name
        $vmx["sharedFolder$i.expiration"] = 'never'
    }
    [IO.File]::WriteAllLines($VmVmx, [string[]]@($vmx.Keys | ForEach-Object { "$_ = `"$($vmx[$_])`"" }))

    # No tray icon or hint dialogs from Workstation for this user.
    $prefs = Join-Path $env:APPDATA 'VMware\preferences.ini'
    New-Item -ItemType Directory -Force -Path (Split-Path $prefs) | Out-Null
    $lines = @(if (Test-Path $prefs) { Get-Content $prefs | Where-Object { $_ -notmatch '^(pref\.trayicon\.enabled|hints\.hideAll)\s*=' } })
    Set-Content $prefs ($lines + 'pref.trayicon.enabled = "FALSE"' + 'hints.hideAll = "TRUE"')
    Say "created $VmVmx"
}

function Step-Install {
    if (Test-VmRunning) { Say 'the VM is running: waiting for the install to finish' }
    else {
        Say 'starting the Windows install (no window)'
        Invoke-Vmrun @('start', $VmVmx, 'nogui') | Out-Null
    }
    Set-VmPriority
    $deadline = (Get-Date).AddMinutes($InstallMinutes)
    $last = ''
    while ($true) {
        Start-Sleep -Seconds 30
        if (-not (Test-VmRunning)) { throw 'the VM powered off during the install (see C:\VMs\dsfforza-test\vmware.log)' }
        Set-VmPriority
        $tools = (Invoke-Vmrun @('checkToolsState', $VmVmx) -Quiet -Timeout 60).Out.Trim()
        $done = $false
        if ($tools -match 'running|installed') {
            $marker = Invoke-Vmrun @('fileExistsInGuest', $VmVmx, 'C:\dsf\first-logon.done') -Guest -Quiet -Timeout 60
            $done = $marker.Exit -eq 0 -and $marker.Out -match 'exists' -and $marker.Out -notmatch 'not exist'
        }
        if ($tools -ne $last) { Say "tools: $tools"; $last = $tools }
        if ($done -and (Test-VmSession)) { break }
        if ((Get-Date) -gt $deadline) { throw "install not finished after $InstallMinutes min (tools: $tools)" }
    }
    Say 'Windows and VMware Tools are installed; the desktop session is up'
    Stop-Vm | Out-Null
    Set-VmxValues @{
        'sata0:1.present' = 'FALSE'; 'sata0:2.present' = 'FALSE'; 'sata0:3.present' = 'FALSE'
        'sound.present' = 'TRUE'; 'sound.virtualDev' = 'hdaudio'; 'sound.fileName' = '-1'; 'sound.autodetect' = 'TRUE'
    }
    Say 'powered off; CDs detached, sound card added'
}

function Step-Provision {
    Start-VmHeadless
    Say 'provisioning (copies the game: several minutes)'
    $out = & (Join-Path $PSScriptRoot 'vm.ps1') Exec -HostScript (Join-Path $PSScriptRoot 'guest\setup-vm.ps1') -Arguments @('-Phase', 'Provision') -TimeoutSeconds 3600
    Get-Content (Join-Path $VmOutputHost 'vm\setup-provision.json') -ErrorAction SilentlyContinue
    Stop-Vm | Out-Null
    Say 'provisioned; powered off'
}

function Step-Snapshot {
    if (Test-VmRunning) { Stop-Vm | Out-Null }
    $list = (Invoke-Vmrun @('listSnapshots', $VmVmx) -Quiet).Out
    if ($list -split "`r?`n" | Where-Object { $_.Trim() -eq 'clean' }) { Invoke-Vmrun @('deleteSnapshot', $VmVmx, 'clean') | Out-Null }
    Invoke-Vmrun @('snapshot', $VmVmx, 'clean') | Out-Null
    Say 'snapshot clean taken'
}

$lifecycle = Enter-VmLifecycle
try {
    foreach ($s in 'Iso', 'Create', 'Install', 'Provision', 'Snapshot') {
        if ($Step -eq 'All' -or $Step -eq $s) { & "Step-$s" }
    }
} finally { $lifecycle.ReleaseMutex() }
