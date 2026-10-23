<#
.SYNOPSIS
Installs (or removes) Forza cars in Driver: San Francisco (PC 1.04).

.DESCRIPTION
Install:
  1. Builds the cars from your own Forza discs (tools\port_batch.py, Python 3.10+),
     or takes a finished build folder (-Build).
  2. Backs up every game file it replaces to <game>\dsfforza-backup\ (with SHA-256
     hashes): Install\dngvehicles.sp, Install\gui.sp, media\Fonts.fchunk,
     Locale\<language>\global.fchunk and, if present,
     Common\UserLuaScripts\bootstrap.lua.
  3. Copies in the new vehicle archive, the garage pictures (gui.sp), the badge
     font (Fonts.fchunk) and the name strings (global.fchunk).
  4. Installs DriverNGHook 0.7.2 (plugins\DriverNGHook.asi and its scripts) unless
     it is already there, and the Ultimate ASI Loader (dinput8.dll) if no ASI
     loader is present. DriverNGHook needs the Microsoft Visual C++ 2015-2022
     runtime, x86 (32-bit); the script checks for it.
  5. Installs the Lua that registers the cars with the game (names, badges, stat
     bars, garage, traffic): Common\UserLuaScripts\bootstrap.lua plus
     dsfforza\register.lua, ported.lua and hud.lua. No test probes.
  6. With -Asi, installs the dsfforza.asi plugin (plugins\dsfforza.asi and
     dsfforza.ini; default out\asi, built by tools\asi\build.ps1, or -AsiPath):
     runtime patches of Driver.exe 1.04 (no file is changed) that raise the
     archive, garage and save limits (research/asi.md). The ASI loader (step 4)
     loads it, so -SkipHook needs a loader of your own. An archive past the
     stock reader (more than 1100 root entries, or 2 GiB) and -IdPolicy asi
     need it.
  Everything it did is recorded in <game>\dsfforza-backup\install.json.
  dsfforza.asi writes plugins\dsfforza.log (removed by -Uninstall) and keeps
  the save flags of added cars in <savegames>\dsfforza-garage.dat next to
  autosave.dat; that file is save data and stays after -Uninstall (the game
  ignores it).

-Uninstall puts every replaced file back from the backup and checks that its
SHA-256 matches the original, and deletes every file it created (only if it is
still the file it installed). -Status shows what is installed.

The game folder is only changed by Install and Uninstall; the build reads it.

.EXAMPLE
.\tools\install.ps1 -GamePath 'C:\Games\Driver San Francisco' -Iso 'D:\isos\Forza.Horizon.XBOX360.iso','D:\isos\Forza 4 [NTSCU][DVD1].iso','D:\isos\Forza 4 [NTSCU][DVD2].iso'

.EXAMPLE
.\tools\install.ps1 -GamePath 'C:\Games\Driver San Francisco' -Build out\batch\first

