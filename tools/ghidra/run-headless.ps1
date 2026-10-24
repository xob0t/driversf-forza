<#
.SYNOPSIS
Runs one Ghidra post-script against the existing DriverSF database, gently.

.DESCRIPTION
Opens the analyzed Driver.exe with analysis disabled, read-only unless -Save
is given. The database is a copy of the driver-sf-mods project, so saving
never touches the original. It lives outside this repo because Ghidra rejects
project paths with a dot-prefixed folder such as `.t3`. With no -Script, -Save just
opens and saves the program, which persists Ghidra version upgrades. While Ghidra
runs, this PowerShell process drops to Idle priority and is pinned to the
processors in -Affinity; Java and the decompiler inherit both at creation.
The shell's original settings are restored afterwards. Output goes to
out/ghidra/<script>.log.

.EXAMPLE
.\tools\ghidra\run-headless.ps1 FindVehicleLookup.java out\ghidra\vehicle-lookup
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Position = 0)] [string]$Script,
    [Parameter(ValueFromRemainingArguments)] [string[]]$ScriptArgs = @(),
    [switch]$Save,
    [string]$GhidraRoot = $env:GHIDRA_INSTALL_DIR,
    [string]$JavaHome = $env:JAVA_HOME,
    # Folder with the analyzed Driver.exe project (<ProjectName>.gpr)
    [string]$ProjectRoot = $env:DSF_GHIDRA_PROJECT,
    [string]$ProjectName = 'DriverSF',
    # Processor mask; 0xF000 = logical processors 12-15.
    [long]$Affinity = 0xF000
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $GhidraRoot) { throw 'Pass -GhidraRoot or set GHIDRA_INSTALL_DIR.' }
if (-not $JavaHome) { throw 'Pass -JavaHome or set JAVA_HOME.' }
if (-not $ProjectRoot) { throw 'Pass -ProjectRoot or set DSF_GHIDRA_PROJECT.' }
$headless = Join-Path $GhidraRoot 'support\analyzeHeadless.bat'
foreach ($path in $headless, (Join-Path $JavaHome 'bin\java.exe'), (Join-Path $ProjectRoot "$ProjectName.gpr")) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Not found: $path" }
}

if (-not $Script -and -not $Save) { throw 'Give a -Script, or -Save to persist upgrades.' }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

$logDir = Join-Path $repo 'out\ghidra'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logName = if ($Script) { [IO.Path]::GetFileNameWithoutExtension($Script) } else { 'save' }
$log = Join-Path $logDir "$logName.log"

$ghidraArgs = @($ProjectRoot, $ProjectName, '-process', 'Driver.exe', '-noanalysis', '-scriptPath', $PSScriptRoot)
if (-not $Save) { $ghidraArgs += '-readOnly' }
if ($Script) {
    $ghidraArgs += '-postScript', $Script
    # Script arguments that look like relative paths are resolved against the repo.
    $ghidraArgs += @(foreach ($arg in $ScriptArgs) {
        if ($arg -match '^(out|research)[\\/]') { Join-Path $repo $arg } else { $arg }
    })
}

$self = [Diagnostics.Process]::GetCurrentProcess()
$savedPriority = $self.PriorityClass
$savedAffinity = $self.ProcessorAffinity
$env:JAVA_HOME = $JavaHome
$started = Get-Date
try {
    $self.ProcessorAffinity = [IntPtr]$Affinity
    $self.PriorityClass = 'Idle'
    & $headless @ghidraArgs *> $log
    $exit = $LASTEXITCODE
}
finally {
    $self.PriorityClass = $savedPriority
    $self.ProcessorAffinity = $savedAffinity
}
Write-Host ("{0} finished in {1:N0}s with exit code {2}; log: {3}" -f $logName, ((Get-Date) - $started).TotalSeconds, $exit, $log)
exit $exit
