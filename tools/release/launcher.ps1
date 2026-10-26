<#
.SYNOPSIS
The PowerShell half of install.cmd: makes sure uv is installed and DSF_GAME_DIR names the
game folder, then runs `uv run dsff install` with install.cmd's arguments.

.DESCRIPTION
- uv (https://docs.astral.sh/uv/) runs the installer; it brings its own Python 3.13 and
  the locked dependencies (uv.lock) into .venv next to this folder. When uv is missing it
  is installed with Astral's official installer, pinned to $UvVersion (into
  %USERPROFILE%\.local\bin).
- The game folder comes from DSF_GAME_DIR (this window's, else the one saved for your
  user). When neither holds Driver.exe the script asks once and saves the answer as your
  user's DSF_GAME_DIR. `--game DIR` skips all of this.

.EXAMPLE
install.cmd
install.cmd --uninstall
install.cmd --help
#>
$ErrorActionPreference = 'Stop'
$UvVersion = '0.13.5'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Forward = @($args)

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($dir in @((Join-Path $env:USERPROFILE '.local\bin'), (Join-Path $env:USERPROFILE '.cargo\bin'))) {
        $exe = Join-Path $dir 'uv.exe'
        if (Test-Path -LiteralPath $exe) { return $exe }
    }
    return $null
}

function Test-Game([string]$Path) {
    return ($Path -and (Test-Path -LiteralPath (Join-Path $Path 'Driver.exe')))
}

$uv = Find-Uv
if (-not $uv) {
    Write-Host "uv (it runs the installer and brings its own Python) is not installed."
    Write-Host "Installing uv $UvVersion with the official installer from https://astral.sh/uv ..."
    $ErrorActionPreference = 'Continue'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/$UvVersion/install.ps1 | iex"
    $ErrorActionPreference = 'Stop'
    $uv = Find-Uv
    if (-not $uv) {
        throw 'uv could not be installed. Install it yourself (winget install astral-sh.uv), open a new window and run install.cmd again.'
    }
}

$help = $Forward | Where-Object { $_ -in @('-h', '--help') }
if (-not ($Forward -contains '--game') -and -not $help) {
    $game = $env:DSF_GAME_DIR
    if (-not (Test-Game $game)) { $game = [Environment]::GetEnvironmentVariable('DSF_GAME_DIR', 'User') }
    if (-not (Test-Game $game)) {
        if ($game) { Write-Host "DSF_GAME_DIR is $game, but there is no Driver.exe there." }
        $asked = $null
        for ($try = 0; $try -lt 5 -and -not (Test-Game $asked); $try++) {
            $asked = (Read-Host 'Driver: San Francisco folder (the one with Driver.exe)').Trim().Trim('"')
            if (-not (Test-Game $asked)) { Write-Host "No Driver.exe in '$asked'." }
        }
        if (-not (Test-Game $asked)) { throw 'No game folder given.' }
        $game = (Resolve-Path -LiteralPath $asked).Path
        [Environment]::SetEnvironmentVariable('DSF_GAME_DIR', $game, 'User')
        Write-Host "Saved as your DSF_GAME_DIR: $game"
    }
    $env:DSF_GAME_DIR = $game
}

# uv reports progress on stderr; Windows PowerShell must not treat that as an error.
$ErrorActionPreference = 'Continue'
& $uv run --project $Root --frozen --no-dev dsff install @Forward
exit $LASTEXITCODE
