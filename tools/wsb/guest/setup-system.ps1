# Runs as SYSTEM inside the sandbox (from C:\Output: SYSTEM can't read the
# read-only mappings). Driver's FirewallInstallHelper runs at startup; keep the
# firewall on but silent and pre-authorize the test executable so no modal
# dialog appears. netsh, because the NetSecurity cmdlets get "Access is
# denied" from CIM under wsb exec.
$ErrorActionPreference = 'Stop'
$log = [System.Collections.Generic.List[string]]::new()
function Run([string[]]$NetshArguments) {
    $output = & netsh.exe @NetshArguments 2>&1 | Out-String
    $log.Add("netsh $($NetshArguments -join ' ') => $LASTEXITCODE $($output.Trim())")
    if ($LASTEXITCODE -ne 0) { throw "netsh failed: $($NetshArguments -join ' ')" }
}
try {
    Run 'advfirewall', 'set', 'allprofiles', 'state', 'on'
    Run 'advfirewall', 'set', 'allprofiles', 'settings', 'inboundusernotification', 'disable'
    & netsh.exe advfirewall firewall delete rule name=dsfforza-driver 2>&1 | Out-Null
    Run 'advfirewall', 'firewall', 'add', 'rule', 'name=dsfforza-driver', 'dir=in', 'action=allow', 'program=C:\Run\Driver.exe', 'profile=any'
    Run 'advfirewall', 'firewall', 'add', 'rule', 'name=dsfforza-driver', 'dir=out', 'action=allow', 'program=C:\Run\Driver.exe', 'profile=any'
    @{ firewall = 'configured'; log = $log; utc = (Get-Date).ToUniversalTime().ToString('o') } |
        ConvertTo-Json | Set-Content C:\Output\setup-system.json
    Remove-Item C:\Output\setup-system-error.txt -ErrorAction SilentlyContinue
}
catch {
    ($log + ($_ | Format-List * -Force | Out-String)) | Set-Content C:\Output\setup-system-error.txt
    exit 1
}
