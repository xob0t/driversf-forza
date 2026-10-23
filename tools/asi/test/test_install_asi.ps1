# install.ps1 with dsfforza.asi, against out\fakegame only (copies, no links).
# Prints PASS/FAIL per case; the fake tree must end byte-identical to before.
#   powershell -File tools\asi\test\test_install_asi.ps1
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
Set-Location $repo
$g = 'out\fakegame'
$gFull = (Resolve-Path $g).Path
$build = 'out\scratch\testbuild'
$loader = 'out\scratch\asi\dinput8.dll'
$inst = Join-Path $repo 'tools\install.ps1'
$backup = Join-Path $gFull 'dsfforza-backup'
$results = @()

function Snapshot {
    Get-ChildItem -LiteralPath $gFull -Recurse -File -Force | Sort-Object FullName |
        ForEach-Object { '{0} {1}' -f $_.FullName.Substring($gFull.Length), (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
}
function Run([string[]]$ArgList) {
    $ErrorActionPreference = 'Continue'
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $inst -GamePath $g @ArgList 2>&1 | ForEach-Object { "$_" }
    return @{ code = $LASTEXITCODE; text = ($out -join "`n") }
}
function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    $script:results += ('{0} {1}{2}' -f ($(if ($Ok) { 'PASS' } else { 'FAIL' })), $Name, $(if ($Detail) { ": $Detail" } else { '' }))
}

if (-not (Test-Path 'out\asi\dsfforza.asi')) { throw 'build out\asi\dsfforza.asi first (tools\asi\build.ps1)' }
if (-not (Test-Path $build)) { throw "$build missing (see research/batch.md, installer tests)" }
New-Item -ItemType Directory -Force (Split-Path $loader) | Out-Null
if (-not (Test-Path $loader)) { Copy-Item 'D:\Games\Driver San Francisco\winmm.dll' $loader }   # read only: a stand-in ASI loader
if (Test-Path $backup) { throw "$backup exists: the fake tree is not clean" }
$before = Snapshot
$asiHash = (Get-FileHash 'out\asi\dsfforza.asi' -Algorithm SHA256).Hash

# 1. -Asi installs out\asi\dsfforza.asi and its ini
$r = Run @('-Build', $build, '-AsiLoader', $loader, '-Force', '-Asi')
Check 'install' ($r.code -eq 0) $(if ($r.code) { $r.text })
Check 'asi installed' ($r.text -match 'created\s+plugins\\dsfforza.asi' -and $r.text -match 'created\s+plugins\\dsfforza.ini')
Check 'asi bytes' ((Get-FileHash (Join-Path $gFull 'plugins\dsfforza.asi') -Algorithm SHA256).Hash -eq $asiHash)
$m = Get-Content (Join-Path $backup 'install.json') -Raw | ConvertFrom-Json
Check 'manifest lists the asi and its log' ((@($m.entries.path) -contains 'plugins\dsfforza.asi') -and (@($m.runtime_files) -contains 'plugins\dsfforza.log'))
# the plugin's runtime log, as the game would write it
Set-Content (Join-Path $gFull 'plugins\dsfforza.log') 'test log'
$r = Run @('-Status')
Check 'status shows the plugin' ($r.text -match 'dsfforza.asi: present; plugins\\dsfforza.log from')
$r = Run @('-Uninstall')
Check 'uninstall removes asi, ini and log' ($r.code -eq 0 -and $r.text -match 'removed\s+plugins\\dsfforza.asi' -and $r.text -match 'removed\s+plugins\\dsfforza.log') $(if ($r.code) { $r.text })
Check 'tree identical after uninstall' ((Compare-Object $before (Snapshot)) -eq $null)

# 2. without -Asi: no plugin (the default, as before)
$r = Run @('-Build', $build, '-AsiLoader', $loader, '-Force')
Check 'install without -Asi' ($r.code -eq 0 -and -not (Test-Path (Join-Path $gFull 'plugins\dsfforza.asi')))
$r = Run @('-Uninstall')
Check 'tree identical after the plain uninstall' ($r.code -eq 0 -and (Compare-Object $before (Snapshot)) -eq $null)

# 3. a manifest from before this change (no runtime_files) still uninstalls
$r = Run @('-Build', $build, '-AsiLoader', $loader, '-Force', '-Asi')
$mp = Join-Path $backup 'install.json'
$m = Get-Content $mp -Raw | ConvertFrom-Json
$m.PSObject.Properties.Remove('runtime_files')
$m | ConvertTo-Json -Depth 6 | Set-Content $mp -Encoding UTF8
$r = Run @('-Uninstall')
Check 'old manifest uninstalls' ($r.code -eq 0 -and (Compare-Object $before (Snapshot)) -eq $null) $(if ($r.code) { $r.text })

# 4. an edited dsfforza.ini is the user's: kept on uninstall
$r = Run @('-Build', $build, '-AsiLoader', $loader, '-Force', '-Asi')
Add-Content (Join-Path $gFull 'plugins\dsfforza.ini') '; my change'
$r = Run @('-Uninstall')
Check 'edited ini kept' ($r.code -eq 0 -and $r.text -match 'kept plugins\\dsfforza.ini' -and (Test-Path (Join-Path $gFull 'plugins\dsfforza.ini')))
Remove-Item (Join-Path $gFull 'plugins\dsfforza.ini')
if (-not (Get-ChildItem (Join-Path $gFull 'plugins') -Force)) { Remove-Item (Join-Path $gFull 'plugins') }
Check 'tree identical after removing the kept ini' ((Compare-Object $before (Snapshot)) -eq $null)

# 5. -IdPolicy asi without -Asi refuses before changing anything
$r = Run @('-Build', $build, '-AsiLoader', $loader, '-Force', '-IdPolicy', 'asi')
Check '-IdPolicy asi without the plugin refused' ($r.code -ne 0 -and $r.text -match 'IdPolicy asi needs the dsfforza.asi plugin' -and -not (Test-Path $backup))

# 6. an archive past the stock reader (1200 root entries): refused without -Asi, installed with the plugin
$big = 'out\scratch\testbuild-bigroot'
New-Item -ItemType Directory -Force $big | Out-Null
Copy-Item (Join-Path $build 'dngvehicles.json'), (Join-Path $build 'dngvehicles.lua') $big -Force
$header = [byte[]](0x43, 0x48, 0x4E, 0x4B, 0, 0x10, 0, 0, 0xB0, 0x04, 0, 0, 3, 0, 0, 0) + (New-Object byte[] 4080)
[IO.File]::WriteAllBytes((Join-Path $repo "$big\dngvehicles.sp"), $header)
$r = Run @('-Build', $big, '-AsiLoader', $loader, '-Force')
Check '1200-entry archive refused without the plugin' ($r.code -ne 0 -and $r.text -match '1200 root entries' -and -not (Test-Path $backup))
$r = Run @('-Build', $big, '-AsiLoader', $loader, '-Force', '-Asi')
Check '1200-entry archive installed with the plugin' ($r.code -eq 0) $(if ($r.code) { $r.text })
$r = Run @('-Uninstall')
Check 'tree identical at the end' ($r.code -eq 0 -and (Compare-Object $before (Snapshot)) -eq $null)

$results
if ($results -match '^FAIL') { exit 1 }
