<#
.SYNOPSIS
Installs (or removes) Forza cars in Driver: San Francisco (PC 1.04).

.DESCRIPTION
The cars are installed as car packages, one file per car beside the game's own files
(research/car-packages.md). No game file is replaced: Install\dngvehicles.sp, gui.sp,
Fonts.fchunk and global.fchunk stay as they are, another mod's included.

Install:
  1. Builds the cars from your own Forza discs (`uv run dsff port_batch --packages`; uv
     installs Python 3.13), or takes a finished build folder (-Build) with its car
     packages (<build>\packages\*.dsfcar).
  2. Installs DriverNGHook 0.7.2 (plugins\DriverNGHook.asi and its scripts) unless
     it is already there, and the Ultimate ASI Loader (dinput8.dll) if no ASI
     loader is present. DriverNGHook needs the Microsoft Visual C++ 2015-2022
     runtime, x86 (32-bit); the script checks for it.
  3. Installs the dsfforza.asi plugin (plugins\dsfforza.asi, and dsfforza.ini with
     [archive] packages=1; -AsiPath, default out\asi, built by tools\asi\build.ps1, or a
     release's plugin\): runtime patches of Driver.exe 1.04 (no file is changed) that load
     the car packages and raise the garage and save limits (research/asi.md). The ASI
     loader (step 2) loads it, so -SkipHook needs a loader of your own.
  4. Installs the Lua that registers the cars with the game (names, badges, stat
     bars, garage, traffic): Common\UserLuaScripts\bootstrap.lua plus
     dsfforza\register.lua. No test probes. Another mod's bootstrap.lua (replaced
     only with -Force, backed up to <game>\dsfforza-backup\) is kept as
     dsfforza\chained-bootstrap.lua, which ours runs first.
  5. Hands the packages to the car manager (dsff car_manager), which writes
     dsfforza\cars\ (the cars' own files, stable IDs), dsfforza\generated\ (copies
     of gui.sp, Fonts.fchunk and global.fchunk the plugin opens in place of the
     game's) and dsfforza\packages.lua / packages_hud.lua.
  Everything it did is recorded in <game>\dsfforza-backup\install.json.
  dsfforza.asi writes plugins\dsfforza.log (removed by -Uninstall) and keeps
  the save flags of added cars in <savegames>\dsfforza-garage.dat next to
  autosave.dat; that file is save data and stays after -Uninstall (the game
  ignores it).

-Force over an earlier install removes it first.

Over a merged install (what install.ps1 did before car packages: the cars merged into
copies of dngvehicles.sp, gui.sp, Fonts.fchunk and global.fchunk, with ported.lua and
hud.lua) the install migrates, -Force or not: it records that install's car IDs in the
manager's key -> ID map (car_manager seed; the save keeps its flags per model ID),
removes it (every replaced game file restored from its backup), deletes ported.lua and
hud.lua, and installs the packages, which take the same IDs.

-Uninstall has the car manager remove its files but its key -> ID map
(dsfforza\cars\manager.json), so a later install gives every car the ID the save's
flags are for. Then it puts every replaced file back from the backup, checks that its
SHA-256 matches the original, and deletes every file it created (only if it is still
the file it installed). It removes a merged install the same way. -Status shows what
is installed.

The game folder is only changed by Install and Uninstall; the build reads it.

.EXAMPLE
.\tools\install.ps1 -GamePath '<game folder>' -Iso '<FH1.iso>','<FM4 disc 1.iso>','<FM4 disc 2.iso>'

.EXAMPLE
.\tools\install.ps1 -GamePath '<game folder>' -Build out\batch\first

.EXAMPLE
.\tools\install.ps1 -GamePath '<game folder>' -Uninstall
#>
[CmdletBinding(DefaultParameterSetName = 'Install')]
param(
    [Parameter(Mandatory = $true)]
    [string]$GamePath,

    [Parameter(ParameterSetName = 'Install')]
    [string[]]$Iso,                       # Forza disc images (FH1, FM4 disc 1 and 2); default: assets\*.iso
    [Parameter(ParameterSetName = 'Install')]
    [string]$Build,                       # a finished port_batch build folder; skips building
    [Parameter(ParameterSetName = 'Install')]
    [string]$List,                        # car list for the build (default tools\batches\first.txt)
    [Parameter(ParameterSetName = 'Install')]
    [string[]]$Car,                       # or single cars, e.g. NIS_370Z_10, BMW_M5_12@fh1
    [Parameter(ParameterSetName = 'Install')]
    [int]$Jobs = 0,                       # build worker processes (default: port_batch's)
    [Parameter(ParameterSetName = 'Install')]
    [string]$BuildDir,                    # where to build (default out\install-build)
    [Parameter(ParameterSetName = 'Install')]
    [string]$HookArchive,                 # DNGHook_072.7z or its unpacked folder (default: the repo's _ref copy, or -Download)
    [Parameter(ParameterSetName = 'Install')]
    [string]$AsiLoader,                   # dinput8.dll of the Ultimate ASI Loader, or a zip holding it
    [Parameter(ParameterSetName = 'Install')]
    [switch]$Download,                    # fetch DriverNGHook / the ASI loader from GitHub when needed
    [Parameter(ParameterSetName = 'Install')]
    [switch]$SkipHook,                    # don't install DriverNGHook or an ASI loader (you manage them)
    [Parameter(ParameterSetName = 'Install')]
    [string]$AsiPath,                     # the dsfforza.asi plugin (default out\asi\dsfforza.asi, else a release's plugin\dsfforza.asi)
    [Parameter(ParameterSetName = 'Install')]
    [switch]$WriteProfile,                # let register.lua mark the garage cars unlocked/owned in the save
    [Parameter(ParameterSetName = 'Install')]
    [switch]$Force,                      # reinstall over an earlier package install; replace a foreign bootstrap.lua

    [Parameter(ParameterSetName = 'Uninstall', Mandatory = $true)]
    [switch]$Uninstall,
    [Parameter(ParameterSetName = 'Uninstall')]
    [switch]$KeepBackup,

    [Parameter(ParameterSetName = 'Status', Mandatory = $true)]
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

# Windows PowerShell 5.1 started from PowerShell 7 through another process (install.cmd from a
# pwsh terminal, dsff install from one, CI's pwsh shell) inherits pwsh's PSModulePath: 5.1 then
# autoloads PowerShell 7's Microsoft.PowerShell.Utility and Management (7.0.0.0, first on the
# path) and Get-FileHash, Invoke-WebRequest, ... are missing. pwsh cleans the path only for a
# powershell.exe it starts itself. So under Windows PowerShell the PowerShell 7 module folders go
# (pwsh's own $PSHOME\Modules, beside pwsh.exe, and the ...\PowerShell\Modules folders of
# Program Files and Documents; Windows PowerShell's are ...\WindowsPowerShell\Modules), for this
# script and the processes it starts. .NET only: a cmdlet here could autoload the wrong module.
if ($PSVersionTable.PSEdition -eq 'Desktop' -and $env:PSModulePath) {
    $env:PSModulePath = (@($env:PSModulePath.Split(';') | Where-Object {
        $folder = $_.Trim().TrimEnd('\')
        if (-not $folder) { return $false }
        if ($folder -match '\\PowerShell\\Modules$') { return $false }
        try { $home7 = [IO.Path]::GetDirectoryName($folder) } catch { return $true }   # (not a path: left as is)
        -not ($home7 -and [IO.File]::Exists([IO.Path]::Combine($home7, 'pwsh.exe')))
    }) -join ';')
}

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$VanillaArchiveHash = '0DDC885E23028F708A92A2426B643938A6934C99204C780E01755C5B7778AE35'   # Install\dngvehicles.sp 1.04
# -Download fetches these pinned releases (the same as src/dsfforza/build/thirdparty.py) into
# out\tools-dl and checks their SHA-256.
$HookUrl = 'https://github.com/ReHamster/DriverNGToolkit/releases/download/0.7.2/DNGHook_072.7z'
$HookSha256 = 'C2AE54B0A7F0FD2D00CBCFE9ED721017F8BA84D1FEC618D53FC99F4C78BC3056'
$AsiLoaderUrl = 'https://github.com/ThirteenAG/Ultimate-ASI-Loader/releases/download/v9.7.4/Ultimate-ASI-Loader-NoPDB.zip'
$AsiLoaderSha256 = '14B3A1AD018899571AC9AA01482977F3C6D49E6CBA99F552D01C5ACACD1315E1'
$VcRedistUrl = 'https://aka.ms/vs/17/release/vc_redist.x86.exe'
# DLL names the Ultimate ASI Loader can be installed as.
$LoaderNames = @('dinput8.dll', 'winmm.dll', 'version.dll', 'd3d9.dll', 'dsound.dll', 'xinput1_3.dll',
                 'wininet.dll', 'winhttp.dll', 'dinput.dll', 'vorbisFile.dll', 'binkw32.dll.asi')

function Say([string]$Text) { Write-Host $Text }
function Fail([string]$Text) { throw $Text }
function Get-Sha256([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }

# A build's dngvehicles.json has Forza part names as keys that differ only in case
# (glassLR / glasslR), which ConvertFrom-Json rejects. JavaScriptSerializer is
# case-sensitive: objects come back as dictionaries, arrays as object[].
function Read-BuildManifest([string]$Path) {
    Add-Type -AssemblyName System.Web.Extensions
    $serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $serializer.MaxJsonLength = [int]::MaxValue
    $serializer.RecursionLimit = 1000
    return $serializer.DeserializeObject([IO.File]::ReadAllText($Path))
}

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

# Vanilla 1.04 SHA-256 of the files a merged install (an earlier release's) replaced, for its
# leftover backups (Resolve-LeftoverBackup). English install; other languages' global.fchunk
# are not known here and are not checked.
$VanillaHashes = @{
    'Install\dngvehicles.sp'       = $VanillaArchiveHash
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

# One of our own bootstraps, any version: every player bootstrap install.ps1 has installed
# (tools\game\lua\player\bootstrap.lua, from its first commit) and the test ones start with
# one of these lines. A mention of dsfforza doesn't count: another mod's bootstrap may
# load register.lua itself (INSTALL.md).
$OurBootstrapHeaders = @(
    '-- dsfforza player bootstrap: registers the added Forza cars with the game.',
    '-- DriverNGHook user bootstrapper for dsfforza tests.',
    '-- Test bootstrap for dsfforza.asi garage/save runs (tools/asi/test): the')
function Test-OurBootstrap([string]$Path) {
    $first = @([IO.File]::ReadAllLines($Path) | Where-Object { $_.Trim() } | Select-Object -First 1)
    if (-not $first) { return $false }
    return $OurBootstrapHeaders -contains $first[0].TrimStart([char]0xFEFF).Trim()
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

# A pinned download into out\tools-dl\<Folder> (kept there), or the copy already there.
function Get-PinnedDownload([string]$Url, [string]$Sha256, [string]$Folder) {
    $target = Join-Path (Join-Path $Repo "out\tools-dl\$Folder") ($Url.Split('/')[-1])
    if ((Test-Path -LiteralPath $target) -and (Get-Sha256 $target) -eq $Sha256) { return $target }
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Say "Downloading $Url"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $part = "$target.part"
    Invoke-WebRequest -Uri $Url -OutFile $part -UseBasicParsing
    $hash = Get-Sha256 $part
    if ($hash -ne $Sha256) {
        Remove-Item -LiteralPath $part -Force
        Fail "$Url has SHA-256 $hash, expected $Sha256; not used"
    }
    Move-Item -LiteralPath $part -Destination $target -Force
    return $target
}

function Find-Uv {
    $exe = Get-Command uv -ErrorAction SilentlyContinue
    if ($exe) { return $exe.Source }
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
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    }
    # Recorded before the game file changes, so an interruption here can be undone (a created
    # file with no installed hash yet is removed by -Uninstall).
    [void]$script:Entries.Add($entry)
    if ($script:Manifest) { Write-Manifest $script:Manifest }
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
    if (Get-Field $manifest 'packages') { Uninstall-CarPackages }
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
# Its files are only safe to drop when the game files are vanilla; with -Force a game
# file that isn't vanilla is put back from a leftover backup that is (a merged install's
# archive left in place would hold the cars the packages add).
function Resolve-LeftoverBackup {
    if (-not (Test-Path -LiteralPath $BackupDir)) { return }
    Say "Found $BackupDir without an install in place:"
    foreach ($file in @(Get-ChildItem -LiteralPath $BackupDir -Recurse -File -Force)) {
        $relative = $file.FullName.Substring($BackupDir.Length + 1)
        if ($relative -like 'install*.json*') { continue }
        $target = Join-Path $Game $relative
        $vanilla = $VanillaHashes[$relative]
        $backupHash = Get-Sha256 $file.FullName
        $gameHash = $null
        if (Test-Path -LiteralPath $target) { $gameHash = Get-Sha256 $target }
        if ($gameHash -eq $backupHash) { Say "  $relative = the game's file"; continue }
        if ($vanilla -and $gameHash -eq $vanilla) { Say "  $relative : the game's file is vanilla (backup differs; not needed)"; continue }
        if ($vanilla -and $backupHash -eq $vanilla) {
            if (-not $Force) {
                Fail ("$relative in the game is not vanilla, but $BackupDir holds the vanilla file. " +
                      "Add -Force to put it back, or copy it back yourself.")
            }
            Copy-Item -LiteralPath $file.FullName -Destination $target -Force
            if ((Get-Sha256 $target) -ne $vanilla) { Fail "could not restore $relative from the leftover backup" }
            Say "  restored $relative from the leftover backup (-Force)"
            continue
        }
        Say "  $relative : differs from the game's file and isn't a known vanilla file; left in the backup folder"
    }
    $manifest = Read-Manifest
    if ($manifest) {
        Move-Item -LiteralPath $ManifestPath -Destination ("$ManifestPath." + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.old') -Force
    }
}

# --- status ---------------------------------------------------------------------------------

function Show-Status {
    $manifest = Read-Manifest
    $archive = Join-Path $Game 'Install\dngvehicles.sp'
    $hash = Get-Sha256 $archive
    $state = 'unknown (not the 1.04 vanilla archive)'
    if ($hash -eq $VanillaArchiveHash) { $state = 'vanilla 1.04' }
    elseif ($manifest) {
        foreach ($e in @($manifest.entries)) {
            if ($e.path -eq 'Install\dngvehicles.sp' -and $e.installed_sha256 -eq $hash) { $state = 'a merged dsfforza build (as installed)' }
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
    if (Get-Field $manifest 'packages') {   # (merged installs, before car packages, have no such field)
        Say "dsfforza:  $state $($manifest.installed) ($(@($manifest.cars).Count) car packages; IDs: uv run dsff car_manager --game '$Game' status)"
    } else {
        Say "dsfforza:  $state $($manifest.installed), merged ($(@($manifest.cars).Count) cars, IDs $($manifest.ids)); the next install moves them to car packages on the same IDs"
    }
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
            $archive = Get-PinnedDownload $HookUrl $HookSha256 'dnghook-0.7.2'
        } else {
            Fail ("DriverNGHook 0.7.2 is needed. Download DNGHook_072.7z from " +
                  "https://github.com/ReHamster/DriverNGToolkit/releases/tag/0.7.2 and pass -HookArchive <path>, or add -Download.")
        }
    }
    # Already unpacked (dsff install passes the folder: src/dsfforza/build/thirdparty.py unpacks the .7z).
    if (Test-Path -LiteralPath $archive -PathType Container) {
        if (-not (Test-Path -LiteralPath (Join-Path $archive 'plugins\DriverNGHook.asi'))) { Fail "$archive has no plugins\DriverNGHook.asi" }
        return (Resolve-Path -LiteralPath $archive).Path
    }
    if (-not (Test-Path -LiteralPath $archive)) { Fail "hook archive not found: $archive" }
    $out = Join-Path $env:TEMP ('dsfforza-hook-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $out | Out-Null
    $asi = Join-Path $out 'plugins\DriverNGHook.asi'
    # Windows' tar.exe reads the 7z only where its build has liblzma ("tar.exe: LZMA codec is
    # unsupported" on the others); the repo's own reader (dsff sevenzip, Python's lzma) always can.
    $why = 'no tar.exe (Windows 10 1803 or later has one)'
    $tar = Join-Path $env:WINDIR 'System32\tar.exe'
    if (Test-Path -LiteralPath $tar) {
        $pref = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'   # tar's stderr is the reason, not a stop
        try { $why = (& $tar -xf $archive -C $out 2>&1 | ForEach-Object { "$_" }) -join '; ' }
        finally { $ErrorActionPreference = $pref }
        if (-not $why) { $why = "tar.exe exit code $LASTEXITCODE" }
    }
    if (-not (Test-Path -LiteralPath $asi)) {
        $uv = Find-Uv
        if ($uv) {
            Say "tar.exe could not unpack $archive ($why); unpacking it with dsff sevenzip"
            & $uv run --project $Repo --frozen --no-dev dsff sevenzip extract $archive $out | Out-Host
        }
    }
    if (-not (Test-Path -LiteralPath $asi)) {
        Fail ("could not unpack $archive ($why). Unpack it with 7-Zip and pass the folder as -HookArchive, " +
              "or install uv (winget install astral-sh.uv) and run this again.")
    }
    return $out
}

function Get-AsiLoaderDll {
    $source = $AsiLoader
    if (-not $source) {
        if (-not $Download) {
            Fail ("No ASI loader in the game folder. DriverNGHook needs the Ultimate ASI Loader: download " +
                  "$AsiLoaderUrl and pass -AsiLoader <zip or dinput8.dll>, or add -Download.")
        }
        $source = Get-PinnedDownload $AsiLoaderUrl $AsiLoaderSha256 'ultimate-asi-loader-v9.7.4'
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

function Invoke-Build {
    $uv = Find-Uv
    if (-not $uv) { Fail 'uv is needed to build the cars: install it from https://docs.astral.sh/uv/getting-started/installation/ (winget install astral-sh.uv), open a new PowerShell window and run this again, or pass -Build <folder>. uv installs Python 3.13 and numpy for the build by itself.' }
    # The build reads the game's vehicle archive (its hosts): it must be the vanilla one.
    $archive = Join-Path $Game 'Install\dngvehicles.sp'
    $hash = Get-Sha256 $archive
    if ($hash -ne $VanillaArchiveHash) {
        Fail ("Install\dngvehicles.sp is not the vanilla 1.04 archive (SHA-256 $hash; another mod?), and the build reads it. " +
              "Restore the original (Steam/Uplay: verify files), or build elsewhere and pass -Build <folder>.")
    }
    $out = $BuildDir
    if (-not $out) { $out = Join-Path $Repo 'out\install-build' }
    $out = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($out)
    # The repo's own Python 3.13 environment (.venv, from uv.lock); --no-dev: no lint/test tools.
    # The same build as dsff install's (src/dsfforza/build/install.py). The ID policy and root
    # limit are for the merged archive the build also writes (the in-game tests run it); the
    # packages (<out>\packages\*.dsfcar) get their IDs from the car manager at install.
    $pyArgs = @('run', '--project', $Repo, '--frozen', '--no-dev', 'dsff', 'port_batch', 'build', '--dsf', $archive,
                 '--out', $out, '--gui', '--wide-glyphs', '--id-policy', 'asi', '--root-limit', '3003', '--packages')
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
    Say "  uv $($pyArgs -join ' ')"
    & $uv @pyArgs | Out-Host   # keep the build's output out of the function's return value
    if ($LASTEXITCODE -ne 0) { Fail "the build failed (exit code $LASTEXITCODE); see the output above and $out\batch-report.json" }
    return $out
}

# The car manager (src/dsfforza/build/car_manager.py) owns everything of an install besides
# what install.json records: dsfforza\cars\ (the cars' files, the plugin's list and its
# key -> ID map), the generated GUI copies in dsfforza\generated\ and packages.lua /
# packages_hud.lua. It writes no game file.
function Invoke-CarManager([string[]]$Arguments, [switch]$Optional) {
    $uv = Find-Uv
    if (-not $uv) {
        if ($Optional) { return $false }
        Fail 'uv is needed for the car manager: install it from https://docs.astral.sh/uv/getting-started/installation/ and run this again.'
    }
    $pyArgs = @('run', '--project', $Repo, '--frozen', '--no-dev', 'dsff', 'car_manager', '--game', $Game) + $Arguments
    & $uv @pyArgs | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail "the car manager failed (exit code $LASTEXITCODE); see the output above" }
    return $true
}

# Hand the build's car packages to the car manager, which picks their IDs (stable per car: an
# earlier install's, or a merged install's after Save-MergedIds).
# The folder goes to the manager, not the files: a roster's 573 paths are longer than a
# Windows command line (32767 characters). Get-BuildPackages has checked that the folder
# holds exactly the build's packages.
function Install-CarPackages([string[]]$Files) {
    $folders = @($Files | ForEach-Object { Split-Path -Parent $_ } | Sort-Object -Unique)
    if ($folders.Count -ne 1) { Fail "the car packages are in more than one folder: $($folders -join ', ')" }
    Say "  car manager: $($Files.Count) packages from $($folders[0])"
    [void](Invoke-CarManager @('install', $folders[0]))
}

# Uninstalling a package install: the manager removes its files and keeps its key -> ID map
# (dsfforza\cars\manager.json), so a later install gives each car the ID the save's flags are
# for. Without uv (the repo gone), its files are removed here and the map is left as it is.
function Uninstall-CarPackages {
    if (Invoke-CarManager @('uninstall') -Optional) { return }
    Say '  uv not found: removing the car manager''s files directly (its ID map stays)'
    $cars = Join-Path $Game 'dsfforza\cars'
    if (Test-Path -LiteralPath $cars) {
        Get-ChildItem -LiteralPath $cars | Where-Object { $_.Name -ne 'manager.json' } | Remove-Item -Recurse -Force
    }
    foreach ($relative in 'dsfforza\generated', 'Common\UserLuaScripts\dsfforza\packages.lua', 'Common\UserLuaScripts\dsfforza\packages_hud.lua') {
        $path = Join-Path $Game $relative
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force; Say "  removed  $relative" }
    }
}

# A merged install (what install.ps1 did up to v0.1.8, before car packages: the cars merged into
# copies of the game's dngvehicles.sp, gui.sp, Fonts.fchunk and global.fchunk) left these two:
# ported.lua (each car's ID and vehicleStats fields) and hud.lua (its badge, plate and picture).
$MergedLua = @('Common\UserLuaScripts\dsfforza\ported.lua', 'Common\UserLuaScripts\dsfforza\hud.lua')

# Before a merged install is removed: its cars' IDs go into the manager's map (car_manager seed
# reads its ported.lua), so the same cars come back as packages on the same IDs and the save's
# owned/unlocked flags (kept per model ID) stay theirs.
function Save-MergedIds {
    if (-not (Test-Path -LiteralPath (Join-Path $Game $MergedLua[0]))) { return }
    Say '  recording the merged install''s car IDs for the packages (car_manager seed)'
    [void](Invoke-CarManager @('seed'))
}

# After a merged install is removed: its ported.lua and hud.lua, if they are still there (its
# uninstall keeps a file that changed since it was installed). register.lua would otherwise give
# vehicleStats entries to cars no archive has, at IDs the packages now use.
function Remove-MergedLua {
    foreach ($relative in $MergedLua) {
        $path = Join-Path $Game $relative
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force; Say "  removed  $relative (the merged install's)" }
    }
}

# A build's car packages, checked: only one build's complete set (port_batch writes a
# packaging error to its report and removes the packages), one package per car of its manifest
# and no more (install.py package_problem checks the same). Called before an earlier install is
# removed when -Build is given, so a bad build leaves the game as it was.
function Get-BuildPackages([string]$Folder) {
    # -Force: hidden files too, as the car manager installs every .dsfcar file in the folder.
    $packageFiles = @(Get-ChildItem -LiteralPath (Join-Path $Folder 'packages') -Filter '*.dsfcar' -File -Force -ErrorAction SilentlyContinue |
                      ForEach-Object { $_.FullName })
    if (-not $packageFiles) { Fail "no car packages in $Folder\packages: build again (uv run dsff install, or port_batch build --gui --packages)." }
    if (-not (Test-Path -LiteralPath (Join-Path $Folder 'dngvehicles.json'))) { Fail "build folder incomplete: $Folder\dngvehicles.json missing" }
    $buildManifest = Read-BuildManifest (Join-Path $Folder 'dngvehicles.json')
    # Only one build's complete set: its packaging must have worked (port_batch writes the
    # error to its report and removes the packages), and there must be one package per car
    # of its manifest, no more (install.py package_problem checks the same).
    $reportPath = Join-Path $Folder 'batch-report.json'
    if (Test-Path -LiteralPath $reportPath) {
        $timings = Get-Field (Read-BuildManifest $reportPath) 'timings_s'   # (no ConvertFrom-Json size limit)
        $packError = if ($timings) { Get-Field $timings 'packages_error' } else { $null }
        if ($packError) { Fail "the build's car packages failed ($packError): build again with --packages." }
    }
    $want = @($buildManifest['cars'] | ForEach-Object { 'forza.{0}.{1}' -f ([string]$_['game']).ToLower(), $_['media'] }) | Sort-Object
    $have = @($packageFiles | ForEach-Object { [IO.Path]::GetFileNameWithoutExtension($_) }) | Sort-Object
    $diff = @(Compare-Object -ReferenceObject $want -DifferenceObject $have)
    if ($diff.Count) {
        $missing = @($diff | Where-Object { $_.SideIndicator -eq '<=' } | ForEach-Object { $_.InputObject })
        $extra = @($diff | Where-Object { $_.SideIndicator -eq '=>' } | ForEach-Object { $_.InputObject })
        Fail ("$Folder\packages doesn't match the build: $($missing.Count) cars without a package " +
              "($(($missing | Select-Object -First 3) -join ', ')), $($extra.Count) packages of other cars " +
              "($(($extra | Select-Object -First 3) -join ', ')). Build again with --packages.")
    }
    return ,$packageFiles
}

function Invoke-Install {
    $existing = Read-Manifest
    $lua = Join-Path $Repo 'tools\game\lua'

    # Everything that can stop the install is checked before the game folder changes.
    if ($Build) { [void](Get-BuildPackages (Resolve-Path -LiteralPath $Build).Path) }
    if (-not (Find-Uv)) {
        Fail 'uv is needed for the car manager: install it from https://docs.astral.sh/uv/getting-started/installation/ (winget install astral-sh.uv), open a new PowerShell window and run this again.'
    }
    # dsfforza.asi's [archive] packages loads the cars: no install without it.
    $asiFile = $AsiPath
    if (-not $asiFile) {
        $asiFile = Join-Path $Repo 'out\asi\dsfforza.asi'
        $bundled = Join-Path $Repo 'plugin\dsfforza.asi'   # a release zip's (src/dsfforza/build/package.py)
        if (-not (Test-Path -LiteralPath $asiFile) -and (Test-Path -LiteralPath $bundled)) { $asiFile = $bundled }
    }
    if (-not (Test-Path -LiteralPath $asiFile)) {
        Fail "dsfforza.asi not found: $asiFile (build it with tools\asi\build.ps1, or pass -AsiPath). It loads the cars."
    }
    $asiFile = (Resolve-Path -LiteralPath $asiFile).Path
    $asiIni = [IO.Path]::ChangeExtension($asiFile, '.ini')
    if (-not (Test-Path -LiteralPath $asiIni)) { $asiIni = Join-Path $Repo 'tools\asi\dsfforza.ini' }
    # [archive] packages=1: the cars from dsfforza\cars, the GUI copies from dsfforza\generated.
    $iniText = [IO.File]::ReadAllText($asiIni) -replace '(?m)^packages=0', 'packages=1'
    if ($iniText -notmatch '(?m)^packages=1') { Fail "$asiIni has no [archive] packages switch: an older plugin?" }

    $bootstrapTarget = Join-Path $Game 'Common\UserLuaScripts\bootstrap.lua'
    $ourBootstrap = Get-Sha256 (Join-Path $lua 'player\bootstrap.lua')
    # Another mod's bootstrap (Felony Rework's, ...) is replaced only with -Force. It is backed up,
    # and kept as dsfforza\chained-bootstrap.lua, which ours runs first (player\bootstrap.lua),
    # unless it is one of our own bootstraps, an older version (Test-OurBootstrap). One that only
    # calls user_open("dsfforza\\register.lua") (INSTALL.md) is chained too: ours skips that call.
    # Over an earlier install, the bootstrap that counts is the one its uninstall puts back.
    $foreignBootstrap = $false
    $ours = @()   # files the earlier install created, which its uninstall removes
    if (Test-Installed $existing) {
        foreach ($e in @($existing.entries)) {
            $path = [string](Get-Field $e 'path')
            if ((Get-Field $e 'action') -eq 'created') { $ours += $path.ToLowerInvariant() }
            elseif ($path -eq 'Common\UserLuaScripts\bootstrap.lua' -and (Get-Field $e 'original_sha256') -ne $ourBootstrap) { $foreignBootstrap = $true }
        }
    } elseif (Test-Path -LiteralPath $bootstrapTarget) {
        $foreignBootstrap = (Get-Sha256 $bootstrapTarget) -ne $ourBootstrap
    }
    if ($foreignBootstrap -and -not $Force) {
        Fail ("Common\UserLuaScripts\bootstrap.lua belongs to another mod. Add -Force to replace it: it is backed up " +
              "and restored by -Uninstall, and ours runs it first, so that mod still loads.")
    }

    # Hook prerequisites, as they will be once an earlier install (which may have brought the
    # loader and DriverNGHook) is removed.
    $hookDir = $null
    $loaderNow = Find-AsiLoader
    $loaderStays = $loaderNow -and ($ours -notcontains $loaderNow.ToLowerInvariant())
    $hookStays = (Test-Path -LiteralPath (Join-Path $Game 'plugins\DriverNGHook.asi')) -and ($ours -notcontains 'plugins\drivernghook.asi')
    if (-not $SkipHook) {
        if (-not (Test-VcRuntimeX86)) {
            Fail ("DriverNGHook needs the Microsoft Visual C++ 2015-2022 Redistributable (x86, 32-bit; the game is 32-bit, " +
                  "so the x64 one is not enough). Install it from $VcRedistUrl and run this again.")
        }
        if (-not $loaderStays -and -not $AsiLoader -and -not $Download) { [void](Get-AsiLoaderDll) }   # explains and stops
        if (-not $hookStays) { $hookDir = Get-HookFiles }
    }

    if (Test-Installed $existing) {
        $earlier = @($existing.cars | Where-Object { $_ })
        if (-not (Get-Field $existing 'packages')) {
            # A merged install of an earlier release: moved to car packages on the same IDs.
            Say "Moving the merged install of $($earlier.Count) cars to car packages: its IDs are kept (the save's flags are per ID), then it is removed."
            Save-MergedIds
        } elseif (-not $Force) {
            Fail "dsfforza is already installed ($ManifestPath). Run -Uninstall first, or add -Force to reinstall."
        } else {
            Say "Removing the earlier install first (-Force): its $($earlier.Count) cars ($((@($earlier | Select-Object -First 6) -join ', '))$(if ($earlier.Count -gt 6) { ', ...' })) are replaced by this build's, not added to."
            Say '  To have several cars, install them in one build: -Car A,B,... (or uv run dsff install --car A --car B).'
        }
        Invoke-Uninstall
    } else {
        Save-MergedIds   # a merged install's ported.lua without its record (an interrupted uninstall)
    }
    Remove-MergedLua
    Resolve-LeftoverBackup

    # After the uninstall: what it restored decides whether bootstrap.lua is another mod's.
    $chainBootstrap = $false
    if ((Test-Path -LiteralPath $bootstrapTarget) -and (Get-Sha256 $bootstrapTarget) -ne $ourBootstrap) {
        if (-not $Force) {
            Fail ("Common\UserLuaScripts\bootstrap.lua belongs to another mod. Add -Force to replace it: it is backed up " +
                  "and restored by -Uninstall, and ours runs it first, so that mod still loads.")
        }
        $chainBootstrap = -not (Test-OurBootstrap $bootstrapTarget)
    }
    $loader = Find-AsiLoader
    $hookInstalled = Test-Path -LiteralPath (Join-Path $Game 'plugins\DriverNGHook.asi')
    if (-not $SkipHook -and -not $hookInstalled -and -not $hookDir) { $hookDir = Get-HookFiles }

    $buildFolder = $Build
    if (-not $buildFolder) { $buildFolder = Invoke-Build }
    $buildFolder = (Resolve-Path -LiteralPath $buildFolder).Path
    # The car packages (port_batch --packages) go through the car manager: no archive, GUI file or
    # Lua of the build's is installed, and no game file is replaced (research/car-packages.md).
    $packageFiles = Get-BuildPackages $buildFolder
    $buildManifest = Read-BuildManifest (Join-Path $buildFolder 'dngvehicles.json')
    $builtFrom = $buildManifest['vanilla_sha256']
    if (-not $builtFrom) { $builtFrom = $buildManifest['stock_sha256'] }   # builds before v0.1.3 name it so
    if ($builtFrom -and $builtFrom -ne $VanillaArchiveHash) { Fail 'the build was made from a different vanilla archive' }
    Say "Installing $($packageFiles.Count) car packages into $Game (the car manager picks their IDs)"

    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $createdDirs = New-Object System.Collections.ArrayList
    foreach ($d in @('plugins', 'plugins\DriverNGHook', 'Common\UserLuaScripts', 'Common\UserLuaScripts\dsfforza')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Game $d))) { [void]$createdDirs.Insert(0, $d) }
    }
    # packages: true tells this install from a merged one (an earlier release's record has no such field).
    $manifest = [ordered]@{
        state = 'installing'; installed = (Get-Date -Format 's'); build = $buildFolder; ids = ''
        cars = @($packageFiles | ForEach-Object { [IO.Path]::GetFileNameWithoutExtension($_) })
        entries = $script:Entries; created_dirs = $createdDirs; runtime_files = @('plugins\dsfforza.log')
        packages = $true
    }
    $script:Manifest = $manifest
    Write-Manifest $manifest
    try {
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

        Install-File $asiFile 'plugins\dsfforza.asi'
        Install-Text $iniText 'plugins\dsfforza.ini'

        if ($chainBootstrap) {
            Install-File $bootstrapTarget 'Common\UserLuaScripts\dsfforza\chained-bootstrap.lua'
            Say "  another mod's bootstrap.lua kept as dsfforza\chained-bootstrap.lua; ours runs it first"
        }
        Install-File (Join-Path $lua 'player\bootstrap.lua') 'Common\UserLuaScripts\bootstrap.lua'
        Install-File (Join-Path $lua 'dsfforza\register.lua') 'Common\UserLuaScripts\dsfforza\register.lua'
        if ($WriteProfile) {
            Install-Text "-- written by tools\install.ps1 -WriteProfile`nreturn { register = { writeProfile = true } }`n" 'Common\UserLuaScripts\dsfforza\config.lua'
        }
        Install-CarPackages $packageFiles
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