.EXAMPLE
.\tools\install.ps1 -GamePath 'C:\Games\Driver San Francisco' -Uninstall
#>
[CmdletBinding(DefaultParameterSetName = 'Install')]
param(
    [Parameter(Mandatory = $true)]
    [string]$GamePath,

    [Parameter(ParameterSetName = 'Install')]
    [string[]]$Iso,                       # Forza disc images (FH1, FM4 disc 1 and 2); default: assets\*.iso
    [Parameter(ParameterSetName = 'Install')]
    [string]$Build,                       # a finished tools\port_batch.py output folder; skips building
    [Parameter(ParameterSetName = 'Install')]
    [string]$List,                        # car list for the build (default tools\batches\first.txt)
    [Parameter(ParameterSetName = 'Install')]
    [string[]]$Car,                       # or single cars, e.g. NIS_370Z_10, BMW_M5_12@fh1
    [Parameter(ParameterSetName = 'Install')]
    [int]$Jobs = 0,                       # build worker processes (default: port_batch's)
    [Parameter(ParameterSetName = 'Install')]
    [ValidateSet('garage', 'high', 'asi')]
    [string]$IdPolicy = 'garage',         # asi: every car from 304 up, garage-listed through dsfforza.asi
    [Parameter(ParameterSetName = 'Install')]
    [string]$BuildDir,                    # where to build (default out\install-build)
    [Parameter(ParameterSetName = 'Install')]
    [string]$HookArchive,                 # DNGHook_072.7z (default: the repo's _ref copy, or -Download)
    [Parameter(ParameterSetName = 'Install')]
    [string]$AsiLoader,                   # dinput8.dll or dinput-Win32.zip of the Ultimate ASI Loader
    [Parameter(ParameterSetName = 'Install')]
    [switch]$Download,                    # fetch DriverNGHook / the ASI loader from GitHub when needed
    [Parameter(ParameterSetName = 'Install')]
    [switch]$SkipHook,                    # don't install DriverNGHook or an ASI loader (you manage them)
    [Parameter(ParameterSetName = 'Install')]
    [switch]$Asi,                         # also install the dsfforza.asi plugin (research/asi.md)
    [Parameter(ParameterSetName = 'Install')]
    [string]$AsiPath,                     # the plugin to install with -Asi (default out\asi\dsfforza.asi)
    [Parameter(ParameterSetName = 'Install')]
    [switch]$WriteProfile,                # let register.lua mark the garage cars unlocked/owned in the save
    [Parameter(ParameterSetName = 'Install')]
    [switch]$Force,                       # reinstall over an earlier install; replace a foreign bootstrap.lua

    [Parameter(ParameterSetName = 'Uninstall', Mandatory = $true)]
    [switch]$Uninstall,
    [Parameter(ParameterSetName = 'Uninstall')]
    [switch]$KeepBackup,

    [Parameter(ParameterSetName = 'Status', Mandatory = $true)]
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$StockArchiveHash = '0DDC885E23028F708A92A2426B643938A6934C99204C780E01755C5B7778AE35'   # Install\dngvehicles.sp 1.04
$HookUrl = 'https://github.com/ReHamster/DriverNGToolkit/releases/download/0.7.2/DNGHook_072.7z'
$AsiLoaderUrl = 'https://github.com/ThirteenAG/Ultimate-ASI-Loader/releases/download/Win32-latest/dinput-Win32.zip'
$VcRedistUrl = 'https://aka.ms/vs/17/release/vc_redist.x86.exe'
# DLL names the Ultimate ASI Loader can be installed as.
$LoaderNames = @('dinput8.dll', 'winmm.dll', 'version.dll', 'd3d9.dll', 'dsound.dll', 'xinput1_3.dll',
                 'wininet.dll', 'winhttp.dll', 'dinput.dll', 'vorbisFile.dll', 'binkw32.dll.asi')

function Say([string]$Text) { Write-Host $Text }
function Fail([string]$Text) { throw $Text }
function Get-Sha256([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }

if (-not (Test-Path -LiteralPath $GamePath -PathType Container)) { Fail "Game folder not found: $GamePath" }
$Game = (Resolve-Path -LiteralPath $GamePath).Path
if (-not (Test-Path -LiteralPath (Join-Path $Game 'Driver.exe'))) { Fail "Driver.exe not found in $Game; -GamePath must be the Driver: San Francisco folder." }
$BackupDir = Join-Path $Game 'dsfforza-backup'
$ManifestPath = Join-Path $BackupDir 'install.json'

function Test-GameRunning {
    foreach ($p in @(Get-Process -Name Driver -ErrorAction SilentlyContinue)) {
        try { if ($p.Path -and $p.Path.StartsWith($Game, [StringComparison]::OrdinalIgnoreCase)) { return $true } }
        catch { return $true }
    }
    return $false
}

# Stock 1.04 SHA-256 of the files the install replaces (English install; other
# languages' global.fchunk are not known here and are not checked).
$StockHashes = @{
    'Install\dngvehicles.sp'       = $StockArchiveHash
    'Install\gui.sp'               = 'AADB62925387307AF7E2BC5091D5E583823E553C23D4E71C5DD6BB71936DA11F'
    'media\Fonts.fchunk'           = '23B1724A878CB9937A764B55F7ADCF0E2D7403C0C63E8504A78566D8EE4602F2'
    'Locale\English\global.fchunk' = 'BA892E734B66A8AA08B98A19CEC32CE1A540792A02205D0AB3FB38BD599A96B8'
}

function Read-Manifest {
    if (-not (Test-Path -LiteralPath $ManifestPath)) { return $null }
    return (Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json)
}

# An install that is in place (or half removed): a manifest not marked uninstalled.
function Test-Installed($Manifest) {
    if (-not $Manifest) { return $false }
    if ($Manifest.PSObject.Properties.Name -contains 'state' -and $Manifest.state -eq 'uninstalled') { return $false }
    return $true
}

function Write-Manifest($Manifest) {
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $temp = "$ManifestPath.tmp"
    $Manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $ManifestPath -Force
}

function Set-Field($Object, [string]$Name, $Value) {
    if ($Object -is [System.Collections.IDictionary]) { $Object[$Name] = $Value; return }
    if ($Object.PSObject.Properties.Name -contains $Name) { $Object.$Name = $Value }
    else { $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value }
}

function Get-Field($Object, [string]$Name) {
    if ($Object -is [System.Collections.IDictionary]) { return $Object[$Name] }
    if ($Object.PSObject.Properties.Name -contains $Name) { return $Object.$Name }
    return $null
}

# Delete a file, retrying while another process holds it. Returns $true when it is gone.
function Remove-FileRetry([string]$Path, [int]$Tries = 5) {
    for ($i = 1; $i -le $Tries; $i++) {
        if (-not (Test-Path -LiteralPath $Path)) { return $true }
        try { Remove-Item -LiteralPath $Path -Force -ErrorAction Stop; return $true }
        catch { if ($i -lt $Tries) { Start-Sleep -Seconds 2 } }
    }
    return -not (Test-Path -LiteralPath $Path)
}

# Remove the backup folder after an uninstall: backups first, install.json last, so
# a folder that can't be removed completely still says "uninstalled". Files another
# process holds open stay (with a warning); the next install reuses or replaces them.
function Remove-BackupFolder {
    if (-not (Test-Path -LiteralPath $BackupDir)) { return $true }
    $left = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $BackupDir -Recurse -File -Force)) {
        if ($file.FullName -eq $ManifestPath) { continue }
        if (-not (Remove-FileRetry $file.FullName)) { $left += $file.FullName.Substring($BackupDir.Length + 1) }
    }
    if ($left.Count) {
        $message = ("  WARNING: could not delete {0} (in use by another process); left in {1}. " -f ($left -join ', '), $BackupDir) +
            'It is not needed any more: delete the folder later, or the next install reuses it.'
        Say $message
        return $false
    }
    [void](Remove-FileRetry $ManifestPath)
    foreach ($dir in @(Get-ChildItem -LiteralPath $BackupDir -Recurse -Directory -Force | Sort-Object { $_.FullName.Length } -Descending)) {
        Remove-Item -LiteralPath $dir.FullName -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $BackupDir -Force -ErrorAction SilentlyContinue
    return -not (Test-Path -LiteralPath $BackupDir)
}

# --- checks ---------------------------------------------------------------------------

function Test-VcRuntimeX86 {
    foreach ($key in @('HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X86',
                       'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X86')) {
        $item = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
        if ($item -and ($item.PSObject.Properties.Name -contains 'Installed') -and $item.Installed -eq 1) { return $true }
    }
    $sys32 = Join-Path $env:WINDIR 'SysWOW64'
    if (-not (Test-Path -LiteralPath $sys32)) { $sys32 = Join-Path $env:WINDIR 'System32' }   # 32-bit Windows
    return ((Test-Path -LiteralPath (Join-Path $sys32 'vcruntime140.dll')) -and
            (Test-Path -LiteralPath (Join-Path $sys32 'msvcp140.dll')))
}

function Find-AsiLoader {
    foreach ($name in $LoaderNames) {
        $path = Join-Path $Game $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $bytes = [IO.File]::ReadAllBytes($path)
        $text = [Text.Encoding]::ASCII.GetString($bytes)
        if ($text.IndexOf('InitializeASI', [StringComparison]::Ordinal) -ge 0 -or
            $text.IndexOf('asi-loader', [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $name }
    }
    return $null
}

function Find-Python {
    foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
        $exe = Get-Command $candidate[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $pyArgs = @()
        if ($candidate.Count -gt 1) { $pyArgs = $candidate[1..($candidate.Count - 1)] }
        $ok = & $exe.Source @pyArgs -c 'import sys; print(int(sys.version_info >= (3, 10)))' 2>$null
        if ($LASTEXITCODE -eq 0 -and "$ok".Trim() -eq '1') { return , (@($exe.Source) + $pyArgs) }
    }
    return $null
}

# --- backup / install bookkeeping ---------------------------------------------------------

$script:Entries = New-Object System.Collections.ArrayList
$script:Manifest = $null   # written after every file, so an interrupted install can be undone

# Copy Source over <game>\Relative, backing up the original first (once). A backup
# already there with the same bytes (left from an earlier install) is reused.
function Install-File([string]$Source, [string]$Relative) {
    $target = Join-Path $Game $Relative
    $entry = [ordered]@{ path = $Relative; action = 'created'; original_sha256 = $null; backup = $null; installed_sha256 = $null }
    if (Test-Path -LiteralPath $target) {
        $backup = Join-Path $BackupDir $Relative
        $entry.original_sha256 = Get-Sha256 $target
        if ((Test-Path -LiteralPath $backup) -and (Get-Sha256 $backup) -eq $entry.original_sha256) {
            Say "  (reusing the backup of $Relative left by an earlier install)"
        } else {
            New-Item -ItemType Directory -Force -Path (Split-Path $backup) | Out-Null
            Copy-Item -LiteralPath $target -Destination $backup -Force
            if ((Get-Sha256 $backup) -ne $entry.original_sha256) { Fail "backup of $Relative does not match the original" }
        }
        $entry.action = 'replaced'
        $entry.backup = $Relative
        # Recorded before the game file changes, so an interruption here can be undone.
        [void]$script:Entries.Add($entry)
        if ($script:Manifest) { Write-Manifest $script:Manifest }
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
        [void]$script:Entries.Add($entry)
    }
    Copy-Item -LiteralPath $Source -Destination $target -Force
    $entry.installed_sha256 = Get-Sha256 $target
    if ($entry.installed_sha256 -ne (Get-Sha256 $Source)) { Fail "copy of $Relative failed (hash mismatch)" }
    if ($script:Manifest) { Write-Manifest $script:Manifest }
    Say ("  {0,-8} {1}" -f $entry.action, $Relative)
}

function Install-Text([string]$Text, [string]$Relative) {
    $temp = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($temp, $Text, (New-Object Text.UTF8Encoding($false)))
        Install-File $temp $Relative
    } finally { Remove-Item -LiteralPath $temp -ErrorAction SilentlyContinue }
}

# --- uninstall ------------------------------------------------------------------------------

# Resumable: each entry is marked done in install.json as soon as it is handled, a
# replaced file that already has its original bytes counts as restored (backup or
# not), and the manifest is marked "uninstalled" before the backup folder goes.
function Invoke-Uninstall([switch]$Keep) {
    $manifest = Read-Manifest
    if (-not (Test-Installed $manifest)) {
        Say "Nothing to uninstall (no install recorded in $ManifestPath)."
        if ((Test-Path -LiteralPath $BackupDir) -and -not $Keep) {
            Say "  removing the leftover $BackupDir"
            [void](Remove-BackupFolder)
        }
        return
    }
    $problems = 0
    $entries = @($manifest.entries)
    [array]::Reverse($entries)
    foreach ($e in $entries) {
        if (Get-Field $e 'done') { continue }
        $target = Join-Path $Game $e.path
        $done = $null
        if ($e.action -eq 'replaced') {
            $backup = Join-Path $BackupDir $e.backup
            if ((Test-Path -LiteralPath $target) -and (Get-Sha256 $target) -eq $e.original_sha256) {
                Say "  original $($e.path) (already restored)"
                $done = 'restored'
            } elseif (-not (Test-Path -LiteralPath $backup)) {
                Say "  MISSING backup for $($e.path), and the game file is not the original"; $problems++
            } elseif ((Get-Sha256 $backup) -ne $e.original_sha256) {
                Say "  backup of $($e.path) is damaged; left as is"; $problems++
            } else {
                Copy-Item -LiteralPath $backup -Destination $target -Force
                if ((Get-Sha256 $target) -ne $e.original_sha256) { Say "  restore of $($e.path) FAILED (hash)"; $problems++ }
                else { Say "  restored $($e.path) (SHA-256 matches the original)"; $done = 'restored' }
            }
        } elseif ($e.action -eq 'created') {
            if (-not (Test-Path -LiteralPath $target)) { Say "  already gone $($e.path)"; $done = 'gone' }
            elseif ($e.installed_sha256 -and (Get-Sha256 $target) -ne $e.installed_sha256) {
                Say "  kept $($e.path): it changed since the install (yours now)"; $done = 'kept'
            } else {
                # (no installed hash: the copy was interrupted; the file didn't exist before)
                Remove-Item -LiteralPath $target -Force
                Say "  removed  $($e.path)"; $done = 'removed'
            }
        }
        if ($done) {
            Set-Field $e 'done' $done
            Write-Manifest $manifest
        }
    }
    # Files the installed plugins write at runtime (not in the entries: they didn't exist at install).
    foreach ($relative in @(Get-Field $manifest 'runtime_files')) {
        if (-not $relative) { continue }
        $full = Join-Path $Game $relative
        if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Force; Say "  removed  $relative (runtime log)" }
    }
    foreach ($dir in @(Get-Field $manifest 'created_dirs')) {
        if (-not $dir) { continue }
        $full = Join-Path $Game $dir
        if ((Test-Path -LiteralPath $full) -and -not (Get-ChildItem -LiteralPath $full -Force | Select-Object -First 1)) {
            Remove-Item -LiteralPath $full -Force
            Say "  removed  $dir\"
        }
    }
    if ($problems) { Fail "$problems file(s) could not be restored; the backup in $BackupDir is kept. Run -Uninstall again after fixing it." }
    Set-Field $manifest 'state' 'uninstalled'
    Set-Field $manifest 'uninstalled' (Get-Date -Format 's')
    Write-Manifest $manifest
    if ($Keep) {
        Say "Uninstalled; every replaced file matches its original SHA-256. Backup kept in $BackupDir."
    } elseif (Remove-BackupFolder) {
        Say 'Uninstalled; every replaced file matches its original SHA-256. Backup folder removed.'
    } else {
        Say 'Uninstalled; every replaced file matches its original SHA-256 (see the warning about the backup folder).'
    }
}

# Before an install: a backup folder with no install in place (an uninstall that
# could not delete everything, or an install that died before writing its record).
# Its files are only safe to drop when the game files are stock; with -Force a game
# file that isn't stock is put back from a leftover backup that is.
function Resolve-LeftoverBackup {
    if (-not (Test-Path -LiteralPath $BackupDir)) { return }
    Say "Found $BackupDir without an install in place:"
    foreach ($file in @(Get-ChildItem -LiteralPath $BackupDir -Recurse -File -Force)) {
        $relative = $file.FullName.Substring($BackupDir.Length + 1)
        if ($relative -like 'install*.json*') { continue }
        $target = Join-Path $Game $relative
        $stock = $StockHashes[$relative]
        $backupHash = Get-Sha256 $file.FullName
        $gameHash = $null
        if (Test-Path -LiteralPath $target) { $gameHash = Get-Sha256 $target }
        if ($gameHash -eq $backupHash) { Say "  $relative = the game's file"; continue }
        if ($stock -and $gameHash -eq $stock) { Say "  $relative : the game's file is stock (backup differs; not needed)"; continue }
        if ($stock -and $backupHash -eq $stock) {
            if (-not $Force) {
                Fail ("$relative in the game is not stock, but $BackupDir holds the stock file. " +
                      "Add -Force to put it back, or copy it back yourself.")
            }
            Copy-Item -LiteralPath $file.FullName -Destination $target -Force
            if ((Get-Sha256 $target) -ne $stock) { Fail "could not restore $relative from the leftover backup" }
            Say "  restored $relative from the leftover backup (-Force)"
            continue
        }
        Say "  $relative : differs from the game's file and isn't a known stock file; left in the backup folder"
    }
    $manifest = Read-Manifest
    if ($manifest) {
        Move-Item -LiteralPath $ManifestPath -Destination ("$ManifestPath." + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.old') -Force
    }
}

# The replaced files must be stock: the build was made from the stock files and the
# backups are what -Uninstall restores. gui.sp, Fonts.fchunk and global.fchunk from
# another mod are replaced (and restored on uninstall) only with -Force.
function Test-StockFiles {
    foreach ($relative in $StockHashes.Keys) {
        $path = Join-Path $Game $relative
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $hash = Get-Sha256 $path
        if ($hash -eq $StockHashes[$relative]) { continue }
        if ($relative -eq 'Install\dngvehicles.sp') {
            Fail ("Install\dngvehicles.sp is not the stock 1.04 archive (SHA-256 $hash). " +
                  "Restore the original (Steam/Uplay: verify files) and run again.")
        }
        if (-not $Force) {
            Fail ("$relative is not the stock 1.04 file (SHA-256 $hash; another mod?). Add -Force to replace it " +
                  "anyway (it is backed up and restored by -Uninstall), or restore the original first.")
        }
        Say "  WARNING: $relative is not stock (another mod?); replacing it (-Force), -Uninstall restores it"
    }
}

# --- status ---------------------------------------------------------------------------------

function Show-Status {
    $manifest = Read-Manifest
    $archive = Join-Path $Game 'Install\dngvehicles.sp'
    $hash = Get-Sha256 $archive
    $state = 'unknown (not the 1.04 stock archive)'
    if ($hash -eq $StockArchiveHash) { $state = 'stock 1.04' }
    elseif ($manifest) {
        foreach ($e in @($manifest.entries)) {
            if ($e.path -eq 'Install\dngvehicles.sp' -and $e.installed_sha256 -eq $hash) { $state = 'dsfforza build (as installed)' }
        }
    }
    Say "Game:      $Game"
    Say "Archive:   $state"
    if (-not (Test-Installed $manifest)) {
        $note = ''
        if (Test-Path -LiteralPath $BackupDir) { $note = " (a leftover $BackupDir is there; the next install or -Uninstall clears it)" }
        Say "dsfforza:  not installed$note"
        return
    }
    $state = Get-Field $manifest 'state'
    if (-not $state) { $state = 'installed' }
    Say "dsfforza:  $state $($manifest.installed) ($(@($manifest.cars).Count) cars, IDs $($manifest.ids))"
    foreach ($e in @($manifest.entries)) {
        $target = Join-Path $Game $e.path
        $now = 'missing'
        if (Test-Path -LiteralPath $target) {
            if ((Get-Sha256 $target) -eq $e.installed_sha256) { $now = 'as installed' } else { $now = 'CHANGED since install' }
        }
        Say ("  {0,-8} {1,-50} {2}" -f $e.action, $e.path, $now)
    }
    $loader = Find-AsiLoader
    if (-not $loader) { $loader = 'none found' }
    Say "ASI loader: $loader"
    if (Test-Path -LiteralPath (Join-Path $Game 'plugins\dsfforza.asi')) {
        $asiLog = Join-Path $Game 'plugins\dsfforza.log'
        $last = '; no plugins\dsfforza.log yet (game not started since)'
        if (Test-Path -LiteralPath $asiLog) { $last = '; plugins\dsfforza.log from ' + (Get-Item -LiteralPath $asiLog).LastWriteTime.ToString('s') }
        Say "dsfforza.asi: present$last"
    }
    $vc = 'missing'
    if (Test-VcRuntimeX86) { $vc = 'present' }
    Say "VC++ x86 runtime: $vc"
}

# --- install ----------------------------------------------------------------------------------

function Get-HookFiles {
    # A folder that holds plugins\DriverNGHook.asi and plugins\DriverNGHook\*.lua.
    $extracted = Join-Path $Repo '_ref\downloads\dnghook-0.7.2\extracted'
    if (-not $HookArchive -and (Test-Path -LiteralPath (Join-Path $extracted 'plugins\DriverNGHook.asi'))) { return $extracted }
    $archive = $HookArchive
    if (-not $archive) {
        $local = Join-Path $Repo '_ref\downloads\dnghook-0.7.2\DNGHook_072.7z'
        if (Test-Path -LiteralPath $local) { $archive = $local }
        elseif ($Download) {
            $archive = Join-Path $env:TEMP 'DNGHook_072.7z'
            Say "Downloading DriverNGHook 0.7.2 from $HookUrl"
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $HookUrl -OutFile $archive -UseBasicParsing
        } else {
            Fail ("DriverNGHook 0.7.2 is needed. Download DNGHook_072.7z from " +
                  "https://github.com/ReHamster/DriverNGToolkit/releases/tag/0.7.2 and pass -HookArchive <path>, or add -Download.")
        }
    }
    $out = Join-Path $env:TEMP ('dsfforza-hook-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $out | Out-Null
    $tar = Join-Path $env:WINDIR 'System32\tar.exe'
    if (-not (Test-Path -LiteralPath $tar)) { Fail "tar.exe (Windows 10 1803 or later) is needed to unpack $archive; unpack it yourself and install DriverNGHook, then use -SkipHook." }
    & $tar -xf $archive -C $out
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $out 'plugins\DriverNGHook.asi'))) { Fail "could not unpack $archive" }
    return $out
}

