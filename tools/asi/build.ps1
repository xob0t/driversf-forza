<#
.SYNOPSIS
Builds out\asi\dsfforza.asi (x86, MSVC 2022) and copies dsfforza.ini next to it.

.DESCRIPTION
Compiles every tools\asi\src\*.cpp into one DLL named dsfforza.asi. Static CRT
(/MT), so the guest needs no VC++ redistributable. /Brepro and sorted inputs
make the output byte-identical for identical sources and compiler version.
Runs the compiler at Idle priority (out\gentle policy).

.EXAMPLE
.\tools\asi\build.ps1
#>
[CmdletBinding()]
param(
    [string]$VcVars = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars32.bat',
    [switch]$DebugBuild,
    # Also build out\asi\offline.exe (test\offline.cpp + the modules) and run it
    # against Driver.exe: maps the image at 0x00400000 and applies every patch.
    [switch]$Test,
    [string]$Exe = 'D:\Games\Driver San Francisco\Driver.exe'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$src = Join-Path $PSScriptRoot 'src'
$out = Join-Path $repo 'out\asi'
$obj = Join-Path $out 'obj'
if (-not (Test-Path -LiteralPath $VcVars)) {
    $vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path $vswhere) {
        $vs = & $vswhere -latest -products * -property installationPath
        $VcVars = Join-Path $vs 'VC\Auxiliary\Build\vcvars32.bat'
    }
    if (-not (Test-Path -LiteralPath $VcVars)) { throw "vcvars32.bat not found: $VcVars" }
}
New-Item -ItemType Directory -Force -Path $out, $obj | Out-Null

$sources = Get-ChildItem -LiteralPath $src -Filter *.cpp | Sort-Object Name | ForEach-Object { '"' + $_.FullName + '"' }
$opt = if ($DebugBuild) { '/Od /Zi' } else { '/O2 /Oy-' }
$cl = "cl /nologo /c /std:c++17 /EHsc- /GR- /GS- /MT /W3 /Brepro $opt /DWIN32 /D_WINDOWS /DNDEBUG /Fo`"$obj\\`" " + ($sources -join ' ')
$objs = Get-ChildItem -LiteralPath $src -Filter *.cpp | Sort-Object Name | ForEach-Object { '"' + (Join-Path $obj ($_.BaseName + '.obj')) + '"' }
$link = "link /nologo /DLL /Brepro /MACHINE:X86 /SAFESEH:NO /DYNAMICBASE:NO /INCREMENTAL:NO /OUT:`"$out\dsfforza.asi`" /DEBUG:NONE " + ($objs -join ' ') + ' kernel32.lib user32.lib'
$cmd = "`"$VcVars`" >nul 2>nul && $cl && $link"

$self = [Diagnostics.Process]::GetCurrentProcess()
$saved = $self.PriorityClass
try {
    $self.PriorityClass = 'Idle'
    Get-ChildItem -LiteralPath $obj -Filter *.obj | Remove-Item -Force
    cmd /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "build failed ($LASTEXITCODE)" }
} finally {
    $self.PriorityClass = $saved
}
if ($Test) {
    $tobj = Join-Path $out 'testobj'
    New-Item -ItemType Directory -Force -Path $tobj | Out-Null
    $tsrc = @(Get-ChildItem -LiteralPath $src -Filter *.cpp | Where-Object Name -ne 'main.cpp' | Sort-Object Name) +
        @(Get-Item -LiteralPath (Join-Path $PSScriptRoot 'test\offline.cpp'))
    $tcl = "cl /nologo /c /std:c++17 /EHsc- /GR- /GS- /MT /W3 /O2 /Oy- /DWIN32 /DNDEBUG /Fo`"$tobj\\`" " + (($tsrc | ForEach-Object { '"' + $_.FullName + '"' }) -join ' ')
    $tlink = "link /nologo /MACHINE:X86 /SAFESEH:NO /BASE:0x20000000 /DYNAMICBASE:NO /INCREMENTAL:NO /OUT:`"$out\offline.exe`" " + (($tsrc | ForEach-Object { '"' + (Join-Path $tobj ($_.BaseName + '.obj')) + '"' }) -join ' ') + ' kernel32.lib user32.lib'
    cmd /c "`"$VcVars`" >nul 2>nul && $tcl && $tlink"
    if ($LASTEXITCODE -ne 0) { throw "test build failed ($LASTEXITCODE)" }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'dsfforza.ini') -Destination (Join-Path $out 'dsfforza.ini') -Force
$asi = Get-Item -LiteralPath (Join-Path $out 'dsfforza.asi')
$hash = (Get-FileHash -LiteralPath $asi.FullName -Algorithm SHA256).Hash
Write-Host ("built {0} ({1} bytes, SHA-256 {2})" -f $asi.FullName, $asi.Length, $hash)
if ($Test) {
    & (Join-Path $out 'offline.exe') $Exe
    if ($LASTEXITCODE -ne 0) { throw "offline test failed ($LASTEXITCODE)" }
}
