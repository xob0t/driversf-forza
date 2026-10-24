<#
.SYNOPSIS
Links a git worktree to the main checkout's untracked inputs: the Forza disc
images (assets\), the stock game copy (out\stock), the extraction cache
(out\cache), the downloaded tools (out\tools-dl: vgmstream for the Forza
engine sound) and the reference downloads (_ref). Then it writes the derived
game data the tools read (out\lua-src\...\vehiclestats.lua, out\db\*.slt)
with `uv run dsff game_data prepare` (uv makes the worktree's .venv). Run once
in a new worktree:

    .\tools\worktree-setup.ps1

Junctions, so nothing is copied; the main checkout stays the only real copy.
out\lua-src and out\db are not linked: prepare writes them, so each worktree
gets its own (a few MB) and a `prepare --force` in one worktree can't change
another's files while it builds. port_batch runs prepare too, so this step only
matters for tools run on their own (host_select, handling_dsf, the tests).

Before removing the worktree, run `.\tools\worktree-setup.ps1 -Unlink`: some
delete tools (PowerShell 5.1's Remove-Item -Recurse among them) follow
junctions and would empty the main checkout's folders.
#>
param([switch]$Unlink)
$ErrorActionPreference = 'Stop'
$here = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
# The main checkout is the first entry of `git worktree list`.
$main = ((git -C $here worktree list --porcelain) | Where-Object { $_ -like 'worktree *' } | Select-Object -First 1).Substring(9) -replace '/', '\'
if ((Resolve-Path $main).Path -eq $here) { "this is the main checkout; nothing to link"; return }
foreach ($rel in 'assets', 'out\stock', 'out\cache', 'out\tools-dl', '_ref') {
    $target = Join-Path $here $rel
    if ($Unlink) {
        $item = Get-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            [IO.Directory]::Delete($target)   # removes the link only
            "unlinked $rel"
        }
        continue
    }
    $source = Join-Path $main $rel
    if (-not (Test-Path $source)) { "skip $rel (not in the main checkout)"; continue }
    if (Test-Path $target) { "keep $rel (exists)"; continue }
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    New-Item -ItemType Junction -Path $target -Target $source | Out-Null
    "linked $rel -> $source"
}
if ($Unlink) { return }
# Derived data (only missing files; reads the game folder, $env:DSF_GAME_DIR or
# the default, and the disc images). A failure only warns: the links are made.
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { Write-Warning "uv not found (https://docs.astral.sh/uv/); run 'uv run dsff game_data prepare' later"; return }
& $uv.Source run --project $here dsff game_data prepare
if ($LASTEXITCODE -ne 0) { Write-Warning "game_data prepare failed (exit $LASTEXITCODE); see above" }
