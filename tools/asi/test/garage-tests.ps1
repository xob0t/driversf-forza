<#
.SYNOPSIS
In-game tests of dsfforza.asi's garage module, in the persistent sandbox
(each run-series call queues on its mutex; the runs share the guest's save).
-Vm runs them in the headless VM instead. The VM copies the host's profile into
the guest at the start of every series, so there set, stock and check don't see
each other's save; garage works.

.DESCRIPTION
  set     stock archive, plugin: set unlocked/owned/driven/take-a-ride for IDs
      305, 320, 500, 650, 999, TriggerAutoSave (writes dsfforza-garage.dat)
  stock   same save, NO plugin: the game loads it and reaches Story; flags of
      the added IDs read false
  check   plugin again: the flags are back from dsfforza-garage.dat
  garage  out\batch\second-high (31 ported cars at 304..334) plus 600 fake
      vehicleStats entries 360..959 in 60 makers; opens garage 1 and walks
      the maker list with menu keys (screenshots menu-NN.png)
Each run's folder is printed; tools\asi\savecheck.py reads its save\ copy.

.EXAMPLE
.\tools\asi\test\garage-tests.ps1                 # all four
.\tools\asi\test\garage-tests.ps1 -Only garage
.\tools\asi\test\garage-tests.ps1 -Only garage -Vm
#>
param(
    [string[]]$Only = @('set', 'stock', 'check', 'garage'),
    [switch]$Vm
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$series = Join-Path $repo 'tools\wsb\run-series.ps1'
$lua = 'tools\asi\test\lua'
$boot = "bootstrap.lua=$lua\bootstrap.lua;dsfforza\savetest.lua=$lua\savetest.lua"
$mode = @{}
if ($Vm) { $mode.Vm = $true }

# run-series.ps1 queues on a named mutex and starts or replaces the sandbox itself.
$stock = 'out\stock\Install\dngvehicles.sp'
if ($Only -contains 'set') {
    & $series @mode -Archives $stock -ProbeIds 266 -WaitSeconds 240 -ShotSeconds 15 -Asi out\asi\dsfforza.asi `
        -LuaFiles "$boot;dsfforza\savetest_cfg.lua=$lua\cfg_set.lua"
}
if ($Only -contains 'stock') {
    & $series @mode -Archives $stock -ProbeIds 266 -WaitSeconds 240 -ShotSeconds 15 `
        -LuaFiles "$boot;dsfforza\savetest_cfg.lua=$lua\cfg_check.lua"
}
if ($Only -contains 'check') {
    & $series @mode -Archives $stock -ProbeIds 266 -WaitSeconds 240 -ShotSeconds 15 -Asi out\asi\dsfforza.asi `
        -LuaFiles "$boot;dsfforza\savetest_cfg.lua=$lua\cfg_check.lua"
}
if ($Only -contains 'garage') {
    $b = 'out\batch\second-high'
    $keys = @('Enter:3', 'Up:2', 'Up:1.5', 'Up:1.5', 'Enter:3', 'Right:1.5', 'Esc:2') + @(1..60 | ForEach-Object { 'Down:0.35' }) +
        @('Enter:3', 'Right:1.5', 'Esc:2', 'Esc:2')
    & $series @mode -Archives "$b\dngvehicles.sp" -ProbeIds 305 -WaitSeconds 480 -ShotSeconds 15 -Asi out\asi\dsfforza.asi `
        -Override "Install\gui.sp=$b\gui\gui.sp;media\Fonts.fchunk=$b\gui\Fonts.fchunk" `
        -LuaFiles ("$boot;dsfforza\savetest_cfg.lua=$lua\cfg_garage.lua;dsfforza\garagetest_on.lua=$lua\garagetest_on.lua;" +
                   "dsfforza\ported.lua=$b\dngvehicles.lua;dsfforza\hud.lua=$b\gui\hud.lua") `
        -MenuKeys ($keys -join ',')
}
