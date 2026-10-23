<#
Runs once, elevated, at the first logon of the test VM's user (autounattend.xml
FirstLogonCommands, from the config ISO as X:\dsf-first-logon.ps1). Makes the guest a
quiet test box, installs VMware Tools from the VMware Tools CD, then restarts.
tools/wsb/vm-build.ps1 waits for C:\dsf\first-logon.done.

Nothing here depends on the display language (the guest is Russian): groups by SID,
services by service name, no parsing of localised command output.
#>
param([string]$Media = '')

$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path C:\dsf | Out-Null
Start-Transcript -Path C:\dsf\first-logon.log -Append | Out-Null

function Set-Reg([string]$Path, [string]$Name, $Value, [string]$Type = 'DWord') {
    if (-not (Test-Path $Path)) { New-Item -Path $Path -Force | Out-Null }
    New-ItemProperty -Path $Path -Name $Name -Value $Value -PropertyType $Type -Force | Out-Null
}

# Autologon for good (Setup's AutoLogon counts down LogonCount).
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-Reg $winlogon AutoAdminLogon '1' String
Set-Reg $winlogon DefaultUserName 'dsf' String
Set-Reg $winlogon DefaultPassword 'dsfforza' String
Set-Reg $winlogon DefaultDomainName $env:COMPUTERNAME String
Remove-ItemProperty $winlogon -Name AutoLogonCount -ErrorAction SilentlyContinue
Set-Reg 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device' DevicePasswordLessBuildVersion 0

# No UAC (takes effect after the restart below).
$system = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
Set-Reg $system EnableLUA 0
Set-Reg $system ConsentPromptBehaviorAdmin 0
Set-Reg $system PromptOnSecureDesktop 0

# Never sleep, blank or lock.
foreach ($setting in 'monitor-timeout-ac', 'monitor-timeout-dc', 'standby-timeout-ac', 'standby-timeout-dc',
                     'hibernate-timeout-ac', 'hibernate-timeout-dc', 'disk-timeout-ac', 'disk-timeout-dc') {
    powercfg.exe /change $setting 0
}
powercfg.exe /hibernate off
powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_NONE CONSOLELOCK 0
powercfg.exe /setactive SCHEME_CURRENT
Set-Reg 'HKCU:\Control Panel\Desktop' ScreenSaveActive '0' String
Set-Reg 'HKCU:\Software\Policies\Microsoft\Windows\Control Panel\Desktop' ScreenSaveActive '0' String
Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' NoLockScreen 1
Set-Reg $system InactivityTimeoutSecs 0
Set-Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\System' DisableLockWorkstation 1

# The test script foregrounds the game window from a background process.
Set-Reg 'HKCU:\Control Panel\Desktop' ForegroundLockTimeout 0

# No notifications, tips or first-run screens over the game.
Set-Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\PushNotifications' ToastEnabled 0
Set-Reg 'HKCU:\Software\Policies\Microsoft\Windows\Explorer' DisableNotificationCenter 1
Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableWindowsConsumerFeatures 1
Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableSoftLanding 1
Set-Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement' ScoobeSystemSettingEnabled 0
Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System' EnableSmartScreen 0
Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender Security Center\Notifications' DisableNotifications 1
Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\GameDVR' AllowGameDVR 0
Set-Reg 'HKCU:\System\GameConfigStore' GameDVR_Enabled 0
# Files from \\vmware-host count as intranet (no "open file" security prompt).
Set-Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings\ZoneMap\Domains\vmware-host' file 1
Set-Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings\ZoneMap\Domains\vmware-host' '*' 1

# No Windows Update (the VM has no network anyway), no indexing or prefetch churn.
Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoUpdate 1
Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' DisableWindowsUpdateAccess 1
foreach ($service in 'wuauserv', 'UsoSvc', 'WSearch', 'SysMain', 'DiagTrack', 'MapsBroker') {
    Stop-Service $service -Force -ErrorAction SilentlyContinue
    Set-Service $service -StartupType Disabled -ErrorAction SilentlyContinue
}
# WaaSMedicSvc re-enables wuauserv; its key is protected, so this may be refused.
Set-Reg 'HKLM:\SYSTEM\CurrentControlSet\Services\WaaSMedicSvc' Start 4

# Defender: exclusions always work; turning real-time protection off is refused while
# Tamper Protection is on, which is fine.
Add-MpPreference -ExclusionPath 'C:\Game', 'C:\Run', 'C:\Repo', 'C:\Output', 'C:\dsf' -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionProcess 'Driver.exe' -ErrorAction SilentlyContinue
Set-MpPreference -DisableRealtimeMonitoring $true -SubmitSamplesConsent 2 -MAPSReporting 0 -ErrorAction SilentlyContinue

# Firewall on but silent; the run copy of the game pre-authorised (as setup-system.ps1).
netsh.exe advfirewall set allprofiles state on
netsh.exe advfirewall set allprofiles settings inboundusernotification disable
netsh.exe advfirewall firewall delete rule name=dsfforza-driver | Out-Null
netsh.exe advfirewall firewall add rule name=dsfforza-driver dir=in action=allow program=C:\Run\Driver.exe profile=any
netsh.exe advfirewall firewall add rule name=dsfforza-driver dir=out action=allow program=C:\Run\Driver.exe profile=any

# VC++ 2015-2022 x86 runtime (DriverNGHook), from the config ISO.
$redist = if ($Media) { Join-Path $Media 'vc_redist.x86.exe' }
if ($redist -and (Test-Path $redist)) {
    $p = Start-Process $redist -ArgumentList '/install', '/quiet', '/norestart' -Wait -PassThru
    "vc_redist.x86 exit $($p.ExitCode)"
}

# VMware Tools (SVGA 3D driver, vmrun guest operations, shared folders).
$tools = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root 'setup64.exe' } |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if ($tools) {
    "VMware Tools from $tools"
    $p = Start-Process $tools -ArgumentList '/S', '/v"/qn REBOOT=R ADDLOCAL=ALL"' -Wait -PassThru
    "VMware Tools setup exit $($p.ExitCode)"
} else { 'VMware Tools CD not found' }

Set-Content C:\dsf\first-logon.done (Get-Date -Format o)
Stop-Transcript | Out-Null
shutdown.exe /r /t 5 /f /d p:4:1
