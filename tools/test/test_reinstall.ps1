# install.ps1 -Force over its own install (issue #129), against a scratch game folder only.
# Prints PASS/FAIL per case and the time of each step; the folder must end byte-identical
# to before. Needs two finished builds with gui\ (port_batch build --gui) whose archives
# differ, and a scratch game folder holding copies of the vanilla 1.04 Install\dngvehicles.sp,
# Install\gui.sp, media\Fonts.fchunk and Locale\, and a Driver.exe (an empty file does; never
# the installed game):
#   powershell -File tools\test\test_reinstall.ps1 -GamePath out\scratch\game -Build out\b1 -OtherBuild out\b2
param(
    [Parameter(Mandatory = $true)][string]$GamePath,
    [Parameter(Mandatory = $true)][string]$Build,
    [Parameter(Mandatory = $true)][string]$OtherBuild
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$inst = Join-Path $repo 'tools\install.ps1'
$game = (Resolve-Path $GamePath).Path
if ($env:DSF_GAME_DIR -and $game -eq (Resolve-Path $env:DSF_GAME_DIR).Path) { throw 'not the installed game: use a scratch copy' }
$backup = Join-Path $game 'dsfforza-backup'
if (Test-Path $backup) { throw "$backup exists: the scratch folder is not clean" }
$Build = (Resolve-Path $Build).Path
$OtherBuild = (Resolve-Path $OtherBuild).Path
$results = @()

function Snapshot {
    Get-ChildItem -LiteralPath $game -Recurse -File -Force | Sort-Object FullName |
        ForEach-Object { '{0} {1}' -f $_.FullName.Substring($game.Length), (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
}
function Run([string[]]$ArgList) {
    $ErrorActionPreference = 'Continue'
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $inst -GamePath $game @ArgList 2>&1 | ForEach-Object { "$_" }
    return @{ code = $LASTEXITCODE; text = ($out -join "`n"); seconds = [math]::Round($watch.Elapsed.TotalSeconds, 1) }
}
function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    $script:results += ('{0} {1}{2}' -f ($(if ($Ok) { 'PASS' } else { 'FAIL' })), $Name, $(if ($Detail) { ": $Detail" } else { '' }))
}
function Entry([string]$Relative) {
    $m = Get-Content -LiteralPath (Join-Path $backup 'install.json') -Raw | ConvertFrom-Json
    return @($m.entries | Where-Object { $_.path -eq $Relative })[0]
}
function Hash([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }
$archive = Join-Path $game 'Install\dngvehicles.sp'
$common = @('-SkipHook', '-IdPolicy', 'garage')

$before = Snapshot
$buildHash, $otherHash = (Hash (Join-Path $Build 'dngvehicles.sp')), (Hash (Join-Path $OtherBuild 'dngvehicles.sp'))
if ($buildHash -eq $otherHash) { throw '-Build and -OtherBuild have the same archive' }

# 1. a first install records the stamps
$r = Run (@('-Build', $Build) + $common)
$e = Entry 'Install\dngvehicles.sp'
Check "install ($($r.seconds) s)" ($r.code -eq 0 -and $e.installed_stamp -and $e.source_stamp -and (Hash $archive) -eq $buildHash) $r.text

# 2. the same build again: archive, gui.sp and Fonts.fchunk stay, the rest is reinstalled
$r = Run (@('-Build', $Build, '-Force') + $common)
$kept = @(([regex]::Matches($r.text, '(?m)^\s*kept\s+(\S+) \(the same file as installed')) | ForEach-Object { $_.Groups[1].Value })
Check "reinstall, same build ($($r.seconds) s)" ($r.code -eq 0 -and (Hash $archive) -eq $buildHash -and
    ($kept -contains 'Install\dngvehicles.sp') -and ($kept -contains 'Install\gui.sp') -and ($kept -contains 'media\Fonts.fchunk')) $r.text
Check 'the kept entry is the first install''s' ((Entry 'Install\dngvehicles.sp').installed_sha256 -eq $buildHash -and
    (Entry 'Install\dngvehicles.sp').original_sha256 -eq $e.original_sha256)

# 3. a copy of the build with a new time on the archive: the same bytes, kept after a hash
$copy = Join-Path (Split-Path $Build) ((Split-Path $Build -Leaf) + '-retimed')
if (Test-Path $copy) { Remove-Item -Recurse -Force $copy }
New-Item -ItemType Directory $copy | Out-Null
foreach ($name in @('dngvehicles.sp', 'dngvehicles.json', 'dngvehicles.lua')) { Copy-Item (Join-Path $Build $name) $copy }
Copy-Item -Recurse (Join-Path $Build 'gui') $copy
(Get-Item (Join-Path $copy 'dngvehicles.sp')).LastWriteTime = Get-Date
$r = Run (@('-Build', $copy, '-Force') + $common)
Check "reinstall, same bytes, new time ($($r.seconds) s)" ($r.code -eq 0 -and $r.text -match 'kept\s+Install\\dngvehicles\.sp \(the same') $r.text
Remove-Item -Recurse -Force $copy

# 4. another build: its archive is copied
$r = Run (@('-Build', $OtherBuild, '-Force') + $common)
Check "reinstall, another build ($($r.seconds) s)" ($r.code -eq 0 -and (Hash $archive) -eq $otherHash -and
    $r.text -notmatch 'kept\s+Install\\dngvehicles\.sp \(the same') $r.text

# 5. back to the first build, then the game's archive touched since the install: copied again
$r = Run (@('-Build', $Build, '-Force') + $common)
(Get-Item $archive).LastWriteTime = (Get-Date).AddMinutes(-5)
$r = Run (@('-Build', $Build, '-Force') + $common)
Check "reinstall after the game file changed ($($r.seconds) s)" ($r.code -eq 0 -and (Hash $archive) -eq $buildHash -and
    $r.text -notmatch 'kept\s+Install\\dngvehicles\.sp \(the same') $r.text

# 6. uninstall: every file as before
$r = Run @('-Uninstall')
$after = Snapshot
Check "uninstall restores every file ($($r.seconds) s)" ($r.code -eq 0 -and -not (Compare-Object $before $after) -and -not (Test-Path $backup)) $r.text

$results
if ($results -match '^FAIL') { exit 1 }