function Get-AsiLoaderDll {
    $source = $AsiLoader
    if (-not $source) {
        if (-not $Download) {
            Fail ("No ASI loader in the game folder. DriverNGHook needs the Ultimate ASI Loader: download dinput-Win32.zip from " +
                  "https://github.com/ThirteenAG/Ultimate-ASI-Loader/releases (Win32-latest) and pass -AsiLoader <zip or dinput8.dll>, or add -Download.")
        }
        $source = Join-Path $env:TEMP 'dinput-Win32.zip'
        Say "Downloading the Ultimate ASI Loader from $AsiLoaderUrl"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $AsiLoaderUrl -OutFile $source -UseBasicParsing
    }
    if ($source -like '*.zip') {
        $out = Join-Path $env:TEMP ('dsfforza-asi-' + [guid]::NewGuid().ToString('N'))
        Expand-Archive -LiteralPath $source -DestinationPath $out
        $dll = Get-ChildItem -LiteralPath $out -Recurse -Filter 'dinput8.dll' | Select-Object -First 1
        if (-not $dll) { Fail "no dinput8.dll in $source" }
        return $dll.FullName
    }
    return (Resolve-Path -LiteralPath $source).Path
}

# Root entry count of a CHNK archive ('CHNK', size, count, version).
function Get-ArchiveRootCount([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $header = New-Object byte[] 16
        if ($stream.Read($header, 0, 16) -ne 16 -or [Text.Encoding]::ASCII.GetString($header, 0, 4) -ne 'CHNK') { return -1 }
        return [BitConverter]::ToUInt32($header, 8)
    } finally { $stream.Dispose() }
}

