<#
.SYNOPSIS
Installs and removes dsfforza test files in the Driver: San Francisco folder.

.DESCRIPTION
Every change is reversible:

  Status            show what is installed
  InstallArchive    back up the stock Install\dngvehicles.sp once (to
                    dngvehicles.sp.stock, only if it matches the stock hash),
                    then copy -Archive over it
  RestoreArchive    copy the stock backup back and verify its hash
  InstallHook       copy DriverNGHook 0.7.2 (plugins\DriverNGHook.asi and its
                    scripts) into plugins\
  RemoveHook        delete exactly the files InstallHook copied
  InstallProbe      copy the Lua bootstrap and clone test into
                    Common\UserLuaScripts\ (refuses to replace a different
                    bootstrap.lua)
  RemoveProbe       delete exactly the files InstallProbe copied

.EXAMPLE
.\tools\game\dsf-test.ps1 InstallArchive -Archive out\dngvehicles.clone.sp
.\tools\game\dsf-test.ps1 InstallHook
.\tools\game\dsf-test.ps1 InstallProbe
# play, then read Driver.log in the game folder for [dsfforza] lines
.\tools\game\dsf-test.ps1 RemoveProbe; .\tools\game\dsf-test.ps1 RemoveHook; .\tools\game\dsf-test.ps1 RestoreArchive
#>
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('Status', 'InstallArchive', 'RestoreArchive', 'InstallHook', 'RemoveHook', 'InstallProbe', 'RemoveProbe')]
    [string]$Action,
    [string]$Archive,
    [string]$GamePath = 'D:\Games\Driver San Francisco'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$stockHash = '0DDC885E23028F708A92A2426B643938A6934C99204C780E01755C5B7778AE35'
$archivePath = Join-Path $GamePath 'Install\dngvehicles.sp'
$backupPath = "$archivePath.stock"
$hookSource = Join-Path $repo '_ref\downloads\dnghook-0.7.2\extracted\plugins'
$probeSource = Join-Path $PSScriptRoot 'lua'
$userLua = Join-Path $GamePath 'Common\UserLuaScripts'

if (-not (Test-Path -LiteralPath (Join-Path $GamePath 'Driver.exe'))) { throw "Driver.exe not found in $GamePath" }
if (Get-Process -Name Driver -ErrorAction SilentlyContinue) {
    if ($Action -ne 'Status') { throw 'Driver.exe is running; close the game first.' }
}

function Get-Hash([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }

# Copies a source tree into the game and records each destination in a manifest.
function Install-Tree([string]$Source, [string]$Destination, [string]$Manifest) {
    $installed = @()
    foreach ($file in Get-ChildItem -LiteralPath $Source -Recurse -File) {
        $relative = $file.FullName.Substring($Source.Length).TrimStart('\')
        $target = Join-Path $Destination $relative
        if (Test-Path -LiteralPath $target) {
            if ((Get-Hash $target) -ne (Get-Hash $file.FullName)) {
                throw "Refusing to replace existing $target (it differs from ours)."
            }
            "already present $target (left out of the manifest)"
            continue
        }
        New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target
        $installed += $target
    }
    Set-Content -LiteralPath $Manifest -Encoding UTF8 -Value $installed
    $installed | ForEach-Object { "installed $_" }
}

function Remove-Tree([string]$Manifest) {
    if (-not (Test-Path -LiteralPath $Manifest)) { "nothing to remove ($Manifest missing)"; return }
    foreach ($target in Get-Content -LiteralPath $Manifest) {
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target; "removed $target" }
    }
    Remove-Item -LiteralPath $Manifest
}

$hookManifest = Join-Path $GamePath 'plugins\dsfforza-hook.manifest'
$probeManifest = Join-Path $userLua 'dsfforza-probe.manifest'

switch ($Action) {
    'Status' {
        $hash = Get-Hash $archivePath
        "archive:  $(if ($hash -eq $stockHash) { 'stock' } else { "modified ($hash)" })"
        "backup:   $(if (Test-Path -LiteralPath $backupPath) { $backupPath } else { 'none' })"
        "hook:     $(if (Test-Path -LiteralPath $hookManifest) { 'installed' } else { 'not installed' })"
        "probe:    $(if (Test-Path -LiteralPath $probeManifest) { 'installed' } else { 'not installed' })"
        "running:  $([bool](Get-Process -Name Driver -ErrorAction SilentlyContinue))"
    }
    'InstallArchive' {
        if (-not $Archive) { throw '-Archive is required' }
        if (-not [IO.Path]::IsPathRooted($Archive)) { $Archive = Join-Path $repo $Archive }
        if (-not (Test-Path -LiteralPath $Archive)) { throw "archive not found: $Archive" }
        if (-not (Test-Path -LiteralPath $backupPath)) {
            if ((Get-Hash $archivePath) -ne $stockHash) { throw "$archivePath is not stock and no backup exists; not touching it." }
            Copy-Item -LiteralPath $archivePath -Destination $backupPath
            "backed up stock archive to $backupPath"
        }
        Copy-Item -LiteralPath $Archive -Destination $archivePath -Force
        "installed $Archive -> $archivePath"
    }
    'RestoreArchive' {
        if (-not (Test-Path -LiteralPath $backupPath)) { throw "no backup at $backupPath" }
        if ((Get-Hash $backupPath) -ne $stockHash) { throw "backup hash is wrong; restore it by hand" }
        Copy-Item -LiteralPath $backupPath -Destination $archivePath -Force
        if ((Get-Hash $archivePath) -ne $stockHash) { throw 'restore failed: hash mismatch' }
        "restored stock archive (backup kept at $backupPath)"
    }
    'InstallHook' { Install-Tree $hookSource (Join-Path $GamePath 'plugins') $hookManifest }
    'RemoveHook' { Remove-Tree $hookManifest }
    'InstallProbe' { Install-Tree $probeSource $userLua $probeManifest }
    'RemoveProbe' { Remove-Tree $probeManifest }
}
