# tools\wsb\host-session.ps1 against a fake profile in %TEMP% (never the real one; speakers
# untouched: -NoMute; its own mutex, so it doesn't wait for or block real series).
# Prints PASS/FAIL per case.
#   powershell -NoProfile -File tools\wsb\test-host-session.ps1
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'host-session.ps1'
$root = Join-Path $env:TEMP ('dsfforza-host-session-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$profileDir = Join-Path $root 'Driver San Francisco'
$stateDir = Join-Path $root 'state\host-session'
$common = @{ ProfileDir = $profileDir; StateDir = $stateDir; MutexName = "Local\dsfforza-test-$PID"; NoMute = $true
    RunPath = (Join-Path $root 'run') }
$results = @()

function Check([string]$Name, [bool]$Ok) { $script:results += "$(if ($Ok) { 'PASS' } else { 'FAIL' }) $Name" }
function Snapshot {
    Get-ChildItem -LiteralPath $profileDir -Recurse -File -Force | Sort-Object FullName |
        ForEach-Object { '{0} {1}' -f $_.FullName.Substring($profileDir.Length), (Get-FileHash -LiteralPath $_.FullName).Hash }
}
function Wait-Until([scriptblock]$Condition, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while (-not (& $Condition) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    return [bool](& $Condition)
}

New-Item -ItemType Directory -Force -Path (Join-Path $profileDir 'reloaded\savegames') | Out-Null
Set-Content (Join-Path $profileDir 'graphics.ini') "Fullscreen = 1`r`nWidth = 3440`r`nHeight = 1440"
Set-Content (Join-Path $profileDir 'reloaded\savegames\autosave.dat') 'save'
$before = Snapshot
try {
    # 1. Enter switches the profile to a window; Restore puts it back and drops files the run added.
    & $script Enter -Resolution 1280x720 @common
    Check 'enter: windowed' ((Get-Content (Join-Path $profileDir 'graphics.ini')) -contains 'Fullscreen = 0')
    Set-Content (Join-Path $profileDir 'reloaded\savegames\autosave.dat') 'changed by the run'
    Set-Content (Join-Path $profileDir 'reloaded\new.dat') 'added by the run'
    & $script Restore @common
    Check 'restore: profile identical' ($null -eq (Compare-Object $before (Snapshot)))
    Check 'restore: state removed' (-not (Test-Path $stateDir))

    # 2. A backup that died half-way (no 'complete') is dropped, not restored; Enter then works.
    New-Item -ItemType Directory -Force -Path (Join-Path $stateDir 'profile') | Out-Null
    Set-Content (Join-Path $stateDir 'profile\graphics.ini') 'partial'
    & $script Enter @common
    Check 'half backup: new backup not nested' (-not (Test-Path (Join-Path $stateDir 'profile\Driver San Francisco')))
    & $script Restore @common
    Check 'half backup: profile identical' ($null -eq (Compare-Object $before (Snapshot)))

    # 3. A killed series: its watcher restores the profile once the series process is gone.
    $series = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList (@('-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-Command', "& '$script' Enter -SeriesId `$PID -NoMute $(($common.Keys | Where-Object { $_ -ne 'NoMute' } |
        ForEach-Object { "-$_ '$($common[$_])'" }) -join ' '); Start-Sleep 600"))
    Check 'killed: entered' (Wait-Until { (Test-Path (Join-Path $stateDir 'complete')) -and
        ((Get-Content (Join-Path $profileDir 'graphics.ini')) -contains 'Fullscreen = 0') } 60)
    Set-Content (Join-Path $profileDir 'reloaded\new.dat') 'added by the run'
    Stop-Process -Id $series.Id -Force
    Check 'killed: watcher restored' (Wait-Until { -not (Test-Path $stateDir) } 60)
    Check 'killed: profile identical' ($null -eq (Compare-Object $before (Snapshot)))
    $log = Join-Path (Split-Path $stateDir) 'host-session-watch.log'
    Check 'killed: watcher logged' ((Test-Path $log) -and ((Get-Content $log) -match 'restored$'))
}
finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
$results
if ($results -match '^FAIL') { exit 1 }