function Invoke-Build([string]$RootLimit) {
    $python = Find-Python
    if (-not $python) { Fail 'Python 3.10 or newer is needed to build the cars (https://www.python.org/downloads/), or pass -Build <folder>.' }
    $out = $BuildDir
    if (-not $out) { $out = Join-Path $Repo 'out\install-build' }
    $out = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($out)
    $pyArgs = @()
    if ($python.Count -gt 1) { $pyArgs = $python[1..($python.Count - 1)] }
    $pyArgs += @((Join-Path $Repo 'tools\port_batch.py'), 'build', '--dsf', (Join-Path $Game 'Install\dngvehicles.sp'),
                 '--out', $out, '--gui', '--id-policy', $IdPolicy)
    # dsfforza.asi's archive module reads more than the stock reader's 1100 root entries.
    if ($RootLimit) { $pyArgs += @('--root-limit', $RootLimit) }
    $isos = $Iso
    if (-not $isos) { $isos = @(Get-ChildItem -LiteralPath (Join-Path $Repo 'assets') -Filter '*.iso' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }) }
    if (-not $isos) { Fail 'No Forza disc images: pass -Iso <FH1 iso>,<FM4 disc 1>,<FM4 disc 2> (or put them in assets\).' }
    foreach ($i in $isos) {
        if (-not (Test-Path -LiteralPath $i)) { Fail "disc image not found: $i" }
        $pyArgs += @('--iso', (Resolve-Path -LiteralPath $i).Path)
    }
    if ($Car) { foreach ($c in $Car) { $pyArgs += @('--car', $c) } }
    else {
        $listFile = $List
        if (-not $listFile) { $listFile = Join-Path $Repo 'tools\batches\first.txt' }
        $pyArgs += @('--list', (Resolve-Path -LiteralPath $listFile).Path)
    }
    if ($Jobs -gt 0) { $pyArgs += @('--jobs', "$Jobs") }
    Say "Building the cars (this takes a while; the build runs at Below Normal priority):"
    Say "  $($python[0]) $($pyArgs -join ' ')"
    & $python[0] @pyArgs | Out-Host   # keep the build's output out of the function's return value
    if ($LASTEXITCODE -ne 0) { Fail "the build failed (exit code $LASTEXITCODE); see the output above and $out\batch-report.json" }
    return $out
}

