<#
.SYNOPSIS
Builds out\re\lampprobe.asi (x86, MSVC 2022, static CRT), the research probe of
research/lights-re.md. Not part of the release; loaded only in test runs.

.EXAMPLE
.\tools\re\lampprobe\build.ps1
#>
[CmdletBinding()]
param(
    [string]$VcVars = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars32.bat'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$out = Join-Path $repo 'out\re'
if (-not (Test-Path -LiteralPath $VcVars)) {
    $vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
    $vs = & $vswhere -latest -products * -property installationPath
    $VcVars = Join-Path $vs 'VC\Auxiliary\Build\vcvars32.bat'
}
New-Item -ItemType Directory -Force -Path $out | Out-Null
$src = Join-Path $PSScriptRoot 'lampprobe.cpp'
$cmd = "`"$VcVars`" >nul 2>nul && cl /nologo /LD /O2 /MT /W3 /GS- /EHa /Fo`"$out\\`" /Fe`"$out\lampprobe.asi`" `"$src`" /link /DYNAMICBASE:NO /SAFESEH:NO kernel32.lib"
$self = [Diagnostics.Process]::GetCurrentProcess()
$saved = $self.PriorityClass
try {
    $self.PriorityClass = 'Idle'
    cmd /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "build failed ($LASTEXITCODE)" }
} finally {
    $self.PriorityClass = $saved
}
Get-Item (Join-Path $out 'lampprobe.asi')
