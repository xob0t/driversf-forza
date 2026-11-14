<#
.SYNOPSIS
Builds out\d3dlog\d3dlog.asi (x86, MSVC), the Direct3D 9 logging plugin for test runs (d3dlog.cpp;
research/in-game-tests.md, "Guest GPU memory").
#>
param([string]$VcVars = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars32.bat')
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $VcVars) -and (Test-Path $vswhere)) {
    $VcVars = Join-Path (& $vswhere -latest -products * -property installationPath) 'VC\Auxiliary\Build\vcvars32.bat'
}
if (-not (Test-Path -LiteralPath $VcVars)) { throw "vcvars32.bat not found: $VcVars" }
$out = (New-Item -ItemType Directory -Force -Path (Join-Path $repo 'out\d3dlog')).FullName
$src = Join-Path $PSScriptRoot 'd3dlog.cpp'
Push-Location $out
try { cmd /c "`"$VcVars`" >nul 2>nul && cl /nologo /O2 /MT /W3 /LD `"$src`" /link /OUT:d3dlog.asi user32.lib" }
finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw "build failed ($LASTEXITCODE)" }
"built $out\d3dlog.asi"
