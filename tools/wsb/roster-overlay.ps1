<#
.SYNOPSIS
Makes the files a VM run needs to play a car roster as a player has it after tools\install.ps1:
an -Overlay folder (dsfcars\ and Common\UserLuaScripts\) and the plugin with its installed .ini.

.DESCRIPTION
tools\install.ps1 installs the build's car packages into a scratch game folder (<Out>\game), which
holds only the game files the install reads, copied from out\vanilla and the game folder:
Driver.exe, Install\dngvehicles.sp, Install\gui.sp, Locale\<language>\global.fchunk,
media\Fonts.fchunk and media\ScriptsLuaScripts.fchunk. The game folder is only read. -SkipHook:
run-test.ps1 brings DriverNGHook and the ASI loader itself. Then:
  <Out>\overlay   dsfcars\ (the packages, generated\ and the car manager's files) and
                  Common\UserLuaScripts\ (bootstrap.lua and dsfcars\: packages.lua, traffic.lua, ...)
  <Out>\plugin    dsfcars.asi and the dsfcars.ini install.ps1 wrote (packages=1)
Run them with (research/in-game-tests.md, "Full roster in a VM"):
  run-series.ps1 -Vm -Archives out\vanilla\Install\dngvehicles.sp -Asi <Out>\plugin\dsfcars.asi
                 -Overlay <Out>\overlay -Traffic ...
The archive stays the vanilla one: the packages are what dsfcars.asi loads. A full roster's overlay
is about 4.3 GB; run-series copies it into each run folder and removes the copy after the run.

.EXAMPLE
.\tools\wsb\roster-overlay.ps1 -Build out\install-build              # 721 cars, master's plugin from out\asi
#>
param(
    [Parameter(Mandatory)] [string]$Build,     # a port_batch build folder with packages\*.dsfcar (e.g. tools\install.ps1's out\install-build)
    [string]$Out = 'out\roster',
    [string]$AsiPath = 'out\asi\dsfcars.asi',  # tools\asi\build.ps1
    [string]$GamePath = $env:DSF_GAME_DIR
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $repo
try {
    if (-not $GamePath) { throw 'Set DSF_GAME_DIR (or pass -GamePath) to your Driver: San Francisco folder.' }
    if (-not (Test-Path -LiteralPath (Join-Path $Build 'packages'))) { throw "no packages\ in $Build (port_batch --packages)" }
    if (-not (Test-Path -LiteralPath $AsiPath)) { throw "no plugin at $AsiPath (tools\asi\build.ps1)" }
    # $Out is deleted first: refuse one that is, holds or lies in an input.
    $full = { param($p) $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p).TrimEnd('') }
    $outFull = & $full $Out
    foreach ($inputPath in @($GamePath, $Build, $AsiPath)) {
        $inFull = & $full $inputPath
        if ($outFull -ieq $inFull -or $outFull.StartsWith("$inFull\", 'OrdinalIgnoreCase') -or $inFull.StartsWith("$outFull\", 'OrdinalIgnoreCase')) {
            throw "-Out $Out overlaps $inputPath"
        }
    }
    if (Test-Path -LiteralPath $Out) { Remove-Item -LiteralPath $Out -Recurse -Force }
    $game = New-Item -ItemType Directory -Force -Path (Join-Path $Out 'game')

    # The game files install.ps1 and the car manager read: the repo's vanilla copies (made while the
    # game folder was vanilla; src\dsfforza\game\game_paths.py), else the game folder's.
    $files = @('Install\dngvehicles.sp', 'Install\gui.sp', 'media\Fonts.fchunk', 'media\ScriptsLuaScripts.fchunk') +
        @(Get-ChildItem -LiteralPath (Join-Path $GamePath 'Locale') -Directory | ForEach-Object { "Locale\$($_.Name)\global.fchunk" })
    foreach ($rel in @('Driver.exe') + $files) {
        $from = Join-Path 'out\vanilla' $rel
        if ($rel -eq 'Driver.exe' -or -not (Test-Path -LiteralPath $from)) { $from = Join-Path $GamePath $rel }
        if (-not (Test-Path -LiteralPath $from)) { continue }
        $to = Join-Path $game.FullName $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null
        Copy-Item -LiteralPath $from -Destination $to
    }

    & (Join-Path $repo 'tools\install.ps1') -GamePath $game.FullName -Build $Build -AsiPath $AsiPath -SkipHook
    if (-not $?) { throw 'install.ps1 failed' }

    $overlay = New-Item -ItemType Directory -Force -Path (Join-Path $Out 'overlay\Common')
    Move-Item -LiteralPath (Join-Path $game.FullName 'dsfcars') -Destination (Split-Path $overlay.FullName)
    Move-Item -LiteralPath (Join-Path $game.FullName 'Common\UserLuaScripts') -Destination $overlay.FullName
    $plugin = New-Item -ItemType Directory -Force -Path (Join-Path $Out 'plugin')
    Copy-Item -Path (Join-Path $game.FullName 'plugins\dsfcars.*') -Exclude '*.log' -Destination $plugin.FullName
    Remove-Item -LiteralPath $game.FullName -Recurse -Force
    $cars = @(Get-ChildItem -LiteralPath (Join-Path $Out 'overlay\dsfcars\cars') -Directory).Count
    "roster: $cars car folders in $Out\overlay, plugin $Out\plugin\dsfcars.asi"
    "  run-series.ps1 -Vm -Archives out\vanilla\Install\dngvehicles.sp -Asi $Out\plugin\dsfcars.asi -Overlay $Out\overlay -Traffic ..."
} finally {
    Pop-Location
}
