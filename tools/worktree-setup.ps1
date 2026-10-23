<#
.SYNOPSIS
Links a git worktree to the main checkout's untracked inputs: the Forza disc
images (assets\), the stock game copy (out\stock), the extraction cache
(out\cache) and the reference downloads (_ref). Run once in a new worktree:

    .\tools\worktree-setup.ps1

Junctions, so nothing is copied; the main checkout stays the only real copy.
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
foreach ($rel in 'assets', 'out\stock', 'out\cache', '_ref') {
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
    $target = Join-Path $here $rel
    if (-not (Test-Path $source)) { "skip $rel (not in the main checkout)"; continue }
    if (Test-Path $target) { "keep $rel (exists)"; continue }
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    New-Item -ItemType Junction -Path $target -Target $source | Out-Null
    "linked $rel -> $source"
}