function Invoke-Install {
    $existing = Read-Manifest
    if (Test-Installed $existing) {
        if (-not $Force) { Fail "dsfforza is already installed ($ManifestPath). Run -Uninstall first, or add -Force to reinstall." }
        Say 'Removing the earlier install first (-Force):'
        Invoke-Uninstall
    }
    Resolve-LeftoverBackup
    Test-StockFiles

    $lua = Join-Path $Repo 'tools\game\lua'
    $bootstrapTarget = Join-Path $Game 'Common\UserLuaScripts\bootstrap.lua'
    if ((Test-Path -LiteralPath $bootstrapTarget) -and
        (Get-Sha256 $bootstrapTarget) -ne (Get-Sha256 (Join-Path $lua 'player\bootstrap.lua')) -and -not $Force) {
        Fail "Common\UserLuaScripts\bootstrap.lua belongs to another mod. Add -Force to replace it (it is backed up and restored by -Uninstall)."
    }

    # Hook prerequisites first, so nothing is changed when they are missing.
    $loader = Find-AsiLoader
    $hookInstalled = Test-Path -LiteralPath (Join-Path $Game 'plugins\DriverNGHook.asi')
    if (-not $SkipHook) {
        if (-not (Test-VcRuntimeX86)) {
            Fail ("DriverNGHook needs the Microsoft Visual C++ 2015-2022 Redistributable (x86, 32-bit; the game is 32-bit, " +
                  "so the x64 one is not enough). Install it from $VcRedistUrl and run this again.")
        }
        if (-not $loader -and -not $AsiLoader -and -not $Download) { [void](Get-AsiLoaderDll) }   # explains and stops
        if (-not $hookInstalled) { $hookDir = Get-HookFiles }
    }

    # dsfforza.asi (opt-in, -Asi).
    $asiFile = $null
    if ($Asi -or $AsiPath) {
        $asiFile = $AsiPath
        if (-not $asiFile) { $asiFile = Join-Path $Repo 'out\asi\dsfforza.asi' }
        if (-not (Test-Path -LiteralPath $asiFile)) { Fail "dsfforza.asi not found: $asiFile (build it with tools\asi\build.ps1)" }
        $asiFile = (Resolve-Path -LiteralPath $asiFile).Path
    }
    if ($IdPolicy -eq 'asi' -and -not $asiFile) {
        Fail '-IdPolicy asi needs the dsfforza.asi plugin: add -Asi (without it cars at 304+ are never in the garage).'
    }

    $buildFolder = $Build
    if (-not $buildFolder) {
        $rootLimit = $null
        if ($asiFile) { $rootLimit = '3003' }   # every base ID below 1000 (3 entries each) plus SSIC, DMWK, BINF
        $buildFolder = Invoke-Build $rootLimit
    }
    $buildFolder = (Resolve-Path -LiteralPath $buildFolder).Path
    $archive = Join-Path $buildFolder 'dngvehicles.sp'
    $buildManifestPath = Join-Path $buildFolder 'dngvehicles.json'
    $portedLua = Join-Path $buildFolder 'dngvehicles.lua'
    foreach ($f in @($archive, $buildManifestPath, $portedLua)) { if (-not (Test-Path -LiteralPath $f)) { Fail "build folder incomplete: $f missing" } }
    # The stock reader holds 1100 root entries and seeks below 2 GiB; more needs the plugin's archive module.
    $rootEntries = Get-ArchiveRootCount $archive
    $archiveBytes = (Get-Item -LiteralPath $archive).Length
    if (-not $asiFile -and ($rootEntries -gt 1100 -or $archiveBytes -ge 2GB)) {
        Fail ("the archive has $rootEntries root entries and $([math]::Round($archiveBytes / 1MB)) MB; the stock game reads at most " +
              "1100 entries and 2 GiB. Add -Asi to install the dsfforza.asi plugin with it (tools\asi\build.ps1).")
    }
    $buildManifest = Get-Content -LiteralPath $buildManifestPath -Raw | ConvertFrom-Json
    if (($buildManifest.PSObject.Properties.Name -contains 'stock_sha256') -and $buildManifest.stock_sha256 -and
        $buildManifest.stock_sha256 -ne $StockArchiveHash) { Fail 'the build was made from a different stock archive' }
    $ids = @($buildManifest.cars | ForEach-Object { $_.spool_id })
    Say "Installing $($ids.Count) cars (IDs $($ids -join ', ')) into $Game"

    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $createdDirs = New-Object System.Collections.ArrayList
    foreach ($d in @('plugins', 'plugins\DriverNGHook', 'Common\UserLuaScripts', 'Common\UserLuaScripts\dsfforza')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Game $d))) { [void]$createdDirs.Insert(0, $d) }
    }
    $manifest = [ordered]@{
        state = 'installing'; installed = (Get-Date -Format 's'); build = $buildFolder; ids = ($ids -join ',')
        cars = @($buildManifest.cars | ForEach-Object { $_.media })
        entries = $script:Entries; created_dirs = $createdDirs; runtime_files = @()
    }
    $script:Manifest = $manifest
    Write-Manifest $manifest
    try {
        Install-File $archive 'Install\dngvehicles.sp'
        $gui = Join-Path $buildFolder 'gui'
        if (Test-Path -LiteralPath (Join-Path $gui 'gui.sp')) { Install-File (Join-Path $gui 'gui.sp') 'Install\gui.sp' }
        if (Test-Path -LiteralPath (Join-Path $gui 'Fonts.fchunk')) { Install-File (Join-Path $gui 'Fonts.fchunk') 'media\Fonts.fchunk' }
        # Name strings for the garage (gui_assets: Locale\<language>\global.fchunk with "ID:7xxxxx" entries).
        if (Test-Path -LiteralPath (Join-Path $gui 'Locale')) {
            foreach ($file in Get-ChildItem -LiteralPath (Join-Path $gui 'Locale') -Recurse -File -Filter 'global.fchunk') {
                $relative = 'Locale\' + $file.FullName.Substring((Join-Path $gui 'Locale').Length).TrimStart('\')
                if (Test-Path -LiteralPath (Join-Path $Game $relative)) { Install-File $file.FullName $relative }
            }
        }

        if (-not $SkipHook) {
            if (-not $loader) {
                Install-File (Get-AsiLoaderDll) 'dinput8.dll'
            } else { Say "  ASI loader already present ($loader)" }
            if (-not $hookInstalled) {
                foreach ($file in Get-ChildItem -LiteralPath (Join-Path $hookDir 'plugins') -Recurse -File) {
                    $relative = 'plugins\' + $file.FullName.Substring((Join-Path $hookDir 'plugins').Length).TrimStart('\')
                    Install-File $file.FullName $relative
                }
            } else { Say '  DriverNGHook already present (left as is)' }
        }

        if ($asiFile) {
            Install-File $asiFile 'plugins\dsfforza.asi'
            $asiIni = [IO.Path]::ChangeExtension($asiFile, '.ini')
            if (-not (Test-Path -LiteralPath $asiIni)) { $asiIni = Join-Path $Repo 'tools\asi\dsfforza.ini' }
            Install-File $asiIni 'plugins\dsfforza.ini'
            $manifest.runtime_files = @('plugins\dsfforza.log')
        }

        Install-File (Join-Path $lua 'player\bootstrap.lua') 'Common\UserLuaScripts\bootstrap.lua'
        Install-File (Join-Path $lua 'dsfforza\register.lua') 'Common\UserLuaScripts\dsfforza\register.lua'
        Install-File $portedLua 'Common\UserLuaScripts\dsfforza\ported.lua'
        if (Test-Path -LiteralPath (Join-Path $gui 'hud.lua')) { Install-File (Join-Path $gui 'hud.lua') 'Common\UserLuaScripts\dsfforza\hud.lua' }
        if ($WriteProfile) {
            Install-Text "-- written by tools\install.ps1 -WriteProfile`nreturn { register = { writeProfile = true } }`n" 'Common\UserLuaScripts\dsfforza\config.lua'
        }
        $manifest.state = 'installed'
    } finally {
        $manifest.entries = $script:Entries
        Write-Manifest $manifest
    }
    if (-not $SkipHook -and -not (Test-VcRuntimeX86)) { Say "WARNING: install the VC++ x86 runtime: $VcRedistUrl" }
    Say "Done. Start the game as usual. Uninstall: .\tools\install.ps1 -GamePath '$Game' -Uninstall"
}

if (Test-GameRunning) { Fail 'Driver.exe is running from this folder; close the game first.' }
switch ($PSCmdlet.ParameterSetName) {
    'Status' { Show-Status }
    'Uninstall' { Invoke-Uninstall -Keep:$KeepBackup }
    default { Invoke-Install }
}
